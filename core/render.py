"""ffmpeg export for a Reel2Reel timeline. No Gradio.

Two-stage, multi-pass orchestration (subprocess with arg LISTS — never a shell
string):

  PASS 1 — NORMALIZE: one ffmpeg call per distinct trimmed clip instance,
  re-encoded to a canonical profile (WxH / fps / SAR / pix_fmt / sample-rate).
  Cached by (src, in, out, canvas). Uniform inputs are what make concat / xfade
  reliable.

  PASS 2 — COMPOSITE: build ONE filter_complex.
    Video — per audible track, fold the clips left-to-right into a single
    yuva420p stream: transparent leading/inter-clip gaps (so lower tracks show
    through), per-clip fades (alpha) and opacity, and xfade where a transition
    joins two clips. Each track stream is overlaid bottom-to-top onto a black
    canvas, then flattened to yuv420p.
    Audio — every audible source (audio-track clips AND un-muted video-clip
    audio) is fade/gain/volume-shaped, front-padded to its start with adelay,
    then amix(normalize=0) + loudnorm.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

from . import paths

logger = logging.getLogger("reel2reel.render")

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class RenderError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
#  binaries                                                                   #
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


def run(args: list[str], timeout: int = 1800) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-24:])
        logger.error("ffmpeg failed (%d): %s\ncmd: %s", proc.returncode, tail,
                     " ".join(args))
        raise RenderError(f"ffmpeg exited {proc.returncode}:\n{tail}")
    return proc.stdout or ""


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
#  canvas + caching                                                           #
# --------------------------------------------------------------------------- #

def canvas_of(tl) -> dict:
    return {"w": int(tl.width), "h": int(tl.height), "fps": int(tl.fps),
            "ar": int(tl.sample_rate), "pix": "yuv420p"}


def _is_image(src: str) -> bool:
    return Path(src).suffix.lower() in _IMAGE_EXTS


def _key(src, in_, out, canvas, stream) -> str:
    raw = f"{src}|{in_:.3f}|{out:.3f}|{canvas['w']}x{canvas['h']}@{canvas['fps']}|{canvas['ar']}|{stream}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
#  PASS 1 — normalize                                                         #
# --------------------------------------------------------------------------- #

def normalize_video(clip, canvas, ffmpeg) -> str:
    out_path = paths.norm_dir() / f"{_key(clip.src, clip.in_, clip.out, canvas, 'v')}.mp4"
    if out_path.exists():
        return str(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dur = max(1.0 / canvas["fps"], clip.dur)
    vf = (f"scale={canvas['w']}:{canvas['h']}:force_original_aspect_ratio=decrease,"
          f"pad={canvas['w']}:{canvas['h']}:-1:-1:color=black,setsar=1,"
          f"fps={canvas['fps']},format={canvas['pix']}")
    args = [ffmpeg, "-y"]
    if _is_image(clip.src):
        args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", clip.src]
    else:
        args += ["-ss", f"{clip.in_:.3f}", "-t", f"{dur:.3f}", "-i", clip.src]
    args += ["-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "18", "-pix_fmt", canvas["pix"], str(out_path)]
    run(args)
    return str(out_path)


def normalize_audio(clip, canvas, ffmpeg) -> str:
    out_path = paths.norm_dir() / f"{_key(clip.src, clip.in_, clip.out, canvas, 'a')}.m4a"
    if out_path.exists():
        return str(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.01, clip.dur)
    af = f"aresample={canvas['ar']},aformat=sample_fmts=fltp:channel_layouts=stereo"
    args = [ffmpeg, "-y", "-ss", f"{clip.in_:.3f}", "-t", f"{dur:.3f}", "-i", clip.src,
            "-vn", "-af", af, "-c:a", "aac", "-ar", str(canvas["ar"]), "-ac", "2",
            str(out_path)]
    run(args)
    return str(out_path)


# --------------------------------------------------------------------------- #
#  PASS 2 — composite                                                         #
# --------------------------------------------------------------------------- #

def has_transitions(tl) -> bool:
    return bool(getattr(tl, "transitions", None))


def _gain(db: float) -> float:
    return 10 ** (float(db) / 20.0)


def export(tl, out_path=None, progress_cb=None) -> str:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RenderError("ffmpeg not found. Install ffmpeg or set REEL2REEL_FFMPEG.")
    canvas = canvas_of(tl)
    total = tl.total_duration()
    if total <= 0:
        raise RenderError("Timeline is empty — add a clip before exporting.")

    vtracks = tl.audible_tracks("Video")
    atracks = tl.audible_tracks("Audio")
    W, H, FPS, AR, PIX = canvas["w"], canvas["h"], canvas["fps"], canvas["ar"], canvas["pix"]

    # transition lookup: left_clip_id -> Transition
    trans_by_left = {x.between[0]: x for x in getattr(tl, "transitions", [])}

    # collect work
    vclips = [(t, c) for t in vtracks for c in sorted(t.clips, key=lambda c: c.start) if c.src]
    audio_src = []
    for t in atracks:
        for c in sorted(t.clips, key=lambda c: c.start):
            if c.src and not c.mute:
                audio_src.append((c, t))
    for t in vtracks:
        for c in sorted(t.clips, key=lambda c: c.start):
            if c.src and c.has_audio and not c.mute:
                audio_src.append((c, t))

    def _progress(frac, desc):
        if callable(progress_cb):
            try:
                progress_cb(frac, desc)
            except Exception:
                pass

    # -- PASS 1: normalize --------------------------------------------------
    norm_v, norm_a = {}, {}
    work = len(vclips) + len(audio_src)
    done = 0
    for _, c in vclips:
        norm_v[c.id] = normalize_video(c, canvas, ffmpeg)
        done += 1
        _progress(0.05 + 0.5 * (done / max(1, work)), f"Normalizing {done}/{work}")
    for c, _ in audio_src:
        norm_a[c.id] = normalize_audio(c, canvas, ffmpeg)
        done += 1
        _progress(0.05 + 0.5 * (done / max(1, work)), f"Normalizing {done}/{work}")

    # -- PASS 2: build the graph -------------------------------------------
    _progress(0.6, "Compositing")
    inputs: list[str] = []
    filt: list[str] = []
    idx = 0

    def transparent(dur: float, label: str):
        filt.append(f"color=c=black:s={W}x{H}:r={FPS}:d={dur:.3f},"
                    f"format=yuva420p,colorchannelmixer=aa=0,setsar=1[{label}]")

    # VIDEO — black base canvas, then a folded stream per track overlaid on top.
    filt.append(f"color=c=black:s={W}x{H}:r={FPS}:d={total:.3f},"
                f"format={PIX},setsar=1[vbase0]")
    vbase = "vbase0"
    ti = 0
    for t in vtracks:
        clips = sorted([c for c in t.clips if c.src], key=lambda c: c.start)
        if not clips:
            continue
        acc, acc_end, prev_clip = None, 0.0, None
        for c in clips:
            i = idx
            inputs += ["-i", norm_v[c.id]]
            idx += 1
            chain = (f"[{i}:v]setpts=PTS-STARTPTS,fps={FPS},format=yuva420p,setsar=1")
            if c.opacity < 0.999:
                chain += f",colorchannelmixer=aa={max(0.0, min(1.0, c.opacity)):.3f}"
            if c.fade_in > 0.001:
                chain += f",fade=t=in:st=0:d={c.fade_in:.3f}:alpha=1"
            if c.fade_out > 0.001:
                st = max(0.0, c.dur - c.fade_out)
                chain += f",fade=t=out:st={st:.3f}:d={c.fade_out:.3f}:alpha=1"
            chain += f"[cl{i}]"
            filt.append(chain)
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
                tr = trans_by_left.get(prev_clip.id) if prev_clip else None
                if tr and tr.between[1] == c.id:
                    off = max(0.0, acc_end - tr.duration)
                    filt.append(f"[{acc}][{cl}]xfade=transition=dissolve:"
                                f"duration={tr.duration:.3f}:offset={off:.3f}[acc{i}]")
                    acc = f"acc{i}"
                    acc_end = acc_end - tr.duration + c.dur
                else:
                    gap = c.start - acc_end
                    if gap > 0.001:
                        transparent(gap, f"gap{i}")
                        filt.append(f"[{acc}][gap{i}][{cl}]concat=n=3:v=1:a=0[acc{i}]")
                    else:
                        filt.append(f"[{acc}][{cl}]concat=n=2:v=1:a=0[acc{i}]")
                    acc = f"acc{i}"
                    acc_end = c.end
            prev_clip = c
        filt.append(f"[{vbase}][{acc}]overlay=eof_action=pass:format=auto[vb{ti}]")
        vbase = f"vb{ti}"
        ti += 1
    filt.append(f"[{vbase}]format={PIX}[vout]")

    # AUDIO — fade/gain/volume each source, place with adelay, mix + loudnorm.
    a_labels = []
    for c, t in audio_src:
        i = idx
        inputs += ["-i", norm_a[c.id]]
        idx += 1
        ch = f"[{i}:a]aresample={AR},asetpts=PTS-STARTPTS"
        if c.fade_in > 0.001:
            ch += f",afade=t=in:st=0:d={c.fade_in:.3f}"
        if c.fade_out > 0.001:
            ch += f",afade=t=out:st={max(0.0, c.dur - c.fade_out):.3f}:d={c.fade_out:.3f}"
        gain = float(c.gain_db) + float(getattr(t, "volume_db", 0.0))
        if abs(gain) > 1e-6:
            ch += f",volume={_gain(gain):.4f}"
        delay = int(round(max(0.0, c.start) * 1000))
        ch += f",adelay={delay}:all=1[a{i}]"
        filt.append(ch)
        a_labels.append(f"[a{i}]")

    if a_labels:
        if len(a_labels) == 1:
            filt.append(f"{a_labels[0]}aresample={AR},loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
        else:
            filt.append(f"{''.join(a_labels)}amix=inputs={len(a_labels)}:"
                        f"duration=longest:normalize=0:dropout_transition=0,"
                        f"loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
    else:
        filt.append(f"anullsrc=channel_layout=stereo:sample_rate={AR}[aout]")

    out_path = str(out_path or _default_out(tl))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    args = [ffmpeg, "-y", *inputs, "-filter_complex", ";".join(filt),
            "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", PIX,
            "-c:a", "aac", "-b:a", "192k", "-ar", str(AR),
            "-movflags", "+faststart", out_path]
    _progress(0.7, "Encoding")
    run(args)
    _progress(1.0, "Done")
    return out_path


def _default_out(tl) -> str:
    base = paths._safe(getattr(tl, "name", "cut"))
    p = paths.renders_dir() / f"{base}.mp4"
    n = 1
    while p.exists():
        p = paths.renders_dir() / f"{base}_{n}.mp4"
        n += 1
    return str(p)


# --------------------------------------------------------------------------- #
#  waveforms (for audio clip thumbnails)                                      #
# --------------------------------------------------------------------------- #

def waveform(src: str, in_: float, out: float, dest: str,
             w: int = 400, h: int = 80) -> str | None:
    """Render a waveform PNG for src[in:out] via showwavespic; cached at dest."""
    exe = ffmpeg_path()
    if not exe:
        return None
    if Path(dest).exists():
        return dest
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.05, float(out) - float(in_))
    try:
        # Label the audio input explicitly ([0:a]) and map the picture output —
        # an unlabeled filter_complex pad can't bind to the audio stream of a
        # multi-stream (video) container.
        run([exe, "-y", "-ss", f"{float(in_):.3f}", "-t", f"{dur:.3f}", "-i", src,
             "-filter_complex",
             f"[0:a]aformat=channel_layouts=mono,compand,"
             f"showwavespic=s={w}x{h}:colors=0xe0a106[w]",
             "-map", "[w]", "-frames:v", "1", dest])
        return dest if Path(dest).exists() else None
    except Exception:
        return None


def extract_frame(src: str, t: float, dest: str) -> str | None:
    """Grab a single frame at time ``t`` (seconds) from ``src`` into ``dest`` (jpg).
    Used to seed the Video Generator's start / end / anchor keyframe slots."""
    exe = ffmpeg_path()
    if not exe:
        return None
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    try:
        run([exe, "-y", "-ss", f"{max(0.0, float(t)):.3f}", "-i", src,
             "-frames:v", "1", "-q:v", "2", dest])
        return dest if Path(dest).exists() else None
    except Exception:
        return None


def ffmpeg_status() -> dict:
    exe = ffmpeg_path()
    return {"present": bool(exe), "path": exe or "", "version": ffmpeg_version(),
            "ffprobe": ffprobe_path() or ""}
