"""Bidirectional converter between a Reel2Reel :class:`~core.timeline.Timeline`
and an OpenTimelineIO-shaped document.

This is additive and deferred: v1 persists the native ``Reel2ReelProject.2``
schema (see ``timeline.save``). This module exists so a timeline can be exported
to / imported from canonical ``.otio`` (and thence Resolve/Premiere/FCPXML/AAF
via OTIO adapters) once the ``opentimelineio`` library is installed. The
pure-dict path here works without the library; the file round-trip uses it when
present.

The dict form materializes Gaps from the explicit clip positions (OTIO tracks
are contiguous: a clip's place is implied by preceding Gaps/Clips), and emits
RationalTime/TimeRange at the project frame rate.
"""
from __future__ import annotations

from . import timeline as _tl


def _rt(seconds: float, rate: int) -> dict:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate,
            "value": round(float(seconds) * rate)}


def _range(start: float, dur: float, rate: int) -> dict:
    return {"OTIO_SCHEMA": "TimeRange.1",
            "start_time": _rt(start, rate), "duration": _rt(dur, rate)}


def _transition_child(x: "_tl.Transition", rate: int) -> dict:
    """A native OTIO Transition.1 emitted inline at the cut between the two clips it
    spans. The duration is split around the cut (in_offset = into the left clip,
    out_offset = into the right). Lossless r2r fields ride in metadata.reel2reel."""
    half = x.duration / 2.0
    return {
        "OTIO_SCHEMA": "Transition.1", "name": x.kind,
        "transition_type": "SMPTE_Dissolve",
        "in_offset": _rt(half, rate), "out_offset": _rt(half, rate),
        "metadata": {"reel2reel": {
            "id": x.id, "kind": x.kind, "direction": x.direction,
            "position": x.position, "duration": x.duration}},
    }


def to_otio(tl: "_tl.Timeline") -> dict:
    """Return an OTIO_SCHEMA-tagged dict. Gaps are inserted to position clips;
    transitions are emitted inline as Transition.1 children and markers as Marker.1
    on the Stack — with a top-level metadata.reel2reel mirror for lossless restore."""
    rate = tl.fps
    trans_by_left = {x.between[0]: x for x in tl.transitions}
    otio_tracks = []
    for t in tl.tracks:
        children = []
        cursor = 0.0
        ordered = sorted(t.clips, key=lambda c: c.start)
        for c in ordered:
            if c.start > cursor + 1e-6:
                children.append({
                    "OTIO_SCHEMA": "Gap.1", "name": "",
                    "source_range": _range(0.0, c.start - cursor, rate)})
            children.append({
                "OTIO_SCHEMA": "Clip.1", "name": c.label or c.id,
                "media_reference": {
                    "OTIO_SCHEMA": "ExternalReference.1",
                    "target_url": c.src,
                    "available_range": _range(0.0, c.src_dur or c.dur, rate)},
                "source_range": _range(c.in_, c.src_len, rate),
                "metadata": {"reel2reel": {
                    "id": c.id, "type": c.type, "gain_db": c.gain_db,
                    "geometry": c.geometry, "speed": c.speed, "reverse": c.reverse,
                    "color": c.color, "text": c.text, "fade_in": c.fade_in,
                    "fade_out": c.fade_out, "opacity": c.opacity, "mute": c.mute,
                    "has_audio": c.has_audio, "keyframes": c.keyframes,
                    "src_dur": c.src_dur, "src_fps": c.src_fps, "thumb": c.thumb}},
            })
            # A transition whose LEFT clip is c sits at the cut between c and the next.
            tr = trans_by_left.get(c.id)
            if tr and tr.between[1] in {cc.id for cc in ordered}:
                children.append(_transition_child(tr, rate))
            cursor = c.start + c.dur
        otio_tracks.append({
            "OTIO_SCHEMA": "Track.1", "name": t.name,
            "kind": t.kind, "children": children})
    markers = [{"OTIO_SCHEMA": "Marker.1", "name": m.label,
                "color": m.color, "marked_range": _range(m.t, 0.0, rate),
                "metadata": {"reel2reel": {"id": m.id, "t": m.t, "label": m.label,
                                           "color": m.color}}}
               for m in tl.markers]
    return {
        "OTIO_SCHEMA": "Timeline.1", "name": tl.name,
        "global_start_time": _rt(0.0, rate),
        "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks",
                   "children": otio_tracks, "markers": markers},
        "metadata": {"reel2reel": {
            "width": tl.width, "height": tl.height, "sample_rate": tl.sample_rate,
            "transitions": [{"id": x.id, "track": x.track, "kind": x.kind,
                             "between": list(x.between), "position": x.position,
                             "duration": x.duration, "direction": x.direction}
                            for x in tl.transitions],
            "markers": [{"id": m.id, "t": m.t, "label": m.label, "color": m.color}
                        for m in tl.markers]}},
    }


def from_otio(doc: dict) -> "_tl.Timeline":
    """Rebuild a Timeline from an OTIO dict, deriving explicit clip starts from the
    preceding Gaps/Clips. Inline Transition children + the metadata.reel2reel mirror
    restore transitions; Stack markers (or the mirror) restore markers; and each clip's
    keyframes/src_dur/src_fps come back off metadata.reel2reel. Clip ids are de-duped so
    a colliding id can't make a clip unselectable / delete the wrong one."""
    meta = (doc.get("metadata") or {}).get("reel2reel", {})
    stack = doc.get("tracks", {}) or {}
    rate = (doc.get("global_start_time") or {}).get("rate") or 24
    tl = _tl.Timeline(name=doc.get("name", "Cut 1"), fps=int(rate),
                      width=int(meta.get("width", 1280)), height=int(meta.get("height", 720)),
                      sample_rate=int(meta.get("sample_rate", 48000)), tracks=[])

    def secs(rt):
        rt = rt or {}
        r = rt.get("rate") or rate
        return (rt.get("value", 0) / r) if r else 0.0

    seen: set[str] = set()
    inline_trans: list[dict] = []          # transitions parsed from inline children
    for otrack in stack.get("children", []):
        track = tl.add_track(otrack.get("kind", "Video"), otrack.get("name", ""))
        cursor = 0.0
        for child in otrack.get("children", []):
            schema = (child.get("OTIO_SCHEMA") or "").split(".")[0]
            if schema == "Transition":
                tm = (child.get("metadata") or {}).get("reel2reel", {})
                if tm:
                    inline_trans.append({**tm, "track": track.id})
                continue                   # transitions don't consume timeline space here
            sr = child.get("source_range") or {}
            dur = secs(sr.get("duration"))
            if schema == "Gap":
                cursor += dur
                continue
            rmeta = (child.get("metadata") or {}).get("reel2reel", {})
            in_ = secs(sr.get("start_time"))
            mref = child.get("media_reference") or {}
            cid = rmeta.get("id")
            while not cid or cid in seen:  # mint a fresh id on collision / when missing
                cid = tl._fresh_id("c")
            seen.add(cid)
            clip = _tl.Clip(
                id=cid,
                src=mref.get("target_url", ""), start=cursor, in_=in_, out=in_ + dur,
                track=track.id, label=child.get("name", ""),
                type=rmeta.get("type", "media"),
                gain_db=float(rmeta.get("gain_db", 0.0) or 0.0),
                geometry=rmeta.get("geometry"),
                speed=float(rmeta.get("speed", 1.0) or 1.0),
                reverse=bool(rmeta.get("reverse", False)), color=rmeta.get("color"),
                text=rmeta.get("text"), fade_in=float(rmeta.get("fade_in", 0.0) or 0.0),
                fade_out=float(rmeta.get("fade_out", 0.0) or 0.0),
                opacity=float(rmeta.get("opacity", 1.0) or 1.0),
                mute=bool(rmeta.get("mute", False)),
                has_audio=bool(rmeta.get("has_audio", False)),
                keyframes=rmeta.get("keyframes"), thumb=rmeta.get("thumb"),
                src_dur=rmeta.get("src_dur"), src_fps=rmeta.get("src_fps"))
            track.clips.append(clip)
            cursor += clip.dur              # advance by on-timeline (speed-aware) length
    if not tl.tracks:
        tl.add_track("Video", "Video 1")

    # Transitions: prefer the top-level mirror (carries track + between ids verbatim),
    # else the inline children. Construct Transition objects directly so 'between'
    # references resolve against the (preserved) clip ids.
    src_trans = meta.get("transitions") or inline_trans
    for x in src_trans:
        b = x.get("between") or ("", "")
        b = (b[0] if len(b) > 0 else "", b[1] if len(b) > 1 else "")
        tl.transitions.append(_tl.Transition(
            id=x.get("id") or tl._fresh_id("x"), track=x.get("track", ""),
            kind=x.get("kind", "dissolve"), between=b,
            position=float(x.get("position", 0.0) or 0.0),
            duration=float(x.get("duration", 0.5) or 0.5),
            direction=x.get("direction", "left")))

    # Markers: the mirror, else Stack Marker.1 entries.
    mk_src = meta.get("markers")
    if not mk_src:
        mk_src = [{**(((mk.get("metadata") or {}).get("reel2reel")) or {}),
                   "t": secs((mk.get("marked_range") or {}).get("start_time")),
                   "label": mk.get("name", ""), "color": mk.get("color", "#e0a106")}
                  for mk in (stack.get("markers") or [])]
    for m in mk_src:
        tl.markers.append(_tl.Marker(
            id=m.get("id") or tl._fresh_id("m"), t=float(m.get("t", 0.0) or 0.0),
            label=m.get("label", ""), color=m.get("color", "#e0a106")))

    return tl.sanitize()


# --- optional .otio file round-trip (needs the opentimelineio library) ------

def write_otio_file(tl: "_tl.Timeline", path: str) -> str:
    import json
    try:
        import opentimelineio as otio  # noqa: F401
        # If the lib is present, prefer its serializer for full fidelity.
        import opentimelineio.adapters as _ad  # noqa: F401
    except Exception:
        # Fall back to writing our dict form (still valid OTIO_SCHEMA JSON).
        from pathlib import Path
        Path(path).write_text(json.dumps(to_otio(tl), indent=2))
        return path
    import opentimelineio as otio
    tl_otio = otio.adapters.read_from_string(__import__("json").dumps(to_otio(tl)), "otio_json")
    otio.adapters.write_to_file(tl_otio, path)
    return path
