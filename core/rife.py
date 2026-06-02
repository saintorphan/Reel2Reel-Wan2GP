"""Optional RIFE temporal interpolation, delegating to the Wan2GP host's bundled
``postprocessing.rife``.

Everything here is BEST-EFFORT and never raises to the caller: if torch / numpy /
the host module / the model weights aren't available (e.g. running outside the host
venv, or the weights haven't been downloaded yet), :func:`available` is ``False`` and
the render falls back to ffmpeg ``minterpolate``. RIFE runs as a post-encode pass
(decode → tensor → interpolate → re-encode + remux audio), so it is GPU- and
memory-bound — capped to a sane frame count; bigger cuts fall back too.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import render

_MODEL_NAMES = ("rife4.26.pkl",)     # v4 weights the host ships / downloads
_MAX_FRAMES = 1200                   # ~40s @30fps — beyond this we fall back (memory)


def _locate_model() -> str | None:
    env = os.environ.get("REEL2REEL_RIFE_MODEL")
    if env and Path(env).exists():
        return env
    for root in (Path.cwd(), Path.cwd() / "ckpts", Path.cwd() / "postprocessing"):
        for name in _MODEL_NAMES:
            try:
                hit = next(root.rglob(name), None)
            except Exception:
                hit = None
            if hit:
                return str(hit)
    return None


def available() -> bool:
    try:
        import numpy  # noqa: F401
        import torch  # noqa: F401
        from postprocessing.rife import inference  # noqa: F401
    except Exception:
        return False
    return _locate_model() is not None


def _device() -> str:
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _probe(src: str):
    """(width, height, frame_count, fps) or None."""
    probe = render.ffprobe_path()
    if not probe:
        return None
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=width,height,nb_read_frames,r_frame_rate",
             "-of", "default=nw=1", src], capture_output=True, text=True, timeout=180).stdout
        w = h = n = 0
        fps = 30.0
        for line in out.splitlines():
            k, _, v = line.partition("=")
            if k == "width":
                w = int(v)
            elif k == "height":
                h = int(v)
            elif k == "nb_read_frames":
                n = int(v)
            elif k == "r_frame_rate" and "/" in v:
                a, b = v.split("/")
                fps = float(a) / max(1.0, float(b))
        return (w, h, n, fps) if (w > 0 and h > 0 and n > 0) else None
    except Exception:
        return None


def interpolate_file(src: str, dest: str, exp: int, progress=None) -> str | None:
    """RIFE-interpolate ``src`` (×2**exp frames) → ``dest``, preserving audio.
    Returns ``dest`` on success, ``None`` on any failure / when too large."""
    exe = render.ffmpeg_path()
    if not exe or int(exp) <= 0:
        return None
    model = _locate_model()
    if not model:
        return None
    try:
        import numpy as np
        import torch
        from postprocessing.rife.inference import temporal_interpolation
    except Exception:
        return None
    info = _probe(src)
    if not info:
        return None
    w, h, n, fps = info
    if n > _MAX_FRAMES:
        return None                                   # too big for an in-memory pass
    try:                                              # decode → uint8 [C,T,H,W]
        raw = subprocess.run([exe, "-v", "error", "-i", src, "-f", "rawvideo",
                              "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=900).stdout
        arr = np.frombuffer(raw, dtype=np.uint8)
        if arr.size < w * h * 3:
            return None
        t = arr.size // (w * h * 3)
        arr = arr[: t * w * h * 3].reshape(t, h, w, 3)
        frames = torch.from_numpy(arr.copy()).permute(3, 0, 1, 2).contiguous()
    except Exception:
        return None
    if callable(progress):
        try:
            progress(0.4, "RIFE interpolating")
        except Exception:
            pass
    try:
        out_t = temporal_interpolation(model, frames, int(exp), device=_device(),
                                       rife_version="v4")
        out_np = out_t.permute(1, 2, 3, 0).contiguous().cpu().numpy().astype(np.uint8)
    except Exception:
        return None
    out_fps = fps * (2 ** int(exp))
    try:                                              # re-encode + remux original audio
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [exe, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
             "-r", f"{out_fps:.5f}", "-i", "-", "-i", src,
             "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", dest],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.communicate(out_np.tobytes(), timeout=1800)
        if proc.returncode != 0 or not Path(dest).exists():
            return None
    except Exception:
        return None
    return dest
