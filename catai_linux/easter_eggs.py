"""Easter-egg mixin for CatAIApp.

All easter-egg constants, trigger helpers, ``eg_*`` implementations, and
their supporting draw / poll methods live here. At runtime the mixin is
mixed into ``CatAIApp`` via multiple inheritance so every ``self.xxx``
reference resolves against the app instance transparently.

Usage in app.py::

    from catai_linux.easter_eggs import EasterEggMixin, MAGIC_EGG_PHRASES, EASTER_EGGS

    class CatAIApp(EasterEggMixin, Gtk.Application):
        ...
"""
from __future__ import annotations

import logging
import math
import os
import random
import time
import typing

import cairo

from gi.repository import Gdk, GLib, Pango, PangoCairo
from PIL import Image

from catai_linux.l10n import L10n
from catai_linux import metrics as _metrics
from catai_linux.reactions import ReactionPool
from catai_linux.x11_helpers import (
    apply_above_all as _apply_above_all,
    get_active_window_fullscreen as _x11_active_fullscreen,
)

if typing.TYPE_CHECKING:
    from catai_linux.app import (  # noqa: F401
        CatAIApp, CatState, pil_to_surface, BOTTOM_MARGIN, RENDER_MS, BEHAVIOR_MS,
    )
    from catai_linux.cat import CatInstance  # noqa: F401


_app_cache: dict = {}


def _ensure_app_imports() -> None:
    """Populate module globals with lazy-imported names from catai_linux.app.

    Must be called at least once before any code in this module references
    CatState, pil_to_surface, BOTTOM_MARGIN, RENDER_MS, or BEHAVIOR_MS as
    bare names.  Module-level ``__getattr__`` only fires for *external*
    attribute access (``from easter_eggs import CatState``); it does NOT
    intercept internal global-name lookups, so functions / methods defined
    in this file would hit ``NameError`` without this helper.
    """
    if _app_cache:
        return  # already resolved
    from catai_linux.app import (
        CatState as _CatState,
        pil_to_surface as _pil_to_surface,
        BOTTOM_MARGIN as _BOTTOM_MARGIN,
        RENDER_MS as _RENDER_MS,
        BEHAVIOR_MS as _BEHAVIOR_MS,
    )
    _app_cache.update({
        "CatState": _CatState,
        "pil_to_surface": _pil_to_surface,
        "BOTTOM_MARGIN": _BOTTOM_MARGIN,
        "RENDER_MS": _RENDER_MS,
        "BEHAVIOR_MS": _BEHAVIOR_MS,
    })
    # Inject into module globals for fast subsequent access.
    globals().update(_app_cache)


def __getattr__(name: str):
    """Lazy import of names from catai_linux.app to break circular imports.

    Handles *external* attribute access (e.g. ``from easter_eggs import
    CatState``).  Internal bare-name lookups inside this module go through
    ``_ensure_app_imports()`` instead."""
    _LAZY = {"CatState", "pil_to_surface", "BOTTOM_MARGIN", "RENDER_MS", "BEHAVIOR_MS"}
    if name in _LAZY:
        _ensure_app_imports()
        return _app_cache[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

log = logging.getLogger("catai")

# ── Constants ────────────────────────────────────────────────────────────────

MAGIC_EGG_PHRASES = {
    # Nyan cat
    "nyan": "nyan",
    "nyan cat": "nyan",
    # Group hug
    "hug": "group_hug",
    "group hug": "group_hug",
    "hugs": "group_hug",
    # Rain
    "rain": "rain",
    "raining": "rain",
    "it's raining cats": "rain",
    # Apocalypse (in addition to "don't panic")
    "apocalypse": "apocalypse",
    "kaboom": "apocalypse",
    # Circle / 42
    "42": "circle",
    "circle": "circle",
    "answer": "circle",
    # Meow party
    "meow": "meow_party",
    "meow party": "meow_party",
    "party": "meow_party",
    # Stampede
    "stampede": "stampede",
    "run": "stampede",
    # Sleepy
    "sleep": "sleepy",
    "zzz": "sleepy",
    "bedtime": "sleepy",
    # Disco
    "disco": "disco",
    "dance": "disco",
    # Shake
    "shake": "shake",
    "earthquake": "shake",
    # Catnip
    "catnip": "catnip",
    "nip": "catnip",
    # Stonks
    "stonks": "stonks",
    # Slow/fast motion
    "slow": "slowmo",
    "slowmo": "slowmo",
    "slow motion": "slowmo",
    "fast": "fastfwd",
    "fastfwd": "fastfwd",
    "fast forward": "fastfwd",
    # Thanos snap
    "snap": "thanos",
    "thanos": "thanos",
    # Beam me up
    "beam": "beam",
    "beam me up": "beam",
    "teleport": "beam",
    # Hello world
    "hello": "hello_world",
    "hello world": "hello_world",
    "hi": "hello_world",
    # Sudo sandwich
    "sudo": "sudo",
    "sandwich": "sudo",
    # Hide & seek
    "hide": "hide_seek",
    "hide and seek": "hide_seek",
    "hide & seek": "hide_seek",
    # Matrix
    "matrix": "matrix",
    "neo": "matrix",
    # Boss fight
    "boss": "boss_fight",
    "boss fight": "boss_fight",
    "fight": "boss_fight",
    # Follow leader
    "follow": "follow",
    "follow me": "follow",
    "follow the leader": "follow",
    # rm -rf / (the normalizer in CatInstance.send_chat strips trailing
    # punctuation+whitespace via [\s\W]+$, so 'rm -rf /' becomes 'rm -rf',
    # 'format c:' becomes 'format c', etc.)
    "rm -rf": "rm_rf",
    "sudo rm -rf": "rm_rf",
    "delete all": "rm_rf",
    "format c": "rm_rf",
    # Caps Lock (backup magic phrase for when caps-lock detection is not
    # available, e.g. CI runners without a working X11 keyboard)
    "capslock": "capslock",
    "caps lock": "capslock",
    "shouting": "capslock",
    # Uptime party
    "uptime": "uptime",
    "how long": "uptime",
    "since when": "uptime",
    # Fullscreen applause (backup manual trigger for testing)
    "fullscreen": "fullscreen",
    "applause": "fullscreen",
    "bravo": "fullscreen",
    # Notification reaction — manual trigger for the same code path as
    # the (future) D-Bus monitor
    "notify": "notification",
    "ping": "notification",
    "notification": "notification",
    # Konami code — magic phrase fallback since we can't hook global
    # arrow-key events (canvas is click-through). Triggering through the
    # chat gets you the same 30-lives celebration.
    "konami": "konami",
    "up up down down": "konami",
    "up up down down left right left right ba": "konami",
    "cheat code": "konami",
    "god mode": "konami",
    # Coffee rush — all cats move 2× speed for 15 s
    "coffee": "coffee",
    "espresso": "coffee",
    "caffeine": "coffee",
    "latte": "coffee",
    # Zen mode — all cats freeze calmly for 10 s
    "zen": "zen",
    "meditate": "zen",
    "meditation": "zen",
    "calm": "zen",
    "breathe": "zen",
}

EASTER_EGGS = [
    ("apocalypse",  "\U0001f4a5", "Apocalypse",    "eg_apocalypse"),
    ("circle",      "\U0001f300", "42 — Circle",   "eg_circle"),
    ("meow_party",  "\U0001f389", "Meow party",    "eg_meow_party"),
    ("stampede",    "\U0001f3c3", "Stampede",      "eg_stampede"),
    ("sleepy",      "\U0001f634", "Sleepy time",   "eg_sleepy"),
    ("group_hug",   "\U0001f917", "Group hug",     "eg_group_hug"),
    ("disco",       "\U0001f57a", "Disco",         "eg_disco"),
    ("rain",        "\U0001f327", "Rain of cats",  "eg_rain"),
    ("shake",       "\U0001f4f3", "Shake",         "eg_shake"),
    ("catnip",      "\U0001f33f", "Catnip",        "eg_catnip"),
    ("stonks",      "\U0001f4c8", "Stonks",        "eg_stonks"),
    ("slowmo",      "\U0001f40c", "Slow motion",   "eg_slowmo"),
    ("fastfwd",     "\u23e9",     "Fast forward",  "eg_fastfwd"),
    ("thanos",      "\U0001f480", "Thanos snap",   "eg_thanos"),
    ("beam",        "\U0001f6f8", "Beam me up",    "eg_beam"),
    ("hello_world", "\U0001f30d", "Hello, World!", "eg_hello_world"),
    ("sudo",        "\U0001f96a", "sudo sandwich", "eg_sudo_sandwich"),
    ("hide_seek",   "\U0001f648", "Hide & seek",   "eg_hide_seek"),
    ("matrix",      "\U0001f7e2", "Matrix",        "eg_matrix"),
    ("boss_fight",  "\U0001f479", "Boss fight",    "eg_boss_fight"),
    ("follow",      "\U0001f463", "Follow leader", "eg_follow_leader"),
    ("nyan",        "\U0001f308", "Nyan!?",        "eg_nyan"),
    ("rm_rf",       "\U0001f480", "rm -rf /",      "eg_rm_rf"),
    ("capslock",    "\U0001f520", "Caps Lock",     "eg_capslock"),
    ("uptime",      "\u23f1",     "Uptime party",  "eg_uptime"),
    ("fullscreen",  "\U0001f64c", "Fullscreen",    "eg_fullscreen"),
    ("notification","\U0001f514", "Notification",  "eg_notification"),
    ("konami",      "\U0001f3ae", "Konami code",   "eg_konami"),
    ("coffee",      "\u2615",     "Coffee rush",   "eg_coffee"),
    ("zen",         "\U0001f9d8", "Zen mode",      "eg_zen"),
]

# ── Mixin ────────────────────────────────────────────────────────────────────


class EasterEggMixin:
    """Mixin providing all easter-egg logic for CatAIApp.

    At runtime ``self`` is a ``CatAIApp`` instance, so all attribute
    accesses (``self.cat_instances``, ``self.screen_w``, etc.) resolve
    against the real application object.
    """

    def show_easter_menu(self):
        n_items = len(EASTER_EGGS)
        cols = self._EASTER_MENU_COLS
        rows = (n_items + cols - 1) // cols
        self._easter_menu_w = cols * self._EASTER_MENU_CELL_W + 2 * self._EASTER_MENU_PAD
        self._easter_menu_h = (rows * self._EASTER_MENU_CELL_H
                               + self._EASTER_MENU_TITLE_H
                               + self._EASTER_MENU_FOOTER_H
                               + 2 * self._EASTER_MENU_PAD)
        self._easter_menu_visible = True
        self._easter_menu_x = (self.screen_w - self._easter_menu_w) // 2
        self._easter_menu_y = (self.screen_h - self._easter_menu_h) // 2
        if self._canvas_area:
            self._canvas_area.queue_draw()
        self._update_input_regions()

    def hide_easter_menu(self):
        self._easter_menu_visible = False
        self._easter_menu_items = []
        if self._canvas_area:
            self._canvas_area.queue_draw()
        self._update_input_regions()

    def _trigger_easter_egg(self, key):
        _ensure_app_imports()
        method_name = next((fn for k, _, _, fn in EASTER_EGGS if k == key), None)
        if method_name and hasattr(self, method_name):
            try:
                getattr(self, method_name)()
                log.info("Easter egg triggered: %s", key)
                _metrics.track("egg_triggered", key=key)
            except Exception:
                log.exception("Easter egg %s failed", key)
        return False

    def _release_encounter_lock(self):
        """Clear in_encounter on all cats and return them to IDLE."""
        _ensure_app_imports()
        for cat in self.cat_instances:
            cat.in_encounter = False
            if cat.state not in (CatState.WALKING,):
                cat.state = CatState.IDLE
                cat.frame_index = 0
                cat.idle_ticks = 0

    # ── Easter egg implementations ───────────────────────────────────────────

    def eg_apocalypse(self):
        self.start_apocalypse()

    def eg_circle(self):
        cx, cy = self.screen_w / 2, self.screen_h / 2 - 50
        radius = 220
        cats = list(self.cat_instances)
        n = len(cats)
        if n == 0:
            return
        for i, cat in enumerate(cats):
            angle = 2 * math.pi * i / n - math.pi / 2
            cat.x = int(cx + math.cos(angle) * radius - cat.display_w / 2)
            cat.y = int(cy + math.sin(angle) * radius - cat.display_h / 2)
            cat._clamp_to_screen()
            cat.state = CatState.FLAT
            cat.direction = "south"
            cat.frame_index = 0
            cat.in_encounter = True
        GLib.timeout_add(6000, lambda: (self._release_encounter_lock(), False)[1])

    def eg_meow_party(self):
        for cat in self.cat_instances:
            cat._show_random_meow()

    def eg_stampede(self):
        direction = random.choice(["east", "west"])
        start_x = -120 if direction == "east" else self.screen_w + 20
        for i, cat in enumerate(self.cat_instances):
            cat.state = CatState.DASHING
            cat.direction = direction
            cat.frame_index = 0
            cat._state_tick = 0
            cat.x = start_x + (i * 40 if direction == "east" else -i * 40)
            cat.y = random.randint(100, max(101, self.screen_h - cat.display_h - 100))

    def eg_sleepy(self):
        for cat in self.cat_instances:
            cat.state = CatState.SLEEPING_BALL
            cat.direction = "south"
            cat.frame_index = 0
            cat._sleep_tick = 0
            cat.idle_ticks = 0

    def eg_group_hug(self):
        cx, cy = self.screen_w / 2, self.screen_h / 2
        for cat in self.cat_instances:
            cat.x = int(cx + random.randint(-100, 100) - cat.display_w / 2)
            cat.y = int(cy + random.randint(-50, 50) - cat.display_h / 2)
            cat._clamp_to_screen()
            cat.state = CatState.LOVE
            cat.direction = "south"
            cat.frame_index = 0
            cat.in_encounter = True
        GLib.timeout_add(6000, lambda: (self._release_encounter_lock(), False)[1])

    def eg_disco(self):
        disco_states = [CatState.LOVE, CatState.ROLLING, CatState.GROOMING, CatState.FLAT]
        for cat in self.cat_instances:
            cat.in_encounter = True
            cat.state = random.choice(disco_states)
            cat.direction = "south"
            cat.frame_index = 0
        ticks = [20]  # 10s at 500ms per tick
        def disco_tick():
            if ticks[0] <= 0:
                self._release_encounter_lock()
                return False
            for c in self.cat_instances:
                c.state = random.choice(disco_states)
                c.frame_index = 0
            ticks[0] -= 1
            return True
        GLib.timeout_add(500, disco_tick)

    def eg_rain(self):
        for cat in self.cat_instances:
            cat.y = -cat.display_h - random.randint(0, 200)
            cat.x = random.randint(0, max(1, self.screen_w - cat.display_w))
            cat.state = CatState.FALLING
            cat.direction = "south"
            cat.frame_index = 0
            cat._rain_falling = True
            cat._rain_velocity = random.uniform(3, 6)
            cat.in_encounter = True  # freeze behavior during fall
        def rain_tick():
            still_falling = False
            for cat in self.cat_instances:
                if not getattr(cat, '_rain_falling', False):
                    continue
                cat.y += cat._rain_velocity
                cat._rain_velocity += 0.35  # gravity
                max_y = cat.screen_h - cat.display_h - cat._app._canvas_y_offset - BOTTOM_MARGIN if cat._app else cat.screen_h - cat.display_h - 30
                if cat.y >= max_y:
                    cat.y = max_y
                    cat._rain_falling = False
                    cat.in_encounter = False
                    cat.state = CatState.LANDING
                    cat.frame_index = 0
                else:
                    still_falling = True
            return still_falling
        GLib.timeout_add(50, rain_tick)

    def eg_shake(self):
        self._shake_amount = 20.0
        def decay():
            self._shake_amount *= 0.82
            if self._shake_amount < 0.5:
                self._shake_amount = 0
                return False
            return True
        GLib.timeout_add(30, decay)

    def eg_catnip(self):
        for cat in self.cat_instances:
            cat.state = CatState.ROLLING
            cat.direction = "south"
            cat.frame_index = 0
            cat.in_encounter = True
        GLib.timeout_add(12000, lambda: (self._release_encounter_lock(), False)[1])

    def eg_stonks(self):
        ticks = [100]
        def climb():
            if ticks[0] <= 0:
                return False
            for c in self.cat_instances:
                c.y = max(0, c.y - 2)
            ticks[0] -= 1
            return True
        GLib.timeout_add(100, climb)

    def eg_slowmo(self):
        if getattr(self, "_slowmo_active", False):
            return
        self._slowmo_active = True
        for tid in self._timers:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._timers = [
            GLib.timeout_add(RENDER_MS * 3, self._render_tick),
            GLib.timeout_add(BEHAVIOR_MS * 3, self._behavior_tick),
            GLib.timeout_add(10000, _apply_above_all),
            GLib.timeout_add(30000, self._gc_collect),
        ]
        def restore():
            for tid in self._timers:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
            self._timers = [
                GLib.timeout_add(RENDER_MS, self._render_tick),
                GLib.timeout_add(BEHAVIOR_MS, self._behavior_tick),
                GLib.timeout_add(10000, _apply_above_all),
                GLib.timeout_add(30000, self._gc_collect),
            ]
            self._slowmo_active = False
            return False
        GLib.timeout_add(10000, restore)

    def eg_fastfwd(self):
        if getattr(self, "_fastfwd_active", False):
            return
        self._fastfwd_active = True
        for tid in self._timers:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._timers = [
            GLib.timeout_add(max(1, RENDER_MS // 2), self._render_tick),
            GLib.timeout_add(max(1, BEHAVIOR_MS // 2), self._behavior_tick),
            GLib.timeout_add(10000, _apply_above_all),
            GLib.timeout_add(30000, self._gc_collect),
        ]
        def restore():
            for tid in self._timers:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
            self._timers = [
                GLib.timeout_add(RENDER_MS, self._render_tick),
                GLib.timeout_add(BEHAVIOR_MS, self._behavior_tick),
                GLib.timeout_add(10000, _apply_above_all),
                GLib.timeout_add(30000, self._gc_collect),
            ]
            self._fastfwd_active = False
            return False
        GLib.timeout_add(10000, restore)

    def eg_thanos(self):
        half = len(self.cat_instances) // 2
        if half == 0:
            return
        doomed = random.sample(self.cat_instances, half)
        for cat in doomed:
            cat._thanos_fading = True
            cat._birth_progress = 1.0  # start full, fade to 0
        def fade_step():
            still_fading = False
            to_remove = []
            for cat in list(self.cat_instances):
                if getattr(cat, "_thanos_fading", False):
                    if cat._birth_progress is None:
                        cat._birth_progress = 1.0
                    cat._birth_progress = max(0.0, cat._birth_progress - 0.05)
                    if cat._birth_progress <= 0.01:
                        to_remove.append(cat)
                    else:
                        still_fading = True
            for cat in to_remove:
                try:
                    cat.cleanup()
                    if cat in self.cat_instances:
                        self.cat_instances.remove(cat)
                except Exception:
                    pass
            return still_fading
        GLib.timeout_add(100, fade_step)

    def eg_beam(self):
        if not self.cat_instances:
            return
        cat = random.choice(self.cat_instances)
        cat._beam_ticks = 30
        def beam_tick():
            cat._beam_ticks -= 1
            cat.y = max(-cat.display_h, cat.y - 10)
            if cat._beam_ticks <= 0:
                cat.x = random.randint(0, max(1, self.screen_w - cat.display_w))
                cat.y = random.randint(100, max(101, self.screen_h - cat.display_h - 100))
                cat._beam_ticks = 0
                return False
            return True
        GLib.timeout_add(33, beam_tick)

    def eg_hello_world(self):
        if not self.cat_instances:
            return
        cat = random.choice(self.cat_instances)
        cat.chat_response = "Hello, World! \U0001f30d"
        cat.chat_visible = True
        def hide():
            cat.chat_visible = False
            cat.chat_response = ""
            return False
        GLib.timeout_add(5000, hide)

    def eg_sudo_sandwich(self):
        if not self.cat_instances:
            return
        cat = random.choice(self.cat_instances)
        cat.chat_response = "okay \U0001f96a"
        cat.chat_visible = True
        def do_sleep():
            cat.chat_visible = False
            cat.chat_response = ""
            cat.state = CatState.SLEEPING_BALL
            cat.direction = "south"
            cat.frame_index = 0
            cat._sleep_tick = 0
            cat.idle_ticks = 0
            return False
        GLib.timeout_add(2500, do_sleep)

    def eg_hide_seek(self):
        if len(self.cat_instances) < 2:
            return
        seeker = random.choice(self.cat_instances)
        for cat in self.cat_instances:
            if cat is not seeker:
                cat._hidden = True
                # Scatter hidden cats to random positions
                cat.x = random.randint(0, max(1, self.screen_w - cat.display_w))
                cat.y = random.randint(100, max(101, self.screen_h - cat.display_h - 100))
                cat.in_encounter = True
        def reveal():
            for cat in self.cat_instances:
                cat._hidden = False
                cat.in_encounter = False
            return False
        GLib.timeout_add(6000, reveal)

    def eg_matrix(self):
        self._matrix_ticks = 150  # ~10s at 65ms
        col_width = 22
        n_cols = self.screen_w // col_width
        chars = "01\u30a2\u30a4\u30a6\u30a8\u30aa\u30ab\u30ad\u30af\u30b1\u30b3\u30b5\u30b7\u30b9\u30bb\u30bd\u30bf\u30c1\u30c4\u30c6\u30c8"
        self._matrix_columns = []
        for i in range(n_cols):
            self._matrix_columns.append({
                'x': i * col_width,
                'y': random.randint(-600, 0),
                'speed': random.uniform(8, 18),
                'trail': [random.choice(chars) for _ in range(14)],
                'chars_pool': chars,
            })
        def tick():
            self._matrix_ticks -= 1
            if self._matrix_ticks <= 0:
                self._matrix_columns = []
                if self._canvas_area:
                    self._canvas_area.queue_draw()
                return False
            for col in self._matrix_columns:
                col['y'] += col['speed']
                if col['y'] > self.screen_h + 400:
                    col['y'] = -400
                    col['trail'] = [random.choice(col['chars_pool']) for _ in range(14)]
                # Occasionally swap a char for twinkle
                if random.random() < 0.08:
                    col['trail'][random.randint(0, 13)] = random.choice(col['chars_pool'])
            if self._canvas_area:
                self._canvas_area.queue_draw()
            return True
        GLib.timeout_add(65, tick)

    def eg_boss_fight(self):
        if not self.cat_instances:
            return
        boss = random.choice(self.cat_instances)
        boss._boss_scale = 2.2  # visual scale, honoured by the draw loop
        # Position boss at center (account for visual scale)
        eff_w = int(boss.display_w * boss._boss_scale)
        eff_h = int(boss.display_h * boss._boss_scale)
        boss.x = (self.screen_w - eff_w) // 2 + (eff_w - boss.display_w) // 2
        boss.y = (self.screen_h - eff_h) // 2 + (eff_h - boss.display_h) // 2
        boss.state = CatState.ANGRY
        boss.direction = "south"
        boss.frame_index = 0
        boss.in_encounter = True

        # Other cats around the boss in a circle, all facing the boss in ANGRY
        others = [c for c in self.cat_instances if c is not boss]
        if others:
            for i, cat in enumerate(others):
                angle = 2 * math.pi * i / len(others)
                cx = boss.x + boss.display_w / 2
                cy = boss.y + boss.display_h / 2
                cat.x = int(cx + math.cos(angle) * 260 - cat.display_w / 2)
                cat.y = int(cy + math.sin(angle) * 180 - cat.display_h / 2)
                cat._clamp_to_screen()
                cat.state = CatState.ANGRY
                cat._face_toward(boss, CatState.ANGRY)  # face the boss
                cat.frame_index = 0
                cat.in_encounter = True

        # Phase 2 (after 5s): boss dies, small cats look surprised
        def phase2():
            for cat in others:
                if cat not in self.cat_instances:
                    continue
                cat.state = CatState.SURPRISED
                cat._face_toward(boss, CatState.SURPRISED)
                cat.frame_index = 0
                # Keep in_encounter so SURPRISED loops (no one-shot end)
            # Boss enters drama_queen sequence — must NOT be in_encounter
            boss.in_encounter = False
            boss._start_sequence("drama_queen")
            # Shrink the boss from 2.2 → 1.0 over ~2s
            shrink_state = {'t': 0, 'total': 25}
            def shrink():
                shrink_state['t'] += 1
                if shrink_state['t'] >= shrink_state['total']:
                    boss._boss_scale = 1.0
                    return False
                p = shrink_state['t'] / shrink_state['total']
                boss._boss_scale = 2.2 + (1.0 - 2.2) * p
                return True
            GLib.timeout_add(80, shrink)
            return False
        GLib.timeout_add(5000, phase2)

        # Final restore (~22s: 5s angry + up to ~17s drama_queen)
        def restore():
            if hasattr(boss, '_boss_scale'):
                del boss._boss_scale
            self._release_encounter_lock()
            return False
        GLib.timeout_add(22000, restore)

    def eg_rm_rf(self):
        """rm -rf / — a cat grows to 3x, 'wipes' across the screen, then laughs
        it off and shrinks back. The effect is 100% harmless (nothing actually
        gets deleted on disk or from the app state) — it's a playful reassurance
        for anyone who typed the infamous command half-jokingly.

        Phases:
          1. Pick a cat (last active chat cat, else random) and freeze it.
          2. Grow it to 3x over ~400 ms via _boss_scale tween.
          3. Walk it across the screen (east->west or west->east depending on
             which side it's closest to) at high speed, with WALKING anim.
          4. While walking, the canvas draws a 'wipe' trail — a translucent
             cream band growing behind the cat. This is the 'deletion'.
          5. On the far side, the cat stops, turns SURPRISED, shows a
             localized 'just kidding!' meow bubble.
          6. After 1.5 s, enter LOVE for a laugh, shrink back to 1x over
             ~600 ms, clear the wipe trail, release encounter lock.

        State stored on the cat:
          - _boss_scale (reused from boss_fight, already honoured by the draw loop)
          - _rm_rf_active (True while animating so the wipe trail draws)
          - _rm_rf_wipe_x (the x coord of the trailing edge of the wipe band)
          - _rm_rf_wipe_direction ("east" or "west")
        """
        if not self.cat_instances:
            return
        if getattr(self, "_rm_rf_active_app", False):
            return  # already running, ignore re-triggers
        self._rm_rf_active_app = True

        # Pick the cat: last active chat cat takes priority
        victim = (self._active_chat_cat
                  if self._active_chat_cat in self.cat_instances
                  else random.choice(self.cat_instances))
        victim.in_encounter = True
        victim._flip_h = False
        victim.meow_visible = False
        victim.chat_visible = False
        victim._rm_rf_active = True
        # Start position: full-height center-ish, place at the side opposite
        # to where it currently is so the wipe travels across the widest area
        start_from_left = victim.x + victim.display_w / 2 < self.screen_w / 2
        victim._rm_rf_wipe_direction = "east" if start_from_left else "west"

        # Anchor position (keep vertically where it was, clamp to leave room
        # for the 3x visual growth without clipping the top/bottom of screen)
        eff_scale_final = 3.0
        eff_h = int(victim.display_h * eff_scale_final)
        anchor_y = max(0, min(victim.y, self.screen_h - eff_h))
        if start_from_left:
            anchor_x = 0
            wipe_end_x = self.screen_w
        else:
            anchor_x = self.screen_w - victim.display_w
            wipe_end_x = 0
        victim.x = anchor_x
        victim.y = anchor_y
        victim._rm_rf_wipe_x = anchor_x + victim.display_w / 2
        victim.direction = victim._rm_rf_wipe_direction
        victim.state = CatState.WALKING
        victim.frame_index = 0
        victim._boss_scale = 1.0  # will tween up

        log.info("rm -rf / easter egg: victim=%s start_from_left=%s",
                 victim.config.get("name"), start_from_left)

        # Phase 1: grow 1.0 -> 3.0 over 400 ms (8 ticks x 50 ms)
        grow_state = {"t": 0, "total": 8}
        def grow():
            grow_state["t"] += 1
            if grow_state["t"] >= grow_state["total"]:
                victim._boss_scale = eff_scale_final
                GLib.timeout_add(30, wipe)
                return False
            p = grow_state["t"] / grow_state["total"]
            victim._boss_scale = 1.0 + (eff_scale_final - 1.0) * p
            return True
        GLib.timeout_add(50, grow)

        # Phase 2: walk across the screen at high speed, trailing the wipe
        # band. Runs at 30 ms for smoothness (~33 fps).
        wipe_speed_px = 32  # pixels per tick — fast enough to cross in ~1.5 s
        def wipe():
            if not getattr(victim, "_rm_rf_active", False):
                return False  # aborted
            if start_from_left:
                victim.x += wipe_speed_px
                victim._rm_rf_wipe_x = victim.x + victim.display_w / 2
                done = victim.x + victim.display_w * eff_scale_final >= wipe_end_x
            else:
                victim.x -= wipe_speed_px
                victim._rm_rf_wipe_x = victim.x + victim.display_w / 2
                done = victim.x <= wipe_end_x
            if done:
                # Snap to final position and switch to the reveal phase
                victim.state = CatState.SURPRISED
                victim.frame_index = 0
                victim.meow_text = L10n.s("rm_rf_jk")
                victim.meow_visible = True
                GLib.timeout_add(1500, laugh)
                return False
            return True

        # Phase 3: laugh (LOVE state) for a beat, then shrink back
        def laugh():
            victim.state = CatState.LOVE
            victim.frame_index = 0
            victim.meow_visible = False
            GLib.timeout_add(1200, shrink_back)
            return False

        # Phase 4: shrink back from 3.0 -> 1.0 over 600 ms, then restore
        def shrink_back():
            shrink_state = {"t": 0, "total": 12}
            def shrink():
                shrink_state["t"] += 1
                if shrink_state["t"] >= shrink_state["total"]:
                    victim._boss_scale = 1.0
                    if hasattr(victim, "_boss_scale"):
                        del victim._boss_scale
                    victim._rm_rf_active = False
                    victim.state = CatState.IDLE
                    victim.frame_index = 0
                    victim.in_encounter = False
                    self._rm_rf_active_app = False
                    return False
                p = shrink_state["t"] / shrink_state["total"]
                victim._boss_scale = eff_scale_final + (1.0 - eff_scale_final) * p
                return True
            GLib.timeout_add(50, shrink)
            return False

    # ── Caps Lock detection + reaction ───────────────────────────────────────

    def _get_caps_lock_state(self) -> bool:
        """Query the Caps Lock modifier via GDK (no subprocess, no ctypes).
        Returns False if the keyboard device isn't available (headless CI)."""
        try:
            display = Gdk.Display.get_default()
            if not display:
                return False
            seat = display.get_default_seat()
            if not seat:
                return False
            kbd = seat.get_keyboard()
            if not kbd:
                return False
            return bool(kbd.get_caps_lock_state())
        except Exception:
            return False

    def _check_caps_lock(self) -> bool:
        """Poll Caps Lock; on False -> True rising edge (with 8 s cooldown),
        trigger the capslock easter egg. Returns True to keep the timer."""
        try:
            now_on = self._get_caps_lock_state()
            prev = self._caps_lock_prev
            self._caps_lock_prev = now_on
            if now_on and not prev:
                now_ts = time.monotonic()
                if now_ts - self._caps_lock_last_trigger >= 8.0:
                    self._caps_lock_last_trigger = now_ts
                    self.eg_capslock()
        except Exception:
            log.exception("caps lock poll crashed")
        return True  # keep the timer running

    def eg_capslock(self):
        """Caps Lock toggled ON — a cat notices and asks why you're shouting.
        The reply is drawn from an AI-generated pool (varied across triggers)
        with a deterministic L10n fallback on the first hit or when no AI
        backend is available (offline / CI mock mode)."""
        if not self.cat_instances:
            return
        # Pick the last-active chat cat if any, else a random cat
        victim = (self._active_chat_cat
                  if self._active_chat_cat in self.cat_instances
                  else random.choice(self.cat_instances))
        # Don't interrupt an ongoing encounter or the rm_rf animation
        if victim.in_encounter or getattr(victim, "_rm_rf_active", False):
            return

        # Fetch a reaction from the pool (instant; kicks off background
        # generation if the pool is empty)
        text = self._reaction_pool.get(victim, ReactionPool.EVT_CAPSLOCK)
        log.info("Caps Lock easter egg: %s says %r", victim.config.get("name"), text)

        victim.state = CatState.SURPRISED
        victim.frame_index = 0
        victim.meow_text = text
        victim.meow_visible = True
        # Auto-hide after 3 s — reuse the existing meow timer slot on the cat
        if victim._meow_timer_id:
            try:
                GLib.source_remove(victim._meow_timer_id)
            except Exception:
                pass
        victim._meow_timer_id = GLib.timeout_add(3000, victim._hide_meow)

    # ── Notification reaction ────────────────────────────────────────────────

    def eg_notification(self, app_name: str = "", summary: str = ""):
        """A desktop notification arrived — the nearest cat reacts. Uses
        the AI-backed ReactionPool (EVT_NOTIFICATION) for a varied quip.

        Currently triggered manually (via the 'notify' socket command or
        a magic phrase in chat) because full D-Bus monitor eavesdropping
        requires additional permissions on modern GNOME. The automatic
        trigger will be a drop-in replacement in a follow-up PR that adds
        a pydbus subscription to org.freedesktop.Notifications.

        Args:
            app_name: optional originating application (Slack, Thunderbird...)
            summary: optional notification summary text
        """
        _ensure_app_imports()
        if not self.cat_instances:
            return
        # Pick the last-active chat cat if any, else a random cat, else the
        # first one. Avoid cats that are busy with another animation.
        candidates = [
            c for c in self.cat_instances
            if not c.in_encounter
            and not c.dragging
            and not getattr(c, "_petting_active", False)
            and not getattr(c, "_rm_rf_active", False)
        ]
        if not candidates:
            return
        victim = (self._active_chat_cat
                  if self._active_chat_cat in candidates
                  else random.choice(candidates))

        text = self._reaction_pool.get(victim, ReactionPool.EVT_NOTIFICATION)
        log.info("notification egg: %s (%s) -> %s says %r",
                 app_name or "?", summary[:30] or "?",
                 victim.config.get("name"), text)

        victim.state = CatState.SURPRISED
        victim.frame_index = 0
        victim.meow_text = text
        victim.meow_visible = True
        if victim._meow_timer_id:
            try:
                GLib.source_remove(victim._meow_timer_id)
            except Exception:
                pass
        victim._meow_timer_id = GLib.timeout_add(2500, victim._hide_meow)

    # ── Uptime party ─────────────────────────────────────────────────────────

    def _read_system_uptime(self) -> tuple[int, int, int] | None:
        """Return (days, hours, minutes) of system uptime, or None on failure.
        Reads /proc/uptime which is the fastest + most portable way on Linux."""
        try:
            with open("/proc/uptime") as f:
                seconds = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            return None
        total_minutes = int(seconds // 60)
        days = total_minutes // (24 * 60)
        hours = (total_minutes % (24 * 60)) // 60
        minutes = total_minutes % 60
        return days, hours, minutes

    def _format_uptime(self, days: int, hours: int, minutes: int) -> str:
        """Format uptime in the current language, long-form and warm."""
        lang = L10n.lang
        if days > 0:
            if lang == "en":
                return f"\u23f1 Up {days}d {hours}h {minutes}m \U0001f4aa"
            if lang == "es":
                return f"\u23f1 Arriba {days}d {hours}h {minutes}m \U0001f4aa"
            return f"\u23f1 Allum\u00e9 depuis {days}j {hours}h {minutes}m \U0001f4aa"
        if hours > 0:
            if lang == "en":
                return f"\u23f1 Up {hours}h {minutes}m \U0001f389"
            if lang == "es":
                return f"\u23f1 Arriba {hours}h {minutes}m \U0001f389"
            return f"\u23f1 Allum\u00e9 depuis {hours}h {minutes}m \U0001f389"
        if lang == "en":
            return f"\u23f1 Up {minutes}m \u2014 just woke up \U0001f638"
        if lang == "es":
            return f"\u23f1 Arriba {minutes}m \u2014 acabando de despertar \U0001f638"
        return f"\u23f1 Allum\u00e9 depuis {minutes}m \u2014 \u00e0 peine r\u00e9veill\u00e9 \U0001f638"

    def eg_uptime(self):
        """Uptime party — the active cat shows the system uptime in a chat
        bubble, other cats gather in a circle around it in LOVE state, and
        the whole thing releases after ~6 seconds. The 'number formation'
        from the original idea is replaced by a contextual bubble because
        with 4-6 cats, a literal pixel-art digit wouldn't be readable."""
        if not self.cat_instances:
            return
        ut = self._read_system_uptime()
        if ut is None:
            log.warning("uptime egg: cannot read /proc/uptime")
            return
        days, hours, minutes = ut
        message = self._format_uptime(days, hours, minutes)
        log.info("uptime egg: %s", message)

        # Pick the focus cat: the active chat cat, else the first one
        focus = (self._active_chat_cat
                 if self._active_chat_cat in self.cat_instances
                 else self.cat_instances[0])
        # Don't interrupt an ongoing encounter
        if focus.in_encounter:
            return

        # Focus cat: show the uptime in its chat bubble, stay IDLE so it
        # doesn't wander off during the display
        focus.chat_response = message
        focus.chat_visible = True
        self._active_chat_cat = focus
        focus.state = CatState.IDLE
        focus.frame_index = 0
        focus.in_encounter = True  # freeze it for the duration

        # Other cats: gather in a half-circle BELOW the focus cat so the
        # chat bubble above stays unobstructed, all in LOVE state
        others = [c for c in self.cat_instances if c is not focus]
        cx = focus.x + focus.display_w / 2
        cy = focus.y + focus.display_h + 40
        radius_x = 220
        radius_y = 40
        for i, cat in enumerate(others):
            if len(others) > 1:
                # Spread from pi (left) to 2pi (right), avoiding the top
                angle = math.pi + (math.pi * i / max(1, len(others) - 1))
            else:
                angle = math.pi + math.pi / 2  # single cat: straight below
            cat.x = int(cx + math.cos(angle) * radius_x - cat.display_w / 2)
            cat.y = int(cy + math.sin(angle) * radius_y - cat.display_h / 2)
            cat._clamp_to_screen()
            cat.state = CatState.LOVE
            cat._face_toward(focus, CatState.LOVE)
            cat.frame_index = 0
            cat.in_encounter = True

        # Release after 6 seconds — long enough to read the message, short
        # enough to feel responsive
        def restore():
            focus.chat_visible = False
            focus.chat_response = ""
            if self._active_chat_cat is focus:
                self._active_chat_cat = None
            self._release_encounter_lock()
            return False
        GLib.timeout_add(6000, restore)

    # ── Fullscreen applause ──────────────────────────────────────────────────

    def _is_any_fullscreen(self) -> bool:
        """Check if the currently active X window has _NET_WM_STATE_FULLSCREEN.
        Direct Xlib query via ctypes — ~50 us, no subprocess fork. Returns
        False on any failure (libX11 missing, no display, parse error)."""
        try:
            return _x11_active_fullscreen()
        except Exception:
            log.debug("fullscreen query failed", exc_info=True)
            return False

    def _check_fullscreen(self) -> bool:
        """Poll fullscreen state; on False -> True rising edge (with 15 s
        cooldown), trigger eg_fullscreen. Returns True to keep the timer."""
        try:
            now_fs = self._is_any_fullscreen()
            # Ignore transitions where our own canvas window is active —
            # we're not the ones going fullscreen, and our canvas isn't
            # tagged fullscreen anyway.
            prev = self._fullscreen_prev
            self._fullscreen_prev = now_fs
            if now_fs and not prev:
                now_ts = time.monotonic()
                if now_ts - self._fullscreen_last_trigger >= 15.0:
                    self._fullscreen_last_trigger = now_ts
                    self.eg_fullscreen()
        except Exception:
            log.exception("fullscreen poll crashed")
        return True

    def eg_fullscreen(self):
        """Fullscreen applause — all cats enter SURPRISED briefly, then LOVE
        with sparkle overlays. Feels like an ovation when the user goes
        full-screen in Firefox / YouTube / presentations."""
        if not self.cat_instances:
            return
        if getattr(self, "_fullscreen_applause_active", False):
            return
        self._fullscreen_applause_active = True

        # Phase 1: all cats SURPRISED for 800 ms
        for cat in self.cat_instances:
            if cat.in_encounter:
                continue
            cat.state = CatState.SURPRISED
            cat.frame_index = 0
            cat.in_encounter = True

        def applause():
            # Phase 2: all cats LOVE for 2.5 s
            for cat in self.cat_instances:
                if not cat.in_encounter:
                    continue
                cat.state = CatState.LOVE
                cat.frame_index = 0
            GLib.timeout_add(2500, end_applause)
            return False

        def end_applause():
            self._fullscreen_applause_active = False
            self._release_encounter_lock()
            return False

        GLib.timeout_add(800, applause)

    # ── Lorem ipsum reading ──────────────────────────────────────────────────

    def eg_lorem(self, cat, full_text: str):
        """Lorem ipsum easter egg — the cat 'reads' a very long pasted text
        by slowly scrolling a window of it across its chat bubble. After
        ~10 s it falls asleep mid-reading (SLEEPING_BALL + '...zzz' bubble)
        and the bubble clears after another few seconds.

        Unlike the other easter eggs, this one takes a pre-selected cat
        as argument (the active chat cat, passed from
        _on_chat_entry_activate), not a random one — the user clicked on
        THIS specific cat and tried to talk to it, so that's who reads.
        """
        _ensure_app_imports()
        # Normalize whitespace so the scrolling window reads cleanly
        text = " ".join(full_text.split())
        WINDOW_CHARS = 40
        STEP_MS = 100
        READ_DURATION_MS = 10000
        SLEEP_BUBBLE_MS = 4000

        # Initial state: show the first window
        cat.state = CatState.EATING  # "processing" animation
        cat.frame_index = 0
        cat.chat_response = text[:WINDOW_CHARS]
        cat.chat_visible = True
        cat.in_encounter = True
        # Register the cat as the active chat target so _position_chat_entry
        # follows it (even though we don't want an input box to show — we
        # hide _chat_box to signal it's read-only).
        if self._chat_box:
            self._chat_box.set_visible(False)
        self._active_chat_cat = cat

        scroll_state = {"offset": 0}
        total_steps = READ_DURATION_MS // STEP_MS

        def scroll():
            scroll_state["offset"] += 1
            if scroll_state["offset"] >= total_steps:
                # Cat falls asleep mid-reading — show what's currently
                # visible + a zzz at the end to signal the transition
                last_window = cat.chat_response
                cat.state = CatState.SLEEPING_BALL
                cat.frame_index = 0
                cat.chat_response = last_window + "  \u2026\U0001f4a4"
                GLib.timeout_add(SLEEP_BUBBLE_MS, finish)
                return False
            start = scroll_state["offset"]
            # Scroll forward — wrap around if we run out of text
            if start + WINDOW_CHARS >= len(text):
                # Pad with spaces so the last frame doesn't abruptly jump
                tail = text[start:] + " " * WINDOW_CHARS
                cat.chat_response = tail[:WINDOW_CHARS]
            else:
                cat.chat_response = text[start : start + WINDOW_CHARS]
            return True

        def finish():
            cat.chat_visible = False
            cat.chat_response = ""
            cat.in_encounter = False
            if self._active_chat_cat is cat:
                self._active_chat_cat = None
            return False

        GLib.timeout_add(STEP_MS, scroll)
        log.info("lorem ipsum egg: cat=%s text_len=%d steps=%d",
                 cat.config.get("name"), len(text), total_steps)

    def eg_follow_leader(self):
        if len(self.cat_instances) < 2:
            return
        # Pick leader: last active chat cat, or a random one
        leader = self._active_chat_cat if self._active_chat_cat in self.cat_instances else None
        if leader is None:
            leader = random.choice(self.cat_instances)
        # Make sure the leader is NOT frozen in encounter or odd state
        leader.state = CatState.WALKING
        leader.in_encounter = False
        leader.frame_index = 0
        # Give the leader a random destination to walk toward
        leader.dest_x = random.uniform(leader.display_w, max(leader.display_w + 1, self.screen_w - leader.display_w))
        leader.dest_y = leader.y
        # All others walk toward the leader's current position
        for i, cat in enumerate(self.cat_instances):
            if cat is leader:
                continue
            cat.state = CatState.WALKING
            cat.in_encounter = False
            cat.frame_index = 0
            # Stagger offsets so they don't overlap
            cat.dest_x = leader.x + (i % 5) * 30 - 60
            cat.dest_y = leader.y

    NYAN_FRAME_COUNT = 6  # frames in nyan_cat.png sprite sheet

    def _load_nyan_assets(self):
        """Lazy-load the nyan cat sprite sheet + rainbow tile into cairo surfaces."""
        if hasattr(self, '_nyan_frames') and self._nyan_frames:
            return
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        cat_path = os.path.join(pkg_dir, "nyan_cat.png")
        rain_path = os.path.join(pkg_dir, "nyan_rainbow.png")
        self._nyan_frames = []
        self._nyan_frame_data = []
        try:
            sheet = Image.open(cat_path).convert("RGBA")
            total_w, frame_h = sheet.size
            frame_w = total_w // self.NYAN_FRAME_COUNT
            self._nyan_frame_w = frame_w
            self._nyan_frame_h = frame_h
            for i in range(self.NYAN_FRAME_COUNT):
                f = sheet.crop((i * frame_w, 0, (i + 1) * frame_w, frame_h))
                surf, data = pil_to_surface(f, frame_w, frame_h)
                self._nyan_frames.append(surf)
                self._nyan_frame_data.append(data)
            rain_pil = Image.open(rain_path).convert("RGBA")
            self._nyan_rain_surface, self._nyan_rain_data = pil_to_surface(rain_pil, rain_pil.width, rain_pil.height)
            self._nyan_rain_w, self._nyan_rain_h = rain_pil.size
        except Exception:
            log.exception("Failed to load nyan cat assets")
            self._nyan_frames = []

    def eg_konami(self):
        """Konami code unlocked — all cats briefly flash through SURPRISED ->
        LOVE -> ROLLING, like a "GOD MODE" celebration. Also bumps every
        cat's mood toward content without pinning it to the extreme values
        that would lock ``_roll_mood_adjusted`` into a single narrow IDLE
        branch for the rest of the session.

        Magic phrases: 'konami', 'up up down down', 'cheat code'."""
        if not self.cat_instances:
            return
        if getattr(self, "_konami_active", False):
            return
        self._konami_active = True

        # Phase 1: SURPRISED (all cats, 500 ms)
        for cat in self.cat_instances:
            if cat.in_encounter:
                continue
            cat.state = CatState.SURPRISED
            cat.direction = "south"
            cat.frame_index = 0
            cat.in_encounter = True
            # Mood bump toward happy/rested WITHOUT pinning to extremes.
            # is_affectionate triggers at happiness > 75 and is_bored at
            # bored > 70 — we stay safely below both so the behavior
            # tick's IDLE branch doesn't get locked into a single band.
            if hasattr(cat, "mood") and cat.mood is not None:
                cat.mood.happiness = min(75.0, cat.mood.happiness + 30)
                cat.mood.energy = min(99.0, cat.mood.energy + 30)
                cat.mood.bored = max(0.0, cat.mood.bored - 40)
                cat.mood.hunger = max(0.0, cat.mood.hunger - 20)

        def phase_love():
            for cat in self.cat_instances:
                cat.state = CatState.LOVE
                cat.frame_index = 0
            GLib.timeout_add(1500, phase_rolling)
            return False

        def phase_rolling():
            for cat in self.cat_instances:
                cat.state = CatState.ROLLING
                cat.frame_index = 0
            GLib.timeout_add(1200, phase_done)
            return False

        def phase_done():
            self._release_encounter_lock()
            self._konami_active = False
            return False

        GLib.timeout_add(500, phase_love)

    def eg_coffee(self):
        """Caffeine rush — all cats move at 2x behavior tick speed for 15 s,
        then settle back. Combined with a one-shot happiness/energy bump.
        Magic phrases: 'coffee', 'espresso', 'caffeine'."""
        if getattr(self, "_coffee_active", False):
            return
        self._coffee_active = True

        # Burst of energy on the mood stats. Cap happiness at 75 so we
        # don't trigger is_affectionate() which would lock the IDLE
        # branch into a narrow LOVE/sparkle band for the rest of the
        # session — same pitfall as eg_konami without the cap.
        for cat in self.cat_instances:
            if hasattr(cat, "mood") and cat.mood is not None:
                cat.mood.energy = min(99.0, cat.mood.energy + 25)
                cat.mood.happiness = min(75.0, cat.mood.happiness + 10)

        # Swap the behavior tick for a 2x faster variant for 15 s. Keep
        # render tick untouched so the cats look hyperactive rather than
        # time-sped-up.
        try:
            for tid in self._timers:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
            self._timers = [
                GLib.timeout_add(RENDER_MS, self._render_tick),
                GLib.timeout_add(max(1, BEHAVIOR_MS // 2), self._behavior_tick),
                GLib.timeout_add(10000, _apply_above_all),
                GLib.timeout_add(30000, self._gc_collect),
            ]
        except Exception:
            log.exception("eg_coffee timer swap failed")

        def restore():
            for tid in self._timers:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
            self._timers = [
                GLib.timeout_add(RENDER_MS, self._render_tick),
                GLib.timeout_add(BEHAVIOR_MS, self._behavior_tick),
                GLib.timeout_add(10000, _apply_above_all),
                GLib.timeout_add(30000, self._gc_collect),
            ]
            self._coffee_active = False
            return False

        GLib.timeout_add(15000, restore)

    def eg_zen(self):
        """Meditation mode — all cats freeze in IDLE state, perfectly still,
        for 10 seconds. Drops their bored stat briefly.
        Magic phrases: 'zen', 'meditate', 'calm'."""
        if not self.cat_instances:
            return
        if getattr(self, "_zen_active", False):
            return
        self._zen_active = True

        for cat in self.cat_instances:
            if cat.in_encounter:
                continue
            cat.state = CatState.IDLE
            cat.direction = "south"
            cat.frame_index = 0
            cat.in_encounter = True
            if hasattr(cat, "mood") and cat.mood is not None:
                cat.mood.bored = max(0.0, cat.mood.bored - 30)
                # Cap below is_affectionate threshold (>75) for the same
                # reason as eg_konami / eg_coffee — avoid locking the
                # IDLE roll into a single narrow band.
                cat.mood.happiness = min(75.0, cat.mood.happiness + 10)

        def release():
            for cat in self.cat_instances:
                cat.in_encounter = False
            self._zen_active = False
            return False

        GLib.timeout_add(10000, release)

    def eg_nyan(self):
        """Classic Nyan Cat: flies across the screen with a tiled animated rainbow trail."""
        self._load_nyan_assets()
        if not self._nyan_frames:
            return
        # Target size: same height as regular cats (display_h at current scale)
        if self.cat_instances:
            target_h = self.cat_instances[0].display_h
        else:
            target_h = int(round(80 * self.cat_scale))  # catset sprites are 80x80
        self._nyan_scale = target_h / self._nyan_frame_h
        self._nyan_target_h = target_h
        self._nyan_target_w = int(self._nyan_frame_w * self._nyan_scale)
        self._nyan_x = float(-self._nyan_target_w)
        self._nyan_y = self.screen_h // 2 - self._nyan_target_h // 2
        self._nyan_active = True
        self._nyan_frame_idx = 0
        self._nyan_frame_tick = 0
        def nyan_tick():
            if not getattr(self, '_nyan_active', False):
                return False
            self._nyan_x += 16
            # Advance animation frame every 4 ticks (~10 fps)
            self._nyan_frame_tick += 1
            if self._nyan_frame_tick >= 2:
                self._nyan_frame_tick = 0
                self._nyan_frame_idx = (self._nyan_frame_idx + 1) % self.NYAN_FRAME_COUNT
            if self._nyan_x > self.screen_w + 20:
                self._nyan_active = False
                if self._canvas_area:
                    self._canvas_area.queue_draw()
                return False
            if self._canvas_area:
                self._canvas_area.queue_draw()
            return True
        GLib.timeout_add(40, nyan_tick)

    def _draw_nyan(self, ctx):
        """Draw the animated nyan cat + tiled rainbow trail with vertical wiggle."""
        if not self._nyan_frames:
            return
        scale = self._nyan_scale
        cat_h = self._nyan_target_h
        nx, ny = self._nyan_x, self._nyan_y
        # Rainbow tile: tile horizontally from x=0 to the cat's left edge
        tile_w = int(self._nyan_rain_w * scale)
        tile_h = int(self._nyan_rain_h * scale)
        rain_y_base = ny + (cat_h - tile_h) // 2
        t_now = time.monotonic()
        x = 0
        tile_idx = 0
        # Rainbow ends right at the cat's rear (left edge + small overlap so there's no gap)
        rain_end_x = nx + cat_h * 0.15  # overlap under the cat a bit
        while x < rain_end_x:
            # Vertical wave — slow wiggle for the whole trail
            wave = math.sin((x - t_now * 200) * 0.015 + tile_idx * 0.4) * 5
            ctx.save()
            ctx.translate(x, rain_y_base + wave)
            ctx.scale(scale, scale)
            ctx.set_source_surface(self._nyan_rain_surface, 0, 0)
            ctx.get_source().set_filter(cairo.FILTER_NEAREST)
            ctx.paint()
            ctx.restore()
            x += tile_w
            tile_idx += 1
        # Draw the current animation frame of the cat on top
        frame = self._nyan_frames[self._nyan_frame_idx]
        ctx.save()
        ctx.translate(nx, ny)
        ctx.scale(scale, scale)
        ctx.set_source_surface(frame, 0, 0)
        ctx.get_source().set_filter(cairo.FILTER_NEAREST)
        ctx.paint()
        ctx.restore()

    def _draw_rm_rf_wipe(self, ctx, cat):
        """Draw the rm -rf 'wipe trail' — a translucent band behind the cat
        with a subtle scan-line effect that reads as 'this area is being
        erased'. Runs only while cat._rm_rf_active is True."""
        if not getattr(cat, "_rm_rf_active", False):
            return
        direction = getattr(cat, "_rm_rf_wipe_direction", "east")
        wipe_x = getattr(cat, "_rm_rf_wipe_x", cat.x + cat.display_w / 2)
        # Trail extends from the cat's trailing edge all the way back to the
        # starting side of the screen.
        if direction == "east":
            band_x0 = 0
            band_x1 = wipe_x
        else:
            band_x0 = wipe_x
            band_x1 = self.screen_w
        if band_x1 <= band_x0:
            return
        # Subtle translucent beige band — same palette as the chat bubble
        # so it looks like 'paper' being revealed underneath.
        ctx.set_source_rgba(0.95, 0.9, 0.8, 0.28)
        ctx.rectangle(band_x0, 0, band_x1 - band_x0, self.screen_h)
        ctx.fill()
        # Scan-line effect — thin darker lines every 8 px for a retro
        # 'wipe progress bar' vibe.
        ctx.set_source_rgba(0.3, 0.2, 0.1, 0.20)
        ctx.set_line_width(1)
        for y in range(0, int(self.screen_h), 8):
            ctx.move_to(band_x0, y)
            ctx.line_to(band_x1, y)
            ctx.stroke()
        # Leading edge — a bright cream vertical line right behind the cat
        # so you see exactly where the 'deletion' is happening.
        ctx.set_source_rgba(1.0, 0.95, 0.75, 0.85)
        ctx.set_line_width(3)
        edge_x = wipe_x - (cat.display_w / 2 if direction == "east" else -cat.display_w / 2)
        ctx.move_to(edge_x, 0)
        ctx.line_to(edge_x, self.screen_h)
        ctx.stroke()

    def _draw_easter_menu(self, ctx):
        bx, by = self._easter_menu_x, self._easter_menu_y
        bw, bh = self._easter_menu_w, self._easter_menu_h
        pad = self._EASTER_MENU_PAD
        cell_w = self._EASTER_MENU_CELL_W
        cell_h = self._EASTER_MENU_CELL_H
        cols = self._EASTER_MENU_COLS
        title_h = self._EASTER_MENU_TITLE_H
        # Dark backdrop
        ctx.set_source_rgba(0, 0, 0, 0.5)
        ctx.rectangle(0, 0, self.screen_w, self.screen_h)
        ctx.fill()
        # Menu background
        ctx.set_source_rgba(0.95, 0.9, 0.8, 0.97)
        ctx.rectangle(bx, by, bw, bh)
        ctx.fill()
        # Border 3px
        px = 3
        ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
        ctx.rectangle(bx, by, bw, px); ctx.fill()
        ctx.rectangle(bx, by + bh - px, bw, px); ctx.fill()
        ctx.rectangle(bx, by, px, bh); ctx.fill()
        ctx.rectangle(bx + bw - px, by, px, bh); ctx.fill()
        # Title
        title = "\U0001f95a  EASTER EGGS  \U0001f95a"
        lay = PangoCairo.create_layout(ctx)
        lay.set_font_description(Pango.FontDescription("sans bold 16"))
        lay.set_text(title, -1)
        tw, _th = lay.get_pixel_size()
        ctx.move_to(bx + (bw - tw) / 2, by + pad / 2)
        ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
        PangoCairo.show_layout(ctx, lay)
        # Items
        self._easter_menu_items = []
        grid_y = by + title_h + pad / 2
        for i, (key, emoji, label, _fn) in enumerate(EASTER_EGGS):
            col = i % cols
            row = i // cols
            ix = bx + pad + col * cell_w
            iy = grid_y + row * cell_h
            iw, ih = cell_w - 8, cell_h - 4
            # Button bg
            ctx.set_source_rgba(0.85, 0.75, 0.55, 0.85)
            ctx.rectangle(ix, iy, iw, ih); ctx.fill()
            # Border
            ctx.set_source_rgba(0.3, 0.2, 0.1, 0.8)
            ctx.set_line_width(1)
            ctx.rectangle(ix, iy, iw, ih); ctx.stroke()
            # Text
            text = f"{emoji}  {label}"
            lay_i = PangoCairo.create_layout(ctx)
            lay_i.set_font_description(Pango.FontDescription("sans bold 12"))
            lay_i.set_text(text, -1)
            _tiw, _tih = lay_i.get_pixel_size()
            ctx.move_to(ix + 10, iy + (ih - _tih) / 2)
            ctx.set_source_rgba(0.15, 0.1, 0.05, 1)
            PangoCairo.show_layout(ctx, lay_i)
            self._easter_menu_items.append(((ix, iy, iw, ih), key))
        # Footer hint
        hint = "Click an egg or outside the menu to close"
        lay_h = PangoCairo.create_layout(ctx)
        lay_h.set_font_description(Pango.FontDescription("sans italic 10"))
        lay_h.set_text(hint, -1)
        hw, _hh = lay_h.get_pixel_size()
        ctx.move_to(bx + (bw - hw) / 2, by + bh - pad)
        ctx.set_source_rgba(0.4, 0.3, 0.2, 0.8)
        PangoCairo.show_layout(ctx, lay_h)
