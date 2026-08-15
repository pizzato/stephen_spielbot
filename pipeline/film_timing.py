"""Learned durations for post-render film-edit jobs.

The main render learns how long each task kind takes and predicts an ETA
(see ``pipeline/timing.py`` + ``DurableStore.timing_table``).  The film-edit
operations — scene re-renders, the parallel scene upscale, narrator/music
regeneration — run as in-process daemon threads rather than orchestrator tasks,
so they get their own tiny version of the same idea here: every finished job
feeds a running average keyed by operation (and output resolution, when that
drives the time); the next run of that operation predicts its wall-clock from
the learned average.

Until an op has been measured a coarse seed is used and the estimate is flagged
``rough`` — the same "measure once, then predict" behaviour the render uses.

Storage is one small JSON file; the concurrent upscale worker threads serialise
on a lock.  Never raises — timing is best-effort bookkeeping.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_PATH = Path.home() / ".local" / "share" / "video-generator" / "film_job_timings.json"
_lock = threading.RLock()
_cache: dict | None = None

# Coarse seed seconds per op, used only until a real sample exists. Resolution-
# sensitive ones are scaled from these FHD-ish baselines by _res_scale().
_DEFAULTS = {
    "rerender_video": 240.0,
    "rerender_image": 40.0,
    "rerender_narration": 12.0,
    "narrator_scene": 12.0,
    "music_regen": 90.0,
    "song_revoice": 180.0,
    "upscale_scene": 300.0,
}
# Ops whose duration scales with the output pixel count.
_RES_SENSITIVE = frozenset({"rerender_video", "rerender_image", "upscale_scene"})
_FHD_PIXELS = 1920 * 1080

# Ignore obviously-bogus samples: sub-second completions are skips/no-ops and
# multi-hour ones are almost certainly clock skew or a hung job.
_MIN_SECONDS = 0.75
_MAX_SECONDS = 6 * 3600.0


def _signature(op: str, width: int | None, height: int | None) -> str:
    if op in _RES_SENSITIVE and width and height:
        return f"{op}|{int(width)}x{int(height)}"
    return op


def _res_scale(width: int | None, height: int | None) -> float:
    if not (width and height):
        return 1.0
    return max(0.15, min((int(width) * int(height)) / _FHD_PIXELS, 4.0))


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    data: dict = {}
    try:
        if _PATH.exists():
            raw = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {k: v for k, v in raw.items() if isinstance(v, dict)}
    except Exception:
        data = {}
    _cache = data
    return _cache


def _persist_locked() -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache or {}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PATH)
    except Exception:
        pass


def record(op: str, seconds: float, *, width: int | None = None, height: int | None = None) -> None:
    """Feed one finished-job duration into the learned average for *op*.

    Uses a running mean that behaves as a true average for the first few samples
    then settles into a light EMA, so it tracks the current cluster's hardware —
    the same shape as ``DurableStore._record_timing_sample``."""
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        return
    if not (_MIN_SECONDS <= secs <= _MAX_SECONDS):
        return
    sig = _signature(op, width, height)
    with _lock:
        table = _load()
        entry = table.get(sig)
        if not entry:
            table[sig] = {
                "sample_count": 1,
                "avg_seconds": secs,
                "last_seconds": secs,
                "updated_at": time.time(),
            }
        else:
            count = int(entry.get("sample_count", 0))
            alpha = max(1.0 / (count + 1), 0.25)
            avg = (1.0 - alpha) * float(entry.get("avg_seconds", secs)) + alpha * secs
            entry.update(
                sample_count=count + 1,
                avg_seconds=avg,
                last_seconds=secs,
                updated_at=time.time(),
            )
        _persist_locked()


def estimate(op: str, *, width: int | None = None, height: int | None = None) -> tuple[float, bool]:
    """``(seconds, learned?)`` for one run of *op*.

    ``learned=False`` means the value is a coarse seed (no sample yet) — callers
    surface that as a rough ETA, exactly like the render's ``confidence`` flag."""
    sig = _signature(op, width, height)
    with _lock:
        entry = _load().get(sig)
    if entry and int(entry.get("sample_count", 0)) >= 1:
        return float(entry["avg_seconds"]), True
    base = _DEFAULTS.get(op, 60.0)
    if op in _RES_SENSITIVE:
        base *= _res_scale(width, height)
    return base, False
