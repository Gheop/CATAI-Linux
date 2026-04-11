"""Seasonal overlay — date-aware particle effects drawn on the canvas.

Pure date → season resolution plus a Cairo-only draw function that layers
snowflakes, pumpkins, hearts, cherry petals or fireworks on top of the
normal cat canvas. The rest of the app doesn't care which season is
active; it just calls ``draw_overlay(ctx, w, h)`` once per frame and the
module decides what (if anything) to paint.

Seasons & special events (Northern hemisphere-centric, user can override):

    winter      Dec 21 → Mar 20       — falling snowflakes
    spring      Mar 21 → Jun 20       — drifting cherry petals
    summer      Jun 21 → Sep 22       — nothing (cats already bright)
    autumn      Sep 23 → Dec 20       — falling leaves

Special overlays take precedence on their dates:

    halloween   Oct 24 → Nov  1       — floating pumpkins 🎃
    christmas   Dec 18 → Dec 26       — heavier snow + 🎄 at edges
    valentines  Feb 13 → Feb 15       — floating hearts ♥
    nye         Dec 31 → Jan  2       — firework bursts

All particles are pseudo-random but deterministic per-frame via a
time-based seed, so they animate smoothly without hitting Python's RNG
on every render. The draw function is a no-op for ``summer`` to keep the
default experience unchanged.

Override the detected season with env var ``CATAI_SEASON=winter`` (and
friends) — useful for screenshots and the e2e test suite.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import time


# ── Season resolver ──────────────────────────────────────────────────────────

# Canonical season names the rest of the code may use.
WINTER = "winter"
SPRING = "spring"
SUMMER = "summer"
AUTUMN = "autumn"
HALLOWEEN = "halloween"
CHRISTMAS = "christmas"
VALENTINES = "valentines"
NYE = "nye"

ALL_SEASONS = (
    WINTER, SPRING, SUMMER, AUTUMN,
    HALLOWEEN, CHRISTMAS, VALENTINES, NYE,
)


def resolve_season(now: _dt.date | None = None) -> str:
    """Return the active season for ``now`` (default: today)."""
    override = os.environ.get("CATAI_SEASON")
    if override and override in ALL_SEASONS:
        return override

    d = now or _dt.date.today()
    m, day = d.month, d.day

    # Special events first — narrow windows beat broad seasons.
    if (m == 12 and day >= 31) or (m == 1 and day <= 2):
        return NYE
    if m == 12 and 18 <= day <= 26:
        return CHRISTMAS
    if m == 10 and day >= 24 or m == 11 and day == 1:
        return HALLOWEEN
    if m == 2 and 13 <= day <= 15:
        return VALENTINES

    # Broad astronomical seasons.
    if (m == 12 and day >= 21) or m in (1, 2) or (m == 3 and day <= 20):
        return WINTER
    if (m == 3 and day >= 21) or m in (4, 5) or (m == 6 and day <= 20):
        return SPRING
    if (m == 6 and day >= 21) or m in (7, 8) or (m == 9 and day <= 22):
        return SUMMER
    return AUTUMN


# ── Cairo drawing ────────────────────────────────────────────────────────────

# Glyphs used by each seasonal overlay. Unicode is enough here — Pango/Cairo
# will fall back to the system emoji font (usually Noto Color Emoji).
_SNOWFLAKE = "\u2744"   # ❄
_PUMPKIN = "\U0001f383"  # 🎃
_HEART = "\u2665"        # ♥
_LEAF = "\U0001f342"     # 🍂
_PETAL = "\U0001f33c"    # 🌼
_TREE = "\U0001f384"     # 🎄
_SPARK = "\u2728"        # ✨


def _draw_symbol(ctx, sym: str, x: float, y: float, size: int,
                 r: float, g: float, b: float, a: float) -> None:
    """Render a single symbol at (x, y) via PangoCairo. We need Pango
    (not cairo's toy_font API) because every seasonal glyph — snowflake,
    pumpkin, heart, leaf, petal, tree, sparkle — is outside the standard
    monospace font and requires Noto Color Emoji fallback, which only
    PangoCairo performs. Without it cairo silently omits the glyph and
    the overlay renders nothing at all."""
    try:
        import gi
        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo
    except (ImportError, ValueError):
        return
    ctx.save()
    ctx.move_to(x, y)
    ctx.set_source_rgba(r, g, b, a)
    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(Pango.FontDescription(f"sans {size}"))
    layout.set_text(sym, -1)
    PangoCairo.show_layout(ctx, layout)
    ctx.restore()


def _pseudo_random(seed: int, mod: int) -> int:
    """Deterministic tiny LCG — avoids touching Python's RNG per-frame."""
    return (seed * 1103515245 + 12345) % mod


def _draw_snowflakes(ctx, w: int, h: int, density: int = 40) -> None:
    """Falling snowflakes — density defaults to 40, christmas bumps it."""
    t = time.monotonic()
    for i in range(density):
        speed = 20 + (i % 5) * 8
        xbase = _pseudo_random(i * 97 + 13, w)
        sway = math.sin(t * 0.7 + i * 0.3) * 12
        x = xbase + sway
        y = (t * speed + i * 73) % (h + 40) - 20
        size = 10 + (i % 4) * 2
        alpha = 0.55 + (i % 3) * 0.12
        _draw_symbol(ctx, _SNOWFLAKE, x, y, size, 0.95, 0.97, 1.0, alpha)


def _draw_pumpkins(ctx, w: int, h: int) -> None:
    """Floating pumpkins drifting upward (Halloween)."""
    t = time.monotonic()
    for i in range(8):
        speed = 12 + (i % 4) * 5
        xbase = _pseudo_random(i * 131 + 7, w)
        sway = math.sin(t * 0.5 + i) * 18
        x = xbase + sway
        y = h - ((t * speed + i * 110) % (h + 60)) + 40
        size = 22 + (i % 3) * 4
        _draw_symbol(ctx, _PUMPKIN, x, y, size, 1.0, 0.6, 0.2, 0.85)


def _draw_hearts(ctx, w: int, h: int) -> None:
    """Floating hearts (Valentine's)."""
    t = time.monotonic()
    for i in range(18):
        speed = 18 + (i % 5) * 6
        xbase = _pseudo_random(i * 53 + 21, w)
        sway = math.sin(t * 0.9 + i * 0.4) * 14
        x = xbase + sway
        y = h - ((t * speed + i * 47) % (h + 80)) + 30
        size = 10 + (i % 3) * 3
        _draw_symbol(ctx, _HEART, x, y, size, 0.95, 0.2, 0.35, 0.85)


def _draw_leaves(ctx, w: int, h: int) -> None:
    """Falling autumn leaves."""
    t = time.monotonic()
    for i in range(24):
        speed = 14 + (i % 5) * 5
        xbase = _pseudo_random(i * 71 + 9, w)
        sway = math.sin(t * 0.6 + i * 0.25) * 20
        x = xbase + sway
        y = (t * speed + i * 83) % (h + 40) - 20
        size = 12 + (i % 3) * 3
        _draw_symbol(ctx, _LEAF, x, y, size, 0.85, 0.5, 0.15, 0.85)


def _draw_petals(ctx, w: int, h: int) -> None:
    """Drifting cherry petals (spring)."""
    t = time.monotonic()
    for i in range(20):
        speed = 10 + (i % 4) * 4
        xbase = _pseudo_random(i * 41 + 19, w)
        sway = math.sin(t * 0.8 + i * 0.35) * 26
        x = xbase + sway
        y = (t * speed + i * 61) % (h + 30) - 15
        size = 10 + (i % 3) * 2
        _draw_symbol(ctx, _PETAL, x, y, size, 1.0, 0.75, 0.85, 0.85)


def _draw_fireworks(ctx, w: int, h: int) -> None:
    """Bursts of sparkles at random anchor points (NYE)."""
    t = time.monotonic()
    for burst in range(3):
        cycle = (t * 0.3 + burst * 0.5) % 1.0
        if cycle > 0.8:  # quiet half of each cycle
            continue
        cx = _pseudo_random(burst * 211 + int(t / 3), w)
        cy = h * 0.3 + _pseudo_random(burst * 37, h // 3)
        for i in range(12):
            angle = i * (2 * math.pi / 12)
            r_out = cycle * 60
            x = cx + math.cos(angle) * r_out
            y = cy + math.sin(angle) * r_out
            alpha = max(0.0, 1.0 - cycle * 1.1)
            size = 10 + int(cycle * 6)
            cr = 0.9 + 0.1 * math.sin(i + burst)
            cg = 0.6 + 0.4 * math.cos(i * 0.5 + burst)
            cb = 0.2 + 0.8 * math.sin(i * 0.3)
            _draw_symbol(ctx, _SPARK, x, y, size, cr, cg, cb, alpha)


def _draw_christmas(ctx, w: int, h: int) -> None:
    _draw_snowflakes(ctx, w, h, density=70)
    # Sprinkle a few 🎄 along the bottom edge
    for i in range(5):
        x = (i + 1) * (w // 6) - 20
        y = h - 10
        _draw_symbol(ctx, _TREE, x, y, 32, 0.2, 0.6, 0.25, 0.85)


# Dispatch table: season → overlay function
_DISPATCH = {
    WINTER:     _draw_snowflakes,
    SPRING:     _draw_petals,
    AUTUMN:     _draw_leaves,
    HALLOWEEN:  _draw_pumpkins,
    CHRISTMAS:  _draw_christmas,
    VALENTINES: _draw_hearts,
    NYE:        _draw_fireworks,
    # SUMMER is omitted → no overlay
}


def draw_overlay(ctx, w: int, h: int, season: str | None = None) -> None:
    """Render the seasonal particles for ``season`` (default: resolve now).

    Safe to call every frame — each helper is O(density) with small
    constants (~20-40 particles max). For ``summer`` (or any unknown
    season) this is a cheap no-op.
    """
    s = season or resolve_season()
    fn = _DISPATCH.get(s)
    if fn is None:
        return
    fn(ctx, w, h)
