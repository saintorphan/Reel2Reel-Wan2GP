"""Reel2Reel — a Wan2GP plugin.

A non-linear, multi-track timeline video editor rendered as one main-webui tab
with four sub-tabs: Library, Timeline, Render and Settings. It composites
*existing* clips (AI-generated elsewhere in Wan2GP and "sent" here, or imported
from the outputs folder) with ffmpeg — it never generates frames, so it needs
none of the submit_task / model machinery.

Clips arrive two ways: the Library tab browses the Wan2GP outputs folder, and any
other tab can push clips with ``reel2reel.inbox.enqueue_clips(state, path)`` then
navigate here — ``on_tab_select`` drains the inbox onto the timeline with no
button press. The browser timeline (assets/static/timeline.js) round-trips its
edit state through two hidden gr.Textbox JSON pipes.

NOTE: not an official plugin. Distribute via the plugin-manager "add from GitHub
URL" flow; do not add to the bundled plugins.json without dbm's approval.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from pathlib import Path

import gradio as gr

from shared.utils.plugins import WAN2GPPlugin

from .core import discovery, inbox, paths, render, timeline
from .ui import logo, settings_panel, suite
from .ui import timeline_widget as tw
from .ui.styles import CSS

logger = logging.getLogger("reel2reel.plugin")

PLUGIN_ID = "Reel2Reel"
PLUGIN_NAME = "Reel2Reel"


class Reel2Reel(WAN2GPPlugin):
    def __init__(self):
        super().__init__()
        self.name = PLUGIN_NAME
        self.version = "0.1.0"
        self.description = ("Multi-track timeline editor: send AI clips to a "
                            "Library, arrange them on video/audio tracks, and "
                            "export a final cut with ffmpeg.")
        self._project = timeline.Timeline()
        self._seq = 0
        self._library: list[dict] = []
        self._last_render: str | None = None

    # -- lifecycle ----------------------------------------------------------
    def setup_ui(self):
        try:
            paths.ensure_dirs()
        except Exception:
            traceback.print_exc()

        # The timeline JS module is delivered through Gradio's on-load js= hook.
        js = tw.timeline_js()
        if js:
            self.add_custom_js(js)
        # Let Gradio serve our assets / renders / thumbs / source clips by path.
        try:
            tw.register_static_paths([
                paths.renders_dir(), paths.thumbs_dir(), paths.norm_dir(),
                paths.wan2gp_outputs_dir(),
            ])
        except Exception:
            traceback.print_exc()

        self.request_component("state")
        self.request_component("main_tabs")
        self.request_component("refresh_form_trigger")
        # Optional (host-dependent) — used by "Send final cut to Img2Vid".
        self.request_component("image_start")
        self.request_component("image_start_row")
        self.request_component("image_prompt_type_radio")
        self.request_global("server_config")
        self.request_global("get_video_info")
        self.request_global("get_video_frame")
        self.request_global("get_current_model_settings")

        self.add_tab(tab_id=PLUGIN_ID, label=PLUGIN_NAME,
                     component_constructor=self.create_ui)

    # -- UI -----------------------------------------------------------------
    def create_ui(self, api_session=None):
        # MUST default api_session to None: the local host calls the constructor
        # with zero args, the newer session-API host passes a session. Reel2Reel
        # composites with ffmpeg and never submits a task, so this stays unused.
        self._api = api_session

        gr.HTML(f"<style>{CSS}</style>", elem_classes="reel2reel-hidden")
        # Tag our main-webui tab button so the amber accent CSS targets only us.
        gr.HTML(
            "<img src=x style='display:none' onerror=\"(function(){"
            "var NAME=" + repr(PLUGIN_NAME) + ";"
            "function mark(){document.querySelectorAll("
            "'.tab-nav button,button[role=&quot;tab&quot;]').forEach(function(b){"
            "if(b.textContent.trim()===NAME)b.classList.add('reel2reel-tabbtn');});}"
            "mark();new MutationObserver(mark).observe(document.body,"
            "{childList:true,subtree:true});})()\">",
            elem_classes="reel2reel-hidden")

        with gr.Column(elem_id="reel2reel-root"):
            gr.HTML(logo.banner_html())
            ui = suite.build_suite()

        # Capture the bridge components for wiring + on_tab_outputs.
        self.tl_to_py = ui["pages"]["timeline"]["tl_to_py"]
        self.tl_from_py = ui["pages"]["timeline"]["tl_from_py"]
        self._wire(ui)
        # When this tab is selected, on_tab_select returns an op-envelope into
        # tl_from_py, whose .change js-hook injects it into the browser timeline.
        self.on_tab_outputs = [self.tl_from_py]
        return ui

    # -- inbox: drain queued clips onto the timeline on every tab entry ------
    def on_tab_select(self, state: dict):
        try:
            for p in inbox.drain(state):
                self._ingest_clip(p)
        except Exception:
            traceback.print_exc()
        return self._load_envelope()

    # -- helpers ------------------------------------------------------------
    def _server_config(self):
        sc = getattr(self, "server_config", None)
        return sc if isinstance(sc, dict) else None

    def _edit_payload(self) -> dict:
        """The flat edit-state with browser-servable URLs injected."""
        edit = self._project.to_edit_json()
        for c in edit["clips"]:
            c["url"] = tw.file_url(c.get("src"))
            c["thumb_url"] = tw.file_url(c.get("thumb")) if c.get("thumb") else None
        return edit

    def _load_envelope(self) -> str:
        self._seq += 1
        return json.dumps({"seq": self._seq, "op": "load", "edit": self._edit_payload()})

    def _ingest_clip(self, path: str, force_kind: str = "auto") -> timeline.Clip | None:
        if not path or not Path(path).exists():
            return None
        k = discovery.kind_of(path) or "video"
        track_kind = "Audio" if (k == "audio" or force_kind == "Audio") else "Video"
        info = discovery.probe_clip(path, getattr(self, "get_video_info", None))
        dur = info.get("dur") or 5.0
        clip = self._project.append_clip(
            path, kind=track_kind, in_=0.0, out=float(dur),
            src_dur=info.get("dur"), src_fps=info.get("fps"), label=Path(path).stem)
        try:
            clip.thumb = discovery.thumbnail(path, clip.id,
                                             getattr(self, "get_video_frame", None))
        except Exception:
            clip.thumb = None
        return clip

    # -- wiring -------------------------------------------------------------
    def _wire(self, ui):
        pages = ui["pages"]
        subtabs, tab_ids = ui["subtabs"], ui["tab_ids"]
        self._wire_bridge()
        self._wire_library(pages["library"], subtabs, tab_ids)
        self._wire_timeline(pages["timeline"])
        self._wire_render(pages["render"])
        self._wire_settings(ui["settings"], pages)

    def _wire_bridge(self):
        # Browser -> Python: persist the edited timeline as it changes.
        self.tl_to_py.change(self._on_timeline_change, inputs=[self.tl_to_py],
                             outputs=[], show_progress="hidden")
        # Python -> browser: run the JS applyOp hook (no Python fn, no feedback loop).
        self.tl_from_py.change(fn=None, inputs=[self.tl_from_py], outputs=[],
                              js=tw.APPLY_OP_JS, show_progress="hidden")

    def _on_timeline_change(self, payload: str):
        if not payload:
            return
        try:
            self._project = timeline.Timeline.from_edit_json(json.loads(payload))
        except Exception:
            logger.debug("bad timeline payload", exc_info=True)

    # -- library ------------------------------------------------------------
    def _wire_library(self, c, subtabs, tab_ids):
        c["refresh"].click(self._refresh_library, outputs=[c["gallery"], c["status"]])
        c["gallery"].select(self._on_pick, outputs=[c["picked"]])
        c["add"].click(self._add_to_timeline, inputs=[c["picked"], c["kind"]],
                      outputs=[self.tl_from_py, subtabs, c["status"]])

    def _refresh_library(self):
        self._library = []
        items = discovery.list_importable(self._server_config())
        gallery = []
        for it in items:
            thumb = discovery.thumbnail(it["path"], f"lib_{abs(hash(it['path'])) % 10**8}",
                                        getattr(self, "get_video_frame", None))
            it = {**it, "thumb": thumb}
            self._library.append(it)
            gallery.append((thumb or it["path"], it["name"]))
        msg = f"Found {len(items)} clip(s)." if items else "No clips in the outputs folder yet."
        return gallery, msg

    def _on_pick(self, evt: gr.SelectData):
        try:
            return self._library[evt.index]["path"]
        except Exception:
            return None

    def _add_to_timeline(self, picked, kind):
        if not picked:
            raise gr.Error("Select a clip in the Library first.")
        self._ingest_clip(picked, force_kind=kind if kind in ("Video", "Audio") else "auto")
        return (self._load_envelope(), gr.update(selected=tab_ids_timeline()),
                f"Added **{Path(picked).stem}** to the timeline.")

    # -- timeline toolbar + projects ----------------------------------------
    def _wire_timeline(self, c):
        c["split"].click(self._split, outputs=[self.tl_from_py, c["status"]])
        c["add_video"].click(lambda: self._add_track("Video"),
                            outputs=[self.tl_from_py, c["status"]])
        c["add_audio"].click(lambda: self._add_track("Audio"),
                            outputs=[self.tl_from_py, c["status"]])
        c["remove_sel"].click(self._remove_selected,
                            outputs=[self.tl_from_py, c["status"]])
        c["new"].click(self._new_project, inputs=[c["proj_name"]],
                      outputs=[self.tl_from_py, c["load_name"], c["status"]])
        c["save"].click(self._save_project, inputs=[c["proj_name"]],
                       outputs=[c["load_name"], c["status"]])
        c["load"].click(self._load_project, inputs=[c["load_name"]],
                       outputs=[self.tl_from_py, c["proj_name"], c["status"]])
        # Prime the open-project dropdown.
        self._load_name = c["load_name"]

    def _split(self):
        ui = self._project.ui or {}
        t = float(ui.get("playhead", 0.0))
        sel = ui.get("selected")
        created = []
        if sel:
            track, _ = self._project.find_clip(sel)
            if track:
                created = self._project.split_at(track.id, t)
        else:
            for trk in self._project.video_tracks():
                created += self._project.split_at(trk.id, t)
        msg = f"Split at {t:.2f}s ({len(created)} new clip(s))." if created else \
            "Nothing to split at the playhead."
        return self._load_envelope(), msg

    def _add_track(self, kind):
        trk = self._project.add_track(kind, "")
        return self._load_envelope(), f"Added track **{trk.name}**."

    def _remove_selected(self):
        sel = (self._project.ui or {}).get("selected")
        if sel and self._project.remove_clip(sel):
            return self._load_envelope(), "Removed selected clip."
        return self._load_envelope(), "No clip selected."

    def _new_project(self, name):
        self._project = timeline.Timeline(name=name or "Cut 1")
        return (self._load_envelope(), gr.update(choices=paths.list_projects()),
                f"New project **{self._project.name}**.")

    def _save_project(self, name):
        self._project.name = name or self._project.name
        try:
            p = timeline.save(paths.project_path(self._project.name), self._project)
        except Exception as e:
            return gr.update(), f"⚠️ Could not save: {e}"
        return gr.update(choices=paths.list_projects()), f"Saved `{p}`."

    def _load_project(self, name):
        if not name:
            raise gr.Error("Pick a project to open.")
        try:
            self._project = timeline.load(paths.project_path(name))
        except Exception as e:
            raise gr.Error(f"Could not open '{name}': {e}")
        return self._load_envelope(), self._project.name, f"Opened **{name}**."

    # -- render -------------------------------------------------------------
    def _wire_render(self, c):
        c["export"].click(self._render, outputs=[c["video"], c["save_as"], c["log"]])
        present = [x for x in (getattr(self, "image_start", None),
                               getattr(self, "image_prompt_type_radio", None),
                               getattr(self, "image_start_row", None),
                               getattr(self, "main_tabs", None)) if x is not None]
        if present:
            c["to_i2v"].click(self._send_to_img2vid, outputs=present)
        else:
            # Host doesn't expose the Img2Vid start-frame components — just guide.
            c["to_i2v"].click(self._img2vid_unavailable, outputs=[c["log"]])

    def _img2vid_unavailable(self):
        if not self._last_render:
            return "Export a cut first, then open the Video Generator to use it."
        return (f"Open the Video Generator and load `{self._last_render}` as the "
                "Img2Vid start frame (this host doesn't expose the start-frame slot "
                "for a one-click hand-off).")

    def _render(self, progress=gr.Progress()):
        try:
            out = render.export(self._project,
                                progress_cb=lambda f, d: progress(f, desc=d))
        except render.RenderError as e:
            return gr.update(), gr.update(), f"❌ {e}"
        except Exception as e:
            traceback.print_exc()
            return gr.update(), gr.update(), f"❌ Render failed: {e}"
        self._last_render = out
        return out, gr.update(value=out), f"✅ Rendered `{out}`"

    def _send_to_img2vid(self):
        if not self._last_render:
            gr.Warning("Export a cut first.")
            return {}
        image_start = getattr(self, "image_start", None)
        radio = getattr(self, "image_prompt_type_radio", None)
        row = getattr(self, "image_start_row", None)
        main_tabs = getattr(self, "main_tabs", None)
        out: dict = {}
        frame = None
        gvf = getattr(self, "get_video_frame", None)
        if callable(gvf):
            try:
                frame = gvf(self._last_render, 0)
            except Exception:
                frame = None
        if image_start is not None and frame is not None:
            out[image_start] = [(frame, "Final Cut Frame")]
        if radio is not None:
            out[radio] = gr.update(value="S")
        if row is not None:
            out[row] = gr.update(visible=True)
        if main_tabs is not None:
            out[main_tabs] = gr.Tabs(selected="video_gen")
            gr.Info("Sent the final-cut frame to the Video Generator (Img2Vid start frame).")
        else:
            gr.Info("Open the Video Generator and use your rendered cut as the start frame.")
        return out

    # -- settings -----------------------------------------------------------
    def _wire_settings(self, s, pages):
        def _save_dirs(projects, renders, outputs):
            try:
                paths.set_dirs(projects=projects or None, renders=renders or None,
                               wan2gp_outputs=outputs or None)
                tw.register_static_paths([paths.renders_dir(), paths.thumbs_dir(),
                                          paths.wan2gp_outputs_dir()])
                status = "✅ Directories saved & created."
            except Exception as e:
                status = f"⚠️ Could not save directories: {e}"
            gallery, _ = self._refresh_library()
            return status, gallery, settings_panel.ffmpeg_md()

        s["save_dirs"].click(
            _save_dirs,
            inputs=[s["projects_dir"], s["renders_dir"], s["wan2gp_outputs_dir"]],
            outputs=[s["dirs_status"], pages["library"]["gallery"], s["ffmpeg_status"]])

        s["rescan"].click(lambda: self._refresh_library(),
                         outputs=[pages["library"]["gallery"], pages["library"]["status"]])


# The plugin loader looks for any WAN2GPPlugin subclass; expose a stable alias too.
Plugin = Reel2Reel


def tab_ids_timeline() -> str:
    return suite._TAB_IDS["timeline"]
