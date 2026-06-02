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

from core import timeline, otio, render  # noqa: E402


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
    tl2 = timeline.Timeline.from_edit_json(edit)
    assert sum(len(t.clips) for t in tl2.tracks) == 3
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
    test_render_smoke()
    print("\nALL PASSED")
