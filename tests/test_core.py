"""Offline tests for the pure core (no GPU, no Wan2GP, no Gradio).

The repo dir is named with a hyphen (Reel2Reel-Wan2GP), which isn't a valid
module name, so we put the repo dir itself on sys.path and import ``core``
directly — the plugin's own relative imports (``from .core import ...``) work
under any installed directory name.

Run:  python tests/test_core.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import timeline, otio, render, projects, paths  # noqa: E402


def _tl():
    tl = timeline.Timeline(name="Cut 1", fps=24, width=320, height=240)
    v = tl.first_track("Video")
    tl.add_clip(timeline.Clip(id="c1", src="/tmp/a.mp4", start=0.0, in_=0.0, out=3.0,
                              track=v.id, label="A", src_dur=5.0))
    tl.add_clip(timeline.Clip(id="c2", src="/tmp/b.mp4", start=3.0, in_=0.0, out=4.0,
                              track=v.id, label="B", src_dur=4.0))
    a = tl.first_track("Audio")
    tl.add_clip(timeline.Clip(id="a1", src="/tmp/m.wav", start=0.0, in_=0.0, out=6.5,
                              track=a.id, label="music", gain_db=-3.0))
    return tl


def test_timeline_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cut.r2r.json"
        tl = _tl()
        assert abs(tl.total_duration() - 7.0) < 1e-6, tl.total_duration()
        timeline.save(p, tl)
        assert p.is_file()
        tl2 = timeline.load(p)
        assert tl2.name == "Cut 1"
        assert len(tl2.tracks) == len(tl.tracks) == 2, ("no duplicate tracks", len(tl2.tracks))
        assert sum(len(t.clips) for t in tl2.tracks) == 3
        assert abs(tl2.total_duration() - 7.0) < 1e-6
        _, c1 = tl2.find_clip("c1")
        assert c1 and abs(c1.dur - 3.0) < 1e-6 and c1.in_ == 0.0
    print("✓ timeline save/load round-trip")


def test_split_at():
    tl = _tl()
    v = tl.first_track("Video")
    n_before = len(v.clips)
    created = tl.split_at(v.id, 1.5)         # splits c1 (0..3) at 1.5
    assert len(created) == 1, created
    assert len(v.clips) == n_before + 1
    _, left = tl.find_clip("c1")
    _, right = tl.find_clip(created[0])
    assert abs(left.out - 1.5) < 1e-6, left.out          # left ends at the cut
    assert abs(right.start - 1.5) < 1e-6, right.start     # right starts at the cut
    assert abs(right.in_ - 1.5) < 1e-6, right.in_         # carries the source offset
    assert abs(tl.total_duration() - 7.0) < 1e-6          # razor preserves length
    print("✓ razor split at playhead")


def test_edit_json_roundtrip():
    tl = _tl()
    edit = tl.to_edit_json()
    assert len(edit["clips"]) == 3 and len(edit["tracks"]) == 2
    assert "src_fps" in edit["clips"][0]           # exposed for JS "match highest fps"
    tl2 = timeline.Timeline.from_edit_json(edit)
    assert len(tl2.tracks) == 2, ("no duplicate tracks", len(tl2.tracks))
    assert sum(len(t.clips) for t in tl2.tracks) == 3
    assert tl2.fps == tl.fps == 24                  # fps survives the JS round-trip
    assert abs(tl2.total_duration() - tl.total_duration()) < 1e-6
    print("✓ flat edit-json round-trip")


def test_otio_convert():
    tl = _tl()
    doc = otio.to_otio(tl)
    assert doc["OTIO_SCHEMA"].startswith("Timeline")
    tl2 = otio.from_otio(doc)
    assert sum(len(t.clips) for t in tl2.tracks) == 3
    # explicit positions recovered from the materialized gaps
    _, c2 = tl2.find_clip("c2")
    assert c2 and abs(c2.start - 3.0) < 1e-6, c2.start if c2 else None
    print("✓ OTIO convert preserves clips + positions")


def test_detach_audio():
    tl = _tl()
    nv = sum(len(t.clips) for t in tl.tracks)
    aid = tl.detach_audio("c1")
    assert aid, "detach returned no id"
    _, vid = tl.find_clip("c1")
    _, aud = tl.find_clip(aid)
    assert vid.mute is True, "source video audio should be muted after detach"
    assert aud.track != vid.track and aud.src == vid.src
    assert abs(aud.start - vid.start) < 1e-6 and abs(aud.dur - vid.dur) < 1e-6
    assert sum(len(t.clips) for t in tl.tracks) == nv + 1
    print("✓ detach audio (separate audio from video)")


def test_clip_track_ops():
    tl = _tl()
    # set_clip clamps fades to clip duration and opacity to [0,1]
    tl.set_clip("c1", fade_in=99, fade_out=99, opacity=2.0, gain_db=-6)
    _, c1 = tl.find_clip("c1")
    assert c1.fade_in <= c1.dur and c1.fade_out <= c1.dur and c1.opacity == 1.0
    assert c1.gain_db == -6
    # duplicate appends a copy with a fresh id at the track end
    n = len(tl.first_track("Video").clips)
    dup = tl.duplicate_clip("c1")
    assert dup and dup != "c1" and len(tl.first_track("Video").clips) == n + 1
    # ripple delete closes the gap
    v = tl.first_track("Video")
    tl2 = _tl()
    before = tl2.total_duration()
    tl2.ripple_delete("c1")          # c1 was 0..3, c2 0..(shifted)
    _, c2 = tl2.find_clip("c2")
    assert abs(c2.start - 0.0) < 1e-6, c2.start
    assert tl2.total_duration() < before
    # track ops
    a2 = tl.add_track("Audio", "Music2")
    assert tl.set_track(a2.id, volume_db=-3, solo=True)
    assert tl.audible_tracks("Audio") == [a2]   # solo wins
    assert tl.move_track(a2.id, -1)
    assert tl.remove_track(a2.id)
    print("✓ set_clip/track, duplicate, ripple-delete, track add/move/remove/solo")


def test_transitions():
    tl = _tl()                       # c1 0..3, c2 at 3..7 on V1
    v = tl.first_track("Video")
    tid = tl.add_transition("c1", 0.5)
    assert tid and len(tl.transitions) == 1
    _, c2 = tl.find_clip("c2")
    _, c1 = tl.find_clip("c1")
    assert abs(c2.start - (c1.end - 0.5)) < 1e-6, (c1.end, c2.start)   # overlap by D
    assert tl.remove_transition("c1") and not tl.transitions
    _, c2b = tl.find_clip("c2")
    assert abs(c2b.start - 3.0) < 1e-6, c2b.start                      # un-rippled
    print("✓ add/remove cross-dissolve transition (ripple)")


def test_render_transition():
    if not render.ffmpeg_path():
        print("· transition render skipped (ffmpeg not found)")
        return
    with tempfile.TemporaryDirectory() as d:
        os.environ["REEL2REEL_DIR"] = d
        srcs = []
        for i, color in enumerate(("red", "blue")):
            sp = Path(d) / f"c{i}.mp4"
            render.run([render.ffmpeg_path(), "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:s=160x120:r=24:d=1.5",
                        "-f", "lavfi", "-i", f"sine=frequency={440 + i*220}:duration=1.5",
                        "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", str(sp)])
            srcs.append(str(sp))
        tl = timeline.Timeline(fps=24, width=160, height=120)
        v = tl.first_track("Video")
        tl.add_clip(timeline.Clip(id="c1", src=srcs[0], start=0, in_=0, out=1.5,
                                  track=v.id, has_audio=True, fade_in=0.2))
        tl.add_clip(timeline.Clip(id="c2", src=srcs[1], start=1.5, in_=0, out=1.5,
                                  track=v.id, has_audio=True, fade_out=0.2))
        tl.add_transition("c1", 0.5)            # overlap -> total 1.5+1.5-0.5 = 2.5
        out = render.export(tl, str(Path(d) / "out.mp4"))
        dur = render.ffprobe_dur(out)
        assert Path(out).stat().st_size > 0
        assert dur is None or abs(dur - 2.5) < 0.5, dur
    print("✓ ffmpeg render with cross-dissolve + fades + audio-from-video")


def test_projects():
    with tempfile.TemporaryDirectory() as d:
        os.environ["REEL2REEL_DIR"] = d
        paths._config = None
        paths.ensure_dirs()
        # legacy flat file migrates into the folder layout
        timeline.save(paths.projects_dir() / "Old.r2r.json", timeline.Timeline(name="Old"))
        assert projects.list_projects() == ["Old"], projects.list_projects()
        # create + load (no duplicate tracks)
        tl = timeline.Timeline(name="A")
        v = tl.first_track("Video")
        tl.add_clip(timeline.Clip(id="c1", src="/x.mp4", start=0, in_=0, out=2, track=v.id))
        projects.create("A", tl)
        assert len(projects.load_timeline("A").tracks) == 2
        # versioning
        projects.snapshot("A", "v1", tl)
        tl.add_clip(timeline.Clip(id="c2", src="/y.mp4", start=2, in_=0, out=1, track=v.id))
        projects.save_timeline("A", tl)
        projects.snapshot("A", "v2", tl)
        assert projects.version_labels("A") == ["v1", "v2"]
        assert sum(len(t.clips) for t in projects.restore_version("A", "v1").tracks) == 1
        assert projects.delete_version("A", "v1") and projects.version_labels("A") == ["v2"]
        # distinct labels with colliding slugs must get distinct files (no wrong restore)
        one = timeline.Timeline(name="A")
        vt = one.first_track("Video")
        one.add_clip(timeline.Clip(id="x", src="/1.mp4", start=0, in_=0, out=1, track=vt.id))
        projects.snapshot("A", "v1@beta", one)
        two = timeline.Timeline(name="A")
        wt = two.first_track("Video")
        two.add_clip(timeline.Clip(id="y", src="/2.mp4", start=0, in_=0, out=1, track=wt.id))
        two.add_clip(timeline.Clip(id="z", src="/3.mp4", start=1, in_=0, out=1, track=wt.id))
        projects.snapshot("A", "v1_beta", two)          # same slug as v1@beta
        files = {v["label"]: v["file"] for v in projects.list_versions("A")}
        assert files["v1@beta"] != files["v1_beta"], files
        assert sum(len(t.clips) for t in projects.restore_version("A", "v1@beta").tracks) == 1
        assert sum(len(t.clips) for t in projects.restore_version("A", "v1_beta").tracks) == 2
        # project bin + global bin
        projects.add_to_bin("A", ["/a", "/b"]); projects.add_to_bin("A", "/a")
        assert projects.get_bin("A") == ["/a", "/b"]
        projects.add_to_global_bin("/g1"); projects.add_to_global_bin("/g1")
        assert projects.get_global_bin() == ["/g1"]
        # rename / duplicate (carries bin) / delete
        projects.rename("A", "A2")
        assert projects.exists("A2") and not projects.exists("A")
        projects.duplicate("A2", "A3")
        assert projects.get_bin("A3") == ["/a", "/b"]
        assert projects.delete("A3") and not projects.exists("A3")
    os.environ.pop("REEL2REEL_DIR", None)
    paths._config = None
    print("✓ projects CRUD + versioning + project/global bins (+legacy migrate)")


def test_model_extensions():
    tl = timeline.Timeline(fps=24, width=320, height=240)
    v = tl.first_track("Video")
    c = timeline.Clip(id="c1", src="/a.mp4", start=0, in_=0, out=4, track=v.id, speed=2.0)
    tl.add_clip(c)
    assert abs(c.dur - 2.0) < 1e-6, c.dur                       # 4s @2x = 2s timeline
    ids = tl.split_at(v.id, 1.0)                                # split at 1s = 2s source
    _, right = tl.find_clip(ids[0])
    assert abs(right.in_ - 2.0) < 1e-6, right.in_
    txt = tl.add_text_clip("Hello", start=5, dur=3)
    assert txt.type == "text" and abs(txt.dur - 3.0) < 1e-6 and txt.text["content"] == "Hello"
    tl.set_clip("c1", out=0.0)                                  # zero-length -> clamped
    _, c1 = tl.find_clip("c1")
    assert c1.out > c1.in_
    mid = tl.add_marker(2.5, "scene 2")
    assert any(m.id == mid and m.label == "scene 2" for m in tl.markers)
    assert tl.remove_marker(mid)
    data = tl.serialize_clips([txt.id])
    new = tl.paste_clips(data, at=10.0)
    _, pasted = tl.find_clip(new[0])
    assert len(new) == 1 and abs(pasted.start - 10.0) < 1e-6 and pasted.type == "text"
    tl.transitions.append(timeline.Transition(id="xbad", track=v.id,
                                              between=("nope", "nope2"), duration=0.5))
    tl.sanitize()
    assert not any(x.id == "xbad" for x in tl.transitions)      # bad transition dropped
    # round-trip with new fields
    tl2 = timeline.from_document(timeline.to_document(tl))
    _, p2 = tl2.find_clip(new[0])
    assert p2.type == "text" and p2.text["content"] == "Hello"
    print("✓ model extensions (speed/split, text, clamp, markers, copy-paste, sanitize, round-trip)")


def test_render_smoke():
    if not render.ffmpeg_path():
        print("· render smoke skipped (ffmpeg not found)")
        return
    with tempfile.TemporaryDirectory() as d:
        os.environ["REEL2REEL_DIR"] = d
        # two 1s synthetic clips via ffmpeg's test sources
        srcs = []
        for i, color in enumerate(("red", "blue")):
            sp = Path(d) / f"clip{i}.mp4"
            render.run([render.ffmpeg_path(), "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:s=160x120:r=24:d=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-shortest", "-pix_fmt", "yuv420p", str(sp)])
            srcs.append(str(sp))
        tl = timeline.Timeline(name="smoke", fps=24, width=160, height=120)
        v = tl.first_track("Video")
        tl.add_clip(timeline.Clip(id="c1", src=srcs[0], start=0.0, in_=0.0, out=1.0, track=v.id))
        tl.add_clip(timeline.Clip(id="c2", src=srcs[1], start=1.0, in_=0.0, out=1.0, track=v.id))
        a = tl.first_track("Audio")
        tl.add_clip(timeline.Clip(id="a1", src=srcs[0], start=0.0, in_=0.0, out=2.0, track=a.id))
        out = render.export(tl, str(Path(d) / "out.mp4"))
        assert Path(out).is_file() and Path(out).stat().st_size > 0
        dur = render.ffprobe_dur(out)
        assert dur is None or abs(dur - 2.0) < 0.5, dur
    print("✓ ffmpeg render smoke (2 clips -> mp4)")


if __name__ == "__main__":
    test_timeline_roundtrip()
    test_split_at()
    test_edit_json_roundtrip()
    test_otio_convert()
    test_detach_audio()
    test_clip_track_ops()
    test_transitions()
    test_model_extensions()
    test_projects()
    test_render_transition()
    test_render_smoke()
    print("\nALL PASSED")
