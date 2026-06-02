"""Settings sub-tab: directory roots + ffmpeg status.

Components only; plugin.py wires Save/Rescan (it owns paths + the library/project
refresh). Directories persist to ``<wan2gp_root>/.reel2reel.json``.
"""
from __future__ import annotations

import gradio as gr

from ..core import paths, render


def build_settings_panel() -> dict:
    c = {}
    with gr.Column(elem_id="r2r-settings"):
        with gr.Group():
            gr.Markdown("**Directories** — where Reel2Reel keeps projects/renders and "
                        "imports clips from (saved to `<wan2gp_root>/.reel2reel.json`).")
            c["projects_dir"] = gr.Textbox(label="Projects dir (saved timelines)",
                                           value=str(paths.projects_dir()))
            c["renders_dir"] = gr.Textbox(label="Renders dir (exported mp4)",
                                          value=str(paths.renders_dir()))
            c["wan2gp_outputs_dir"] = gr.Textbox(
                label="Import-from dir (Wan2GP outputs — read-only source)",
                value=str(paths.wan2gp_outputs_dir()))
            with gr.Row():
                c["save_dirs"] = gr.Button("Save directories", variant="primary",
                                          elem_classes="reel2reel-prim")
                c["rescan"] = gr.Button("Rescan library")
            c["dirs_status"] = gr.Markdown("")

        with gr.Row():
            with gr.Group():
                gr.Markdown("**Cache**")
                c["cache_status"] = gr.Markdown(cache_md())
                with gr.Row():
                    c["clear_renders"] = gr.Checkbox(label="also delete rendered mp4s",
                                                    value=False)
                    c["clear_cache"] = gr.Button("🧹 Clear cache")
            with gr.Group():
                gr.Markdown("**ffmpeg**")
                c["ffmpeg_status"] = gr.Markdown(ffmpeg_md())
    return c


def cache_md() -> str:
    cb, rb = paths.cache_bytes(), paths.renders_bytes()
    return (f"Thumbnails + normalized clips: **{paths.human_size(cb)}** · "
            f"Renders: **{paths.human_size(rb)}** (safe to delete; regenerated on demand)")


def ffmpeg_md() -> str:
    st = render.ffmpeg_status()
    if not st["present"]:
        return ("⚠️ **ffmpeg not found.** Install it (`apt install ffmpeg`) or set "
                "`REEL2REEL_FFMPEG=/path/to/ffmpeg`. Rendering is disabled until then.")
    probe = f" · ffprobe `{st['ffprobe']}`" if st["ffprobe"] else " · ffprobe missing (probing degraded)"
    ver = st["version"] or "ffmpeg"
    return f"✅ `{st['path']}`{probe}\n\n`{ver}`"
