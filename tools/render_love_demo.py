#!/usr/bin/env python3
"""Render an animated GIF demonstrating the love encounter + birth feature.

Scenes:
    1. "Two cats meet"                    — two cats face each other
    2. "Cat A falls in love"              — cat A shows LOVE + hearts
    3. "Cat B reacts in 3 possible ways"  — 3 mini-panels: angry / surprised / love
    4. "If both in love... a miracle!"    — focus on love→love, kitten appears with sparkles
    5. "A new kitten is born!"            — fully grown kitten between parents

Output: love_demo.gif (in repo root)

Usage: python3 tools/render_love_demo.py
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from render_screenshots import (
    W, H, CAT_W, ROOT,
    load_frame, load_rotation, pil_to_surface,
    draw_background, draw_hearts, draw_exclamation, draw_sparkle,
    _pango_sym,
)

import cairo
import gi
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo

FPS = 12
DURATION_S = 13
TOTAL_FRAMES = FPS * DURATION_S  # 156 frames

KITTEN_W = int(CAT_W * 0.8)  # kittens are 80% size of cats

# ── Helpers ──────────────────────────────────────────────────────────────────

def draw_cat(ctx, cat_id, anim, direction, frame_idx, x, y, w=CAT_W):
    img = load_frame(cat_id, anim, direction, frame_idx)
    if img is None:
        img = load_rotation(cat_id)
    if img:
        s, _d = pil_to_surface(img, w, w)
        ctx.set_source_surface(s, x, y)
        ctx.paint()


def draw_cat_fade_scale(ctx, cat_id, anim, direction, frame_idx, x, y, w, scale, alpha):
    """Draw a cat with scale and alpha (for birth animation)."""
    img = load_frame(cat_id, anim, direction, frame_idx)
    if img is None:
        img = load_rotation(cat_id)
    if img is None:
        return
    scaled_w = int(w * scale)
    if scaled_w < 1:
        return
    s, _d = pil_to_surface(img, scaled_w, scaled_w)
    cx = x + w / 2
    cy = y + w / 2
    ox = cx - scaled_w / 2
    oy = cy - scaled_w / 2
    ctx.set_source_surface(s, ox, oy)
    ctx.paint_with_alpha(alpha)


def frame_of(t, fps, n_frames):
    return int(t * fps) % n_frames


def draw_angry_overlay(ctx, x, y, w, h, phase):
    """Shaking anger symbol above an angry cat."""
    shake = math.sin(phase * 14) * 3
    _pango_sym(ctx, "\U0001f4a2", x + w // 2 + shake, y + h * 0.25, 16, 0.85, 0.15, 0.1, 1.0)


def draw_birth_sparkles(ctx, cx, cy, size, progress):
    """6 sparkles rotating around (cx, cy), fading as progress goes to 1."""
    import time as _t
    t = _t.monotonic()
    radius = size * 0.6
    base_alpha = 1.0 - progress * 0.5
    for i in range(6):
        angle = t * 2.5 + i * (math.pi / 3)
        sx = cx + math.cos(angle) * radius
        sy = cy + math.sin(angle) * radius * 0.7
        twinkle = 0.5 + 0.5 * math.sin(t * 4 + i)
        alpha = base_alpha * twinkle
        sz = 12 + int(twinkle * 5)
        _pango_sym(ctx, "\u2728", sx - sz / 2, sy - sz / 2, sz, 1.0, 0.95, 0.5, alpha)


def draw_caption(ctx, text, color=(1, 0.85, 0.3, 0.95)):
    ctx.set_source_rgba(*color)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription("monospace bold 24"))
    lay.set_text(text, -1)
    tw, _ = lay.get_pixel_size()
    ctx.move_to(W // 2 - tw // 2, 40)
    PangoCairo.show_layout(ctx, lay)


def draw_subcaption(ctx, text, y, color=(0.7, 0.85, 0.95, 0.9)):
    ctx.set_source_rgba(*color)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription("monospace bold 14"))
    lay.set_text(text, -1)
    tw, _ = lay.get_pixel_size()
    ctx.move_to(W // 2 - tw // 2, y)
    PangoCairo.show_layout(ctx, lay)


def draw_panel_label(ctx, text, x, y, width, color=(0.9, 0.75, 0.4, 0.95)):
    ctx.set_source_rgba(*color)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription("monospace bold 15"))
    lay.set_text(text, -1)
    tw, _ = lay.get_pixel_size()
    ctx.move_to(x + width // 2 - tw // 2, y)
    PangoCairo.show_layout(ctx, lay)


def draw_title(ctx):
    ctx.set_source_rgba(1, 0.85, 0.3, 0.4)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription("monospace bold 14"))
    lay.set_text(":: CATAI-LINUX — LOVE ENCOUNTERS ::", -1)
    tw, _ = lay.get_pixel_size()
    ctx.move_to(W - tw - 20, H - 30)
    PangoCairo.show_layout(ctx, lay)


# ── Scenes ───────────────────────────────────────────────────────────────────
# PARENT_A = cat_orange, PARENT_B = cat02 (dark) — contrasting visuals
# If both in love → kitten = kitten_orange (orange parent wins the demo)

P_A = "cat_orange"
P_B = "cat02"
KIT = "kitten_orange"

# Positions for the "meeting" scene
CENTER_Y = int(H * 0.55)
POS_A_X = int(W * 0.35 - CAT_W / 2)
POS_B_X = int(W * 0.65 - CAT_W / 2)


def scene_meet(ctx, t, p):
    """Two cats approach each other and face off."""
    # Start apart, end close (reverse: start close, end close — just facing off)
    dx_a = int((1 - p) * -40)
    dx_b = int((1 - p) * 40)
    draw_cat(ctx, P_A, "running-8-frames", "east", frame_of(t, 8, 4),
             POS_A_X + dx_a, CENTER_Y)
    draw_cat(ctx, P_B, "running-8-frames", "west", frame_of(t, 8, 4),
             POS_B_X + dx_b, CENTER_Y)
    draw_caption(ctx, "Two cats cross paths...")


def scene_love_start(ctx, t, p):
    """Cat A falls in love, cat B still idle facing."""
    draw_cat(ctx, P_A, "love", "south", frame_of(t, 8, 8), POS_A_X, CENTER_Y)
    draw_hearts(ctx, POS_A_X, CENTER_Y, CAT_W, CAT_W, phase=t)

    draw_cat(ctx, P_B, "flat", "south", 2, POS_B_X, CENTER_Y)
    draw_caption(ctx, "One falls in love \u2665")


def scene_three_reactions(ctx, t, p):
    """3 panels side-by-side showing the 3 possible reactions."""
    # Draw dividers
    ctx.set_source_rgba(1, 1, 1, 0.08)
    ctx.set_line_width(1)
    ctx.move_to(W / 3, 120); ctx.line_to(W / 3, H - 100); ctx.stroke()
    ctx.move_to(2 * W / 3, 120); ctx.line_to(2 * W / 3, H - 100); ctx.stroke()

    # Each panel: parent A (love) + parent B (one of 3 states)
    panel_y = int(H * 0.55)

    # Panel 1: ANGRY (30%)
    p1_ax = int(W / 6 - CAT_W * 0.9)
    p1_bx = int(W / 6 + CAT_W * 0.1)
    draw_cat(ctx, P_A, "love", "south", frame_of(t, 8, 8), p1_ax, panel_y)
    draw_hearts(ctx, p1_ax, panel_y, CAT_W, CAT_W, phase=t)
    draw_cat(ctx, P_B, "angry", "south", frame_of(t, 8, 7), p1_bx, panel_y)
    draw_angry_overlay(ctx, p1_bx, panel_y, CAT_W, CAT_W, phase=t)
    draw_panel_label(ctx, "ANGRY \U0001f4a2   30%", 0, H - 80, W // 3)

    # Panel 2: SURPRISED (30%)
    p2_ax = int(W / 2 - CAT_W * 0.9)
    p2_bx = int(W / 2 + CAT_W * 0.1)
    draw_cat(ctx, P_A, "love", "south", frame_of(t, 8, 8), p2_ax, panel_y)
    draw_hearts(ctx, p2_ax, panel_y, CAT_W, CAT_W, phase=t)
    draw_cat(ctx, P_B, "surprised", "east", frame_of(t, 8, 8), p2_bx, panel_y)
    draw_exclamation(ctx, p2_bx, panel_y, CAT_W, CAT_W)
    draw_panel_label(ctx, "SURPRISED !!!   30%", W // 3, H - 80, W // 3)

    # Panel 3: LOVE (40%) — highlighted
    p3_ax = int(5 * W / 6 - CAT_W * 0.9)
    p3_bx = int(5 * W / 6 + CAT_W * 0.1)
    draw_cat(ctx, P_A, "love", "south", frame_of(t, 8, 8), p3_ax, panel_y)
    draw_hearts(ctx, p3_ax, panel_y, CAT_W, CAT_W, phase=t)
    draw_cat(ctx, P_B, "love", "south", frame_of(t, 8, 8), p3_bx, panel_y)
    draw_hearts(ctx, p3_bx, panel_y, CAT_W, CAT_W, phase=t)
    draw_panel_label(ctx, "LOVE \u2665   40%", 2 * W // 3, H - 80, W // 3,
                     color=(1, 0.4, 0.5, 1.0))

    draw_caption(ctx, "The other may react 3 ways...")


def scene_birth(ctx, t, p):
    """Both in love — kitten materializes between them with sparkles."""
    # Parents in LOVE, facing each other
    draw_cat(ctx, P_A, "love", "south", frame_of(t, 8, 8), POS_A_X, CENTER_Y)
    draw_hearts(ctx, POS_A_X, CENTER_Y, CAT_W, CAT_W, phase=t)
    draw_cat(ctx, P_B, "love", "south", frame_of(t, 8, 8), POS_B_X, CENTER_Y)
    draw_hearts(ctx, POS_B_X, CENTER_Y, CAT_W, CAT_W, phase=t)

    # Kitten position (midpoint, slightly below)
    kit_x = (POS_A_X + POS_B_X) / 2 + (CAT_W - KITTEN_W) / 2
    kit_y = CENTER_Y + 28

    # Birth progress: fade + grow from 10% to 100%
    scale = 0.1 + 0.9 * p
    alpha = 0.15 + 0.85 * p
    draw_cat_fade_scale(ctx, KIT, "grooming", "south", frame_of(t, 8, 8),
                        kit_x, kit_y, KITTEN_W, scale, alpha)

    # Sparkles around the kitten throughout the birth
    draw_birth_sparkles(ctx, kit_x + KITTEN_W / 2, kit_y + KITTEN_W / 2, KITTEN_W, p)

    draw_caption(ctx, "When both fall... \u2728 a miracle! \u2728")


def scene_done(ctx, t, p):
    """Final scene: kitten fully grown, bouncing happily between parents."""
    draw_cat(ctx, P_A, "flat", "south", 2, POS_A_X, CENTER_Y)
    draw_cat(ctx, P_B, "flat", "south", 2, POS_B_X, CENTER_Y)

    kit_x = (POS_A_X + POS_B_X) / 2 + (CAT_W - KITTEN_W) / 2
    kit_y = CENTER_Y + 28 + int(math.sin(t * 6) * 3)  # gentle bounce

    draw_cat(ctx, KIT, "love", "south", frame_of(t, 8, 8), kit_x, kit_y, KITTEN_W)
    # Hearts above kitten
    draw_hearts(ctx, kit_x, kit_y, KITTEN_W, KITTEN_W, phase=t)

    draw_caption(ctx, "A new kitten is born! \U0001f43e")


# ── Act list ─────────────────────────────────────────────────────────────────

ACTS = [
    (0,   24,  scene_meet),            # 2s
    (24,  48,  scene_love_start),      # 2s
    (48,  96,  scene_three_reactions), # 4s
    (96,  132, scene_birth),           # 3s
    (132, 156, scene_done),            # 2s
]


def render_frame(global_frame):
    t = global_frame / FPS
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx = cairo.Context(surface)
    ctx.set_antialias(cairo.ANTIALIAS_NONE)
    draw_background(ctx, W, H)

    current = None
    for start, end, fn in ACTS:
        if start <= global_frame < end:
            current = (start, end, fn)
            break
    if current is None:
        current = ACTS[-1]

    start, end, fn = current
    local_frame = global_frame - start
    local_progress = local_frame / max(1, end - start - 1)
    fn(ctx, t, local_progress)

    draw_title(ctx)
    return surface


def main():
    tmpdir = tempfile.mkdtemp(prefix="catai_love_")
    print(f"Rendering {TOTAL_FRAMES} frames to {tmpdir}...")

    for gf in range(TOTAL_FRAMES):
        surface = render_frame(gf)
        surface.write_to_png(os.path.join(tmpdir, f"frame_{gf:04d}.png"))
        if gf % 20 == 0:
            print(f"  frame {gf}/{TOTAL_FRAMES}")

    # Assemble with ffmpeg palette optimization
    print("\nAssembling GIF with ffmpeg...")
    palette = os.path.join(tmpdir, "palette.png")
    out_gif = os.path.join(ROOT, "love_demo.gif")

    subprocess.run([
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", os.path.join(tmpdir, "frame_%04d.png"),
        "-vf", "palettegen=max_colors=128:stats_mode=diff",
        palette,
    ], check=True, capture_output=True)

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
    print(f"\n\u2713 {out_gif} ({size_kb} KB, {TOTAL_FRAMES} frames @ {FPS}fps, {DURATION_S}s)")

    shutil.rmtree(tmpdir)


if __name__ == "__main__":
    main()
