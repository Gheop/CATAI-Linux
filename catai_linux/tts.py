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
    with leading/trailing whitespace stripped; empty text chunks are
    skipped entirely.

    Example::

        >>> split_cat_sounds("Miaou mon ami! *ronron* ça va.")
        [Chunk('cat', 'meow'),
         Chunk('text', 'mon ami!'),
         Chunk('cat', 'purr'),
         Chunk('text', 'ça va.')]

    Empty input returns an empty list. Input with no cat tokens
    returns a single text chunk with the original string stripped.
    """
    if not text or not text.strip():
        return []

    # Gather all token matches with their span + pool key
    matches: list[tuple[int, int, str]] = []
    for pattern, key in _TOKEN_PATTERNS:
        for m in pattern.finditer(text):
            matches.append((m.start(), m.end(), key))
    if not matches:
        return [Chunk("text", text.strip())]

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
            before = text[cursor:start].strip()
            if before:
                chunks.append(Chunk("text", before))
        chunks.append(Chunk("cat", key))
        cursor = end
    tail = text[cursor:].strip()
    if tail:
        chunks.append(Chunk("text", tail))
    return chunks


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
        self._queue: list[list[Chunk]] = []
        # Piper voice loaded lazily on first text chunk. Shared across
        # every cat (per-cat voices are deferred to a later PR). None
        # means "not loaded yet", False means "load failed permanently".
        self._voice = None
        self._voice_download_started = False

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
            log.debug("TTS: piper-tts not installed — text chunks silent")
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

    def play(self, chunks: list[Chunk]) -> None:
        """Enqueue a chunk list for playback. Non-blocking."""
        if not chunks:
            return
        with self._lock:
            self._queue.append(chunks)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._drain_queue, daemon=True)
                self._worker.start()

    # ── Worker loop ──────────────────────────────────────────────────────

    def _drain_queue(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                chunks = self._queue.pop(0)
            for chunk in chunks:
                try:
                    if chunk.kind == "cat":
                        self._play_cat(chunk.content)
                    elif chunk.kind == "text":
                        self._play_text(chunk.content)
                except Exception:
                    log.exception("TTS chunk failed: %r", chunk)

    # ── Cat sample playback (GStreamer) ──────────────────────────────────

    def _play_cat(self, pool_key: str) -> None:
        path = _resolve_sample(pool_key)
        if path is None:
            log.debug("TTS: no sample for pool %r", pool_key)
            return
        self._play_file_blocking(path)

    def _play_file_blocking(self, path: str) -> None:
        """Play a media file through GStreamer and block until EOS.
        Called from the worker thread, never from the main thread."""
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst, GLib
        except (ImportError, ValueError):
            log.debug("TTS: GStreamer unavailable, can't play %s", path)
            return
        if not Gst.is_initialized():
            Gst.init(None)
        # playbin handles everything: file:// URI, decode, audio sink
        uri = "file://" + os.path.abspath(path)
        pipeline = Gst.ElementFactory.make("playbin", None)
        if pipeline is None:
            log.debug("TTS: playbin element unavailable")
            return
        pipeline.set_property("uri", uri)
        pipeline.set_state(Gst.State.PLAYING)
        bus = pipeline.get_bus()
        # Wait for EOS or error, max 10 s to avoid a stuck sample from
        # freezing the queue drain
        msg = bus.timed_pop_filtered(
            10 * Gst.SECOND,
            Gst.MessageType.EOS | Gst.MessageType.ERROR,
        )
        if msg and msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.debug("TTS: GStreamer error %s (%s)", err, debug)
        pipeline.set_state(Gst.State.NULL)
        _ = GLib  # keep import alive

    # ── Text playback (Piper) ────────────────────────────────────────────

    def _play_text(self, text: str) -> None:
        """Synthesize ``text`` via Piper and play the resulting WAV.

        On first use this will block for ~2-5 s to download the 74 MB
        voice model and another ~1 s to load it. Subsequent calls
        synthesize in ~100-500 ms depending on sentence length. Every
        call writes to a temp WAV file in /tmp and deletes it after
        playback to keep the surface area minimal — we don't try to
        stream chunks directly into a GStreamer appsrc because the
        reliability gain isn't worth the pipeline complexity.
        """
        voice = self._get_voice()
        if voice is None:
            # Already logged in _get_voice on the first failure; later
            # calls just silently skip text chunks.
            return
        import tempfile
        import wave
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=".wav", delete=False) as tmp:
                wav_path = tmp.name
            with wave.open(wav_path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)  # int16 → 2 bytes/sample
                wav.setframerate(voice.config.sample_rate)
                for chunk in voice.synthesize(text):
                    wav.writeframes(chunk.audio_int16_bytes)
            self._play_file_blocking(wav_path)
        except Exception:
            log.debug("TTS: Piper synthesis failed", exc_info=True)
        finally:
            try:
                os.unlink(wav_path)
            except (OSError, NameError):
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
