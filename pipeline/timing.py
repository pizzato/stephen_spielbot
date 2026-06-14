"""Render-ETA prediction from learned per-task durations.

The orchestrator records how long each task kind takes (see
``DurableStore.timing_table``).  This module turns that learned table, plus a
job's planned tasks and the configured worker counts, into a wall-clock ETA.

Model (deliberately simple — see CLAUDE.md §2):
  * Heavy generation runs on two parallel pools — ComfyUI (image/video/music)
    and TTS (narration) — so each pool's remaining work is divided by its
    worker count.
  * Muxing and final assembly run locally; muxes overlap with ongoing ComfyUI
    work, so only the final assembly plus one trailing mux are added as a tail.
  * ETA ≈ max(comfy_wall, tts_wall) + last_mux + finalize.

Until a kind has been measured, a coarse built-in default is used and the
estimate is flagged ``rough``; after the first render the learned table takes
over — exactly the "build it once, then predict" behaviour requested.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pipeline.orchestrator import (
    TIMING_KIND_LABELS,
    json_loads,
    timing_signature,
)

# Coarse seed durations (seconds) per label, at FHD for resolution-sensitive
# kinds.  Only used until real samples exist; intentionally rough.
_DEFAULT_SECONDS = {
    "image": 40.0,
    "video": 240.0,
    "narration": 12.0,
    "music": 60.0,
    "mux": 8.0,
    "finalize": 25.0,
}
_FHD_PIXELS = 1920 * 1080
_RES_SENSITIVE = ("image", "video", "finalize")

# Which worker pool each label competes for.
_POOL = {
    "image": "comfy",
    "video": "comfy",
    "music": "comfy",
    "narration": "tts",
    "mux": "local",
    "finalize": "local",
}

_DONE_STATUSES = {"succeeded", "cancelled"}
_RUNNING_STATUSES = {"running", "leased"}


def _resolution_scale(payload: dict[str, Any]) -> float:
    """How a payload's resolution scales the FHD default (clamped)."""
    w, h = payload.get("vid_width"), payload.get("vid_height")
    if not (w and h):
        return 1.0
    return max(0.15, min((int(w) * int(h)) / _FHD_PIXELS, 1.5))


def _default_for(label: str, payload: dict[str, Any]) -> float:
    base = _DEFAULT_SECONDS.get(label, 20.0)
    if label in _RES_SENSITIVE:
        base *= _resolution_scale(payload)
    return base


def _estimate_one(table: dict, kind: str, payload: dict[str, Any]) -> tuple[float, bool]:
    """(seconds, learned?) for a single task — learned=False means a seed default."""
    label = TIMING_KIND_LABELS.get(kind)
    if label is None:
        return 0.0, True  # untracked (e.g. story.ready) — contributes no time
    sig = timing_signature(kind, payload)
    entry = table.get(sig) if sig else None
    if entry and entry.get("sample_count", 0) >= 1:
        return float(entry["avg_seconds"]), True
    return _default_for(label, payload), False


def _friendly_label(kind: str, payload: dict[str, Any]) -> str:
    label = TIMING_KIND_LABELS.get(kind, kind)
    if label in _RES_SENSITIVE:
        w, h = payload.get("vid_width"), payload.get("vid_height")
        if w and h:
            return f"{label} @ {int(w)}×{int(h)}"
    return label


def humanize_eta(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 45:
        return "under a minute"
    minutes = s / 60.0
    if minutes < 60:
        return f"~{int(round(minutes))} min"
    hours = int(minutes // 60)
    rem = int(round(minutes % 60))
    return f"~{hours}h {rem}m" if rem else f"~{hours}h"


# The standard task plan ensure_generation_plan() creates for a job (see
# DurableStore.ensure_generation_plan) — used to predict a render that hasn't
# been planned yet, e.g. a waiting queue item.
_PLAN_PER_SCENE = (
    "scene.image.generate",
    "scene.narration.generate",
    "scene.video.generate",
    "scene.video.mux",
)
_PLAN_PER_JOB = ("music.generate", "video.finalize")


def estimate_planned_job(
    n_scenes: int,
    vid_width: int,
    vid_height: int,
    table: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """Predict wall-clock for a job that has no orchestrator tasks yet.

    Synthesizes the standard plan (per scene: image, narration, video, mux;
    plus one music and one finalize) at the given output resolution and runs
    it through :func:`estimate_eta`."""
    payload_json = json.dumps({"vid_width": vid_width, "vid_height": vid_height})
    tasks = [
        {"kind": kind, "status": "queued", "payload_json": payload_json}
        for kind in _PLAN_PER_SCENE
        for _ in range(max(1, int(n_scenes)))
    ]
    tasks += [
        {"kind": kind, "status": "queued", "payload_json": payload_json}
        for kind in _PLAN_PER_JOB
    ]
    return estimate_eta(tasks, table, cfg)


def next_worker_free_seconds(
    tasks: list[dict[str, Any]],
    table: dict[str, dict[str, Any]],
    now: float | None = None,
) -> float | None:
    """Seconds until the next ComfyUI worker finishes its current task.

    Used to tell the UI "a worker will be available in ~X" (issue #98) when every
    render worker is busy. Looks only at running comfy-pool tasks (image/video/
    music), takes each one's learned remaining time, and returns the soonest.
    None when nothing comfy-pool is running (no basis to estimate)."""
    now = now if now is not None else time.time()
    remaining: list[float] = []
    for t in tasks:
        kind = t.get("kind", "")
        label = TIMING_KIND_LABELS.get(kind)
        if _POOL.get(label) != "comfy":
            continue
        if t.get("status") in _RUNNING_STATUSES and t.get("started_at"):
            est, _ = _estimate_one(table, kind, json_loads(t.get("payload_json")))
            remaining.append(max(0.0, est - (now - float(t["started_at"]))))
    if not remaining:
        return None
    return round(min(remaining))


def estimate_eta(
    tasks: list[dict[str, Any]],
    table: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
    now: float | None = None,
    reserved_comfy: int = 0,
) -> dict[str, Any] | None:
    """Predict render wall-clock from planned tasks + learned table + workers.

    ``tasks`` is the list of task dicts from the orchestrator (each carries
    ``kind``, ``status``, ``started_at`` and ``payload_json``).  Returns a dict
    ready to drop into the /api/progress payload, or ``None`` when there is
    nothing trackable to estimate.

    ``reserved_comfy`` is how many ComfyUI workers are currently held out of the
    render — e.g. the one kept idle for the web UI while it is in use (issue #98).
    They are subtracted from the comfy pool so the ETA reflects only the workers
    actually rendering; since /api/progress recomputes this each poll, the ETA
    tracks the count as the UI goes active/idle mid-render. The last worker is
    never reservable, mirroring WorkerPool.
    """
    now = now if now is not None else time.time()
    n_comfy_total = max(1, len(cfg.get("comfy_workers") or []))
    n_tts = max(1, len(cfg.get("tts_workers") or []))
    reserved = max(0, min(int(reserved_comfy), n_comfy_total - 1))
    n_comfy = n_comfy_total - reserved

    # full = whole render (stable estimate); rem = work still outstanding.
    full = {"comfy": 0.0, "tts": 0.0}
    rem = {"comfy": 0.0, "tts": 0.0}
    full_mux = full_finalize = 0.0
    rem_last_mux = rem_finalize = 0.0
    by_label: dict[str, dict[str, Any]] = {}
    any_default = False
    tracked = 0

    for t in tasks:
        kind = t.get("kind", "")
        label = TIMING_KIND_LABELS.get(kind)
        if label is None:
            continue
        tracked += 1
        payload = json_loads(t.get("payload_json"))
        est, learned = _estimate_one(table, kind, payload)
        if not learned:
            any_default = True

        status = t.get("status", "")
        if status in _DONE_STATUSES:
            rem_one = 0.0
        elif status in _RUNNING_STATUSES and t.get("started_at"):
            # part-way through: only the un-elapsed remainder counts (floor it
            # so a long-running task never reports exactly 0 and "vanishes").
            rem_one = max(3.0, est - (now - float(t["started_at"])))
        else:
            rem_one = est

        pool = _POOL.get(label, "local")
        if pool in ("comfy", "tts"):
            full[pool] += est
            rem[pool] += rem_one
        elif label == "mux":
            full_mux = max(full_mux, est)
            rem_last_mux = max(rem_last_mux, rem_one)
        elif label == "finalize":
            full_finalize = est
            rem_finalize = rem_one

        # breakdown ("the table"): one row per friendly label. Tasks sharing a
        # label share a signature, hence the same per-task estimate.
        fl = _friendly_label(kind, payload)
        row = by_label.setdefault(fl, {"label": fl, "seconds": est, "count": 0, "estimated": False})
        row["count"] += 1
        row["estimated"] = row["estimated"] or (not learned)

    if tracked == 0:
        return None

    def _wall(d: dict[str, float]) -> float:
        return max(d["comfy"] / n_comfy, d["tts"] / n_tts)

    total_seconds = _wall(full) + full_mux + full_finalize
    eta_seconds = _wall(rem) + rem_last_mux + rem_finalize

    table_rows = sorted(
        by_label.values(), key=lambda r: r["seconds"] * r["count"], reverse=True
    )
    for r in table_rows:
        r["seconds"] = round(r["seconds"])

    return {
        "eta_seconds": round(eta_seconds),
        "eta_text": humanize_eta(eta_seconds),
        "total_seconds": round(total_seconds),
        "total_text": humanize_eta(total_seconds),
        "confidence": "rough" if any_default else "learned",
        "workers": {"comfy": n_comfy, "tts": n_tts, "comfy_reserved": reserved},
        "table": table_rows,
    }
