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
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import unquote

import gradio as gr

from shared.utils.plugins import WAN2GPPlugin

from .core import discovery, inbox, otio, paths, projects, render, timeline
from .ui import logo, settings_panel, suite
from .ui import timeline_widget as tw
from .ui.styles import CSS

logger = logging.getLogger("reel2reel.plugin")

PLUGIN_ID = "Reel2Reel"
PLUGIN_NAME = "Reel2Reel"
_UNDO_CAP = 60

# Pure-view JS (no Gradio round-trip): hide the Render "Start/End" number row when
# "Export range only" is unchecked. Re-syncs on body mutations (tab renders) + once
# at boot. Must go through add_custom_js — gr.HTML <script> won't execute.
_RANGE_ROW_JS = (
    "(function(){function sync(){var ctl=document.getElementById('r2r-render-controls');"
    "if(!ctl)return;var cb=ctl.querySelector('input[type=checkbox]');"
    "var row=ctl.querySelector('.r2r-range-row');if(!row)return;"
    "row.classList.toggle('r2r-range-off',!(cb&&cb.checked));}"
    "document.addEventListener('change',function(e){if(e.target&&e.target.matches&&"
    "e.target.matches('#r2r-render-controls input[type=checkbox]'))sync();},true);"
    "try{new MutationObserver(sync).observe(document.body,{childList:true,subtree:true});}"
    "catch(e){}sync();})();"
)

# Shared saintorphan right-click menu. The scaffold block (window.SaintorphanMenu)
# is COPIED VERBATIM from Replicant's _CTX_MENU_JS so whichever of the user's
# plugins loads first builds the identical menu; our section is guarded by
# M._reel2reel. We announce('reel2reel') — which fires the whenPresent('reel2reel')
# hooks Replicant/ImageSuite register, so their "(Reference)" / "(Img2Img)" items
# attach to our `.r2r-timeline-clip` surface automatically — and register our own
# native command (Send to Vid2Vid), relayed to Python via #reel2reel-ctx-relay.
# Injected via <img onerror> (gr.HTML innerHTML doesn't run <script>).
_CTX_MENU_JS = (
    "<img src=x style='display:none' onerror=\"(function(){"
    "if(!window.SaintorphanMenu){var M=window.SaintorphanMenu={items:[],present:{},_w:{}};"
    "M.announce=function(n){M.present[n]=true;(M._w[n]||[]).forEach(function(f){"
    "try{f();}catch(e){console.error(e);}});M._w[n]=[];};"
    "M.whenPresent=function(n,cb){if(M.present[n]){try{cb();}catch(e){console.error(e);}}"
    "else{(M._w[n]||(M._w[n]=[])).push(cb);}};"
    "M.register=function(match,label,handler){M.items.push("
    "{match:match,label:label,handler:handler});};"
    "M.srcOf=function(el){if(!el)return '';"
    "var a=el.getAttribute&&el.getAttribute('data-media-src');if(a)return a;"
    "if(el.currentSrc||el.src)return el.currentSrc||el.src;"
    "var q=el.querySelector&&el.querySelector('img,video');"
    "return q?(q.currentSrc||q.src||''):'';};"
    "function hit(match,el){if(match==='image')return el.closest('img');"
    "if(match==='video')return el.closest('video');"
    "try{return el.closest(match);}catch(e){return null;}}"
    "function close(){var m=document.getElementById('saintorphan-ctx');if(m)m.remove();}"
    "function build(x,y,hits){close();"
    "var menu=document.createElement('div');menu.id='saintorphan-ctx';"
    "menu.style.cssText='position:fixed;z-index:99999;background:#1f2430;border:1px solid "
    "#3a3f4b;border-radius:8px;padding:4px 0;box-shadow:0 6px 24px rgba(0,0,0,.5);"
    "min-width:210px;font-family:sans-serif;font-size:13px;color:#e5e7eb;';"
    "var h=document.createElement('div');h.textContent='saintorphan';"
    "h.style.cssText='padding:4px 14px;font-weight:700;color:#e83e8c;cursor:default;"
    "user-select:none;';menu.appendChild(h);"
    "var hr=document.createElement('div');hr.style.cssText='height:1px;background:#3a3f4b;"
    "margin:4px 0;';menu.appendChild(hr);"
    "hits.forEach(function(hk){var el=document.createElement('div');el.textContent=hk.it.label;"
    "el.style.cssText='padding:6px 14px;cursor:pointer;white-space:nowrap;';"
    "el.onmouseenter=function(){el.style.background='#2d3340';};"
    "el.onmouseleave=function(){el.style.background='';};"
    "el.addEventListener('click',function(ev){ev.stopPropagation();close();"
    "try{hk.it.handler(hk.el);}catch(err){console.error(err);}});menu.appendChild(el);});"
    "document.body.appendChild(menu);var r=menu.getBoundingClientRect();"
    "if(x+r.width>window.innerWidth)x=window.innerWidth-r.width-6;"
    "if(y+r.height>window.innerHeight)y=window.innerHeight-r.height-6;"
    "menu.style.left=x+'px';menu.style.top=y+'px';}"
    "document.addEventListener('contextmenu',function(e){var hits=[];"
    "M.items.forEach(function(it){var el=hit(it.match,e.target);if(el)hits.push({it:it,el:el});});"
    "if(!hits.length)return;e.preventDefault();build(e.clientX,e.clientY,hits);},true);"
    "document.addEventListener('click',close);document.addEventListener('scroll',close,true);}"
    "var M=window.SaintorphanMenu;if(!M._reel2reel){M._reel2reel=true;M.announce('reel2reel');"
    "var relay=function(v){var b=document.querySelector('#reel2reel-ctx-relay textarea')"
    "||document.querySelector('#reel2reel-ctx-relay input');"
    "if(b){b.value=v+'|'+Date.now();b.dispatchEvent(new Event('input',{bubbles:true}));}};"
    "var toG=function(el){var s=M.srcOf(el);if(s)relay('global|'+s);};"
    "var toP=function(el){var s=M.srcOf(el);if(s)relay('project|'+s);};"
    "var rid=function(el){return (el&&el.getAttribute)?(el.getAttribute('data-id')||''):'';};"
    "M.register('image','Reel2Reel Library (global)',toG);"
    "M.register('image','Reel2Reel Library (project)',toP);"
    "M.register('video','Reel2Reel Library (global)',toG);"
    "M.register('video','Reel2Reel Library (project)',toP);"
    "M.register('.r2r-timeline-clip','Send to Vid2Vid',function(el){relay('vid2vid|'+rid(el));});"
    "M.register('.r2r-timeline-clip','Send → I2V first frame',function(el){relay('start|'+rid(el));});"
    "M.register('.r2r-timeline-clip','Send → I2V last frame',function(el){relay('end|'+rid(el));});"
    "M.register('.r2r-timeline-clip','Send → sliding-window anchor',function(el){relay('anchor|'+rid(el));});"
    "M.register('.r2r-timeline-clip','Split at playhead',function(el){relay('csplit|'+rid(el));});"
    "M.register('.r2r-timeline-clip','Duplicate',function(el){relay('cdup|'+rid(el));});"
    "M.register('.r2r-timeline-clip','Detach audio',function(el){relay('cdetach|'+rid(el));});"
    "M.register('.r2r-timeline-clip','Delete clip',function(el){relay('cdel|'+rid(el));});}"
    "})()\">")


_COMMON_FPS = [8, 12, 15, 16, 24, 25, 30, 48, 50, 60]


def _snap_fps(f) -> int:
    """Snap a probed frame rate to the nearest common rate (23.976->24, 29.97->30)."""
    try:
        f = float(f)
    except (TypeError, ValueError):
        return 30
    for c in _COMMON_FPS:
        if abs(f - c) <= 0.6:
            return c
    return max(1, round(f))


class Reel2Reel(WAN2GPPlugin):
    def __init__(self):
        super().__init__()
        self.name = PLUGIN_NAME
        self.version = "0.4.0"
        self.description = ("Multi-track timeline editor: arrange AI clips on "
                            "video/audio tracks, detach/edit audio, transitions, "
                            "projects with versioning, a media library, a shared "
                            "right-click menu, and ffmpeg export.")
        self._project = timeline.Timeline()
        self._seq = 0
        self._library: list[dict] = []
        self._last_render: str | None = None
        self._undo: list[str] = []
        self._redo: list[str] = []
        self._last_sig = ""
        self._project_name: str | None = None
        self._bin: list[str] = []          # current project's media bin
        self._gbin: list[str] = []         # global (cross-project) media bin
        self._bin_view: list[dict] = []
        self._gbin_view: list[dict] = []
        self._ctx_out: list[str] = []      # context-menu relay output order
        self._cancel_event = threading.Event()
        self._clipboard: dict | None = None

    # -- lifecycle ----------------------------------------------------------
    def setup_ui(self):
        try:
            paths.ensure_dirs()
            projects.migrate_legacy()
            self._gbin = projects.get_global_bin()
        except Exception:
            traceback.print_exc()
        js = tw.timeline_js()
        combined = "\n".join(p for p in (js, _RANGE_ROW_JS) if p)
        if combined:
            self.add_custom_js(combined)
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
        # Shared saintorphan right-click menu (scaffold + announce + our command).
        gr.HTML(_CTX_MENU_JS, elem_classes="reel2reel-hidden")

        with gr.Column(elem_id="reel2reel-root"):
            gr.HTML(logo.banner_html())
            ui = suite.build_suite()
            # Relay for the timeline context-menu's native commands (JS -> Python).
            self.ctx_relay = gr.Textbox(elem_id="reel2reel-ctx-relay", visible=False,
                                       interactive=True, value="")

        tl = ui["pages"]["timeline"]
        lib = ui["pages"]["library"]
        bar = ui["bar"]                       # persistent project/version bar
        self.tl_to_py = tl["tl_to_py"]
        self.tl_from_py = tl["tl_from_py"]
        self.trk_dd = tl["trk_dd"]
        # proj_dd / ver_dd now live in the suite-level bar; keep the same attr names
        # so on_tab_outputs / on_tab_select keep feeding the live components.
        self.proj_dd = bar["proj_dd"]
        self.ver_dd = bar["ver_dd"]
        self.bin_gallery = lib["bin_gallery"]
        self.global_gallery = lib["global_gallery"]
        self._last_sig = self._content_sig()
        self._wire(ui)
        # On tab entry: drain the inbox, reload the timeline, refresh tracks /
        # project list / versions / both media bins.
        self.on_tab_outputs = [self.tl_from_py, self.trk_dd, self.proj_dd,
                               self.ver_dd, self.bin_gallery, self.global_gallery]
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
        return (self._load_envelope(), gr.update(choices=self._track_choices()),
                gr.update(choices=projects.list_projects(), value=self._project_name),
                gr.update(choices=self._ver_choices()),
                self._bin_value(), self._gbin_value())

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

    def _ver_choices(self):
        return projects.version_labels(self._project_name) if self._project_name else []

    def _current_md(self):
        return (f"**Open project:** `{self._project_name}`" if self._project_name
                else "*No project open — use **Save as** to name one.*")

    @staticmethod
    def _dedup(items):
        seen, out = set(), []
        for p in items:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _bin_thumb(self, path):
        cid = f"bin_{abs(hash(path)) % 10**8}"
        return discovery.thumbnail(path, cid, getattr(self, "get_video_frame", None))

    def _gallery_value(self, items, view_attr):
        view, gallery = [], []
        for p in items:
            if not Path(p).exists():
                continue
            try:
                thumb = self._bin_thumb(p)
            except Exception:
                thumb = None
            view.append({"path": p, "thumb": thumb, "name": Path(p).name})
            gallery.append((thumb or p, Path(p).name))
        setattr(self, view_attr, view)
        return gallery

    def _bin_value(self):
        return self._gallery_value(self._bin, "_bin_view")

    def _gbin_value(self):
        return self._gallery_value(self._gbin, "_gbin_view")

    def _url_to_path(self, url):
        """A /gradio_api/file=… (or /file=…) URL, or a bare path, → absolute path."""
        if not url or url.startswith("data:"):
            return None
        u = url.split("?", 1)[0]
        for marker in ("/gradio_api/file=", "/file="):
            i = u.find(marker)
            if i >= 0:
                return unquote(u[i + len(marker):])
        return unquote(u) if u.startswith("/") else None

    # -- clip ingest --------------------------------------------------------
    def _thumb_for(self, clip, kind):
        if kind == "audio":
            dest = str(paths.thumbs_dir() / f"wave_{clip.id}.png")
            return render.waveform(clip.src, clip.in_, clip.out, dest) \
                or discovery.audio_placeholder()
        if kind == "video":
            dest = str(paths.thumbs_dir() / f"strip_{clip.id}.png")
            fs = render.filmstrip(clip.src, clip.in_, clip.out, dest)
            if fs:
                return fs
        return discovery.thumbnail(clip.src, clip.id, getattr(self, "get_video_frame", None))

    def _ingest_clip(self, path: str, force_kind: str = "auto"):
        if not path or not Path(path).exists():
            return None
        k = discovery.kind_of(path) or "video"
        track_kind = "Audio" if (k == "audio" or force_kind == "Audio") else "Video"
        info = discovery.probe_clip(path, getattr(self, "get_video_info", None))
        # First clip on an empty timeline adopts its fps + resolution (then it's the
        # locked sequence timebase; later clips conform on export). Override via the
        # timeline's FPS / size fields or "Match highest fps".
        if not any(t.clips for t in self._project.tracks):
            if info.get("fps"):
                self._project.fps = _snap_fps(info["fps"])
            if info.get("width") and info.get("height"):
                self._project.width = int(info["width"])
                self._project.height = int(info["height"])
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
        self._wire_projects(ui["bar"])
        self._wire_render(pages["render"])
        self._wire_settings(ui["settings"], pages)
        self._wire_ctx(pages["library"])

    # -- shared context-menu relay (global/project bin + native Vid2Vid) ----
    def _wire_ctx(self, lib):
        relay = getattr(self, "ctx_relay", None)
        state = getattr(self, "state", None)
        if relay is None or state is None:
            logger.warning("Context-menu relay not wired (relay=%s, state=%s); the "
                           "right-click 'Reel2Reel Library' / 'Send to Vid2Vid' items "
                           "won't reach Python.", relay is not None, state is not None)
            return
        # Ordered outputs; the handler returns values positionally for whichever
        # targets exist on this host.
        out = [("tl", self.tl_from_py), ("bin", lib["bin_gallery"]),
               ("global", lib["global_gallery"]), ("status", lib["status"])]
        rft = getattr(self, "refresh_form_trigger", None)
        mt = getattr(self, "main_tabs", None)
        if rft is not None:
            out.append(("rft", rft))
        if mt is not None:
            out.append(("main_tabs", mt))
        self._ctx_out = [n for n, _ in out]
        relay.change(self._on_ctx, inputs=[state, relay],
                    outputs=[c for _, c in out], show_progress="hidden")

    def _on_ctx(self, state, val):
        names = getattr(self, "_ctx_out", [])
        upd = {n: gr.update() for n in names}
        if val:
            parts = str(val).split("|")
            cmd = parts[0] if parts else ""
            payload = "|".join(parts[1:-1]) if len(parts) > 2 else \
                (parts[1] if len(parts) > 1 else "")
            if cmd in ("global", "project"):
                path = self._url_to_path(payload)
                if not path:
                    upd["status"] = "Couldn't resolve that media to a file."
                elif cmd == "global":
                    self._gbin = self._dedup(self._gbin + [path])
                    projects.set_global_bin(self._gbin)
                    upd["global"] = self._gbin_value()
                    upd["status"] = f"Added **{Path(path).name}** to the global library."
                    gr.Info("Added to the Reel2Reel global library.")
                else:
                    self._bin = self._dedup(self._bin + [path])
                    if self._project_name:
                        projects.set_bin(self._project_name, self._bin)
                    upd["bin"] = self._bin_value()
                    upd["status"] = f"Added **{Path(path).name}** to the project bin."
                    gr.Info("Added to the Reel2Reel project bin.")
            elif cmd in ("vid2vid", "start", "end", "anchor"):
                which = "vid" if cmd == "vid2vid" else cmd
                upd["status"] = self._send_to_gen(state, payload, which, upd)
            elif cmd in ("csplit", "cdel", "cdup", "cdetach", "copy", "cut", "paste",
                         "delsel", "razor"):
                upd["status"] = self._clip_action(cmd, payload, upd)
        return upd[names[0]] if len(names) == 1 else tuple(upd[n] for n in names)

    def _clip_action(self, cmd, payload, upd):
        """Per-clip context-menu actions + clipboard, relayed from the timeline."""
        ph = float((self._project.ui or {}).get("playhead", 0.0))
        if cmd == "copy":
            ids = [x for x in payload.split(",") if x]
            self._clipboard = self._project.serialize_clips(ids)
            return f"Copied {len(self._clipboard.get('clips', []))} clip(s)."
        if cmd == "cut":
            ids = [x for x in payload.split(",") if x]
            self._clipboard = self._project.serialize_clips(ids)
            self._push_undo()
            n = self._project.remove_clips(ids)
            self._project.ui["selected"] = None
            upd["tl"] = self._env_after()
            return f"Cut {n} clip(s)."
        if cmd == "paste":
            if not self._clipboard or not self._clipboard.get("clips"):
                return "Clipboard is empty."
            self._push_undo()
            new = self._project.paste_clips(self._clipboard, at=ph)
            upd["tl"] = self._env_after()
            return f"Pasted {len(new)} clip(s) at {ph:.2f}s."
        if cmd == "razor":                       # razor tool: cut clip at a clicked time
            p = payload.split("|")
            cid = p[0]
            try:
                t = float(p[1]) if len(p) > 1 else ph
            except ValueError:
                t = ph
            track, _ = self._project.find_clip(cid)
            if track is None:
                return "Clip not found."
            self._push_undo()
            n = len(self._project.split_at(track.id, t))
            upd["tl"] = self._env_after()
            return f"Razor cut at {t:.2f}s ({n} new)."
        if cmd == "delsel":
            ids = [x for x in payload.split(",") if x]
            if not ids:
                return "Nothing selected."
            self._push_undo()
            n = self._project.remove_clips(ids)
            self._project.ui["selected"] = None
            upd["tl"] = self._env_after()
            return f"Deleted {n} clip(s)."
        track, clip = self._project.find_clip(payload)
        if clip is None:
            return "Clip not found."
        self._push_undo()
        if cmd == "csplit":
            n = len(self._project.split_at(track.id, ph))
            msg = f"Split at {ph:.2f}s ({n} new)." if n else "Playhead isn't over this clip."
        elif cmd == "cdel":
            self._project.remove_clip(clip.id)
            self._project.ui["selected"] = None
            msg = "Deleted clip."
        elif cmd == "cdup":
            nid = self._project.duplicate_clip(clip.id)
            if nid:
                self._project.ui["selected"] = nid
            msg = "Duplicated clip."
        elif cmd == "cdetach":
            if track.kind == "Video" and clip.has_audio:
                self._project.detach_audio(clip.id)
                msg = "Detached audio."
            else:
                msg = "No audio to detach."
        else:
            return "Unknown action."
        upd["tl"] = self._env_after()
        return msg

    def _frame_of(self, clip, which):
        """A still IMAGE for the gen keyframe slots: the source image as-is, or a
        frame extracted at the clip's in-point (first) / out-point (last). Never
        returns the video path — falls back to the poster thumb, else None."""
        if discovery.kind_of(clip.src) == "image":
            return clip.src
        fps = float(clip.src_fps or self._project.fps or 24)
        t = float(clip.in_) if which == "first" else max(0.0, float(clip.out) - 1.0 / fps)
        dest = str(paths.thumbs_dir() / f"frame_{clip.id}_{which}.jpg")
        f = render.extract_frame(clip.src, t, dest)
        if f is None and which != "first":     # out may exceed the source length
            f = render.extract_frame(clip.src, float(clip.in_),
                                     str(paths.thumbs_dir() / f"frame_{clip.id}_in.jpg"))
        return f or clip.thumb

    def _send_to_gen(self, state, cid, which, upd):
        """Hand a clip (or a frame of it) to the Video Generator: video source
        (Vid2Vid), I2V start/end keyframe, or a sliding-window anchor frame."""
        _, clip = self._project.find_clip(cid)
        if clip is None or not clip.src:
            gr.Warning("Couldn't find that clip on the timeline.")
            return "Clip not found."
        getter = getattr(self, "get_current_model_settings", None)
        if not callable(getter):
            return "This host doesn't expose the Video Generator settings."
        try:
            s = getter(state)
            if which == "vid":
                s["video_source"] = clip.src
                ipt = s.get("image_prompt_type") or ""
                if "V" not in ipt:
                    s["image_prompt_type"] = ("V" + ipt) if ipt else "V"
                msg = "Vid2Vid source"
            elif which == "start":
                f = self._frame_of(clip, "first")
                if not f:
                    return "Couldn't extract a frame from that clip."
                s["image_start"] = [f]
                ipt = s.get("image_prompt_type") or ""
                if "S" not in ipt:
                    s["image_prompt_type"] = "S" + ipt
                msg = "I2V first frame"
            elif which == "end":
                f = self._frame_of(clip, "last")
                if not f:
                    return "Couldn't extract a frame from that clip."
                s["image_end"] = [f]
                ipt = s.get("image_prompt_type") or ""
                if "E" not in ipt:
                    s["image_prompt_type"] = ipt + "E"
                msg = "I2V last frame"
            else:  # anchor — sliding-window reference frame at a timeline position
                f = self._frame_of(clip, "first")
                if not f:
                    return "Couldn't extract a frame from that clip."
                refs = list(s.get("image_refs") or [])
                refs.append(f)
                s["image_refs"] = refs
                pos = max(1, round(float(clip.start) * int(self._project.fps)) + 1)
                fp = (s.get("frames_positions") or "").strip()
                s["frames_positions"] = f"{fp} {pos}".strip() if fp else str(pos)
                vpt = s.get("video_prompt_type") or ""
                if "F" not in vpt:
                    s["video_prompt_type"] = vpt + "F"
                msg = f"sliding-window anchor @frame {pos}"
        except render.RenderError as e:
            return f"Frame extraction failed: {e}"
        except Exception:
            traceback.print_exc()
            return "Couldn't hand the clip to the Video Generator."
        if "rft" in upd:
            upd["rft"] = time.time()
        if "main_tabs" in upd:
            upd["main_tabs"] = gr.Tabs(selected="video_gen")
        gr.Info(f"Sent to the Video Generator ({msg}).")
        return f"Sent **{clip.label or clip.id}** → {msg}."

    # -- library ------------------------------------------------------------
    def _wire_library(self, c, subtabs):
        # All three source galleries feed ONE shared picker; the single action bar
        # then operates on whatever is selected, regardless of the active source tab.
        c["refresh"].click(self._refresh_library, outputs=[c["gallery"], c["status"]])
        c["gallery"].select(self._on_pick, outputs=[c["picked"]])
        c["bin_gallery"].select(self._bin_pick, outputs=[c["picked"]])
        c["global_gallery"].select(self._gbin_pick, outputs=[c["picked"]])
        c["picked"].change(
            lambda p: f"**{Path(p).name}**" if p else "*No clip selected*",
            inputs=[c["picked"]], outputs=[c["lib_selected"]])
        c["add"].click(self._add_to_timeline, inputs=[c["picked"], c["kind"]],
                      outputs=[self.tl_from_py, subtabs, self.trk_dd, c["status"]])
        c["add_gbin"].click(self._add_to_global, inputs=[c["picked"]],
                           outputs=[c["global_gallery"], c["status"]])
        c["add_pbin"].click(self._add_to_project_bin, inputs=[c["picked"]],
                           outputs=[c["bin_gallery"], c["status"]])
        c["bin_remove"].click(self._bin_remove, inputs=[c["picked"]],
                             outputs=[c["bin_gallery"], c["picked"], c["status"]])
        c["global_remove"].click(self._gbin_remove, inputs=[c["picked"]],
                                outputs=[c["global_gallery"], c["picked"], c["status"]])

    # -- media bins ---------------------------------------------------------
    def _add_to_global(self, picked):
        if not picked:
            raise gr.Error("Select an output first.")
        self._gbin = self._dedup(self._gbin + [picked])
        projects.set_global_bin(self._gbin)
        return self._gbin_value(), f"Added **{Path(picked).name}** to the global library."

    def _add_to_project_bin(self, picked):
        if not picked:
            raise gr.Error("Select an output first.")
        self._bin = self._dedup(self._bin + [picked])
        if self._project_name:
            projects.set_bin(self._project_name, self._bin)
        return self._bin_value(), f"Added **{Path(picked).name}** to the project bin."

    def _bin_pick(self, evt: gr.SelectData):
        try:
            return self._bin_view[evt.index]["path"]
        except Exception:
            return None

    def _gbin_pick(self, evt: gr.SelectData):
        try:
            return self._gbin_view[evt.index]["path"]
        except Exception:
            return None

    def _bin_remove(self, picked):
        if not picked:
            raise gr.Error("Pick a project-bin item first.")
        self._bin = [p for p in self._bin if p != picked]
        if self._project_name:
            projects.set_bin(self._project_name, self._bin)
        return self._bin_value(), None, "Removed from the project bin."

    def _gbin_remove(self, picked):
        if not picked:
            raise gr.Error("Pick a global-library item first.")
        self._gbin = [p for p in self._gbin if p != picked]
        projects.set_global_bin(self._gbin)
        return self._gbin_value(), None, "Removed from the global library."

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
        ins = [c["ins_label"], c["ins_gain"], c["ins_speed"], c["ins_reverse"],
               c["ins_fade_in"], c["ins_fade_out"], c["ins_opacity"], c["ins_mute"],
               c["ins_bright"], c["ins_contrast"], c["ins_sat"], c["ins_gamma"],
               c["ins_tx"], c["ins_ty"], c["ins_scale"], c["ins_rotate"],
               c["ins_fit"], c["ins_crop"]]
        # Selection also drives the clip preview + info (extra outputs, not inputs).
        ins_out = ins + [c["clip_preview"], c["clip_info"]]
        # Browser -> Python: persist edits + auto-populate the clip inspector.
        c["tl_to_py"].change(self._on_timeline_change, inputs=[c["tl_to_py"]],
                            outputs=ins_out, show_progress="hidden")

        c["ins_apply"].click(self._apply_clip, inputs=ins,
                            outputs=[self.tl_from_py, c["status"]])
        c["ins_detach"].click(self._detach_audio,
                            outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["ins_dup"].click(self._duplicate, outputs=[self.tl_from_py, c["status"]])
        c["ins_ripple"].click(self._ripple_delete, outputs=[self.tl_from_py, c["status"]])
        c["ins_delete"].click(self._lift_delete, outputs=[self.tl_from_py, c["status"]])
        c["trans_add"].click(self._add_transition,
                            inputs=[c["trans_dur"], c["trans_kind"], c["trans_dir"]],
                            outputs=[self.tl_from_py, c["status"]])
        c["trans_rm"].click(self._remove_transition, outputs=[self.tl_from_py, c["status"]])

        c["split"].click(self._split, outputs=[self.tl_from_py, c["status"]])
        c["add_title"].click(self._add_title,
                            outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["add_marker"].click(self._add_marker, outputs=[self.tl_from_py, c["status"]])
        c["add_video"].click(lambda: self._add_track("Video"),
                            outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["add_audio"].click(lambda: self._add_track("Audio"),
                            outputs=[self.tl_from_py, self.trk_dd, c["status"]])
        c["undo"].click(self._do_undo,
                       outputs=[self.tl_from_py, self.trk_dd, c["undo"], c["redo"], c["status"]])
        c["redo"].click(self._do_redo,
                       outputs=[self.tl_from_py, self.trk_dd, c["undo"], c["redo"], c["status"]])

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

    def _wire_projects(self, bar):
        """Project CRUD + versioning + OTIO — now on the persistent suite-level bar.
        Handler bodies are unchanged; only the components they read/write moved here,
        and every status message now lands on the always-visible bar_status."""
        st = bar["bar_status"]
        proj_io = [self.tl_from_py, bar["proj_dd"], bar["proj_name"], bar["current_lbl"],
                   self.trk_dd, bar["ver_dd"], st]
        bar["open"].click(self._open_project, inputs=[bar["proj_dd"]], outputs=proj_io)
        bar["new"].click(self._new_project, inputs=[bar["proj_name"]], outputs=proj_io)
        bar["saveas"].click(self._saveas_project, inputs=[bar["proj_name"]],
                           outputs=[bar["proj_dd"], bar["proj_name"], bar["current_lbl"],
                                    bar["ver_dd"], st])
        bar["save"].click(self._save_project, outputs=[bar["current_lbl"], st])
        bar["rename"].click(self._rename_project, inputs=[bar["proj_name"]],
                           outputs=[bar["proj_dd"], bar["proj_name"], bar["current_lbl"], st])
        bar["dup"].click(self._dup_project, inputs=[bar["proj_name"]],
                        outputs=[bar["proj_dd"], st])
        bar["delete"].click(self._delete_project, inputs=[bar["proj_dd"]],
                           outputs=[bar["proj_dd"], bar["current_lbl"], st])
        bar["restore_auto"].click(self._restore_autosave,
                                 outputs=[self.tl_from_py, self.trk_dd, st])
        bar["otio_export"].click(self._export_otio, outputs=[bar["otio_export"], st])
        bar["otio_import"].upload(self._import_otio, inputs=[bar["otio_import"]],
                                 outputs=[self.tl_from_py, self.trk_dd, st])
        # Versions (manual named snapshots)
        bar["snapshot"].click(self._snapshot, inputs=[bar["ver_label"]],
                             outputs=[bar["ver_dd"], bar["ver_label"], st])
        bar["restore"].click(self._restore_version, inputs=[bar["ver_dd"]],
                            outputs=[self.tl_from_py, self.trk_dd, st])
        bar["delver"].click(self._delete_version, inputs=[bar["ver_dd"]],
                           outputs=[bar["ver_dd"], st])

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
        changed = new_sig != self._last_sig
        if changed:
            self._undo.append(json.dumps(timeline.to_document(self._project)))
            self._undo = self._undo[-_UNDO_CAP:]
            self._redo.clear()
            self._last_sig = new_sig
        self._project = new
        if changed:                                     # crash-recovery autosave
            try:                                        # (skip selection-only payloads)
                timeline.save(paths.autosave_path(), self._project)
            except Exception:
                pass
        return self._inspector_values()

    def _sel(self):
        return self._project.find_clip((self._project.ui or {}).get("selected"))

    def _clip_info_md(self, clip) -> str:
        info = f"**{clip.label or clip.id}** · {clip.type}  \n"
        info += f"timeline {clip.start:.2f}–{clip.end:.2f}s  ·  {clip.dur:.2f}s long"
        if clip.speed != 1.0 or clip.reverse:
            info += f"  ·  {clip.speed:g}×{' rev' if clip.reverse else ''}"
        if clip.src:
            from pathlib import Path as _P
            info += f"  \nsrc `{_P(clip.src).name}`"
            if clip.src_dur:
                info += f"  ·  source {clip.src_dur:.1f}s"
            if clip.src_fps:
                info += f"  ·  {clip.src_fps:g} fps"
        return info

    def _inspector_values(self):
        _, clip = self._sel()
        if clip is None:
            return ([gr.update()] * 18
                    + [gr.update(value=None),
                       gr.update(value="*Double-click a clip to inspect it.*")])
        col = clip.color or {}
        g = clip.geometry or {}
        label = (clip.text.get("content") if clip.type == "text" and clip.text
                 else clip.label)
        kind = discovery.kind_of(clip.src) if clip.src else clip.type
        preview = clip.src if kind == "video" else None
        return [gr.update(value=label), gr.update(value=clip.gain_db),
                gr.update(value=clip.speed), gr.update(value=clip.reverse),
                gr.update(value=clip.fade_in), gr.update(value=clip.fade_out),
                gr.update(value=clip.opacity), gr.update(value=clip.mute),
                gr.update(value=col.get("brightness", 0.0)),
                gr.update(value=col.get("contrast", 1.0)),
                gr.update(value=col.get("saturation", 1.0)),
                gr.update(value=col.get("gamma", 1.0)),
                gr.update(value=str(g.get("x", "center"))),
                gr.update(value=str(g.get("y", "center"))),
                gr.update(value=g.get("scale", 1.0)),
                gr.update(value=g.get("rotate", 0.0)),
                gr.update(value=g.get("fit", "fit")),
                gr.update(value=g.get("crop", 0.0)),
                gr.update(value=preview),
                gr.update(value=self._clip_info_md(clip))]

    @staticmethod
    def _coord(v):
        v = str(v).strip().lower()
        if v in ("", "center"):
            return "center"
        try:
            return int(float(v))
        except ValueError:
            return "center"

    # -- clip ops -----------------------------------------------------------
    def _apply_clip(self, label, gain, speed, reverse, fin, fout, opacity, mute,
                    bright, contrast, sat, gamma, tx, ty, scale, rotate, fit, crop):
        _, clip = self._sel()
        if clip is None:
            raise gr.Error("Select a clip on the timeline first.")
        self._push_undo()
        color = {"brightness": float(bright), "contrast": float(contrast),
                 "saturation": float(sat), "gamma": float(gamma)}
        geometry = {"x": self._coord(tx), "y": self._coord(ty),
                    "scale": float(scale), "rotate": float(rotate),
                    "fit": fit or "fit", "crop": float(crop)}
        props = dict(label=label, gain_db=gain, speed=speed, reverse=reverse,
                     fade_in=fin, fade_out=fout, opacity=opacity, mute=mute,
                     color=color, geometry=geometry)
        if clip.type == "text":
            txt = dict(clip.text or {})
            txt["content"] = label
            props["text"] = txt
        self._project.set_clip(clip.id, **props)
        return self._env_after(), f"Updated **{clip.label or clip.id}**."

    def _add_title(self):
        ph = float((self._project.ui or {}).get("playhead", 0.0))
        self._push_undo()
        track, sel = self._sel()
        tid = track.id if (track and track.kind == "Video") else None
        clip = self._project.add_text_clip("Title", start=ph, dur=3.0, track_id=tid)
        self._project.ui["selected"] = clip.id
        return (self._env_after(), gr.update(choices=self._track_choices()),
                "Added a title — edit its text in the inspector.")

    def _add_marker(self):
        ph = float((self._project.ui or {}).get("playhead", 0.0))
        self._push_undo()
        self._project.add_marker(ph, label="")
        return self._env_after(), f"Marker added at {ph:.2f}s."

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

    def _add_transition(self, dur, kind, direction):
        track, clip = self._sel()
        if clip is None:
            raise gr.Error("Select the left clip of the pair first.")
        self._push_undo()
        tid = self._project.add_transition(clip.id, float(dur), kind=kind or "dissolve",
                                           direction=direction or "left")
        if not tid:
            return self._load_envelope(), "No clip follows the selection (or clips too short)."
        return self._env_after(), f"Added a {float(dur):g}s {kind or 'dissolve'} → next clip."

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

    def _undo_labels(self):
        """Button labels that surface how deep the undo / redo stacks are."""
        u = f"↶ Undo ({len(self._undo)})" if self._undo else "↶ Undo"
        r = f"↷ Redo ({len(self._redo)})" if self._redo else "↷ Redo"
        return gr.update(value=u), gr.update(value=r)

    def _do_undo(self):
        if not self._undo:
            return (self._load_envelope(), gr.update(choices=self._track_choices()),
                    *self._undo_labels(), "Nothing to undo.")
        self._redo.append(json.dumps(timeline.to_document(self._project)))
        self._project = timeline.from_document(json.loads(self._undo.pop()))
        return (self._env_after(), gr.update(choices=self._track_choices()),
                *self._undo_labels(), "Undid.")

    def _do_redo(self):
        if not self._redo:
            return (self._load_envelope(), gr.update(choices=self._track_choices()),
                    *self._undo_labels(), "Nothing to redo.")
        self._undo.append(json.dumps(timeline.to_document(self._project)))
        self._project = timeline.from_document(json.loads(self._redo.pop()))
        return (self._env_after(), gr.update(choices=self._track_choices()),
                *self._undo_labels(), "Redid.")

    # -- projects: CRUD + versioning ----------------------------------------
    def _proj_io(self, status):
        """The 7-tuple returned by open/new (env, proj_dd, name, current, tracks,
        versions, status)."""
        return (self._load_envelope(),
                gr.update(choices=projects.list_projects(), value=self._project_name),
                gr.update(value=self._project_name or ""), self._current_md(),
                gr.update(choices=self._track_choices()),
                gr.update(choices=self._ver_choices()), status)

    def _switch_to(self, name, tl):
        self._project = tl
        self._project_name = name
        self._bin = projects.get_bin(name) if name else []
        self._undo.clear()
        self._redo.clear()
        self._last_sig = self._content_sig()

    def _open_project(self, name):
        if not name:
            raise gr.Error("Pick a project to open.")
        tl = projects.load_timeline(name)
        if tl is None:
            raise gr.Error(f"Could not open '{name}'.")
        self._switch_to(name, tl)
        missing = [c.label or c.id for _, c in tl.all_clips()
                   if c.src and not Path(c.src).exists()]
        msg = f"Opened **{name}**."
        if missing:
            msg += (f" ⚠️ {len(missing)} clip source(s) missing "
                    f"({', '.join(missing[:3])}{'…' if len(missing) > 3 else ''}) — relink before export.")
        return self._proj_io(msg)

    def _export_otio(self):
        out = str(paths.renders_dir() / f"{paths._safe(self._project.name)}.otio")
        try:
            otio.write_otio_file(self._project, out)
        except Exception as e:
            return gr.update(), f"⚠️ OTIO export failed: {e}"
        return gr.update(value=out), f"Exported `{out}` (OpenTimelineIO)."

    def _import_otio(self, fileobj):
        if not fileobj:
            raise gr.Error("Choose a .otio file.")
        try:
            data = json.loads(Path(fileobj.name).read_text())
            self._push_undo()
            self._project = otio.from_otio(data)
            self._last_sig = self._content_sig()
        except Exception as e:
            raise gr.Error(f"Could not import OTIO: {e}")
        return (self._env_after(), gr.update(choices=self._track_choices()),
                f"Imported **{Path(fileobj.name).name}** ({len(list(self._project.all_clips()))} clips).")

    def _restore_autosave(self):
        p = paths.autosave_path()
        if not p.exists():
            return self._load_envelope(), gr.update(), "No autosave found."
        try:
            self._push_undo()
            self._project = timeline.load(p)
            self._last_sig = self._content_sig()
        except Exception as e:
            raise gr.Error(f"Could not restore autosave: {e}")
        return (self._env_after(), gr.update(choices=self._track_choices()),
                "Restored from autosave.")

    def _new_project(self, name):
        name = (name or "").strip()
        if not name:
            raise gr.Error("Enter a name for the new project.")
        if projects.exists(name):
            raise gr.Error(f"'{name}' already exists — Open it instead.")
        projects.create(name)
        self._switch_to(name, timeline.Timeline(name=name))
        projects.save_timeline(name, self._project)
        return self._proj_io(f"Created **{name}**.")

    def _save_project(self):
        if not self._project_name:
            return self._current_md(), "No project open — use **Save as** to name one."
        try:
            projects.save_timeline(self._project_name, self._project)
            projects.set_bin(self._project_name, self._bin)
        except Exception as e:
            return self._current_md(), f"⚠️ Could not save: {e}"
        return self._current_md(), f"Saved **{self._project_name}**."

    def _saveas_project(self, name):
        name = (name or "").strip()
        if not name:
            raise gr.Error("Enter a name to save as.")
        if projects.exists(name):
            raise gr.Error(f"'{name}' already exists — pick another name.")
        projects.create(name, self._project)
        projects.set_bin(name, self._bin)
        self._project_name = name
        return (gr.update(choices=projects.list_projects(), value=name), name,
                self._current_md(), gr.update(choices=self._ver_choices()),
                f"Saved as **{name}**.")

    def _rename_project(self, name):
        if not self._project_name:
            raise gr.Error("Open a project first.")
        name = (name or "").strip()
        if not name:
            raise gr.Error("Enter the new name.")
        try:
            projects.rename(self._project_name, name)
        except Exception as e:
            raise gr.Error(str(e))
        self._project_name = name
        self._project.name = name
        return (gr.update(choices=projects.list_projects(), value=name), name,
                self._current_md(), f"Renamed to **{name}**.")

    def _dup_project(self, name):
        if not self._project_name:
            raise gr.Error("Open a project first.")
        name = (name or "").strip()
        if not name:
            raise gr.Error("Enter a name for the duplicate.")
        try:
            projects.duplicate(self._project_name, name)
        except Exception as e:
            raise gr.Error(str(e))
        return gr.update(choices=projects.list_projects()), f"Duplicated to **{name}**."

    def _delete_project(self, name):
        if not name:
            raise gr.Error("Pick a project to delete.")
        projects.delete(name)
        if name == self._project_name:
            # Close the project but KEEP the in-memory timeline (don't wipe the
            # user's canvas); its bin is gone, so clear that.
            self._project_name = None
            self._bin = []
        return (gr.update(choices=projects.list_projects(), value=None),
                self._current_md(), f"Deleted **{name}**.")

    def _snapshot(self, label):
        if not self._project_name:
            raise gr.Error("Save the project first (Save as), then snapshot.")
        lbl = projects.snapshot(self._project_name, label, self._project)
        return gr.update(choices=self._ver_choices()), "", f"Snapshot **{lbl}** saved."

    def _restore_version(self, label):
        if not (self._project_name and label):
            raise gr.Error("Open a project and pick a version.")
        tl = projects.restore_version(self._project_name, label)
        if tl is None:
            raise gr.Error(f"Version '{label}' not found.")
        self._push_undo()
        self._project = tl
        self._last_sig = self._content_sig()
        return (self._load_envelope(), gr.update(choices=self._track_choices()),
                f"Restored version **{label}**.")

    def _delete_version(self, label):
        if not (self._project_name and label):
            raise gr.Error("Pick a version to delete.")
        projects.delete_version(self._project_name, label)
        return gr.update(choices=self._ver_choices(), value=None), f"Deleted version **{label}**."

    # -- render -------------------------------------------------------------
    def _wire_render(self, c):
        c["export"].click(
            self._render,
            inputs=[c["preset"], c["quality"], c["resolution"], c["range_on"],
                    c["range_start"], c["range_end"]],
            outputs=[c["video"], c["save_as"], c["log"]])
        c["cancel"].click(self._cancel_render, outputs=[c["log"]])
        c["preview"].click(self._preview, inputs=[c["preview_secs"]],
                          outputs=[c["video"], c["log"]])
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

    def _render(self, preset, quality, resolution, range_on, rstart, rend,
                progress=gr.Progress()):
        self._cancel_event.clear()
        w = h = None
        if resolution and "x" in str(resolution).lower():
            try:
                w, h = (int(x) for x in str(resolution).lower().split("x")[:2])
            except Exception:
                w = h = None
        start = float(rstart) if (range_on and rstart) else None
        end = float(rend) if (range_on and rend and float(rend) > 0) else None
        try:
            out = render.export(self._project, preset=preset or "mp4",
                                quality=quality or "high", width=w, height=h,
                                start=start, end=end, cancel=self._cancel_event,
                                progress_cb=lambda f, d: progress(f, desc=d))
        except render.RenderError as e:
            return gr.update(), gr.update(), f"❌ {e}"
        except Exception as e:
            traceback.print_exc()
            return gr.update(), gr.update(), f"❌ Render failed: {e}"
        self._last_render = out
        return out, gr.update(value=out), f"✅ Rendered `{out}`"

    def _cancel_render(self):
        self._cancel_event.set()
        return "Cancelling render…"

    def _preview(self, secs, progress=gr.Progress()):
        """A true low-res composite of a window at the playhead (the real cut)."""
        self._cancel_event.clear()
        ph = float((self._project.ui or {}).get("playhead", 0.0))
        secs = float(secs or 8)
        pw = 480
        phh = max(2, int(round(self._project.height * pw / max(1, self._project.width))) // 2 * 2)
        try:
            out = render.export(
                self._project, out_path=str(paths.cache_dir() / "preview.mp4"),
                preset="mp4", quality="low", width=pw, height=phh,
                start=ph, end=ph + secs, cancel=self._cancel_event,
                progress_cb=lambda f, d: progress(f, desc="Preview: " + d))
        except render.RenderError as e:
            return gr.update(), f"❌ {e}"
        except Exception as e:
            traceback.print_exc()
            return gr.update(), f"❌ Preview failed: {e}"
        return out, f"👁 Composite preview {ph:.1f}–{ph + secs:.1f}s ({pw}px)."

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

        def _clear(also_renders):
            freed = paths.clear_cache(include_renders=bool(also_renders))
            return settings_panel.cache_md(), f"🧹 Freed {paths.human_size(freed)}."
        s["clear_cache"].click(_clear, inputs=[s["clear_renders"]],
                              outputs=[s["cache_status"], s["dirs_status"]])


# The plugin loader looks for any WAN2GPPlugin subclass; expose a stable alias too.
Plugin = Reel2Reel
