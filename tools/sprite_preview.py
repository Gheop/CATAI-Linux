#!/usr/bin/env python3
"""CATAI Sprite Preview — renders all cats with animations + overlays like the real app.

Usage: python3 tools/sprite_preview.py
"""
import math
import os
import sys
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Gtk, Pango, PangoCairo
import cairo
from PIL import Image

# ── Config ────────────────────────────────────────────────────────────────────

PKG_DIR = os.path.join(os.path.dirname(__file__), "..", "catai_linux")

CATS = [
    ("cat_orange", "\U0001f7e0 Orange"),
    ("cat01",      "\U0001f7e4 Tabby"),
    ("cat02",      "\u2b1b Dark"),
    ("cat03",      "\U0001f7eb Brown"),
    ("cat04",      "\U0001faa6 Grey"),
    ("cat05",      "\U0001f5a4 Black"),
]

# Animations to showcase per cat, with overlay info
# (anim_name, direction, label, overlay_func_name_or_None)
SHOWCASE = [
    ("grooming",         "south", "Grooming",    "_draw_grooming"),
    ("flat",             "south", "Flat",        None),
    ("love",             "south", "Love",        "_draw_love"),
    ("sleeping-ball",    "south", "Sleep",       "_draw_zzz"),
    ("angry",            "south", "Angry",       "_draw_angry"),
    ("surprised",        "east",  "Surprised",   "_draw_surprised"),
    ("running-8-frames", "east",  "Run",         None),
    ("dash",             "east",  "Dash",        "_draw_dash"),
    ("chasing-mouse",    "east",  "Chase",       None),
    ("jumping",          "south", "Jump",        None),
    ("climbing",         "east",  "Climb",       None),
    ("wallclimb",        "east",  "WallClimb",   None),
    ("wallgrab",         "east",  "WallGrab",    None),
    ("ledgegrab",        "east",  "LedgeGrab",   None),
    ("ledgeidle",        "east",  "LedgeIdle",   None),
    ("ledgeclimb-struggle", "east", "Struggle",  None),
    ("hurt",             "south", "Hurt",        "_draw_hurt"),
    ("die",              "south", "Die",         "_draw_die"),
    ("fall",             "south", "Fall",        None),
    ("land",             "south", "Land",        None),
    ("eating",           "south", "Eat",         None),
    ("rolling",          "south", "Roll",        None),
    ("waking-getting-up","south", "Wake",        None),
]

SPRITE_SIZE = 80
SCALE = 1  # 1:1 — sprites are already 80×80
CELL_W = 90
CELL_H = 110
LABEL_H = 14
FPS = 8
BUBBLE_FONT = "monospace bold 11"

# ── Sprite loading ────────────────────────────────────────────────────────────

def load_frames(cat_id, anim_name, direction):
    """Load animation frames as PIL RGBA images."""
    anim_dir = os.path.join(PKG_DIR, cat_id, "animations", anim_name, direction)
    if not os.path.isdir(anim_dir):
        return []
    frames = []
    for f in sorted(os.listdir(anim_dir)):
        if f.endswith(".png"):
            img = Image.open(os.path.join(anim_dir, f)).convert("RGBA")
            frames.append(img)
    return frames


def pil_to_surface(img):
    """Convert PIL RGBA image to cairo.ImageSurface."""
    w, h = img.size
    data = bytearray(img.tobytes())
    for i in range(0, len(data), 4):
        data[i], data[i+2] = data[i+2], data[i]  # RGBA → BGRA
    surface = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_ARGB32, w, h, w * 4)
    return surface, data  # data must stay alive


def load_idle(cat_id):
    """Load idle rotation (south) as a single frame."""
    p = os.path.join(PKG_DIR, cat_id, "rotations", "south.png")
    if os.path.exists(p):
        return Image.open(p).convert("RGBA")
    return None


# ── Overlay drawing functions (same style as app.py) ──────────────────────────

def _pango_sym(ctx, text, x, y, size, r, g, b, a):
    """Render a Unicode symbol via PangoCairo."""
    ctx.save()
    ctx.move_to(x, y)
    ctx.set_source_rgba(r, g, b, a)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(f"sans bold {size}"))
    lay.set_text(text, -1)
    PangoCairo.show_layout(ctx, lay)
    ctx.restore()


def _draw_zzz(ctx, x, y, w, h):
    t = time.monotonic()
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    base_y = y + w * 0.25
    for i, (size, phase, dx) in enumerate([(10, 0.0, 4), (8, 1.0, 10), (6, 2.0, 14)]):
        offset_y = ((t * 0.6 + phase) % 3.0) / 3.0
        alpha = 1.0 - offset_y * 0.7
        ctx.set_font_size(size)
        ctx.set_source_rgba(0.3, 0.2, 0.1, alpha)
        ctx.move_to(x + w // 2 + dx, base_y - int(offset_y * 18))
        ctx.show_text("Z")


def _draw_surprised(ctx, x, y, w, h):
    t = time.monotonic()
    ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    head_y = y + h * 0.3
    for i, (size, dx) in enumerate([(11, -6), (9, 2), (7, 8)]):
        shake = math.sin(t * 12 + i * 2) * 2
        r = 0.9 - i * 0.2
        ctx.set_font_size(size)
        ctx.set_source_rgba(r, 0.7 - i * 0.2, 0.0, 1.0)
        ctx.move_to(x + w // 2 + dx + shake, head_y - i * 3)
        ctx.show_text("!")


def _draw_love(ctx, x, y, w, h):
    t = time.monotonic()
    head_y = y + h * 0.3
    for i, (size, phase, dx) in enumerate([(10, 0.0, -2), (8, 1.2, 6), (7, 2.4, 12)]):
        offset_y = ((t * 0.5 + phase) % 3.0) / 3.0
        alpha = 1.0 - offset_y * 0.8
        r = 0.9 - i * 0.15
        _pango_sym(ctx, "\u2665", x + w // 2 + dx, head_y - int(offset_y * 18), size, r, 0.2, 0.3, alpha)


def _draw_hurt(ctx, x, y, w, h):
    t = time.monotonic()
    cx, cy = x + w * 0.5, y + h * 0.3
    r = w * 0.18
    sz = max(6, int(w * 0.08))
    for i in range(3):
        angle = t * 3 + i * (2 * math.pi / 3)
        sym = "\u2726" if i % 2 == 0 else "\u2727"
        _pango_sym(ctx, sym, cx + math.cos(angle) * r, cy + math.sin(angle) * r * 0.5, sz, 0.95, 0.85, 0.1, 0.9)


def _draw_die(ctx, x, y, w, h):
    t = time.monotonic()
    offset_y = ((t * 0.4) % 2.5) / 2.5
    alpha = 1.0 - offset_y * 0.9
    head_y = y + h * 0.3
    _pango_sym(ctx, "\U0001f480", x + w // 2 - 6, head_y - int(offset_y * 20), 12, 0.5, 0.4, 0.4, alpha)


def _draw_grooming(ctx, x, y, w, h):
    t = time.monotonic()
    pulse = 0.6 + 0.4 * math.sin(t * 3)
    _pango_sym(ctx, "\u2728", x + w // 2 + 4, y + h * 0.3, 10, 0.7, 0.85, 0.95, pulse)


def _draw_angry(ctx, x, y, w, h):
    t = time.monotonic()
    shake = math.sin(t * 14) * 2
    _pango_sym(ctx, "\U0001f4a2", x + w // 2 + shake, y + h * 0.25, 11, 0.85, 0.15, 0.1, 1.0)


def _draw_dash(ctx, x, y, w, h):
    t = time.monotonic()
    base_x = x - 4  # behind for east-facing
    ctx.set_line_width(2)
    for i in range(5):
        phase = (t * 8 + i * 0.4) % 1.0
        alpha = 0.7 - phase * 0.6
        ly = y + h * 0.25 + i * (h * 0.13)
        length = 12 + phase * 10
        ctx.set_source_rgba(0.6, 0.5, 0.4, max(0, alpha))
        ctx.move_to(base_x, ly)
        ctx.line_to(base_x - length, ly)
        ctx.stroke()


OVERLAY_MAP = {
    "_draw_zzz": _draw_zzz,
    "_draw_surprised": _draw_surprised,
    "_draw_love": _draw_love,
    "_draw_hurt": _draw_hurt,
    "_draw_die": _draw_die,
    "_draw_grooming": _draw_grooming,
    "_draw_angry": _draw_angry,
    "_draw_dash": _draw_dash,
}

# ── Meow / Chat bubble drawing ───────────────────────────────────────────────

def _pango_text_size(ctx, text):
    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(Pango.FontDescription(BUBBLE_FONT))
    layout.set_text(text, -1)
    return layout.get_pixel_size()


def _pango_show_text(ctx, text, r=0.3, g=0.2, b=0.1, a=1.0):
    ctx.set_source_rgba(r, g, b, a)
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(BUBBLE_FONT))
    lay.set_text(text, -1)
    PangoCairo.show_layout(ctx, lay)


def _draw_meow_bubble(ctx, text, cat_x, cat_y, cat_w, cat_h):
    text_w, text_h = _pango_text_size(ctx, text)
    pad_x, pad_y = 12, 8
    bw = max(80, text_w + pad_x * 2)
    bh = text_h + pad_y * 2
    bx = cat_x + cat_w / 2 - bw / 2
    by = cat_y - bh - 8
    bx = max(4, bx)

    ctx.set_source_rgba(0.95, 0.9, 0.8, 1)
    ctx.rectangle(bx, by, bw, bh); ctx.fill()
    px = 2
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.rectangle(bx, by, bw, px); ctx.fill()
    ctx.rectangle(bx, by + bh - px, bw, px); ctx.fill()
    ctx.rectangle(bx, by, px, bh); ctx.fill()
    ctx.rectangle(bx + bw - px, by, px, bh); ctx.fill()
    i = px * 2
    ctx.rectangle(bx + i, by + i, bw - i*2, px); ctx.fill()
    ctx.rectangle(bx + i, by + bh - i - px, bw - i*2, px); ctx.fill()
    ctx.rectangle(bx + i, by + i, px, bh - i*2); ctx.fill()
    ctx.rectangle(bx + i + bw - i*2 - px, by + i, px, bh - i*2); ctx.fill()
    ctx.set_source_rgba(0.95, 0.9, 0.8, 1)
    for cx, cy in [(bx, by), (bx + bw - px, by), (bx, by + bh - px), (bx + bw - px, by + bh - px)]:
        ctx.rectangle(cx, cy, px, px); ctx.fill()

    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.move_to(bx + (bw - text_w) / 2, by + (bh - text_h) / 2)
    _pango_show_text(ctx, text)


def _draw_chat_bubble(ctx, text, cat_x, cat_y, cat_w, cat_h):
    pad = 12
    content_w = 200
    lay = PangoCairo.create_layout(ctx)
    lay.set_font_description(Pango.FontDescription(BUBBLE_FONT))
    lay.set_text(text, -1)
    lay.set_width(content_w * Pango.SCALE)
    lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    lay.set_height(-4)
    lay.set_ellipsize(Pango.EllipsizeMode.END)
    _tw, th = lay.get_pixel_size()

    bw = content_w + pad * 2
    bh = pad * 2 + th + 32
    bx = cat_x + cat_w / 2 - bw / 2
    by = cat_y - bh - 15
    bx = max(4, bx)

    ctx.set_source_rgba(0.95, 0.9, 0.8, 0.95)
    ctx.rectangle(bx, by, bw, bh); ctx.fill()
    px = 3
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.rectangle(bx, by, bw, px); ctx.fill()
    ctx.rectangle(bx, by + bh - px, bw, px); ctx.fill()
    ctx.rectangle(bx, by, px, bh); ctx.fill()
    ctx.rectangle(bx + bw - px, by, px, bh); ctx.fill()
    ctx.move_to(bx + pad, by + pad)
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    PangoCairo.show_layout(ctx, lay)

    # Fake input field
    iy = by + pad + th + 6
    iw = content_w
    ih = 22
    ix = bx + pad
    ctx.set_source_rgba(1, 1, 1, 0.8)
    ctx.rectangle(ix, iy, iw, ih); ctx.fill()
    ctx.set_source_rgba(0.3, 0.2, 0.1, 0.5)
    ctx.rectangle(ix, iy, iw, 1); ctx.fill()
    ctx.rectangle(ix, iy + ih, iw, 1); ctx.fill()
    ctx.rectangle(ix, iy, 1, ih); ctx.fill()
    ctx.rectangle(ix + iw, iy, 1, ih); ctx.fill()
    ctx.move_to(ix + 6, iy + 4)
    ctx.set_source_rgba(0.5, 0.4, 0.3, 0.5)
    lay2 = PangoCairo.create_layout(ctx)
    lay2.set_font_description(Pango.FontDescription("monospace 10"))
    lay2.set_text("Parle au chat...", -1)
    PangoCairo.show_layout(ctx, lay2)

    # Tail
    tx = bx + bw / 2
    ty = by + bh
    ctx.set_source_rgba(0.3, 0.2, 0.1, 1)
    ctx.move_to(tx - 8, ty); ctx.line_to(tx + 8, ty); ctx.line_to(tx, ty + 10)
    ctx.close_path(); ctx.fill()


# ── Main app ──────────────────────────────────────────────────────────────────

class PreviewApp:
    def __init__(self):
        self.frame_index = 0
        self.cells = []  # [(cat_id, anim_name, dir, frames_as_surfaces, overlay_fn, label)]
        self.data_refs = []  # prevent GC

    def load_all(self):
        for cat_id, cat_label in CATS:
            for anim_name, direction, label, overlay_name in SHOWCASE:
                pil_frames = load_frames(cat_id, anim_name, direction)
                if not pil_frames:
                    self.cells.append((cat_id, anim_name, direction, [], None, label))
                    continue
                surfaces = []
                for img in pil_frames:
                    s, d = pil_to_surface(img)
                    surfaces.append(s)
                    self.data_refs.append(d)
                overlay_fn = OVERLAY_MAP.get(overlay_name)
                self.cells.append((cat_id, anim_name, direction, surfaces, overlay_fn, label))

        # Special cells: meow bubble cat + chat bubble cat
        for special in ["_meow_", "_chat_"]:
            cat_id = "cat_orange"
            idle = load_idle(cat_id)
            if idle:
                s, d = pil_to_surface(idle)
                self.cells.append((cat_id, special, "south", [s], None, special))
                self.data_refs.append(d)

    def build_ui(self):
        self.app = Gtk.Application(application_id="catai.preview")
        self.app.connect("activate", self._on_activate)
        self.app.run([])

    def _on_activate(self, app):
        n_cols = len(SHOWCASE)
        n_rows = len(CATS)

        total_w = n_cols * CELL_W + 60
        # Extra row for meow+chat demo
        total_h = n_rows * (CELL_H + 18) + 180 + 40

        win = Gtk.Window(application=app, title="CATAI Sprite Preview")
        win.set_default_size(min(total_w, 2200), min(total_h, 1200))

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        win.set_child(scroll)

        da = Gtk.DrawingArea()
        da.set_content_width(total_w)
        da.set_content_height(total_h)
        da.set_draw_func(self._draw)
        scroll.set_child(da)

        self._da = da
        GLib.timeout_add(int(1000 / FPS), self._tick)
        win.present()

    def _tick(self):
        self.frame_index += 1
        self._da.queue_draw()
        return True

    def _draw(self, da, ctx, w, h):
        # Background
        ctx.set_source_rgb(0.1, 0.1, 0.18)
        ctx.paint()

        n_cols = len(SHOWCASE)
        margin_x, margin_y = 30, 30

        # Title
        ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(16)
        ctx.set_source_rgba(1, 0.8, 0.3, 1)
        ctx.move_to(margin_x, 20)
        ctx.show_text(":: CATAI SPRITE PREVIEW ::")

        # Column headers
        ctx.set_font_size(9)
        ctx.set_source_rgba(0.7, 0.6, 0.4, 0.8)
        for col, (anim_name, direction, label, _) in enumerate(SHOWCASE):
            cx = margin_x + col * CELL_W + CELL_W // 2
            ctx.move_to(cx - len(label) * 2.5, margin_y + 4)
            ctx.show_text(label)

        start_y = margin_y + 12

        for row, (cat_id, cat_label) in enumerate(CATS):
            ry = start_y + row * (CELL_H + 18)

            # Row label
            ctx.set_font_size(10)
            ctx.set_source_rgba(0.9, 0.75, 0.3, 1)
            ctx.move_to(4, ry + CELL_H // 2)
            ctx.show_text(cat_label.split(" ")[-1])

            for col in range(n_cols):
                idx = row * n_cols + col
                if idx >= len(self.cells):
                    break
                cat_id_c, anim_name, direction, surfaces, overlay_fn, label = self.cells[idx]

                cx = margin_x + col * CELL_W
                cy = ry

                if not surfaces:
                    # Missing anim — draw grey placeholder
                    ctx.set_source_rgba(0.2, 0.2, 0.25, 0.5)
                    ctx.rectangle(cx, cy, SPRITE_SIZE, SPRITE_SIZE)
                    ctx.fill()
                    ctx.set_font_size(8)
                    ctx.set_source_rgba(0.4, 0.4, 0.4, 1)
                    ctx.move_to(cx + 10, cy + 42)
                    ctx.show_text("N/A")
                    continue

                # Draw speed lines BEFORE sprite (they go behind)
                if overlay_fn is _draw_dash:
                    overlay_fn(ctx, cx, cy, SPRITE_SIZE, SPRITE_SIZE)

                # Draw sprite frame
                fi = self.frame_index % len(surfaces)
                ctx.set_source_surface(surfaces[fi], cx, cy)
                ctx.paint()

                # Draw other overlays AFTER sprite (they go on top)
                if overlay_fn and overlay_fn is not _draw_dash:
                    overlay_fn(ctx, cx, cy, SPRITE_SIZE, SPRITE_SIZE)

        # ── Bottom row: meow + chat demo ──────────────────────────────────────
        demo_y = start_y + len(CATS) * (CELL_H + 18) + 20

        ctx.set_font_size(12)
        ctx.set_source_rgba(1, 0.8, 0.3, 1)
        ctx.move_to(margin_x, demo_y)
        ctx.show_text(":: BUBBLES DEMO ::")
        demo_y += 16

        # Find the meow and chat special cells
        meow_cell = next((c for c in self.cells if c[1] == "_meow_"), None)
        chat_cell = next((c for c in self.cells if c[1] == "_chat_"), None)

        # Meow bubble demo
        if meow_cell and meow_cell[3]:
            mx = margin_x + 60
            my = demo_y + 50
            s = meow_cell[3][0]
            ctx.set_source_surface(s, mx, my)
            ctx.paint()
            _draw_meow_bubble(ctx, "Mrrp! 😺 Miaou~", mx, my, SPRITE_SIZE, SPRITE_SIZE)

            ctx.set_font_size(9)
            ctx.set_source_rgba(0.5, 0.5, 0.5, 1)
            ctx.move_to(mx - 10, my + SPRITE_SIZE + 14)
            ctx.show_text("Meow bubble")

        # Chat bubble demo
        if chat_cell and chat_cell[3]:
            cx_pos = margin_x + 340
            cy_pos = demo_y + 120
            s = chat_cell[3][0]
            ctx.set_source_surface(s, cx_pos, cy_pos)
            ctx.paint()
            _draw_chat_bubble(ctx, "Prrr... je suis un petit chat orange, je fais la sieste au soleil ! 🐾", cx_pos, cy_pos, SPRITE_SIZE, SPRITE_SIZE)

            ctx.set_font_size(9)
            ctx.set_source_rgba(0.5, 0.5, 0.5, 1)
            ctx.move_to(cx_pos - 10, cy_pos + SPRITE_SIZE + 14)
            ctx.show_text("Chat bubble + input")


if __name__ == "__main__":
    app = PreviewApp()
    print("Loading sprites...")
    app.load_all()
    print(f"Loaded {len(app.cells)} cells, launching preview...")
    app.build_ui()
