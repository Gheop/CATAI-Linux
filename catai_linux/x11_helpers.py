"""Low-level X11 / GDK helpers: window positioning, always-on-top, notification
type, XShape input region passthrough.

These are kept as a separate module so the rest of the codebase doesn't have to
deal with ctypes and libX11 directly. The entire module degrades gracefully:
if Xlib isn't available (e.g. pure Wayland session), the higher-level helpers
fall back to xdotool / wmctrl subprocesses.
"""
from __future__ import annotations

import ctypes
import logging
import subprocess
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GdkX11", "4.0")
from gi.repository import Gdk, GdkX11  # noqa: E402

log = logging.getLogger("catai")

# ── XID cache ─────────────────────────────────────────────────────────────────

_xid_cache: dict[int, int] = {}


def _get_xid(window):
    """Get the X11 window ID for a GTK window (None on Wayland)."""
    wid = id(window)
    if wid in _xid_cache:
        return _xid_cache[wid]
    surface = window.get_surface()
    if surface and isinstance(surface, GdkX11.X11Surface):
        xid = surface.get_xid()
        _xid_cache[wid] = xid
        return xid
    return None


# ── Xlib direct bindings via ctypes ───────────────────────────────────────────

_xlib = None
_xdpy = None


def _init_xlib():
    """Initialize Xlib for direct window moves. Returns True on success."""
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


def move_window(window, x: float, y: float) -> None:
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


def flush_x11() -> None:
    """Flush all pending X11 operations (call once per frame)."""
    global _xlib_dirty
    if _xlib_dirty and _xlib and _xdpy:
        _xlib.XFlush(ctypes.c_void_p(_xdpy))
        _xlib_dirty = False


def _run_x11(cmd: list[str]) -> None:
    """Run an X11 tool non-blocking in a background thread."""
    def _bg():
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        except Exception:
            pass
    threading.Thread(target=_bg, daemon=True).start()


def _x11_set_property_atom(xid: int, prop_name: str, value_name: str) -> bool:
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


def _x11_set_above_skip_taskbar(xid: int) -> bool:
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


# ── Window-hint management ────────────────────────────────────────────────────

_above_pending: list = []
_applied: set = set()
_notification_windows: list = []


def _apply_xid_hints(window, above: bool = False, notification: bool = False) -> None:
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


def set_always_on_top(window) -> None:
    """Mark window for always-on-top + skip-taskbar."""
    _above_pending.append(window)
    window.connect("realize", lambda w: _apply_xid_hints(w, above=True))


def set_notification_type(window) -> None:
    """Mark window as NOTIFICATION type. Call realize() before first set_visible()."""
    _notification_windows.append(window)
    window.connect("realize", lambda w: _apply_xid_hints(w, notification=True))
    # Force realize now so type is set BEFORE first show
    if not window.get_realized():
        window.realize()


def unregister_window(window) -> None:
    """Remove window from all global tracking lists and caches."""
    wid = id(window)
    for lst in [_above_pending, _notification_windows]:
        while window in lst:
            lst.remove(window)
    for prefix in ["above", "notif"]:
        _applied.discard((prefix, wid))
    _xid_cache.pop(wid, None)


def apply_above_all() -> bool:
    """Fallback: apply X11 hints to any windows missed by realize callback.
    Returns True so the caller can use it as a GLib timeout_add handler."""
    for w in list(_above_pending):
        if ("above", id(w)) not in _applied:
            _apply_xid_hints(w, above=True)
    for w in list(_notification_windows):
        if ("notif", id(w)) not in _applied:
            _apply_xid_hints(w, notification=True)
    return True


# ── XShape input passthrough ──────────────────────────────────────────────────

_xext = None


def _init_xext() -> bool:
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


def update_input_shape(window_xid: int, rects: list) -> None:
    """Set the input shape of a window to only the given rectangles.
    rects: list of (x, y, w, h) tuples.
    If rects is empty, set a 1×1 rect at -1,-1 (effectively no input)."""
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
