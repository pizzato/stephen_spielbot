"""Where an acted take can be picked up again.

H3 can only continue a take it still holds the motion context for, and that
context is a latent file in the WORKER's ComfyUI output folder — not in the film
folder, and not on any other worker. So each acted render drops a note next to
the scene saying where its continuation point is:

    scene_03_h3_context.json → {latent, token, clip_index, comfy_url, …}

The film editor's Continue reads that note days later, queues the next clip on
the same worker, and rewrites the note so the take can be continued again.

The note is only good for the take it was written for. Selecting an older take,
or trimming this one, leaves the film holding a clip the saved latent does not
end on — continuing that would splice one take onto another. ``final_bytes``
catches exactly that: it is the size of the scene's final when the note was
written, and a mismatch means the clip on screen is no longer the one the
context belongs to.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger("video_gen")


def path(work_dir: Path, scene_id: int) -> Path:
    return Path(work_dir) / f"scene_{int(scene_id):02d}_h3_context.json"


def token_prefix(work_dir: Path, scene_id: int) -> str:
    """The worker-side prefix this scene's context latents are saved under.

    Stable per scene (the save node's fixed slots overwrite, so re-shoots and
    gate retakes replace their own file instead of piling up on the worker) and
    unique per scene (two scenes chaining at once must not share a slot). It
    names the same h3_context/ folder a chained render uses.
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(work_dir).name)[:48]
    return f"h3_context/{slug}_s{int(scene_id):02d}"


def load(work_dir: Path, scene_id: int) -> dict | None:
    try:
        data = json.loads(path(work_dir, scene_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("latent") else None


def save(work_dir: Path, scene_id: int, **fields) -> dict:
    """Merge *fields* into the scene's note (best effort — a film that cannot be
    continued is worse than a film that fails to render, never the other way)."""
    data = {**(load(work_dir, scene_id) or {}), **fields, "saved_at": time.time()}
    try:
        path(work_dir, scene_id).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Scene %s: could not save the continuation point (%s)", scene_id, exc)
    return data


def stamp_final(work_dir: Path, scene_id: int, final: Path) -> None:
    """Bind the note to the clip that is actually in the cut."""
    if not load(work_dir, scene_id):
        return          # nothing saved this render — nothing to bind
    try:
        size = Path(final).stat().st_size
    except OSError:
        return
    save(work_dir, scene_id, final_bytes=size)


def continuable(work_dir: Path, scene_id: int, final: Path) -> bool:
    """True if *final* is still the take the saved context ends on."""
    data = load(work_dir, scene_id)
    if not data or not data.get("final_bytes"):
        return False
    try:
        return Path(final).stat().st_size == int(data["final_bytes"])
    except OSError:
        return False
