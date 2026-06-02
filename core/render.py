"""ffmpeg export for a Reel2Reel timeline. No Gradio.

Two-stage, multi-pass orchestration (subprocess with arg LISTS — never a shell
string):

  PASS 1 — NORMALIZE: one ffmpeg call per distinct trimmed clip instance, each
  re-encoded to a canonical profile (WxH / fps / SAR / pix_fmt / sample-rate).
  Cached by (src, in, out, canvas) so re-exports and repeated clips are cheap.
  Uniform inputs are what make the composite pass reliable — concat/overlay/xfade
  corrupt or hard-error on mismatched parameters.

  PASS 2 — COMPOSITE: build ONE filter_complex from the normalized intermediates.
  Universal model = overlay-onto-canvas: a black ``color`` source of the full
  timeline length is the base; every video clip is shifted to its timeline start
  (transparent leading pad so lower tracks show through gaps) and overlaid bottom
  track first. Audio clips are front-padded with ``adelay`` to their start,
  volume-adjusted, mixed with ``amix=normalize=0`` and a final ``loudnorm``.

v1 ships this linear path end-to-end. Cross-dissolves (xfade/acrossfade) are
gated behind :func:`has_transitions` and raised as not-yet-implemented so a
render never silently drops a transition.
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
_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


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
    cand = shutil.which("ffprobe")
    return cand if cand else None


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
    """Run ffmpeg/ffprobe (arg list, no shell). Raise RenderError with the stderr
    tail on failure. Returns stdout."""
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-20:])
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
#  canvas profile                                                             #
# --------------------------------------------------------------------------- #

def canvas_of(tl) -> dict:
    return {"w": int(tl.width), "h": int(tl.height), "fps": int(tl.fps),
            "ar": int(tl.sample_rate), "pix": "yuv420p"}


def _is_image(src: str) -> bool:
    return Path(src).suffix.lower() in _IMAGE_EXTS


def _is_audio_only(src: str) -> bool:
    return Path(src).suffix.lower() in _AUDIO_EXTS


def _key(src: str, in_: float, out: float, canvas: dict, stream: str) -> str:
    raw = f"{src}|{in_:.3f}|{out:.3f}|{canvas['w']}x{canvas['h']}@{canvas['fps']}|{canvas['ar']}|{stream}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
#  PASS 1 — normalize                                                         #
# --------------------------------------------------------------------------- #

def normalize_video(clip, canvas: dict, ffmpeg: str) -> str:
    """Re-encode clip[in:out] to a canonical, audio-less, zero-based video."""
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


def normalize_audio(clip, canvas: dict, ffmpeg: str) -> str:
    """Re-encode clip[in:out] to a canonical, zero-based stereo audio file."""
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


def _video_clips_in_order(tl):
    """All video clips, bottom track first then by start — i.e. overlay paint
    order (later = on top)."""
    out = []
    for t in sorted(tl.video_tracks(), key=lambda t: t.index):
        for c in sorted(t.clips, key=lambda c: c.start):
            if c.src:
                out.append(c)
    return out


def _audio_clips(tl):
    out = []
    for t in tl.audio_tracks():
        if t.muted:
            continue
        for c in sorted(t.clips, key=lambda c: c.start):
            if c.src:
                out.append(c)
    return out


def export(tl, out_path: str | None = None, progress_cb=None) -> str:
    """Render the timeline to an mp4. Returns the output path."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RenderError("ffmpeg not found. Install ffmpeg or set REEL2REEL_FFMPEG.")
    if has_transitions(tl):
        raise RenderError(
            "Cross-dissolve transitions aren't wired into the renderer yet "
            "(deferred milestone). Remove transitions to export, or render hard cuts.")

    canvas = canvas_of(tl)
    total = tl.total_duration()
    if total <= 0:
        raise RenderError("Timeline is empty — add a clip before exporting.")

    vclips = _video_clips_in_order(tl)
    aclips = _audio_clips(tl)

    def _progress(frac, desc):
        if callable(progress_cb):
            try:
                progress_cb(frac, desc)
            except Exception:
                pass

    # -- PASS 1: normalize every distinct clip ------------------------------
    norm_v: dict[str, str] = {}
    norm_a: dict[str, str] = {}
    work = vclips + aclips
    for i, c in enumerate(work):
        _progress(0.05 + 0.55 * (i / max(1, len(work))),
                  f"Normalizing clip {i + 1}/{len(work)}")
        if c in vclips:
            norm_v[c.id] = normalize_video(c, canvas, ffmpeg)
        else:
            norm_a[c.id] = normalize_audio(c, canvas, ffmpeg)

    # -- PASS 2: build the composite graph ----------------------------------
    _progress(0.65, "Compositing")
    inputs: list[str] = []
    filt: list[str] = []

    # Base black canvas spanning the whole timeline.
    filt.append(f"color=c=black:s={canvas['w']}x{canvas['h']}:r={canvas['fps']}:"
                f"d={total:.3f},format={canvas['pix']},setsar=1[base]")

    # Video: overlay each clip onto the running base at its timeline start.
    prev = "base"
    for idx, c in enumerate(vclips):
        inputs += ["-i", norm_v[c.id]]
        i = idx  # ffmpeg input index for this clip
        lead = max(0.0, float(c.start))
        # transparent leading pad so lower tracks/canvas show through before the
        # clip begins; opaque clip frames paint over from `start`.
        filt.append(
            f"[{i}:v]setpts=PTS-STARTPTS,format=yuva420p,"
            f"tpad=start_duration={lead:.3f}:start_mode=add:color=black@0[v{i}]")
        out_lbl = f"o{i}"
        filt.append(f"[{prev}][v{i}]overlay=eof_action=pass:format=auto[{out_lbl}]")
        prev = out_lbl
    filt.append(f"[{prev}]format={canvas['pix']}[vout]")

    # Audio: front-pad each clip to its start, gain, then mix + loudnorm.
    n_v = len(vclips)
    a_labels: list[str] = []
    for j, c in enumerate(aclips):
        inputs += ["-i", norm_a[c.id]]
        i = n_v + j
        delay_ms = int(round(max(0.0, float(c.start)) * 1000))
        vol = f",volume={10 ** (float(c.gain_db) / 20):.4f}" if c.gain_db else ""
        filt.append(
            f"[{i}:a]aresample={canvas['ar']},asetpts=PTS-STARTPTS,"
            f"adelay={delay_ms}:all=1{vol}[a{i}]")
        a_labels.append(f"[a{i}]")

    if a_labels:
        if len(a_labels) == 1:
            filt.append(f"{a_labels[0]}aresample={canvas['ar']},"
                        f"loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
        else:
            filt.append(f"{''.join(a_labels)}amix=inputs={len(a_labels)}:"
                        f"duration=longest:normalize=0:dropout_transition=0,"
                        f"loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
        have_audio = True
    else:
        # silent bed so the muxer always has an audio track
        filt.append(f"anullsrc=channel_layout=stereo:sample_rate={canvas['ar']}[aout]")
        have_audio = True

    out_path = str(out_path or _default_out(tl))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    args = [ffmpeg, "-y", *inputs]
    if not aclips:
        # anullsrc is generated in-graph; cap it to the timeline length below.
        pass
    args += ["-filter_complex", ";".join(filt),
             "-map", "[vout]", "-map", "[aout]",
             "-t", f"{total:.3f}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", canvas["pix"],
             "-c:a", "aac", "-b:a", "192k", "-ar", str(canvas["ar"]),
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


def ffmpeg_status() -> dict:
    exe = ffmpeg_path()
    return {"present": bool(exe), "path": exe or "", "version": ffmpeg_version(),
            "ffprobe": ffprobe_path() or ""}
