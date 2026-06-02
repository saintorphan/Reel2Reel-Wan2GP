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
    gr.Markdown("### Library")

    # --- global media bin (cross-project, persistent) ---
    with gr.Accordion("🌐 Library (global) — reusable across all projects", open=False):
        c["global_gallery"] = gr.Gallery(label="Global bin", columns=6, height=180,
                                        object_fit="cover", preview=False, allow_preview=False)
        c["global_picked"] = gr.State(None)
        with gr.Row():
            c["global_kind"] = gr.Radio(["auto", "Video", "Audio"], value="auto",
                                       label="Add to track", scale=1)
            c["global_add_tl"] = gr.Button("➕ Add to timeline", variant="primary",
                                          scale=1, elem_classes="reel2reel-prim")
            c["global_remove"] = gr.Button("✖ Remove", scale=1)

    # --- per-project media bin ---
    with gr.Accordion("📦 Library (project) — media for the open project", open=True):
        gr.Markdown("Right-click any clip in the app → *Reel2Reel Library (global/project)* "
                    "drops it into these bins; pick one and **Add to timeline**.")
        c["bin_gallery"] = gr.Gallery(label="Project bin", columns=6, height=180,
                                     object_fit="cover", preview=False, allow_preview=False)
        c["bin_picked"] = gr.State(None)
        with gr.Row():
            c["bin_kind"] = gr.Radio(["auto", "Video", "Audio"], value="auto",
                                    label="Add to track", scale=1)
            c["bin_add_tl"] = gr.Button("➕ Add to timeline", variant="primary",
                                       scale=1, elem_classes="reel2reel-prim")
            c["bin_remove"] = gr.Button("✖ Remove", scale=1)

    # --- global outputs browser ---
    gr.Markdown("#### Outputs browser\nNewest first, from the Wan2GP outputs folder "
                "(and your renders).")
    with gr.Row():
        c["refresh"] = gr.Button("🔄 Refresh", scale=0)
        c["kind"] = gr.Radio(["auto", "Video", "Audio"], value="auto",
                             label="Add to track", scale=1)
        c["add"] = gr.Button("➕ Add to timeline", variant="primary", scale=1,
                            elem_classes="reel2reel-prim")
        c["add_gbin"] = gr.Button("🌐 To global", scale=1)
        c["add_pbin"] = gr.Button("📦 To project", scale=1)
    c["gallery"] = gr.Gallery(label="Outputs", columns=4, height=360,
                             object_fit="cover", preview=False,
                             elem_classes="reel2reel-gallery", allow_preview=False)
    c["picked"] = gr.State(None)       # absolute path of the selected output
    c["status"] = gr.Markdown("")
    return c


def _timeline() -> dict:
    c = {"mode": "timeline"}
    gr.Markdown("### Timeline\n"
                "Drag to move, drag an edge to trim, click the ruler to scrub, "
                "**Space** to play. Select a clip and edit it below. Preview is "
                "approximate — **export** for the real cut.")
    with gr.Row():
        c["undo"] = gr.Button("↶ Undo", scale=0, elem_id="r2r-undo")
        c["redo"] = gr.Button("↷ Redo", scale=0, elem_id="r2r-redo")
        c["split"] = gr.Button("✂ Split", scale=0, elem_id="r2r-split")
        c["add_video"] = gr.Button("➕ Video track", scale=0)
        c["add_audio"] = gr.Button("➕ Audio track", scale=0)
    widget = timeline_widget.build_timeline_widget()
    c.update(widget)                   # mount, tl_to_py, tl_from_py

    with gr.Accordion("Selected clip", open=True):
        c["ins_label"] = gr.Textbox(label="Label")
        with gr.Row():
            c["ins_gain"] = gr.Slider(-40, 12, value=0, step=0.5, label="Gain (dB)")
            c["ins_opacity"] = gr.Slider(0, 1, value=1, step=0.05, label="Opacity (overlay)")
        with gr.Row():
            c["ins_fade_in"] = gr.Slider(0, 5, value=0, step=0.05, label="Fade in (s)")
            c["ins_fade_out"] = gr.Slider(0, 5, value=0, step=0.05, label="Fade out (s)")
        c["ins_mute"] = gr.Checkbox(label="Mute this clip's audio")
        c["ins_apply"] = gr.Button("Apply to selected clip", variant="primary",
                                   elem_classes="reel2reel-prim")
        with gr.Row():
            c["ins_detach"] = gr.Button("🎙 Detach audio")
            c["ins_dup"] = gr.Button("⧉ Duplicate")
            c["ins_ripple"] = gr.Button("⇤ Ripple delete", elem_id="r2r-ripple")
            c["ins_delete"] = gr.Button("🗑 Delete (lift)")
        with gr.Row():
            c["trans_dur"] = gr.Slider(0.1, 3, value=0.5, step=0.1, label="Dissolve (s)")
            c["trans_add"] = gr.Button("⇆ Add dissolve → next")
            c["trans_rm"] = gr.Button("Remove transition")

    with gr.Accordion("Tracks", open=False):
        c["trk_dd"] = gr.Dropdown(label="Track", choices=[])
        c["trk_name"] = gr.Textbox(label="Track name")
        c["trk_volume"] = gr.Slider(-40, 12, value=0, step=0.5, label="Track volume (dB, audio)")
        with gr.Row():
            c["trk_mute"] = gr.Checkbox(label="Mute")
            c["trk_solo"] = gr.Checkbox(label="Solo")
            c["trk_lock"] = gr.Checkbox(label="Lock")
        with gr.Row():
            c["trk_apply"] = gr.Button("Apply to track", variant="primary",
                                       elem_classes="reel2reel-prim")
            c["trk_del"] = gr.Button("Delete track")
            c["trk_up"] = gr.Button("▲ Up")
            c["trk_down"] = gr.Button("▼ Down")

    with gr.Accordion("📁 Project — save · versions", open=False):
        c["current_lbl"] = gr.Markdown("*No project open — use **Save as** to name one.*")
        with gr.Row():
            c["proj_dd"] = gr.Dropdown(label="Open project", choices=[], scale=2)
            c["open"] = gr.Button("Open", scale=0)
            c["delete"] = gr.Button("🗑 Delete", scale=0)
        c["proj_name"] = gr.Textbox(label="Name (for New / Save as / Rename / Duplicate)",
                                    scale=2)
        with gr.Row():
            c["new"] = gr.Button("New")
            c["save"] = gr.Button("💾 Save")
            c["saveas"] = gr.Button("Save as")
            c["rename"] = gr.Button("Rename")
            c["dup"] = gr.Button("Duplicate")
        gr.Markdown("**Versions** — manual named snapshots")
        with gr.Row():
            c["ver_label"] = gr.Textbox(label="Snapshot name", scale=2)
            c["snapshot"] = gr.Button("📸 Snapshot")
        with gr.Row():
            c["ver_dd"] = gr.Dropdown(label="Versions", choices=[], scale=2)
            c["restore"] = gr.Button("Restore")
            c["delver"] = gr.Button("Delete version")
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
