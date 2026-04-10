"""Voice chat (push-to-talk) backend for CATAI-Linux.

Optional feature — only loaded if `faster-whisper` is installed
(`pip install catai-linux[voice]`). The module's top-level `VOICE_AVAILABLE`
flag reflects this. If False, the higher-level app should hide the mic button
and skip creating a VoiceRecorder.

Recording uses GStreamer's autoaudiosrc pipeline to capture 16-bit mono WAV
at 16 kHz directly into a temp file. Transcription runs faster-whisper on
the WAV in a background thread, with automatic CUDA detection via ctranslate2.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

log = logging.getLogger("catai")

try:
    from faster_whisper import WhisperModel as _WhisperModel
    VOICE_AVAILABLE = True
except ImportError:
    _WhisperModel = None
    VOICE_AVAILABLE = False

# Ensure GStreamer is initialized before any pipeline is built.
Gst.init(None)


# ── Whisper model metadata ────────────────────────────────────────────────────
# Approx download sizes (MB) for user-facing status display.

WHISPER_MODEL_SIZES: dict[str, int] = {
    "tiny": 39, "tiny.en": 39,
    "base": 74, "base.en": 74,
    "small": 244, "small.en": 244,
    "medium": 769, "medium.en": 769,
    "large-v1": 1550, "large-v2": 1550, "large-v3": 1550,
    "large-v3-turbo": 809, "turbo": 809,
    "distil-large-v3": 756,
}


def is_model_cached(name: str) -> bool:
    """Return True if the faster-whisper model is already in HuggingFace cache.
    Scans all orgs since different models live under different namespaces
    (Systran, mobiuslabsgmbh, deepdml, distil-whisper, ...)."""
    cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
    if not os.path.isdir(cache_dir):
        return False
    suffix = f"faster-whisper-{name}"
    try:
        entries = os.listdir(cache_dir)
    except OSError:
        return False
    for entry in entries:
        # HF cache format: "models--<org>--<repo>"
        if not entry.startswith("models--"):
            continue
        repo = entry.split("--")[-1]
        if repo != suffix:
            continue
        snapshots = os.path.join(cache_dir, entry, "snapshots")
        if not os.path.isdir(snapshots):
            continue
        for snap in os.listdir(snapshots):
            p = os.path.join(snapshots, snap)
            if os.path.isdir(p) and os.listdir(p):
                return True
    return False


class VoiceRecorder:
    """Push-to-talk audio recording + Whisper transcription.

    - ``start()`` begins GStreamer capture to a temp WAV file
    - ``stop_and_transcribe(lang, on_result)`` ends capture, runs Whisper in a
      background thread, then calls ``on_result(text)`` on the main thread
      (``text`` is ``None`` on error / empty / too-short recording)
    """
    MIN_RECORDING_MS = 300  # ignore tiny accidental presses

    def __init__(self, model_name: str | None = None):
        # Precedence: explicit arg > env var > "base" default
        self.MODEL_NAME = model_name or os.environ.get("CATAI_WHISPER_MODEL", "base")
        self._model = None
        self._pipeline = None
        self._wav_path: str | None = None
        self._recording = False
        self._start_time = 0.0

    def set_model(self, model_name: str) -> None:
        """Change the Whisper model. Clears the cached model so the next
        recording reloads with the new name."""
        if model_name == self.MODEL_NAME:
            return
        self.MODEL_NAME = model_name
        self._model = None  # force reload on next _ensure_model()

    def _ensure_model(self) -> None:
        """Lazy-load the whisper model (downloads it on first use)."""
        if self._model is None and VOICE_AVAILABLE:
            # Device selection: env override, else auto (CUDA > CPU)
            device = os.environ.get("CATAI_WHISPER_DEVICE", "auto").lower()
            if device == "auto":
                try:
                    import ctranslate2
                    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                except Exception:
                    device = "cpu"
            compute_type = "float16" if device == "cuda" else "int8"
            log.warning("VOICE: loading Whisper %r on %s (%s)...", self.MODEL_NAME, device, compute_type)
            t0 = time.monotonic()
            try:
                self._model = _WhisperModel(
                    self.MODEL_NAME, device=device, compute_type=compute_type
                )
            except Exception as e:
                log.warning("VOICE: Whisper %s failed (%s), falling back to CPU int8", device, e)
                self._model = _WhisperModel(
                    self.MODEL_NAME, device="cpu", compute_type="int8"
                )
            log.warning("VOICE: Whisper ready in %.1fs", time.monotonic() - t0)

    def start(self) -> None:
        if self._recording:
            return
        import tempfile
        fd, self._wav_path = tempfile.mkstemp(prefix="catai_voice_", suffix=".wav")
        os.close(fd)
        pipeline_desc = (
            "autoaudiosrc ! "
            "audioconvert ! "
            "audioresample ! "
            "audio/x-raw,format=S16LE,channels=1,rate=16000 ! "
            f"wavenc ! filesink location={self._wav_path}"
        )
        try:
            self._pipeline = Gst.parse_launch(pipeline_desc)
            ret = self._pipeline.set_state(Gst.State.PLAYING)
            log.warning("VOICE: pipeline state change = %s", ret)
        except Exception:
            log.exception("VOICE: failed to start pipeline")
            self._pipeline = None
            return
        self._recording = True
        self._start_time = time.monotonic()
        log.warning("VOICE: recording started -> %s", self._wav_path)

    def stop_and_transcribe(
        self,
        lang: str,
        on_result: Callable[[str | None], None],
    ) -> None:
        """Stop recording, run transcription in background, then call on_result
        on the main thread. text is None on error / empty / too short."""
        if not self._recording:
            log.warning("VOICE: stop called while not recording")
            on_result(None)
            return
        duration_ms = (time.monotonic() - self._start_time) * 1000
        log.warning("VOICE: stopping after %.0fms", duration_ms)
        self._pipeline.send_event(Gst.Event.new_eos())
        bus = self._pipeline.get_bus()
        bus.timed_pop_filtered(1000 * Gst.MSECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        self._recording = False
        wav_path = self._wav_path
        self._wav_path = None

        if duration_ms < self.MIN_RECORDING_MS:
            log.warning("VOICE: recording too short (%dms), ignored", duration_ms)
            try:
                os.remove(wav_path)
            except Exception:
                pass
            on_result(None)
            return

        try:
            wav_size = os.path.getsize(wav_path)
            log.warning("VOICE: wav file size = %d bytes", wav_size)
        except Exception:
            log.warning("VOICE: could not stat wav file")

        def work():
            try:
                if self._model is None:
                    log.warning("VOICE: model not preloaded, loading now...")
                else:
                    log.warning("VOICE: model already loaded (preloaded), go straight to transcribe")
                self._ensure_model()
                if not self._model:
                    log.warning("VOICE: model load returned None")
                    GLib.idle_add(on_result, None)
                    return
                log.warning("VOICE: transcribing lang=%s file=%s", lang, wav_path)
                segments, info = self._model.transcribe(
                    wav_path, language=lang, beam_size=1, vad_filter=True
                )
                seg_list = list(segments)
                log.warning("VOICE: got %d segments, detected_lang=%s",
                            len(seg_list), getattr(info, 'language', '?'))
                text = " ".join(seg.text.strip() for seg in seg_list).strip()
                log.warning("VOICE: transcription result = %r", text)
                GLib.idle_add(on_result, text or None)
            except Exception:
                log.exception("VOICE: transcription failed")
                GLib.idle_add(on_result, None)
            finally:
                try:
                    os.remove(wav_path)
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()
