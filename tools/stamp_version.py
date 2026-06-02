#!/usr/bin/env python3
"""Stamp a version badge onto the Reel2Reel logo.

Always renders from the pristine no-badge base (assets/reel2reel_base.png) so the
badge never accumulates. Writes both reel2reel.png (repo root, for the README)
and assets/reel2reel.png (served by the plugin banner — see ui/logo.py).

The REEL2REEL artwork is full-bleed (the wordmark + glow fill the whole canvas),
so the badge sits bottom-right with a dark stroke to stay legible over the chrome.

Usage:
    python tools/stamp_version.py            # uses VERSION below
    python tools/stamp_version.py v0.3.0     # override the label

--- Badge parameters (tweak here; the badge will change often until stable) ---
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent


def _default_version() -> str:
    """Single source of truth: read the version from plugin_info.json (CLI arg overrides)."""
    try:
        v = json.loads((ROOT / "plugin_info.json").read_text()).get("version", "0.0.0")
    except Exception:
        v = "0.0.0"
    return v if v.startswith("v") else f"v{v}"


VERSION = _default_version()
COLOR = (255, 209, 26, 255)        # gold, to echo the glowing "2"
STROKE = (16, 16, 20, 255)         # dark outline so it reads over the chrome
STROKE_W = 3
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SIZE = 40
# Bottom-right; anchor "rs" = right edge / baseline.
POS_X_FROM_RIGHT = 26
POS_Y_FROM_BOTTOM = 22
ANCHOR = "rs"

BASE = ROOT / "assets" / "reel2reel_base.png"


def stamp(version: str = VERSION) -> None:
    img = Image.open(BASE).convert("RGBA")
    W, H = img.size
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except Exception:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(img)
    draw.text((W - POS_X_FROM_RIGHT, H - POS_Y_FROM_BOTTOM), version, font=font,
              fill=COLOR, anchor=ANCHOR, stroke_width=STROKE_W, stroke_fill=STROKE)
    img.save(ROOT / "reel2reel.png")
    img.save(ROOT / "assets" / "reel2reel.png")
    print(f"stamped {version!r} bottom-right of {W}x{H} (size={FONT_SIZE})")


if __name__ == "__main__":
    stamp(sys.argv[1] if len(sys.argv) > 1 else VERSION)
