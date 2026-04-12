"""Cairo/Pango drawing helpers: speech bubbles, overlays, context menus, CSS.

All functions are pure — they take a `cairo.Context` and primitives (text,
coordinates, cat dimensions) and draw onto the context. No dependency on
CatInstance or CatAIApp, so they can be tested in isolation with a
tempfile-backed surface.
"""
from __future__ import annotations

import math
import os
import time

import cairo
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gtk, Pango, PangoCairo  # noqa: E402

# Bundled pixel-art icons (mic + speaker on/off) live here. Cached as
# cairo.ImageSurface on first load — they're tiny so memory is fine.
ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
_icon_cache: dict[str, cairo.ImageSurface] = {}


def _load_icon(name: str) -> cairo.ImageSurface | None:
    """Return a cached cairo.ImageSurface for ``name``, or None if the
    PNG isn't bundled (graceful degradation: callers fall back to a
    Pango emoji glyph)."""
    if name in _icon_cache:
        return _icon_cache[name]
    path = os.path.join(ICONS_DIR, f"{name}.png")
    if not os.path.isfile(path):
        return None
    try:
        surface = cairo.ImageSurface.create_from_png(path)
    except Exception:
        return None
    _icon_cache[name] = surface
    return surface


# ── CSS Theme ─────────────────────────────────────────────────────────────────

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
.quake-console {
    background: rgba(0, 0, 0, 0.85);
}
.quake-output {
    color: #33ff33;
    font-family: monospace;
    font-size: 12px;
}
.quake-input {
    color: #33ff33;
    background: transparent;
    border: none;
    font-family: monospace;
    font-size: 12px;
    caret-color: #33ff33;
}
.quake-input:focus {
    outline: none;
    box-shadow: none;
}
.quake-prompt {
    color: #33ff33;
    font-family: monospace;
    font-size: 12px;
}
.quake-border {
    background: #33ff33;
    min-height: 2px;
}
"""


def apply_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


# ── Theme palette ────────────────────────────────────────────────────────────
# Mutable singleton — switched at runtime by set_theme() when the desktop
# dark/light preference changes. Each draw_* function reads its colors
# from here instead of hardcoding, so swapping the palette in one place
# ripples across every bubble / menu / overlay.

LIGHT_THEME = {
    "bubble_bg": (0.95, 0.9, 0.8, 1.0),         # cream background
    "bubble_bg_translucent": (0.95, 0.9, 0.8, 0.95),
    "bubble_border": (0.3, 0.2, 0.1, 1.0),      # dark brown border
    "bubble_text": (0.3, 0.2, 0.1, 1.0),        # dark brown text
    "overlay_text": (0.3, 0.2, 0.1, 1.0),       # ZzZ etc.
}

DARK_THEME = {
    "bubble_bg": (0.16, 0.13, 0.10, 1.0),         # deep coffee background
    "bubble_bg_translucent": (0.16, 0.13, 0.10, 0.93),
    "bubble_border": (0.95, 0.82, 0.55, 1.0),     # warm tan border
    "bubble_text": (0.95, 0.88, 0.70, 1.0),       # warm cream text
    "overlay_text": (0.95, 0.88, 0.70, 1.0),
}

# Current active palette — mutated in place by set_theme() so existing
# module-level references stay valid.
THEME: dict = dict(LIGHT_THEME)


def set_theme(dark: bool) -> None:
    """Swap the active bubble/menu palette. Called by the CatAIApp theme
    poller (catai_linux.theme.is_dark_mode) when the desktop color-scheme
    preference flips."""
    src = DARK_THEME if dark else LIGHT_THEME
    THEME.clear()
    THEME.update(src)


# ── Pixel-art atoms ───────────────────────────────────────────────────────────

def draw_pixel_tail(ctx, w: int, h: int, px: int = 3) -> None:
    """Pixel-art speech-bubble tail pointing down. Used by the chat bubble."""
    cx = w / 2
    ctx.set_source_rgba(*THEME["bubble_border"])
    for row in range(5):
        bw = px * (5 - row)
        ctx.rectangle(cx - bw/2, row * px, bw, px); ctx.fill()
    ctx.set_source_rgba(*THEME["bubble_bg"])
    for row in range(4):
        bw = px * (5 - row) - px * 2
        if bw > 0:
            ctx.rectangle(cx - bw/2, row * px, bw, px); ctx.fill()


# ── Pango text helpers ────────────────────────────────────────────────────────

BUBBLE_FONT = "monospace bold 11"


def _pango_show_text(ctx, text: str, r: float | None = None, g: float | None = None,
                     b: float | None = None, a: float | None = None) -> None:
    """Render text with PangoCairo (supports COLRv1 emoji).

    When r/g/b/a are None, uses the active THEME["bubble_text"] — so bubbles
    pick up the dark/light palette automatically without every caller having
    to thread the color through.
    """
    if r is None or g is None or b is None or a is None:
        r, g, b, a = THEME["bubble_text"]
    ctx.set_source_rgba(r, g, b, a)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(BUBBLE_FONT))
    lay.set_text(text, -1)
    PangoCairo.show_layout(ctx, lay)


def _pango_text_size(ctx, text: str) -> tuple[int, int]:
    """Return (width, height) in pixels for text using current bubble font."""
    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(Pango.FontDescription(BUBBLE_FONT))
    layout.set_text(text, -1)
    return layout.get_pixel_size()  # (w, h)


def _draw_pango_symbol(ctx, text: str, x: float, y: float, size: int,
                       r: float, g: float, b: float, a: float) -> None:
    """Render a single Unicode symbol via PangoCairo (handles emoji/symbols)."""
    ctx.save()
    ctx.move_to(x, y)
    ctx.set_source_rgba(r, g, b, a)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(f"sans bold {size}"))
    lay.set_text(text, -1)
    PangoCairo.show_layout(ctx, lay)
    ctx.restore()


# ── Speech bubbles ────────────────────────────────────────────────────────────

def draw_meow_bubble(ctx, text: str, cat_x: float, cat_y: float,
                     cat_w: float, cat_h: float = 80, screen_h: int | None = None) -> None:
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
    if screen_h is not None and by + bh > screen_h:
        by = cat_y - bh - 8  # back above (last resort)
    bx = max(4, bx)

    # Background
    ctx.set_source_rgba(*THEME["bubble_bg"])
    ctx.rectangle(bx, by, bw, bh)
    ctx.fill()

    # Border (2px)
    px = 2
    ctx.set_source_rgba(*THEME["bubble_border"])
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
    ctx.set_source_rgba(*THEME["bubble_bg"])
    for cx, cy in [(bx, by), (bx + bw - px, by), (bx, by + bh - px), (bx + bw - px, by + bh - px)]:
        ctx.rectangle(cx, cy, px, px); ctx.fill()

    # Text (centered)
    tx = bx + (bw - text_w) / 2
    ty = by + (bh - text_h) / 2
    ctx.move_to(tx, ty)
    _pango_show_text(ctx, text)


def draw_encounter_bubble(ctx, text: str, cat_x: float, cat_y: float,
                          cat_w: float, cat_h: float) -> None:
    """Draw a short encounter speech bubble above a cat (word-wrapped, no entry)."""
    pad_x, pad_y = 10, 6
    max_content_w = 260  # max text width in pixels

    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(BUBBLE_FONT))
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

    ctx.set_source_rgba(*THEME["bubble_bg"])
    ctx.rectangle(bx, by, bw, bh)
    ctx.fill()
    px = 2
    ctx.set_source_rgba(*THEME["bubble_border"])
    for rx, ry, rw, rh in [(bx, by, bw, px), (bx, by + bh - px, bw, px),
                            (bx, by, px, bh), (bx + bw - px, by, px, bh)]:
        ctx.rectangle(rx, ry, rw, rh); ctx.fill()
    ctx.set_source_rgba(*THEME["bubble_bg"])
    for cx, cy in [(bx, by), (bx + bw - px, by), (bx, by + bh - px), (bx + bw - px, by + bh - px)]:
        ctx.rectangle(cx, cy, px, px); ctx.fill()

    ctx.move_to(bx + pad_x, by + pad_y)
    ctx.set_source_rgba(*THEME["bubble_text"])
    PangoCairo.show_layout(ctx, lay)


def draw_chat_bubble(ctx, text: str, cat_x: float, cat_y: float,
                     cat_w: float, cat_h: float,
                     speaker_state: bool | None = None) -> tuple[int, int, int, int] | None:
    """Draw a chat response bubble above a cat on the Cairo canvas.

    When ``speaker_state`` is not None, a small speaker icon is drawn
    in the top-right corner of the bubble (🔊 when True, 🔇 when False)
    and the function returns its click rect ``(x, y, w, h)`` so the
    caller can route clicks. Returns None if ``speaker_state`` is None.
    """
    pad = 12
    content_w = 256  # text area = bw - 2*pad

    # Reserve text width on the right for the speaker icon so wrapped
    # lines never run under it. Pango doesn't natively support shape
    # exclusion regions, so we just narrow the whole layout — wastes
    # a tiny strip below the icon but keeps the text clean.
    icon_reserve = 0
    if speaker_state is not None:
        icon_name = "speaker_on" if speaker_state else "speaker_off"
        icon_surface_preview = _load_icon(icon_name)
        iw = icon_surface_preview.get_width() if icon_surface_preview else 22
        icon_reserve = iw + 12  # icon + margin + outline padding
    text_w = content_w - icon_reserve

    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(BUBBLE_FONT))
    lay.set_text(text, -1)
    lay.set_width(text_w * Pango.SCALE)
    lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    # Cap at 16 lines: Claude/Ollama responses are clamped to 256
    # max_tokens (~12-14 lines at this bubble width), so 16 leaves
    # comfortable headroom while still ellipsizing pathological cases.
    lay.set_height(-16)
    lay.set_ellipsize(Pango.EllipsizeMode.END)
    _tw, th = lay.get_pixel_size()

    bw = content_w + pad * 2  # 280 — keep bubble width constant so the
                              # icon sits in the right strip outside
                              # the text area
    bh = pad * 2 + th + 42   # text + 30 entry + 12 pad
    bx = cat_x + cat_w / 2 - bw / 2
    by = cat_y - bh - 15
    # Track which side of the cat the bubble lands on so the tail
    # points the right way (towards the cat).
    bubble_above_cat = True
    if by < 0:
        by = cat_y + cat_h + 10
        bubble_above_cat = False

    # Background
    ctx.set_source_rgba(*THEME["bubble_bg_translucent"])
    ctx.rectangle(bx, by, bw, bh)
    ctx.fill()

    # Border
    px = 3
    ctx.set_source_rgba(*THEME["bubble_border"])
    ctx.rectangle(bx, by, bw, px); ctx.fill()
    ctx.rectangle(bx, by + bh - px, bw, px); ctx.fill()
    ctx.rectangle(bx, by, px, bh); ctx.fill()
    ctx.rectangle(bx + bw - px, by, px, bh); ctx.fill()

    # Text via Pango (handles emoji width correctly)
    ctx.move_to(bx + pad, by + pad)
    ctx.set_source_rgba(*THEME["bubble_text"])
    PangoCairo.show_layout(ctx, lay)

    # Tail — small triangle pointing toward the cat. If the bubble
    # had to flip below the cat (cat near the top of the screen),
    # the tail goes on the TOP edge pointing UP instead of the
    # bottom edge pointing DOWN.
    tx = bx + bw / 2
    ctx.set_source_rgba(*THEME["bubble_border"])
    if bubble_above_cat:
        ty = by + bh
        ctx.move_to(tx - 8, ty)
        ctx.line_to(tx + 8, ty)
        ctx.line_to(tx, ty + 10)
    else:
        ty = by
        ctx.move_to(tx - 8, ty)
        ctx.line_to(tx + 8, ty)
        ctx.line_to(tx, ty - 10)
    ctx.close_path()
    ctx.fill()

    # Speaker toggle icon in the top-right corner of the bubble.
    # Returns the click rect (x, y, w, h) so the canvas click handler
    # can detect toggles. Renders the bundled pixel-art PNG via cairo
    # ImageSurface — falls back to a Pango emoji glyph if the icon
    # files are missing.
    if speaker_state is not None:
        icon_name = "speaker_on" if speaker_state else "speaker_off"
        surface = _load_icon(icon_name)
        if surface is not None:
            icon_w = surface.get_width()
            icon_h = surface.get_height()
        else:
            icon_w, icon_h = 22, 20
        icon_margin = 6
        icon_x = int(bx + bw - icon_w - icon_margin - px)
        icon_y = int(by + icon_margin + px)
        # Background chip so the icon stays visible on top of any
        # text bleed (semi-opaque rounded square outlined in the
        # bubble border color).
        ctx.set_source_rgba(*THEME["bubble_bg"])
        ctx.rectangle(icon_x - 2, icon_y - 2, icon_w + 4, icon_h + 4)
        ctx.fill()
        ctx.set_source_rgba(*THEME["bubble_border"])
        ctx.set_line_width(1.5)
        ctx.rectangle(icon_x - 2, icon_y - 2, icon_w + 4, icon_h + 4)
        ctx.stroke()
        if surface is not None:
            ctx.save()
            ctx.set_source_surface(surface, icon_x, icon_y)
            ctx.paint()
            ctx.restore()
        else:
            # Fallback emoji rendering if PNGs are missing for some reason
            glyph = "\U0001f4e2" if speaker_state else "\U0001f507"
            _draw_pango_symbol(ctx, glyph, icon_x, icon_y, 14,
                               *THEME["bubble_text"])
        return (icon_x - 2, icon_y - 2, icon_w + 4, icon_h + 4)
    return None


def draw_context_menu(ctx, mx: float, my: float,
                      label_settings: str, label_quit: str) -> None:
    """Draw a right-click context menu on the canvas."""
    bw, bh = 120, 50
    pad = 8
    ctx.set_source_rgba(*THEME["bubble_bg_translucent"])
    ctx.rectangle(mx, my, bw, bh)
    ctx.fill()
    # Border
    ctx.set_source_rgba(*THEME["bubble_border"])
    ctx.set_line_width(2)
    ctx.rectangle(mx, my, bw, bh)
    ctx.stroke()
    # Separator
    ctx.move_to(mx + pad, my + 25)
    ctx.line_to(mx + bw - pad, my + 25)
    ctx.stroke()
    # Text
    ctx.set_source_rgba(*THEME["bubble_text"])
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(11)
    ctx.move_to(mx + pad, my + 17)
    ctx.show_text(label_settings)
    ctx.move_to(mx + pad, my + 42)
    ctx.show_text(label_quit)


# ── Cat state overlays ────────────────────────────────────────────────────────

def draw_zzz(ctx, cat_x: float, cat_y: float, cat_w: float) -> None:
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
        tr, tg, tb, _ta = THEME["overlay_text"]
        ctx.set_source_rgba(tr, tg, tb, alpha)
        ctx.move_to(x, y)
        ctx.show_text("Z")


def draw_exclamation(ctx, cat_x: float, cat_y: float, cat_w: float, cat_h: float) -> None:
    """Draw shaking !!! just above a surprised cat's head."""
    t = time.monotonic()
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    head_y = cat_y + cat_h * 0.3
    for i, (size, dx) in enumerate([(11, -6), (9, 2), (7, 8)]):
        shake = math.sin(t * 12 + i * 2) * 2
        x = cat_x + cat_w // 2 + dx + shake
        y = head_y - i * 3
        r = 0.9 - i * 0.2
        ctx.set_font_size(size)
        ctx.set_source_rgba(r, 0.7 - i * 0.2, 0.0, 1.0)
        ctx.move_to(x, y)
        ctx.show_text("!")


def draw_hearts(ctx, cat_x: float, cat_y: float, cat_w: float, cat_h: float) -> None:
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


def draw_hurt_stars(ctx, cat_x: float, cat_y: float, cat_w: float, cat_h: float) -> None:
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


def draw_skull(ctx, cat_x: float, cat_y: float, cat_w: float, cat_h: float) -> None:
    """Draw a floating skull above a dying cat, rising and fading."""
    t = time.monotonic()
    offset_y = ((t * 0.4) % 2.5) / 2.5  # slow rise cycle
    alpha = 1.0 - offset_y * 0.9
    head_y = cat_y + cat_h * 0.3
    _draw_pango_symbol(ctx, "\U0001f480", cat_x + cat_w // 2 - 6, head_y - int(offset_y * 20), 12, 0.5, 0.4, 0.4, alpha)


def draw_birth_sparkles(ctx, cat_x: float, cat_y: float, cat_w: float,
                        cat_h: float, progress: float) -> None:
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
        twinkle = 0.5 + 0.5 * math.sin(t * 4 + i)
        alpha = base_alpha * twinkle
        size = 10 + int(twinkle * 4)
        _draw_pango_symbol(ctx, "\u2728", sx - size / 2, sy - size / 2,
                           size, 1.0, 0.95, 0.5, alpha)


def draw_sparkle(ctx, cat_x: float, cat_y: float, cat_w: float, cat_h: float) -> None:
    """Draw a pulsing sparkle above a grooming cat."""
    t = time.monotonic()
    pulse = 0.6 + 0.4 * math.sin(t * 3)
    _draw_pango_symbol(ctx, "\u2728", cat_x + cat_w // 2 + 4, cat_y + cat_h * 0.3, 10, 0.7, 0.85, 0.95, pulse)


def draw_anger(ctx, cat_x: float, cat_y: float, cat_w: float, cat_h: float) -> None:
    """Draw a shaking anger symbol above an angry cat."""
    t = time.monotonic()
    shake = math.sin(t * 14) * 2
    _draw_pango_symbol(ctx, "\U0001f4a2", cat_x + cat_w // 2 + shake, cat_y + cat_h * 0.25, 11, 0.85, 0.15, 0.1, 1.0)


def draw_speed_lines(ctx, cat_x: float, cat_y: float, cat_w: float,
                     cat_h: float, direction: str) -> None:
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
