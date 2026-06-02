"""Reel2Reel — a Wan2GP plugin.

A non-linear, multi-track timeline video editor rendered as one main-webui tab
(Library / Timeline / Render / Settings). It composites *existing* clips (AI
clips sent here, or imported from the outputs folder) with ffmpeg — it never
generates frames, so it needs none of the submit_task / model machinery.

Clips arrive via the Library tab or via ``reel2reel.inbox.enqueue_clips(state,
path)`` from any other tab (drained onto the timeline by ``on_tab_select``). The
browser timeline (assets/static/timeline.js) round-trips its edit state through
two hidden gr.Textbox JSON pipes; property edits (gain, fades, opacity, mute,
detach-audio, transitions, track ops, undo/redo) are Gradio-side and arrive back
as a load envelope.

NOTE: not an official plugin. Distribute via the plugin-manager "add from GitHub
URL" flow; do not add to the bundled plugins.json without dbm's approval.
"""
from __future__ import annotations

import json
import logging
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
_UNDO_CAP = 60


class Reel2Reel(WAN2GPPlugin):
    def __init__(self):
        super().__init__()
        self.name = PLUGIN_NAME
        self.version = "0.2.0"
        self.description = ("Multi-track timeline editor: arrange AI clips on "
                            "video/audio tracks, detach/edit audio, add fades and "
                            "cross-dissolves, and export a final cut with ffmpeg.")
        self._project = timeline.Timeline()
        self._seq = 0
        self._library: list[dict] = []
        self._last_render: str | None = None
        self._undo: list[str] = []
        self._redo: list[str] = []
        self._last_sig = ""

    # -- lifecycle ----------------------------------------------------------
    def setup_ui(self):
        try:
            paths.ensure_dirs()
        except Exception:
            traceback.print_exc()
        js = tw.timeline_js()
        if js:
            self.add_custom_js(js)
        try:
            tw.register_static_paths([
                paths.renders_dir(), paths.thumbs_dir(), paths.norm_dir(),
                paths.wan2gp_outputs_dir()])
        except Exception:
            traceback.print_exc()

        self.request_component("state")
        self.request_component("main_tabs")
        self.request_component("refresh_form_trigger")
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
        # api_session defaults to None: the local host calls the constructor with
        # zero args; the newer session-API host passes a session. ffmpeg-only, so
        # self._api stays unused.
        self._api = api_session

        gr.HTML(f"<style>{CSS}</style>", elem_classes="reel2reel-hidden")
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

        tl = ui["pages"]["timeline"]
        self.tl_to_py = tl["tl_to_py"]
        self.tl_from_py = tl["tl_from_py"]
        self.trk_dd = tl["trk_dd"]
        self._last_sig = self._content_sig()
        self._wire(ui)
        # On tab entry: drain the inbox + reload the timeline + refresh the track list.
        self.on_tab_outputs = [self.tl_from_py, self.trk_dd]
        return ui

    # -- inbox --------------------------------------------------------------
    def on_tab_select(self, state: dict):
        try:
            drained = inbox.drain(state)
            if drained:
                self._push_undo()
                for p in drained:
                    self._ingest_clip(p)
                self._last_sig = self._content_sig()
        except Exception:
            traceback.print_exc()
        return self._load_envelope(), gr.update(choices=self._track_choices())

    # -- envelopes / signatures --------------------------------------------
    def _edit_payload(self) -> dict:
        edit = self._project.to_edit_json()
        for c in edit["clips"]:
            c["url"] = tw.file_url(c.get("src"))
            c["thumb_url"] = tw.file_url(c.get("thumb")) if c.get("thumb") else None
        return edit

    def _load_envelope(self) -> str:
        self._seq += 1
        return json.dumps({"seq": self._seq, "op": "load", "edit": self._edit_payload()})

    def _content_sig(self) -> str:
        d = timeline.to_document(self._project)
        d.pop("ui", None)
        return json.dumps(d, sort_keys=True)

    def _env_after(self) -> str:
        """Build a reload envelope and re-baseline the undo signature (call after
        any server-side mutation so the next browser edit diffs correctly)."""
        self._last_sig = self._content_sig()
        return self._load_envelope()

    # -- undo / redo --------------------------------------------------------
    def _push_undo(self):
        self._undo.append(json.dumps(timeline.to_document(self._project)))
        self._undo = self._undo[-_UNDO_CAP:]
        self._redo.clear()

    def _track_choices(self):
        return [(f"{t.name} · {t.kind}", t.id) for t in self._project.tracks]

    # -- clip ingest --------------------------------------------------------
    def _thumb_for(self, clip, kind):
        if kind == "audio":
            dest = str(paths.thumbs_dir() / f"wave_{clip.id}.png")
            return render.waveform(clip.src, clip.in_, clip.out, dest) \
                or discovery.audio_placeholder()
        return discovery.thumbnail(clip.src, clip.id, getattr(self, "get_video_frame", None))

    def _ingest_clip(self, path: str, force_kind: str = "auto"):
        if not path or not Path(path).exists():
            return None
        k = discovery.kind_of(path) or "video"
        track_kind = "Audio" if (k == "audio" or force_kind == "Audio") else "Video"
        info = discovery.probe_clip(path, getattr(self, "get_video_info", None))
        dur = info.get("dur") or 5.0
        clip = self._project.append_clip(
            path, kind=track_kind, in_=0.0, out=float(dur), src_dur=info.get("dur"),
            src_fps=info.get("fps"), has_audio=bool(info.get("has_audio")),
            label=Path(path).stem)
        try:
            clip.thumb = self._thumb_for(clip, "audio" if track_kind == "Audio" else k)
        except Exception:
            clip.thumb = None
        return clip

    # -- wiring -------------------------------------------------------------
    def _wire(self, ui):
        pages = ui["pages"]
        # Python -> browser hook (no fn, no feedback loop).
        self.tl_from_py.change(fn=None, inputs=[self.tl_from_py], outputs=[],
                              js=tw.APPLY_OP_JS, show_progress="hidden")
        self._wire_library(pages["library"], ui["subtabs"])
        self._wire_timeline(pages["timeline"])
        self._wire_render(pages["render"])
        self._wire_settings(ui["settings"], pages)

    # -- library ------------------------------------------------------------
    def _wire_library(self, c, subtabs):
        c["refresh"].click(self._refresh_library, outputs=[c["gallery"], c["status"]])
        c["gallery"].select(self._on_pick, outputs=[c["picked"]])
        c["add"].click(self._add_to_timeline, inputs=[c["picked"], c["kind"]],
                      outputs=[self.tl_from_py, subtabs, self.trk_dd, c["status"]])

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
        self._push_undo()
        self._ingest_clip(picked, force_kind=kind if kind in ("Video", "Audio") else "auto")
        return (self._env_after(), gr.update(selected=suite._TAB_IDS["timeline"]),
                gr.update(choices=self._track_choices()),
                f"Added **{Path(picked).stem}** to the timeline.")

    # -- timeline: bridge persist + inspectors + toolbar --------------------
    def _wire_timeline(self, c):
        ins = [c["ins_label"], c["ins_gain"], c["ins_fade_in"], c["ins_fade_out"],
               c["ins_opacity"], c["ins_mute"]]
        # Browser -> Python: persist edits + auto-populate the clip inspector.
        c["tl_to_py"].change(self._on_timeline_change, inputs=[c["tl_to_py"]],
                            outputs=ins, show_progress="hidden")

        c["ins_apply"].click(self._apply_clip, inputs=ins,
                            outputs=[self.tl_from_py, c["status"]])
        c["ins_detach"].click(self._detach_audio,
                            outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["ins_dup"].click(self._duplicate, outputs=[self.tl_from_py, c["status"]])
        c["ins_ripple"].click(self._ripple_delete, outputs=[self.tl_from_py, c["status"]])
        c["ins_delete"].click(self._lift_delete, outputs=[self.tl_from_py, c["status"]])
        c["trans_add"].click(self._add_transition, inputs=[c["trans_dur"]],
                            outputs=[self.tl_from_py, c["status"]])
        c["trans_rm"].click(self._remove_transition, outputs=[self.tl_from_py, c["status"]])

        c["split"].click(self._split, outputs=[self.tl_from_py, c["status"]])
        c["add_video"].click(lambda: self._add_track("Video"),
                            outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["add_audio"].click(lambda: self._add_track("Audio"),
                            outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["undo"].click(self._do_undo, outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["redo"].click(self._do_redo, outputs=[self.tl_from_py, self.trk_dd, c["status"]])

        # Track inspector
        c["trk_dd"].change(self._load_track, inputs=[c["trk_dd"]],
                          outputs=[c["trk_name"], c["trk_volume"], c["trk_mute"],
                                   c["trk_solo"], c["trk_lock"]])
        c["trk_apply"].click(
            self._apply_track,
            inputs=[c["trk_dd"], c["trk_name"], c["trk_volume"], c["trk_mute"],
                    c["trk_solo"], c["trk_lock"]],
            outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["trk_del"].click(self._delete_track, inputs=[c["trk_dd"]],
                          outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["trk_up"].click(lambda t: self._move_track(t, -1), inputs=[c["trk_dd"]],
                         outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["trk_down"].click(lambda t: self._move_track(t, 1), inputs=[c["trk_dd"]],
                           outputs=[self.tl_from_py, self.trk_dd, c["status"]])

        # Projects
        c["new"].click(self._new_project, inputs=[c["proj_name"]],
                      outputs=[self.tl_from_py, c["load_name"], self.trk_dd, c["status"]])
        c["save"].click(self._save_project, inputs=[c["proj_name"]],
                       outputs=[c["load_name"], c["status"]])
        c["load"].click(self._load_project, inputs=[c["load_name"]],
                       outputs=[self.tl_from_py, c["proj_name"], self.trk_dd, c["status"]])

    def _on_timeline_change(self, payload: str):
        if not payload:
            return self._inspector_values()
        try:
            new = timeline.Timeline.from_edit_json(json.loads(payload))
        except Exception:
            logger.debug("bad timeline payload", exc_info=True)
            return self._inspector_values()
        new_sig = json.dumps({k: v for k, v in timeline.to_document(new).items()
                              if k != "ui"}, sort_keys=True)
        if new_sig != self._last_sig:
            self._undo.append(json.dumps(timeline.to_document(self._project)))
            self._undo = self._undo[-_UNDO_CAP:]
            self._redo.clear()
            self._last_sig = new_sig
        self._project = new
        return self._inspector_values()

    def _sel(self):
        return self._project.find_clip((self._project.ui or {}).get("selected"))

    def _inspector_values(self):
        _, clip = self._sel()
        if clip is None:
            return [gr.update()] * 6
        return [gr.update(value=clip.label), gr.update(value=clip.gain_db),
                gr.update(value=clip.fade_in), gr.update(value=clip.fade_out),
                gr.update(value=clip.opacity), gr.update(value=clip.mute)]

    # -- clip ops -----------------------------------------------------------
    def _apply_clip(self, label, gain, fin, fout, opacity, mute):
        _, clip = self._sel()
        if clip is None:
            raise gr.Error("Select a clip on the timeline first.")
        self._push_undo()
        self._project.set_clip(clip.id, label=label, gain_db=gain, fade_in=fin,
                               fade_out=fout, opacity=opacity, mute=mute)
        return self._env_after(), f"Updated **{clip.label or clip.id}**."

    def _detach_audio(self):
        track, clip = self._sel()
        if clip is None:
            raise gr.Error("Select a video clip first.")
        if track.kind != "Video":
            return self._load_envelope(), gr.update(), "Selected clip is already audio."
        if not clip.has_audio:
            return self._load_envelope(), gr.update(), "This clip has no audio stream to detach."
        self._push_undo()
        aid = self._project.detach_audio(clip.id)
        if not aid:
            return self._load_envelope(), gr.update(), "Could not detach audio."
        _, ac = self._project.find_clip(aid)
        try:
            ac.thumb = self._thumb_for(ac, "audio")
        except Exception:
            pass
        self._project.ui["selected"] = aid
        return (self._env_after(), gr.update(choices=self._track_choices()),
                f"Detached audio from **{clip.label or clip.id}** → {ac.track}.")

    def _duplicate(self):
        _, clip = self._sel()
        if clip is None:
            raise gr.Error("Select a clip first.")
        self._push_undo()
        nid = self._project.duplicate_clip(clip.id)
        if nid:
            self._project.ui["selected"] = nid
        return self._env_after(), "Duplicated clip."

    def _ripple_delete(self):
        _, clip = self._sel()
        if clip is None:
            raise gr.Error("Select a clip first.")
        self._push_undo()
        self._project.ripple_delete(clip.id)
        self._project.ui["selected"] = None
        return self._env_after(), "Ripple-deleted (gap closed)."

    def _lift_delete(self):
        _, clip = self._sel()
        if clip is None:
            raise gr.Error("Select a clip first.")
        self._push_undo()
        self._project.remove_clip(clip.id)
        self._project.ui["selected"] = None
        return self._env_after(), "Deleted clip (gap left)."

    def _add_transition(self, dur):
        track, clip = self._sel()
        if clip is None:
            raise gr.Error("Select the left clip of the pair first.")
        self._push_undo()
        tid = self._project.add_transition(clip.id, float(dur))
        if not tid:
            return self._load_envelope(), "No clip follows the selection on its track."
        return self._env_after(), f"Added a {dur:g}s dissolve into the next clip."

    def _remove_transition(self):
        _, clip = self._sel()
        if clip is None:
            raise gr.Error("Select the left clip of a transition first.")
        self._push_undo()
        ok = self._project.remove_transition(clip.id)
        return self._env_after(), ("Removed the transition." if ok else "No transition here.")

    def _split(self):
        ui = self._project.ui or {}
        t = float(ui.get("playhead", 0.0))
        sel = ui.get("selected")
        self._push_undo()
        created = []
        if sel:
            track, _ = self._project.find_clip(sel)
            if track:
                created = self._project.split_at(track.id, t)
        else:
            for trk in self._project.video_tracks():
                created += self._project.split_at(trk.id, t)
        msg = f"Split at {t:.2f}s ({len(created)} new)." if created else "Nothing under the playhead."
        return self._env_after(), msg

    # -- track ops ----------------------------------------------------------
    def _add_track(self, kind):
        self._push_undo()
        trk = self._project.add_track(kind, "")
        return (self._env_after(), gr.update(choices=self._track_choices(), value=trk.id),
                f"Added track **{trk.name}**.")

    def _load_track(self, track_id):
        t = self._project.get_track(track_id)
        if t is None:
            return [gr.update()] * 5
        return [gr.update(value=t.name), gr.update(value=t.volume_db),
                gr.update(value=t.muted), gr.update(value=t.solo), gr.update(value=t.locked)]

    def _apply_track(self, track_id, name, vol, mute, solo, lock):
        if not track_id:
            raise gr.Error("Pick a track first.")
        self._push_undo()
        self._project.set_track(track_id, name=name, volume_db=vol, muted=mute,
                                solo=solo, locked=lock)
        return (self._env_after(), gr.update(choices=self._track_choices(), value=track_id),
                "Updated track.")

    def _delete_track(self, track_id):
        if not track_id:
            raise gr.Error("Pick a track first.")
        self._push_undo()
        ok = self._project.remove_track(track_id)
        msg = "Deleted track." if ok else "Can't delete the last track."
        return self._env_after(), gr.update(choices=self._track_choices(), value=None), msg

    def _move_track(self, track_id, delta):
        if not track_id:
            raise gr.Error("Pick a track first.")
        self._push_undo()
        self._project.move_track(track_id, delta)
        return (self._env_after(), gr.update(choices=self._track_choices(), value=track_id),
                "Reordered tracks.")

    def _do_undo(self):
        if not self._undo:
            return self._load_envelope(), gr.update(choices=self._track_choices()), "Nothing to undo."
        self._redo.append(json.dumps(timeline.to_document(self._project)))
        self._project = timeline.from_document(json.loads(self._undo.pop()))
        return self._env_after(), gr.update(choices=self._track_choices()), "Undid."

    def _do_redo(self):
        if not self._redo:
            return self._load_envelope(), gr.update(choices=self._track_choices()), "Nothing to redo."
        self._undo.append(json.dumps(timeline.to_document(self._project)))
        self._project = timeline.from_document(json.loads(self._redo.pop()))
        return self._env_after(), gr.update(choices=self._track_choices()), "Redid."

    # -- projects -----------------------------------------------------------
    def _new_project(self, name):
        self._push_undo()
        self._project = timeline.Timeline(name=name or "Cut 1")
        return (self._env_after(), gr.update(choices=paths.list_projects()),
                gr.update(choices=self._track_choices()),
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
            self._push_undo()
            self._project = timeline.load(paths.project_path(name))
        except Exception as e:
            raise gr.Error(f"Could not open '{name}': {e}")
        return (self._env_after(), self._project.name,
                gr.update(choices=self._track_choices()), f"Opened **{name}**.")

    # -- render -------------------------------------------------------------
    def _wire_render(self, c):
        c["export"].click(self._render, outputs=[c["video"], c["save_as"], c["log"]])
        targets = [(n, getattr(self, n, None)) for n in
                   ("image_start", "image_prompt_type_radio", "image_start_row", "main_tabs")]
        self._i2v_targets = [n for n, comp in targets if comp is not None]
        comps = [comp for _, comp in targets if comp is not None]
        if comps:
            # Return values POSITIONALLY (matching outputs order), not as a dict —
            # unambiguous across Gradio versions.
            c["to_i2v"].click(self._send_to_img2vid, outputs=comps)
        else:
            c["to_i2v"].click(self._img2vid_unavailable, outputs=[c["log"]])

    def _render(self, progress=gr.Progress()):
        try:
            out = render.export(self._project, progress_cb=lambda f, d: progress(f, desc=d))
        except render.RenderError as e:
            return gr.update(), gr.update(), f"❌ {e}"
        except Exception as e:
            traceback.print_exc()
            return gr.update(), gr.update(), f"❌ Render failed: {e}"
        self._last_render = out
        return out, gr.update(value=out), f"✅ Rendered `{out}`"

    def _send_to_img2vid(self):
        names = getattr(self, "_i2v_targets", [])

        def _pack(vals):
            return vals[0] if len(names) == 1 else tuple(vals)

        if not self._last_render:
            gr.Warning("Export a cut first.")
            return _pack([gr.update() for _ in names])
        frame = None
        gvf = getattr(self, "get_video_frame", None)
        if callable(gvf):
            try:
                frame = gvf(self._last_render, 0)
            except Exception:
                frame = None
        vals = []
        for n in names:
            if n == "image_start":
                vals.append([(frame, "Final Cut Frame")] if frame is not None else gr.update())
            elif n == "image_prompt_type_radio":
                vals.append(gr.update(value="S"))
            elif n == "image_start_row":
                vals.append(gr.update(visible=True))
            elif n == "main_tabs":
                vals.append(gr.Tabs(selected="video_gen"))
        if "main_tabs" in names:
            gr.Info("Sent the final-cut frame to the Video Generator (Img2Vid start frame).")
        else:
            gr.Info("Open the Video Generator and use your rendered cut as the start frame.")
        return _pack(vals)

    def _img2vid_unavailable(self):
        if not self._last_render:
            return "Export a cut first, then open the Video Generator to use it."
        return (f"Open the Video Generator and load `{self._last_render}` as the "
                "Img2Vid start frame.")

    # -- settings -----------------------------------------------------------
    def _server_config(self):
        sc = getattr(self, "server_config", None)
        return sc if isinstance(sc, dict) else None

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
        s["rescan"].click(self._refresh_library,
                         outputs=[pages["library"]["gallery"], pages["library"]["status"]])


# The plugin loader looks for any WAN2GPPlugin subclass; expose a stable alias too.
Plugin = Reel2Reel
