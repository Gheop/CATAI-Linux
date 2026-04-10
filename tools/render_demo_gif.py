#!/usr/bin/env python3
"""Render an animated demo GIF for CATAI-Linux README.

Produces a sequence of frames showing multiple cats running various animations
and sequences, then assembles them into a GIF using ffmpeg (with palette
optimization).

Output: demo.gif (in repo root)

Usage: python3 tools/render_demo_gif.py
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile

# Reuse rendering helpers from sibling module
sys.path.insert(0, os.path.dirname(__file__))
from render_screenshots import (
    W, H, CAT_W, ROOT,
    load_frame, load_rotation, pil_to_surface,
    draw_background, draw_zzz, draw_hearts, draw_sparkle, draw_skull, draw_speed_lines,
    draw_meow_bubble, draw_chat_bubble,
)

import cairo
import gi
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo

FPS = 12
DURATION_S = 14
TOTAL_FRAMES = FPS * DURATION_S  # 168 frames

# ── Scene script ──────────────────────────────────────────────────────────────
# Each act: (start_frame, end_frame, caption, scene_fn)
# scene_fn receives (ctx, local_t_seconds, local_progress 0..1)

def scene_roaming(ctx, t, p):
    """4 cats roaming with various states showing off overlays."""
    # Cat 0: sleeping_ball (stationary)
    x0 = int(W * 0.12)
    y0 = int(H * 0.55)
    frames = ["cat_orange", "sleeping-ball", "south"]
    draw_cat(ctx, *frames, frame_of(t, 8, 4), x0, y0)
    draw_zzz(ctx, x0, y0, CAT_W, CAT_W, phase=t)

    # Cat 1: love
    x1 = int(W * 0.34)
    y1 = int(H * 0.55)
    draw_cat(ctx, "cat02", "love", "south", frame_of(t, 8, 8), x1, y1)
    draw_hearts(ctx, x1, y1, CAT_W, CAT_W, phase=t)

    # Cat 2: running east (moves across its local area)
    x2 = int(W * 0.5 + math.sin(t * 0.8) * 60)
    y2 = int(H * 0.55)
    draw_cat(ctx, "cat04", "running-8-frames", "east", frame_of(t, 8, 4), x2, y2)

    # Cat 3: grooming with sparkle
    x3 = int(W * 0.78)
    y3 = int(H * 0.55)
    draw_cat(ctx, "cat05", "grooming", "south", frame_of(t, 8, 8), x3, y3)
    draw_sparkle(ctx, x3, y3, CAT_W, CAT_W, phase=t)


def scene_dash(ctx, t, p):
    """Cat dashes across screen left to right with speed lines."""
    # Background cats idle
    draw_cat(ctx, "cat02", "flat", "south", 2, int(W * 0.15), int(H * 0.6))
    draw_cat(ctx, "cat04", "rolling", "south", frame_of(t, 8, 8), int(W * 0.85), int(H * 0.6))

    # Dashing cat moves fast
    dash_x = int(-CAT_W + (W + CAT_W * 2) * p)
    dash_y = int(H * 0.4)
    draw_speed_lines(ctx, dash_x, dash_y, CAT_W, CAT_W, "east", phase=t)
    draw_cat(ctx, "cat_orange", "dash", "east", frame_of(t, 12, 9), dash_x, dash_y)


def scene_drama(ctx, t, p):
    """Drama queen sequence: hurt → die (with skull) → wake up."""
    x = W // 2 - CAT_W // 2
    y = int(H * 0.55)

    # Background cats
    draw_cat(ctx, "cat_orange", "flat", "south", 2, int(W * 0.15), int(H * 0.6))
    draw_cat(ctx, "cat04", "love", "south", frame_of(t, 8, 8), int(W * 0.82), int(H * 0.6))

    # Main: phases
    if p < 0.15:  # hurt
        draw_cat(ctx, "cat02", "hurt", "south", int(p / 0.15 * 4) % 4, x, y)
    elif p < 0.7:  # dying with skull floating
        # Use a late dying frame for "lying on ground"
        lp = (p - 0.15) / 0.55
        draw_cat(ctx, "cat02", "die", "south", 7, x, y)
        draw_skull(ctx, x, y, CAT_W, CAT_W, phase=lp * 3)
    elif p < 0.88:  # hurt again (resurrection)
        lp = (p - 0.7) / 0.18
        draw_cat(ctx, "cat02", "hurt", "south", int(lp * 4) % 4, x, y)
    else:  # waking up
        lp = (p - 0.88) / 0.12
        fi = min(7, int(lp * 8))
        draw_cat(ctx, "cat02", "waking-getting-up", "south", fi, x, y)


def scene_ledge(ctx, t, p):
    """Ledge adventure: grab → idle → struggle → climb."""
    x = W // 2 - CAT_W // 2
    y = int(H * 0.55)
    # Background cats
    draw_cat(ctx, "cat02", "sleeping-ball", "south", frame_of(t, 8, 4), int(W * 0.15), int(H * 0.6))
    draw_zzz(ctx, int(W * 0.15), int(H * 0.6), CAT_W, CAT_W, phase=t)
    draw_cat(ctx, "cat05", "grooming", "south", frame_of(t, 8, 8), int(W * 0.85), int(H * 0.6))
    draw_sparkle(ctx, int(W * 0.85), int(H * 0.6), CAT_W, CAT_W, phase=t)

    if p < 0.2:
        draw_cat(ctx, "cat_orange", "ledgegrab", "east", int(p / 0.2 * 5) % 5, x, y)
    elif p < 0.5:
        lp = (p - 0.2) / 0.3
        draw_cat(ctx, "cat_orange", "ledgeidle", "east", int(lp * 16) % 8, x, y)
    elif p < 0.75:
        lp = (p - 0.5) / 0.25
        draw_cat(ctx, "cat_orange", "ledgeclimb-struggle", "east", int(lp * 12) % 12, x, y)
    else:
        lp = (p - 0.75) / 0.25
        draw_cat(ctx, "cat_orange", "climbing", "east", int(lp * 11) % 11, x, y)


def scene_meow(ctx, t, p):
    """Cat shows a meow bubble."""
    # 3 background cats
    draw_cat(ctx, "cat02", "love", "south", frame_of(t, 8, 8), int(W * 0.15), int(H * 0.6))
    draw_hearts(ctx, int(W * 0.15), int(H * 0.6), CAT_W, CAT_W, phase=t)
    draw_cat(ctx, "cat04", "grooming", "south", frame_of(t, 8, 8), int(W * 0.82), int(H * 0.6))
    draw_sparkle(ctx, int(W * 0.82), int(H * 0.6), CAT_W, CAT_W, phase=t)

    # Main cat with bouncing meow
    x = W // 2 - CAT_W // 2
    y = int(H * 0.55)
    draw_cat(ctx, "cat_orange", "flat", "south", 2, x, y)

    # Bounce bubble
    bob = math.sin(t * 5) * 4
    texts = ["Meow~", "Purr...", "Mrrp! \U0001f63a", "Miaou~"]
    idx = int(p * len(texts)) % len(texts)
    draw_meow_bubble_at(ctx, texts[idx], x, int(y + bob), CAT_W)


def scene_chat(ctx, t, p):
    """Cat with AI chat bubble showing streaming text."""
    # Background cats
    draw_cat(ctx, "cat02", "flat", "south", 2, int(W * 0.12), int(H * 0.65))
    draw_cat(ctx, "cat04", "rolling", "south", frame_of(t, 8, 8), int(W * 0.85), int(H * 0.65))

    # Main cat
    x = W // 2 - CAT_W // 2
    y = int(H * 0.6)
    draw_cat(ctx, "cat_orange", "flat", "south", 2, x, y)

    # Streaming text effect
    full_text = "Purr... I'm Tangerine! I love napping in sunbeams and chasing laser dots. What's on your mind today? \U0001f43e"
    n_chars = int(p * len(full_text) * 1.4)
    visible = full_text[:n_chars] if n_chars > 0 else " "
    # Add blinking cursor
    if int(t * 3) % 2 == 0 and n_chars < len(full_text):
        visible += "|"
    draw_chat_bubble(ctx, visible, x, y, CAT_W)


# ── Helpers ──────────────────────────────────────────────────────────────────

def draw_cat(ctx, cat_id, anim, direction, frame_idx, x, y):
    img = load_frame(cat_id, anim, direction, frame_idx)
    if img is None:
        img = load_rotation(cat_id)
    if img:
        s, _d = pil_to_surface(img, CAT_W, CAT_W)
        ctx.set_source_surface(s, x, y)
        ctx.paint()


def frame_of(t, fps, n_frames):
    """Return a frame index given the global time `t`, animation fps, and total frames."""
    return int(t * fps) % n_frames


def draw_meow_bubble_at(ctx, text, x, y, cat_w):
    draw_meow_bubble(ctx, text, x, y, cat_w)


def draw_caption(ctx, text):
    ctx.set_source_rgba(1, 0.85, 0.3, 0.95)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription("monospace bold 22"))
    lay.set_text(text, -1)
    tw, _ = lay.get_pixel_size()
    ctx.move_to(W // 2 - tw // 2, 30)
    PangoCairo.show_layout(ctx, lay)


def draw_title(ctx):
    ctx.set_source_rgba(1, 0.85, 0.3, 0.4)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription("monospace bold 14"))
    lay.set_text(":: CATAI-LINUX ::", -1)
    tw, _ = lay.get_pixel_size()
    ctx.move_to(W - tw - 20, H - 30)
    PangoCairo.show_layout(ctx, lay)


# ── Act list ─────────────────────────────────────────────────────────────────

ACTS = [
    (0,   24,  "Meet the cats",         scene_roaming),  # 2s
    (24,  48,  "Dashing with style",    scene_dash),      # 2s
    (48,  96,  "Drama queen mode",      scene_drama),     # 4s
    (96,  132, "Ledge adventure",       scene_ledge),     # 3s
    (132, 150, "Random meows",          scene_meow),      # 1.5s
    (150, 168, "AI chat",               scene_chat),      # 1.5s
]


def render_frame(global_frame):
    """Render a single frame and return a cairo ImageSurface."""
    t = global_frame / FPS  # seconds since start
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx = cairo.Context(surface)
    ctx.set_antialias(cairo.ANTIALIAS_NONE)
    draw_background(ctx, W, H)

    # Find current act
    current = None
    for start, end, caption, fn in ACTS:
        if start <= global_frame < end:
            current = (start, end, caption, fn)
            break
    if current is None:
        current = ACTS[-1]

    start, end, caption, fn = current
    local_frames = end - start
    local_frame = global_frame - start
    local_progress = local_frame / max(1, local_frames - 1)

    # Use global t so animation loops feel continuous
    fn(ctx, t, local_progress)

    draw_caption(ctx, caption)
    draw_title(ctx)

    return surface


def main():
    tmpdir = tempfile.mkdtemp(prefix="catai_demo_")
    print(f"Rendering {TOTAL_FRAMES} frames to {tmpdir}...")

    for gf in range(TOTAL_FRAMES):
        surface = render_frame(gf)
        surface.write_to_png(os.path.join(tmpdir, f"frame_{gf:04d}.png"))
        if gf % 20 == 0:
            print(f"  frame {gf}/{TOTAL_FRAMES}")

    # Assemble with ffmpeg using palette optimization (high quality)
    print("\nAssembling GIF with ffmpeg...")
    palette = os.path.join(tmpdir, "palette.png")
    out_gif = os.path.join(ROOT, "demo.gif")

    # Generate palette
    subprocess.run([
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", os.path.join(tmpdir, "frame_%04d.png"),
        "-vf", "palettegen=max_colors=128:stats_mode=diff",
        palette,
    ], check=True, capture_output=True)

    # Apply palette
    subprocess.run([
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", os.path.join(tmpdir, "frame_%04d.png"),
        "-i", palette,
        "-lavfi", f"fps={FPS}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5",
        "-loop", "0",
        out_gif,
    ], check=True, capture_output=True)

    size_kb = os.path.getsize(out_gif) // 1024
    print(f"\n✓ {out_gif} ({size_kb} KB, {TOTAL_FRAMES} frames @ {FPS}fps, {DURATION_S}s)")

    shutil.rmtree(tmpdir)


if __name__ == "__main__":
    main()
