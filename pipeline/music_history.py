"""Background-music regeneration history.

Music is a film-level asset (one ``background_music.wav`` per work dir), so this
mirrors the cover-image history rather than the per-scene image/video history:
there is no scene id. Every regeneration keeps the prior tracks so the user can
listen to each one and pick the best. History lives entirely in the work dir:

    {work_dir}/music_history.json                     manifest
    {work_dir}/music_history/background_music_v{id}.wav  the kept versions

The canonical ``background_music.wav`` always holds the *selected* version, so the
re-mux/render paths (which read that filename) are untouched. Each version also
records the prompt (``music_desc``) that produced it, so the UI can label them.

All versions are kept (no pruning) — the user explicitly wants to compare every
generation. A module-level lock guards the read-modify-write, and ``_save`` writes
atomically so readers never see a half-written manifest.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

_MANIFEST = "music_history.json"
_SUBDIR = "music_history"
_LOCK = threading.Lock()


def _manifest_path(work_dir: Path) -> Path:
    return Path(work_dir) / _MANIFEST


def _hist_dir(work_dir: Path) -> Path:
    return Path(work_dir) / _SUBDIR


def _canonical(work_dir: Path) -> Path:
    return Path(work_dir) / "background_music.wav"


def _load(work_dir: Path) -> dict:
    p = _manifest_path(work_dir)
    if not p.exists():
        return {"versions": [], "selected": None, "next_id": 1}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {"versions": [], "selected": None, "next_id": 1}
    except Exception:
        return {"versions": [], "selected": None, "next_id": 1}


def _save(work_dir: Path, data: dict) -> None:
    p = _manifest_path(work_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)


def _add_version(work_dir: Path, music: Path, entry: dict, desc: str = "") -> dict:
    """Copy *music* into the history dir as a new version and select it.

    Mutates and returns *entry*; the caller saves the manifest."""
    hist = _hist_dir(work_dir)
    hist.mkdir(parents=True, exist_ok=True)
    vid = int(entry.get("next_id", 1))
    fname = f"background_music_v{vid}.wav"
    shutil.copy2(music, hist / fname)
    entry.setdefault("versions", []).append({"id": vid, "file": fname, "desc": desc or ""})
    entry["selected"] = vid
    entry["next_id"] = vid + 1
    return entry


def seed_if_empty(work_dir: Path, current: Path, desc: str = "") -> None:
    """Capture an existing music track as the first kept version before it's
    overwritten, so the original generation isn't silently discarded."""
    work_dir, current = Path(work_dir), Path(current)
    if not current.exists():
        return
    with _LOCK:
        data = _load(work_dir)
        if data.get("versions"):
            return
        _add_version(work_dir, current, data, desc)
        _save(work_dir, data)


def record(work_dir: Path, music: Path, desc: str = "") -> dict:
    """Add the just-generated track as a new selected version. Returns ``history``."""
    work_dir = Path(work_dir)
    with _LOCK:
        data = _load(work_dir)
        _add_version(work_dir, Path(music), data, desc)
        _save(work_dir, data)
    return history(work_dir)


def select(work_dir: Path, version_id: int) -> Path:
    """Copy a kept version onto the canonical ``background_music.wav`` and return it.

    Raises ValueError/FileNotFoundError if the version is unknown or missing."""
    work_dir = Path(work_dir)
    version_id = int(version_id)
    with _LOCK:
        data = _load(work_dir)
        match = next((v for v in data.get("versions", []) if int(v["id"]) == version_id), None)
        if match is None:
            raise ValueError(f"No music version {version_id}")
        src = _hist_dir(work_dir) / match["file"]
        if not src.exists():
            raise FileNotFoundError(f"Music version file missing: {src}")
        canonical = _canonical(work_dir)
        shutil.copy2(src, canonical)
        data["selected"] = version_id
        _save(work_dir, data)
        return canonical


def history(work_dir: Path) -> dict:
    """Return ``{"versions": [{"id", "path", "desc"}], "selected": id|None}`` for
    API responses, dropping any version whose file has gone missing."""
    work_dir = Path(work_dir)
    data = _load(work_dir)
    hist = _hist_dir(work_dir)
    versions = []
    for v in data.get("versions", []):
        f = hist / v["file"]
        if f.exists():
            versions.append({"id": int(v["id"]), "path": str(f), "desc": v.get("desc", "")})
    selected = data.get("selected")
    valid = {v["id"] for v in versions}
    if selected not in valid:
        selected = versions[-1]["id"] if versions else None
    return {"versions": versions, "selected": selected}
