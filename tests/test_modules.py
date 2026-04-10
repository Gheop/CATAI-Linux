#!/usr/bin/env python3
"""Headless unit tests for the extracted modules (l10n, voice, chat_backend,
drawing, x11_helpers). No GDK display needed, no network, no faster-whisper.

Run via: `make test` or `python3 tests/test_modules.py`.

Style matches tests/e2e_test.py: plain asserts + a PASS/FAIL counter so the
tests run without pytest as a dependency and CI output stays readable.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from unittest import mock

# Ensure the package is importable when run from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def test(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \u2713 {name}", flush=True)
    else:
        FAIL += 1
        print(f"  \u2717 {name} \u2014 {detail}", flush=True)


# ── catai_linux.l10n ─────────────────────────────────────────────────────────

def test_l10n() -> None:
    print("\n[l10n]", flush=True)
    from catai_linux.l10n import L10n

    # Default language is French
    original = L10n.lang
    try:
        L10n.lang = "fr"
        test("L10n.s('title') fr", L10n.s("title") == ":: RÉGLAGES ::", L10n.s("title"))

        L10n.lang = "en"
        test("L10n.s('title') en", L10n.s("title") == ":: SETTINGS ::", L10n.s("title"))

        L10n.lang = "es"
        test("L10n.s('title') es", L10n.s("title") == ":: AJUSTES ::", L10n.s("title"))

        # Unknown key falls back to the key itself
        L10n.lang = "fr"
        test("L10n.s unknown key falls back to key",
             L10n.s("totally_nonexistent_key") == "totally_nonexistent_key")

        # Unknown language falls back to fr
        L10n.lang = "de"
        test("L10n.s unknown lang falls back to fr",
             L10n.s("title") == ":: RÉGLAGES ::")

        # random_meow returns a string from the French pool
        L10n.lang = "fr"
        meow = L10n.random_meow()
        test("random_meow returns a string", isinstance(meow, str) and len(meow) > 0, meow)
        test("random_meow is from the fr pool", meow in L10n.meows["fr"], meow)
    finally:
        L10n.lang = original


# ── catai_linux.voice ────────────────────────────────────────────────────────

def test_voice() -> None:
    print("\n[voice]", flush=True)
    from catai_linux import voice

    # WHISPER_MODEL_SIZES metadata table
    test("WHISPER_MODEL_SIZES has base",
         "base" in voice.WHISPER_MODEL_SIZES and voice.WHISPER_MODEL_SIZES["base"] > 0)
    test("WHISPER_MODEL_SIZES has large-v3-turbo",
         "large-v3-turbo" in voice.WHISPER_MODEL_SIZES)
    test("base is smaller than large-v3",
         voice.WHISPER_MODEL_SIZES["base"] < voice.WHISPER_MODEL_SIZES["large-v3"])

    # is_model_cached returns False for nonexistent cache dir
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(os.path, "expanduser", return_value=os.path.join(td, "nope")):
            test("is_model_cached returns False on missing cache",
                 voice.is_model_cached("base") is False)

    # is_model_cached finds a model under ANY huggingface org prefix
    with tempfile.TemporaryDirectory() as td:
        hub = os.path.join(td, ".cache", "huggingface", "hub")
        # Simulate the mobiuslabsgmbh layout used by large-v3-turbo
        snap = os.path.join(hub, "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
                            "snapshots", "abc123")
        os.makedirs(snap)
        # Need at least one file in the snapshot to count as valid
        with open(os.path.join(snap, "model.bin"), "w") as f:
            f.write("x")
        with mock.patch.object(os.path, "expanduser", return_value=hub):
            test("is_model_cached finds model in mobiuslabsgmbh org",
                 voice.is_model_cached("large-v3-turbo") is True)
            test("is_model_cached returns False for model not cached",
                 voice.is_model_cached("tiny") is False)

    # VoiceRecorder instantiation and model switching (no actual recording)
    if voice.VOICE_AVAILABLE:
        rec = voice.VoiceRecorder(model_name="tiny")
        test("VoiceRecorder.__init__ sets MODEL_NAME", rec.MODEL_NAME == "tiny")
        test("VoiceRecorder starts not recording", rec._recording is False)
        test("VoiceRecorder._model is None initially", rec._model is None)

        rec.set_model("base")
        test("set_model updates MODEL_NAME", rec.MODEL_NAME == "base")
        test("set_model clears _model cache", rec._model is None)

        # Setting the same model is a no-op (no reload)
        rec._model = "fake-model-instance"
        rec.set_model("base")  # same as current
        test("set_model with same name is a no-op",
             rec._model == "fake-model-instance")
    else:
        print("  (faster_whisper not installed, skipping VoiceRecorder tests)")


# ── catai_linux.chat_backend ─────────────────────────────────────────────────

def test_chat_backend() -> None:
    print("\n[chat_backend]", flush=True)
    from catai_linux import chat_backend

    # Constants
    test("CLAUDE_MODEL is a claude-* model",
         chat_backend.CLAUDE_MODEL.startswith("claude-"))
    test("MEM_MAX is a positive int",
         isinstance(chat_backend.MEM_MAX, int) and chat_backend.MEM_MAX > 0)
    test("OLLAMA_URL points at localhost",
         "localhost" in chat_backend.OLLAMA_URL)

    # _find_claude_cli doesn't crash when the binary is missing
    with mock.patch.object(shutil, "which", return_value=None):
        with mock.patch.object(os.path, "isfile", return_value=False):
            result = chat_backend._find_claude_cli()
            test("_find_claude_cli returns None when claude missing",
                 result is None, str(result))

    # ChatBackend.send appends the user message to history
    class StubBackend(chat_backend.ChatBackend):
        def _stream_chunks(self):
            yield "hello "
            yield "world"

    b = StubBackend("fake-model")
    b.messages = [{"role": "system", "content": "sys"}]
    test("ChatBackend starts not streaming", b.is_streaming is False)
    test("ChatBackend.messages has system prompt at index 0",
         b.messages[0]["role"] == "system")

    # Verify cancel flag flip
    b.cancel()
    test("cancel() sets _cancel True", b._cancel is True)


# ── catai_linux.x11_helpers ──────────────────────────────────────────────────

def test_x11_helpers() -> None:
    print("\n[x11_helpers]", flush=True)
    from catai_linux import x11_helpers

    # The module has to be importable without an X server — all the Xlib
    # init is lazy. Just touch the public names.
    test("module exposes move_window", callable(x11_helpers.move_window))
    test("module exposes flush_x11", callable(x11_helpers.flush_x11))
    test("module exposes update_input_shape",
         callable(x11_helpers.update_input_shape))
    test("module exposes set_always_on_top",
         callable(x11_helpers.set_always_on_top))
    test("module exposes set_notification_type",
         callable(x11_helpers.set_notification_type))

    # XRectangle is a ctypes.Structure
    rect = x11_helpers.XRectangle(x=10, y=20, width=100, height=200)
    test("XRectangle(x,y,w,h) assigns fields",
         rect.x == 10 and rect.y == 20 and rect.width == 100 and rect.height == 200)

    # Module-level caches are dicts / sets
    test("_xid_cache is a dict", isinstance(x11_helpers._xid_cache, dict))
    test("_applied is a set", isinstance(x11_helpers._applied, set))


# ── catai_linux.drawing ──────────────────────────────────────────────────────

def test_drawing() -> None:
    print("\n[drawing]", flush=True)
    import cairo

    from catai_linux import drawing

    # Sanity: the CSS blob is non-empty bytes and mentions our key classes
    test("CSS is bytes and non-empty",
         isinstance(drawing.CSS, bytes) and len(drawing.CSS) > 100)
    test("CSS mentions .pixel-mic-btn",
         b".pixel-mic-btn" in drawing.CSS)
    test("CSS mentions .canvas-window",
         b".canvas-window" in drawing.CSS)

    # Offscreen cairo surface — we can invoke every pure drawing function
    # without a display.
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
    ctx = cairo.Context(surface)

    # Pango text sizing
    w, h = drawing._pango_text_size(ctx, "Meow!")
    test("_pango_text_size returns positive (w, h)", w > 0 and h > 0, f"{w}x{h}")

    # draw_pixel_tail doesn't crash
    try:
        drawing.draw_pixel_tail(ctx, 30, 15, px=3)
        test("draw_pixel_tail runs", True)
    except Exception as e:
        test("draw_pixel_tail runs", False, str(e))

    # Bubble drawing calls don't crash
    for name, fn in [
        ("draw_meow_bubble", lambda: drawing.draw_meow_bubble(ctx, "Meow~", 100, 100, 80, 80)),
        ("draw_encounter_bubble", lambda: drawing.draw_encounter_bubble(ctx, "Hi there!", 100, 100, 80, 80)),
        ("draw_chat_bubble", lambda: drawing.draw_chat_bubble(ctx, "Bonjour mon petit chat", 100, 100, 80, 80)),
        ("draw_context_menu", lambda: drawing.draw_context_menu(ctx, 10, 10, "Settings", "Quit")),
    ]:
        try:
            fn()
            test(f"{name} runs", True)
        except Exception as e:
            test(f"{name} runs", False, str(e))

    # Overlay drawing calls don't crash
    for name in ("draw_zzz", "draw_exclamation", "draw_hearts", "draw_hurt_stars",
                 "draw_skull", "draw_sparkle", "draw_anger"):
        fn = getattr(drawing, name)
        try:
            if name == "draw_zzz":
                fn(ctx, 100, 100, 80)
            else:
                fn(ctx, 100, 100, 80, 80)
            test(f"{name} runs", True)
        except Exception as e:
            test(f"{name} runs", False, str(e))

    # draw_speed_lines takes a direction
    try:
        drawing.draw_speed_lines(ctx, 100, 100, 80, 80, "east")
        drawing.draw_speed_lines(ctx, 100, 100, 80, 80, "west")
        test("draw_speed_lines runs (east + west)", True)
    except Exception as e:
        test("draw_speed_lines runs (east + west)", False, str(e))

    # draw_birth_sparkles with progress ∈ [0,1]
    try:
        drawing.draw_birth_sparkles(ctx, 100, 100, 80, 80, 0.0)
        drawing.draw_birth_sparkles(ctx, 100, 100, 80, 80, 0.5)
        drawing.draw_birth_sparkles(ctx, 100, 100, 80, 80, 1.0)
        test("draw_birth_sparkles runs (progress 0, 0.5, 1)", True)
    except Exception as e:
        test("draw_birth_sparkles runs (progress 0, 0.5, 1)", False, str(e))


# ── catai_linux (package smoke test) ─────────────────────────────────────────

def test_import_smoke() -> None:
    print("\n[import smoke]", flush=True)
    modules = [
        "catai_linux",
        "catai_linux.l10n",
        "catai_linux.x11_helpers",
        "catai_linux.chat_backend",
        "catai_linux.voice",
        "catai_linux.drawing",
        "catai_linux.app",
    ]
    for m in modules:
        try:
            __import__(m)
            test(f"import {m}", True)
        except Exception as e:
            test(f"import {m}", False, str(e))


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=== CATAI Unit Tests (headless) ===\n", flush=True)
    test_import_smoke()
    test_l10n()
    test_voice()
    test_chat_backend()
    test_x11_helpers()
    test_drawing()
    print(f"\n=== Results: {PASS} passed, {FAIL} failed ===\n", flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
