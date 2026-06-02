"""Reel2Reel logo banner.

Rendered top-right of the suite (absolutely positioned via styles.py). The PNG is
base64-embedded into the HTML so it survives Gradio's static-file routing (same
approach as Image Suite / Replicant). Drop the artwork at
``assets/reel2reel_logo.png`` — if it's missing we fall back to a styled text
banner so the plugin still renders.
"""
from __future__ import annotations

import base64
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_LOGO = _ASSETS / "reel2reel_logo.png"


def _logo_data_uri() -> str:
    try:
        b64 = base64.b64encode(_LOGO.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""


def banner_html() -> str:
    uri = _logo_data_uri()
    if uri:
        return f'<div id="reel2reel-banner"><img src="{uri}" alt="Reel2Reel"/></div>'
    return '<div id="reel2reel-banner"><h2>Reel2Reel</h2></div>'
