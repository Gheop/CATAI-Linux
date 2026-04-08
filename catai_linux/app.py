#!/usr/bin/env python3
"""CATAI-Linux — Virtual desktop pet cats for Linux (GNOME/Wayland)
Port of https://github.com/wil-pe/CATAI (macOS) to GTK4.
Uses XWayland for window positioning since GNOME doesn't support wlr-layer-shell.
"""

# Force X11 backend for free window positioning on Wayland compositors
import os
os.environ.setdefault("GDK_BACKEND", "x11")

import ctypes
import enum
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

RENDER_MS = 100        # 10 FPS
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
        "model":    {"fr": "MODÈLE OLLAMA", "en": "OLLAMA MODEL", "es": "MODELO OLLAMA"},
        "quit":     {"fr": "Quitter", "en": "Quit", "es": "Salir"},
        "settings": {"fr": "Réglages...", "en": "Settings...", "es": "Ajustes..."},
        "talk":     {"fr": "Parle au chat...", "en": "Talk to the cat...", "es": "Habla al gato..."},
        "hi":       {"fr": "Miaou! ~(=^..^=)~", "en": "Meow! ~(=^..^=)~", "es": "¡Miau! ~(=^..^=)~"},
        "loading":  {"fr": "Chargement...", "en": "Loading...", "es": "Cargando..."},
        "no_ollama": {"fr": "(Ollama indisponible)", "en": "(Ollama unavailable)", "es": "(Ollama no disponible)"},
        "err":      {"fr": "Mrrp... pas de connexion", "en": "Mrrp... no connection", "es": "Mrrp... sin conexión"},
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
        # Use 'catai' command if available (pip install), else python3 + script
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

def pil_to_texture(img, target_w, target_h):
    """Convert PIL Image to Gdk.MemoryTexture, scaled nearest-neighbor."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    scaled = img.resize((target_w, target_h), Image.NEAREST)
    data = scaled.tobytes()
    gbytes = GLib.Bytes.new(data)
    return Gdk.MemoryTexture.new(target_w, target_h, Gdk.MemoryFormat.R8G8B8A8,
                                  gbytes, target_w * 4)

def load_and_tint(path, color_def):
    """Load a sprite PNG and apply color tinting. Returns PIL Image."""
    try:
        src = Image.open(path).convert("RGBA")
    except Exception:
        log.warning("Missing sprite: %s", path)
        src = Image.new("RGBA", (68, 68), (255, 0, 255, 128))
    return tint_sprite(src, color_def)

# ── X11 Window Helpers ─────────────────────────────────────────────────────────

# Cache of window XIDs (GTK window id -> X11 window id)
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
    """Initialize Xlib for direct window moves (much faster than xdotool subprocess)."""
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
            # Extract raw pointer: try hash() first (stable), fallback to repr parsing
            try:
                _xdpy = hash(xdpy_obj)
                # Validate: hash of ctypes-backed object IS the pointer on CPython
                lib.XFlush(ctypes.c_void_p(_xdpy))
                _xlib = lib
                log.debug("Xlib initialized via hash(), pointer=%#x", _xdpy)
                return True
            except (TypeError, OSError):
                pass
            # Fallback: parse repr string
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

def move_window(window, x, y):
    """Move a GTK4 window. Uses Xlib directly, falls back to xdotool."""
    xid = _get_xid(window)
    if not xid:
        return
    if _init_xlib() and _xdpy:
        _xlib.XMoveWindow(ctypes.c_void_p(_xdpy), xid, int(x), int(y))
        _xlib.XFlush(ctypes.c_void_p(_xdpy))
    else:
        subprocess.run(["xdotool", "windowmove", str(xid), str(int(x)), str(int(y))],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)


def _run_x11(cmd):
    """Run an X11 tool (wmctrl/xprop) with proper cleanup (no zombies)."""
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
    except Exception:
        pass


_above_pending = []
_applied = set()
_no_focus_windows = []
_notification_windows = []


def _apply_xid_hints(window, above=False, no_focus=False, notification=False):
    """Apply X11 hints immediately if XID is available."""
    xid = _get_xid(window)
    if not xid:
        return
    wid = id(window)
    if above and ("above", wid) not in _applied:
        _run_x11(["wmctrl", "-i", "-r", str(xid), "-b", "add,above,skip_taskbar"])
        _applied.add(("above", wid))
    if notification and ("notif", wid) not in _applied:
        _run_x11(["xprop", "-id", str(xid), "-f", "_NET_WM_WINDOW_TYPE", "32a",
                  "-set", "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_NOTIFICATION"])
        _applied.add(("notif", wid))
    if no_focus and ("nofocus", wid) not in _applied:
        _run_x11(["xprop", "-id", str(xid), "-f", "WM_HINTS", "32i",
                  "-set", "WM_HINTS", "2, 0, 0, 0, 0, 0, 0, 0, 0"])
        _applied.add(("nofocus", wid))


def set_no_focus(window):
    """Mark window as not accepting focus + NOTIFICATION type."""
    _no_focus_windows.append(window)
    _notification_windows.append(window)
    _above_pending.append(window)
    window.connect("realize", lambda w: GLib.idle_add(
        lambda: _apply_xid_hints(w, above=True, no_focus=True, notification=True) or False))

def set_notification_type(window):
    """Mark window as NOTIFICATION type only."""
    _notification_windows.append(window)
    window.connect("realize", lambda w: GLib.idle_add(
        lambda: _apply_xid_hints(w, notification=True) or False))

def set_always_on_top(window):
    """Mark window for always-on-top + skip-taskbar."""
    _above_pending.append(window)
    window.connect("realize", lambda w: GLib.idle_add(
        lambda: _apply_xid_hints(w, above=True) or False))

def unregister_window(window):
    """Remove window from all global tracking lists and caches."""
    wid = id(window)
    for lst in [_above_pending, _no_focus_windows, _notification_windows]:
        while window in lst:
            lst.remove(window)
    for prefix in ["above", "notif", "nofocus"]:
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
    for w in list(_no_focus_windows):
        if ("nofocus", id(w)) not in _applied:
            _apply_xid_hints(w, no_focus=True)
    return True


# ── Chat Backends ──────────────────────────────────────────────────────────────

CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_CREDS = os.path.expanduser("~/.claude/.credentials.json")


def _get_claude_api_key():
    """Get API key from env var or Claude Code's OAuth token."""
    return os.environ.get("ANTHROPIC_API_KEY") or _read_claude_oauth()

def _read_claude_oauth():
    try:
        # Check file permissions aren't too open
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

def fetch_ollama_models():
    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return [m["name"] for m in resp.json().get("models", [])]
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
                    log.warning("Chat error: %s", e)
                    GLib.idle_add(on_error, L10n.s("err"))
            finally:
                with self._lock:
                    if full:
                        self.messages.append({"role": "assistant", "content": full})
                self.is_streaming = False
                GLib.idle_add(on_done)

        threading.Thread(target=_run, daemon=True).start()

    def _stream_chunks(self):
        """Yield text chunks. Implemented by subclasses."""
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
        with self.client.messages.stream(
            model=self.model, max_tokens=256,
            system=system_prompt, messages=api_messages,
        ) as stream:
            yield from stream.text_stream


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
    """Check Ollama availability once (cached)."""
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
        return ClaudeChat(model)
    if not model.startswith("claude-") and _ollama_available():
        return OllamaChat(model)
    # Ollama not running or Claude model requested — use Claude if available
    if claude_available():
        return ClaudeChat(CLAUDE_MODEL)
    return OllamaChat(model)

# ── CSS Theme ──────────────────────────────────────────────────────────────────

CSS = b"""
.cat-window {
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
        self._cat_pos = (0, 0, 0, 0)  # cat_x, cat_y, cat_w, cat_h for repositioning

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

        # Bubble body
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.set_margin_start(self.padding)
        body.set_margin_end(self.padding)
        body.set_margin_top(6)
        body.set_margin_bottom(self.padding)
        body.add_css_class("bubble-body")

        # Close button
        close_btn = Gtk.Button(label="\u00d7")
        close_css = Gtk.CssProvider()
        close_css.load_from_data(b"button { background: transparent; color: #4d3319; min-width: 20px; min-height: 16px; font-size: 14px; padding: 0; border: none; }")
        close_btn.get_style_context().add_provider(close_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        close_btn.set_halign(Gtk.Align.END)
        close_btn.connect("clicked", lambda b: self._do_close())
        body.append(close_btn)

        # Response in a scrollable area
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

        # Tail pointing down, directly attached to body
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

    def show(self, cat_x, cat_y, cat_w, cat_h):
        was_visible = self.window.get_visible()
        self._cat_pos = (cat_x, cat_y, cat_w, cat_h)
        bx = int(cat_x + cat_w / 2 - self.bubble_w / 2)
        self.window.set_visible(True)
        # Reposition twice: immediately + after GTK relayouts (for reopened bubbles with content)
        GLib.idle_add(self._move_above, bx, cat_y, cat_h)
        GLib.timeout_add(100, self._move_above, bx, cat_y, cat_h)
        if not was_visible:
            self._entry.grab_focus()

    def reposition(self, cat_x, cat_y, cat_w, cat_h):
        """Move bubble to follow cat, without stealing focus."""
        if not self.is_visible:
            return
        self._cat_pos = (cat_x, cat_y, cat_w, cat_h)
        bx = int(cat_x + cat_w / 2 - self.bubble_w / 2)
        GLib.idle_add(self._move_above, bx, cat_y, cat_h)

    def _do_close(self):
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
            # Debounced auto-scroll + reposition (max once per 200ms)
            if self._scroll and not getattr(self, '_scroll_pending', False):
                self._scroll_pending = True
                def _do_scroll():
                    adj = self._scroll.get_vadjustment()
                    adj.set_value(adj.get_upper())
                    self._scroll_pending = False
                    # Reposition bubble above cat as it grows
                    cx, cy, cw, ch = self._cat_pos
                    if cx or cy:
                        bx = int(cx + cw / 2 - self.bubble_w / 2)
                        self._move_above(bx, cy, ch)
                    return False
                GLib.timeout_add(200, _do_scroll)


# ── Meow Bubble ───────────────────────────────────────────────────────────────

class MeowBubble:
    def __init__(self, app):
        self.app = app
        self.window = None
        self._label = None
        self._timer_id = None

    def setup(self):
        self.window = Gtk.Window(application=self.app)
        self.window.set_decorated(False)
        self.window.add_css_class("meow-window")
        self.window.set_resizable(False)
        self.window.set_can_focus(False)
        self.window.set_focusable(False)

        overlay = Gtk.Overlay()
        bg = Gtk.DrawingArea()
        bg.set_content_width(120)
        bg.set_content_height(32)
        bg.set_draw_func(lambda a, ctx, w, h: draw_pixel_border(ctx, w, h, 2))
        overlay.set_child(bg)

        self._label = Gtk.Label(label="")
        self._label.add_css_class("pixel-label-small")
        overlay.add_overlay(self._label)
        self.window.set_child(overlay)
        set_no_focus(self.window)  # includes above + notification + no-focus

    def show(self, text, cat_x, cat_y, cat_w, cat_h):
        if self._timer_id:
            GLib.source_remove(self._timer_id)
        self._label.set_label(text)
        text_w = max(80, len(text) * 9 + 24)
        bx = max(0, int(cat_x + cat_w / 2 - text_w / 2))
        by = max(0, int(cat_y - 40))
        self.window.set_default_size(text_w, 32)
        # Save focused window XID, show meow, then restore focus in background
        focused = getattr(self, '_last_focused', None)
        try:
            r = subprocess.run(["xdotool", "getactivewindow"],
                               capture_output=True, text=True, timeout=1)
            focused = r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            pass
        self.window.set_visible(True)
        GLib.idle_add(lambda: move_window(self.window, bx, by) or False)
        if focused:
            # Restore focus after a short delay (let WM process the meow window first)
            GLib.timeout_add(50, lambda: _run_x11(["xdotool", "windowactivate", "--sync", focused]) or False)
        self._timer_id = GLib.timeout_add(random.randint(2000, 3000), self._auto_hide)

    def _auto_hide(self):
        self.window.set_visible(False)
        self._timer_id = None
        return False

    def hide(self):
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        if self.window:
            self.window.set_visible(False)


# ── Cat Instance ───────────────────────────────────────────────────────────────

class CatInstance:
    def __init__(self, config, color_def_obj):
        self.config = config
        self.color_def = color_def_obj
        self.window = None
        self.picture = None
        self.rotations = {}      # dir_name -> PIL Image (unscaled)
        self.animations = {}     # anim_name -> {dir_name -> [PIL Image]}
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
        self.chat_bubble = None
        self.meow_bubble = None
        self.chat_backend = None
        self.screen_w = 0
        self.screen_h = 0
        self._app = None
        self._siblings = []

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

        self.window = Gtk.Window(application=app)
        self.window.set_decorated(False)
        self.window.add_css_class("cat-window")
        self.window.set_default_size(dw, dh)
        self.window.set_resizable(False)
        set_always_on_top(self.window)
        set_notification_type(self.window)

        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(False)
        self.picture.set_size_request(dw, dh)
        self.picture.set_paintable(self._fallback_tex())
        self.window.set_child(self.picture)

        # Click
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released", self._on_click)
        self.window.add_controller(click)

        # Right-click
        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("released", self._on_right_click)
        self.window.add_controller(rclick)

        # Drag
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.window.add_controller(drag)

        # Chat
        self.chat_bubble = ChatBubbleController(app)
        self.chat_bubble.setup()
        self.chat_bubble.on_send = self.send_chat

        self.meow_bubble = MeowBubble(app)
        self.meow_bubble.setup()

        self.setup_chat(model, lang)

        self.window.set_visible(True)
        GLib.idle_add(lambda: move_window(self.window, int(self.x), int(self.y)) or False)

    def setup_chat(self, model, lang):
        prompt = self.color_def.prompt(self.config["name"], lang)
        self.chat_backend = create_chat(model)
        self.chat_backend.messages = [{"role": "system", "content": prompt}]
        mem = load_memory(self.config["id"])
        if len(mem) > 1:
            self.chat_backend.messages.extend(mem[1:])

    def load_assets(self, meta, cat_dir):
        """Load all sprites, tint them, and pre-convert to GPU textures."""
        dw, dh = self.display_w, self.display_h
        self.rotations = {}
        for dir_name, rel_path in meta["frames"]["rotations"].items():
            pil = load_and_tint(os.path.join(cat_dir, rel_path), self.color_def)
            self.rotations[dir_name] = pil_to_texture(pil, dw, dh)
        self.animations = {}
        for anim_name, dirs in meta["frames"]["animations"].items():
            self.animations[anim_name] = {}
            for dir_name, frame_paths in dirs.items():
                self.animations[anim_name][dir_name] = [
                    pil_to_texture(load_and_tint(os.path.join(cat_dir, p), self.color_def), dw, dh)
                    for p in frame_paths
                ]

    def _fallback_tex(self):
        return self.rotations.get(self.direction) or self.rotations.get("south") or next(iter(self.rotations.values()))

    def _current_texture(self):
        if self.state in (CatState.IDLE, CatState.SLEEPING):
            return self._fallback_tex()
        key = ANIM_KEYS.get(self.state)
        if key:
            frames = self.animations.get(key, {}).get(self.direction, [])
            if frames:
                return frames[self.frame_index % len(frames)]
        return self._fallback_tex()

    def render_tick(self):
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
                # Move proportionally on both axes
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
            move_window(self.window, int(self.x), int(self.y))
            if self.chat_bubble and self.chat_bubble.is_visible:
                self.chat_bubble.reposition(self.x, self.y, self.display_w, self.display_h)
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

        tex = self._current_texture()
        if tex is not getattr(self, '_last_tex', None):
            self._last_tex = tex
            self.picture.set_paintable(tex)

    def behavior_tick(self):
        if self.chat_bubble and self.chat_bubble.is_visible:
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

    def _on_click(self, gesture, n_press, x, y):
        if self.mouse_moved:
            return
        if n_press == 2:
            # Double-click → open settings
            if self._app:
                self._app._open_settings()
            return
        for cat in self._siblings:
            if cat is not self:
                cat.chat_bubble.hide()
        self._toggle_chat()

    def _on_right_click(self, gesture, n_press, x, y):
        if not self._app:
            return
        # Reuse a single shared menu window
        menu = getattr(self._app, '_context_menu', None)
        if menu and menu.get_visible():
            menu.set_visible(False)
            return
        if not menu:
            menu = Gtk.Window(application=self._app)
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
            btn_settings.connect("clicked", lambda b: (menu.set_visible(False), self._app._open_settings()))
            box.append(btn_settings)

            btn_quit = Gtk.Button(label=L10n.s("quit"))
            btn_quit.add_css_class("pixel-label-small")
            btn_quit.connect("clicked", lambda b: self._app.quit())
            box.append(btn_quit)

            menu.set_child(box)
            self._app._context_menu = menu

        menu.set_visible(True)
        GLib.idle_add(lambda: move_window(menu, int(self.x + self.display_w), int(self.y)) or False)
        # Auto-close after 5s
        def _auto_close_menu():
            self._app._menu_timer = None
            menu.set_visible(False)
            return False
        if getattr(self._app, '_menu_timer', None):
            GLib.source_remove(self._app._menu_timer)
        self._app._menu_timer = GLib.timeout_add(5000, _auto_close_menu)

    def _on_drag_begin(self, gesture, start_x, start_y):
        self.dragging = True
        self.mouse_moved = False
        self.drag_win_x = self.x
        self.drag_win_y = self.y

    def _on_drag_update(self, gesture, offset_x, offset_y):
        if not self.dragging:
            return
        if abs(offset_x) > 3 or abs(offset_y) > 3:
            self.mouse_moved = True
        self.x = max(0, min(self.drag_win_x + offset_x, self.screen_w - self.display_w))
        self.y = max(0, min(self.drag_win_y + offset_y, self.screen_h - self.display_h))
        move_window(self.window, int(self.x), int(self.y))

    def _on_drag_end(self, gesture, offset_x, offset_y):
        self.dragging = False

    def _toggle_chat(self):
        if self.chat_bubble.is_visible:
            self.chat_bubble.hide()
        else:
            self.chat_bubble.show(self.x, self.y, self.display_w, self.display_h)

    def send_chat(self, text):
        if self.chat_backend.is_streaming:
            return
        self.chat_bubble.set_response("...")
        self.chat_bubble.show(self.x, self.y, self.display_w, self.display_h)
        self.state = CatState.EATING
        self.frame_index = 0

        first_token = True
        def on_token(token):
            nonlocal first_token
            if first_token:
                self.chat_bubble.set_response(token)
                first_token = False
            else:
                self.chat_bubble.append_response(token)
            return False

        def on_done():
            self.state = CatState.IDLE
            self.frame_index = 0
            self.idle_ticks = 0
            save_memory(self.config["id"], self.chat_backend.messages)
            return False

        def on_error(msg):
            self.chat_bubble.set_response(msg)
            self.state = CatState.IDLE
            self.frame_index = 0
            return False

        self.chat_backend.send(text, on_token, on_done, on_error)

    def update_system_prompt(self, lang):
        p = self.color_def.prompt(self.config["name"], lang)
        if self.chat_backend.messages:
            self.chat_backend.messages[0] = {"role": "system", "content": p}
        if self.chat_bubble and self.chat_bubble._entry:
            self.chat_bubble._entry.set_placeholder_text(L10n.s("talk"))

    def _show_random_meow(self):
        if self.chat_bubble and self.chat_bubble.is_visible:
            return
        self.meow_bubble.show(L10n.random_meow(), self.x, self.y, self.display_w, self.display_h)

    def apply_scale(self, new_w, new_h, meta, cat_dir):
        self.display_w = new_w
        self.display_h = new_h
        self.load_assets(meta, cat_dir)
        self.window.set_default_size(new_w, new_h)
        self._last_tex = None
        self.picture.set_paintable(self._current_texture())
        move_window(self.window, int(self.x), int(self.y))

    def cleanup(self):
        if self.chat_backend:
            self.chat_backend.cancel()
        self.chat_bubble.hide()
        self.meow_bubble.hide()
        self.window.set_visible(False)
        # Remove from all global tracking to prevent leaks
        for w in [self.window, self.chat_bubble.window, self.meow_bubble.window]:
            unregister_window(w)
        self._app = None
        self.chat_backend = None


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
            self.window = Gtk.Window(application=self.app)
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
        self._stop_timers()  # clean up old animation timers and pictures
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

        # Cat sprite selector — animated previews!
        bubbles_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bubbles_box.set_halign(Gtk.Align.CENTER)
        self._anim_pictures = []  # track for animation timer
        for c in CAT_COLORS:
            is_active = c.id in active_ids
            is_sel = self.selected_color_id == c.id and is_active
            cat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

            # Sprite preview button
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
                # Load walking frames for animation
                if self._get_anim_frames:
                    frames = self._get_anim_frames(c.id, sprite_size)
                    if frames:
                        self._anim_pictures.append((pic, frames, [0]))
            else:
                btn.connect("clicked", self._on_bubble_add, c.id)
            cat_box.append(btn)

            # × remove button
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

        # Start animation timer for active cat previews
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
            # Debounce: wait 800ms after last change before applying (reloads all sprites)
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

        def _load_models():
            all_models = []
            # Add Claude models if Claude Code is available
            if claude_available():
                all_models.append(f"{CLAUDE_MODEL} (Claude)")
            # Add Ollama models (only if Ollama is reachable)
            if _ollama_available():
                all_models.extend(fetch_ollama_models())
            def _update():
                model_strings.splice(0, model_strings.get_n_items(),
                                     all_models if all_models else [L10n.s("no_ollama")])
                if all_models:
                    # Find current model in list
                    current = self.current_model
                    for i, m in enumerate(all_models):
                        if m.startswith(current):
                            model_combo.set_selected(i)
                            break
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
        """Advance animation frame for each active cat preview."""
        for pic, frames, idx in self._anim_pictures:
            idx[0] = (idx[0] + 1) % len(frames)
            pic.set_paintable(frames[idx[0]])
        return True  # keep timer running

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
        idx = dropdown.get_selected()
        if idx < string_list.get_n_items():
            name = string_list.get_string(idx)
            if name and not name.startswith("(") and name != L10n.s("loading"):
                # Extract model id from display name like "claude-haiku-4-5 (Claude)"
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
        self._timers = []

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
            # Use bounding box of all monitors for multi-monitor support
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
        for i, cat_cfg in enumerate(self.cat_configs):
            self._create_instance(cat_cfg, i)
        for cat in self.cat_instances:
            cat._siblings = self.cat_instances

        # Actions
        for name, cb in [("quit", lambda *a: self.quit()), ("settings", lambda *a: self._open_settings())]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)

        self._timers = [
            GLib.timeout_add(RENDER_MS, self._render_tick),
            GLib.timeout_add(BEHAVIOR_MS, self._behavior_tick),
            GLib.timeout_add(10000, _apply_above_all),
        ]

    def do_shutdown(self):
        """Clean shutdown: stop timers, cleanup cats, close windows."""
        for tid in self._timers:
            GLib.source_remove(tid)
        self._timers.clear()
        for cat in self.cat_instances:
            cat.cleanup()
        self.cat_instances.clear()
        if self.settings_ctrl and self.settings_ctrl.window:
            self.settings_ctrl._stop_timers()
            self.settings_ctrl.window.set_visible(False)
        Gtk.Application.do_shutdown(self)

    def _check_deps(self):
        """Check optional external tools (not required, just enhance UX)."""
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
        dt = (time.monotonic() - t0) * 1000
        if dt > 50:  # log if render takes more than 50ms
            log.warning("Slow render tick: %.0fms (%d cats)", dt, len(self.cat_instances))
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
        for cat in self.cat_instances:
            cat._siblings = self.cat_instances
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
        cat.cleanup()
        self.cat_instances.pop(idx)
        self.cat_configs = [c for c in self.cat_configs if c["color_id"] != color_id]
        delete_memory(cat.config["id"])
        self._save_all()
        for c in self.cat_instances:
            c._siblings = self.cat_instances
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
        if self.settings_ctrl:
            self.settings_ctrl.refresh()

    def set_model(self, model):
        self.selected_model = model
        self._save_all()
        for cat in self.cat_instances:
            if not cat.chat_backend:
                continue
            # Don't switch backend while streaming
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
