"""ffmpeg filter-string builders for clip effects. Pure string helpers, no I/O.

Kept separate from render.py so the (fiddly, well-defined) per-clip filter chains
are easy to read and test. All builders return filter fragments (no leading/
trailing separators) or "" when a no-op.
"""
from __future__ import annotations


def _esc_drawtext(s: str) -> str:
    """Escape text for ffmpeg drawtext (colon, backslash, single-quote, percent)."""
    s = (s or "").replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\\\'")
    return s.replace("%", "\\%").replace("\n", "\\n")


def color_vf(color: dict | None) -> str:
    """An eq filter from {brightness,contrast,saturation,gamma}; "" if neutral."""
    if not isinstance(color, dict):
        return ""
    b = float(color.get("brightness", 0.0) or 0.0)     # -1..1
    c = float(color.get("contrast", 1.0) or 1.0)       # 0..2
    s = float(color.get("saturation", 1.0) or 1.0)     # 0..3
    g = float(color.get("gamma", 1.0) or 1.0)          # 0.1..3
    if abs(b) < 1e-3 and abs(c - 1) < 1e-3 and abs(s - 1) < 1e-3 and abs(g - 1) < 1e-3:
        return ""
    return f"eq=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}:gamma={g:.3f}"


def speed_vf(speed: float, reverse: bool) -> str:
    """Video setpts for speed + optional reverse. Speed>1 = faster."""
    parts = []
    if reverse:
        parts.append("reverse")
    s = float(speed or 1.0)
    if abs(s - 1.0) > 1e-3 and s > 0.01:
        parts.append(f"setpts={1.0 / s:.5f}*PTS")
    return ",".join(parts)


def speed_af(speed: float, reverse: bool) -> str:
    """Audio atempo chain for speed (atempo is 0.5..2.0 per stage) + areverse."""
    parts = []
    if reverse:
        parts.append("areverse")
    s = float(speed or 1.0)
    if abs(s - 1.0) > 1e-3 and s > 0.01:
        remaining = s
        guard = 0
        while remaining > 2.0 and guard < 8:
            parts.append("atempo=2.0")
            remaining /= 2.0
            guard += 1
        while remaining < 0.5 and guard < 16:
            parts.append("atempo=0.5")
            remaining /= 0.5
            guard += 1
        if abs(remaining - 1.0) > 1e-3:
            parts.append(f"atempo={remaining:.5f}")
    return ",".join(parts)


# xfade transition names: kind -> base name; wipe/slide add a direction.
_XFADE = {"dissolve": "dissolve", "fade_black": "fadeblack", "fade_white": "fadewhite"}
_WIPE_DIR = {"left": "wipeleft", "right": "wiperight", "up": "wipeup", "down": "wipedown"}
_SLIDE_DIR = {"left": "slideleft", "right": "slideright", "up": "slideup", "down": "slidedown"}


def xfade_transition(kind: str, direction: str = "left") -> str:
    if kind == "wipe":
        return _WIPE_DIR.get(direction, "wipeleft")
    if kind == "slide":
        return _SLIDE_DIR.get(direction, "slideleft")
    return _XFADE.get(kind, "dissolve")


def geometry_scale(geometry: dict | None) -> float:
    if isinstance(geometry, dict):
        try:
            return max(0.02, min(8.0, float(geometry.get("scale", 1.0) or 1.0)))
        except (TypeError, ValueError):
            return 1.0
    return 1.0


def overlay_xy(geometry: dict | None) -> tuple[str, str]:
    """overlay x:y expressions. Default centers (letterbox); geometry sets a corner
    offset in canvas pixels via {x,y}. 'center' keeps centered."""
    if not isinstance(geometry, dict):
        return "(W-w)/2", "(H-h)/2"
    x = geometry.get("x", "center")
    y = geometry.get("y", "center")
    xs = "(W-w)/2" if x in (None, "center") else str(int(x))
    ys = "(H-h)/2" if y in (None, "center") else str(int(y))
    return xs, ys


def drawtext(text: dict | None, fontfile: str | None, W: int, H: int) -> str:
    """A drawtext filter for a title/text clip. Position via {x,y} (pixels or
    'center'); optional box."""
    if not isinstance(text, dict):
        return ""
    content = _esc_drawtext(text.get("content", ""))
    if not content:
        return ""
    size = int(text.get("size", 64) or 64)
    col = (text.get("color") or "#ffffff").replace("#", "0x")
    x = text.get("x", "center")
    y = text.get("y", "center")
    xexpr = "(w-text_w)/2" if x in (None, "center") else str(int(x))
    yexpr = "(h-text_h)/2" if y in (None, "center") else str(int(y))
    parts = [f"text='{content}'", f"fontsize={size}", f"fontcolor={col}",
             f"x={xexpr}", f"y={yexpr}"]
    if fontfile:
        parts.append(f"fontfile='{fontfile}'")
    if text.get("box"):
        bc = (text.get("box_color") or "#000000aa").replace("#", "0x")
        parts.append("box=1")
        parts.append(f"boxcolor={bc}")
        parts.append("boxborderw=12")
    return "drawtext=" + ":".join(parts)
