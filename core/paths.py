"""Filesystem layout for Reel2Reel.

Roots, each independently overridable from the Settings panel and persisted to
``<wan2gp_root>/.reel2reel.json`` so the choice survives restarts:

    projects_dir   default <wan2gp_root>/reel2reel/projects   (saved timelines, *.r2r.json)
    renders_dir    default <wan2gp_root>/reel2reel/renders    (exported *.mp4)
    cache_dir      default <wan2gp_root>/reel2reel/.cache      (thumbnails, normalized clips)

``REEL2REEL_DIR`` overrides the default root for all of them at once.

There is one *read-only* root that is NOT created here and NOT part of the
plugin's data dir: ``wan2gp_outputs_dir()`` — the Wan2GP outputs folder we import
clips *from*. It resolves to the plugin's own override, else the host's configured
save path, else ``<wan2gp_root>/outputs``.

This module imports nothing from Gradio or Wan2GP; it is pure and unit-testable.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("reel2reel.paths")

_DEFAULT_SUBDIR = "reel2reel"
_CONFIG_NAME = ".reel2reel.json"
_config: dict | None = None


# --- config persistence ----------------------------------------------------

def _config_path() -> Path:
    # Stable location (cwd = Wan2GP root), independent of the configurable dirs.
    return Path(os.getcwd()) / _CONFIG_NAME


def load_config() -> dict:
    global _config
    if _config is None:
        try:
            _config = json.loads(_config_path().read_text())
        except Exception:
            _config = {}
    return _config


def save_config() -> None:
    try:
        _config_path().write_text(json.dumps(load_config(), indent=2))
    except Exception:
        logger.warning("Could not write %s", _config_path(), exc_info=True)


def set_dirs(*, projects=None, renders=None, wan2gp_outputs=None) -> None:
    """Override any of the roots (absolute paths) and persist. Empty string clears
    an override (reverts to the default)."""
    cfg = load_config()
    for key, val in (("projects_dir", projects), ("renders_dir", renders),
                     ("wan2gp_outputs_dir", wan2gp_outputs)):
        if val is not None:
            cfg[key] = str(Path(val).expanduser()) if val else ""
    save_config()
    ensure_dirs()


# --- roots -----------------------------------------------------------------

def lab_root() -> Path:
    override = os.environ.get("REEL2REEL_DIR")
    if override:
        return Path(override).expanduser()
    return Path(os.getcwd()) / _DEFAULT_SUBDIR


def _dir(key: str, default_leaf: str) -> Path:
    val = load_config().get(key)
    return Path(val).expanduser() if val else lab_root() / default_leaf


def projects_dir() -> Path:
    return _dir("projects_dir", "projects")


def renders_dir() -> Path:
    return _dir("renders_dir", "renders")


def cache_dir() -> Path:
    return lab_root() / ".cache"


def thumbs_dir() -> Path:
    return cache_dir() / "thumbs"


def norm_dir() -> Path:
    """Normalized-clip intermediates produced by the render pre-pass."""
    return cache_dir() / "norm"


# --- the Wan2GP outputs folder we import FROM (read-only) -------------------

def wan2gp_outputs_dir(server_config: dict | None = None) -> Path:
    """Where to look for clips to import. Resolution order:
        1. our own override (.reel2reel.json -> wan2gp_outputs_dir)
        2. the host's configured save path (server_config['save_path'])
        3. <wan2gp_root>/outputs
    This is read-only and intentionally excluded from ensure_dirs()."""
    val = load_config().get("wan2gp_outputs_dir")
    if val:
        return Path(val).expanduser()
    if isinstance(server_config, dict):
        sp = server_config.get("save_path") or server_config.get("image_save_path")
        if sp:
            return Path(sp).expanduser()
    return Path(os.getcwd()) / "outputs"


def import_candidates(server_config: dict | None = None) -> list[Path]:
    """All the directories worth scanning for importable clips (the outputs dir
    plus our own renders, so a finished cut can be re-edited)."""
    out: list[Path] = []
    for d in (wan2gp_outputs_dir(server_config), renders_dir()):
        if d and Path(d).is_dir():
            out.append(Path(d))
    return out


# --- project files ----------------------------------------------------------

def _safe(name: str) -> str:
    """A filesystem-safe stem for a project name."""
    keep = "-_. "
    cleaned = "".join(c if (c.isalnum() or c in keep) else "_" for c in (name or "")).strip()
    return (cleaned or "untitled").rstrip(". ")


def project_path(name: str) -> Path:
    return projects_dir() / f"{_safe(name)}.r2r.json"


def list_projects() -> list[str]:
    d = projects_dir()
    if not d.is_dir():
        return []
    return sorted(p.name[:-len(".r2r.json")] for p in d.glob("*.r2r.json"))


# --- lifecycle --------------------------------------------------------------

def ensure_dirs() -> Path:
    """Create the plugin's own directory tree if missing. Idempotent; called on
    plugin setup. Never touches the (read-only) Wan2GP outputs dir."""
    for d in (projects_dir(), renders_dir(), cache_dir(), thumbs_dir(), norm_dir()):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.warning("Could not create %s", d, exc_info=True)
    return lab_root()
