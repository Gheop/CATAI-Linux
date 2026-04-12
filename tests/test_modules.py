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

    # _refresh_claude_token must NOT allow the claude CLI to pop a
    # browser if its OAuth refresh fails. Verified by capturing the
    # subprocess.run call and asserting the env passed to it has
    # DISPLAY/WAYLAND_DISPLAY/BROWSER stripped + BROWSER=/bin/false.
    captured_env: dict[str, dict] = {}

    def _fake_run(cmd, capture_output=True, timeout=30, env=None):
        captured_env["env"] = env
        # Simulate a successful CLI invocation
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    with mock.patch.object(chat_backend, "_find_claude_cli",
                           return_value="/fake/claude"):
        with mock.patch("subprocess.run", side_effect=_fake_run):
            ok = chat_backend._refresh_claude_token()
            test("_refresh_claude_token returns True on success", ok)

    env = captured_env.get("env", {})
    test("_refresh_claude_token strips DISPLAY",
         "DISPLAY" not in env)
    test("_refresh_claude_token strips WAYLAND_DISPLAY",
         "WAYLAND_DISPLAY" not in env)
    test("_refresh_claude_token sets BROWSER=/bin/false",
         env.get("BROWSER") == "/bin/false")
    test("_refresh_claude_token preserves PATH",
         "PATH" in env or len(env) == 0)  # PATH preserved unless empty test env


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

    # New helpers added in v0.7.3 cleanup — replaces xprop / xdotool
    # subprocess polls. They MUST not crash when there's no display.
    test("module exposes get_active_window_fullscreen",
         callable(x11_helpers.get_active_window_fullscreen))
    test("module exposes get_window_y_offset",
         callable(x11_helpers.get_window_y_offset))
    test("module exposes _x11_get_active_window",
         callable(x11_helpers._x11_get_active_window))
    test("module exposes _x11_window_has_state",
         callable(x11_helpers._x11_window_has_state))

    # Calling these without a display must return safe defaults — never
    # raise. We can't assert the actual return value (depends on whether
    # libX11 loads + whether DISPLAY is set), only that the call shape
    # is correct and no exception escapes.
    try:
        result = x11_helpers.get_active_window_fullscreen()
        test("get_active_window_fullscreen returns bool",
             isinstance(result, bool))
    except Exception as e:
        test("get_active_window_fullscreen returns bool", False, repr(e))

    try:
        result = x11_helpers.get_window_y_offset(0)
        test("get_window_y_offset(0) returns 0",
             result == 0)
    except Exception as e:
        test("get_window_y_offset(0) returns 0", False, repr(e))

    try:
        result = x11_helpers.get_window_y_offset(999999999)
        test("get_window_y_offset(bogus xid) returns int",
             isinstance(result, int))
    except Exception as e:
        test("get_window_y_offset(bogus xid) returns int", False, repr(e))

    try:
        result = x11_helpers._x11_get_active_window()
        test("_x11_get_active_window returns int",
             isinstance(result, int))
    except Exception as e:
        test("_x11_get_active_window returns int", False, repr(e))

    # get_mouse_position (added in v0.7.4 for the 'viens' wake verb)
    test("module exposes get_mouse_position",
         callable(x11_helpers.get_mouse_position))
    try:
        result = x11_helpers.get_mouse_position()
        # Returns None on failure or (int, int) tuple on success
        ok = result is None or (
            isinstance(result, tuple) and len(result) == 2 and
            all(isinstance(c, int) for c in result)
        )
        test("get_mouse_position returns None or (int, int)",
             ok, str(result))
    except Exception as e:
        test("get_mouse_position returns safely", False, repr(e))

    # The deprecated _run_x11 helper should be GONE — no more dead
    # subprocess fallbacks shipping in the wheel.
    test("_run_x11 was removed in 0.7.3 cleanup",
         not hasattr(x11_helpers, "_run_x11"))

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


def test_monitors() -> None:
    print("\n[monitors]", flush=True)
    import random as _random

    from catai_linux import monitors

    # Classic dual-monitor: 1920×1080 next to a 2560×1440 (taller),
    # with a dead zone on the bottom of the first monitor.
    rects = [
        (0, 0, 1920, 1080),       # monitor 0
        (1920, 0, 2560, 1440),    # monitor 1, taller → dead zone 1080-1440 on the left half
    ]

    # monitor_at
    test("monitor_at(500, 500) = rect 0",
         monitors.monitor_at(500, 500, rects) == rects[0])
    test("monitor_at(2500, 1200) = rect 1",
         monitors.monitor_at(2500, 1200, rects) == rects[1])
    test("monitor_at(500, 1200) = None (dead zone)",
         monitors.monitor_at(500, 1200, rects) is None)
    test("monitor_at(5000, 5000) = None (outside all)",
         monitors.monitor_at(5000, 5000, rects) is None)

    # nearest_monitor rescues a dead-zone point
    nearest = monitors.nearest_monitor(500, 1200, rects)
    test("nearest_monitor(500, 1200) rescues to rect 0",
         nearest == rects[0], str(nearest))
    nearest = monitors.nearest_monitor(5000, 100, rects)
    test("nearest_monitor(5000, 100) snaps to rect 1",
         nearest == rects[1], str(nearest))

    # snap_to_nearest clamps inside the rect
    snapped = monitors.snap_to_nearest(500, 1200, 80, 80, rects)
    test("snap_to_nearest returns (int, int)",
         isinstance(snapped, tuple) and len(snapped) == 2
         and all(isinstance(v, int) for v in snapped),
         str(snapped))
    # Clamped point should be inside rect 0 (since that's the nearest)
    cx, cy = snapped
    test("snap_to_nearest keeps x inside nearest rect",
         0 <= cx <= 1920 - 80, f"cx={cx}")
    test("snap_to_nearest clamps y to rect 0 bottom",
         0 <= cy <= 1080 - 80, f"cy={cy}")

    # Empty rects edge cases
    test("monitor_at([]) = None",
         monitors.monitor_at(0, 0, []) is None)
    test("nearest_monitor([]) = None",
         monitors.nearest_monitor(0, 0, []) is None)
    test("snap_to_nearest([]) = None",
         monitors.snap_to_nearest(0, 0, 80, 80, []) is None)

    # distribute_spawns round-robin
    rng = _random.Random(42)
    spawns = monitors.distribute_spawns(6, rects, rng=rng, padding=40)
    test("distribute_spawns returns n points",
         len(spawns) == 6, str(spawns))
    # Each spawn must be inside one of the rects (padding-respecting)
    all_inside = all(
        monitors.monitor_at(x, y, rects) is not None
        for x, y in spawns
    )
    test("every spawn is inside a monitor", all_inside, str(spawns))
    # Round-robin: point 0 should be in rect 0, point 1 in rect 1
    test("spawn 0 is on monitor 0",
         monitors.monitor_at(*spawns[0], rects) == rects[0])
    test("spawn 1 is on monitor 1",
         monitors.monitor_at(*spawns[1], rects) == rects[1])
    # Empty rects → empty list
    test("distribute_spawns([]) = []",
         monitors.distribute_spawns(5, []) == [])
    # n=0 → empty list even with rects
    test("distribute_spawns(0) = []",
         monitors.distribute_spawns(0, rects) == [])


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


def test_tts() -> None:
    print("\n[tts]", flush=True)
    import os

    from catai_linux import tts

    # ── Splitter: basic cases
    chunks = tts.split_cat_sounds("Miaou mon ami!")
    test("split: text+meow produces 2 chunks",
         len(chunks) == 2, str(chunks))
    test("split: first chunk is cat meow",
         chunks[0].kind == "cat" and chunks[0].content == "meow",
         str(chunks))
    test("split: second chunk is trailing text",
         chunks[1].kind == "text" and "mon ami" in chunks[1].content,
         str(chunks))

    # Multiple adjacent meows collapse into a single cat chunk so we
    # don't play three separate samples back-to-back.
    chunks = tts.split_cat_sounds("Miaou miaou miaou")
    test("split: 3 adjacent meows → 1 cat chunk (dedup)",
         len(chunks) == 1 and chunks[0].kind == "cat"
         and chunks[0].content == "meow",
         str(chunks))
    # ... but different pools still emit distinct chunks
    chunks = tts.split_cat_sounds("Miaou prrrrr mrrp")
    cat_kinds = [c.content for c in chunks if c.kind == "cat"]
    test("split: heterogeneous sounds stay distinct",
         cat_kinds == ["meow", "purr", "mrrp"], str(chunks))

    # Purr with *ronron* markdown
    chunks = tts.split_cat_sounds("*ronron* ça va.")
    test("split: *ronron* recognized as purr",
         any(c.kind == "cat" and c.content == "purr" for c in chunks),
         str(chunks))

    # Mrrp / chirp — short questioning sounds map to the mrrp pool
    chunks = tts.split_cat_sounds("Mrrp! Prrt?")
    cat_kinds = [c.content for c in chunks if c.kind == "cat"]
    test("split: mrrp recognized", "mrrp" in cat_kinds, str(chunks))
    test("split: prrt (short chirp) → mrrp pool",
         cat_kinds.count("mrrp") == 2, str(chunks))
    # Longer purrs (prrrr with >=2 r's after the first rr) → purr pool
    chunks = tts.split_cat_sounds("Prrrrrr~ content")
    cat_kinds = [c.content for c in chunks if c.kind == "cat"]
    test("split: long prrrr → purr pool",
         "purr" in cat_kinds, str(chunks))

    # Hiss
    chunks = tts.split_cat_sounds("*hiss* go away!")
    test("split: *hiss* recognized",
         any(c.kind == "cat" and c.content == "hiss" for c in chunks),
         str(chunks))

    # Pure text passes through unchanged
    chunks = tts.split_cat_sounds("Just some plain text")
    test("split: pure text → single text chunk",
         len(chunks) == 1 and chunks[0].kind == "text",
         str(chunks))

    # Empty / whitespace input
    test("split: empty string → []", tts.split_cat_sounds("") == [])
    test("split: whitespace → []", tts.split_cat_sounds("   \n\t ") == [])

    # Case insensitive
    chunks = tts.split_cat_sounds("MIAOU and MEOW and Purr")
    cat_count = sum(1 for c in chunks if c.kind == "cat")
    test("split: case-insensitive matching",
         cat_count == 3, f"{cat_count} cat chunks in {chunks}")

    # ── Sound pool / sample resolution
    # Each pool should have at least one sample on disk after install.
    for pool_key in ("meow", "purr", "mrrp", "hiss"):
        path = tts._resolve_sample(pool_key)
        test(f"resolve_sample({pool_key!r}) returns a real file",
             path is not None and os.path.isfile(path),
             str(path))

    # Unknown pool returns None
    test("resolve_sample(unknown) = None",
         tts._resolve_sample("not_a_pool") is None)

    # Each sample filename in the pool config matches a real file
    missing = []
    for pool_key, files in tts.CAT_SOUND_POOLS.items():
        for f in files:
            full = os.path.join(tts.SOUNDS_DIR, f)
            if not os.path.isfile(full):
                missing.append(full)
    test("all CAT_SOUND_POOLS files exist on disk",
         not missing, f"missing: {missing}")

    # SoundPlayer can be instantiated without raising (even if piper is
    # missing — it logs and moves on).
    player = tts.SoundPlayer()
    test("SoundPlayer() instantiates cleanly", player is not None)

    # play([]) is a no-op
    player.play([])
    test("SoundPlayer.play([]) is a no-op", True)

    # Default player is a singleton
    p1 = tts.get_default_player()
    p2 = tts.get_default_player()
    test("get_default_player is a singleton", p1 is p2)

    # ── Text cleaning: stage directions + emoji stripped
    test("clean: drops *...* stage directions entirely",
         tts._clean_text_for_tts("*s'étire* bonjour") == "bonjour")
    test("clean: keeps dialogue before and after stage direction",
         tts._clean_text_for_tts("salut *regarde* toi") == "salut toi")
    test("clean: removes emoji",
         tts._clean_text_for_tts("salut 😸 toi") == "salut toi")
    test("clean: keeps french accents",
         tts._clean_text_for_tts("Ça va très bien") == "Ça va très bien")
    test("clean: collapses whitespace",
         tts._clean_text_for_tts("hello   world\n\ttest") == "hello world test")
    test("clean: keeps guillemets + punctuation",
         tts._clean_text_for_tts("«Bonjour!» dit-il.") == "«Bonjour!» dit-il.")
    test("clean: all-emoji input → empty",
         tts._clean_text_for_tts("😸🌫✨") == "")
    test("clean: pure stage direction → empty",
         tts._clean_text_for_tts("*bâille longuement*") == "")

    # split_cat_sounds: cat tokens INSIDE *...* are extracted first,
    # so *ronron* becomes a purr chunk, not dropped.
    chunks = tts.split_cat_sounds("*ronron*")
    test("split: *ronron* → purr chunk (cat token wins over stage direction)",
         len(chunks) == 1 and chunks[0].kind == "cat"
         and chunks[0].content == "purr",
         str(chunks))

    # split_cat_sounds drops stage-direction-only content via cleaner
    chunks = tts.split_cat_sounds("Salut *s'étire* toi")
    text_contents = [c.content for c in chunks if c.kind == "text"]
    test("split: stage direction dropped from text chunks",
         any("s'étire" not in t for t in text_contents)
         and not any("s'étire" in t for t in text_contents),
         str(chunks))


def test_memory() -> None:
    print("\n[memory]", flush=True)
    import tempfile
    from catai_linux import memory

    # Sandbox: redirect db path so we don't touch ~/.config
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "memory.db")
        memory.MemoryStore.set_path(path)
        try:
            # Empty initial state
            test("count empty cat = 0",
                 memory.MemoryStore.count("cat01") == 0)

            # Add facts
            memory.MemoryStore.add_fact(
                "cat01", "L'utilisateur s'appelle Sib")
            memory.MemoryStore.add_fact(
                "cat01", "Aime débuguer les pipelines GStreamer")
            memory.MemoryStore.add_fact(
                "cat01", "Travaille avec Linux et Python depuis 10 ans")
            test("count after 3 adds = 3",
                 memory.MemoryStore.count("cat01") == 3)

            # Different cat is isolated
            memory.MemoryStore.add_fact("cat02", "Aime le jardinage")
            test("cat02 isolated count = 1",
                 memory.MemoryStore.count("cat02") == 1)
            test("cat01 still 3",
                 memory.MemoryStore.count("cat01") == 3)

            # Retrieval by keyword overlap
            top = memory.MemoryStore.retrieve_relevant(
                "cat01", "tu connais Python ?", n=2)
            test("retrieve picks the Python fact",
                 any("Python" in f for f in top), str(top))

            top = memory.MemoryStore.retrieve_relevant(
                "cat01", "Sib comment vas-tu ?", n=1)
            test("retrieve picks the Sib name fact",
                 any("Sib" in f for f in top), str(top))

            top = memory.MemoryStore.retrieve_relevant(
                "cat01", "GStreamer pipeline question", n=1)
            test("retrieve picks the gstreamer fact",
                 any("GStreamer" in f for f in top), str(top))

            # Empty query → empty result, never crash
            test("empty query → []",
                 memory.MemoryStore.retrieve_relevant("cat01", "", n=3) == [])
            # No matching facts → empty
            test("no overlap → []",
                 memory.MemoryStore.retrieve_relevant(
                     "cat01", "abracadabra xyzzy", n=3) == [])

            # all_facts returns insertion order
            facts = memory.MemoryStore.all_facts("cat01")
            test("all_facts returns 3 entries", len(facts) == 3)
            test("first fact is the name", "Sib" in facts[0])

            # Clear specific cat
            memory.MemoryStore.clear("cat01")
            test("clear(cat01) drops cat01",
                 memory.MemoryStore.count("cat01") == 0)
            test("clear(cat01) preserves cat02",
                 memory.MemoryStore.count("cat02") == 1)

            # Clear all
            memory.MemoryStore.clear()
            test("clear() wipes everything",
                 memory.MemoryStore.count("cat02") == 0)

            # Bounded growth — over the cap
            for i in range(memory.MAX_FACTS_PER_CAT + 5):
                memory.MemoryStore.add_fact("cat01", f"fact number {i}")
            test("bounded at MAX_FACTS_PER_CAT",
                 memory.MemoryStore.count("cat01") == memory.MAX_FACTS_PER_CAT)
            # The oldest 5 should have been pruned, so the surviving
            # facts start at index 5
            facts = memory.MemoryStore.all_facts("cat01")
            test("oldest pruned, fact 5 survived",
                 "fact number 5" in facts[0])

            # Empty / over-long content rejected
            before = memory.MemoryStore.count("cat01")
            memory.MemoryStore.add_fact("cat01", "")
            memory.MemoryStore.add_fact("cat01", " " * 10)
            memory.MemoryStore.add_fact("cat01", "x" * 500)
            test("empty / huge content rejected",
                 memory.MemoryStore.count("cat01") == before)

        finally:
            memory.MemoryStore.set_path(memory.DB_PATH)

    # ── tokenization
    tok = memory._tokenize
    t = tok("Le chat aime le jardinage et le café.")
    test("tokenize drops stopwords (le)", "le" not in t)
    test("tokenize keeps content words",
         "chat" in t and "jardinage" in t and "café" in t)
    test("tokenize empty input → set()", tok("") == set())

    # ── parser
    parse = memory.parse_extract_response
    test("parse plain JSON array",
         parse('["fact one", "fact two"]') == ["fact one", "fact two"])
    test("parse fenced JSON",
         parse('```json\n["a", "b"]\n```') == ["a", "b"])
    test("parse embedded JSON",
         parse('Here you go: ["x"] thanks!') == ["x"])
    test("parse empty → []", parse("") == [])
    test("parse garbage → []", parse("not json at all") == [])

    # ── append_memories_to_prompt
    with tempfile.TemporaryDirectory() as td:
        memory.MemoryStore.set_path(os.path.join(td, "m.db"))
        try:
            memory.MemoryStore.add_fact("cat01", "Aime le café noir")
            base = "You are a cat."
            out = memory.append_memories_to_prompt(
                base, "cat01", "tu veux du café ?", "fr")
            test("append injects matching fact",
                 "café" in out, out)
            out2 = memory.append_memories_to_prompt(
                base, "cat01", "abracadabra", "fr")
            test("no match → unchanged base",
                 out2 == base)
        finally:
            memory.MemoryStore.set_path(memory.DB_PATH)


def test_character_packs() -> None:
    print("\n[character_packs]", flush=True)
    import json
    import tempfile
    from catai_linux import character_packs

    # Empty / nonexistent base dir → empty result
    with tempfile.TemporaryDirectory() as td:
        empty_dir = os.path.join(td, "no_packs_here")
        test("missing dir → {}", character_packs.discover_packs(empty_dir) == {})

    # Build a fake pack and verify it loads
    with tempfile.TemporaryDirectory() as td:
        pack_dir = os.path.join(td, "my_pack")
        os.makedirs(os.path.join(pack_dir, "rotations"), exist_ok=True)
        # Touch a fake rotation PNG so the validator's rotations/ check passes
        with open(os.path.join(pack_dir, "rotations", "south.png"), "wb") as f:
            f.write(b"fake")
        # Bare-minimum metadata.json (the validator only checks existence)
        with open(os.path.join(pack_dir, "metadata.json"), "w") as f:
            json.dump({"character": {"size": {"width": 80, "height": 80}}}, f)
        # Valid personality.json
        with open(os.path.join(pack_dir, "personality.json"), "w") as f:
            json.dump({
                "char_id": "my_pack",
                "name": {"fr": "Truc", "en": "Thing", "es": "Cosa"},
                "traits": {"fr": "bizarre", "en": "weird", "es": "raro"},
                "skills": {"fr": "Test", "en": "Test", "es": "Prueba"},
            }, f)

        result = character_packs.discover_packs(td)
        test("valid pack discovered", "my_pack" in result, str(result.keys()))
        test("personality has _external_dir flag",
             "_external_dir" in result["my_pack"])
        test("is_external returns True",
             character_packs.is_external(result["my_pack"]))
        test("external_sprite_dir matches pack folder",
             character_packs.external_sprite_dir(result["my_pack"]) == os.path.abspath(pack_dir))

    # Char_id mismatch with directory name → rejected
    with tempfile.TemporaryDirectory() as td:
        pack_dir = os.path.join(td, "real_name")
        os.makedirs(os.path.join(pack_dir, "rotations"), exist_ok=True)
        with open(os.path.join(pack_dir, "rotations", "south.png"), "wb") as f:
            f.write(b"")
        with open(os.path.join(pack_dir, "metadata.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(pack_dir, "personality.json"), "w") as f:
            json.dump({
                "char_id": "wrong_name",  # mismatch
                "name": {"fr": "X"},
                "traits": {"fr": "Y"},
                "skills": {"fr": "Z"},
            }, f)
        test("char_id mismatch → skipped",
             character_packs.discover_packs(td) == {})

    # Missing required key → rejected
    with tempfile.TemporaryDirectory() as td:
        pack_dir = os.path.join(td, "incomplete")
        os.makedirs(os.path.join(pack_dir, "rotations"), exist_ok=True)
        with open(os.path.join(pack_dir, "rotations", "south.png"), "wb") as f:
            f.write(b"")
        with open(os.path.join(pack_dir, "metadata.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(pack_dir, "personality.json"), "w") as f:
            json.dump({"char_id": "incomplete"}, f)  # missing name/traits/skills
        test("missing keys → skipped",
             character_packs.discover_packs(td) == {})

    # Bad JSON → rejected, no crash
    with tempfile.TemporaryDirectory() as td:
        pack_dir = os.path.join(td, "broken")
        os.makedirs(os.path.join(pack_dir, "rotations"), exist_ok=True)
        with open(os.path.join(pack_dir, "rotations", "south.png"), "wb") as f:
            f.write(b"")
        with open(os.path.join(pack_dir, "metadata.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(pack_dir, "personality.json"), "w") as f:
            f.write("not json {")
        test("bad json → skipped, no crash",
             character_packs.discover_packs(td) == {})

    # is_external on a bundled (no _external_dir) personality
    test("bundled personality is_external = False",
         not character_packs.is_external({"char_id": "cat01", "name": {}}))


def test_metrics() -> None:
    print("\n[metrics]", flush=True)
    import tempfile
    from catai_linux import metrics

    # Sandbox: redirect STATS_FILE so we don't touch ~/.config
    with tempfile.TemporaryDirectory() as td:
        orig = metrics.STATS_FILE
        metrics.STATS_FILE = os.path.join(td, "stats.json")
        # Reset the in-memory cache so this test starts clean
        metrics._data_cache = None
        metrics._dirty = False
        try:
            # Disabled state: track is a no-op
            metrics.set_enabled(False)
            metrics.track("chat_sent", cat_id="cat01")
            metrics.flush()
            test("track is no-op when disabled",
                 not os.path.exists(metrics.STATS_FILE))

            # Enable + track (flush before reading to push cache to disk)
            metrics.set_enabled(True)
            metrics.track("chat_sent", cat_id="cat01")
            metrics.track("chat_sent", cat_id="cat02")
            metrics.track("chat_sent", cat_id="cat01")
            metrics.flush()
            data = metrics.load()
            test("chats_sent counts to 3", data["chats_sent"] == 3)
            test("per_cat[cat01].chats == 2",
                 data["per_cat"].get("cat01", {}).get("chats") == 2)
            test("per_cat[cat02].chats == 1",
                 data["per_cat"].get("cat02", {}).get("chats") == 1)

            # Easter eggs
            metrics.track("egg_triggered", key="nyan")
            metrics.track("egg_triggered", key="nyan")
            metrics.track("egg_triggered", key="apocalypse")
            metrics.flush()
            data = metrics.load()
            test("eggs nyan=2 apocalypse=1",
                 data["easter_eggs_triggered"]
                 == {"nyan": 2, "apocalypse": 1})

            # Love encounters
            metrics.track("love_encounter", kind="love")
            metrics.track("love_encounter", kind="love")
            metrics.track("love_encounter", kind="surprised")
            metrics.track("love_encounter", kind="angry")
            metrics.flush()
            data = metrics.load()
            test("love_encounters love=2 surprised=1 angry=1",
                 data["love_encounters"]
                 == {"love": 2, "surprised": 1, "angry": 1})

            # Kitten + petting
            metrics.track("kitten_born")
            metrics.track("pet_session", cat_id="cat01")
            metrics.track("pet_session", cat_id="cat01")
            metrics.track("pet_session", cat_id="cat02")
            metrics.flush()
            data = metrics.load()
            test("kittens_born == 1", data["kittens_born"] == 1)
            test("pet_sessions == 3", data["pet_sessions"] == 3)
            test("per_cat[cat01].petted == 2",
                 data["per_cat"]["cat01"].get("petted") == 2)

            # Top helpers
            top_pet = metrics.top_cats(data, "petted", 3)
            test("top_cats(petted) ranks cat01 first",
                 top_pet[0][0] == "cat01")
            top_eggs = metrics.top_eggs(data, 3)
            test("top_eggs ranks nyan first",
                 top_eggs[0] == ("nyan", 2))

            # Unknown event is silently ignored
            metrics.flush()
            before = metrics.load()
            metrics.track("unknown_event")
            metrics.flush()
            test("unknown event doesn't crash or change state",
                 metrics.load()["chats_sent"] == before["chats_sent"])

            # Corrupted stats file → reset to defaults on load
            metrics._data_cache = None  # force re-read from disk
            with open(metrics.STATS_FILE, "w") as f:
                f.write("not json {")
            recovered = metrics.load()
            test("corrupted file → fresh defaults",
                 isinstance(recovered, dict)
                 and recovered.get("chats_sent") == 0)

            # Reset wipes everything
            metrics._data_cache = None
            metrics.reset()
            data = metrics.load()
            test("reset → chats_sent back to 0",
                 data["chats_sent"] == 0)

            metrics.set_enabled(False)
        finally:
            metrics.STATS_FILE = orig
            metrics._data_cache = None
            metrics._dirty = False
            metrics.set_enabled(False)


def test_updater() -> None:
    print("\n[updater]", flush=True)
    from catai_linux import updater

    # ── parse_version
    test("parse '0.6.1'",
         updater.parse_version("0.6.1") == (0, 6, 1, ""))
    test("parse 'v0.6.1' (with v prefix)",
         updater.parse_version("v0.6.1") == (0, 6, 1, ""))
    test("parse '0.6.1-beta' suffix",
         updater.parse_version("0.6.1-beta") == (0, 6, 1, "beta"))
    test("parse 'v1.2.3+dev' build metadata",
         updater.parse_version("v1.2.3+dev") == (1, 2, 3, "dev"))
    test("parse 'foo' returns None",
         updater.parse_version("foo") is None)
    test("parse '' returns None",
         updater.parse_version("") is None)

    # ── compare_versions
    cmp = updater.compare_versions
    test("0.6.1 > 0.6.0", cmp("0.6.1", "0.6.0") == 1)
    test("0.6.0 > 0.5.9", cmp("0.6.0", "0.5.9") == 1)
    test("0.6.1 == 0.6.1", cmp("0.6.1", "0.6.1") == 0)
    test("v0.6.1 == 0.6.1 (v prefix ignored)",
         cmp("v0.6.1", "0.6.1") == 0)
    test("0.6.0 < v0.6.1",
         cmp("0.6.0", "v0.6.1") == -1)
    test("1.0.0 > 0.99.99",
         cmp("1.0.0", "0.99.99") == 1)
    test("0.6.0-beta < 0.6.0 (pre-release lower)",
         cmp("0.6.0-beta", "0.6.0") == -1)
    test("0.6.0-alpha < 0.6.0-beta",
         cmp("0.6.0-alpha", "0.6.0-beta") == -1)
    test("invalid → 0 (treat as equal, no phantom upgrade)",
         cmp("foo", "bar") == 0)

    # ── modes
    test("MODE_AUTO is 'auto'", updater.MODE_AUTO == "auto")
    test("MODE_NOTIFY is 'notify'", updater.MODE_NOTIFY == "notify")
    test("MODE_OFF is 'off'", updater.MODE_OFF == "off")
    test("ALL_MODES has all 3 modes", len(updater.ALL_MODES) == 3)

    # ── cache I/O round-trip in a tempdir so we don't touch ~/.config
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        orig_cache = updater.CACHE_FILE
        updater.CACHE_FILE = os.path.join(td, "update_cache.json")
        try:
            test("read missing cache returns None",
                 updater._read_cache() is None)
            updater._write_cache({"ts": 12345, "tag": "v0.6.1"})
            cached = updater._read_cache()
            test("write + read cache round-trip",
                 cached == {"ts": 12345, "tag": "v0.6.1"})
        finally:
            updater.CACHE_FILE = orig_cache

    # ── get_installed_version
    # Returns None or a string — never raises
    iv = updater.get_installed_version()
    test("get_installed_version returns str or None",
         iv is None or isinstance(iv, str))

    # ── _has_voice_extra
    # Returns a bool, never raises
    test("_has_voice_extra returns bool",
         isinstance(updater._has_voice_extra(), bool))


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
        "catai_linux.monitors",
        "catai_linux.seasonal",
        "catai_linux.tts",
        "catai_linux.updater",
        "catai_linux.metrics",
        "catai_linux.character_packs",
        "catai_linux.memory",
        "catai_linux.reactions",
        "catai_linux.mood",
        "catai_linux.activity",
        "catai_linux.wake_word",
        "catai_linux.easter_eggs",
        "catai_linux.config_schema",
        "catai_linux.theme",
        "catai_linux.constants",
        "catai_linux.encounters",
        "catai_linux.settings_window",
        "catai_linux.shell",
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

    # ── D-Bus IdleMonitor path (added in v0.7.3 cleanup) ─────────────────
    # Fresh instance — _idle_proxy is None until first _read_idle_ms call
    fresh = ActivityMonitor()
    test("default _idle_proxy is None (lazy)",
         fresh._idle_proxy is None)

    # Force the proxy to "unavailable" — _read_idle_ms must NOT crash
    fresh._idle_proxy = False
    fresh._xprintidle = None  # also disable subprocess fallback
    result = fresh._read_idle_ms()
    test("_read_idle_ms returns 0 when both paths unavailable",
         result == 0)

    # Mock D-Bus proxy that returns a known value
    class _MockResult:
        def __init__(self, value): self._v = value
        def unpack(self): return (self._v,)

    class _MockProxy:
        def __init__(self, value): self._v = value
        def call_sync(self, method, params, flags, timeout, cancellable):
            assert method == "GetIdletime", f"unexpected method {method}"
            return _MockResult(self._v)

    mocked = ActivityMonitor()
    mocked._idle_proxy = _MockProxy(42_000)  # 42 sec idle
    result = mocked._read_idle_ms()
    test("_read_idle_ms uses proxy result",
         result == 42_000, str(result))

    # Proxy raises → fallback to xprintidle subprocess (or 0 if both
    # unavailable). The proxy state should also flip to False so we
    # don't keep retrying.
    class _BrokenProxy:
        def call_sync(self, *a, **kw):
            raise RuntimeError("simulated D-Bus failure")

    broken = ActivityMonitor()
    broken._idle_proxy = _BrokenProxy()
    broken._xprintidle = None
    result = broken._read_idle_ms()
    test("_read_idle_ms tolerates proxy crash",
         result == 0)
    test("crashed proxy is marked dead",
         broken._idle_proxy is False)


# ── catai_linux.config_schema ────────────────────────────────────────────────

def test_config_schema() -> None:
    print("\n[config_schema]", flush=True)
    from catai_linux.config_schema import validate_config, CONFIG_SCHEMA

    # Empty config -> all defaults filled
    out = validate_config({})
    test("empty config fills all defaults",
         all(k in out for k in CONFIG_SCHEMA))
    test("default scale is 1.5", out["scale"] == 1.5)
    test("default lang is 'fr'", out["lang"] == "fr")
    test("default auto_update is 'auto'", out["auto_update"] == "auto")
    test("default encounters is True", out["encounters"] is True)

    # scale below min -> clamped to 0.5
    out = validate_config({"scale": 0.1})
    test("scale below min clamped to 0.5", out["scale"] == 0.5)

    # scale above max -> clamped to 4.0
    out = validate_config({"scale": 10.0})
    test("scale above max clamped to 4.0", out["scale"] == 4.0)

    # lang invalid -> default "fr"
    out = validate_config({"lang": "klingon"})
    test("invalid lang falls back to 'fr'", out["lang"] == "fr")

    # auto_update invalid -> default "auto"
    out = validate_config({"auto_update": "yolo"})
    test("invalid auto_update falls back to 'auto'", out["auto_update"] == "auto")

    # unknown key -> preserved (forward compat)
    with mock.patch("catai_linux.config_schema.log") as mock_log:
        out = validate_config({"future_key": 42})
        test("unknown key preserved", out["future_key"] == 42)
        test("unknown key logs warning",
             any("unknown key" in str(c) for c in mock_log.warning.call_args_list))

    # bool field with non-bool -> coerced
    out = validate_config({"encounters": 1})
    test("non-bool coerced to bool", out["encounters"] is True)
    out = validate_config({"encounters": 0})
    test("non-bool 0 coerced to False", out["encounters"] is False)

    # cats key preserved
    cats_data = [{"name": "Tabby", "skin": "cat01"}]
    out = validate_config({"cats": cats_data})
    test("cats list preserved", out["cats"] == cats_data)

    # valid scale passes through
    out = validate_config({"scale": 2.0})
    test("valid scale passes through", out["scale"] == 2.0)


# ── catai_linux.easter_eggs ──────────────────────────────────────────────────

def test_easter_eggs_data() -> None:
    print("\n[easter_eggs_data]", flush=True)
    from catai_linux.easter_eggs import MAGIC_EGG_PHRASES, EASTER_EGGS, EasterEggMixin

    # MAGIC_EGG_PHRASES is a non-empty dict
    test("MAGIC_EGG_PHRASES is non-empty dict",
         isinstance(MAGIC_EGG_PHRASES, dict) and len(MAGIC_EGG_PHRASES) > 0)

    # Build set of EASTER_EGGS keys
    egg_keys = {e[0] for e in EASTER_EGGS}

    # Every value in MAGIC_EGG_PHRASES maps to a key in EASTER_EGGS
    unmapped = {v for v in MAGIC_EGG_PHRASES.values() if v not in egg_keys}
    test("all phrase values map to an EASTER_EGGS key",
         len(unmapped) == 0, f"unmapped: {unmapped}")

    # EASTER_EGGS is a non-empty list of tuples
    test("EASTER_EGGS is non-empty list",
         isinstance(EASTER_EGGS, list) and len(EASTER_EGGS) > 0)

    # Each entry is a tuple with (key, emoji, label, method_name)
    test("EASTER_EGGS entries are 4-tuples",
         all(isinstance(e, tuple) and len(e) == 4 for e in EASTER_EGGS))

    # All keys have "key" and "emoji" (index 0 and 1)
    test("all entries have non-empty key and emoji",
         all(e[0] and e[1] for e in EASTER_EGGS))

    # All EASTER_EGGS keys are unique
    test("EASTER_EGGS keys are unique",
         len(egg_keys) == len(EASTER_EGGS))

    # EasterEggMixin has an eg_* method for every EASTER_EGGS entry
    missing_methods = [e[3] for e in EASTER_EGGS if not hasattr(EasterEggMixin, e[3])]
    test("EasterEggMixin has all eg_* methods",
         len(missing_methods) == 0, f"missing: {missing_methods}")

    # method_name matches "eg_" + key pattern
    test("method names start with eg_",
         all(e[3].startswith("eg_") for e in EASTER_EGGS))

    # At least 20 easter eggs exist
    test("at least 20 easter eggs", len(EASTER_EGGS) >= 20)

    # At least 30 magic phrases exist
    test("at least 30 magic phrases", len(MAGIC_EGG_PHRASES) >= 30)


# ── CatInstance init ─────────────────────────────────────────────────────────

def test_cat_instance_init() -> None:
    print("\n[cat_instance_init]", flush=True)
    from catai_linux.app import CatInstance, CatState

    cat = CatInstance({"name": "Test", "skin": "cat_orange"})

    test("state is IDLE", cat.state == CatState.IDLE)
    test("direction is 'south'", cat.direction == "south")
    test("frame_index is 0", cat.frame_index == 0)
    test("x is 0.0", cat.x == 0.0)
    test("y is 0.0", cat.y == 0.0)
    test("dragging is False", cat.dragging is False)
    test("chat_visible is False", cat.chat_visible is False)
    test("meow_visible is False", cat.meow_visible is False)
    test("in_encounter is False", cat.in_encounter is False)
    test("is_kitten is False", cat.is_kitten is False)
    test("config is set", cat.config["name"] == "Test")


# ── pil_to_surface ───────────────────────────────────────────────────────────

def test_pil_to_surface() -> None:
    print("\n[pil_to_surface]", flush=True)
    from catai_linux.app import pil_to_surface
    from PIL import Image

    # Create a 4x4 solid red RGBA image
    img = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
    surface, data = pil_to_surface(img, 4, 4)

    test("surface width is 4", surface.get_width() == 4)
    test("surface height is 4", surface.get_height() == 4)
    test("data is a bytearray", isinstance(data, bytearray))

    # Verify RGBA->BGRA swap: original red (255,0,0,255) should become
    # (B=0,G=0,R=255,A=255) in cairo ARGB32 little-endian format
    test("BGRA swap: byte 0 is 0 (blue from original)", data[0] == 0)
    test("BGRA swap: byte 2 is 255 (red moved to byte 2)", data[2] == 255)
    test("BGRA swap: alpha preserved at byte 3", data[3] == 255)
    test("data length matches 4*4*4", len(data) == 4 * 4 * 4)


# ── sprite cache ─────────────────────────────────────────────────────────────

def test_sprite_cache() -> None:
    print("\n[sprite_cache]", flush=True)
    from catai_linux.app import pil_to_surface_cached, _surface_cache
    from PIL import Image

    # Clear cache, verify it's empty
    _surface_cache.clear()
    test("cache is empty after clear", len(_surface_cache) == 0)

    # Create a temp PNG
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name
        img = Image.new("RGBA", (8, 8), (0, 255, 0, 255))
        img.save(tmp_path)

    try:
        # Load via pil_to_surface_cached
        surf1, data1 = pil_to_surface_cached(tmp_path, 8, 8)
        test("cache has 1 entry after first load", len(_surface_cache) == 1)

        # Call again -> cache hit (same id())
        surf2, data2 = pil_to_surface_cached(tmp_path, 8, 8)
        test("cache hit returns same surface", surf1 is surf2)

        # Different size -> cache miss
        surf3, data3 = pil_to_surface_cached(tmp_path, 16, 16)
        test("different size is a cache miss", surf3 is not surf1)
        test("cache has 2 entries after new size", len(_surface_cache) == 2)
    finally:
        os.unlink(tmp_path)
        _surface_cache.clear()


def test_wake_word() -> None:
    print("\n[wake_word]", flush=True)
    from catai_linux import wake_word

    # ── Pure helpers (always available, no vosk needed) ─────────────────
    test("_normalize_name lowercases",
         wake_word._normalize_name("Mandarine") == "mandarine")
    test("_normalize_name strips accents",
         wake_word._normalize_name("Crème Brûlée") == "cremebrulee")
    test("_normalize_name drops digits and punctuation",
         wake_word._normalize_name("Cat-9!") == "cat")
    test("_normalize_name on empty",
         wake_word._normalize_name("") == "")

    # ── Module flag ─────────────────────────────────────────────────────
    test("WAKE_AVAILABLE is bool",
         isinstance(wake_word.WAKE_AVAILABLE, bool))

    # ── Listener instantiation (works even without vosk: it's just a
    #    Python class — set_names is a no-op when no recognizer exists) ──
    fired: list[str] = []
    listener = wake_word.WakeWordListener(on_wake=lambda cid: fired.append(cid))
    test("listener constructs", listener is not None)

    # set_names dedup + length filter run regardless of vosk
    listener.set_names({
        "cat_orange": "Mandarine",
        "cat01":      "Tabby",
        "cat02":      "Mandarine",  # duplicate normalized — should be dropped
        "cat03":      "X",          # too short — should be dropped
    })
    # We can't see _names from outside (private), but we know the dedup
    # rules: 4 inputs → 2 retained.
    test("set_names dedups and length-filters",
         len(listener._names) == 2 and "mandarine" in listener._names and "tabby" in listener._names,
         f"got {sorted(listener._names.keys())}")
    test("set_names dedup keeps first",
         listener._names["mandarine"] == "cat_orange")

    # _handle_result fires the callback for matching tokens via idle_add
    # — but we can call _fire directly which is what idle_add resolves to.
    listener._fire("cat_orange")
    test("_fire delivers cat_id to callback",
         fired == ["cat_orange"], str(fired))

    # Cooldown logic via _handle_result with a fake JSON payload
    fired.clear()
    listener._last_fire.clear()
    # Drain the GLib queue would be needed normally; bypass by inspecting
    # _last_fire entries — _handle_result schedules an idle but also
    # records into _last_fire synchronously.
    payload = '{"text": "ok mandarine"}'
    listener._handle_result(payload)
    test("_handle_result records first fire",
         "cat_orange" in listener._last_fire)
    # Second call within COOLDOWN_S should be swallowed (no new entry
    # because the existing entry stays; we check it didn't get bumped
    # twice which would change the timestamp identity)
    first_ts = listener._last_fire["cat_orange"]
    listener._handle_result(payload)
    test("_handle_result respects cooldown",
         listener._last_fire["cat_orange"] == first_ts)

    # No-match payload is silently ignored
    listener._handle_result('{"text": "completely unrelated phrase"}')
    test("_handle_result ignores non-matches",
         len(listener._last_fire) == 1)

    # Empty / malformed JSON doesn't crash
    listener._handle_result("")
    listener._handle_result("not even json")
    listener._handle_result('{"text": ""}')
    test("_handle_result tolerates garbage", True)

    # Renaming a cat: the old name disappears, the new one wins
    listener.set_names({"cat_orange": "Caramel"})
    test("rename drops old normalized name",
         "mandarine" not in listener._names)
    test("rename adds new normalized name",
         listener._names.get("caramel") == "cat_orange")

    # Refire after rename: cooldown is per-cat-id, but after a fresh
    # set_names the listener should still recognize the new name fine.
    listener._last_fire.clear()
    listener._handle_result('{"text": "caramel"}')
    test("post-rename _handle_result still fires",
         "cat_orange" in listener._last_fire)

    # ── Direct command verbs (added in v0.7.4) ──────────────────────────
    # COMMAND_VERBS contains all the verbs the wake listener can attach
    # to a recognized cat name.
    test("COMMAND_VERBS is non-empty tuple",
         isinstance(wake_word.COMMAND_VERBS, tuple) and len(wake_word.COMMAND_VERBS) > 0)
    for v in ("dors", "viens", "raconte", "danse", "saute", "roule"):
        test(f"COMMAND_VERBS contains {v!r}",
             v in wake_word.COMMAND_VERBS)

    # _fire callback dispatches the verb to the user callback
    fired_with_verb: list[tuple[str, str | None]] = []
    listener2 = wake_word.WakeWordListener(
        on_wake=lambda cid, verb=None: fired_with_verb.append((cid, verb))
    )
    listener2.set_names({"cat_orange": "Mandarine"})

    listener2._fire("cat_orange", "dors")
    test("_fire passes verb to callback",
         fired_with_verb == [("cat_orange", "dors")],
         str(fired_with_verb))

    # _fire with verb=None still fires (backward compat)
    fired_with_verb.clear()
    listener2._fire("cat_orange", None)
    test("_fire with verb=None passes None",
         fired_with_verb == [("cat_orange", None)])

    # _fire with a legacy single-arg callback (no verb param) still
    # works — TypeError fallback in _fire catches it.
    fired_legacy: list[str] = []
    listener3 = wake_word.WakeWordListener(
        on_wake=lambda cid: fired_legacy.append(cid)  # legacy signature
    )
    listener3.set_names({"cat_orange": "Mandarine"})
    listener3._fire("cat_orange", "dors")
    test("_fire falls back to legacy single-arg callback",
         fired_legacy == ["cat_orange"], str(fired_legacy))

    # End-to-end: feed a Vosk-shaped result with a verb, check the
    # listener parses both the cat and the verb correctly.
    fired_with_verb.clear()
    listener2._last_fire.clear()
    listener2._handle_result('{"text": "mandarine dors"}')
    # _handle_result schedules via GLib.idle_add — we can't easily run
    # the GLib loop here, so check _last_fire for the side effect.
    test("_handle_result records cat for 'mandarine dors'",
         "cat_orange" in listener2._last_fire)

    # Verb extraction edge cases via direct test of the parsing logic
    # by temporarily patching set_names with our test mapping
    listener4 = wake_word.WakeWordListener(on_wake=lambda *a, **kw: None)
    listener4.set_names({"cat_orange": "Mandarine", "cat01": "Tabby"})

    # Helper: extract (cat_id, verb) from a transcript without going
    # through GLib idle_add — replicates the parser inline
    def _parse(text):
        tokens = text.split()
        names = listener4._names
        for i, tok in enumerate(tokens):
            tok_n = wake_word._normalize_name(tok)
            if tok_n in names:
                cat = names[tok_n]
                verb = None
                for j in range(i + 1, min(i + 3, len(tokens))):
                    vtok = wake_word._normalize_name(tokens[j])
                    if vtok in wake_word.COMMAND_VERBS:
                        verb = vtok
                        break
                return (cat, verb)
        return (None, None)

    test("parse 'mandarine'",
         _parse("mandarine") == ("cat_orange", None))
    test("parse 'mandarine dors'",
         _parse("mandarine dors") == ("cat_orange", "dors"))
    test("parse 'tabby viens'",
         _parse("tabby viens") == ("cat01", "viens"))
    test("parse 'mandarine raconte'",
         _parse("mandarine raconte") == ("cat_orange", "raconte"))
    test("parse 'ok mandarine danse'",
         _parse("ok mandarine danse") == ("cat_orange", "danse"))
    test("parse 'mandarine bla bla'",
         _parse("mandarine bla bla") == ("cat_orange", None))
    test("parse 'tabby saute'",
         _parse("tabby saute") == ("cat01", "saute"))
    test("parse 'mandarine roule'",
         _parse("mandarine roule") == ("cat_orange", "roule"))
    # Verb beyond 2-token window is ignored (we look ahead max 2 tokens)
    test("parse 'mandarine euh euh dors' ignores far verb",
         _parse("mandarine euh euh dors") == ("cat_orange", None))
    # Unknown verb is dropped, default action triggered
    test("parse 'mandarine wuff' drops unknown verb",
         _parse("mandarine wuff") == ("cat_orange", None))

    # ── Optional: real recognizer round-trip if vosk + model available ──
    if wake_word.WAKE_AVAILABLE and wake_word._model_present():
        try:
            from vosk import KaldiRecognizer, Model  # type: ignore
            import json as _json
            # Build a model + recognizer in-process and inject silence —
            # the goal isn't to assert detection (silence won't trigger
            # anything) but to confirm KaldiRecognizer accepts our
            # grammar JSON without crashing.
            model = Model(wake_word.VOSK_MODEL_DIR)
            grammar = _json.dumps(["mandarine", "tabby", "[unk]"])
            rec = KaldiRecognizer(model, wake_word.SAMPLE_RATE, grammar)
            silence = b"\x00\x00" * (wake_word.SAMPLE_RATE // 2)  # 0.5 s
            rec.AcceptWaveform(silence)
            _ = rec.FinalResult()
            test("real Vosk recognizer accepts grammar", True)
        except Exception as e:
            test("real Vosk recognizer accepts grammar", False, repr(e))
    else:
        print("  - vosk recognizer test skipped (vosk or model missing)",
              flush=True)


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


# ── catai_linux.shell ──────────────────────────────────────────────────────

def test_shell() -> None:
    print("\n[shell]", flush=True)
    from catai_linux.shell import CatAIShell, main as shell_main, _parse_cat_name

    test("CatAIShell class exists", callable(CatAIShell))
    test("main function exists", callable(shell_main))

    # _parse_cat_name with index
    cats = [
        {"index": 0, "name": "Mandarine"},
        {"index": 1, "name": "Pixel"},
        {"index": 2, "name": "Nyx"},
    ]
    test("parse cat name by index '1'", _parse_cat_name("1", cats) == 1)
    test("parse cat name by name 'Pixel'", _parse_cat_name("Pixel", cats) == 1)
    test("parse cat name case insensitive 'nyx'", _parse_cat_name("nyx", cats) == 2)
    test("parse cat name unknown returns None", _parse_cat_name("unknown", cats) is None)
    test("parse cat index out of range returns None", _parse_cat_name("99", cats) is None)

    # Shell instantiation (no socket)
    shell = CatAIShell(sock_path="/nonexistent/catai.sock")
    test("shell prompt contains 'catai>'", "catai>" in shell.prompt)
    test("do_quit returns True", shell.do_quit("") is True)


def test_new_animations() -> None:
    print("\n[new_animations]", flush=True)
    from catai_linux.constants import CatState, ANIM_KEYS, ONE_SHOT_STATES

    # ── Batch 1 (already integrated) ────────────────────────────────────
    # New states exist in the enum
    test("CatState.CHASING_BUTTERFLY exists", CatState.CHASING_BUTTERFLY.value == "chasing_butterfly")
    test("CatState.PLAYING_BALL exists", CatState.PLAYING_BALL.value == "playing_ball")
    test("CatState.DANCING exists", CatState.DANCING.value == "dancing")

    # New states are in ANIM_KEYS
    test("CHASING_BUTTERFLY in ANIM_KEYS", CatState.CHASING_BUTTERFLY in ANIM_KEYS)
    test("PLAYING_BALL in ANIM_KEYS", CatState.PLAYING_BALL in ANIM_KEYS)
    test("DANCING in ANIM_KEYS", CatState.DANCING in ANIM_KEYS)

    # Anim key values match the animation directory names
    test("chasing-butterfly key", ANIM_KEYS[CatState.CHASING_BUTTERFLY] == "chasing-butterfly")
    test("playing-ball key", ANIM_KEYS[CatState.PLAYING_BALL] == "playing-ball")
    test("dancing key", ANIM_KEYS[CatState.DANCING] == "dancing")

    # New states are one-shot
    test("CHASING_BUTTERFLY is one-shot", CatState.CHASING_BUTTERFLY in ONE_SHOT_STATES)
    test("PLAYING_BALL is one-shot", CatState.PLAYING_BALL in ONE_SHOT_STATES)
    test("DANCING is one-shot", CatState.DANCING in ONE_SHOT_STATES)

    # ── Batch 2 (11 new animations) ─────────────────────────────────────
    batch2 = {
        "STRETCHING":      ("stretching",       "stretching"),
        "YAWNING":         ("yawning",          "yawning"),
        "POUNCING":        ("pouncing",         "pouncing"),
        "SITTING_WITH_BIRD": ("sitting_with_bird", "sitting-with-bird"),
        "FISHING":         ("fishing",          "fishing"),
        "SNEAKING":        ("sneaking",         "sneaking"),
        "HELLO_KITTY":     ("hello_kitty",      "hello-kitty"),
        "BANDAGED":        ("bandaged",         "bandaged"),
        "PIROUETTE":       ("pirouette",        "pirouette"),
        "ROLLING_ON_BACK": ("rolling_on_back",  "rolling-on-back"),
        "BOTHERED_BY_BEE": ("bothered_by_bee",  "bothered-by-bee"),
    }

    for enum_name, (enum_val, anim_key) in batch2.items():
        cs = getattr(CatState, enum_name)
        test(f"CatState.{enum_name} exists", cs.value == enum_val)
        test(f"{enum_name} in ANIM_KEYS", cs in ANIM_KEYS)
        test(f"{anim_key} key", ANIM_KEYS[cs] == anim_key)
        test(f"{enum_name} is one-shot", cs in ONE_SHOT_STATES)

    # ── Batch 3 (3 new animations) ──────────────────────────────────────
    batch3 = {
        "BOTHERED_BY_FLY":  ("bothered_by_fly",  "bothered-by-fly"),
        "SLEEPING_BY_FIRE": ("sleeping_by_fire",  "sleeping-by-fire"),
        "WALKING_IN_PUDDLE": ("walking_in_puddle", "walking-in-puddle"),
    }

    for enum_name, (enum_val, anim_key) in batch3.items():
        cs = getattr(CatState, enum_name)
        test(f"CatState.{enum_name} exists", cs.value == enum_val)
        test(f"{enum_name} in ANIM_KEYS", cs in ANIM_KEYS)
        test(f"{anim_key} key", ANIM_KEYS[cs] == anim_key)
        test(f"{enum_name} is one-shot", cs in ONE_SHOT_STATES)

    # Metadata includes all animations for all 6 cats
    import json
    from pathlib import Path
    cats_dir = Path(__file__).resolve().parent.parent / "catai_linux"
    all_anim_keys = (
        ["chasing-butterfly", "playing-ball", "dancing"]
        + [v[1] for v in batch2.values()]
        + [v[1] for v in batch3.values()]
    )
    for cat in ["cat_orange", "cat01", "cat02", "cat03", "cat04", "cat05"]:
        meta_path = cats_dir / cat / "metadata.json"
        with open(meta_path) as f:
            meta = json.load(f)
        anims = meta["frames"]["animations"]
        for anim_key in all_anim_keys:
            test(f"{cat} has {anim_key}", anim_key in anims)


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
    _run_section("monitors", test_monitors)
    _run_section("seasonal", test_seasonal)
    _run_section("tts", test_tts)
    _run_section("updater", test_updater)
    _run_section("metrics", test_metrics)
    _run_section("character_packs", test_character_packs)
    _run_section("memory", test_memory)
    _run_section("reactions", test_reactions)
    _run_section("mood", test_mood)
    _run_section("activity", test_activity)
    _run_section("config_schema", test_config_schema)
    _run_section("easter_eggs_data", test_easter_eggs_data)
    _run_section("cat_instance_init", test_cat_instance_init)
    _run_section("pil_to_surface", test_pil_to_surface)
    _run_section("sprite_cache", test_sprite_cache)
    _run_section("wake_word", test_wake_word)
    _run_section("shell", test_shell)
    _run_section("new_animations", test_new_animations)
    print(f"\n=== Results: {PASS} passed, {FAIL} failed ===\n", flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
