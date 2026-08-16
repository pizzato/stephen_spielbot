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

A song film's versions come from two different acts, so each one also records
``voice`` — empty for a straight generation, the library voice for a seed-vc
re-voicing — and ``source_id``, the version a re-voicing was converted from.
That is what keeps the before/after pair of a re-voicing legible (and lets the
next re-voicing start from the *sung* original rather than stacking clones).

Nothing is pruned automatically — the user wants to compare every generation —
but a version can be deleted by hand (``delete``), because a song film often
takes a dozen tries to land one. A module-level lock guards the
read-modify-write, and ``_save`` writes atomically so readers never see a
half-written manifest.
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


def _add_version(work_dir: Path, music: Path, entry: dict, desc: str = "",
                 voice: str = "", source_id: int | None = None) -> dict:
    """Copy *music* into the history dir as a new version and select it.

    Mutates and returns *entry*; the caller saves the manifest."""
    hist = _hist_dir(work_dir)
    hist.mkdir(parents=True, exist_ok=True)
    vid = int(entry.get("next_id", 1))
    fname = f"background_music_v{vid}.wav"
    shutil.copy2(music, hist / fname)
    entry.setdefault("versions", []).append(
        {"id": vid, "file": fname, "desc": desc or "", "voice": voice or "",
         "source_id": int(source_id) if source_id else None})
    entry["selected"] = vid
    entry["next_id"] = vid + 1
    return entry


def seed_if_empty(work_dir: Path, current: Path, desc: str = "",
                  voice: str = "") -> None:
    """Capture an existing music track as the first kept version before it's
    overwritten, so the original generation isn't silently discarded."""
    work_dir, current = Path(work_dir), Path(current)
    if not current.exists():
        return
    with _LOCK:
        data = _load(work_dir)
        if data.get("versions"):
            return
        _add_version(work_dir, current, data, desc, voice)
        _save(work_dir, data)


def record(work_dir: Path, music: Path, desc: str = "", voice: str = "",
           source_id: int | None = None) -> dict:
    """Add the just-generated track as a new selected version. Returns ``history``."""
    work_dir = Path(work_dir)
    with _LOCK:
        data = _load(work_dir)
        _add_version(work_dir, Path(music), data, desc, voice, source_id)
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


def delete(work_dir: Path, version_id: int) -> dict:
    """Drop a kept version and its file. Returns the remaining ``history``.

    Deleting the version in use hands the film the newest one left (copied onto
    the canonical track), so ``background_music.wav`` is always a version the
    user can point at. The last remaining version can't be deleted — it *is*
    the film's soundtrack.

    Raises ValueError if the version is unknown or is the only one left."""
    work_dir = Path(work_dir)
    version_id = int(version_id)
    with _LOCK:
        data = _load(work_dir)
        versions = data.get("versions", [])
        match = next((v for v in versions if int(v["id"]) == version_id), None)
        if match is None:
            raise ValueError(f"No music version {version_id}")
        if len(versions) < 2:
            raise ValueError("This is the film's only song — generate another before deleting it.")
        data["versions"] = [v for v in versions if int(v["id"]) != version_id]
        (_hist_dir(work_dir) / match["file"]).unlink(missing_ok=True)
        if data.get("selected") == version_id:
            # Promote the newest survivor rather than leaving the film pointing
            # at a track nothing lists. Inlined instead of calling select():
            # the lock is not reentrant.
            fallback = data["versions"][-1]
            src = _hist_dir(work_dir) / fallback["file"]
            if src.exists():
                shutil.copy2(src, _canonical(work_dir))
            data["selected"] = int(fallback["id"])
        _save(work_dir, data)
    return history(work_dir)


def history(work_dir: Path) -> dict:
    """Return ``{"versions": [{"id", "path", "desc", "voice", "source_id"}],
    "selected": id|None}`` for API responses, dropping any version whose file
    has gone missing."""
    work_dir = Path(work_dir)
    data = _load(work_dir)
    hist = _hist_dir(work_dir)
    versions = []
    for v in data.get("versions", []):
        f = hist / v["file"]
        if f.exists():
            versions.append({"id": int(v["id"]), "path": str(f),
                             "desc": v.get("desc", ""), "voice": v.get("voice", ""),
                             "source_id": v.get("source_id")})
    selected = data.get("selected")
    valid = {v["id"] for v in versions}
    if selected not in valid:
        selected = versions[-1]["id"] if versions else None
    return {"versions": versions, "selected": selected}


def find(work_dir: Path, version_id: int) -> dict | None:
    """One kept version (with its on-disk ``path``), or None."""
    return next((v for v in history(work_dir)["versions"]
                 if v["id"] == int(version_id)), None)


def latest_sung(work_dir: Path) -> dict | None:
    """The newest version that came out of the music engine rather than a
    re-voicing — the track a "sing this as X" should convert FROM, so a second
    re-voicing clones the original vocals instead of the previous clone."""
    return next((v for v in reversed(history(work_dir)["versions"])
                 if not v.get("voice")), None)
