"""Lightweight user-activity monitor.

Polls cheap system signals (idle time, CPU load, hour of day) so the
behavior tick can make the cats feel aware of their environment:

  - **User idle for >10 min** → all cats gather in SLEEPING_BALL. On
    user return (idle drops back to <5 s) the cats wake up with a
    brief SURPRISED / WAKING_UP transition. This is the single most
    noticeable "alive" signal in the app.

  - **Time of day** → a small bias on the mood system's `wants_rest`
    predicate so late-night and very early morning lean toward sleep.
    Subtle but accumulates: by 3 AM most cats will be napping on
    their own.

Idle detection layered fallback (cheapest first):

  1. **Mutter D-Bus IdleMonitor** — ``org.gnome.Mutter.IdleMonitor``
     on the session bus. Method ``GetIdletime`` returns ms exactly
     like ``xprintidle``. ~0.1 ms per call, no subprocess fork.
     Available on every modern GNOME (Wayland or X11). Some KDE
     setups also expose it via the kglobalaccel-mutter shim.
  2. **xprintidle subprocess** — legacy fallback for X11 sessions
     where Mutter isn't running (e.g. minimal i3, IceWM). ~5-15 ms
     per fork.
  3. **Constant 0** — last-resort no-op so AFK detection silently
     stays off rather than crashing.

The monitor is stateless: callers (CatAIApp.behavior_tick) invoke
``update()`` once per tick, then read the properties. No background
threads, no subprocess pools — D-Bus calls go through the in-process
session bus connection that GIO already maintains.
"""
from __future__ import annotations

import datetime
import logging
import shutil
import subprocess
import time

log = logging.getLogger("catai")


class ActivityMonitor:
    """Read-only view of the current user activity / system state.

    Thread-safe only in the trivial sense that ``update()`` assigns to
    plain attributes (Python's GIL covers that). Callers should not run
    ``update()`` from multiple threads concurrently.
    """

    IDLE_THRESHOLD_MS = 10 * 60 * 1000   # 10 min of no input → AFK
    IDLE_WAKEUP_THRESHOLD_MS = 5 * 1000  # return from AFK when < 5 s idle
    POLL_INTERVAL_MS = 2000              # skip some updates to keep cost low
    NIGHT_HOURS = frozenset({22, 23, 0, 1, 2, 3, 4, 5})  # local wall clock

    def __init__(self):
        self.idle_ms: int = 0
        self.cpu_load: float = 0.0
        self.hour: int = 12
        self.is_afk: bool = False
        self._last_update: float = 0.0
        # D-Bus idle monitor proxy (lazy init in _read_idle_ms). Three
        # states: None = not yet attempted, False = attempted and
        # unavailable, anything else = a live Gio.DBusProxy.
        self._idle_proxy = None
        # Legacy subprocess fallback. Resolved once at construction so
        # we don't shutil.which() in the hot path.
        self._xprintidle = shutil.which("xprintidle")
        # Test override: when set to True/False, update() leaves is_afk
        # alone (pinned) so the e2e suite can force AFK transitions on a
        # CI runner where idle detection reports 0 ms and the hysteresis
        # would otherwise reset the flag on the next tick. None means
        # "normal behavior, hysteresis in charge".
        self._pinned_afk: bool | None = None

    def update(self) -> None:
        """Refresh all signals. Throttled to POLL_INTERVAL_MS so rapid
        callers (e.g. the 1 Hz behavior tick × 6 cats) don't hammer
        xprintidle every single time."""
        now = time.monotonic() * 1000
        if now - self._last_update < self.POLL_INTERVAL_MS:
            return
        self._last_update = now

        self.hour = datetime.datetime.now().hour
        self.cpu_load = self._read_loadavg()
        self.idle_ms = self._read_idle_ms()

        # Test pin wins over hysteresis — used by the e2e suite.
        if self._pinned_afk is not None:
            self.is_afk = self._pinned_afk
            return

        # AFK state is sticky — we enter it at >= IDLE_THRESHOLD_MS and
        # only leave it when idle drops well below (< IDLE_WAKEUP_THRESHOLD_MS)
        if not self.is_afk and self.idle_ms >= self.IDLE_THRESHOLD_MS:
            self.is_afk = True
        elif self.is_afk and self.idle_ms < self.IDLE_WAKEUP_THRESHOLD_MS:
            self.is_afk = False

    # ── Signal readers ───────────────────────────────────────────────────────

    def _read_idle_ms(self) -> int:
        """Return current user idle time in ms. Tries Mutter D-Bus
        first (cheap, no fork), falls back to xprintidle subprocess
        on non-GNOME setups, then to a constant 0 if neither is
        available. Never raises."""
        # Path 1 — Mutter D-Bus IdleMonitor (works on GNOME Wayland and X11)
        proxy = self._ensure_idle_proxy()
        if proxy is not None:
            try:
                # call_sync(method, params, flags, timeout_ms, cancellable)
                # GetIdletime takes no args and returns (t: u64)
                from gi.repository import Gio
                result = proxy.call_sync(
                    "GetIdletime", None,
                    Gio.DBusCallFlags.NONE, 200, None,
                )
                return int(result.unpack()[0])
            except Exception:
                # Mark dead so we don't keep retrying every poll. The
                # subprocess fallback below picks up the slack.
                log.debug("activity: Mutter idle monitor call failed", exc_info=True)
                self._idle_proxy = False

        # Path 2 — legacy xprintidle subprocess (X11-only)
        if self._xprintidle:
            try:
                r = subprocess.run(
                    [self._xprintidle],
                    capture_output=True, text=True, timeout=1,
                )
                return int(r.stdout.strip())
            except Exception:
                return 0
        return 0

    def _ensure_idle_proxy(self):
        """Lazy-create the Gio.DBusProxy for org.gnome.Mutter.IdleMonitor.
        Returns the proxy on success, None if D-Bus / GIO is unavailable
        or if we already determined Mutter isn't on the bus.

        ``self._idle_proxy`` tri-state:
            None  → first call, attempt construction
            False → previous attempt failed, don't retry every poll
            obj   → live proxy, ready to call
        """
        if self._idle_proxy is False:
            return None
        if self._idle_proxy is not None:
            return self._idle_proxy
        try:
            from gi.repository import Gio
            self._idle_proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,  # interface info
                "org.gnome.Mutter.IdleMonitor",
                "/org/gnome/Mutter/IdleMonitor/Core",
                "org.gnome.Mutter.IdleMonitor",
                None,  # cancellable
            )
            log.debug("activity: bound Mutter D-Bus IdleMonitor")
            return self._idle_proxy
        except Exception:
            log.debug("activity: Mutter D-Bus IdleMonitor unavailable", exc_info=True)
            self._idle_proxy = False
            return None

    def _read_loadavg(self) -> float:
        """First value of /proc/loadavg (1-min average). Returns 0 on non-Linux."""
        try:
            with open("/proc/loadavg") as f:
                return float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            return 0.0

    # ── Convenience predicates ───────────────────────────────────────────────

    def is_night(self) -> bool:
        """True between 22:00 and 05:59 local time."""
        return self.hour in self.NIGHT_HOURS

    def is_cpu_busy(self, threshold: float = 2.0) -> bool:
        """True if the 1-min load average is above the given threshold."""
        return self.cpu_load >= threshold

    def snapshot(self) -> dict:
        """Plain dict for the debug socket command."""
        return {
            "idle_ms": self.idle_ms,
            "is_afk": self.is_afk,
            "cpu_load": round(self.cpu_load, 2),
            "hour": self.hour,
            "is_night": self.is_night(),
        }
