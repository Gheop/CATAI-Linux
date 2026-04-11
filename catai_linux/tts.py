"""Voice output for CATAI — hybrid pipeline.

Cats can speak their chat responses out loud. The naive approach of
running an entire LLM response through a TTS engine produces a human
pronouncing ``"Miaou"`` phonetically, which sounds wrong and breaks
the immersion. Instead we split each response into alternating
text / cat-sound chunks and play each chunk through the right backend:

    text chunk  → Piper TTS (soft dependency, opt-in)
    cat chunk   → one of the CC0 real-cat WAV samples in
                  ``catai_linux/sounds/`` played via GStreamer

The splitter is a **pure function**, easily unit-testable without
touching GStreamer or Piper. The playback pipeline is soft-wired: on
a system without Piper installed, text chunks are silently skipped and
only the cat sounds play — still better than nothing and makes the
``[voice]`` extra truly optional.

Design notes:
    - No classes for backends yet; a single ``SoundPlayer`` is enough.
    - Piper integration is lazy and defensive — any ImportError /
      RuntimeError falls back to cat-sounds-only mode.
    - The sample directory is resolved at import time so PyInstaller
      bundles and pip-installed wheels both find it.
    - All playback is non-blocking: GStreamer pipelines spawn in the
      background and auto-dispose on EOS.
    - Per-cat enable/disable is the caller's responsibility — this
      module doesn't know anything about ``CatInstance``.
"""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("catai")


# Directory containing the CC0 .mp3 samples. Resolved at import time so
# every caller shares the same path.
SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")

# Cache directory for downloaded Piper voice models. Voices are ~20-80 MB
# each, so we don't ship them in the wheel — they're fetched on first use
# and reused forever. Mirrors the whisper cache pattern in voice.py.
PIPER_CACHE_DIR = os.path.expanduser("~/.cache/catai/piper")

# Default voice model. French, good quality, ~74 MB. Users can override
# via ``CATAI_PIPER_VOICE`` env var (absolute path to a custom .onnx).
DEFAULT_VOICE_NAME = "fr_FR-upmc-medium"
DEFAULT_VOICE_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "fr/fr_FR/upmc/medium/fr_FR-upmc-medium.onnx"
)
DEFAULT_VOICE_CONFIG_URL = DEFAULT_VOICE_URL + ".json"


# ── Chunk type ───────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    """One segment of a split chat response.

    Attributes:
        kind: ``"text"`` (speak via Piper) or ``"cat"`` (play a sample).
        content: For ``text``, the raw phrase to synthesize. For ``cat``,
            the sample-pool key to play (see ``CAT_SOUND_POOLS``).
    """
    kind: str
    content: str


# ── Splitter ─────────────────────────────────────────────────────────────────

# Cat-sound tokens. Each entry maps a regex to a sample-pool key. The
# regex must match the WHOLE token (word-boundary anchored) and is run
# case-insensitively. Order matters — first match wins, so put the
# longer/more specific tokens first ("prrrr" before "prrt").
#
# When you add a new token, also add it to the ``CAT_SOUND_POOLS`` dict
# below so the player knows which samples to choose from.
_TOKEN_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Purrs — long/sustained sound, use purr samples
    (re.compile(r"\*(?:ronron|purr+|prr+r+|prrrr+t?)\*", re.I), "purr"),
    (re.compile(r"\bronron(?:ronron)*\b", re.I), "purr"),
    (re.compile(r"\bpurr+\b", re.I), "purr"),
    (re.compile(r"\bprr+r+t?\b", re.I), "purr"),
    # Short chirps / mrrps — small mewing samples
    (re.compile(r"\bmrr+p\b", re.I), "mrrp"),
    (re.compile(r"\bprrt\b", re.I), "mrrp"),
    (re.compile(r"\bnyaa+\b", re.I), "mrrp"),
    # Regular meows
    (re.compile(r"\bmiaou+\b", re.I), "meow"),
    (re.compile(r"\bmiaw+\b", re.I), "meow"),
    (re.compile(r"\bmeow+\b", re.I), "meow"),
    (re.compile(r"\bmiao+\b", re.I), "meow"),
    (re.compile(r"\bmew+\b", re.I), "meow"),
    # Hiss / growl
    (re.compile(r"\*hiss\*", re.I), "hiss"),
    (re.compile(r"\bhiss+\b", re.I), "hiss"),
]


def split_cat_sounds(text: str) -> list[Chunk]:
    """Split ``text`` into alternating text / cat-sound chunks.

    The splitter recognizes a small vocabulary of cat onomatopoeia
    (``miaou``, ``prrrt``, ``*ronron*``, ``mrrp``, ``mew``, …) and
    replaces each occurrence with a ``Chunk(kind="cat", content=key)``.
    Everything between tokens becomes a ``Chunk(kind="text", content=…)``
    with markdown/emoji stripped for TTS. Empty text chunks are
    skipped entirely, and consecutive cat chunks from the same sample
    pool are collapsed into a single chunk so ``*miaou miaou*`` plays
    one meow instead of two back-to-back.

    Example::

        >>> split_cat_sounds("Miaou mon ami! *ronron* ça va.")
        [Chunk('cat', 'meow'),
         Chunk('text', 'mon ami!'),
         Chunk('cat', 'purr'),
         Chunk('text', 'ça va.')]

    Empty input returns an empty list. Input with no cat tokens
    returns a single text chunk with the original string cleaned.
    """
    if not text or not text.strip():
        return []

    # Gather all token matches with their span + pool key
    matches: list[tuple[int, int, str]] = []
    for pattern, key in _TOKEN_PATTERNS:
        for m in pattern.finditer(text):
            matches.append((m.start(), m.end(), key))
    if not matches:
        cleaned = _clean_text_for_tts(text)
        return [Chunk("text", cleaned)] if cleaned else []

    # Sort by start offset. When two patterns overlap (e.g. "prrrr" is
    # matched by both the purr and mrrp patterns), keep the one that
    # appeared first in _TOKEN_PATTERNS order — we already scan in
    # priority order but multiple patterns can still match the same
    # position. Dedup by start offset, keep the LONGEST match there.
    matches.sort(key=lambda m: (m[0], -(m[1] - m[0])))
    deduped: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, key in matches:
        if start >= last_end:
            deduped.append((start, end, key))
            last_end = end

    # Walk the string and emit chunks
    chunks: list[Chunk] = []
    cursor = 0
    for start, end, key in deduped:
        if start > cursor:
            before = _clean_text_for_tts(text[cursor:start])
            if before:
                chunks.append(Chunk("text", before))
        chunks.append(Chunk("cat", key))
        cursor = end
    tail = _clean_text_for_tts(text[cursor:])
    if tail:
        chunks.append(Chunk("text", tail))

    # Collapse consecutive same-pool cat chunks. The behavior the user
    # sees: "miaou miaou miaou" → one meow, not three spaced by brief
    # pauses; "miaou *ronron*" → meow then purr (different pools, kept).
    merged: list[Chunk] = []
    for ch in chunks:
        if (merged and merged[-1].kind == "cat"
                and ch.kind == "cat"
                and merged[-1].content == ch.content):
            continue
        merged.append(ch)
    return merged


# Whitelist of characters Piper can pronounce cleanly: word characters,
# whitespace, common punctuation, French accents, and guillemets.
# Everything else (emoji, symbols) is stripped before synthesis so the
# TTS engine doesn't read "nuage pensif" literally.
_TTS_TEXT_SAFE_RE = re.compile(
    r"[^0-9A-Za-z\s.,!?;:'\"«»()\[\]\-…"
    r"àâäéèêëîïôöùûüÿç"
    r"ÀÂÄÉÈÊËÎÏÔÖÙÛÜŸÇ"
    r"œŒæÆ]",
)
# LLMs writing roleplay cat responses use *...* to mark stage directions
# — "*s'étire*", "*bâille*", "*regarde la fenêtre*". These describe cat
# ACTIONS, not dialogue, so they must never be spoken. We drop the
# whole span (contents included) before the whitelist filter runs.
# Any cat onomatopoeia inside asterisks has already been extracted by
# the splitter's _TOKEN_PATTERNS (which explicitly matches `*ronron*`,
# `*purr*`, etc. before the text slicer calls _clean_text_for_tts),
# so anything left here is definitively a stage direction.
_TTS_ACTION_RE = re.compile(r"\*[^*]*\*")
_TTS_WS_RE = re.compile(r"\s+")


def _clean_text_for_tts(text: str) -> str:
    """Strip markdown stage directions (*...*), emoji and other symbol
    characters so Piper doesn't read them phonetically. Returns a
    whitespace-collapsed stripped string — may be empty if the input
    was entirely a stage direction or emoji."""
    # First pass: remove *...* spans entirely (stage directions)
    cleaned = _TTS_ACTION_RE.sub(" ", text)
    # Second pass: drop everything else outside the whitelist
    cleaned = _TTS_TEXT_SAFE_RE.sub(" ", cleaned)
    return _TTS_WS_RE.sub(" ", cleaned).strip()


# ── Sample pools ─────────────────────────────────────────────────────────────

# Maps a pool key from the splitter to a list of sample filenames in
# SOUNDS_DIR. The player picks one at random on each invocation so the
# same token played twice doesn't sound identical.
CAT_SOUND_POOLS: dict[str, list[str]] = {
    "meow": [
        "meow_short_1.mp3",
        "meow_short_2.mp3",
        "meow_short_3.mp3",
        "meow_long.mp3",
        "meow_kitten.mp3",
    ],
    "mrrp": [
        "meow_small_1.mp3",
        "meow_small_2.mp3",
    ],
    "purr": [
        "purr_1.mp3",
        "purr_2.mp3",
    ],
    "hiss": [
        "hiss.mp3",
    ],
}


def _resolve_sample(pool_key: str) -> str | None:
    """Return a random sample path for ``pool_key`` or None if the pool
    is empty / the key is unknown. Missing files in the pool are
    silently filtered out so a partial install degrades gracefully."""
    candidates = CAT_SOUND_POOLS.get(pool_key, [])
    available = [
        os.path.join(SOUNDS_DIR, f)
        for f in candidates
        if os.path.isfile(os.path.join(SOUNDS_DIR, f))
    ]
    if not available:
        return None
    return random.choice(available)


# ── SoundPlayer — GStreamer playback ─────────────────────────────────────────

class SoundPlayer:
    """Non-blocking playback of a chunk list.

    Call ``play(chunks)`` to queue a full response. Playback runs in a
    background thread that iterates through the chunks sequentially;
    subsequent calls to ``play`` while a previous one is still running
    are queued (the thread drains its input queue before exiting).

    Handles both cat samples (always available) and text chunks via
    Piper TTS (if the soft dep is installed). On a system without
    Piper, text chunks are logged and skipped.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        # Each queue entry is (chunks, voice_params) — voice_params is
        # an optional dict with Piper SynthesisConfig fields per cat
        # (see CATSET_PERSONALITIES['<char_id>']['tts_voice']).
        self._queue: list[tuple[list[Chunk], dict | None]] = []
        # Piper voice loaded lazily on first text chunk. Shared across
        # every cat (per-cat voices are deferred to a later PR). None
        # means "not loaded yet", False means "load failed permanently".
        self._voice = None
        self._voice_download_started = False
        # Currently-playing GStreamer pipeline so stop() can yank it
        # synchronously without waiting for the chunk to finish. Set by
        # _play_file_blocking while the pipeline is in PLAYING state;
        # cleared back to None on EOS/error.
        self._active_pipeline = None
        # Cancel flag watched by the worker drain loop. When set, the
        # current chunk finishes (or is aborted via _active_pipeline)
        # and no further chunks in the current or queued batches run.
        self._cancel = False

    # ── Piper lazy load + download ───────────────────────────────────────

    def _voice_paths(self) -> tuple[str, str]:
        """Return (onnx_path, json_path) for the configured voice."""
        override = os.environ.get("CATAI_PIPER_VOICE")
        if override and os.path.isfile(override):
            return override, override + ".json"
        onnx = os.path.join(PIPER_CACHE_DIR, f"{DEFAULT_VOICE_NAME}.onnx")
        return onnx, onnx + ".json"

    def _ensure_voice_files(self) -> bool:
        """Make sure the default Piper voice files are on disk.
        Downloads ~74 MB on first use. Returns True if files are present
        after the call, False on any network failure."""
        onnx, cfg = self._voice_paths()
        if os.path.isfile(onnx) and os.path.isfile(cfg):
            return True
        os.makedirs(PIPER_CACHE_DIR, exist_ok=True)
        log.info("TTS: downloading Piper voice %s (one-time, ~74 MB)...",
                 DEFAULT_VOICE_NAME)
        try:
            urllib.request.urlretrieve(DEFAULT_VOICE_URL, onnx)
            urllib.request.urlretrieve(DEFAULT_VOICE_CONFIG_URL, cfg)
            log.info("TTS: voice model ready at %s", onnx)
            return True
        except Exception:
            log.exception("TTS: failed to download Piper voice")
            # Clean up partial downloads so a retry doesn't see stale files
            for p in (onnx, cfg):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            return False

    def _get_voice(self):
        """Lazily load the Piper voice. Returns a PiperVoice instance
        or None if unavailable (missing dep, download failed, load
        failed). Cached after the first successful load."""
        if self._voice is not None:
            return self._voice if self._voice is not False else None
        try:
            from piper import PiperVoice  # type: ignore
        except ImportError:
            log.warning("TTS: piper-tts not installed — text chunks silent")
            self._voice = False
            return None
        if not self._ensure_voice_files():
            self._voice = False
            return None
        onnx, _cfg = self._voice_paths()
        try:
            self._voice = PiperVoice.load(onnx)
            log.info("TTS: Piper voice loaded from %s", onnx)
            return self._voice
        except Exception:
            log.exception("TTS: Piper voice load failed")
            self._voice = False
            return None

    # ── Public API ───────────────────────────────────────────────────────

    def play(self, chunks: list[Chunk],
             voice_params: dict | None = None) -> None:
        """Enqueue a chunk list for playback. Non-blocking.

        ``voice_params`` is an optional dict of Piper SynthesisConfig
        fields (``speaker_id``, ``length_scale``, ``noise_scale``,
        ``noise_w_scale``) applied only to text chunks in this call —
        lets each cat speak with a distinct voice profile."""
        if not chunks:
            return
        with self._lock:
            self._cancel = False
            self._queue.append((chunks, voice_params))
            need_worker = self._worker is None or not self._worker.is_alive()
        log.warning("TTS: queued %d chunks, need_worker=%s, queue_len=%d",
                  len(chunks), need_worker, len(self._queue))
        if need_worker:
            self._worker = threading.Thread(
                target=self._drain_queue, daemon=True)
            self._worker.start()

    def stop(self) -> None:
        """Cancel any in-flight playback synchronously, drop any queued
        batches, and **block** until the worker thread has fully exited.

        Blocking the caller (typically the main GTK thread inside
        ``_start_voice_recording``) is intentional: without it, the
        voice recorder's autoaudiosrc pipeline races with the in-flight
        TTS pipeline's teardown, and the autoaudiosrc capture returns
        silence because the audio backend is still reconfiguring. The
        wait is bounded to 2 s so a stuck worker never freezes the UI.
        """
        with self._lock:
            self._cancel = True
            self._queue.clear()
            pipeline = self._active_pipeline
            worker = self._worker
        # Yank the active pipeline synchronously — this makes the
        # worker's bus loop wake up immediately instead of waiting up
        # to 500 ms for the next poll.
        if pipeline is not None:
            try:
                import gi
                gi.require_version("Gst", "1.0")
                from gi.repository import Gst
                pipeline.set_state(Gst.State.NULL)
                # Wait for the NULL transition to fully complete so
                # the audio backend releases the device before the
                # voice pipeline tries to open it.
                pipeline.get_state(500 * Gst.MSECOND)
            except Exception:
                log.warning("TTS: stop() pipeline NULL failed", exc_info=True)
        # Wait for the worker to finish its cleanup + drain loop exit.
        # Gated at 2 s so a runaway worker can't hang the UI.
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
            if worker.is_alive():
                log.warning("TTS: stop() worker still alive after 2 s")
        log.warning("TTS: stop() — queue cleared, pipeline yanked, worker joined")

    # ── Worker loop ──────────────────────────────────────────────────────

    def _drain_queue(self) -> None:
        log.warning("TTS: worker thread start")
        while True:
            with self._lock:
                if not self._queue or self._cancel:
                    log.warning("TTS: worker exit (empty=%s, cancel=%s)",
                              not self._queue, self._cancel)
                    return
                chunks, voice_params = self._queue.pop(0)
            for chunk in chunks:
                if self._cancel:
                    break
                try:
                    if chunk.kind == "cat":
                        self._play_cat(chunk.content)
                    elif chunk.kind == "text":
                        self._play_text(chunk.content, voice_params)
                except Exception:
                    log.exception("TTS chunk failed: %r", chunk)

    # ── Cat sample playback (GStreamer) ──────────────────────────────────

    def _play_cat(self, pool_key: str) -> None:
        path = _resolve_sample(pool_key)
        if path is None:
            log.warning("TTS: no sample for pool %r", pool_key)
            return
        self._play_file_blocking(path)

    def _play_file_blocking(self, path: str) -> None:
        """Play a media file through GStreamer and block until EOS.
        Called from the worker thread, never from the main thread.

        The pipeline is stored on ``self._active_pipeline`` while it's
        in PLAYING state so ``stop()`` can yank it synchronously from
        another thread. Cleanup (State.NULL + drop the reference) runs
        in a finally block so a partial playback can't leak a held
        audio device."""
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError):
            log.warning("TTS: GStreamer unavailable, can't play %s", path)
            return
        if not Gst.is_initialized():
            Gst.init(None)
        uri = "file://" + os.path.abspath(path)
        pipeline = Gst.ElementFactory.make("playbin", None)
        if pipeline is None:
            log.warning("TTS: playbin element unavailable")
            return
        pipeline.set_property("uri", uri)
        # Force an explicit audio sink instead of letting playbin use
        # autoaudiosink. Without this, after a voice recording cycle
        # (which opened autoaudiosrc on the mic), GStreamer sometimes
        # cached an audio sink selection that silently routed output
        # to a null device, making TTS inaudible until the next audio
        # backend state change. Explicit pipewiresink → pulsesink →
        # autoaudiosink fallback guarantees a working output path on
        # modern Linux (PipeWire) and legacy (PulseAudio).
        sink = None
        for sink_name in ("pipewiresink", "pulsesink", "autoaudiosink"):
            sink = Gst.ElementFactory.make(sink_name, None)
            if sink is not None:
                log.warning("TTS: using %s", sink_name)
                break
        if sink is not None:
            pipeline.set_property("audio-sink", sink)
        with self._lock:
            self._active_pipeline = pipeline
        try:
            size = os.path.getsize(path) if os.path.exists(path) else -1
            log.warning("TTS: play %s (%d bytes)", os.path.basename(path), size)
            ret = pipeline.set_state(Gst.State.PLAYING)
            log.warning("TTS: set_state(PLAYING) = %s", ret)
            bus = pipeline.get_bus()
            # Drain ALL bus messages to a console log so we see state
            # changes, warnings, tag events, and not just EOS/ERROR.
            import time as _t
            deadline = _t.monotonic() + 10.0
            while _t.monotonic() < deadline:
                msg = bus.timed_pop(500 * Gst.MSECOND)
                if msg is None:
                    continue
                if msg.type == Gst.MessageType.EOS:
                    log.warning("TTS: EOS")
                    break
                if msg.type == Gst.MessageType.ERROR:
                    err, debug = msg.parse_error()
                    log.warning("TTS: GStreamer error %s (%s)", err, debug)
                    break
                if msg.type == Gst.MessageType.WARNING:
                    w, d = msg.parse_warning()
                    log.warning("TTS: GStreamer warning %s (%s)", w, d)
                elif msg.type == Gst.MessageType.STATE_CHANGED:
                    if msg.src == pipeline:
                        old, new, pending = msg.parse_state_changed()
                        log.warning("TTS: state %s → %s", old, new)
        finally:
            try:
                pipeline.set_state(Gst.State.NULL)
                # Drain any pending state change — some sinks (PulseAudio,
                # PipeWire) only release the device after the state
                # transition actually completes. 500 ms is more than
                # enough on a healthy system.
                pipeline.get_state(500 * Gst.MSECOND)
            except Exception:
                log.warning("TTS: pipeline cleanup failed", exc_info=True)
            with self._lock:
                if self._active_pipeline is pipeline:
                    self._active_pipeline = None

    # ── Text playback (Piper) ────────────────────────────────────────────

    def _play_text(self, text: str, voice_params: dict | None = None) -> None:
        """Synthesize ``text`` via Piper and play the resulting WAV.

        ``voice_params`` is an optional dict with Piper SynthesisConfig
        fields (``speaker_id``, ``length_scale``, ``noise_scale``,
        ``noise_w_scale``) — applied to this call only. On first use
        this blocks for ~2-5 s to download the 74 MB voice model and
        another ~1 s to load it; subsequent calls synthesize in
        ~100-500 ms depending on sentence length.
        """
        # Cleaned text should never contain markdown asterisks or emoji
        # because split_cat_sounds already ran _clean_text_for_tts on
        # every text chunk — this is a defensive fallback for callers
        # that feed raw strings through the player directly.
        text = _clean_text_for_tts(text)
        if not text:
            return
        # Piper can't synthesize a string with no letters (pure
        # punctuation like '!' or '?'). It silently emits 0 bytes of
        # audio. Skip those chunks to avoid playing empty WAV files
        # through GStreamer for no benefit.
        if not any(c.isalpha() for c in text):
            log.warning("TTS: skip punctuation-only chunk %r", text)
            return
        voice = self._get_voice()
        if voice is None:
            return
        # Build a SynthesisConfig with the per-cat params on top of the
        # voice's defaults. Fields we don't pass stay at the model's
        # trained values.
        syn_config = None
        if voice_params:
            try:
                from piper import SynthesisConfig  # type: ignore
                syn_config = SynthesisConfig(
                    speaker_id=voice_params.get("speaker_id"),
                    length_scale=voice_params.get("length_scale"),
                    noise_scale=voice_params.get("noise_scale"),
                    noise_w_scale=voice_params.get("noise_w_scale"),
                )
            except Exception:
                log.warning("TTS: couldn't build SynthesisConfig", exc_info=True)
                syn_config = None
        import tempfile
        import wave
        wav_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=".wav", delete=False) as tmp:
                wav_path = tmp.name
            byte_count = 0
            with wave.open(wav_path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)  # int16 → 2 bytes/sample
                wav.setframerate(voice.config.sample_rate)
                if syn_config is not None:
                    gen = voice.synthesize(text, syn_config=syn_config)
                else:
                    gen = voice.synthesize(text)
                for chunk in gen:
                    wav.writeframes(chunk.audio_int16_bytes)
                    byte_count += len(chunk.audio_int16_bytes)
            log.warning("TTS: synth %r → %d bytes at %s",
                        text[:40], byte_count, wav_path)
            self._play_file_blocking(wav_path)
        except Exception:
            log.warning("TTS: Piper synthesis failed", exc_info=True)
        finally:
            if wav_path:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass


# ── Module-level convenience ─────────────────────────────────────────────────

_default_player: SoundPlayer | None = None


def get_default_player() -> SoundPlayer:
    """Return the shared SoundPlayer instance for the app.

    Most callers want one player, not per-cat instances, because the
    underlying GStreamer pipeline happily serializes concurrent
    requests through the worker queue. Per-cat isolation is handled at
    the call site (a cat with ``tts_enabled=False`` simply doesn't
    call ``play()``)."""
    global _default_player
    if _default_player is None:
        _default_player = SoundPlayer()
    return _default_player
