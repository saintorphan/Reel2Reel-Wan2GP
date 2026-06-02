"""ffmpeg export for a Reel2Reel timeline. No Gradio.

Two-stage: PASS 1 normalizes every clip to a canonical profile (trim + speed/
reverse + color + scale, cached); PASS 2 builds ONE filter graph — per video
track a concat/xfade fold (transitions, fades, gaps) overlaid onto a black
canvas, then PiP (geometry) clips and text/title clips overlaid on top; audio is
gain/fade/overlap-cross-faded, delayed to position, mixed and loudness-normalized.

Export supports presets (mp4/webm/prores/gif), a quality target, a resolution
override and an in/out range. The filter graph is passed via -filter_complex_script
so very large timelines don't hit the command-line length limit. Keyframe
automation is carried in the model but not yet applied here (static values used).
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

from . import effects, paths

logger = logging.getLogger("reel2reel.render")

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

# preset -> ffmpeg output options. crf filled from QUALITY.
PRESETS = {
    "mp4":    {"ext": ".mp4",  "v": ["-c:v", "libx264", "-preset", "medium"],
               "a": ["-c:a", "aac", "-b:a", "192k"], "pix": "yuv420p",
               "extra": ["-movflags", "+faststart"]},
    "webm":   {"ext": ".webm", "v": ["-c:v", "libvpx-vp9", "-b:v", "0"],
               "a": ["-c:a", "libopus", "-b:a", "160k"], "pix": "yuv420p", "extra": []},
    "prores": {"ext": ".mov",  "v": ["-c:v", "prores_ks", "-profile:v", "3"],
               "a": ["-c:a", "pcm_s16le"], "pix": "yuv422p10le", "extra": []},
    "gif":    {"ext": ".gif",  "v": [], "a": [], "pix": "rgb24", "extra": []},
}
QUALITY = {"high": 16, "medium": 21, "low": 27}


class RenderError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
#  binaries                                                                    #
# --------------------------------------------------------------------------- #

def ffmpeg_path() -> str | None:
    cand = os.environ.get("REEL2REEL_FFMPEG") or shutil.which("ffmpeg")
    if cand and Path(cand).exists():
        return cand
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe") or None


def ffmpeg_version() -> str:
    exe = ffmpeg_path()
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10)
        return (out.stdout or "").splitlines()[0] if out.stdout else ""
    except Exception:
        return ""


def _font() -> str | None:
    return next((f for f in _FONT_CANDIDATES if Path(f).exists()), None)


def run(args: list[str], timeout: int = 3600, cancel=None) -> str:
    if cancel is None:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or "").strip().splitlines()[-24:])
            logger.error("ffmpeg failed (%d): %s", proc.returncode, tail)
            raise RenderError(f"ffmpeg exited {proc.returncode}:\n{tail}")
        return proc.stdout or ""
    # Cancelable path: Popen + poll the cancel event, terminate the subprocess.
    import time as _t
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = _t.monotonic()
    while proc.poll() is None:
        if cancel.is_set():
            proc.terminate()
            try:                       # communicate() drains the pipes (wait() can deadlock)
                proc.communicate(timeout=5)
            except Exception:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
            raise RenderError("Render cancelled.")
        if _t.monotonic() - started > timeout:
            proc.kill()
            raise RenderError("Render timed out.")
        _t.sleep(0.25)
    out, err = proc.communicate()
    if proc.returncode != 0:
        tail = "\n".join((err or "").strip().splitlines()[-24:])
        logger.error("ffmpeg failed (%d): %s", proc.returncode, tail)
        raise RenderError(f"ffmpeg exited {proc.returncode}:\n{tail}")
    return out or ""


def ffprobe_dur(path: str) -> float | None:
    exe = ffprobe_path()
    if not exe:
        return None
    try:
        out = run([exe, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", str(path)], timeout=30)
        return float(out.strip())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  canvas + caching                                                            #
# --------------------------------------------------------------------------- #

def canvas_of(tl, width=None, height=None, fps=None) -> dict:
    return {"w": int(width or tl.width), "h": int(height or tl.height),
            "fps": int(fps or tl.fps), "ar": int(tl.sample_rate), "pix": "yuv420p"}


def _is_image(src: str) -> bool:
    from . import discovery        # lazy: discovery imports render (avoid the cycle)
    return Path(src).suffix.lower() in discovery.IMAGE_EXTS


def _clip_key(clip, canvas, stream, pad) -> str:
    raw = (f"{clip.src}|{clip.in_:.3f}|{clip.out:.3f}|{canvas['w']}x{canvas['h']}"
           f"@{canvas['fps']}|{canvas['ar']}|{stream}|sp{clip.speed_f:.3f}|rv{int(clip.reverse)}"
           f"|pad{int(pad)}|col{hashlib.sha1(str(clip.color).encode()).hexdigest()[:6]}"
           f"|geo{hashlib.sha1(str(clip.geometry).encode()).hexdigest()[:6]}")
    return hashlib.sha1(raw.encode()).hexdigest()[:18]


# --------------------------------------------------------------------------- #
#  PASS 1 — normalize                                                          #
# --------------------------------------------------------------------------- #

def normalize_video(clip, canvas, ffmpeg, pad=True) -> str:
    out_path = paths.norm_dir() / f"{_clip_key(clip, canvas, 'v', pad)}.mp4"
    if out_path.exists():
        return str(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src_len = max(1.0 / canvas["fps"], clip.src_len)
    vf = []
    # For an image the input is `-loop 1 -t {clip.dur}`, which already bakes in speed
    # via clip.dur; re-applying setpts (and reverse buffering a looped still) would
    # desync the timeline — so only speed/reverse a real video source.
    if not _is_image(clip.src):
        sp = effects.speed_vf(clip.speed_f, clip.reverse)
        if sp:
            vf.append(sp)
    crop = effects.crop_vf(clip.geometry)
    if crop:
        vf.append(crop)
    W, H = canvas["w"], canvas["h"]
    LZ = ":flags=lanczos"                         # high-quality resampling everywhere
    if pad:
        fm = effects.fit_mode(clip.geometry)
        if fm == "stretch":
            vf.append(f"scale={W}:{H}{LZ}")
        elif fm == "fill":                       # crop-to-fit (cover, no letterbox)
            vf.append(f"scale={W}:{H}:force_original_aspect_ratio=increase{LZ}")
            vf.append(f"crop={W}:{H}")
        else:                                    # fit (letterbox)
            vf.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease{LZ}")
            vf.append(f"pad={W}:{H}:-1:-1:color=black")
    else:
        vf.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease{LZ}")
    col = effects.color_vf(clip.color)
    if col:
        vf.append(col)
    vf += [f"fps={canvas['fps']}", "setsar=1", f"format={canvas['pix']}"]
    args = [ffmpeg, "-y"]
    if _is_image(clip.src):
        args += ["-loop", "1", "-t", f"{clip.dur:.3f}", "-i", clip.src]
    else:
        args += ["-ss", f"{clip.in_:.3f}", "-t", f"{src_len:.3f}", "-i", clip.src]
    args += ["-an", "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "18", "-pix_fmt", canvas["pix"], str(out_path)]
    run(args)
    return str(out_path)


def _has_audio_stream(src) -> bool:
    """True if ``src`` has at least one audio stream. Unknown (no ffprobe) → True so
    we still attempt extraction (normalize_audio's fallback then covers a real miss)."""
    exe = ffprobe_path()
    if not exe:
        return True
    try:
        out = run([exe, "-v", "error", "-select_streams", "a", "-show_entries",
                   "stream=index", "-of", "csv=p=0", str(src)], timeout=30)
        return bool(out.strip())
    except Exception:
        return True


def normalize_audio(clip, canvas, ffmpeg) -> str:
    out_path = paths.norm_dir() / f"{_clip_key(clip, canvas, 'a', False)}.m4a"
    if out_path.exists():
        return str(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src_len = max(0.01, clip.src_len)
    sil = [ffmpeg, "-y", "-f", "lavfi", "-i",
           f"anullsrc=channel_layout=stereo:sample_rate={canvas['ar']}",
           "-t", f"{src_len:.3f}", "-c:a", "aac", "-ar", str(canvas["ar"]),
           "-ac", "2", str(out_path)]
    if not _has_audio_stream(clip.src):
        # No audio stream (e.g. a silent video clip on a track) — synthesize silence
        # directly, so we never log a failed extraction or abort the render.
        run(sil)
        return str(out_path)
    af = []
    sp = effects.speed_af(clip.speed_f, clip.reverse)
    if sp:
        af.append(sp)
    af += [f"aresample={canvas['ar']}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    args = [ffmpeg, "-y", "-ss", f"{clip.in_:.3f}", "-t", f"{src_len:.3f}",
            "-i", clip.src, "-vn", "-af", ",".join(af), "-c:a", "aac",
            "-ar", str(canvas["ar"]), "-ac", "2", str(out_path)]
    try:
        run(args)
    except RenderError:
        run(sil)                                   # belt-and-suspenders
    return str(out_path)


# --------------------------------------------------------------------------- #
#  PASS 2 — composite                                                          #
# --------------------------------------------------------------------------- #

def _is_pip(c) -> bool:
    g = c.geometry
    return isinstance(g, dict) and (effects.geometry_scale(g) != 1.0
                                    or g.get("x", "center") != "center"
                                    or g.get("y", "center") != "center")


def has_transitions(tl) -> bool:
    return bool(getattr(tl, "transitions", None))


def _gain(db: float) -> float:
    return 10 ** (float(db) / 20.0)


def export(tl, out_path=None, preset="mp4", quality="high", width=None, height=None,
           fps=None, start=None, end=None, progress_cb=None, cancel=None) -> str:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RenderError("ffmpeg not found. Install ffmpeg or set REEL2REEL_FFMPEG.")

    def _ck():
        if cancel is not None and cancel.is_set():
            raise RenderError("Render cancelled.")
    tl.sanitize()
    canvas = canvas_of(tl, width, height, fps)
    total = tl.total_duration()
    if total <= 0:
        raise RenderError("Timeline is empty — add a clip before exporting.")
    preset = preset if preset in PRESETS else "mp4"
    p = PRESETS[preset]
    W, H, FPS, AR, PIX = canvas["w"], canvas["h"], canvas["fps"], canvas["ar"], p["pix"]
    font = _font()

    trans_by_left = {x.between[0]: x for x in getattr(tl, "transitions", [])}

    def _prog(frac, desc):
        if callable(progress_cb):
            try:
                progress_cb(frac, desc)
            except Exception:
                pass

    # classify clips
    fold = []     # (track, clip) canvas-sized media on a track (concat/xfade)
    pip = []      # (track, clip) positioned/scaled video overlays
    text = []     # (track, clip) drawtext titles
    for t in tl.video_tracks():
        for c in sorted(t.clips, key=lambda c: c.start):
            if c.type == "text":
                text.append((t, c))
            elif not c.src:
                continue
            elif _is_pip(c):
                pip.append((t, c))
            else:
                fold.append((t, c))
    # Include every non-muted clip with a source and let normalize_audio synthesize
    # silence when a source has no audio stream. We deliberately do NOT gate on
    # has_audio: under the Wan2GP host get_video_info reports has_audio=False for
    # everything, so gating would silently drop ALL audio on the export.
    audio_src = []
    for t in tl.audible_tracks("Audio"):
        for c in sorted(t.clips, key=lambda c: c.start):
            if c.src and not c.mute:
                audio_src.append((c, t))
    for t in tl.audible_tracks("Video"):
        for c in sorted(t.clips, key=lambda c: c.start):
            if c.src and c.type == "media" and not c.mute:
                audio_src.append((c, t))

    # -- PASS 1 normalize ---------------------------------------------------
    norm_v, norm_a = {}, {}
    work = len(fold) + len(pip) + len(audio_src)
    done = 0
    for t, c in fold:
        _ck()
        norm_v[c.id] = normalize_video(c, canvas, ffmpeg, pad=True)
        done += 1; _prog(0.05 + 0.5 * done / max(1, work), f"Normalizing {done}/{work}")
    for t, c in pip:
        _ck()
        norm_v[c.id] = normalize_video(c, canvas, ffmpeg, pad=False)
        done += 1; _prog(0.05 + 0.5 * done / max(1, work), f"Normalizing {done}/{work}")
    for c, t in audio_src:
        _ck()
        norm_a[c.id] = normalize_audio(c, canvas, ffmpeg)
        done += 1; _prog(0.05 + 0.5 * done / max(1, work), f"Normalizing {done}/{work}")

    _prog(0.6, "Compositing")
    inputs: list[str] = []
    filt: list[str] = []
    idx = 0

    def transparent(dur, label):
        filt.append(f"color=c=black:s={W}x{H}:r={FPS}:d={dur:.3f},format=yuva420p,"
                    f"colorchannelmixer=aa=0,setsar=1[{label}]")

    # VIDEO base
    filt.append(f"color=c=black:s={W}x{H}:r={FPS}:d={total:.3f},format=yuva420p,"
                f"colorchannelmixer=aa=1,setsar=1[vb0]")
    vbase = "vb0"
    vi = 0

    # per-track fold of canvas-sized clips
    fold_by_track = {}
    for t, c in fold:
        fold_by_track.setdefault(t.id, []).append(c)
    for tid, clips in fold_by_track.items():
        acc, acc_end, prev = None, 0.0, None
        for c in clips:
            i = idx
            inputs += ["-i", norm_v[c.id]]
            idx += 1
            chain = [f"[{i}:v]setpts=PTS-STARTPTS", f"fps={FPS}", "format=yuva420p", "setsar=1"]
            if c.opacity < 0.999:
                chain.append(f"colorchannelmixer=aa={max(0, min(1, c.opacity)):.3f}")
            if c.fade_in > 0.001:
                chain.append(f"fade=t=in:st=0:d={c.fade_in:.3f}:alpha=1")
            if c.fade_out > 0.001:
                chain.append(f"fade=t=out:st={max(0, c.dur - c.fade_out):.3f}:d={c.fade_out:.3f}:alpha=1")
            filt.append(",".join(chain) + f"[cl{i}]")
            cl = f"cl{i}"
            if acc is None:
                if c.start > 0.001:
                    transparent(c.start, f"gap{i}")
                    filt.append(f"[gap{i}][{cl}]concat=n=2:v=1:a=0[acc{i}]")
                    acc = f"acc{i}"
                else:
                    acc = cl
                acc_end = c.end
            else:
                tr = trans_by_left.get(prev.id) if prev else None
                gap = c.start - acc_end
                # xfade only when the clips actually abut; a gap (which from_edit_json/
                # from_otio can introduce) would make xfade's offset drift acc_end for
                # every later clip — fall back to the gap+concat path for that pair.
                if tr and tr.between[1] == c.id and gap <= 0.001:
                    off = max(0.0, acc_end - tr.duration)
                    name = effects.xfade_transition(tr.kind, tr.direction)
                    # Run xfade in OPAQUE space: flatten both inputs' alpha (opacity/
                    # fades) against opaque black first, so xfade doesn't cross-fade the
                    # alpha channel and mis-blend transition edges. Re-introduce alpha on
                    # the result so the accumulator keeps the transparent-canvas contract
                    # (concat with transparent gaps, final overlay with eof_action=pass).
                    filt.append(f"color=c=black:s={W}x{H}:r={FPS},format=yuva420p,"
                                f"colorchannelmixer=aa=1,setsar=1[xb{i}]")
                    filt.append(f"[xb{i}][{acc}]overlay=eof_action=pass:format=auto,"
                                f"format=yuv420p[xa{i}]")
                    filt.append(f"color=c=black:s={W}x{H}:r={FPS},format=yuva420p,"
                                f"colorchannelmixer=aa=1,setsar=1[xb2{i}]")
                    filt.append(f"[xb2{i}][{cl}]overlay=eof_action=pass:format=auto,"
                                f"format=yuv420p[xc{i}]")
                    filt.append(f"[xa{i}][xc{i}]xfade=transition={name}:"
                                f"duration={tr.duration:.3f}:offset={off:.3f},"
                                f"format=yuva420p,setsar=1[acc{i}]")
                    acc = f"acc{i}"; acc_end = acc_end - tr.duration + c.dur
                else:
                    if gap > 0.001:
                        transparent(gap, f"gap{i}")
                        filt.append(f"[{acc}][gap{i}][{cl}]concat=n=3:v=1:a=0[acc{i}]")
                    else:
                        filt.append(f"[{acc}][{cl}]concat=n=2:v=1:a=0[acc{i}]")
                    acc = f"acc{i}"; acc_end = c.end
            prev = c
        filt.append(f"[{vbase}][{acc}]overlay=eof_action=pass:format=auto[vb{vi + 1}]")
        vbase = f"vb{vi + 1}"; vi += 1

    # PiP (positioned/scaled) clips on top
    for t, c in pip:
        i = idx
        inputs += ["-i", norm_v[c.id]]
        idx += 1
        g = c.geometry or {}
        sc = effects.geometry_scale(g)
        chain = [f"[{i}:v]setpts=PTS-STARTPTS", f"fps={FPS}", "format=yuva420p"]
        if abs(sc - 1.0) > 1e-3:
            chain.append(f"scale=iw*{sc:.3f}:ih*{sc:.3f}")
        try:
            rot = float(g.get("rotate") or 0.0)
        except (TypeError, ValueError):
            rot = 0.0
        if abs(rot) > 0.01:
            rad = rot * 3.14159265 / 180.0
            chain.append(f"rotate={rad:.5f}:c=black@0:ow=rotw({rad:.5f}):oh=roth({rad:.5f})")
        chain.append("setsar=1")
        if c.opacity < 0.999:
            chain.append(f"colorchannelmixer=aa={max(0, min(1, c.opacity)):.3f}")
        if c.fade_in > 0.001:
            chain.append(f"fade=t=in:st=0:d={c.fade_in:.3f}:alpha=1")
        if c.fade_out > 0.001:
            chain.append(f"fade=t=out:st={max(0, c.dur - c.fade_out):.3f}:d={c.fade_out:.3f}:alpha=1")
        chain.append(f"tpad=start_duration={c.start:.3f}:start_mode=add:color=black@0")
        filt.append(",".join(chain) + f"[pip{i}]")
        x, y = effects.overlay_xy(g)
        filt.append(f"[{vbase}][pip{i}]overlay=x={x}:y={y}:eof_action=pass:format=auto[vb{vi + 1}]")
        vbase = f"vb{vi + 1}"; vi += 1

    # text / title clips on top (drawtext, time-gated)
    for t, c in text:
        dt = effects.drawtext(c.text, font, W, H)
        if not dt:
            continue
        s, e = c.start, c.end
        filt.append(f"[{vbase}]{dt}:enable='between(t\\,{s:.3f}\\,{e:.3f})'[vb{vi + 1}]")
        vbase = f"vb{vi + 1}"; vi += 1

    # Whole-cut "finish": fold the master video chain in right before [vout] so BOTH
    # the encode and the GIF path inherit it; master_af carries the (retargetable)
    # loudnorm into the audio mix below. See effects.master_vf / master_af.
    master = getattr(tl, "master", None)
    mvf = effects.master_vf(master)
    maf = effects.master_af(master)
    filt.append(f"[{vbase}]format={PIX}" + (("," + mvf) if mvf else "") + "[vout]")

    # AUDIO — gain/fade/overlap-fade, delay to position, mix + loudnorm.
    # Kept in its own list so the (audio-less) GIF path can use video only.
    fade_plan = _augment_overlap_fades(audio_src)
    afilt = []
    a_labels = []
    for c, t in audio_src:
        i = idx
        inputs += ["-i", norm_a[c.id]]
        idx += 1
        fin, fout = fade_plan.get(c.id, (c.fade_in, c.fade_out))
        ch = [f"[{i}:a]aresample={AR}", "asetpts=PTS-STARTPTS"]
        if fin > 0.001:
            ch.append(f"afade=t=in:st=0:d={fin:.3f}")
        if fout > 0.001:
            ch.append(f"afade=t=out:st={max(0, c.dur - fout):.3f}:d={fout:.3f}")
        gain = float(c.gain_db) + float(getattr(t, "volume_db", 0.0))
        if abs(gain) > 1e-6:
            ch.append(f"volume={_gain(gain):.4f}")
        ch.append(f"adelay={int(round(max(0, c.start) * 1000))}:all=1")
        afilt.append(",".join(ch) + f"[a{i}]")
        a_labels.append(f"[a{i}]")
    _ln = ("," + maf) if maf else ""
    if len(a_labels) == 1:
        afilt.append(f"{a_labels[0]}aresample={AR}{_ln}[aout]")
    elif a_labels:
        afilt.append(f"{''.join(a_labels)}amix=inputs={len(a_labels)}:duration=longest:"
                     f"normalize=0:dropout_transition=0{_ln}[aout]")
    else:
        afilt.append(f"anullsrc=channel_layout=stereo:sample_rate={AR}[aout]")

    out_path = str(out_path or _default_out(tl, p["ext"]))
    if not out_path.endswith(p["ext"]):
        out_path = str(Path(out_path).with_suffix(p["ext"]))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    # Range trim on output: build the full-length graph, then output-seek (-ss r0 after
    # the graph). This is the intentional 'correct + simple' approach — re-rendering the
    # whole graph keeps positions/transitions exact, the trim is just a cheap output seek.
    # `is not None` so an explicit 0.0 start is honored (not treated as 'unset').
    r0 = max(0.0, float(start)) if start is not None else 0.0
    r1 = float(end) if end else total
    rdur = max(1.0 / FPS, r1 - r0)

    _prog(0.7, "Encoding")
    _ck()
    if preset == "gif":
        _encode_gif(ffmpeg, inputs, filt, out_path, r0, rdur, FPS, W, cancel)
    else:
        full = filt + afilt
        graph = paths.norm_dir() / f"graph_{hashlib.sha1(';'.join(full).encode()).hexdigest()[:12]}.txt"
        graph.write_text(";".join(full))
        crf = QUALITY.get(quality, 18)
        args = [ffmpeg, "-y", *inputs, "-filter_complex_script", str(graph),
                "-map", "[vout]", "-map", "[aout]"]
        if r0 > 0.001:
            args += ["-ss", f"{r0:.3f}"]
        args += ["-t", f"{rdur:.3f}", *p["v"]]
        if preset in ("mp4", "webm"):     # libx264 + libvpx-vp9 both honor -crf
            args += ["-crf", str(crf)]
        args += ["-pix_fmt", PIX, *p["a"], *p["extra"], out_path]
        run(args, cancel=cancel)
    _prog(1.0, "Done")
    return out_path


def _augment_overlap_fades(audio_src):
    """Compute symmetric fades over same-track audio overlaps (avoids the sum
    level-jump / clicks; an equal-gain approximation of acrossfade). Returns a plan
    {clip_id: (fade_in, fade_out)} seeded from each clip's existing fades and max()'d
    with the computed overlap fade — it NEVER mutates the Clip objects (that would
    autosave as user-authored automation)."""
    plan = {c.id: (c.fade_in, c.fade_out) for c, _ in audio_src}
    by_track = {}
    for c, t in audio_src:
        by_track.setdefault(t.id, []).append(c)
    for clips in by_track.values():
        clips.sort(key=lambda c: c.start)
        for a, b in zip(clips, clips[1:]):
            ov = a.end - b.start
            if ov > 0.02:
                d = min(ov, a.dur * 0.5, b.dur * 0.5)
                ain, aout = plan[a.id]
                bin_, bout = plan[b.id]
                plan[a.id] = (ain, max(aout, d))
                plan[b.id] = (max(bin_, d), bout)
    return plan


def _encode_gif(ffmpeg, inputs, filt, out_path, r0, rdur, fps, W, cancel=None):
    gfps = min(fps, 15)
    scale = min(W, 640)
    filt = list(filt) + [f"[vout]fps={gfps},scale={scale}:-1:flags=lanczos,split[gv1][gv2]",
                         "[gv1]palettegen=stats_mode=diff[pal]",
                         "[gv2][pal]paletteuse=dither=bayer:bayer_scale=3[gout]"]
    graph = paths.norm_dir() / f"gif_{hashlib.sha1(';'.join(filt).encode()).hexdigest()[:12]}.txt"
    graph.write_text(";".join(filt))
    args = [ffmpeg, "-y", *inputs, "-filter_complex_script", str(graph), "-map", "[gout]"]
    if r0 > 0.001:
        args += ["-ss", f"{r0:.3f}"]
    args += ["-t", f"{rdur:.3f}", out_path]
    run(args, cancel=cancel)


def _default_out(tl, ext=".mp4") -> str:
    base = paths._safe(getattr(tl, "name", "cut"))
    p = paths.renders_dir() / f"{base}{ext}"
    n = 1
    while p.exists():
        p = paths.renders_dir() / f"{base}_{n}{ext}"
        n += 1
    return str(p)


# --------------------------------------------------------------------------- #
#  waveforms + frame extraction                                                #
# --------------------------------------------------------------------------- #

def waveform(src, in_, out, dest, w=400, h=80) -> str | None:
    exe = ffmpeg_path()
    if not exe or Path(dest).exists():
        return dest if Path(dest).exists() else None
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.05, float(out) - float(in_))
    try:
        run([exe, "-y", "-ss", f"{float(in_):.3f}", "-t", f"{dur:.3f}", "-i", src,
             "-filter_complex",
             f"[0:a]aformat=channel_layouts=mono,compand,showwavespic=s={w}x{h}:colors=0xe0a106[w]",
             "-map", "[w]", "-frames:v", "1", dest])
        return dest if Path(dest).exists() else None
    except Exception:
        return None


def filmstrip(src, in_, out, dest, n=6, fh=48) -> str | None:
    """A horizontal strip of ~n evenly-sampled frames for a video clip's thumbnail."""
    exe = ffmpeg_path()
    if not exe or Path(dest).exists():
        return dest if Path(dest).exists() else None
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, float(out) - float(in_))
    rate = max(0.5, n / dur)
    try:
        run([exe, "-y", "-ss", f"{float(in_):.3f}", "-t", f"{dur:.3f}", "-i", src,
             "-frames:v", "1", "-vf",
             f"fps={rate:.4f},scale=-1:{fh},tile={n}x1", dest], timeout=120)
        return dest if Path(dest).exists() else None
    except Exception:
        return None


def frame_stats(src, t, n=24) -> dict | None:
    """Cheap per-frame color stats for Auto-Enhance / Color-match.

    Decodes ONE frame at time ``t`` area-averaged down to n×n rgb24 and sums it in
    pure Python — no metadata parsing, no version-specific filter behavior. Returns
    mean r/g/b (0-255), luma mean/std, and an HSV-ish mean saturation (0-1); ``None``
    if ffmpeg is missing or the decode produced too few bytes.
    """
    exe = ffmpeg_path()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-ss", f"{max(0.0, float(t)):.3f}", "-i", src,
             "-frames:v", "1", "-vf", f"scale={n}:{n}:flags=area,format=rgb24",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=60)
        buf = proc.stdout or b""
        px = len(buf) // 3
        if px < n * n:
            return None
        sr = sg = sb = 0
        sat = 0.0
        ys = []
        for i in range(px):
            r, g, b = buf[3 * i], buf[3 * i + 1], buf[3 * i + 2]
            sr += r; sg += g; sb += b
            ys.append(0.299 * r + 0.587 * g + 0.114 * b)
            mx, mn = max(r, g, b), min(r, g, b)
            sat += (mx - mn) / mx if mx else 0.0
        ymean = sum(ys) / px
        ystd = (sum((y - ymean) ** 2 for y in ys) / px) ** 0.5
        return {"r": sr / px, "g": sg / px, "b": sb / px,
                "ymean": ymean, "ystd": ystd, "sat": sat / px}
    except Exception:
        return None


def extract_frame(src, t, dest) -> str | None:
    exe = ffmpeg_path()
    if not exe:
        return None
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    try:
        run([exe, "-y", "-ss", f"{max(0.0, float(t)):.3f}", "-i", src,
             "-frames:v", "1", "-q:v", "2", dest], timeout=60)
        return dest if Path(dest).exists() else None
    except Exception:
        return None


def ffmpeg_status() -> dict:
    exe = ffmpeg_path()
    return {"present": bool(exe), "path": exe or "", "version": ffmpeg_version(),
            "ffprobe": ffprobe_path() or ""}
