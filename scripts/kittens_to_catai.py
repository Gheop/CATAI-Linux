#!/usr/bin/env python3
"""Convert seethingswarm kitten spritesheets into CATAI character directories.

Usage:
    python3 scripts/kittens_to_catai.py <kittens_spritesheets_dir> <output_catai_dir>

Example:
    python3 scripts/kittens_to_catai.py kittens_assets/kittens_spritesheets catai_linux/

Each kitten{NN}_spritesheets/ becomes a catai_linux/kitten{NN}/ character.
Sprites are upscaled 2× (32→64px) — kittens are intentionally smaller than cats
(cats are 80px) to reinforce the "they're babies" feeling.

Kittens have 17 animations (vs 23 for cats). Missing: sleep, fright, ledgeclimb,
ledgeclimb_struggle, ledgegrab, ledgeidle.
"""
import json
import os
import re
import sys
import uuid
from pathlib import Path
from PIL import Image

# ── Config ─────────────────────────────────────────────────────────────────────

SCALE = 2            # 32×32 → 64×64
TARGET = 32 * SCALE  # 64

# Default character names (edit freely)
KITTEN_NAMES = {
    "kitten01": "tabby_kitten",
    "kitten02": "dark_kitten",
    "kitten03": "brown_kitten",
    "kitten04": "grey_kitten",
    "kitten05": "black_kitten",
}

# ── Palette swap ───────────────────────────────────────────────────────────────

# Orange gradient from dark to light — same as cats
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


# kitten anim → (catai_anim, directions, max_frames)
# Same naming conventions as catset_to_catai.py — so kittens can reuse the
# same CatState + ANIM_KEYS infrastructure later if we integrate them.
ANIM_MAP = [
    # kitten_anim         catai_anim             directions              max_frames
    ("run",               "running-8-frames",    ["east", "west"],       None),  # 4
    ("sit",               "flat",                ["south"],              None),  # 8
    ("attack",            "angry",               ["south"],              None),  # 6
    ("sneak",             "chasing-mouse",       ["east", "west"],       None),  # 4
    ("crouch",            "eating",              ["south"],              None),  # 8
    ("idle_blink",        "love",                ["south"],              None),  # 8
    ("idle",              "grooming",            ["south"],              None),  # 8
    ("liedown",           "rolling",             ["south"],              8),    # first 8 of 24
    ("walk",              "waking-getting-up",   ["south"],              None),  # 8
    ("jump",              "jumping",             ["south"],              None),  # 5
    ("dash",              "dash",                ["east", "west"],       None),  # 10
    ("die",               "die",                 ["south"],              None),  # 8
    ("fall",              "fall",                ["south"],              None),  # 5
    ("hurt",              "hurt",                ["south"],              None),  # 5
    ("land",              "land",                ["south"],              None),  # 3
    ("wallclimb",         "wallclimb",           ["east", "west"],       None),  # 4
    ("wallgrab",          "wallgrab",            ["east", "west"],       None),  # 8
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def extract_frames(sheet_path: Path, n_frames: int, max_frames: int | None = None,
                   recolor=None) -> list[Image.Image]:
    """Slice a horizontal strip into individual RGBA frames, upscaled."""
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


def find_sheet(kitten_dir: Path, anim_name: str) -> tuple[Path, int] | None:
    """Find the spritesheet for a given animation, return (path, n_frames)."""
    for p in kitten_dir.iterdir():
        m = re.match(rf"kitten\d+_{re.escape(anim_name)}_strip(\d+)\.png$", p.name)
        if m:
            return p, int(m.group(1))
    return None


# ── Main ───────────────────────────────────────────────────────────────────────

def convert_kitten(src_dir: Path, out_dir: Path, kitten_id: str, recolor=None):
    name = KITTEN_NAMES.get(kitten_id, kitten_id)
    print(f"\n── {kitten_id} → {name}")

    out_dir.mkdir(parents=True, exist_ok=True)
    rot_dir = out_dir / "rotations"
    rot_dir.mkdir(exist_ok=True)
    anim_base = out_dir / "animations"

    # ── Rotations ──────────────────────────────────────────────────────────────
    idle_info = find_sheet(src_dir, "idle")
    sit_info  = find_sheet(src_dir, "sit")

    if not idle_info:
        print(f"  ERROR: no idle sheet found in {src_dir}")
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

    for kitten_anim, catai_anim, directions, max_frames in ANIM_MAP:
        info = find_sheet(src_dir, kitten_anim)
        if not info:
            print(f"  skip {kitten_anim} (not found)")
            continue
        sheet_path, n_frames = info
        frames_east = extract_frames(sheet_path, n_frames, max_frames, recolor=recolor)
        frames_west = flip_frames(frames_east)

        anim_meta[catai_anim] = {}
        for direction in directions:
            frames = frames_west if direction == "west" else frames_east
            anim_out = anim_base / catai_anim / direction
            rel_paths = []
            anim_out.mkdir(parents=True, exist_ok=True)
            for i, f in enumerate(frames):
                p = anim_out / f"frame_{i:03d}.png"
                f.save(p)
                rel_paths.append(f"animations/{catai_anim}/{direction}/frame_{i:03d}.png")
            anim_meta[catai_anim][direction] = rel_paths
        print(f"  {kitten_anim:15s} → {catai_anim:22s} {directions} ({len(frames_east)} frames)")

    # ── metadata.json ──────────────────────────────────────────────────────────
    meta = {
        "character": {
            "id": str(uuid.uuid4()),
            "name": name,
            "prompt": name.replace("_", " "),
            "size": {"width": TARGET, "height": TARGET},
            "template_id": "kitten",
            "directions": 8,
            "view": "side",
            "created_at": "2026-04-10T00:00:00Z",
        },
        "frames": {
            "rotations": rot_paths,
            "animations": anim_meta,
        }
    }
    with open(out_dir / "metadata.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(f"  metadata.json written")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    src_root = Path(sys.argv[1])
    out_root = Path(sys.argv[2])

    kitten_dirs = sorted(p for p in src_root.iterdir()
                         if p.is_dir() and re.match(r"kitten\d+_spritesheets", p.name))
    if not kitten_dirs:
        print(f"No kitten{{NN}}_spritesheets directories found in {src_root}")
        sys.exit(1)

    print(f"Found {len(kitten_dirs)} kittens in {src_root}")
    for src in kitten_dirs:
        kitten_id = src.name.replace("_spritesheets", "")  # kitten01
        convert_kitten(src, out_root / kitten_id, kitten_id)

    # Generate orange kitten: palette-swap of kitten02 (dark) with orange tones
    kitten02_src = src_root / "kitten02_spritesheets"
    if kitten02_src.exists():
        print("\n── kitten_orange (kitten02 + orange palette swap)")
        KITTEN_NAMES["kitten_orange"] = "orange_kitten"
        convert_kitten(kitten02_src, out_root / "kitten_orange", "kitten_orange", recolor=ORANGE_PALETTE)

    print(f"\nDone. Characters written to {out_root}")


if __name__ == "__main__":
    main()
