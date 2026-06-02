"""Discover importable clips from the Wan2GP outputs folder, and probe/thumbnail
them. No Gradio.

``probe_clip`` / ``thumbnail`` shell out to ffprobe/ffmpeg via the helpers in
``render`` (so the binary is resolved once). Host probe callables (Wan2GP's
``get_video_info`` / extension predicates) can be injected for speed/consistency,
but everything degrades to a self-contained ffprobe fallback.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from . import paths, render

logger = logging.getLogger("reel2reel.discovery")

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".gif"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def kind_of(path) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def list_importable(server_config: dict | None = None, limit: int = 200) -> list[dict]:
    """Newest-first list of importable media in the outputs/renders dirs.
    Returns dicts: {path, name, kind, ctime}."""
    seen: set[str] = set()
    items: list[dict] = []
    for d in paths.import_candidates(server_config):
        try:
            entries = list(Path(d).iterdir())
        except Exception:
            continue
        for p in entries:
            if not p.is_file():
                continue
            k = kind_of(p)
            if not k:
                continue
            ap = str(p.resolve())
            if ap in seen:
                continue
            seen.add(ap)
            try:
                ctime = p.stat().st_mtime
            except Exception:
                ctime = 0.0
            items.append({"path": ap, "name": p.name, "kind": k, "ctime": ctime})
    items.sort(key=lambda e: e["ctime"], reverse=True)
    return items[:limit]


def probe_clip(path: str, get_video_info=None) -> dict:
    """Return {fps, dur, width, height, has_audio} for a media file.
    Uses an injected host ``get_video_info`` if given, else ffprobe."""
    info = {"fps": None, "dur": None, "width": None, "height": None, "has_audio": False}
    if callable(get_video_info):
        try:
            vi = get_video_info(path)
            if isinstance(vi, dict):
                info.update({k: vi.get(k, info[k]) for k in info if k in vi})
                return info
        except Exception:
            pass
    ffprobe = render.ffprobe_path()
    if not ffprobe:
        return info
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
    except Exception:
        return info
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and info["width"] is None:
            info["width"] = s.get("width")
            info["height"] = s.get("height")
            rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
            try:
                num, _, den = rate.partition("/")
                info["fps"] = round(float(num) / float(den), 3) if float(den) else None
            except Exception:
                info["fps"] = None
        if s.get("codec_type") == "audio":
            info["has_audio"] = True
    try:
        info["dur"] = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    return info


def audio_placeholder() -> str | None:
    """A shared amber ♪ tile so audio clips show in the (image-only) gallery."""
    out = paths.thumbs_dir() / "_audio.png"
    if out.exists():
        return str(out)
    try:
        from PIL import Image, ImageDraw
        out.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (320, 200), (47, 111, 94))
        d = ImageDraw.Draw(img)
        d.text((130, 80), "♪", fill=(234, 255, 247))
        img.save(out)
        return str(out)
    except Exception:
        return None


def thumbnail(path: str, clip_id: str, get_video_frame=None) -> str | None:
    """Write a poster-frame jpg into the thumbs cache and return its path.
    Images are shown as-is; videos grab a frame near t=0; audio gets a tile."""
    k = kind_of(path)
    out = paths.thumbs_dir() / f"{clip_id}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return str(out)
    if k == "image":
        return str(path)
    if k == "audio":
        return audio_placeholder()
    ffmpeg = render.ffmpeg_path()
    if not ffmpeg or k != "video":
        return None
    try:
        render.run([ffmpeg, "-y", "-ss", "0.0", "-i", str(path), "-frames:v", "1",
                    "-vf", "scale=320:-1", str(out)])
        return str(out) if out.exists() else None
    except Exception:
        logger.debug("thumbnail failed for %s", path, exc_info=True)
        return None
