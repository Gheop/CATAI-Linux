#!/usr/bin/env python3
"""Post-process raw SD outputs into CATAI-ready sprites.

Usage:
    python3 scripts/sd_postprocess.py <input_dir> <output_dir>

Input:  flat directory of PNGs named  <anim>__<dir>__<frame>.png
        e.g.  running__east__000.png
Output: catai_linux/<character>/animations/<anim>/<dir>/frame_NNN.png

Dependencies:
    pip install rembg pillow
"""
import os, sys, re
from PIL import Image

try:
    from rembg import remove as rembg_remove
    HAS_REMBG = True
except ImportError:
    print("Warning: rembg not installed — skipping background removal")
    HAS_REMBG = False

TARGET_SIZE = 32   # final sprite size in pixels

def palette_quantize(img: Image.Image, n_colors: int = 24) -> Image.Image:
    """Quantize to n_colors while preserving transparency."""
    rgb  = img.convert("RGB")
    mask = img.split()[3]                          # alpha channel
    q    = rgb.quantize(colors=n_colors, method=Image.Quantize.MEDIANCUT)
    out  = q.convert("RGBA")
    out.putalpha(mask)
    return out

def process(src: str, dst: str) -> None:
    img = Image.open(src).convert("RGBA")

    # 1. Remove background
    if HAS_REMBG:
        img = rembg_remove(img)

    # 2. Crop to bounding box (remove empty borders)
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    # 3. Resize to square TARGET_SIZE, nearest-neighbor (pixel-art sharp)
    img = img.resize((TARGET_SIZE, TARGET_SIZE), Image.NEAREST)

    # 4. Palette quantize for cross-frame colour consistency
    img = palette_quantize(img, n_colors=24)

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    img.save(dst)

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    src_dir, out_base = sys.argv[1], sys.argv[2]
    pattern = re.compile(r"^(.+?)__(.+?)__(\d+)\.png$")

    files = sorted(f for f in os.listdir(src_dir) if f.endswith(".png"))
    if not files:
        print(f"No PNG files found in {src_dir}")
        sys.exit(1)

    for fname in files:
        m = pattern.match(fname)
        if not m:
            print(f"  skip (unexpected name): {fname}")
            continue
        anim, direction, frame_n = m.group(1), m.group(2), m.group(3)
        dst = os.path.join(out_base, "animations", anim, direction,
                           f"frame_{frame_n}.png")
        process(os.path.join(src_dir, fname), dst)
        print(f"  ✓ {anim}/{direction}/frame_{frame_n}.png")

    print("\nDone.")

if __name__ == "__main__":
    main()
