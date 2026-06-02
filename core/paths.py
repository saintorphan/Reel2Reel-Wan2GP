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
import tempfile
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


def validate_dir(val) -> str | None:
    """Validate a user-supplied directory override. Returns an error string if the
    path is unsafe/unwritable, else None (valid). An empty/None value is a no-op
    (valid — caller treats it as 'leave the configured value alone')."""
    if val in (None, ""):
        return None
    try:
        p = Path(str(val)).expanduser()
        rp = p.resolve()
    except Exception:
        return "not a valid path"
    if rp == rp.parent:                       # a filesystem root ('/', 'C:\\')
        return "refusing a filesystem root"
    if rp == Path.home().resolve():
        return "refusing the home directory"
    if p.exists() and not p.is_dir():
        return "exists but is not a directory"
    # Verify the dir (or its nearest existing parent) is writable.
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        return "could not create the directory"
    if not os.access(str(p), os.W_OK):
        return "directory is not writable"
    return None


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


def uploads_dir() -> Path:
    """Library uploads (bin-imported media) — kept OUT of renders_dir so a
    clear-cache(include_renders=True) can't wipe user media."""
    return lab_root() / "uploads"


def luts_dir() -> Path:
    """Uploaded .cube LUTs — likewise kept out of renders_dir so clear-cache
    can't delete them."""
    return lab_root() / "luts"


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
    """All the directories worth scanning for importable clips (the outputs dir, our
    own renders so a finished cut can be re-edited, and the uploads dir where
    Library-imported media lives)."""
    out: list[Path] = []
    for d in (wan2gp_outputs_dir(server_config), renders_dir(), uploads_dir()):
        if d and Path(d).is_dir():
            out.append(Path(d))
    return out


# --- project files ----------------------------------------------------------

def _safe(name: str) -> str:
    """A filesystem-safe stem for a project name."""
    keep = "-_. "
    cleaned = "".join(c if (c.isalnum() or c in keep) else "_" for c in (name or "")).strip()
    return (cleaned or "untitled").rstrip(". ")


# --- durable atomic writes --------------------------------------------------

def atomic_write_text(path, text: str) -> None:
    """Write ``text`` to ``path`` durably: temp file + flush + fsync, atomic
    os.replace, then fsync the parent directory so a crash can't leave a zero/
    truncated file. The shared writer behind timeline.save and the project
    sidecars (project.json / global_bin.json)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        try:                                # fsync the dir entry too (best-effort)
            dfd = os.open(str(p.parent), os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except (OSError, AttributeError):   # O_DIRECTORY missing on some platforms
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- lifecycle --------------------------------------------------------------

def autosave_path() -> Path:
    return cache_dir() / "autosave.r2r.json"


def _dir_bytes(d: Path) -> int:
    total = 0
    if d and Path(d).is_dir():
        for p in Path(d).rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return total


def cache_bytes() -> int:
    """Bytes used by the thumbnail + normalized-clip caches (safe to delete)."""
    return _dir_bytes(thumbs_dir()) + _dir_bytes(norm_dir())


def renders_bytes() -> int:
    return _dir_bytes(renders_dir())


def clear_cache(include_renders: bool = False) -> int:
    """Wipe the thumbs + norm caches (and optionally renders). Returns bytes freed."""
    import shutil
    freed = cache_bytes() + (renders_bytes() if include_renders else 0)
    targets = [thumbs_dir(), norm_dir()]
    if include_renders:
        targets.append(renders_dir())
    for d in targets:
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    return freed


def human_size(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def ensure_dirs() -> Path:
    """Create the plugin's own directory tree if missing. Idempotent; called on
    plugin setup. Never touches the (read-only) Wan2GP outputs dir."""
    for d in (projects_dir(), renders_dir(), cache_dir(), thumbs_dir(), norm_dir(),
              uploads_dir(), luts_dir()):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.warning("Could not create %s", d, exc_info=True)
    return lab_root()
