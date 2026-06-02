"""The timeline widget: a gr.HTML mount shell + the two hidden gr.Textbox JSON
pipes that bridge the browser timeline (assets/static/timeline.js) to Python.

The JS module itself is delivered via WAN2GPPlugin.add_custom_js() in plugin.py
(read with :func:`timeline_js`), because Gradio runs add_custom_js inside its
single on-load init function — whereas <script> tags inside gr.HTML innerHTML do
NOT execute. The CSS, by contrast, is injected here as a <style> blob (styles in
innerHTML do apply).
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

import gradio as gr

logger = logging.getLogger("reel2reel.widget")

_STATIC = Path(__file__).resolve().parent.parent / "assets" / "static"
_ASSETS = Path(__file__).resolve().parent.parent / "assets"

ROOT_ID = "r2r_timeline_root"
TO_PY_ID = "r2r_tl_to_py"
FROM_PY_ID = "r2r_tl_from_py"

# JS hook run by tl_from_py.change to push an op-envelope into the browser.
APPLY_OP_JS = ("(p) => { try { window.R2RTimeline && window.R2RTimeline.applyOp(p); } "
               "catch (e) { console.error('[R2R]', e); } }")


def _read(name: str) -> str:
    try:
        return (_STATIC / name).read_text(encoding="utf-8")
    except Exception:
        logger.warning("Could not read %s", name, exc_info=True)
        return ""


def timeline_js() -> str:
    return _read("timeline.js")


def timeline_css() -> str:
    return _read("timeline.css")


def file_url(path) -> str | None:
    """A browser URL for a server-side absolute path via Gradio's static route.
    Filenames contain spaces (Wan2GP names outputs after the prompt), so quote."""
    if not path:
        return None
    return "/gradio_api/file=" + quote(str(path), safe="/:")


def register_static_paths(extra_dirs=None) -> None:
    """Allow Gradio to serve our assets + render/cache dirs by absolute path.
    Cumulative global; safe to call more than once. Best-effort.

    Hardened: each dir is normalized to an absolute resolved path and any that
    resolves to '/', a filesystem root, or the user's home dir is SKIPPED (with a
    warning) — set_static_paths is process-global, so an over-broad root would make
    the whole filesystem fetchable via /gradio_api/file=."""
    home = Path.home().resolve()
    dirs = [str(_ASSETS.resolve())]
    for d in (extra_dirs or []):
        if not d:
            continue
        try:
            rp = Path(str(d)).expanduser().resolve()
        except Exception:
            continue
        if rp == rp.parent or rp == home:        # filesystem root or $HOME
            logger.warning("Refusing to register over-broad static path %s", rp)
            continue
        dirs.append(str(rp))
    try:
        gr.set_static_paths(dirs)
    except Exception:
        logger.debug("set_static_paths unavailable", exc_info=True)


def build_timeline_widget() -> dict:
    """Returns {mount, tl_to_py, tl_from_py}. plugin.py owns the wiring."""
    css = timeline_css()
    mount_html = (
        f"<style>{css}</style>"
        f"<div id='{ROOT_ID}'>"
        f"  <div class='r2r-tl'><div class='r2r-scroll' style='padding:24px;color:#888'>"
        f"  Loading timeline…</div></div>"
        f"</div>"
    )
    c = {}
    c["mount"] = gr.HTML(mount_html, elem_classes="reel2reel-acc")
    # Hidden JSON bridges. Kept interactive so JS can change them / Python can write.
    c["tl_to_py"] = gr.Textbox(elem_id=TO_PY_ID, visible=False, interactive=True,
                               value="", lines=1)
    c["tl_from_py"] = gr.Textbox(elem_id=FROM_PY_ID, visible=False, interactive=True,
                                 value="", lines=1)
    return c
