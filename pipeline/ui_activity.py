"""Shared UI-activity signal (issue #98).

Replaces the dedicated "ui worker" with a dynamic reservation: while the web UI
is being actively used, the render's WorkerPool holds one ComfyUI worker idle so
cover/preview jobs land on a free GPU instead of queueing behind a render. After
the UI has been idle for ``ui_idle_timeout_seconds`` the held worker rejoins the
render pool.

The signal is a single timestamp file so it crosses the process boundary: the
web backend (which serves the UI) is the writer; the render subprocess
(resume_generation.py, via WorkerPool) is the reader. Kept dependency-free so the
render subprocess can import it without pulling in the web app.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Same runtime state dir the orchestrator DB lives in (kept literal to avoid a
# heavy import in the render subprocess).
APP_STATE_DIR = Path.home() / ".local" / "share" / "video-generator"
ACTIVITY_FILE = APP_STATE_DIR / "ui_activity.json"

# Default idle window before a reserved worker returns to the render pool.
DEFAULT_IDLE_TIMEOUT = 300.0  # 5 minutes


def mark_active(now: float | None = None) -> None:
    """Stamp 'the UI was just used'. Cheap; called on heartbeats and on every
    cover/preview request. Best-effort — a write failure must never break the UI."""
    ts = time.time() if now is None else now
    try:
        ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so the render subprocess never reads a torn file.
        tmp = ACTIVITY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"last_activity": ts}))
        os.replace(tmp, ACTIVITY_FILE)
    except Exception:
        pass


def last_activity() -> float:
    """Epoch seconds of the last UI interaction, or 0.0 if never/unreadable."""
    try:
        return float(json.loads(ACTIVITY_FILE.read_text()).get("last_activity") or 0.0)
    except Exception:
        return 0.0


def idle_seconds(now: float | None = None) -> float:
    """Seconds since the UI was last used (large if never)."""
    return (time.time() if now is None else now) - last_activity()


def is_active(timeout_seconds: float = DEFAULT_IDLE_TIMEOUT, now: float | None = None) -> bool:
    """True if the UI was used within the idle window — i.e. a worker should be
    reserved for it."""
    return idle_seconds(now) < max(0.0, float(timeout_seconds))


def seconds_until_idle(timeout_seconds: float = DEFAULT_IDLE_TIMEOUT, now: float | None = None) -> float:
    """Seconds until the reservation would lapse (0 if already idle)."""
    return max(0.0, float(timeout_seconds) - idle_seconds(now))
