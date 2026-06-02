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

MODES = ("timeline", "render")   # 'library' is now a collapsible rail inside the editor


def _library() -> dict:
    """The Library rail: three thumbnail drawers (Outputs / Project / Global), each with
    its own upload, a shared caption + refresh, and a kind-aware preview pane below.
    There is NO action bar — actions live on the thumbnails: right-click for the menu
    (Add to timeline / copy to a bin / remove + the cross-plugin items), or drag a
    thumbnail onto a track / blank canvas. Decoration + DnD live in timeline.js; the
    relay verbs (libadd / libpbin / libgbin / librm / libdrop) are handled in _on_ctx."""
    c = {"mode": "library"}

    def _bin(elem_id):     # narrow drawer — the library is a left rail
        return gr.Gallery(columns=2, height=240, object_fit="cover", preview=False,
                          allow_preview=False, show_label=False, elem_id=elem_id,
                          elem_classes="reel2reel-gallery")

    with gr.Row(elem_id="reel2reel-lib-head"):
        c["lib_selected"] = gr.Markdown("*Right-click a thumbnail for actions, or drag it "
                                        "onto a track. Press **B** to hide this panel.*")
        c["refresh"] = gr.Button("🔄", scale=0, min_width=34, elem_id="reel2reel-lib-refresh")

    with gr.Tabs(elem_id="reel2reel-lib-tabs"):
        with gr.Tab("🌐 Global"):
            c["global_gallery"] = _bin("r2r-bin-gbin")
            c["up_gbin"] = gr.UploadButton("⬆ Add to global bin", size="sm",
                                          file_count="single")
        with gr.Tab("📦 Project"):
            c["bin_gallery"] = _bin("r2r-bin-pbin")
            c["up_pbin"] = gr.UploadButton("⬆ Add to project bin", size="sm",
                                          file_count="single")
        with gr.Tab("🎞 Outputs"):
            c["gallery"] = _bin("r2r-bin-outputs")
            c["up_outputs"] = gr.UploadButton("⬆ Import a file", size="sm",
                                             file_count="single")

    # Preview pane — drawer-width; shows whichever component matches the selection.
    with gr.Group(elem_id="reel2reel-lib-preview"):
        c["prev_empty"] = gr.Markdown("*Select a clip to preview it here.*")
        c["prev_img"] = gr.Image(visible=False, show_label=False, interactive=False)
        c["prev_vid"] = gr.Video(visible=False, show_label=False, interactive=False)
        c["prev_aud"] = gr.Audio(visible=False, show_label=False, interactive=False)

    c["picked"] = gr.State(None)       # absolute path of the selected clip (any source)
    c["status"] = gr.Markdown("", visible=False)   # kept for _on_ctx / settings rescan
    return c


def build_editor() -> tuple[dict, dict]:
    """The single editing surface, returned as (timeline_dict, library_dict):
    collapsible Library rail (left) · timeline canvas (center) · collapsible clip
    inspector (right). The two dicts stay separate so the existing _wire_timeline /
    _wire_library keep operating on their own keys — no plugin wiring changes."""
    c = {"mode": "timeline"}
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

    # #r2r-stage is the STABLE host for the collapse state + injected reveal/close chrome
    # (Gradio re-renders the rails' children on every tl_to_py.change, so chrome must live
    # on the stage wrapper, not inside a rail). Default classes: library rail OPEN, clip
    # inspector COLLAPSED — a collapsed rail (display:none) lets the canvas flex-grow.
    with gr.Column(elem_id="r2r-stage", elem_classes="r2r-ins-collapsed"):
        with gr.Row():
            with gr.Column(scale=1, elem_id="reel2reel-librail"):     # the library bins
                lib = _library()
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
                    c["ins_opacity"] = gr.Slider(0, 1, value=1, step=0.05, label="Opacity",
                                                elem_id="r2r-ins-opacity")
                    with gr.Row():
                        c["ins_fade_in"] = gr.Slider(0, 5, value=0, step=0.05, label="Fade in")
                        c["ins_fade_out"] = gr.Slider(0, 5, value=0, step=0.05, label="Fade out")
                    c["ins_mute"] = gr.Checkbox(label="Mute this clip's audio")
                with gr.Accordion("Speed / time", open=False):
                    c["ins_speed"] = gr.Slider(0.1, 8, value=1, step=0.05, label="Speed")
                    c["ins_reverse"] = gr.Checkbox(label="Reverse")
                with gr.Accordion("Color", open=False):
                    c["ins_auto"] = gr.Button("✨ Auto-Enhance", size="sm")
                    c["ins_bright"] = gr.Slider(-1, 1, value=0, step=0.02, label="Brightness",
                                               elem_id="r2r-ins-bright")
                    c["ins_contrast"] = gr.Slider(0, 2, value=1, step=0.02, label="Contrast",
                                                 elem_id="r2r-ins-contrast")
                    c["ins_sat"] = gr.Slider(0, 3, value=1, step=0.02, label="Saturation",
                                            elem_id="r2r-ins-sat")
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
    return c, lib


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
                with gr.Accordion("✨ Finish — applied once to the whole cut", open=False):
                    gr.Markdown("*Each stage is optional and stacks on top of per-clip "
                                "grades; values are clamped so the master can't over-process. "
                                "Hit **Preview** to check the look before exporting.*")
                    with gr.Group():
                        c["mst_color_on"] = gr.Checkbox(label="Master colour grade")
                        c["mst_bright"] = gr.Slider(-0.5, 0.5, value=0, step=0.02, label="Brightness")
                        c["mst_contrast"] = gr.Slider(0.5, 1.6, value=1, step=0.02, label="Contrast")
                        c["mst_sat"] = gr.Slider(0, 1.8, value=1, step=0.02, label="Saturation")
                        c["mst_temp"] = gr.Slider(-1, 1, value=0, step=0.02, label="Temp (cool↔warm)")
                    with gr.Group():
                        c["mst_loud_on"] = gr.Checkbox(label="Normalize loudness", value=True)
                        c["mst_lufs"] = gr.Slider(-31, -9, value=-16, step=1,
                                                 label="Target loudness (LUFS)")
                    with gr.Group():
                        c["mst_sharpen_on"] = gr.Checkbox(label="Sharpen")
                        c["mst_sharpen"] = gr.Slider(0, 2, value=0.8, step=0.05, label="Sharpen amount")
                        c["mst_denoise_on"] = gr.Checkbox(label="Denoise")
                        c["mst_denoise"] = gr.Slider(0, 12, value=4, step=0.5, label="Denoise strength")
                    with gr.Group():
                        c["mst_interp_mode"] = gr.Dropdown(
                            ["off", "minterpolate", "rife2", "rife4"], value="off",
                            label="Frame interpolation",
                            info="rife2/rife4 = host RIFE ×2/×4 (GPU, best, short cuts); "
                                 "minterpolate = ffmpeg (any length, slower, can smear). "
                                 "Falls back to minterpolate if RIFE is unavailable.")
                        c["mst_interp_fps"] = gr.Slider(24, 120, value=60, step=1,
                                                       label="Target fps (minterpolate only)")
                    with gr.Group():
                        c["mst_lut_on"] = gr.Checkbox(label="Apply 3D LUT (.cube)")
                        with gr.Row():
                            c["mst_lut"] = gr.UploadButton("⬆ Load .cube", size="sm",
                                                          file_types=[".cube"], file_count="single")
                            c["mst_lut_name"] = gr.Markdown("*no LUT loaded*")
                        c["mst_lut_path"] = gr.State("")
                    with gr.Group():
                        gr.Markdown("**Consistency** — even out per-clip grades so the "
                                    "master doesn't over-process some shots")
                        with gr.Row():
                            c["mst_check"] = gr.Button("🔍 Check", scale=0)
                            c["mst_match_all"] = gr.Button("Apply selected grade to all", scale=1)
                            c["mst_clear_all"] = gr.Button("Clear all clip grades", scale=1)
                        c["mst_status"] = gr.Markdown("")
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
                        c["to_i2v"] = gr.Button("→ Send final cut to Vid2Vid", scale=1)
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
    """Standalone per-tab pages. Only Render now — the editor (timeline + library
    rail + inspector) is built together by build_editor()."""
    assert mode == "render", mode
    return _render()
