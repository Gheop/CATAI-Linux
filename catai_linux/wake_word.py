"""Wake-word listener — chaque chat répond à son propre prénom.

Optional feature for CATAI-Linux. Loaded only when ``vosk`` is installed
(``pip install catai-linux[voice]``). The module-level flag
``WAKE_AVAILABLE`` reflects whether the import succeeded — callers should
check it before instantiating ``WakeWordListener``.

Why Vosk?
    - **Apache-2.0** — no non-commercial trap (unlike openWakeWord +
      Picovoice Porcupine).
    - **No retraining** — wake words are passed as a JSON grammar list
      that we rebuild on every rename. Adding/removing a cat is free.
    - **Offline** — no API key, no network beyond first-launch model
      download (~41 MB for the small French model).
    - **Stream-friendly** — we feed PCM int16 chunks via
      ``KaldiRecognizer.AcceptWaveform`` and read JSON results out.

Architecture
------------

Two threads cooperate:

1. **GStreamer capture pipeline** (parallel to ``voice.py`` PTT):
   ``autoaudiosrc → audioconvert → audioresample → S16LE 16kHz mono →
   appsink``. The appsink ``new-sample`` callback runs on a GStreamer
   thread; it pulls the buffer, copies the bytes, and enqueues them.

2. **Vosk worker thread**: dequeues PCM chunks and feeds them to a
   ``KaldiRecognizer`` whose grammar is a closed list of normalized
   cat names. When the recognizer reports a final result whose text
   matches a cat name, we ``GLib.idle_add(self.on_wake, cat_id)`` so
   the high-level callback runs on the GTK main thread.

Interaction with push-to-talk
-----------------------------

``autoaudiosrc`` is exclusive on most Linux audio backends. Whenever the
user is doing a normal push-to-talk recording via ``voice.VoiceRecorder``,
we must release the device. The high-level app calls
``WakeWordListener.pause()`` before starting a PTT recording and
``resume()`` once it's done. We use ``Gst.State.PAUSED`` so the worker
thread keeps its model loaded — restart is essentially instant.

Renaming live
-------------

When the user renames a cat from Settings (``rename_catset_char`` in
``app.py``), the higher-level app re-calls ``set_names()`` with the
fresh mapping. We rebuild the ``KaldiRecognizer`` with the new grammar
under a lock so the worker can't race against an in-flight
``AcceptWaveform``.

First-launch model download
---------------------------

The Vosk small-FR model isn't shipped with the package — it's downloaded
on first ``start()`` to ``~/.cache/catai/vosk/``. The download runs on
a daemon thread; ``start()`` returns immediately and the pipeline kicks
in once the zip has been extracted.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import threading
import time
import unicodedata
import urllib.request
import zipfile
from collections.abc import Callable

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

log = logging.getLogger("catai")

# Wrap the import — vosk is an optional dep
try:
    from vosk import KaldiRecognizer, Model, SetLogLevel  # type: ignore
    SetLogLevel(-1)  # silence Kaldi's chatty stderr
    WAKE_AVAILABLE = True
except ImportError:
    KaldiRecognizer = None  # type: ignore
    Model = None  # type: ignore
    WAKE_AVAILABLE = False

# Ensure GStreamer is initialized — voice.py also calls this, both are
# safe to call multiple times.
Gst.init(None)


# ── Constants ────────────────────────────────────────────────────────────────

VOSK_MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-fr-0.22.zip"
VOSK_MODEL_NAME = "vosk-model-small-fr-0.22"
VOSK_CACHE_DIR = os.path.expanduser("~/.cache/catai/vosk")
VOSK_MODEL_DIR = os.path.join(VOSK_CACHE_DIR, VOSK_MODEL_NAME)

SAMPLE_RATE = 16000

# Minimum normalized name length — anything shorter is rejected because
# Vosk struggles to disambiguate single phonemes from background noise.
MIN_NAME_LEN = 3

# Cooldown between successive triggers for the same cat (seconds). The
# user saying "Mandarine" twice in a row should fire twice; saying it
# once shouldn't fire 5x because Vosk happens to also report partials.
COOLDOWN_S = 2.0

# ── Direct command verbs ─────────────────────────────────────────────────────
#
# When the user says a cat's name FOLLOWED BY one of these verbs, we
# fire on_wake(cat_id, verb=<verb>) instead of the default
# on_wake(cat_id, verb=None) chat-opening behavior. Each verb is added
# to the Vosk grammar so the recognizer can transcribe full phrases
# like "mandarine dors".
#
# Adding a new verb is two lines: list it here AND wire it in the
# higher-level _on_wake_word_heard() callback.
#
# All verbs are normalized identically to cat names — lowercase, no
# accents, no punctuation. So "danse" matches "Danse", "DANSE", and
# (more importantly) Vosk's `[unk]` model variants.
COMMAND_VERBS: tuple[str, ...] = (
    "dors",      # cat.state = SLEEPING_BALL
    "viens",     # walk to mouse cursor
    "raconte",   # open chat + ask for an anecdote
    "danse",     # mini-disco loop on this one cat
    "saute",     # JUMPING animation
    "roule",     # ROLLING animation
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _normalize_name(name: str) -> str:
    """Lowercase, strip accents, collapse to a single token. The Vosk
    grammar matches against this canonical form so 'Mandarine',
    'mandarine' and 'Mandariné' all behave the same."""
    if not name:
        return ""
    nfd = unicodedata.normalize("NFD", name)
    ascii_only = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return "".join(c for c in ascii_only.lower() if c.isalpha())


def _model_present() -> bool:
    """Return True if the Vosk model directory looks ready to use.
    We check for a few sentinel files inside (am/final.mdl + conf/),
    not just the directory existence — a partial extraction would
    crash Model() at startup."""
    if not os.path.isdir(VOSK_MODEL_DIR):
        return False
    sentinels = [
        os.path.join(VOSK_MODEL_DIR, "am", "final.mdl"),
        os.path.join(VOSK_MODEL_DIR, "conf"),
    ]
    return all(os.path.exists(p) for p in sentinels)


def download_model_blocking(progress_cb: Callable[[int, int], None] | None = None) -> bool:
    """Download + unzip the Vosk small-FR model. Returns True on success.

    Safe to call multiple times: a no-op if the model is already on disk.
    Atomic: download to ``.zip.tmp``, unzip to ``<name>.tmp/``, then
    rename to the final dir, so a crash mid-download leaves a clean
    state.

    ``progress_cb`` is invoked from the download thread with
    ``(bytes_downloaded, total_bytes)``. Use it from the UI to render
    a progress bar; otherwise pass None.
    """
    if _model_present():
        return True
    try:
        os.makedirs(VOSK_CACHE_DIR, exist_ok=True)
        zip_tmp = os.path.join(VOSK_CACHE_DIR, VOSK_MODEL_NAME + ".zip.tmp")
        log.warning("WAKE: downloading Vosk model from %s", VOSK_MODEL_URL)
        with urllib.request.urlopen(VOSK_MODEL_URL, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            done = 0
            with open(zip_tmp, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb is not None:
                        try:
                            progress_cb(done, total)
                        except Exception:
                            pass
        log.warning("WAKE: download done (%d bytes), extracting...", done)
        extract_tmp = VOSK_MODEL_DIR + ".tmp"
        if os.path.isdir(extract_tmp):
            shutil.rmtree(extract_tmp, ignore_errors=True)
        os.makedirs(extract_tmp, exist_ok=True)
        with zipfile.ZipFile(zip_tmp) as zf:
            zf.extractall(extract_tmp)
        # The zip contains a top-level dir matching VOSK_MODEL_NAME;
        # move it into place.
        inner = os.path.join(extract_tmp, VOSK_MODEL_NAME)
        if os.path.isdir(inner):
            shutil.move(inner, VOSK_MODEL_DIR)
            shutil.rmtree(extract_tmp, ignore_errors=True)
        else:
            # Some mirrors flatten the zip — fall back to renaming
            # the temp dir.
            os.rename(extract_tmp, VOSK_MODEL_DIR)
        try:
            os.remove(zip_tmp)
        except OSError:
            pass
        log.warning("WAKE: model ready at %s", VOSK_MODEL_DIR)
        return True
    except Exception:
        log.exception("WAKE: model download failed")
        return False


# ── Listener ─────────────────────────────────────────────────────────────────


class WakeWordListener:
    """Continuous listener that fires ``on_wake(cat_id)`` whenever the
    user says one of the registered cat names.

    Lifecycle::

        listener = WakeWordListener(on_wake=app._on_wake_word_heard)
        listener.set_names({'cat_orange': 'Mandarine', 'cat01': 'Tabby'})
        listener.start()
        ...
        listener.pause()        # before starting a PTT recording
        listener.resume()       # after PTT done
        ...
        listener.stop()         # on shutdown

    All public methods are safe to call from the GTK main thread; the
    callback ``on_wake`` is also delivered on the GTK main thread via
    ``GLib.idle_add``.
    """

    def __init__(self, on_wake: Callable[[str], None]):
        self.on_wake = on_wake
        self._names: dict[str, str] = {}  # normalized → cat_id
        self._model = None
        self._recognizer = None
        self._pipeline: Gst.Pipeline | None = None
        self._appsink = None
        self._worker: threading.Thread | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self._running = False
        self._paused = False
        self._lock = threading.RLock()
        self._last_fire: dict[str, float] = {}

    # ── public API ──────────────────────────────────────────────────────────

    def set_names(self, mapping: dict[str, str]) -> None:
        """Replace the active wake-word list. ``mapping`` is keyed by
        ``cat_id`` and valued by the cat's display name. Names < 3
        normalized chars are rejected with a debug log; duplicates
        keep the first cat_id (and log a warning)."""
        normalized: dict[str, str] = {}
        for cat_id, raw_name in mapping.items():
            n = _normalize_name(raw_name)
            if len(n) < MIN_NAME_LEN:
                log.debug("WAKE: skipping short name %r for %s", raw_name, cat_id)
                continue
            if n in normalized:
                log.warning("WAKE: duplicate normalized name %r (%s and %s) — keeping %s",
                            n, normalized[n], cat_id, normalized[n])
                continue
            normalized[n] = cat_id
        with self._lock:
            self._names = normalized
            if self._model is not None:
                self._rebuild_recognizer_locked()
        log.debug("WAKE: active names = %s", list(normalized.keys()))

    def start(self) -> None:
        """Begin listening. If the Vosk model isn't downloaded yet,
        kick off a background download and start the pipeline once
        it's ready. Idempotent."""
        if not WAKE_AVAILABLE:
            log.debug("WAKE: vosk not installed, listener disabled")
            return
        with self._lock:
            if self._running:
                return
        if not _model_present():
            log.warning("WAKE: model not on disk, downloading in background")
            threading.Thread(target=self._download_then_start,
                             daemon=True).start()
            return
        self._load_model_and_start()

    def stop(self) -> None:
        """Tear down pipeline + worker. Safe even if already stopped."""
        with self._lock:
            self._running = False
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            self._pipeline = None
            self._appsink = None
        # Sentinel wakes up the worker so it can exit cleanly.
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=1.0)
        self._worker = None
        log.debug("WAKE: stopped")

    def pause(self) -> None:
        """Release the audio device temporarily (e.g. before a
        push-to-talk recording grabs the same autoaudiosrc). Keeps
        the model and worker alive — ``resume()`` is fast."""
        with self._lock:
            if not self._running or self._paused or self._pipeline is None:
                return
            self._paused = True
        try:
            # NULL fully releases the device. PAUSED isn't enough — on
            # PulseAudio, autoaudiosrc keeps the source allocated until
            # the pipeline goes NULL.
            self._pipeline.set_state(Gst.State.NULL)
        except Exception:
            log.debug("WAKE: pause set_state failed", exc_info=True)
        log.debug("WAKE: paused")

    def resume(self) -> None:
        """Re-acquire the audio device after a pause()."""
        with self._lock:
            if not self._running or not self._paused or self._pipeline is None:
                self._paused = False
                return
            self._paused = False
        try:
            self._pipeline.set_state(Gst.State.PLAYING)
        except Exception:
            log.debug("WAKE: resume set_state failed", exc_info=True)
        log.debug("WAKE: resumed")

    @property
    def is_running(self) -> bool:
        return self._running and not self._paused

    # ── internals ───────────────────────────────────────────────────────────

    def _download_then_start(self) -> None:
        ok = download_model_blocking()
        if not ok:
            log.warning("WAKE: download failed, listener disabled")
            return
        # Hop back to GTK main thread to start the pipeline so all
        # GLib operations stay on one thread.
        GLib.idle_add(self._load_model_and_start)

    def _load_model_and_start(self) -> bool:
        try:
            log.warning("WAKE: loading Vosk model %s", VOSK_MODEL_DIR)
            t0 = time.monotonic()
            self._model = Model(VOSK_MODEL_DIR)
            log.warning("WAKE: model loaded in %.1fs", time.monotonic() - t0)
        except Exception:
            log.exception("WAKE: failed to load Vosk model")
            self._model = None
            return False
        with self._lock:
            self._rebuild_recognizer_locked()
            self._running = True
        self._start_pipeline()
        self._start_worker()
        return False  # for idle_add

    def _rebuild_recognizer_locked(self) -> None:
        """Build a fresh KaldiRecognizer with the current grammar.
        Caller must hold ``self._lock``.

        Grammar = cat names + command verbs + ``[unk]``. Vosk happily
        composes them, so the user can say either ``"mandarine"`` or
        ``"mandarine dors"`` and both transcribe correctly."""
        if self._model is None:
            return
        names = list(self._names.keys())
        if not names:
            self._recognizer = None
            return
        # Closed grammar = whitelist of phrases. ``[unk]`` lets Vosk
        # report "unknown" instead of forcing a misclassification on
        # background noise. Verbs are added so phrases like
        # "mandarine dors" can be transcribed in one shot.
        grammar = json.dumps(names + list(COMMAND_VERBS) + ["[unk]"])
        try:
            self._recognizer = KaldiRecognizer(self._model, SAMPLE_RATE, grammar)
        except Exception:
            log.exception("WAKE: KaldiRecognizer build failed")
            self._recognizer = None

    def _start_pipeline(self) -> None:
        """Build the parallel GStreamer capture pipeline. Uses appsink
        with sync=false + drop=true so we don't block on slow consumers
        (the worker can fall behind without backpressuring the mic)."""
        desc = (
            "autoaudiosrc ! "
            "audioconvert ! "
            "audioresample ! "
            "audio/x-raw,format=S16LE,channels=1,rate=16000 ! "
            "appsink name=sink emit-signals=true sync=false drop=true max-buffers=4"
        )
        try:
            self._pipeline = Gst.parse_launch(desc)
        except Exception:
            log.exception("WAKE: failed to parse pipeline")
            self._pipeline = None
            return
        self._appsink = self._pipeline.get_by_name("sink")
        if self._appsink is None:
            log.warning("WAKE: appsink not found in pipeline")
            return
        self._appsink.connect("new-sample", self._on_new_sample)
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        log.warning("WAKE: pipeline state change = %s", ret)

    def _on_new_sample(self, sink) -> int:
        """GStreamer callback — pull a buffer, copy bytes, enqueue."""
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            # Drop the chunk — better to skip than to back up the
            # capture thread. Logged at debug to avoid spam.
            log.debug("WAKE: queue full, dropping %d bytes", len(data))
        return Gst.FlowReturn.OK

    def _start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop, name="catai-wake", daemon=True
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        """Background thread: pull PCM bytes, feed Vosk, fire callbacks."""
        log.debug("WAKE: worker thread started")
        while True:
            try:
                chunk = self._queue.get(timeout=0.5)
            except queue.Empty:
                if not self._running:
                    break
                continue
            if chunk is None:
                break
            if not self._running:
                continue
            with self._lock:
                rec = self._recognizer
            if rec is None:
                continue
            try:
                if rec.AcceptWaveform(chunk):
                    raw = rec.Result()
                    self._handle_result(raw)
                # Partial results are ignored — we only want clean
                # one-word completions, not "manda… mandari… mandarine".
            except Exception:
                log.exception("WAKE: AcceptWaveform crashed")
        log.debug("WAKE: worker thread exiting")

    def _handle_result(self, raw_json: str) -> None:
        try:
            payload = json.loads(raw_json)
        except (ValueError, TypeError):
            return
        text = (payload.get("text") or "").strip().lower()
        if not text:
            return
        # Vosk may emit multi-word phrases like "ok mandarine dors" —
        # walk the tokens, find the cat name, then look ahead 1-2
        # tokens for an optional verb.
        tokens = text.split()
        with self._lock:
            names = dict(self._names)  # snapshot
        cat_id: str | None = None
        verb: str | None = None
        for i, tok in enumerate(tokens):
            tok_n = _normalize_name(tok)
            if tok_n in names:
                cat_id = names[tok_n]
                # Look ahead up to 2 tokens for a verb. Stops at the
                # first verb match — the user normally says
                # "<name> <verb>" or "<name>" alone.
                for j in range(i + 1, min(i + 3, len(tokens))):
                    vtok = _normalize_name(tokens[j])
                    if vtok in COMMAND_VERBS:
                        verb = vtok
                        break
                break
        if cat_id is None:
            return
        now = time.monotonic()
        last = self._last_fire.get(cat_id, 0.0)
        if now - last < COOLDOWN_S:
            log.debug("WAKE: cooldown swallowed %s", cat_id)
            return
        self._last_fire[cat_id] = now
        if verb:
            log.warning("WAKE: heard %r → %s + verb %r", text, cat_id, verb)
        else:
            log.warning("WAKE: heard %r → %s", text, cat_id)
        try:
            GLib.idle_add(self._fire, cat_id, verb)
        except Exception:
            log.exception("WAKE: idle_add failed")

    def _fire(self, cat_id: str, verb: str | None = None) -> bool:
        """Trampoline to the user-supplied callback. Tolerates two
        signatures for backward compatibility:
            on_wake(cat_id)
            on_wake(cat_id, verb)
        Older callers that take a single arg keep working when no
        verb was detected."""
        try:
            try:
                self.on_wake(cat_id, verb)
            except TypeError:
                # Caller has the legacy single-arg signature.
                self.on_wake(cat_id)
        except Exception:
            log.exception("WAKE: on_wake callback crashed")
        return False  # one-shot idle

    # ── test hook ───────────────────────────────────────────────────────────

    def _test_inject_pcm(self, pcm: bytes) -> None:
        """Bypass GStreamer and feed raw PCM directly. Used by the
        unit test suite to drive the recognizer with synthesized
        speech without requiring a real microphone.

        ``pcm`` must be 16-bit little-endian mono samples at 16 kHz.
        """
        with self._lock:
            rec = self._recognizer
        if rec is None:
            log.debug("WAKE: no recognizer for inject")
            return
        # Feed in 4 KB slices so AcceptWaveform behaves like the live
        # path (it returns True only at chunk boundaries that contain
        # a final endpoint, otherwise it accumulates).
        STEP = 4096
        for i in range(0, len(pcm), STEP):
            slice_ = pcm[i:i + STEP]
            try:
                rec.AcceptWaveform(slice_)
            except Exception:
                log.exception("WAKE: inject AcceptWaveform crashed")
                return
        # Flush whatever's left and parse the final result.
        try:
            self._handle_result(rec.FinalResult())
        except Exception:
            log.exception("WAKE: inject FinalResult crashed")
