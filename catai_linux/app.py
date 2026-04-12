#!/usr/bin/env python3
"""CATAI-Linux — Virtual desktop pet cats for Linux (GNOME/Wayland)
Port of https://github.com/wil-pe/CATAI (macOS) to GTK4.
Single fullscreen transparent canvas with Cairo rendering.
"""
from __future__ import annotations

# Force X11 backend — needed for XShape input passthrough + chat bubble positioning
import os
os.environ.setdefault("GDK_BACKEND", "x11")

import cairo
import functools
import gc
import json
import logging
import random
import re
import shutil
import time
import sys
import threading
from typing import Any
import uuid
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

log = logging.getLogger("catai")

def _setup_logging():
    level = logging.DEBUG if "--debug" in sys.argv else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")
    # Silence noisy loggers
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkX11", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, Gio, GLib, Gst, Gtk, Pango, PangoCairo

Gst.init(None)

from PIL import Image

from catai_linux.voice import (
    VOICE_AVAILABLE, VoiceRecorder, WHISPER_MODEL_SIZES,
    is_model_cached as _whisper_model_cached,
)
from catai_linux.wake_word import WAKE_AVAILABLE, WakeWordListener

# ── Constants (from catai_linux.constants) ─────────────────────────────────────

from catai_linux.constants import (  # noqa: E402
    RENDER_MS, BEHAVIOR_MS, WALK_SPEED, PETTING_THRESHOLD_MS,
    DEFAULT_SCALE, BOTTOM_MARGIN,
    CatState, ANIM_KEYS, ONE_SHOT_STATES,
    SEQUENCES,
    CATSET_PERSONALITIES,  # re-exported for catai_linux.reactions
)

CONFIG_DIR = os.path.expanduser("~/.config/catai")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

from catai_linux.l10n import L10n  # noqa: E402
from catai_linux.easter_eggs import (  # noqa: E402
    EasterEggMixin, MAGIC_EGG_PHRASES, EASTER_EGGS,
)

# Baby emojis for kitten meows — they can't talk yet, just symbols
BABY_MEOWS = [
    "\U0001f42d",  # 🐭 mouse
    "\U0001f37c",  # 🍼 bottle
    "\u2665",      # ♥ heart
    "\U0001f9f8",  # 🧸 teddy bear
    "\U0001f423",  # 🐣 hatching chick
    "\U0001f95b",  # 🥛 glass of milk
    "\u2728",      # ✨ sparkles
    "\U0001fa9b",  # 🪛 (rattle proxy)
    "\U0001f431",  # 🐱 cat face
    "\U0001f436",  # 🐶 (playmate)
    "\U0001f4a4",  # 💤
    "\U0001f31f",  # 🌟
    "\U0001f380",  # 🎀 ribbon
    "\U0001f4a9",  # 💩 (kids love it)
    "\U0001f4a8",  # 💨 (prout)
]


# MAGIC_EGG_PHRASES, EASTER_EGGS — moved to catai_linux.easter_eggs

def _catset_prompt(char_id, name, lang):
    p = CATSET_PERSONALITIES.get(char_id, CATSET_PERSONALITIES["cat01"])
    t = p["traits"].get(lang, p["traits"]["fr"])
    sk = p["skills"].get(lang, p["skills"]["fr"])
    # The TTS pipeline reads text chunks via Piper and plays cat
    # onomatopoeia as real CC0 samples. If a response is ONLY cat
    # sounds, Piper has nothing to say and the user hears meows
    # with no words. The prompt explicitly asks for a real sentence
    # on top of the cat sounds, and forbids stage directions which
    # the splitter would drop anyway.
    if lang == "en":
        return (
            f"You are a little {t} cat named {name}. {sk} "
            f"Always answer with AT LEAST one full real English sentence "
            f"the user can understand, and you may sprinkle short cat "
            f"sounds like 'meow', 'purr', 'mrrp'. Max 2 sentences. "
            f"Never use emoji or *stage directions*."
        )
    elif lang == "es":
        return (
            f"Eres un gatito {t} llamado {name}. {sk} "
            f"Siempre responde con AL MENOS una frase real completa en "
            f"español que el usuario pueda entender, y puedes añadir "
            f"sonidos cortos de gato como 'miau', 'purr', 'mrrp'. "
            f"Máximo 2 frases. Nunca uses emoji ni *acciones*."
        )
    return (
        f"Tu es un petit chat {t} nommé {name}. {sk} "
        f"Réponds TOUJOURS avec AU MOINS une vraie phrase complète en "
        f"français compréhensible, et tu peux ajouter quelques sons de "
        f"chat courts comme 'miaou', 'prrrt', 'mrrp'. Max 2 phrases. "
        f"N'utilise jamais d'emoji ni de *didascalies*."
    )

# ── Persistence ────────────────────────────────────────────────────────────────

def _ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)

def load_config() -> dict[str, Any]:
    _ensure_config_dir()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Corrupted config, using defaults: %s", e)
    return {}

def _atomic_write(path, data):
    """Write JSON atomically (temp + rename) to avoid corruption."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Failed to save %s: %s", path, e)

def save_config(cfg: dict[str, Any]) -> None:
    _ensure_config_dir()
    _atomic_write(CONFIG_FILE, cfg)

def load_memory(cat_id):
    path = os.path.join(CONFIG_DIR, f"mem_{cat_id}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Corrupted memory for %s, resetting: %s", cat_id, e)
    return []

def save_memory(cat_id, msgs):
    _ensure_config_dir()
    s = msgs[:]
    if len(s) > MEM_MAX * 2 + 1:
        s = [s[0]] + s[-(MEM_MAX * 2):]
    _atomic_write(os.path.join(CONFIG_DIR, f"mem_{cat_id}.json"), s)

def delete_memory(cat_id):
    path = os.path.join(CONFIG_DIR, f"mem_{cat_id}.json")
    if os.path.exists(path):
        os.remove(path)

# ── Autostart ─────────────────────────────────────────────────────────────────

AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "catai.desktop")

def is_autostart():
    return os.path.exists(AUTOSTART_FILE)

def set_autostart(enabled):
    if enabled:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        catai_cmd = shutil.which("catai")
        exec_cmd = catai_cmd if catai_cmd else f'python3 "{os.path.abspath(__file__)}"'
        with open(AUTOSTART_FILE, "w") as f:
            f.write(f"""[Desktop Entry]
Type=Application
Name=CATAI
Comment=Virtual desktop pet cats
Exec={exec_cmd}
Terminal=false
X-GNOME-Autostart-enabled=true
""")
    elif os.path.exists(AUTOSTART_FILE):
        os.remove(AUTOSTART_FILE)

# ── Sprite Loading & Tinting ──────────────────────────────────────────────────

def load_metadata(cat_dir):
    path = os.path.join(cat_dir, "metadata.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: Cannot load {path}: {e}")
        sys.exit(1)

def _sprite_floor_y(img: Image.Image) -> int:
    """Return Y of the lowest non-transparent pixel in a PIL RGBA image
    (sprite 'floor'). Uses PIL's getbbox() on the alpha channel — one
    C-level scan instead of a pixel-by-pixel Python loop."""
    # getbbox() returns (left, upper, right, lower) of non-zero area
    bbox = img.split()[3].getbbox()  # alpha channel
    if bbox is None:
        return img.size[1] - 1
    return bbox[3] - 1  # lower edge, zero-indexed


def _sprite_center_x(img: Image.Image) -> float:
    """Return X centroid of non-transparent pixels. Uses PIL getbbox()
    as a fast estimate — returns the center of the bounding box rather
    than a true centroid, which is close enough for sprite anchoring
    and avoids a per-pixel Python loop."""
    bbox = img.split()[3].getbbox()
    if bbox is None:
        return img.size[0] / 2.0
    return (bbox[0] + bbox[2]) / 2.0


OFFSET_ANIMS = {
    "climbing", "wallclimb", "wallgrab", "fall", "ledgegrab",
    "ledgeidle", "ledgeclimb-struggle", "die", "land", "dash", "hurt",
}

def _compute_anim_offsets(meta, cat_dir):
    """Return dict: anim_key -> {direction -> (y_off, x_off)} in sprite pixels.
    y_off > 0 means the sprite's floor moved UP relative to idle (shift self.y down to compensate).
    x_off > 0 means the sprite shifted RIGHT relative to idle.
    """
    offsets = {}
    try:
        south_rel = meta["frames"]["rotations"].get("south", "")
        ref_img = Image.open(os.path.join(cat_dir, south_rel)).convert("RGBA")
        ref_floor = _sprite_floor_y(ref_img)
        ref_cx = _sprite_center_x(ref_img)
    except Exception:
        return offsets

    for anim_key in OFFSET_ANIMS:
        anim_data = meta["frames"]["animations"].get(anim_key, {})
        if not anim_data:
            continue
        offsets[anim_key] = {}
        for direction, paths in anim_data.items():
            if not paths:
                continue
            try:
                last_img = Image.open(os.path.join(cat_dir, paths[-1])).convert("RGBA")
                y_off = ref_floor - _sprite_floor_y(last_img)
                x_off = _sprite_center_x(last_img) - ref_cx
                offsets[anim_key][direction] = (y_off, x_off)
            except Exception:
                offsets[anim_key][direction] = (0, 0)
    return offsets


def pil_to_surface(img: Image.Image, target_w: int, target_h: int) -> tuple[cairo.ImageSurface, bytearray]:
    """Convert PIL Image to cairo.ImageSurface, scaled nearest-neighbor.
    Returns (surface, data_ref) — data_ref MUST be kept alive while surface is used."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    scaled = img.resize((target_w, target_h), Image.NEAREST)
    # Swap RGBA → BGRA for cairo ARGB32 (little-endian) using PIL's
    # native channel split/merge. ~50x faster than the old per-pixel
    # Python loop on a typical 80×80 sprite (0.02 ms vs 1 ms).
    r, g, b, a = scaled.split()
    scaled = Image.merge("RGBA", (b, g, r, a))
    data = bytearray(scaled.tobytes())
    surface = cairo.ImageSurface.create_for_data(
        data, cairo.FORMAT_ARGB32, target_w, target_h, target_w * 4)
    return surface, data  # caller must keep data alive


# ── Surface cache ─────────────────────────────────────────────────────────────
# Caches (cairo.ImageSurface, data_ref) by (path, w, h) so identical sprites
# at the same scale are loaded once. The data_ref is kept alive by the cache.

_surface_cache: dict[tuple[str, int, int], tuple[cairo.ImageSurface, bytearray]] = {}


def pil_to_surface_cached(path: str, target_w: int, target_h: int) -> tuple[cairo.ImageSurface, bytearray]:
    """Cached wrapper around load_sprite + pil_to_surface."""
    key = (path, target_w, target_h)
    cached = _surface_cache.get(key)
    if cached is not None:
        return cached
    result = pil_to_surface(load_sprite(path), target_w, target_h)
    _surface_cache[key] = result
    return result


def pil_to_texture(img, target_w, target_h):
    """Convert PIL Image to Gdk.MemoryTexture, scaled nearest-neighbor.
    Still needed for settings window previews (Gtk.Picture)."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    scaled = img.resize((target_w, target_h), Image.NEAREST)
    data = scaled.tobytes()
    gbytes = GLib.Bytes.new(data)
    return Gdk.MemoryTexture.new(target_w, target_h, Gdk.MemoryFormat.R8G8B8A8,
                                  gbytes, target_w * 4)


@functools.lru_cache(maxsize=512)
def load_sprite(path: str) -> Image.Image:
    """Load a sprite PNG as a PIL RGBA image. Returns a magenta placeholder on error.
    Catset sprites are pre-colored, no tinting needed."""
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        log.warning("Missing sprite: %s", path)
        return Image.new("RGBA", (80, 80), (255, 0, 255, 128))


from catai_linux.x11_helpers import (  # noqa: E402
    _get_xid, move_window, flush_x11, _apply_xid_hints,
    set_always_on_top, set_notification_type, unregister_window,
    apply_above_all as _apply_above_all,
    update_input_shape as _update_input_shape,
    get_window_y_offset as _x11_window_y_offset,
    get_mouse_position as _x11_mouse_position,
)

from catai_linux.reactions import ReactionPool  # noqa: E402
from catai_linux.mood import CatMood  # noqa: E402
from catai_linux.activity import ActivityMonitor  # noqa: E402
from catai_linux import personality as _personality  # noqa: E402
from catai_linux import monitors as _monitors_mod  # noqa: E402
from catai_linux import seasonal as _seasonal  # noqa: E402
from catai_linux import tts as _tts  # noqa: E402
from catai_linux import updater as _updater  # noqa: E402
from catai_linux import metrics as _metrics  # noqa: E402
from catai_linux import character_packs as _character_packs  # noqa: E402
from catai_linux import memory as _memory  # noqa: E402


from catai_linux.chat_backend import (  # noqa: E402
    MEM_MAX,
    ChatBackend, ClaudeChat, create_chat,
)
from catai_linux.config_schema import validate_config  # noqa: E402


from catai_linux.drawing import (  # noqa: E402
    apply_css, BUBBLE_FONT as _BUBBLE_FONT,
    set_theme as _set_theme,
    draw_meow_bubble as _draw_meow_bubble,
    draw_encounter_bubble as _draw_encounter_bubble,
    draw_chat_bubble as _draw_chat_bubble,
    draw_context_menu as _draw_context_menu,
    draw_zzz as _draw_zzz,
    draw_exclamation as _draw_exclamation,
    draw_hearts as _draw_hearts,
    draw_hurt_stars as _draw_hurt_stars,
    draw_skull as _draw_skull,
    draw_birth_sparkles as _draw_birth_sparkles,
    draw_sparkle as _draw_sparkle,
    draw_anger as _draw_anger,
    draw_speed_lines as _draw_speed_lines,
)
from catai_linux.theme import is_dark_mode as _is_dark_mode  # noqa: E402


# ── Cat Instance ───────────────────────────────────────────────────────────────

class CatInstance:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config: dict[str, Any] = config
        self.rotations: dict[str, tuple[cairo.ImageSurface, bytearray]] = {}
        self.animations: dict[str, dict[str, list[tuple[cairo.ImageSurface, bytearray]]]] = {}
        self.state: CatState = CatState.IDLE
        self.direction: str = "south"
        self.frame_index: int = 0
        self.idle_ticks: int = 0
        self.x: float = 0.0
        self.y: float = 0.0
        self.dest_x: float = 0.0
        self.dest_y: float = 0.0
        self.display_w: int = 0
        self.display_h: int = 0
        self.dragging: bool = False
        self.drag_start_x: float = 0.0
        self.drag_start_y: float = 0.0
        self.drag_win_x: float = 0.0
        self.drag_win_y: float = 0.0
        self.mouse_moved: bool = False
        self.chat_backend: ChatBackend | None = None
        self.screen_w: int = 0
        self.screen_h: int = 0
        # Per-cat voice output toggle. Controls whether this cat's chat
        # responses go through the TTS hybrid pipeline. Default off so
        # the feature is pure opt-in — toggled via the speaker icon in
        # the chat bubble (see _on_canvas_click) and persisted in the
        # cat's config dict so it survives restarts.
        self.tts_enabled: bool = bool(config.get("tts_enabled", False))
        self._app = None
        self._meta = None
        self._cat_dir = ""
        self._anim_offsets = {}         # anim_key -> {direction -> (dy, dx)} in display-px
        self._sprite_bottom_padding = 0 # px of empty rows between sprite feet and box bottom
        self.is_kitten: bool = False          # True for kittens born from love encounters
        self.is_apocalypse_clone: bool = False # True for clones spawned by apocalypse mode
        self._birth_progress = None     # None = fully visible; 0.0..1.0 = birth animation
        self._flip_h = False            # Horizontal flip (override for face-each-other in encounters)
        self._sequence = None           # list[SequenceStep] or None
        self._sequence_index = 0
        self._sequence_direction = None # locked east/west for the whole sequence
        self._sequence_pause_ticks = 0
        self._sequence_loop_counter = 1
        # Animation state ticks — previously created dynamically via
        # getattr(self, '_state_tick', 0). Now explicit so the class is
        # inspectable and IDE-friendly.
        self._state_tick = 0
        self._die_threshold = 0
        self._die_resurrect = 0
        self._sleep_tick = 0
        # Meow bubble state (drawn on canvas)
        self.meow_text: str = ""
        self.meow_visible: bool = False
        self._meow_timer_id = None
        # Chat bubble state (drawn on canvas, entry via overlay)
        self.chat_visible: bool = False
        self.chat_response: str = ""
        # Encounter state (cat-to-cat conversation)
        self.in_encounter: bool = False
        self.encounter_text: str = ""
        self.encounter_visible: bool = False
        # Easter egg per-cat state — initialized here so all dynamic
        # attributes are visible in __init__. Previously scattered as
        # getattr(self, '_foo', default) across easter egg methods.
        self._petting_active = False
        self._hidden = False
        self._boss_scale = 0
        self._beam_ticks = 0
        self._rm_rf_active = False
        self._nyan_active = False
        # Invisible mood system — loaded/created on setup() once we know
        # the cat's ID. Default sentinel prevents crashes from code paths
        # that touch mood before setup() runs.
        self.mood: CatMood = CatMood()
        self._encounter_cooldown_until: float = 0.0  # monotonic timestamp

    def setup(self, app: CatAIApp, meta: dict[str, Any], cat_dir: str, dw: int, dh: int, model: str, lang: str, start_x: float, screen_w: int, screen_h: int) -> None:
        self._app = app
        self.display_w = dw
        self.display_h = dh
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.x = start_x
        self.y = random.randint(int(screen_h * 0.3), screen_h - dh)
        self.dest_x = start_x
        self.dest_y = self.y
        self._meta = meta
        self._cat_dir = cat_dir
        # Default offsets + padding until bg thread computes them
        self._anim_offsets = {}
        self._sprite_bottom_padding = 0
        # Load persisted mood state (fresh default on any error / first run)
        self.mood = CatMood.load(self.config["id"])
        # Load persisted personality drift state (quirks accumulated from
        # previous chat sessions — appended to the system prompt below).
        self.personality = _personality.PersonalityState.load(self.config["id"])

        # Everything heavy (sprite loading, anim offset computation, pixel
        # scans, chat backend creation which may do HTTP + subprocess) goes
        # into one background thread so main loop (including clicks) stays
        # responsive from t=0.
        self.load_assets(meta, cat_dir)
        sprite_w = meta["character"]["size"]["width"]
        sprite_h = meta["character"]["size"]["height"]
        scale_x = dw / sprite_w
        scale_y = dh / sprite_h

        def bg_setup():
            # 1. Anim offsets (14 anims × PIL Image.open + pixel-loop)
            raw_offsets = _compute_anim_offsets(meta, cat_dir)
            scaled_offsets = {}
            for anim_key, dirs in raw_offsets.items():
                scaled_offsets[anim_key] = {}
                for direction, (y_off, x_off) in dirs.items():
                    scaled_offsets[anim_key][direction] = (round(y_off * scale_y), round(x_off * scale_x))
            GLib.idle_add(lambda d=scaled_offsets: setattr(self, "_anim_offsets", d) or False)

            # 2. Sprite floor/bottom padding
            try:
                south_rel = meta["frames"]["rotations"].get("south", "")
                ref_img = Image.open(os.path.join(cat_dir, south_rel)).convert("RGBA")
                ref_floor = _sprite_floor_y(ref_img)
                padding = round((sprite_h - 1 - ref_floor) * scale_y)
            except Exception:
                padding = 0
            GLib.idle_add(lambda p=padding: setattr(self, "_sprite_bottom_padding", p) or False)

            # 3. Chat backend (may do httpx.get Ollama probe + subprocess OAuth refresh)
            self.setup_chat(model, lang)

        threading.Thread(target=bg_setup, daemon=True).start()

    def setup_chat(self, model, lang):
        prompt = _catset_prompt(self.config["char_id"], self.config["name"], lang)
        # Append any drifted personality quirks collected over prior chats
        if getattr(self, "personality", None) is not None:
            prompt = self.personality.append_to_prompt(prompt, lang)
        self.chat_backend = create_chat(model)
        self.chat_backend.messages = [{"role": "system", "content": prompt}]
        mem = load_memory(self.config["id"])
        if len(mem) > 1:
            self.chat_backend.messages.extend(mem[1:])

    def load_assets(self, meta: dict[str, Any], cat_dir: str, lazy: bool = True) -> None:
        """Load sprites as cairo.ImageSurface. Rotations + running anim in a
        background thread so startup doesn't block the main thread/clicks.
        Remaining animations also load in background after."""
        dw, dh = self.display_w, self.display_h
        self.rotations = {}
        self.animations = {}
        walk_key = "running-8-frames"

        def bg_load():
            # 1. Rotations first (cat becomes visible & clickable as soon as these are ready)
            rots = {}
            for dir_name, rel_path in meta["frames"]["rotations"].items():
                rots[dir_name] = pil_to_surface_cached(os.path.join(cat_dir, rel_path), dw, dh)
            GLib.idle_add(lambda: self.rotations.update(rots) or False)

            # 2. Running anim (needed for WALKING)
            if walk_key in meta["frames"]["animations"]:
                walk_data = {}
                for dir_name, frame_paths in meta["frames"]["animations"][walk_key].items():
                    walk_data[dir_name] = [
                        pil_to_surface_cached(os.path.join(cat_dir, p), dw, dh)
                        for p in frame_paths
                    ]
                GLib.idle_add(lambda d=walk_data: self.animations.update({walk_key: d}) or False)

            # 3. Everything else (only if lazy mode)
            if lazy:
                remaining = {k: v for k, v in meta["frames"]["animations"].items() if k != walk_key}
                for anim_name, dirs in remaining.items():
                    anim_data = {}
                    for dir_name, frame_paths in dirs.items():
                        anim_data[dir_name] = [
                            pil_to_surface_cached(os.path.join(cat_dir, p), dw, dh)
                            for p in frame_paths
                        ]
                    GLib.idle_add(lambda an=anim_name, ad=anim_data: self.animations.update({an: ad}) or False)
            else:
                # Synchronous for scale changes
                for anim_name, dirs in meta["frames"]["animations"].items():
                    if anim_name == walk_key:
                        continue
                    anim_data = {}
                    for dir_name, frame_paths in dirs.items():
                        anim_data[dir_name] = [
                            pil_to_surface_cached(os.path.join(cat_dir, p), dw, dh)
                            for p in frame_paths
                        ]
                    GLib.idle_add(lambda an=anim_name, ad=anim_data: self.animations.update({an: ad}) or False)

        if lazy:
            threading.Thread(target=bg_load, daemon=True).start()
        else:
            bg_load()  # still threaded via GLib.idle_add for surface updates

    def _fallback_surface(self):
        if not self.rotations:
            return None  # still loading in background
        return self.rotations.get(self.direction) or self.rotations.get("south") or next(iter(self.rotations.values()))

    def _current_surface(self):
        """Return (cairo.ImageSurface, data_ref) for the current frame."""
        if self.state in (CatState.IDLE, CatState.SLEEPING, CatState.SOCIALIZING):
            return self._fallback_surface()
        # SLEEPING_BALL: loop 4 frames slowly; fall back to idle if not yet loaded
        if self.state == CatState.SLEEPING_BALL:
            frames = self.animations.get("sleeping-ball", {}).get("south", [])
            if frames:
                return frames[self.frame_index % len(frames)]
            return self._fallback_surface()
        key = ANIM_KEYS.get(self.state)
        if key:
            frames = self.animations.get(key, {}).get(self.direction, [])
            if frames:
                return frames[self.frame_index % len(frames)]
        return self._fallback_surface()

    def render_tick(self) -> None:
        """Update position/animation state. No window management needed."""
        if self.dragging:
            return

        # Advance birth animation (~3s at 12fps behavior tick = 36 steps, but
        # render_tick runs at ~15fps, so ~45 ticks → bump progress per tick)
        if self._birth_progress is not None:
            self._birth_progress += 1.0 / 45.0
            if self._birth_progress >= 1.0:
                self._birth_progress = None

        # Defensive: make sure we always start a tick inside screen bounds
        self._clamp_to_screen()

        if self.state == CatState.WALKING:
            dest_y = self.dest_y
            dx = self.dest_x - self.x
            dy = dest_y - self.y
            dist = max(abs(dx), abs(dy))
            if dist <= WALK_SPEED:
                self.x = self.dest_x
                self.y = dest_y
                self.state = CatState.IDLE
                self.frame_index = 0
                self.idle_ticks = 0
            else:
                ratio = WALK_SPEED / dist
                step_x = dx * ratio
                step_y = dy * ratio
                self.x += step_x
                self.y += step_y
                if abs(dx) > abs(dy):
                    self.direction = "east" if step_x > 0 else "west"
                elif dy < 0:
                    self.direction = "north" if abs(dy) > abs(dx) else ("north-east" if step_x > 0 else "north-west")
                else:
                    self.direction = "south" if abs(dy) > abs(dx) else ("south-east" if step_x > 0 else "south-west")
                # Only east/west — catset sprites have no north/south/diagonal walk frames
                self.direction = "east" if step_x >= 0 else "west"
                self.frame_index += 1
            self.x = max(0, min(self.x, self.screen_w - self.display_w))
            self.y = max(0, min(self.y, self.screen_h - self.display_h))
        elif self.state == CatState.DASHING:
            # Fast horizontal movement with dash animation
            speed = WALK_SPEED * 3
            if self.direction == "east":
                self.x += speed
            else:
                self.x -= speed
            self.x = max(0, min(self.x, self.screen_w - self.display_w))
            frames = self.animations.get("dash", {}).get(self.direction, [])
            if not frames or self.x <= 0 or self.x >= self.screen_w - self.display_w:
                self._end_current_step()
            else:
                self.frame_index = (self.frame_index + 1) % len(frames)
        elif self.state == CatState.DYING:
            # Stay dead for 5-10s, slow frame loop, then resurrect
            frames = self.animations.get("die", {}).get(self.direction, [])
            if not frames:
                self._end_current_step()
            else:
                self._state_tick = getattr(self, '_state_tick', 0) + 1
                if not hasattr(self, '_die_threshold'):
                    self._die_threshold = random.randint(40, 80)
                # Slow loop on last frames
                if self._state_tick % 6 == 0:
                    half = len(frames) // 2
                    self.frame_index = half + (self.frame_index - half + 1) % (len(frames) - half)
                # After 5-10s, resurrect
                if self._state_tick > self._die_threshold:
                    self._state_tick = 0
                    del self._die_threshold
                    if self._sequence:
                        self._end_current_step()
                    else:
                        # Direct transition: hurt → waking up via _die_resurrect counter
                        self._die_resurrect = 3
                        self.state = CatState.HURTING
                        self.direction = "south"
                        self.frame_index = 0
                        log.warning("RESURRECT: %s → HURTING (frames=%d)",
                                 self.config.get("char_id", "?"),
                                 len(self.animations.get("hurt", {}).get("south", [])))
        elif self.state == CatState.HURTING:
            # Custom handler: supports resurrection loop (_die_resurrect counter)
            frames = self.animations.get("hurt", {}).get(self.direction, [])
            if not frames:
                log.warning("HURTING: no frames for dir=%s, anims_keys=%s", self.direction, list(self.animations.keys()))
                self._end_current_step()
            elif self.frame_index >= len(frames) - 1:
                resurrect = getattr(self, '_die_resurrect', 0)
                if resurrect > 1:
                    # Loop hurt animation again
                    self._die_resurrect = resurrect - 1
                    self.frame_index = 0
                elif resurrect == 1:
                    # Last loop done → waking up
                    self._die_resurrect = 0
                    self.state = CatState.WAKING_UP
                    self.direction = "south"
                    self.frame_index = 0
                else:
                    # Normal one-shot (in sequence or standalone)
                    self._end_current_step()
            else:
                self.frame_index += 1
        elif self.state == CatState.WALLGRAB:
            # Slide downward with acceleration (glass surface feel)
            frames = self.animations.get("wallgrab", {}).get(self.direction, [])
            if not frames:
                self._end_current_step()
            else:
                self._state_tick = getattr(self, '_state_tick', 0) + 1
                # v₀=2, +0.15/tick, capped at 8 → ~360px over 60 ticks (~4s)
                velocity = min(8.0, 2.0 + self._state_tick * 0.15)
                self.y += velocity
                self.frame_index = (self.frame_index + 1) % len(frames)

                hit_bottom = self.y >= self.screen_h - self.display_h - BOTTOM_MARGIN
                if hit_bottom:
                    self.y = self.screen_h - self.display_h - BOTTOM_MARGIN
                    # Crash! If slid significantly, 40% chance of drama_queen
                    if self._state_tick > 15 and random.random() < 0.40:
                        self._state_tick = 0
                        self._sequence = None
                        self._sequence_index = 0
                        self._start_sequence("drama_queen")
                        return
                    self._state_tick = 0
                    self._end_current_step()
                elif self._state_tick > 60:  # ~4s max slide
                    self._state_tick = 0
                    self._end_current_step()
        elif self.state == CatState.LEDGEIDLE:
            # Hang for a bit then move on
            frames = self.animations.get("ledgeidle", {}).get(self.direction, [])
            if not frames:
                self._end_current_step()
            else:
                self._state_tick = getattr(self, '_state_tick', 0) + 1
                self.frame_index = (self.frame_index + 1) % len(frames)
                # After ~2-3s, move on
                if self._state_tick > random.randint(16, 24):
                    self._state_tick = 0
                    self._end_current_step()
        elif self.state == CatState.SLEEPING_BALL:
            # Advance breathing frame every 6 render ticks (~0.75s per frame, ~3s per breath)
            self._sleep_tick = getattr(self, '_sleep_tick', 0) + 1
            if self._sleep_tick >= 6:
                self._sleep_tick = 0
                self.frame_index = (self.frame_index + 1) % 4
        elif self.in_encounter and self.state in (CatState.LOVE, CatState.SURPRISED, CatState.ANGRY):
            # During love encounter, loop the expression animation instead of ending
            key = ANIM_KEYS.get(self.state)
            frames = self.animations.get(key, {}).get(self.direction, [])
            if frames:
                self.frame_index = (self.frame_index + 1) % len(frames)
        elif self.state in ONE_SHOT_STATES:
            # Handle sequence pause (hold on last frame between steps)
            if self._sequence and self._sequence_pause_ticks > 0:
                self._sequence_pause_ticks -= 1
                return
            key = ANIM_KEYS.get(self.state)
            if key:
                frames = self.animations.get(key, {}).get(self.direction, [])
                if not frames:
                    self._end_current_step()
                elif self.frame_index >= len(frames) - 1:
                    self._end_current_step()
                else:
                    self.frame_index += 1

        # Safety clamp after any handler mutated position
        self._clamp_to_screen()

    def _end_current_step(self):
        """Called when the current animation finishes. Advance sequence or go IDLE."""
        if self._sequence and self._sequence_index < len(self._sequence):
            step = self._sequence[self._sequence_index]
            # Handle loop_count
            if self._sequence_loop_counter < step.loop_count:
                self._sequence_loop_counter += 1
                self.frame_index = 0
                return
            # Handle pause_after
            if step.pause_after > 0 and self._sequence_pause_ticks == 0:
                self._sequence_pause_ticks = step.pause_after
                return
            # Advance to next step
            self._sequence_index += 1
            self._sequence_loop_counter = 1
            self._sequence_pause_ticks = 0
            if self._sequence_index < len(self._sequence):
                next_step = self._sequence[self._sequence_index]
                next_key = ANIM_KEYS.get(next_step.state)
                next_dir = "south" if next_step.direction_mode == "south" else self._sequence_direction
                frames = self.animations.get(next_key, {}).get(next_dir, [])
                if not frames:
                    log.warning("SEQUENCE: skip %s dir=%s (no frames, loaded=%s)", next_step.state, next_dir, list(self.animations.keys()))
                    self._end_current_step()
                    return
                self.state = next_step.state
                self.direction = next_dir
                self.frame_index = 0
                return
        # Sequence finished (or no sequence) → compensate offsets → IDLE
        self._apply_sequence_offset_compensation()
        self._sequence = None
        self._sequence_index = 0
        self._sequence_direction = None
        self._sequence_pause_ticks = 0
        self._sequence_loop_counter = 1
        self.state = CatState.IDLE
        self.frame_index = 0
        self.idle_ticks = 0

    def _apply_sequence_offset_compensation(self):
        """Apply accumulated offset from all steps played."""
        if self._sequence:
            total_y, total_x = 0, 0
            for i in range(min(self._sequence_index + 1, len(self._sequence))):
                step = self._sequence[i]
                step_key = ANIM_KEYS.get(step.state, "")
                step_dir = "south" if step.direction_mode == "south" else self._sequence_direction
                dy, dx = self._anim_offsets.get(step_key, {}).get(step_dir, (0, 0))
                total_y += dy
                total_x += dx
        else:
            key = ANIM_KEYS.get(self.state, "")
            dy, dx = self._anim_offsets.get(key, {}).get(self.direction, (0, 0))
            total_y, total_x = dy, dx
        self.y -= total_y
        self.x += total_x
        if self.y < 0:
            # Wrapped off the top (e.g. climbing up past the screen) → bottom
            self.y = self.screen_h - self.display_h - BOTTOM_MARGIN
        self._clamp_to_screen()

    def _start_sequence(self, seq_name):
        """Begin a multi-step animation sequence."""
        seq = SEQUENCES.get(seq_name)
        if not seq:
            return
        direction = random.choice(["east", "west"])
        self._sequence = list(seq)
        self._sequence_index = 0
        self._sequence_direction = direction
        self._sequence_loop_counter = 1
        self._sequence_pause_ticks = 0
        first_step = self._sequence[0]
        first_dir = "south" if first_step.direction_mode == "south" else direction
        first_key = ANIM_KEYS.get(first_step.state)
        frames = self.animations.get(first_key, {}).get(first_dir, [])
        if not frames:
            self._sequence = None
            return
        self.state = first_step.state
        self.direction = first_dir
        self.frame_index = 0
        self.idle_ticks = 0

    def _clamp_to_screen(self):
        """Defensive clamp — keep cat within screen bounds with a safety margin.
        Accounts for the GNOME top bar offset AND the sprite's own bottom padding
        (transparent rows below the cat feet), so the VISIBLE feet end up flush
        with the bottom of the screen (within BOTTOM_MARGIN)."""
        top_offset = self._app._canvas_y_offset if self._app else 0
        self.x = max(0, min(self.x, self.screen_w - self.display_w))
        # Add sprite_bottom_padding back — the "empty" rows can extend below
        # the screen without being visible
        max_y = self.screen_h - self.display_h - top_offset - BOTTOM_MARGIN + self._sprite_bottom_padding
        self.y = max(0, min(self.y, max_y))

    def behavior_tick(self) -> None:
        # Mood stats always tick, even when busy — a cat dragged around
        # still gets tired, a cat in a chat bubble still gets hungry over
        # time. The tick itself is dirt cheap.
        self.mood.tick(self.state.value)

        if self.chat_visible or self.dragging or self.in_encounter:
            return

        if self.state == CatState.IDLE:
            self.idle_ticks += 1
            r = self._roll_mood_adjusted()
            if self.idle_ticks > 15 and r < 0.05:
                self.state = CatState.SLEEPING_BALL
                self.frame_index = 0
                self._sleep_tick = 0
                self.direction = "south"  # only south frames available
                self.idle_ticks = 0
            elif r < 0.22:
                self.state = CatState.WALKING
                self.frame_index = 0
                self.dest_x = random.uniform(self.display_w, max(self.display_w + 1, self.screen_w - self.display_w))
                self.dest_y = self.y  # walk horizontally only — no vertical drift
            elif r < 0.27:
                self.state = CatState.EATING
                self.frame_index = 0
                self.direction = "south"
            elif r < 0.30:
                self._show_random_meow()
            elif r < 0.35:
                self.state = CatState.CHASING_MOUSE
                self.frame_index = 0
                self.direction = random.choice(["east", "west"])
            elif r < 0.40:
                self.state = CatState.FLAT
                self.frame_index = 0
                self.direction = "south"
            elif r < 0.45:
                self.state = CatState.GROOMING
                self.frame_index = 0
                self.direction = "south"
            elif r < 0.50:
                self.state = CatState.LOVE
                self.frame_index = 0
                self.direction = "south"
            elif r < 0.55:
                self.state = CatState.ROLLING
                self.frame_index = 0
                self.direction = "south"
            elif r < 0.60:
                self.state = CatState.SURPRISED
                self.frame_index = 0
                self.direction = random.choice(["east", "west"])
            elif r < 0.65:
                self.state = CatState.JUMPING
                self.frame_index = 0
                self.direction = "south"
            elif r < 0.75:
                # Climbing is now much more common to counter wall slides
                self.state = CatState.CLIMBING
                self.frame_index = 0
                self.direction = random.choice(["east", "west"])
            elif r < 0.78:
                self.state = CatState.ANGRY
                self.frame_index = 0
                self.direction = "south"
            elif r < 0.80:
                # Wall adventure (slide down) — rare
                self._start_sequence("wall_adventure")
            elif r < 0.85:
                # Ledge adventure ends with climbing up — more common
                self._start_sequence("ledge_adventure")
            elif r < 0.88:
                self._start_sequence("dash_crash")
            elif r < 0.91:
                self._start_sequence("full_jump")
            # drama_queen no longer random — only triggered by wall crash or angry attack
        elif self.state == CatState.SLEEPING_BALL:
            self.idle_ticks += 1
            if self.idle_ticks > random.randint(5, 15):
                self.state = CatState.WAKING_UP
                self.frame_index = 0
                self.idle_ticks = 0


    def _close_chat_fully(self):
        """Close the chat bubble AND the floating input entry."""
        self.chat_visible = False
        self.chat_response = ""
        if self._app and getattr(self._app, "_chat_box", None):
            self._app._chat_box.set_visible(False)
            if self._app._chat_entry:
                self._app._chat_entry.set_text("")
        if self._app:
            self._app._active_chat_cat = None

    def send_chat(self, text: str) -> None:
        if not self.chat_backend:
            # Background init still running — show a friendly hint and drop the text
            self.chat_response = "..."
            self.chat_visible = True
            return
        if self.chat_backend.is_streaming:
            return
        # Local metrics: count this chat, no-op if user hasn't opted in
        _metrics.track("chat_sent", cat_id=self.config.get("char_id"))
        # Lorem ipsum easter egg: a very long input (>500 chars) is not a real
        # message, the user is probably pasting / testing. Short-circuit to
        # the reading animation BEFORE hitting the backend (saves an API call
        # and triggers the delight). Detection lives in send_chat rather than
        # _on_chat_entry_activate so the test socket's `type_chat` command
        # hits the same path.
        if len(text) > 500 and self._app and hasattr(self._app, "eg_lorem"):
            self._close_chat_fully()
            self._app.eg_lorem(self, text)
            return
        # Magic phrase: "Don't panic" triggers/stops apocalypse mode (HGttG 🚀)
        # Case-insensitive, ignoring trailing punctuation and whitespace
        normalized = re.sub(r"[\s\W]+$", "", text.strip().lower())
        if normalized == "don't panic":
            if self._app and hasattr(self._app, "toggle_apocalypse"):
                self._app.toggle_apocalypse(self)
                self._close_chat_fully()
            return
        if normalized == "easter egg":
            if self._app and hasattr(self._app, "show_easter_menu"):
                self._close_chat_fully()
                self._app.show_easter_menu()
            return
        # Magic phrases for individual easter eggs
        egg_key = MAGIC_EGG_PHRASES.get(normalized)
        if egg_key and self._app and hasattr(self._app, "_trigger_easter_egg"):
            self._close_chat_fully()
            self._app._trigger_easter_egg(egg_key)
            return
        self.chat_response = "..."
        self.chat_visible = True
        self.state = CatState.EATING
        self.frame_index = 0

        first_token = True
        # Animated spinner for status messages (e.g. token refresh)
        status_state = {"timer": None}
        def stop_status_spinner():
            if status_state["timer"]:
                try:
                    GLib.source_remove(status_state["timer"])
                except Exception:
                    pass
                status_state["timer"] = None

        def on_token(token):
            nonlocal first_token
            # Real tokens arrived → stop any status spinner
            stop_status_spinner()
            if first_token:
                self.chat_response = token
                first_token = False
            else:
                self.chat_response += token
            return False

        def on_done():
            stop_status_spinner()
            self.state = CatState.IDLE
            self.frame_index = 0
            self.idle_ticks = 0
            if self.chat_backend:
                save_memory(self.config["id"], self.chat_backend.messages)
            # Personality drift — bump the message counter and, if the
            # cat has hit its DRIFT_EVERY_MESSAGES threshold, kick off
            # a background reflection call on the recent history.
            # Gated by the app-level _personality_drift_enabled flag so
            # users can opt out via `"personality_drift": false` in
            # config.json. The actual LLM call is non-blocking and
            # idempotent per cat_id (no concurrent drifts).
            app = self._app
            if (getattr(self, "personality", None) is not None
                    and getattr(app, "_personality_drift_enabled", True)):
                self.personality.on_message_added()
                self.personality.save()
                if self.personality.should_drift():
                    char_id = self.config.get("char_id", "cat01")
                    p = CATSET_PERSONALITIES.get(
                        char_id, CATSET_PERSONALITIES["cat01"])
                    base_traits = (p.get("traits") or {}).get(
                        L10n.lang, p["traits"]["fr"])
                    _personality.drift_async(
                        state=self.personality,
                        cat_name=self.config.get("name", "Cat"),
                        base_traits=base_traits,
                        lang=L10n.lang,
                        create_chat_fn=create_chat,
                        model=app.selected_model,
                    )
                # Long-term memory extraction (#5) — fire less often
                # than drift, every EXTRACT_EVERY_MESSAGES messages.
                # Reuses the personality message_count so we don't add
                # yet another counter on the cat instance.
                if (getattr(app, "_long_term_memory_enabled", True)
                        and self.personality.message_count > 0
                        and self.personality.message_count % _memory.EXTRACT_EVERY_MESSAGES == 0):
                    try:
                        recent = self.chat_backend.messages[-12:]
                        _memory.extract_facts_async(
                            cat_id=self.config["id"],
                            cat_name=self.config.get("name", "Cat"),
                            recent_messages=recent,
                            lang=L10n.lang,
                            create_chat_fn=create_chat,
                            model=app.selected_model,
                        )
                    except Exception:
                        log.exception("memory extract dispatch failed")
            # TTS voice output — hybrid pipeline splits the response into
            # text + cat-sound chunks and plays them through GStreamer.
            # Gated by BOTH the app-level _tts_enabled flag and the
            # per-cat self.tts_enabled toggle (the speaker icon in the
            # chat bubble). Both default to off so the feature is pure
            # opt-in — no risk of a silent install suddenly starting to
            # make noises at the user. Per-cat voice profile (speaker_id
            # + length_scale) comes from CATSET_PERSONALITIES so each
            # character has a distinct voice on a single Piper model.
            if (getattr(app, "_tts_enabled", False)
                    and getattr(self, "tts_enabled", False)
                    and self.chat_response):
                try:
                    chunks = _tts.split_cat_sounds(self.chat_response)
                    cat_sounds_on = getattr(app, "_tts_cat_sounds_enabled", True)
                    log.warning(
                        "TTS: on_done cat=%s resp=%r → %d chunks, cat_sfx=%s",
                        self.config.get("char_id"),
                        self.chat_response[:60], len(chunks), cat_sounds_on,
                    )
                    # Optional filter: drop cat-sound chunks when the
                    # user prefers text-only TTS. Keeps per-cat voice
                    # intact, just removes the audio interjections.
                    if not cat_sounds_on:
                        chunks = [c for c in chunks if c.kind != "cat"]
                    for i, c in enumerate(chunks):
                        log.warning("TTS: chunk[%d] kind=%s content=%r",
                                    i, c.kind, c.content[:40])
                    char_id = self.config.get("char_id", "cat01")
                    p = CATSET_PERSONALITIES.get(
                        char_id, CATSET_PERSONALITIES["cat01"])
                    voice_params = p.get("tts_voice")
                    _tts.get_default_player().play(chunks, voice_params)
                except Exception:
                    log.exception("TTS playback failed")
            return False

        def on_error(msg):
            stop_status_spinner()
            self.chat_response = msg
            self.state = CatState.IDLE
            self.frame_index = 0
            return False

        def on_status(kind):
            # Animated braille spinner in the chat bubble until tokens start streaming
            spinner_frames = ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834", "\u2826", "\u2827", "\u2807", "\u280f"]
            idx = {"i": 0}
            if kind == "refreshing":
                label = L10n.s("refreshing_auth")
            else:
                label = kind
            def tick():
                frame = spinner_frames[idx["i"] % len(spinner_frames)]
                idx["i"] += 1
                self.chat_response = f"{frame} {label}"
                return True
            tick()  # show immediately
            stop_status_spinner()
            status_state["timer"] = GLib.timeout_add(120, tick)
            return False

        # Long-term memory injection (#5): rebuild the system message
        # with retrieved facts that overlap with the user's input. This
        # gives the cat the impression of "remembering" past conversations
        # without needing embeddings — pure keyword matching against the
        # sqlite memory.db.
        try:
            if self.chat_backend.messages:
                base = self.chat_backend.messages[0].get("content", "")
                # Strip any previous memory injection so we don't accumulate
                if "\n\nCe dont tu te souviens" in base:
                    base = base.split("\n\nCe dont tu te souviens")[0]
                elif "\n\nThings you remember" in base:
                    base = base.split("\n\nThings you remember")[0]
                elif "\n\nLo que recuerdas" in base:
                    base = base.split("\n\nLo que recuerdas")[0]
                augmented = _memory.append_memories_to_prompt(
                    base, self.config["id"], text, L10n.lang)
                self.chat_backend.messages[0]["content"] = augmented
        except Exception:
            log.debug("memory injection failed", exc_info=True)
        self.chat_backend.send(text, on_token, on_done, on_error, on_status=on_status)
        # Mood: chatting with the user bumps happiness and clears boredom.
        try:
            self.mood.on_chat_sent()
        except Exception:
            pass

    def update_system_prompt(self, lang):
        p = _catset_prompt(self.config["char_id"], self.config["name"], lang)
        if getattr(self, "personality", None) is not None:
            p = self.personality.append_to_prompt(p, lang)
        if self.chat_backend and self.chat_backend.messages:
            self.chat_backend.messages[0] = {"role": "system", "content": p}

    # States that have native east/west frames in catset metadata
    _EW_STATES = frozenset({
        CatState.SURPRISED, CatState.CHASING_MOUSE, CatState.CLIMBING,
        CatState.DASHING, CatState.WALLCLIMB, CatState.WALLGRAB,
        CatState.LEDGEGRAB, CatState.LEDGEIDLE, CatState.LEDGECLIMB_STRUGGLE,
    })

    def _face_toward(self, other, state):
        """Orient this cat so it appears to face `other` while in `state`.
        South-only states (love, angry, flat, etc.) use direction='south' + flip,
        while east/west-capable states use their native direction."""
        should_face_east = self.x < other.x
        if state in self._EW_STATES:
            self.direction = "east" if should_face_east else "west"
            self._flip_h = False
        else:
            # South frames are east-facing originals → flip for west
            self.direction = "south"
            self._flip_h = not should_face_east

    def _roll_mood_adjusted(self) -> float:
        """Return a random [0, 1) biased by the cat's current mood. Used in
        the IDLE branch of behavior_tick instead of a raw random.random()
        so the emergent behavior reflects the invisible stats.

        Biases are layered: a single mood can only win (most extreme first).
        The effect is visible but not overwhelming — ~50% of rolls are
        still uniform when the cat is in the default 'neutral' zone.
        """
        r = random.random()
        m = self.mood
        if m.wants_rest():
            # Halve the range so low-r branches (SLEEPING_BALL at 0.05,
            # WALKING at 0.22) become much more likely, biasing toward rest.
            return r * 0.5
        if m.is_bored():
            # Map [0,1) → [0.28, 0.78) so CHASING_MOUSE (0.35), JUMPING
            # (0.65), CLIMBING (0.75) dominate.
            return 0.28 + r * 0.50
        if m.is_grumpy():
            # Narrow band around ANGRY (0.75–0.78).
            return 0.70 + r * 0.10
        if m.is_affectionate():
            # Narrow band around LOVE (0.45–0.50).
            return 0.35 + r * 0.20
        return r

    def _show_random_meow(self):
        if self.chat_visible:
            return
        if self.is_kitten:
            self.meow_text = random.choice(BABY_MEOWS)
        else:
            self.meow_text = L10n.random_meow()
        self.meow_visible = True
        if self._meow_timer_id:
            GLib.source_remove(self._meow_timer_id)
        self._meow_timer_id = GLib.timeout_add(random.randint(2000, 3000), self._hide_meow)

    def _hide_meow(self):
        self.meow_visible = False
        self._meow_timer_id = None
        return False

    def apply_scale(self, new_w: int, new_h: int, meta: dict[str, Any] | None = None, cat_dir: str | None = None) -> None:
        m = meta or self._meta
        d = cat_dir or self._cat_dir
        self.display_w = new_w
        self.display_h = new_h
        self.load_assets(m, d, lazy=False)
        # Recompute anim offsets at new scale
        sprite_w = m["character"]["size"]["width"]
        sprite_h = m["character"]["size"]["height"]
        raw_offsets = _compute_anim_offsets(m, d)
        scale_x = new_w / sprite_w
        scale_y = new_h / sprite_h
        self._anim_offsets = {}
        for anim_key, dirs in raw_offsets.items():
            self._anim_offsets[anim_key] = {}
            for direction, (y_off, x_off) in dirs.items():
                self._anim_offsets[anim_key][direction] = (round(y_off * scale_y), round(x_off * scale_x))
        # Re-compute sprite bottom padding at new scale
        try:
            south_rel = m["frames"]["rotations"].get("south", "")
            ref_img = Image.open(os.path.join(d, south_rel)).convert("RGBA")
            ref_floor = _sprite_floor_y(ref_img)
            self._sprite_bottom_padding = round((sprite_h - 1 - ref_floor) * scale_y)
        except Exception:
            self._sprite_bottom_padding = 0

    def cleanup(self) -> None:
        if self.chat_backend:
            self.chat_backend.cancel()
        if self._meow_timer_id:
            GLib.source_remove(self._meow_timer_id)
            self._meow_timer_id = None
        self.meow_visible = False
        self.in_encounter = False
        self.encounter_visible = False
        self._app = None
        self.chat_backend = None

    def hit_test(self, mx: float, my: float) -> bool:
        """Check if point (mx, my) is inside this cat's bounding rect."""
        return (self.x <= mx <= self.x + self.display_w and
                self.y <= my <= self.y + self.display_h)


# SettingsWindow — moved to catai_linux.settings_window
from catai_linux.settings_window import SettingsWindow  # noqa: E402



# CatEncounter, LoveEncounter — moved to catai_linux.encounters
from catai_linux.encounters import CatEncounter, LoveEncounter  # noqa: E402


# ── Main Application ───────────────────────────────────────────────────────────

class CatAIApp(EasterEggMixin, Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=None,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.cat_instances: list[CatInstance] = []
        self.cat_configs: list[dict[str, Any]] = []
        self.cat_scale: float = DEFAULT_SCALE
        self.screen_w: int = 1920
        self.screen_h: int = 1080
        self.selected_model: str = ""
        self.settings_ctrl: SettingsWindow | None = None
        self._menu_visible = False
        self._menu_x = 0
        self._menu_y = 0
        self._active_chat_cat: CatInstance | None = None
        self._menu_timer = None
        self._canvas_window = None
        self._canvas_area = None
        self._canvas_xid = None
        self._canvas_y_offset = 0  # GNOME top bar height (detected at launch)
        self._apocalypse_active = False
        self._apocalypse_timer = None
        self._apocalypse_queue: list = []
        self._apocalypse_spawning = False
        # Voice chat state (push-to-talk microphone)
        self._voice_enabled = False
        self._voice_model = "base"
        self._voice_recorder = None
        self._voice_btn = None
        # Wake word — each cat answers to its own first name. Off by
        # default; flipped on via the Voice section in Settings. The
        # listener instance is lazy: only built once Vosk + a model
        # are available, so users without the optional dep pay zero
        # cost.
        self._wake_word_enabled = False
        self._wake_ack_sound = True
        self._wake: WakeWordListener | None = None
        self._wake_ptt_stop_timer = None
        # Easter egg menu state
        self._easter_menu_visible = False
        self._easter_menu_x = 0
        self._easter_menu_y = 0
        # Dimensions computed dynamically in show_easter_menu
        self._easter_menu_w = 560
        self._easter_menu_h = 380
        self._easter_menu_items = []  # list of ((x, y, w, h), key)
        # Matrix effect state
        self._matrix_columns = []
        self._matrix_ticks = 0
        self._shake_amount = 0  # pixels — used by eg_shake
        # Seasonal overlay state — date-aware particles (snow, pumpkins,
        # hearts, fireworks, …). `_season_override` wins over the resolver
        # when set via the `season` socket cmd or CATAI_SEASON env var.
        self._seasonal_enabled = True
        self._season_override: str | None = None
        self._timers = []
        # Drag state for canvas
        self._drag_cat = None
        # Encounter state
        self._active_encounter = None
        self.encounters_enabled = True
        self._last_encounter_check = 0.0
        # Perf caches (reused across render frames)
        self._bubble_layout_cache = None
        self._bubble_layout_cached_text = None
        self._regions_dirty = True
        self._regions_cache_key = None
        self._cairo_region_cache = None
        # Reaction pool — AI-generated short replies per cat per event,
        # lazy-filled on first trigger, cached in memory for the session.
        self._reaction_pool = ReactionPool(
            create_chat_fn=create_chat,
            get_model_fn=lambda: self.selected_model,
        )
        # Caps lock detection (edge-triggered, with cooldown)
        self._caps_lock_prev = False
        self._caps_lock_last_trigger = 0.0
        self._rm_rf_active_app = False
        # Fullscreen detection (edge-triggered, with cooldown)
        self._fullscreen_prev = False
        self._fullscreen_last_trigger = 0.0
        self._fullscreen_applause_active = False
        # Petting state
        self._petting_timer_id = None
        # Mood system save interval (60 s periodic + on shutdown)
        self._mood_save_timer_id = None
        # Activity monitor — polled from behavior_tick, drives AFK sleep
        self._activity = ActivityMonitor()
        self._afk_sleep_active = False  # cats currently mass-sleeping from AFK
        # Quake-style drop-down console (² key)
        self._quake_revealer = None
        self._quake_output = None   # Gtk.TextView
        self._quake_entry = None    # Gtk.Entry
        self._quake_history: list[str] = []
        self._quake_history_idx = -1

    def do_activate(self):
        _setup_logging()
        apply_css()
        # Sync bubble/menu palette with desktop dark-mode preference before
        # anything renders. Poller below keeps it live-updated.
        self._dark_mode = _is_dark_mode()
        _set_theme(dark=self._dark_mode)

        # Discover external character packs (#9 Tier 1) and merge them
        # into CATSET_PERSONALITIES so they show up in the catset picker
        # exactly like the bundled cats. Each pack lives in
        # ~/.local/share/catai/characters/<char_id>/ and is auto-loaded
        # at startup. Invalid packs are silently skipped.
        try:
            external = _character_packs.discover_packs()
            if external:
                CATSET_PERSONALITIES.update(external)
                log.info("Loaded %d external character pack(s)", len(external))
        except Exception:
            log.exception("character pack discovery crashed")

        display = Gdk.Display.get_default()
        monitors = display.get_monitors()
        # Collect per-monitor rects for multi-monitor awareness (dead-zone
        # rescue, spawn distribution, socket inspection). The bounding-box
        # screen_w/screen_h stay the same as before so nothing else has
        # to change.
        self._monitor_rects: list[tuple[int, int, int, int]] = []
        if monitors.get_n_items() > 0:
            max_w, max_h = 0, 0
            for i in range(monitors.get_n_items()):
                geo = monitors.get_item(i).get_geometry()
                self._monitor_rects.append((geo.x, geo.y, geo.width, geo.height))
                max_w = max(max_w, geo.x + geo.width)
                max_h = max(max_h, geo.y + geo.height)
            self.screen_w = max_w
            self.screen_h = max_h
        log.info("Monitors detected: %d %r", len(self._monitor_rects), self._monitor_rects)

        cfg = validate_config(load_config())
        self.cat_scale = cfg.get("scale", DEFAULT_SCALE)
        self.selected_model = cfg.get("model", "gemma3:1b")
        L10n.lang = cfg.get("lang", "fr")
        self.encounters_enabled = cfg.get("encounters", True)
        # Seasonal overlay — enabled by default, user can opt out via
        # `"seasonal": false` in config.json. The resolver does its own
        # CATAI_SEASON env-var override, independent of this flag.
        #
        # The overlay is meant to *announce* the current season at startup
        # then get out of the way, so it auto-dismisses after
        # `seasonal_duration_sec` (default 30 s). Set the duration to 0
        # in config.json to keep the overlay on permanently (the classic
        # desktop-pet behavior).
        # Additionally, the announce only fires on the FIRST launch of
        # the day — relaunching CATAI several times in the same day
        # would otherwise replay the falling petals/snow over and over,
        # which gets old fast. We persist the last-shown date in
        # ~/.config/catai/seasonal_last_shown and skip the overlay if
        # today's date matches.
        self._seasonal_enabled = cfg.get("seasonal", True)
        self._seasonal_duration_sec = int(cfg.get("seasonal_duration_sec", 30))
        if self._seasonal_enabled and self._seasonal_duration_sec > 0:
            try:
                import datetime
                today_iso = datetime.date.today().isoformat()
                stamp_path = os.path.join(CONFIG_DIR, "seasonal_last_shown")
                last = None
                if os.path.isfile(stamp_path):
                    with open(stamp_path) as f:
                        last = f.read().strip()
                if last == today_iso:
                    log.info("Seasonal overlay already shown today, skipping")
                    self._seasonal_enabled = False
                else:
                    os.makedirs(CONFIG_DIR, exist_ok=True)
                    with open(stamp_path, "w") as f:
                        f.write(today_iso)
            except OSError:
                log.debug("seasonal stamp read/write failed", exc_info=True)
        self.cat_configs = cfg.get("cats", [])
        # Personality drift — on by default. Users can opt out via
        # config.json key `"personality_drift": false`. The drift engine
        # respects this flag in the chat on_done hook.
        self._personality_drift_enabled = cfg.get("personality_drift", True)
        # TTS voice output — OFF by default. Opt-in via config.json key
        # `"tts_enabled": true` OR via the per-cat speaker icon in the
        # chat bubble. The per-cat toggle is the primary UI; the app
        # flag is a global kill switch.
        self._tts_enabled = cfg.get("tts_enabled", False)
        # Cat sound effects in TTS output. When True (default) the
        # splitter's cat chunks are played via the CC0 WAV samples.
        # When False only Piper text chunks play — some users find the
        # text-only flow more intelligible since the cat samples add
        # latency between phrases.
        self._tts_cat_sounds_enabled = cfg.get("tts_cat_sounds_enabled", True)
        # Auto-update mode (issue #24). Default 'auto' so once installed
        # CATAI keeps itself current without manual intervention. Set to
        # 'notify' to just show a meow bubble when an update lands, or
        # 'off' to skip the GitHub check entirely.
        self._auto_update_mode = cfg.get("auto_update", _updater.MODE_AUTO)
        if self._auto_update_mode not in _updater.ALL_MODES:
            self._auto_update_mode = _updater.MODE_AUTO
        # Local metrics (#9). Off by default — privacy-first, opt-in
        # via the settings checkbox. When enabled, the metrics module
        # tracks chats sent, eggs triggered, love encounters, kittens
        # born, and pet sessions in ~/.config/catai/stats.json. Never
        # transmitted anywhere; pure self-curiosity feature.
        self._metrics_enabled = bool(cfg.get("metrics_enabled", False))
        _metrics.set_enabled(self._metrics_enabled)
        # Public scriptable API socket (#9). Off by default for security.
        # When enabled, opens a Unix socket at $XDG_RUNTIME_DIR/catai.sock
        # with mode 0600 (same-user-only) exposing a small curated set
        # of commands so shell scripts can make cats react to external
        # events without importing Python or running --test-socket.
        self._api_enabled = bool(cfg.get("api_enabled", False))
        # Long-term memory (#5). On by default — periodic LLM extraction
        # of memorable facts from chats, sqlite-backed in
        # ~/.config/catai/memory.db. Retrieved on each new message via
        # keyword overlap and injected into the system prompt as
        # "things you remember about this user". Opt out with
        # "long_term_memory": false in config.json.
        self._long_term_memory_enabled = bool(cfg.get("long_term_memory", True))

        # Voice chat: enabled from --voice CLI flag OR config.json
        cli_voice = "--voice" in sys.argv
        cfg_voice = cfg.get("voice_enabled", False)
        wanted = cli_voice or cfg_voice
        self._voice_enabled = wanted and VOICE_AVAILABLE
        self._voice_model = cfg.get("voice_model", "base")
        if self._voice_enabled:
            self._voice_recorder = VoiceRecorder(model_name=self._voice_model)
            log.warning("VOICE: enabled (push-to-talk) with model %r", self._voice_model)
            # Preload the Whisper model in a background thread — but only if
            # it's already cached locally, so we don't trigger a silent
            # background download on first launch.
            if _whisper_model_cached(self._voice_model):
                log.warning("VOICE: model %r cached, preloading in background...", self._voice_model)
                def _preload():
                    try:
                        self._voice_recorder._ensure_model()
                    except Exception:
                        log.exception("VOICE: preload failed")
                threading.Thread(target=_preload, daemon=True).start()
            else:
                log.warning("VOICE: model %r not cached, will download on first use", self._voice_model)
        elif wanted and not VOICE_AVAILABLE:
            log.warning("Voice chat requested but faster-whisper not installed. "
                        "Run: pip install catai-linux[voice]")

        # Wake word — opt-in (issue #4 Tier 2). Each cat responds to
        # its own renameable first name via Vosk. Off by default; the
        # user enables it from Settings → Voice. The listener is only
        # built when the optional `vosk` dep is installed; otherwise
        # we silently no-op so the rest of the app keeps working.
        self._wake_word_enabled = bool(cfg.get("wake_word_enabled", False))
        self._wake_ack_sound = bool(cfg.get("wake_word_ack_sound", True))
        if self._wake_word_enabled:
            if WAKE_AVAILABLE:
                self._wake = WakeWordListener(on_wake=self._on_wake_word_heard)
                # set_names is deferred until after self.cat_configs is
                # finalized below — see the call right after instance
                # creation.
            else:
                log.warning("Wake word requested but vosk not installed. "
                            "Run: pip install catai-linux[voice]")

        # Migrate: drop any legacy color_id-only configs (replaced by catset chars)
        catset_cfgs = [c for c in self.cat_configs if c.get("char_id")]
        if not catset_cfgs:
            p = CATSET_PERSONALITIES["cat_orange"]
            self.cat_configs = [{
                "id": f"cat_{uuid.uuid4().hex[:8]}",
                "char_id": "cat_orange",
                "name": p["name"].get(L10n.lang, p["name"]["fr"]),
            }]
            self._save_all()
        elif len(catset_cfgs) < len(self.cat_configs):
            # Some legacy cats removed — save cleaned config
            self.cat_configs = catset_cfgs
            self._save_all()

        # Create the single fullscreen transparent canvas window
        self._create_canvas()

        # Pre-create context menu and settings (hidden) so NOTIFICATION type
        # is applied before user ever sees them (avoids GNOME "is ready" alert)
        # Settings window created on first use (not pre-opened to avoid GNOME notification)

        # Plan initial spawn coordinates across monitors (round-robin) so
        # a dual-screen user doesn't get every cat stacked on monitor 0.
        # Falls back to an empty list → _create_instance uses its own
        # random-on-monitor-0 logic.
        if self._monitor_rects and self.cat_configs:
            self._spawn_plan = _monitors_mod.distribute_spawns(
                len(self.cat_configs), self._monitor_rects, padding=80)
        else:
            self._spawn_plan = []

        # Create cat instances (no windows, just state)
        for i, cat_cfg in enumerate(self.cat_configs):
            self._create_instance(cat_cfg, i)

        # Register wake-word names now that the catset is finalized.
        # The download (~41 MB) and model load happen in a daemon
        # thread inside start(), so this never blocks the GTK init.
        if self._wake is not None:
            self._wake.set_names({
                c.get("char_id"): c.get("name", "")
                for c in self.cat_configs if c.get("char_id")
            })
            self._wake.start()

        # Actions
        for name, cb in [("quit", lambda *a: self.quit()), ("settings", lambda *a: self._open_settings())]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)

        # Disable automatic GC to prevent random pauses
        gc.disable()

        self._timers = [
            GLib.timeout_add(RENDER_MS, self._render_tick),
            GLib.timeout_add(BEHAVIOR_MS, self._behavior_tick),
            GLib.timeout_add(10000, _apply_above_all),
            GLib.timeout_add(30000, self._gc_collect),
            # Caps lock poller — fires every 500 ms, triggers eg_capslock
            # on the False → True transition. Cheap (reads a GDK device
            # flag, no I/O).
            GLib.timeout_add(500, self._check_caps_lock),
            # Fullscreen poller — fires every 1500 ms, triggers eg_fullscreen
            # on the False → True transition of the active window's
            # _NET_WM_STATE_FULLSCREEN atom. Direct Xlib query (~50 µs)
            # since v0.7.3 — used to be 2 xprop subprocesses.
            GLib.timeout_add(1500, self._check_fullscreen),
            # Mood save — persist all cats' mood state to disk every 60s
            # (plus on shutdown and on key mood events).
            GLib.timeout_add(60000, self._save_all_moods),
            # Metrics cache flush — batch writes to stats.json every 30 s
            # instead of on every single track() call (saves 2-3 disk
            # writes per second during active chat).
            GLib.timeout_add(30000, self._flush_metrics),
            # Theme poller — every 30 s, re-read the GNOME dark/light
            # preference and flip the bubble palette if it changed.
            # Cheap (one gsettings subprocess), so polling beats a live
            # D-Bus subscription for our needs.
            GLib.timeout_add(30000, self._check_theme),
        ]

        # Schedule the seasonal overlay auto-dismiss. Fires once after
        # `seasonal_duration_sec` seconds, then the particles fade out
        # permanently unless the user re-enables via the `season` socket
        # command. Duration=0 keeps the overlay on forever.
        if self._seasonal_enabled and self._seasonal_duration_sec > 0:
            GLib.timeout_add(self._seasonal_duration_sec * 1000,
                             self._seasonal_auto_dismiss)

        # Auto-update background check (issue #24). Spawn a daemon
        # thread that checks GitHub for a new release ~5 s after
        # startup so it doesn't compete with sprite loading. The
        # thread either silently installs (auto mode) and shows a
        # meow bubble, or just shows the bubble (notify mode).
        if self._auto_update_mode != _updater.MODE_OFF:
            threading.Thread(
                target=self._auto_update_worker, daemon=True).start()

        # Test socket for E2E tests (--test-socket flag)
        if "--test-socket" in sys.argv:
            self._start_test_socket()

        # Public scriptable API socket (#9, opt-in via config)
        if self._api_enabled:
            try:
                self._start_api_socket()
            except Exception:
                log.exception("API socket failed to start")

    # ── Public scriptable API socket (#9) ────────────────────────────────

    def _api_socket_path(self) -> str:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        return os.path.join(runtime, "catai.sock")

    def _start_api_socket(self) -> None:
        """Open a Unix socket for the public scriptable API. Mode 0600
        means same-user-only — anyone else on the box can't connect.
        Curated dispatch (vs the test socket which exposes everything)."""
        import socket as sock_mod
        path = self._api_socket_path()
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        self._api_sock = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
        self._api_sock.setblocking(False)
        self._api_sock.bind(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            log.debug("api socket: chmod 0600 failed", exc_info=True)
        self._api_sock.listen(4)
        GLib.io_add_watch(
            self._api_sock.fileno(),
            GLib.IOCondition.IN,
            self._on_api_connection,
        )
        log.info("API socket listening on %s", path)

    def _on_api_connection(self, fd, condition):
        try:
            conn, _ = self._api_sock.accept()
        except Exception:
            log.exception("api socket accept failed")
            return True
        try:
            conn.setblocking(True)
            conn.settimeout(5.0)  # prevent DoS from a client that connects but never sends
            data = conn.recv(4096).decode().strip()
            response = self._handle_api_cmd(data)
            try:
                conn.sendall((response + "\n").encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
        except Exception:
            log.exception("api socket handler error")
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True

    def _handle_api_cmd(self, raw: str) -> str:
        """Public API dispatch — only the curated commands. Unlike
        _handle_test_cmd, this never exposes internal state or test
        helpers. Same response shape: 'OK ...' or 'ERR: ...'."""
        parts = raw.split()
        if not parts:
            return "ERR: empty command"
        cmd = parts[0]
        try:
            if cmd == "status":
                return (f"OK cats={len(self.cat_instances)} "
                        f"version={_updater.get_installed_version() or 'dev'}")
            if cmd == "list_cats":
                items = [
                    f"{i}:{c.config.get('char_id', '?')}:{c.config.get('name', '?')}"
                    for i, c in enumerate(self.cat_instances)
                ]
                return "OK " + " | ".join(items)
            if cmd == "list_eggs":
                keys = [k for k, _, _, _ in EASTER_EGGS]
                return "OK " + " ".join(keys)
            if cmd == "meow":
                if len(parts) < 2:
                    return "ERR: usage: meow <idx> [text]"
                try:
                    idx = int(parts[1])
                except ValueError:
                    return "ERR: cat idx must be int"
                if not (0 <= idx < len(self.cat_instances)):
                    return "ERR: cat idx out of range"
                cat = self.cat_instances[idx]
                text = " ".join(parts[2:]) if len(parts) > 2 else None
                if text:
                    cat.meow_text = text
                    cat.meow_visible = True
                    if getattr(cat, "_meow_timer_id", None):
                        try:
                            GLib.source_remove(cat._meow_timer_id)
                        except Exception:
                            pass
                    def _hide():
                        cat.meow_visible = False
                        cat._meow_timer_id = None
                        return False
                    cat._meow_timer_id = GLib.timeout_add(8000, _hide)
                else:
                    cat._show_random_meow()
                return f"OK meow on {idx}"
            if cmd == "egg":
                if len(parts) < 2:
                    return "ERR: usage: egg <key>"
                key = parts[1]
                valid = {k for k, _, _, _ in EASTER_EGGS}
                if key not in valid:
                    return f"ERR: unknown egg '{key}'"
                GLib.timeout_add(10, lambda k=key: self._trigger_easter_egg(k))
                return f"OK trigger {key}"
            if cmd == "notify":
                # Forward to the existing notification reaction infra
                app_name = parts[1] if len(parts) > 1 else ""
                summary = " ".join(parts[2:]) if len(parts) > 2 else ""
                if hasattr(self, "eg_notification"):
                    GLib.timeout_add(10,
                        lambda: self.eg_notification(app_name, summary) or False)
                return "OK notify queued"
            if cmd == "cats":
                items = []
                for i, c in enumerate(self.cat_instances):
                    items.append({
                        "name": c.config.get("name", "?"),
                        "char_id": c.config.get("char_id", "?"),
                        "x": int(c.x), "y": int(c.y),
                        "state": c.state.value if hasattr(c.state, "value") else str(c.state),
                        "index": i,
                    })
                return "OK " + json.dumps(items)
            if cmd == "force_state":
                if len(parts) < 3:
                    return "ERR: usage: force_state <idx> <state_name>"
                try:
                    idx = int(parts[1])
                except ValueError:
                    return "ERR: cat idx must be int"
                if not (0 <= idx < len(self.cat_instances)):
                    return "ERR: cat idx out of range"
                cat = self.cat_instances[idx]
                state_name = parts[2]
                try:
                    cat.state = CatState(state_name)
                except ValueError:
                    return f"ERR: unknown state {state_name}"
                cat.frame_index = 0
                cat.idle_ticks = 0
                cat._sequence = None
                cat._sequence_index = 0
                cat._sequence_pause_ticks = 0
                return f"OK cat {idx} -> {state_name}"
            if cmd == "say":
                if len(parts) < 3:
                    return "ERR: usage: say <idx> <text>"
                try:
                    idx = int(parts[1])
                except ValueError:
                    return "ERR: cat idx must be int"
                if not (0 <= idx < len(self.cat_instances)):
                    return "ERR: cat idx out of range"
                cat = self.cat_instances[idx]
                text = " ".join(parts[2:])
                cat.send_chat(text)
                return f"OK sent: {text}"
            if cmd == "move":
                if len(parts) < 4:
                    return "ERR: usage: move <idx> <x> <y>"
                try:
                    idx = int(parts[1])
                except ValueError:
                    return "ERR: cat idx must be int"
                if not (0 <= idx < len(self.cat_instances)):
                    return "ERR: cat idx out of range"
                cat = self.cat_instances[idx]
                try:
                    cat.dest_x, cat.dest_y = int(parts[2]), int(parts[3])
                except ValueError:
                    return "ERR: invalid coordinates"
                cat.state = CatState.WALKING
                cat.frame_index = 0
                return f"OK cat {idx} walking to {parts[2]},{parts[3]}"
            if cmd == "mood":
                if len(parts) < 2:
                    return "ERR: usage: mood <idx>"
                try:
                    idx = int(parts[1])
                except ValueError:
                    return "ERR: cat idx must be int"
                if not (0 <= idx < len(self.cat_instances)):
                    return "ERR: cat idx out of range"
                cat = self.cat_instances[idx]
                mood_data = {}
                if hasattr(cat, "mood"):
                    m = cat.mood
                    mood_data = {
                        "happiness": getattr(m, "happiness", None),
                        "energy": getattr(m, "energy", None),
                        "social": getattr(m, "social", None),
                    }
                return "OK " + json.dumps(mood_data)
            if cmd == "season":
                return self._cmd_season(parts)
            if cmd == "help":
                return ("OK commands: status list_cats list_eggs cats "
                        "meow egg notify force_state say move mood season help")
            return f"ERR: unknown command '{cmd}' (try 'help')"
        except Exception as e:
            log.exception("api cmd %r crashed", cmd)
            return f"ERR: {e}"

    # ── Test socket for E2E tests ─────────────────────────────────────────

    @staticmethod
    def _test_sock_path() -> str:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        return os.path.join(runtime, "catai_test.sock")

    def _start_test_socket(self):
        """Start a Unix socket for E2E test commands."""
        import socket as sock_mod
        path = self._test_sock_path()
        if os.path.exists(path):
            os.remove(path)
        self._test_sock = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
        self._test_sock.setblocking(False)
        self._test_sock.bind(path)
        os.chmod(path, 0o600)  # same-user-only like the API socket
        self._test_sock.listen(1)
        GLib.io_add_watch(self._test_sock.fileno(), GLib.IOCondition.IN, self._on_test_connection)
        log.debug("Test socket listening on %s", path)

    def _on_test_connection(self, fd, condition):
        try:
            conn, _ = self._test_sock.accept()
        except Exception:
            log.exception("test socket accept failed")
            return True
        try:
            conn.setblocking(True)
            conn.settimeout(5.0)
            data = conn.recv(4096).decode().strip()
            response = self._handle_test_cmd(data)
            try:
                conn.sendall((response + "\n").encode())
            except (BrokenPipeError, ConnectionResetError):
                # Client closed socket before reading (e.g. ncat --send-only)
                log.debug("test client closed before reading response")
        except Exception:
            log.exception("test socket handler error")
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True  # keep the watch alive no matter what

    # ── Test socket command handlers ─────────────────────────────────────────
    # Each _cmd_* receives the already-split `parts` list (action + args).
    # Dispatched via self._test_cmd_handlers (built lazily on first use).

    def _get_cat_at_idx(self, parts, idx_pos=1):
        """Parse an int cat index from parts[idx_pos]. Returns (cat, None) on
        success or (None, err_string) on failure."""
        try:
            idx = int(parts[idx_pos])
        except (IndexError, ValueError):
            return None, "ERR: missing or invalid cat index"
        if not (0 <= idx < len(self.cat_instances)):
            return None, "ERR: invalid cat index"
        return self.cat_instances[idx], None

    def _cmd_status(self, parts):
        return (f"OK cats={len(self.cat_instances)} canvas_xid={self._canvas_xid} "
                f"screen={self.screen_w}x{self.screen_h} y_offset={self._canvas_y_offset}")

    def _cmd_cat_positions(self, parts):
        positions = [f"{c.config.get('char_id', '?')}:{c.x:.0f},{c.y:.0f}"
                     for c in self.cat_instances]
        return "OK " + " ".join(positions)

    def _cmd_force_state(self, parts):
        if len(parts) < 3:
            return "ERR: usage: force_state <idx> <state_name>"
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        state_name = parts[2]
        try:
            cat.state = CatState(state_name)
        except ValueError:
            return f"ERR: unknown state {state_name}"
        cat.frame_index = 0
        cat.idle_ticks = 0
        cat._sequence = None
        cat._sequence_index = 0
        cat._sequence_pause_ticks = 0
        cat.direction = "east" if state_name in ("dashing", "surprised") else "south"
        return f"OK cat {parts[1]} -> {state_name}"

    def _cmd_apocalypse(self, parts):
        self.toggle_apocalypse()
        return f"OK apocalypse {'ON' if self._apocalypse_active else 'OFF'}"

    def _cmd_easter_menu(self, parts):
        self.show_easter_menu()
        return "OK easter menu shown"

    def _cmd_egg(self, parts):
        if len(parts) < 2:
            return f"ERR: usage: egg <key>  (available: {[k for k,_,_,_ in EASTER_EGGS]})"
        key = parts[1]
        if not any(k == key for k, _, _, _ in EASTER_EGGS):
            return f"ERR: unknown egg {key}"
        self._trigger_easter_egg(key)
        return f"OK egg {key}"

    def _cmd_love_encounter(self, parts):
        if len(parts) < 3:
            return "ERR: usage: love_encounter <idx_a> <idx_b> [love|surprised|angry]"
        try:
            ia, ib = int(parts[1]), int(parts[2])
        except ValueError:
            return "ERR: invalid indices"
        n = len(self.cat_instances)
        if not (0 <= ia < n and 0 <= ib < n and ia != ib):
            return "ERR: invalid indices"
        forced = None
        if len(parts) >= 4:
            if parts[3] not in ("love", "surprised", "angry"):
                return "ERR: outcome must be love, surprised, or angry"
            forced = parts[3]
        if self._active_encounter:
            self._active_encounter.cancel()
        enc = LoveEncounter(self.cat_instances[ia], self.cat_instances[ib], self,
                            forced_outcome=forced)
        self._active_encounter = enc
        enc.start()
        return f"OK love encounter {ia}<->{ib}" + (f" forced={forced}" if forced else "")

    def _cmd_start_sequence(self, parts):
        if len(parts) < 3:
            return "ERR: usage: start_sequence <idx> <seq_name>"
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        seq_name = parts[2]
        if seq_name not in SEQUENCES:
            return f"ERR: unknown sequence {seq_name} (available: {list(SEQUENCES.keys())})"
        cat._start_sequence(seq_name)
        return f"OK cat {parts[1]} -> sequence {seq_name}"

    def _cmd_meow(self, parts):
        if len(parts) < 2:
            return "ERR: usage: meow <idx> [text]"
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        cat.meow_text = " ".join(parts[2:]) if len(parts) > 2 else "Meow~"
        cat.meow_visible = True
        return f"OK meow on cat {parts[1]}"

    def _cmd_move_cat(self, parts):
        if len(parts) < 4:
            return "ERR: usage: move_cat <idx> <x> <y>"
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        try:
            cat.x, cat.y = int(parts[2]), int(parts[3])
        except ValueError:
            return "ERR: invalid coordinates"
        cat._clamp_to_screen()  # honours canvas y offset and margins
        return f"OK cat {parts[1]} at {cat.x},{cat.y}"

    def _cmd_fake_chat(self, parts):
        if len(parts) < 3:
            return "ERR: usage: fake_chat <idx> <text>"
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        cat.chat_response = " ".join(parts[2:])
        cat.chat_visible = True
        self._active_chat_cat = cat
        return f"OK fake chat on cat {parts[1]}"

    def _cmd_click_cat(self, parts):
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        self._toggle_chat_for(cat)
        return f"OK toggled chat for cat {parts[1] if len(parts) > 1 else 0}"

    def _cmd_right_click_cat(self, parts):
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        self._menu_visible = True
        self._menu_x = int(cat.x + cat.display_w)
        self._menu_y = int(cat.y)
        return "OK menu shown"

    def _cmd_click_menu_settings(self, parts):
        self._menu_visible = False
        self._open_settings()
        return "OK settings opened"

    def _cmd_click_menu_quit(self, parts):
        self._menu_visible = False
        GLib.timeout_add(100, lambda: self.quit() or False)
        return "OK quitting"

    def _cmd_type_chat(self, parts):
        text = " ".join(parts[1:]) if len(parts) > 1 else "coucou"
        cat = self._active_chat_cat
        if not cat:
            return "ERR: no active chat"
        cat.send_chat(text)
        return f"OK sent: {text}"

    def _cmd_close_chat(self, parts):
        cat = self._active_chat_cat
        if not cat:
            return "ERR: no active chat"
        cat.chat_visible = False
        self._chat_box.set_visible(False)
        self._active_chat_cat = None
        return "OK chat closed"

    def _cmd_drag_cat(self, parts):
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        try:
            dx = int(parts[2]) if len(parts) > 2 else 100
            dy = int(parts[3]) if len(parts) > 3 else 0
        except ValueError:
            return "ERR: invalid offset"
        cat.x += dx
        cat.y += dy
        return f"OK cat {parts[1]} moved to {cat.x:.0f},{cat.y:.0f}"

    def _cmd_close_settings(self, parts):
        if not (self.settings_ctrl and self.settings_ctrl.window):
            return "ERR: settings not open"
        self.settings_ctrl._on_close()
        return "OK settings closed"

    def _cmd_settings_state(self, parts):
        """Report whether the settings window exists and is currently visible.
        Used by the e2e tests instead of xdotool window class search, which
        is fragile across WM configs."""
        ctrl = self.settings_ctrl
        if not (ctrl and ctrl.window):
            return "OK settings=absent"
        visible = "yes" if ctrl.window.get_visible() else "no"
        return f"OK settings=present visible={visible}"

    def _cmd_egg_state(self, parts):
        """Probe internal state of easter-egg effects so tests can verify
        that triggering an egg actually mutated the right state flag."""
        nyan_active = bool(getattr(self, "_nyan_active", False))
        matrix_cols = len(getattr(self, "_matrix_columns", []) or [])
        apocalypse = bool(getattr(self, "_apocalypse_active", False))
        shake = float(getattr(self, "_shake_amount", 0) or 0)
        hidden_cats = sum(1 for c in self.cat_instances if getattr(c, "_hidden", False))
        boss_cats = sum(1 for c in self.cat_instances
                        if getattr(c, "_boss_scale", None) is not None)
        beam_cats = sum(1 for c in self.cat_instances
                        if getattr(c, "_beam_ticks", 0) > 0)
        rm_rf_active = bool(getattr(self, "_rm_rf_active_app", False))
        return (f"OK nyan={nyan_active} matrix_cols={matrix_cols} "
                f"apocalypse={apocalypse} shake={shake:.1f} "
                f"hidden={hidden_cats} boss={boss_cats} beam={beam_cats} "
                f"rm_rf={rm_rf_active}")

    def _on_petting_start(self, cat) -> None:
        """Mood hook — called by _try_enter_petting when a petting session
        begins. Delegates to the cat's mood instance."""
        try:
            cat.mood.on_petting_start()
        except Exception:
            log.exception("mood.on_petting_start failed")

    def _on_petting_end(self, cat) -> None:
        try:
            cat.mood.on_petting_end()
            # Persist immediately so a happy moment isn't lost to a crash
            cat.mood.save(cat.config["id"])
        except Exception:
            log.exception("mood.on_petting_end failed")

    def _save_all_moods(self) -> bool:
        """Persist all cats' mood state. Called by the 60 s save timer and
        by do_shutdown. Returns True so the GLib timeout keeps firing."""
        for cat in self.cat_instances:
            try:
                cat.mood.save(cat.config["id"])
            except Exception:
                log.debug("mood save failed for %s", cat.config.get("id"), exc_info=True)
        return True

    @staticmethod
    def _flush_metrics() -> bool:
        """Batch-write the in-memory metrics cache to disk. Called by a
        30 s GLib timer — much cheaper than the old per-track() save.
        Returns True so the timer keeps firing."""
        try:
            _metrics.flush()
        except Exception:
            log.debug("metrics flush failed", exc_info=True)
        return True

    def _cmd_notify(self, parts):
        """E2E + manual trigger for the notification reaction.
        Usage: notify [<app_name> [<summary>]]"""
        app_name = parts[1] if len(parts) > 1 else ""
        summary = " ".join(parts[2:]) if len(parts) > 2 else ""
        self.eg_notification(app_name=app_name, summary=summary)
        return f"OK notify app={app_name!r} summary={summary[:40]!r}"

    def _cmd_activity_state(self, parts):
        """Return the activity monitor snapshot + afk_sleep flag."""
        snap = self._activity.snapshot()
        return (f"OK idle_ms={snap['idle_ms']} is_afk={snap['is_afk']} "
                f"cpu_load={snap['cpu_load']} hour={snap['hour']} "
                f"is_night={snap['is_night']} afk_sleep={self._afk_sleep_active}")

    def _cmd_force_afk(self, parts):
        """E2E hook: force the AFK-sleep transition without waiting for
        10 minutes of idle time. Usage: force_afk on | off

        Pins the state on the ActivityMonitor so the very next
        behavior_tick (which calls update()) doesn't immediately reset
        it via hysteresis. CI runners report idle_ms=0 under xvfb, so
        without the pin force_afk on would flip back to off within
        ~200-400 ms on a slow runner, flaking the assertion."""
        if len(parts) < 2 or parts[1] not in ("on", "off"):
            return "ERR: usage: force_afk on|off"
        state = parts[1] == "on"
        self._activity._pinned_afk = state
        self._activity.is_afk = state
        # Kick the activity application immediately
        self._apply_activity_signals()
        return f"OK afk={parts[1]}"

    def _cmd_mood_state(self, parts):
        """Return the mood snapshot for every cat. Usage: mood_state [idx]
        If an index is provided, returns just that cat's stats."""
        if len(parts) >= 2:
            cat, err = self._get_cat_at_idx(parts)
            if err:
                return err
            snap = cat.mood.snapshot()
            return (f"OK idx={parts[1]} happiness={snap['happiness']} "
                    f"energy={snap['energy']} bored={snap['bored']} "
                    f"hunger={snap['hunger']}")
        # All cats
        parts_out = []
        for i, c in enumerate(self.cat_instances):
            snap = c.mood.snapshot()
            parts_out.append(
                f"[{i}] h={snap['happiness']:.0f} e={snap['energy']:.0f} "
                f"b={snap['bored']:.0f} f={snap['hunger']:.0f}"
            )
        return "OK " + " | ".join(parts_out)

    def _cmd_mood_set(self, parts):
        """Force a specific mood stat on a cat. Usage:
        mood_set <idx> <happiness|energy|bored|hunger> <value>
        E2E helper so tests can exercise the mood-biased IDLE branch
        without waiting for natural decay."""
        if len(parts) < 4:
            return "ERR: usage: mood_set <idx> <stat> <value>"
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        stat = parts[2]
        if stat not in ("happiness", "energy", "bored", "hunger"):
            return f"ERR: unknown stat {stat}"
        try:
            value = float(parts[3])
        except ValueError:
            return "ERR: value must be a number"
        value = max(0.0, min(100.0, value))
        setattr(cat.mood, stat, value)
        return f"OK cat {parts[1]} {stat}={value}"

    def _cmd_pet_cat(self, parts):
        """E2E hook: simulate a long-press on a cat to enter petting mode,
        without actually waiting for the PETTING_THRESHOLD_MS timer.
        Usage: pet_cat <idx>"""
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        # Spoof the drag state so _try_enter_petting accepts the call
        self._drag_cat = cat
        cat.dragging = True
        cat.mouse_moved = False
        self._try_enter_petting(cat)
        if getattr(cat, "_petting_active", False):
            return f"OK petting {parts[1]}"
        return "ERR: petting did not start (cat busy?)"

    def _cmd_unpet_cat(self, parts):
        """E2E hook: release a cat from petting mode."""
        cat, err = self._get_cat_at_idx(parts)
        if err:
            return err
        self._exit_petting(cat)
        self._drag_cat = None
        cat.dragging = False
        return f"OK unpet {parts[1]}"

    def _cmd_petting_state(self, parts):
        """Return which cats are currently being petted."""
        petted = [
            i for i, c in enumerate(self.cat_instances)
            if getattr(c, "_petting_active", False)
        ]
        return f"OK petted={','.join(map(str, petted)) if petted else 'none'}"

    def _cmd_kitten_count(self, parts):
        """Return the number of real kittens currently on the canvas, i.e.
        cats born from love encounters. Explicitly excludes apocalypse
        clones which happen to share the is_kitten flag but are tagged
        is_apocalypse_clone too — otherwise a lingering apocalypse would
        make kitten_count report the entire clone army."""
        kittens = sum(
            1 for c in self.cat_instances
            if getattr(c, "is_kitten", False)
            and not getattr(c, "is_apocalypse_clone", False)
        )
        return f"OK kittens={kittens}"

    def _cmd_get_chat_response(self, parts):
        cat = self._active_chat_cat
        if not cat:
            return "ERR: no active chat"
        return f"OK {cat.chat_response}"

    def _cmd_screenshot(self, parts):
        if self._canvas_area:
            self._canvas_area.queue_draw()
        return "OK redraw queued"

    def _cmd_tts_debug(self, parts):
        """Dump per-cat TTS state: tts_enabled, chat_visible, speaker_click_rect.
        Used to diagnose why a speaker icon isn't clickable on a given chat."""
        out = [f"global={self._tts_enabled}"]
        for i, c in enumerate(self.cat_instances):
            name = c.config.get("char_id", "?")
            rect = getattr(c, "_speaker_click_rect", None)
            out.append(
                f"[{i}] {name} tts={c.tts_enabled} "
                f"chat_visible={c.chat_visible} "
                f"response_len={len(c.chat_response) if c.chat_response else 0} "
                f"rect={rect}"
            )
        return "OK " + " | ".join(out)

    def _cmd_monitors(self, parts):
        """Return the list of detected monitor rects. Usage:
            monitors              → list all rects
            monitors at <x> <y>   → report which monitor (if any) contains
                                    the given point
        Used by e2e tests and for manual debugging of multi-monitor
        geometry (e.g. when a cat walks into a dead zone)."""
        if len(parts) >= 2 and parts[1] == "at":
            if len(parts) != 4:
                return "ERR: usage: monitors at <x> <y>"
            try:
                px, py = int(parts[2]), int(parts[3])
            except ValueError:
                return "ERR: usage: monitors at <x> <y>"
            rect = _monitors_mod.monitor_at(px, py, self._monitor_rects)
            if rect is None:
                nearest = _monitors_mod.nearest_monitor(px, py, self._monitor_rects)
                return f"OK dead_zone=True nearest={nearest}"
            return f"OK dead_zone=False rect={rect}"
        rects = self._monitor_rects
        return f"OK count={len(rects)} rects={rects}"

    def _cmd_theme(self, parts):
        """Query or force the bubble-palette dark mode. Usage:
            theme           → report current state
            theme dark      → force dark palette
            theme light     → force light palette
        E2E helper so tests can exercise set_theme without requiring a real
        gsettings flip on the CI runner."""
        if len(parts) < 2:
            return f"OK dark={getattr(self, '_dark_mode', False)}"
        target = parts[1].lower()
        if target not in ("dark", "light"):
            return "ERR: usage: theme [dark|light]"
        dark = (target == "dark")
        self._dark_mode = dark
        _set_theme(dark=dark)
        if self._canvas_area:
            self._canvas_area.queue_draw()
        return f"OK dark={dark}"

    def _cmd_personality(self, parts):
        """Query, force-drift, or reset a cat's personality state.
        Usage:
            personality state <idx>               → dump quirks + counters
            personality force_drift <idx> <trait> → inject a trait (e2e)
            personality reset <idx>               → clear all quirks
        """
        if len(parts) < 2:
            return ("ERR: usage: personality state|force_drift|reset "
                    "<idx> [trait]")
        sub = parts[1]
        if sub == "state":
            cat, err = self._get_cat_at_idx(parts[1:])
            if err:
                return err
            st = cat.personality
            return (f"OK count={st.message_count} "
                    f"quirks={st.drifted_traits} "
                    f"enabled={self._personality_drift_enabled}")
        if sub == "force_drift":
            if len(parts) < 4:
                return "ERR: usage: personality force_drift <idx> <trait>"
            cat, err = self._get_cat_at_idx(parts[1:])
            if err:
                return err
            trait = " ".join(parts[3:])
            cat.personality.apply_drift(trait)
            cat.personality.save()
            cat.update_system_prompt(L10n.lang)
            return f"OK quirks={cat.personality.drifted_traits}"
        if sub == "reset":
            cat, err = self._get_cat_at_idx(parts[1:])
            if err:
                return err
            cat.personality.drifted_traits = []
            cat.personality.message_count = 0
            cat.personality.last_drift_at = 0.0
            cat.personality.save()
            cat.update_system_prompt(L10n.lang)
            return "OK reset"
        return f"ERR: unknown subcommand {sub}"

    def _seasonal_auto_dismiss(self) -> bool:
        """One-shot GLib timer: turn off the seasonal overlay after the
        configured duration so the particles announce the season then
        fade out of the way. Returns False so GLib removes the source."""
        if self._seasonal_enabled:
            self._seasonal_enabled = False
            log.info("Seasonal overlay auto-dismissed after %d s",
                     self._seasonal_duration_sec)
            if self._canvas_area:
                self._canvas_area.queue_draw()
        return False

    def _cmd_season(self, parts):
        """Query or force the seasonal overlay. Usage:
            season                    → report active season (resolved)
            season <name>             → override (winter/halloween/…)
            season auto               → clear override, use date resolver
            season off                → disable overlay entirely
            season on                 → re-enable overlay"""
        if len(parts) < 2:
            active = self._season_override or _seasonal.resolve_season()
            return (f"OK season={active} override={self._season_override} "
                    f"enabled={self._seasonal_enabled}")
        target = parts[1].lower()
        if target == "off":
            self._seasonal_enabled = False
        elif target == "on":
            self._seasonal_enabled = True
        elif target == "auto":
            self._season_override = None
        elif target in _seasonal.ALL_SEASONS:
            self._season_override = target
            self._seasonal_enabled = True
        else:
            return f"ERR: unknown season '{target}'"
        if self._canvas_area:
            self._canvas_area.queue_draw()
        return (f"OK override={self._season_override} "
                f"enabled={self._seasonal_enabled}")

    def _handle_test_cmd(self, cmd):
        """Handle a test command. Returns response string."""
        parts = cmd.split()
        if not parts:
            return "ERR: empty command"
        # Lazy-build the dispatch dict on first call
        if not hasattr(self, "_test_cmd_handlers"):
            self._test_cmd_handlers = {
                "status": self._cmd_status,
                "cat_positions": self._cmd_cat_positions,
                "force_state": self._cmd_force_state,
                "apocalypse": self._cmd_apocalypse,
                "easter_menu": self._cmd_easter_menu,
                "egg": self._cmd_egg,
                "love_encounter": self._cmd_love_encounter,
                "start_sequence": self._cmd_start_sequence,
                "meow": self._cmd_meow,
                "move_cat": self._cmd_move_cat,
                "fake_chat": self._cmd_fake_chat,
                "click_cat": self._cmd_click_cat,
                "right_click_cat": self._cmd_right_click_cat,
                "click_menu_settings": self._cmd_click_menu_settings,
                "click_menu_quit": self._cmd_click_menu_quit,
                "type_chat": self._cmd_type_chat,
                "close_chat": self._cmd_close_chat,
                "drag_cat": self._cmd_drag_cat,
                "close_settings": self._cmd_close_settings,
                "settings_state": self._cmd_settings_state,
                "egg_state": self._cmd_egg_state,
                "kitten_count": self._cmd_kitten_count,
                "pet_cat": self._cmd_pet_cat,
                "unpet_cat": self._cmd_unpet_cat,
                "petting_state": self._cmd_petting_state,
                "mood_state": self._cmd_mood_state,
                "mood_set": self._cmd_mood_set,
                "activity_state": self._cmd_activity_state,
                "force_afk": self._cmd_force_afk,
                "notify": self._cmd_notify,
                "get_chat_response": self._cmd_get_chat_response,
                "screenshot": self._cmd_screenshot,
                "tts_debug": self._cmd_tts_debug,
                "monitors": self._cmd_monitors,
                "theme": self._cmd_theme,
                "personality": self._cmd_personality,
                "season": self._cmd_season,
            }
        handler = self._test_cmd_handlers.get(parts[0])
        if handler is None:
            return f"ERR: unknown command '{parts[0]}'"
        return handler(parts)

    def _create_canvas(self):
        """Create the single fullscreen transparent overlay window."""
        win = Gtk.Window(application=self)
        win.set_decorated(False)
        win.add_css_class("canvas-window")
        set_notification_type(win)
        win.set_default_size(self.screen_w, self.screen_h)
        win.set_resizable(False)

        overlay = Gtk.Overlay()

        area = Gtk.DrawingArea()
        area.set_content_width(self.screen_w)
        area.set_content_height(self.screen_h)
        area.set_draw_func(self._canvas_draw)
        area.set_can_target(False)  # Don't capture mouse on DrawingArea → passthrough
        overlay.set_child(area)

        # Chat input box (entry + optional mic button) inside the overlay
        self._chat_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self._chat_box.set_halign(Gtk.Align.START)
        self._chat_box.set_valign(Gtk.Align.START)
        self._chat_box.set_visible(False)

        self._chat_entry = Gtk.Entry()
        self._chat_entry.set_placeholder_text(L10n.s("talk"))
        self._chat_entry.add_css_class("pixel-entry")
        entry_w = 226 if self._voice_enabled else 256
        self._chat_entry.set_size_request(entry_w, -1)
        self._chat_entry.connect("activate", self._on_chat_entry_activate)
        self._chat_box.append(self._chat_entry)

        # Space push-to-talk: hold Space inside the empty chat entry to record.
        # Intercept in CAPTURE phase so we run BEFORE the entry's default text
        # handler inserts a space character.
        if self._voice_enabled:
            key_ctrl = Gtk.EventControllerKey()
            key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            key_ctrl.connect("key-pressed", self._on_entry_key_pressed)
            key_ctrl.connect("key-released", self._on_entry_key_released)
            self._chat_entry.add_controller(key_ctrl)

        # ² key handled in _on_entry_key_pressed (when chat entry has focus)
        # + right-click context menu "Console" entry (always available)

        if self._voice_enabled:
            self._voice_btn = Gtk.Button()
            # Bundled pixel-art mic icon (matches the chat bubble's
            # speaker icon style and the cream/brown bubble palette).
            from catai_linux.drawing import ICONS_DIR as _ICONS_DIR
            self._mic_icon_path = os.path.join(_ICONS_DIR, "mic.png")
            self._mic_image = Gtk.Image.new_from_file(self._mic_icon_path)
            self._mic_image.set_pixel_size(28)
            self._voice_btn.set_child(self._mic_image)
            self._voice_btn.add_css_class("pixel-mic-btn")
            self._voice_btn.set_size_request(36, -1)
            self._voice_btn.set_tooltip_text("Hold to talk (or hold Space in the entry)")
            press_gesture = Gtk.GestureClick()
            press_gesture.set_button(1)
            press_gesture.connect("pressed", self._on_voice_press)
            press_gesture.connect("released", self._on_voice_release)
            self._voice_btn.add_controller(press_gesture)
            self._chat_box.append(self._voice_btn)

        overlay.add_overlay(self._chat_box)

        # Quake drop-down console (² key)
        self._create_quake_console(overlay)

        win.set_child(overlay)

        # Gesture controllers on the canvas
        # Right-click for context menu
        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("released", self._on_canvas_right_click)
        overlay.add_controller(rclick)

        # Left: drag gesture handles both click and drag
        # - Short drag (no movement) = click → toggle chat
        # - Long drag = move cat
        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_canvas_drag_begin)
        drag.connect("drag-update", self._on_canvas_drag_update)
        drag.connect("drag-end", self._on_canvas_drag_end)
        overlay.add_controller(drag)

        set_always_on_top(win)

        def _set_empty_input(w):
            """Set empty input region so clicks pass through everywhere."""
            surface = w.get_surface()
            if surface:
                import cairo as _cairo
                surface.set_input_region(_cairo.Region())
            xid = _get_xid(w)
            if xid:
                self._canvas_xid = xid
                _update_input_shape(xid, [])
                flush_x11()
                log.debug("Canvas passthrough active (XID=%d)", xid)

        # Apply as soon as realized
        def _on_realize(w):
            _apply_xid_hints(w, above=True, notification=True)
            _set_empty_input(w)
        win.connect("realize", _on_realize)

        win.set_visible(True)

        # Fallback: retry after visible + detect canvas Y offset
        def _late_xid_check():
            if not self._canvas_xid:
                _set_empty_input(win)
            # Detect canvas Y offset (GNOME top bar) via direct Xlib
            # ctypes — no fork. The previous xdotool subprocess was the
            # only reason CATAI runtime-depended on xdotool.
            xid = _get_xid(win)
            if xid:
                try:
                    self._canvas_y_offset = _x11_window_y_offset(xid)
                    log.debug("Canvas Y offset: %d", self._canvas_y_offset)
                except Exception:
                    log.debug("Y offset query failed", exc_info=True)
                    self._canvas_y_offset = 0
            return False
        GLib.timeout_add(500, _late_xid_check)
        GLib.idle_add(lambda: move_window(win, 0, 0) or False)

        self._canvas_window = win
        self._canvas_area = area

    def _canvas_draw(self, area, ctx, width, height):
        """Draw all cats and meow bubbles on the canvas."""
        # Clear with full transparency
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)

        # Seasonal overlay (snowflakes in winter, pumpkins for Halloween,
        # hearts on Valentine's, fireworks for NYE...). Drawn first so it
        # sits behind cats and bubbles. Resolver returns SUMMER outside of
        # any special window → draw_overlay is a cheap no-op then.
        if self._seasonal_enabled:
            try:
                _seasonal.draw_overlay(ctx, width, height,
                                       season=self._season_override)
            except Exception:
                log.exception("seasonal overlay crashed")

        # Global shake (eg_shake easter egg)
        if self._shake_amount > 0:
            sx = random.uniform(-self._shake_amount, self._shake_amount)
            sy = random.uniform(-self._shake_amount, self._shake_amount)
            ctx.translate(sx, sy)

        # Matrix digit rain (eg_matrix)
        if self._matrix_columns:
            ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(16)
            for col in self._matrix_columns:
                for i, ch in enumerate(col['trail']):
                    y = col['y'] + i * 20
                    if y < -20 or y > self.screen_h + 20:
                        continue
                    # Head is bright white, tail fades green
                    if i == 0:
                        ctx.set_source_rgba(0.9, 1.0, 0.9, 0.95)
                    else:
                        alpha = max(0, 1 - i / 14)
                        ctx.set_source_rgba(0.2, 0.9, 0.3, alpha * 0.85)
                    ctx.move_to(col['x'], y)
                    ctx.show_text(ch)

        # Nyan cat overlay (drawn BEFORE cats so regular cats render on top)
        if getattr(self, '_nyan_active', False) and getattr(self, '_nyan_frames', None):
            self._draw_nyan(ctx)

        # rm -rf easter egg — draw the 'wipe trail' band BEFORE the cats so
        # the wiping cat renders on top of its own destruction path.
        if getattr(self, "_rm_rf_active_app", False):
            for cat in self.cat_instances:
                if not getattr(cat, "_rm_rf_active", False):
                    continue
                self._draw_rm_rf_wipe(ctx, cat)

        for cat in self.cat_instances:
            # Hide & seek: skip hidden cats
            if getattr(cat, '_hidden', False):
                continue
            # Defensive: always clamp before drawing (except during birth scale anim)
            if cat._birth_progress is None:
                cat._clamp_to_screen()

            # Beam me up light column
            beam_ticks = getattr(cat, '_beam_ticks', 0)
            if beam_ticks > 0:
                bcx = cat.x + cat.display_w / 2
                # Light column with gradient
                grad = cairo.LinearGradient(bcx - 30, 0, bcx + 30, 0)
                grad.add_color_stop_rgba(0.0, 0.6, 0.9, 1.0, 0.0)
                grad.add_color_stop_rgba(0.5, 0.6, 0.9, 1.0, 0.5)
                grad.add_color_stop_rgba(1.0, 0.6, 0.9, 1.0, 0.0)
                ctx.set_source(grad)
                ctx.rectangle(bcx - 30, 0, 60, self.screen_h)
                ctx.fill()

            result = cat._current_surface()
            if result is None:
                continue  # assets still loading in background
            surface, _data_ref = result
            boss_scale = getattr(cat, '_boss_scale', None)
            if cat._birth_progress is not None:
                # Birth animation: grow from 10% to 100%, fade in from 0.15 to 1.0
                p = cat._birth_progress
                scale = 0.10 + 0.90 * p
                alpha = 0.15 + 0.85 * p
                ctx.save()
                cx = cat.x + cat.display_w / 2
                cy = cat.y + cat.display_h / 2
                ctx.translate(cx, cy)
                ctx.scale(scale, scale)
                ctx.translate(-cat.display_w / 2, -cat.display_h / 2)
                ctx.set_source_surface(surface, 0, 0)
                ctx.paint_with_alpha(alpha)
                ctx.restore()
                _draw_birth_sparkles(ctx, cat.x, cat.y, cat.display_w, cat.display_h, p)
            elif boss_scale:
                # Boss fight: cairo-scale around cat center
                ctx.save()
                cx = cat.x + cat.display_w / 2
                cy = cat.y + cat.display_h / 2
                ctx.translate(cx, cy)
                ctx.scale(boss_scale, boss_scale)
                ctx.translate(-cat.display_w / 2, -cat.display_h / 2)
                ctx.set_source_surface(surface, 0, 0)
                ctx.paint()
                ctx.restore()
            elif cat._flip_h:
                # Horizontal flip: translate to right edge, scale X by -1, draw at origin
                ctx.save()
                ctx.translate(cat.x + cat.display_w, cat.y)
                ctx.scale(-1, 1)
                ctx.set_source_surface(surface, 0, 0)
                ctx.paint()
                ctx.restore()
            else:
                ctx.save()
                ctx.rectangle(cat.x, cat.y, cat.display_w, cat.display_h)
                ctx.clip()
                ctx.set_source_surface(surface, cat.x, cat.y)
                ctx.paint()
                ctx.restore()

            # Draw overlays above cat
            if cat.state == CatState.SLEEPING_BALL:
                _draw_zzz(ctx, cat.x, cat.y, cat.display_w)
            elif cat.state == CatState.SURPRISED:
                _draw_exclamation(ctx, cat.x, cat.y, cat.display_w, cat.display_h)
            elif cat.state == CatState.LOVE:
                _draw_hearts(ctx, cat.x, cat.y, cat.display_w, cat.display_h)
            elif cat.state == CatState.HURTING:
                _draw_hurt_stars(ctx, cat.x, cat.y, cat.display_w, cat.display_h)
            elif cat.state == CatState.DYING:
                _draw_skull(ctx, cat.x, cat.y, cat.display_w, cat.display_h)
            elif cat.state == CatState.GROOMING:
                _draw_sparkle(ctx, cat.x, cat.y, cat.display_w, cat.display_h)
            elif cat.state == CatState.ANGRY:
                _draw_anger(ctx, cat.x, cat.y, cat.display_w, cat.display_h)
            elif cat.state == CatState.DASHING:
                _draw_speed_lines(ctx, cat.x, cat.y, cat.display_w, cat.display_h, cat.direction)

            # Draw meow bubble if visible
            if cat.meow_visible and cat.meow_text:
                _draw_meow_bubble(ctx, cat.meow_text, cat.x, cat.y, cat.display_w, cat.display_h, self.screen_h)

            # Draw chat response bubble if visible — with a clickable
            # speaker toggle in the top-right when TTS is configured
            # for this cat. The returned rect is stashed on the cat
            # so _on_canvas_click can hit-test it.
            if cat.chat_visible and cat.chat_response:
                speaker_rect = _draw_chat_bubble(
                    ctx, cat.chat_response, cat.x, cat.y,
                    cat.display_w, cat.display_h,
                    speaker_state=cat.tts_enabled,
                )
                cat._speaker_click_rect = speaker_rect
            else:
                cat._speaker_click_rect = None

            # Draw encounter bubble if visible
            if cat.encounter_visible and cat.encounter_text:
                _draw_encounter_bubble(ctx, cat.encounter_text, cat.x, cat.y, cat.display_w, cat.display_h)

        # Draw context menu if visible
        if self._menu_visible:
            _draw_context_menu(ctx, self._menu_x, self._menu_y, L10n.s("settings"), L10n.s("quit"))

        # Draw easter egg menu (on top of everything)
        if self._easter_menu_visible:
            self._draw_easter_menu(ctx)

    def _compute_regions_key(self):
        """Return a hashable snapshot of every input that affects input regions.
        If this is identical frame-to-frame, we can skip the Cairo region rebuild."""
        cat_keys = tuple(
            (round(c.x), round(c.y), c.display_w, c.display_h,
             c.meow_visible, c.meow_text if c.meow_visible else "",
             c.chat_visible, bool(c.chat_response) and c.chat_visible,
             c.encounter_visible, c.encounter_text if c.encounter_visible else "")
            for c in self.cat_instances
        )
        box = getattr(self, '_chat_box', None)
        box_visible = bool(box and box.get_visible())
        quake_visible = (self._quake_revealer is not None
                         and self._quake_revealer.get_reveal_child())
        return (
            cat_keys,
            self._menu_visible, self._menu_x, self._menu_y,
            self._easter_menu_visible,
            box_visible,
            box.get_margin_start() if box_visible else 0,
            box.get_margin_top() if box_visible else 0,
            self._voice_enabled,
            quake_visible,
        )

    def _build_rects(self):
        """Build the list of input rectangles that should pass mouse events through."""
        rects = []
        for cat in self.cat_instances:
            # Add some padding for easier clicking
            pad = 4
            rects.append((cat.x - pad, cat.y - pad,
                         cat.display_w + pad * 2, cat.display_h + pad * 2))
            # Also include meow bubble area if visible
            if cat.meow_visible and cat.meow_text:
                text_w = max(80, len(cat.meow_text) * 9 + 24)
                bx = cat.x + cat.display_w / 2 - text_w / 2
                by = cat.y - 40
                rects.append((bx, by, text_w, 24))
            # Include chat bubble area if visible. The bubble's actual
            # height is computed from the Pango layout and grows with
            # the response length — for long multi-line responses it
            # easily exceeds 200 px. We use a generous 320 px so the
            # entire bubble (including the speaker icon at the top)
            # always falls inside the input region.
            if cat.chat_visible and cat.chat_response:
                bw = 280
                bh = 320  # generous — actual is pad*2 + th + 42
                bx = cat.x + cat.display_w / 2 - bw / 2
                by = cat.y - bh - 15
                if by < 0:
                    by = cat.y + cat.display_h + 10
                rects.append((bx, by, bw, bh))
                # Also explicitly include the speaker icon click rect
                # (computed by drawing.draw_chat_bubble at the actual
                # bubble position) so even if the generous bh ever
                # under-counts, the icon never falls outside the input
                # region.
                speaker_rect = getattr(cat, "_speaker_click_rect", None)
                if speaker_rect is not None:
                    rects.append(speaker_rect)
            # Include encounter bubble area if visible
            if cat.encounter_visible and cat.encounter_text:
                enc_bw = max(90, len(cat.encounter_text) * 7 + 20)
                enc_bh = 60  # approximate (up to 4 lines)
                enc_bx = cat.x + cat.display_w / 2 - enc_bw / 2
                enc_by = cat.y - enc_bh - 8
                if enc_by < 4:
                    enc_by = cat.y + cat.display_h + 8
                rects.append((enc_bx, enc_by, enc_bw, enc_bh))
        # Include context menu if visible
        if self._menu_visible:
            rects.append((self._menu_x, self._menu_y, 120, 50))
        # Include easter egg menu (covers whole screen so clicks anywhere dismiss)
        if self._easter_menu_visible:
            rects.append((0, 0, self.screen_w, self.screen_h))
        # Include chat box (entry + optional mic button) in input region when visible
        if getattr(self, '_chat_box', None) and self._chat_box.get_visible():
            box_w = 290 if self._voice_enabled else 260
            rects.append((self._chat_box.get_margin_start(),
                         self._chat_box.get_margin_top(), box_w, 32))
        # Include Quake console when revealed — covers the top ~40% of screen
        if (self._quake_revealer is not None
                and self._quake_revealer.get_reveal_child()):
            console_h = int(self.screen_h * 0.4) + 4  # +4 for border
            rects.append((0, 0, self.screen_w, console_h))
        return rects

    def _update_input_regions(self):
        """Update XShape + GDK input regions. Uses a dirty-key cache to skip
        the (expensive) Cairo region rebuild when no cat has moved between
        frames — typically the common case when cats are IDLE."""
        key = self._compute_regions_key()
        if key == self._regions_cache_key and self._cairo_region_cache is not None:
            return  # nothing changed → reuse previously applied regions
        self._regions_cache_key = key

        rects = self._build_rects()

        # XShape for X11 level (skip if not X11)
        if self._canvas_xid:
            _update_input_shape(self._canvas_xid, rects)

        # GDK surface input region (GTK4 level — needed for GTK to pass events through,
        # including on Wayland where XShape is unavailable)
        if self._canvas_window:
            surface = self._canvas_window.get_surface()
            if surface:
                region = cairo.Region()
                for rx, ry, rw, rh in rects:
                    region.union(cairo.RectangleInt(int(rx), int(ry), max(1, int(rw)), max(1, int(rh))))
                surface.set_input_region(region)
                self._cairo_region_cache = region

    def _find_cat_at(self, x, y):
        """Find the topmost cat at position (x, y). Returns CatInstance or None."""
        # Iterate in reverse so last-drawn (topmost) cat wins
        for cat in reversed(self.cat_instances):
            if cat.hit_test(x, y):
                return cat
        return None

    # ── Canvas gesture handlers ──────────────────────────────────────────────

    def _toggle_chat_for(self, cat):
        """Toggle chat bubble for a specific cat."""
        if cat.is_kitten:
            # Kittens can't talk — just show a random baby meow
            cat._show_random_meow()
            return
        if cat.chat_visible:
            cat.chat_visible = False
            self._chat_box.set_visible(False)
            self._active_chat_cat = None
        else:
            # Close other chats
            for c in self.cat_instances:
                c.chat_visible = False
            cat.chat_visible = True
            if not cat.chat_response:
                cat.chat_response = L10n.s("hi")
            self._active_chat_cat = cat
            self._position_chat_entry(cat)
            self._chat_box.set_visible(True)
            self._chat_entry.grab_focus()

    def _position_chat_entry(self, cat):
        """Position the entry inside the chat bubble (same layout as _draw_chat_bubble).
        Caches the Pango layout across frames — only the bubble text needs to change
        when the AI streams new tokens, the font/wrap/width setup is constant."""
        text = cat.chat_response or ""
        pad = 12
        content_w = 256
        # Match draw_chat_bubble's reservation for the speaker icon so
        # the entry's bh estimate matches the actual bubble height.
        # The chat bubble always shows the speaker icon (per-cat
        # tts_enabled state, but presence is constant), so we always
        # subtract the icon column.
        ICON_RESERVE = 24 + 12  # speaker_on width + margin/outline pad
        text_w = content_w - ICON_RESERVE

        # Lazy-create + cache the Pango layout (once per CatAIApp lifetime)
        lay = self._bubble_layout_cache
        if lay is None:
            tmp = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
            tctx = cairo.Context(tmp)
            lay = PangoCairo.create_layout(tctx)
            lay.set_font_description(Pango.FontDescription(_BUBBLE_FONT))
            lay.set_wrap(Pango.WrapMode.WORD_CHAR)
            # Must stay in lockstep with drawing.draw_chat_bubble's
            # set_height(-16) — otherwise the entry box position drifts
            # away from the actual bubble bottom edge.
            lay.set_height(-16)
            lay.set_ellipsize(Pango.EllipsizeMode.END)
            self._bubble_layout_cache = lay
            self._bubble_layout_cached_text = None
        # Width may change frame-to-frame if the layout ever gets
        # narrowed dynamically; set it every call for safety.
        lay.set_width(text_w * Pango.SCALE)

        if text != self._bubble_layout_cached_text:
            lay.set_text(text, -1)
            self._bubble_layout_cached_text = text
        _tw, th = lay.get_pixel_size()

        bw = content_w + pad * 2  # 280
        bh = pad * 2 + th + 42
        bx = cat.x + cat.display_w / 2 - bw / 2
        by = cat.y - bh - 15
        if by < 0:
            by = cat.y + cat.display_h + 10

        entry_x = int(bx + pad)
        entry_y = int(by + bh - 36)
        self._chat_box.set_margin_start(max(0, entry_x))
        self._chat_box.set_margin_top(max(0, entry_y))

    def _on_chat_entry_activate(self, entry):
        """User pressed Enter in the chat entry. Long-paste detection and
        easter egg dispatch live inside CatInstance.send_chat so all code
        paths (this one + the test socket) share the same logic."""
        text = entry.get_text().strip()
        if not text or not self._active_chat_cat:
            return
        entry.set_text("")
        self._active_chat_cat.send_chat(text)

    # ── Voice chat (push-to-talk) ─────────────────────────────────────────────

    def _start_voice_recording(self):
        """Start recording + update UI. Shared by mic button press & Space keydown.
        Returns True if recording started, False otherwise."""
        if not self._voice_recorder or self._voice_recorder._recording:
            return False
        # Release the wake-word listener's grip on autoaudiosrc — both
        # pipelines target the same exclusive device on PulseAudio. We
        # call resume() in the on_result callback once Whisper is done.
        if self._wake is not None:
            self._wake.pause()
        # Stop any in-flight TTS playback synchronously. Without this,
        # the TTS GStreamer pipeline can still hold the audio device
        # in a transient state that makes the next autoaudiosrc capture
        # return silence — user reported "first record per cat works,
        # next ones empty" which was exactly this. stop() clears the
        # queue and drops the active playbin pipeline.
        try:
            _tts.get_default_player().stop()
        except Exception:
            log.debug("TTS stop during voice start failed", exc_info=True)
        # Cancel any pending delayed submit from a previous recording
        if getattr(self, "_voice_submit_timer", None):
            try:
                GLib.source_remove(self._voice_submit_timer)
            except Exception:
                pass
            self._voice_submit_timer = None
        self._chat_entry.set_text("")
        if self._voice_btn:
            # Recording state — keep the pixel-art mic image but apply
            # the red CSS class to flag the active capture.
            self._voice_btn.add_css_class("pixel-mic-btn-recording")
        self._chat_entry.set_placeholder_text("Recording... (release to send)")
        try:
            self._voice_recorder.start()
            return True
        except Exception:
            log.exception("Failed to start voice recording")
            if self._voice_btn:
                self._voice_btn.remove_css_class("pixel-mic-btn-recording")
            self._chat_entry.set_placeholder_text(L10n.s("talk"))
            return False

    def _stop_voice_recording(self):
        """Stop recording + transcribe + auto-submit. Shared by mic button release
        & Space keyup. Returns True if a stop was triggered."""
        if not self._voice_recorder or not self._voice_recorder._recording:
            return False
        # If a wake-triggered auto-stop timer is pending and the user
        # released the mic / Space themselves, cancel the timer so it
        # doesn't fire on a no-op recording later.
        if getattr(self, "_wake_ptt_stop_timer", None):
            try:
                GLib.source_remove(self._wake_ptt_stop_timer)
            except Exception:
                pass
            self._wake_ptt_stop_timer = None
        if self._voice_btn:
            # Transcribing state — swap the mic image for a pixel-art
            # hourglass that matches the bubble theme. The mic image
            # comes back in on_result below.
            from catai_linux.drawing import ICONS_DIR as _ICONS_DIR
            sablier_path = os.path.join(_ICONS_DIR, "sablier.png")
            sablier_img = Gtk.Image.new_from_file(sablier_path)
            sablier_img.set_pixel_size(28)
            self._voice_btn.set_child(sablier_img)
            self._voice_btn.set_sensitive(False)
            self._voice_btn.remove_css_class("pixel-mic-btn-recording")

        # Detect if we need to download the model (first-time use for this model)
        model_name = self._voice_recorder.MODEL_NAME
        need_download = (
            self._voice_recorder._model is None
            and not _whisper_model_cached(model_name)
        )
        size_mb = WHISPER_MODEL_SIZES.get(model_name, 0)

        # Animated spinner on the placeholder
        spinner_frames = ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834", "\u2826", "\u2827", "\u2807", "\u280f"]
        spinner_state = {"i": 0}
        def tick_spinner():
            i = spinner_state["i"]
            spinner_state["i"] = (i + 1) % len(spinner_frames)
            frame = spinner_frames[i]
            if need_download:
                msg = f"{frame} Downloading {model_name} model (~{size_mb} MB)..."
            elif self._voice_recorder._model is None:
                msg = f"{frame} Loading voice model..."
            else:
                msg = f"{frame} Transcribing..."
            self._chat_entry.set_placeholder_text(msg)
            return True
        # Run spinner every 120ms
        spinner_timer = GLib.timeout_add(120, tick_spinner)
        # Kick off the initial frame immediately
        tick_spinner()

        def on_result(text):
            log.debug("VOICE: on_result called with text=%r", text)
            # Stop the download/transcribing spinner
            try:
                GLib.source_remove(spinner_timer)
            except Exception:
                pass
            if self._voice_btn:
                # Re-attach the pixel-art mic image (set_label removed
                # the child Image during the transcribing spinner).
                self._mic_image = Gtk.Image.new_from_file(self._mic_icon_path)
                self._mic_image.set_pixel_size(28)
                self._voice_btn.set_child(self._mic_image)
                self._voice_btn.set_sensitive(True)
            self._chat_entry.set_placeholder_text(L10n.s("talk"))
            if text and self._active_chat_cat:
                _metrics.track("voice_recording")
                # Show the transcribed text briefly AND submit immediately in
                # parallel — the chat generation starts right away, the text
                # stays visible ~1.5s as confirmation then clears.
                self._chat_entry.set_text(text)
                self._active_chat_cat.send_chat(text)
                # Cancel any previous clear timer
                if getattr(self, "_voice_submit_timer", None):
                    try:
                        GLib.source_remove(self._voice_submit_timer)
                    except Exception:
                        pass
                def clear_entry():
                    self._voice_submit_timer = None
                    self._chat_entry.set_text("")
                    return False
                self._voice_submit_timer = GLib.timeout_add(1500, clear_entry)
            # Hand the mic back to the wake-word listener now that
            # transcription is done. Cheap — pipeline state is just
            # flipped back to PLAYING.
            if self._wake is not None:
                self._wake.resume()
            return False  # idle_add callback

        self._voice_recorder.stop_and_transcribe(L10n.lang, on_result)
        return True

    # ── Wake word ────────────────────────────────────────────────────────────

    def _on_wake_word_heard(self, char_id: str, verb: str | None = None) -> bool:
        """GTK-main callback fired by ``WakeWordListener`` when one of
        the registered cat names is recognized in live audio.

        ``verb`` is None for plain wake calls (just the cat's name) and
        triggers the legacy "open chat + auto-PTT" flow. When the user
        chained a command verb (e.g. "Mandarine dors") we dispatch to
        a dedicated handler instead.

        Returns False so callers can pass this directly to GLib.idle_add.
        """
        target = next(
            (c for c in self.cat_instances if c.config.get("char_id") == char_id),
            None,
        )
        if target is None:
            log.debug("WAKE: no instance for %s", char_id)
            return False
        _metrics.track("wake_word_triggered", cat_id=target.config.get("id"))

        if verb is None:
            self._wake_open_chat_and_listen(target)
            return False

        # Verb dispatch — each handler is short and isolated so adding
        # a new verb is one entry in COMMAND_VERBS + one branch here.
        log.warning("WAKE: dispatching verb %r on %s", verb, char_id)
        try:
            if verb == "dors":
                self._wake_action_sleep(target)
            elif verb == "viens":
                self._wake_action_come(target)
            elif verb == "raconte":
                self._wake_action_tell_story(target)
            elif verb == "danse":
                self._wake_action_dance(target)
            elif verb == "saute":
                self._wake_action_jump(target)
            elif verb == "roule":
                self._wake_action_roll(target)
            else:
                log.debug("WAKE: unknown verb %r — falling back to chat", verb)
                self._wake_open_chat_and_listen(target)
        except Exception:
            log.exception("WAKE: verb handler %r crashed", verb)
        return False

    # ── Wake-word verb handlers ──────────────────────────────────────────────

    def _wake_open_chat_and_listen(self, target) -> None:
        """Default wake action (no verb): open the cat's chat bubble
        and auto-start a 6-second push-to-talk window so the user can
        speak right after their wake word without touching the keyboard.

        Critical: the normal PTT design is press-and-hold (mic button
        or Space). When triggered programmatically nobody is holding
        anything down — we MUST schedule an auto-stop, otherwise the
        recording runs forever, on_result never fires, the wake
        listener stays paused, and subsequent wake calls go nowhere."""
        log.warning("WAKE: opening chat for %s", target.config.get("char_id"))
        # Make sure the bubble is open & focused. _toggle_chat_for would
        # close it if already open, so we set state directly.
        for c in self.cat_instances:
            c.chat_visible = False
        target.chat_visible = True
        if not target.chat_response:
            target.chat_response = L10n.s("hi")
        self._active_chat_cat = target
        self._position_chat_entry(target)
        if self._chat_box is not None:
            self._chat_box.set_visible(True)
        if self._chat_entry is not None:
            self._chat_entry.grab_focus()
        if self._voice_enabled and self._voice_recorder is not None:
            try:
                started = self._start_voice_recording()
            except Exception:
                log.exception("WAKE: auto-start PTT crashed")
                started = False
            if started:
                if getattr(self, "_wake_ptt_stop_timer", None):
                    try:
                        GLib.source_remove(self._wake_ptt_stop_timer)
                    except Exception:
                        pass
                    self._wake_ptt_stop_timer = None

                def _wake_ptt_auto_stop():
                    self._wake_ptt_stop_timer = None
                    try:
                        self._stop_voice_recording()
                    except Exception:
                        log.exception("WAKE: auto-stop PTT crashed")
                    return False

                # 6 s gives the user time to formulate a sentence after
                # the wake word, which is what every voice assistant
                # gives roughly. Adjust here if it feels too short.
                self._wake_ptt_stop_timer = GLib.timeout_add(
                    6000, _wake_ptt_auto_stop)

    def _wake_action_sleep(self, target) -> None:
        """Verb 'dors' — curl the cat into a sleeping ball."""
        target.state = CatState.SLEEPING_BALL
        target.frame_index = 0
        target.direction = "south"
        # Reset the breathing tick so the animation starts fresh
        if hasattr(target, "_sleep_tick"):
            target._sleep_tick = 0

    def _wake_action_come(self, target) -> None:
        """Verb 'viens' — make the cat walk to the user's mouse cursor.
        Uses XQueryPointer via ctypes (no subprocess). Falls back to a
        random screen point if the mouse query fails (e.g. pure
        Wayland session)."""
        pos = _x11_mouse_position()
        if pos is None:
            # Fallback: random point in monitor 0 — cat at least moves
            # so the user gets feedback that the command was heard.
            log.debug("WAKE: mouse query failed, fallback to random target")
            tx = random.randint(target.display_w, max(target.display_w + 1,
                                                      self.screen_w - target.display_w))
            ty = target.y
        else:
            tx, ty = pos
            # Clamp to canvas safe area so the cat doesn't try to walk
            # into a screen corner where it would clip.
            tx = max(target.display_w // 2,
                     min(self.screen_w - target.display_w // 2, tx))
            ty = max(target.display_h // 2,
                     min(self.screen_h - target.display_h // 2, ty))
        target.dest_x = float(tx)
        target.dest_y = float(ty)
        target.state = CatState.WALKING
        target.frame_index = 0

    def _wake_action_tell_story(self, target) -> None:
        """Verb 'raconte' — open the chat and inject a 'tell me a
        story' prompt so the AI generates an anecdote on the fly."""
        # Open the chat bubble first (same path as the default wake)
        for c in self.cat_instances:
            c.chat_visible = False
        target.chat_visible = True
        self._active_chat_cat = target
        self._position_chat_entry(target)
        if self._chat_box is not None:
            self._chat_box.set_visible(True)
        if self._chat_entry is not None:
            self._chat_entry.grab_focus()
        # Pick a localized prompt — the AI's response language is set
        # by L10n.lang at backend creation, so a French prompt yields
        # a French story.
        prompts = {
            "fr": "Raconte-moi une petite anecdote rigolote en quelques phrases.",
            "en": "Tell me a short funny story in a few sentences.",
            "es": "Cuéntame una pequeña anécdota divertida en unas frases.",
        }
        prompt = prompts.get(L10n.lang, prompts["fr"])
        target.send_chat(prompt)

    def _wake_action_dance(self, target) -> None:
        """Verb 'danse' — mini disco loop on this single cat. Cycles
        through the celebratory states (LOVE / ROLLING / GROOMING /
        FLAT) every 500 ms for 5 seconds, then returns to IDLE.

        Reuses the same state list as the global ``eg_disco`` easter
        egg but scoped to one cat so the others keep doing their own
        thing."""
        dance_states = [CatState.LOVE, CatState.ROLLING,
                        CatState.GROOMING, CatState.FLAT]
        target.in_encounter = True
        target.state = random.choice(dance_states)
        target.frame_index = 0
        target.direction = "south"
        ticks = [10]  # 5 s at 500 ms per tick

        def _dance_tick():
            if ticks[0] <= 0:
                target.in_encounter = False
                target.state = CatState.IDLE
                target.frame_index = 0
                return False
            target.state = random.choice(dance_states)
            target.frame_index = 0
            ticks[0] -= 1
            return True

        GLib.timeout_add(500, _dance_tick)

    def _wake_action_jump(self, target) -> None:
        """Verb 'saute' — JUMPING animation, then return to IDLE
        automatically via the existing animation tick logic."""
        target.state = CatState.JUMPING
        target.frame_index = 0
        target.direction = "south"

    def _wake_action_roll(self, target) -> None:
        """Verb 'roule' — ROLLING animation, same lifetime as jump."""
        target.state = CatState.ROLLING
        target.frame_index = 0
        target.direction = "south"

    def _on_voice_press(self, gesture, n_press, x, y):
        """Mic button pressed down → start recording."""
        if self._start_voice_recording():
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_voice_release(self, gesture, n_press, x, y):
        """Mic button released → stop recording, transcribe, auto-submit."""
        if self._stop_voice_recording():
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_entry_key_pressed(self, ctrl, keyval, keycode, state):
        """Hold Space inside the empty chat entry → push-to-talk record.
        ² key → toggle Quake console. Escape → close chat bubble.
        Runs in CAPTURE phase so we intercept before Gtk.Entry inserts chars."""
        # ² → toggle Quake console (consume the event so ² doesn't
        # appear in the chat entry text)
        if keyval == Gdk.KEY_twosuperior:
            self._toggle_quake_console()
            return True
        # Escape → close the chat bubble
        if keyval == Gdk.KEY_Escape:
            if self._active_chat_cat:
                self._active_chat_cat.chat_visible = False
                self._chat_box.set_visible(False)
                self._active_chat_cat = None
            return True
        if keyval != Gdk.KEY_space:
            return False
        # Modifier keys: let Ctrl+Space / Shift+Space pass through untouched
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        # Only act when the entry is empty AND voice is enabled
        if not self._voice_enabled or self._chat_entry.get_text():
            return False
        # Ignore auto-repeat: if we're already recording, swallow the event
        # (return True) so Space is not inserted, but don't restart.
        if self._voice_recorder and self._voice_recorder._recording:
            return True
        if self._start_voice_recording():
            return True  # consume — do not type a space
        return False

    def _on_entry_key_released(self, ctrl, keyval, keycode, state):
        """Release Space → stop recording + auto-submit."""
        if keyval != Gdk.KEY_space:
            return False
        if self._voice_recorder and self._voice_recorder._recording:
            self._stop_voice_recording()
            return True
        return False


    # ── Quake-style drop-down console ────────────────────────────────────────

    def _create_quake_console(self, overlay):
        """Build the Quake console widget tree and add it to *overlay*."""
        import textwrap as _tw

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(200)
        revealer.set_reveal_child(False)
        revealer.set_valign(Gtk.Align.START)
        revealer.set_hexpand(True)

        # Main container — force dark background via inline CSS because
        # GTK4's global CSS doesn't reliably paint Box backgrounds on
        # all themes / compositors.
        console_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        console_box.add_css_class("quake-console")
        _qcss = Gtk.CssProvider()
        _qcss.load_from_data(b"""
            .quake-console { background-color: rgba(0,0,0,0.92); }
            .quake-output  { background-color: rgba(0,0,0,0); color: #33ff33;
                             font-family: monospace; font-size: 12px; }
            .quake-input   { background-color: rgba(0,0,0,0); color: #33ff33;
                             border: none; font-family: monospace; font-size: 12px;
                             caret-color: #33ff33; }
            .quake-input:focus { outline: none; box-shadow: none; }
            .quake-prompt  { color: #33ff33; font-family: monospace; font-size: 12px; }
            .quake-border  { background-color: #33ff33; min-height: 2px; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), _qcss,
            Gtk.STYLE_PROVIDER_PRIORITY_USER)  # USER > APPLICATION, wins
        # ~40 % screen height
        console_box.set_size_request(-1, int(self.screen_h * 0.4))

        # Output area (scrolled, non-editable text view)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.set_hexpand(True)
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        tv = Gtk.TextView()
        tv.set_editable(False)
        tv.set_cursor_visible(False)
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.add_css_class("quake-output")
        tv.set_left_margin(6)
        tv.set_right_margin(6)
        tv.set_top_margin(4)
        tv.set_bottom_margin(4)
        sw.set_child(tv)
        console_box.append(sw)

        # Bottom border
        border = Gtk.Box()
        border.add_css_class("quake-border")
        console_box.append(border)

        # Input row
        input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        input_row.set_margin_start(6)
        input_row.set_margin_end(6)
        input_row.set_margin_top(2)
        input_row.set_margin_bottom(4)

        prompt_label = Gtk.Label(label="catai> ")
        prompt_label.add_css_class("quake-prompt")
        input_row.append(prompt_label)

        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.add_css_class("quake-input")
        entry.connect("activate", self._on_quake_entry_activate)

        # Key controller for history navigation + escape
        entry_key_ctrl = Gtk.EventControllerKey()
        entry_key_ctrl.connect("key-pressed", self._on_quake_entry_key)
        entry.add_controller(entry_key_ctrl)

        input_row.append(entry)
        console_box.append(input_row)

        revealer.set_child(console_box)
        overlay.add_overlay(revealer)

        self._quake_revealer = revealer
        self._quake_output = tv
        self._quake_entry = entry
        self._quake_sw = sw

        # Print welcome banner
        banner = _tw.dedent("""\
            CATAI Console v1.0 — type 'help' for commands
            ─────────────────────────────────────────────
        """)
        buf = tv.get_buffer()
        buf.set_text(banner)

    def _toggle_quake_console(self):
        """Show or hide the Quake console."""
        if self._quake_revealer is None:
            return
        visible = self._quake_revealer.get_reveal_child()
        if visible:
            self._quake_revealer.set_reveal_child(False)
            # Return focus to canvas
            if self._canvas_window:
                self._canvas_window.set_focus(None)
        else:
            self._quake_revealer.set_reveal_child(True)
            # Grab focus on entry
            if self._quake_entry:
                self._quake_entry.grab_focus()
            self._quake_history_idx = -1

    def _on_quake_entry_key(self, ctrl, keyval, keycode, state):
        """Handle special keys in the Quake console entry."""
        if keyval == Gdk.KEY_Escape:
            self._toggle_quake_console()
            return True
        if keyval == Gdk.KEY_twosuperior:
            self._toggle_quake_console()
            return True
        if keyval == Gdk.KEY_Up:
            if self._quake_history:
                if self._quake_history_idx == -1:
                    self._quake_history_idx = len(self._quake_history) - 1
                elif self._quake_history_idx > 0:
                    self._quake_history_idx -= 1
                self._quake_entry.set_text(self._quake_history[self._quake_history_idx])
                self._quake_entry.set_position(-1)
            return True
        if keyval == Gdk.KEY_Down:
            if self._quake_history:
                if self._quake_history_idx == -1:
                    return True
                if self._quake_history_idx < len(self._quake_history) - 1:
                    self._quake_history_idx += 1
                    self._quake_entry.set_text(self._quake_history[self._quake_history_idx])
                else:
                    self._quake_history_idx = -1
                    self._quake_entry.set_text("")
                self._quake_entry.set_position(-1)
            return True
        # Tab → autocomplete cat names, egg names, commands
        if keyval == Gdk.KEY_Tab:
            self._quake_tab_complete()
            return True
        return False

    def _quake_tab_complete(self) -> None:
        """Simple prefix-based tab completion for the Quake console."""
        text = self._quake_entry.get_text()
        if not text:
            return

        # Known commands
        cmds = ["status", "cats", "meow", "say", "egg", "eggs", "sleep",
                "wake", "dance", "come", "mood", "move", "season",
                "notify", "ai", "help", "clear", "quit"]

        parts = text.split()
        if len(parts) == 1 and not text.endswith(" "):
            # Complete command name
            matches = [c for c in cmds if c.startswith(parts[0].lower())]
            if len(matches) == 1:
                self._quake_entry.set_text(matches[0] + " ")
                self._quake_entry.set_position(-1)
            elif matches:
                self._quake_print("  ".join(matches))
        elif len(parts) >= 1:
            # Complete argument (cat name or egg name)
            cmd = parts[0].lower()
            prefix = parts[-1].lower() if len(parts) > 1 else ""
            cat_cmds = {"meow", "say", "sleep", "wake", "dance",
                        "come", "mood", "move"}
            if cmd in cat_cmds:
                names = [c.config.get("name", "") for c in self.cat_instances]
                matches = [n for n in names if n.lower().startswith(prefix)]
            elif cmd == "egg":
                from catai_linux.easter_eggs import EASTER_EGGS
                egg_keys = [e["key"] for e in EASTER_EGGS]
                matches = [k for k in egg_keys if k.startswith(prefix)]
            elif cmd == "season":
                seasons = ["winter", "halloween", "christmas", "valentines",
                           "nye", "spring", "autumn", "summer", "off", "on", "auto"]
                matches = [s for s in seasons if s.startswith(prefix)]
            else:
                return

            if len(matches) == 1:
                # Replace the last word with the match
                new_text = " ".join(parts[:-1] + [matches[0]]) if len(parts) > 1 else f"{cmd} {matches[0]}"
                self._quake_entry.set_text(new_text + " ")
                self._quake_entry.set_position(-1)
            elif matches:
                self._quake_print("  ".join(matches))

    def _quake_resolve_name(self, text: str) -> str:
        """Resolve cat names to indices in a command string.

        'mood Mandarine' → 'mood 0'
        'meow Tabby hello' → 'meow 1 hello'
        """
        cat_cmds = {"meow", "say", "sleep", "wake", "dance",
                    "come", "mood", "move", "force_state"}
        parts = text.split()
        if len(parts) < 2:
            return text
        cmd = parts[0].lower()
        if cmd not in cat_cmds:
            return text
        name = parts[1]
        # Already an int? Leave it
        try:
            int(name)
            return text
        except ValueError:
            pass
        # Resolve name to index
        lower = name.lower()
        for i, c in enumerate(self.cat_instances):
            if c.config.get("name", "").lower() == lower:
                parts[1] = str(i)
                return " ".join(parts)
        return text  # not found, let the socket return the error

    def _quake_log(self, line: str) -> None:
        """Append a line to ~/.config/catai/console.log for debugging."""
        try:
            import datetime as _dt
            ts = _dt.datetime.now().strftime("%H:%M:%S")
            path = os.path.join(CONFIG_DIR, "console.log")
            with open(path, "a") as f:
                f.write(f"[{ts}] {line}\n")
        except Exception:
            pass

    def _on_quake_entry_activate(self, entry):
        """Process a command entered in the Quake console."""
        text = entry.get_text().strip()
        if not text:
            return
        entry.set_text("")
        self._quake_history_idx = -1

        # Add to history (dedup consecutive)
        if not self._quake_history or self._quake_history[-1] != text:
            self._quake_history.append(text)
        # Cap history size
        if len(self._quake_history) > 200:
            self._quake_history = self._quake_history[-200:]

        # Print the command + log
        self._quake_print(f"catai> {text}")
        self._quake_log(f"> {text}")

        # Handle special built-in commands
        if text in ("quit", "exit", "q"):
            self._toggle_quake_console()
            return
        if text == "clear":
            buf = self._quake_output.get_buffer()
            buf.set_text("")
            return

        # AI command — dispatch to background thread
        parts = text.split(None, 1)
        if parts[0] == "ai":
            arg = parts[1] if len(parts) > 1 else ""
            if not arg.strip():
                self._quake_print("Usage: ai <votre demande en langage naturel>\n")
                return
            self._quake_ai(arg.strip())
            return

        # Intercept 'help' for a user-friendly version
        if text.strip().lower() == "help":
            self._quake_print(
                "Commandes :\n"
                "  cats             Liste des chats\n"
                "  meow <nom> [txt] Bulle de meow\n"
                "  say <nom> <txt>  Envoyer un message chat\n"
                "  sleep <nom>      Mettre en dodo\n"
                "  wake <nom>       Réveiller\n"
                "  dance <nom>      Faire danser\n"
                "  come <nom>       Venir au centre\n"
                "  mood <nom>       Voir l'humeur\n"
                "  move <nom> x y   Déplacer\n"
                "  egg <clé>        Easter egg\n"
                "  eggs             Lister les eggs\n"
                "  season [nom]     Overlay saisonnier\n"
                "  ai <texte>       IA interprète\n"
                "  clear            Effacer\n"
                "  quit             Fermer\n"
                "  Tab = complétion, ↑↓ = historique\n"
            )
            self._quake_log("< [help]")
            return

        # High-level aliases that resolve names → socket commands
        parts = text.split()
        cmd0 = parts[0].lower() if parts else ""
        if cmd0 == "sleep" and len(parts) >= 2:
            text = f"force_state {parts[1]} sleeping_ball"
        elif cmd0 == "wake" and len(parts) >= 2:
            text = f"force_state {parts[1]} idle"
        elif cmd0 == "dance" and len(parts) >= 2:
            text = f"force_state {parts[1]} love"
        elif cmd0 == "come" and len(parts) >= 2:
            text = f"move {parts[1]} {self.screen_w // 2} {self.screen_h // 2}"
        elif cmd0 == "eggs":
            text = "list_eggs"

        # Resolve cat names to indices
        text = self._quake_resolve_name(text)

        # Try API command first
        resp = self._handle_api_cmd(text)
        if "ERR: unknown command" in resp:
            # Fall through to test commands
            resp = self._handle_test_cmd(text)
        self._quake_print(f"{resp}\n")
        self._quake_log(f"< {resp}")

    def _quake_print(self, text):
        """Append *text* to the Quake console output and auto-scroll."""
        if self._quake_output is None:
            return
        buf = self._quake_output.get_buffer()
        end_iter = buf.get_end_iter()
        buf.insert(end_iter, text + "\n")

        # Trim to ~500 lines
        line_count = buf.get_line_count()
        if line_count > 500:
            start = buf.get_start_iter()
            trim_to = buf.get_iter_at_line(line_count - 500)
            buf.delete(start, trim_to)

        # Auto-scroll to bottom
        def _scroll():
            end = buf.get_end_iter()
            mark = buf.create_mark(None, end, False)
            self._quake_output.scroll_mark_onscreen(mark)
            buf.delete_mark(mark)
            return False
        GLib.idle_add(_scroll)

    def _quake_ai(self, user_text):
        """Run AI command interpretation in a background thread."""
        import textwrap as _tw

        self._quake_print("Interrogation de l'IA...\n")

        # Fetch current cats for context
        cats_info = []
        for i, c in enumerate(self.cat_instances):
            cats_info.append(
                f"  index={i} name={c.config.get('name', '?')} "
                f"state={c.state.value if hasattr(c.state, 'value') else str(c.state)}"
            )
        cat_list_str = "\n".join(cats_info) or "  (aucun chat)"

        system_prompt = _tw.dedent(f"""\
            You are a CATAI command interpreter. Given a natural language request,
            output ONLY the raw commands to execute, one per line.
            Do not add explanations or markdown.

            Available commands:
            - meow <cat_index> <text>
            - egg <key>
            - notify [app] [summary]
            - force_state <cat_index> <state>
            - season <name>
            - say <cat_index> <text>
            - move <cat_index> <x> <y>

            Available states: idle, sleeping_ball, walking, love, rolling, grooming,
            flat, surprised, jumping, dashing, dying

            Current cats:
            {cat_list_str}

            Examples:
            User: "mets tous les chats en dodo"
            force_state 0 sleeping_ball
            force_state 1 sleeping_ball

            User: "fait danser Mandarine"
            force_state 0 love
        """)

        def _run():
            try:
                from catai_linux.chat_backend import create_chat
                backend = create_chat("claude-haiku-4-5")
                backend.messages = [{"role": "system", "content": system_prompt}]
                backend.messages.append({"role": "user", "content": user_text})
                full = ""
                for chunk in backend._stream_chunks():
                    full += chunk

                if not full.strip():
                    GLib.idle_add(self._quake_print,
                                  "L'IA n'a retourne aucune commande.\n")
                    return

                commands = [line.strip() for line in full.strip().splitlines()
                            if line.strip()]

                def _exec():
                    for cmd in commands:
                        self._quake_print(f"[AI] {cmd}")
                        self._quake_log(f"[AI] {cmd}")
                        resp = self._handle_api_cmd(cmd)
                        if "ERR: unknown command" in resp:
                            resp = self._handle_test_cmd(cmd)
                        self._quake_print(f"  {resp}")
                        self._quake_log(f"< {resp}")
                    self._quake_print("")
                    return False
                GLib.idle_add(_exec)
            except Exception as e:
                GLib.idle_add(self._quake_print, f"Erreur IA : {e}\n")
                self._quake_log(f"[AI ERROR] {e}")

        threading.Thread(target=_run, daemon=True).start()

    def _on_canvas_right_click(self, gesture, n_press, x, y):
        cat = self._find_cat_at(x, y)
        if not cat:
            self._menu_visible = False
            return
        if self._menu_visible:
            # Click on "Réglages" zone?
            if self._menu_x <= x <= self._menu_x + 120 and self._menu_y <= y <= self._menu_y + 25:
                self._menu_visible = False
                self._open_settings()
                return
            # Click on "Quitter" zone?
            if self._menu_x <= x <= self._menu_x + 120 and self._menu_y + 25 <= y <= self._menu_y + 50:
                self._menu_visible = False
                self.quit()
                return
            self._menu_visible = False
        else:
            self._menu_visible = True
            self._menu_x = int(cat.x + cat.display_w)
            self._menu_y = int(cat.y)
            # Auto-close after 5s
            if getattr(self, '_menu_timer', None):
                GLib.source_remove(self._menu_timer)
            self._menu_timer = GLib.timeout_add(5000, self._close_menu)

    def _close_menu(self):
        self._menu_visible = False
        self._menu_timer = None
        return False

    def _on_canvas_drag_begin(self, gesture, start_x, start_y):
        # Easter egg menu dispatch (before anything else)
        if self._easter_menu_visible:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            # Check if click is on one of the egg items
            for rect, key in self._easter_menu_items:
                rx, ry, rw, rh = rect
                if rx <= start_x <= rx + rw and ry <= start_y <= ry + rh:
                    self.hide_easter_menu()
                    # Trigger the egg after a small delay so the redraw shows the menu gone
                    GLib.timeout_add(50, lambda k=key: self._trigger_easter_egg(k))
                    return
            # Click outside items → close menu
            self.hide_easter_menu()
            return

        # Chat bubble speaker icon (toggle TTS for that cat). Clicking
        # this is the ONE-STOP way to enable voice output — if the
        # global kill switch happens to be off we flip it on too,
        # because requiring the user to also dig into Settings for a
        # feature they just clicked the icon for is bad UX.
        for c in self.cat_instances:
            rect = getattr(c, "_speaker_click_rect", None)
            if rect is None:
                continue
            ix, iy, iw, ih = rect
            if ix <= start_x <= ix + iw and iy <= start_y <= iy + ih:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                c.tts_enabled = not c.tts_enabled
                c.config["tts_enabled"] = c.tts_enabled
                # Enabling a cat implicitly enables the global flag so
                # the play() hook in send_chat's on_done actually fires.
                # Disabling just flips the per-cat — the global stays
                # whatever the user set it to.
                if c.tts_enabled and not self._tts_enabled:
                    self._tts_enabled = True
                # Disabling cuts any in-flight playback synchronously
                # so the user doesn't have to wait for the current
                # response to finish speaking.
                if not c.tts_enabled:
                    try:
                        _tts.get_default_player().stop()
                    except Exception:
                        log.debug("TTS stop on mute click failed",
                                  exc_info=True)
                self._save_all()
                if self._canvas_area:
                    self._canvas_area.queue_draw()
                log.debug("TTS toggled for %s -> %s (global=%s)",
                          c.config.get("char_id"), c.tts_enabled,
                          self._tts_enabled)
                return

        # Check context menu click (2 entries: Settings / Quit)
        if self._menu_visible:
            mx, my = self._menu_x, self._menu_y
            if mx <= start_x <= mx + 120 and my <= start_y <= my + 50:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self._menu_visible = False
                if start_y < my + 25:
                    self._open_settings()
                else:
                    self.quit()
                return
            self._menu_visible = False

        cat = self._find_cat_at(start_x, start_y)
        if not cat:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._drag_cat = cat
        cat.dragging = True
        cat.mouse_moved = False
        cat.drag_win_x = cat.x
        cat.drag_win_y = cat.y
        # Petting detection: schedule a "check still stationary" callback
        # after PETTING_THRESHOLD_MS. If the user hasn't moved by then, we
        # enter petting mode. Cancelled by _on_canvas_drag_update on move.
        self._cancel_petting_timer()
        self._petting_timer_id = GLib.timeout_add(
            PETTING_THRESHOLD_MS, self._try_enter_petting, cat
        )

    def _on_canvas_drag_update(self, gesture, offset_x, offset_y):
        cat = self._drag_cat
        if not cat or not cat.dragging:
            return
        if abs(offset_x) > 3 or abs(offset_y) > 3:
            cat.mouse_moved = True
            # Real drag — cancel any pending petting trigger
            self._cancel_petting_timer()
            # If we were already petting, a drag 'pulls out' of petting mode
            if getattr(cat, "_petting_active", False):
                self._exit_petting(cat)
        cat.x = max(0, min(cat.drag_win_x + offset_x, cat.screen_w - cat.display_w))
        cat.y = max(0, min(cat.drag_win_y + offset_y, cat.screen_h - cat.display_h))
        # Force immediate redraw for smooth drag
        if self._canvas_area:
            self._canvas_area.queue_draw()

    def _on_canvas_drag_end(self, gesture, offset_x, offset_y):
        cat = self._drag_cat
        self._cancel_petting_timer()
        if cat:
            was_petting = getattr(cat, "_petting_active", False)
            cat.dragging = False
            if was_petting:
                # End petting gracefully — no chat toggle, just restore state
                self._exit_petting(cat)
            elif not cat.mouse_moved:
                # Short press, no movement → treat as click → toggle chat
                self._toggle_chat_for(cat)
        self._drag_cat = None

    # ── Petting (long-press on a stationary cat) ─────────────────────────────

    def _cancel_petting_timer(self) -> None:
        tid = getattr(self, "_petting_timer_id", None)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
            self._petting_timer_id = None

    def _try_enter_petting(self, cat) -> bool:
        """Called by GLib timeout PETTING_THRESHOLD_MS after a drag began.
        If the user hasn't moved the mouse and the cat is still the active
        drag target, commit to petting mode."""
        self._petting_timer_id = None
        if cat is not self._drag_cat:
            return False
        if cat.mouse_moved:
            return False
        if cat.in_encounter or getattr(cat, "_rm_rf_active", False):
            return False
        # Enter petting mode
        cat._petting_active = True
        cat._petting_prev_state = cat.state
        cat.state = CatState.LOVE  # hearts auto-drawn by the canvas
        cat.frame_index = 0
        # Show a short purr bubble — pool-backed so it varies across pets
        try:
            text = self._reaction_pool.get(cat, ReactionPool.EVT_PETTING)
        except Exception:
            text = L10n.s("petting_purr")
        cat.meow_text = text
        cat.meow_visible = True
        # Keep the bubble up until the user releases — refresh every 2.5s
        def refresh():
            if not getattr(cat, "_petting_active", False):
                return False
            try:
                cat.meow_text = self._reaction_pool.get(cat, ReactionPool.EVT_PETTING)
            except Exception:
                pass
            return True
        if cat._meow_timer_id:
            try:
                GLib.source_remove(cat._meow_timer_id)
            except Exception:
                pass
        cat._meow_timer_id = GLib.timeout_add(2500, refresh)
        # Mood boost hook — no-op until the mood system lands
        hook = getattr(self, "_on_petting_start", None)
        if callable(hook):
            try:
                hook(cat)
            except Exception:
                log.exception("petting start hook failed")
        log.info("petting: start %s", cat.config.get("name"))
        return False  # one-shot timer

    def _exit_petting(self, cat) -> None:
        """Release a cat from petting mode — restore its previous state and
        clear the purr bubble."""
        if not getattr(cat, "_petting_active", False):
            return
        _metrics.track("pet_session", cat_id=cat.config.get("char_id"))
        cat._petting_active = False
        # Stop the refresh timer
        if cat._meow_timer_id:
            try:
                GLib.source_remove(cat._meow_timer_id)
            except Exception:
                pass
            cat._meow_timer_id = None
        cat.meow_visible = False
        cat.meow_text = ""
        # Restore the pre-petting state (usually IDLE / WALKING)
        prev = getattr(cat, "_petting_prev_state", CatState.IDLE)
        cat.state = prev
        cat.frame_index = 0
        # Mood hook for the end of petting (duration-aware mood boost later)
        hook = getattr(self, "_on_petting_end", None)
        if callable(hook):
            try:
                hook(cat)
            except Exception:
                log.exception("petting end hook failed")
        log.info("petting: end %s", cat.config.get("name"))

    # ── Tick callbacks ───────────────────────────────────────────────────────

    def _gc_collect(self):
        gc.collect(0)
        return True

    def do_shutdown(self):
        """Clean shutdown: stop timers, cleanup cats, close windows."""
        if self._active_encounter:
            self._active_encounter.cancel()
            self._active_encounter = None
        # Final mood save so we don't lose 0-60 s of stat drift
        try:
            self._save_all_moods()
        except Exception:
            pass
        # Flush metrics session minutes — no-op if metrics disabled
        try:
            _metrics.shutdown()
        except Exception:
            pass
        # Release the Whisper CUDA model BEFORE Python/GTK teardown so
        # ctranslate2 doesn't crash with "CUDA driver shutting down" when
        # the user Ctrl+C's during the ~2 s preload window.
        if self._voice_recorder is not None:
            try:
                self._voice_recorder._model = None
            except Exception:
                pass
        # Tear down the wake-word listener so we release autoaudiosrc
        # and don't leak the worker thread.
        if self._wake is not None:
            try:
                self._wake.stop()
            except Exception:
                log.debug("WAKE: shutdown stop failed", exc_info=True)
            self._wake = None
        if getattr(self, "_wake_ptt_stop_timer", None):
            try:
                GLib.source_remove(self._wake_ptt_stop_timer)
            except Exception:
                pass
            self._wake_ptt_stop_timer = None
        for tid in self._timers:
            GLib.source_remove(tid)
        self._timers.clear()
        for cat in self.cat_instances:
            cat.cleanup()
        self.cat_instances.clear()
        # Release sprite / surface caches
        _surface_cache.clear()
        load_sprite.cache_clear()
        self._chat_box.set_visible(False)
        if self._canvas_window:
            unregister_window(self._canvas_window)
        if self.settings_ctrl and self.settings_ctrl.window:
            self.settings_ctrl._stop_timers()
            self.settings_ctrl.window.set_visible(False)
        Gtk.Application.do_shutdown(self)


    def _create_instance(self, config, index):
        """Instantiate a catset character (all configs are catset-based since v0.3.0)."""
        char_id = config.get("char_id")
        if not char_id:
            log.warning("Config without char_id — skipping: %r", config)
            return
        # External character pack? Use its absolute sprite directory.
        # Bundled cats live under the package directory keyed by char_id.
        perso = CATSET_PERSONALITIES.get(char_id, {})
        ext_dir = _character_packs.external_sprite_dir(perso)
        if ext_dir:
            char_dir = ext_dir
        else:
            pkg_dir = os.path.dirname(os.path.abspath(__file__))
            char_dir = os.path.join(pkg_dir, char_id)
        if not os.path.isdir(char_dir):
            log.warning("Catset dir not found: %s — skipping", char_dir)
            return
        meta = load_metadata(char_dir)
        sprite_w = meta["character"]["size"]["width"]
        sprite_h = meta["character"]["size"]["height"]
        dw = int(round(sprite_w * self.cat_scale))
        dh = int(round(sprite_h * self.cat_scale))
        inst = CatInstance(config)
        start_x = random.randint(int(dw), int(self.screen_w - dw * 2))
        start_x = max(0, min(start_x, self.screen_w - dw))
        inst.setup(self, meta, char_dir,
                   dw, dh,
                   self.selected_model, L10n.lang,
                   start_x, self.screen_w, self.screen_h)
        # Multi-monitor spawn: override the post-setup (x, y) with our
        # planned round-robined point so cats don't all start on monitor 0.
        plan = getattr(self, "_spawn_plan", None)
        if plan and index < len(plan):
            inst.x, inst.y = plan[index]
            inst.dest_x = inst.x
            inst.dest_y = inst.y
        self.cat_instances.append(inst)

    def _save_all(self):
        save_config({
            "version": 1,
            "cats": self.cat_configs,
            "scale": self.cat_scale,
            "model": self.selected_model,
            "lang": L10n.lang,
            "encounters": self.encounters_enabled,
            "voice_enabled": self._voice_enabled,
            "voice_model": getattr(self, "_voice_model", "base"),
            "tts_enabled": getattr(self, "_tts_enabled", False),
            "tts_cat_sounds_enabled": getattr(self, "_tts_cat_sounds_enabled", True),
            "auto_update": getattr(self, "_auto_update_mode", _updater.MODE_AUTO),
            "metrics_enabled": getattr(self, "_metrics_enabled", False),
            "api_enabled": getattr(self, "_api_enabled", False),
            "long_term_memory": getattr(self, "_long_term_memory_enabled", True),
            "wake_word_enabled": getattr(self, "_wake_word_enabled", False),
            "wake_word_ack_sound": getattr(self, "_wake_ack_sound", True),
        })

    def _render_tick(self):
        t0 = time.monotonic()
        for cat in self.cat_instances:
            try:
                cat.render_tick()
            except Exception:
                log.exception("render_tick crashed for %s", cat.config.get("char_id", "?"))
        self._check_encounters()
        # Reposition chat entry if following a walking cat
        if self._active_chat_cat and self._chat_box.get_visible():
            self._position_chat_entry(self._active_chat_cat)
        # Redraw the canvas
        if self._canvas_area:
            self._canvas_area.queue_draw()
        # Update XShape input regions
        self._update_input_regions()
        flush_x11()
        dt = (time.monotonic() - t0) * 1000
        if dt > 20:
            log.warning("Slow render: %.0fms (%d cats)", dt, len(self.cat_instances))
        return True

    def _behavior_tick(self):
        # Activity monitor update is cheap (internally throttled to 2s),
        # call it every tick so AFK detection feels responsive.
        try:
            self._activity.update()
            self._apply_activity_signals()
        except Exception:
            log.exception("activity monitor update failed")

        for cat in self.cat_instances:
            try:
                cat.behavior_tick()
            except Exception:
                log.exception("behavior_tick crashed for %s", cat.config.get("char_id", "?"))
        return True

    def _apply_activity_signals(self) -> None:
        """Translate ActivityMonitor state into cat behavior. Edge-triggered
        so a cat that's already sleeping doesn't get re-forced every tick."""
        afk_now = self._activity.is_afk
        if afk_now and not self._afk_sleep_active:
            # Transition INTO AFK → send everyone to sleep
            for cat in self.cat_instances:
                if cat.in_encounter or cat.dragging:
                    continue
                if getattr(cat, "_petting_active", False):
                    continue
                cat.state = CatState.SLEEPING_BALL
                cat.frame_index = 0
                cat._sleep_tick = 0
                cat.direction = "south"
                cat.in_encounter = True  # freeze them until we wake them up
            self._afk_sleep_active = True
            log.info("activity: user AFK — cats are sleeping")
        elif not afk_now and self._afk_sleep_active:
            # Transition OUT of AFK → wake everyone up with a brief surprise
            for cat in self.cat_instances:
                if not cat.in_encounter:
                    continue
                cat.state = CatState.WAKING_UP
                cat.frame_index = 0
                cat.in_encounter = False
                cat.idle_ticks = 0
            self._afk_sleep_active = False
            log.info("activity: user back — cats waking up")

    def _check_encounters(self):
        """Detect two nearby cats and maybe start an encounter (called at render rate)."""
        if not self.encounters_enabled or self._active_encounter:
            return
        now = time.monotonic()
        # Throttle to once every 250ms to avoid spamming but keep it reactive
        if now - self._last_encounter_check < 0.25:
            return
        self._last_encounter_check = now

        ok_states = {CatState.IDLE, CatState.WALKING}
        candidates = [c for c in self.cat_instances
                      if not c.in_encounter and not c.chat_visible
                      and not c.dragging and c.state in ok_states
                      and not c.is_kitten  # kittens don't have encounters
                      and c._birth_progress is None  # being born
                      and now >= c._encounter_cooldown_until]
        if len(candidates) < 2:
            return
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                ca, cb = candidates[i], candidates[j]
                # Euclidean distance (cats can overlap in Y too)
                dist = ((ca.x - cb.x) ** 2 + (ca.y - cb.y) ** 2) ** 0.5
                if dist < CatEncounter.PROXIMITY and random.random() < 0.30:
                    # Stop both cats where they are
                    ca.state = CatState.IDLE
                    cb.state = CatState.IDLE
                    # 40% of encounters are silent love encounters (maybe baby!)
                    if random.random() < 0.40:
                        enc = LoveEncounter(ca, cb, self)
                        log.debug("Love encounter: %s ↔ %s", ca.config["name"], cb.config["name"])
                    else:
                        enc = CatEncounter(ca, cb, self)
                        log.debug("Encounter started: %s ↔ %s (dist=%.0f)",
                                  ca.config["name"], cb.config["name"], dist)
                    self._active_encounter = enc
                    enc.start()
                    return

    def set_encounters_enabled(self, enabled):
        self.encounters_enabled = enabled
        if not enabled and self._active_encounter:
            self._active_encounter.cancel()
            self._active_encounter = None
        self._save_all()

    # ── Apocalypse mode ───────────────────────────────────────────────────────
    # Triggered by typing "Don't panic" in any chat bubble (HGttG reference).
    # Each cat doubles every second until MAX_APOCALYPSE cats are on screen.
    # Typing "Don't panic" again stops it and removes the clones.

    APOCALYPSE_MAX = 1000
    APOCALYPSE_INTERVAL_MS = 1000
    APOCALYPSE_BATCH_SIZE = 10  # spawn up to N clones per idle slice
    APOCALYPSE_AUTO_STOP_MS = 30000  # auto-end after 30s

    def toggle_apocalypse(self, triggering_cat=None):
        if getattr(self, "_apocalypse_active", False):
            self.stop_apocalypse()
        else:
            self.start_apocalypse()

    def start_apocalypse(self):
        if getattr(self, "_apocalypse_active", False):
            return
        self._apocalypse_active = True
        log.warning("\U0001f680 APOCALYPSE MODE ACTIVATED — don't panic!")
        self._apocalypse_timer = GLib.timeout_add(self.APOCALYPSE_INTERVAL_MS, self._apocalypse_tick)
        # Auto-stop after APOCALYPSE_AUTO_STOP_MS
        self._apocalypse_auto_stop_timer = GLib.timeout_add(
            self.APOCALYPSE_AUTO_STOP_MS, lambda: (self.stop_apocalypse(), False)[1]
        )

    def stop_apocalypse(self):
        if not getattr(self, "_apocalypse_active", False):
            return
        self._apocalypse_active = False
        for attr in ("_apocalypse_timer", "_apocalypse_auto_stop_timer"):
            tid = getattr(self, attr, None)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                setattr(self, attr, None)
        # Remove all apocalypse clones
        removed = 0
        remaining = []
        for cat in self.cat_instances:
            if getattr(cat, "is_apocalypse_clone", False):
                cat.cleanup()
                removed += 1
            else:
                remaining.append(cat)
        self.cat_instances = remaining
        log.info("Apocalypse stopped — removed %d clones", removed)

    def _apocalypse_tick(self):
        """Every tick, double the population — but queue the actual spawns
        to be processed lazily via GLib.idle_add to avoid blocking."""
        if not getattr(self, "_apocalypse_active", False):
            return False
        if len(self.cat_instances) >= self.APOCALYPSE_MAX:
            log.info("Apocalypse cap (%d) reached, auto-stopping spawns", self.APOCALYPSE_MAX)
            return False
        # Build a spawn queue: snapshot current cats, each spawns 1 clone
        parents = list(self.cat_instances)
        if not hasattr(self, "_apocalypse_queue"):
            self._apocalypse_queue = []
        self._apocalypse_queue.extend(parents)
        # Kick off a lazy spawner if not already running
        if not getattr(self, "_apocalypse_spawning", False):
            self._apocalypse_spawning = True
            GLib.idle_add(self._apocalypse_drain)
        return True

    def _apocalypse_drain(self):
        """Process a few spawns per idle slot so the UI stays responsive."""
        if not getattr(self, "_apocalypse_active", False):
            self._apocalypse_spawning = False
            self._apocalypse_queue = []
            return False
        for _ in range(self.APOCALYPSE_BATCH_SIZE):
            if not self._apocalypse_queue:
                break
            if len(self.cat_instances) >= self.APOCALYPSE_MAX:
                self._apocalypse_queue = []
                break
            parent = self._apocalypse_queue.pop(0)
            # Parent may have been removed (if user stopped)
            if parent not in self.cat_instances:
                continue
            self._spawn_apocalypse_clone(parent)
        if self._apocalypse_queue:
            return True  # keep draining on next idle slot
        self._apocalypse_spawning = False
        return False

    def _spawn_apocalypse_clone(self, parent):
        """Spawn a lightweight clone that SHARES sprite surfaces with its parent.
        Avoids the expensive load_assets() call that makes naive cloning unbearably slow."""
        cfg = {
            "id": f"apoc_{uuid.uuid4().hex[:8]}",
            "char_id": parent.config.get("char_id", "cat_orange"),
            "name": "Clone",
        }
        clone = CatInstance(cfg)
        # Copy display metrics from parent
        clone.display_w = parent.display_w
        clone.display_h = parent.display_h
        clone.screen_w = parent.screen_w
        clone.screen_h = parent.screen_h
        clone._app = parent._app
        clone._meta = parent._meta
        clone._cat_dir = parent._cat_dir
        # Share surfaces (same dict refs — no copying)
        clone.animations = parent.animations
        clone.rotations = parent.rotations
        clone._anim_offsets = parent._anim_offsets
        clone._sprite_bottom_padding = parent._sprite_bottom_padding
        # Minimal state
        clone.state = CatState.IDLE
        clone.direction = "south"
        clone.frame_index = 0
        clone.x = parent.x + random.randint(-60, 60)
        clone.y = parent.y + random.randint(-40, 40)
        clone.dest_x = clone.x
        clone.dest_y = clone.y
        clone.is_kitten = True
        clone.is_apocalypse_clone = True
        clone._birth_progress = 0.0
        clone.chat_backend = None  # no chat for clones (saves memory)
        clone._clamp_to_screen()
        self.cat_instances.append(clone)

    # ── Easter egg menu ──────────────────────────────────────────────────────

    _EASTER_MENU_COLS = 3
    _EASTER_MENU_CELL_W = 180
    _EASTER_MENU_CELL_H = 38
    _EASTER_MENU_PAD = 20
    _EASTER_MENU_TITLE_H = 36
    _EASTER_MENU_FOOTER_H = 28

    # Easter egg methods (show_easter_menu, hide_easter_menu,
    # _trigger_easter_egg, _release_encounter_lock, all eg_* methods,
    # _draw_nyan, _draw_rm_rf_wipe, _draw_easter_menu, _get_caps_lock_state,
    # _check_caps_lock, _is_any_fullscreen, _check_fullscreen,
    # _read_system_uptime, _format_uptime) are provided by EasterEggMixin.

    # ── Auto-update (#24) ────────────────────────────────────────────────

    def _auto_update_worker(self):
        """Background thread: poll GitHub releases, optionally install,
        then bubble-notify the user from the GTK main thread."""
        try:
            time.sleep(5)  # let the app finish booting before any net call
            result = _updater.check_for_update()
            if result is None:
                log.debug("updater: no update available")
                return
            installed, latest = result
            log.info("updater: %s available (installed=%s, mode=%s)",
                     latest, installed, self._auto_update_mode)
            if self._auto_update_mode == _updater.MODE_AUTO:
                ok = _updater.install_update_blocking()
                if ok:
                    GLib.idle_add(self._on_update_installed, installed, latest)
                else:
                    GLib.idle_add(self._on_update_failed, latest)
            else:  # notify
                GLib.idle_add(self._on_update_available, installed, latest)
        except Exception:
            log.exception("auto update worker crashed")

    def _meow_first_cat(self, text: str) -> None:
        """Helper: show a transient meow bubble on the first available
        cat. Used for update notifications."""
        if not self.cat_instances:
            return
        cat = self.cat_instances[0]
        cat.meow_text = text
        cat.meow_visible = True
        # Cancel any existing meow timer for this cat then schedule a
        # 12 s auto-hide so the bubble doesn't linger forever.
        tid = getattr(cat, "_meow_timer_id", None)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        def _hide():
            cat.meow_visible = False
            cat._meow_timer_id = None
            return False
        cat._meow_timer_id = GLib.timeout_add(12000, _hide)

    def _on_update_installed(self, old: str, new: str) -> bool:
        """Auto mode: pip already finished, the user just needs to
        relaunch CATAI to pick up the new code."""
        self._meow_first_cat(f"Mise à jour {new} prête!")
        log.info("updater: installed %s -> %s, restart on next launch", old, new)
        return False

    def _on_update_available(self, old: str, new: str) -> bool:
        """Notify mode: a meow bubble lets the user know."""
        self._meow_first_cat(f"Mise à jour dispo: {new}")
        log.info("updater: notified user about %s", new)
        return False

    def _on_update_failed(self, new: str) -> bool:
        """Auto mode: install failed, fall back to a notify bubble."""
        self._meow_first_cat(f"Mise à jour {new} : échec install")
        log.warning("updater: install of %s failed", new)
        return False

    def _check_theme(self) -> bool:
        """Poll GNOME dark-mode preference; on flip, swap the bubble palette
        in `catai_linux.drawing.THEME`. Returns True to keep the timer."""
        try:
            now_dark = _is_dark_mode()
            if now_dark != getattr(self, "_dark_mode", False):
                self._dark_mode = now_dark
                _set_theme(dark=now_dark)
                log.info("Theme flipped: dark=%s", now_dark)
        except Exception:
            log.exception("theme poll crashed")
        return True

    def add_catset_char(self, char_id):
        if any(c.get("char_id") == char_id for c in self.cat_configs):
            return
        p = CATSET_PERSONALITIES.get(char_id, CATSET_PERSONALITIES["cat01"])
        name = p["name"].get(L10n.lang, p["name"]["fr"])
        cfg = {"id": f"cat_{uuid.uuid4().hex[:8]}", "char_id": char_id, "name": name}
        self.cat_configs.append(cfg)
        self._save_all()
        self._create_instance(cfg, len(self.cat_instances))
        if self.settings_ctrl:
            self.settings_ctrl.selected_char_id = char_id
            self.settings_ctrl.refresh()

    def remove_catset_char(self, char_id):
        if len(self.cat_configs) <= 1:
            return
        idx = next((i for i, c in enumerate(self.cat_instances) if c.config.get("char_id") == char_id), None)
        if idx is None:
            return
        cat = self.cat_instances[idx]
        if self._active_chat_cat is cat:
            cat.chat_visible = False
            self._chat_box.set_visible(False)
            self._active_chat_cat = None
        cat.cleanup()
        self.cat_instances.pop(idx)
        self.cat_configs = [c for c in self.cat_configs if c.get("char_id") != char_id]
        delete_memory(cat.config["id"])
        self._save_all()
        if self.settings_ctrl:
            if self.settings_ctrl.selected_char_id == char_id:
                self.settings_ctrl.selected_char_id = None
            self.settings_ctrl.refresh()

    def rename_catset_char(self, char_id, name):
        for cfg in self.cat_configs:
            if cfg.get("char_id") == char_id:
                cfg["name"] = name; break
        self._save_all()
        for inst in self.cat_instances:
            if inst.config.get("char_id") == char_id:
                inst.config["name"] = name
                inst.update_system_prompt(L10n.lang)
        # Refresh the wake-word grammar so the renamed cat answers to
        # the new name immediately (and forgets the old one). No-op
        # when the listener is None.
        if getattr(self, "_wake", None):
            self._wake.set_names({
                c.get("char_id"): c.get("name", "")
                for c in self.cat_configs if c.get("char_id")
            })

    def apply_new_scale(self, s):
        self.cat_scale = s
        self._save_all()
        for cat in self.cat_instances:
            # Each cat knows its own meta/cat_dir; dw/dh scaled from its sprite size
            sw = cat._meta["character"]["size"]["width"]
            sh = cat._meta["character"]["size"]["height"]
            cat.apply_scale(int(round(sw * s)), int(round(sh * s)))

    def set_language(self, lang):
        L10n.lang = lang
        self._save_all()
        for cat in self.cat_instances:
            cat.update_system_prompt(lang)
        # Update chat bubble placeholder
        if self._chat_entry:
            self._chat_entry.set_placeholder_text(L10n.s("talk"))
        if self.settings_ctrl:
            self.settings_ctrl.refresh()

    def set_model(self, model):
        self.selected_model = model
        self._save_all()
        for cat in self.cat_instances:
            if not cat.chat_backend:
                continue
            if cat.chat_backend.is_streaming:
                cat.chat_backend.model = model
                continue
            if model.startswith("claude-") and not isinstance(cat.chat_backend, ClaudeChat):
                cat.setup_chat(model, L10n.lang)
            elif not model.startswith("claude-") and isinstance(cat.chat_backend, ClaudeChat):
                cat.setup_chat(model, L10n.lang)
            else:
                cat.chat_backend.model = model

    def _open_settings(self):
        if not self.settings_ctrl:
            self.settings_ctrl = SettingsWindow(self)
        ctrl = self.settings_ctrl
        ctrl.get_configs = lambda: self.cat_configs
        ctrl.get_catset_preview = self._get_catset_preview
        ctrl.on_add_catset = self.add_catset_char
        ctrl.on_remove_catset = self.remove_catset_char
        ctrl.on_rename_catset = self.rename_catset_char
        ctrl.on_scale_changed = self.apply_new_scale
        ctrl.on_model_changed = self.set_model
        ctrl.on_lang_changed = self.set_language
        ctrl.on_encounters_changed = self.set_encounters_enabled
        ctrl.setup(self.cat_scale, self.selected_model)
        ctrl.show()

    def _get_catset_preview(self, char_id):
        """Get idle south sprite for a catset character."""
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        char_dir = os.path.join(pkg_dir, char_id)
        if not os.path.isdir(char_dir):
            return None
        try:
            meta = load_metadata(char_dir)
            south_rel = meta["frames"]["rotations"].get("south")
            if not south_rel:
                return None
            return Image.open(os.path.join(char_dir, south_rel)).convert("RGBA")
        except Exception:
            return None


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    import signal
    gtk_args = [a for a in sys.argv if a not in ("--debug", "--test-socket", "--voice")]
    app = CatAIApp()
    # Ctrl+C → graceful quit (triggers do_shutdown for proper cleanup
    # of Whisper semaphores, wake word listener, metrics flush, etc.)
    # os._exit fallback if quit itself crashes (CUDA teardown race).
    def _sigint(*_):
        try:
            app.quit()
        except Exception:
            os._exit(0)
    signal.signal(signal.SIGINT, _sigint)
    app.run(gtk_args)

if __name__ == "__main__":
    main()
