#!/usr/bin/env python3
"""CATAI-Linux — Virtual desktop pet cats for Linux (GNOME/Wayland)
Port of https://github.com/wil-pe/CATAI (macOS) to GTK4.
Single fullscreen transparent canvas with Cairo rendering.
"""

# Force X11 backend — needed for XShape input passthrough + chat bubble positioning
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
from gi.repository import Gdk, GdkX11, Gio, GLib, Gtk, Pango

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


ANIM_KEYS = {
    CatState.WALKING: "running-8-frames",
    CatState.EATING: "eating",
    CatState.DRINKING: "drinking",
    CatState.ANGRY: "angry",
    CatState.WAKING_UP: "waking-getting-up",
}
ONE_SHOT_STATES = {CatState.EATING, CatState.DRINKING, CatState.ANGRY, CatState.WAKING_UP}

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
    if color_def.id == "orange":
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
    if cache_size:
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

    if cache_size:
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
    window.connect("realize", lambda w: GLib.idle_add(
        lambda: _apply_xid_hints(w, above=True) or False))

def set_notification_type(window):
    """Mark window as NOTIFICATION type only."""
    _notification_windows.append(window)
    window.connect("realize", lambda w: GLib.idle_add(
        lambda: _apply_xid_hints(w, notification=True) or False))

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
        _anthropic_client = anthropic.Anthropic(api_key=_get_claude_api_key())
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
                log.warning("Auth failed, refreshing token...")
                new_key = _read_claude_oauth()
                if new_key:
                    import anthropic
                    global _anthropic_client
                    _anthropic_client = anthropic.Anthropic(api_key=new_key)
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
.cat-window {
    background: transparent;
}
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

def _draw_meow_bubble(ctx, text, cat_x, cat_y, cat_w):
    """Draw a meow speech bubble above a cat on the Cairo canvas."""
    font_size = 11
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(font_size)
    extents = ctx.text_extents(text)
    text_w = extents.width
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
    ty = by + bh / 2 + font_size / 3
    ctx.move_to(tx, ty)
    ctx.show_text(text)


# ── Chat Bubble ────────────────────────────────────────────────────────────────

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
        self.window = Gtk.Window(application=self.app)
        self.window.set_decorated(False)
        self.window.add_css_class("bubble-window")
        self.window.set_default_size(self.bubble_w, -1)
        self.window.set_resizable(False)
        self.response_text = L10n.s("hi")
        self._build()
        set_always_on_top(self.window)
        set_notification_type(self.window)

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
        # Meow bubble state (drawn on canvas)
        self.meow_text = ""
        self.meow_visible = False
        self._meow_timer_id = None

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

        self.load_assets(meta, cat_dir)
        self.setup_chat(model, lang)

    def setup_chat(self, model, lang):
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
        if self.state in (CatState.IDLE, CatState.SLEEPING):
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
                self.frame_index += 1
            self.x = max(0, min(self.x, self.screen_w - self.display_w))
            self.y = max(0, min(self.y, self.screen_h - self.display_h))
            # Reposition chat bubble if visible for this cat
            if self._app and self._app.chat_bubble and self._app.chat_bubble._active_cat is self:
                self._app.chat_bubble.reposition()
        elif self.state in ONE_SHOT_STATES:
            key = ANIM_KEYS.get(self.state)
            if key:
                frames = self.animations.get(key, {}).get(self.direction, [])
                if frames and self.frame_index >= len(frames) - 1:
                    self.state = CatState.IDLE
                    self.frame_index = 0
                    self.idle_ticks = 0
                else:
                    self.frame_index += 1

    def behavior_tick(self):
        if self._app and self._app.chat_bubble and self._app.chat_bubble._active_cat is self and self._app.chat_bubble.is_visible:
            return
        if self.dragging:
            return

        if self.state == CatState.IDLE:
            self.idle_ticks += 1
            r = random.random()
            if self.idle_ticks > 15 and r < 0.05:
                self.state = CatState.SLEEPING
                self.idle_ticks = 0
            elif r < 0.25:
                self.state = CatState.WALKING
                self.frame_index = 0
                self.dest_x = random.uniform(self.display_w, max(self.display_w + 1, self.screen_w - self.display_w))
                self.dest_y = random.randint(int(self.screen_h * 0.3), self.screen_h - self.display_h)
            elif r < 0.30:
                self.state = CatState.EATING
                self.frame_index = 0
            elif r < 0.35:
                self.state = CatState.DRINKING
                self.frame_index = 0
            elif r < 0.38:
                self._show_random_meow()
        elif self.state == CatState.SLEEPING:
            self.idle_ticks += 1
            if self.idle_ticks > random.randint(5, 15):
                self.state = CatState.WAKING_UP
                self.frame_index = 0
                self.idle_ticks = 0

    def _toggle_chat(self):
        bubble = self._app.chat_bubble
        if bubble._active_cat is self and bubble.is_visible:
            bubble.hide()
        else:
            bubble.show_for_cat(self)

    def send_chat(self, text):
        if self.chat_backend.is_streaming:
            return
        bubble = self._app.chat_bubble
        bubble.set_response("...")
        bubble.show_for_cat(self)
        self.state = CatState.EATING
        self.frame_index = 0

        first_token = True
        def on_token(token):
            nonlocal first_token
            if first_token:
                bubble.set_response(token)
                first_token = False
            else:
                bubble.append_response(token)
            return False

        def on_done():
            self.state = CatState.IDLE
            self.frame_index = 0
            self.idle_ticks = 0
            if self.chat_backend:
                save_memory(self.config["id"], self.chat_backend.messages)
            return False

        def on_error(msg):
            bubble.set_response(msg)
            self.state = CatState.IDLE
            self.frame_index = 0
            return False

        self.chat_backend.send(text, on_token, on_done, on_error)

    def update_system_prompt(self, lang):
        p = self.color_def.prompt(self.config["name"], lang)
        if self.chat_backend.messages:
            self.chat_backend.messages[0] = {"role": "system", "content": p}

    def _show_random_meow(self):
        if self._app and self._app.chat_bubble and self._app.chat_bubble._active_cat is self and self._app.chat_bubble.is_visible:
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

    def apply_scale(self, new_w, new_h, meta, cat_dir):
        self.display_w = new_w
        self.display_h = new_h
        self.load_assets(meta, cat_dir, lazy=False)

    def cleanup(self):
        if self.chat_backend:
            self.chat_backend.cancel()
        if self._meow_timer_id:
            GLib.source_remove(self._meow_timer_id)
            self._meow_timer_id = None
        self.meow_visible = False
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
        self.on_scale_changed = None
        self.on_model_changed = None
        self.on_lang_changed = None
        self.get_configs = None
        self.get_preview = None
        self._get_anim_frames = None
        self._anim_pictures = []
        self._anim_timer = None

    def setup(self, scale, model):
        self.current_scale = scale
        self.current_model = model
        if not self.window:
            self.window = Gtk.Window()
            self.window.set_title("~ Cat Settings ~")
            self.window.set_default_size(340, 680)
            self.window.set_resizable(False)
            self.window.add_css_class("settings-window")
            self.window.connect("close-request", self._on_close)
        if not self.selected_color_id:
            cfgs = self.get_configs() if self.get_configs else []
            if cfgs:
                self.selected_color_id = cfgs[0]["color_id"]
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
        active_ids = {c["color_id"] for c in configs}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

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

        # Cat sprite selector
        bubbles_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bubbles_box.set_halign(Gtk.Align.CENTER)
        self._anim_pictures = []
        for c in CAT_COLORS:
            is_active = c.id in active_ids
            is_sel = self.selected_color_id == c.id and is_active
            cat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

            btn = Gtk.Button()
            sprite_size = 40
            pic = Gtk.Picture()
            pic.set_size_request(sprite_size, sprite_size)
            pic.set_can_shrink(True)
            if self.get_preview:
                pil_img = self.get_preview(c.id)
                if pil_img:
                    pic.set_paintable(pil_to_texture(pil_img, sprite_size, sprite_size))
            btn.set_child(pic)
            btn_css = Gtk.CssProvider()
            border_color = '#ffcc33' if is_sel else ('#4d3319' if is_active else 'transparent')
            btn_css.load_from_data(f"""
                button {{ background: transparent; padding: 2px;
                         border: {3 if is_sel else 2}px solid {border_color};
                         border-radius: 6px; opacity: {1.0 if is_active else 0.4}; }}
                button:hover {{ opacity: 1.0; }}
            """.encode())
            btn.get_style_context().add_provider(btn_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            if is_active:
                btn.connect("clicked", self._on_bubble_select, c.id)
                if self._get_anim_frames:
                    frames = self._get_anim_frames(c.id, sprite_size)
                    if frames:
                        self._anim_pictures.append((pic, frames, [0]))
            else:
                btn.connect("clicked", self._on_bubble_add, c.id)
            cat_box.append(btn)

            if is_active and len(active_ids) > 1:
                rm_btn = Gtk.Button(label="\u00d7")
                rm_css = Gtk.CssProvider()
                rm_css.load_from_data(b"button { background: #cc3333; color: white; border-radius: 50%; min-width: 16px; min-height: 16px; font-size: 10px; padding: 0; }")
                rm_btn.get_style_context().add_provider(rm_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                rm_btn.set_halign(Gtk.Align.CENTER)
                rm_btn.connect("clicked", self._on_bubble_remove, c.id)
                cat_box.append(rm_btn)

            bubbles_box.append(cat_box)
        box.append(bubbles_box)

        if getattr(self, '_anim_timer', None):
            GLib.source_remove(self._anim_timer)
            self._anim_timer = None
        if self._anim_pictures:
            self._anim_timer = GLib.timeout_add(150, self._animate_previews)

        # Selected cat details
        if self.selected_color_id and self.selected_color_id in active_ids:
            cd = color_def(self.selected_color_id)
            cfg = next((c for c in configs if c["color_id"] == self.selected_color_id), None)
            if cd:
                if self.get_preview:
                    pil_img = self.get_preview(self.selected_color_id)
                    if pil_img:
                        pic = Gtk.Picture()
                        pic.set_paintable(pil_to_texture(pil_img, 48, 48))
                        pic.set_size_request(48, 48)
                        pic.set_halign(Gtk.Align.CENTER)
                        pic.set_margin_top(8)
                        box.append(pic)

                name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                name_box.set_margin_top(8)
                nl = Gtk.Label(label=L10n.s("name"))
                nl.add_css_class("pixel-label-small")
                name_box.append(nl)
                ne = Gtk.Entry()
                ne.set_text(cfg["name"] if cfg else cd.names.get(L10n.lang, ""))
                ne.set_max_length(30)
                ne.add_css_class("pixel-entry")
                ne.set_hexpand(True)
                ne.connect("changed", self._on_name_changed, self.selected_color_id)
                name_box.append(ne)
                box.append(name_box)

                trait = Gtk.Label(label=f"\u2726 {cd.traits.get(L10n.lang, '')}")
                trait.add_css_class("pixel-trait")
                trait.set_xalign(0)
                trait.set_margin_start(4)
                box.append(trait)

                skill = Gtk.Label(label=cd.skills.get(L10n.lang, ""))
                skill.add_css_class("pixel-trait")
                skill.set_xalign(0)
                skill.set_margin_start(4)
                box.append(skill)

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
        self.chat_bubble = None
        self._context_menu = None
        self._menu_timer = None
        self._canvas_window = None
        self._canvas_area = None
        self._canvas_xid = None
        self._timers = []
        # Drag state for canvas
        self._drag_cat = None
        self._drag_offset_x = 0.0
        self._drag_offset_y = 0.0

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
        self.cat_configs = cfg.get("cats", [])

        if not self.cat_configs:
            self.cat_configs = [{
                "id": f"cat_{uuid.uuid4().hex[:8]}",
                "color_id": "orange",
                "name": CAT_COLORS[0].names.get(L10n.lang, "Citrouille"),
            }]
            self._save_all()

        self._recompute_size()

        # Create the single fullscreen transparent canvas window
        self._create_canvas()

        # Create shared chat bubble
        self.chat_bubble = ChatBubbleController(self)
        self.chat_bubble.setup()

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

    def _create_canvas(self):
        """Create the single fullscreen transparent overlay window."""
        win = Gtk.Window(application=self)
        win.set_decorated(False)
        win.add_css_class("canvas-window")
        win.set_default_size(self.screen_w, self.screen_h)
        win.set_resizable(False)

        area = Gtk.DrawingArea()
        area.set_content_width(self.screen_w)
        area.set_content_height(self.screen_h)
        area.set_draw_func(self._canvas_draw)
        win.set_child(area)

        # Gesture controllers on the canvas
        # Right-click for context menu
        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("released", self._on_canvas_right_click)
        area.add_controller(rclick)

        # Left: drag gesture handles both click and drag
        # - Short drag (no movement) = click → toggle chat
        # - Long drag = move cat
        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_canvas_drag_begin)
        drag.connect("drag-update", self._on_canvas_drag_update)
        drag.connect("drag-end", self._on_canvas_drag_end)
        area.add_controller(drag)

        set_always_on_top(win)
        set_notification_type(win)

        # We need the XID once realized, for XShape input passthrough
        def _on_realize(w):
            def _apply():
                _apply_xid_hints(w, above=True, notification=True)
                xid = _get_xid(w)
                if xid:
                    self._canvas_xid = xid
                    # Set initial empty input shape so clicks pass through
                    _update_input_shape(xid, [])
                return False
            GLib.idle_add(_apply)
        win.connect("realize", _on_realize)

        win.set_visible(True)
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
            surface, _data_ref = cat._current_surface()
            ctx.save()
            ctx.set_source_surface(surface, cat.x, cat.y)
            ctx.paint()
            ctx.restore()

            # Draw meow bubble if visible
            if cat.meow_visible and cat.meow_text:
                _draw_meow_bubble(ctx, cat.meow_text, cat.x, cat.y, cat.display_w)

    def _update_input_regions(self):
        """Update XShape input regions to only cover cat bounding rects."""
        if not self._canvas_xid:
            return
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
        _update_input_shape(self._canvas_xid, rects)

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
        if self.chat_bubble._active_cat is cat and self.chat_bubble.is_visible:
            self.chat_bubble.hide()
        else:
            self.chat_bubble.hide()
            self.chat_bubble.show_for_cat(cat, self.screen_w, self.screen_h)

    def _on_canvas_right_click(self, gesture, n_press, x, y):
        cat = self._find_cat_at(x, y)
        if not cat:
            return
        menu = self._context_menu
        if menu and menu.get_visible():
            menu.set_visible(False)
            return
        if not menu:
            menu = Gtk.Window(application=self)
            menu.set_decorated(False)
            menu.set_resizable(False)
            menu.add_css_class("bubble-body")
            set_always_on_top(menu)
            set_notification_type(menu)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(8)
            box.set_margin_end(8)

            btn_settings = Gtk.Button(label=L10n.s("settings"))
            btn_settings.add_css_class("pixel-label-small")
            btn_settings.connect("clicked", lambda b: (menu.set_visible(False), self._open_settings()))
            box.append(btn_settings)

            btn_quit = Gtk.Button(label=L10n.s("quit"))
            btn_quit.add_css_class("pixel-label-small")
            btn_quit.connect("clicked", lambda b: self.quit())
            box.append(btn_quit)

            menu.set_child(box)
            self._context_menu = menu

        menu.set_visible(True)
        GLib.idle_add(lambda: move_window(menu, int(cat.x + cat.display_w), int(cat.y)) or False)

        def _auto_close_menu():
            self._menu_timer = None
            menu.set_visible(False)
            return False
        if self._menu_timer:
            GLib.source_remove(self._menu_timer)
        self._menu_timer = GLib.timeout_add(5000, _auto_close_menu)

    def _on_canvas_drag_begin(self, gesture, start_x, start_y):
        cat = self._find_cat_at(start_x, start_y)
        if not cat:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._drag_cat = cat
        cat.dragging = True
        cat.mouse_moved = False
        cat.drag_win_x = cat.x
        cat.drag_win_y = cat.y
        self._drag_offset_x = start_x - cat.x
        self._drag_offset_y = start_y - cat.y

    def _on_canvas_drag_update(self, gesture, offset_x, offset_y):
        cat = self._drag_cat
        if not cat or not cat.dragging:
            return
        if abs(offset_x) > 3 or abs(offset_y) > 3:
            cat.mouse_moved = True
        cat.x = max(0, min(cat.drag_win_x + offset_x, cat.screen_w - cat.display_w))
        cat.y = max(0, min(cat.drag_win_y + offset_y, cat.screen_h - cat.display_h))
        # Force immediate redraw for smooth drag
        if self._canvas:
            self._canvas.queue_draw()

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
        for tid in self._timers:
            GLib.source_remove(tid)
        self._timers.clear()
        for cat in self.cat_instances:
            cat.cleanup()
        self.cat_instances.clear()
        if self.chat_bubble and self.chat_bubble.window:
            self.chat_bubble.hide()
            unregister_window(self.chat_bubble.window)
        if self._context_menu:
            unregister_window(self._context_menu)
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
        cd = color_def(config["color_id"])
        if not cd:
            return
        inst = CatInstance(config, cd)
        start_x = random.randint(int(self.display_w), int(self.screen_w - self.display_w * 2))
        start_x = max(0, min(start_x, self.screen_w - self.display_w))
        inst.setup(self, self.meta, self.cat_dir,
                   self.display_w, self.display_h,
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
        })

    def _render_tick(self):
        import time
        t0 = time.monotonic()
        for cat in self.cat_instances:
            cat.render_tick()
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
        if self.chat_bubble and self.chat_bubble._active_cat is cat:
            self.chat_bubble.hide()
        cat.cleanup()
        self.cat_instances.pop(idx)
        self.cat_configs = [c for c in self.cat_configs if c["color_id"] != color_id]
        delete_memory(cat.config["id"])
        self._save_all()
        if self.settings_ctrl:
            self.settings_ctrl.selected_color_id = self.cat_configs[0]["color_id"] if self.cat_configs else None
            self.settings_ctrl.refresh()

    def rename_cat(self, color_id, name):
        for cfg in self.cat_configs:
            if cfg["color_id"] == color_id:
                cfg["name"] = name; break
        self._save_all()
        for inst in self.cat_instances:
            if inst.color_def.id == color_id:
                inst.config["name"] = name
                inst.update_system_prompt(L10n.lang)

    def apply_new_scale(self, s):
        self.cat_scale = s
        self._recompute_size()
        self._save_all()
        for cat in self.cat_instances:
            cat.apply_scale(self.display_w, self.display_h, self.meta, self.cat_dir)

    def set_language(self, lang):
        L10n.lang = lang
        self._save_all()
        for cat in self.cat_instances:
            cat.update_system_prompt(lang)
        # Update chat bubble placeholder
        if self.chat_bubble and self.chat_bubble._entry:
            self.chat_bubble._entry.set_placeholder_text(L10n.s("talk"))
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
        ctrl._get_anim_frames = self._get_anim_frames
        ctrl.on_add = self.add_cat
        ctrl.on_remove = self.remove_cat
        ctrl.on_rename = self.rename_cat
        ctrl.on_scale_changed = self.apply_new_scale
        ctrl.on_model_changed = self.set_model
        ctrl.on_lang_changed = self.set_language
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


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    gtk_args = [a for a in sys.argv if a != "--debug"]
    app = CatAIApp()
    app.run(gtk_args)

if __name__ == "__main__":
    main()
