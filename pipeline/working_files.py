"""Conservative, per-film cleanup of superseded versions and upscale caches.

Canonical scene media, audio, captions, metadata and the published final are
never candidates. Unknown files and symlinks are left alone.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from pipeline import final_video_history, image_history, music_history, video_history


@contextmanager
def _history_locks():
    with ExitStack() as stack:
        for module in (image_history, video_history, music_history, final_video_history):
            stack.enter_context(module._LOCK)
        yield


def _local_file(wd: Path, path: Path) -> bool:
    """Do not follow links, even links pointing elsewhere inside the film."""
    try:
        relative = path.relative_to(wd)
        return (".." not in relative.parts and path.is_file()
                and not any((wd / Path(*relative.parts[:n])).is_symlink()
                            for n in range(len(relative.parts) + 1)))
    except (ValueError, OSError):
        return False


def _plan(wd: Path, final: Path) -> tuple[dict, list]:
    candidates = {}
    manifests = []
    protected = final.resolve()

    def add(path: Path, group: str):
        if _local_file(wd, path) and path.resolve() != protected:
            candidates[path] = group

    for name, grouped in (("image_history", True), ("video_history", True),
                          ("music_history", False), ("final_video_history", False)):
        manifest = wd / f"{name}.json"
        if not _local_file(wd, manifest):
            continue
        # A damaged manifest must not turn into permission to delete its media.
        data = json.loads(manifest.read_text())
        entries = list(data.values()) if grouped else [data]
        base = wd if name == "final_video_history" else wd / name
        for entry in entries:
            versions = entry.get("versions", [])
            existing = [v for v in versions if _local_file(wd, base / v["file"])]
            ids = {v["id"] for v in existing}
            selected = entry.get("selected")
            if selected not in ids:
                selected = existing[-1]["id"] if existing else None
            keep = {selected}
            if name == "final_video_history":
                # Factor upscales size themselves from the latest base cut;
                # deleting it would make a repeat 2x upscale compound to 4x.
                base_cut = next((v for v in reversed(existing)
                                 if final_video_history.is_base(v)), None)
                if base_cut:
                    keep.add(base_cut["id"])
            # Re-voicing starts from the original sung track, not another clone.
            if name == "music_history":
                by_id = {v["id"]: v for v in versions}
                parent = by_id.get(selected, {}).get("source_id")
                while parent and parent not in keep:
                    keep.add(parent)
                    parent = by_id.get(parent, {}).get("source_id")
            for version in versions:
                path = base / version["file"]
                # A manifest cannot nominate arbitrary files for deletion.
                if path.parent != wd / name or version["id"] in keep:
                    continue
                add(path, "Older versions")
                if name == "image_history" and path.name.startswith("cover_v"):
                    add(path.with_stem(path.stem + "_bg"), "Older versions")
        manifests.append((manifest, data, entries, base))

    for name in ("final_upscale_scenes", "finish_upscale_scenes"):
        folder = wd / name
        if folder.is_symlink():
            continue
        for path in folder.rglob("*.mp4"):
            if path.name.startswith("scene_"):
                add(path, "Upscale caches")
    return candidates, manifests


def _summary(candidates: dict) -> dict:
    groups = {}
    for path, group in candidates.items():
        item = groups.setdefault(group, {"label": group, "files": 0, "bytes": 0})
        item["files"] += 1
        item["bytes"] += path.stat().st_size
    return {"files": sum(g["files"] for g in groups.values()),
            "bytes": sum(g["bytes"] for g in groups.values()),
            "groups": list(groups.values())}


def preview(work_dir: Path, final: Path) -> dict:
    with _history_locks():
        candidates, _ = _plan(work_dir, final)
        return _summary(candidates)


def clean(work_dir: Path, final: Path) -> dict:
    """Rescan under the history writers' locks; only unlink explicit candidates."""
    with _history_locks():
        candidates, manifests = _plan(work_dir, final)
        deleted, errors, freed = set(), [], 0
        for path in candidates:
            try:
                size = path.stat().st_size
                path.unlink()
                deleted.add(path)
                freed += size
            except OSError as exc:
                errors.append(f"{path.relative_to(work_dir)}: {exc.strerror}")
        for manifest, data, entries, base in manifests:
            changed = False
            for entry in entries:
                versions = entry.get("versions", [])
                kept = [v for v in versions if base / v["file"] not in deleted]
                if len(kept) != len(versions):
                    # Preserve next_id and selected metadata for future edits.
                    entry["versions"] = kept
                    changed = True
            if changed:
                with tempfile.NamedTemporaryFile(mode="w", dir=work_dir, delete=False) as tmp:
                    json.dump(data, tmp, indent=2)
                os.replace(tmp.name, manifest)
        return {"files": len(deleted), "bytes": freed, "errors": errors}
