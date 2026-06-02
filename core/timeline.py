"""The Reel2Reel timeline data model — pure, no Gradio, no ffmpeg.

The live edit state is a flat, OpenShot-style clip list with explicit positions
in seconds; persistence emulates OpenTimelineIO's frame-rate-aware hierarchy.

Coordinate conventions, all floating-point SECONDS on the project timebase:
    clip.start  -> position on the timeline where the clip begins
    clip.in_    -> in-point within the SOURCE media
    clip.out    -> out-point within the source media
    clip.dur    -> on-timeline length = (out - in_) / speed     (media)
                                       = (out - in_)             (text clip, speed 1)

Clips carry edit attributes (gain, fades, opacity, mute), creative attributes
(speed/reverse, color, geometry/transform, text for titles) and per-property
keyframe automation. Editing ops (split, detach-audio, ripple, duplicate,
transitions, markers, multi-select copy/paste) live here so they're unit-testable
offline.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

SCHEMA = "Reel2ReelProject.2"

_id_counter = 0


def new_id(prefix: str = "c") -> str:
    global _id_counter
    _id_counter += 1
    return f"{prefix}{_id_counter}"


def _f(v, default: float = 0.0, lo: float = -1e7, hi: float = 1e7) -> float:
    """Sanitize a float: reject NaN/inf, clamp to a sane range."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x or x in (float("inf"), float("-inf")):
        return default
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
#  Dataclasses                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class Clip:
    id: str
    src: str = ""                  # source path ("" for text/title clips)
    start: float = 0.0             # timeline position (s)
    in_: float = 0.0               # source in-point (s)
    out: float = 0.0               # source out-point (s)
    track: str = ""
    label: str = ""
    type: str = "media"            # "media" | "text"
    gain_db: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    opacity: float = 1.0
    mute: bool = False
    has_audio: bool = False
    speed: float = 1.0             # playback speed (>0); reverse is separate
    reverse: bool = False
    color: Optional[dict] = None   # {brightness, contrast, saturation, gamma, temp, tint}
    geometry: Optional[dict] = None  # {x, y, scale, rotate, crop:{l,t,r,b}}
    text: Optional[dict] = None    # {content, size, color, box, box_color, x, y, font}
    keyframes: Optional[dict] = None  # {prop: [[t, v], ...]} t relative to clip start
    src_fps: Optional[float] = None
    src_dur: Optional[float] = None
    thumb: Optional[str] = None

    @property
    def speed_f(self) -> float:
        s = float(self.speed or 1.0)
        return s if s > 0.01 else 1.0

    @property
    def src_len(self) -> float:
        """Seconds consumed from the source."""
        return max(0.0, float(self.out) - float(self.in_))

    @property
    def dur(self) -> float:
        """On-timeline length (source length scaled by speed)."""
        return self.src_len / self.speed_f

    @property
    def end(self) -> float:
        return float(self.start) + self.dur


@dataclass
class Track:
    id: str
    kind: str = "Video"            # "Video" | "Audio"
    index: int = 0
    name: str = ""
    muted: bool = False
    solo: bool = False
    locked: bool = False
    volume_db: float = 0.0
    height: int = 0                # 0 = default; UI may override
    clips: list = field(default_factory=list)


@dataclass
class Transition:
    id: str
    track: str
    kind: str = "dissolve"         # dissolve | fade_black | fade_white | wipe | slide
    between: tuple = ("", "")
    position: float = 0.0
    duration: float = 0.5
    direction: str = "left"        # for wipe/slide: left|right|up|down


@dataclass
class Marker:
    id: str
    t: float = 0.0
    label: str = ""
    color: str = "#e0a106"


TRANSITION_KINDS = ("dissolve", "fade_black", "fade_white", "wipe", "slide")


# --------------------------------------------------------------------------- #
#  Timeline                                                                   #
# --------------------------------------------------------------------------- #

class Timeline:
    def __init__(self, name: str = "Cut 1", fps: int = 30, width: int = 1280,
                 height: int = 720, sample_rate: int = 48000,
                 tracks: Optional[list] = None, transitions: Optional[list] = None,
                 markers: Optional[list] = None, ui: Optional[dict] = None,
                 master: Optional[dict] = None):
        self.name = name
        self.fps = int(fps)
        self.width = int(width)
        self.height = int(height)
        self.sample_rate = int(sample_rate)
        self.tracks: list[Track] = tracks if tracks is not None else []
        self.transitions: list[Transition] = transitions if transitions is not None else []
        self.markers: list[Marker] = markers if markers is not None else []
        # Whole-cut "finish" stage applied once to the final composite — see
        # effects.master_vf / master_af. Keys (all optional, each gated by its *_on):
        #   color_on, brightness, contrast, saturation, temp,
        #   sharpen_on, sharpen, denoise_on, denoise,
        #   lut_on, lut_path, loud_on, loud_lufs
        self.master: dict = master or {}
        self.ui: dict = ui or {"px_per_sec": 80, "playhead": 0.0, "selected": None,
                               "selection": [], "snap": True}
        if tracks is None and not self.tracks:
            self.add_track("Video", "Video 1")
            self.add_track("Audio", "Audio 1")

    # -- tracks -------------------------------------------------------------
    def add_track(self, kind: str = "Video", name: str = "") -> Track:
        prefix = "V" if kind == "Video" else "A"
        same_kind = sum(1 for t in self.tracks if t.kind == kind) + 1
        tid = f"{prefix}{same_kind}"
        while any(t.id == tid for t in self.tracks):
            same_kind += 1
            tid = f"{prefix}{same_kind}"
        track = Track(id=tid, kind=kind, index=len(self.tracks),
                      name=name or f"{kind} {same_kind}")
        self.tracks.append(track)
        self._reindex()
        return track

    def get_track(self, track_id: str) -> Optional[Track]:
        return next((t for t in self.tracks if t.id == track_id), None)

    def first_track(self, kind: str) -> Optional[Track]:
        return next((t for t in self.tracks if t.kind == kind), None)

    def _reindex(self):
        for i, t in enumerate(self.tracks):
            t.index = i

    def _fresh_id(self, prefix: str = "c") -> str:
        existing = {c.id for _, c in self.all_clips()} | {t.id for t in self.tracks}
        existing |= {x.id for x in self.transitions} | {m.id for m in self.markers}
        cid = new_id(prefix)
        while cid in existing:
            cid = new_id(prefix)
        return cid

    def set_track(self, track_id: str, **props) -> bool:
        t = self.get_track(track_id)
        if t is None:
            return False
        for k in ("name", "muted", "solo", "locked"):
            if k in props and props[k] is not None:
                setattr(t, k, props[k])
        if props.get("volume_db") is not None:
            t.volume_db = _f(props["volume_db"], 0.0, -60, 24)
        if props.get("height") is not None:
            t.height = int(props["height"])
        return True

    def remove_track(self, track_id: str) -> bool:
        t = self.get_track(track_id)
        if t is None or len(self.tracks) <= 1:
            return False
        clip_ids = {c.id for c in t.clips}
        self.tracks.remove(t)
        self.transitions = [x for x in self.transitions
                            if x.track != track_id and x.between[0] not in clip_ids
                            and x.between[1] not in clip_ids]
        self._reindex()
        return True

    def move_track(self, track_id: str, delta: int) -> bool:
        idx = next((i for i, t in enumerate(self.tracks) if t.id == track_id), None)
        if idx is None:
            return False
        new = max(0, min(len(self.tracks) - 1, idx + delta))
        if new == idx:
            return False
        self.tracks.insert(new, self.tracks.pop(idx))
        self._reindex()
        return True

    def audible_tracks(self, kind: str) -> list[Track]:
        any_solo = any(t.solo for t in self.tracks)
        out = []
        for t in self.tracks:
            if t.kind != kind:
                continue
            if any_solo:
                if t.solo:
                    out.append(t)
            elif not t.muted:
                out.append(t)
        return out

    # -- clips --------------------------------------------------------------
    def all_clips(self):
        for t in self.tracks:
            for c in t.clips:
                yield t, c

    def find_clip(self, clip_id: str):
        for t in self.tracks:
            for c in t.clips:
                if c.id == clip_id:
                    return t, c
        return None, None

    def next_clip(self, clip_id: str):
        track, clip = self.find_clip(clip_id)
        if clip is None:
            return None, None
        ordered = sorted(track.clips, key=lambda c: c.start)
        i = ordered.index(clip)
        return (track, ordered[i + 1]) if i + 1 < len(ordered) else (track, None)

    def add_clip(self, clip: Clip, track_id: Optional[str] = None) -> Clip:
        track = self.get_track(track_id) if track_id else self.get_track(clip.track)
        if track is None:
            track = self.first_track("Video") or self.add_track("Video", "Video 1")
        clip.track = track.id
        track.clips.append(clip)
        track.clips.sort(key=lambda c: c.start)
        return clip

    def append_clip(self, src: str, kind: str = "Video", **kw) -> Clip:
        track = self.first_track(kind) or self.add_track(kind)
        start = max((c.end for c in track.clips), default=0.0)
        dur = kw.pop("dur", None)
        out = kw.pop("out", None)
        if out is None:
            out = dur if dur is not None else (kw.get("src_dur") or 0.0)
        clip = Clip(id=self._fresh_id("c"), src=str(src), start=start,
                    in_=kw.pop("in_", 0.0), out=float(out), track=track.id,
                    label=kw.pop("label", Path(str(src)).stem), **kw)
        track.clips.append(clip)
        return clip

    def add_text_clip(self, content: str = "Title", start: float = 0.0,
                      dur: float = 3.0, track_id: Optional[str] = None) -> Clip:
        track = self.get_track(track_id) if track_id else None
        track = track or self.first_track("Video") or self.add_track("Video", "Video 1")
        clip = Clip(id=self._fresh_id("t"), src="", type="text", start=max(0.0, start),
                    in_=0.0, out=max(0.1, dur), track=track.id, label=content[:24] or "Title",
                    text={"content": content, "size": 64, "color": "#ffffff", "box": True,
                          "box_color": "#000000aa", "x": "center", "y": "center"})
        track.clips.append(clip)
        track.clips.sort(key=lambda c: c.start)
        return clip

    def remove_clip(self, clip_id: str) -> bool:
        for t in self.tracks:
            for i, c in enumerate(t.clips):
                if c.id == clip_id:
                    del t.clips[i]
                    self._drop_transitions_for(clip_id)
                    return True
        return False

    def remove_clips(self, clip_ids) -> int:
        return sum(1 for cid in list(clip_ids) if self.remove_clip(cid))

    def ripple_delete(self, clip_id: str) -> bool:
        track, clip = self.find_clip(clip_id)
        if clip is None:
            return False
        gap, cut = clip.dur, clip.start
        track.clips.remove(clip)
        self._drop_transitions_for(clip_id)
        for c in track.clips:
            if c.start >= cut:
                c.start = max(0.0, c.start - gap)
        for x in self.transitions:
            if x.track == track.id and x.position >= cut:
                x.position = max(0.0, x.position - gap)
        track.clips.sort(key=lambda c: c.start)
        return True

    def duplicate_clip(self, clip_id: str) -> Optional[str]:
        track, clip = self.find_clip(clip_id)
        if clip is None:
            return None
        start = max((c.end for c in track.clips), default=clip.end)
        dup = Clip(**{**asdict(clip), "id": self._fresh_id("c"), "start": start})
        track.clips.append(dup)
        track.clips.sort(key=lambda c: c.start)
        return dup.id

    def move_clip(self, clip_id: str, start: float, track_id: Optional[str] = None) -> bool:
        track, clip = self.find_clip(clip_id)
        if clip is None:
            return False
        clip.start = max(0.0, _f(start))
        if track_id and track_id != track.id:
            dest = self.get_track(track_id)
            if dest is not None and dest.kind == track.kind:
                track.clips.remove(clip)
                clip.track = dest.id
                dest.clips.append(clip)
                dest.clips.sort(key=lambda c: c.start)
        else:
            track.clips.sort(key=lambda c: c.start)
        return True

    def trim_clip(self, clip_id, in_=None, out=None, start=None) -> bool:
        _, clip = self.find_clip(clip_id)
        if clip is None:
            return False
        if in_ is not None:
            clip.in_ = max(0.0, _f(in_))
        if out is not None:
            clip.out = max(clip.in_ + 1.0 / max(1, self.fps), _f(out))
        if start is not None:
            clip.start = max(0.0, _f(start))
        return True

    def set_clip(self, clip_id: str, **props) -> bool:
        _, clip = self.find_clip(clip_id)
        if clip is None:
            return False
        if props.get("label") is not None:
            clip.label = props["label"]
        for k in ("gain_db", "fade_in", "fade_out", "opacity", "start", "in_", "out",
                  "speed"):
            if props.get(k) is not None:
                setattr(clip, k, _f(props[k]))
        for k in ("mute", "reverse"):
            if props.get(k) is not None:
                setattr(clip, k, bool(props[k]))
        for k in ("color", "geometry", "text", "keyframes"):
            if k in props:
                setattr(clip, k, props[k])
        # invariants
        clip.in_ = max(0.0, clip.in_)
        clip.out = max(clip.in_ + 1.0 / max(1, self.fps), clip.out)
        clip.speed = 1.0 if clip.type == "text" else clip.speed_f
        clip.fade_in = max(0.0, min(clip.fade_in, clip.dur))
        clip.fade_out = max(0.0, min(clip.fade_out, clip.dur))
        clip.opacity = max(0.0, min(1.0, clip.opacity))
        return True

    def split_at(self, track_id: str, t: float) -> list[str]:
        """Razor: split clips on ``track_id`` straddling timeline time ``t``."""
        track = self.get_track(track_id)
        if track is None:
            return []
        created: list[str] = []
        for clip in list(track.clips):
            if clip.start < t < clip.end and clip.dur > 1.0 / self.fps:
                src_off = (t - clip.start) * clip.speed_f      # source seconds into clip
                right = Clip(**{**asdict(clip), "id": self._fresh_id("c"), "start": t,
                               "in_": clip.in_ + src_off, "fade_in": 0.0})
                clip.out = clip.in_ + src_off
                clip.fade_out = 0.0
                clip.fade_in = min(clip.fade_in, clip.dur)      # halves are shorter now
                right.fade_out = min(right.fade_out, right.dur)
                track.clips.append(right)
                created.append(right.id)
        track.clips.sort(key=lambda c: c.start)
        return created

    # -- multi-select copy / paste -----------------------------------------
    def serialize_clips(self, clip_ids) -> dict:
        ids = set(clip_ids or [])
        clips, base = [], None
        for _, c in self.all_clips():
            if c.id in ids:
                clips.append(asdict(c))
                base = c.start if base is None else min(base, c.start)
        return {"base": base or 0.0, "clips": clips}

    def paste_clips(self, data: dict, at: float, track_id: Optional[str] = None) -> list[str]:
        base = float((data or {}).get("base", 0.0))
        out = []
        for cd in (data or {}).get("clips", []):
            track = self.get_track(track_id) or self.get_track(cd.get("track")) \
                or self.first_track("Video")
            if track is None:
                continue
            cd = {**cd, "id": self._fresh_id("c"), "track": track.id,
                  "start": max(0.0, at + (float(cd.get("start", 0.0)) - base))}
            cd.pop("in", None)
            clip = _clip_from_dict(cd, track.id)
            track.clips.append(clip)
            out.append(clip.id)
        for t in self.tracks:
            t.clips.sort(key=lambda c: c.start)
        return out

    # -- markers ------------------------------------------------------------
    def add_marker(self, t: float, label: str = "", color: str = "#e0a106") -> str:
        m = Marker(id=self._fresh_id("m"), t=max(0.0, _f(t)), label=label, color=color)
        self.markers.append(m)
        self.markers.sort(key=lambda m: m.t)
        return m.id

    def remove_marker(self, marker_id: str) -> bool:
        n = len(self.markers)
        self.markers = [m for m in self.markers if m.id != marker_id]
        return len(self.markers) != n

    # -- audio from video ---------------------------------------------------
    def detach_audio(self, clip_id: str) -> Optional[str]:
        track, clip = self.find_clip(clip_id)
        if clip is None or track.kind != "Video":
            return None
        atrack = self.first_track("Audio") or self.add_track("Audio", "Audio 1")
        adet = Clip(id=self._fresh_id("a"), src=clip.src, start=clip.start, in_=clip.in_,
                    out=clip.out, track=atrack.id, label=f"{clip.label} (audio)",
                    gain_db=clip.gain_db, has_audio=True, speed=clip.speed,
                    reverse=clip.reverse, src_dur=clip.src_dur, src_fps=clip.src_fps)
        atrack.clips.append(adet)
        atrack.clips.sort(key=lambda c: c.start)
        clip.mute = True
        return adet.id

    # -- transitions --------------------------------------------------------
    def _drop_transitions_for(self, clip_id: str):
        self.transitions = [x for x in self.transitions
                            if clip_id not in (x.between[0], x.between[1])]

    def transition_for(self, clip_id: str) -> Optional[Transition]:
        return next((x for x in self.transitions if x.between[0] == clip_id), None)

    def add_transition(self, clip_id: str, duration: float = 0.5,
                       kind: str = "dissolve", direction: str = "left") -> Optional[str]:
        track, clip = self.find_clip(clip_id)
        if clip is None:
            return None
        _, nxt = self.next_clip(clip_id)
        if nxt is None:
            return None
        if clip.dur < 1.0 / (self.fps) or nxt.dur < 1.0 / (self.fps):
            return None
        d = max(1.0 / self.fps, min(float(duration), clip.dur * 0.9, nxt.dur * 0.9))
        target_start = clip.end - d
        shift = nxt.start - target_start
        if abs(shift) > 1e-6:
            for c in sorted(track.clips, key=lambda c: c.start):
                if c.start >= nxt.start - 1e-6:
                    c.start = max(0.0, c.start - shift)
            for x in self.transitions:
                if x.track == track.id and x.position >= nxt.start - 1e-6:
                    x.position = max(0.0, x.position - shift)
        self._drop_transitions_for(clip_id)
        kind = kind if kind in TRANSITION_KINDS else "dissolve"
        tr = Transition(id=self._fresh_id("x"), track=track.id, kind=kind,
                        between=(clip.id, nxt.id), position=clip.end - d, duration=d,
                        direction=direction)
        self.transitions.append(tr)
        track.clips.sort(key=lambda c: c.start)
        return tr.id

    def remove_transition(self, clip_id: str) -> bool:
        tr = self.transition_for(clip_id)
        if tr is None:
            return False
        track = self.get_track(tr.track)
        _, nxt = self.find_clip(tr.between[1])
        self.transitions.remove(tr)
        if track and nxt is not None:
            for c in sorted(track.clips, key=lambda c: c.start):
                if c.start >= nxt.start - 1e-6:
                    c.start += tr.duration
            track.clips.sort(key=lambda c: c.start)
        return True

    # -- validation / duration ----------------------------------------------
    def sanitize(self) -> "Timeline":
        """Clamp clips to valid ranges + re-clamp transitions; call after loading
        from any external source (browser JSON, disk)."""
        minf = 1.0 / max(1, self.fps)
        for t in self.tracks:
            for c in t.clips:
                c.start = max(0.0, _f(c.start))
                c.in_ = max(0.0, _f(c.in_))
                c.speed = c.speed_f
                if c.type == "text":
                    c.speed = 1.0                       # text clips ignore speed
                    c.out = max(minf, _f(c.out, minf))
                else:
                    c.out = max(c.in_ + minf, _f(c.out))
                c.opacity = max(0.0, min(1.0, _f(c.opacity, 1.0)))
            t.clips.sort(key=lambda c: c.start)
        # drop transitions whose neighbours no longer fit
        good = []
        for x in self.transitions:
            _, a = self.find_clip(x.between[0])
            _, b = self.find_clip(x.between[1])
            if a is None or b is None:
                continue
            x.duration = max(minf, min(x.duration, a.dur * 0.9, b.dur * 0.9))
            good.append(x)
        self.transitions = good
        return self

    def total_duration(self) -> float:
        return max((c.end for _, c in self.all_clips()), default=0.0)

    def video_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "Video"]

    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "Audio"]

    # -- the flat browser edit state ----------------------------------------
    def to_edit_json(self) -> dict:
        clips = []
        for t in self.tracks:
            for c in t.clips:
                clips.append({
                    "id": c.id, "track": c.track, "src": c.src, "url": None,
                    "type": c.type, "start": round(float(c.start), 4),
                    "in": round(float(c.in_), 4), "out": round(float(c.out), 4),
                    "dur": round(c.dur, 4), "label": c.label, "gain_db": c.gain_db,
                    "fade_in": c.fade_in, "fade_out": c.fade_out, "opacity": c.opacity,
                    "mute": c.mute, "has_audio": c.has_audio, "speed": c.speed,
                    "reverse": c.reverse, "color": c.color, "geometry": c.geometry,
                    "text": c.text, "keyframes": c.keyframes, "kind": t.kind,
                    "thumb": c.thumb, "thumb_url": None, "src_fps": c.src_fps,
                })
        return {
            "name": self.name, "fps": self.fps, "width": self.width,
            "height": self.height, "sample_rate": self.sample_rate,
            "tracks": [{"id": t.id, "kind": t.kind, "index": t.index, "name": t.name,
                        "muted": t.muted, "solo": t.solo, "locked": t.locked,
                        "volume_db": t.volume_db, "height": t.height} for t in self.tracks],
            "clips": clips,
            "transitions": [{"id": x.id, "track": x.track, "kind": x.kind,
                             "between": list(x.between), "position": x.position,
                             "duration": x.duration, "direction": x.direction}
                            for x in self.transitions],
            "markers": [{"id": m.id, "t": m.t, "label": m.label, "color": m.color}
                        for m in self.markers],
            "ui": self.ui,
        }

    @classmethod
    def from_edit_json(cls, d: dict) -> "Timeline":
        # NB: master (the render-time finish stage) is intentionally NOT round-tripped
        # through the browser edit payload — it's set server-side from the Render-tab
        # controls at export/preview time, so the JS can't clobber it.
        tl = cls(name=d.get("name", "Cut 1"), fps=int(d.get("fps", 30)),
                 width=int(d.get("width", 1280)), height=int(d.get("height", 720)),
                 sample_rate=int(d.get("sample_rate", 48000)), tracks=[],
                 ui=d.get("ui") or None)
        for td in d.get("tracks", []):
            tl.tracks.append(_track_from_dict(td, len(tl.tracks)))
        if not tl.tracks:
            tl.add_track("Video", "Video 1")
        for cd in d.get("clips", []):
            track = tl.get_track(cd.get("track", "")) or tl.tracks[0]
            track.clips.append(_clip_from_dict(cd, track.id))
        for x in d.get("transitions", []):
            tl.transitions.append(_transition_from_dict(x))
        for m in d.get("markers", []):
            tl.markers.append(Marker(id=m["id"], t=_f(m.get("t", 0.0)),
                                     label=m.get("label", ""), color=m.get("color", "#e0a106")))
        return tl.sanitize()


def _track_from_dict(td: dict, fallback_index: int) -> Track:
    return Track(id=td["id"], kind=td.get("kind", "Video"),
                 index=int(td.get("index", fallback_index)), name=td.get("name", ""),
                 muted=bool(td.get("muted")), solo=bool(td.get("solo")),
                 locked=bool(td.get("locked")), volume_db=_f(td.get("volume_db", 0.0)),
                 height=int(td.get("height", 0) or 0))


def _transition_from_dict(x: dict) -> Transition:
    return Transition(id=x["id"], track=x.get("track", ""), kind=x.get("kind", "dissolve"),
                      between=tuple(x.get("between", ("", ""))),
                      position=_f(x.get("position", 0.0)), duration=_f(x.get("duration", 0.5), 0.5),
                      direction=x.get("direction", "left"))


def _clip_from_dict(cd: dict, track_id: str) -> Clip:
    return Clip(
        id=cd["id"], src=cd.get("src", ""), start=_f(cd.get("start", 0.0)),
        in_=_f(cd.get("in_", cd.get("in", 0.0))), out=_f(cd.get("out", 0.0)),
        track=track_id, label=cd.get("label", ""), type=cd.get("type", "media"),
        gain_db=_f(cd.get("gain_db", 0.0)), fade_in=_f(cd.get("fade_in", 0.0)),
        fade_out=_f(cd.get("fade_out", 0.0)), opacity=_f(cd.get("opacity", 1.0), 1.0),
        mute=bool(cd.get("mute", False)), has_audio=bool(cd.get("has_audio", False)),
        speed=_f(cd.get("speed", 1.0), 1.0), reverse=bool(cd.get("reverse", False)),
        color=cd.get("color"), geometry=cd.get("geometry"), text=cd.get("text"),
        keyframes=cd.get("keyframes"), src_fps=cd.get("src_fps"), src_dur=cd.get("src_dur"),
        thumb=cd.get("thumb"))


# --------------------------------------------------------------------------- #
#  Persistence — the Reel2ReelProject document                                #
# --------------------------------------------------------------------------- #

def to_document(tl: Timeline) -> dict:
    doc = {
        "schema": SCHEMA, "name": tl.name, "fps": tl.fps, "width": tl.width,
        "height": tl.height, "sample_rate": tl.sample_rate, "tracks": [],
        "transitions": [{"id": x.id, "track": x.track, "kind": x.kind,
                         "between": list(x.between), "position": x.position,
                         "duration": x.duration, "direction": x.direction}
                        for x in tl.transitions],
        "markers": [{"id": m.id, "t": m.t, "label": m.label, "color": m.color}
                    for m in tl.markers],
        "ui": tl.ui,
    }
    for t in tl.tracks:
        doc["tracks"].append({
            "id": t.id, "kind": t.kind, "index": t.index, "name": t.name,
            "muted": t.muted, "solo": t.solo, "locked": t.locked,
            "volume_db": t.volume_db, "height": t.height,
            "clips": [asdict(c) for c in t.clips],
        })
    return doc


def from_document(doc: dict) -> Timeline:
    tl = Timeline(name=doc.get("name", "Cut 1"), fps=int(doc.get("fps", 30)),
                  width=int(doc.get("width", 1280)), height=int(doc.get("height", 720)),
                  sample_rate=int(doc.get("sample_rate", 48000)), tracks=[],
                  ui=doc.get("ui") or None)
    for td in doc.get("tracks", []):
        track = _track_from_dict(td, len(tl.tracks))
        for cd in td.get("clips", []):
            track.clips.append(_clip_from_dict(cd, track.id))
        tl.tracks.append(track)
    if not tl.tracks:
        tl.add_track("Video", "Video 1")
        tl.add_track("Audio", "Audio 1")
    for x in doc.get("transitions", []):
        tl.transitions.append(_transition_from_dict(x))
    for m in doc.get("markers", []):
        tl.markers.append(Marker(id=m["id"], t=_f(m.get("t", 0.0)),
                                 label=m.get("label", ""), color=m.get("color", "#e0a106")))
    return tl.sanitize()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(path, tl: Timeline) -> str:
    p = Path(path)
    _atomic_write(p, json.dumps(to_document(tl), indent=2))
    return str(p)


def load(path) -> Timeline:
    return from_document(json.loads(Path(path).read_text()))
