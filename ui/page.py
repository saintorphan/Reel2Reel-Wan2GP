"""Surface builders for the three Reel2Reel working sub-tabs.

Each builder returns a plain components dict; plugin.py owns every event wire
(it holds the live project, the Wan2GP globals and the bridge). The sub-tabs:

  * library  — browse the Wan2GP outputs folder, send clips to the timeline.
  * timeline — the multi-track canvas + edit toolbar + project save/load.
  * render   — export to mp4, preview, send the cut to Img2Vid / Save As.
"""
from __future__ import annotations

import gradio as gr

from . import timeline_widget

MODES = ("library", "timeline", "render")


def _library() -> dict:
    c = {"mode": "library"}
    gr.Markdown("### Library — your generated clips\n"
                "Newest first, from the Wan2GP outputs folder (and your renders). "
                "Pick one and **Add to timeline**, or just switch to the Video "
                "Generator and *Send to Reel2Reel* from there.")
    with gr.Row():
        c["refresh"] = gr.Button("🔄 Refresh", scale=0)
        c["kind"] = gr.Radio(["auto", "Video", "Audio"], value="auto",
                             label="Add to track", scale=1)
        c["add"] = gr.Button("➕ Add to timeline", variant="primary", scale=1,
                            elem_classes="reel2reel-prim")
    c["gallery"] = gr.Gallery(label="Outputs", columns=4, height=420,
                             object_fit="cover", preview=False,
                             elem_classes="reel2reel-gallery", allow_preview=False)
    c["picked"] = gr.State(None)       # absolute path of the selected library item
    c["status"] = gr.Markdown("")
    return c


def _timeline() -> dict:
    c = {"mode": "timeline"}
    gr.Markdown("### Timeline\n"
                "Drag clips to move, drag an edge to trim. Click the ruler to set "
                "the playhead; the preview is approximate — **export** for the real cut.")
    with gr.Row():
        c["split"] = gr.Button("✂ Split at playhead", scale=0)
        c["add_video"] = gr.Button("➕ Video track", scale=0)
        c["add_audio"] = gr.Button("➕ Audio track", scale=0)
        c["remove_sel"] = gr.Button("🗑 Remove selected", scale=0)
    widget = timeline_widget.build_timeline_widget()
    c.update(widget)                   # mount, tl_to_py, tl_from_py
    with gr.Row():
        c["proj_name"] = gr.Textbox(label="Project", value="Cut 1", scale=2)
        c["new"] = gr.Button("New", scale=0)
        c["save"] = gr.Button("💾 Save", scale=0)
        c["load_name"] = gr.Dropdown(label="Open project", choices=[], scale=2)
        c["load"] = gr.Button("Open", scale=0)
    c["status"] = gr.Markdown("")
    return c


def _render() -> dict:
    c = {"mode": "render"}
    gr.Markdown("### Render\n"
                "Composite the timeline to an mp4 with ffmpeg (normalize → "
                "overlay-onto-canvas → mux). Hard cuts + gaps render today; "
                "cross-dissolves are a deferred milestone.")
    with gr.Row():
        c["export"] = gr.Button("🎬 Export mp4", variant="primary",
                               elem_classes="reel2reel-prim", scale=1)
        c["to_i2v"] = gr.Button("→ Send final cut to Img2Vid", scale=1)
    c["video"] = gr.Video(label="Rendered cut", height=420, interactive=False)
    c["save_as"] = gr.DownloadButton("Save As…", size="sm")
    c["log"] = gr.Markdown("")
    return c


def build_page(mode: str) -> dict:
    assert mode in MODES, mode
    return {"library": _library, "timeline": _timeline, "render": _render}[mode]()
