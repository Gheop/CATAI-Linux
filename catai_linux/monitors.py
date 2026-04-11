"""Multi-monitor geometry helpers — pure Python, no GTK dependency.

Takes a list of ``(x, y, width, height)`` monitor rects (typically gathered
from ``Gdk.Display.get_monitors()`` in ``app.py``) and provides:

- ``monitor_at(px, py, rects)`` — point-in-rect lookup, returns the rect
  containing the point or None.
- ``nearest_monitor(px, py, rects)`` — closest rect by Euclidean distance,
  used to rescue cats that wandered into a dead zone between mismatched
  monitors (e.g. a 1080p next to a 4K where the bottom half is empty).
- ``snap_to_nearest(px, py, w, h, rects)`` — returns a clamped position
  that is guaranteed to be fully inside one of the rects, or None if
  ``rects`` is empty.
- ``distribute_spawns(n, rects, rng)`` — picks ``n`` spawn points round-
  robined across the monitor list. Used to seed initial cat positions so
  a dual-monitor user doesn't get all cats in the top-left of monitor 0.

Kept module-scoped & stateless so it's trivial to unit-test on a headless
CI runner — no GTK import, no display connection required.
"""
from __future__ import annotations

import random
from collections.abc import Sequence


# Canonical type: a monitor rect is a 4-tuple (x, y, width, height) in the
# Gdk coordinate system (Y grows downward). The left/top edge can be
# negative if the monitor is placed above/left of monitor 0.
Rect = tuple[int, int, int, int]


def _contains(rect: Rect, px: float, py: float) -> bool:
    x, y, w, h = rect
    return x <= px < x + w and y <= py < y + h


def _center(rect: Rect) -> tuple[float, float]:
    x, y, w, h = rect
    return x + w / 2, y + h / 2


def _clamped_distance(rect: Rect, px: float, py: float) -> float:
    """Squared distance from (px, py) to the nearest edge of ``rect``.
    Returns 0 if the point is inside the rect."""
    x, y, w, h = rect
    dx = 0.0
    if px < x:
        dx = x - px
    elif px >= x + w:
        dx = px - (x + w - 1)
    dy = 0.0
    if py < y:
        dy = y - py
    elif py >= y + h:
        dy = py - (y + h - 1)
    return dx * dx + dy * dy


def monitor_at(px: float, py: float,
               rects: Sequence[Rect]) -> Rect | None:
    """Return the first rect containing (px, py), or None."""
    for r in rects:
        if _contains(r, px, py):
            return r
    return None


def nearest_monitor(px: float, py: float,
                    rects: Sequence[Rect]) -> Rect | None:
    """Return the rect whose closest edge is nearest to (px, py).
    Returns None if ``rects`` is empty."""
    if not rects:
        return None
    best = None
    best_d = float("inf")
    for r in rects:
        d = _clamped_distance(r, px, py)
        if d < best_d:
            best_d = d
            best = r
    return best


def snap_to_nearest(px: float, py: float, w: int, h: int,
                    rects: Sequence[Rect]) -> tuple[int, int] | None:
    """Clamp ``(px, py)`` so a ``w×h`` box is fully inside the nearest
    monitor. Returns ``(clamped_x, clamped_y)`` or None if ``rects`` is
    empty. Used to rescue cats that have wandered into a dead zone.

    If the box doesn't fit the rect (rect smaller than the box), the box
    is left-top-aligned inside the rect — this should never happen in
    practice (cats are 80×80 at 1.0 scale and even 1024×600 displays fit).
    """
    rect = nearest_monitor(px, py, rects)
    if rect is None:
        return None
    rx, ry, rw, rh = rect
    cx = int(max(rx, min(px, rx + rw - w)))
    cy = int(max(ry, min(py, ry + rh - h)))
    return cx, cy


def distribute_spawns(n: int, rects: Sequence[Rect],
                      rng: random.Random | None = None,
                      padding: int = 40) -> list[tuple[int, int]]:
    """Return ``n`` random spawn points distributed round-robin across
    ``rects``. Each point is kept ``padding`` px away from the monitor
    edges so the cat doesn't clip off-screen. Empty ``rects`` returns an
    empty list (caller falls back to a hard-coded default)."""
    if not rects or n <= 0:
        return []
    r = rng or random
    out: list[tuple[int, int]] = []
    for i in range(n):
        rect = rects[i % len(rects)]
        rx, ry, rw, rh = rect
        x_lo = rx + padding
        x_hi = max(x_lo + 1, rx + rw - padding)
        y_lo = ry + padding
        y_hi = max(y_lo + 1, ry + rh - padding)
        out.append((r.randint(x_lo, x_hi - 1), r.randint(y_lo, y_hi - 1)))
    return out
