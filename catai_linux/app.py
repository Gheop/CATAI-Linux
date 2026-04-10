#!/usr/bin/env python3
"""CATAI-Linux — Virtual desktop pet cats for Linux (GNOME/Wayland)
Port of https://github.com/wil-pe/CATAI (macOS) to GTK4.
Single fullscreen transparent canvas with Cairo rendering.
"""

# Force X11 backend — needed for XShape input passthrough + chat bubble positioning
import math
import os
os.environ.setdefault("GDK_BACKEND", "x11")

import cairo
import enum
import gc
import json
import logging
import random
import shutil
import subprocess
import time
import sys
import threading
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

# ── Constants ──────────────────────────────────────────────────────────────────

RENDER_MS = 125        # 8 FPS
BEHAVIOR_MS = 1000     # 1 Hz
WALK_SPEED = 4
PETTING_THRESHOLD_MS = 800  # long-press duration to enter petting mode
DEFAULT_SCALE = 1.5
MIN_SCALE = 0.5
MAX_SCALE = 4.0
CONFIG_DIR = os.path.expanduser("~/.config/catai")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


class CatState(enum.Enum):
    IDLE = "idle"
    WALKING = "walking"
    EATING = "eating"                # transitory state while AI is thinking
    ANGRY = "angry"
    SLEEPING = "sleeping"
    WAKING_UP = "waking_up"
    SOCIALIZING = "socializing"      # frozen during cat-to-cat encounter
    SLEEPING_BALL = "sleeping_ball"  # curled in a ball, breathing slowly
    CHASING_MOUSE = "chasing_mouse"
    # catset-specific states
    FLAT = "flat"
    LOVE = "love"
    GROOMING = "grooming"
    ROLLING = "rolling"
    SURPRISED = "surprised"
    JUMPING = "jumping"
    CLIMBING = "climbing"
    DASHING = "dashing"
    DYING = "dying"
    FALLING = "falling"
    HURTING = "hurting"
    LANDING = "landing"
    LEDGECLIMB_STRUGGLE = "ledgeclimb_struggle"
    LEDGEGRAB = "ledgegrab"
    LEDGEIDLE = "ledgeidle"
    WALLCLIMB = "wallclimb"
    WALLGRAB = "wallgrab"


ANIM_KEYS = {
    CatState.WALKING: "running-8-frames",
    CatState.EATING: "eating",
    CatState.ANGRY: "angry",
    CatState.WAKING_UP: "waking-getting-up",
    CatState.SLEEPING_BALL: "sleeping-ball",
    CatState.CHASING_MOUSE: "chasing-mouse",
    CatState.FLAT: "flat",
    CatState.LOVE: "love",
    CatState.GROOMING: "grooming",
    CatState.ROLLING: "rolling",
    CatState.SURPRISED: "surprised",
    CatState.JUMPING: "jumping",
    CatState.CLIMBING: "climbing",
    CatState.DASHING: "dash",
    CatState.DYING: "die",
    CatState.FALLING: "fall",
    CatState.HURTING: "hurt",
    CatState.LANDING: "land",
    CatState.LEDGECLIMB_STRUGGLE: "ledgeclimb-struggle",
    CatState.LEDGEGRAB: "ledgegrab",
    CatState.LEDGEIDLE: "ledgeidle",
    CatState.WALLCLIMB: "wallclimb",
    CatState.WALLGRAB: "wallgrab",
}
ONE_SHOT_STATES = {
    CatState.EATING, CatState.ANGRY, CatState.WAKING_UP,
    CatState.CHASING_MOUSE,
    CatState.FLAT, CatState.LOVE, CatState.GROOMING, CatState.ROLLING,
    CatState.SURPRISED, CatState.JUMPING, CatState.CLIMBING,
    CatState.FALLING,
    CatState.LANDING, CatState.LEDGECLIMB_STRUGGLE, CatState.LEDGEGRAB,
    CatState.WALLCLIMB,
}


# ── Sequences ─────────────────────────────────────────────────────────────────

class SequenceStep:
    __slots__ = ('state', 'direction_mode', 'pause_after', 'loop_count')
    def __init__(self, state, direction_mode="inherit", pause_after=0, loop_count=1):
        self.state = state
        self.direction_mode = direction_mode  # "inherit" | "south"
        self.pause_after = pause_after        # ticks to hold on last frame
        self.loop_count = loop_count          # how many times to play

SEQUENCES = {
    "wall_adventure": [
        SequenceStep(CatState.WALLCLIMB),
        SequenceStep(CatState.WALLGRAB, pause_after=8),
        SequenceStep(CatState.FALLING, direction_mode="south"),
        SequenceStep(CatState.LANDING, direction_mode="south"),
    ],
    "ledge_adventure": [
        SequenceStep(CatState.LEDGEGRAB),
        SequenceStep(CatState.LEDGEIDLE, loop_count=2),
        SequenceStep(CatState.LEDGECLIMB_STRUGGLE),
        SequenceStep(CatState.CLIMBING),
    ],
    "dash_crash": [
        SequenceStep(CatState.DASHING),
        SequenceStep(CatState.HURTING, direction_mode="south"),
    ],
    "full_jump": [
        SequenceStep(CatState.JUMPING, direction_mode="south"),
        SequenceStep(CatState.FALLING, direction_mode="south"),
        SequenceStep(CatState.LANDING, direction_mode="south"),
    ],
    "drama_queen": [
        SequenceStep(CatState.HURTING, direction_mode="south"),
        SequenceStep(CatState.DYING, direction_mode="south"),
        SequenceStep(CatState.HURTING, direction_mode="south", loop_count=3),
        SequenceStep(CatState.WAKING_UP, direction_mode="south"),
    ],
}

from catai_linux.l10n import L10n  # noqa: E402

# ── Cat Personalities ──────────────────────────────────────────────────────────

class _CatsetMarker:
    """Sentinel marker indicating a cat uses pre-colored catset sprites
    (set on every CatInstance.color_def since v0.3.0 — legacy color-tinting
    system was removed in v0.5.0)."""
    id = "catset"

# Every cat's .color_def points to this singleton
_CATSET_COLOR_DEF = _CatsetMarker()

CATSET_CHARS = [
    ("cat_orange", "🟠"),
    ("cat01",      "🟤"),
    ("cat02",      "⬛"),
    ("cat03",      "🟫"),
    ("cat04",      "🩶"),
    ("cat05",      "🖤"),
]

# Parent cat → child kitten (for love-encounter births)
CAT_TO_KITTEN = {
    "cat_orange": "kitten_orange",
    "cat01":      "kitten01",
    "cat02":      "kitten02",
    "cat03":      "kitten03",
    "cat04":      "kitten04",
    "cat05":      "kitten05",
}

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

MAX_KITTENS = 6

# Small safety margin at the bottom for sub-pixel rendering. The real
# offset for the GNOME top bar is computed per-cat via _canvas_y_offset.
BOTTOM_MARGIN = 5

# ── Easter eggs ──────────────────────────────────────────────────────────────
# Triggered by typing "easter egg" in any chat bubble. Shows a clickable menu.
# (key, emoji, label, method_name)
# Magic phrases that trigger an easter egg directly from the chat bubble.
# Case-insensitive, trailing punctuation stripped. Multiple aliases allowed.
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
]

CATSET_PERSONALITIES = {
    "cat_orange": {
        "name": {"fr": "Mandarine", "en": "Tangerine", "es": "Mandarina"},
        "traits": {"fr": "espiègle et joueur", "en": "mischievous and playful", "es": "travieso y juguetón"},
        "skills": {"fr": "Tu adores faire des bêtises et tu es toujours en mouvement.",
                   "en": "You love getting into mischief and are always on the move.",
                   "es": "Te encanta hacer travesuras y siempre estás en movimiento."},
    },
    "cat01": {
        "name": {"fr": "Tabby", "en": "Tabby", "es": "Tabby"},
        "traits": {"fr": "curieux et aventurier", "en": "curious and adventurous", "es": "curioso y aventurero"},
        "skills": {"fr": "Tu explores chaque recoin et poses beaucoup de questions.",
                   "en": "You explore every corner and ask lots of questions.",
                   "es": "Exploras cada rincón y haces muchas preguntas."},
    },
    "cat02": {
        "name": {"fr": "Ombre", "en": "Shadow", "es": "Sombra"},
        "traits": {"fr": "mystérieux et silencieux", "en": "mysterious and silent", "es": "misterioso y silencioso"},
        "skills": {"fr": "Tu observes tout sans rien dire et parles peu, mais avec profondeur.",
                   "en": "You observe everything silently and speak rarely but deeply.",
                   "es": "Observas todo en silencio y hablas poco, pero con profundidad."},
    },
    "cat03": {
        "name": {"fr": "Noisette", "en": "Hazel", "es": "Avellana"},
        "traits": {"fr": "doux et réconfortant", "en": "gentle and comforting", "es": "dulce y reconfortante"},
        "skills": {"fr": "Tu aimes câliner et remonter le moral de tout le monde.",
                   "en": "You love to cuddle and cheer everyone up.",
                   "es": "Te encanta mimar y animar a todos."},
    },
    "cat04": {
        "name": {"fr": "Brume", "en": "Mist", "es": "Niebla"},
        "traits": {"fr": "sage et philosophe", "en": "wise and philosophical", "es": "sabio y filosófico"},
        "skills": {"fr": "Tu médites et partages des pensées profondes sur la vie.",
                   "en": "You meditate and share deep thoughts about life.",
                   "es": "Meditas y compartes pensamientos profundos sobre la vida."},
    },
    "cat05": {
        "name": {"fr": "Minuit", "en": "Midnight", "es": "Medianoche"},
        "traits": {"fr": "élégant et nocturne", "en": "elegant and nocturnal", "es": "elegante y nocturno"},
        "skills": {"fr": "Tu es à ton aise dans l'obscurité et racontes des histoires mystérieuses.",
                   "en": "You're at home in the dark and tell mysterious stories.",
                   "es": "Te sientes a gusto en la oscuridad y cuentas historias misteriosas."},
    },
}

def _catset_prompt(char_id, name, lang):
    p = CATSET_PERSONALITIES.get(char_id, CATSET_PERSONALITIES["cat01"])
    t = p["traits"].get(lang, p["traits"]["fr"])
    sk = p["skills"].get(lang, p["skills"]["fr"])
    if lang == "en":
        return f"You are a little {t} cat named {name}. {sk} Respond briefly with cat sounds (meow, purr, mrrp). Max 2-3 sentences."
    elif lang == "es":
        return f"Eres un gatito {t} llamado {name}. {sk} Responde brevemente con sonidos de gato (miau, purr, mrrp). Máximo 2-3 frases."
    return f"Tu es un petit chat {t} nommé {name}. {sk} Réponds brièvement avec des sons de chat (miaou, purr, mrrp). Max 2-3 phrases."

# ── Persistence ────────────────────────────────────────────────────────────────

def _ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)

def load_config():
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

def save_config(cfg):
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

def _sprite_floor_y(img):
    """Return Y of the lowest non-transparent pixel in a PIL RGBA image (sprite 'floor')."""
    data = img.load()
    w, h = img.size
    for y in range(h - 1, -1, -1):
        for x in range(w):
            if data[x, y][3] > 10:
                return y
    return h - 1


def _sprite_center_x(img):
    """Return X centroid of non-transparent pixels."""
    data = img.load()
    w, h = img.size
    xs = [x for y in range(h) for x in range(w) if data[x, y][3] > 10]
    return sum(xs) / len(xs) if xs else w / 2.0


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


def pil_to_surface(img, target_w, target_h):
    """Convert PIL Image to cairo.ImageSurface, scaled nearest-neighbor.
    Returns (surface, data_ref) — data_ref MUST be kept alive while surface is used."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    scaled = img.resize((target_w, target_h), Image.NEAREST)
    data = bytearray(scaled.tobytes())
    # Swap RGBA -> BGRA for cairo ARGB32 (little-endian)
    for i in range(0, len(data), 4):
        data[i], data[i+2] = data[i+2], data[i]
    surface = cairo.ImageSurface.create_for_data(
        data, cairo.FORMAT_ARGB32, target_w, target_h, target_w * 4)
    return surface, data  # caller must keep data alive


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


def load_sprite(path):
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
)

from catai_linux.reactions import ReactionPool  # noqa: E402
from catai_linux.mood import CatMood  # noqa: E402
from catai_linux.activity import ActivityMonitor  # noqa: E402
from catai_linux import monitors as _monitors_mod  # noqa: E402


from catai_linux.chat_backend import (  # noqa: E402
    CLAUDE_MODEL, MEM_MAX,
    ClaudeChat, create_chat,
    claude_available, fetch_ollama_models, _ollama_available,
)


from catai_linux.drawing import (  # noqa: E402
    apply_css, BUBBLE_FONT as _BUBBLE_FONT,
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


# ── Cat Instance ───────────────────────────────────────────────────────────────

class CatInstance:
    def __init__(self, config, color_def_obj):
        self.config = config
        self.color_def = color_def_obj
        self.rotations = {}      # dir_name -> (cairo.ImageSurface, data_ref)
        self.animations = {}     # anim_name -> {dir_name -> [(surface, data_ref)]}
        self.state = CatState.IDLE
        self.direction = "south"
        self.frame_index = 0
        self.idle_ticks = 0
        self.x = 0.0
        self.y = 0.0
        self.dest_x = 0.0
        self.dest_y = 0.0
        self.display_w = 0
        self.display_h = 0
        self.dragging = False
        self.drag_start_x = 0.0
        self.drag_start_y = 0.0
        self.drag_win_x = 0.0
        self.drag_win_y = 0.0
        self.mouse_moved = False
        self.chat_backend = None
        self.screen_w = 0
        self.screen_h = 0
        self._app = None
        self._meta = None
        self._cat_dir = ""
        self._anim_offsets = {}         # anim_key -> {direction -> (dy, dx)} in display-px
        self._sprite_bottom_padding = 0 # px of empty rows between sprite feet and box bottom
        self.is_kitten = False          # True for kittens born from love encounters
        self.is_apocalypse_clone = False # True for clones spawned by apocalypse mode
        self._birth_progress = None     # None = fully visible; 0.0..1.0 = birth animation
        self._flip_h = False            # Horizontal flip (override for face-each-other in encounters)
        self._sequence = None           # list[SequenceStep] or None
        self._sequence_index = 0
        self._sequence_direction = None # locked east/west for the whole sequence
        self._sequence_pause_ticks = 0
        self._sequence_loop_counter = 1
        # Meow bubble state (drawn on canvas)
        self.meow_text = ""
        self.meow_visible = False
        self._meow_timer_id = None
        # Chat bubble state (drawn on canvas, entry via overlay)
        self.chat_visible = False
        self.chat_response = ""
        # Encounter state (cat-to-cat conversation)
        self.in_encounter = False
        self.encounter_text = ""
        self.encounter_visible = False
        # Invisible mood system — loaded/created on setup() once we know
        # the cat's ID. Default sentinel prevents crashes from code paths
        # that touch mood before setup() runs.
        self.mood = CatMood()
        self._encounter_cooldown_until = 0.0  # monotonic timestamp

    def setup(self, app, meta, cat_dir, dw, dh, model, lang, start_x, screen_w, screen_h):
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
        self.chat_backend = create_chat(model)
        self.chat_backend.messages = [{"role": "system", "content": prompt}]
        mem = load_memory(self.config["id"])
        if len(mem) > 1:
            self.chat_backend.messages.extend(mem[1:])

    def load_assets(self, meta, cat_dir, lazy=True):
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
                pil = load_sprite(os.path.join(cat_dir, rel_path))
                rots[dir_name] = pil_to_surface(pil, dw, dh)
            GLib.idle_add(lambda: self.rotations.update(rots) or False)

            # 2. Running anim (needed for WALKING)
            if walk_key in meta["frames"]["animations"]:
                walk_data = {}
                for dir_name, frame_paths in meta["frames"]["animations"][walk_key].items():
                    walk_data[dir_name] = [
                        pil_to_surface(load_sprite(os.path.join(cat_dir, p)), dw, dh)
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
                            pil_to_surface(load_sprite(os.path.join(cat_dir, p)), dw, dh)
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
                            pil_to_surface(load_sprite(os.path.join(cat_dir, p)), dw, dh)
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

    def render_tick(self):
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
            for i in range(self._sequence_index + 1):
                if i >= len(self._sequence):
                    break
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

    def behavior_tick(self):
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

    def send_chat(self, text):
        if not self.chat_backend:
            # Background init still running — show a friendly hint and drop the text
            self.chat_response = "..."
            self.chat_visible = True
            return
        if self.chat_backend.is_streaming:
            return
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
        import re
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

        self.chat_backend.send(text, on_token, on_done, on_error, on_status=on_status)
        # Mood: chatting with the user bumps happiness and clears boredom.
        try:
            self.mood.on_chat_sent()
        except Exception:
            pass

    def update_system_prompt(self, lang):
        p = _catset_prompt(self.config["char_id"], self.config["name"], lang)
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

    def apply_scale(self, new_w, new_h, meta=None, cat_dir=None):
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

    def cleanup(self):
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

    def hit_test(self, mx, my):
        """Check if point (mx, my) is inside this cat's bounding rect."""
        return (self.x <= mx <= self.x + self.display_w and
                self.y <= my <= self.y + self.display_h)


# ── Settings Window ────────────────────────────────────────────────────────────

class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.window = None
        self.selected_char_id = None
        self.current_scale = DEFAULT_SCALE
        self.current_model = ""
        self._scale_timer = None

        self.on_add_catset = None
        self.on_remove_catset = None
        self.on_rename_catset = None
        self.on_scale_changed = None
        self.on_model_changed = None
        self.on_lang_changed = None
        self.on_encounters_changed = None
        self.get_configs = None
        self.get_catset_preview = None
        self._anim_pictures = []
        self._anim_timer = None

    def setup(self, scale, model):
        self.current_scale = scale
        self.current_model = model
        if not self.window:
            self.window = Gtk.Window()
            self.window.set_hide_on_close(True)
            self.window.set_decorated(False)
            set_notification_type(self.window)
            set_always_on_top(self.window)
            # Clamp height to fit small screens (keep 80 px margin for top bar etc.)
            screen_h = getattr(self.app, "screen_h", 0) or 900
            win_h = min(900, max(480, screen_h - 80))
            self.window.set_default_size(340, win_h)
            self.window.set_resizable(False)
            self.window.add_css_class("settings-window")
            self.window.connect("close-request", self._on_close)
        cfgs = self.get_configs() if self.get_configs else []
        if not self.selected_char_id:
            first_catset = next((c for c in cfgs if c.get("char_id")), None)
            if first_catset:
                self.selected_char_id = first_catset["char_id"]
        self._build()

    def _on_close(self, *args):
        self._stop_timers()
        self.window.set_visible(False)
        return True

    def _stop_timers(self):
        for attr in ('_anim_timer', '_scale_timer'):
            tid = getattr(self, attr, None)
            if tid:
                GLib.source_remove(tid)
                setattr(self, attr, None)
        self._anim_pictures = []

    def refresh(self):
        self._build()

    def _build(self):
        self._stop_timers()
        configs = self.get_configs() if self.get_configs else []
        active_ids = {c["color_id"] for c in configs if c.get("color_id")}
        active_char_ids = {c["char_id"] for c in configs if c.get("char_id")}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # Close button (top-right)
        close_btn = Gtk.Button(label="\u00d7")
        close_css = Gtk.CssProvider()
        close_css.load_from_data(b"button { background: transparent; color: #4d3319; font-size: 18px; font-weight: bold; min-width: 24px; min-height: 24px; padding: 0; border: none; }")
        close_btn.get_style_context().add_provider(close_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        close_btn.set_halign(Gtk.Align.END)
        close_btn.connect("clicked", lambda b: self._on_close())
        box.append(close_btn)

        # Title
        title = Gtk.Label(label=L10n.s("title"))
        title.add_css_class("pixel-title")
        box.append(title)

        # Language
        lang_label = Gtk.Label(label=L10n.s("lang_label"))
        lang_label.add_css_class("pixel-label-small")
        lang_label.set_margin_top(8)
        box.append(lang_label)

        flags_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        flags_box.set_halign(Gtk.Align.CENTER)
        for lang_code, flag in [("fr", "\U0001f1eb\U0001f1f7"), ("en", "\U0001f1ec\U0001f1e7"), ("es", "\U0001f1ea\U0001f1f8")]:
            btn = Gtk.Button(label=flag)
            btn.set_size_request(50, 36)
            if lang_code == L10n.lang:
                btn.add_css_class("suggested-action")
            btn.connect("clicked", self._on_lang_click, lang_code)
            flags_box.append(btn)
        box.append(flags_box)

        # MY CATS
        cats_label = Gtk.Label(label=L10n.s("cats"))
        cats_label.add_css_class("pixel-label")
        cats_label.set_margin_top(12)
        box.append(cats_label)

        self._anim_pictures = []

        # ── Catset character row ──────────────────────────────────────────────
        catset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        catset_box.set_halign(Gtk.Align.CENTER)
        catset_box.set_margin_top(4)
        total_cats = len(active_ids) + len(active_char_ids)
        for char_id, emoji in CATSET_CHARS:
            is_active = char_id in active_char_ids
            is_selected = char_id == self.selected_char_id
            cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            btn = Gtk.Button()
            sprite_size = 40
            pic = Gtk.Picture()
            pic.set_size_request(sprite_size, sprite_size)
            pic.set_can_shrink(True)
            if self.get_catset_preview:
                pil_img = self.get_catset_preview(char_id)
                if pil_img:
                    pic.set_paintable(pil_to_texture(pil_img, sprite_size, sprite_size))
            btn.set_child(pic)
            btn_css = Gtk.CssProvider()
            if is_selected:
                border_color = '#ffaa22'
            elif is_active:
                border_color = '#4d3319'
            else:
                border_color = 'transparent'
            btn_css.load_from_data(f"""
                button {{ background: transparent; padding: 2px;
                         border: 2px solid {border_color};
                         border-radius: 6px; opacity: {1.0 if is_active else 0.4}; }}
                button:hover {{ opacity: 1.0; }}
            """.encode())
            btn.get_style_context().add_provider(btn_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            if is_active:
                btn.connect("clicked", self._on_catset_select, char_id)
            else:
                btn.connect("clicked", self._on_catset_add, char_id)
            cbox.append(btn)
            if is_active and total_cats > 1:
                rm_btn = Gtk.Button(label="\u00d7")
                rm_css = Gtk.CssProvider()
                rm_css.load_from_data(b"button { background: #cc3333; color: white; border-radius: 50%; min-width: 16px; min-height: 16px; font-size: 10px; padding: 0; }")
                rm_btn.get_style_context().add_provider(rm_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                rm_btn.set_halign(Gtk.Align.CENTER)
                rm_btn.connect("clicked", self._on_catset_remove, char_id)
                cbox.append(rm_btn)
            catset_box.append(cbox)
        box.append(catset_box)

        # ── Detail panel for selected catset char ─────────────────────────────
        if self.selected_char_id and self.selected_char_id in active_char_ids:
            char_id = self.selected_char_id
            p = CATSET_PERSONALITIES.get(char_id, CATSET_PERSONALITIES["cat01"])
            cfg = next((c for c in configs if c.get("char_id") == char_id), None)

            name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name_box.set_margin_top(8)
            nl = Gtk.Label(label=L10n.s("name"))
            nl.add_css_class("pixel-label-small")
            name_box.append(nl)
            ne = Gtk.Entry()
            ne.set_text(cfg["name"] if cfg else p["name"].get(L10n.lang, p["name"]["fr"]))
            ne.set_max_length(30)
            ne.add_css_class("pixel-entry")
            ne.set_hexpand(True)
            ne.connect("changed", self._on_catset_name_changed, char_id)
            name_box.append(ne)
            box.append(name_box)

            trait_lbl = Gtk.Label(label=f"\u2726 {p['traits'].get(L10n.lang, p['traits']['fr'])}")
            trait_lbl.add_css_class("pixel-trait")
            trait_lbl.set_xalign(0)
            trait_lbl.set_margin_start(4)
            box.append(trait_lbl)

            skill_lbl = Gtk.Label(label=p["skills"].get(L10n.lang, p["skills"]["fr"]))
            skill_lbl.add_css_class("pixel-trait")
            skill_lbl.set_xalign(0)
            skill_lbl.set_wrap(True)
            skill_lbl.set_margin_start(4)
            box.append(skill_lbl)

        if getattr(self, '_anim_timer', None):
            GLib.source_remove(self._anim_timer)
            self._anim_timer = None
        if self._anim_pictures:
            self._anim_timer = GLib.timeout_add(150, self._animate_previews)

        # SIZE
        size_label = Gtk.Label(label=L10n.s("size"))
        size_label.add_css_class("pixel-label")
        size_label.set_margin_top(16)
        box.append(size_label)

        size_value = Gtk.Label(label=f"x{self.current_scale:.1f}")
        size_value.add_css_class("pixel-label-small")
        box.append(size_value)

        scale_widget = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, MIN_SCALE, MAX_SCALE, 0.1)
        scale_widget.set_value(self.current_scale)
        scale_widget.set_draw_value(False)
        def on_scale(s):
            v = s.get_value()
            size_value.set_label(f"x{v:.1f}")
            self.current_scale = v
            if self._scale_timer:
                try:
                    GLib.source_remove(self._scale_timer)
                except Exception:
                    pass
            self._scale_timer = GLib.timeout_add(800, self._do_scale_change, v)
        scale_widget.connect("value-changed", on_scale)
        box.append(scale_widget)

        # MODEL
        model_label = Gtk.Label(label=L10n.s("model"))
        model_label.add_css_class("pixel-label")
        model_label.set_margin_top(12)
        box.append(model_label)

        model_combo = Gtk.DropDown()
        model_combo.set_margin_top(4)
        model_strings = Gtk.StringList.new([L10n.s("loading")])
        model_combo.set_model(model_strings)

        self._model_loading = True

        def _load_models():
            all_models = []
            if claude_available():
                all_models.append(f"{CLAUDE_MODEL} (Claude)")
            if _ollama_available():
                all_models.extend(fetch_ollama_models())
            def _update():
                self._model_loading = True
                model_strings.splice(0, model_strings.get_n_items(),
                                     all_models if all_models else [L10n.s("no_ollama")])
                if all_models:
                    current = self.current_model
                    for i, m in enumerate(all_models):
                        if m.startswith(current):
                            model_combo.set_selected(i)
                            break
                self._model_loading = False
                return False
            GLib.idle_add(_update)
        threading.Thread(target=_load_models, daemon=True).start()

        model_combo.connect("notify::selected", self._on_model_select, model_strings)
        box.append(model_combo)

        # AUTOSTART
        autostart_check = Gtk.CheckButton(label=L10n.s("autostart"))
        autostart_check.set_active(is_autostart())
        autostart_check.add_css_class("pixel-label-small")
        autostart_check.set_margin_top(16)
        autostart_check.connect("toggled", lambda btn: set_autostart(btn.get_active()))
        box.append(autostart_check)

        # ENCOUNTERS
        enc_enabled = True
        if self.on_encounters_changed and hasattr(self.app, 'encounters_enabled'):
            enc_enabled = self.app.encounters_enabled
        enc_check = Gtk.CheckButton(label=L10n.s("encounters"))
        enc_check.set_active(enc_enabled)
        enc_check.add_css_class("pixel-label-small")
        enc_check.set_margin_top(4)
        enc_check.connect("toggled", lambda btn: self.on_encounters_changed(btn.get_active()) if self.on_encounters_changed else None)
        box.append(enc_check)

        # Voice chat (push-to-talk) — optional feature
        voice_check = Gtk.CheckButton(label="Voice chat (hold mic button)")
        voice_check.set_active(getattr(self.app, "_voice_enabled", False))
        voice_check.add_css_class("pixel-label-small")
        voice_check.set_margin_top(4)
        if not VOICE_AVAILABLE:
            voice_check.set_sensitive(False)
        def _on_voice_toggled(btn):
            enabled = btn.get_active() and VOICE_AVAILABLE
            self.app._voice_enabled = enabled
            if enabled and self.app._voice_recorder is None:
                self.app._voice_recorder = VoiceRecorder()
            self.app._save_all()
        voice_check.connect("toggled", _on_voice_toggled)
        box.append(voice_check)

        if not VOICE_AVAILABLE:
            hint = Gtk.Label()
            hint.set_markup(
                '<span foreground="#cc2222" size="x-small">'
                'Not installed — run: <tt>pip install catai-linux[voice]</tt>'
                '</span>'
            )
            hint.set_wrap(True)
            hint.set_xalign(0)
            hint.set_margin_start(24)
            box.append(hint)
        else:
            restart_hint = Gtk.Label()
            restart_hint.set_markup(
                '<span foreground="#888888" size="x-small">'
                'Restart CATAI to apply enable/disable'
                '</span>'
            )
            restart_hint.set_xalign(0)
            restart_hint.set_margin_start(24)
            box.append(restart_hint)

            # Whisper model dropdown (size + recommended device hint)
            # Entry format: "name — <size> MB (<device>)"
            voice_models = [
                ("tiny",             39, "CPU"),
                ("base",             74, "CPU"),
                ("small",           244, "CPU/GPU"),
                ("medium",          769, "GPU"),
                ("distil-large-v3", 756, "GPU"),
                ("large-v3-turbo",  809, "GPU"),
                ("large-v3",       1550, "GPU"),
            ]
            model_label = Gtk.Label(label="Voice model")
            model_label.add_css_class("pixel-label-small")
            model_label.set_margin_top(8)
            model_label.set_margin_start(24)
            model_label.set_xalign(0)
            box.append(model_label)

            voice_drop = Gtk.DropDown()
            voice_drop.set_margin_top(2)
            voice_drop.set_margin_start(24)
            voice_labels = [f"{name} — {size} MB ({dev})" for name, size, dev in voice_models]
            voice_drop.set_model(Gtk.StringList.new(voice_labels))
            current_model = getattr(self.app, "_voice_model", "base")
            for i, (name, _sz, _d) in enumerate(voice_models):
                if name == current_model:
                    voice_drop.set_selected(i)
                    break
            voice_drop.set_sensitive(VOICE_AVAILABLE)

            def _on_voice_model_changed(drop, _param):
                idx = drop.get_selected()
                if 0 <= idx < len(voice_models):
                    name = voice_models[idx][0]
                    self.app._voice_model = name
                    if self.app._voice_recorder:
                        self.app._voice_recorder.set_model(name)
                        # Preload the new model in background if already cached
                        if _whisper_model_cached(name):
                            rec = self.app._voice_recorder
                            def _preload():
                                try:
                                    rec._ensure_model()
                                except Exception:
                                    log.exception("Whisper preload failed")
                            threading.Thread(target=_preload, daemon=True).start()
                    self.app._save_all()
                    log.info("Voice model set to %r", name)
            voice_drop.connect("notify::selected", _on_voice_model_changed)
            box.append(voice_drop)

            voice_model_hint = Gtk.Label()
            voice_model_hint.set_markup(
                '<span foreground="#888888" size="x-small">'
                'Larger = more accurate but slower. GPU needs CUDA.'
                '</span>'
            )
            voice_model_hint.set_xalign(0)
            voice_model_hint.set_margin_start(24)
            box.append(voice_model_hint)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(box)
        self.window.set_child(scroll)

    def _animate_previews(self):
        for pic, frames, idx in self._anim_pictures:
            idx[0] = (idx[0] + 1) % len(frames)
            pic.set_paintable(frames[idx[0]])
        return True

    def _on_catset_select(self, btn, char_id):
        self.selected_char_id = char_id
        self._build()

    def _on_catset_add(self, btn, char_id):
        if self.on_add_catset:
            self.on_add_catset(char_id)

    def _on_catset_remove(self, btn, char_id):
        if self.on_remove_catset:
            self.on_remove_catset(char_id)

    def _on_catset_name_changed(self, entry, char_id):
        if self.on_rename_catset:
            self.on_rename_catset(char_id, entry.get_text())

    def _on_lang_click(self, btn, lang_code):
        if self.on_lang_changed:
            self.on_lang_changed(lang_code)

    def _on_model_select(self, dropdown, pspec, string_list):
        if getattr(self, '_model_loading', False):
            return
        idx = dropdown.get_selected()
        if idx < string_list.get_n_items():
            name = string_list.get_string(idx)
            if name and not name.startswith("(") and name != L10n.s("loading"):
                model_id = name.split(" (")[0] if " (" in name else name
                self.current_model = model_id
                if self.on_model_changed:
                    self.on_model_changed(model_id)

    def _do_scale_change(self, v):
        self._scale_timer = None
        if self.on_scale_changed:
            self.on_scale_changed(v)
        return False

    def show(self):
        self.window.set_visible(True)
        self.window.present()
        # Center on screen
        display = Gdk.Display.get_default()
        if display:
            monitors = display.get_monitors()
            if monitors.get_n_items() > 0:
                geo = monitors.get_item(0).get_geometry()
                cx = (geo.width - 340) // 2
                cy = (geo.height - 680) // 2
                move_window(self.window, cx, cy)


# ── Cat Encounter (cat-to-cat AI conversation) ────────────────────────────────

class CatEncounter:
    """Manages a short AI-generated conversation between two nearby cats."""

    PROXIMITY = 180    # px — horizontal distance to trigger
    MSG_DURATION = 4500  # ms to display each message

    def __init__(self, cat_a, cat_b, app):
        self.cat_a = cat_a   # initiator
        self.cat_b = cat_b   # responder
        self.app = app
        self.n_exchanges = random.randint(1, 3)
        self._step = 0       # 0=A speaks, 1=B replies, 2=A again, …
        self._total_steps = self.n_exchanges * 2
        self._last_text = ""
        self._timer_id = None
        self.active = True

    def start(self):
        """Freeze cats and begin the conversation."""
        for cat in (self.cat_a, self.cat_b):
            cat.in_encounter = True
            cat.state = CatState.SOCIALIZING
            cat.meow_visible = False
        # Face each other
        if self.cat_b.x > self.cat_a.x:
            self.cat_a.direction = "east"
            self.cat_b.direction = "west"
        else:
            self.cat_a.direction = "west"
            self.cat_b.direction = "east"
        self._generate_next()

    def _speaker(self):
        return self.cat_a if self._step % 2 == 0 else self.cat_b

    def _listener(self):
        return self.cat_b if self._step % 2 == 0 else self.cat_a

    @staticmethod
    def _cat_traits(cat, lang):
        p = CATSET_PERSONALITIES.get(cat.config.get("char_id", "cat01"), CATSET_PERSONALITIES["cat01"])
        return p["traits"].get(lang, p["traits"]["fr"])

    def _build_prompt(self, speaker, listener):
        lang = L10n.lang
        s_name = speaker.config["name"]
        l_name = listener.config["name"]
        s_traits = self._cat_traits(speaker, lang)
        l_traits = self._cat_traits(listener, lang)
        if lang == "en":
            system = (f"You are {s_name}, a {s_traits} cat. You've just run into {l_name}, "
                      f"a {l_traits} cat. Reply with exactly 1 short sentence, in character, "
                      f"using cat sounds (meow, purr, mrrp). No quotation marks.")
            user = (f"Say hello to {l_name}." if self._step == 0 else
                    f"{l_name} just said: '{self._last_text}'. Reply briefly.")
        elif lang == "es":
            system = (f"Eres {s_name}, un gato {s_traits}. Acabas de cruzarte con {l_name}, "
                      f"un gato {l_traits}. Responde con exactamente 1 frase corta, en personaje, "
                      f"con sonidos de gato (miau, purr, mrrp). Sin comillas.")
            user = (f"Saluda a {l_name}." if self._step == 0 else
                    f"{l_name} acaba de decir: '{self._last_text}'. Respóndele brevemente.")
        else:
            system = (f"Tu es {s_name}, un chat {s_traits}. Tu croises {l_name}, "
                      f"un chat {l_traits}. Réponds avec exactement 1 courte phrase, dans le personnage, "
                      f"avec des sons de chat (miaou, purr, mrrp). Sans guillemets.")
            user = (f"Dis quelque chose à {l_name}." if self._step == 0 else
                    f"{l_name} vient de dire : '{self._last_text}'. Réponds-lui brièvement.")
        return system, user

    def _generate_next(self):
        if not self.active:
            return
        speaker = self._speaker()
        listener = self._listener()
        system, user = self._build_prompt(speaker, listener)

        backend = create_chat(self.app.selected_model)
        backend.messages = [{"role": "system", "content": system}]

        collected = []
        _spk = speaker
        _lst = listener

        def on_token(tok):
            collected.append(tok)
            return False

        def on_done():
            if not self.active:
                return False
            text = "".join(collected).strip()
            if not text:
                text = L10n.random_meow()
            self._last_text = text
            _spk.encounter_text = text
            _spk.encounter_visible = True
            _lst.encounter_visible = False
            self._timer_id = GLib.timeout_add(self.MSG_DURATION, self._advance)
            return False

        def on_error(msg):
            if not self.active:
                return False
            _spk.encounter_text = L10n.random_meow()
            _spk.encounter_visible = True
            self._timer_id = GLib.timeout_add(self.MSG_DURATION, self._advance)
            return False

        backend.send(user, on_token, on_done, on_error)

    def _advance(self):
        self._timer_id = None
        self._speaker().encounter_visible = False
        self._step += 1
        if self._step >= self._total_steps or not self.active:
            self._end()
        else:
            # Brief pause before next line
            self._timer_id = GLib.timeout_add(400, lambda: self._generate_next() or False)
        return False

    COOLDOWN = 120.0  # seconds before same cat can encounter again

    def _end(self):
        self.active = False
        cooldown_until = time.monotonic() + self.COOLDOWN
        for cat in (self.cat_a, self.cat_b):
            cat.state = CatState.IDLE
            cat.in_encounter = False
            cat.encounter_visible = False
            cat.encounter_text = ""
            cat.idle_ticks = 0
            cat._encounter_cooldown_until = cooldown_until
        self.app._active_encounter = None

    def cancel(self):
        self.active = False
        if self._timer_id:
            try:
                GLib.source_remove(self._timer_id)
            except Exception:
                pass
            self._timer_id = None
        cooldown_until = time.monotonic() + self.COOLDOWN
        for cat in (self.cat_a, self.cat_b):
            cat.state = CatState.IDLE
            cat.in_encounter = False
            cat.encounter_visible = False
            cat.encounter_text = ""
            cat._encounter_cooldown_until = cooldown_until


class LoveEncounter:
    """Silent encounter between two cats. Cat A is the initiator; the outcome
    is decided up front:
      - LOVE (40%):       both in LOVE → a kitten is born
      - SURPRISED (30%):  A in LOVE, B surprised, no drama
      - ANGRY (30%):      A attacks with ANGRY, B is the victim → drama_queen
    """

    PROXIMITY = CatEncounter.PROXIMITY
    COOLDOWN = 300.0  # 5 min — no baby-boom

    def __init__(self, cat_a, cat_b, app, forced_outcome=None):
        self.cat_a = cat_a  # initiator
        self.cat_b = cat_b  # responder / potential victim
        self.app = app
        self.active = True
        self._timers = []
        # None = random; "love" / "surprised" / "angry" = forced (used by E2E tests)
        self._forced_outcome = forced_outcome
        self._outcome = None  # resolved in start()

    def start(self):
        for cat in (self.cat_a, self.cat_b):
            cat.in_encounter = True
            cat.meow_visible = False
            cat.chat_visible = False

        # Decide the outcome up front so cat A shows the right initial state
        if self._forced_outcome in ("love", "surprised", "angry"):
            self._outcome = self._forced_outcome
        else:
            r = random.random()
            if r < 0.40:
                self._outcome = "love"
            elif r < 0.70:
                self._outcome = "surprised"
            else:
                self._outcome = "angry"

        # Cat A enters its initial state based on outcome
        if self._outcome == "angry":
            self.cat_a.state = CatState.ANGRY  # aggressor
            self.cat_a._face_toward(self.cat_b, CatState.ANGRY)
        else:
            self.cat_a.state = CatState.LOVE
            self.cat_a._face_toward(self.cat_b, CatState.LOVE)
        self.cat_a.frame_index = 0

        # Cat B stays idle but faces cat A
        self.cat_b.state = CatState.IDLE
        self.cat_b._face_toward(self.cat_a, CatState.IDLE)

        # After 1.2s, cat B reacts
        tid = GLib.timeout_add(1200, self._cat_b_reacts)
        self._timers.append(tid)

    def _cat_b_reacts(self):
        if not self.active:
            return False
        # Reaction depends on the pre-decided outcome
        if self._outcome == "love":
            self.cat_b.state = CatState.LOVE
        elif self._outcome == "surprised":
            self.cat_b.state = CatState.SURPRISED
        else:  # angry → cat B is surprised/scared before the attack
            self.cat_b.state = CatState.SURPRISED
        self.cat_b._face_toward(self.cat_a, self.cat_b.state)
        self.cat_b.frame_index = 0

        # Hold reaction for 3s, then decide outcome
        tid = GLib.timeout_add(3000, self._decide_outcome)
        self._timers.append(tid)
        return False

    def _decide_outcome(self):
        if not self.active:
            return False
        if self._outcome == "love":
            # Both in love → birth!
            self._give_birth()
            tid = GLib.timeout_add(3500, self._end)
            self._timers.append(tid)
        elif self._outcome == "angry":
            # Attack! Cat A was the aggressor, cat B is now the victim
            self._attack_cat_b()
        else:
            self._end()
        return False

    def _attack_cat_b(self):
        """Cat A attacks cat B. Cat B plays drama_queen, both exit encounter."""
        log.info("Love encounter attack: %s -> %s", self.cat_a.config["name"], self.cat_b.config["name"])
        # Release cat_b from encounter so drama_queen can play freely
        self.cat_b.in_encounter = False
        self.cat_b._flip_h = False
        self.cat_b._start_sequence("drama_queen")
        # Cat A steps out with cooldown
        self.cat_a.state = CatState.IDLE
        self.cat_a.in_encounter = False
        self.cat_a._flip_h = False
        self.cat_a._encounter_cooldown_until = time.monotonic() + self.COOLDOWN
        self.cat_a.idle_ticks = 0
        # Global encounter end (but cat_b still running drama_queen on its own)
        self.active = False
        self.app._active_encounter = None

    def _give_birth(self):
        # Check global kitten limit
        kitten_count = sum(1 for c in self.app.cat_instances if c.is_kitten)
        if kitten_count >= MAX_KITTENS:
            log.info("Love encounter: skipping birth, kitten limit reached (%d)", MAX_KITTENS)
            return

        # Pick a random parent for genetics
        parent = random.choice([self.cat_a, self.cat_b])
        kitten_char_id = CAT_TO_KITTEN.get(parent.config.get("char_id"))
        if not kitten_char_id:
            log.warning("No kitten mapping for char_id %s", parent.config.get("char_id"))
            return

        # Create ephemeral kitten config (NOT saved to disk)
        kitten_cfg = {
            "id": f"kitten_{uuid.uuid4().hex[:8]}",
            "char_id": kitten_char_id,
            "name": parent.config["name"] + " Jr.",
        }
        idx = len(self.app.cat_instances)
        try:
            self.app._create_instance(kitten_cfg, idx)
        except Exception:
            log.exception("Failed to create kitten")
            return

        kitten = self.app.cat_instances[-1]
        kitten.is_kitten = True
        kitten._birth_progress = 0.0
        # Place at midpoint between parents, slightly below
        kitten.x = (self.cat_a.x + self.cat_b.x) / 2 + (self.cat_a.display_w - kitten.display_w) / 2
        kitten.y = (self.cat_a.y + self.cat_b.y) / 2 + 20
        kitten.x = max(0, min(kitten.x, kitten.screen_w - kitten.display_w))
        kitten.y = max(0, min(kitten.y, kitten.screen_h - kitten.display_h))
        log.info("Birth! %s + %s → %s (%s)",
                 self.cat_a.config["name"], self.cat_b.config["name"],
                 kitten.config["name"], kitten_char_id)

    def _end(self):
        self.active = False
        cooldown = time.monotonic() + self.COOLDOWN
        for cat in (self.cat_a, self.cat_b):
            cat.state = CatState.IDLE
            cat.in_encounter = False
            cat._flip_h = False
            cat._encounter_cooldown_until = cooldown
            cat.idle_ticks = 0
        self.app._active_encounter = None
        return False

    def cancel(self):
        self.active = False
        for tid in self._timers:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._timers.clear()
        for cat in (self.cat_a, self.cat_b):
            cat.state = CatState.IDLE
            cat.in_encounter = False
            cat._flip_h = False


# ── Main Application ───────────────────────────────────────────────────────────

class CatAIApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=None,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.cat_instances = []
        self.cat_configs = []
        self.cat_scale = DEFAULT_SCALE
        self.screen_w = 1920
        self.screen_h = 1080
        self.selected_model = ""
        self.settings_ctrl = None
        self._menu_visible = False
        self._menu_x = 0
        self._menu_y = 0
        self._active_chat_cat = None
        self._menu_timer = None
        self._canvas_window = None
        self._canvas_area = None
        self._canvas_xid = None
        self._canvas_y_offset = 0  # GNOME top bar height (detected at launch)
        self._apocalypse_active = False
        self._apocalypse_timer = None
        # Voice chat state (push-to-talk microphone)
        self._voice_enabled = False
        self._voice_model = "base"
        self._voice_recorder = None
        self._voice_btn = None
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
        self._shake_amount = 0  # pixels — used by eg_shake
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

    def do_activate(self):
        _setup_logging()
        apply_css()
        self._check_deps()

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

        cfg = load_config()
        self.cat_scale = cfg.get("scale", DEFAULT_SCALE)
        self.selected_model = cfg.get("model", "gemma3:1b")
        L10n.lang = cfg.get("lang", "fr")
        self.encounters_enabled = cfg.get("encounters", True)
        self.cat_configs = cfg.get("cats", [])

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
            # _NET_WM_STATE_FULLSCREEN atom. Uses xprop subprocess so
            # polling is a bit more expensive — hence slower rate.
            GLib.timeout_add(1500, self._check_fullscreen),
            # Mood save — persist all cats' mood state to disk every 60s
            # (plus on shutdown and on key mood events).
            GLib.timeout_add(60000, self._save_all_moods),
        ]

        # Test socket for E2E tests (--test-socket flag)
        if "--test-socket" in sys.argv:
            self._start_test_socket()

    # ── Test socket for E2E tests ─────────────────────────────────────────

    SOCK_PATH = "/tmp/catai_test.sock"

    def _start_test_socket(self):
        """Start a Unix socket for E2E test commands."""
        import socket as sock_mod
        if os.path.exists(self.SOCK_PATH):
            os.remove(self.SOCK_PATH)
        self._test_sock = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
        self._test_sock.setblocking(False)
        self._test_sock.bind(self.SOCK_PATH)
        self._test_sock.listen(1)
        GLib.io_add_watch(self._test_sock.fileno(), GLib.IOCondition.IN, self._on_test_connection)
        log.debug("Test socket listening on %s", self.SOCK_PATH)

    def _on_test_connection(self, fd, condition):
        try:
            conn, _ = self._test_sock.accept()
        except Exception:
            log.exception("test socket accept failed")
            return True
        try:
            conn.setblocking(True)
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
        10 minutes of idle time. Usage: force_afk on | off"""
        if len(parts) < 2 or parts[1] not in ("on", "off"):
            return "ERR: usage: force_afk on|off"
        self._activity.is_afk = (parts[1] == "on")
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

    def _cmd_monitors(self, parts):
        """Return the list of detected monitor rects. Usage:
            monitors              → list all rects
            monitors at <x> <y>   → report which monitor (if any) contains
                                    the given point
        Used by e2e tests and for manual debugging of multi-monitor
        geometry (e.g. when a cat walks into a dead zone)."""
        if len(parts) == 4 and parts[1] == "at":
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
                "monitors": self._cmd_monitors,
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

        if self._voice_enabled:
            self._voice_btn = Gtk.Button(label="\U0001f3a4")  # 🎤
            self._voice_btn.add_css_class("pixel-mic-btn")
            self._voice_btn.set_size_request(30, -1)
            self._voice_btn.set_tooltip_text("Hold to talk (or hold Space in the entry)")
            press_gesture = Gtk.GestureClick()
            press_gesture.set_button(1)
            press_gesture.connect("pressed", self._on_voice_press)
            press_gesture.connect("released", self._on_voice_release)
            self._voice_btn.add_controller(press_gesture)
            self._chat_box.append(self._voice_btn)

        overlay.add_overlay(self._chat_box)

        win.set_child(overlay)
        self._entry_window = None  # no separate window needed

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
            # Detect canvas Y offset (GNOME top bar)
            xid = _get_xid(win)
            if xid:
                try:
                    r = subprocess.run(["xdotool", "getwindowgeometry", "--shell", str(xid)],
                                       capture_output=True, text=True, timeout=1)
                    for line in r.stdout.split("\n"):
                        if line.startswith("Y="):
                            self._canvas_y_offset = int(line.split("=")[1])
                            log.debug("Canvas Y offset: %d", self._canvas_y_offset)
                except Exception:
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

            # Draw chat response bubble if visible
            if cat.chat_visible and cat.chat_response:
                _draw_chat_bubble(ctx, cat.chat_response, cat.x, cat.y, cat.display_w, cat.display_h)

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
        return (
            cat_keys,
            self._menu_visible, self._menu_x, self._menu_y,
            self._easter_menu_visible,
            box_visible,
            box.get_margin_start() if box_visible else 0,
            box.get_margin_top() if box_visible else 0,
            self._voice_enabled,
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
            # Include chat bubble area if visible
            if cat.chat_visible and cat.chat_response:
                bw = 280
                bh = 150  # approximate
                bx = cat.x + cat.display_w / 2 - bw / 2
                by = cat.y - bh - 15
                if by < 0:
                    by = cat.y + cat.display_h + 10
                rects.append((bx, by, bw, bh))
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

        # Lazy-create + cache the Pango layout (once per CatAIApp lifetime)
        lay = self._bubble_layout_cache
        if lay is None:
            tmp = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
            tctx = cairo.Context(tmp)
            lay = PangoCairo.create_layout(tctx)
            lay.set_font_description(Pango.FontDescription(_BUBBLE_FONT))
            lay.set_width(content_w * Pango.SCALE)
            lay.set_wrap(Pango.WrapMode.WORD_CHAR)
            lay.set_height(-8)
            lay.set_ellipsize(Pango.EllipsizeMode.END)
            self._bubble_layout_cache = lay
            self._bubble_layout_cached_text = None

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
        # Cancel any pending delayed submit from a previous recording
        if getattr(self, "_voice_submit_timer", None):
            try:
                GLib.source_remove(self._voice_submit_timer)
            except Exception:
                pass
            self._voice_submit_timer = None
        self._chat_entry.set_text("")
        if self._voice_btn:
            self._voice_btn.set_label("\U0001f534")  # 🔴
            self._voice_btn.add_css_class("pixel-mic-btn-recording")
        self._chat_entry.set_placeholder_text("Recording... (release to send)")
        try:
            self._voice_recorder.start()
            return True
        except Exception:
            log.exception("Failed to start voice recording")
            if self._voice_btn:
                self._voice_btn.set_label("\U0001f3a4")
                self._voice_btn.remove_css_class("pixel-mic-btn-recording")
            self._chat_entry.set_placeholder_text(L10n.s("talk"))
            return False

    def _stop_voice_recording(self):
        """Stop recording + transcribe + auto-submit. Shared by mic button release
        & Space keyup. Returns True if a stop was triggered."""
        if not self._voice_recorder or not self._voice_recorder._recording:
            return False
        if self._voice_btn:
            self._voice_btn.set_label("\u23f3")  # ⏳
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
                self._voice_btn.set_label("\U0001f3a4")  # 🎤
                self._voice_btn.set_sensitive(True)
            self._chat_entry.set_placeholder_text(L10n.s("talk"))
            if text and self._active_chat_cat:
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
            return False  # idle_add callback

        self._voice_recorder.stop_and_transcribe(L10n.lang, on_result)
        return True

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
        Runs in CAPTURE phase so we intercept before Gtk.Entry inserts ' '."""
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

        # Check context menu click
        if self._menu_visible:
            mx, my = self._menu_x, self._menu_y
            if mx <= start_x <= mx + 120 and my <= start_y <= my + 50:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                if start_y < my + 25:
                    self._menu_visible = False
                    self._open_settings()
                else:
                    self._menu_visible = False
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
        for tid in self._timers:
            GLib.source_remove(tid)
        self._timers.clear()
        for cat in self.cat_instances:
            cat.cleanup()
        self.cat_instances.clear()
        self._chat_box.set_visible(False)
        if self._canvas_window:
            unregister_window(self._canvas_window)
        if self.settings_ctrl and self.settings_ctrl.window:
            self.settings_ctrl._stop_timers()
            self.settings_ctrl.window.set_visible(False)
        Gtk.Application.do_shutdown(self)

    def _check_deps(self):
        if shutil.which("apt"):
            pkg_cmd = "sudo apt install"
        elif shutil.which("dnf"):
            pkg_cmd = "sudo dnf install"
        else:
            pkg_cmd = "install"
        for tool in ["xdotool", "wmctrl"]:
            if not shutil.which(tool):
                log.debug("Optional tool not found: %s (%s %s)", tool, pkg_cmd, tool)

    def _create_instance(self, config, index):
        """Instantiate a catset character (all configs are catset-based since v0.3.0)."""
        char_id = config.get("char_id")
        if not char_id:
            log.warning("Config without char_id — skipping: %r", config)
            return
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
        inst = CatInstance(config, _CATSET_COLOR_DEF)
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
        now = time.monotonic()
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
        clone = CatInstance(cfg, parent.color_def)
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
        method_name = next((fn for k, _, _, fn in EASTER_EGGS if k == key), None)
        if method_name and hasattr(self, method_name):
            try:
                getattr(self, method_name)()
                log.info("Easter egg triggered: %s", key)
            except Exception:
                log.exception("Easter egg %s failed", key)
        return False

    def _release_encounter_lock(self):
        """Clear in_encounter on all cats and return them to IDLE."""
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
        chars = "01アイウエオカキクケコサシスセソタチツテト"
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
        """rm -rf / — a cat grows to 3×, 'wipes' across the screen, then laughs
        it off and shrinks back. The effect is 100% harmless (nothing actually
        gets deleted on disk or from the app state) — it's a playful reassurance
        for anyone who typed the infamous command half-jokingly.

        Phases:
          1. Pick a cat (last active chat cat, else random) and freeze it.
          2. Grow it to 3× over ~400 ms via _boss_scale tween.
          3. Walk it across the screen (east→west or west→east depending on
             which side it's closest to) at high speed, with WALKING anim.
          4. While walking, the canvas draws a 'wipe' trail — a translucent
             cream band growing behind the cat. This is the 'deletion'.
          5. On the far side, the cat stops, turns SURPRISED, shows a
             localized 'just kidding!' meow bubble.
          6. After 1.5 s, enter LOVE for a laugh, shrink back to 1× over
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
        # for the 3× visual growth without clipping the top/bottom of screen)
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

        # Phase 1: grow 1.0 → 3.0 over 400 ms (8 ticks × 50 ms)
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

        # Phase 4: shrink back from 3.0 → 1.0 over 600 ms, then restore
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
        """Poll Caps Lock; on False → True rising edge (with 8 s cooldown),
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
            app_name: optional originating application (Slack, Thunderbird…)
            summary: optional notification summary text
        """
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
        log.info("notification egg: %s (%s) → %s says %r",
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
                return f"⏱ Up {days}d {hours}h {minutes}m 💪"
            if lang == "es":
                return f"⏱ Arriba {days}d {hours}h {minutes}m 💪"
            return f"⏱ Allumé depuis {days}j {hours}h {minutes}m 💪"
        if hours > 0:
            if lang == "en":
                return f"⏱ Up {hours}h {minutes}m 🎉"
            if lang == "es":
                return f"⏱ Arriba {hours}h {minutes}m 🎉"
            return f"⏱ Allumé depuis {hours}h {minutes}m 🎉"
        if lang == "en":
            return f"⏱ Up {minutes}m — just woke up 😸"
        if lang == "es":
            return f"⏱ Arriba {minutes}m — acabando de despertar 😸"
        return f"⏱ Allumé depuis {minutes}m — à peine réveillé 😸"

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
                # Spread from π (left) to 2π (right), avoiding the top
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
        Uses xprop subprocesses — 2 cheap calls totaling <20 ms. Falls back
        to False on any failure (non-X11 sessions, xprop missing, etc.)."""
        try:
            r = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                capture_output=True, text=True, timeout=1,
            )
            # Example stdout: '_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3800003'
            if "# " not in r.stdout:
                return False
            xid = r.stdout.strip().split("# ", 1)[1].split(",")[0].strip()
            if not xid or xid == "0x0":
                return False
            r = subprocess.run(
                ["xprop", "-id", xid, "_NET_WM_STATE"],
                capture_output=True, text=True, timeout=1,
            )
            return "_NET_WM_STATE_FULLSCREEN" in r.stdout
        except Exception:
            return False

    def _check_fullscreen(self) -> bool:
        """Poll fullscreen state; on False → True rising edge (with 15 s
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
        ~10 s it falls asleep mid-reading (SLEEPING_BALL + '…💤' bubble)
        and the bubble clears after another few seconds.

        Unlike the other easter eggs, this one takes a pre-selected cat
        as argument (the active chat cat, passed from
        _on_chat_entry_activate), not a random one — the user clicked on
        THIS specific cat and tried to talk to it, so that's who reads.
        """
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
                cat.chat_response = last_window + "  …💤"
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

    def eg_nyan(self):
        """Classic Nyan Cat: flies across the screen with a tiled animated rainbow trail."""
        self._load_nyan_assets()
        if not self._nyan_frames:
            return
        # Target size: same height as regular cats (display_h at current scale)
        if self.cat_instances:
            target_h = self.cat_instances[0].display_h
        else:
            target_h = int(round(80 * self.cat_scale))  # catset sprites are 80×80
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
    gtk_args = [a for a in sys.argv if a not in ("--debug", "--test-socket", "--voice")]
    app = CatAIApp()
    app.run(gtk_args)

if __name__ == "__main__":
    main()
