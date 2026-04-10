#!/usr/bin/env python3
"""Render CATAI-Linux README screenshots directly to PNG via Cairo.

Bypasses screen capture entirely — uses the same drawing primitives as the
real app to produce deterministic, pixel-perfect images.

Output:
    screenshot1.png — 4 cats showcasing different overlays on a screen-like bg
    screenshot2.png — cat with open chat bubble

Usage: python3 tools/render_screenshots.py
"""
import math
import os
import time

import gi
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo
import cairo
from PIL import Image

# ── Config ────────────────────────────────────────────────────────────────────

ROOT = os.path.join(os.path.dirname(__file__), "..")
PKG_DIR = os.path.join(ROOT, "catai_linux")

# Screenshot dimensions
W, H = 1280, 720

# Scaled cat display size (matches DEFAULT_SCALE ~1.5 in the real app)
CAT_SCALE = 1.5
SPRITE = 80
CAT_W = int(SPRITE * CAT_SCALE)  # 120

# ── Sprite loading ────────────────────────────────────────────────────────────

def load_frame(cat_id, anim_name, direction, frame_idx):
    anim_dir = os.path.join(PKG_DIR, cat_id, "animations", anim_name, direction)
    if not os.path.isdir(anim_dir):
        return None
    files = sorted(f for f in os.listdir(anim_dir) if f.endswith(".png"))
    if not files or frame_idx >= len(files):
        return None
    return Image.open(os.path.join(anim_dir, files[frame_idx])).convert("RGBA")


def load_rotation(cat_id, direction="south"):
    p = os.path.join(PKG_DIR, cat_id, "rotations", f"{direction}.png")
    return Image.open(p).convert("RGBA") if os.path.exists(p) else None


def pil_to_surface(img, target_w=None, target_h=None):
    """Convert PIL to cairo.ImageSurface, scaled nearest-neighbor."""
    if target_w and target_h:
        img = img.resize((target_w, target_h), Image.NEAREST)
    w, h = img.size
    data = bytearray(img.tobytes())
    for i in range(0, len(data), 4):
        data[i], data[i+2] = data[i+2], data[i]
    surface = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_ARGB32, w, h, w * 4)
    return surface, data


# ── Overlay drawings (copied from app.py, static at chosen time offset) ───────

def _pango_sym(ctx, text, x, y, size, r, g, b, a):
    ctx.save()
    ctx.move_to(x, y)
    ctx.set_source_rgba(r, g, b, a)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(f"sans bold {size}"))
    lay.set_text(text, -1)
    PangoCairo.show_layout(ctx, lay)
    ctx.restore()


def draw_zzz(ctx, x, y, w, h, phase=0.0):
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    base_y = y + w * 0.25
    for i, (size, ph, dx) in enumerate([(14, 0.0, 6), (11, 1.0, 14), (8, 2.0, 20)]):
        offset_y = ((phase * 0.6 + ph) % 3.0) / 3.0
        alpha = 1.0 - offset_y * 0.7
        ctx.set_font_size(size)
        ctx.set_source_rgba(0.3, 0.2, 0.1, alpha)
        ctx.move_to(x + w // 2 + dx, base_y - int(offset_y * 22))
        ctx.show_text("Z")


def draw_hearts(ctx, x, y, w, h, phase=0.0):
    head_y = y + h * 0.3
    for i, (size, ph, dx) in enumerate([(14, 0.0, -2), (11, 1.2, 8), (9, 2.4, 16)]):
        offset_y = ((phase * 0.5 + ph) % 3.0) / 3.0
        alpha = 1.0 - offset_y * 0.7
        dr = 0.9 - i * 0.15
        _pango_sym(ctx, "\u2665", x + w // 2 + dx, head_y - int(offset_y * 22), size, dr, 0.2, 0.3, alpha)


def draw_exclamation(ctx, x, y, w, h, phase=0.0):
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    head_y = y + h * 0.3
    for i, (size, dx) in enumerate([(15, -8), (12, 3), (9, 11)]):
        r = 0.9 - i * 0.2
        ctx.set_font_size(size)
        ctx.set_source_rgba(r, 0.7 - i * 0.2, 0.0, 1.0)
        ctx.move_to(x + w // 2 + dx, head_y - i * 4)
        ctx.show_text("!")


def draw_sparkle(ctx, x, y, w, h, phase=0.0):
    pulse = 0.6 + 0.4 * math.sin(phase * 3)
    _pango_sym(ctx, "\u2728", x + w // 2 + 4, y + h * 0.3, 14, 0.7, 0.85, 0.95, pulse)


def draw_skull(ctx, x, y, w, h, phase=0.0):
    offset_y = ((phase * 0.4) % 2.5) / 2.5
    alpha = 1.0 - offset_y * 0.9
    head_y = y + h * 0.3
    _pango_sym(ctx, "\U0001f480", x + w // 2 - 8, head_y - int(offset_y * 24), 16, 0.5, 0.4, 0.4, alpha)


def draw_speed_lines(ctx, x, y, w, h, direction, phase=0.0):
    if direction == "east":
        base_x = x - 6
    else:
        base_x = x + w + 6
    ctx.set_line_width(2.5)
    for i in range(5):
        ph = (phase * 8 + i * 0.4) % 1.0
        alpha = 0.7 - ph * 0.6
        ly = y + h * 0.25 + i * (h * 0.13)
        length = 16 + ph * 12
        ctx.set_source_rgba(0.6, 0.5, 0.4, max(0, alpha))
        ctx.move_to(base_x, ly)
        if direction == "east":
            ctx.line_to(base_x - length, ly)
        else:
            ctx.line_to(base_x + length, ly)
        ctx.stroke()


# ── Meow / Chat bubble drawing ───────────────────────────────────────────────

BUBBLE_FONT = "monospace bold 14"


def _pango_text_size(ctx, text, font=BUBBLE_FONT):
    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(Pango.FontDescription(font))
    layout.set_text(text, -1)
    return layout.get_pixel_size()


def draw_meow_bubble(ctx, text, cat_x, cat_y, cat_w):
    text_w, text_h = _pango_text_size(ctx, text)
    pad_x, pad_y = 14, 10
    bw = max(90, text_w + pad_x * 2)
    bh = text_h + pad_y * 2
    bx = cat_x + cat_w / 2 - bw / 2
    by = cat_y - bh - 10

    ctx.set_source_rgba(0.95, 0.9, 0.8, 1)
    ctx.rectangle(bx, by, bw, bh)
    ctx.fill()

    px = 2
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.rectangle(bx, by, bw, px); ctx.fill()
    ctx.rectangle(bx, by + bh - px, bw, px); ctx.fill()
    ctx.rectangle(bx, by, px, bh); ctx.fill()
    ctx.rectangle(bx + bw - px, by, px, bh); ctx.fill()
    # Inner border (pixel art double line)
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
    ctx.move_to(bx + (bw - text_w) / 2, by + (bh - text_h) / 2)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(BUBBLE_FONT))
    lay.set_text(text, -1)
    PangoCairo.show_layout(ctx, lay)


def draw_chat_bubble(ctx, text, cat_x, cat_y, cat_w, placeholder="Talk to the cat..."):
    pad = 14
    content_w = 280
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(BUBBLE_FONT))
    lay.set_text(text, -1)
    lay.set_width(content_w * Pango.SCALE)
    lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    _tw, th = lay.get_pixel_size()

    bw = content_w + pad * 2
    bh = pad * 2 + th + 40  # text + fake entry + pad
    bx = cat_x + cat_w / 2 - bw / 2
    by = cat_y - bh - 18

    # Background
    ctx.set_source_rgba(0.95, 0.9, 0.8, 0.97)
    ctx.rectangle(bx, by, bw, bh)
    ctx.fill()

    # Border (3px)
    px = 3
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.rectangle(bx, by, bw, px); ctx.fill()
    ctx.rectangle(bx, by + bh - px, bw, px); ctx.fill()
    ctx.rectangle(bx, by, px, bh); ctx.fill()
    ctx.rectangle(bx + bw - px, by, px, bh); ctx.fill()

    # Text
    ctx.move_to(bx + pad, by + pad)
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    PangoCairo.show_layout(ctx, lay)

    # Fake input field
    iy = by + pad + th + 8
    iw = content_w
    ih = 26
    ix = bx + pad
    ctx.set_source_rgba(1, 1, 1, 0.85)
    ctx.rectangle(ix, iy, iw, ih); ctx.fill()
    ctx.set_source_rgba(0.3, 0.2, 0.1, 0.7)
    ctx.set_line_width(1)
    ctx.rectangle(ix, iy, iw, ih); ctx.stroke()
    ctx.move_to(ix + 8, iy + 6)
    ctx.set_source_rgba(0.5, 0.4, 0.3, 0.55)
    lay2 = PangoCairo.create_layout(ctx)
    lay2.set_font_description(Pango.FontDescription("monospace 12"))
    lay2.set_text(placeholder, -1)
    PangoCairo.show_layout(ctx, lay2)

    # Tail (triangle pointing down)
    tx = bx + bw / 2
    ty = by + bh
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.move_to(tx - 10, ty)
    ctx.line_to(tx + 10, ty)
    ctx.line_to(tx, ty + 12)
    ctx.close_path()
    ctx.fill()


# ── Background (desktop-like) ────────────────────────────────────────────────

def draw_background(ctx, w, h):
    """Draw a subtle desktop-like gradient background."""
    # Dark purple/navy gradient
    pat = cairo.LinearGradient(0, 0, 0, h)
    pat.add_color_stop_rgb(0, 0.10, 0.10, 0.18)
    pat.add_color_stop_rgb(1, 0.05, 0.05, 0.10)
    ctx.set_source(pat)
    ctx.rectangle(0, 0, w, h)
    ctx.fill()
    # Subtle dots pattern
    ctx.set_source_rgba(1, 1, 1, 0.03)
    for yy in range(0, h, 40):
        for xx in range(0, w, 40):
            ctx.rectangle(xx, yy, 2, 2)
            ctx.fill()


# ── Scene renderers ──────────────────────────────────────────────────────────

def render_scene1():
    """4 cats with different overlays on a desktop-like background."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx = cairo.Context(surface)
    ctx.set_antialias(cairo.ANTIALIAS_NONE)

    draw_background(ctx, W, H)

    # Title
    ctx.set_source_rgba(1, 0.85, 0.3, 0.9)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription("monospace bold 28"))
    lay.set_text(":: CATAI-LINUX ::", -1)
    tw, th = lay.get_pixel_size()
    ctx.move_to(W // 2 - tw // 2, 40)
    PangoCairo.show_layout(ctx, lay)

    # Subtitle
    ctx.set_source_rgba(0.7, 0.6, 0.4, 0.8)
    lay2 = PangoCairo.create_layout(ctx)
    lay2.set_font_description(Pango.FontDescription("monospace 14"))
    lay2.set_text("Virtual desktop pet cats with AI chat", -1)
    tw2, _ = lay2.get_pixel_size()
    ctx.move_to(W // 2 - tw2 // 2, 82)
    PangoCairo.show_layout(ctx, lay2)

    # 4 cats with overlays, spread horizontally
    cats = [
        ("cat_orange", "sleeping-ball", "south", 3, draw_zzz, None),
        ("cat02",      "love",          "south", 4, draw_hearts, None),
        ("cat04",      "surprised",     "east",  2, draw_exclamation, None),
        ("cat05",      "grooming",      "south", 4, draw_sparkle, None),
    ]

    y = H // 2 - CAT_W // 2 + 40
    for i, (cat_id, anim, direction, frame_idx, overlay_fn, _) in enumerate(cats):
        x = int((i + 0.5) * W / 4 - CAT_W / 2)
        img = load_frame(cat_id, anim, direction, frame_idx)
        if img is None:
            img = load_rotation(cat_id)
        if img:
            s, _d = pil_to_surface(img, CAT_W, CAT_W)
            ctx.set_source_surface(s, x, y)
            ctx.paint()
        overlay_fn(ctx, x, y, CAT_W, CAT_W, phase=time.monotonic())

    # Caption at bottom
    ctx.set_source_rgba(0.6, 0.5, 0.4, 0.7)
    lay3 = PangoCairo.create_layout(ctx)
    lay3.set_font_description(Pango.FontDescription("monospace 13"))
    lay3.set_text("6 unique characters · 23 animations · multi-step sequences", -1)
    tw3, _ = lay3.get_pixel_size()
    ctx.move_to(W // 2 - tw3 // 2, H - 50)
    PangoCairo.show_layout(ctx, lay3)

    return surface


def render_scene2():
    """Cat with chat bubble open."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx = cairo.Context(surface)
    ctx.set_antialias(cairo.ANTIALIAS_NONE)

    draw_background(ctx, W, H)

    # Two background cats doing their own thing, slightly faded
    bg_cats = [
        ("cat02",  "flat",    "south", 2, 200, H // 2 + 100),
        ("cat04",  "rolling", "south", 3, W - 280, H // 2 + 150),
    ]
    for cat_id, anim, direction, frame_idx, bx, by in bg_cats:
        img = load_frame(cat_id, anim, direction, frame_idx)
        if img is None:
            img = load_rotation(cat_id)
        if img:
            s, _d = pil_to_surface(img, CAT_W, CAT_W)
            ctx.set_source_surface(s, bx, by)
            ctx.paint_with_alpha(0.7)

    # Foreground cat with chat bubble — cat_orange sitting
    cat_x = W // 2 - CAT_W // 2
    cat_y = H // 2 + 120
    img = load_frame("cat_orange", "flat", "south", 2)
    if img is None:
        img = load_rotation("cat_orange")
    s, _d = pil_to_surface(img, CAT_W, CAT_W)
    ctx.set_source_surface(s, cat_x, cat_y)
    ctx.paint()

    # Chat bubble above
    chat_text = "Purr... I'm Tangerine the orange cat! I love napping in the sunlight and chasing butterflies. What can I help you with today? \U0001f43e"
    draw_chat_bubble(ctx, chat_text, cat_x, cat_y, CAT_W)

    return surface


def render_scene_sequences():
    """Bonus scene: show a sequence in action (wall adventure frozen mid-way)."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx = cairo.Context(surface)
    ctx.set_antialias(cairo.ANTIALIAS_NONE)
    draw_background(ctx, W, H)

    # Title
    ctx.set_source_rgba(1, 0.85, 0.3, 0.9)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription("monospace bold 22"))
    lay.set_text(":: Animation sequences ::", -1)
    tw, _ = lay.get_pixel_size()
    ctx.move_to(W // 2 - tw // 2, 30)
    PangoCairo.show_layout(ctx, lay)

    # Show 3 mid-sequence frames
    scenes = [
        ("cat_orange", "dash",      "east",  4, draw_speed_lines, "east",  "Dashing"),
        ("cat02",      "die",       "south", 5, draw_skull,       None,    "Drama Queen"),
        ("cat05",      "wallclimb", "east",  5, None,             None,    "Wall Climb"),
    ]
    y = H // 2 - CAT_W // 2 + 30
    for i, (cat_id, anim, direction, frame_idx, overlay, extra, label) in enumerate(scenes):
        x = int((i + 0.5) * W / 3 - CAT_W / 2)
        img = load_frame(cat_id, anim, direction, frame_idx)
        if img is None:
            img = load_rotation(cat_id)
        if img:
            if overlay is draw_speed_lines:
                overlay(ctx, x, y, CAT_W, CAT_W, extra, phase=time.monotonic())
            s, _d = pil_to_surface(img, CAT_W, CAT_W)
            ctx.set_source_surface(s, x, y)
            ctx.paint()
            if overlay and overlay is not draw_speed_lines:
                overlay(ctx, x, y, CAT_W, CAT_W, phase=time.monotonic())

        # Label below
        ctx.set_source_rgba(0.8, 0.7, 0.4, 0.9)
        lay_l = PangoCairo.create_layout(ctx)
        lay_l.set_font_description(Pango.FontDescription("monospace bold 14"))
        lay_l.set_text(label, -1)
        lw, _ = lay_l.get_pixel_size()
        ctx.move_to(x + CAT_W // 2 - lw // 2, y + CAT_W + 14)
        PangoCairo.show_layout(ctx, lay_l)

    return surface


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    out1 = os.path.join(ROOT, "screenshot1.png")
    out2 = os.path.join(ROOT, "screenshot2.png")
    out3 = os.path.join(ROOT, "screenshot3.png")

    print("Rendering scene 1 (overlays showcase)...")
    s1 = render_scene1()
    s1.write_to_png(out1)
    print(f"  → {out1}")

    print("Rendering scene 2 (chat bubble)...")
    s2 = render_scene2()
    s2.write_to_png(out2)
    print(f"  → {out2}")

    print("Rendering scene 3 (sequences)...")
    s3 = render_scene_sequences()
    s3.write_to_png(out3)
    print(f"  → {out3}")

    print("\nDone!")


if __name__ == "__main__":
    main()
