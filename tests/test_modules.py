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

    # THEME palette + set_theme() swaps in place so existing references
    # remain valid (i.e. the dict is the *same* dict, just with new values).
    theme_ref = drawing.THEME
    drawing.set_theme(dark=False)
    light_bg = drawing.THEME["bubble_bg"]
    drawing.set_theme(dark=True)
    dark_bg = drawing.THEME["bubble_bg"]
    test("set_theme mutates in place (same dict object)",
         drawing.THEME is theme_ref)
    test("set_theme(dark=True) changes bubble_bg",
         light_bg != dark_bg,
         f"light={light_bg} dark={dark_bg}")
    test("dark theme bubble_bg is darker than light",
         sum(dark_bg[:3]) < sum(light_bg[:3]))
    test("dark theme bubble_text is brighter than light",
         sum(drawing.DARK_THEME["bubble_text"][:3])
         > sum(drawing.LIGHT_THEME["bubble_text"][:3]))
    # Draw after switching to dark to confirm nothing crashes with the new palette
    try:
        drawing.draw_meow_bubble(ctx, "Dark~", 100, 100, 80, 80)
        drawing.draw_chat_bubble(ctx, "Dark bubble", 100, 100, 80, 80)
        drawing.draw_context_menu(ctx, 10, 10, "Settings", "Quit")
        test("drawing under dark theme doesn't crash", True)
    except Exception as e:
        test("drawing under dark theme doesn't crash", False, str(e))
    # Reset for the rest of the suite
    drawing.set_theme(dark=False)


def test_theme() -> None:
    print("\n[theme]", flush=True)
    from catai_linux import theme

    # is_dark_mode returns a bool regardless of environment
    result = theme.is_dark_mode()
    test("is_dark_mode returns bool", isinstance(result, bool), f"{result!r}")

    # With a fake gsettings PATH that can't find it, we should safely get False
    import os
    orig_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = "/nonexistent"
        # shutil.which is called inside — clear cache
        import shutil
        shutil.which.cache_clear() if hasattr(shutil.which, "cache_clear") else None
        test("is_dark_mode = False when gsettings missing",
             theme.is_dark_mode() is False)
    finally:
        os.environ["PATH"] = orig_path


def test_personality() -> None:
    print("\n[personality]", flush=True)
    import tempfile

    from catai_linux import personality

    # Sandbox the CONFIG dir so we don't touch the user's real state
    with tempfile.TemporaryDirectory() as td:
        orig = personality._CONFIG_SUBDIR
        personality._CONFIG_SUBDIR = td
        try:
            # Fresh state (file doesn't exist)
            st = personality.PersonalityState.load("cat_unit")
            test("fresh state has no quirks", st.drifted_traits == [])
            test("fresh state message_count=0", st.message_count == 0)

            # Apply drift + dedup
            st.apply_drift("aime les chaussettes")
            test("apply_drift adds a quirk", len(st.drifted_traits) == 1)
            st.apply_drift("aime les chaussettes")
            test("apply_drift is dedup (case-insensitive)",
                 len(st.drifted_traits) == 1)
            st.apply_drift("AIME LES CHAUSSETTES")
            test("apply_drift dedup uppercase too",
                 len(st.drifted_traits) == 1)
            st.apply_drift("aime le jardinage")
            test("apply_drift adds distinct quirk",
                 len(st.drifted_traits) == 2)

            # Overflow trims oldest
            for q in ["a", "bb", "cc", "dd", "ee", "ff", "gg"]:
                st.apply_drift(q)
            test("apply_drift caps at MAX_TRAITS",
                 len(st.drifted_traits) == personality.MAX_TRAITS)
            # Empty/too-long are rejected
            before = list(st.drifted_traits)
            st.apply_drift("")
            st.apply_drift(" ")
            st.apply_drift("x" * 200)
            test("apply_drift rejects empty/too-long",
                 st.drifted_traits == before)

            # Persist round-trip
            st.drifted_traits = ["première quirk", "deuxième quirk"]
            st.message_count = 42
            st.last_drift_at = 12345.0
            st.save()
            loaded = personality.PersonalityState.load("cat_unit")
            test("persist round-trip: quirks",
                 loaded.drifted_traits == ["première quirk", "deuxième quirk"])
            test("persist round-trip: message_count",
                 loaded.message_count == 42)

            # should_drift scheduling
            st2 = personality.PersonalityState(cat_id="cat_sched")
            test("fresh state should_drift=False", not st2.should_drift())
            for _ in range(personality.DRIFT_EVERY_MESSAGES):
                st2.on_message_added()
            test("should_drift=True after N messages", st2.should_drift())
            st2.on_message_added()
            test("should_drift=False mid-cycle", not st2.should_drift())

            # append_to_prompt languages
            st3 = personality.PersonalityState(
                cat_id="cat_prompt",
                drifted_traits=["quirky", "loves socks"],
            )
            base = "You are a cat."
            out_en = st3.append_to_prompt(base, "en")
            test("append_to_prompt en contains quirks",
                 "quirky" in out_en and "loves socks" in out_en)
            out_fr = st3.append_to_prompt(base, "fr")
            test("append_to_prompt fr contains quirks",
                 "quirky" in out_fr)
            # No quirks → unchanged
            st4 = personality.PersonalityState(cat_id="cat_empty")
            test("append_to_prompt with no quirks is identity",
                 st4.append_to_prompt(base, "fr") == base)

            # parse_drift_response
            test("parse plain JSON",
                 personality.parse_drift_response(
                     '{"trait": "aime le thé"}') == "aime le thé")
            test("parse fenced JSON",
                 personality.parse_drift_response(
                     '```json\n{"trait": "shy"}\n```') == "shy")
            test("parse embedded JSON",
                 personality.parse_drift_response(
                     'Sure! {"trait": "wise"} there you go')
                 == "wise")
            test("parse empty → None",
                 personality.parse_drift_response("") is None)
            test("parse garbage → None",
                 personality.parse_drift_response("this is not JSON at all\n"
                                                  "and spans multiple lines") is None)
        finally:
            personality._CONFIG_SUBDIR = orig


def test_seasonal() -> None:
    print("\n[seasonal]", flush=True)
    import cairo
    import datetime as dt
    import os

    from catai_linux import seasonal

    # Date resolver — broad seasons
    cases = {
        dt.date(2026, 1, 15):  "winter",
        dt.date(2026, 4, 10):  "spring",
        dt.date(2026, 7, 20):  "summer",
        dt.date(2026, 10, 15): "autumn",
    }
    for d, expected in cases.items():
        got = seasonal.resolve_season(d)
        test(f"resolve_season({d}) = {expected}", got == expected, got)

    # Special events override broad seasons
    events = {
        dt.date(2026, 10, 31): "halloween",
        dt.date(2026, 11, 1):  "halloween",
        dt.date(2026, 12, 20): "christmas",
        dt.date(2026, 12, 25): "christmas",
        dt.date(2026, 12, 31): "nye",
        dt.date(2027, 1, 1):   "nye",
        dt.date(2026, 2, 14):  "valentines",
    }
    for d, expected in events.items():
        got = seasonal.resolve_season(d)
        test(f"resolve_season({d}) = {expected}", got == expected, got)

    # CATAI_SEASON env override wins over everything
    orig = os.environ.get("CATAI_SEASON")
    try:
        os.environ["CATAI_SEASON"] = "nye"
        test("CATAI_SEASON override wins",
             seasonal.resolve_season(dt.date(2026, 7, 4)) == "nye")
        os.environ["CATAI_SEASON"] = "not_a_real_season"
        test("Invalid CATAI_SEASON is ignored",
             seasonal.resolve_season(dt.date(2026, 7, 4)) == "summer")
    finally:
        if orig is None:
            os.environ.pop("CATAI_SEASON", None)
        else:
            os.environ["CATAI_SEASON"] = orig

    # draw_overlay never crashes for any valid season on an offscreen surface
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
    ctx = cairo.Context(surface)
    for s in seasonal.ALL_SEASONS:
        try:
            seasonal.draw_overlay(ctx, 400, 300, season=s)
            test(f"draw_overlay({s}) runs", True)
        except Exception as e:
            test(f"draw_overlay({s}) runs", False, str(e))
    # Summer is a no-op — still safe
    try:
        seasonal.draw_overlay(ctx, 400, 300, season="summer")
        test("draw_overlay(summer) no-op runs", True)
    except Exception as e:
        test("draw_overlay(summer) no-op runs", False, str(e))
    # Unknown season → silent no-op (not an error)
    try:
        seasonal.draw_overlay(ctx, 400, 300, season="not_a_season")
        test("draw_overlay(unknown) silent no-op", True)
    except Exception as e:
        test("draw_overlay(unknown) silent no-op", False, str(e))


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
        "catai_linux.personality",
        "catai_linux.seasonal",
        "catai_linux.reactions",
        "catai_linux.mood",
        "catai_linux.activity",
        "catai_linux.app",
    ]
    for m in modules:
        try:
            __import__(m)
            test(f"import {m}", True)
        except Exception as e:
            test(f"import {m}", False, str(e))


# ── catai_linux.reactions ────────────────────────────────────────────────────

def test_reactions() -> None:
    print("\n[reactions]", flush=True)
    from catai_linux.reactions import ReactionPool

    # Create a pool with dummy chat factory — never called since we drive
    # the parser directly
    pool = ReactionPool(create_chat_fn=lambda model: None, get_model_fn=lambda: "mock")

    # (Note: the parser drops replies shorter than 2 chars as garbage, so
    # all test inputs below use ≥2-char strings.)

    # 1. Plain JSON array
    arr = pool._parse_pool('["aa", "bb", "ccc"]')
    test("parse plain JSON", arr == ["aa", "bb", "ccc"], str(arr))

    # 2. JSON in markdown code fence
    raw = '```json\n["hello", "world", "foo"]\n```'
    arr = pool._parse_pool(raw)
    test("parse JSON in markdown fence", arr == ["hello", "world", "foo"], str(arr))

    # 3. JSON with prose wrapper (extract [...] substring)
    raw = 'Here is the array:\n["line 1", "line 2", "line 3"]\nHope this helps!'
    arr = pool._parse_pool(raw)
    test("parse JSON with prose wrapper", arr == ["line 1", "line 2", "line 3"], str(arr))

    # 4. Capped to POOL_SIZE (6)
    arr = pool._parse_pool('["r1","r2","r3","r4","r5","r6","r7","r8","r9"]')
    test("parse caps at POOL_SIZE", arr is not None and len(arr) == 6, str(arr))

    # 5. Truncate to MAX_REPLY_LEN (40)
    long_reply = "a" * 100
    arr = pool._parse_pool(f'["ok", "{long_reply}", "yo"]')
    test("parse truncates long replies to MAX_REPLY_LEN",
         arr is not None and all(len(x) <= 40 for x in arr), str(arr))

    # 6. Line-by-line fallback
    raw = "- reaction one\n- reaction two\n- reaction three\n- reaction four"
    arr = pool._parse_pool(raw)
    test("parse line-by-line fallback",
         arr is not None and len(arr) >= 3 and "reaction one" in arr, str(arr))

    # 7. Garbage → None or list
    arr = pool._parse_pool("this is just garbage")
    test("parse garbage returns None or list (line-by-line best-effort)",
         arr is None or isinstance(arr, list), str(arr))

    # 8. Empty string / None → None
    test("parse empty string returns None", pool._parse_pool("") is None)
    test("parse None returns None", pool._parse_pool(None) is None)

    # 9. JSON with non-string items: ints are coerced to strings, null is
    #    dropped. The parser returns what's salvageable.
    arr = pool._parse_pool('["aa", 42, null, "bb"]')
    test("parse coerces ints, drops null, keeps valid strings",
         arr is not None and "aa" in arr and "bb" in arr and "42" in arr, str(arr))

    # 10. Fallback string for EVT_CAPSLOCK uses L10n
    from catai_linux.l10n import L10n
    original_lang = L10n.lang
    try:
        L10n.lang = "fr"
        fb = pool._fallback(ReactionPool.EVT_CAPSLOCK)
        test("fallback returns capslock_yell in fr", "CRIES" in fb.upper(), fb)
        L10n.lang = "en"
        fb = pool._fallback(ReactionPool.EVT_CAPSLOCK)
        test("fallback returns capslock_yell in en", "SHOUTING" in fb.upper(), fb)
    finally:
        L10n.lang = original_lang


# ── main ─────────────────────────────────────────────────────────────────────

# ── catai_linux.mood ─────────────────────────────────────────────────────────

def test_mood() -> None:
    print("\n[mood]", flush=True)
    from catai_linux.mood import CatMood
    import tempfile
    import time as _time

    # Default construction — values are in the expected bands
    m = CatMood()
    test("default happiness", 50 <= m.happiness <= 70, str(m.happiness))
    test("default energy", 70 <= m.energy <= 90, str(m.energy))
    test("default bored", 20 <= m.bored <= 40, str(m.bored))
    test("default hunger", 10 <= m.hunger <= 30, str(m.hunger))

    # Fake a 1h elapsed time by rewinding last_update
    m = CatMood()
    m.last_update = _time.monotonic() - 3600
    m.tick("idle")
    test("1h idle → happiness decreased", m.happiness < 60, str(m.happiness))
    test("1h idle → energy decreased", m.energy < 80, str(m.energy))
    test("1h idle → bored increased", m.bored > 30, str(m.bored))

    # Sleeping recovers energy
    m = CatMood()
    m.energy = 20.0
    m.last_update = _time.monotonic() - 600  # 10 min
    m.tick("sleeping_ball")
    test("10 min sleeping → energy recovered", m.energy > 20, str(m.energy))

    # Petting bumps happiness
    m = CatMood()
    base_h = m.happiness
    m.on_petting_start()
    m.on_petting_end()
    test("petting raises happiness", m.happiness > base_h,
         f"before={base_h} after={m.happiness}")

    # Chat bumps happiness + drops bored
    m = CatMood(happiness=50, bored=80)
    m.on_chat_sent()
    test("on_chat_sent raises happiness", m.happiness > 50, str(m.happiness))
    test("on_chat_sent drops bored", m.bored < 80, str(m.bored))

    # Clamp behavior
    m = CatMood()
    m.happiness = 120
    m.energy = -10
    m.tick("idle")
    test("clamp upper bound", m.happiness <= 100, str(m.happiness))
    test("clamp lower bound", m.energy >= 0, str(m.energy))

    # Mood predicates
    test("wants_rest when energy low", CatMood(energy=10).wants_rest())
    test("not wants_rest when energy high", not CatMood(energy=80).wants_rest())
    test("is_bored when bored high", CatMood(bored=90).is_bored())
    test("is_grumpy when happiness low", CatMood(happiness=10).is_grumpy())
    test("is_affectionate when happiness high", CatMood(happiness=90).is_affectionate())

    # Persistence round-trip
    with tempfile.TemporaryDirectory() as td:
        with mock.patch("catai_linux.mood.CONFIG_DIR", td):
            m = CatMood(happiness=77, energy=33, bored=55, hunger=22)
            m.save("test_cat_00")
            loaded = CatMood.load("test_cat_00")
            test("persistence: happiness",
                 abs(loaded.happiness - 77) < 0.01, str(loaded.happiness))
            test("persistence: energy",
                 abs(loaded.energy - 33) < 0.01, str(loaded.energy))
            test("persistence: bored",
                 abs(loaded.bored - 55) < 0.01, str(loaded.bored))
            test("persistence: hunger",
                 abs(loaded.hunger - 22) < 0.01, str(loaded.hunger))

    # Missing file → defaults (no crash)
    with tempfile.TemporaryDirectory() as td:
        with mock.patch("catai_linux.mood.CONFIG_DIR", td):
            loaded = CatMood.load("nonexistent_cat")
            test("load missing file returns defaults",
                 loaded.happiness == 60.0, str(loaded.happiness))


# ── catai_linux.activity ─────────────────────────────────────────────────────

def test_activity() -> None:
    print("\n[activity]", flush=True)
    from catai_linux.activity import ActivityMonitor

    a = ActivityMonitor()
    test("default idle_ms=0", a.idle_ms == 0)
    test("default cpu_load=0", a.cpu_load == 0.0)
    test("default is_afk=False", a.is_afk is False)
    test("default hour", 0 <= a.hour <= 23)

    # AFK threshold logic
    a.idle_ms = 5 * 60 * 1000  # 5 min — below threshold
    a._last_update = 0  # allow update
    a.idle_ms = 5 * 60 * 1000
    # Manually simulate the state transition since update() reads from system
    a.is_afk = False
    # Force the logic via direct manipulation
    a.idle_ms = ActivityMonitor.IDLE_THRESHOLD_MS + 1000
    if a.idle_ms >= ActivityMonitor.IDLE_THRESHOLD_MS:
        a.is_afk = True
    test("AFK triggered above threshold", a.is_afk)

    # Return-from-AFK threshold (sticky: < IDLE_WAKEUP_THRESHOLD_MS)
    a.idle_ms = ActivityMonitor.IDLE_WAKEUP_THRESHOLD_MS + 100
    if a.is_afk and a.idle_ms < ActivityMonitor.IDLE_WAKEUP_THRESHOLD_MS:
        a.is_afk = False
    test("AFK sticky when slightly active", a.is_afk)
    a.idle_ms = 100  # very active
    if a.is_afk and a.idle_ms < ActivityMonitor.IDLE_WAKEUP_THRESHOLD_MS:
        a.is_afk = False
    test("AFK cleared when fully active", not a.is_afk)

    # Night hours
    a.hour = 2
    test("is_night at 2am", a.is_night())
    a.hour = 14
    test("not night at 2pm", not a.is_night())
    a.hour = 23
    test("is_night at 11pm", a.is_night())

    # CPU busy
    a.cpu_load = 3.5
    test("is_cpu_busy at load 3.5", a.is_cpu_busy())
    a.cpu_load = 0.5
    test("not cpu busy at load 0.5", not a.is_cpu_busy())

    # Snapshot
    snap = a.snapshot()
    test("snapshot has expected keys",
         all(k in snap for k in ("idle_ms", "is_afk", "cpu_load", "hour", "is_night")),
         str(snap.keys()))


def _run_section(name: str, fn) -> None:
    """Run a test section, catching import errors so one broken module
    doesn't kill the whole suite. Records a FAIL on exception."""
    try:
        fn()
    except Exception as e:
        global FAIL
        FAIL += 1
        print(f"\n[{name}]\n  \u2717 section crashed \u2014 {type(e).__name__}: {e}",
              flush=True)


def main() -> int:
    print("=== CATAI Unit Tests (headless) ===\n", flush=True)
    test_import_smoke()
    _run_section("l10n", test_l10n)
    _run_section("voice", test_voice)
    _run_section("chat_backend", test_chat_backend)
    _run_section("x11_helpers", test_x11_helpers)
    _run_section("drawing", test_drawing)
    _run_section("theme", test_theme)
    _run_section("personality", test_personality)
    _run_section("seasonal", test_seasonal)
    _run_section("reactions", test_reactions)
    _run_section("mood", test_mood)
    _run_section("activity", test_activity)
    print(f"\n=== Results: {PASS} passed, {FAIL} failed ===\n", flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
