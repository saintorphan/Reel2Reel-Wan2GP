"""Projects: each saved timeline is a folder under ``projects_dir()`` with full
CRUD, a per-project media bin, and manual named version snapshots. No Gradio.

Layout::

    projects_dir()/<slug>/
        project.json          # meta: name, created, modified, bin[], versions[]
        timeline.r2r.json     # the current timeline (Reel2ReelProject.1)
        versions/<slug>.r2r.json   # named snapshots

Legacy flat ``<name>.r2r.json`` files (v0.1/0.2) are migrated into this layout on
first scan.
"""
from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path

from . import paths, timeline

META_SCHEMA = "Reel2ReelProjectMeta.1"
_TIMELINE = "timeline.r2r.json"
_META = "project.json"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def slug(name: str) -> str:
    return paths._safe(name)


def project_dir(name: str) -> Path:
    return paths.projects_dir() / slug(name)


def _meta_path(name: str) -> Path:
    return project_dir(name) / _META


def _timeline_path(name: str) -> Path:
    return project_dir(name) / _TIMELINE


def _versions_dir(name: str) -> Path:
    return project_dir(name) / "versions"


def exists(name: str) -> bool:
    return bool(name) and _meta_path(name).is_file()


# --- meta ------------------------------------------------------------------

def _default_meta(name: str) -> dict:
    return {"schema": META_SCHEMA, "name": name, "created": _now(),
            "modified": _now(), "bin": [], "versions": []}


def load_meta(name: str) -> dict:
    try:
        m = json.loads(_meta_path(name).read_text())
        m.setdefault("bin", [])
        m.setdefault("versions", [])
        m.setdefault("name", name)
        return m
    except Exception:
        return _default_meta(name)


def save_meta(name: str, meta: dict) -> None:
    meta["modified"] = _now()
    project_dir(name).mkdir(parents=True, exist_ok=True)
    _meta_path(name).write_text(json.dumps(meta, indent=2))


# --- discovery / migration -------------------------------------------------

def migrate_legacy() -> int:
    """Move any flat ``projects_dir()/*.r2r.json`` into the folder layout."""
    root = paths.projects_dir()
    if not root.is_dir():
        return 0
    moved = 0
    for p in list(root.glob("*.r2r.json")):
        if not p.is_file():
            continue
        name = p.name[:-len(".r2r.json")]
        d = project_dir(name)
        if d.exists():
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(_timeline_path(name)))
            save_meta(name, _default_meta(name))
            moved += 1
        except Exception:
            pass
    return moved


def list_projects() -> list[str]:
    migrate_legacy()
    root = paths.projects_dir()
    if not root.is_dir():
        return []
    names = []
    for d in root.iterdir():
        if d.is_dir() and (d / _META).is_file():
            try:
                names.append(json.loads((d / _META).read_text()).get("name", d.name))
            except Exception:
                names.append(d.name)
    return sorted(names, key=str.lower)


# --- CRUD ------------------------------------------------------------------

def create(name: str, tl: timeline.Timeline | None = None) -> str:
    if not (name or "").strip():
        raise ValueError("Project needs a name.")
    if exists(name):
        raise FileExistsError(f"Project '{name}' already exists.")
    project_dir(name).mkdir(parents=True, exist_ok=True)
    _versions_dir(name).mkdir(parents=True, exist_ok=True)
    tl = tl or timeline.Timeline(name=name)
    tl.name = name
    timeline.save(_timeline_path(name), tl)
    save_meta(name, _default_meta(name))
    return name


def delete(name: str) -> bool:
    d = project_dir(name)
    if not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return not d.exists()


def rename(old: str, new: str) -> str:
    if not exists(old):
        raise FileNotFoundError(f"Project '{old}' not found.")
    if slug(old) == slug(new):
        meta = load_meta(old)
        meta["name"] = new
        save_meta(old, meta)
        return new
    if exists(new):
        raise FileExistsError(f"Project '{new}' already exists.")
    project_dir(old).rename(project_dir(new))
    meta = load_meta(new)
    meta["name"] = new
    save_meta(new, meta)
    return new


def duplicate(src: str, new: str) -> str:
    if not exists(src):
        raise FileNotFoundError(f"Project '{src}' not found.")
    if exists(new):
        raise FileExistsError(f"Project '{new}' already exists.")
    shutil.copytree(project_dir(src), project_dir(new))
    meta = load_meta(new)
    meta["name"] = new
    meta["created"] = _now()
    save_meta(new, meta)
    return new


# --- timeline read/write ---------------------------------------------------

def save_timeline(name: str, tl: timeline.Timeline) -> str:
    if not exists(name):
        create(name, tl)
        return name
    tl.name = name
    timeline.save(_timeline_path(name), tl)
    save_meta(name, load_meta(name))      # touch modified
    return name


def load_timeline(name: str) -> timeline.Timeline | None:
    p = _timeline_path(name)
    if not p.is_file():
        return None
    return timeline.load(p)


# --- versioning (manual named snapshots) -----------------------------------

def snapshot(name: str, label: str, tl: timeline.Timeline) -> str:
    if not exists(name):
        create(name, tl)
    label = (label or "").strip() or _now().replace(":", "-")
    vdir = _versions_dir(name)
    vdir.mkdir(parents=True, exist_ok=True)
    meta = load_meta(name)
    existing = next((v for v in meta["versions"] if v.get("label") == label), None)
    if existing:
        fname = existing["file"]                       # re-snapshot a label -> overwrite
    else:
        # Distinct labels must get distinct files even when their slugs collide
        # (e.g. "v1 beta" vs "v1-beta") — otherwise one restore loads the other.
        used = {v.get("file") for v in meta["versions"]}
        base = slug(label)
        fname, n = f"{base}.r2r.json", 1
        while fname in used or (vdir / fname).exists():
            fname = f"{base}_{n}.r2r.json"
            n += 1
    timeline.save(vdir / fname, tl)
    meta["versions"] = [v for v in meta["versions"] if v.get("label") != label]
    meta["versions"].append({"label": label, "file": fname, "created": _now()})
    save_meta(name, meta)
    return label


def list_versions(name: str) -> list[dict]:
    return list(load_meta(name).get("versions", []))


def version_labels(name: str) -> list[str]:
    return [v.get("label", "") for v in list_versions(name)]


def restore_version(name: str, label: str) -> timeline.Timeline | None:
    for v in list_versions(name):
        if v.get("label") == label:
            p = _versions_dir(name) / v.get("file", "")
            if p.is_file():
                return timeline.load(p)
    return None


def delete_version(name: str, label: str) -> bool:
    meta = load_meta(name)
    keep, removed = [], None
    for v in meta.get("versions", []):
        if v.get("label") == label:
            removed = v
        else:
            keep.append(v)
    if removed is None:
        return False
    try:
        (_versions_dir(name) / removed.get("file", "")).unlink(missing_ok=True)
    except Exception:
        pass
    meta["versions"] = keep
    save_meta(name, meta)
    return True


# --- media bin -------------------------------------------------------------

def get_bin(name: str) -> list[str]:
    return list(load_meta(name).get("bin", [])) if exists(name) else []


def set_bin(name: str, items: list[str]) -> None:
    if not exists(name):
        return
    meta = load_meta(name)
    seen, out = set(), []
    for p in items:
        ap = str(p)
        if ap and ap not in seen:
            seen.add(ap)
            out.append(ap)
    meta["bin"] = out
    save_meta(name, meta)


def add_to_bin(name: str, items) -> list[str]:
    cur = get_bin(name)
    add = [items] if isinstance(items, str) else list(items or [])
    set_bin(name, cur + add)
    return get_bin(name)


def remove_from_bin(name: str, item: str) -> list[str]:
    set_bin(name, [p for p in get_bin(name) if p != item])
    return get_bin(name)


# --- global media bin (cross-project, persistent) --------------------------

def _global_bin_path() -> Path:
    return paths.lab_root() / "global_bin.json"


def get_global_bin() -> list[str]:
    try:
        data = json.loads(_global_bin_path().read_text())
        return [str(p) for p in data] if isinstance(data, list) else []
    except Exception:
        return []


def set_global_bin(items: list[str]) -> None:
    seen, out = set(), []
    for p in items:
        ap = str(p)
        if ap and ap not in seen:
            seen.add(ap)
            out.append(ap)
    try:
        _global_bin_path().parent.mkdir(parents=True, exist_ok=True)
        _global_bin_path().write_text(json.dumps(out, indent=2))
    except Exception:
        pass


def add_to_global_bin(items) -> list[str]:
    add = [items] if isinstance(items, str) else list(items or [])
    set_global_bin(get_global_bin() + add)
    return get_global_bin()


def remove_from_global_bin(item: str) -> list[str]:
    set_global_bin([p for p in get_global_bin() if p != item])
    return get_global_bin()
