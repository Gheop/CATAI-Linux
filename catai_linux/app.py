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
import ctypes
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
from gi.repository import Gdk, GdkX11, Gio, GLib, Gst, Gtk, Pango, PangoCairo

Gst.init(None)

from PIL import Image
import httpx

# ── Optional voice support (faster-whisper) ──────────────────────────────────
try:
    from faster_whisper import WhisperModel as _WhisperModel
    VOICE_AVAILABLE = True
except ImportError:
    _WhisperModel = None
    VOICE_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────────

RENDER_MS = 125        # 8 FPS
BEHAVIOR_MS = 1000     # 1 Hz
WALK_SPEED = 4
MEM_MAX = 20
OLLAMA_URL = "http://localhost:11434"
OLLAMA_TIMEOUT = 60
DEFAULT_SCALE = 1.5
MIN_SCALE = 0.5
MAX_SCALE = 4.0
CONFIG_DIR = os.path.expanduser("~/.config/catai")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


class CatState(enum.Enum):
    IDLE = "idle"
    WALKING = "walking"
    EATING = "eating"
    DRINKING = "drinking"
    ANGRY = "angry"
    SLEEPING = "sleeping"
    WAKING_UP = "waking_up"
    SOCIALIZING = "socializing"      # frozen during cat-to-cat encounter
    SLEEPING_BALL = "sleeping_ball"  # curled in a ball, breathing slowly
    CHASING_MOUSE = "chasing_mouse"
    PLAYING_BALL = "playing_ball"
    BUTTERFLY = "butterfly"
    SCRATCHING_TREE = "scratching_tree"
    PEEING = "peeing"
    POOPING = "pooping"
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
    CatState.DRINKING: "drinking",
    CatState.ANGRY: "angry",
    CatState.WAKING_UP: "waking-getting-up",
    CatState.SLEEPING_BALL: "sleeping-ball",
    CatState.CHASING_MOUSE: "chasing-mouse",
    CatState.PLAYING_BALL: "playing-ball",
    CatState.BUTTERFLY: "butterfly",
    CatState.SCRATCHING_TREE: "scratching-tree",
    CatState.PEEING: "peeing",
    CatState.POOPING: "pooping",
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
    CatState.EATING, CatState.DRINKING, CatState.ANGRY, CatState.WAKING_UP,
    CatState.CHASING_MOUSE, CatState.PLAYING_BALL, CatState.BUTTERFLY,
    CatState.SCRATCHING_TREE, CatState.PEEING, CatState.POOPING,
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

# ── Localization ───────────────────────────────────────────────────────────────

class L10n:
    lang = "fr"
    strings = {
        "title":    {"fr": ":: RÉGLAGES ::", "en": ":: SETTINGS ::", "es": ":: AJUSTES ::"},
        "cats":     {"fr": "MES CHATS", "en": "MY CATS", "es": "MIS GATOS"},
        "name":     {"fr": "Nom :", "en": "Name:", "es": "Nombre:"},
        "size":     {"fr": "TAILLE", "en": "SIZE", "es": "TAMAÑO"},
        "model":    {"fr": "MODÈLE IA", "en": "AI MODEL", "es": "MODELO IA"},
        "quit":     {"fr": "Quitter", "en": "Quit", "es": "Salir"},
        "settings": {"fr": "Réglages...", "en": "Settings...", "es": "Ajustes..."},
        "talk":     {"fr": "Parle au chat...", "en": "Talk to the cat...", "es": "Habla al gato..."},
        "hi":       {"fr": "Miaou! ~(=^..^=)~", "en": "Meow! ~(=^..^=)~", "es": "¡Miau! ~(=^..^=)~"},
        "loading":  {"fr": "Chargement...", "en": "Loading...", "es": "Cargando..."},
        "no_ollama": {"fr": "(Ollama indisponible)", "en": "(Ollama unavailable)", "es": "(Ollama no disponible)"},
        "err":      {"fr": "Mrrp... pas de connexion", "en": "Mrrp... no connection", "es": "Mrrp... sin conexión"},
        "err_auth": {"fr": "Mrrp... token expiré, relance 'claude' pour renouveler", "en": "Mrrp... token expired, run 'claude' to renew", "es": "Mrrp... token expirado, ejecuta 'claude' para renovar"},
        "refreshing_auth": {"fr": "Renouvellement du token Claude...", "en": "Refreshing Claude token...", "es": "Renovando token de Claude..."},
        "lang_label": {"fr": "LANGUE", "en": "LANGUAGE", "es": "IDIOMA"},
        "autostart": {"fr": "Lancer au démarrage", "en": "Start at login", "es": "Iniciar al arrancar"},
        "encounters": {"fr": "Rencontres entre chats", "en": "Cat encounters", "es": "Encuentros entre gatos"},
    }
    meows = {
        "fr": ["Miaou~", "Mrrp!", "Prrrr...", "Miaou miaou!", "Nyaa~", "*ronron*", "Mew!", "Prrrt?"],
        "en": ["Meow~", "Mrrp!", "Purrrr...", "Meow meow!", "Nyaa~", "*purr*", "Mew!", "Prrrt?"],
        "es": ["Miau~!", "Mrrp!", "Purrrr...", "Miau miau!", "Nyaa~", "*ronroneo*", "Mew!", "Prrrt?"],
    }

    @classmethod
    def s(cls, key):
        d = cls.strings.get(key, {})
        return d.get(cls.lang) or d.get("fr") or key

    @classmethod
    def random_meow(cls):
        return random.choice(cls.meows.get(cls.lang, cls.meows["fr"]))

# ── Cat Colors & Personalities ─────────────────────────────────────────────────

class CatColorDef:
    def __init__(self, id, color_rgba, hue_shift, sat_mul, bri_off, traits, names, skills):
        self.id = id
        self.color_rgba = color_rgba
        self.hue_shift = hue_shift
        self.sat_mul = sat_mul
        self.bri_off = bri_off
        self.traits = traits
        self.names = names
        self.skills = skills

    def prompt(self, name, lang):
        t = self.traits.get(lang, self.traits["fr"])
        sk = self.skills.get(lang, self.skills["fr"])
        if lang == "en":
            return f"You are a little {t} cat named {name}. {sk} Respond briefly with cat sounds (meow, purr, mrrp). Max 2-3 sentences."
        elif lang == "es":
            return f"Eres un gatito {t} llamado {name}. {sk} Responde brevemente con sonidos de gato (miau, purr, mrrp). Máximo 2-3 frases."
        return f"Tu es un petit chat {t} nommé {name}. {sk} Réponds brièvement avec des sons de chat (miaou, purr, mrrp). Max 2-3 phrases."


CAT_COLORS = [
    CatColorDef("orange", (1.0, 0.6, 0.2, 1.0), 0, 1, 0,
        {"fr": "joueur et espiègle", "en": "playful and mischievous", "es": "juguetón y travieso"},
        {"fr": "Citrouille", "en": "Pumpkin", "es": "Calabaza"},
        {"fr": "Tu adores les blagues et jeux de mots.", "en": "You love jokes and puns.", "es": "Adoras los chistes y juegos de palabras."}),
    CatColorDef("black", (0.15, 0.15, 0.18, 1.0), 0, 0.1, -0.45,
        {"fr": "mystérieux et philosophe", "en": "mysterious and philosophical", "es": "misterioso y filósofo"},
        {"fr": "Ombre", "en": "Shadow", "es": "Sombra"},
        {"fr": "Tu poses des questions profondes et aimes réfléchir.", "en": "You ask deep questions and love to reflect.", "es": "Haces preguntas profundas y te encanta reflexionar."}),
    CatColorDef("white", (0.95, 0.95, 0.97, 1.0), 0, 0.05, 0.4,
        {"fr": "élégant et poétique", "en": "elegant and poetic", "es": "elegante y poético"},
        {"fr": "Neige", "en": "Snow", "es": "Nieve"},
        {"fr": "Tu t'exprimes avec grâce et tu adores la poésie.", "en": "You speak gracefully and love poetry.", "es": "Te expresas con gracia y adoras la poesía."}),
    CatColorDef("grey", (0.55, 0.55, 0.58, 1.0), 0, 0, -0.05,
        {"fr": "sage et savant", "en": "wise and scholarly", "es": "sabio y erudito"},
        {"fr": "Einstein", "en": "Einstein", "es": "Einstein"},
        {"fr": "Tu expliques des faits scientifiques fascinants.", "en": "You explain fascinating scientific facts.", "es": "Explicas datos científicos fascinantes."}),
    CatColorDef("brown", (0.5, 0.3, 0.15, 1.0), -0.03, 0.7, -0.2,
        {"fr": "aventurier et conteur", "en": "adventurous storyteller", "es": "aventurero y cuentacuentos"},
        {"fr": "Indiana", "en": "Indiana", "es": "Indiana"},
        {"fr": "Tu racontes des aventures extraordinaires.", "en": "You tell extraordinary adventures.", "es": "Cuentas aventuras extraordinarias."}),
    CatColorDef("cream", (0.95, 0.88, 0.75, 1.0), 0.02, 0.3, 0.15,
        {"fr": "câlin et réconfortant", "en": "cuddly and comforting", "es": "cariñoso y reconfortante"},
        {"fr": "Caramel", "en": "Caramel", "es": "Caramelo"},
        {"fr": "Tu remontes le moral avec tendresse.", "en": "You comfort with tenderness.", "es": "Animas con ternura."}),
]

def color_def(id):
    return next((c for c in CAT_COLORS if c.id == id), None)

# Sentinel: catset chars are pre-colored — skip tinting
_CATSET_COLOR_DEF = CatColorDef("catset", None, 0, 1, 0, {}, {}, {})

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

def rgb_to_hsb(r, g, b):
    mx = max(r, g, b)
    mn = min(r, g, b)
    delta = mx - mn
    h = 0.0
    if delta > 0.001:
        if mx == r:
            h = ((g - b) / delta) % 6 / 6.0
        elif mx == g:
            h = ((b - r) / delta + 2) / 6.0
        else:
            h = ((r - g) / delta + 4) / 6.0
        if h < 0:
            h += 1.0
    s = delta / mx if mx > 0.001 else 0.0
    return h, s, mx

def hsb_to_rgb(h, s, b):
    c = b * s
    x = c * (1 - abs((h * 6) % 2 - 1))
    m = b - c
    sector = int(h * 6) % 6
    if sector == 0:   r, g, bb = c, x, 0
    elif sector == 1: r, g, bb = x, c, 0
    elif sector == 2: r, g, bb = 0, c, x
    elif sector == 3: r, g, bb = 0, x, c
    elif sector == 4: r, g, bb = x, 0, c
    else:             r, g, bb = c, 0, x
    return r + m, g + m, bb + m

def tint_sprite(img, color_def):
    if color_def is _CATSET_COLOR_DEF or color_def.id == "orange":
        return img
    img = img.convert("RGBA")
    pixels = list(img.getdata())
    new_pixels = []
    for r8, g8, b8, a8 in pixels:
        if a8 < 3:
            new_pixels.append((r8, g8, b8, a8))
            continue
        a = a8 / 255.0
        r = (r8 / 255.0) / a if a > 0 else 0
        g = (g8 / 255.0) / a if a > 0 else 0
        b = (b8 / 255.0) / a if a > 0 else 0
        h, s, br = rgb_to_hsb(r, g, b)
        nh = (h + color_def.hue_shift + 1) % 1.0
        ns = max(0, min(1, s * color_def.sat_mul))
        nb = max(0, min(1, br + color_def.bri_off))
        nr, ng, nbb = hsb_to_rgb(nh, ns, nb)
        new_pixels.append((
            max(0, min(255, int(nr * a * 255))),
            max(0, min(255, int(ng * a * 255))),
            max(0, min(255, int(nbb * a * 255))),
            a8,
        ))
    out = Image.new("RGBA", img.size)
    out.putdata(new_pixels)
    return out


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


CACHE_DIR = os.path.expanduser("~/.cache/catai")

def _cache_path(path, color_id, size):
    """Get cache file path for a tinted sprite."""
    name = os.path.basename(os.path.dirname(os.path.dirname(path))) + "_" + \
           os.path.basename(os.path.dirname(path)) + "_" + os.path.basename(path)
    return os.path.join(CACHE_DIR, color_id, f"{size[0]}x{size[1]}", name)

def load_and_tint(path, color_def, cache_size=None):
    """Load a sprite PNG and apply color tinting. Uses disk cache if cache_size given."""
    if cache_size and color_def is not _CATSET_COLOR_DEF:
        cp = _cache_path(path, color_def.id, cache_size)
        if os.path.exists(cp):
            try:
                return Image.open(cp).convert("RGBA")
            except Exception:
                pass

    try:
        src = Image.open(path).convert("RGBA")
    except Exception:
        log.warning("Missing sprite: %s", path)
        src = Image.new("RGBA", (68, 68), (255, 0, 255, 128))
    tinted = tint_sprite(src, color_def)

    if cache_size and color_def is not _CATSET_COLOR_DEF:
        cp = _cache_path(path, color_def.id, cache_size)
        try:
            os.makedirs(os.path.dirname(cp), exist_ok=True)
            tinted.save(cp, "PNG")
        except Exception:
            pass

    return tinted

# ── X11 Window Helpers (kept for chat bubble + context menu positioning) ──────

_xid_cache = {}

def _get_xid(window):
    """Get the X11 window ID for a GTK window."""
    wid = id(window)
    if wid in _xid_cache:
        return _xid_cache[wid]
    surface = window.get_surface()
    if surface and isinstance(surface, GdkX11.X11Surface):
        xid = surface.get_xid()
        _xid_cache[wid] = xid
        return xid
    return None


_xlib = None
_xdpy = None

def _init_xlib():
    """Initialize Xlib for direct window moves."""
    global _xlib, _xdpy
    if _xlib is not None:
        return _xlib is not False
    try:
        lib = ctypes.cdll.LoadLibrary("libX11.so.6")
        lib.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
        lib.XFlush.argtypes = [ctypes.c_void_p]
        display = Gdk.Display.get_default()
        if isinstance(display, GdkX11.X11Display):
            xdpy_obj = display.get_xdisplay()
            try:
                _xdpy = hash(xdpy_obj)
                lib.XFlush(ctypes.c_void_p(_xdpy))
                _xlib = lib
                log.debug("Xlib initialized via hash(), pointer=%#x", _xdpy)
                return True
            except (TypeError, OSError):
                pass
            s = str(xdpy_obj)
            if "void at 0x" in s:
                _xdpy = int(s.split("void at ")[1].rstrip(")>"), 16)
                _xlib = lib
                log.debug("Xlib initialized via repr(), pointer=%#x", _xdpy)
                return True
    except Exception as e:
        log.debug("Xlib init failed: %s", e)
    _xlib = False
    log.debug("Xlib unavailable, using xdotool fallback")
    return False

_xlib_dirty = False

def move_window(window, x, y):
    """Move a GTK4 window. Uses Xlib directly, falls back to xdotool."""
    global _xlib_dirty
    xid = _get_xid(window)
    if not xid:
        return
    if _init_xlib() and _xdpy:
        _xlib.XMoveWindow(ctypes.c_void_p(_xdpy), xid, int(x), int(y))
        _xlib_dirty = True
    else:
        _run_x11(["xdotool", "windowmove", str(xid), str(int(x)), str(int(y))])

def flush_x11():
    """Flush all pending X11 operations (call once per frame)."""
    global _xlib_dirty
    if _xlib_dirty and _xlib and _xdpy:
        _xlib.XFlush(ctypes.c_void_p(_xdpy))
        _xlib_dirty = False


def _run_x11(cmd):
    """Run an X11 tool non-blocking in a background thread."""
    def _bg():
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        except Exception:
            pass
    threading.Thread(target=_bg, daemon=True).start()


def _x11_set_property_atom(xid, prop_name, value_name):
    """Set an X11 atom property directly via Xlib."""
    if not (_init_xlib() and _xdpy):
        return False
    try:
        dpy = ctypes.c_void_p(_xdpy)
        _xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        _xlib.XInternAtom.restype = ctypes.c_ulong
        _xlib.XChangeProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
        prop = _xlib.XInternAtom(dpy, prop_name.encode(), 0)
        val = _xlib.XInternAtom(dpy, value_name.encode(), 0)
        xa_atom = _xlib.XInternAtom(dpy, b"ATOM", 0)
        data = (ctypes.c_ulong * 1)(val)
        _xlib.XChangeProperty(dpy, xid, prop, xa_atom, 32, 0, data, 1)
        return True
    except Exception:
        return False


def _x11_set_above_skip_taskbar(xid):
    """Set _NET_WM_STATE_ABOVE + _NET_WM_STATE_SKIP_TASKBAR via X11 client message."""
    if not (_init_xlib() and _xdpy):
        return False
    try:
        dpy = ctypes.c_void_p(_xdpy)

        wm_state = _xlib.XInternAtom(dpy, b"_NET_WM_STATE", 0)
        above = _xlib.XInternAtom(dpy, b"_NET_WM_STATE_ABOVE", 0)
        skip = _xlib.XInternAtom(dpy, b"_NET_WM_STATE_SKIP_TASKBAR", 0)

        _xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        _xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        root = _xlib.XDefaultRootWindow(dpy)

        class XClientMessageEvent(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_ulong * 5),
            ]

        _xlib.XSendEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
            ctypes.POINTER(XClientMessageEvent)]
        _xlib.XSendEvent.restype = ctypes.c_int

        mask = 0x00080000 | 0x00100000

        for atom in [above, skip]:
            ev = XClientMessageEvent()
            ev.type = 33  # ClientMessage
            ev.send_event = 1
            ev.display = dpy
            ev.window = xid
            ev.message_type = wm_state
            ev.format = 32
            ev.data[0] = 1  # _NET_WM_STATE_ADD
            ev.data[1] = atom
            ev.data[2] = 0
            ev.data[3] = 1
            ev.data[4] = 0
            _xlib.XSendEvent(dpy, root, 0, mask, ctypes.byref(ev))

        return True
    except Exception as e:
        log.debug("Xlib set_above failed: %s", e)
        return False


_above_pending = []
_applied = set()
_notification_windows = []


def _apply_xid_hints(window, above=False, notification=False):
    """Apply X11 hints immediately if XID is available."""
    xid = _get_xid(window)
    if not xid:
        return
    wid = id(window)
    if above and ("above", wid) not in _applied:
        if not _x11_set_above_skip_taskbar(xid):
            _run_x11(["wmctrl", "-i", "-r", str(xid), "-b", "add,above,skip_taskbar"])
        _applied.add(("above", wid))
    if notification and ("notif", wid) not in _applied:
        if not _x11_set_property_atom(xid, "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_NOTIFICATION"):
            _run_x11(["xprop", "-id", str(xid), "-f", "_NET_WM_WINDOW_TYPE", "32a",
                      "-set", "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_NOTIFICATION"])
        _applied.add(("notif", wid))
    if _xlib and _xdpy:
        _xlib.XFlush(ctypes.c_void_p(_xdpy))


def set_always_on_top(window):
    """Mark window for always-on-top + skip-taskbar."""
    _above_pending.append(window)
    window.connect("realize", lambda w: _apply_xid_hints(w, above=True))

def set_notification_type(window):
    """Mark window as NOTIFICATION type. Call realize() before first set_visible()."""
    _notification_windows.append(window)
    window.connect("realize", lambda w: _apply_xid_hints(w, notification=True))
    # Force realize now so type is set BEFORE first show
    if not window.get_realized():
        window.realize()

def unregister_window(window):
    """Remove window from all global tracking lists and caches."""
    wid = id(window)
    for lst in [_above_pending, _notification_windows]:
        while window in lst:
            lst.remove(window)
    for prefix in ["above", "notif"]:
        _applied.discard((prefix, wid))
    _xid_cache.pop(wid, None)

def _apply_above_all():
    """Fallback: apply X11 hints to any windows missed by realize callback."""
    for w in list(_above_pending):
        if ("above", id(w)) not in _applied:
            _apply_xid_hints(w, above=True)
    for w in list(_notification_windows):
        if ("notif", id(w)) not in _applied:
            _apply_xid_hints(w, notification=True)
    return True


# ── XShape Input Passthrough ─────────────────────────────────────────────────

_xext = None

def _init_xext():
    """Load libXext for XShape extension."""
    global _xext
    if _xext is not None:
        return _xext is not False
    try:
        _xext = ctypes.cdll.LoadLibrary("libXext.so.6")
        return True
    except Exception as e:
        log.debug("libXext unavailable: %s", e)
        _xext = False
        return False


class XRectangle(ctypes.Structure):
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]


def _update_input_shape(window_xid, rects):
    """Set the input shape of a window to only the given rectangles.
    rects: list of (x, y, w, h) tuples.
    If rects is empty, set a 1x1 rect at -1,-1 (effectively no input)."""
    if not (_init_xlib() and _xdpy and _init_xext()):
        return
    try:
        dpy = ctypes.c_void_p(_xdpy)
        ShapeInput = 2  # XShape ShapeInput kind
        ShapeSet = 0    # XShape ShapeSet operation
        Unsorted = 0

        _xext.XShapeCombineRectangles.argtypes = [
            ctypes.c_void_p,   # display
            ctypes.c_ulong,    # window
            ctypes.c_int,      # dest_kind (ShapeInput=2)
            ctypes.c_int,      # x_off
            ctypes.c_int,      # y_off
            ctypes.POINTER(XRectangle),  # rectangles
            ctypes.c_int,      # n_rects
            ctypes.c_int,      # op (ShapeSet=0)
            ctypes.c_int,      # ordering
        ]

        if not rects:
            # No cats visible — set tiny offscreen input region
            arr = (XRectangle * 1)(XRectangle(-1, -1, 1, 1))
            _xext.XShapeCombineRectangles(dpy, window_xid, ShapeInput, 0, 0, arr, 1, ShapeSet, Unsorted)
        else:
            n = len(rects)
            arr = (XRectangle * n)()
            for i, (rx, ry, rw, rh) in enumerate(rects):
                arr[i].x = max(0, int(rx))
                arr[i].y = max(0, int(ry))
                arr[i].width = max(1, int(rw))
                arr[i].height = max(1, int(rh))
            _xext.XShapeCombineRectangles(dpy, window_xid, ShapeInput, 0, 0, arr, n, ShapeSet, Unsorted)
    except Exception as e:
        log.debug("XShape input update failed: %s", e)


# ── Chat Backends ──────────────────────────────────────────────────────────────

CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_CREDS = os.path.expanduser("~/.claude/.credentials.json")


def _get_claude_api_key():
    """Get API key from env var or Claude Code's OAuth token."""
    return os.environ.get("ANTHROPIC_API_KEY") or _read_claude_oauth()

def _find_claude_cli():
    """Find the claude CLI binary, checking common locations."""
    for path in [
        shutil.which("claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
    ]:
        if path and os.path.isfile(path):
            return path
    return None

def _refresh_claude_token():
    """Force Claude Code to refresh the OAuth token by calling it."""
    cli = _find_claude_cli()
    if not cli:
        log.debug("Claude CLI not found, cannot refresh token")
        return False
    try:
        log.debug("Refreshing Claude token via %s...", cli)
        subprocess.run([cli, "-p", "ok", "--output-format", "text"],
                       capture_output=True, timeout=30)
        return True
    except Exception as e:
        log.debug("Token refresh failed: %s", e)
        return False

def _read_claude_oauth_raw():
    """Read credentials JSON without refreshing. Returns oauth dict or None."""
    try:
        if os.path.exists(CLAUDE_CREDS):
            mode = os.stat(CLAUDE_CREDS).st_mode
            if mode & 0o077:
                log.warning("Credentials file %s is accessible by others (mode %o)", CLAUDE_CREDS, mode)
        with open(CLAUDE_CREDS) as f:
            return json.load(f).get("claudeAiOauth")
    except Exception:
        return None


def _read_claude_oauth():
    """Return a valid access token, proactively refreshing if near expiry."""
    oa = _read_claude_oauth_raw()
    if not oa:
        return None
    # Check expiresAt (milliseconds). Refresh if < 5 min remaining.
    exp_ms = oa.get("expiresAt", 0)
    now_ms = time.time() * 1000
    if exp_ms and (exp_ms - now_ms) < 5 * 60 * 1000:
        log.debug("Claude token expires in < 5min, refreshing proactively")
        if _refresh_claude_token():
            oa = _read_claude_oauth_raw() or oa
    return oa.get("accessToken")

_claude_available = None
def claude_available():
    global _claude_available
    if _claude_available is None:
        _claude_available = _get_claude_api_key() is not None
    return _claude_available

_ollama_models_cache = None

def fetch_ollama_models():
    global _ollama_models_cache
    if _ollama_models_cache is not None:
        return _ollama_models_cache
    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1)
        _ollama_models_cache = [m["name"] for m in resp.json().get("models", [])]
        return _ollama_models_cache
    except Exception as e:
        log.debug("Ollama unavailable: %s", e)
        return []


class ChatBackend:
    """Base class for chat backends. Handles message history and threading."""

    def __init__(self, model):
        self.model = model
        self.messages = []
        self.is_streaming = False
        self._cancel = False
        self._lock = threading.Lock()

    def send(self, text, on_token, on_done, on_error=None, on_status=None):
        with self._lock:
            self.messages.append({"role": "user", "content": text})
            if len(self.messages) > MEM_MAX * 2 + 1:
                self.messages = [self.messages[0]] + self.messages[-(MEM_MAX * 2):]
        self.is_streaming = True
        self._cancel = False
        # Status callback (e.g. "refreshing auth...") — used by ClaudeChat
        self._on_status = on_status

        def _run():
            full = ""
            try:
                for chunk in self._stream_chunks():
                    if self._cancel:
                        break
                    full += chunk
                    GLib.idle_add(on_token, chunk)
            except Exception as e:
                if on_error and not full:
                    err_str = str(e)
                    log.warning("Chat error: %s", err_str)
                    if "401" in err_str or "authentication" in err_str.lower() or "token" in err_str.lower():
                        GLib.idle_add(on_error, L10n.s("err_auth"))
                    else:
                        GLib.idle_add(on_error, L10n.s("err"))
            finally:
                with self._lock:
                    if full:
                        self.messages.append({"role": "assistant", "content": full})
                self.is_streaming = False
                GLib.idle_add(on_done)

        threading.Thread(target=_run, daemon=True).start()

    def _stream_chunks(self):
        raise NotImplementedError

    def cancel(self):
        self._cancel = True


_anthropic_client = None

def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        key = _get_claude_api_key()
        if key and not key.startswith('sk-ant-'):
            # OAuth token from claude.ai — must be sent as Bearer, not x-api-key
            _anthropic_client = anthropic.Anthropic(
                api_key="placeholder",
                default_headers={"Authorization": f"Bearer {key}"},
            )
        else:
            _anthropic_client = anthropic.Anthropic(api_key=key)
    return _anthropic_client


class ClaudeChat(ChatBackend):

    def __init__(self, model=CLAUDE_MODEL):
        super().__init__(model)
        self.client = _get_anthropic_client()

    def _stream_chunks(self):
        system_prompt = ""
        api_messages = []
        for msg in self.messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            else:
                api_messages.append(msg)
        try:
            with self.client.messages.stream(
                model=self.model, max_tokens=256,
                system=system_prompt, messages=api_messages,
            ) as stream:
                yield from stream.text_stream
        except Exception as e:
            if "401" in str(e) or "authentication" in str(e).lower():
                log.warning("Auth failed, refreshing token via Claude CLI...")
                if getattr(self, "_on_status", None):
                    GLib.idle_add(self._on_status, "refreshing")
                _refresh_claude_token()
                new_key = _read_claude_oauth()
                if new_key:
                    import anthropic
                    global _anthropic_client
                    if new_key.startswith('sk-ant-'):
                        _anthropic_client = anthropic.Anthropic(api_key=new_key)
                    else:
                        _anthropic_client = anthropic.Anthropic(
                            api_key="placeholder",
                            default_headers={"Authorization": f"Bearer {new_key}"},
                        )
                    self.client = _anthropic_client
                    with self.client.messages.stream(
                        model=self.model, max_tokens=256,
                        system=system_prompt, messages=api_messages,
                    ) as stream:
                        yield from stream.text_stream
                else:
                    raise ValueError(L10n.s("err_auth"))
            else:
                raise


class OllamaChat(ChatBackend):

    def _stream_chunks(self):
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            with client.stream("POST", f"{OLLAMA_URL}/api/chat",
                               json={"model": self.model, "messages": self.messages, "stream": True}) as resp:
                for line in resp.iter_lines():
                    if self._cancel:
                        return
                    try:
                        content = json.loads(line).get("message", {}).get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        pass


_ollama_checked = None

def _ollama_available():
    global _ollama_checked
    if _ollama_checked is None:
        try:
            httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1)
            _ollama_checked = True
        except Exception:
            _ollama_checked = False
    return _ollama_checked

def create_chat(model):
    """Create the best available chat backend."""
    if model.startswith("claude-") and claude_available():
        log.debug("Using Claude API (%s)", model)
        return ClaudeChat(model)
    if not model.startswith("claude-") and _ollama_available():
        available = fetch_ollama_models()
        if available and model in available:
            log.debug("Using Ollama (%s)", model)
            return OllamaChat(model)
        elif available:
            log.debug("Model %s not in Ollama (available: %s), trying Claude", model, available)
        else:
            log.debug("Ollama running but no models installed")
    if claude_available():
        log.debug("Using Claude API (fallback)")
        return ClaudeChat(CLAUDE_MODEL)
    if _ollama_available():
        models = fetch_ollama_models()
        if models:
            log.debug("Using Ollama with first available model: %s", models[0])
            return OllamaChat(models[0])
    log.warning("No AI backend available")
    return OllamaChat(model)

# ── CSS Theme ──────────────────────────────────────────────────────────────────

CSS = b"""
.canvas-window {
    background: transparent;
}
.bubble-window {
    background: transparent;
}
.bubble-body {
    background-color: #f2e6cc;
    border: 3px solid #4d3319;
    border-radius: 4px;
    color: #4d3319;
}
.bubble-body button {
    background-color: #e6d5b8;
    color: #4d3319;
    border: 1px solid #4d3319;
}
.bubble-body button:hover {
    background-color: #d4c4a6;
}
.meow-window {
    background: transparent;
}
.settings-window {
    background-color: #f2e6cc;
    color: #4d3319;
}
.settings-window button {
    background-color: #e6d5b8;
    color: #4d3319;
}
.settings-window button:hover {
    background-color: #d4c4a6;
}
.settings-window label {
    color: #4d3319;
}
.settings-window checkbutton label {
    color: #4d3319;
}
.settings-window scale trough {
    background-color: #d4c4a6;
}
.settings-window scale highlight {
    background-color: #ff9933;
}
.pixel-label {
    font-family: monospace;
    font-weight: bold;
    color: #4d3319;
}
.pixel-label-small {
    font-family: monospace;
    font-weight: bold;
    font-size: 11px;
    color: #4d3319;
}
.pixel-entry {
    font-family: monospace;
    background-color: #fff9ee;
    border: 2px solid #4d3319;
    color: #4d3319;
    min-height: 24px;
}
.pixel-mic-btn {
    font-family: monospace;
    background-color: #fff9ee;
    background-image: none;
    border: 2px solid #4d3319;
    color: #4d3319;
    min-height: 24px;
    padding: 0 4px;
    box-shadow: none;
    text-shadow: none;
}
.pixel-mic-btn:hover {
    background-color: #f0e4d0;
}
.pixel-mic-btn-recording {
    background-color: #ffdddd;
    border-color: #cc2222;
}
.pixel-title {
    font-family: monospace;
    font-weight: bold;
    font-size: 14px;
    color: #4d3319;
}
.pixel-trait {
    font-family: monospace;
    font-size: 11px;
    color: #805020;
}
"""

def apply_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

# ── Pixel Art Drawing Helpers (Cairo) ──────────────────────────────────────────

def draw_pixel_border(ctx, w, h, px=3):
    ctx.set_source_rgba(0.95, 0.9, 0.8, 1)
    ctx.rectangle(0, 0, w, h)
    ctx.fill()
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    for rect in [(0, 0, w, px), (0, h-px, w, px), (0, 0, px, h), (w-px, 0, px, h)]:
        ctx.rectangle(*rect); ctx.fill()
    i = px * 2
    for rect in [(i, i, w-i*2, px), (i, h-i-px, w-i*2, px), (i, i, px, h-i*2), (w-i-px, i, px, h-i*2)]:
        ctx.rectangle(*rect); ctx.fill()
    ctx.set_source_rgba(0.95, 0.9, 0.8, 1)
    for cx, cy in [(0, 0), (w-px, 0), (0, h-px), (w-px, h-px)]:
        ctx.rectangle(cx, cy, px, px); ctx.fill()

def draw_pixel_tail(ctx, w, h, px=3):
    cx = w / 2
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    for row in range(5):
        bw = px * (5 - row)
        ctx.rectangle(cx - bw/2, row * px, bw, px); ctx.fill()
    ctx.set_source_rgba(0.95, 0.9, 0.8, 1)
    for row in range(4):
        bw = px * (5 - row) - px * 2
        if bw > 0:
            ctx.rectangle(cx - bw/2, row * px, bw, px); ctx.fill()

# ── Meow bubble drawing on canvas ────────────────────────────────────────────

_BUBBLE_FONT = "monospace bold 11"

def _pango_text_width(ctx, text):
    """Measure text width in pixels using Pango (supports emoji)."""
    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(Pango.FontDescription(_BUBBLE_FONT))
    layout.set_text(text, -1)
    w, _h = layout.get_pixel_size()
    return w

def _pango_show_text(ctx, text, r=0.3, g=0.2, b=0.1, a=1.0):
    """Render text with PangoCairo (supports COLRv1 emoji)."""
    ctx.set_source_rgba(r, g, b, a)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(_BUBBLE_FONT))
    lay.set_text(text, -1)
    PangoCairo.show_layout(ctx, lay)

def _pango_text_size(ctx, text):
    """Return (width, height) in pixels for text using current bubble font."""
    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(Pango.FontDescription(_BUBBLE_FONT))
    layout.set_text(text, -1)
    return layout.get_pixel_size()  # (w, h)

def _draw_meow_bubble(ctx, text, cat_x, cat_y, cat_w, cat_h=80, screen_h=None):
    """Draw a meow speech bubble above (or below) a cat on the Cairo canvas."""
    text_w, text_h = _pango_text_size(ctx, text)
    pad_x, pad_y = 12, 8
    bw = max(80, text_w + pad_x * 2)
    bh = text_h + pad_y * 2

    bx = cat_x + cat_w / 2 - bw / 2
    by = cat_y - bh - 8  # 8px gap above cat

    # Flip below cat if bubble goes off-screen top
    if by < 0:
        by = cat_y + cat_h + 8
    # Clamp horizontally
    if screen_h is not None and by + bh > screen_h:
        by = cat_y - bh - 8  # back above (last resort)
    bx = max(4, bx)

    # Background
    ctx.set_source_rgba(0.95, 0.9, 0.8, 1)
    ctx.rectangle(bx, by, bw, bh)
    ctx.fill()

    # Border (2px)
    px = 2
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.rectangle(bx, by, bw, px); ctx.fill()
    ctx.rectangle(bx, by + bh - px, bw, px); ctx.fill()
    ctx.rectangle(bx, by, px, bh); ctx.fill()
    ctx.rectangle(bx + bw - px, by, px, bh); ctx.fill()
    # Inner border
    i = px * 2
    ctx.rectangle(bx + i, by + i, bw - i*2, px); ctx.fill()
    ctx.rectangle(bx + i, by + bh - i - px, bw - i*2, px); ctx.fill()
    ctx.rectangle(bx + i, by + i, px, bh - i*2); ctx.fill()
    ctx.rectangle(bx + i + bw - i*2 - px, by + i, px, bh - i*2); ctx.fill()
    # Corner cleanup
    ctx.set_source_rgba(0.95, 0.9, 0.8, 1)
    for cx, cy in [(bx, by), (bx + bw - px, by), (bx, by + bh - px), (bx + bw - px, by + bh - px)]:
        ctx.rectangle(cx, cy, px, px); ctx.fill()

    # Text (centered)
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    tx = bx + (bw - text_w) / 2
    ty = by + (bh - text_h) / 2
    ctx.move_to(tx, ty)
    _pango_show_text(ctx, text)


def _draw_zzz(ctx, cat_x, cat_y, cat_w):
    """Draw floating ZzZ letters above a sleeping cat."""
    t = time.monotonic()
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    base_y = cat_y + cat_w * 0.25  # just above head
    for i, (size, phase, dx) in enumerate([(10, 0.0, 4), (8, 1.0, 10), (6, 2.0, 14)]):
        offset_y = ((t * 0.6 + phase) % 3.0) / 3.0
        alpha = 1.0 - offset_y * 0.7
        x = cat_x + cat_w // 2 + dx
        y = base_y - int(offset_y * 18)
        ctx.set_font_size(size)
        ctx.set_source_rgba(0.3, 0.2, 0.1, alpha)
        ctx.move_to(x, y)
        ctx.show_text("Z")


def _draw_exclamation(ctx, cat_x, cat_y, cat_w, cat_h):
    """Draw shaking !!! just above a surprised cat's head."""
    t = time.monotonic()
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    head_y = cat_y + cat_h * 0.3  # just above head
    for i, (size, dx) in enumerate([(11, -6), (9, 2), (7, 8)]):
        shake = math.sin(t * 12 + i * 2) * 2
        x = cat_x + cat_w // 2 + dx + shake
        y = head_y - i * 3
        r = 0.9 - i * 0.2
        ctx.set_font_size(size)
        ctx.set_source_rgba(r, 0.7 - i * 0.2, 0.0, 1.0)
        ctx.move_to(x, y)
        ctx.show_text("!")


def _draw_pango_symbol(ctx, text, x, y, size, r, g, b, a):
    """Render a single Unicode symbol via PangoCairo (handles emoji/symbols)."""
    ctx.save()
    ctx.move_to(x, y)
    ctx.set_source_rgba(r, g, b, a)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(f"sans bold {size}"))
    lay.set_text(text, -1)
    PangoCairo.show_layout(ctx, lay)
    ctx.restore()


def _draw_hearts(ctx, cat_x, cat_y, cat_w, cat_h):
    """Draw floating hearts above a loving cat."""
    t = time.monotonic()
    head_y = cat_y + cat_h * 0.3
    for i, (size, phase, dx) in enumerate([(10, 0.0, -2), (8, 1.2, 6), (7, 2.4, 12)]):
        offset_y = ((t * 0.5 + phase) % 3.0) / 3.0
        alpha = 1.0 - offset_y * 0.8
        x = cat_x + cat_w // 2 + dx
        y = head_y - int(offset_y * 18)
        r = 0.9 - i * 0.15
        _draw_pango_symbol(ctx, "\u2665", x, y, size, r, 0.2, 0.3, alpha)


def _draw_hurt_stars(ctx, cat_x, cat_y, cat_w, cat_h):
    """Draw spinning pain stars around a hurt cat's head."""
    t = time.monotonic()
    cx = cat_x + cat_w * 0.5
    cy = cat_y + cat_h * 0.3
    r = cat_w * 0.18
    sz = max(6, int(cat_w * 0.08))
    for i in range(3):
        angle = t * 3 + i * (2 * math.pi / 3)
        x = cx + math.cos(angle) * r
        y = cy + math.sin(angle) * r * 0.5
        sym = "\u2726" if i % 2 == 0 else "\u2727"
        _draw_pango_symbol(ctx, sym, x, y, sz, 0.95, 0.85, 0.1, 0.9)


def _draw_skull(ctx, cat_x, cat_y, cat_w, cat_h):
    """Draw a floating skull above a dying cat, rising and fading."""
    t = time.monotonic()
    offset_y = ((t * 0.4) % 2.5) / 2.5  # slow rise cycle
    alpha = 1.0 - offset_y * 0.9
    head_y = cat_y + cat_h * 0.3
    _draw_pango_symbol(ctx, "\U0001f480", cat_x + cat_w // 2 - 6, head_y - int(offset_y * 20), 12, 0.5, 0.4, 0.4, alpha)


def _draw_birth_sparkles(ctx, cat_x, cat_y, cat_w, cat_h, progress):
    """Draw swirling sparkles around a newborn kitten during birth animation.
    progress: 0.0 (just born) → 1.0 (fully grown, sparkles fade out)"""
    t = time.monotonic()
    cx = cat_x + cat_w / 2
    cy = cat_y + cat_h / 2
    radius = cat_w * 0.6
    # Sparkles fade out as progress → 1
    base_alpha = 1.0 - progress * 0.6
    for i in range(6):
        angle = t * 2 + i * (math.pi / 3)
        sx = cx + math.cos(angle) * radius
        sy = cy + math.sin(angle) * radius * 0.7
        # Each sparkle twinkles at its own phase
        twinkle = 0.5 + 0.5 * math.sin(t * 4 + i)
        alpha = base_alpha * twinkle
        size = 10 + int(twinkle * 4)
        _draw_pango_symbol(ctx, "\u2728", sx - size / 2, sy - size / 2,
                           size, 1.0, 0.95, 0.5, alpha)


def _draw_sparkle(ctx, cat_x, cat_y, cat_w, cat_h):
    """Draw a pulsing sparkle above a grooming cat."""
    t = time.monotonic()
    pulse = 0.6 + 0.4 * math.sin(t * 3)
    _draw_pango_symbol(ctx, "\u2728", cat_x + cat_w // 2 + 4, cat_y + cat_h * 0.3, 10, 0.7, 0.85, 0.95, pulse)


def _draw_anger(ctx, cat_x, cat_y, cat_w, cat_h):
    """Draw a shaking anger symbol above an angry cat."""
    t = time.monotonic()
    shake = math.sin(t * 14) * 2
    _draw_pango_symbol(ctx, "\U0001f4a2", cat_x + cat_w // 2 + shake, cat_y + cat_h * 0.25, 11, 0.85, 0.15, 0.1, 1.0)


def _draw_speed_lines(ctx, cat_x, cat_y, cat_w, cat_h, direction):
    """Speed-line overlay for DASHING cats: foot streaks + dust particles."""
    t = time.monotonic()
    east = direction == "east"
    back_x = cat_x - 4 if east else cat_x + cat_w + 4
    flush_x = cat_x if east else cat_x + cat_w
    sign = -1 if east else 1

    # Foot streaks — 3 short horizontal lines flush with the cat, in the lower half
    ctx.set_line_width(2)
    for i in range(3):
        phase = (t * 10 + i * 0.35) % 1.0
        alpha = 0.85 - phase * 0.7
        y = cat_y + cat_h * (0.60 + i * 0.10)
        length = 10 + phase * 10
        ctx.set_source_rgba(0.65, 0.55, 0.4, max(0, alpha))
        ctx.move_to(flush_x, y)
        ctx.line_to(flush_x + sign * length, y)
        ctx.stroke()

    # Dust particles — scattered circles around the cat
    for i in range(10):
        phase = (t * 6 + i * 0.25) % 1.0
        alpha = 0.75 - phase * 0.7
        dx_off = -(8 + phase * 35)
        dy_off = math.sin(i * 2.3 + t) * (cat_h * 0.18)
        x = back_x + sign * dx_off
        y = cat_y + cat_h * 0.65 + dy_off
        r = 1.5 + phase * 2
        ctx.set_source_rgba(0.72, 0.62, 0.48, max(0, alpha))
        ctx.arc(x, y, r, 0, 2 * math.pi)
        ctx.fill()


def _cairo_ellipse(ctx, cx, cy, rx, ry):
    """Draw an axis-aligned ellipse path in Cairo (no native support)."""
    ctx.save()
    ctx.translate(cx, cy)
    ctx.scale(rx, ry)
    ctx.arc(0, 0, 1, 0, 2 * math.pi)
    ctx.restore()


def _draw_playing_ball_prop(ctx, cat):
    """Red yarn ball in front of playing cat — drawn at canvas level to survive tinting."""
    ball_xs = [31, 33, 35, 33, 31, 29, 31, 33]  # sprite-pixel X per frame
    sc = cat.display_w / 68
    fi = cat.frame_index % 8
    cx = cat.x + ball_xs[fi] * sc
    cy = cat.y + 58 * sc
    r = 6 * sc
    # Main red fill
    ctx.set_source_rgb(0.80, 0.16, 0.12)
    ctx.arc(cx, cy, r, 0, 2 * math.pi)
    ctx.fill()
    # Highlight
    ctx.set_source_rgba(0.95, 0.38, 0.28, 0.55)
    ctx.arc(cx - r * 0.25, cy - r * 0.3, r * 0.55, 0, 2 * math.pi)
    ctx.fill()
    # Yarn arc lines
    ctx.set_source_rgba(0.55, 0.07, 0.05, 0.65)
    ctx.set_line_width(max(1.0, sc * 0.8))
    ctx.arc(cx, cy, r * 0.82, 0.35, 2.8)
    ctx.stroke()
    ctx.arc(cx, cy, r * 0.58, 2.1, 5.5)
    ctx.stroke()


_BUTTERFLY_YS   = [22, 18, 14, 10, 10, 14, 18, 22]  # sprite-pixel Y per frame
_BUTTERFLY_FLAP = [0,  1,  2,  3,  2,  1,  0,  1]   # wing flap index per frame


def _draw_butterfly_prop(ctx, cat):
    """Blue-purple butterfly — drawn at canvas level to survive tinting."""
    sc = cat.display_w / 68
    fi = cat.frame_index % 8
    bx = cat.x + 38 * sc
    by = cat.y + _BUTTERFLY_YS[fi] * sc
    flap = _BUTTERFLY_FLAP[fi]
    wy = (3 - abs(flap - 1)) * sc   # wing vertical spread

    # Upper wings
    ctx.set_source_rgba(0.63, 0.57, 1.0, 0.88)
    _cairo_ellipse(ctx, bx - 4 * sc, by, 3 * sc, wy + sc)
    ctx.fill()
    ctx.set_source_rgba(0.35, 0.29, 0.76, 0.88)
    _cairo_ellipse(ctx, bx + 4 * sc, by, 3 * sc, wy + sc)
    ctx.fill()
    # Lower wings
    ctx.set_source_rgba(0.35, 0.29, 0.76, 0.75)
    _cairo_ellipse(ctx, bx - 3 * sc, by + 2 * sc, 2.5 * sc, max(sc, wy * 0.5 + sc))
    ctx.fill()
    ctx.set_source_rgba(0.63, 0.57, 1.0, 0.75)
    _cairo_ellipse(ctx, bx + 3 * sc, by + 2 * sc, 2.5 * sc, max(sc, wy * 0.5 + sc))
    ctx.fill()
    # Body
    ctx.set_source_rgb(0.12, 0.09, 0.04)
    ctx.arc(bx, by, max(1.0, sc), 0, 2 * math.pi)
    ctx.fill()


def _draw_pee_drops(ctx, cat):
    """Yellow pee stream — drawn at canvas level to survive tinting."""
    fi = cat.frame_index
    if fi < 2:
        return
    sc = cat.display_w / 68
    # Stream origin: belly/crotch area at sprite x=22 (east) or x=46 (west mirror)
    sx_sprite = 22 if cat.direction == "east" else 46
    sx = cat.x + sx_sprite * sc
    sy = cat.y + 52 * sc
    ctx.set_source_rgba(1.0, 0.84, 0.08, 0.92)
    for drop in range(min(fi, 5)):
        dy = sy + drop * 5 * sc
        if dy < cat.y + cat.display_h:
            ctx.arc(sx, dy, max(1.0, sc * 1.2), 0, 2 * math.pi)
            ctx.fill()


def _draw_poop_drops(ctx, cat):
    """Brown poop drops — drawn at canvas level to survive tinting."""
    fi = cat.frame_index
    if fi < 3:
        return
    sc = cat.display_w / 68
    drops = fi - 2
    for d_idx in range(drops):
        drop_x = cat.x + 34 * sc
        drop_y = cat.y + (62 - d_idx * 4) * sc
        r = max(1.0, (3 - d_idx) * sc)
        if cat.y < drop_y < cat.y + cat.display_h:
            ctx.set_source_rgba(0.45, 0.27, 0.11, 0.95)
            ctx.arc(drop_x, drop_y, r, 0, 2 * math.pi)
            ctx.fill()
            ctx.set_source_rgba(0.27, 0.15, 0.04, 0.8)
            ctx.arc(drop_x, drop_y, r * 0.55, 0, 2 * math.pi)
            ctx.fill()


def _tree_tx(cat):
    """Tree trunk left-edge X in screen coords, for east or west direction."""
    sc = cat.display_w / 68
    # East: trunk at sprite x=47..55  West (mirrored): sprite x=12..20
    tx_sprite = 47 if cat.direction == "east" else 12
    return cat.x + tx_sprite * sc, sc


def _draw_tree_bg_cairo(ctx, cat):
    """Upper trunk (behind cat) — drawn before the cat sprite."""
    tx, sc = _tree_tx(cat)
    y0 = cat.y + 15 * sc
    y1 = cat.y + 42 * sc   # boundary where fg takes over
    h = y1 - y0
    w = 9 * sc
    # Outer dark trunk
    ctx.set_source_rgb(0.27, 0.15, 0.04)
    ctx.rectangle(tx, y0, w, h)
    ctx.fill()
    # Inner lighter bark
    ctx.set_source_rgb(0.45, 0.27, 0.11)
    ctx.rectangle(tx + sc, y0, w - 2 * sc, h)
    ctx.fill()
    # Central dark stripe
    ctx.set_source_rgb(0.27, 0.15, 0.04)
    ctx.rectangle(tx + 3 * sc, y0, 3 * sc, h)
    ctx.fill()
    # Bark texture
    ctx.set_source_rgba(0.20, 0.10, 0.02, 0.6)
    ctx.set_line_width(max(1.0, sc))
    for by in [y0 + 4 * sc, y0 + 12 * sc, y0 + 20 * sc]:
        ctx.move_to(tx + sc, by)
        ctx.line_to(tx + 2 * sc, by + 2 * sc)
        ctx.stroke()
        ctx.move_to(tx + 6 * sc, by + 3 * sc)
        ctx.line_to(tx + 7 * sc, by + 5 * sc)
        ctx.stroke()


def _draw_tree_fg_cairo(ctx, cat):
    """Lower trunk (in front of cat legs) + foliage + scratch marks."""
    tx, sc = _tree_tx(cat)
    y_split = cat.y + 42 * sc
    y_bot   = cat.y + 67 * sc
    h = y_bot - y_split
    w = 9 * sc
    # Lower trunk
    ctx.set_source_rgb(0.27, 0.15, 0.04)
    ctx.rectangle(tx, y_split, w, h)
    ctx.fill()
    ctx.set_source_rgb(0.45, 0.27, 0.11)
    ctx.rectangle(tx + sc, y_split, w - 2 * sc, h)
    ctx.fill()
    ctx.set_source_rgb(0.27, 0.15, 0.04)
    ctx.rectangle(tx + 3 * sc, y_split, 3 * sc, h)
    ctx.fill()
    # Bark texture (lower)
    ctx.set_source_rgba(0.20, 0.10, 0.02, 0.6)
    ctx.set_line_width(max(1.0, sc))
    for by in [y_split + 2 * sc, y_split + 10 * sc, y_split + 18 * sc]:
        ctx.move_to(tx + sc, by)
        ctx.line_to(tx + 2 * sc, by + 2 * sc)
        ctx.stroke()
        ctx.move_to(tx + 6 * sc, by + 3 * sc)
        ctx.line_to(tx + 7 * sc, by + 5 * sc)
        ctx.stroke()
    # Foliage (on top of everything)
    ctx.set_source_rgba(0.25, 0.57, 0.16, 0.95)
    _cairo_ellipse(ctx, tx + 5 * sc, cat.y + 10 * sc, 13 * sc, 10 * sc)
    ctx.fill()
    ctx.set_source_rgba(0.16, 0.38, 0.09, 0.95)
    _cairo_ellipse(ctx, tx - 2 * sc, cat.y + 14 * sc, 9 * sc, 8 * sc)
    ctx.fill()
    ctx.set_source_rgba(0.25, 0.57, 0.16, 0.88)
    _cairo_ellipse(ctx, tx + 12 * sc, cat.y + 12 * sc, 8 * sc, 7 * sc)
    ctx.fill()
    # Scratch marks (alternate high/low per frame)
    dy = (cat.frame_index % 2) * 3 * sc
    ctx.set_source_rgba(0.60, 0.38, 0.12, 0.85)
    ctx.set_line_width(max(1.0, sc * 0.9))
    for sx_off in [2 * sc, 4 * sc, 6 * sc]:
        sy0 = cat.y + 22 * sc + dy
        sy1 = cat.y + 40 * sc + dy
        ctx.move_to(tx + sx_off, sy0)
        ctx.line_to(tx + sx_off - sc, sy1)
        ctx.stroke()


def _draw_fly(ctx, cat_x, cat_y, cat_w, cat_h):
    """Draw a small fly circling below a pooping cat."""
    t = time.monotonic()
    angle = t * 3.5  # radians/s — orbit speed
    cx = cat_x + cat_w // 2
    cy = cat_y + cat_h - 6   # just below the cat
    rx, ry = 14, 5            # orbit ellipse radii
    fx = cx + rx * math.cos(angle)
    fy = cy + ry * math.sin(angle)
    # Tiny black dot with wings
    ctx.set_source_rgba(0.1, 0.1, 0.1, 0.9)
    ctx.arc(fx, fy, 2.5, 0, 2 * math.pi)
    ctx.fill()
    # Wings (two small semi-transparent ovals)
    wing_angle = angle + math.pi / 2
    for sign in (-1, 1):
        wx = fx + sign * 4 * math.cos(wing_angle)
        wy = fy + sign * 4 * math.sin(wing_angle)
        ctx.save()
        ctx.translate(wx, wy)
        ctx.scale(3, 1.5)
        ctx.arc(0, 0, 1, 0, 2 * math.pi)
        ctx.restore()
        ctx.set_source_rgba(0.7, 0.7, 0.9, 0.5)
        ctx.fill()


def _draw_encounter_bubble(ctx, text, cat_x, cat_y, cat_w, cat_h):
    """Draw a short encounter speech bubble above a cat (word-wrapped, no entry)."""
    pad_x, pad_y = 10, 6
    max_content_w = 260  # max text width in pixels

    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(_BUBBLE_FONT))
    lay.set_text(text, -1)
    lay.set_width(max_content_w * Pango.SCALE)
    lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    lay.set_height(-6)  # max 6 lines
    tw, th = lay.get_pixel_size()

    bw = max(90, tw + pad_x * 2)
    bh = pad_y * 2 + th
    bx = cat_x + cat_w / 2 - bw / 2
    by = cat_y - bh - 8
    if by < 4:
        by = cat_y + cat_h + 8

    ctx.set_source_rgba(0.95, 0.9, 0.8, 1)
    ctx.rectangle(bx, by, bw, bh)
    ctx.fill()
    px = 2
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    for rx, ry, rw, rh in [(bx, by, bw, px), (bx, by + bh - px, bw, px),
                            (bx, by, px, bh), (bx + bw - px, by, px, bh)]:
        ctx.rectangle(rx, ry, rw, rh); ctx.fill()
    ctx.set_source_rgba(0.95, 0.9, 0.8, 1)
    for cx, cy in [(bx, by), (bx + bw - px, by), (bx, by + bh - px), (bx + bw - px, by + bh - px)]:
        ctx.rectangle(cx, cy, px, px); ctx.fill()

    ctx.move_to(bx + pad_x, by + pad_y)
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    PangoCairo.show_layout(ctx, lay)


def _draw_chat_bubble(ctx, text, cat_x, cat_y, cat_w, cat_h):
    """Draw a chat response bubble above a cat on the Cairo canvas."""
    pad = 12
    content_w = 256  # text area = bw - 2*pad

    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(_BUBBLE_FONT))
    lay.set_text(text, -1)
    lay.set_width(content_w * Pango.SCALE)
    lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    lay.set_height(-8)  # max 8 lines
    lay.set_ellipsize(Pango.EllipsizeMode.END)
    _tw, th = lay.get_pixel_size()

    bw = content_w + pad * 2  # 280
    bh = pad * 2 + th + 42   # text + 30 entry + 12 pad
    bx = cat_x + cat_w / 2 - bw / 2
    by = cat_y - bh - 15
    if by < 0:
        by = cat_y + cat_h + 10

    # Background
    ctx.set_source_rgba(0.95, 0.9, 0.8, 0.95)
    ctx.rectangle(bx, by, bw, bh)
    ctx.fill()

    # Border
    px = 3
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.rectangle(bx, by, bw, px); ctx.fill()
    ctx.rectangle(bx, by + bh - px, bw, px); ctx.fill()
    ctx.rectangle(bx, by, px, bh); ctx.fill()
    ctx.rectangle(bx + bw - px, by, px, bh); ctx.fill()

    # Text via Pango (handles emoji width correctly)
    ctx.move_to(bx + pad, by + pad)
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    PangoCairo.show_layout(ctx, lay)

    # Tail (small triangle pointing down)
    tx = bx + bw / 2
    ty = by + bh
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.move_to(tx - 8, ty)
    ctx.line_to(tx + 8, ty)
    ctx.line_to(tx, ty + 10)
    ctx.close_path()
    ctx.fill()


# ── Chat Bubble ────────────────────────────────────────────────────────────────

def _draw_context_menu(ctx, mx, my, label_settings, label_quit):
    """Draw a context menu on the canvas."""
    bw, bh = 120, 50
    pad = 8
    ctx.set_source_rgba(0.95, 0.9, 0.8, 0.95)
    ctx.rectangle(mx, my, bw, bh)
    ctx.fill()
    # Border
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.set_line_width(2)
    ctx.rectangle(mx, my, bw, bh)
    ctx.stroke()
    # Separator
    ctx.move_to(mx + pad, my + 25)
    ctx.line_to(mx + bw - pad, my + 25)
    ctx.stroke()
    # Text
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(11)
    ctx.move_to(mx + pad, my + 17)
    ctx.show_text(label_settings)
    ctx.move_to(mx + pad, my + 42)
    ctx.show_text(label_quit)


class ChatBubbleController:
    def __init__(self, app):
        self.app = app
        self.window = None
        self.response_text = ""
        self.on_send = None
        self.bubble_w = 300
        self.tail_h = 15
        self.padding = 18
        self._entry = None
        self._response_label = None
        self._cat_pos = (0, 0, 0, 0)
        self._active_cat = None  # which CatInstance this bubble is showing for

    def setup(self):
        self.window = Gtk.Window()
        self.window.set_decorated(False)
        self.window.add_css_class("bubble-window")
        set_always_on_top(self.window)
        self.window.set_default_size(self.bubble_w, -1)
        self.window.set_resizable(False)
        self.response_text = L10n.s("hi")
        self._build()

    def _build(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.set_margin_start(self.padding)
        body.set_margin_end(self.padding)
        body.set_margin_top(6)
        body.set_margin_bottom(self.padding)
        body.add_css_class("bubble-body")

        close_btn = Gtk.Button(label="\u00d7")
        close_css = Gtk.CssProvider()
        close_css.load_from_data(b"button { background: transparent; color: #4d3319; min-width: 20px; min-height: 16px; font-size: 14px; padding: 0; border: none; }")
        close_btn.get_style_context().add_provider(close_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        close_btn.set_halign(Gtk.Align.END)
        close_btn.connect("clicked", lambda b: self._do_close())
        body.append(close_btn)

        self._response_label = Gtk.Label(label=self.response_text)
        self._response_label.set_wrap(True)
        self._response_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._response_label.set_xalign(0)
        self._response_label.set_yalign(0)
        self._response_label.set_max_width_chars(35)
        self._response_label.add_css_class("pixel-label-small")

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_max_content_height(200)
        self._scroll.set_propagate_natural_height(True)
        self._scroll.set_child(self._response_label)
        self._scroll.set_vexpand(True)
        body.append(self._scroll)

        self._entry = Gtk.Entry()
        self._entry.set_placeholder_text(L10n.s("talk"))
        self._entry.add_css_class("pixel-entry")
        self._entry.connect("activate", self._on_activate)
        body.append(self._entry)

        main_box.append(body)

        tail = Gtk.DrawingArea()
        tail.set_content_width(self.bubble_w)
        tail.set_content_height(self.tail_h)
        tail.set_draw_func(lambda a, ctx, w, h: draw_pixel_tail(ctx, w, h, 3))
        main_box.append(tail)

        self.window.set_child(main_box)

    def _on_activate(self, entry):
        text = entry.get_text().strip()
        if text and self.on_send:
            entry.set_text("")
            self.on_send(text)

    def show_for_cat(self, cat):
        """Show the chat bubble for a specific cat instance."""
        was_visible = self.window.get_visible()
        old_cat = self._active_cat

        # If switching cats, reset the response
        if old_cat is not cat:
            self._active_cat = cat
            self.on_send = cat.send_chat
            self.response_text = L10n.s("hi")
            if self._response_label:
                self._response_label.set_label(self.response_text)

        self._cat_pos = (cat.x, cat.y, cat.display_w, cat.display_h)
        bx = int(cat.x + cat.display_w / 2 - self.bubble_w / 2)
        self.window.set_visible(True)
        GLib.idle_add(self._move_above, bx, cat.y, cat.display_h)
        GLib.timeout_add(100, self._move_above, bx, cat.y, cat.display_h)
        if not was_visible:
            self._entry.grab_focus()

    def reposition(self):
        """Move bubble to follow the active cat."""
        cat = self._active_cat
        if not self.is_visible or not cat:
            return
        self._cat_pos = (cat.x, cat.y, cat.display_w, cat.display_h)
        bx = int(cat.x + cat.display_w / 2 - self.bubble_w / 2)
        GLib.idle_add(self._move_above, bx, cat.y, cat.display_h)

    def _do_close(self):
        self._active_cat = None
        self.hide()

    def _move_above(self, bx, cat_y, cat_h):
        alloc = self.window.get_allocation()
        bubble_h = max(alloc.height, 120)
        by = int(cat_y - bubble_h - 5)
        if by < 0:
            by = 0
        move_window(self.window, max(0, bx), by)
        return False

    def hide(self):
        if self.window:
            self.window.set_visible(False)
        self._active_cat = None

    @property
    def is_visible(self):
        return self.window and self.window.get_visible()

    def set_response(self, text):
        self.response_text = text
        if self._response_label:
            self._response_label.set_label(text)

    def append_response(self, token):
        self.response_text += token
        if self._response_label:
            self._response_label.set_label(self.response_text)
            if self._scroll and not getattr(self, '_scroll_pending', False):
                self._scroll_pending = True
                def _do_scroll():
                    adj = self._scroll.get_vadjustment()
                    adj.set_value(adj.get_upper())
                    self._scroll_pending = False
                    self.reposition()
                    return False
                GLib.timeout_add(200, _do_scroll)


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
        char_id = self.config.get("char_id")
        if char_id and self.color_def is _CATSET_COLOR_DEF:
            prompt = _catset_prompt(char_id, self.config["name"], lang)
        else:
            prompt = self.color_def.prompt(self.config["name"], lang)
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
        size = (dw, dh)
        self.rotations = {}
        self.animations = {}
        walk_key = "running-8-frames"

        def bg_load():
            # 1. Rotations first (cat becomes visible & clickable as soon as these are ready)
            rots = {}
            for dir_name, rel_path in meta["frames"]["rotations"].items():
                pil = load_and_tint(os.path.join(cat_dir, rel_path), self.color_def, cache_size=size)
                rots[dir_name] = pil_to_surface(pil, dw, dh)
            GLib.idle_add(lambda: self.rotations.update(rots) or False)

            # 2. Running anim (needed for WALKING)
            if walk_key in meta["frames"]["animations"]:
                walk_data = {}
                for dir_name, frame_paths in meta["frames"]["animations"][walk_key].items():
                    walk_data[dir_name] = [
                        pil_to_surface(load_and_tint(os.path.join(cat_dir, p), self.color_def, cache_size=size), dw, dh)
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
                            pil_to_surface(load_and_tint(os.path.join(cat_dir, p), self.color_def, cache_size=size), dw, dh)
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
                            pil_to_surface(load_and_tint(os.path.join(cat_dir, p), self.color_def, cache_size=size), dw, dh)
                            for p in frame_paths
                        ]
                    GLib.idle_add(lambda an=anim_name, ad=anim_data: self.animations.update({an: ad}) or False)

        if lazy:
            threading.Thread(target=bg_load, daemon=True).start()
        else:
            bg_load()  # still threaded via GLib.idle_add for surface updates

    def _load_anims_bg(self, anims, cat_dir, size):
        """Background-load remaining animations."""
        dw, dh = size
        for anim_name, dirs in anims.items():
            anim_data = {}
            for dir_name, frame_paths in dirs.items():
                anim_data[dir_name] = [
                    pil_to_surface(load_and_tint(os.path.join(cat_dir, p), self.color_def, cache_size=size), dw, dh)
                    for p in frame_paths
                ]
            GLib.idle_add(lambda an=anim_name, ad=anim_data: self.animations.update({an: ad}) or False)

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
        if self.chat_visible or self.dragging or self.in_encounter:
            return

        if self.state == CatState.IDLE:
            self.idle_ticks += 1
            r = random.random()
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

    def update_system_prompt(self, lang):
        char_id = self.config.get("char_id")
        if char_id and self.color_def is _CATSET_COLOR_DEF:
            p = _catset_prompt(char_id, self.config["name"], lang)
        else:
            p = self.color_def.prompt(self.config["name"], lang)
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
        self.selected_color_id = None
        self.selected_char_id = None
        self.current_scale = DEFAULT_SCALE
        self.current_model = ""
        self._scale_timer = None

        self.on_add = None
        self.on_remove = None
        self.on_rename = None
        self.on_add_catset = None
        self.on_remove_catset = None
        self.on_rename_catset = None
        self.on_scale_changed = None
        self.on_model_changed = None
        self.on_lang_changed = None
        self.on_encounters_changed = None
        self.get_configs = None
        self.get_preview = None
        self.get_catset_preview = None
        self._get_anim_frames = None
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
        if not self.selected_color_id and not self.selected_char_id:
            legacy = next((c for c in cfgs if c.get("color_id")), None)
            if legacy:
                self.selected_color_id = legacy["color_id"]
            else:
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

    def _on_bubble_select(self, btn, color_id):
        self.selected_color_id = color_id
        self._build()

    def _on_bubble_add(self, btn, color_id):
        if self.on_add:
            self.on_add(color_id)

    def _on_bubble_remove(self, btn, color_id):
        if self.on_remove:
            self.on_remove(color_id)

    def _on_catset_select(self, btn, char_id):
        self.selected_char_id = char_id
        self.selected_color_id = None
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

    def _on_name_changed(self, entry, color_id):
        if self.on_rename:
            self.on_rename(color_id, entry.get_text())

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
        if cat.color_def is _CATSET_COLOR_DEF:
            p = CATSET_PERSONALITIES.get(cat.config.get("char_id", "cat01"), CATSET_PERSONALITIES["cat01"])
            return p["traits"].get(lang, p["traits"]["fr"])
        return cat.color_def.traits.get(lang, cat.color_def.traits.get("fr", ""))

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

    def __init__(self, cat_a, cat_b, app):
        self.cat_a = cat_a  # initiator
        self.cat_b = cat_b  # responder / potential victim
        self.app = app
        self.active = True
        self._timers = []
        self._outcome = None  # "love" | "surprised" | "angry"

    def start(self):
        for cat in (self.cat_a, self.cat_b):
            cat.in_encounter = True
            cat.meow_visible = False
            cat.chat_visible = False

        # Decide the outcome up front so cat A shows the right initial state
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


# ── Voice recorder (optional, faster-whisper) ──────────────────────────────────

# Approx download sizes (MB) for user-facing status display
WHISPER_MODEL_SIZES = {
    "tiny": 39, "tiny.en": 39,
    "base": 74, "base.en": 74,
    "small": 244, "small.en": 244,
    "medium": 769, "medium.en": 769,
    "large-v1": 1550, "large-v2": 1550, "large-v3": 1550,
    "large-v3-turbo": 809, "turbo": 809,
    "distil-large-v3": 756,
}


def _whisper_model_cached(name):
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
        # Repo name is the last segment after the final "--"
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
    - start() begins GStreamer capture to a WAV file
    - stop_and_transcribe() ends capture, runs Whisper in a thread, calls on_result(text) on the main thread
    """
    MIN_RECORDING_MS = 300  # ignore tiny accidental presses

    def __init__(self, model_name=None):
        # Precedence: explicit arg > env var > "base" default
        self.MODEL_NAME = model_name or os.environ.get("CATAI_WHISPER_MODEL", "base")
        self._model = None
        self._pipeline = None
        self._wav_path = None
        self._recording = False
        self._start_time = 0.0

    def set_model(self, model_name):
        """Change the Whisper model. Clears the cached model so the next
        recording reloads with the new name."""
        if model_name == self.MODEL_NAME:
            return
        self.MODEL_NAME = model_name
        self._model = None  # force reload on next _ensure_model()

    def _ensure_model(self):
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

    def start(self):
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

    def stop_and_transcribe(self, lang, on_result):
        """Stop recording. Runs transcription in background thread, calls on_result(text)
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


# ── Main Application ───────────────────────────────────────────────────────────

class CatAIApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=None,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.cat_instances = []
        self.cat_configs = []
        self.cat_dir = ""
        self.meta = None
        self.sprite_w = 68
        self.sprite_h = 68
        self.display_w = 0
        self.display_h = 0
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

    def do_activate(self):
        _setup_logging()
        apply_css()
        self._check_deps()

        # Assets are inside the package directory
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        self.cat_dir = os.path.join(pkg_dir, "cute_orange_cat")
        if not os.path.isdir(self.cat_dir):
            print(f"ERROR: cute_orange_cat/ not found in {pkg_dir}")
            sys.exit(1)

        self.meta = load_metadata(self.cat_dir)
        self.sprite_w = self.meta["character"]["size"]["width"]
        self.sprite_h = self.meta["character"]["size"]["height"]

        display = Gdk.Display.get_default()
        monitors = display.get_monitors()
        if monitors.get_n_items() > 0:
            max_w, max_h = 0, 0
            for i in range(monitors.get_n_items()):
                geo = monitors.get_item(i).get_geometry()
                max_w = max(max_w, geo.x + geo.width)
                max_h = max(max_h, geo.y + geo.height)
            self.screen_w = max_w
            self.screen_h = max_h

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

        self._recompute_size()

        # Create the single fullscreen transparent canvas window
        self._create_canvas()

        # Pre-create context menu and settings (hidden) so NOTIFICATION type
        # is applied before user ever sees them (avoids GNOME "is ready" alert)
        # Settings window created on first use (not pre-opened to avoid GNOME notification)

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

    def _handle_test_cmd(self, cmd):
        """Handle a test command. Returns response string."""
        parts = cmd.split()
        if not parts:
            return "ERR: empty command"
        action = parts[0]

        if action == "status":
            return (f"OK cats={len(self.cat_instances)} canvas_xid={self._canvas_xid} "
                    f"screen={self.screen_w}x{self.screen_h} y_offset={self._canvas_y_offset}")

        elif action == "cat_positions":
            positions = [f"{c.config.get('char_id', c.config.get('color_id', '?'))}:{c.x:.0f},{c.y:.0f}" for c in self.cat_instances]
            return "OK " + " ".join(positions)

        elif action == "force_state":
            if len(parts) < 3:
                return "ERR: usage: force_state <idx> <state_name>"
            idx = int(parts[1])
            state_name = parts[2]
            if 0 <= idx < len(self.cat_instances):
                cat = self.cat_instances[idx]
                try:
                    cat.state = CatState(state_name)
                except ValueError:
                    return f"ERR: unknown state {state_name}"
                cat.frame_index = 0
                cat.idle_ticks = 0
                cat._sequence = None
                cat._sequence_index = 0
                cat._sequence_pause_ticks = 0
                if state_name in ("dashing",):
                    cat.direction = "east"
                elif state_name in ("surprised",):
                    cat.direction = "east"
                else:
                    cat.direction = "south"
                return f"OK cat {idx} -> {state_name}"
            return "ERR: invalid cat index"

        elif action == "apocalypse":
            self.toggle_apocalypse()
            return f"OK apocalypse {'ON' if self._apocalypse_active else 'OFF'}"

        elif action == "easter_menu":
            self.show_easter_menu()
            return "OK easter menu shown"

        elif action == "egg":
            if len(parts) < 2:
                return f"ERR: usage: egg <key>  (available: {[k for k,_,_,_ in EASTER_EGGS]})"
            key = parts[1]
            if not any(k == key for k, _, _, _ in EASTER_EGGS):
                return f"ERR: unknown egg {key}"
            self._trigger_easter_egg(key)
            return f"OK egg {key}"

        elif action == "love_encounter":
            if len(parts) < 3:
                return "ERR: usage: love_encounter <idx_a> <idx_b>"
            ia, ib = int(parts[1]), int(parts[2])
            if 0 <= ia < len(self.cat_instances) and 0 <= ib < len(self.cat_instances) and ia != ib:
                ca = self.cat_instances[ia]
                cb = self.cat_instances[ib]
                if self._active_encounter:
                    self._active_encounter.cancel()
                enc = LoveEncounter(ca, cb, self)
                self._active_encounter = enc
                enc.start()
                return f"OK love encounter {ia}<->{ib}"
            return "ERR: invalid indices"

        elif action == "start_sequence":
            if len(parts) < 3:
                return "ERR: usage: start_sequence <idx> <seq_name>"
            idx = int(parts[1])
            seq_name = parts[2]
            if 0 <= idx < len(self.cat_instances):
                if seq_name not in SEQUENCES:
                    return f"ERR: unknown sequence {seq_name} (available: {list(SEQUENCES.keys())})"
                self.cat_instances[idx]._start_sequence(seq_name)
                return f"OK cat {idx} -> sequence {seq_name}"
            return "ERR: invalid cat index"

        elif action == "meow":
            if len(parts) < 2:
                return "ERR: usage: meow <idx> [text]"
            idx = int(parts[1])
            text = " ".join(parts[2:]) if len(parts) > 2 else "Meow~"
            if 0 <= idx < len(self.cat_instances):
                cat = self.cat_instances[idx]
                cat.meow_text = text
                cat.meow_visible = True
                return f"OK meow on cat {idx}"
            return "ERR: invalid cat index"

        elif action == "move_cat":
            if len(parts) < 4:
                return "ERR: usage: move_cat <idx> <x> <y>"
            idx = int(parts[1])
            x = int(parts[2])
            y = int(parts[3])
            if 0 <= idx < len(self.cat_instances):
                cat = self.cat_instances[idx]
                cat.x = x
                cat.y = y
                cat._clamp_to_screen()  # honours canvas y offset and margins
                return f"OK cat {idx} at {cat.x},{cat.y}"
            return "ERR: invalid cat index"

        elif action == "fake_chat":
            if len(parts) < 3:
                return "ERR: usage: fake_chat <idx> <text>"
            idx = int(parts[1])
            text = " ".join(parts[2:])
            if 0 <= idx < len(self.cat_instances):
                cat = self.cat_instances[idx]
                cat.chat_response = text
                cat.chat_visible = True
                self._active_chat_cat = cat
                return f"OK fake chat on cat {idx}"
            return "ERR: invalid cat index"

        elif action == "click_cat":
            idx = int(parts[1]) if len(parts) > 1 else 0
            if 0 <= idx < len(self.cat_instances):
                self._toggle_chat_for(self.cat_instances[idx])
                return f"OK toggled chat for cat {idx}"
            return "ERR: invalid cat index"

        elif action == "right_click_cat":
            idx = int(parts[1]) if len(parts) > 1 else 0
            if 0 <= idx < len(self.cat_instances):
                cat = self.cat_instances[idx]
                self._menu_visible = True
                self._menu_x = int(cat.x + cat.display_w)
                self._menu_y = int(cat.y)
                return "OK menu shown"
            return "ERR: invalid cat index"

        elif action == "click_menu_settings":
            self._menu_visible = False
            self._open_settings()
            return "OK settings opened"

        elif action == "click_menu_quit":
            self._menu_visible = False
            GLib.timeout_add(100, lambda: self.quit() or False)
            return "OK quitting"

        elif action == "type_chat":
            text = " ".join(parts[1:]) if len(parts) > 1 else "coucou"
            cat = self._active_chat_cat
            if cat:
                cat.send_chat(text)
                return f"OK sent: {text}"
            return "ERR: no active chat"

        elif action == "close_chat":
            cat = self._active_chat_cat
            if cat:
                cat.chat_visible = False
                self._chat_box.set_visible(False)
                self._active_chat_cat = None
                return "OK chat closed"
            return "ERR: no active chat"

        elif action == "drag_cat":
            idx = int(parts[1]) if len(parts) > 1 else 0
            dx = int(parts[2]) if len(parts) > 2 else 100
            dy = int(parts[3]) if len(parts) > 3 else 0
            if 0 <= idx < len(self.cat_instances):
                cat = self.cat_instances[idx]
                cat.x += dx
                cat.y += dy
                return f"OK cat {idx} moved to {cat.x:.0f},{cat.y:.0f}"
            return "ERR: invalid cat index"

        elif action == "close_settings":
            if self.settings_ctrl and self.settings_ctrl.window:
                self.settings_ctrl._on_close()
                return "OK settings closed"
            return "ERR: settings not open"

        elif action == "get_chat_response":
            cat = self._active_chat_cat
            if cat:
                return f"OK {cat.chat_response}"
            return "ERR: no active chat"

        elif action == "screenshot":
            if self._canvas_area:
                self._canvas_area.queue_draw()
            return "OK redraw queued"

        return f"ERR: unknown command '{action}'"

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

        for cat in self.cat_instances:
            # Hide & seek: skip hidden cats
            if getattr(cat, '_hidden', False):
                continue
            # Defensive: always clamp before drawing (except during birth scale anim)
            if cat._birth_progress is None:
                cat._clamp_to_screen()

            # Background props (drawn BEFORE tinted sprite)
            if cat.state == CatState.SCRATCHING_TREE:
                _draw_tree_bg_cairo(ctx, cat)

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

            # Foreground props (drawn AFTER tinted sprite — correct colour for all skins)
            if cat.state == CatState.SCRATCHING_TREE:
                _draw_tree_fg_cairo(ctx, cat)
            elif cat.state == CatState.PLAYING_BALL:
                _draw_playing_ball_prop(ctx, cat)
            elif cat.state == CatState.BUTTERFLY:
                _draw_butterfly_prop(ctx, cat)
            elif cat.state == CatState.PEEING:
                _draw_pee_drops(ctx, cat)
            elif cat.state == CatState.POOPING:
                _draw_poop_drops(ctx, cat)
                _draw_fly(ctx, cat.x, cat.y, cat.display_w, cat.display_h)

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

    def _update_input_regions(self):
        """Update XShape input regions to only cover cat bounding rects."""
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
        """Position the entry inside the chat bubble (same layout as _draw_chat_bubble)."""
        text = cat.chat_response or ""
        pad = 12
        content_w = 256

        # Measure text height with Pango — must match _draw_chat_bubble exactly
        tmp = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        tctx = cairo.Context(tmp)
        lay = PangoCairo.create_layout(tctx)
        lay.set_font_description(Pango.FontDescription(_BUBBLE_FONT))
        lay.set_text(text, -1)
        lay.set_width(content_w * Pango.SCALE)
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
        lay.set_height(-8)
        lay.set_ellipsize(Pango.EllipsizeMode.END)
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
        """User pressed Enter in the chat entry."""
        text = entry.get_text().strip()
        if not text or not self._active_chat_cat:
            return
        entry.set_text("")
        cat = self._active_chat_cat
        cat.send_chat(text)

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

    def _on_canvas_drag_update(self, gesture, offset_x, offset_y):
        cat = self._drag_cat
        if not cat or not cat.dragging:
            return
        if abs(offset_x) > 3 or abs(offset_y) > 3:
            cat.mouse_moved = True
        cat.x = max(0, min(cat.drag_win_x + offset_x, cat.screen_w - cat.display_w))
        cat.y = max(0, min(cat.drag_win_y + offset_y, cat.screen_h - cat.display_h))
        # Force immediate redraw for smooth drag
        if self._canvas_area:
            self._canvas_area.queue_draw()

    def _on_canvas_drag_end(self, gesture, offset_x, offset_y):
        cat = self._drag_cat
        if cat:
            cat.dragging = False
            if not cat.mouse_moved:
                # No movement → treat as click → toggle chat
                self._toggle_chat_for(cat)
        self._drag_cat = None

    # ── Tick callbacks ───────────────────────────────────────────────────────

    def _gc_collect(self):
        gc.collect(0)
        return True

    def do_shutdown(self):
        """Clean shutdown: stop timers, cleanup cats, close windows."""
        if self._active_encounter:
            self._active_encounter.cancel()
            self._active_encounter = None
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

    def _recompute_size(self):
        self.display_w = int(round(self.sprite_w * self.cat_scale))
        self.display_h = int(round(self.sprite_h * self.cat_scale))

    def _create_instance(self, config, index):
        char_id = config.get("char_id")
        if char_id:
            # Catset character: load its own metadata
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
            cd = _CATSET_COLOR_DEF
        else:
            cd = color_def(config.get("color_id", ""))
            if not cd:
                return
            meta = self.meta
            char_dir = self.cat_dir
            dw = self.display_w
            dh = self.display_h
        inst = CatInstance(config, cd)
        start_x = random.randint(int(dw), int(self.screen_w - dw * 2))
        start_x = max(0, min(start_x, self.screen_w - dw))
        inst.setup(self, meta, char_dir,
                   dw, dh,
                   self.selected_model, L10n.lang,
                   start_x, self.screen_w, self.screen_h)
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
        for cat in self.cat_instances:
            try:
                cat.behavior_tick()
            except Exception:
                log.exception("behavior_tick crashed for %s", cat.config.get("char_id", "?"))
        return True

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
            target_h = int(round(self.sprite_h * self.cat_scale))
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

    def add_cat(self, color_id):
        cd = color_def(color_id)
        if not cd or any(c["color_id"] == color_id for c in self.cat_configs):
            return
        cfg = {"id": f"cat_{uuid.uuid4().hex[:8]}", "color_id": color_id,
               "name": cd.names.get(L10n.lang, cd.names["fr"])}
        self.cat_configs.append(cfg)
        self._save_all()
        self._create_instance(cfg, len(self.cat_instances))
        if self.settings_ctrl:
            self.settings_ctrl.selected_color_id = color_id
            self.settings_ctrl.refresh()

    def remove_cat(self, color_id):
        if len(self.cat_configs) <= 1:
            return
        idx = next((i for i, c in enumerate(self.cat_instances) if c.color_def.id == color_id), None)
        if idx is None:
            return
        cat = self.cat_instances[idx]
        # If chat bubble is showing for this cat, hide it
        if self._active_chat_cat is cat:
            cat.chat_visible = False
            self._chat_box.set_visible(False)
            self._active_chat_cat = None
        cat.cleanup()
        self.cat_instances.pop(idx)
        self.cat_configs = [c for c in self.cat_configs if c.get("color_id") != color_id]
        delete_memory(cat.config["id"])
        self._save_all()
        if self.settings_ctrl:
            first_legacy = next((c for c in self.cat_configs if c.get("color_id")), None)
            self.settings_ctrl.selected_color_id = first_legacy["color_id"] if first_legacy else None
            self.settings_ctrl.refresh()

    def rename_cat(self, color_id, name):
        for cfg in self.cat_configs:
            if cfg.get("color_id") == color_id:
                cfg["name"] = name; break
        self._save_all()
        for inst in self.cat_instances:
            if inst.config.get("color_id") == color_id:
                inst.config["name"] = name
                inst.update_system_prompt(L10n.lang)

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
            self.settings_ctrl.selected_color_id = None
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
        self._recompute_size()
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
        ctrl.get_preview = self._get_preview
        ctrl.get_catset_preview = self._get_catset_preview
        ctrl._get_anim_frames = self._get_anim_frames
        ctrl.on_add = self.add_cat
        ctrl.on_remove = self.remove_cat
        ctrl.on_rename = self.rename_cat
        ctrl.on_add_catset = self.add_catset_char
        ctrl.on_remove_catset = self.remove_catset_char
        ctrl.on_rename_catset = self.rename_catset_char
        ctrl.on_scale_changed = self.apply_new_scale
        ctrl.on_model_changed = self.set_model
        ctrl.on_lang_changed = self.set_language
        ctrl.on_encounters_changed = self.set_encounters_enabled
        ctrl.setup(self.cat_scale, self.selected_model)
        ctrl.show()

    def _get_preview(self, color_id):
        cd = color_def(color_id)
        if not cd:
            return None
        south_rel = self.meta["frames"]["rotations"].get("south")
        if not south_rel:
            return None
        path = os.path.join(self.cat_dir, south_rel)
        try:
            img = Image.open(path).convert("RGBA")
            return tint_sprite(img, cd)
        except Exception:
            return None

    def _get_anim_frames(self, color_id, size):
        """Get walking animation frames as textures for settings preview."""
        cd = color_def(color_id)
        if not cd:
            return None
        anim_key = "running-8-frames"
        direction = "east"
        frame_paths = self.meta["frames"]["animations"].get(anim_key, {}).get(direction, [])
        if not frame_paths:
            return None
        textures = []
        for p in frame_paths:
            pil = load_and_tint(os.path.join(self.cat_dir, p), cd)
            textures.append(pil_to_texture(pil, size, size))
        return textures if textures else None

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
