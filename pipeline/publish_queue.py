"""Publish queue for the decoupled publishing scheduler.

A small JSON store (one entry per finished video) that lets publishing happen
on a controlled cadence, independent of when a render finishes. Each entry has
independent YouTube and X sub-states so a video can be released to each platform
on that platform's own channel/account schedule.

Mirrors the atomic-write helpers in ``youtube.py`` (load/save/add/update/remove).
The render queue (``youtube_queue.json``) stays about *rendering*; this store is
only about *publishing*.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "video-generator"
PUBLISH_QUEUE_PATH = _CONFIG_DIR / "publish_queue.json"
PUBLISH_CLOCK_PATH = _CONFIG_DIR / "publish_clock.json"

# Per-platform sub-state status values:
#   pending     — waiting for its channel/account cadence to allow a release
#   publishing  — release triggered; the async upload thread is running
#   done         — uploaded (video_id / tweet_id recorded)
#   skipped      — removed by the user (won't be re-added by a scan)
#   error        — the upload failed to even start (see "error" field)


def load_queue() -> list[dict]:
    try:
        return json.loads(PUBLISH_QUEUE_PATH.read_text())
    except Exception:
        return []


def save_queue(queue: list[dict]) -> None:
    PUBLISH_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUBLISH_QUEUE_PATH.write_text(json.dumps(queue, indent=2))


def item_by_work_dir(work_dir: str) -> dict | None:
    return next((e for e in load_queue() if e.get("work_dir") == str(work_dir)), None)


def add_item(work_dir: str, title: str, source: str = "manual",
             queue_item_id: str = "", youtube: dict | None = None,
             x: dict | None = None) -> dict:
    """Add a finished video to the publish queue. Returns the new entry, or {} if
    an entry for this work_dir already exists (a removed/skipped one counts — a
    re-scan must not resurrect something the user dropped)."""
    queue = load_queue()
    if any(e.get("work_dir") == str(work_dir) for e in queue):
        return {}
    now = time.time()
    entry = {
        "id": str(uuid.uuid4())[:8],
        "work_dir": str(work_dir),
        "title": title,
        "source": source,
        "queue_item_id": queue_item_id or "",
        "created_at": now,
        "updated_at": now,
        "youtube": youtube or {"enabled": False, "channel": "", "status": "skipped",
                               "video_id": None, "url": None,
                               "released_at": None, "published_at": None, "error": None},
        "x": x or {"enabled": False, "account": "", "status": "skipped",
                   "tweet_id": None, "url": None,
                   "released_at": None, "published_at": None, "error": None},
    }
    queue.append(entry)
    save_queue(queue)
    return entry


def update_item(item_id: str, **updates) -> bool:
    queue = load_queue()
    for entry in queue:
        if entry.get("id") == item_id:
            entry.update(updates)
            entry["updated_at"] = time.time()
            save_queue(queue)
            return True
    return False


def remove_item(item_id: str) -> bool:
    queue = load_queue()
    new_q = [e for e in queue if e.get("id") != item_id]
    if len(new_q) == len(queue):
        return False
    save_queue(new_q)
    return True


def _is_waiting(entry: dict) -> bool:
    """True while an entry still has a target waiting to publish — the set the
    manual order applies to (done/skipped entries are history)."""
    return ((entry.get("youtube") or {}).get("status") == "pending"
            or (entry.get("x") or {}).get("status") == "pending")


def move_item(item_id: str, direction: int) -> bool:
    """Move an entry up (direction=-1) or down (direction=1) among the entries
    still waiting to publish, so the manual publish order can be hand-tuned.
    Mirrors youtube.move_queue_item — the file order *is* the manual order."""
    queue = load_queue()
    waiting = [i for i, e in enumerate(queue) if _is_waiting(e)]
    try:
        item_idx = next(i for i, e in enumerate(queue) if e.get("id") == item_id)
    except StopIteration:
        return False
    if item_idx not in waiting:
        return False
    pos = waiting.index(item_idx)
    target = pos + direction
    if target < 0 or target >= len(waiting):
        return False
    other_idx = waiting[target]
    queue[item_idx], queue[other_idx] = queue[other_idx], queue[item_idx]
    save_queue(queue)
    return True


# ── Publishing clock resets ───────────────────────────────────────────────────
# The cadence has no stored anchor: each channel/account's next-eligible time is
# derived from its newest release in the queue. A reset re-anchors that clock —
# releases made at or before ``set_at`` stop counting, and the next release is
# allowed at ``next_at`` (later ones space from whenever it actually goes out).
# A record goes inert on its own once a release lands after ``set_at``; setting
# the same time on a YouTube channel and an X account syncs the two platforms.

def load_clock() -> dict:
    """``"<platform>:<key>"`` → ``{"set_at", "next_at"}`` reset records."""
    try:
        data = json.loads(PUBLISH_CLOCK_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def reset_clock(platform: str, key: str, next_at: float) -> dict:
    """Record a clock reset for a channel/account. Overwrites any earlier reset
    for the same key. Returns the full clock map."""
    clock = load_clock()
    clock[f"{platform}:{key}"] = {"set_at": time.time(), "next_at": float(next_at)}
    PUBLISH_CLOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUBLISH_CLOCK_PATH.write_text(json.dumps(clock, indent=2))
    return clock
