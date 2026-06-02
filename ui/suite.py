"""Assemble the Reel2Reel tab: Library / Timeline / Render + Settings sub-tabs.

Returns a wiring contract dict:
    {
      "subtabs": gr.Tabs,            # so 'Add to timeline' can switch sub-tab
      "tab_ids": {"library":..., "timeline":..., "render":..., "settings":...},
      "pages":   {"library":{...}, "timeline":{...}, "render":{...}},
      "settings": {...},
    }
"""
from __future__ import annotations

import gradio as gr

from . import page
from .settings_panel import build_settings_panel

_TAB_IDS = {"library": "reel2reel-lib", "timeline": "reel2reel-tl",
            "render": "reel2reel-render", "settings": "reel2reel-settings"}
_LABELS = {"library": "📚 Library", "timeline": "🎞 Timeline", "render": "🎬 Render"}


def build_suite() -> dict:
    pages = {}
    with gr.Tabs() as subtabs:
        for mode in page.MODES:
            with gr.Tab(_LABELS[mode], id=_TAB_IDS[mode]):
                pages[mode] = page.build_page(mode)
        with gr.Tab("⚙ Settings", id=_TAB_IDS["settings"]):
            settings = build_settings_panel()
    return {"subtabs": subtabs, "tab_ids": _TAB_IDS, "pages": pages,
            "settings": settings}
