"""Version history for finished film video files."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


_MANIFEST = "final_video_history.json"
_SUBDIR = "final_video_history"


def _manifest_path(work_dir: str | Path) -> Path:
    return Path(work_dir) / _MANIFEST


def _history_dir(work_dir: str | Path) -> Path:
    return Path(work_dir) / _SUBDIR


def _final_path(work_dir: str | Path) -> Path:
    return Path(work_dir).with_suffix(".mp4")


def _load(work_dir: str | Path) -> dict[str, Any]:
    path = _manifest_path(work_dir)
    if not path.exists():
        return {"selected": None, "versions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"selected": None, "versions": []}
    if not isinstance(data, dict):
        return {"selected": None, "versions": []}
    versions = data.get("versions")
    if not isinstance(versions, list):
        versions = []
    return {"selected": data.get("selected"), "versions": versions}


def _save(work_dir: str | Path, data: dict[str, Any]) -> None:
    path = _manifest_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def seed_if_empty(
    work_dir: str | Path,
    video_path: str | Path | None = None,
    label: str = "Original",
    lang: str | None = None,
) -> dict[str, Any]:
    data = _load(work_dir)
    if data["versions"]:
        return history(work_dir)
    source = Path(video_path) if video_path else _final_path(work_dir)
    if not source.exists():
        return history(work_dir)
    return record(work_dir, source, label=label, lang=lang)


def record(
    work_dir: str | Path,
    video_path: str | Path,
    label: str = "",
    lang: str | None = None,
) -> dict[str, Any]:
    source = Path(video_path)
    if not source.exists():
        raise FileNotFoundError(str(source))

    data = _load(work_dir)
    versions = list(data.get("versions") or [])
    next_id = max([int(v.get("id", 0) or 0) for v in versions] + [0]) + 1
    out_dir = _history_dir(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest_name = f"final_video_v{next_id}.mp4"
    dest = out_dir / dest_name
    shutil.copy2(source, dest)

    entry = {
        "id": next_id,
        "file": str(Path(_SUBDIR) / dest_name),
        "label": label or f"Version {next_id}",
        "lang": lang,
    }
    versions.append(entry)
    data = {"selected": next_id, "versions": versions}
    _save(work_dir, data)
    return history(work_dir)


def select(
    work_dir: str | Path,
    version_id: int,
    final_path: str | Path | None = None,
) -> dict[str, Any]:
    data = _load(work_dir)
    for version in data.get("versions") or []:
        if int(version.get("id", 0) or 0) != int(version_id):
            continue
        source = Path(work_dir) / str(version.get("file", ""))
        if not source.exists():
            raise FileNotFoundError(str(source))
        dest = Path(final_path) if final_path else _final_path(work_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        data["selected"] = int(version_id)
        _save(work_dir, data)
        return history(work_dir)
    raise ValueError(f"Unknown final video version {version_id}")


def history(work_dir: str | Path) -> dict[str, Any]:
    data = _load(work_dir)
    versions = []
    for version in data.get("versions") or []:
        path = Path(work_dir) / str(version.get("file", ""))
        if not path.exists():
            continue
        versions.append(
            {
                "id": int(version.get("id", 0) or 0),
                "path": str(path),
                "label": version.get("label") or "Version",
                "lang": version.get("lang"),
            }
        )
    selected = data.get("selected")
    valid = {version["id"] for version in versions}
    if selected not in valid:
        selected = versions[-1]["id"] if versions else None
    return {"selected": selected, "versions": versions}
