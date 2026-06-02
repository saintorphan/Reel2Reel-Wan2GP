"""Bidirectional converter between a Reel2Reel :class:`~core.timeline.Timeline`
and an OpenTimelineIO-shaped document.

This is additive and deferred: v1 persists the native ``Reel2ReelProject.1``
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


def to_otio(tl: "_tl.Timeline") -> dict:
    """Return an OTIO_SCHEMA-tagged dict. Gaps are inserted to position clips."""
    rate = tl.fps
    otio_tracks = []
    for t in tl.tracks:
        children = []
        cursor = 0.0
        for c in sorted(t.clips, key=lambda c: c.start):
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
                    "has_audio": c.has_audio}},
            })
            cursor = c.start + c.dur
        otio_tracks.append({
            "OTIO_SCHEMA": "Track.1", "name": t.name,
            "kind": t.kind, "children": children})
    return {
        "OTIO_SCHEMA": "Timeline.1", "name": tl.name,
        "global_start_time": _rt(0.0, rate),
        "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "children": otio_tracks},
        "metadata": {"reel2reel": {"width": tl.width, "height": tl.height,
                                   "sample_rate": tl.sample_rate}},
    }


def from_otio(doc: dict) -> "_tl.Timeline":
    """Rebuild a Timeline from an OTIO dict, deriving explicit clip starts from the
    preceding Gaps/Clips."""
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

    for otrack in stack.get("children", []):
        track = tl.add_track(otrack.get("kind", "Video"), otrack.get("name", ""))
        cursor = 0.0
        for child in otrack.get("children", []):
            schema = (child.get("OTIO_SCHEMA") or "").split(".")[0]
            sr = child.get("source_range") or {}
            dur = secs(sr.get("duration"))
            if schema == "Gap":
                cursor += dur
                continue
            rmeta = (child.get("metadata") or {}).get("reel2reel", {})
            in_ = secs(sr.get("start_time"))
            mref = child.get("media_reference") or {}
            clip = _tl.Clip(
                id=rmeta.get("id") or _tl.new_id("c"),
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
                has_audio=bool(rmeta.get("has_audio", False)))
            track.clips.append(clip)
            cursor += clip.dur              # advance by on-timeline (speed-aware) length
    if not tl.tracks:
        tl.add_track("Video", "Video 1")
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
