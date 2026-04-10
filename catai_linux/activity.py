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

Everything is best-effort. If the platform doesn't expose a given
signal (e.g. xprintidle missing on pure Wayland, /proc/loadavg on
non-Linux) the corresponding check silently returns a neutral value.

The monitor is stateless: callers (CatAIApp.behavior_tick) invoke
``update()`` once per tick, then read the properties. No background
threads, no subprocess pools — the subprocess cost is ~10 ms every
few seconds and the rest is file reads / int arithmetic.
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
        self._xprintidle = shutil.which("xprintidle")

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

        # AFK state is sticky — we enter it at >= IDLE_THRESHOLD_MS and
        # only leave it when idle drops well below (< IDLE_WAKEUP_THRESHOLD_MS)
        if not self.is_afk and self.idle_ms >= self.IDLE_THRESHOLD_MS:
            self.is_afk = True
        elif self.is_afk and self.idle_ms < self.IDLE_WAKEUP_THRESHOLD_MS:
            self.is_afk = False

    # ── Signal readers ───────────────────────────────────────────────────────

    def _read_idle_ms(self) -> int:
        """Return current user idle time in ms. Returns 0 if we can't
        determine it (non-X11 session, xprintidle missing, etc.)."""
        if not self._xprintidle:
            return 0
        try:
            r = subprocess.run(
                [self._xprintidle],
                capture_output=True, text=True, timeout=1,
            )
            return int(r.stdout.strip())
        except Exception:
            return 0

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
