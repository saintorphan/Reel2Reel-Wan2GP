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
        c["add_title"] = gr.Button("🆃 Title", scale=0)
        c["add_marker"] = gr.Button("🚩 Marker", scale=0)
        c["add_video"] = gr.Button("➕ Video track", scale=0)
        c["add_audio"] = gr.Button("➕ Audio track", scale=0)
    widget = timeline_widget.build_timeline_widget()
    c.update(widget)                   # mount, tl_to_py, tl_from_py

    with gr.Accordion("Selected clip", open=True):
        c["ins_label"] = gr.Textbox(label="Label / title text")
        with gr.Row():
            c["ins_gain"] = gr.Slider(-40, 12, value=0, step=0.5, label="Gain (dB)")
            c["ins_speed"] = gr.Slider(0.1, 8, value=1, step=0.05, label="Speed")
            c["ins_reverse"] = gr.Checkbox(label="Reverse")
        with gr.Row():
            c["ins_fade_in"] = gr.Slider(0, 5, value=0, step=0.05, label="Fade in (s)")
            c["ins_fade_out"] = gr.Slider(0, 5, value=0, step=0.05, label="Fade out (s)")
            c["ins_opacity"] = gr.Slider(0, 1, value=1, step=0.05, label="Opacity")
        c["ins_mute"] = gr.Checkbox(label="Mute this clip's audio")
        with gr.Accordion("Color", open=False):
            with gr.Row():
                c["ins_bright"] = gr.Slider(-1, 1, value=0, step=0.02, label="Brightness")
                c["ins_contrast"] = gr.Slider(0, 2, value=1, step=0.02, label="Contrast")
            with gr.Row():
                c["ins_sat"] = gr.Slider(0, 3, value=1, step=0.02, label="Saturation")
                c["ins_gamma"] = gr.Slider(0.1, 3, value=1, step=0.02, label="Gamma")
        with gr.Accordion("Transform (position / scale)", open=False):
            with gr.Row():
                c["ins_tx"] = gr.Textbox(label="X (px or center)", value="center")
                c["ins_ty"] = gr.Textbox(label="Y (px or center)", value="center")
            with gr.Row():
                c["ins_scale"] = gr.Slider(0.05, 4, value=1, step=0.05, label="Scale")
                c["ins_rotate"] = gr.Slider(-180, 180, value=0, step=1, label="Rotate °")
        c["ins_apply"] = gr.Button("Apply to selected clip", variant="primary",
                                   elem_classes="reel2reel-prim")
        with gr.Row():
            c["ins_detach"] = gr.Button("🎙 Detach audio")
            c["ins_dup"] = gr.Button("⧉ Duplicate")
            c["ins_ripple"] = gr.Button("⇤ Ripple delete", elem_id="r2r-ripple")
            c["ins_delete"] = gr.Button("🗑 Delete (lift)")
        with gr.Row():
            c["trans_kind"] = gr.Dropdown(
                ["dissolve", "fade_black", "fade_white", "wipe", "slide"],
                value="dissolve", label="Transition", scale=1)
            c["trans_dir"] = gr.Dropdown(["left", "right", "up", "down"], value="left",
                                        label="Direction", scale=1)
            c["trans_dur"] = gr.Slider(0.1, 3, value=0.5, step=0.1, label="Duration", scale=1)
        with gr.Row():
            c["trans_add"] = gr.Button("⇆ Add transition → next")
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
            c["restore_auto"] = gr.Button("↺ Restore autosave")
        gr.Markdown("**Versions** — manual named snapshots")
        with gr.Row():
            c["ver_label"] = gr.Textbox(label="Snapshot name", scale=2)
            c["snapshot"] = gr.Button("📸 Snapshot")
        with gr.Row():
            c["ver_dd"] = gr.Dropdown(label="Versions", choices=[], scale=2)
            c["restore"] = gr.Button("Restore")
            c["delver"] = gr.Button("Delete version")
        gr.Markdown("**Interchange** — OpenTimelineIO (Resolve / Premiere / FCPXML)")
        with gr.Row():
            c["otio_export"] = gr.DownloadButton("⬇ Export .otio", size="sm")
            c["otio_import"] = gr.UploadButton("⬆ Import .otio", file_types=[".otio", ".json"],
                                              size="sm")
    c["status"] = gr.Markdown("")
    return c


def _render() -> dict:
    c = {"mode": "render"}
    gr.Markdown("### Render\n"
                "Composite the timeline with ffmpeg (transitions, fades, speed, "
                "color, titles, PiP). Pick a format and quality; export the whole "
                "timeline or a range.")
    with gr.Row():
        c["preset"] = gr.Dropdown(["mp4", "webm", "prores", "gif"], value="mp4",
                                 label="Format", scale=1)
        c["quality"] = gr.Dropdown(["high", "medium", "low"], value="high",
                                  label="Quality", scale=1)
        c["resolution"] = gr.Dropdown(
            ["timeline", "1920x1080", "1280x720", "1080x1080", "720x1280", "854x480"],
            value="timeline", label="Resolution", scale=1)
    with gr.Row():
        c["range_on"] = gr.Checkbox(label="Export range only", scale=0)
        c["range_start"] = gr.Number(label="Start (s)", value=0, scale=1)
        c["range_end"] = gr.Number(label="End (s)", value=0, scale=1)
    with gr.Row():
        c["export"] = gr.Button("🎬 Export", variant="primary",
                               elem_classes="reel2reel-prim", scale=1)
        c["cancel"] = gr.Button("✖ Cancel", scale=0)
        c["to_i2v"] = gr.Button("→ Send final cut to Img2Vid", scale=1)
    gr.Markdown("**Preview** renders a low-res composite window at the playhead — "
                "the true cut (transitions, overlays, audio), unlike the approximate "
                "scrub preview on the Timeline tab.")
    with gr.Row():
        c["preview"] = gr.Button("👁 Preview at playhead", scale=1)
        c["preview_secs"] = gr.Slider(2, 30, value=8, step=1, label="Window (s)", scale=1)
    c["video"] = gr.Video(label="Rendered cut / preview", height=420, interactive=False)
    c["save_as"] = gr.DownloadButton("Save As…", size="sm")
    c["log"] = gr.Markdown("")
    return c


def build_page(mode: str) -> dict:
    assert mode in MODES, mode
    return {"library": _library, "timeline": _timeline, "render": _render}[mode]()
