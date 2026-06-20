"""Per-scene image regeneration history.

Every time a scene's preview / first-frame image is (re)generated we keep the
prior versions so the user can flip back and forth and pick the best one. History
lives entirely in the job's work directory — no DB schema change:

    {work_dir}/image_history.json                  manifest (one entry per scene)
    {work_dir}/image_history/scene_NN_v{id}.png     the kept versions

The canonical ``scene_NN_preview.png`` always holds the *selected* version, so the
render path (which reuses ``preview_path`` / that filename) is untouched.

All manifest writes happen in the single web-backend process — the bulk preview
endpoint generates scenes in parallel threads — so a module-level lock around the
read-modify-write is enough; ``_save`` also writes atomically so readers never see
a half-written file.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

_MANIFEST = "image_history.json"
_SUBDIR = "image_history"
_LOCK = threading.Lock()


def _manifest_path(work_dir: Path) -> Path:
    return Path(work_dir) / _MANIFEST


def _hist_dir(work_dir: Path) -> Path:
    return Path(work_dir) / _SUBDIR


def _canonical_preview(work_dir: Path, scene_id: int) -> Path:
    return Path(work_dir) / f"scene_{int(scene_id):02d}_preview.png"


def _canonical_first_frame(work_dir: Path, scene_id: int) -> Path:
    return Path(work_dir) / f"scene_{int(scene_id):02d}_first_frame.png"


def _load(work_dir: Path) -> dict:
    p = _manifest_path(work_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(work_dir: Path, data: dict) -> None:
    p = _manifest_path(work_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)


def _entry(data: dict, scene_id: int) -> dict:
    return data.get(str(int(scene_id))) or {"versions": [], "selected": None, "next_id": 1}


def _add_version(work_dir: Path, scene_id: int, image: Path, entry: dict) -> dict:
    """Copy *image* into the history dir as a new version and select it.

    Mutates and returns *entry*; the caller is responsible for saving the manifest."""
    hist = _hist_dir(work_dir)
    hist.mkdir(parents=True, exist_ok=True)
    vid = int(entry.get("next_id", 1))
    fname = f"scene_{int(scene_id):02d}_v{vid}.png"
    shutil.copy2(image, hist / fname)
    entry.setdefault("versions", []).append({"id": vid, "file": fname})
    entry["selected"] = vid
    entry["next_id"] = vid + 1
    return entry


def seed_if_empty(work_dir: Path, scene_id: int, current: Path) -> None:
    """Record *current* as the first kept version if this scene has no history yet.

    Captures a pre-existing preview (made before this feature, or before history was
    first touched) so the next regeneration doesn't silently discard it."""
    work_dir = Path(work_dir)
    current = Path(current)
    if not current.exists():
        return
    with _LOCK:
        data = _load(work_dir)
        entry = _entry(data, scene_id)
        if entry.get("versions"):
            return
        _add_version(work_dir, scene_id, current, entry)
        data[str(int(scene_id))] = entry
        _save(work_dir, data)


def record(work_dir: Path, scene_id: int, image: Path) -> dict:
    """Add *image* (the just-generated canonical preview) as a new version and make
    it the selected one. Returns the history dict (see ``history``)."""
    work_dir = Path(work_dir)
    with _LOCK:
        data = _load(work_dir)
        entry = _entry(data, scene_id)
        _add_version(work_dir, scene_id, Path(image), entry)
        data[str(int(scene_id))] = entry
        _save(work_dir, data)
    return history(work_dir, scene_id)


def select(work_dir: Path, scene_id: int, version_id: int) -> Path:
    """Make version *version_id* the selected one: copy it onto the canonical preview
    (and the first frame too, if one already exists). Returns the canonical preview
    path. Raises ValueError/FileNotFoundError if the version is unknown or missing."""
    work_dir = Path(work_dir)
    version_id = int(version_id)
    with _LOCK:
        data = _load(work_dir)
        entry = _entry(data, scene_id)
        match = next((v for v in entry.get("versions", []) if int(v["id"]) == version_id), None)
        if match is None:
            raise ValueError(f"No image version {version_id} for scene {scene_id}")
        src = _hist_dir(work_dir) / match["file"]
        if not src.exists():
            raise FileNotFoundError(f"Image version file missing: {src}")
        preview = _canonical_preview(work_dir, scene_id)
        shutil.copy2(src, preview)
        first_frame = _canonical_first_frame(work_dir, scene_id)
        if first_frame.exists():
            shutil.copy2(src, first_frame)
        entry["selected"] = version_id
        data[str(int(scene_id))] = entry
        _save(work_dir, data)
        return preview


def history(work_dir: Path, scene_id: int) -> dict:
    """Return ``{"versions": [{"id", "path"}], "selected": id|None}`` for API
    responses, dropping any version whose file has gone missing."""
    work_dir = Path(work_dir)
    data = _load(work_dir)
    entry = _entry(data, scene_id)
    hist = _hist_dir(work_dir)
    versions = []
    for v in entry.get("versions", []):
        f = hist / v["file"]
        if f.exists():
            versions.append({"id": int(v["id"]), "path": str(f)})
    selected = entry.get("selected")
    valid = {v["id"] for v in versions}
    if selected not in valid:
        selected = versions[-1]["id"] if versions else None
    return {"versions": versions, "selected": selected}
