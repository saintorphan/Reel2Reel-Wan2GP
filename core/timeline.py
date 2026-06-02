"""The Reel2Reel timeline data model — pure, no Gradio, no ffmpeg.

Design (see the architecture notes in the README): the *live edit state* is a
flat, OpenShot-style clip list with explicit positions in seconds — trivial to
drag and snap in the browser — while persistence emulates OpenTimelineIO's
frame-rate-aware hierarchy (Timeline -> Tracks -> Clips). Both share the same
field names, so the converters (``to_edit_json`` / ``from_edit_json`` and the
``save``/``load`` round-trip) stay thin.

Coordinate conventions, all in floating-point SECONDS on the project timebase:
    clip.start  -> position on the timeline where the clip begins
    clip.in_    -> in-point within the SOURCE media
    clip.out    -> out-point within the source media
    clip.dur    -> out - in_  (derived; the clip's length on the timeline)

A clip therefore plays ``src[in_:out]`` starting at ``start`` on its track.

This module is the offline unit-test target (tests/test_core.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

SCHEMA = "Reel2ReelProject.1"

_id_counter = 0


def new_id(prefix: str = "c") -> str:
    """A short, process-unique id. Deterministic enough for tests, unique enough
    for a session (ids are also re-homed on load to avoid collisions)."""
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
    gain_db: float = 0.0           # audio gain (audio clips)
    src_fps: Optional[float] = None
    src_dur: Optional[float] = None
    geometry: Optional[dict] = None  # {x, y, scale} for overlay video tracks
    thumb: Optional[str] = None    # absolute path to a poster frame

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
    locked: bool = False
    clips: list = field(default_factory=list)  # list[Clip]


@dataclass
class Transition:
    id: str
    track: str
    kind: str = "dissolve"
    between: tuple = ("", "")      # (left_clip_id, right_clip_id)
    position: float = 0.0          # timeline time where the transition is centered/starts
    duration: float = 0.5


# --------------------------------------------------------------------------- #
#  Timeline                                                                   #
# --------------------------------------------------------------------------- #

class Timeline:
    def __init__(self, name: str = "Cut 1", fps: int = 24, width: int = 1280,
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
        self.ui: dict = ui or {"px_per_sec": 80, "playhead": 0.0, "selected": None}
        if not self.tracks:
            self.add_track("Video", "Video 1")
            self.add_track("Audio", "Audio 1")

    # -- tracks -------------------------------------------------------------
    def add_track(self, kind: str = "Video", name: str = "") -> Track:
        idx = len(self.tracks)
        prefix = "V" if kind == "Video" else "A"
        same_kind = sum(1 for t in self.tracks if t.kind == kind) + 1
        tid = f"{prefix}{same_kind}"
        # Guarantee uniqueness even after deletes.
        while any(t.id == tid for t in self.tracks):
            same_kind += 1
            tid = f"{prefix}{same_kind}"
        track = Track(id=tid, kind=kind, index=idx,
                      name=name or f"{kind} {same_kind}")
        self.tracks.append(track)
        return track

    def get_track(self, track_id: str) -> Optional[Track]:
        return next((t for t in self.tracks if t.id == track_id), None)

    def _fresh_id(self, prefix: str = "c") -> str:
        """A new id guaranteed not to collide with any existing clip/track id —
        robust against manually-assigned ids and ids carried in from a loaded
        project (where the module counter has reset)."""
        existing = {c.id for _, c in self.all_clips()} | {t.id for t in self.tracks}
        cid = new_id(prefix)
        while cid in existing:
            cid = new_id(prefix)
        return cid

    def first_track(self, kind: str) -> Optional[Track]:
        return next((t for t in self.tracks if t.kind == kind), None)

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

    def add_clip(self, clip: Clip, track_id: Optional[str] = None) -> Clip:
        track = self.get_track(track_id) if track_id else self.get_track(clip.track)
        if track is None:
            track = self.first_track("Audio" if clip.gain_db and not clip.src else "Video")
        if track is None:
            track = self.add_track("Video", "Video 1")
        clip.track = track.id
        track.clips.append(clip)
        track.clips.sort(key=lambda c: c.start)
        return clip

    def append_clip(self, src: str, kind: str = "Video", **kw) -> Clip:
        """Add a source at the end of the first track of ``kind`` (snap to the
        end of the last clip there)."""
        track = self.first_track(kind) or self.add_track(kind)
        start = max((c.end for c in track.clips), default=0.0)
        dur = kw.pop("dur", None)
        out = kw.pop("out", None)
        if out is None:
            out = dur if dur is not None else (kw.get("src_dur") or 0.0)
        clip = Clip(id=self._fresh_id("c"), src=str(src), start=start, in_=kw.pop("in_", 0.0),
                    out=float(out), track=track.id,
                    label=kw.pop("label", Path(str(src)).stem), **kw)
        track.clips.append(clip)
        return clip

    def remove_clip(self, clip_id: str) -> bool:
        for t in self.tracks:
            for i, c in enumerate(t.clips):
                if c.id == clip_id:
                    del t.clips[i]
                    return True
        return False

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

    def trim_clip(self, clip_id: str, in_: Optional[float] = None,
                  out: Optional[float] = None, start: Optional[float] = None) -> bool:
        _, clip = self.find_clip(clip_id)
        if clip is None:
            return False
        if in_ is not None:
            clip.in_ = max(0.0, float(in_))
        if out is not None:
            lo = clip.in_ + 1.0 / max(1, self.fps)
            clip.out = max(lo, float(out))
        if start is not None:
            clip.start = max(0.0, float(start))
        return True

    def split_at(self, track_id: str, t: float) -> list[str]:
        """Razor: split every clip on ``track_id`` that straddles timeline time
        ``t`` into two, returning the ids of the newly created right-hand clips."""
        track = self.get_track(track_id)
        if track is None:
            return []
        created: list[str] = []
        for clip in list(track.clips):
            if clip.start < t < clip.end:
                offset = t - clip.start            # seconds into the clip
                right = Clip(
                    id=self._fresh_id("c"), src=clip.src, start=t,
                    in_=clip.in_ + offset, out=clip.out, track=track.id,
                    label=clip.label, gain_db=clip.gain_db, src_fps=clip.src_fps,
                    src_dur=clip.src_dur, geometry=dict(clip.geometry) if clip.geometry else None,
                    thumb=clip.thumb)
                clip.out = clip.in_ + offset        # left clip ends at the cut
                track.clips.append(right)
                created.append(right.id)
        track.clips.sort(key=lambda c: c.start)
        return created

    # -- duration -----------------------------------------------------------
    def total_duration(self) -> float:
        return max((c.end for _, c in self.all_clips()), default=0.0)

    def video_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "Video"]

    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "Audio"]

    # -- the flat browser edit state ----------------------------------------
    def to_edit_json(self) -> dict:
        """The flat shape the JS timeline consumes. URLs are NOT filled in here
        (this module is path-only / pure); the plugin layer injects ``url`` /
        ``thumb_url`` from these absolute paths before sending to the browser."""
        clips = []
        for t in self.tracks:
            for c in t.clips:
                clips.append({
                    "id": c.id, "track": c.track, "src": c.src, "url": None,
                    "start": round(float(c.start), 4), "in": round(float(c.in_), 4),
                    "out": round(float(c.out), 4), "dur": round(c.dur, 4),
                    "label": c.label, "gain_db": c.gain_db, "kind": t.kind,
                    "thumb": c.thumb, "thumb_url": None,
                })
        return {
            "name": self.name, "fps": self.fps, "width": self.width,
            "height": self.height, "sample_rate": self.sample_rate,
            "tracks": [{"id": t.id, "kind": t.kind, "index": t.index,
                        "name": t.name, "muted": t.muted, "locked": t.locked}
                       for t in self.tracks],
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
            tl.tracks.append(Track(id=td["id"], kind=td.get("kind", "Video"),
                                   index=int(td.get("index", len(tl.tracks))),
                                   name=td.get("name", ""), muted=bool(td.get("muted")),
                                   locked=bool(td.get("locked"))))
        if not tl.tracks:
            tl.add_track("Video", "Video 1")
        for cd in d.get("clips", []):
            clip = Clip(id=cd["id"], src=cd.get("src", ""),
                        start=float(cd.get("start", 0.0)), in_=float(cd.get("in", 0.0)),
                        out=float(cd.get("out", 0.0)), track=cd.get("track", ""),
                        label=cd.get("label", ""), gain_db=float(cd.get("gain_db", 0.0)),
                        src_fps=cd.get("src_fps"), src_dur=cd.get("src_dur"),
                        geometry=cd.get("geometry"), thumb=cd.get("thumb"))
            track = tl.get_track(clip.track) or (tl.tracks[0] if tl.tracks else tl.add_track())
            clip.track = track.id
            track.clips.append(clip)
        for x in d.get("transitions", []):
            tl.transitions.append(Transition(
                id=x["id"], track=x.get("track", ""), kind=x.get("kind", "dissolve"),
                between=tuple(x.get("between", ("", ""))),
                position=float(x.get("position", 0.0)),
                duration=float(x.get("duration", 0.5))))
        for t in tl.tracks:
            t.clips.sort(key=lambda c: c.start)
        return tl


# --------------------------------------------------------------------------- #
#  Persistence — the Reel2ReelProject.1 document                              #
# --------------------------------------------------------------------------- #

def to_document(tl: Timeline) -> dict:
    """The on-disk schema. OTIO-shaped field names, explicit positions retained
    so a reload is loss-free without depending on the opentimelineio library."""
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
            "muted": t.muted, "locked": t.locked,
            "clips": [{k: v for k, v in asdict(c).items()} for c in t.clips],
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
                      muted=bool(td.get("muted")), locked=bool(td.get("locked")))
        for cd in td.get("clips", []):
            track.clips.append(Clip(
                id=cd["id"], src=cd.get("src", ""), start=float(cd.get("start", 0.0)),
                in_=float(cd.get("in_", cd.get("in", 0.0))), out=float(cd.get("out", 0.0)),
                track=track.id, label=cd.get("label", ""),
                gain_db=float(cd.get("gain_db", 0.0)), src_fps=cd.get("src_fps"),
                src_dur=cd.get("src_dur"), geometry=cd.get("geometry"),
                thumb=cd.get("thumb")))
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
    doc = json.loads(Path(path).read_text())
    return from_document(doc)
