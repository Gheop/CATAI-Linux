"""Low-level X11 / GDK helpers: window positioning, always-on-top, notification
type, XShape input region passthrough, plus direct Xlib queries for the
active window's fullscreen state and a window's screen-Y offset.

These are kept as a separate module so the rest of the codebase doesn't have
to deal with ctypes and libX11 directly. Everything goes through libX11 +
libXext loaded via ``ctypes.cdll.LoadLibrary`` — no subprocess forks during
normal operation. If libX11 isn't loadable (extremely rare on systems that
can run GTK4), every helper degrades to a graceful no-op.
"""
from __future__ import annotations

import ctypes
import logging

import gi
gi.require_version("Gdk", "4.0")
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
    log.debug("Xlib unavailable — window helpers will be no-ops")
    return False


_xlib_dirty = False


def move_window(window, x: float, y: float) -> None:
    """Move a GTK4 window via Xlib XMoveWindow. No-op when libX11 isn't
    loadable (e.g. pure Wayland session without Xwayland)."""
    global _xlib_dirty
    xid = _get_xid(window)
    if not xid:
        return
    if _init_xlib() and _xdpy:
        _xlib.XMoveWindow(ctypes.c_void_p(_xdpy), xid, int(x), int(y))
        _xlib_dirty = True


def flush_x11() -> None:
    """Flush all pending X11 operations (call once per frame)."""
    global _xlib_dirty
    if _xlib_dirty and _xlib and _xdpy:
        _xlib.XFlush(ctypes.c_void_p(_xdpy))
        _xlib_dirty = False


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
    """Apply X11 hints immediately if XID is available. Pure Xlib —
    no subprocess fallbacks. Failures are logged at debug level and
    silently swallowed; the worst case is the canvas window not being
    perfectly always-on-top, which the user can manually fix."""
    xid = _get_xid(window)
    if not xid:
        return
    wid = id(window)
    if above and ("above", wid) not in _applied:
        _x11_set_above_skip_taskbar(xid)
        _applied.add(("above", wid))
    if notification and ("notif", wid) not in _applied:
        _x11_set_property_atom(xid, "_NET_WM_WINDOW_TYPE",
                               "_NET_WM_WINDOW_TYPE_NOTIFICATION")
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


# ── Global hotkey grab ───────────────────────────────────────────────────────

_grabbed_keycode: int | None = None


def grab_key_global(keycode: int) -> bool:
    """Grab a key globally via XGrabKey so it's intercepted no matter
    which window has focus. Used for the ² Quake console toggle.

    Returns True if the grab succeeded. Call ``ungrab_key_global`` on
    shutdown to release."""
    global _grabbed_keycode
    if not (_init_xlib() and _xdpy):
        return False
    try:
        dpy = ctypes.c_void_p(_xdpy)
        _xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        _xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        root = _xlib.XDefaultRootWindow(dpy)

        # XGrabKey(display, keycode, modifiers, grab_window,
        #          owner_events, pointer_mode, keyboard_mode)
        _xlib.XGrabKey.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
            ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        AnyModifier = (1 << 15)  # GrabAny
        GrabModeAsync = 1
        # Grab with no modifier + grab with any modifier (so Caps Lock
        # / Num Lock states don't block the hotkey)
        for mod in (0, AnyModifier):
            _xlib.XGrabKey(dpy, keycode, mod, root, True,
                           GrabModeAsync, GrabModeAsync)
        _xlib.XFlush(ctypes.c_void_p(_xdpy))
        _grabbed_keycode = keycode
        log.debug("XGrabKey OK for keycode %d", keycode)
        return True
    except Exception:
        log.debug("XGrabKey failed", exc_info=True)
        return False


def ungrab_key_global() -> None:
    """Release the global key grab."""
    global _grabbed_keycode
    if _grabbed_keycode is None or not (_init_xlib() and _xdpy):
        return
    try:
        dpy = ctypes.c_void_p(_xdpy)
        root = _xlib.XDefaultRootWindow(dpy)
        _xlib.XUngrabKey.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong,
        ]
        AnyModifier = (1 << 15)
        for mod in (0, AnyModifier):
            _xlib.XUngrabKey(dpy, _grabbed_keycode, mod, root)
        _xlib.XFlush(ctypes.c_void_p(_xdpy))
        _grabbed_keycode = None
    except Exception:
        log.debug("XUngrabKey failed", exc_info=True)


def poll_grabbed_key() -> bool:
    """Check if the grabbed key was pressed since last poll. Non-blocking.

    Uses XCheckTypedEvent to peek at KeyPress events on the root window.
    Returns True if the grabbed key was fired. Must be called from a
    GLib timer (~100 ms) so we don't miss events."""
    if _grabbed_keycode is None or not (_init_xlib() and _xdpy):
        return False
    try:
        dpy = ctypes.c_void_p(_xdpy)

        # XEvent is a union of 192 bytes on 64-bit (24 longs)
        class XEvent(ctypes.Structure):
            _fields_ = [("data", ctypes.c_ulong * 24)]

        _xlib.XCheckTypedEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(XEvent),
        ]
        _xlib.XCheckTypedEvent.restype = ctypes.c_int

        ev = XEvent()
        KeyPress = 2
        if _xlib.XCheckTypedEvent(dpy, KeyPress, ctypes.byref(ev)):
            # XKeyEvent: type(0), serial(1), send_event(2), display(3),
            #            window(4), root(5), subwindow(6), time(7),
            #            x(8), y(9), x_root(10), y_root(11),
            #            state(12), keycode(13), ...
            pressed_keycode = int(ev.data[13]) & 0xFFFF
            if pressed_keycode == _grabbed_keycode:
                return True
            # Not our key — put it back
            _xlib.XPutBackEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(XEvent)]
            _xlib.XPutBackEvent(dpy, ctypes.byref(ev))
        return False
    except Exception:
        return False


# ── Direct Xlib queries (replace xprop / xdotool subprocess polls) ──────────


def _ensure_xlib_query_signatures() -> None:
    """Set up the ctypes signatures for the read-only Xlib query
    functions used by ``get_active_window_fullscreen`` and
    ``get_window_y_offset``. Idempotent — only fills in argtypes that
    haven't been set yet."""
    # XInternAtom + XDefaultRootWindow signatures are already set in
    # _x11_set_above_skip_taskbar / _x11_set_property_atom on the same
    # _xlib instance, but we re-set them defensively in case those
    # paths haven't been triggered yet.
    _xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    _xlib.XInternAtom.restype = ctypes.c_ulong
    _xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    _xlib.XDefaultRootWindow.restype = ctypes.c_ulong
    _xlib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p,                              # display
        ctypes.c_ulong,                               # window
        ctypes.c_ulong,                               # property atom
        ctypes.c_long,                                # long_offset
        ctypes.c_long,                                # long_length
        ctypes.c_int,                                 # delete (Bool)
        ctypes.c_ulong,                               # req_type atom (AnyPropertyType=0)
        ctypes.POINTER(ctypes.c_ulong),               # actual_type_return
        ctypes.POINTER(ctypes.c_int),                 # actual_format_return
        ctypes.POINTER(ctypes.c_ulong),               # nitems_return
        ctypes.POINTER(ctypes.c_ulong),               # bytes_after_return
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),  # prop_return
    ]
    _xlib.XGetWindowProperty.restype = ctypes.c_int
    _xlib.XFree.argtypes = [ctypes.c_void_p]
    _xlib.XFree.restype = ctypes.c_int
    _xlib.XTranslateCoordinates.argtypes = [
        ctypes.c_void_p,                # display
        ctypes.c_ulong,                 # src_w
        ctypes.c_ulong,                 # dst_w
        ctypes.c_int,                   # src_x
        ctypes.c_int,                   # src_y
        ctypes.POINTER(ctypes.c_int),   # dest_x_return
        ctypes.POINTER(ctypes.c_int),   # dest_y_return
        ctypes.POINTER(ctypes.c_ulong), # child_return
    ]
    _xlib.XTranslateCoordinates.restype = ctypes.c_int


def _x11_get_active_window() -> int:
    """Return the XID of the currently focused window via
    ``_NET_ACTIVE_WINDOW`` on the root window. Returns 0 on failure."""
    if not (_init_xlib() and _xdpy):
        return 0
    try:
        _ensure_xlib_query_signatures()
        dpy = ctypes.c_void_p(_xdpy)
        root = _xlib.XDefaultRootWindow(dpy)
        active_atom = _xlib.XInternAtom(dpy, b"_NET_ACTIVE_WINDOW", 0)

        actual_type = ctypes.c_ulong(0)
        actual_format = ctypes.c_int(0)
        nitems = ctypes.c_ulong(0)
        bytes_after = ctypes.c_ulong(0)
        prop_ptr = ctypes.POINTER(ctypes.c_ulong)()

        # Read up to 1 long (32-bit on the wire, c_ulong here is fine
        # since Xlib zero-extends to native long).
        rc = _xlib.XGetWindowProperty(
            dpy, root, active_atom, 0, 1, 0, 0,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(nitems),
            ctypes.byref(bytes_after),
            ctypes.byref(prop_ptr),
        )
        if rc != 0 or not prop_ptr or nitems.value == 0:
            return 0
        try:
            return int(prop_ptr[0])
        finally:
            _xlib.XFree(prop_ptr)
    except Exception:
        log.debug("Xlib _x11_get_active_window failed", exc_info=True)
        return 0


def _x11_window_has_state(xid: int, state_atom_name: str) -> bool:
    """Return True if ``xid`` has ``state_atom_name`` in its
    ``_NET_WM_STATE`` property. Reads up to 32 atoms (more than any
    real window would carry)."""
    if not (_init_xlib() and _xdpy and xid):
        return False
    try:
        _ensure_xlib_query_signatures()
        dpy = ctypes.c_void_p(_xdpy)
        wm_state_atom = _xlib.XInternAtom(dpy, b"_NET_WM_STATE", 0)
        target_atom = _xlib.XInternAtom(dpy, state_atom_name.encode(), 0)

        actual_type = ctypes.c_ulong(0)
        actual_format = ctypes.c_int(0)
        nitems = ctypes.c_ulong(0)
        bytes_after = ctypes.c_ulong(0)
        prop_ptr = ctypes.POINTER(ctypes.c_ulong)()

        rc = _xlib.XGetWindowProperty(
            dpy, xid, wm_state_atom, 0, 32, 0, 0,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(nitems),
            ctypes.byref(bytes_after),
            ctypes.byref(prop_ptr),
        )
        if rc != 0 or not prop_ptr:
            return False
        try:
            n = int(nitems.value)
            for i in range(n):
                if int(prop_ptr[i]) == int(target_atom):
                    return True
            return False
        finally:
            _xlib.XFree(prop_ptr)
    except Exception:
        log.debug("Xlib _x11_window_has_state failed", exc_info=True)
        return False


def get_active_window_fullscreen() -> bool:
    """Return True if the currently focused window has the
    ``_NET_WM_STATE_FULLSCREEN`` atom set. Replaces the previous
    ``xprop -root`` + ``xprop -id`` subprocess pair: same result,
    ~50 µs instead of ~30 ms, no fork."""
    xid = _x11_get_active_window()
    if not xid:
        return False
    return _x11_window_has_state(xid, "_NET_WM_STATE_FULLSCREEN")


def get_window_y_offset(xid: int) -> int:
    """Return the absolute Y position (screen-pixel) of an X11 window
    via ``XTranslateCoordinates`` (translate (0, 0) of ``xid`` into the
    root window's frame). Used to detect the GNOME top bar offset for
    chat-bubble positioning. Replaces ``xdotool getwindowgeometry``.
    Returns 0 on any failure."""
    if not (_init_xlib() and _xdpy and xid):
        return 0
    try:
        _ensure_xlib_query_signatures()
        dpy = ctypes.c_void_p(_xdpy)
        root = _xlib.XDefaultRootWindow(dpy)
        dest_x = ctypes.c_int(0)
        dest_y = ctypes.c_int(0)
        child = ctypes.c_ulong(0)
        ok = _xlib.XTranslateCoordinates(
            dpy, xid, root, 0, 0,
            ctypes.byref(dest_x),
            ctypes.byref(dest_y),
            ctypes.byref(child),
        )
        return int(dest_y.value) if ok else 0
    except Exception:
        log.debug("Xlib get_window_y_offset failed", exc_info=True)
        return 0


def get_mouse_position() -> tuple[int, int] | None:
    """Return the absolute (x, y) of the mouse cursor on the root
    window via ``XQueryPointer``. Used by the wake-word ``viens``
    command to make a cat walk to the user's cursor.

    Returns ``None`` if libX11 isn't loadable or the query failed."""
    if not (_init_xlib() and _xdpy):
        return None
    try:
        # XQueryPointer(display, window, root_return, child_return,
        #               root_x_return, root_y_return,
        #               win_x_return, win_y_return, mask_return)
        _xlib.XQueryPointer.argtypes = [
            ctypes.c_void_p,                # display
            ctypes.c_ulong,                 # window
            ctypes.POINTER(ctypes.c_ulong), # root_return
            ctypes.POINTER(ctypes.c_ulong), # child_return
            ctypes.POINTER(ctypes.c_int),   # root_x_return
            ctypes.POINTER(ctypes.c_int),   # root_y_return
            ctypes.POINTER(ctypes.c_int),   # win_x_return
            ctypes.POINTER(ctypes.c_int),   # win_y_return
            ctypes.POINTER(ctypes.c_uint),  # mask_return
        ]
        _xlib.XQueryPointer.restype = ctypes.c_int
        _xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        _xlib.XDefaultRootWindow.restype = ctypes.c_ulong

        dpy = ctypes.c_void_p(_xdpy)
        root = _xlib.XDefaultRootWindow(dpy)
        root_return = ctypes.c_ulong(0)
        child_return = ctypes.c_ulong(0)
        root_x = ctypes.c_int(0)
        root_y = ctypes.c_int(0)
        win_x = ctypes.c_int(0)
        win_y = ctypes.c_int(0)
        mask = ctypes.c_uint(0)
        ok = _xlib.XQueryPointer(
            dpy, root,
            ctypes.byref(root_return),
            ctypes.byref(child_return),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(win_x),
            ctypes.byref(win_y),
            ctypes.byref(mask),
        )
        if not ok:
            return None
        return (int(root_x.value), int(root_y.value))
    except Exception:
        log.debug("Xlib get_mouse_position failed", exc_info=True)
        return None


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
