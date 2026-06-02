#!/usr/bin/env python3
"""Stamp a version badge onto the Reel2Reel logo.

Always renders from the pristine no-badge base (assets/reel2reel_base.png) so the
badge never accumulates. Writes both reel2reel.png (repo root, for the README)
and assets/reel2reel.png (served by the plugin banner).

Usage:
    python tools/stamp_version.py            # uses VERSION below
    python tools/stamp_version.py v0.2.0     # override the label

--- Badge parameters (tweak here; the badge will change often until stable) ---
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

VERSION = "v0.2.0"
# Amber sampled from the "REEL2REEL" wordmark.
COLOR = (224, 161, 6, 255)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SIZE = 34
# Bottom-left, inside the inner border. anchor="ls" = left edge / baseline.
POS_X = 83
POS_Y_FROM_BOTTOM = 42       # baseline = image_height - this
ANCHOR = "ls"

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "assets" / "reel2reel_base.png"


def stamp(version: str = VERSION) -> None:
    img = Image.open(BASE).convert("RGBA")
    W, H = img.size
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except Exception:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(img)
    draw.text((POS_X, H - POS_Y_FROM_BOTTOM), version, font=font, fill=COLOR, anchor=ANCHOR)
    # Keep RGBA on BOTH so the transparent background is preserved.
    img.save(ROOT / "reel2reel.png")
    img.save(ROOT / "assets" / "reel2reel.png")
    print(f"stamped {version!r} at x={POS_X}, baseline_y={H - POS_Y_FROM_BOTTOM}, size={FONT_SIZE}")


if __name__ == "__main__":
    stamp(sys.argv[1] if len(sys.argv) > 1 else VERSION)
