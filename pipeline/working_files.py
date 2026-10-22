"""Remove working video media while retaining the final and reusable assets."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from pipeline import final_video_history, image_history, music_history, video_history


def state(work_dir: Path) -> dict:
    path = work_dir / "working_files.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def is_cleaned(work_dir: Path) -> bool:
    try:
        return bool(state(work_dir).get("video_sources_removed"))
    except (ValueError, OSError):
        return False  # A damaged record must not break the whole Films list.


def preserve_scene_audio(work_dir: Path) -> None:
    """Acted takes may carry speech only inside the video: keep it separately."""
    from pipeline.assembler import _has_audio_stream, extract_audio

    for folder in [work_dir, *(work_dir / "localize").glob("*")]:
        for video in folder.glob("scene_*_final.mp4"):
            if not _local_file(work_dir, video):
                continue
            stem = video.stem.removesuffix("_final")
            if _local_file(work_dir, folder / f"{stem}_narration.wav"):
                continue
            audio = folder / f"{stem}_original_audio.wav"
            if audio.is_symlink():
                raise ValueError(f"Cannot preserve scene audio through a symbolic link: {audio.name}")
            if audio.is_file() and audio.stat().st_size:
                continue
            if _has_audio_stream(video):
                with tempfile.NamedTemporaryFile(suffix=".wav", dir=folder, delete=False) as tmp:
                    staged = Path(tmp.name)
                try:
                    extract_audio(video, staged)
                    if not staged.stat().st_size:
                        raise ValueError(f"No audio extracted from {video.name}")
                    os.replace(staged, audio)
                finally:
                    staged.unlink(missing_ok=True)


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

    # Audio histories are deliberately retained, including alternate songs and
    # source vocals. They are reusable originals, not disposable render caches.
    for name, grouped in (("image_history", True), ("video_history", True),
                          ("final_video_history", False)):
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
            keep = {selected} if name == "image_history" else set()
            for version in versions:
                path = base / version["file"]
                # A manifest cannot nominate arbitrary files for deletion.
                suffixes = {".png", ".jpg", ".jpeg", ".webp"} if name == "image_history" else {".mp4"}
                if (path.parent != wd / name or version["id"] in keep
                        or path.suffix.lower() not in suffixes):
                    continue
                add(path, "Older images" if name == "image_history" else "Video versions")
                if name == "image_history" and path.name.startswith("cover_v"):
                    add(path.with_stem(path.stem + "_bg"), "Older images")
        manifests.append((manifest, data, entries, base))

    # Only pipeline-owned locations/names, never an arbitrary recursive MP4
    # sweep: imported reference videos and unknown assets must survive.
    for folder in [wd, *(wd / "localize").glob("*")]:
        for path in folder.glob("scene_*.mp4"):
            add(path, "Scene video clips")
        add(folder / "combined.mp4", "Intermediate assembled videos")
    for name in ("final_localize.staging.mp4", "final_upscale.staging.mp4", "finish_upscale.staging.mp4"):
        add(wd / name, "Intermediate assembled videos")

    for name in ("final_upscale_scenes", "finish_upscale_scenes"):
        folder = wd / name
        if folder.is_symlink():
            continue
        for path in folder.rglob("*.mp4"):
            if path.name.startswith("scene_") or path.name == "combined.upscaled.mp4":
                add(path, "Upscale caches")
    return candidates, manifests


def _summary(work_dir: Path, candidates: dict) -> dict:
    groups = {}
    for path, group in candidates.items():
        item = groups.setdefault(group, {"label": group, "files": 0, "bytes": 0, "items": []})
        item["files"] += 1
        item["bytes"] += path.stat().st_size
        item["items"].append({"name": os.path.relpath(path, work_dir),
                              "path": str(path), "bytes": path.stat().st_size})
    return {"files": sum(g["files"] for g in groups.values()),
            "bytes": sum(g["bytes"] for g in groups.values()),
            "groups": list(groups.values())}


def preview(work_dir: Path, final: Path) -> dict:
    with _history_locks():
        candidates, _ = _plan(work_dir, final)
        kept = {path: "Other retained files" for path in work_dir.rglob("*")
                if _local_file(work_dir, path) and path not in candidates}
        for path in kept:
            if path.suffix.lower() in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}:
                kept[path] = "Music, narration and other audio"
            elif path.suffix.lower() in {".srt", ".vtt"}:
                kept[path] = "Closed captions"
            elif path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                kept[path] = "Current images, covers and references"
            elif path.suffix.lower() in {".json", ".txt", ".yaml"}:
                kept[path] = "Scripts and metadata"
        if final.is_file():
            kept[final] = "Final video"
        return {**_summary(work_dir, candidates), "kept": _summary(work_dir, kept)}


def clean(work_dir: Path, final: Path) -> dict:
    """Rescan under the history writers' locks; only unlink explicit candidates."""
    with _history_locks():
        candidates, manifests = _plan(work_dir, final)
        deleted, errors, freed, removed = set(), [], 0, []
        for path, group in candidates.items():
            try:
                size = path.stat().st_size
                path.unlink()
                deleted.add(path)
                freed += size
                removed.append({"name": str(path.relative_to(work_dir)), "bytes": size, "group": group})
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
        return {"files": len(deleted), "bytes": freed, "errors": errors, "removed": removed}
