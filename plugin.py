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
from .ui import settings_panel, suite
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
    "var h=document.createElement('div');h.textContent='OrphanSuite';"
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
    # Generic 'image'/'video' items let the user right-click ANY image elsewhere in the
    # app to drop it into a Reel2Reel bin — kept here.
    "M.register('image','Reel2Reel Library (global)',toG);"
    "M.register('image','Reel2Reel Library (project)',toP);"
    "M.register('video','Reel2Reel Library (global)',toG);"
    "M.register('video','Reel2Reel Library (project)',toP);}"
    # Reel2Reel's own clip + bin menus are built in timeline.js (openClipMenu /
    # openLibMenu) — they suppress this shared menu via a window-capture listener so we
    # control ordering, drop the "saintorphan" header, and append cross-plugin items
    # (which DO still register against .r2r-timeline-clip) at the bottom of the clip menu.
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
            ui = suite.build_suite()    # the logo banner now lives in the project bar
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
        self.gallery = lib["gallery"]                 # Outputs drawer (auto-loaded on entry)
        self._last_sig = self._content_sig()
        self._wire(ui)
        # On tab entry: drain the inbox, reload the timeline, refresh tracks /
        # project list / versions / both media bins.
        self.on_tab_outputs = [self.tl_from_py, self.trk_dd, self.proj_dd,
                               self.ver_dd, self.bin_gallery, self.global_gallery,
                               self.gallery]
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
                self._bin_value(), self._gbin_value(),
                self._refresh_library()[0])           # auto-populate the Outputs drawer

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

    def _ingest_at(self, path, track_id=None, start=None, force_kind="auto"):
        """Like _ingest_clip but place the clip at a dropped (track, start). The file's
        own kind wins: an audio file always lands on an audio track (the requested one
        if it matches, else first/new of that kind); track_id="NEW" forces a new track."""
        if not path or not Path(path).exists():
            return None
        k = discovery.kind_of(path) or "video"
        need = "Audio" if (k == "audio" or force_kind == "Audio") else "Video"
        info = discovery.probe_clip(path, getattr(self, "get_video_info", None))
        if not any(t.clips for t in self._project.tracks):
            if info.get("fps"):
                self._project.fps = _snap_fps(info["fps"])
            if info.get("width") and info.get("height"):
                self._project.width = int(info["width"])
                self._project.height = int(info["height"])
        if track_id == "NEW":
            track = self._project.add_track(need)
        else:
            t = self._project.get_track(track_id) if track_id else None
            track = t if (t and t.kind == need) else \
                (self._project.first_track(need) or self._project.add_track(need))
        st = float(start) if start is not None else \
            max((c.end for c in track.clips), default=0.0)
        dur = info.get("dur") or 5.0
        clip = timeline.Clip(id=self._project._fresh_id("c"), src=str(path),
                             start=max(0.0, st), in_=0.0, out=float(dur), track=track.id,
                             label=Path(path).stem, src_dur=info.get("dur"),
                             src_fps=info.get("fps"), has_audio=bool(info.get("has_audio")))
        self._project.add_clip(clip, track.id)
        try:
            clip.thumb = self._thumb_for(clip, "audio" if need == "Audio" else k)
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
               ("global", lib["global_gallery"]), ("outputs", lib["gallery"]),
               ("status", lib["status"])]
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
                         "delsel", "razor", "clip2pbin", "clip2gbin"):
                upd["status"] = self._clip_action(cmd, payload, upd)
            elif cmd in ("libadd", "libdrop", "libpbin", "libgbin", "librm", "libdel"):
                upd["status"] = self._lib_action(cmd, payload, upd)
        return upd[names[0]] if len(names) == 1 else tuple(upd[n] for n in names)

    def _lib_action(self, cmd, payload, upd):
        """Library thumbnail actions, relayed from the right-click menu / drag-drop.
        payload = "bin|idx[|track|time]"; bin∈{outputs,pbin,gbin}, idx is the thumb's
        display index. 'tl'/'bin'/'global' updates are written into upd in place."""
        args = payload.split("|")
        bin_name = args[0] if args else ""
        path = self._lib_path(bin_name, args[1] if len(args) > 1 else "")
        if not path:
            return "Couldn't resolve that thumbnail to a file."
        name = Path(path).name
        if cmd == "libadd":
            self._push_undo(); self._ingest_clip(path)
            upd["tl"] = self._env_after(); gr.Info(f"Added {name} to the timeline.")
            return f"Added **{name}** to the timeline."
        if cmd == "libdrop":
            track = args[2] if len(args) > 2 else "NEW"
            t = args[3] if len(args) > 3 and args[3] not in ("", "None") else None
            self._push_undo(); self._ingest_at(path, track_id=track, start=t)
            upd["tl"] = self._env_after()
            return f"Dropped **{name}** onto the timeline."
        if cmd == "libdel":                       # delete the actual file from disk
            try:
                Path(path).unlink()
            except Exception as e:
                return f"Couldn't delete {name}: {e}"
            self._bin = [p for p in self._bin if p != path]
            self._gbin = [p for p in self._gbin if p != path]
            if self._project_name:
                projects.set_bin(self._project_name, self._bin)
            projects.set_global_bin(self._gbin)
            g, _ = self._refresh_library()
            upd["outputs"] = g; upd["bin"] = self._bin_value(); upd["global"] = self._gbin_value()
            gr.Info(f"Deleted {name} from disk.")
            return f"Deleted **{name}** from disk."
        if cmd == "libpbin":
            self._bin = self._dedup(self._bin + [path])
            if self._project_name:
                projects.set_bin(self._project_name, self._bin)
            upd["bin"] = self._bin_value(); gr.Info("Added to the project bin.")
            return f"Added **{name}** to the project bin."
        if cmd == "libgbin":
            self._gbin = self._dedup(self._gbin + [path]); projects.set_global_bin(self._gbin)
            upd["global"] = self._gbin_value(); gr.Info("Added to the global bin.")
            return f"Added **{name}** to the global library."
        if cmd == "librm":
            if bin_name == "pbin":
                self._bin = [p for p in self._bin if p != path]
                if self._project_name:
                    projects.set_bin(self._project_name, self._bin)
                upd["bin"] = self._bin_value()
            elif bin_name == "gbin":
                self._gbin = [p for p in self._gbin if p != path]
                projects.set_global_bin(self._gbin)
                upd["global"] = self._gbin_value()
            else:
                return "Outputs are read-only — nothing to remove."
            return f"Removed **{name}**."
        return ""

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
        if cmd in ("clip2pbin", "clip2gbin"):     # send a timeline clip back to a bin
            _, clip = self._project.find_clip(payload)
            if clip is None or not clip.src:
                return "Clip not found."
            src, name = clip.src, Path(clip.src).name
            if cmd == "clip2gbin":
                self._gbin = self._dedup(self._gbin + [src]); projects.set_global_bin(self._gbin)
                upd["global"] = self._gbin_value(); gr.Info("Added to the global bin.")
                return f"Added **{name}** to the global library."
            self._bin = self._dedup(self._bin + [src])
            if self._project_name:
                projects.set_bin(self._project_name, self._bin)
            upd["bin"] = self._bin_value(); gr.Info("Added to the project bin.")
            return f"Added **{name}** to the project bin."
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
        # All three drawers feed ONE shared picker; selection drives the caption + the
        # kind-aware preview pane. Actions (add / copy-to-bin / remove) are on the
        # thumbnails via the right-click relay; per-bin uploads import new media.
        c["refresh"].click(self._refresh_library, outputs=[c["gallery"], c["lib_selected"]])
        c["gallery"].select(self._on_pick, outputs=[c["picked"]])
        c["bin_gallery"].select(self._bin_pick, outputs=[c["picked"]])
        c["global_gallery"].select(self._gbin_pick, outputs=[c["picked"]])
        c["picked"].change(self._lib_select, inputs=[c["picked"]],
                          outputs=[c["lib_selected"], c["prev_empty"], c["prev_img"],
                                   c["prev_vid"], c["prev_aud"]])
        c["up_outputs"].upload(lambda f: self._lib_upload(f, "outputs"),
                              inputs=[c["up_outputs"]], outputs=[c["gallery"], c["lib_selected"]])
        c["up_pbin"].upload(lambda f: self._lib_upload(f, "pbin"),
                           inputs=[c["up_pbin"]], outputs=[c["bin_gallery"], c["lib_selected"]])
        c["up_gbin"].upload(lambda f: self._lib_upload(f, "gbin"),
                           inputs=[c["up_gbin"]], outputs=[c["global_gallery"], c["lib_selected"]])

    def _lib_select(self, path):
        """Selection → caption + show the one preview component that matches the kind."""
        if not path:
            return (gr.update(value="*Right-click a thumbnail for actions, or drag it onto "
                                    "a track. Press **B** to hide this panel.*"),
                    gr.update(visible=True),
                    gr.update(visible=False, value=None), gr.update(visible=False, value=None),
                    gr.update(visible=False, value=None))
        k = discovery.kind_of(path) or "video"
        return (gr.update(value=f"**{Path(path).name}**  ·  {k}"),
                gr.update(visible=False),
                gr.update(visible=(k == "image"), value=path if k == "image" else None),
                gr.update(visible=(k == "video"), value=path if k == "video" else None),
                gr.update(visible=(k == "audio"), value=path if k == "audio" else None))

    def _lib_path(self, bin_name, idx):
        """(bin, display-index) → absolute source path, from the ordered per-bin views."""
        src = {"outputs": getattr(self, "_library", None),
               "pbin": getattr(self, "_bin_view", None),
               "gbin": getattr(self, "_gbin_view", None)}.get(bin_name)
        try:
            return src[int(idx)]["path"]
        except Exception:
            return None

    def _lib_upload(self, f, which):
        """Import an uploaded file into a bin (copied into renders_dir so it persists)."""
        import shutil
        src = f if isinstance(f, str) else getattr(f, "name", None)
        if not src or not Path(src).exists():
            return gr.update(), gr.update(value="Upload failed — no file received.")
        dest = paths.renders_dir() / Path(src).name
        try:
            paths.renders_dir().mkdir(parents=True, exist_ok=True)
            if str(Path(src).resolve()) != str(dest.resolve()):
                shutil.copy2(src, dest)
            path = str(dest)
        except Exception:
            path = src
        name = Path(path).name
        if which == "gbin":
            self._gbin = self._dedup(self._gbin + [path]); projects.set_global_bin(self._gbin)
            return self._gbin_value(), gr.update(value=f"Imported **{name}** to the global bin.")
        if which == "pbin":
            self._bin = self._dedup(self._bin + [path])
            if self._project_name:
                projects.set_bin(self._project_name, self._bin)
            return self._bin_value(), gr.update(value=f"Imported **{name}** to the project bin.")
        g, _ = self._refresh_library()      # outputs: it now lives in renders_dir → rescan
        return g, gr.update(value=f"Imported **{name}**.")

    # -- media bins (add / copy / remove are now thumbnail right-click verbs:
    #    see _lib_action; these just resolve gallery selections to a picked path) ------
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

    # -- timeline: bridge persist + inspectors + toolbar --------------------
    def _wire_timeline(self, c):
        ins = [c["ins_label"], c["ins_gain"], c["ins_speed"], c["ins_reverse"],
               c["ins_fade_in"], c["ins_fade_out"], c["ins_opacity"], c["ins_mute"],
               c["ins_bright"], c["ins_contrast"], c["ins_sat"], c["ins_gamma"],
               c["ins_temp"], c["ins_tint"],
               c["ins_tx"], c["ins_ty"], c["ins_scale"], c["ins_rotate"],
               c["ins_fit"], c["ins_crop"]]
        # Selection also drives the preview, info + the Match-to clip list (outputs only).
        ins_out = ins + [c["clip_preview"], c["clip_info"], c["ins_match_ref"]]
        # Browser -> Python: persist edits + auto-populate the clip inspector.
        c["tl_to_py"].change(self._on_timeline_change, inputs=[c["tl_to_py"]],
                            outputs=ins_out, show_progress="hidden")

        c["ins_apply"].click(self._apply_clip, inputs=ins,
                            outputs=[self.tl_from_py, c["status"]])
        # Auto-Enhance / Color-match fill the colour sliders (review, then Apply).
        c["ins_auto"].click(self._auto_enhance,
                           outputs=[c["ins_bright"], c["ins_contrast"], c["ins_sat"],
                                    c["ins_gamma"], c["status"]])
        c["ins_match"].click(self._color_match, inputs=[c["ins_match_ref"]],
                            outputs=[c["ins_bright"], c["ins_contrast"], c["ins_sat"],
                                     c["ins_temp"], c["ins_tint"], c["status"]])
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
        # Track rename / mute / solo / lock / volume / delete / reorder are handled
        # entirely client-side on the track heads (timeline.js mutates S.edit + commits
        # through the same payload channel as collapse/resize). trk_dd is kept only as a
        # hidden choices sink for the handlers above. _add_track stays Python-side (it
        # needs to mint a fresh track id) and fires from the +Video/+Audio toolbar buttons.

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
            return ([gr.update()] * 20
                    + [gr.update(value=None),
                       gr.update(value="*Double-click a clip to inspect it.*"),
                       gr.update(choices=self._clip_choices())])
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
                gr.update(value=col.get("temp", 0.0)),
                gr.update(value=col.get("tint", 0.0)),
                gr.update(value=str(g.get("x", "center"))),
                gr.update(value=str(g.get("y", "center"))),
                gr.update(value=g.get("scale", 1.0)),
                gr.update(value=g.get("rotate", 0.0)),
                gr.update(value=g.get("fit", "fit")),
                gr.update(value=g.get("crop", 0.0)),
                gr.update(value=preview),
                gr.update(value=self._clip_info_md(clip)),
                gr.update(choices=self._clip_choices(exclude=clip.id))]

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
                    bright, contrast, sat, gamma, temp, tint,
                    tx, ty, scale, rotate, fit, crop):
        _, clip = self._sel()
        if clip is None:
            raise gr.Error("Select a clip on the timeline first.")
        self._push_undo()
        color = {"brightness": float(bright), "contrast": float(contrast),
                 "saturation": float(sat), "gamma": float(gamma),
                 "temp": float(temp), "tint": float(tint)}
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

    # -- colour: auto-enhance + clip-to-clip match --------------------------
    # Both only fill the inspector's colour sliders (brightness/contrast/sat/gamma,
    # +temp/tint for match) — the user reviews and hits Apply, reusing _apply_clip.
    _AUTO_PRESET = (0.0, 1.12, 1.15, 0.96)   # brightness, contrast, saturation, gamma

    @staticmethod
    def _clamp(v, lo, hi):
        return max(lo, min(hi, float(v)))

    def _clip_choices(self, exclude=None):
        """(label, id) for every video clip — populates the Match-color-to dropdown."""
        out = []
        for t in self._project.tracks:
            for cl in t.clips:
                if cl.id == exclude or cl.type == "text" or not cl.src:
                    continue
                if discovery.kind_of(cl.src) != "video":
                    continue
                out.append((f"{cl.label or cl.id} · {t.name}", cl.id))
        return out

    @staticmethod
    def _mid(clip) -> float:
        return float(clip.in_) + clip.src_len / 2.0

    def _auto_enhance(self):
        _, clip = self._sel()
        if clip is None:
            raise gr.Error("Select a clip on the timeline first.")
        if not clip.src or discovery.kind_of(clip.src) != "video":
            raise gr.Error("Auto-Enhance works on video clips.")
        st = render.frame_stats(clip.src, self._mid(clip))
        if not st:
            b, c, s, g = self._AUTO_PRESET
            msg = "✨ Auto-Enhance: couldn't read the frame — applied a default pop preset. Review and **Apply**."
        else:
            b = self._clamp((128.0 - st["ymean"]) / 255.0 * 0.8, -0.25, 0.25)
            c = self._clamp(55.0 / max(1.0, st["ystd"]), 1.0, 1.4)
            s = self._clamp(1.0 + (0.35 - st["sat"]) * 0.9, 1.0, 1.4) if st["sat"] < 0.35 else 1.0
            g = 1.0
            msg = (f"✨ Auto-Enhance: brightness {b:+.2f}, contrast {c:.2f}, "
                   f"saturation {s:.2f} — review and **Apply**.")
        return (gr.update(value=round(b, 3)), gr.update(value=round(c, 3)),
                gr.update(value=round(s, 3)), gr.update(value=round(g, 3)), msg)

    def _color_match(self, ref_id):
        _, clip = self._sel()
        if clip is None:
            raise gr.Error("Select the clip you want to grade first.")
        if not ref_id:
            raise gr.Error("Pick a reference clip in “Match color to…”.")
        _, ref = self._project.find_clip(ref_id)
        if ref is None:
            raise gr.Error("Reference clip not found — it may have been removed.")
        if not (clip.src and ref.src):
            raise gr.Error("Both clips need a video source to match colour.")
        ts = render.frame_stats(clip.src, self._mid(clip))
        rs = render.frame_stats(ref.src, self._mid(ref))
        if not ts or not rs:
            raise gr.Error("Couldn't analyse one of the frames (ffmpeg/frame unavailable).")
        b = self._clamp((rs["ymean"] - ts["ymean"]) / 255.0, -0.4, 0.4)
        c = self._clamp(rs["ystd"] / max(1.0, ts["ystd"]), 0.6, 1.8)
        s = self._clamp(rs["sat"] / max(0.02, ts["sat"]), 0.4, 2.0)
        # White balance: per-channel gain, made hue-only by dividing out the luma (geomean).
        gr_, gg, gb = (rs["r"] / max(1.0, ts["r"]), rs["g"] / max(1.0, ts["g"]),
                       rs["b"] / max(1.0, ts["b"]))
        k = (gr_ * gg * gb) ** (1.0 / 3.0) or 1.0
        gr_, gg, gb = gr_ / k, gg / k, gb / k
        temp = self._clamp((gr_ - gb) / 0.5, -1.0, 1.0)     # inverse of color_vf's mixer
        tint = self._clamp((1.0 - gg) / 0.15, -1.0, 1.0)
        msg = (f"🎯 Matched to **{ref.label or ref.id}**: bright {b:+.2f}, contrast {c:.2f}, "
               f"sat {s:.2f}, temp {temp:+.2f}, tint {tint:+.2f} — review and **Apply**.")
        return (gr.update(value=round(b, 3)), gr.update(value=round(c, 3)),
                gr.update(value=round(s, 3)), gr.update(value=round(temp, 3)),
                gr.update(value=round(tint, 3)), msg)

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
    # rename / mute / solo / lock / volume / delete / reorder are client-side on the
    # track heads (timeline.js); only add-track stays here — it mints a fresh id.
    def _add_track(self, kind):
        self._push_undo()
        trk = self._project.add_track(kind, "")
        return (self._env_after(), gr.update(choices=self._track_choices(), value=trk.id),
                f"Added track **{trk.name}**.")

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
        # The master "finish" controls are read at export/preview time (not round-tripped
        # through the timeline), so the JS can never clobber them. Ordered to match the
        # tail of _render / _preview and _pack_master.
        mst = [c["mst_color_on"], c["mst_bright"], c["mst_contrast"], c["mst_sat"],
               c["mst_temp"], c["mst_loud_on"], c["mst_lufs"], c["mst_sharpen_on"],
               c["mst_sharpen"], c["mst_denoise_on"], c["mst_denoise"], c["mst_interp_mode"],
               c["mst_interp_fps"], c["mst_lut_on"], c["mst_lut_path"]]
        c["export"].click(
            self._render,
            inputs=[c["preset"], c["quality"], c["resolution"], c["range_on"],
                    c["range_start"], c["range_end"]] + mst,
            outputs=[c["video"], c["save_as"], c["log"]])
        c["cancel"].click(self._cancel_render, outputs=[c["log"]])
        c["preview"].click(self._preview, inputs=[c["preview_secs"]] + mst,
                          outputs=[c["video"], c["log"]])
        c["mst_lut"].upload(self._master_lut, inputs=[c["mst_lut"]],
                           outputs=[c["mst_lut_path"], c["mst_lut_name"]])
        # consistency guardrails
        c["mst_check"].click(self._finish_check, inputs=[c["mst_color_on"]],
                            outputs=[c["mst_status"]])
        c["mst_match_all"].click(self._match_all_grades,
                                outputs=[self.tl_from_py, c["mst_status"]])
        c["mst_clear_all"].click(self._clear_all_grades,
                                outputs=[self.tl_from_py, c["mst_status"]])
        # The final cut is a VIDEO, so send it to the Video Generator as a Vid2Vid
        # source — the same host path the clip menu's "Send to Vid2Vid" uses
        # (get_current_model_settings + a form-refresh trigger + tab switch).
        state = getattr(self, "state", None)
        rft = getattr(self, "refresh_form_trigger", None)
        mt = getattr(self, "main_tabs", None)
        can = state is not None and callable(getattr(self, "get_current_model_settings", None))
        if can:
            out, self._cut_out = [], []
            if rft is not None:
                out.append(rft); self._cut_out.append("rft")
            if mt is not None:
                out.append(mt); self._cut_out.append("mt")
            out.append(c["log"]); self._cut_out.append("log")
            c["to_i2v"].click(self._send_cut_to_vid2vid, inputs=[state], outputs=out)
        else:
            c["to_i2v"].click(self._cut_unavailable, outputs=[c["log"]])

    @staticmethod
    def _pack_master(color_on, bright, contrast, sat, temp, loud_on, lufs,
                     sharpen_on, sharpen, denoise_on, denoise, interp_mode, interp_fps,
                     lut_on, lut_path):
        return {"color_on": bool(color_on), "brightness": float(bright),
                "contrast": float(contrast), "saturation": float(sat), "temp": float(temp),
                "loud_on": bool(loud_on), "loud_lufs": float(lufs),
                "sharpen_on": bool(sharpen_on), "sharpen": float(sharpen),
                "denoise_on": bool(denoise_on), "denoise": float(denoise),
                "interp": str(interp_mode or "off"), "interp_fps": float(interp_fps),
                "lut_on": bool(lut_on), "lut_path": str(lut_path or "")}

    def _master_lut(self, f):
        """Persist an uploaded .cube into renders_dir and report its name."""
        import shutil
        src = f if isinstance(f, str) else getattr(f, "name", None)
        if not src or not Path(src).exists():
            return "", "*no LUT loaded*"
        dest = paths.renders_dir() / Path(src).name
        try:
            paths.renders_dir().mkdir(parents=True, exist_ok=True)
            if str(Path(src).resolve()) != str(dest.resolve()):
                shutil.copy2(src, dest)
            path = str(dest)
        except Exception:
            path = src
        return path, f"LUT: **{Path(path).name}**"

    # -- finish / consistency guardrails ------------------------------------
    @staticmethod
    def _is_graded(col):
        if not col:
            return False
        return (abs(col.get("brightness", 0) or 0) > 1e-3
                or abs((col.get("contrast", 1) or 1) - 1) > 1e-3
                or abs((col.get("saturation", 1) or 1) - 1) > 1e-3
                or abs((col.get("gamma", 1) or 1) - 1) > 1e-3
                or abs(col.get("temp", 0) or 0) > 1e-3
                or abs(col.get("tint", 0) or 0) > 1e-3)

    def _graded_clips(self):
        out = []
        for t in self._project.tracks:
            if t.kind != "Video":
                continue
            for cl in t.clips:
                if cl.type != "text" and cl.src and self._is_graded(cl.color):
                    out.append(cl)
        return out

    def _finish_check(self, master_color_on):
        graded = self._graded_clips()
        notes = []
        if master_color_on and graded:
            notes.append(f"⚠ Master grade **+** {len(graded)} per-clip grade(s) — these "
                         "**stack** (clamped, but consider clearing one of them).")
        if len(graded) >= 2:
            sats = [float(c.color.get("saturation", 1.0)) for c in graded]
            brs = [float(c.color.get("brightness", 0.0)) for c in graded]
            if (max(sats) - min(sats) > 0.5) or (max(brs) - min(brs) > 0.25):
                notes.append("⚠ Per-clip grades vary a lot across the cut — "
                             "**Apply selected grade to all** to even them out.")
        if not notes:
            notes.append("✅ Finish looks consistent.")
        return "  \n".join(notes)

    def _match_all_grades(self):
        _, clip = self._sel()
        if clip is None:
            raise gr.Error("Select the reference clip on the timeline first.")
        ref = dict(clip.color or {})
        self._push_undo()
        n = 0
        for t in self._project.tracks:
            if t.kind != "Video":
                continue
            for cl in t.clips:
                if cl.type != "text" and cl.src:
                    cl.color = dict(ref); n += 1
        return self._env_after(), f"Applied **{clip.label or clip.id}**'s grade to {n} clip(s)."

    def _clear_all_grades(self):
        self._push_undo()
        n = 0
        for t in self._project.tracks:
            for cl in t.clips:
                if self._is_graded(cl.color):
                    cl.color = {}; n += 1
        return self._env_after(), f"Cleared per-clip grades on {n} clip(s)."

    def _render(self, preset, quality, resolution, range_on, rstart, rend,
                m_color_on, m_bright, m_contrast, m_sat, m_temp, m_loud_on, m_lufs,
                m_sharpen_on, m_sharpen, m_denoise_on, m_denoise, m_interp_mode, m_interp_fps,
                m_lut_on, m_lut_path, progress=gr.Progress()):
        from .core import rife
        master = self._pack_master(
            m_color_on, m_bright, m_contrast, m_sat, m_temp, m_loud_on, m_lufs,
            m_sharpen_on, m_sharpen, m_denoise_on, m_denoise, m_interp_mode, m_interp_fps,
            m_lut_on, m_lut_path)
        # RIFE runs as a post-encode pass; if it's selected but unavailable, fall back
        # to ffmpeg minterpolate inside the filtergraph so the user still gets smoothing.
        mode = master.get("interp", "off")
        use_rife = mode in ("rife2", "rife4") and rife.available()
        if mode in ("rife2", "rife4") and not use_rife:
            master["interp"] = "minterpolate"
        self._project.master = master
        if master.get("color_on") and self._graded_clips():
            gr.Warning("Master grade stacks on per-clip grades — check the Finish → "
                       "Consistency panel if the result looks over-processed.")
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
        msg = f"✅ Rendered `{out}`"
        if use_rife:
            exp = 1 if mode == "rife2" else 2
            dest = str(paths.renders_dir() / (Path(out).stem + f"_rife{2 ** exp}x.mp4"))
            r = rife.interpolate_file(out, dest, exp,
                                      progress=lambda f, d: progress(f, desc=d))
            if r:
                out = r; msg = f"✅ Rendered + RIFE ×{2 ** exp} `{out}`"
            else:
                gr.Warning("RIFE unavailable or the cut was too long for it — "
                           "exported without interpolation (try minterpolate).")
        self._last_render = out
        return out, gr.update(value=out), msg

    def _cancel_render(self):
        self._cancel_event.set()
        return "Cancelling render…"

    def _preview(self, secs, m_color_on, m_bright, m_contrast, m_sat, m_temp, m_loud_on,
                 m_lufs, m_sharpen_on, m_sharpen, m_denoise_on, m_denoise, m_interp_mode,
                 m_interp_fps, m_lut_on, m_lut_path, progress=gr.Progress()):
        """A true low-res composite of a window at the playhead (the real cut) — WITH
        the master finish stage, so you see the final look before committing."""
        master = self._pack_master(
            m_color_on, m_bright, m_contrast, m_sat, m_temp, m_loud_on, m_lufs,
            m_sharpen_on, m_sharpen, m_denoise_on, m_denoise, m_interp_mode, m_interp_fps,
            m_lut_on, m_lut_path)
        # Preview is meant to be quick: approximate RIFE with minterpolate (RIFE only
        # runs on the real export, where it's worth the GPU/decode cost).
        if master.get("interp") in ("rife2", "rife4"):
            master["interp"] = "minterpolate"
        self._project.master = master
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

    def _send_cut_to_vid2vid(self, state):
        """Hand the exported cut to the Video Generator as a Vid2Vid source — same
        host path as the clip menu's "Send to Vid2Vid", but on the whole render."""
        names = getattr(self, "_cut_out", ["log"])
        upd = {n: gr.update() for n in names}

        def pack():
            return upd[names[0]] if len(names) == 1 else tuple(upd[n] for n in names)

        if not self._last_render:
            gr.Warning("Export a cut first.")
            upd["log"] = "Export a cut first, then send it to Vid2Vid."
            return pack()
        try:
            s = self.get_current_model_settings(state)
            s["video_source"] = self._last_render
            ipt = s.get("image_prompt_type") or ""
            if "V" not in ipt:
                s["image_prompt_type"] = ("V" + ipt) if ipt else "V"
        except Exception:
            traceback.print_exc()
            upd["log"] = "❌ Couldn't hand the cut to the Video Generator."
            return pack()
        if "rft" in upd:
            upd["rft"] = time.time()
        if "mt" in upd:
            upd["mt"] = gr.Tabs(selected="video_gen")
        upd["log"] = "✅ Sent the final cut → Vid2Vid source."
        gr.Info("Sent the final cut to the Video Generator (Vid2Vid source).")
        return pack()

    def _cut_unavailable(self):
        if not self._last_render:
            return "Export a cut first, then open the Video Generator to use it."
        return (f"Open the Video Generator and load `{self._last_render}` as the "
                "Vid2Vid video source.")

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
