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

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkX11", "4.0")
from gi.repository import Gdk, GdkX11, Gio, GLib, Gtk, Pango, PangoCairo

from PIL import Image
import httpx

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
}
ONE_SHOT_STATES = {
    CatState.EATING, CatState.DRINKING, CatState.ANGRY, CatState.WAKING_UP,
    CatState.CHASING_MOUSE, CatState.PLAYING_BALL, CatState.BUTTERFLY,
    CatState.SCRATCHING_TREE, CatState.PEEING, CatState.POOPING,
    CatState.FLAT, CatState.LOVE, CatState.GROOMING, CatState.ROLLING,
    CatState.SURPRISED, CatState.JUMPING,
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


def _climb_offset(meta, cat_dir):
    """Return (y_offset, x_offset_east, x_offset_west) in sprite pixels.
    y_offset  > 0 → shift self.y up (decrease) after climbing
    x_offset* > 0 → shift self.x right (increase) after climbing
    """
    try:
        south_rel = meta["frames"]["rotations"].get("south", "")
        ref_img = Image.open(os.path.join(cat_dir, south_rel)).convert("RGBA")
        ref_floor = _sprite_floor_y(ref_img)
        ref_cx = _sprite_center_x(ref_img)

        y_off, x_east, x_west = 0, 0, 0

        for direction in ("east", "west"):
            paths = meta["frames"]["animations"].get("climbing", {}).get(direction, [])
            if not paths:
                continue
            last_img = Image.open(os.path.join(cat_dir, paths[-1])).convert("RGBA")
            if direction == "east":
                y_off = ref_floor - _sprite_floor_y(last_img)
                x_east = _sprite_center_x(last_img) - ref_cx   # + → cat shifted right
            else:
                x_west = _sprite_center_x(last_img) - ref_cx   # - → cat shifted left

        return y_off, x_east, x_west
    except Exception:
        return 0, 0, 0


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

def _read_claude_oauth():
    try:
        if os.path.exists(CLAUDE_CREDS):
            mode = os.stat(CLAUDE_CREDS).st_mode
            if mode & 0o077:
                log.warning("Credentials file %s is accessible by others (mode %o)", CLAUDE_CREDS, mode)
        with open(CLAUDE_CREDS) as f:
            return json.load(f)["claudeAiOauth"]["accessToken"]
    except Exception:
        return None

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

    def send(self, text, on_token, on_done, on_error=None):
        with self._lock:
            self.messages.append({"role": "user", "content": text})
            if len(self.messages) > MEM_MAX * 2 + 1:
                self.messages = [self.messages[0]] + self.messages[-(MEM_MAX * 2):]
        self.is_streaming = True
        self._cancel = False

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

def _draw_meow_bubble(ctx, text, cat_x, cat_y, cat_w):
    """Draw a meow speech bubble above a cat on the Cairo canvas."""
    font_size = 11
    text_w = _pango_text_width(ctx, text)
    pad_x = 12
    bw = max(80, text_w + pad_x * 2)
    bh = 24
    bx = cat_x + cat_w / 2 - bw / 2
    by = cat_y - 40

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

    # Text
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    tx = bx + (bw - text_w) / 2
    ty = by + bh / 2 - font_size / 2
    ctx.move_to(tx, ty)
    _pango_show_text(ctx, text)


def _draw_zzz(ctx, cat_x, cat_y, cat_w):
    """Draw floating ZzZ letters above a sleeping cat."""
    t = time.monotonic()
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    # Three Z's at different phases, drifting upward
    for i, (size, phase, dx) in enumerate([(10, 0.0, 4), (8, 1.0, 10), (6, 2.0, 14)]):
        offset_y = ((t * 0.6 + phase) % 3.0) / 3.0  # 0..1 float cycle
        alpha = 1.0 - offset_y * 0.7  # fade out as it rises
        x = cat_x + cat_w // 2 + dx
        y = cat_y - 8 - int(offset_y * 22)
        ctx.set_font_size(size)
        ctx.set_source_rgba(0.3, 0.2, 0.1, alpha)
        ctx.move_to(x, y)
        ctx.show_text("Z")


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
        self._climb_y_offset = 0       # display-px to shift Y up after climbing anim
        self._climb_x_offset_east = 0  # display-px to shift X right after east climb
        self._climb_x_offset_west = 0  # display-px to shift X right after west climb
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

        self.load_assets(meta, cat_dir)
        self.setup_chat(model, lang)

        # Compute sprite-pixel offsets to align next anim after climbing
        sprite_w = meta["character"]["size"]["width"]
        sprite_h = meta["character"]["size"]["height"]
        y_off, x_east, x_west = _climb_offset(meta, cat_dir)
        scale_x = dw / sprite_w
        scale_y = dh / sprite_h
        self._climb_y_offset = round(y_off * scale_y)
        self._climb_x_offset_east = round(x_east * scale_x)   # shift right after east climb
        self._climb_x_offset_west = round(x_west * scale_x)   # shift (negative) after west

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
        """Load sprites as cairo.ImageSurface with disk cache."""
        dw, dh = self.display_w, self.display_h
        size = (dw, dh)

        # Always load rotations immediately (8 sprites, fast)
        self.rotations = {}
        for dir_name, rel_path in meta["frames"]["rotations"].items():
            pil = load_and_tint(os.path.join(cat_dir, rel_path), self.color_def, cache_size=size)
            self.rotations[dir_name] = pil_to_surface(pil, dw, dh)

        # Load walking animation immediately (needed right away)
        self.animations = {}
        walk_key = "running-8-frames"
        if walk_key in meta["frames"]["animations"]:
            self.animations[walk_key] = {}
            for dir_name, frame_paths in meta["frames"]["animations"][walk_key].items():
                self.animations[walk_key][dir_name] = [
                    pil_to_surface(load_and_tint(os.path.join(cat_dir, p), self.color_def, cache_size=size), dw, dh)
                    for p in frame_paths
                ]

        if lazy:
            remaining = {k: v for k, v in meta["frames"]["animations"].items() if k != walk_key}
            if remaining:
                threading.Thread(target=self._load_anims_bg, args=(remaining, cat_dir, size), daemon=True).start()
        else:
            for anim_name, dirs in meta["frames"]["animations"].items():
                if anim_name == walk_key:
                    continue
                self.animations[anim_name] = {}
                for dir_name, frame_paths in dirs.items():
                    self.animations[anim_name][dir_name] = [
                        pil_to_surface(load_and_tint(os.path.join(cat_dir, p), self.color_def, cache_size=size), dw, dh)
                        for p in frame_paths
                    ]

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
        elif self.state == CatState.SLEEPING_BALL:
            # Advance breathing frame every 6 render ticks (~0.75s per frame, ~3s per breath)
            self._sleep_tick = getattr(self, '_sleep_tick', 0) + 1
            if self._sleep_tick >= 6:
                self._sleep_tick = 0
                self.frame_index = (self.frame_index + 1) % 4
        elif self.state in ONE_SHOT_STATES:
            key = ANIM_KEYS.get(self.state)
            if key:
                frames = self.animations.get(key, {}).get(self.direction, [])
                if not frames:
                    # Animation absent for this character — fall back to IDLE immediately
                    self.state = CatState.IDLE
                    self.frame_index = 0
                    self.idle_ticks = 0
                elif self.frame_index >= len(frames) - 1:
                    self.state = CatState.IDLE
                    self.frame_index = 0
                    self.idle_ticks = 0
                else:
                    self.frame_index += 1
        elif self.state == CatState.CLIMBING:
            frames = self.animations.get("climbing", {}).get(self.direction, [])
            if not frames:
                self.state = CatState.IDLE
                self.frame_index = 0
                self.idle_ticks = 0
            elif self.frame_index >= len(frames) - 1:
                # Compensate for visual floor/center shift within the sprite
                self.y -= self._climb_y_offset
                if self.direction == "east":
                    self.x += self._climb_x_offset_east
                else:
                    self.x += self._climb_x_offset_west
                self.x = max(0, min(self.x, self.screen_w - self.display_w))
                if self.y < 0:
                    self.y = self.screen_h - self.display_h
                self.state = CatState.IDLE
                self.frame_index = 0
                self.idle_ticks = 0
            else:
                self.frame_index += 1

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
            elif r < 0.80:
                self.state = CatState.CLIMBING
                self.frame_index = 0
                self.direction = random.choice(["east", "west"])
            elif r < 0.85:
                self.state = CatState.ANGRY
                self.frame_index = 0
                self.direction = "south"
        elif self.state == CatState.SLEEPING_BALL:
            self.idle_ticks += 1
            if self.idle_ticks > random.randint(5, 15):
                self.state = CatState.WAKING_UP
                self.frame_index = 0
                self.idle_ticks = 0


    def send_chat(self, text):
        if self.chat_backend.is_streaming:
            return
        self.chat_response = "..."
        self.chat_visible = True
        self.state = CatState.EATING
        self.frame_index = 0

        first_token = True
        def on_token(token):
            nonlocal first_token
            if first_token:
                self.chat_response = token
                first_token = False
            else:
                self.chat_response += token
            return False

        def on_done():
            self.state = CatState.IDLE
            self.frame_index = 0
            self.idle_ticks = 0
            if self.chat_backend:
                save_memory(self.config["id"], self.chat_backend.messages)
            return False

        def on_error(msg):
            self.chat_response = msg
            self.state = CatState.IDLE
            self.frame_index = 0
            return False

        self.chat_backend.send(text, on_token, on_done, on_error)

    def update_system_prompt(self, lang):
        char_id = self.config.get("char_id")
        if char_id and self.color_def is _CATSET_COLOR_DEF:
            p = _catset_prompt(char_id, self.config["name"], lang)
        else:
            p = self.color_def.prompt(self.config["name"], lang)
        if self.chat_backend.messages:
            self.chat_backend.messages[0] = {"role": "system", "content": p}

    def _show_random_meow(self):
        if self.chat_visible:
            return
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
        self.display_w = new_w
        self.display_h = new_h
        self.load_assets(meta or self._meta, cat_dir or self._cat_dir, lazy=False)

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
            self.window.set_default_size(340, 720)
            self.window.set_resizable(False)
            self.window.add_css_class("settings-window")
            self.window.connect("close-request", self._on_close)
        if not self.selected_color_id:
            cfgs = self.get_configs() if self.get_configs else []
            # Find first legacy (color_id) config to pre-select
            legacy = next((c for c in cfgs if c.get("color_id")), None)
            if legacy:
                self.selected_color_id = legacy["color_id"]
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
            border_color = '#4d3319' if is_active else 'transparent'
            btn_css.load_from_data(f"""
                button {{ background: transparent; padding: 2px;
                         border: 2px solid {border_color};
                         border-radius: 6px; opacity: {1.0 if is_active else 0.4}; }}
                button:hover {{ opacity: 1.0; }}
            """.encode())
            btn.get_style_context().add_provider(btn_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            if is_active:
                pass  # no select-highlight for catset (no details panel)
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

    def _on_catset_add(self, btn, char_id):
        if self.on_add_catset:
            self.on_add_catset(char_id)

    def _on_catset_remove(self, btn, char_id):
        if self.on_remove_catset:
            self.on_remove_catset(char_id)

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
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        cooldown_until = time.monotonic() + self.COOLDOWN
        for cat in (self.cat_a, self.cat_b):
            cat.state = CatState.IDLE
            cat.in_encounter = False
            cat.encounter_visible = False
            cat.encounter_text = ""
            cat._encounter_cooldown_until = cooldown_until


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
        conn, _ = self._test_sock.accept()
        conn.setblocking(True)
        data = conn.recv(4096).decode().strip()
        response = self._handle_test_cmd(data)
        conn.sendall((response + "\n").encode())
        conn.close()
        return True

    def _handle_test_cmd(self, cmd):
        """Handle a test command. Returns response string."""
        parts = cmd.split()
        if not parts:
            return "ERR: empty command"
        action = parts[0]

        if action == "status":
            return f"OK cats={len(self.cat_instances)} canvas_xid={self._canvas_xid}"

        elif action == "cat_positions":
            positions = [f"{c.config['color_id']}:{c.x:.0f},{c.y:.0f}" for c in self.cat_instances]
            return "OK " + " ".join(positions)

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
                self._chat_entry.set_visible(False)
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

        # Chat input entry inside the overlay (visible + positioned dynamically)
        self._chat_entry = Gtk.Entry()
        self._chat_entry.set_placeholder_text(L10n.s("talk"))
        self._chat_entry.add_css_class("pixel-entry")
        self._chat_entry.set_halign(Gtk.Align.START)
        self._chat_entry.set_valign(Gtk.Align.START)
        self._chat_entry.set_size_request(256, -1)
        self._chat_entry.set_visible(False)
        self._chat_entry.connect("activate", self._on_chat_entry_activate)
        overlay.add_overlay(self._chat_entry)

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

        for cat in self.cat_instances:
            # Background props (drawn BEFORE tinted sprite)
            if cat.state == CatState.SCRATCHING_TREE:
                _draw_tree_bg_cairo(ctx, cat)

            surface, _data_ref = cat._current_surface()
            ctx.save()
            ctx.rectangle(cat.x, cat.y, cat.display_w, cat.display_h)
            ctx.clip()
            ctx.set_source_surface(surface, cat.x, cat.y)
            ctx.paint()
            ctx.restore()

            # Draw ZzZ if sleeping in a ball
            if cat.state == CatState.SLEEPING_BALL:
                _draw_zzz(ctx, cat.x, cat.y, cat.display_w)

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
                _draw_meow_bubble(ctx, cat.meow_text, cat.x, cat.y, cat.display_w)

            # Draw chat response bubble if visible
            if cat.chat_visible and cat.chat_response:
                _draw_chat_bubble(ctx, cat.chat_response, cat.x, cat.y, cat.display_w, cat.display_h)

            # Draw encounter bubble if visible
            if cat.encounter_visible and cat.encounter_text:
                _draw_encounter_bubble(ctx, cat.encounter_text, cat.x, cat.y, cat.display_w, cat.display_h)

        # Draw context menu if visible
        if self._menu_visible:
            _draw_context_menu(ctx, self._menu_x, self._menu_y, L10n.s("settings"), L10n.s("quit"))

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
        # Include chat entry area
        # Include chat entry in input region when visible
        if self._chat_entry and self._chat_entry.get_visible():
            rects.append((self._chat_entry.get_margin_start(),
                         self._chat_entry.get_margin_top(), 260, 30))
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
        if cat.chat_visible:
            cat.chat_visible = False
            self._chat_entry.set_visible(False)
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
            self._chat_entry.set_visible(True)
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
        self._chat_entry.set_margin_start(max(0, entry_x))
        self._chat_entry.set_margin_top(max(0, entry_y))

    def _on_chat_entry_activate(self, entry):
        """User pressed Enter in the chat entry."""
        text = entry.get_text().strip()
        if not text or not self._active_chat_cat:
            return
        entry.set_text("")
        cat = self._active_chat_cat
        cat.send_chat(text)

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
        # Check menu click first
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
        self._chat_entry.set_visible(False)
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
        })

    def _render_tick(self):
        t0 = time.monotonic()
        for cat in self.cat_instances:
            cat.render_tick()
        self._check_encounters()
        # Reposition chat entry if following a walking cat
        if self._active_chat_cat and self._chat_entry.get_visible():
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
            cat.behavior_tick()
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
                    enc = CatEncounter(ca, cb, self)
                    self._active_encounter = enc
                    enc.start()
                    log.debug("Encounter started: %s ↔ %s (dist=%.0f)",
                              ca.config["name"], cb.config["name"], dist)
                    return

    def set_encounters_enabled(self, enabled):
        self.encounters_enabled = enabled
        if not enabled and self._active_encounter:
            self._active_encounter.cancel()
            self._active_encounter = None
        self._save_all()

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
            self._chat_entry.set_visible(False)
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
            self._chat_entry.set_visible(False)
            self._active_chat_cat = None
        cat.cleanup()
        self.cat_instances.pop(idx)
        self.cat_configs = [c for c in self.cat_configs if c.get("char_id") != char_id]
        delete_memory(cat.config["id"])
        self._save_all()
        if self.settings_ctrl:
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
    gtk_args = [a for a in sys.argv if a not in ("--debug", "--test-socket")]
    app = CatAIApp()
    app.run(gtk_args)

if __name__ == "__main__":
    main()
