#!/usr/bin/env python3
"""Generate new cat animation sprites for CATAI-Linux.
Strategy: use existing rotation sprites as pixel-perfect bases,
add props/effects on top or erase+redraw only specific body parts.
Run from project root: python3 scripts/generate_sprites.py
"""
import os
import math
from PIL import Image, ImageDraw

ROOT = os.path.join(os.path.dirname(__file__), "..")
SPRITES = os.path.join(ROOT, "catai_linux", "cute_orange_cat")
ANIM_DIR = os.path.join(SPRITES, "animations")
ROT_DIR  = os.path.join(SPRITES, "rotations")

# ── Palette (sampled from existing sprites) ───────────────────────────────────
OR   = (232, 175,  20, 255)   # main orange
OR_D = (120,  60,  12, 255)   # dark orange shadow
OR_L = (248, 210,  90, 255)   # light orange
BK   = ( 30,  23,   9, 255)   # near-black
PK   = (241, 127, 121, 255)   # pink
GY   = (165, 162, 158, 255)   # grey (mouse)
GY_D = ( 95,  92,  88, 255)   # dark grey
RD   = (205,  42,  32, 255)   # red (ball)
RD_D = (145,  18,  12, 255)   # dark red
YL   = (255, 215,  20, 255)   # yellow (pee)
BR   = (115,  68,  28, 255)   # brown (tree bark, poop)
BR_D = ( 68,  38,  10, 255)   # dark brown
GR   = ( 65, 145,  42, 255)   # green (leaves)
GR_D = ( 42,  98,  24, 255)   # dark green
BU   = ( 90,  75, 195, 255)   # blue-purple (butterfly)
BU_L = (160, 145, 255, 255)   # light butterfly
EMPTY = (0, 0, 0, 0)
W, H = 68, 68


def load_rot(direction):
    return Image.open(os.path.join(ROT_DIR, f"{direction}.png")).convert("RGBA")


def load_anim_frame(anim, direction, idx):
    path = os.path.join(ANIM_DIR, anim, direction, f"frame_{idx:03d}.png")
    return Image.open(path).convert("RGBA")


def new_frame():
    return Image.new("RGBA", (W, H), EMPTY)


def erase_region(img, x0, y0, x1, y1):
    """Make a rectangular region fully transparent."""
    for y in range(max(0, y0), min(H, y1 + 1)):
        for x in range(max(0, x0), min(W, x1 + 1)):
            if img.getpixel((x, y))[3] > 0:
                img.putpixel((x, y), EMPTY)


def draw_ellipse(img, cx, cy, rx, ry, color):
    d = ImageDraw.Draw(img)
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=color)


def draw_line(img, x0, y0, x1, y1, color, width=2):
    d = ImageDraw.Draw(img)
    d.line([x0, y0, x1, y1], fill=color, width=width)


def draw_rect(img, x0, y0, x1, y1, color):
    d = ImageDraw.Draw(img)
    d.rectangle([x0, y0, x1, y1], fill=color)


def save_frames(frames, anim_name, direction="south"):
    out = os.path.join(ANIM_DIR, anim_name, direction)
    os.makedirs(out, exist_ok=True)
    for i, f in enumerate(frames):
        f.save(os.path.join(out, f"frame_{i:03d}.png"))
    print(f"  ✓ {anim_name}/{direction}: {len(frames)} frames")


def mirror_west(east_frames, anim_name):
    west = [f.transpose(Image.FLIP_LEFT_RIGHT) for f in east_frames]
    save_frames(west, anim_name, "west")


# ── Props ─────────────────────────────────────────────────────────────────────

def draw_mouse(img, mx, my):
    """Tiny grey mouse facing right."""
    draw_ellipse(img, mx,   my,   5, 4, GY)        # body
    draw_ellipse(img, mx+5, my-2, 4, 4, GY)        # head
    img.putpixel((mx+8, my-3), BK)                 # eye
    img.putpixel((mx+10, my-1), PK)                # nose
    draw_ellipse(img, mx+3, my-5, 2, 2, GY)        # ear1
    draw_ellipse(img, mx+6, my-6, 2, 2, GY)        # ear2
    draw_ellipse(img, mx+3, my-5, 1, 1, PK)        # inner ear
    draw_ellipse(img, mx+6, my-6, 1, 1, PK)
    d = ImageDraw.Draw(img)
    d.arc([mx-9, my-2, mx-1, my+6], start=0, end=160, fill=GY_D, width=2)  # tail


def draw_yarn_ball(img, bx, by, r=7):
    """Red ball of yarn."""
    draw_ellipse(img, bx, by, r, r, RD)
    draw_ellipse(img, bx-2, by-2, r-2, r-2, (220, 55, 42, 200))
    d = ImageDraw.Draw(img)
    d.arc([bx-r+1, by-r+1, bx+r-1, by+r-1], start=20,  end=160, fill=RD_D, width=1)
    d.arc([bx-r+3, by-r+3, bx+r-3, by+r-3], start=200, end=340, fill=(255, 90, 70, 180), width=1)
    d.arc([bx-r+2, by-r+2, bx+r-2, by+r-2], start=80,  end=260, fill=RD_D, width=1)


def draw_butterfly(img, bx, by, flap=0):
    """Small butterfly. flap=0..3 for wing animation."""
    wy = 3 - abs(flap - 1)  # wing height offset
    d = ImageDraw.Draw(img)
    d.ellipse([bx-7, by-wy-3, bx-1, by+wy+1], fill=BU_L)   # left upper wing
    d.ellipse([bx+1, by-wy-3, bx+7, by+wy+1], fill=BU)     # right upper wing
    d.ellipse([bx-6, by+1,    bx,   by+wy+4], fill=BU)      # left lower wing
    d.ellipse([bx,   by+1,    bx+6, by+wy+4], fill=BU_L)    # right lower wing
    draw_ellipse(img, bx, by, 1, 3, BK)                      # body
    img.putpixel((bx-1, by-4), BK)                           # antennae
    img.putpixel((bx+1, by-5), BK)


def draw_tree_bg(img, tx):
    """Tree trunk — background portion (behind cat)."""
    draw_rect(img, tx,   15, tx+9,  50, BR_D)
    draw_rect(img, tx+1, 15, tx+8,  50, BR)
    draw_rect(img, tx+3, 15, tx+6,  50, BR_D)
    d = ImageDraw.Draw(img)
    for y in range(20, 50, 8):
        d.line([tx+1, y, tx+2, y+2], fill=BR_D, width=1)
        d.line([tx+6, y+3, tx+7, y+5], fill=BR_D, width=1)
    draw_ellipse(img, tx+5,  10, 13, 10, GR)
    draw_ellipse(img, tx-2,  14,  9,  8, GR_D)
    draw_ellipse(img, tx+12, 12,  8,  7, GR)


def draw_tree_fg(img, tx):
    """Tree trunk — foreground portion (in front of cat legs).
    Starts at y=42 so the lower trunk is visible in front of the cat's body."""
    draw_rect(img, tx,   42, tx+9,  67, BR_D)
    draw_rect(img, tx+1, 42, tx+8,  67, BR)
    draw_rect(img, tx+3, 42, tx+6,  67, BR_D)
    d = ImageDraw.Draw(img)
    d.line([tx+1, 44, tx+2, 46], fill=BR_D, width=1)
    d.line([tx+6, 50, tx+7, 52], fill=BR_D, width=1)
    d.line([tx+1, 56, tx+2, 58], fill=BR_D, width=1)
    d.line([tx+6, 62, tx+7, 64], fill=BR_D, width=1)


# ── CHASING MOUSE (6 frames, east + west) ────────────────────────────────────

def gen_chasing_mouse():
    print("Generating: chasing-mouse")
    base_e = load_rot("east")
    frames = []
    for i in range(6):
        tilt = -8 - (i % 2) * 3   # lean forward
        frame = new_frame()
        tilted = base_e.rotate(tilt, resample=Image.BICUBIC,
                                center=(44, 34), expand=False)
        frame.paste(tilted, (0, 0), tilted)
        mx = 48 + (i % 2) * 2
        draw_mouse(frame, mx, 40)
        if i % 2 == 0:
            for dx, dy in [(2, 2), (4, 4), (6, 3)]:
                draw_ellipse(frame, mx - 4 + dx, 36 + dy, 1, 1,
                             (180, 120, 10, 160))
        frames.append(frame)
    save_frames(frames, "chasing-mouse", "east")
    mirror_west(frames, "chasing-mouse")


# ── PLAYING BALL (8 frames, south) — angry/south base, ball drawn at canvas level
# The red ball is NOT baked into the PNG — it is drawn in _canvas_draw so it
# keeps its colour regardless of the cat tint (grey cat, etc.).

def gen_playing_ball():
    print("Generating: playing-ball")
    frames = []
    for i in range(8):
        base = load_anim_frame("angry", "south", i)
        frame = new_frame()
        frame.paste(base, (0, 0), base)
        frames.append(frame)
    save_frames(frames, "playing-ball")


# ── BUTTERFLY (8 frames, south) ───────────────────────────────────────────────
# The butterfly itself is NOT baked into the PNG — drawn at canvas level so it
# keeps its blue colour regardless of the cat tint.

def gen_butterfly():
    print("Generating: butterfly")
    base = load_rot("south")
    arm_lifts  = [0, 3, 7, 11, 11, 7, 3, 0]
    body_rises = [0, 1, 2,  3,  3, 2, 1, 0]
    frames = []
    for i, (lift, rise) in enumerate(zip(arm_lifts, body_rises)):
        frame = new_frame()
        if rise > 0:
            shifted = Image.new("RGBA", (W, H), EMPTY)
            shifted.paste(base, (0, -rise), base)
            frame.paste(shifted, (0, 0), shifted)
        else:
            frame.paste(base, (0, 0), base)
        # Just the raised paw — no butterfly drawn here
        draw_line(frame, 41, 36 - rise, 44 - lift//3, 35 - lift - rise,
                  OR, width=4)
        draw_ellipse(frame, 44 - lift//3, 34 - lift - rise, 4, 3, OR)
        frames.append(frame)
    save_frames(frames, "butterfly")


# ── SCRATCHING TREE (6 frames, east + west) ───────────────────────────────────

def gen_scratching_tree():
    print("Generating: scratching-tree")
    base = load_rot("east")
    tx = 47   # tree x position (right edge of cat)
    # Paw alternates between high and low scratch position
    paw_ys = [28, 22, 28, 22, 28, 22]
    frames = []
    for i, py in enumerate(paw_ys):
        frame = new_frame()

        # Cat body only — tree and scratch marks drawn at Cairo canvas level
        frame.paste(base, (0, 0), base)

        # Front paw raised against tree — erase existing front leg area
        erase_region(frame, 38, 38, 54, 60)
        draw_line(frame, 41, 44, 44, py, OR, width=5)
        draw_ellipse(frame, 44, py, 5, 3, OR)
        draw_ellipse(frame, 44, py + 3, 3, 2, OR_D)

        frames.append(frame)
    save_frames(frames, "scratching-tree", "east")
    mirror_west(frames, "scratching-tree")


# ── PEEING (6 frames, east + west) ───────────────────────────────────────────

def gen_peeing():
    print("Generating: peeing")
    base = load_rot("east")
    # In east view: cat faces right, rear/hind legs are on the LEFT (x≈10-25)
    # Hind leg raise: starts at hip (≈15, 46), swings outward-left and UP
    leg_raises = [0, 8, 18, 28, 28, 28]
    frames = []
    for i, raise_h in enumerate(leg_raises):
        frame = new_frame()
        frame.paste(base, (0, 0), base)

        if raise_h > 0:
            # Erase the hind leg region (left side of cat, lower half)
            erase_region(frame, 7, 38, 26, 62)

            # Draw raised hind leg — swings to the left and UP
            hip_x, hip_y = 17, 46          # hip joint
            paw_x = hip_x - raise_h // 3   # moves left as it raises
            paw_y = hip_y - raise_h         # moves up

            # Thigh segment (hip → knee)
            knee_x = hip_x - raise_h // 5
            knee_y = hip_y - raise_h // 2
            draw_line(frame, hip_x, hip_y, knee_x, knee_y, OR_D, width=6)
            draw_line(frame, hip_x - 1, hip_y, knee_x - 1, knee_y, OR, width=4)

            # Shin segment (knee → paw)
            draw_line(frame, knee_x, knee_y, paw_x, paw_y, OR, width=5)
            draw_ellipse(frame, paw_x, paw_y, 4, 3, OR)
            draw_ellipse(frame, paw_x, paw_y + 2, 2, 1, OR_D)

            # Yellow drops NOT baked here — drawn at canvas level to survive tinting

        frames.append(frame)
    save_frames(frames, "peeing", "east")
    mirror_west(frames, "peeing")


# ── POOPING (6 frames, south — seen from behind using north sprite) ───────────

def gen_pooping():
    print("Generating: pooping")
    # north.png shows the cat from behind — perfect for pooping
    base = load_rot("north")
    # Slight squat: lower the whole cat 2-3px in squatted frames
    squats = [0, 1, 3, 3, 3, 3]
    frames = []
    for i, squat in enumerate(squats):
        frame = new_frame()
        if squat == 0:
            frame.paste(base, (0, 0), base)
        else:
            # Shift cat down slightly (squat effect) — paste with Y offset
            frame.paste(base, (0, squat), base)

        # Brown drops NOT baked here — drawn at canvas level to survive tinting

        frames.append(frame)
    save_frames(frames, "pooping")


# ── Run all ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Generating new cat animations (sprite-based)...\n")
    gen_chasing_mouse()
    gen_playing_ball()
    gen_butterfly()
    gen_scratching_tree()
    gen_peeing()
    gen_pooping()
    print("\nAll done!")
