"""The Reel2Reel timeline data model — pure, no Gradio, no ffmpeg.

Design (see the README): the *live edit state* is a flat, OpenShot-style clip
list with explicit positions in seconds — trivial to drag and snap in the
browser — while persistence emulates OpenTimelineIO's frame-rate-aware hierarchy
(Timeline -> Tracks -> Clips). Both share field names so the converters
(``to_edit_json`` / ``from_edit_json`` and ``save`` / ``load``) stay thin.

Coordinate conventions, all in floating-point SECONDS on the project timebase:
    clip.start  -> position on the timeline where the clip begins
    clip.in_    -> in-point within the SOURCE media
    clip.out    -> out-point within the source media
    clip.dur    -> out - in_  (derived; the clip's length on the timeline)

A clip plays ``src[in_:out]`` starting at ``start`` on its track. Editing
operations (split, detach-audio, ripple-delete, duplicate, transitions, fades,
gain, mute/solo, track ops) live here so they're unit-testable offline.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

SCHEMA = "Reel2ReelProject.1"

_id_counter = 0


def new_id(prefix: str = "c") -> str:
    global _id_counter
    _id_counter += 1
    return f"{prefix}{_id_counter}"


# --------------------------------------------------------------------------- #
#  Dataclasses                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class Clip:
    id: str
    src: str                       # absolute path to the source media
    start: float = 0.0             # timeline position (s)
    in_: float = 0.0               # source in-point (s)
    out: float = 0.0               # source out-point (s)
    track: str = ""                # owning track id
    label: str = ""
    gain_db: float = 0.0           # audio gain
    fade_in: float = 0.0           # fade-in length (s) — video and/or audio
    fade_out: float = 0.0          # fade-out length (s)
    opacity: float = 1.0           # 0..1, for overlay (upper) video tracks
    mute: bool = False             # mute this clip's embedded audio in the render
    has_audio: bool = False        # source carries an audio stream (set on import)
    src_fps: Optional[float] = None
    src_dur: Optional[float] = None
    geometry: Optional[dict] = None  # {x, y, scale} reserved for picture-in-picture
    thumb: Optional[str] = None    # poster frame (video/image) or waveform (audio)

    @property
    def dur(self) -> float:
        return max(0.0, float(self.out) - float(self.in_))

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
    volume_db: float = 0.0         # track-level audio gain
    clips: list = field(default_factory=list)  # list[Clip]


@dataclass
class Transition:
    id: str
    track: str
    kind: str = "dissolve"         # video cross-dissolve (xfade)
    between: tuple = ("", "")      # (left_clip_id, right_clip_id)
    position: float = 0.0          # timeline time where the overlap begins
    duration: float = 0.5


# --------------------------------------------------------------------------- #
#  Timeline                                                                   #
# --------------------------------------------------------------------------- #

class Timeline:
    def __init__(self, name: str = "Cut 1", fps: int = 30, width: int = 1280,
                 height: int = 720, sample_rate: int = 48000,
                 tracks: Optional[list] = None, transitions: Optional[list] = None,
                 ui: Optional[dict] = None):
        self.name = name
        self.fps = int(fps)
        self.width = int(width)
        self.height = int(height)
        self.sample_rate = int(sample_rate)
        self.tracks: list[Track] = tracks if tracks is not None else []
        self.transitions: list[Transition] = transitions if transitions is not None else []
        self.ui: dict = ui or {"px_per_sec": 80, "playhead": 0.0, "selected": None,
                               "snap": True}
        # Auto-create the default V1/A1 only for a brand-new timeline. Loaders pass
        # tracks=[] and populate themselves (with their own empty-doc fallback), so
        # an explicit list — even empty — must NOT trigger defaults (else loading a
        # 2-track project yields 4 tracks, compounding on every undo/reload).
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
        """An id that collides with no existing clip/track id — robust against
        manual ids and ids carried in from a loaded project."""
        existing = {c.id for _, c in self.all_clips()} | {t.id for t in self.tracks}
        existing |= {x.id for x in self.transitions}
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
            t.volume_db = float(props["volume_db"])
        return True

    def remove_track(self, track_id: str) -> bool:
        t = self.get_track(track_id)
        if t is None or len(self.tracks) <= 1:
            return False
        clip_ids = {c.id for c in t.clips}
        self.tracks.remove(t)
        self.transitions = [x for x in self.transitions
                            if x.track != track_id
                            and x.between[0] not in clip_ids
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
        """Tracks of ``kind`` that contribute to the render, honoring solo/mute.
        Solo is global: if any track is soloed, only soloed tracks are audible."""
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
        """The clip immediately after ``clip_id`` on the same track (by start)."""
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

    def remove_clip(self, clip_id: str) -> bool:
        for t in self.tracks:
            for i, c in enumerate(t.clips):
                if c.id == clip_id:
                    del t.clips[i]
                    self._drop_transitions_for(clip_id)
                    return True
        return False

    def ripple_delete(self, clip_id: str) -> bool:
        """Remove a clip and slide later clips on the same track left to close
        the gap it left."""
        track, clip = self.find_clip(clip_id)
        if clip is None:
            return False
        gap = clip.dur
        cut = clip.start
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
        clip.start = max(0.0, float(start))
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
            clip.in_ = max(0.0, float(in_))
        if out is not None:
            clip.out = max(clip.in_ + 1.0 / max(1, self.fps), float(out))
        if start is not None:
            clip.start = max(0.0, float(start))
        return True

    def set_clip(self, clip_id: str, **props) -> bool:
        _, clip = self.find_clip(clip_id)
        if clip is None:
            return False
        if props.get("label") is not None:
            clip.label = props["label"]
        for k in ("gain_db", "fade_in", "fade_out", "opacity", "start", "in_", "out"):
            if props.get(k) is not None:
                setattr(clip, k, float(props[k]))
        if props.get("mute") is not None:
            clip.mute = bool(props["mute"])
        clip.fade_in = max(0.0, min(clip.fade_in, clip.dur))
        clip.fade_out = max(0.0, min(clip.fade_out, clip.dur))
        clip.opacity = max(0.0, min(1.0, clip.opacity))
        return True

    def split_at(self, track_id: str, t: float) -> list[str]:
        """Razor: split every clip on ``track_id`` straddling time ``t``."""
        track = self.get_track(track_id)
        if track is None:
            return []
        created: list[str] = []
        for clip in list(track.clips):
            if clip.start < t < clip.end:
                offset = t - clip.start
                right = Clip(**{**asdict(clip), "id": self._fresh_id("c"), "start": t,
                               "in_": clip.in_ + offset, "fade_in": 0.0})
                clip.out = clip.in_ + offset       # left ends at the cut
                clip.fade_out = 0.0
                track.clips.append(right)
                created.append(right.id)
        track.clips.sort(key=lambda c: c.start)
        return created

    # -- audio from video ---------------------------------------------------
    def detach_audio(self, clip_id: str) -> Optional[str]:
        """Split a video clip's audio onto a (new if needed) audio track so it can
        be moved/trimmed/gained independently, and mute the video clip's audio."""
        track, clip = self.find_clip(clip_id)
        if clip is None or track.kind != "Video":
            return None
        atrack = self.first_track("Audio") or self.add_track("Audio", "Audio 1")
        adet = Clip(id=self._fresh_id("a"), src=clip.src, start=clip.start,
                    in_=clip.in_, out=clip.out, track=atrack.id,
                    label=f"{clip.label} (audio)", gain_db=clip.gain_db,
                    has_audio=True, src_dur=clip.src_dur, src_fps=clip.src_fps)
        atrack.clips.append(adet)
        atrack.clips.sort(key=lambda c: c.start)
        clip.mute = True                            # the video no longer renders its audio
        return adet.id

    # -- transitions --------------------------------------------------------
    def _drop_transitions_for(self, clip_id: str):
        self.transitions = [x for x in self.transitions
                            if clip_id not in (x.between[0], x.between[1])]

    def transition_for(self, clip_id: str) -> Optional[Transition]:
        return next((x for x in self.transitions if x.between[0] == clip_id), None)

    def add_transition(self, clip_id: str, duration: float = 0.5,
                       kind: str = "dissolve") -> Optional[str]:
        """Cross-dissolve from ``clip_id`` into the next clip on its track. The
        next clip (and everything after it) ripples left so the two overlap by
        ``duration``."""
        track, clip = self.find_clip(clip_id)
        if clip is None:
            return None
        _, nxt = self.next_clip(clip_id)
        if nxt is None:
            return None
        d = max(1.0 / max(1, self.fps), min(float(duration), clip.dur, nxt.dur))
        target_start = clip.end - d
        shift = nxt.start - target_start            # >0 => move left
        if abs(shift) > 1e-6:
            for c in sorted(track.clips, key=lambda c: c.start):
                if c.start >= nxt.start - 1e-6:
                    c.start = max(0.0, c.start - shift)
            for x in self.transitions:
                if x.track == track.id and x.position >= nxt.start - 1e-6:
                    x.position = max(0.0, x.position - shift)
        self._drop_transitions_for(clip_id)
        tr = Transition(id=self._fresh_id("t"), track=track.id, kind=kind,
                        between=(clip.id, nxt.id), position=clip.end - d, duration=d)
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
        # un-ripple: open the overlap back up so it's a clean cut
        if track and nxt is not None:
            for c in sorted(track.clips, key=lambda c: c.start):
                if c.start >= nxt.start - 1e-6:
                    c.start += tr.duration
            track.clips.sort(key=lambda c: c.start)
        return True

    # -- duration -----------------------------------------------------------
    def total_duration(self) -> float:
        return max((c.end for _, c in self.all_clips()), default=0.0)

    def video_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "Video"]

    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "Audio"]

    # -- the flat browser edit state ----------------------------------------
    def to_edit_json(self) -> dict:
        """The flat shape the JS timeline consumes. URLs are filled in by the
        plugin layer (this module is path-only / pure)."""
        clips = []
        for t in self.tracks:
            for c in t.clips:
                clips.append({
                    "id": c.id, "track": c.track, "src": c.src, "url": None,
                    "start": round(float(c.start), 4), "in": round(float(c.in_), 4),
                    "out": round(float(c.out), 4), "dur": round(c.dur, 4),
                    "label": c.label, "gain_db": c.gain_db, "fade_in": c.fade_in,
                    "fade_out": c.fade_out, "opacity": c.opacity, "mute": c.mute,
                    "has_audio": c.has_audio, "kind": t.kind, "thumb": c.thumb,
                    "thumb_url": None, "src_fps": c.src_fps,
                })
        return {
            "name": self.name, "fps": self.fps, "width": self.width,
            "height": self.height, "sample_rate": self.sample_rate,
            "tracks": [{"id": t.id, "kind": t.kind, "index": t.index, "name": t.name,
                        "muted": t.muted, "solo": t.solo, "locked": t.locked,
                        "volume_db": t.volume_db} for t in self.tracks],
            "clips": clips,
            "transitions": [{"id": x.id, "track": x.track, "kind": x.kind,
                             "between": list(x.between), "position": x.position,
                             "duration": x.duration} for x in self.transitions],
            "ui": self.ui,
        }

    @classmethod
    def from_edit_json(cls, d: dict) -> "Timeline":
        tl = cls(name=d.get("name", "Cut 1"), fps=int(d.get("fps", 24)),
                 width=int(d.get("width", 1280)), height=int(d.get("height", 720)),
                 sample_rate=int(d.get("sample_rate", 48000)), tracks=[],
                 ui=d.get("ui") or None)
        for td in d.get("tracks", []):
            tl.tracks.append(Track(
                id=td["id"], kind=td.get("kind", "Video"),
                index=int(td.get("index", len(tl.tracks))), name=td.get("name", ""),
                muted=bool(td.get("muted")), solo=bool(td.get("solo")),
                locked=bool(td.get("locked")), volume_db=float(td.get("volume_db", 0.0))))
        if not tl.tracks:
            tl.add_track("Video", "Video 1")
        for cd in d.get("clips", []):
            track = tl.get_track(cd.get("track", "")) or tl.tracks[0]
            track.clips.append(_clip_from_dict(cd, track.id))
        for x in d.get("transitions", []):
            tl.transitions.append(Transition(
                id=x["id"], track=x.get("track", ""), kind=x.get("kind", "dissolve"),
                between=tuple(x.get("between", ("", ""))),
                position=float(x.get("position", 0.0)),
                duration=float(x.get("duration", 0.5))))
        for t in tl.tracks:
            t.clips.sort(key=lambda c: c.start)
        return tl


def _clip_from_dict(cd: dict, track_id: str) -> Clip:
    return Clip(
        id=cd["id"], src=cd.get("src", ""), start=float(cd.get("start", 0.0)),
        in_=float(cd.get("in_", cd.get("in", 0.0))), out=float(cd.get("out", 0.0)),
        track=track_id, label=cd.get("label", ""),
        gain_db=float(cd.get("gain_db", 0.0)), fade_in=float(cd.get("fade_in", 0.0)),
        fade_out=float(cd.get("fade_out", 0.0)), opacity=float(cd.get("opacity", 1.0)),
        mute=bool(cd.get("mute", False)), has_audio=bool(cd.get("has_audio", False)),
        src_fps=cd.get("src_fps"), src_dur=cd.get("src_dur"),
        geometry=cd.get("geometry"), thumb=cd.get("thumb"))


# --------------------------------------------------------------------------- #
#  Persistence — the Reel2ReelProject.1 document                              #
# --------------------------------------------------------------------------- #

def to_document(tl: Timeline) -> dict:
    doc = {
        "schema": SCHEMA, "name": tl.name, "fps": tl.fps, "width": tl.width,
        "height": tl.height, "sample_rate": tl.sample_rate, "tracks": [],
        "transitions": [{"id": x.id, "track": x.track, "kind": x.kind,
                         "between": list(x.between), "position": x.position,
                         "duration": x.duration} for x in tl.transitions],
        "ui": tl.ui,
    }
    for t in tl.tracks:
        doc["tracks"].append({
            "id": t.id, "kind": t.kind, "index": t.index, "name": t.name,
            "muted": t.muted, "solo": t.solo, "locked": t.locked,
            "volume_db": t.volume_db,
            "clips": [asdict(c) for c in t.clips],
        })
    return doc


def from_document(doc: dict) -> Timeline:
    tl = Timeline(name=doc.get("name", "Cut 1"), fps=int(doc.get("fps", 24)),
                  width=int(doc.get("width", 1280)), height=int(doc.get("height", 720)),
                  sample_rate=int(doc.get("sample_rate", 48000)), tracks=[],
                  ui=doc.get("ui") or None)
    for td in doc.get("tracks", []):
        track = Track(id=td["id"], kind=td.get("kind", "Video"),
                      index=int(td.get("index", len(tl.tracks))), name=td.get("name", ""),
                      muted=bool(td.get("muted")), solo=bool(td.get("solo")),
                      locked=bool(td.get("locked")), volume_db=float(td.get("volume_db", 0.0)))
        for cd in td.get("clips", []):
            track.clips.append(_clip_from_dict(cd, track.id))
        tl.tracks.append(track)
    if not tl.tracks:
        tl.add_track("Video", "Video 1")
        tl.add_track("Audio", "Audio 1")
    for x in doc.get("transitions", []):
        tl.transitions.append(Transition(
            id=x["id"], track=x.get("track", ""), kind=x.get("kind", "dissolve"),
            between=tuple(x.get("between", ("", ""))),
            position=float(x.get("position", 0.0)), duration=float(x.get("duration", 0.5))))
    return tl


def save(path, tl: Timeline) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(to_document(tl), indent=2))
    return str(p)


def load(path) -> Timeline:
    return from_document(json.loads(Path(path).read_text()))
