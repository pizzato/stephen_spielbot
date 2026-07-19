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


def _delete_version(work_dir: Path, entry: dict, version_id: int, what: str) -> dict:
    """Remove a kept version from *entry*, deleting its file. The version in use
    (the manifest's selected id, falling back to the newest like ``history``) can't
    be deleted — pick another version first.

    Mutates and returns *entry*; the caller is responsible for saving the manifest.
    Raises ValueError if the version is unknown or currently in use."""
    version_id = int(version_id)
    versions = entry.get("versions", [])
    match = next((v for v in versions if int(v["id"]) == version_id), None)
    if match is None:
        raise ValueError(f"No {what} version {version_id}")
    ids = [int(v["id"]) for v in versions]
    selected = entry.get("selected")
    if selected not in ids:
        selected = ids[-1] if ids else None
    if version_id == selected:
        raise ValueError("This version is in use — pick another one first.")
    (_hist_dir(work_dir) / match["file"]).unlink(missing_ok=True)
    entry["versions"] = [v for v in versions if int(v["id"]) != version_id]
    return entry


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


def delete(work_dir: Path, scene_id: int, version_id: int) -> dict:
    """Delete a kept image version (not the one in use). Returns the history dict."""
    work_dir = Path(work_dir)
    with _LOCK:
        data = _load(work_dir)
        entry = _entry(data, scene_id)
        _delete_version(work_dir, entry, version_id, f"scene {int(scene_id)} image")
        data[str(int(scene_id))] = entry
        _save(work_dir, data)
    return history(work_dir, scene_id)


def history(work_dir: Path, scene_id: int) -> dict:
    """Return ``{"versions": [{"id", "path", "t"}], "selected": id|None}`` for API
    responses, dropping any version whose file has gone missing. ``t`` is the
    file's mtime, for cache-busting version thumbnails whose path was reused
    (scene renumbering renames version files across scenes)."""
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
    → None to drop the scene's history (deleted scene). Version files are
    renamed to match their new scene id (two-phase, so swapped ids can't
    collide) and touched so cache-busted URLs change with the content.
    Non-scene entries (cover, characters) are untouched; scene entries not in
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


# ── cover image history ───────────────────────────────────────────────────────
# The cover is one image per work dir (``cover.png``), so it has no scene id; it
# lives under the reserved manifest key "cover" with versions ``cover_v{id}.png``
# in the same image_history/ dir. Mirrors the scene helpers above.
_COVER_KEY = "cover"


def _cover_canonical(work_dir: Path) -> Path:
    return Path(work_dir) / "cover.png"


def _add_cover_version(work_dir: Path, image: Path, entry: dict) -> dict:
    hist = _hist_dir(work_dir)
    hist.mkdir(parents=True, exist_ok=True)
    vid = int(entry.get("next_id", 1))
    fname = f"cover_v{vid}.png"
    shutil.copy2(image, hist / fname)
    entry.setdefault("versions", []).append({"id": vid, "file": fname})
    entry["selected"] = vid
    entry["next_id"] = vid + 1
    return entry


def cover_seed_if_empty(work_dir: Path, current: Path) -> None:
    """Capture an existing cover as the first kept version before it's overwritten."""
    work_dir, current = Path(work_dir), Path(current)
    if not current.exists():
        return
    with _LOCK:
        data = _load(work_dir)
        entry = data.get(_COVER_KEY) or {"versions": [], "selected": None, "next_id": 1}
        if entry.get("versions"):
            return
        _add_cover_version(work_dir, current, entry)
        data[_COVER_KEY] = entry
        _save(work_dir, data)


def cover_record(work_dir: Path, image: Path) -> dict:
    """Add the just-generated/edited cover as a new selected version."""
    work_dir = Path(work_dir)
    with _LOCK:
        data = _load(work_dir)
        entry = data.get(_COVER_KEY) or {"versions": [], "selected": None, "next_id": 1}
        _add_cover_version(work_dir, Path(image), entry)
        data[_COVER_KEY] = entry
        _save(work_dir, data)
    return cover_history(work_dir)


def cover_select(work_dir: Path, version_id: int) -> Path:
    """Copy a kept cover version onto the canonical cover.png and return it."""
    work_dir = Path(work_dir)
    version_id = int(version_id)
    with _LOCK:
        data = _load(work_dir)
        entry = data.get(_COVER_KEY) or {"versions": [], "selected": None, "next_id": 1}
        match = next((v for v in entry.get("versions", []) if int(v["id"]) == version_id), None)
        if match is None:
            raise ValueError(f"No cover version {version_id}")
        src = _hist_dir(work_dir) / match["file"]
        if not src.exists():
            raise FileNotFoundError(f"Cover version file missing: {src}")
        cover = _cover_canonical(work_dir)
        shutil.copy2(src, cover)
        entry["selected"] = version_id
        data[_COVER_KEY] = entry
        _save(work_dir, data)
        return cover


def cover_delete(work_dir: Path, version_id: int) -> dict:
    """Delete a kept cover version (not the one in use). Returns the history dict."""
    work_dir = Path(work_dir)
    with _LOCK:
        data = _load(work_dir)
        entry = data.get(_COVER_KEY) or {"versions": [], "selected": None, "next_id": 1}
        _delete_version(work_dir, entry, version_id, "cover")
        data[_COVER_KEY] = entry
        _save(work_dir, data)
    return cover_history(work_dir)


def cover_history(work_dir: Path) -> dict:
    """Return ``{"versions": [{"id", "path"}], "selected": id|None}`` for the cover."""
    work_dir = Path(work_dir)
    data = _load(work_dir)
    entry = data.get(_COVER_KEY) or {"versions": [], "selected": None, "next_id": 1}
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


# ── per-script character look history ─────────────────────────────────────────
# Each per-script character has one look image at ``{work_dir}/characters/{id}.png``.
# We keep prior looks so the user can flip back and choose which one anchors scene
# generation. Versions live in the same image_history/ dir as ``char_{id}_v{n}.png``;
# the manifest key is ``char:{id}``. Mirrors the cover helpers above.


def _char_key(char_id: str) -> str:
    return f"char:{char_id}"


def _char_canonical(work_dir: Path, char_id: str) -> Path:
    return Path(work_dir) / "characters" / f"{char_id}.png"


def _add_char_version(work_dir: Path, char_id: str, image: Path, entry: dict) -> dict:
    hist = _hist_dir(work_dir)
    hist.mkdir(parents=True, exist_ok=True)
    vid = int(entry.get("next_id", 1))
    fname = f"char_{char_id}_v{vid}.png"
    shutil.copy2(image, hist / fname)
    entry.setdefault("versions", []).append({"id": vid, "file": fname})
    entry["selected"] = vid
    entry["next_id"] = vid + 1
    return entry


def char_seed_if_empty(work_dir: Path, char_id: str, current: Path) -> None:
    """Capture an existing look as the first kept version before it's overwritten."""
    work_dir, current = Path(work_dir), Path(current)
    if not current.exists():
        return
    with _LOCK:
        data = _load(work_dir)
        entry = data.get(_char_key(char_id)) or {"versions": [], "selected": None, "next_id": 1}
        if entry.get("versions"):
            return
        _add_char_version(work_dir, char_id, current, entry)
        data[_char_key(char_id)] = entry
        _save(work_dir, data)


def char_record(work_dir: Path, char_id: str, image: Path) -> dict:
    """Add the just-generated/uploaded look as a new selected version."""
    work_dir = Path(work_dir)
    with _LOCK:
        data = _load(work_dir)
        entry = data.get(_char_key(char_id)) or {"versions": [], "selected": None, "next_id": 1}
        _add_char_version(work_dir, char_id, Path(image), entry)
        data[_char_key(char_id)] = entry
        _save(work_dir, data)
    return char_history(work_dir, char_id)


def char_select(work_dir: Path, char_id: str, version_id: int) -> Path:
    """Copy a kept look version onto the canonical characters/{id}.png and return it."""
    work_dir = Path(work_dir)
    version_id = int(version_id)
    with _LOCK:
        data = _load(work_dir)
        entry = data.get(_char_key(char_id)) or {"versions": [], "selected": None, "next_id": 1}
        match = next((v for v in entry.get("versions", []) if int(v["id"]) == version_id), None)
        if match is None:
            raise ValueError(f"No character look version {version_id} for {char_id}")
        src = _hist_dir(work_dir) / match["file"]
        if not src.exists():
            raise FileNotFoundError(f"Character look version file missing: {src}")
        canonical = _char_canonical(work_dir, char_id)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, canonical)
        entry["selected"] = version_id
        data[_char_key(char_id)] = entry
        _save(work_dir, data)
        return canonical


def char_delete(work_dir: Path, char_id: str, version_id: int) -> dict:
    """Delete a kept look version (not the one in use). Returns the history dict."""
    work_dir = Path(work_dir)
    with _LOCK:
        data = _load(work_dir)
        entry = data.get(_char_key(char_id)) or {"versions": [], "selected": None, "next_id": 1}
        _delete_version(work_dir, entry, version_id, "character look")
        data[_char_key(char_id)] = entry
        _save(work_dir, data)
    return char_history(work_dir, char_id)


def char_history(work_dir: Path, char_id: str) -> dict:
    """Return ``{"versions": [{"id", "path"}], "selected": id|None}`` for a character."""
    work_dir = Path(work_dir)
    data = _load(work_dir)
    entry = data.get(_char_key(char_id)) or {"versions": [], "selected": None, "next_id": 1}
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
