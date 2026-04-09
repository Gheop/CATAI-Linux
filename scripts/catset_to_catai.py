#!/usr/bin/env python3
"""Convert seethingswarm catset spritesheets into CATAI character directories.

Usage:
    python3 scripts/catset_to_catai.py <catset_spritesheets_dir> <output_catai_dir>

Example:
    python3 scripts/catset_to_catai.py catset_assets/catset_spritesheets catai_linux/

Each cat{NN}_spritesheets/ subdirectory becomes a catai_linux/cat{NN}/ character.
Sprites are upscaled 2× (40→80px) with nearest-neighbor interpolation.
"""
import json
import os
import re
import sys
import uuid
from pathlib import Path
from PIL import Image

# ── Config ─────────────────────────────────────────────────────────────────────

SCALE = 2          # 40×40 → 80×80
TARGET = 40 * SCALE  # 80

# Default character names (edit freely)
CAT_NAMES = {
    "cat01": "tabby",
    "cat02": "dark_cat",
    "cat03": "brown_cat",
    "cat04": "grey_cat",
    "cat05": "black_cat",
}

# ── Palette swap ───────────────────────────────────────────────────────────────

# Orange gradient from dark to light (for palette swap)
ORANGE_PALETTE = [
    (45, 18, 0),
    (90, 38, 5),
    (145, 65, 10),
    (195, 95, 25),
    (230, 130, 45),
    (255, 165, 65),
    (255, 195, 100),
    (255, 220, 145),
    (255, 240, 195),
]

def palette_swap(img: Image.Image, target_palette: list) -> Image.Image:
    """Map all non-transparent colors to target_palette by luminance order."""
    def lum(rgb):
        r, g, b = [x / 255.0 for x in rgb]
        return 0.299 * r + 0.587 * g + 0.114 * b

    pixels = list(img.getdata())
    unique = sorted({p[:3] for p in pixels if p[3] > 10}, key=lum)
    n = len(unique)
    pal = target_palette

    color_map = {}
    for i, color in enumerate(unique):
        t = i / max(1, n - 1)
        idx = t * (len(pal) - 1)
        lo, hi = int(idx), min(int(idx) + 1, len(pal) - 1)
        f = idx - lo
        r = int(pal[lo][0] + f * (pal[hi][0] - pal[lo][0]))
        g = int(pal[lo][1] + f * (pal[hi][1] - pal[lo][1]))
        b = int(pal[lo][2] + f * (pal[hi][2] - pal[lo][2]))
        color_map[color] = (r, g, b)

    out = Image.new("RGBA", img.size)
    out.putdata([
        (color_map[p[:3]] + (p[3],)) if p[3] > 10 else p
        for p in pixels
    ])
    return out

# catset anim → (catai_anim, directions, max_frames)
# directions: list of CATAI directions to generate
#   "east"  = catset frames as-is (face right)
#   "west"  = horizontal flip of east
#   "south" = catset frames used as-is (side view for stationary anims)
ANIM_MAP = [
    # catset_anim        catai_anim             directions              max_frames
    ("run",             "running-8-frames",    ["east", "west"],       None),
    ("sleep",           "sleeping-ball",       ["south"],              None),
    ("sit",             "flat",                ["south"],              None),
    ("attack",          "angry",               ["south"],              None),
    ("sneak",           "chasing-mouse",       ["east", "west"],       None),
    ("fright",          "surprised",           ["east", "west"],       None),
    ("crouch",          "eating",              ["south"],              None),
    ("idle_blink",      "love",                ["south"],              None),
    ("idle",            "grooming",            ["south"],              None),
    ("liedown",         "rolling",             ["south"],              8),   # first 8 of 24
    ("walk",            "waking-getting-up",   ["south"],              None),
    ("jump",            "jumping",             ["south"],              None),
    ("ledgeclimb",      "climbing",            ["east", "west"],       None),
    ("dash",                "dash",                ["east", "west"],       None),
    ("die",                 "die",                 ["south"],              None),
    ("fall",                "fall",                ["south"],              None),
    ("hurt",                "hurt",                ["south"],              None),
    ("land",                "land",                ["south"],              None),
    ("ledgeclimb_struggle", "ledgeclimb-struggle", ["east", "west"],       None),
    ("ledgegrab",           "ledgegrab",           ["east", "west"],       None),
    ("ledgeidle",           "ledgeidle",           ["east", "west"],       None),
    ("wallclimb",           "wallclimb",           ["east", "west"],       None),
    ("wallgrab",            "wallgrab",            ["east", "west"],       None),
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def extract_frames(sheet_path: Path, n_frames: int, max_frames: int | None = None,
                   recolor=None) -> list[Image.Image]:
    """Slice a horizontal strip into individual RGBA frames, upscaled 2×.
    If recolor is a palette list, apply palette_swap to each frame."""
    img = Image.open(sheet_path).convert("RGBA")
    w, h = img.size
    fw = w // n_frames
    frames = []
    limit = min(n_frames, max_frames) if max_frames else n_frames
    for i in range(limit):
        frame = img.crop((i * fw, 0, (i + 1) * fw, h))
        frame = frame.resize((TARGET, TARGET), Image.NEAREST)
        if recolor:
            frame = palette_swap(frame, recolor)
        frames.append(frame)
    return frames

def flip_frames(frames: list[Image.Image]) -> list[Image.Image]:
    return [f.transpose(Image.FLIP_LEFT_RIGHT) for f in frames]

def save_frames(frames: list[Image.Image], out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, f in enumerate(frames):
        rel = out_dir / f"frame_{i:03d}.png"
        f.save(rel)
        paths.append(str(rel))
    return paths

def find_sheet(cat_dir: Path, anim_name: str) -> tuple[Path, int] | None:
    """Find the spritesheet for a given animation name, return (path, n_frames)."""
    for p in cat_dir.iterdir():
        m = re.match(rf"cat\d+_{re.escape(anim_name)}_strip(\d+)\.png$", p.name)
        if m:
            return p, int(m.group(1))
    return None

# ── Main ───────────────────────────────────────────────────────────────────────

def convert_cat(cat_src_dir: Path, cat_out_dir: Path, cat_id: str, recolor=None):
    name = CAT_NAMES.get(cat_id, cat_id)
    print(f"\n── {cat_id} → {name} ({'→'.join([str(cat_src_dir.name), str(cat_out_dir.name)])})")

    cat_out_dir.mkdir(parents=True, exist_ok=True)
    rot_dir = cat_out_dir / "rotations"
    rot_dir.mkdir(exist_ok=True)
    anim_base = cat_out_dir / "animations"

    # ── Rotations ──────────────────────────────────────────────────────────────
    # east/diagonals-east: idle frame 0
    # west/diagonals-west: idle frame 0 flipped
    # south/north: sit frame 0

    idle_info = find_sheet(cat_src_dir, "idle")
    sit_info  = find_sheet(cat_src_dir, "sit")

    if not idle_info:
        print(f"  ERROR: no idle sheet found in {cat_src_dir}")
        return
    idle_frames = extract_frames(*idle_info, recolor=recolor)
    sit_frames  = extract_frames(*sit_info, recolor=recolor) if sit_info else idle_frames

    east_rot  = idle_frames[0]
    west_rot  = east_rot.transpose(Image.FLIP_LEFT_RIGHT)
    south_rot = sit_frames[0]

    rotations = {
        "east":       east_rot,
        "north-east": east_rot,
        "south-east": east_rot,
        "west":       west_rot,
        "north-west": west_rot,
        "south-west": west_rot,
        "south":      south_rot,
        "north":      south_rot,
    }
    rot_paths = {}
    for dir_name, img in rotations.items():
        p = rot_dir / f"{dir_name}.png"
        img.save(p)
        rot_paths[dir_name] = f"rotations/{dir_name}.png"
    print(f"  rotations: {len(rot_paths)} saved")

    # ── Animations ─────────────────────────────────────────────────────────────
    anim_meta: dict[str, dict[str, list[str]]] = {}

    for catset_anim, catai_anim, directions, max_frames in ANIM_MAP:
        info = find_sheet(cat_src_dir, catset_anim)
        if not info:
            print(f"  skip {catset_anim} (not found)")
            continue
        sheet_path, n_frames = info
        frames_east = extract_frames(sheet_path, n_frames, max_frames, recolor=recolor)
        frames_west = flip_frames(frames_east)

        anim_meta[catai_anim] = {}
        for direction in directions:
            frames = frames_west if direction == "west" else frames_east
            out_dir = anim_base / catai_anim / direction
            rel_paths = []
            out_dir.mkdir(parents=True, exist_ok=True)
            for i, f in enumerate(frames):
                p = out_dir / f"frame_{i:03d}.png"
                f.save(p)
                rel_paths.append(f"animations/{catai_anim}/{direction}/frame_{i:03d}.png")
            anim_meta[catai_anim][direction] = rel_paths
        print(f"  {catset_anim:20s} → {catai_anim:22s} {directions} ({len(frames_east)} frames)")

    # ── metadata.json ──────────────────────────────────────────────────────────
    meta = {
        "character": {
            "id": str(uuid.uuid4()),
            "name": name,
            "prompt": name.replace("_", " "),
            "size": {"width": TARGET, "height": TARGET},
            "template_id": "catset",
            "directions": 8,
            "view": "side",
            "created_at": "2026-04-09T00:00:00Z",
        },
        "frames": {
            "rotations": rot_paths,
            "animations": anim_meta,
        }
    }
    with open(cat_out_dir / "metadata.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(f"  metadata.json written")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    src_root = Path(sys.argv[1])
    out_root = Path(sys.argv[2])

    cat_dirs = sorted(p for p in src_root.iterdir()
                      if p.is_dir() and re.match(r"cat\d+_spritesheets", p.name))
    if not cat_dirs:
        print(f"No cat{{NN}}_spritesheets directories found in {src_root}")
        sys.exit(1)

    print(f"Found {len(cat_dirs)} cats in {src_root}")
    for cat_src in cat_dirs:
        cat_id = cat_src.name.replace("_spritesheets", "")  # cat01
        cat_out = out_root / cat_id
        convert_cat(cat_src, cat_out, cat_id)

    # Generate orange cat: palette-swap of cat01 with orange/yellow tones
    cat01_src = src_root / "cat01_spritesheets"
    if cat01_src.exists():
        print("\n── cat_orange (cat01 + orange palette swap)")
        CAT_NAMES["cat_orange"] = "orange_cat"
        convert_cat(cat01_src, out_root / "cat_orange", "cat_orange", recolor=ORANGE_PALETTE)

    print(f"\nDone. Characters written to {out_root}")
    print("Next: open sprite_viewer.html and point it at one of the cat directories.")


if __name__ == "__main__":
    main()
