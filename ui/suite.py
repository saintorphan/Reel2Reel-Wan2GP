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

from . import logo, page
from .settings_panel import build_settings_panel

_TAB_IDS = {"library": "reel2reel-lib", "timeline": "reel2reel-tl",
            "render": "reel2reel-render", "settings": "reel2reel-settings"}
_LABELS = {"library": "📚 Library", "timeline": "🎞 Timeline", "render": "🎬 Render"}


def _projbar() -> dict:
    """Persistent project/version bar — rendered ABOVE the sub-tabs so it shows on
    every Reel2Reel page. Hot path (open/save/snapshot) inline; rare/destructive ops
    tucked behind one disclosure. plugin.py wires it via _wire_projects(ui['bar'])."""
    c = {}
    # Logo on the left (flows in the header like ImageSuite's), CRUD pushed to the
    # right as icon buttons (tooltips set in timeline.js decorateProjbar).
    with gr.Row(elem_id="reel2reel-projbar"):
        gr.HTML(logo.banner_html(), elem_id="reel2reel-bannerwrap")
        c["current_lbl"] = gr.Markdown("*No project open*", elem_id="reel2reel-projlabel")
        c["proj_dd"] = gr.Dropdown(label="Open project", choices=[], scale=0,
                                  container=False, min_width=170)
        c["open"] = gr.Button("📂", scale=0, min_width=42, elem_id="r2r-pb-open")
        c["save"] = gr.Button("💾", scale=0, min_width=42, elem_id="r2r-pb-save",
                             elem_classes="reel2reel-prim")
        c["ver_label"] = gr.Textbox(placeholder="snapshot name", scale=0, min_width=120,
                                   container=False, show_label=False)
        c["snapshot"] = gr.Button("📸", scale=0, min_width=42, elem_id="r2r-pb-snap")
        c["bar_status"] = gr.Markdown("", elem_id="reel2reel-bar-status")
    with gr.Accordion("⚙ Manage project · versions · interchange", open=False,
                      elem_id="reel2reel-manage"):
        c["proj_name"] = gr.Textbox(label="Name (New / Save as / Rename / Duplicate)")
        with gr.Row():
            c["new"] = gr.Button("New")
            c["saveas"] = gr.Button("Save as")
            c["rename"] = gr.Button("Rename")
            c["dup"] = gr.Button("Duplicate")
            c["delete"] = gr.Button("🗑 Delete")
            c["restore_auto"] = gr.Button("↺ Restore autosave")
        with gr.Row():
            c["ver_dd"] = gr.Dropdown(label="Versions", choices=[], scale=2)
            c["restore"] = gr.Button("Restore")
            c["delver"] = gr.Button("Delete version")
        with gr.Row():
            c["otio_export"] = gr.DownloadButton("⬇ Export .otio", size="sm")
            c["otio_import"] = gr.UploadButton("⬆ Import .otio",
                                              file_types=[".otio", ".json"], size="sm")
    return c


def build_suite() -> dict:
    pages = {}
    bar = _projbar()
    with gr.Tabs() as subtabs:
        # The editor tab hosts the library (left rail) + timeline + inspector together;
        # build_editor returns the two wiring dicts separately so plugin.py keeps using
        # pages["timeline"] / pages["library"] exactly as before.
        with gr.Tab(_LABELS["timeline"], id=_TAB_IDS["timeline"]):
            pages["timeline"], pages["library"] = page.build_editor()
        with gr.Tab(_LABELS["render"], id=_TAB_IDS["render"]):
            pages["render"] = page.build_page("render")
        with gr.Tab("⚙ Settings", id=_TAB_IDS["settings"]):
            settings = build_settings_panel()
    return {"subtabs": subtabs, "tab_ids": _TAB_IDS, "pages": pages,
            "settings": settings, "bar": bar}
