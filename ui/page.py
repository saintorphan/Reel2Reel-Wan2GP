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

MODES = ("timeline", "library", "render")


def _library() -> dict:
    c = {"mode": "library"}

    def _bin(label):
        return gr.Gallery(columns=5, height=300, object_fit="cover", preview=False,
                          allow_preview=False, show_label=False,
                          elem_classes="reel2reel-gallery", label=label)

    # One source-switcher → three uniform galleries, all feeding ONE shared picker.
    with gr.Tabs(elem_id="reel2reel-lib-tabs"):
        with gr.Tab("🎞 Outputs"):
            gr.Markdown("Newest first, from the Wan2GP outputs folder (and your renders).")
            c["gallery"] = _bin("Outputs")
        with gr.Tab("📦 Project bin"):
            gr.Markdown("Media saved with the open project. Right-click any clip in the "
                        "app → *Reel2Reel Library (project)* to drop it here.")
            c["bin_gallery"] = _bin("Project bin")
        with gr.Tab("🌐 Global bin"):
            gr.Markdown("Reusable across every project. Right-click → "
                        "*Reel2Reel Library (global)*.")
            c["global_gallery"] = _bin("Global bin")

    # One action bar for whatever is selected, regardless of source tab.
    with gr.Row(elem_id="reel2reel-lib-actions"):
        c["lib_selected"] = gr.Markdown("*No clip selected*")
        c["kind"] = gr.Radio(["auto", "Video", "Audio"], value="auto", label="Add as",
                             scale=0)
        c["add"] = gr.Button("➕ Add to timeline", variant="primary", scale=0,
                            elem_classes="reel2reel-prim")
        c["add_pbin"] = gr.Button("📦 To project", scale=0)
        c["add_gbin"] = gr.Button("🌐 To global", scale=0)
        c["refresh"] = gr.Button("🔄 Refresh", scale=0)
    with gr.Row(elem_classes="reel2reel-lib-danger"):
        c["bin_remove"] = gr.Button("Remove from project", scale=0)
        c["global_remove"] = gr.Button("Remove from global", scale=0)
    c["picked"] = gr.State(None)       # absolute path of the selected clip (any source)
    c["status"] = gr.Markdown("")
    return c


def _timeline() -> dict:
    c = {"mode": "timeline"}
    gr.Markdown("### Timeline\n"
                "Drag to move, drag an edge to trim, click the ruler to scrub, "
                "**Space** to play, **R** razor. **Double-click a clip** to open it "
                "in the inspector on the right (**?** for all shortcuts).")
    # Host-action buttons: demoted to a CSS-hidden Row (#r2r-host-tools). They stay
    # in the DOM so the in-canvas toolbar + keyboard shortcuts can fire them by
    # elem_id via the clickGr('#id button') bridge — visible=False would drop them.
    with gr.Row(elem_id="r2r-host-tools"):
        c["undo"] = gr.Button("↶ Undo", scale=0, elem_id="r2r-undo")
        c["redo"] = gr.Button("↷ Redo", scale=0, elem_id="r2r-redo")
        c["split"] = gr.Button("✂ Split", scale=0, elem_id="r2r-split")
        c["add_title"] = gr.Button("🆃 Title", scale=0, elem_id="r2r-title")
        c["add_marker"] = gr.Button("🚩 Marker", scale=0, elem_id="r2r-marker")
        c["add_video"] = gr.Button("➕ Video track", scale=0, elem_id="r2r-addv")
        c["add_audio"] = gr.Button("➕ Audio track", scale=0, elem_id="r2r-adda")
        # Track management lives on the timeline track heads now (inline M/S/L,
        # double-click rename, right-click menu). trk_dd stays here, hidden, only
        # because many handlers refresh their track-choices into it.
        c["trk_dd"] = gr.Dropdown(label="Track", choices=[])

    # #r2r-stage is the STABLE host for the collapse state + injected >>/reveal chrome
    # (Gradio re-renders the inspector's children on every tl_to_py.change, so chrome
    # must live on the stage wrapper, not inside the inspector). Default-collapsed:
    # the inspector hides and the scale=3 canvas flex-grows to the full row width.
    with gr.Column(elem_id="r2r-stage", elem_classes="r2r-ins-collapsed"):
        with gr.Row():
            with gr.Column(scale=3):                       # the timeline canvas
                widget = timeline_widget.build_timeline_widget()
                c.update(widget)                           # mount, tl_to_py, tl_from_py
            with gr.Column(scale=1, elem_id="reel2reel-inspector"):   # the clip inspector
                gr.Markdown("#### 🎬 Clip")
                c["clip_preview"] = gr.Video(label="Preview", height=320, interactive=False)
                c["clip_info"] = gr.Markdown("*Double-click a clip to inspect it.*")
                c["ins_label"] = gr.Textbox(label="Label / title text")
                with gr.Accordion("Basics", open=True):
                    c["ins_gain"] = gr.Slider(-40, 12, value=0, step=0.5, label="Gain (dB)")
                    c["ins_opacity"] = gr.Slider(0, 1, value=1, step=0.05, label="Opacity")
                    with gr.Row():
                        c["ins_fade_in"] = gr.Slider(0, 5, value=0, step=0.05, label="Fade in")
                        c["ins_fade_out"] = gr.Slider(0, 5, value=0, step=0.05, label="Fade out")
                    c["ins_mute"] = gr.Checkbox(label="Mute this clip's audio")
                with gr.Accordion("Speed / time", open=False):
                    c["ins_speed"] = gr.Slider(0.1, 8, value=1, step=0.05, label="Speed")
                    c["ins_reverse"] = gr.Checkbox(label="Reverse")
                with gr.Accordion("Color", open=False):
                    c["ins_auto"] = gr.Button("✨ Auto-Enhance", size="sm")
                    c["ins_bright"] = gr.Slider(-1, 1, value=0, step=0.02, label="Brightness")
                    c["ins_contrast"] = gr.Slider(0, 2, value=1, step=0.02, label="Contrast")
                    c["ins_sat"] = gr.Slider(0, 3, value=1, step=0.02, label="Saturation")
                    c["ins_gamma"] = gr.Slider(0.1, 3, value=1, step=0.02, label="Gamma")
                    with gr.Row():
                        c["ins_temp"] = gr.Slider(-1, 1, value=0, step=0.02,
                                                 label="Temp (cool↔warm)")
                        c["ins_tint"] = gr.Slider(-1, 1, value=0, step=0.02,
                                                 label="Tint (green↔magenta)")
                    with gr.Row():
                        c["ins_match_ref"] = gr.Dropdown(choices=[], label="Match color to…",
                                                        scale=2)
                        c["ins_match"] = gr.Button("🎯 Match", scale=1)
                with gr.Accordion("Transform / crop", open=False):
                    with gr.Row():
                        c["ins_tx"] = gr.Textbox(label="X (px/center)", value="center")
                        c["ins_ty"] = gr.Textbox(label="Y (px/center)", value="center")
                    c["ins_scale"] = gr.Slider(0.05, 4, value=1, step=0.05, label="Scale (resize/zoom)")
                    c["ins_rotate"] = gr.Slider(-180, 180, value=0, step=1, label="Rotate °")
                    c["ins_fit"] = gr.Dropdown(["fit", "fill", "stretch"], value="fit",
                                              label="Fit (fill = crop-to-fit)")
                    c["ins_crop"] = gr.Slider(0, 0.45, value=0, step=0.01, label="Crop / zoom-in")
                c["ins_apply"] = gr.Button("Apply", variant="primary",
                                           elem_classes="reel2reel-prim")
                with gr.Row():
                    c["ins_detach"] = gr.Button("🎙 Detach", scale=1)
                    c["ins_dup"] = gr.Button("⧉ Dup", elem_id="r2r-dup", scale=1)
                    c["ins_ripple"] = gr.Button("⇤ Ripple", elem_id="r2r-ripple", scale=1)
                    c["ins_delete"] = gr.Button("🗑 Del", elem_id="r2r-lift", scale=1)
                with gr.Accordion("Transition → next clip", open=False):
                    c["trans_kind"] = gr.Dropdown(
                        ["dissolve", "fade_black", "fade_white", "wipe", "slide"],
                        value="dissolve", label="Transition")
                    c["trans_dir"] = gr.Dropdown(["left", "right", "up", "down"], value="left",
                                                label="Direction")
                    c["trans_dur"] = gr.Slider(0.1, 3, value=0.5, step=0.1, label="Duration")
                    with gr.Row():
                        c["trans_add"] = gr.Button("⇆ Add")
                        c["trans_rm"] = gr.Button("Remove")

    # Track management (rename / mute / solo / lock / volume / delete / reorder)
    # is on the track heads in the timeline canvas — see assets/static/timeline.js.
    # Project / version CRUD lives in the persistent suite-level bar (above the
    # sub-tabs, visible on every page) — see ui/suite.py _projbar().
    c["status"] = gr.Markdown("")
    return c


def _render() -> dict:
    c = {"mode": "render"}
    with gr.Column(elem_id="r2r-render"):
        with gr.Row():
            # left: compact grouped controls; right: big sticky preview
            with gr.Column(scale=1, elem_id="r2r-render-controls"):
                with gr.Group():
                    gr.Markdown("**Output**")
                    c["preset"] = gr.Dropdown(["mp4", "webm", "prores", "gif"],
                                             value="mp4", label="Format")
                    c["quality"] = gr.Dropdown(["high", "medium", "low"], value="high",
                                              label="Quality")
                    c["resolution"] = gr.Dropdown(
                        ["timeline", "1920x1080", "1280x720", "1080x1080",
                         "720x1280", "854x480"],
                        value="timeline", label="Resolution")
                with gr.Group():
                    c["range_on"] = gr.Checkbox(label="Export range only")
                    with gr.Row(elem_classes="r2r-range-row"):
                        c["range_start"] = gr.Number(label="Start (s)", value=0)
                        c["range_end"] = gr.Number(label="End (s)", value=0)
                with gr.Group():
                    c["export"] = gr.Button("🎬 Export", variant="primary",
                                           elem_classes="reel2reel-prim")
                    with gr.Row():
                        c["cancel"] = gr.Button("✖ Cancel", scale=0)
                        c["to_i2v"] = gr.Button("→ Final cut to Img2Vid", scale=1)
                    with gr.Row():
                        c["preview"] = gr.Button("👁 Preview at playhead", scale=2)
                        c["preview_secs"] = gr.Slider(2, 30, value=8, step=1,
                                                     label="Window (s)", scale=1)
                    c["save_as"] = gr.DownloadButton("Save As…", size="sm")
                    c["log"] = gr.Markdown("")
            with gr.Column(scale=2, elem_id="r2r-render-preview"):
                gr.Markdown("**Preview** = true composite (transitions, overlays, audio) "
                            "of a window at the playhead — unlike the approximate scrub "
                            "preview on the Timeline tab.")
                c["video"] = gr.Video(label="Rendered cut / preview", interactive=False,
                                     elem_classes="r2r-render-video")
    return c


def build_page(mode: str) -> dict:
    assert mode in MODES, mode
    return {"library": _library, "timeline": _timeline, "render": _render}[mode]()
