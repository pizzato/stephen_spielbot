"""Per-scene rendered-video history.

Every time a scene's video is re-rendered (or re-muxed after a narration change)
we keep the prior takes so the user can flip back and forth in the edit screen and
pick the best one. History lives entirely in the job's work directory — no DB
schema change, mirroring ``image_history``:

    {work_dir}/video_history.json                  manifest (one entry per scene)
    {work_dir}/video_history/scene_NN_v{id}.mp4     the kept takes

The canonical ``scene_NN_final.mp4`` always holds the *selected* take, so the
assembly path (which concatenates those filenames) is untouched.

Video takes are large (~5–20 MB each), so only the most recent ``_MAX_VERSIONS``
takes per scene are kept; older take files are pruned on each new record. The
currently-selected take is never pruned.

All manifest writes happen in the single web-backend process, so a module-level
lock around the read-modify-write is enough; ``_save`` writes atomically so
readers never see a half-written file.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

_MANIFEST = "video_history.json"
_SUBDIR = "video_history"
# 10, not 3: acted (H3) takes cost ~6 minutes of GPU each — "every re-shoot is
# kept" has to actually mean it. Still capped so a re-roll marathon can't eat
# the disk; the selected take is always kept regardless.
_MAX_VERSIONS = 10
_LOCK = threading.Lock()


def _manifest_path(work_dir: Path) -> Path:
    return Path(work_dir) / _MANIFEST


def _hist_dir(work_dir: Path) -> Path:
    return Path(work_dir) / _SUBDIR


def _canonical_final(work_dir: Path, scene_id: int) -> Path:
    return Path(work_dir) / f"scene_{int(scene_id):02d}_final.mp4"


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


def _prune(work_dir: Path, entry: dict) -> None:
    """Keep only the most recent ``_MAX_VERSIONS`` takes (plus the selected one),
    deleting the dropped take files. Mutates *entry* in place."""
    versions = entry.get("versions", [])
    if len(versions) <= _MAX_VERSIONS:
        return
    selected = entry.get("selected")
    keep_ids = {v["id"] for v in versions[-_MAX_VERSIONS:]}
    if selected is not None:
        keep_ids.add(selected)
    kept, dropped = [], []
    for v in versions:
        (kept if v["id"] in keep_ids else dropped).append(v)
    hist = _hist_dir(work_dir)
    for v in dropped:
        (hist / v["file"]).unlink(missing_ok=True)
    entry["versions"] = kept


def _add_version(work_dir: Path, scene_id: int, video: Path, entry: dict) -> dict:
    """Copy *video* into the history dir as a new take and select it.

    Mutates and returns *entry*; the caller is responsible for saving the manifest."""
    hist = _hist_dir(work_dir)
    hist.mkdir(parents=True, exist_ok=True)
    vid = int(entry.get("next_id", 1))
    fname = f"scene_{int(scene_id):02d}_v{vid}.mp4"
    shutil.copy2(video, hist / fname)
    entry.setdefault("versions", []).append({"id": vid, "file": fname})
    entry["selected"] = vid
    entry["next_id"] = vid + 1
    _prune(work_dir, entry)
    return entry


def seed_if_empty(work_dir: Path, scene_id: int, current: Path) -> None:
    """Record *current* as the first kept take if this scene has no history yet.

    Captures a pre-existing final (made before this feature) so the next re-render
    doesn't silently discard it."""
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


def record(work_dir: Path, scene_id: int, video: Path) -> dict:
    """Add *video* (the just-rendered canonical final) as a new take and make it the
    selected one. Returns the history dict (see ``history``)."""
    work_dir = Path(work_dir)
    with _LOCK:
        data = _load(work_dir)
        entry = _entry(data, scene_id)
        _add_version(work_dir, scene_id, Path(video), entry)
        data[str(int(scene_id))] = entry
        _save(work_dir, data)
    return history(work_dir, scene_id)


def select(work_dir: Path, scene_id: int, version_id: int) -> Path:
    """Make take *version_id* the selected one: copy it onto the canonical final.
    Returns the canonical final path. Raises ValueError/FileNotFoundError if the
    take is unknown or missing."""
    work_dir = Path(work_dir)
    version_id = int(version_id)
    with _LOCK:
        data = _load(work_dir)
        entry = _entry(data, scene_id)
        match = next((v for v in entry.get("versions", []) if int(v["id"]) == version_id), None)
        if match is None:
            raise ValueError(f"No video take {version_id} for scene {scene_id}")
        src = _hist_dir(work_dir) / match["file"]
        if not src.exists():
            raise FileNotFoundError(f"Video take file missing: {src}")
        final = _canonical_final(work_dir, scene_id)
        shutil.copy2(src, final)
        entry["selected"] = version_id
        data[str(int(scene_id))] = entry
        _save(work_dir, data)
        return final


def delete(work_dir: Path, scene_id: int, version_id: int) -> dict:
    """Delete a kept take (not the one in use — the manifest's selected id, falling
    back to the newest like ``history``). Returns the history dict. Raises
    ValueError if the take is unknown or currently in use."""
    work_dir = Path(work_dir)
    version_id = int(version_id)
    with _LOCK:
        data = _load(work_dir)
        entry = _entry(data, scene_id)
        versions = entry.get("versions", [])
        match = next((v for v in versions if int(v["id"]) == version_id), None)
        if match is None:
            raise ValueError(f"No video take {version_id} for scene {scene_id}")
        ids = [int(v["id"]) for v in versions]
        selected = entry.get("selected")
        if selected not in ids:
            selected = ids[-1] if ids else None
        if version_id == selected:
            raise ValueError("This take is in use — pick another one first.")
        (_hist_dir(work_dir) / match["file"]).unlink(missing_ok=True)
        entry["versions"] = [v for v in versions if int(v["id"]) != version_id]
        data[str(int(scene_id))] = entry
        _save(work_dir, data)
    return history(work_dir, scene_id)


def history(work_dir: Path, scene_id: int) -> dict:
    """Return ``{"versions": [{"id", "path", "t"}], "selected": id|None}`` for API
    responses, dropping any take whose file has gone missing. ``t`` is the file's
    mtime, for cache-busting take thumbnails whose path was reused (scene
    renumbering renames take files across scenes)."""
    work_dir = Path(work_dir)
    data = _load(work_dir)
    entry = _entry(data, scene_id)
    hist = _hist_dir(work_dir)
    versions = []
    for v in entry.get("versions", []):
        f = hist / v["file"]
        if f.exists():
            versions.append({"id": int(v["id"]), "path": str(f), "t": int(f.stat().st_mtime)})
    selected = entry.get("selected")
    valid = {v["id"] for v in versions}
    if selected not in valid:
        selected = versions[-1]["id"] if versions else None
    return {"versions": versions, "selected": selected}


def remap_scene_ids(work_dir: Path, id_map: dict) -> None:
    """Renumber the scene-keyed manifest entries after the script editor
    inserts/removes/reorders scenes. *id_map* maps old scene id → new id, or
    → None to drop the scene's takes (deleted scene). Take files are renamed to
    match their new scene id (two-phase, so swapped ids can't collide) and
    touched so cache-busted URLs change with the content. Scene entries not in
    *id_map* are orphans of scenes that no longer exist and are dropped."""
    work_dir = Path(work_dir)
    with _LOCK:
        data = _load(work_dir)
        if not data:
            return
        hist = _hist_dir(work_dir)
        out: dict = {}
        renames: list[tuple[Path, Path]] = []
        for key, entry in data.items():
            if not key.isdigit():
                out[key] = entry
                continue
            new_id = id_map.get(int(key))
            if new_id is None:
                for v in entry.get("versions", []):
                    (hist / v["file"]).unlink(missing_ok=True)
                continue
            for v in entry.get("versions", []):
                new_name = f"scene_{int(new_id):02d}_v{int(v['id'])}{Path(v['file']).suffix}"
                src = hist / v["file"]
                if v["file"] != new_name and src.exists():
                    renames.append((src, hist / new_name))
                v["file"] = new_name
            out[str(int(new_id))] = entry
        # Two-phase rename: park every source under a unique temp name first so
        # a swap (1↔2) never overwrites a file that is itself being moved.
        staged = []
        for src, dst in renames:
            tmp = dst.with_name(dst.name + ".remap-tmp")
            src.replace(tmp)
            staged.append((tmp, dst))
        for tmp, dst in staged:
            tmp.replace(dst)
            dst.touch()
        _save(work_dir, out)
