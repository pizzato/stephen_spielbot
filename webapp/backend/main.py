#!/usr/bin/env python3
"""FastAPI backend for the Stephen Spielbot web UI — the only interface.

This is a thin REST/JSON layer over the EXISTING pipeline. It imports the
``app`` module to reuse its helper functions (config, work-dir bookkeeping,
job launching, progress polling) plus the ``pipeline`` package directly.
``app.py`` is a helper library (the former Gradio UI has been removed).

Run it from the repo root:

    uvicorn webapp.backend.main:app --port 8001 --reload
"""

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager, contextmanager, nullcontext
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

# Make the repo root importable so `import app` and `import pipeline.*` resolve
# the same way they do when app.py runs directly.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Reuse the original app's pure helpers. Importing app.py only runs module-level
# setup (logging, dir creation, constants) — build_ui()/demo.launch() are guarded
# behind `if __name__ == "__main__"`, so nothing UI-related starts here.
import app as gapp  # noqa: E402
import pipeline.youtube as yt  # noqa: E402
import pipeline.x as xt  # noqa: E402
import pipeline.publish_queue as pq  # noqa: E402
import pipeline.llm as llm  # noqa: E402
import pipeline.engagement as eng  # noqa: E402
import pipeline.c2pa as _c2pa  # noqa: E402
import pipeline.prompts as _prompts  # noqa: E402
from pipeline.llm import generate_video_suggestions, Scene  # noqa: E402
import pipeline.story as story_mode  # noqa: E402
import pipeline.performance as performance_mode  # noqa: E402

# A music video's AUTO split: takes of about this long. Short takes keep a
# performance tight against the beat, and the song's length divided by this is
# the scene count the Song tab shows (an explicit count overrides it).
SONG_SCENE_SECONDS = 5.0
from pipeline.orchestrator import DurableStore, job_id_from_work_dir, task_id as make_task_id, worker_id  # noqa: E402
from pipeline.timing import estimate_eta, estimate_planned_job, humanize_eta, next_worker_free_seconds  # noqa: E402
from pipeline import cadence  # noqa: E402
from pipeline import continuity  # noqa: E402
from pipeline import ui_activity  # noqa: E402
from pipeline import film_timing  # noqa: E402
from pipeline import image_history  # noqa: E402
from pipeline import video_history  # noqa: E402
from pipeline import music_history  # noqa: E402
from pipeline import final_video_history  # noqa: E402
from pipeline import scene_context  # noqa: E402
from pipeline.cover import (  # noqa: E402
    COVER_PHRASE_FILE,
    COVER_PHRASE_MAX_CHARS,
    cover_dimensions,
    cover_phrase_for,
    default_cover_phrase,
)
from pipeline import title_cards as _title_cards  # noqa: E402
from pipeline.cover_typography import (  # noqa: E402
    COVER_BASE_NAME,
    apply_cover_typography,
    bundled_fonts,
    mark_accent,
    preview_background,
    render_cover_typography,
)

@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    # SPIELBOT_NO_BACKGROUND=1 runs a UI/API-only instance: no automation tick,
    # no publish scheduler, no re-render requeue. For test/preview servers run
    # NEXT TO the real service — two automation engines over the same queue
    # caused duplicate renders and double uploads.
    if os.environ.get("SPIELBOT_NO_BACKGROUND"):
        gapp.logger.warning("SPIELBOT_NO_BACKGROUND set — background loops disabled")
        yield
        return
    # Startup: launch the opt-in background automation loop (defined near the
    # bottom of this module; the name resolves at startup, not import). Replaces
    # the deprecated @app.on_event("startup") handler.
    _start_automation_loop()
    # Requeue scene re-renders a restart killed mid-flight (in-process threads,
    # unlike the full render's subprocess). Off-thread: it probes sqlite + disk.
    threading.Thread(target=_resume_interrupted_rerenders, daemon=True).start()
    yield
    # Shutdown: nothing to clean up — the loop runs in a daemon thread.


api = FastAPI(title="Stephen Spielbot API", lifespan=_lifespan)
# `uvicorn webapp.backend.main:app` is the conventional entry point — expose the
# instance under both names so either `:app` or `:api` works.
app = api

# Where the built frontend lives (after `npm run build`). Optional in dev — the
# Vite dev server proxies /api to this process instead.
FRONTEND_DIST = REPO_ROOT / "webapp" / "frontend" / "dist"


# ── helpers ──────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    """sqlite3.Row → plain dict (JSON-serialisable)."""
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


def _busted_file_url(p: Path) -> str:
    """/api/file URL with the file's mtime as a cache-buster, so a rewritten
    final (reassemble, remix) gets a fresh URL and the player refetches it
    instead of replaying the browser's cached copy of the old cut."""
    try:
        return f"/api/file?path={p}&t={int(p.stat().st_mtime)}"
    except OSError:
        return f"/api/file?path={p}"


_WORKER_RUNNING_STATUSES = ("running", "leased")


def _worker_activity(cfg: dict, tasks: list[dict], reserved_comfy: int = 0) -> list[dict]:
    """Live view of the configured render fleet: what each ComfyUI/TTS worker is
    doing right now.

    Sourced from the configured pools (not the append-only durable registry, which
    accumulates stale endpoints and the cover agent's kind="ui" rows — issue #98
    removed the dedicated ui_workers pool). Each in-flight task records the worker
    it leased via ``lease_owner`` (= ``worker_id(kind, endpoint)``), so we map that
    back to an endpoint to show the actual job ("Scene 3 video") instead of a flat
    "online" badge. One idle ComfyUI worker is flagged ``reserved`` while it is held
    for the UI — its reservation lives in the render subprocess's WorkerPool, not
    the store, but during a running render the lone idle comfy worker is that one.
    """
    ep_by_wid: dict[str, str] = {}
    fleet: list[tuple[str, str]] = []
    for ep in cfg.get("comfy_workers") or []:
        ep_by_wid[worker_id("comfy", ep)] = ep
        fleet.append(("comfy", ep))
    for ep in cfg.get("tts_workers") or []:
        ep_by_wid[worker_id("tts", ep)] = ep
        fleet.append(("tts", ep))
    job_by_ep: dict[str, str] = {}
    for t in tasks:
        if t.get("status") in _WORKER_RUNNING_STATUSES:
            ep = ep_by_wid.get(t.get("lease_owner") or "")
            if ep:
                job_by_ep[ep] = t.get("name") or ""

    workers = [
        {"kind": kind, "endpoint": ep, "job": job_by_ep.get(ep, ""),
         "state": "working" if job_by_ep.get(ep) else "idle"}
        for kind, ep in fleet
    ]

    # Flag one idle comfy worker as held for the UI, but only while a render is
    # actually using the others — otherwise an all-idle fleet would mislabel one.
    if reserved_comfy > 0 and any(w["kind"] == "comfy" and w["state"] == "working" for w in workers):
        for w in workers:
            if w["kind"] == "comfy" and w["state"] == "idle":
                w["state"] = "reserved"
                break
    return workers


def _safe_under(path: Path, *roots: Path) -> bool:
    """True if `path` resolves to something inside one of `roots`."""
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            rp.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


# (acted-silent flag, chaining, style note) per work dir, held for a moment so a
# forty-scene payload reads the config once rather than forty times.
_ACTED_CTX: dict[str, tuple[float, dict]] = {}


def _acted_scene_ctx(wd: Path) -> dict:
    """What a work dir's scenes need to assemble their H3 prompt."""
    key = str(wd)
    hit = _ACTED_CTX.get(key)
    if hit and time.monotonic() - hit[0] < 2.0:
        return hit[1]
    job_id = job_id_from_work_dir(wd)
    jc = dict(_film_job_config(wd))
    if not jc.get("style_name"):
        jc["style_name"] = _job_style_name(job_id)
    ss = gapp.style_settings(gapp.load_config(), jc["style_name"])
    ctx = {"acted_cfg": _acted_silent_cfg(jc),
           "chained": bool(jc.get("h3_chain_scenes") if jc.get("h3_chain_scenes") is not None
                           else ss.get("h3_chain_scenes")),
           # Style paints a first frame for EVERY acted scene (h3_first_frames)
           # — same job-snapshot-then-style resolution as the flags above.
           "first_frames": bool(jc.get("h3_first_frames") if jc.get("h3_first_frames") is not None
                                else ss.get("h3_first_frames")),
           "style_note": _scene_style_note(job_id)}
    _ACTED_CTX[key] = (time.monotonic(), ctx)
    return ctx


def _scene_to_json(row: dict, wd: Path | None = None) -> dict:
    sid = int(row["id"])
    preview = row.get("preview_path") or ""
    has_preview = bool(preview and Path(preview).exists())
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    out = {
        "id": sid,
        "title": row.get("title", ""),
        "image_prompt": row.get("image_prompt", ""),
        "video_prompt": row.get("video_prompt", ""),
        "narration": row.get("narration", ""),
        "tts_text": meta.get("tts_text", ""),
        "voice": meta.get("voice", ""),
        "mode": meta.get("mode", "narration"),
        "lines": meta.get("lines", []),
        "duration": meta.get("duration", 0),
        # Acted-scene fields (empty on narrated scenes): the editor writes the
        # scene through these, and the video prompt is assembled from them.
        "setting": meta.get("setting", ""),
        "camera": meta.get("camera", ""),
        "soundscape": meta.get("soundscape", ""),
        "no_wardrobe": bool(meta.get("no_wardrobe")),
        # This scene picks up the previous scene's shot without a cut — set by
        # the divide step or the editors; the render chains such scenes.
        "continues_previous": bool(meta.get("continues_previous")),
        "cast": meta.get("cast", []),
        # Normalized (a prose string becomes one whole-take beat): the editor
        # maps over these, and the renderer normalizes identically.
        "beats": performance_mode.norm_beats(
            meta.get("beats"),
            float(meta.get("seconds") or meta.get("duration") or 10)),
        "seconds": meta.get("seconds", 0),
        # A song film's performance beat: acted whatever the style toggle says,
        # so the editor must show the acted setup off the SCENE, not the style.
        "singing": bool(meta.get("singing")),
        # The stretch of the film's song this beat performs — the take ships
        # muted, so a screen showing the clip needs the window to play the
        # music that belongs under it.
        "song_window": meta.get("song_window") or None,
        "prompt_edited": bool(meta.get("prompt_override")),
        "preview_path": preview if has_preview else "",
        "has_preview": has_preview,
    }
    if wd is not None:
        out["history"] = image_history.history(wd, sid)
        out["video_history"] = video_history.history(wd, sid)
        # A PERFORMED silent scene is shot on H3 from the fields above, so the
        # editor shows the prompt they assemble into — the same one a dialogue
        # scene keeps in video_prompt. It is rebuilt on read rather than stored:
        # the video prompt still belongs to the I2V render this style opted out
        # of, and turning the toggle back off must leave it usable.
        if out["mode"] == "silent":
            try:
                ctx = _acted_scene_ctx(wd)
                if performance_mode.renders_acted({"metadata": meta}, ctx["acted_cfg"]):
                    acted = performance_mode.acted_meta(
                        {**row, "metadata": meta, "lines": []}, chained=ctx["chained"])
                    out["acted_prompt"] = performance_mode.build_h3_prompt(
                        acted, style_note=ctx["style_note"],
                        picture_names=list(acted.get("cast") or []))
            except Exception:
                gapp.logger.debug("Could not assemble the acted prompt for scene %d", sid,
                                  exc_info=True)
    return out


def _character_to_json(wd: Path, c: dict) -> dict:
    """Serialize one per-script character for the editor's Characters tab,
    attaching a cache-busted image URL when its look image exists on disk."""
    img = gapp._script_character_image_path(wd, c.get("ref_image"))
    has_image = bool(img and img.exists() and img.stat().st_size > 0)
    return {
        "id": c.get("id", ""),
        "name": c.get("name", ""),
        "aliases": c.get("aliases") or [],
        "description": c.get("description", ""),
        "voice": c.get("voice", ""),
        "has_image": has_image,
        "image_url": f"/api/file?path={img}&t={int(img.stat().st_mtime)}" if has_image else "",
        "history": image_history.char_history(wd, c.get("id", "")),
    }


def _script_characters_payload(wd: Path) -> list[dict]:
    """All of a script's own characters, serialized for the frontend."""
    return [_character_to_json(wd, c) for c in gapp._read_script_characters(wd)]


# ── activity tracker ─────────────────────────────────────────────────────────
# Live ops + durable history for the Activity screen. History survives restarts
# (JSON under ~/.local/share/video-generator/). Events are grouped by film when
# a work_dir is known; low-value background work is marked noise for UI fold-up.

_op_lock = threading.Lock()
_current_ops: dict = {}   # op_id -> live event dict
# Concurrent worker sub-jobs *within* a single film task — e.g. every scene of a
# parallel final upscale, which otherwise share one _film_tasks entry and show as
# a single flickering status. Surfaced on /api/activity so each busy worker is
# visible with its own ETA; deliberately ignored by /api/films/task(s), which
# track the parent task. Keyed by a stable sub-job id; guarded by _op_lock.
_film_subjobs: dict = {}  # subjob_id -> live sub-job dict
_activity_log: list = []  # completed events, newest first
_ACTIVITY_LOG_MAX = 200
_ACTIVITY_LOG_PATH = Path.home() / ".local" / "share" / "video-generator" / "activity_log.json"
_activity_log_loaded = False

# Names that are useful for debugging but noisy on the Activity screen.
_NOISE_OP_NAMES = frozenset({
    "Automation tick",
    "Fetching comments",
    "Fetching X mentions",
})

# Live sub-phase labels for film edit tasks (keyed by _film_tasks["step"]).
_RERENDER_STEP_LABELS = {
    "narration": "recording narration",
    "image": "painting first frame",
    "video": "rendering video",
    "final_upscale": "upscaling final video",
    "first_frame_cover": "burning cover into first frame",
    "music": "composing music",
    "finalize": "assembling film",
    "mux": "muxing audio",
    "revoice": "singing it in the new voice",
    "translate": "translating the script",
    "continuing": "extending the take",
    "acted scene": "shooting the take",
}


def _activity_category(name: str) -> str:
    n = (name or "").lower()
    if "automation" in n:
        return "automation"
    if any(k in n for k in ("upscal", "re-render", "rerender", "narrator", "music", "remix", "film", "assemble")):
        return "film"
    if any(k in n for k in ("render", "generat", "script", "preview", "scene")):
        return "script" if "script" in n else "render"
    if any(k in n for k in ("youtube", "upload", "x ", "posting", "comment", "mention", "thumbnail", "description", "title")):
        return "publish"
    if any(k in n for k in ("engagement", "voice", "suggestion")):
        return "system"
    return "other"


def _activity_is_noise(name: str, category: str = "") -> bool:
    if (name or "") in _NOISE_OP_NAMES:
        return True
    return category == "automation"


def _activity_group_for(work_dir: str = "", title: str = "", *, noise: bool = False, category: str = "") -> tuple[str, str]:
    """Return (group_key, group_label) for collapse/expand in the UI."""
    if noise or category == "automation":
        return "noise", "Background & automation"
    wd = (work_dir or "").strip()
    if wd:
        key = Path(wd).name
        label = (title or "").strip() or key
        return f"film:{key}", label
    if category in {"publish", "system", "other"}:
        return f"cat:{category}", category.replace("_", " ").title()
    return "system", "System"


def _make_activity_event(
    *,
    name: str,
    detail: str = "",
    started_at: float | None = None,
    finished_at: float | None = None,
    status: str = "done",
    work_dir: str = "",
    title: str = "",
    pct: float | int | None = None,
    eta_text: str | None = None,
    eta_seconds: float | None = None,
    event_id: str | None = None,
    category: str | None = None,
) -> dict:
    now = time.time()
    started = float(started_at or finished_at or now)
    finished = finished_at
    cat = category or _activity_category(name)
    noise = _activity_is_noise(name, cat)
    # Prefer a real film title when we only have a work_dir.
    film_title = (title or "").strip()
    if work_dir and not film_title:
        try:
            film_title = _video_title_for(Path(work_dir))
        except Exception:
            film_title = Path(work_dir).name
    group_key, group_label = _activity_group_for(
        work_dir, film_title, noise=noise, category=cat,
    )
    duration_s = None
    elapsed_s = None
    if finished is not None:
        duration_s = round(max(0.0, float(finished) - started), 1)
    else:
        elapsed_s = round(max(0.0, now - started), 1)
    ev = {
        "id": event_id or uuid.uuid4().hex,
        "name": name,
        "detail": detail or "",
        "category": cat,
        "noise": noise,
        "group_key": group_key,
        "group_label": group_label,
        "work_dir": work_dir or "",
        "title": film_title or "",
        "status": status,
        "started_at": started,
        "ts": float(finished if finished is not None else now),
        "duration_s": duration_s,
        "elapsed_s": elapsed_s,
        "pct": int(round(pct)) if pct is not None else None,
        "eta_text": eta_text,
        "eta_seconds": eta_seconds,
    }
    return ev


def _load_activity_log() -> None:
    global _activity_log_loaded, _activity_log
    if _activity_log_loaded:
        return
    _activity_log_loaded = True
    try:
        if _ACTIVITY_LOG_PATH.exists():
            data = json.loads(_ACTIVITY_LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _activity_log = [e for e in data if isinstance(e, dict)][:_ACTIVITY_LOG_MAX]
    except Exception:
        _activity_log = list(_activity_log)


def _persist_activity_log_locked() -> None:
    try:
        _ACTIVITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ACTIVITY_LOG_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(_activity_log[:_ACTIVITY_LOG_MAX], ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(_ACTIVITY_LOG_PATH)
    except Exception:
        pass


def _append_activity_locked(
    name: str,
    detail: str,
    end: float,
    started: float,
    *,
    work_dir: str = "",
    title: str = "",
    status: str = "done",
    category: str | None = None,
) -> None:
    _load_activity_log()
    ev = _make_activity_event(
        name=name,
        detail=detail,
        started_at=started,
        finished_at=end,
        status=status,
        work_dir=work_dir,
        title=title,
        category=category,
    )
    _activity_log.insert(0, ev)
    del _activity_log[_ACTIVITY_LOG_MAX:]
    _persist_activity_log_locked()


@contextmanager
def _track_op(name: str, detail: str = "", work_dir: str = "", title: str = "",
              category: str | None = None):
    started = time.time()
    op_id = uuid.uuid4().hex
    with _op_lock:
        _load_activity_log()
        _current_ops[op_id] = _make_activity_event(
            name=name,
            detail=detail,
            started_at=started,
            status="running",
            work_dir=work_dir,
            title=title,
            event_id=op_id,
            category=category,
        )
    status = "done"
    try:
        yield op_id
    except BaseException:
        # A failed op must not land in the history claiming it succeeded.
        status = "error"
        raise
    finally:
        end = time.time()
        with _op_lock:
            _current_ops.pop(op_id, None)
            _append_activity_locked(
                name, detail, end, started,
                work_dir=work_dir, title=title, status=status,
                category=category,
            )


def _acquire_op_worker(pool, op_id: str) -> str:
    """pool.acquire() that shows the wait: while it blocks on a busy GPU the
    tracked op's Activity row reads "queued" instead of a green "running" one.
    The _track_op twin of _acquire_render_worker; *op_id* is what _track_op
    yields. started_at is left alone so the elapsed clock keeps ticking and the
    wait itself stays visible."""
    with _op_lock:
        detail = str((_current_ops.get(op_id) or {}).get("detail") or "")

    def _set(status: str, text: str) -> None:
        with _op_lock:
            ev = _current_ops.get(op_id)
            if ev:
                ev["status"], ev["detail"] = status, text

    _set("queued", f"waiting for a free worker · {detail}" if detail
         else "waiting for a free worker")
    try:
        return pool.acquire()
    finally:
        _set("running", detail)


def _film_task_started_at(task_id: str) -> float:
    meta = (_film_task_meta.get(task_id) or {}) if "_film_task_meta" in globals() else {}
    try:
        return float(meta.get("started_at") or 0.0)
    except (TypeError, ValueError):
        pass
    try:
        return float(str(task_id).rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 0.0


def _film_job_dims(work_dir: str) -> tuple[int | None, int | None]:
    """Output (width, height) for a film's scenes, for resolution-keyed ETAs."""
    if not work_dir:
        return None, None
    try:
        jc = _film_job_config(Path(work_dir))
    except Exception:
        return None, None
    dims = gapp._RESOLUTIONS.get((jc.get("resolution") or "").strip())
    if dims:
        return int(dims[0]), int(dims[1])
    w, h = jc.get("vid_width"), jc.get("vid_height")
    if w and h:
        try:
            return int(w), int(h)
        except (TypeError, ValueError):
            return None, None
    return None, None


def _film_op_for(component: str) -> str:
    """Learned-timing op key for a film task's component (empty = don't estimate)."""
    if component == "narrator":
        return "narrator_scene"
    if component == "music":
        return "music_regen"
    if component == "song_revoice":
        return "song_revoice"
    if component in ("narration", "image", "video"):
        return f"rerender_{component}"
    return ""


def _film_task_eta(component: str, task: dict, started: float, dims: tuple) -> tuple:
    """(eta_text, eta_seconds) for a running film task from learned durations.

    Single-shot ops (scene re-render, music) count down from their learned
    average as they run; the narrator change is sequential across scenes, so its
    ETA is the per-scene average times the scenes still to go."""
    op = _film_op_for(component)
    if not op:
        return None, None
    w, h = dims
    est, _learned = film_timing.estimate(op, width=w, height=h)
    if op == "narrator_scene":
        try:
            total, current = int(task.get("total") or 0), int(task.get("current") or 0)
        except (TypeError, ValueError):
            total = current = 0
        remaining_scenes = max(1, total - current + 1) if total else 1
        remaining = est * remaining_scenes
    else:
        remaining = max(3.0, est - max(0.0, time.time() - started))
    return humanize_eta(remaining), round(remaining)


def _film_task_activity_op(task_id: str, task: dict) -> dict | None:
    if task.get("status") != "running":
        return None
    # The parallel scene upscale surfaces one sub-job per busy worker (see
    # _film_subjobs); skip the coarse parent while its workers fan out so the
    # screen shows every scene, not one flickering "Upscaling scene N".
    if task.get("fanout"):
        return None

    meta = _film_task_meta.get(task_id) or {}
    component = str(meta.get("component") or "").strip()
    started = _film_task_started_at(task_id) or time.time()
    step = str(task.get("step") or component or "").strip()
    detail = _RERENDER_STEP_LABELS.get(step, step)
    # Blocked in _acquire_render_worker: in line for a worker, not on a GPU yet.
    queued = bool(task.get("queued"))
    if queued:
        detail = f"waiting for a free worker · {detail}" if detail else "waiting for a free worker"
    work_dir = str(meta.get("work_dir") or "")

    scene_id = int(meta.get("scene_id") or task.get("scene_id") or 0)
    if task_id.startswith("rerender_") and not scene_id:
        parts = task_id.split("_")
        if len(parts) >= 2:
            try:
                scene_id = int(parts[1])
            except ValueError:
                scene_id = 0
    if scene_id > 0 and component == "continue":
        name = f"Continuing scene {scene_id}"
    elif scene_id > 0 and component != "narrator":
        name = f"Re-rendering scene {scene_id}"
    elif component == "final_upscale" or task_id.startswith("final_upscale_"):
        name = "Upscaling final video"
    elif component == "song_revoice" or task_id.startswith("song_revoice_"):
        name = "Re-voicing the song"
    elif component == "music" or task_id.startswith("music_regen_"):
        name = "Regenerating music"
    elif component == "narrator" or task_id.startswith("narrator_regen_"):
        name = "Changing narrator"
        current, total = task.get("current"), task.get("total")
        if current and total:
            detail = f"scene {current}/{total} · {detail}" if detail else f"scene {current}/{total}"
    elif component == "localize" or task_id.startswith("localize_"):
        name = "Localizing film"
    elif component == "first_frame_cover" or task_id.startswith("first_frame_cover_"):
        name = "Burning the cover in"
    else:
        name = "Updating film"

    title = ""
    if work_dir:
        try:
            title = _video_title_for(Path(work_dir))
        except Exception:
            title = Path(work_dir).name
        if not detail:
            detail = title
        elif title and title not in detail:
            detail = f"{detail} · {title}"

    pct = task.get("pct")
    if pct is None and task.get("current") and task.get("total"):
        try:
            pct = 100.0 * float(task["current"]) / max(1.0, float(task["total"]))
        except (TypeError, ValueError):
            pct = None

    if queued:
        # No countdown while nothing runs — the step ETA starts once a worker
        # is acquired; elapsed keeps ticking so the wait itself is visible.
        eta_text, eta_seconds = None, None
    else:
        eta_text, eta_seconds = _film_task_eta(component, task, started, _film_job_dims(work_dir))

    return _make_activity_event(
        name=name,
        detail=detail,
        started_at=started,
        status="queued" if queued else "running",
        work_dir=work_dir,
        title=title,
        pct=pct,
        eta_text=eta_text,
        eta_seconds=eta_seconds,
        event_id=f"film:{task_id}",
        category="film",
    )


def _register_film_subjob(subjob_id: str, **fields) -> None:
    with _op_lock:
        _film_subjobs[subjob_id] = {"id": subjob_id, "started_at": time.time(), **fields}


def _clear_film_subjob(subjob_id: str) -> None:
    with _op_lock:
        _film_subjobs.pop(subjob_id, None)


def _clear_film_subjobs_for(task_id: str) -> None:
    """Drop any leftover sub-jobs for a task (e.g. after it errors mid fan-out)."""
    prefix = f"{task_id}#"
    with _op_lock:
        for k in [k for k in _film_subjobs if k.startswith(prefix)]:
            _film_subjobs.pop(k, None)


def _film_subjob_activity_ops() -> list[dict]:
    """Live activity events for concurrent worker sub-jobs (parallel upscale)."""
    with _op_lock:
        subs = [dict(s) for s in _film_subjobs.values()]
    ops: list[dict] = []
    now = time.time()
    for s in subs:
        started = float(s.get("started_at") or now)
        est = s.get("est_seconds")
        # Waiting for a worker lease: no ETA countdown until it's on a GPU.
        queued = bool(s.get("queued"))
        eta_text = eta_seconds = None
        if est and not queued:
            remaining = max(3.0, float(est) - max(0.0, now - started))
            eta_text, eta_seconds = humanize_eta(remaining), round(remaining)
        detail = s.get("detail") or ""
        if queued:
            detail = f"waiting for a free worker · {detail}" if detail else "waiting for a free worker"
        ops.append(_make_activity_event(
            name=s.get("name") or "Working",
            detail=detail,
            started_at=started,
            status="queued" if queued else "running",
            work_dir=s.get("work_dir") or "",
            title=s.get("title") or "",
            pct=s.get("pct"),
            eta_text=eta_text,
            eta_seconds=eta_seconds,
            event_id=s.get("id"),
            category="film",
        ))
    return ops


def _record_film_task_activity(
    task_id: str,
    *,
    started: float,
    done_name: str,
    failed_name: str,
    cancelled_name: str,
    detail: str = "",
) -> None:
    end = time.time()
    status = (_film_tasks.get(task_id) or {}).get("status")
    if status == "error":
        name = failed_name
        st = "error"
    elif status == "cancelled":
        name = cancelled_name
        st = "cancelled"
    else:
        name = done_name
        st = "done"
    meta = _film_task_meta.get(task_id) or {}
    work_dir = str(meta.get("work_dir") or "")
    title = ""
    if work_dir:
        try:
            title = _video_title_for(Path(work_dir))
        except Exception:
            title = Path(work_dir).name
    # Feed the learned-duration model so this op predicts its own ETA next time.
    if st == "done":
        component = str(meta.get("component") or "").strip()
        if component == "music":
            film_timing.record("music_regen", end - started)
        elif component == "song_revoice":
            film_timing.record("song_revoice", end - started)
        elif component == "narrator":
            n = int((_film_tasks.get(task_id) or {}).get("scene_count") or 0)
            if n > 0:
                film_timing.record("narrator_scene", (end - started) / n)
    with _op_lock:
        _append_activity_locked(
            name, detail, end, started,
            work_dir=work_dir, title=title, status=st, category="film",
        )


def _render_activity_item(wd: Path) -> dict:
    """One "Rendering film" activity row with progress % and a learned ETA."""
    pct, msg = gapp._status_for_work_dir(wd)
    title = wd.name
    eta_text, eta_seconds = None, None
    started_at = None
    eta, status = None, ""
    store = DurableStore.default()
    try:
        job_row = store.get_job_by_work_dir(str(wd))
        if job_row is not None:
            job = _row_to_dict(job_row)
            status = job.get("status", "")
            title = job.get("title") or title
            try:
                started_at = float(job.get("started_at") or job.get("created_at") or 0) or None
            except (TypeError, ValueError):
                started_at = None
            tasks = [_row_to_dict(t) for t in store.task_rows(job["id"])]
            cfg = gapp.load_config()
            try:
                eta = estimate_eta(tasks, store.timing_table(), cfg,
                                   reserved_comfy=_ui_reserved_comfy(cfg))
                if eta:
                    eta_text = eta.get("eta_text")
                    eta_seconds = eta.get("eta_seconds")
            except Exception:
                pass
    finally:
        store.close()
    # Same reconciliation the Progress screen + sidebar badge use, so the
    # Activity % matches the ETA instead of the raw band value.
    final_path = gapp._final_path_for_work_dir(wd)
    _done = final_path.exists() and final_path.stat().st_size > 10_000 and (wd / "combined.mp4").exists()
    pct = _display_pct(int(round(pct)), eta, status, _done)
    return _make_activity_event(
        name="Rendering film",
        detail=msg or title,
        started_at=started_at or time.time(),
        status="running",
        work_dir=str(wd),
        title=title,
        pct=pct,
        eta_text=eta_text,
        eta_seconds=eta_seconds,
        event_id=f"render:{wd.name}",
        category="render",
    )


def _live_render_activity_items() -> list[dict]:
    """Live full-film generation, one item per job.

    Running renders are found by their job.json pid — the mtime heuristic in
    _preferred_work_dir flips to a freshly created film while another is still
    rendering, which made the real render vanish from Activity. Queue items
    still creating their script/song follow as "queued" rows so a film whose
    render hasn't started is never presented as the one rendering."""
    items: list[dict] = []
    try:
        running = gapp._running_work_dirs()
        if not running and gapp._is_job_running():
            # No live render process (script/song generation, or the moment
            # between launch and the pid landing in job.json) — fall back to
            # the recency pick so the banner keeps showing the live work.
            wd = gapp._preferred_work_dir("")
            if wd is not None:
                running = [wd]
        for wd in running:
            try:
                items.append(_render_activity_item(wd))
            except Exception:
                continue
        seen = {str(wd) for wd in running}
        cutoff = time.time() - 86400
        for q in yt.load_queue():
            if q.get("status") != "creating":
                continue
            qwd = q.get("work_dir") or ""
            if not qwd or qwd in seen:
                continue
            ts = q.get("updated_at") or q.get("created_at") or 0
            if ts <= cutoff:
                continue
            try:
                job_meta = json.loads((Path(qwd) / "job.json").read_text())
                if job_meta.get("status") in ("error", "cancelled", "paused", "done"):
                    continue
            except Exception:
                pass
            _pct, msg = gapp._status_for_work_dir(Path(qwd))
            items.append(_make_activity_event(
                name="Render queued",
                detail=msg or "",
                started_at=float(ts) or time.time(),
                status="queued",
                work_dir=qwd,
                title=q.get("title") or Path(qwd).name,
                event_id=f"render:{Path(qwd).name}",
                category="render",
            ))
    except Exception:
        pass
    return items


def _group_activity_events(events: list[dict]) -> list[dict]:
    """Nest flat events under collapsible groups (film / noise / system)."""
    order: list[str] = []
    buckets: dict[str, dict] = {}
    for ev in events:
        key = str(ev.get("group_key") or "system")
        if key not in buckets:
            order.append(key)
            buckets[key] = {
                "key": key,
                "label": ev.get("group_label") or key,
                "work_dir": ev.get("work_dir") or "",
                "title": ev.get("title") or "",
                "noise": bool(ev.get("noise")),
                "live_count": 0,
                "items": [],
            }
        g = buckets[key]
        g["items"].append(ev)
        if ev.get("status") in ("running", "queued"):
            g["live_count"] += 1
        # Prefer a non-empty film title/work_dir on the group header.
        if not g["work_dir"] and ev.get("work_dir"):
            g["work_dir"] = ev["work_dir"]
        if not g["title"] and ev.get("title"):
            g["title"] = ev["title"]
            if not str(g["label"]).startswith("Background"):
                g["label"] = ev["title"]
    groups = [buckets[k] for k in order]
    # Live film groups first, then other live, then noise last among idle.
    def _sort_key(g: dict) -> tuple:
        live = 1 if g["live_count"] else 0
        noise = 1 if g["noise"] else 0
        return (-live, noise, g["label"].lower())
    groups.sort(key=_sort_key)
    return groups


# ── config ───────────────────────────────────────────────────────────────────

@api.get("/api/config")
def get_config() -> dict:
    cfg = gapp.load_config()
    return {
        "config": gapp.public_config(cfg),
        "voices": gapp.get_voice_choices(),
        # Root of every work_dir; the frontend joins it with a URL slug to
        # reconstruct a full path for deep links (issue #32).
        "videos_dir": str(gapp.OUTPUT_DIR),
        # Where character reference images live; the Settings UI joins it with
        # "<char_id>.png" to display a thumbnail via /api/file.
        "characters_dir": str(gapp._characters_dir()),
        # Kept for backward compatibility (composed name strings stay canonical).
        "resolutions": list(gapp._RESOLUTIONS.keys()),
        # Upscale-only finishing sizes (QHD/4K) live here, never in `resolutions`
        # — nothing may render at them.
        "upscale_resolutions": list(gapp._UPSCALE_RESOLUTIONS.keys()),
        "default_resolution": gapp._DEFAULT_RESOLUTION,
        # Structured selectors so the UI can offer an orientation + pixel toggle.
        "orientations": gapp._ORIENTATIONS,
        "pixel_tiers": [{"key": t["key"], "label": t["label"],
                         "upscale_only": bool(t.get("upscale_only"))}
                        for t in gapp._PIXEL_TIERS],
        "default_orientation": gapp._DEFAULT_ORIENTATION,
        "default_pixels": gapp._DEFAULT_PIXELS,
        # Small/Medium/Large size buckets and their fallback presets, so the
        # Settings editor and AI-ideas screen can render a per-style size picker.
        "size_buckets": list(gapp._SIZE_BUCKETS),
        "default_size_presets": gapp.DEFAULT_CFG["default_size_presets"],
        # Measured per-voice cadences keyed "<voice>|<engine>" (words/minute at
        # speed 1.0) + the fallback used until a voice is measured — drives the
        # cadence slider defaults and the length→word-count estimates.
        "voice_cadences": cadence.load_store(),
        "default_wpm": cadence.DEFAULT_WPM,
    }


class ConfigUpdate(BaseModel):
    config: dict


@api.post("/api/config")
def post_config(body: ConfigUpdate) -> dict:
    cfg = gapp.load_config()
    merged = gapp.merge_config_update(cfg, body.config)
    gapp.save_config(merged)
    return {"ok": True, "config": gapp.public_config(gapp.load_config())}


# ── settings backup / restore (issue #106) ───────────────────────────────────

@api.get("/api/settings/backup")
def settings_backup(scope: str = Query("full")):
    """Download a zip of this machine's settings. scope=full is everything in
    the config dir (config, YouTube login, voices, operational state) minus
    regenerable scratch; scope=operational is just the app-accumulated state."""
    try:
        data, filename = gapp.build_settings_backup(scope)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class SettingsRestore(BaseModel):
    data: str


@api.post("/api/settings/restore")
def settings_restore(body: SettingsRestore) -> dict:
    """Restore a backup zip (full or operational) over the config dir."""
    raw = body.data or ""
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        blob = base64.b64decode(raw)
    except Exception:
        raise HTTPException(400, "Could not read the uploaded backup file.")
    if not blob:
        raise HTTPException(400, "The uploaded backup is empty.")
    try:
        result = gapp.restore_settings_backup(blob)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Restored tokens/secrets won't be seen while the old auth result is cached.
    try:
        yt._auth_cache.clear()
    except Exception:
        pass
    return {"ok": True, **result, "config": gapp.public_config(gapp.load_config())}


# ── prompt editor ────────────────────────────────────────────────────────────
# The LLM/image prompts behind every generation. The app's own prompts.yaml is
# the baseline and is never written to; edits are stored as a sparse override in
# the config dir, so any prompt can be reverted to the original at any time.

@api.get("/api/prompts")
def get_prompts() -> dict:
    return {
        "prompts": _prompts.catalogue(),
        "override_path": str(_prompts._OVERRIDE_PATH),
    }


class PromptUpdate(BaseModel):
    name: str
    fields: dict


@api.post("/api/prompts")
def post_prompt(body: PromptUpdate) -> dict:
    fields = {k: v for k, v in (body.fields or {}).items() if isinstance(v, str)}
    if not fields:
        raise HTTPException(400, "No prompt text supplied.")
    if any(not v.strip() for v in fields.values()):
        raise HTTPException(400, "A prompt cannot be saved empty — use Revert to restore the original.")
    try:
        _prompts.save(body.name, fields)
    except KeyError:
        raise HTTPException(404, f"Unknown prompt {body.name!r}.")
    return {"ok": True, "prompts": _prompts.catalogue()}


class PromptReset(BaseModel):
    name: str | None = None


@api.post("/api/prompts/reset")
def post_prompt_reset(body: PromptReset) -> dict:
    """Revert one prompt (name) or every prompt (name omitted) to the original."""
    if body.name and body.name not in _prompts.defaults():
        raise HTTPException(404, f"Unknown prompt {body.name!r}.")
    _prompts.reset(body.name or None)
    return {"ok": True, "prompts": _prompts.catalogue()}


# ── voices ───────────────────────────────────────────────────────────────────

_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm", ".opus"}


def _decode_audio(data: str, filename: str) -> tuple[bytes, str]:
    """Decode a base64 (optionally data-URL) audio payload to (bytes, extension)."""
    if not data:
        raise HTTPException(400, "No audio provided.")
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        raw = base64.b64decode(data)
    except Exception:
        raise HTTPException(400, "Could not read the audio file.")
    if not raw:
        raise HTTPException(400, "The audio file is empty.")
    ext = Path(filename or "").suffix.lower()
    if ext not in _AUDIO_EXTS:
        ext = ".wav"
    return raw, ext


def _voice_response(cfg: dict) -> dict:
    return {"ok": True, "config": gapp.public_config(cfg), "voices": gapp.get_voice_choices()}


class VoiceAdd(BaseModel):
    name: str
    filename: str = ""
    data: str
    # casting metadata (drives the character voice auto-cast; all optional)
    gender: str | None = None
    age: str | None = None
    accent: str | None = None
    tone: str | None = None


class VoiceUpdate(BaseModel):
    name: str
    new_name: str | None = None
    filename: str = ""
    data: str | None = None
    gender: str | None = None
    age: str | None = None
    accent: str | None = None
    tone: str | None = None


class VoiceDelete(BaseModel):
    name: str


@api.post("/api/voices/add")
def voices_add(body: VoiceAdd) -> dict:
    raw, ext = _decode_audio(body.data, body.filename)
    try:
        cfg = gapp.add_voice(body.name, raw, ext,
                             gender=body.gender, age=body.age,
                             accent=body.accent, tone=body.tone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _voice_response(cfg)


@api.post("/api/voices/update")
def voices_update(body: VoiceUpdate) -> dict:
    audio, ext = None, ".wav"
    if body.data:
        audio, ext = _decode_audio(body.data, body.filename)
    try:
        cfg = gapp.update_voice(body.name, new_name=body.new_name, audio=audio, ext=ext,
                                gender=body.gender, age=body.age,
                                accent=body.accent, tone=body.tone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _voice_response(cfg)


@api.post("/api/voices/delete")
def voices_delete(body: VoiceDelete) -> dict:
    try:
        cfg = gapp.delete_voice(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _voice_response(cfg)


# A ready-made passage for the record-a-voice screen (issue #192): English gets
# it instantly with no LLM call; other languages (and "New script") are written
# by the LLM. ~60 words ≈ 20–25 seconds read aloud.
_DEFAULT_READING_SCRIPT = (
    "Hello! I'm recording a short sample of my voice so it can narrate videos. "
    "I love telling stories — some are funny, some are serious, and a few are "
    "downright strange. When I read aloud, I try to speak clearly and calmly, "
    "with a little warmth in my voice. Thanks for listening; I think this is "
    "just about long enough."
)


class VoiceReadingScript(BaseModel):
    language: str = "en"
    fresh: bool = False   # True = write a new script even where a canned one exists


@api.post("/api/voices/reading-script")
def voices_reading_script(body: VoiceReadingScript) -> dict:
    """A short passage to read aloud when recording a voice clip (issue #192)."""
    from pipeline.chatterbox import LANGUAGES, norm_language
    cfg = gapp.load_config()
    lang = norm_language(body.language)
    lang_name = LANGUAGES.get(lang, "English")
    canned = _DEFAULT_READING_SCRIPT if lang == "en" else ""
    if canned and not body.fresh:
        return {"ok": True, "text": canned, "language": lang}
    if not llm.llm_backend_ready(cfg):
        if canned:
            return {"ok": True, "text": canned, "language": lang}
        raise HTTPException(503, f"Writing a script in {lang_name} needs the LLM backend (Settings → Infrastructure).")
    system = ("You write short scripts people read aloud to record a voice-cloning "
              "reference clip. Return ONLY the passage — no title, no quotes, no notes.")
    user = (f"Write a passage of roughly 50-70 words (about 20 seconds read aloud), "
            f"entirely in {lang_name}. First person, friendly and natural, with varied "
            "sentence lengths and a touch of emotion so the recording captures the "
            "speaker's range. Easy to read aloud: no numbers, abbreviations, or "
            "hard-to-pronounce names.")
    try:
        with _track_op("Writing a reading script", lang_name):
            text = _llm_complete(system, user, cfg, max_tokens=300).strip().strip('"').strip()
    except Exception as e:
        if canned:
            return {"ok": True, "text": canned, "language": lang}
        raise HTTPException(503, f"Script generation failed: {str(e).splitlines()[0][:200]}")
    if not text and not canned:
        raise HTTPException(503, "The LLM returned an empty script.")
    return {"ok": True, "text": text or canned, "language": lang}


# ── Character reference images (consistent characters, Phase 2) ──────────────
# Characters are a global library, identified by char_id. These persist
# immediately (mirroring voice ops) and return the fresh config; the client
# merges the global characters list back into any staged edits.

class CharacterImage(BaseModel):
    char_id: str
    filename: str = ""
    data: str


class CharacterRef(BaseModel):
    char_id: str


class CharacterPortrait(BaseModel):
    char_id: str
    extra_prompt: str = ""


class CharacterSelect(BaseModel):
    char_id: str
    version_id: int


def _decode_image(data: str) -> bytes:
    """Decode a base64 (optionally data-URL) image payload to bytes."""
    if not data:
        raise HTTPException(400, "No image provided.")
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        raw = base64.b64decode(data)
    except Exception:
        raise HTTPException(400, "Could not read the image file.")
    if not raw:
        raise HTTPException(400, "The image file is empty.")
    return raw


def _character_response(cfg: dict) -> dict:
    return {"ok": True, "config": gapp.public_config(cfg)}


@api.post("/api/characters/image")
def characters_set_image(body: CharacterImage) -> dict:
    raw = _decode_image(body.data)
    try:
        cfg = gapp.set_character_image(body.char_id, raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _character_response(cfg)


@api.post("/api/characters/image/clear")
def characters_clear_image(body: CharacterRef) -> dict:
    try:
        cfg = gapp.clear_character_image(body.char_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _character_response(cfg)


@api.post("/api/characters/image/select")
def characters_select_image(body: CharacterSelect) -> dict:
    """Make a previously-kept look version this catalogue character's reference."""
    try:
        cfg = gapp.select_character_image(body.char_id, int(body.version_id))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    return _character_response(cfg)


@api.post("/api/characters/image/delete")
def characters_delete_image(body: CharacterSelect) -> dict:
    """Delete a kept look version (the one in use can't be deleted)."""
    try:
        cfg = gapp.delete_character_image_version(body.char_id, int(body.version_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _character_response(cfg)


@api.post("/api/characters/portrait")
def characters_portrait(body: CharacterPortrait) -> dict:
    try:
        cfg = gapp.generate_character_portrait(body.char_id, body.extra_prompt)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return _character_response(cfg)


# ── Character turnaround sheets ──────────────────────────────────────────────
# Several views of one character in one strip, built by whichever engine the
# user picks AT GENERATION TIME (see gapp.generate_character_sheet). An orbit
# sheet keeps its clip, so its panels can be re-picked frame by frame without
# another render. Rendering runs on a daemon thread — the orbit takes minutes
# and an HTTP request has no business holding that open — and the UI polls
# GET /api/characters/sheet until the status leaves "rendering".


class CharacterSheet(BaseModel):
    char_id: str
    engine: str = "image"          # "image" (one pass) | "orbit" (H3 frames)
    extra_prompt: str = ""


class CharacterSheetPanels(BaseModel):
    char_id: str
    times: list[float]


_sheet_jobs: set[str] = set()      # char_ids with a sheet render in flight
_sheet_lock = threading.Lock()


def _sheet_payload(char_id: str) -> dict:
    """Sheet state plus the URLs of the artefacts, cache-busted by mtime so a
    re-picked sheet actually reloads in the browser."""
    state = dict(gapp.character_sheet_state(char_id))
    _, sheet, clip, _ = gapp._sheet_paths(char_id)
    state["sheet_url"] = _busted_file_url(sheet) if sheet.exists() else ""
    state["clip_url"] = _busted_file_url(clip) if clip.exists() else ""
    with _sheet_lock:
        if char_id in _sheet_jobs:
            state["status"] = "rendering"
    return {"ok": True, "sheet": state}


def _sheet_in_background(char_id: str, engine: str, extra_prompt: str, name: str) -> None:
    """Daemon-thread target: build one character's sheet, tracked so the
    Activity screen shows the worker is busy (an orbit holds it for minutes)."""
    try:
        with _track_op(f"Building {name}'s character sheet",
                       "camera orbit" if engine == "orbit" else "image model"):
            gapp.generate_character_sheet(char_id, engine, extra_prompt)
    except Exception:
        gapp.logger.warning("Character sheet failed for %s", char_id, exc_info=True)
    finally:
        with _sheet_lock:
            _sheet_jobs.discard(char_id)


@api.post("/api/characters/sheet")
def characters_sheet(body: CharacterSheet) -> dict:
    """Start a sheet render and return immediately; poll for the result."""
    if body.engine not in gapp.SHEET_ENGINES:
        raise HTTPException(400, f"Unknown sheet engine {body.engine!r}.")
    try:
        char = gapp._find_character(gapp.load_config(), body.char_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not char.get("ref_image"):
        raise HTTPException(400, "This character needs a reference image before a sheet can be built.")
    with _sheet_lock:
        if body.char_id in _sheet_jobs:
            raise HTTPException(409, "A sheet is already being built for this character.")
        _sheet_jobs.add(body.char_id)
    threading.Thread(
        target=_sheet_in_background,
        args=(body.char_id, body.engine, body.extra_prompt, char.get("name") or "the character"),
        daemon=True).start()
    return _sheet_payload(body.char_id)


@api.get("/api/characters/sheet")
def characters_sheet_state(char_id: str = Query(...)) -> dict:
    return _sheet_payload(char_id)


@api.post("/api/characters/sheet/panels")
def characters_sheet_panels(body: CharacterSheetPanels) -> dict:
    """Re-stitch an orbit sheet from hand-picked frames of its own clip."""
    with _sheet_lock:
        if body.char_id in _sheet_jobs:
            raise HTTPException(409, "This character's sheet is still being built.")
    try:
        gapp.repick_character_sheet(body.char_id, body.times)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return _sheet_payload(body.char_id)


@api.post("/api/characters/sheet/clear")
def characters_sheet_clear(body: CharacterRef) -> dict:
    gapp.clear_character_sheet(body.char_id)
    return _sheet_payload(body.char_id)


# ── Per-script characters (main-character consistency) ───────────────────────
# A script carries its OWN cast, identified by the LLM at generation time and
# living in the work dir (not the global catalogue). The editor's Characters tab
# edits them here; "Save to catalogue" promotes one into the shared library.

class ScriptCharacterCreate(BaseModel):
    name: str = ""
    aliases: list[str] = []
    description: str = ""


class ScriptCharacterUpdate(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    voice: str | None = None


class ScriptCharacterImage(BaseModel):
    filename: str = ""
    data: str


class ScriptCharacterPortrait(BaseModel):
    extra_prompt: str = ""


class ScriptCharacterSelect(BaseModel):
    version_id: int


def _job_wd_or_404(job_id: str) -> Path:
    wd = gapp._job_work_dir(job_id)
    if wd is None or not Path(wd).exists() or not _safe_under(Path(wd), gapp.OUTPUT_DIR):
        raise HTTPException(404, "Script not found.")
    return Path(wd)


def _job_style_name(job_id: str) -> str:
    return _script_source_meta(job_id, "")[3]


def _film_reference_usage(wd: Path, characters=()) -> tuple[set, bool]:
    """(cast names in any scene, film has acted scenes) — what the reference
    wall means by "used by this video". Case-insensitive names.

    Narrated and ordinarily-silent scenes do not carry an explicit ``cast``
    list, so also resolve catalogue names and aliases from their image prompts.
    That is the same text and matcher the image renderer uses when deciding
    which canonical character look belongs in a scene.

    "Acted" is the render-time predicate, not the mode alone: a style that
    performs its silent scenes (h3_silent_scenes) shoots them as Ref2VA takes,
    and those takes are fed the same locations and wardrobe — so a film made of
    them needs its visuals wall exactly as a dialogue film does."""
    names: set = set()
    has_acted = False
    try:
        job_id = job_id_from_work_dir(wd)
        store = DurableStore.default()
        try:
            rows = store.scene_rows(job_id)
        finally:
            store.close()
        if not rows and (wd / "script.json").exists():
            rows = json.loads((wd / "script.json").read_text())
        jc = dict(_film_job_config(wd))
        if not jc.get("style_name"):
            jc["style_name"] = _job_style_name(job_id)
        acted_cfg = _acted_silent_cfg(jc)
        for r in rows or []:
            md = (r.get("metadata") or {}) if isinstance(r, dict) else {}
            if performance_mode.renders_acted({"metadata": md}, acted_cfg):
                has_acted = True
            for n in (md.get("cast") or []):
                if str(n).strip():
                    names.add(str(n).strip().lower())
            for ln in (md.get("lines") or []):
                spk = str((ln or {}).get("speaker") or "").strip()
                if spk:
                    names.add(spk.lower())
            image_prompt = str(r.get("image_prompt") or "")
            for c in characters or ():
                name = str((c or {}).get("name") or "").strip()
                if name and gapp._character_mentions(image_prompt, c):
                    names.add(name.lower())
    except Exception:
        pass
    return names, has_acted


def _voice_clip_url(cfg: dict, voice_name: str) -> str:
    """A playable URL for a library voice's reference clip, or ''."""
    for v in cfg.get("voices") or []:
        if v.get("name") == voice_name and v.get("path"):
            pth = Path(v["path"])
            if pth.exists():
                return f"/api/file?path={pth}"
    return ""


def _script_chars_ok(wd: Path) -> dict:
    """Script characters (editable) plus the style's catalogue members
    (read-only — they are shared across films, so they edit in Settings).
    A script character shadows a same-named catalogue one."""
    payload = _script_characters_payload(wd)
    taken = {(c.get("name") or "").strip().lower() for c in payload}
    catalogue = []
    try:
        cfg = gapp.load_config()
        style_name = _job_style_name(job_id_from_work_dir(wd))
        style_characters = gapp._style_characters(cfg, style_name)
        used, _has_acted = _film_reference_usage(wd, style_characters)
        for c in style_characters:
            name_key = (c.get("name") or "").strip().lower()
            # Only members THIS video casts — the wall shows the film's
            # references, not the whole catalogue.
            if name_key in taken or name_key not in used:
                continue
            img = gapp._character_image_path(c.get("ref_image"))
            has = bool(img and img.exists() and img.stat().st_size > 0)
            catalogue.append({
                "id": c.get("id", ""), "name": c.get("name", ""),
                "aliases": c.get("aliases") or [],
                "description": c.get("description", ""),
                "voice": c.get("voice", ""),
                "voice_url": _voice_clip_url(cfg, c.get("voice", "")),
                "has_image": has,
                "image_url": f"/api/file?path={img}&t={int(img.stat().st_mtime)}" if has else "",
                "scope": "catalogue",
            })
    except Exception:
        pass
    return {"ok": True, "characters": payload, "catalogue": catalogue}


@api.get("/api/jobs/{job_id}/characters")
def list_script_characters(job_id: str) -> dict:
    return _script_chars_ok(_job_wd_or_404(job_id))


@api.post("/api/jobs/{job_id}/characters")
def create_script_character(job_id: str, body: ScriptCharacterCreate) -> dict:
    wd = _job_wd_or_404(job_id)
    gapp.add_script_character(wd, body.name, body.aliases, body.description)
    return _script_chars_ok(wd)


@api.put("/api/jobs/{job_id}/characters/{char_id}")
def edit_script_character(job_id: str, char_id: str, body: ScriptCharacterUpdate) -> dict:
    wd = _job_wd_or_404(job_id)
    try:
        gapp.update_script_character(wd, char_id, name=body.name, aliases=body.aliases,
                                     description=body.description, voice=body.voice)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return _script_chars_ok(wd)


@api.delete("/api/jobs/{job_id}/characters/{char_id}")
def remove_script_character(job_id: str, char_id: str) -> dict:
    wd = _job_wd_or_404(job_id)
    gapp.delete_script_character(wd, char_id)
    return _script_chars_ok(wd)


@api.post("/api/jobs/{job_id}/characters/{char_id}/image")
def set_script_character_image(job_id: str, char_id: str, body: ScriptCharacterImage) -> dict:
    wd = _job_wd_or_404(job_id)
    raw = _decode_image(body.data)
    try:
        gapp.set_script_character_image(wd, char_id, raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _script_chars_ok(wd)


@api.post("/api/jobs/{job_id}/characters/{char_id}/image/clear")
def clear_script_character_image(job_id: str, char_id: str) -> dict:
    wd = _job_wd_or_404(job_id)
    try:
        gapp.clear_script_character_image(wd, char_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return _script_chars_ok(wd)


@api.post("/api/jobs/{job_id}/characters/{char_id}/image/select")
def select_script_character_image(job_id: str, char_id: str, body: ScriptCharacterSelect) -> dict:
    """Make a previously-kept look version this character's current image."""
    wd = _job_wd_or_404(job_id)
    try:
        gapp.select_script_character_image(wd, char_id, int(body.version_id))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    return _script_chars_ok(wd)


@api.post("/api/jobs/{job_id}/characters/{char_id}/image/delete")
def delete_script_character_image_version(job_id: str, char_id: str, body: ScriptCharacterSelect) -> dict:
    """Delete a kept look version (the one in use can't be deleted)."""
    wd = _job_wd_or_404(job_id)
    try:
        gapp.delete_script_character_image_version(wd, char_id, int(body.version_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _script_chars_ok(wd)


@api.post("/api/jobs/{job_id}/characters/{char_id}/portrait")
def script_character_portrait(job_id: str, char_id: str, body: ScriptCharacterPortrait) -> dict:
    wd = _job_wd_or_404(job_id)
    try:
        gapp.generate_script_character_portrait(wd, char_id, _job_style_name(job_id), body.extra_prompt)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return _script_chars_ok(wd)


@api.post("/api/jobs/{job_id}/characters/{char_id}/promote")
def promote_script_character(job_id: str, char_id: str) -> dict:
    """Save a per-script character into the global catalogue and opt this job's
    style into it (non-destructive — the script keeps its own copy)."""
    wd = _job_wd_or_404(job_id)
    try:
        cfg = gapp.promote_script_character(wd, char_id, _job_style_name(job_id))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True, "config": gapp.public_config(cfg),
            "characters": _script_characters_payload(wd)}


class VoiceTest(BaseModel):
    voice: str = ""
    robotic_amount: float | None = None
    speed: float | None = None
    # Target cadence (words/minute) to audition — resolved against the voice's
    # measured natural pace. None = the default style's setting; 0 = natural.
    cadence_wpm: float | None = None
    text: str = ""
    engine: str = ""
    language: str = ""
    sentence_pause: float | None = None


@api.post("/api/voices/test")
def voices_test(body: VoiceTest) -> dict:
    """Synthesize a short sample so the user can audition a voice and dial in the
    robotic level. The result is cached by (voice, robotic level, text, reference
    clip) in the config dir (served by /api/file): replaying the same setup is
    instant, and re-recording a voice invalidates its cached sample.

    On a cache miss F5-TTS runs synchronously (a few seconds for one sentence) —
    the client shows a 'Generating…' state while it waits.
    """
    from pipeline.tts_worker import generate_narration, DEFAULT_REF

    cfg = gapp.load_config()
    voice = (body.voice or "").strip()
    spoken = voice if voice and voice != gapp.F5TTS_DEFAULT_OPTION else "the default narrator"
    text = (body.text or "").strip() or f"This is the voice of {spoken}. What do you think?"

    ref_str = gapp.voice_path_for(voice)
    ref = Path(ref_str).expanduser() if ref_str else None

    from pipeline import cadence as _cadence
    amount = (body.robotic_amount if body.robotic_amount is not None
              else float(gapp.style_settings(cfg).get("voice_robotic_amount", 0.0) or 0.0))
    engine = gapp.tts_engines.norm(body.engine or gapp.style_settings(cfg).get("tts_engine"))
    language = gapp._norm_tts_language(body.language or gapp.style_settings(cfg).get("tts_language"))
    if body.speed is not None:
        speed = float(body.speed)
    else:
        # Speed is expressed as a target cadence (words/minute) resolved against
        # the voice's measured natural pace; legacy voice_speed still honored.
        speed_settings = {**gapp.style_settings(cfg), "tts_engine": engine}
        if voice:
            speed_settings["voice"] = voice
        if body.cadence_wpm is not None:
            speed_settings["voice_cadence_wpm"] = body.cadence_wpm
        speed = _cadence.resolve_voice_speed(speed_settings)
    # The sentence gap applies here too, so the tester is where cadence and
    # [pause] markers (typed into a custom line) can be auditioned.
    sentence_pause = gapp._norm_tts_sentence_pause(
        body.sentence_pause if body.sentence_pause is not None
        else gapp.style_settings(cfg).get("tts_sentence_pause"))

    # Content-addressed cache key: a given (voice, robotic level, speed, text,
    # source clip) always maps to the same file, so F5-TTS never re-runs for a
    # setup we've already rendered. Folding in the clip's mtime+size means
    # replacing a voice's reference audio busts its cached sample.
    try:
        st = (ref or DEFAULT_REF).stat()
        ref_stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        ref_stamp = ""
    key = hashlib.md5(
        f"{voice}|{engine}|{language}|{round(amount, 3)}|{round(speed, 3)}|{text}|{ref_stamp}|{round(sentence_pause, 3)}".encode()
    ).hexdigest()[:16]
    out = gapp.CONFIG_FILE.parent / f"voice_test_{key}.wav"

    cached = out.exists() and out.stat().st_size > 1000
    if not cached:
        tts_host = _first_live_tts_host(cfg)
        try:
            with _track_op("Testing voice", spoken):
                generate_narration(text, out, reference_wav=ref, host=tts_host,
                                   robotic_amount=amount, speed=speed,
                                   tts_engine=engine, language=language,
                                   sentence_pause=sentence_pause,
                                   cadence_voice=voice)
        except Exception as e:
            raise HTTPException(503, f"Voice test failed: {str(e).splitlines()[0][:200]}")

    return {"ok": True, "url": f"/api/file?path={out}&t={int(out.stat().st_mtime)}", "cached": cached}


def _first_live_tts_host(cfg: dict) -> str:
    """Pick a reachable TTS worker for the single-scene narration paths.

    Those paths default to one worker. Blindly using the first-configured worker
    fails the whole request when it happens to be down, even though other workers
    are up (e.g. s1 offline while s2/s3 are healthy). Probe the configured workers
    and return the first reachable one; fall back to the first configured worker
    (so any resulting error names a real endpoint) or localhost when none are set.
    """
    from pipeline.tts_worker import worker_alive
    configured = [h for h in (cfg.get("tts_workers") or []) if str(h).strip()]
    for h in configured:
        if worker_alive(h, timeout=3):
            return h
    return configured[0] if configured else "localhost"


class VoiceCalibrateBody(BaseModel):
    voice: str = ""     # "" / the default option = the bundled default narrator
    engine: str = ""    # "" = the default style's TTS engine
    language: str = ""


@api.post("/api/voices/calibrate")
def voices_calibrate(body: VoiceCalibrateBody) -> dict:
    """Measure one voice's natural cadence (words/minute) by synthesizing the
    fixed calibration passage at speed 1.0 and timing it. Synchronous — a few
    seconds per voice; the frontend loops over voices to calibrate the library.
    The measurement lands in the cadence store (pipeline/cadence.py) that every
    length estimate and speed derivation reads."""
    cfg = gapp.load_config()
    voice = (body.voice or "").strip()
    ref_str = gapp.voice_path_for(voice)
    ref = Path(ref_str).expanduser() if ref_str else None
    engine = gapp.tts_engines.norm(body.engine or gapp.style_settings(cfg).get("tts_engine"))
    language = gapp._norm_tts_language(body.language or "en")
    label = voice if voice and voice != gapp.F5TTS_DEFAULT_OPTION else "the default narrator"
    try:
        with _track_op("Calibrating voice cadence", label):
            wpm = cadence.calibrate_voice(voice, ref, _first_live_tts_host(cfg),
                                          engine=engine, language=language)
    except Exception as e:
        raise HTTPException(503, f"Cadence calibration failed: {str(e).splitlines()[0][:200]}")
    return {"ok": True, "voice": voice, "engine": engine, "wpm": round(wpm, 1),
            "voice_cadences": cadence.load_store()}


@api.get("/api/script/length-estimate")
def script_length_estimate(style_name: str = Query(""), minutes: float = Query(0.0),
                           voice: str = Query("")) -> dict:
    """What a target length means in words and scenes for a style/narrator:
    the word budget (minutes × the narrator's cadence, pause-aware) and the
    10–15 s scene plan. Drives the live word-count indication shown when the
    user picks a video length."""
    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, style_name)
    if (voice or "").strip():
        ss = {**ss, "voice": voice.strip()}
    plan = gapp.style_script_plan(ss, minutes=minutes if minutes and minutes > 0 else None)
    return {"ok": True, **plan}


def _next_worker_free_eta(cfg: dict) -> float | None:
    """Seconds until the running render frees its next ComfyUI worker, or None."""
    store = DurableStore.default()
    try:
        recent = store.recent_jobs(limit=1)
        if not recent:
            return None
        job = _row_to_dict(recent[0])
        tasks = [_row_to_dict(t) for t in store.task_rows(job["id"])]
        return next_worker_free_seconds(tasks, store.timing_table())
    except Exception:
        return None
    finally:
        store.close()


def _ui_worker_status(cfg: dict) -> dict:
    """Live state of the dynamic UI-worker reservation (issue #98).

    Reports whether the UI is being actively used, whether a worker is free for
    it right now (during a render the reserved worker reads as idle), and — when
    every worker is busy — an ETA until one frees, from the running render."""
    from pipeline.worker_pool import queue_depth
    timeout = float(cfg.get("ui_idle_timeout_seconds", ui_activity.DEFAULT_IDLE_TIMEOUT))
    comfy = cfg.get("comfy_workers") or []
    out = {
        "active": ui_activity.is_active(timeout),
        "idle_timeout_seconds": int(timeout),
        "idle_seconds": int(max(0.0, ui_activity.idle_seconds())),
        "n_workers": len(comfy),
        "available": False,      # a worker is idle and ready for the UI right now
        "eta_seconds": None,     # else, estimated seconds until one frees
        "eta_text": "",
    }
    if not out["active"] or not comfy:
        return out
    # A worker with an empty queue is free for the UI (the reserved one, or any
    # idle worker when no render is running).
    if any(queue_depth(u, timeout=2) == 0 for u in comfy):
        out["available"] = True
        return out
    eta = _next_worker_free_eta(cfg)
    if eta is not None:
        out["eta_seconds"] = int(eta)
        out["eta_text"] = humanize_eta(eta)
    return out


@api.get("/api/workers/status")
def workers_status() -> dict:
    """Live, read-only health of the configured workers.

    comfy endpoints are HTTP-probed via ComfyUI /queue, which gives both
    reachability and load (idle vs busy) — during a render the idle worker is the
    one held for the UI. tts endpoints are HTTP-probed via /health for reachability
    (F5-TTS has no queue, so there is no idle/busy). `ui` carries the dynamic
    UI-worker reservation state (issue #98). Never raises — an unreachable host is
    reported as up:false.
    """
    from pipeline.worker_pool import queue_depth
    from pipeline.tts_worker import worker_alive as tts_alive
    cfg = gapp.load_config()

    def probe(urls: list[str]) -> list[dict]:
        # queue_depth: <0 unreachable, 0 idle (free for the UI), >0 busy rendering.
        out = []
        for u in urls or []:
            depth = queue_depth(u, timeout=3)
            out.append({"endpoint": u, "up": depth >= 0, "busy": depth > 0})
        return out

    return {
        "comfy": probe(cfg.get("comfy_workers", [])),
        "tts": [{"endpoint": h, "up": tts_alive(h, timeout=3)} for h in cfg.get("tts_workers", [])],
        "ui": _ui_worker_status(cfg),
    }


_WORKER_ACTIONS = {"start", "stop", "restart"}


def _host_of(entry: str) -> str:
    """SSH hostname for a worker entry: http://HOST:PORT → HOST, host:port → host.
    Returns "" for anything that can't be a safe hostname — notably a value
    starting with '-', which ssh would parse as an option (command-injection
    guard, since worker entries come from saved config)."""
    e = (entry or "").strip()
    if "://" in e:
        host = urllib.parse.urlparse(e).hostname or ""
    else:
        host = e.split("/")[0].split(":")[0]
    return "" if host.startswith("-") else host


def _worker_hosts(cfg: dict) -> list[str]:
    """Unique SSH hosts for the fleet, from the configured comfy/tts worker URLs.
    Each host runs one docker compose stack (ComfyUI + F5-TTS) that
    scripts/worker.sh controls over SSH — same hosts `make stop W=<host>` uses."""
    hosts: list[str] = []
    for entry in (cfg.get("comfy_workers") or []) + (cfg.get("tts_workers") or []):
        h = _host_of(entry)
        if h and h not in hosts:
            hosts.append(h)
    return hosts


class WorkerControl(BaseModel):
    host: str
    action: str


@api.post("/api/workers/control")
def workers_control(body: WorkerControl) -> dict:
    """Start/stop/restart one host's worker containers (ComfyUI + F5-TTS) over
    SSH, via scripts/worker.sh — the same path as `make start/stop W=<host>`.
    The machine stays powered on; this only toggles the docker compose stack.
    `host` must be one of the configured workers (no arbitrary SSH targets)."""
    action = (body.action or "").strip().lower()
    host = (body.host or "").strip()
    if action not in _WORKER_ACTIONS:
        raise HTTPException(400, f"action must be one of {sorted(_WORKER_ACTIONS)}")
    if host not in _worker_hosts(gapp.load_config()):
        raise HTTPException(400, f"unknown worker host: {host!r}")
    script = REPO_ROOT / "scripts" / "worker.sh"
    try:
        proc = subprocess.run(
            ["bash", str(script), action, host],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{action} {host} timed out (host unreachable?)")
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise HTTPException(500, output or f"{action} {host} failed (exit {proc.returncode})")
    return {"ok": True, "host": host, "action": action, "output": output}


@api.get("/api/ui/worker")
def ui_worker_status() -> dict:
    """Lightweight poll for the UI-worker reservation indicator (issue #98)."""
    return _ui_worker_status(gapp.load_config())


@api.post("/api/ui/heartbeat")
def ui_heartbeat() -> dict:
    """The frontend pings this while the user is actively interacting with the
    UI, so the render holds one worker idle for cover/preview jobs (issue #98)."""
    ui_activity.mark_active()
    return {"ok": True, "ui": _ui_worker_status(gapp.load_config())}


# ── script generation ────────────────────────────────────────────────────────

class GenerateScriptBody(BaseModel):
    video_title: str = ""
    topic: str = ""
    # Target video length in minutes — the primary length control. The word
    # budget and scene count come from the narrator's cadence
    # (app.style_script_plan). 0 falls back to n_scenes, then the style.
    minutes: float = 0.0
    # Legacy/explicit scene count. 0 = derive from minutes (or the style).
    n_scenes: int = 0
    visual_style: str | None = None
    auto_approve: bool = False
    voice: str = ""
    resolution: str = ""
    queue_item_id: str = ""
    style_name: str = ""
    # "narration" (default) | "dialogue" | "mixed" | "silent" | "song" — see
    # docs/performance_films.md ("song" is the Music-video format: the film's
    # soundtrack is a sung song and the cast performs it on camera).
    format: str = "narration"
    # Music-video flow: the work dir /api/song/draft created (song.json, and
    # background_music.wav once generated). The story is drafted INTO it, from
    # its lyrics, instead of a fresh dir.
    work_dir: str = ""
    # Music-video flow: how long each performed clip runs. The generated
    # track's real duration is divided into clips of about this length —
    # 5 gives "5-second parts". 0 = the acted default (~10 s takes).
    clip_secs: float = 0
    # Automation only: run the script critic right after generation, before the
    # script can queue/render (config: youtube_auto_critic/_passes). The
    # interactive Create flow leaves this False — the user runs it by hand.
    auto_critic: bool = False
    # Score this film? None = whatever the style says. Rides the create brief
    # to the render, so the choice made at Create survives approve → queue.
    music: bool | None = None


# In-memory store for background script-generation tasks {task_id -> {status, ...}}.
# Generation is several Claude calls (tens of seconds). Running it inline held the
# browser's POST connection open the whole time, so any blip on that long-lived
# connection surfaced as a "NetworkError" in the UI — even though the script was
# actually created. We kick it off in a thread and let the client poll the status
# (a sequence of short GETs, which already retry on transient failures), mirroring
# the upload/cover/film tasks.
_script_tasks: dict = {}


def _run_script_task(task_id: str, body: "GenerateScriptBody") -> None:
    try:
        _script_tasks[task_id] = {"status": "done", "result": _do_script_generate(body)}
    except HTTPException as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e.detail)[:300]}
    except Exception as e:  # surface a clean message to the client
        _script_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:300]}


@api.post("/api/script/generate")
def script_generate(body: GenerateScriptBody) -> dict:
    """Kick off script generation in the background and return a task id to poll
    (see _script_tasks for why it isn't run inline)."""
    topic = (body.topic or "").strip() or (body.video_title or "").strip()
    if not topic:
        raise HTTPException(400, "Enter a video title or describe what you want to create.")
    task_id = uuid.uuid4().hex[:12]
    _script_tasks[task_id] = {"status": "running"}
    threading.Thread(target=_run_script_task, args=(task_id, body), daemon=True).start()
    return {"task_id": task_id}


@api.get("/api/script/generate/status")
def script_generate_status(task_id: str = Query(...)) -> dict:
    task = _script_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Script task not found — it may have been lost on a restart; try again.")
    if task.get("status") == "done":
        return {**task["result"], "status": "done"}
    return dict(task)


def _create_brief_path(wd: Path) -> Path:
    return Path(wd) / "create_brief.json"


def _write_create_brief(wd: Path, brief: dict) -> None:
    """Persist the Create-form inputs used to generate this script so Re-draft
    can restore the whole brief (title, direction, style, narrator, scenes…)."""
    try:
        _create_brief_path(wd).write_text(json.dumps(brief, indent=2))
    except Exception:
        gapp.logger.warning("Could not write create_brief.json for %s", wd, exc_info=True)


def _read_create_brief(wd: Path) -> dict:
    p = _create_brief_path(wd)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_brief_direction(wd: Path, direction: str | None) -> None:
    """Persist a direction given (or edited) in the Song tab into the film's
    create brief, so it steers every later re-write and comes back when the
    Brief button restores the film's settings. ``None`` leaves the brief
    alone; an empty string clears a stored direction back to the bare title
    (the same fallback Create applies to an empty Direction box)."""
    if direction is None:
        return
    brief = _read_create_brief(wd)
    if not brief:
        return
    topic = direction.strip() or str(brief.get("video_title") or "").strip()
    if topic and topic != str(brief.get("topic") or "").strip():
        brief["topic"] = topic
        _write_create_brief(wd, brief)


def _brief_direction(wd: Path, song: dict | None = None) -> str:
    """The film's direction as the Song tab shows it: the brief's topic, blank
    when that is just the bare title Create fell back to for an empty box."""
    brief = _read_create_brief(wd)
    topic = str(brief.get("topic") or "").strip()
    title = str(brief.get("video_title") or (song or {}).get("title") or "").strip()
    return "" if topic == title else topic


def _clean_lines(raw_lines) -> list[dict]:
    """Validated shot sequence from a client payload. Keeps SPEAKING shots
    (non-empty text) and SILENT shots (marked; people move, no speech); drops
    empty rows. Optional per-shot framing/video-prompt/duration preserved."""
    out = []
    for ln in raw_lines or []:
        if not isinstance(ln, dict):
            continue
        text = str(ln.get("text") or "").strip()
        shot = str(ln.get("shot") or "").strip()
        if ln.get("silent"):
            try:
                dur = float(ln.get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0.0
            row = {"silent": True, "duration": dur if dur > 0 else 3.0}
            if shot:
                row["shot"] = shot
            vp = str(ln.get("video_prompt") or "").strip()
            if vp:
                row["video_prompt"] = vp
            out.append(row)
            continue
        if not text:
            continue
        row = {"speaker": str(ln.get("speaker") or "Narrator").strip() or "Narrator", "text": text}
        if shot:
            row["shot"] = shot
        # Performance lines carry a delivery direction ("low, flat, unhurried")
        # that shapes the read. Only set when present, so dialogue scripts stay
        # byte-identical.
        delivery = str(ln.get("delivery") or "").strip()
        if delivery:
            row["delivery"] = delivery
        out.append(row)
    return out


def _scene_snapshot_row(s, image_prompt: str | None = None) -> dict:
    """A Scene as a script.json row. Dialogue/silent fields go under "metadata"
    (only when non-default), so narration scripts stay byte-identical — and so
    the render, which reads script.json, actually sees authored dialogue."""
    row = {"id": s.id, "title": s.title,
           "image_prompt": image_prompt if image_prompt is not None else s.image_prompt,
           "video_prompt": s.video_prompt, "narration": s.narration}
    md = getattr(s, "metadata", None) or {}
    if md:
        row["metadata"] = md
    return row


def _story_format_note(fmt: str) -> str | None:
    """Draft-stage counterpart of _build_dialogue_note: tells the story draft
    WHO will tell it, with none of the scene schema or clip budgets — those
    bind at the divide step, where the story becomes scenes. The draft must
    come out well-formed whatever the film's length."""
    fmt = (fmt or "narration").strip().lower()
    if fmt == "dialogue":
        return (
            "PERFORMED STORY: this story will be acted ON CAMERA by its characters "
            "speaking — there is no narrator reading it. Plan recurring characters who "
            "can carry the story in spoken exchanges (never return an empty characters "
            "array), and write chapter summaries whose beats live in what people say "
            "and do. Ignore any video length or scene limits while drafting — the "
            "story is divided into scenes afterwards."
        )
    if fmt == "mixed":
        return (
            "MIXED STORY: this story will be staged as a mix of narrated voice-over "
            "AND characters speaking on camera. Plan recurring characters who can "
            "speak (never return an empty characters array), and let the chapter "
            "summaries flag natural moments of spoken exchange alongside the "
            "narration. Ignore any video length or scene limits while drafting — the "
            "story is divided into scenes afterwards."
        )
    if fmt == "silent":
        return (
            "SILENT STORY: this story will be staged as a SILENT film — told in "
            "pictures, with no narrator reading it and hardly anyone speaking. Write "
            "chapter summaries whose beats are VISIBLE: what happens, what changes, "
            "what is seen — never information that could only arrive in words. Plan "
            "recurring characters to carry it on screen (never return an empty "
            "characters array). Ignore any video length or scene limits while "
            "drafting — the story is divided into scenes afterwards."
        )
    if fmt == "song":
        return (
            "MUSIC VIDEO STORY: this story will become the SONG of a music video — "
            "sung over performed scenes, with no narrator and no spoken lines. Plan "
            "ONE clear lead performer among the recurring characters (never return "
            "an empty characters array) — the person the camera keeps returning to — "
            "and write chapter summaries whose beats are VISIBLE and singable: "
            "images, actions and turns a song can carry, never information that "
            "could only arrive in spoken words. Ignore any video length or scene "
            "limits while drafting — the story is divided into scenes afterwards."
        )
    return None


def _build_dialogue_note(fmt: str, cast_names: list[str],
                         chained: bool = False, acted_silent: bool = False,
                         scene_secs: float | None = None) -> str | None:
    """Instruction appended to the script prompts so the LLM stages scenes as
    ACTED takes. None for the narration format (the prompts are unchanged).

    An acted scene is one continuous H3 generation carrying its own voices, so
    the shape it asks for is the one pipeline/performance.py assembles: who is
    on screen, what they say, where, and how it sounds. The word budget is the
    binding constraint — the model truncates a clip that runs past its length,
    mid-sentence. *chained* (h3_chain_scenes) roughly doubles that budget: the
    renderer shoots long scenes as two joined clips, so without the bigger
    budget here the LLM keeps writing single-clip scenes and nothing ever
    chains. *acted_silent* (h3_silent_scenes) asks for a cast on the SILENT
    scenes as well: those are performed from the same portraits, and a silent
    scene with nobody named stays on the I2V path.

    The "silent" format shares all of it — a silent film is staged from the
    same schema, with the balance pushed the other way: pictures carry the
    story and a spoken line is the exception.

    *scene_secs* is how long one take runs when the brief asked for a scene
    count (_acted_scene_plan); the budgets below are written to it, since a
    scene the writer fills to the wrong length is the one that truncates."""
    fmt = (fmt or "narration").strip().lower()
    if fmt not in ("dialogue", "mixed", "silent", "song"):
        return None
    # A song film's scenes are all PERFORMED silent takes — the schema below
    # must always ask for their cast, whatever the style's own toggle says.
    if fmt == "song":
        acted_silent = True
    # An acted take is bound by the model, not by the narrated scene window a
    # mixed film's plan carries: hold the asked-for length inside it.
    take_secs = min(max(float(scene_secs or 0) or performance_mode.scene_seconds(chained),
                        performance_mode.MIN_SCENE_SECONDS),
                    performance_mode.acted_limits(chained)[0])
    cast = ", ".join(n for n in cast_names if n)
    speakers = (
        f"Speakers are these existing characters ({cast}) and/or the main character(s) "
        "in the story. Do not invent speakers outside those."
        if cast else
        "The speakers are the story's recurring characters — identify them and use them "
        "consistently; a scene with nobody to speak must be \"narration\" or \"silent\"."
    )
    # A silent scene's length is AUTHORED (there are no words to count it from),
    # so the writer is given the window the renderer will actually hold it
    # inside — which chaining widens to the joined-clip take.
    silent_target = round(take_secs)
    silent_max = int(performance_mode.acted_limits(chained)[0])
    silent_budget = (
        f"A silent scene is ONE continuous take, so give it a \"seconds\" of about "
        f"{silent_target} (never below {int(performance_mode.MIN_SCENE_SECONDS)} or above "
        f"{silent_max}) and write a visual that holds for exactly that long"
        + (" — with the beats spread across the whole take rather than crowded into its "
           "opening seconds. " if chained else ". "))
    if fmt == "dialogue":
        balance = (
            "Almost every scene should be mode \"dialogue\": the characters carry the story by "
            "speaking to camera or to each other. Use \"narration\" only where no one could "
            "plausibly say it.")
    elif fmt == "silent":
        balance = (
            "Almost every scene should be mode \"silent\": the story is told in PICTURES — "
            "what happens on screen carries it, with no voice at all. Use \"dialogue\" only "
            "for the rare beat that genuinely turns on something said out loud (one or two "
            "in the whole film), and NEVER use \"narration\": this film has no narrator. "
            # The acted-silent schema below already states the budget; saying it
            # twice in one prompt is noise.
            + ("" if acted_silent else silent_budget))
    elif fmt == "song":
        balance = (
            "EVERY scene must be mode \"silent\": this film is a MUSIC VIDEO — one "
            "continuous song is laid over the whole film, so no scene carries a voice of "
            "its own. NEVER use \"narration\" and NEVER give any scene \"lines\": nothing "
            "said on camera survives the mix. Stage the scenes as the song's pictures: "
            "the lead performer SINGING to camera and performing — put them in \"cast\" in "
            "most scenes (the face that keeps returning is what makes it a music video) — "
            "with pure story imagery between the performance shots. Write each scene's "
            "\"beats\" as performance action (singing to camera, turning, walking, "
            "dancing, a look), spread across the whole take. Every scene's imagery comes "
            "STRAIGHT FROM THE SONG — what the lyrics sing about at that stretch of the "
            "track, or the performer singing it; nothing the song doesn't sing about.")
    else:
        balance = (
            "Mix deliberately: \"dialogue\" when characters speak or interact, \"narration\" for "
            "scene-setting voice-over, \"silent\" for a pure visual beat. A mixed film must "
            "actually MIX — acted dialogue scenes AND narrated scenes both appear, spread "
            "through the film rather than clustered; if every scene comes back the same mode "
            "and the direction did not ask for that, the division has failed the brief.")
    # The direction is the user's own instruction and outranks the balance above
    # in BOTH directions: a brief asking for a particular staging ("mostly
    # silent", "no narrator") used to lose to the must-mix rule, and the
    # narrator's own words used to be handed to a character.
    if fmt == "silent":
        instructions_rule = (
            "The TOPIC/DIRECTION outranks this balance whenever it speaks to staging. If it asks "
            "for specific words to be said, give them to a character in a \"dialogue\" scene — a "
            "silent film never adds a narrator.")
    elif fmt == "song":
        instructions_rule = (
            "The TOPIC/DIRECTION outranks this balance whenever it speaks to staging — but a "
            "music video never adds a narrator and never stages spoken lines: the song is the "
            "film's only voice.")
    else:
        instructions_rule = (
            "The TOPIC/DIRECTION outranks this mode balance whenever it speaks to staging: if it "
            "asks for mostly silent scenes, for no narrator, or for more spoken exchanges, stage "
            "the film the way it asks rather than the way the balance above describes. And when "
            "it asks the narrator to introduce themselves, address the viewer, or say specific "
            "things, stage those beats as \"narration\" scenes carrying exactly that content — "
            "never drop them and never reassign the narrator's own words to a character.")
    return (
        ("SILENT FILM — the story is told in PICTURES: no narrator reads it, and a character "
         "speaks only where a beat truly needs a line. "
         if fmt == "silent" else
         "MUSIC VIDEO — the story is told in PICTURES under one continuous song: nobody "
         "narrates and nobody speaks. "
         if fmt == "song" else
         "ACTED SCENES — the characters SPEAK ON CAMERA rather than only a narrator. ")
        + "Add a \"mode\" field to EVERY scene object: \"dialogue\" | \"narration\" | \"silent\". "
        "A \"dialogue\" scene also gets:\n"
        "  \"cast\": [names on screen, AT MOST 2 — a third face makes the model swap them],\n"
        "  \"lines\": ordered [{\"speaker\": <a cast name>, \"delivery\": <2-4 words, e.g. "
        "\"quiet, certain\">, \"text\": <ONE short spoken sentence>}],\n"
        "  \"setting\": one sentence — where this happens and what is around them,\n"
        "  \"camera\": one sentence — the whole scene is ONE continuous take, so describe a "
        "single shot and at most one move,\n"
        "  \"soundscape\": diegetic sound only (no score),\n"
        "and leaves \"narration\" EMPTY. "
        + (f"HARD BUDGET: the take runs about {round(take_secs)} seconds, so keep a dialogue "
           f"scene to AT MOST {max(1, round(take_secs / 3.2))} lines and "
           f"{max(6, round(take_secs * 2.25))} spoken words TOTAL — "
           + ("a real exchange, not a fragment — and split " if take_secs >= 15 else "split ")
           + "anything longer across consecutive scenes in the same setting rather than "
           "overfilling one. When you split one continuous exchange that way, mark each "
           "follow-on scene with \"continues_previous\": true — the renderer then continues "
           "the SAME take without a cut instead of re-staging the room. ")
        + ("A \"silent\" scene leaves narration empty (visuals only) and gets \"setting\", "
           "\"camera\" and \"soundscape\" exactly as a dialogue scene does — silent scenes "
           "are PERFORMED, not animated from a still. Whenever anyone is in shot, give it a "
           "\"cast\" too (AT MOST 2, from the same characters): their portraits are what keep "
           "a face the same as in the acted scenes. Leave the cast out for a beat with nobody "
           "in it — the scene still opens on its image_prompt. Nobody speaks in a silent "
           "scene: never give it \"lines\". " + silent_budget
           if acted_silent else
           "A \"silent\" scene leaves narration empty (visuals only) and may set \"seconds\". ")
        +
        f"{speakers} {balance} {instructions_rule} "
        "Still fill image_prompt and video_prompt as usual — for a dialogue scene they "
        "describe the setting the performance happens in."
    )


def _plan_for_generate(body: GenerateScriptBody, ss: dict) -> dict:
    """Cadence plan (word budget + 10–15 s scene caps) for a generation
    request: explicit minutes win, an explicit scene count pins the count,
    else the style's own length. The narrator considered is the request's
    voice override when set (else the style's) — cadence is per voice."""
    plan_ss = dict(ss)
    if (body.voice or "").strip():
        plan_ss["voice"] = body.voice.strip()
    try:
        minutes = float(body.minutes or 0)
    except (TypeError, ValueError):
        minutes = 0.0
    try:
        n = int(body.n_scenes or 0)
    except (TypeError, ValueError):
        n = 0
    return gapp.style_script_plan(plan_ss, minutes=minutes or None, n_scenes=n or None)


def _do_script_generate(body: GenerateScriptBody) -> dict:
    """Run the LLM script generation and persist a durable job (mirrors
    app.on_generate_script, minus the Gradio plumbing). Synchronous: the API runs
    it inside _run_script_task; tests call it directly."""
    # Keep the user's original direction separate from style extra_instructions
    # so Re-draft can restore the Create form without baked-in style text.
    user_topic = (body.topic or "").strip() or (body.video_title or "").strip()
    if not user_topic:
        raise HTTPException(400, "Enter a video title or describe what you want to create.")

    # Every script is story-first: draft and judge the prose, then divide it
    # into scenes in whatever mode the format asks for (narrated, acted, or a
    # mix). Chained inline so automation callers run it headless with no
    # review pause; the Create screen calls the two phases separately so the
    # story can be read and edited before it becomes scenes.
    sg = _do_story_generate(body)
    return _do_story_divide(DivideStoryBody(
        work_dir=sg["work_dir"], voice=body.voice,
        resolution=body.resolution,
        auto_approve=body.auto_approve, queue_item_id=body.queue_item_id,
        style_name=body.style_name, auto_critic=body.auto_critic))


def _portraits_in_background(work_dir: str, style_name: str, n: int) -> None:
    """Daemon-thread target: paint the new cast's look images.

    Tracked because it is a run of image generations on a worker — without it
    the Activity screen shows nothing while the GPU is busy on portraits."""
    try:
        with _track_op(f"Painting {n} character portrait{'' if n == 1 else 's'}",
                       Path(work_dir).name, work_dir=work_dir):
            gapp.generate_all_script_portraits(work_dir, style_name)
    except Exception:
        gapp.logger.warning("Background portrait generation failed for %s",
                            work_dir, exc_info=True)


def _persist_generated_script(body: GenerateScriptBody, cfg: dict, ss: dict,
                              user_topic: str, scenes, music_desc: str, style: str,
                              characters: list[dict],
                              work_dir: Path | None = None,
                              scene_plan: dict | None = None) -> dict:
    """Persist a freshly generated scene list and return the client payload
    (extracted from _do_script_generate so the story-mode divide step reuses the
    exact same persistence). work_dir targets an existing folder (story mode);
    None creates a fresh one from the title."""
    display_title = (body.video_title or "").strip() or user_topic
    if work_dir is None:
        work_dir = gapp._script_work_dir(display_title)
    job_id = job_id_from_work_dir(work_dir)
    # Bake the visual style prefix into each image_prompt so it's visible in the
    # scene editor and consistent even if the style profile is later renamed/edited.
    # The render step guards against re-adding a prefix that's already present.
    combined_style = gapp._compose_visual_style(style, cfg, ss["name"])
    scenes_list = [
        _scene_snapshot_row(s, image_prompt=(
            f"{combined_style}. {s.image_prompt}"
            if combined_style and s.image_prompt
            and not s.image_prompt.startswith(combined_style)
            else s.image_prompt))
        for s in scenes
    ]
    gapp._persist_script_snapshot(work_dir, scenes_list)

    # Persist only NEW cast members as per-script characters. Any LLM-identified
    # figure that already exists in the style's global catalogue is skipped so
    # the catalogue look/description is not shadowed by a fresh script entry
    # (see app._filter_identified_against_style / _job_characters).
    # Render look images in the background for the new ones; best-effort when
    # no worker is up (editor offers a manual "Generate look").
    new_characters = gapp._filter_identified_against_style(characters, cfg, ss["name"])
    # A story-divide fork copied the source's cast (its edited looks and
    # painted portraits) into this dir before the divide — keep those and add
    # only genuinely new names, so re-dividing never clobbers approved looks.
    existing_characters = gapp._read_script_characters(work_dir)
    if existing_characters:
        new_characters = [
            c for c in new_characters
            if not any(gapp._characters_refer_to_same(c, e)
                       for e in existing_characters)]
    # Cast voices: each new character gets a fitting library voice (gender/age
    # matched, the style narrator's voice excluded) so dialogue doesn't come out
    # with the narrator speaking every part. Assigned over the WHOLE cast so
    # the spread avoids voices a fork's copied characters already speak with
    # (already-voiced entries are untouched).
    saved_characters = gapp._write_script_characters(
        work_dir, gapp._auto_assign_character_voices(
            existing_characters + new_characters, cfg,
            exclude=(ss.get("voice") or "").strip()))
    # Paint looks only for the cast that still lacks one (a fork's copied
    # portraits are kept as-is; generate_all skips them anyway).
    to_paint = [c for c in saved_characters
                if c.get("description") and not c.get("ref_image")]
    if to_paint:
        threading.Thread(
            target=_portraits_in_background,
            args=(str(work_dir), ss["name"], len(to_paint)),
            daemon=True,
        ).start()

    create_brief = {
        "video_title": (body.video_title or "").strip(),
        "topic": user_topic,
        "minutes": (scene_plan or {}).get("minutes") or float(body.minutes or 0),
        "n_scenes": (scene_plan or {}).get("n_scenes") or len(scenes_list),
        "scene_plan": scene_plan,
        "visual_style": (body.visual_style or "").strip(),
        "voice": (body.voice or "").strip(),
        "resolution": (body.resolution or "").strip() or (ss.get("resolution") or gapp._DEFAULT_RESOLUTION),
        "style_name": body.style_name or ss["name"],
        "auto_approve": bool(body.auto_approve),
        "format": (body.format or "narration").strip().lower(),
        # A song film IS its song — music can't be opted out of.
        "music": True if (body.format or "").strip().lower() == "song" else (
            bool(ss.get("music_enabled", True)) if body.music is None else bool(body.music)),
    }
    _write_create_brief(work_dir, create_brief)

    store = DurableStore.default()
    try:
        store.create_or_update_job(
            job_id, work_dir, display_title,
            config={"title": display_title, "video_title": (body.video_title or "").strip(),
                    "topic": user_topic, "phase": "script_review", "style_name": ss["name"],
                    "create_brief": create_brief},
            metadata={"scene_count": len(scenes_list), "music_desc": music_desc, "style": style,
                      "music_enabled": create_brief["music"]},
        )
        store.upsert_scenes(job_id, scenes_list)
    finally:
        store.close()

    # Automation QC (youtube_auto_critic): run the critic now, BEFORE the queue
    # attach below can auto-start a render — its edits must land while nothing
    # is rendering. Best-effort: a critic failure never fails script creation.
    if body.auto_critic:
        p = gapp.automation_settings(cfg, ss["name"])["auto_critic_passes"]
        try:
            _do_critic_run(job_id, CriticRunBody(passes=p or 1, until_converged=(p == 0)))
            store = DurableStore.default()
            try:
                scenes_list = store.scene_rows(job_id)
            finally:
                store.close()
        except Exception:
            gapp.logger.warning("Auto-critic failed for %s — keeping the uncritiqued script",
                                job_id, exc_info=True)

    # Write the YouTube description alongside the fresh script (issue #66
    # follow-up): a daemon thread caches description.txt so the Cover and
    # Publish screens find it ready — no manual "Generate" click needed.
    threading.Thread(
        target=_describe_in_background,
        args=(str(work_dir), display_title),
        daemon=True,
    ).start()

    result = {
        "job_id": job_id,
        "work_dir": str(work_dir),
        "title": display_title,
        "video_title": (body.video_title or "").strip(),
        "topic": user_topic,
        "style": style,
        "style_name": ss["name"],
        "music_desc": music_desc,
        "voice": create_brief["voice"] or ss.get("voice", ""),
        "resolution": create_brief["resolution"],
        "create_brief": create_brief,
        "scenes": [_scene_to_json(s, work_dir) for s in scenes_list],
        "characters": _script_characters_payload(work_dir),
    }
    # Attach the script to the queue. Auto-approve enqueues a fresh slot (and may
    # auto-start a render). Editing a queued request (queue_item_id set) links the
    # script to that existing slot in place — even without auto-approve — so the
    # Queue then offers "Edit script" instead of "Create script". An in-place link
    # never auto-starts a render.
    if body.auto_approve or body.queue_item_id:
        queued = queue_from_job(FromJobBody(
            job_id=job_id,
            work_dir=str(work_dir),
            video_title=(body.video_title or display_title).strip(),
            minutes=float(create_brief.get("minutes") or 0),
            n_scenes=len(scenes_list),
            style=style,
            resolution=body.resolution or ss.get("resolution") or gapp._DEFAULT_RESOLUTION,
            voice=body.voice or ss.get("voice", ""),
            music_desc=music_desc,
            queue_item_id=body.queue_item_id,
            style_name=ss["name"],
            # Auto-approve means the user opted to render without review; an
            # in-place link from the Edit-script flow (queue_item_id only) just
            # attaches the script and leaves it unapproved for review.
            approved=bool(body.auto_approve),
        ))
        result.update({
            "auto_approved": bool(body.auto_approve),
            "queue_item_id": queued.get("queue_item_id", ""),
            "started": queued.get("started"),
        })
    return result


# ── Story-first: the one way a script is written ─────────────────────────────

class StoryChapterEdit(BaseModel):
    chapter: int
    text: str = ""


class DivideStoryBody(BaseModel):
    work_dir: str
    # Edited chapter texts from the Create review panel; [] keeps the draft as-is.
    chapters: list[StoryChapterEdit] = []
    voice: str = ""
    resolution: str = ""
    auto_approve: bool = False
    queue_item_id: str = ""
    style_name: str = ""
    # Automation only — see GenerateScriptBody.auto_critic.
    auto_critic: bool = False


def _story_path(wd: Path) -> Path:
    return Path(wd) / "story.json"


def _read_story(wd: Path) -> dict:
    p = _story_path(wd)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _merge_story_edits(story: dict, edits: list["StoryChapterEdit"]) -> None:
    """Fold edited chapter texts into the story dict in place (blank edits are
    ignored so a partial payload can't wipe a chapter)."""
    by_id = {c.get("chapter"): c for c in (story.get("chapters") or [])
             if isinstance(c, dict)}
    for edit in edits or []:
        target = by_id.get(edit.chapter)
        if target is not None and (edit.text or "").strip():
            target["text"] = edit.text.strip()


def _acted_scene_plan(body: GenerateScriptBody, ss: dict) -> tuple[int, float]:
    """(scene count, seconds per scene) for a film made of CLIPS — acted or
    silent.

    Not the narration cadence plan: these scenes are clips capped by the video
    model (~10 s each, or ~19 s where the style chains them), not a word budget.
    The requested length is divided by the requested scene count (the style's
    ``video_scenes`` when the brief doesn't say), so fewer scenes are longer
    takes — up to the threshold a take is split at, past which the LENGTH gives
    way rather than the clip truncating. With no count, the length alone sets
    how many one-take scenes it takes to fill."""
    chained = gapp._norm_h3_chain_scenes(ss.get("h3_chain_scenes"))
    secs = performance_mode.scene_seconds(chained)
    try:
        n = int(body.n_scenes or 0)
    except (TypeError, ValueError):
        n = 0
    n = n or gapp.style_video_scenes(ss)
    try:
        minutes = float(body.minutes or 0)
    except (TypeError, ValueError):
        minutes = 0.0
    minutes = minutes if minutes > 0 else gapp.style_video_minutes(ss)
    if n > 0:
        n = min(gapp.MAX_SCENES, n)
        ceiling = performance_mode.acted_limits(chained)[0]
        secs = min(max(minutes * 60.0 / n, performance_mode.MIN_SCENE_SECONDS), ceiling)
        return n, secs
    return max(1, min(gapp.MAX_SCENES, round(minutes * 60.0 / secs))), secs


def _do_story_generate(body: GenerateScriptBody) -> dict:
    """Story-mode phase 1: draft, judge, and revise the prose story, persist it
    as story.json (phase "story_review"), and return it for the Create review
    panel. _do_story_divide turns it into scenes."""
    user_topic = (body.topic or "").strip() or (body.video_title or "").strip()
    if not user_topic:
        raise HTTPException(400, "Enter a video title or describe what you want to create.")

    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, body.style_name)
    extra = (ss.get("extra_instructions") or "").strip()
    llm_topic = f"{user_topic}\n\n{extra}" if extra else user_topic
    style_hint = body.visual_style or ss.get("visual_style", "") or None
    avoid_hint = (ss.get("script_avoid") or "").strip() or None
    # Only the catalogue characters the brief NAMES — the story invents its own
    # cast otherwise (gapp._requested_characters).
    draft_chars = gapp._requested_characters(
        cfg, body.style_name, user_topic, body.video_title, extra)
    display_topic = (body.video_title or "").strip() or user_topic.splitlines()[0][:80]
    fmt = (body.format or "narration").strip().lower()
    # Song-first: the film's song may already exist (written and generated via
    # /api/song/draft + /api/song/generate). Resolved up front because the
    # TRACK's real duration is what the scene plan divides.
    song_wd: Path | None = None
    song_data: dict = {}
    if fmt == "song" and (body.work_dir or "").strip():
        cand = Path(body.work_dir)
        if _safe_under(cand, gapp.OUTPUT_DIR) and (cand / "song.json").exists():
            song_wd = cand
            try:
                song_data = json.loads((cand / "song.json").read_text())
            except Exception:
                song_data = {}
    # The song's cast singer joins the sheet even when the brief never names
    # them — the story has to plan its beats around this one performer.
    singer_char = gapp._catalogue_character_named(
        cfg, ss["name"], song_data.get("singer") or "")
    if singer_char is not None and singer_char not in draft_chars:
        draft_chars = [*draft_chars, singer_char]
    character_sheet = gapp._character_sheet(draft_chars) or None
    plan = _plan_for_generate(body, ss)
    if fmt in ("dialogue", "silent", "song"):
        # Every scene is one clip — acted, a silent beat nobody narrates, or a
        # song film's performed take — so the length comes from clip count,
        # not a narrator's word budget.
        acted_n, acted_secs = _acted_scene_plan(body, ss)
        if fmt == "song":
            # The film follows the SONG: its generated duration (else the
            # asked-for length) divided into clips of about clip_secs each.
            total = 0.0
            track = (song_wd / "background_music.wav") if song_wd else None
            if track is not None and track.exists():
                try:
                    from pipeline.assembler import _get_duration
                    total = float(_get_duration(track) or 0)
                except Exception:
                    total = 0.0
            if total <= 0:
                total = acted_n * acted_secs
            # The film runs the SONG's length, so the only division is how many
            # scenes to split it into (the Song tab's "Scenes"). An explicit
            # count wins; clip_secs is the older way of saying the same thing;
            # neither given means AUTO — takes of about SONG_SCENE_SECONDS.
            try:
                want_n = int(body.n_scenes or 0)
            except (TypeError, ValueError):
                want_n = 0
            clip = float(body.clip_secs or 0)
            if want_n > 0:
                acted_n = min(gapp.MAX_SCENES, want_n)
            else:
                if clip <= 0:
                    clip = SONG_SCENE_SECONDS
                clip = min(max(clip, float(performance_mode.MIN_SCENE_SECONDS)),
                           performance_mode.H3_CEILING_SECONDS)
                acted_n = max(1, round(total / clip))
            acted_secs = total / max(1, acted_n)
        plan = {**plan, "n_scenes": acted_n, "scene_secs_target": acted_secs,
                "minutes": round(acted_n * acted_secs / 60.0, 2)}
    # The draft only learns WHO tells the story (acted / mixed); the acted
    # scene schema and clip budgets bind at the divide step, so the prose
    # comes out well-formed whatever the film's length.
    dialogue_note = _story_format_note(fmt)
    # Song-first: when the film's song already exists, the story is drafted
    # FROM its lyrics — the song leads, the pictures follow.
    if song_wd is not None and (song_data.get("lyrics") or "").strip():
        dialogue_note = (
            (dialogue_note or "") +
            "\nTHE FILM'S SONG IS ALREADY WRITTEN AND APPROVED — this story is "
            "the VIDEO for it, nothing more. Stay entirely inside the song's "
            "world: its subject, mood and images ARE the film's, and every beat "
            "must be something the lyrics sing or directly show. Keep it SIMPLE "
            "and visual — a music video is pictures following a song, not a "
            "plot. No subplots, no extra characters, no backstory, no twist the "
            "song doesn't sing about; the same few images the song repeats are "
            "what the camera returns to. These are the lyrics:\n"
            + song_data["lyrics"])
    if fmt == "song":
        dialogue_note = (dialogue_note or "") + _song_singer_story_note(song_data)
    try:
        with _track_op("Drafting story", display_topic):
            story = story_mode.generate_story(
                llm_topic, plan["n_scenes"], style_hint=style_hint,
                video_title=(body.video_title or "").strip() or None,
                character_sheet=character_sheet, avoid_hint=avoid_hint,
                scene_plan=plan, dialogue_note=dialogue_note,
            )
    except Exception as e:  # surface a clean message to the client
        raise HTTPException(500, f"Story generation failed: {str(e).splitlines()[0][:300]}")

    display_title = (body.video_title or "").strip() or user_topic
    work_dir = song_wd if song_wd is not None else gapp._script_work_dir(display_title)
    job_id = job_id_from_work_dir(work_dir)
    _story_path(work_dir).write_text(json.dumps(story, indent=2))
    create_brief = {
        "video_title": (body.video_title or "").strip(),
        "topic": user_topic,
        "minutes": plan["minutes"],
        "n_scenes": plan["n_scenes"],
        "scene_plan": plan,
        "visual_style": (body.visual_style or "").strip(),
        "voice": (body.voice or "").strip(),
        "resolution": (body.resolution or "").strip() or (ss.get("resolution") or gapp._DEFAULT_RESOLUTION),
        "style_name": body.style_name or ss["name"],
        "auto_approve": bool(body.auto_approve),
        "format": fmt,
        # Carried, not re-derived: a music video's brief was written by
        # /api/song/draft before this story existed, and the queue slot it
        # belongs to is only recorded there.
        "queue_item_id": ((body.queue_item_id or "").strip()
                          or str(_read_create_brief(work_dir).get("queue_item_id") or "")),
        # A song film IS its song — music can't be opted out of.
        "music": True if fmt == "song" else (
            bool(ss.get("music_enabled", True)) if body.music is None else bool(body.music)),
    }
    _write_create_brief(work_dir, create_brief)
    store = DurableStore.default()
    try:
        store.create_or_update_job(
            job_id, work_dir, display_title,
            config={"title": display_title, "video_title": (body.video_title or "").strip(),
                    "topic": user_topic, "phase": "story_review", "style_name": ss["name"],
                    "create_brief": create_brief},
            metadata={"scene_count": 0, "music_desc": story.get("music", ""),
                      "style": story.get("style", ""),
                      "music_enabled": create_brief["music"]},
        )
    finally:
        store.close()
    # Shaped like the classic generate payload (scenes empty until division) so
    # the Create screen can hand straight off to the Script screen's Story view.
    return {"job_id": job_id, "work_dir": str(work_dir), "title": display_title,
            "video_title": (body.video_title or "").strip(), "topic": user_topic,
            "style": story.get("style", ""), "style_name": ss["name"],
            "music_desc": story.get("music", ""),
            "voice": create_brief["voice"] or ss.get("voice", ""),
            "resolution": create_brief["resolution"],
            "n_scenes": plan["n_scenes"],
            "story": story, "create_brief": create_brief,
            "scenes": [], "characters": []}


def _copy_song_artifacts(src: Path, dst: Path) -> None:
    """Carry a song film's approved song from *src* into the fork *dst*.

    A music video IS its song: the lyrics and caption in song.json, the track
    every singing take pins its own window of, and the kept versions the Song
    tab picks between. A fork that leaves them behind is no longer recognised
    as a music video (see _is_music_video) and its re-render writes a brand-new
    instrumental — with no lyrics — over scenes timed to the old track."""
    import shutil
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("song.json", "background_music.wav", "music_history.json"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    if (src / "music_history").is_dir():
        shutil.copytree(src / "music_history", dst / "music_history",
                        dirs_exist_ok=True)


def _copy_script_reference_files(src: Path, dst: Path, *,
                                 keep_scene_scope: bool) -> None:
    """Carry a script's reference anchors into a copy: the per-script cast
    (characters.json + its look images) and the visuals wall — locations,
    wardrobe, image/video/audio references — with their files. Without them
    the copy's re-render shoots H3 takes with no location, wardrobe or cast
    anchors (a music-video fork was seen losing its studio and wardrobe refs).

    keep_scene_scope: a duplicate copies the scenes verbatim, so a visual's
    scene scoping still names the same beats and is kept. A story-divide fork
    RE-DIVIDES the story — its new scene ids need not line up with the old —
    so scene-scoped visuals are widened to every scene (empty scenes list):
    the reference stays alive film-wide for the user to re-scope, instead of
    silently pinning to whatever scenes now wear the old ids. (Wardrobe stays
    safe widened: it only rides scenes that cast its owner.)"""
    import shutil
    dst.mkdir(parents=True, exist_ok=True)
    if (src / "characters.json").exists():
        shutil.copy2(src / "characters.json", dst / "characters.json")
    src_chars = gapp._script_characters_dir(src)
    if src_chars.is_dir():
        shutil.copytree(src_chars, gapp._script_characters_dir(dst),
                        dirs_exist_ok=True)
    visuals = gapp.read_script_visuals(src)
    if visuals:
        if not keep_scene_scope:
            for v in visuals:
                v["scenes"] = []
        gapp.write_script_visuals(dst, visuals)
    src_vis = gapp._script_visuals_dir(src)
    if src_vis.is_dir():
        shutil.copytree(src_vis, gapp._script_visuals_dir(dst),
                        dirs_exist_ok=True)


def _do_story_divide(body: DivideStoryBody) -> dict:
    """Story-mode phase 2: divide the (possibly user-edited) story into scenes
    and persist the script through the exact classic path, into the same work
    dir the draft lives in."""
    if not (body.work_dir or "").strip():
        raise HTTPException(400, "Choose a story draft to divide.")
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Story path is outside the output folder.")
    story = _read_story(wd)
    if not story:
        raise HTTPException(404, "No story draft found in the selected folder.")
    _merge_story_edits(story, body.chapters)

    brief = _read_create_brief(wd)
    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, body.style_name or brief.get("style_name", ""))
    user_topic = (brief.get("topic") or story.get("topic") or "").strip() or wd.name
    video_title = (brief.get("video_title") or story.get("video_title") or "").strip()

    # Dividing a story that ALREADY has scenes forks into a fresh work dir, so
    # the existing script — its scene edits, previews and history — is kept
    # intact and the edited story becomes a new script alongside it.
    if (wd / "script.json").exists():
        src = wd
        wd = gapp._script_work_dir(video_title or user_topic)
        gapp.logger.info("Story divide: %s already has scenes — forking to %s", src, wd)
        # A song film's approved song travels with the fork — it IS the film —
        # and so do its kept versions, so the fork can still put the original
        # generation back after a re-voicing.
        _copy_song_artifacts(src, wd)
        # So do the script's reference anchors (cast looks, locations,
        # wardrobe, uploaded references) — scene scoping is widened because
        # the re-divide renumbers scenes.
        _copy_script_reference_files(src, wd, keep_scene_scope=False)
    style_hint = brief.get("visual_style") or ss.get("visual_style", "") or None
    video_style_hint = ss.get("video_style", "") or None
    avoid_hint = (ss.get("script_avoid") or "").strip() or None
    # The same brief-named subset the draft was given, so the scenes stage the
    # story's own cast rather than the whole library (gapp._requested_characters).
    requested_chars = gapp._requested_characters(
        cfg, ss["name"], user_topic, video_title, (ss.get("extra_instructions") or ""))
    character_sheet = gapp._character_sheet(requested_chars) or None
    language = gapp._norm_tts_language(ss.get("tts_language"))
    display_topic = video_title or user_topic.splitlines()[0][:80]
    # The format decides how the story is STAGED: narrated voice-over, acted
    # scenes the characters speak, or a mix of both (and silent beats).
    fmt = (brief.get("format") or "narration").strip().lower()
    # The take length the draft was planned to (an explicit scene count divides
    # the film's length into longer or shorter scenes) so the acted budgets the
    # writer works to match the clips the renderer will actually shoot.
    plan = story.get("scene_plan") or brief.get("scene_plan") or {}
    # A song film's cast singer: read before the divide so the scene prompts
    # cast them by name, in their per-video outfit, in every singing scene.
    song_data: dict = {}
    if fmt == "song" and (wd / "song.json").exists():
        try:
            song_data = json.loads((wd / "song.json").read_text())
        except Exception:
            song_data = {}
        singer_char = gapp._catalogue_character_named(
            cfg, ss["name"], song_data.get("singer") or "")
        if singer_char is not None and singer_char not in requested_chars:
            requested_chars = [*requested_chars, singer_char]
            character_sheet = gapp._character_sheet(requested_chars) or None
    dialogue_note = _build_dialogue_note(
        fmt, [c.get("name", "") for c in requested_chars],
        chained=gapp._norm_h3_chain_scenes(ss.get("h3_chain_scenes")),
        acted_silent=gapp._norm_h3_silent_scenes(ss.get("h3_silent_scenes")),
        scene_secs=plan.get("scene_secs_target") if isinstance(plan, dict) else None)
    if fmt == "song":
        dialogue_note = (dialogue_note or "") + _song_singer_story_note(song_data)
    try:
        with _track_op("Dividing story into scenes", display_topic):
            scenes, music_desc, style, characters = story_mode.divide_story(
                story, style_hint=style_hint, video_title=video_title or None,
                video_style_hint=video_style_hint, character_sheet=character_sheet,
                avoid_hint=avoid_hint, language=language, dialogue_note=dialogue_note,
            )
    except HTTPException:
        raise
    except Exception as e:  # surface a clean message to the client
        raise HTTPException(500, f"Story division failed: {str(e).splitlines()[0][:300]}")

    if fmt == "song":
        # A song film: stamp the performance flag onto every silent scene (it
        # rides scene metadata through the editor and every re-render). The
        # song normally already exists — the song-FIRST flow wrote and
        # generated it before the story was drafted — and is only written
        # here as a fallback for headless runs that skipped that step.
        story_mode.mark_singing(scenes)
        secs = 0.0
        if isinstance(plan, dict):
            try:
                secs = float(plan.get("minutes") or 0) * 60.0
            except (TypeError, ValueError):
                secs = 0.0
        if secs <= 0:
            secs = len(scenes) * performance_mode.SCENE_SECONDS
        song = None
        if (wd / "song.json").exists():
            try:
                song = json.loads((wd / "song.json").read_text())
            except Exception:
                song = None
        if not (song and (song.get("lyrics") or "").strip()):
            singer, singer_desc = _pick_song_singer(
                cfg, ss, user_topic, video_title,
                (ss.get("extra_instructions") or ""))
            try:
                with _track_op("Writing the song", display_topic):
                    song = story_mode.write_song(story, secs, language=language,
                                                 singer_note=singer_desc)
            except Exception as e:
                raise HTTPException(500, f"Song writing failed: {str(e).splitlines()[0][:300]}")
            (wd / "song.json").write_text(json.dumps(
                {**song, "singer": singer, "created_at": time.time()}, indent=2))
        # Each singing scene gets its WINDOW of the song and the words sung in
        # it — timed against the real generated track when one exists.
        track_secs = None
        track = wd / "background_music.wav"
        if track.exists():
            try:
                from pipeline.assembler import _get_duration
                track_secs = _get_duration(track) or None
            except Exception:
                track_secs = None
        # Tracked: the first divide against a track separates its vocal stem
        # (demucs) and, with the option on, whisper-aligns the lyric lines to
        # it — a couple of minutes worth showing in Activity.
        with _track_op("Timing the song's lyrics", display_topic):
            story_mode.assign_song_slices(
                scenes, song.get("lyrics") or "",
                total_seconds=track_secs or secs,
                track=track if track.exists() else None,
                align_lyrics=gapp._norm_song_align_lyrics(
                    ss.get("song_align_lyrics")),
                language=language)
        # The caption is the film's music description from here on — Remix
        # shows and edits it, and the render appends the singer's voice.
        music_desc = song.get("caption") or music_desc

    story["status"] = "divided"
    story["updated_at"] = time.time()
    _story_path(wd).write_text(json.dumps(story, indent=2))
    gen_body = GenerateScriptBody(
        video_title=video_title,
        topic=user_topic,
        n_scenes=int(story.get("n_scenes") or len(scenes)),
        visual_style=(brief.get("visual_style") or "").strip(),
        auto_approve=bool(body.auto_approve),
        voice=(body.voice or brief.get("voice") or "").strip(),
        resolution=(body.resolution or brief.get("resolution") or "").strip(),
        # The brief's slot is the fallback: a music video automation parked for
        # review is continued from the Song tab, which knows the work dir but
        # not the queue item — without this the finished script would open a
        # second slot instead of filling the one that is waiting for it.
        queue_item_id=(body.queue_item_id
                       or str(brief.get("queue_item_id") or "")),
        style_name=ss["name"],
        format=fmt,
        auto_critic=body.auto_critic,
    )
    return _persist_generated_script(gen_body, cfg, ss, user_topic,
                                     scenes, music_desc, style, characters,
                                     work_dir=wd, scene_plan=story.get("scene_plan"))


def _run_story_generate_task(task_id: str, body: "GenerateScriptBody") -> None:
    try:
        _script_tasks[task_id] = {"status": "done", "result": _do_story_generate(body)}
    except HTTPException as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e.detail)[:300]}
    except Exception as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:300]}


def _run_story_divide_task(task_id: str, body: "DivideStoryBody") -> None:
    try:
        _script_tasks[task_id] = {"status": "done", "result": _do_story_divide(body)}
    except HTTPException as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e.detail)[:300]}
    except Exception as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:300]}


@api.post("/api/script/story/generate")
def story_generate(body: GenerateScriptBody) -> dict:
    """Kick off the story draft (story-mode phase 1) in the background; poll
    /api/script/generate/status with the returned task id."""
    topic = (body.topic or "").strip() or (body.video_title or "").strip()
    if not topic:
        raise HTTPException(400, "Enter a video title or describe what you want to create.")
    task_id = uuid.uuid4().hex[:12]
    _script_tasks[task_id] = {"status": "running"}
    threading.Thread(target=_run_story_generate_task, args=(task_id, body), daemon=True).start()
    return {"task_id": task_id}


@api.post("/api/script/story/divide")
def story_divide(body: DivideStoryBody) -> dict:
    """Divide a reviewed story draft into scenes (story-mode phase 2) in the
    background; poll /api/script/generate/status with the returned task id."""
    task_id = uuid.uuid4().hex[:12]
    _script_tasks[task_id] = {"status": "running"}
    threading.Thread(target=_run_story_divide_task, args=(task_id, body), daemon=True).start()
    return {"task_id": task_id}


class SongDraftBody(BaseModel):
    video_title: str = ""
    topic: str = ""
    minutes: float = 0
    style_name: str = ""
    voice: str = ""          # the SINGING voice (library name); "" = model's pick
    n_scenes: int = 0        # how many scenes to split the song into; 0 = Auto
    # The queue slot this song belongs to, when automation drafted it. Kept in
    # the create brief so the script written from the song later links back to
    # that slot rather than opening a second one.
    queue_item_id: str = ""


def _pick_song_singer(cfg: dict, ss: dict, *texts: str) -> tuple[str, str]:
    """Cast the film's LEAD SINGER from the style's character catalogue.

    Returns ``(name, descriptor)`` — the character's name and their vocalist
    description (sex, age, background, plus their library voice's tone/accent)
    — or ``("", "")`` when the style has no usable catalogue character. The
    descriptor is what makes the sung voice match the person on camera: it is
    appended to the music caption when the track is generated, and the story
    prompts cast the named character in the singing scenes."""
    char = gapp.pick_song_singer(cfg, ss["name"], *texts)
    if not char:
        return "", ""
    desc = gapp.singer_descriptor(char, cfg)
    if desc:
        gapp.logger.info("Song singer: cast %r from style %r catalogue (%s)",
                         char.get("name"), ss["name"], desc)
    return str(char.get("name") or ""), desc


def _song_singer_story_note(song_data: dict) -> str:
    """The lead-singer + wardrobe instruction a song film's story prompts get.

    Whoever was cast (a catalogue character by name, else the song's own
    vocalist description) must be the person the film SHOWS singing — sex and
    age on camera matching the sung voice — and each video dresses them fresh
    rather than repeating the catalogue look."""
    name = (song_data.get("singer") or "").strip()
    desc = (song_data.get("vocalist") or "").strip()
    if not (name or desc):
        return ""
    if name:
        who = (f"THE LEAD SINGER IS {name}" + (f" ({desc})" if desc else "")
               + " — an existing character: cast them BY NAME")
    else:
        who = (f"THE LEAD SINGER IS: {desc} — invent this one performer "
               "(give them a name) and cast them")
    return (
        f"\n{who} as the film's one lead performer. The person shown singing "
        "in EVERY performance shot must be this singer — their sex and age on "
        "camera must match that description, because the sung voice on the "
        "track is theirs; never show anyone else mouthing the song.\n"
        "WARDROBE: dress the lead singer in ONE distinctive outfit chosen "
        "fresh for THIS video — name it in the scene prompts (a change of "
        "clothes is the one thing you may describe on a named character) and "
        "keep it identical in every scene; do not fall back to their usual "
        "look, and pick something a different video would not pick.")


def _register_song_job(wd: Path, *, title: str, video_title: str, topic: str,
                       minutes: float, n_scenes: int, voice: str, ss: dict,
                       caption: str, queue_item_id: str) -> tuple[str, dict]:
    """Register a song-FIRST job — one that exists BEFORE any story or scenes —
    so the Script screen's Song tab (the song studio) can load it and carry the
    flow from there. Returns ``(job_id, create_brief)``.

    Shared by both ways in: the model writing the song (/api/song/draft) and the
    user supplying one (/api/song/import). Whichever wrote it, what lands is the
    same kind of job."""
    create_brief = {
        "video_title": video_title, "topic": topic, "minutes": minutes,
        # The scene count asked for at Create (0 = Auto) — the Song tab's
        # Scenes control opens on it rather than asking again.
        "n_scenes": max(0, int(n_scenes or 0)), "visual_style": "",
        "voice": voice, "resolution": ss.get("resolution") or "",
        "style_name": ss["name"], "auto_approve": False,
        "format": "song", "music": True,
        "queue_item_id": (queue_item_id or "").strip(),
    }
    _write_create_brief(wd, create_brief)
    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        store.create_or_update_job(
            job_id, wd, title,
            config={"title": title, "video_title": video_title,
                    "topic": topic, "phase": "song_review", "style_name": ss["name"],
                    "create_brief": create_brief},
            metadata={"scene_count": 0, "music_desc": caption,
                      "music_enabled": True})
    finally:
        store.close()
    return job_id, create_brief


@api.post("/api/song/draft")
def song_draft(body: SongDraftBody) -> dict:
    """Song-FIRST step of the Music-video flow: write the film's song from the
    brief alone — caption + tagged lyrics — into a fresh work dir. Generate the
    track with /api/song/generate, iterate, and only then draft the story from
    the approved lyrics (story/generate with this work_dir)."""
    title = (body.video_title or "").strip() or (body.topic or "").strip()
    if not title:
        raise HTTPException(400, "Enter a video title or describe the song.")
    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, body.style_name)
    minutes = float(body.minutes or 0) or gapp.style_video_minutes(ss)
    secs = max(15.0, minutes * 60.0)
    extra = (ss.get("extra_instructions") or "").strip()
    topic = (body.topic or "").strip() or title
    display_topic = title.splitlines()[0][:80]
    # The lead singer is cast BEFORE the song is written: the track is sung
    # from this draft's caption + vocalist and reused verbatim at render, so
    # who sings has to be settled here — not discovered from the cast later,
    # when the vocals are already on disk.
    singer, singer_desc = _pick_song_singer(cfg, ss, topic,
                                            (body.video_title or ""), extra)
    try:
        with _track_op("Writing the song", display_topic):
            song = story_mode.write_song(
                None, secs,
                language=gapp._norm_tts_language(ss.get("tts_language")),
                topic=f"{topic}\n\n{extra}" if extra else topic,
                video_title=(body.video_title or "").strip(),
                singer_note=singer_desc)
    except Exception as e:
        raise HTTPException(500, f"Song writing failed: {str(e).splitlines()[0][:300]}")
    wd = gapp._script_work_dir(title)
    song.update({"voice": (body.voice or "").strip(), "seconds": secs,
                 "title": title, "style_name": ss["name"], "singer": singer,
                 "created_at": time.time()})
    (wd / "song.json").write_text(json.dumps(song, indent=2))
    job_id, create_brief = _register_song_job(
        wd, title=title, video_title=(body.video_title or "").strip(), topic=topic,
        minutes=minutes, n_scenes=body.n_scenes, voice=(body.voice or "").strip(),
        ss=ss, caption=song["caption"], queue_item_id=body.queue_item_id)
    return {"work_dir": str(wd), "job_id": job_id, "title": title,
            "video_title": (body.video_title or "").strip(), "topic": topic,
            "style": "", "style_name": ss["name"], "music_desc": song["caption"],
            "voice": (body.voice or "").strip() or ss.get("voice", ""),
            "resolution": ss.get("resolution") or "", "n_scenes": 0,
            "create_brief": create_brief, "scenes": [],
            "caption": song["caption"], "lyrics": song["lyrics"],
            "song_voice": song["voice"], "seconds": secs}


# Most an uploaded song may weigh. A five-minute stereo WAV is around 50 MB, so
# this is roomy for a real song while still refusing, say, a video dropped in by
# mistake.
_MAX_SONG_UPLOAD_BYTES = 80 * 1024 * 1024


def _import_song_file(wd: Path, data: str, filename: str) -> dict:
    """Make an audio file the user supplied the film's song.

    The file is re-encoded into ``background_music.wav`` — the one track the
    whole music-video pipeline hangs off — and kept as a version like any
    generation. Anything already there is captured first, so uploading over a
    generated take doesn't lose it and either can be put back from the list.

    The song's real duration replaces the asked-for length: an uploaded song
    IS the film's length, and it is what the scenes are divided out of."""
    from pipeline.assembler import _get_duration, transcode_to_wav

    raw, ext = _decode_audio(data, filename)
    if len(raw) > _MAX_SONG_UPLOAD_BYTES:
        raise HTTPException(413, f"That file is over {_MAX_SONG_UPLOAD_BYTES // (1024 * 1024)} MB — "
                                 "upload an mp3 or a shorter track.")
    try:
        song = json.loads((wd / "song.json").read_text())
    except Exception:
        song = {}
    track = wd / "background_music.wav"
    try:
        music_history.seed_if_empty(wd, track, song.get("caption") or "")
    except Exception:
        gapp.logger.warning("Could not seed the song into history", exc_info=True)
    upload = wd / f"uploaded_song{ext}"
    upload.write_bytes(raw)
    staged = wd / "background_music.staging.wav"
    try:
        transcode_to_wav(upload, staged)
    except Exception as e:
        raise HTTPException(400, f"That file could not be read as audio: {str(e).splitlines()[0][:200]}")
    finally:
        upload.unlink(missing_ok=True)
    staged.replace(track)
    dur = _get_duration(track)
    name = Path(filename or "").name or "a file"
    try:
        music_history.record(wd, track, f"uploaded — {name}")
    except Exception:
        gapp.logger.warning("Could not record the uploaded song", exc_info=True)
    # An uploaded track is nobody's re-voicing: a stale "sung as X" would label
    # this file with a conversion it never had.
    song.update({"duration": dur, "seconds": round(dur, 1), "sung_as": "",
                 "uploaded_from": name, "updated_at": time.time()})
    (wd / "song.json").write_text(json.dumps(song, indent=2))
    return {"ok": True, "duration": dur,
            "song_url": f"/api/file?path={track}&t={int(time.time())}",
            **{k: music_history.history(wd).get(k) for k in ("versions", "selected")}}


class SongUploadBody(BaseModel):
    work_dir: str
    filename: str = ""
    data: str          # base64 (optionally a data URL)


@api.post("/api/song/upload")
def song_upload(body: SongUploadBody) -> dict:
    """Replace this film's song with an audio file from the user's machine."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    if not (wd / "song.json").exists():
        raise HTTPException(404, "This film has no song.")
    return _import_song_file(wd, body.data, body.filename)


class SongImportBody(BaseModel):
    video_title: str = ""
    topic: str = ""
    style_name: str = ""
    n_scenes: int = 0
    voice: str = ""
    lyrics: str = ""     # what the file sings, if the user has the words
    caption: str = ""
    filename: str = ""
    data: str
    queue_item_id: str = ""


@api.post("/api/song/import")
def song_import(body: SongImportBody) -> dict:
    """The other way into the Music-video flow: the song already exists as a
    file, so nothing is written or generated — the upload becomes the film's
    track, and the story is drafted from it (and from whatever lyrics the user
    pastes in the Song tab) exactly as a generated song would be."""
    title = (body.video_title or "").strip() or (body.topic or "").strip() \
        or Path(body.filename or "").stem.strip()
    if not title:
        raise HTTPException(400, "Enter a video title for the film.")
    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, body.style_name)
    wd = gapp._script_work_dir(title)
    topic = (body.topic or "").strip() or title
    # An uploaded track's vocals are already baked in, but the film still needs
    # a lead singer for the VISUALS — cast one so the story shows a consistent
    # performer (with luck, one matching what the file sings).
    singer, singer_desc = _pick_song_singer(cfg, ss, topic,
                                            (body.video_title or ""))
    (wd / "song.json").write_text(json.dumps(
        {"caption": (body.caption or "").strip(), "lyrics": (body.lyrics or "").strip(),
         "voice": (body.voice or "").strip(), "title": title,
         "singer": singer, "vocalist": singer_desc,
         "style_name": ss["name"], "created_at": time.time()}, indent=2))
    result = _import_song_file(wd, body.data, body.filename)
    song = json.loads((wd / "song.json").read_text())
    job_id, create_brief = _register_song_job(
        wd, title=title, video_title=(body.video_title or "").strip(), topic=topic,
        minutes=round(float(result["duration"]) / 60.0, 3), n_scenes=body.n_scenes,
        voice=(body.voice or "").strip(), ss=ss, caption=song.get("caption") or "",
        queue_item_id=body.queue_item_id)
    return {"work_dir": str(wd), "job_id": job_id, "title": title,
            "video_title": (body.video_title or "").strip(), "topic": topic,
            "style": "", "style_name": ss["name"],
            "music_desc": song.get("caption") or "",
            "voice": (body.voice or "").strip() or ss.get("voice", ""),
            "resolution": ss.get("resolution") or "", "n_scenes": 0,
            "create_brief": create_brief, "scenes": [],
            "caption": song.get("caption") or "", "lyrics": song.get("lyrics") or "",
            "song_voice": song.get("voice") or "", "seconds": result["duration"]}


class SongGenerateBody(BaseModel):
    work_dir: str
    caption: str = ""
    lyrics: str = ""
    voice: str = ""
    # The vocalist line as the studio shows it — saved before the render so an
    # unsaved edit is what the track is sung as. None = keep the stored one.
    vocalist: str | None = None
    # "Re-generate X seconds longer": added to the song's current length before
    # it is sung again, so the model has room to land an ending it was cutting
    # off. 0 = generate at the length it already has.
    add_seconds: float = 0


# Most an ending may be stretched by in one go — either as a padded tail or as
# a longer re-generation.
_MAX_SONG_EXTEND = 30.0


def _do_song_generate(wd: Path, add_seconds: float = 0.0) -> dict:
    """Render the song itself (song.json → background_music.wav) on a worker.

    The finished track IS the film's final soundtrack: the render's music step
    sees the file already present and reuses it verbatim, so what was approved
    here is exactly what plays under the film. Each generation is kept in the
    music history for comparison.

    *add_seconds* re-generates the take marked "In use" that much longer — a
    repaint extend: the current audio survives verbatim and only the added tail
    is generated, so the song stays the same. That needs the engine's extend
    graph on the worker (ACE-Step + the AudioLatentExtendMask node); without
    it this falls back to a fresh full-length take, flagged ``extended: False``
    so the UI can say which one happened."""
    from pipeline.assembler import _get_duration, _resolve_media_tool
    from pipeline.comfyui import generate_music, music_engine_can_extend
    from pipeline.worker_pool import WorkerPool

    cfg = gapp.load_config()
    data = json.loads((wd / "song.json").read_text())
    secs = float(data.get("seconds") or 60)
    voices = {v.get("name"): v for v in (cfg.get("voices") or []) if v.get("name")}
    # Who sings: an explicitly picked library voice wins (it is what a re-voice
    # converts to), else the vocalist cast at draft time — the lead singer's
    # description, or the songwriter's own. A track sung with NO vocalist line
    # is the one whose voice never matches the person shown singing.
    vocal = (gapp.voice_descriptor(voices.get((data.get("voice") or "").strip()))
             or (data.get("vocalist") or "").strip())
    caption = ", ".join(x for x in ((data.get("caption") or "").strip(), vocal) if x)
    ss = gapp.style_settings(cfg, data.get("style_name") or "")
    engine = gapp._norm_music_engine(ss.get("music_engine"))
    urls = gapp._preview_worker_urls()
    if not urls:
        raise RuntimeError("No ComfyUI workers reachable.")
    final = wd / "background_music.wav"
    keep = 0.0
    if add_seconds > 0 and final.exists():
        # Longer than what the user is HEARING — the take in use is the
        # canonical file (select() keeps them in sync), so measure it rather
        # than trusting song.json's last-generation fields.
        keep = float(_get_duration(final) or 0.0)
        if keep > 0:
            secs = round(keep + float(add_seconds), 1)
    pool = WorkerPool(urls)
    staged = wd / "background_music.staging.wav"
    padded = wd / "background_music.extend-src.wav"
    title = str(data.get("title") or wd.name)
    url = pool.acquire()
    extended = False
    # Minutes on a GPU — tracked so the Activity screen shows the song being
    # sung, whether it was asked for in the Song tab or by automation.
    try:
        extend_from = None
        if keep > 0 and music_engine_can_extend(url, engine):
            # The extend graph wants the source already at the target length —
            # the padding is what the model turns into the new tail.
            subprocess.run(
                [_resolve_media_tool("ffmpeg"), "-v", "error", "-y", "-i", str(final),
                 "-af", f"apad=whole_dur={secs}", str(padded)],
                check=True, capture_output=True, timeout=120)
            extend_from = padded
            extended = True
        op = "Singing the song's new ending" if extended else "Singing the song"
        with _track_op(op, f"{int(secs)}s · {engine}",
                       work_dir=str(wd), title=title, category="film"):
            generate_music(title, secs, staged,
                           caption or None, comfy_url=url, music_engine=engine,
                           lyrics=data.get("lyrics") or None,
                           extend_from=extend_from,
                           keep_seconds=keep if extend_from else None)
    finally:
        pool.release(url)
        padded.unlink(missing_ok=True)
    staged.replace(final)
    try:
        music_history.record(wd, final, caption)
    except Exception:
        gapp.logger.warning("Could not record song into music history", exc_info=True)
    dur = _get_duration(final)
    data.update({"generated_at": time.time(), "duration": dur, "seconds": secs})
    (wd / "song.json").write_text(json.dumps(data, indent=2))
    return {"ok": True, "duration": dur, "extended": extended,
            "song_url": f"/api/file?path={final}&t={int(time.time())}"}


def _run_song_generate_task(task_id: str, wd: Path, add_seconds: float = 0.0) -> None:
    try:
        _script_tasks[task_id] = {"status": "done",
                                  "result": _do_song_generate(wd, add_seconds)}
    except Exception as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:300]}


@api.post("/api/song/generate")
def song_generate(body: SongGenerateBody) -> dict:
    """Save the (possibly edited) song and start rendering its audio in the
    background; poll /api/script/generate/status with the returned task id."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    path = wd / "song.json"
    if not path.exists():
        raise HTTPException(404, "Draft the song first.")
    try:
        data = json.loads(path.read_text())
    except Exception:
        data = {}
    if (body.lyrics or "").strip():
        data["lyrics"] = body.lyrics.strip()
    if (body.caption or "").strip():
        data["caption"] = body.caption.strip()
    if body.voice is not None:
        data["voice"] = (body.voice or "").strip()
    if body.vocalist is not None:
        data["vocalist"] = body.vocalist.strip()
    add = float(body.add_seconds or 0)
    if add and not (0.5 <= add <= _MAX_SONG_EXTEND):
        raise HTTPException(400, f"Extend by between 0.5 and {int(_MAX_SONG_EXTEND)} seconds.")
    if not (data.get("lyrics") or "").strip():
        raise HTTPException(400, "The song has no lyrics.")
    data["updated_at"] = time.time()
    path.write_text(json.dumps(data, indent=2))
    task_id = uuid.uuid4().hex[:12]
    _script_tasks[task_id] = {"status": "running"}
    # The length arithmetic happens in _do_song_generate, measured off the take
    # in use — not off song.json, which describes the last generation.
    threading.Thread(target=_run_song_generate_task, args=(task_id, wd, add),
                     daemon=True).start()
    return {"task_id": task_id}


class SongExtendBody(BaseModel):
    work_dir: str
    seconds: float = 3.0


def _do_song_extend(wd: Path, seconds: float) -> dict:
    """Give the generated song a longer ending: its last couple of seconds are
    faded out and *seconds* of silence padded after them.

    This keeps the take — nothing is re-generated and no worker is involved —
    so a song that merely stops dead gets a proper ending without losing the
    arrangement that was approved. The extended track becomes the film's, and
    the abrupt one stays in the music history to be put back."""
    from pipeline.assembler import _get_duration, extend_audio_tail

    track = wd / "background_music.wav"
    if not track.exists():
        raise RuntimeError("Generate the song first.")
    try:
        data = json.loads((wd / "song.json").read_text())
    except Exception:
        data = {}
    # The un-extended take is a version in its own right — capture it before it
    # is overwritten.
    try:
        music_history.seed_if_empty(wd, track, data.get("caption") or "")
    except Exception:
        gapp.logger.warning("Could not seed the song into history", exc_info=True)
    hist = music_history.history(wd)
    source = next((v for v in hist["versions"] if v["id"] == hist["selected"]), None)
    staged = wd / "background_music.staging.wav"
    with _track_op("Extending the song's ending", f"+{seconds:g}s",
                   work_dir=str(wd), title=str(data.get("title") or ""),
                   category="film"):
        extend_audio_tail(track, staged, seconds)
    staged.replace(track)
    dur = _get_duration(track)
    try:
        # A padded re-voicing is still sung by that voice: carrying the label
        # over keeps the version list honest and keeps the next re-voicing off
        # an already-converted track.
        music_history.record(wd, track, f"+{seconds:g}s ending",
                             voice=(source or {}).get("voice") or "",
                             source_id=(source or {}).get("id"))
    except Exception:
        gapp.logger.warning("Could not record the extended song", exc_info=True)
    data.update({"duration": dur, "updated_at": time.time()})
    (wd / "song.json").write_text(json.dumps(data, indent=2))
    return {"ok": True, "duration": dur,
            "song_url": f"/api/file?path={track}&t={int(time.time())}",
            **{k: music_history.history(wd).get(k) for k in ("versions", "selected")}}


@api.post("/api/song/extend")
def song_extend(body: SongExtendBody) -> dict:
    """Extend the song's ending by *seconds* without re-generating it (ffmpeg
    on the controller, so it returns when it's done)."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    secs = float(body.seconds or 0)
    if not (0.5 <= secs <= _MAX_SONG_EXTEND):
        raise HTTPException(400, f"Extend by between 0.5 and {int(_MAX_SONG_EXTEND)} seconds.")
    try:
        return _do_song_extend(wd, secs)
    except Exception as e:
        raise HTTPException(500, f"Extending the song failed: {str(e).splitlines()[0][:300]}")


class SongConvertBody(BaseModel):
    work_dir: str
    voice: str


def _do_song_convert(wd: Path, voice: str, *, track_op: bool = True) -> dict:
    """Re-voice the approved song as a library voice (seed-vc).

    Both sides of the conversion are kept: the sung original is captured into
    the music history first (if it wasn't already), and the converted track is
    recorded as its own version before becoming background_music.wav — the one
    file the whole music-video pipeline hangs off, so the per-scene segments
    pinned into the takes sing in this voice too. Either version can be put
    back (and re-mixed into the final) from the Song tab or the film editor.

    The conversion runs on the version marked "In use" — the take the user is
    hearing — never the newest one. If the take in use is itself a re-voicing,
    its engine-sung source is converted instead (re-voicing an already
    re-voiced track would clone a clone), which is still the same take."""
    from pipeline import svc
    from pipeline.assembler import _get_duration

    ref = gapp.voice_path_for(voice)
    if not ref or not Path(ref).exists():
        raise RuntimeError(f"No reference clip for voice {voice!r}.")
    track = wd / "background_music.wav"
    if not track.exists():
        raise RuntimeError("Generate the song first.")
    data = {}
    try:
        data = json.loads((wd / "song.json").read_text())
    except Exception:
        pass
    try:
        music_history.seed_if_empty(wd, track, data.get("caption") or "")
    except Exception:
        gapp.logger.warning("Could not seed original song into history", exc_info=True)
    source, source_id = track, None
    hist = music_history.history(wd)
    sel = next((v for v in hist["versions"] if v["id"] == hist["selected"]), None)
    # If the take in use is a re-voicing, walk back to its engine-sung source —
    # the same take before any cloning (converting a clone clones a clone).
    seen_ids = set()
    while sel and sel.get("voice") and sel.get("source_id") and sel["id"] not in seen_ids:
        seen_ids.add(sel["id"])
        parent = music_history.find(wd, sel["source_id"])
        if parent is None:
            break
        sel = parent
    if sel and Path(sel["path"]).exists():
        source, source_id = Path(sel["path"]), sel["id"]
    cfg = gapp.load_config()
    staged = wd / "background_music.staging.wav"
    # Re-voicing is UI work like a cover: stamping activity makes a running
    # render hold a worker idle, so the conversion lands on a free GPU
    # instead of queueing behind (or contending with) the render.
    ui_activity.mark_active()
    # Minutes of diffusion — it belongs on the Activity screen. The film
    # editor's re-voicing already reports itself as a film task, so it opts out
    # here rather than showing the same work twice.
    op = (_track_op(f"Re-voicing the song as {voice}", data.get("title") or wd.name,
                    work_dir=str(wd), title=str(data.get("title") or ""), category="film")
          if track_op else nullcontext())
    with op:
        svc.convert_song(
            source, Path(ref), staged,
            # 30 steps by default (seed-vc's own default is 25; 50 is the
            # high-polish setting), and the diffusion goes to whichever GPU
            # worker is free — the controller's Apple GPU, the fallback when
            # none takes it, is ~12x slower than real time.
            diffusion_steps=int(cfg.get("svc_diffusion_steps") or 30),
            workers=svc.candidate_workers(cfg))
    staged.replace(track)
    try:
        music_history.record(wd, track, f"sung as {voice}", voice=voice,
                             source_id=source_id)
    except Exception:
        gapp.logger.warning("Could not record converted song", exc_info=True)
    data.update({"sung_as": voice, "converted_at": time.time()})
    (wd / "song.json").write_text(json.dumps(data, indent=2))
    dur = _get_duration(track)
    return {"ok": True, "duration": dur, "sung_as": voice,
            "song_url": f"/api/file?path={track}&t={int(time.time())}"}


def _run_song_convert_task(task_id: str, wd: Path, voice: str) -> None:
    try:
        _script_tasks[task_id] = {"status": "done",
                                  "result": _do_song_convert(wd, voice)}
    except Exception as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:300]}


@api.post("/api/song/convert")
def song_convert(body: SongConvertBody) -> dict:
    """Start re-voicing the song as *voice* in the background; poll
    /api/script/generate/status with the returned task id."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    if not (body.voice or "").strip():
        raise HTTPException(400, "Pick a voice to sing it.")
    from pipeline import svc
    if not svc.available():
        raise HTTPException(503, "Voice conversion is not installed — run "
                                 "scripts/install_svc.sh on the controller.")
    task_id = uuid.uuid4().hex[:12]
    _script_tasks[task_id] = {"status": "running"}
    threading.Thread(target=_run_song_convert_task,
                     args=(task_id, wd, body.voice.strip()), daemon=True).start()
    return {"task_id": task_id}


@api.get("/api/jobs/{job_id}/song")
def get_job_song(job_id: str) -> dict:
    """A song film's song.json — the caption and tagged lyrics the music model
    sings (404 for every other film) — plus the generated track and its kept
    versions, so the Song tab is the whole studio in one payload."""
    wd = _job_wd_or_404(job_id)
    path = wd / "song.json"
    if not path.exists():
        raise HTTPException(404, "This film has no song.")
    try:
        data = json.loads(path.read_text())
    except Exception:
        raise HTTPException(500, "song.json is unreadable.")
    track = wd / "background_music.wav"
    hist = music_history.history(wd)
    return {"caption": str(data.get("caption") or ""),
            "lyrics": str(data.get("lyrics") or ""),
            # The film's direction — editable in the studio because a song is
            # often steered long after Create, and a re-write needs it.
            "direction": _brief_direction(wd, data),
            # WHO sings: the vocalist line appended to the caption when the
            # track is generated, and the catalogue character it was cast
            # from. Surfaced so the studio can show and edit it — appended
            # silently, it looked like the model was picking its own singer.
            "vocalist": str(data.get("vocalist") or ""),
            "singer": str(data.get("singer") or ""),
            "voice": str(data.get("voice") or ""),
            "sung_as": str(data.get("sung_as") or ""),
            # The generated track's real length (else the asked-for length) —
            # what the clip-length control divides into scenes.
            "duration": float(data.get("duration") or data.get("seconds") or 0),
            "song_url": (f"/api/file?path={track}&t={int(track.stat().st_mtime)}"
                         if track.exists() else ""),
            "versions": hist.get("versions", []),
            "selected": hist.get("selected")}


def _stamp_song_voice(wd: Path, version_id: int) -> str:
    """Point song.json's ``sung_as`` and ``duration`` at the version now in use.

    The labels have to follow the track: reverting to the original generation
    must stop claiming a re-voicing, and the length shown (and extended from)
    must be the selected take's, not the last generation's. Returns the voice
    ("" for the original)."""
    from pipeline.assembler import _get_duration

    voice = ((music_history.find(wd, version_id) or {}).get("voice") or "")
    path = wd / "song.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            data["sung_as"] = voice
            track = wd / "background_music.wav"
            if track.exists():
                data["duration"] = _get_duration(track)
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            gapp.logger.warning("Could not update sung_as in song.json", exc_info=True)
    return voice


@api.post("/api/jobs/{job_id}/song/select")
def select_song_version(job_id: str, body: dict) -> dict:
    """The accept/revert step: put a kept version (the original generation, or
    a re-voiced one) back as the film's track."""
    wd = _job_wd_or_404(job_id)
    try:
        vid = int(body.get("version_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "version_id required")
    try:
        track = music_history.select(wd, vid)
    except Exception as e:
        raise HTTPException(404, str(e)[:200])
    sung_as = _stamp_song_voice(wd, vid)
    return {"ok": True, "sung_as": sung_as,
            "song_url": f"/api/file?path={track}&t={int(time.time())}",
            **{k: music_history.history(wd).get(k) for k in ("versions", "selected")}}


@api.post("/api/jobs/{job_id}/song/version/delete")
def delete_song_version(job_id: str, body: dict) -> dict:
    """Throw a take away. Landing a song takes many generations and the list
    only grows — this is how the ones nobody wants leave it.

    Deleting the version in use puts the newest remaining one back as the film's
    track (the song file always has to be one of the listed versions)."""
    wd = _job_wd_or_404(job_id)
    try:
        vid = int(body.get("version_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "version_id required")
    try:
        hist = music_history.delete(wd, vid)
    except Exception as e:
        raise HTTPException(400, str(e)[:200])
    sung_as = _stamp_song_voice(wd, hist["selected"]) if hist.get("selected") else ""
    track = wd / "background_music.wav"
    return {"ok": True, "sung_as": sung_as,
            "song_url": (f"/api/file?path={track}&t={int(time.time())}"
                         if track.exists() else ""),
            **{k: hist.get(k) for k in ("versions", "selected")}}


class SongUpdateBody(BaseModel):
    caption: str = ""
    lyrics: str = ""
    # The vocalist line (sex, age, background, voice quality) appended to the
    # caption when the track is sung. None = leave it alone (an older client
    # that doesn't send the field must not clear it).
    vocalist: str | None = None
    # The film's direction, saved into the create brief. None = leave it alone
    # (an older client that doesn't send the field must not clear it).
    direction: str | None = None


def _save_song_text(wd: Path, caption: str, lyrics: str,
                    vocalist: str | None = None) -> dict:
    """Write the song's words and sound into song.json (and the job_config
    mirror a rendered film re-sings from). *vocalist* None leaves the stored
    line untouched. Returns the saved song.json."""
    path = wd / "song.json"
    try:
        data = json.loads(path.read_text())
    except Exception:
        data = {}
    data.update({"caption": caption, "lyrics": lyrics, "updated_at": time.time()})
    if vocalist is not None:
        data["vocalist"] = vocalist.strip()
    path.write_text(json.dumps(data, indent=2))
    # A film that already rendered stamped the song into job_config.json for
    # the Remix regen — keep that mirror fresh so a re-sing uses the edit.
    jc_path = wd / "job_config.json"
    if jc_path.exists():
        try:
            jc = json.loads(jc_path.read_text())
            jc["music_lyrics"] = lyrics
            jc_path.write_text(json.dumps(jc, indent=2))
        except Exception:
            gapp.logger.warning("Could not mirror song edit into job_config.json",
                                exc_info=True)
    return data


@api.put("/api/jobs/{job_id}/song")
def update_job_song(job_id: str, body: SongUpdateBody) -> dict:
    """Save edited song lyrics/caption. The render reads song.json directly,
    so an edit made before (or between) renders is what gets sung."""
    wd = _job_wd_or_404(job_id)
    if not (wd / "song.json").exists():
        raise HTTPException(404, "This film has no song.")
    if not (body.lyrics or "").strip():
        raise HTTPException(400, "The lyrics can't be empty — the song is the film's audio.")
    _save_brief_direction(wd, body.direction)
    data = _save_song_text(wd, (body.caption or "").strip(), body.lyrics.strip(),
                           vocalist=body.vocalist)
    return {"ok": True, "caption": data["caption"], "lyrics": data["lyrics"],
            "vocalist": str(data.get("vocalist") or ""),
            "direction": _brief_direction(wd, data)}


class SongRegenBody(BaseModel):
    field: str = "lyrics"          # "lyrics" | "caption" (the Sound field)
    caption: str = ""              # what the editor currently shows…
    lyrics: str = ""               # …so a re-write never drops unsaved edits
    instruction: str = ""          # optional "tell it how" steering
    # The film's direction as the studio shows it — persisted into the create
    # brief BEFORE the re-write, so it both steers this one and survives for
    # the next (and for the Brief button). None = leave the brief alone.
    direction: str | None = None
    # The vocalist line as the studio shows it — an unsaved edit steers the
    # re-write and is saved with it. None = keep what song.json has.
    vocalist: str | None = None


@api.post("/api/jobs/{job_id}/song/regenerate")
def regenerate_job_song(job_id: str, body: SongRegenBody) -> dict:
    """Re-write ONE half of the song with the LLM — the lyrics, or the Sound
    caption the music model is given. The other half is kept as the editor has
    it and steers the re-write: new lyrics for the sound you liked, or a new
    sound for the lyrics you liked. Both halves are saved, so the re-write is
    what the next generation sings."""
    if body.field not in ("lyrics", "caption"):
        raise HTTPException(400, f"Unknown field: {body.field}")
    wd = _job_wd_or_404(job_id)
    path = wd / "song.json"
    if not path.exists():
        raise HTTPException(404, "This film has no song.")
    try:
        data = json.loads(path.read_text())
    except Exception:
        raise HTTPException(500, "song.json is unreadable.")
    caption = (body.caption or data.get("caption") or "").strip()
    lyrics = (body.lyrics or data.get("lyrics") or "").strip()
    vocalist = (body.vocalist if body.vocalist is not None
                else str(data.get("vocalist") or "")).strip()
    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, data.get("style_name") or "")
    _save_brief_direction(wd, body.direction)
    brief = _read_create_brief(wd)
    video_title = (brief.get("video_title") or "").strip()
    topic = (brief.get("topic") or data.get("title") or "").strip()
    # The song's asked-for length is the lyric budget; a generated track's real
    # duration is closer still.
    secs = float(data.get("duration") or data.get("seconds") or 0) or 60.0
    label = video_title or topic or wd.name

    if body.field == "lyrics":
        extra = (ss.get("extra_instructions") or "").strip()
        try:
            with _track_op("Re-writing the lyrics", label):
                song = story_mode.write_song(
                    None, secs,
                    language=gapp._norm_tts_language(ss.get("tts_language")),
                    topic=f"{topic}\n\n{extra}" if extra else topic,
                    video_title=video_title,
                    # The sound the user kept is the direction the new words
                    # are written to (its own caption is discarded).
                    music_hint=caption,
                    singer_note=vocalist,
                    instruction=body.instruction or "")
        except Exception as e:
            raise HTTPException(503, f"Lyric re-write failed: {str(e).splitlines()[0][:200]}")
        lyrics = song["lyrics"]
    else:
        system = ("You describe the MUSIC of a song for a music-generation model. "
                  "Return ONLY the description: 20-40 words, comma-separated — genre and "
                  "tempo/BPM, mood, then the lead instruments and groove. Never describe "
                  "the vocalist's gender, age or voice (that is added automatically), and "
                  "never write \"instrumental\".")
        user = (
            f"Song: {video_title or topic or '(untitled)'}\n"
            f"It runs about {int(secs)} seconds.\n"
            f"Current description: {caption or '(none yet)'}\n\n"
            f"The lyrics it has to carry:\n{lyrics or '(none yet)'}\n\n"
            "Write a fresh description of the music for these lyrics."
            + _instruction_note(body.instruction)
        )
        try:
            with _track_op("Re-writing the sound", label):
                caption = _llm_complete(system, user, cfg, max_tokens=300).strip().strip('"').strip()
        except Exception as e:
            raise HTTPException(503, f"Sound re-write failed: {str(e).splitlines()[0][:200]}")

    saved = _save_song_text(wd, caption, lyrics, vocalist=vocalist)
    return {"ok": True, "field": body.field,
            "caption": saved["caption"], "lyrics": saved["lyrics"],
            "vocalist": str(saved.get("vocalist") or ""),
            "direction": _brief_direction(wd, saved)}


@api.get("/api/jobs/{job_id}/story")
def get_job_story(job_id: str) -> dict:
    """The story.json behind a story-mode script (404 for classic scripts)."""
    wd = _job_wd_or_404(job_id)
    story = _read_story(wd)
    if not story:
        raise HTTPException(404, "This script has no story draft.")
    return story


class StorySaveBody(BaseModel):
    chapters: list[StoryChapterEdit] = []


@api.put("/api/jobs/{job_id}/story")
def save_job_story(job_id: str, body: StorySaveBody) -> dict:
    """Persist edited chapter texts into story.json, so a review can be resumed
    later (dividing also saves — this is for leaving mid-review)."""
    wd = _job_wd_or_404(job_id)
    story = _read_story(wd)
    if not story:
        raise HTTPException(404, "This script has no story draft.")
    _merge_story_edits(story, body.chapters)
    story["updated_at"] = time.time()
    _story_path(wd).write_text(json.dumps(story, indent=2))
    return story


class StoryRedraftBody(BaseModel):
    # Target length in minutes (preferred; the scene count comes from the
    # narrator's cadence) — or an explicit scene count when minutes is 0.
    minutes: float = 0.0
    n_scenes: int = 0
    # Edited chapter texts folded in before redrafting; [] keeps the draft as-is.
    chapters: list[StoryChapterEdit] = []


def _do_story_redraft(job_id: str, body: StoryRedraftBody) -> dict:
    """Retell the prose story at a new length (pipeline.story.redraft_story)
    and persist it back to story.json — the normal review + divide flow then
    runs with the new count."""
    wd = _job_wd_or_404(job_id)
    story = _read_story(wd)
    if not story:
        raise HTTPException(404, "This script has no story draft.")
    _merge_story_edits(story, body.chapters)

    brief = _read_create_brief(wd)
    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, brief.get("style_name", ""))
    plan_ss = dict(ss)
    if (brief.get("voice") or "").strip():
        plan_ss["voice"] = brief["voice"].strip()
    try:
        minutes = float(body.minutes or 0)
    except (TypeError, ValueError):
        minutes = 0.0
    if minutes > 0:
        plan = gapp.style_script_plan(plan_ss, minutes=minutes)
        n = plan["n_scenes"]
    else:
        n = int(body.n_scenes or 0)
        if not (1 <= n <= 200):
            raise HTTPException(400, "Give a target length in minutes, or a scene count between 1 and 200.")
        plan = gapp.style_script_plan(plan_ss, n_scenes=n)
    avoid_hint = (ss.get("script_avoid") or "").strip() or None
    user_topic = (brief.get("topic") or story.get("topic") or "").strip() or wd.name
    video_title = (brief.get("video_title") or story.get("video_title") or "").strip()
    # Retelling the same story keeps the same cast rule: only the catalogue
    # characters the brief named (gapp._requested_characters).
    character_sheet = gapp._character_sheet(gapp._requested_characters(
        cfg, ss["name"], user_topic, video_title, (ss.get("extra_instructions") or ""))) or None
    display_topic = video_title or user_topic.splitlines()[0][:80]
    try:
        with _track_op(f"Redrafting story to {n} scenes", display_topic):
            story = story_mode.redraft_story(story, n, character_sheet=character_sheet,
                                             avoid_hint=avoid_hint, scene_plan=plan)
    except HTTPException:
        raise
    except Exception as e:  # surface a clean message to the client
        raise HTTPException(500, f"Story redraft failed: {str(e).splitlines()[0][:300]}")
    _story_path(wd).write_text(json.dumps(story, indent=2))
    if brief:
        brief["minutes"] = plan["minutes"]
        brief["n_scenes"] = n
        brief["scene_plan"] = plan
        _write_create_brief(wd, brief)
    return story


def _run_story_redraft_task(task_id: str, job_id: str, body: "StoryRedraftBody") -> None:
    try:
        _script_tasks[task_id] = {"status": "done", "result": _do_story_redraft(job_id, body)}
    except HTTPException as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e.detail)[:300]}
    except Exception as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:300]}


@api.post("/api/jobs/{job_id}/story/redraft")
def story_redraft(job_id: str, body: StoryRedraftBody) -> dict:
    """Redraft the story for a new scene count in the background; poll
    /api/script/generate/status with the returned task id."""
    _job_wd_or_404(job_id)
    task_id = uuid.uuid4().hex[:12]
    _script_tasks[task_id] = {"status": "running"}
    threading.Thread(target=_run_story_redraft_task, args=(task_id, job_id, body),
                     daemon=True).start()
    return {"task_id": task_id}


# ── Script critic: post-generation QC (rewrite / delete / reorder scenes) ────

_CRITIC_MAX_PASSES = 5
_CRITIC_RUN_HISTORY = 20   # critic.json keeps this many runs' reports
_SCRIPT_VERSION_KEEP = 20
_VERSION_FILE_RE = re.compile(r"^\d+\.json$")


class CriticRunBody(BaseModel):
    passes: int = 1                # how many passes to run this time
    until_converged: bool = False  # keep passing until no edits (≤ _CRITIC_MAX_PASSES)


def _versions_dir(wd: Path) -> Path:
    return Path(wd) / "script_versions"


def _snapshot_script_version(job_id: str, wd: Path, label: str) -> None:
    """Snapshot the job's current scenes into script_versions/ so a critic pass
    (or a restore) can be undone. Content-level: titles, prompts, narrations,
    metadata; previews come back only where their files still exist."""
    store = DurableStore.default()
    try:
        rows = [dict(r) for r in store.scene_rows(job_id)]
    finally:
        store.close()
    if not rows:
        return
    d = _versions_dir(wd)
    d.mkdir(exist_ok=True)
    (d / f"{int(time.time() * 1000)}.json").write_text(json.dumps(
        {"saved_at": time.time(), "label": label,
         "scene_count": len(rows), "scenes": rows}, indent=2))
    for f in sorted(d.glob("*.json"))[:-_SCRIPT_VERSION_KEEP]:
        f.unlink(missing_ok=True)


def _list_script_versions(wd: Path) -> list[dict]:
    d = _versions_dir(wd)
    out = []
    if d.is_dir():
        for f in sorted(d.glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            out.append({"file": f.name, "label": data.get("label", ""),
                        "saved_at": data.get("saved_at", 0),
                        "scene_count": data.get("scene_count",
                                                len(data.get("scenes") or []))})
    return out


@api.get("/api/jobs/{job_id}/script-versions")
def list_job_script_versions(job_id: str) -> dict:
    return {"versions": _list_script_versions(_job_wd_or_404(job_id))}


class RestoreVersionBody(BaseModel):
    file: str


@api.post("/api/jobs/{job_id}/script-versions/restore")
def restore_job_script_version(job_id: str, body: RestoreVersionBody) -> dict:
    """Roll the script back to a saved version. The current state is snapshotted
    first ("before restore"), so a restore is itself undoable."""
    wd = _job_wd_or_404(job_id)
    if not _VERSION_FILE_RE.match(body.file or ""):
        raise HTTPException(400, "Unknown version.")
    p = _versions_dir(wd) / body.file
    if not p.exists():
        raise HTTPException(404, "That version no longer exists.")
    try:
        rows = json.loads(p.read_text()).get("scenes") or []
    except Exception:
        raise HTTPException(500, "Could not read that version.")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(400, "That version is empty.")
    _snapshot_script_version(job_id, wd, "before restore")
    for r in rows:
        pv = r.get("preview_path") or ""
        if pv and not Path(pv).exists():  # artifact renamed/deleted since the snapshot
            r["preview_path"] = ""
    store = DurableStore.default()
    try:
        store.replace_scenes(job_id, rows)
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    gapp._persist_script_snapshot(wd, rows)
    return {"restored": body.file,
            "scenes": [_scene_to_json(r, wd) for r in rows],
            "versions": _list_script_versions(wd)}


def _apply_critic_ops(job_id: str, ops: dict) -> dict:
    """Apply one critic pass's validated ops. Rewrites merge into the existing
    rows (upsert_scene replaces every field, so unspecified ones are carried
    over); deletes and reorders go through _restructure_job_scenes so
    renumbering, artifact renames and history remaps all follow."""
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
        by_id = {int(r["id"]): r for r in rows}
        ids = [int(r["id"]) for r in rows]
        applied_rewrites = 0
        for rw in ops.get("rewrites") or []:
            cur = by_id.get(rw["id"])
            if not cur:
                continue
            acted = performance_mode.is_performance_mode(
                (cur.get("metadata") or {}).get("mode"))
            if acted:
                # The dialogue is the scene: prose rewrites would desync the
                # narration mirror and the assembled prompt from the lines.
                # Titles are fair game; the rest is the acted editor's job.
                if not rw.get("title") or rw["title"] == cur.get("title"):
                    continue
                store.upsert_scene(
                    job_id, rw["id"], title=rw["title"],
                    image_prompt=cur.get("image_prompt") or "",
                    video_prompt=cur.get("video_prompt") or "",
                    narration=cur.get("narration") or "",
                    metadata=cur.get("metadata") or {},
                )
                applied_rewrites += 1
                continue
            store.upsert_scene(
                job_id, rw["id"],
                title=rw.get("title") or cur.get("title") or "",
                image_prompt=rw.get("image_prompt") or cur.get("image_prompt") or "",
                video_prompt=rw.get("video_prompt") or cur.get("video_prompt") or "",
                narration=rw.get("narration") or cur.get("narration") or "",
                metadata=cur.get("metadata") or {},
            )
            applied_rewrites += 1
    finally:
        store.close()

    deletes = [i for i in (ops.get("deletes") or []) if i in by_id]
    order = ops.get("order")
    # An `order` that is a valid subset of the current ids (unique, all real,
    # but missing some) expresses deletions by omission — the critic dropped a
    # scene from the new ordering instead of listing it in `deletes`. Honour
    # that intent so the removal isn't silently lost (which looked like "the
    # critic said it deleted a scene but the count didn't change").
    if (isinstance(order, list) and order and len(set(order)) == len(order)
            and all(i in by_id for i in order) and set(order) != set(ids)):
        deletes = list(dict.fromkeys(deletes + [i for i in ids if i not in set(order)]))
    # Guard: never delete the first or final scene (protect the hook + payoff),
    # matching the critic contract — even if the model asks to.
    protected = {ids[0], ids[-1]}
    deletes = [i for i in deletes if i not in protected]
    surviving = [i for i in ids if i not in set(deletes)]
    if not surviving:  # never let the critic delete the entire script
        deletes, surviving = [], ids
    sequence = order if (order and sorted(order) == sorted(surviving)) else surviving
    reordered = sequence != surviving
    # Weave inserts in as placeholder entries; _restructure_job_scenes turns a
    # None into a fresh blank scene at that position, which we then fill.
    items: list = list(sequence)
    inserts = ops.get("inserts") or []
    for ins in inserts:
        if ins["after"] == 0:
            idx = 0
        else:
            idx = next((k + 1 for k, v in enumerate(items)
                        if isinstance(v, int) and v == ins["after"]), len(items))
        items.insert(idx, dict(ins))
    structural = bool(inserts) or sequence != ids
    if structural:
        rows = _restructure_job_scenes(
            job_id, [i if isinstance(i, int) else None for i in items])
        if inserts:
            store = DurableStore.default()
            try:
                for pos, item in enumerate(items, start=1):
                    if isinstance(item, dict):
                        store.upsert_scene(
                            job_id, pos, title=item["title"],
                            image_prompt=item["image_prompt"],
                            video_prompt=item["video_prompt"],
                            narration=item["narration"], metadata={})
                rows = store.scene_rows(job_id)
            finally:
                store.close()
            wd = gapp._job_work_dir(job_id)
            if wd:
                gapp._persist_script_snapshot(Path(wd), rows)
    elif applied_rewrites:
        store = DurableStore.default()
        try:
            rows = store.scene_rows(job_id)
        finally:
            store.close()
        wd = gapp._job_work_dir(job_id)
        if wd:
            gapp._persist_script_snapshot(Path(wd), rows)
    return {"rewrites": applied_rewrites, "deleted": deletes, "added": len(inserts),
            "reordered": reordered, "scene_count": len(rows)}


def _do_critic_run(job_id: str, body: CriticRunBody) -> dict:
    """Run up to N critic passes over the job's scenes, stopping early when a
    pass proposes no edits (converged). Synchronous — the endpoint wraps it in
    a background task."""
    wd = _job_wd_or_404(job_id)
    _, _, _, style_name, brief = _script_source_meta(job_id, wd.name)
    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, style_name)
    # Rewrites must keep the script's own scene word caps (the plan it was
    # generated with; else the style's current cadence).
    scene_plan = brief.get("scene_plan") if isinstance(brief.get("scene_plan"), dict) else None
    if not scene_plan:
        plan_ss = dict(ss)
        if (brief.get("voice") or "").strip():
            plan_ss["voice"] = brief["voice"].strip()
        scene_plan = gapp.style_script_plan(plan_ss)
    avoid_hint = (ss.get("script_avoid") or "").strip() or None
    title = (brief.get("topic") or brief.get("video_title") or wd.name).strip()
    video_title = (brief.get("video_title") or "").strip() or None
    # Topic + the style's instructions: lets the critic run the INSTRUCTIONS
    # guardrail (world rules over narration AND visual prompts) and honour
    # direction-level exceptions (e.g. "the narrator introduces themselves").
    parts = [title]
    if (ss.get("extra_instructions") or "").strip():
        parts.append(ss["extra_instructions"].strip())
    if (ss.get("visual_style") or "").strip():
        parts.append(f"Visual style (baked into every image prompt as its leading "
                     f"prefix — intentional): {ss['visual_style'].strip()}")
    if (ss.get("video_style") or "").strip():
        parts.append(f"Motion/video direction: {ss['video_style'].strip()}")
    direction = "\n".join(parts)
    max_passes = (_CRITIC_MAX_PASSES if body.until_converged
                  else min(max(1, int(body.passes or 1)), _CRITIC_MAX_PASSES))
    # Cumulative pass count across runs: five separate single-pass clicks should
    # bias toward convergence exactly like one five-pass run.
    prior_passes = 0
    try:
        prior_passes = int(json.loads((wd / "critic.json").read_text()).get("total_passes") or 0)
    except Exception:
        pass
    report, converged = [], False
    for i in range(1, max_passes + 1):
        store = DurableStore.default()
        try:
            rows = store.scene_rows(job_id)
        finally:
            store.close()
        if len(rows) < 2:
            converged = True
            break
        def _critic_view(r) -> dict:
            # An acted scene's narration IS its spoken lines, which is exactly
            # what the critic should judge for flow and repetition. Its
            # video_prompt is the assembled H3 text — noise to the critic, and
            # a rewrite of it would desync the prompt from the lines.
            acted = performance_mode.is_performance_mode(
                (r.get("metadata") or {}).get("mode"))
            return {"id": int(r["id"]), "title": r.get("title") or "",
                    "narration": r.get("narration") or "",
                    "image_prompt": "" if acted else (r.get("image_prompt") or ""),
                    "video_prompt": ("(acted scene — the characters speak this "
                                     "on camera; visuals come from references)"
                                     if acted else (r.get("video_prompt") or ""))}
        scene_rows_min = [_critic_view(r) for r in rows]
        # Deterministic backstop: hand the critic any mechanically-detected
        # near-duplicate narrations so it cannot overlook them (e.g. a pair
        # involving the protected final scene).
        dups = story_mode.near_duplicate_pairs(scene_rows_min)
        dup_note = ""
        if dups:
            listed = "; ".join(f"scenes {a} and {b} ({int(r * 100)}% similar)"
                               for a, b, r in dups)
            dup_note = (f"\nDETECTED NEAR-DUPLICATE NARRATIONS — these MUST be resolved "
                        f"this pass (delete or rewrite one of each pair; scene 1 and the "
                        f"final scene may be rewritten but not deleted): {listed}.")
        try:
            with _track_op(f"Critic pass {i}", video_title or title):
                ops = story_mode.critique_scenes(
                    scene_rows_min,
                    title, video_title=video_title, avoid_hint=avoid_hint,
                    pass_num=prior_passes + i, direction=direction,
                    dup_note=dup_note, scene_plan=scene_plan)
        except Exception as e:
            raise HTTPException(500, f"Critic pass failed: {str(e).splitlines()[0][:300]}")
        if not ops["changed"]:
            notes = ops["notes"]
            if dups:
                listed = "; ".join(f"scenes {a}/{b}" for a, b, _ in dups)
                notes = notes + [f"duplicate detector still flags {listed} — "
                                 "the critic judged them acceptable"]
            report.append({"pass": i, "rewrites": 0, "deleted": [], "added": 0,
                           "reordered": False, "notes": notes})
            converged = True
            break
        # Snapshot the pre-edit script so this pass can be rolled back.
        _snapshot_script_version(job_id, wd, f"before critic pass {i}")
        summary = _apply_critic_ops(job_id, ops)
        report.append({"pass": i, **summary, "notes": ops["notes"]})
        if not (summary["rewrites"] or summary["deleted"] or summary["added"]
                or summary["reordered"]):
            converged = True
            break
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    result = {"passes": report, "converged": converged,
              "scenes": [_scene_to_json(r, wd) for r in rows],
              "versions": _list_script_versions(wd)}
    # Best-effort record next to the script. The top-level fields describe the
    # LATEST run (and feed the cumulative pass counter above); "runs" keeps the
    # history — overwriting it lost what earlier runs reported, which made a
    # "the critic said it removed a scene" report impossible to check after a
    # later run.
    try:
        try:
            prev = json.loads((wd / "critic.json").read_text())
            prev = prev if isinstance(prev, dict) else {}
        except Exception:
            prev = {}
        runs = prev.get("runs") if isinstance(prev.get("runs"), list) else []
        if not runs and prev.get("passes"):  # migrate a pre-history file
            runs = [{"ran_at": prev.get("ran_at", 0), "passes": prev["passes"],
                     "converged": prev.get("converged")}]
        runs.append({"ran_at": time.time(), "passes": report, "converged": converged})
        (wd / "critic.json").write_text(json.dumps(
            {"ran_at": time.time(), "passes": report, "converged": converged,
             "total_passes": prior_passes + len(report),
             "runs": runs[-_CRITIC_RUN_HISTORY:]}, indent=2))
    except Exception:
        pass
    return result


def _run_critic_task(task_id: str, job_id: str, body: "CriticRunBody") -> None:
    try:
        _script_tasks[task_id] = {"status": "done", "result": _do_critic_run(job_id, body)}
    except HTTPException as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e.detail)[:300]}
    except Exception as e:
        _script_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:300]}


@api.post("/api/jobs/{job_id}/critic")
def run_script_critic(job_id: str, body: CriticRunBody) -> dict:
    """Kick off a critic run in the background; poll
    /api/script/generate/status with the returned task id."""
    _job_wd_or_404(job_id)
    task_id = uuid.uuid4().hex[:12]
    _script_tasks[task_id] = {"status": "running"}
    threading.Thread(target=_run_critic_task, args=(task_id, job_id, body), daemon=True).start()
    return {"task_id": task_id}


def _read_script_scenes(wd: Path) -> list:
    """Read and validate the saved script.json from `wd`, or raise HTTPException."""
    script_path = wd / "script.json"
    if not script_path.exists():
        raise HTTPException(404, "No script found in the selected folder.")
    try:
        scenes_list = json.loads(script_path.read_text())
    except Exception as e:
        raise HTTPException(500, f"Could not read script: {str(e).splitlines()[0][:200]}")
    if not isinstance(scenes_list, list):
        raise HTTPException(400, "Saved script has an unexpected format.")
    return scenes_list


def _script_source_meta(src_job_id: str, fallback_title: str) -> tuple[str, str, str, str, dict]:
    """Resolve (video_title, style, music_desc, style_name, create_brief) for an
    existing job from the durable store, falling back to the folder-derived title."""
    video_title, style, music_desc, style_name = fallback_title, "", "", ""
    create_brief: dict = {}
    store = DurableStore.default()
    try:
        job = store.get_job(src_job_id)
        if job:
            d = _row_to_dict(job)
            cfg = json.loads(d.get("config_json") or "{}")
            meta = json.loads(d.get("metadata_json") or "{}")
            video_title = cfg.get("video_title") or d.get("title") or fallback_title
            style = meta.get("style", "")
            music_desc = meta.get("music_desc", "")
            style_name = cfg.get("style_name", "")
            if isinstance(cfg.get("create_brief"), dict):
                create_brief = dict(cfg["create_brief"])
            # Older jobs only kept topic (sometimes with style extras baked in).
            if not create_brief and cfg.get("topic"):
                create_brief = {
                    "video_title": video_title,
                    "topic": str(cfg.get("topic") or ""),
                    "style_name": style_name,
                }
    finally:
        store.close()
    return video_title, style, music_desc, style_name, create_brief


def _register_script_into(wd: Path, scenes_list: list, *, video_title: str,
                          style: str, music_desc: str, style_name: str,
                          create_brief: dict | None = None) -> dict:
    """Register `scenes_list` as the script of work dir `wd` (a reload of its own
    folder, or a fresh duplicate) and return the Script-editor payload. Back-fills
    each scene's preview_path from any matching image already in `wd`, so a first
    frame produced by an earlier render (or copied from a source script) is reused
    instead of regenerated. Shared by /scripts/load and /scripts/duplicate."""
    job_id = job_id_from_work_dir(wd)
    brief = dict(create_brief or {}) or _read_create_brief(wd)
    # Prefer on-disk brief (source of truth after generate); merge store fallbacks.
    disk = _read_create_brief(wd)
    if disk:
        brief = {**brief, **disk}
    store = DurableStore.default()
    try:
        cfg_payload = {"video_title": video_title, "phase": "script_review",
                       "style_name": style_name}
        if brief:
            cfg_payload["create_brief"] = brief
            if brief.get("topic"):
                cfg_payload["topic"] = brief["topic"]
            if brief.get("video_title"):
                cfg_payload["video_title"] = brief["video_title"]
                video_title = brief["video_title"] or video_title
            if brief.get("style_name"):
                cfg_payload["style_name"] = brief["style_name"]
                style_name = brief["style_name"] or style_name
        store.create_or_update_job(
            job_id, wd, video_title,
            config=cfg_payload,
            metadata={"scene_count": len(scenes_list), "music_desc": music_desc, "style": style},
        )
        store.upsert_scenes(job_id, scenes_list)
        rows = store.scene_rows(job_id)
        # Back-fill preview_path for scenes whose image already exists on disk
        # (e.g. first_frame generated during a previous video render).
        for r in rows:
            if r.get("preview_path") and Path(r["preview_path"]).exists():
                continue
            sid = int(r["id"])
            for suffix in ("_preview.png", "_first_frame.png"):
                candidate = wd / f"scene_{sid:02d}{suffix}"
                if candidate.exists():
                    store.update_scene_preview(job_id, sid, candidate)
                    r["preview_path"] = str(candidate)
                    break
    finally:
        store.close()

    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, style_name)
    voice = (brief.get("voice") or "").strip() or ss.get("voice", "")
    resolution = (brief.get("resolution") or "").strip() or ss.get("resolution") or gapp._DEFAULT_RESOLUTION
    return {
        "job_id": job_id,
        "work_dir": str(wd),
        "title": video_title,
        "video_title": video_title,
        "topic": brief.get("topic") or "",
        "style": style,
        "style_name": ss["name"],
        "music_desc": music_desc,
        "voice": voice,
        "resolution": resolution,
        "create_brief": brief,
        "scenes": [_scene_to_json(r, wd) for r in rows],
        "characters": _script_characters_payload(wd),
    }


@api.get("/api/scripts/load")
def load_script(work_dir: str = Query("")) -> dict:
    if not work_dir:
        raise HTTPException(400, "Choose a saved script.")
    wd = Path(work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Script path is outside the output folder.")
    # A story-first draft has no script.json yet — load with zero scenes so the
    # Script screen opens the Story view for review/division. A music video is
    # earlier still: its song is written before any story exists, so a song on
    # its own is enough to open (on the Song tab).
    if not (wd / "script.json").exists() and (_read_story(wd) or (wd / "song.json").exists()):
        scenes_list = []
    else:
        scenes_list = _read_script_scenes(wd)
    fallback_title = wd.name.replace("-", " ").title()
    video_title, style, music_desc, style_name, create_brief = _script_source_meta(
        job_id_from_work_dir(wd), fallback_title)
    return _register_script_into(wd, scenes_list, video_title=video_title,
                                 style=style, music_desc=music_desc, style_name=style_name,
                                 create_brief=create_brief)


def _cast_member(wd: Path, cfg: dict, style_name: str, name: str) -> dict:
    """One cast member for the performance view: who they are, whether their
    look and voice are pinned, and whether this film can edit them.

    A per-script character is this film's own, so its look and voice are edited
    in place. A catalogue character is shared with every other film that uses
    it, so it is reported read-only rather than silently rewritten from here."""
    key = (name or "").strip().lower()

    def _matches(c: dict) -> bool:
        names = [c.get("name", ""), *(c.get("aliases") or [])]
        return any(key == str(n).strip().lower() for n in names if str(n).strip())

    for c in gapp._read_script_characters(wd):
        if _matches(c):
            return {**_character_to_json(wd, c), "name": name, "scope": "script",
                    "editable": True}
    for c in gapp._style_characters(cfg, style_name):
        if _matches(c):
            img = gapp._character_image_path(c.get("ref_image"))
            has_image = bool(img and img.exists() and img.stat().st_size > 0)
            return {"id": c.get("id", ""), "name": name,
                    "description": c.get("description", ""),
                    "voice": c.get("voice", ""), "has_image": has_image,
                    "image_url": (f"/api/file?path={img}&t={int(img.stat().st_mtime)}"
                                  if has_image else ""),
                    "scope": "catalogue", "editable": False}
    return {"id": "", "name": name, "description": "", "voice": "",
            "has_image": False, "image_url": "", "scope": "missing", "editable": False}


# ── asset catalogue: reusable locations and wardrobe ─────────────────────────
# Characters are the other kind of asset and keep their own catalogue (they
# carry voices, aliases and casting rules these do not).

class AssetsBody(BaseModel):
    assets: list[dict]


class AssetImageBody(BaseModel):
    asset_id: str
    style_name: str = ""
    extra_prompt: str = ""
    # Paint a wardrobe asset as a turnaround sheet of its character WEARING the
    # outfit (portrait-conditioned), instead of the default empty-garments flat lay.
    worn: bool = False


class AssetUploadBody(BaseModel):
    asset_id: str
    filename: str = ""
    data: str


def _assets_payload(cfg: dict) -> dict:
    out = []
    for a in gapp._norm_assets(cfg.get("assets")):
        img = gapp._asset_image_path(a.get("ref_image"))
        has = bool(img and img.exists() and img.stat().st_size > 0)
        out.append({**a, "has_image": has,
                    "image_url": (f"/api/file?path={img}&t={int(img.stat().st_mtime)}"
                                  if has else "")})
    return {"ok": True, "assets": out}


@api.get("/api/assets")
def list_assets() -> dict:
    return _assets_payload(gapp.load_config())


@api.post("/api/assets")
def save_assets(body: AssetsBody) -> dict:
    return _assets_payload(gapp.save_assets(body.assets))


@api.post("/api/assets/image")
def generate_asset_image(body: AssetImageBody) -> dict:
    try:
        with _track_op("Painting a reference image", body.asset_id):
            cfg = gapp.generate_asset_image(body.asset_id, body.style_name, body.extra_prompt,
                                            worn=body.worn)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(503, f"Image generation failed: {str(e).splitlines()[0][:200]}")
    return _assets_payload(cfg)


@api.post("/api/assets/upload")
def upload_asset_image(body: AssetUploadBody) -> dict:
    try:
        cfg = gapp.set_asset_image(body.asset_id, _decode_image(body.data))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _assets_payload(cfg)


# ── per-script visuals: locations and wardrobe ───────────────────────────────

class VisualCreate(BaseModel):
    name: str = ""
    kind: str = "location"
    description: str = ""
    character: str = ""
    usage: str = ""


class VisualUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    description: str | None = None
    character: str | None = None
    usage: str | None = None
    scenes: list[int] | None = None
    enabled: bool | None = None


class VisualImageBody(BaseModel):
    extra_prompt: str = ""
    worn: bool = False    # wardrobe only: worn turnaround sheet, not a flat lay


def _visual_to_json(wd: Path, v: dict) -> dict:
    img = gapp._script_visual_image_path(wd, v.get("ref_image"))
    has_image = bool(img and img.exists() and img.stat().st_size > 0)
    aud = gapp._script_visual_image_path(wd, v.get("source_audio"))
    has_audio = bool(aud and aud.exists() and aud.stat().st_size > 0)
    return {**v, "has_image": has_image,
            "image_url": (f"/api/file?path={img}&t={int(img.stat().st_mtime)}"
                          if has_image else ""),
            "has_audio": has_audio,
            "audio_url": (f"/api/file?path={aud}&t={int(aud.stat().st_mtime)}"
                          if has_audio else "")}


def _visuals_ok(wd: Path) -> dict:
    """Film visuals (editable) plus the style's asset catalogue (read-only —
    shared across films, edited in Settings → Assets). A film visual shadows a
    same-named catalogue asset, mirroring scene_visuals' render-time rule.

    A song film's SONG is listed first as a read-only soundtrack artifact —
    it is an input of every singing take (each pins its own window of it), and
    an input the wall doesn't show is one nobody can reason about."""
    own = [_visual_to_json(wd, v) for v in gapp.read_script_visuals(wd)]
    track = wd / "background_music.wav"
    if (wd / "song.json").exists() and track.exists():
        own.insert(0, {
            "id": "__song__", "name": "The film's song", "kind": "audio",
            "description": ("The soundtrack every singing take is generated "
                            "against — each scene pins its own window of it. "
                            "Generate, re-voice and pick versions in the Song "
                            "tab."),
            "character": "", "ref_image": "", "source_audio": "",
            "scenes": [], "enabled": True, "readonly": True,
            "has_image": False, "image_url": "", "has_audio": True,
            "audio_url": f"/api/file?path={track}&t={int(track.stat().st_mtime)}",
        })
    taken = {(v.get("name") or "").strip().lower() for v in own}
    used, has_acted = _film_reference_usage(wd)
    catalogue = []
    try:
        cfg = gapp.load_config()
        style_name = _job_style_name(job_id_from_work_dir(wd))
        for a in gapp.style_assets(cfg, style_name):
            if (a.get("name") or "").strip().lower() in taken:
                continue
            img = gapp._asset_image_path(a.get("ref_image"))
            has = bool(img and img.exists() and img.stat().st_size > 0)
            # Only what this video's renders actually pull in: an asset feeds
            # the slots when it has an image, the film has acted scenes, and a
            # character-owned wardrobe's owner is in the cast (scene_visuals'
            # own rule).
            if not (has and has_acted and a.get("enabled", True)):
                continue
            if (a.get("kind") == "wardrobe" and (a.get("character") or "").strip()
                    and a["character"].strip().lower() not in used):
                continue
            catalogue.append({
                "id": a.get("id", ""), "name": a.get("name", ""),
                "kind": a.get("kind", "location"),
                "description": a.get("description", ""),
                "character": a.get("character", ""),
                "scenes": [], "enabled": a.get("enabled", True),
                "has_image": has,
                "image_url": f"/api/file?path={img}&t={int(img.stat().st_mtime)}" if has else "",
                "scope": "catalogue",
            })
    except Exception:
        pass
    return {"ok": True, "visuals": own, "catalogue": catalogue}


@api.get("/api/jobs/{job_id}/visuals")
def list_script_visuals(job_id: str) -> dict:
    return _visuals_ok(_job_wd_or_404(job_id))


@api.post("/api/jobs/{job_id}/visuals")
def create_script_visual(job_id: str, body: VisualCreate) -> dict:
    wd = _job_wd_or_404(job_id)
    gapp.add_script_visual(wd, body.name, body.kind, body.description, body.character,
                           body.usage)
    return _visuals_ok(wd)


@api.put("/api/jobs/{job_id}/visuals/{visual_id}")
def edit_script_visual(job_id: str, visual_id: str, body: VisualUpdate) -> dict:
    wd = _job_wd_or_404(job_id)
    try:
        gapp.update_script_visual(wd, visual_id, **body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return _visuals_ok(wd)


@api.delete("/api/jobs/{job_id}/visuals/{visual_id}")
def remove_script_visual(job_id: str, visual_id: str) -> dict:
    wd = _job_wd_or_404(job_id)
    gapp.delete_script_visual(wd, visual_id)
    return _visuals_ok(wd)


class VisualUpload(BaseModel):
    filename: str = ""
    data: str


@api.post("/api/jobs/{job_id}/visuals/{visual_id}/upload")
def upload_script_visual_image(job_id: str, visual_id: str, body: VisualUpload) -> dict:
    """Use a real photo (or clip) of the thing instead of painting one.

    Video uploads get a representative frame extracted — the picture slots
    feed the model stills."""
    wd = _job_wd_or_404(job_id)
    try:
        gapp.set_script_visual_media(wd, visual_id, _decode_image(body.data),
                                     filename=body.filename or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _visuals_ok(wd)


class VisualFromUrlBody(BaseModel):
    url: str


@api.post("/api/jobs/{job_id}/visuals/{visual_id}/from-url")
def visual_from_url(job_id: str, visual_id: str, body: VisualFromUrlBody) -> dict:
    """Fetch a visual's reference from a URL — a direct image/video file, or a
    page whose og:image / og:video points at one. Video gets a frame extracted."""
    wd = _job_wd_or_404(job_id)
    try:
        with _track_op("Fetching reference", body.url[:60]):
            gapp.fetch_visual_from_url(wd, visual_id, body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Fetch failed: {str(e).splitlines()[0][:200]}")
    return _visuals_ok(wd)


@api.post("/api/jobs/{job_id}/visuals/{visual_id}/image")
def generate_script_visual_image(job_id: str, visual_id: str, body: VisualImageBody) -> dict:
    wd = _job_wd_or_404(job_id)
    _, _, _, style_name, _ = _script_source_meta(job_id, wd.name)
    try:
        with _track_op("Painting a reference image", wd.name):
            gapp.generate_script_visual_image(wd, visual_id, style_name, worn=body.worn,
                                              extra_prompt=body.extra_prompt)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(503, f"Image generation failed: {str(e).splitlines()[0][:200]}")
    return _visuals_ok(wd)


@api.post("/api/jobs/{job_id}/visuals/{visual_id}/image/from-character-sheet")
def script_visual_from_character_sheet(job_id: str, visual_id: str) -> dict:
    """Copy the wardrobe visual's character's turnaround sheet in as its image."""
    wd = _job_wd_or_404(job_id)
    _, _, _, style_name, _ = _script_source_meta(job_id, wd.name)
    try:
        gapp.script_visual_use_character_sheet(wd, visual_id, style_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _visuals_ok(wd)


@api.post("/api/assets/image/from-character-sheet")
def asset_from_character_sheet(body: AssetImageBody) -> dict:
    """Copy the wardrobe asset's character's turnaround sheet in as its image."""
    try:
        cfg = gapp.asset_use_character_sheet(body.asset_id, body.style_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _assets_payload(cfg)


@api.get("/api/scripts/performance")
def load_performance_script(work_dir: str = Query("")) -> dict:
    """Everything the performance view needs for a whole film, in one payload.

    Each scene comes back with its references already resolved into numbered
    slots — <Picture 1> is this portrait, <Audio 1> is that voice clip — because
    the prompt cites slot numbers and a screen that only showed the prompt would
    leave you guessing which reference is which. Resolved by the same function
    the renderer uses, so the screen and the render never disagree."""
    if not work_dir:
        raise HTTPException(400, "Choose a saved script.")
    wd = Path(work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Script path is outside the output folder.")
    rows = _read_script_scenes(wd)
    cfg = gapp.load_config()
    _, _, _, style_name, _ = _script_source_meta(
        job_id_from_work_dir(wd), wd.name.replace("-", " ").title())
    ss = gapp.style_settings(cfg, style_name)
    # EVERY take this film shoots on H3, which is what this screen is about:
    # the dialogue scenes plus, when the style performs them, the silent ones.
    # Same predicate as the renderer, so a take that is shot here is shown here.
    jc = dict(_film_job_config(wd))
    jc.setdefault("style_name", style_name)
    acted_cfg = _acted_silent_cfg(jc)
    chained = bool(jc.get("h3_chain_scenes") if jc.get("h3_chain_scenes") is not None
                   else ss.get("h3_chain_scenes"))

    scenes = []
    for row in rows:
        meta = dict(row.get("metadata") or {})
        if not performance_mode.renders_acted({"metadata": meta}, acted_cfg):
            continue
        # A dialogue scene authored in a mixed film carries only its lines —
        # fill in the cast/length/setting the acted render derives. Chaining is
        # read for the same reason the renderer reads it: it decides how long a
        # SILENT take may run, and the screen must show the length it will get.
        meta = performance_mode.acted_meta(
            {**row, "metadata": meta, "lines": meta.get("lines") or []}, chained=chained)
        refs = gapp.resolve_performance_references(meta, cfg, wd, style_name,
                                                   scene_id=int(row.get("id") or 0))
        lines = performance_mode.norm_lines(meta.get("lines"))
        # Every cast member, whether or not a reference resolved, so the screen
        # can offer the look/voice controls in place. Per-script characters are
        # editable here; catalogue ones are shared with other films, so those
        # are shown read-only with a pointer to Settings.
        picture_slot = {p["name"]: p["slot"] for p in refs["pictures"]}
        audio_by_name = {a["name"]: a for a in refs["audios"]}
        cast = []
        for name in (meta.get("cast") or []):
            entry = _cast_member(wd, cfg, style_name, name)
            entry["picture_slot"] = picture_slot.get(name)
            aud = audio_by_name.get(name)
            entry["audio_slot"] = aud["slot"] if aud else None
            entry["speaks"] = name in performance_mode.speakers_in(lines)
            cast.append(entry)
        # The rendered clip, when there is one: the performance view doubles as
        # the film view, so the same screen shows either the plan or the result.
        media = _film_scene_files(wd, int(row.get("id") or 0))
        take_history = video_history.history(wd, int(row.get("id") or 0))
        scenes.append({
            "id": row.get("id"),
            "video_history": take_history,
            "title": row.get("title") or "",
            # Not shown, but PUT back untouched when the dialogue or prompt is
            # edited (the scene endpoint writes every field it is given).
            "image_prompt": row.get("image_prompt") or "",
            "video_prompt": row.get("video_prompt") or "",
            "narration": row.get("narration") or "",
            "mode": meta.get("mode") or "dialogue",
            # A performed SILENT take: same shoot, nobody speaks. The screen
            # drops the dialogue editor for it rather than offering lines that
            # would turn the beat into a conversation.
            "silent": performance_mode.is_silent({"metadata": meta}),
            # A song film's beat — performed singing the film's song; the
            # window is the stretch of the track pinned into this take, shown
            # as one of the take's INPUTS beside its pictures and voices.
            "singing": performance_mode.is_singing({"metadata": meta}),
            # False when the scene says nobody sings on camera here (the song
            # still plays over the shot); performing is the default.
            "performs": meta.get("performs") is not False,
            "song_window": meta.get("song_window") or None,
            "sings": meta.get("sings") or "",
            # True once the prompt has been hand-edited: the screen then shows
            # the override instead of re-assembling, and offers to drop it.
            "prompt_edited": bool(meta.get("prompt_override")),
            "video_url": media.get("video_url") or "",
            "has_video": bool(media.get("has_video") or media.get("has_final")),
            "seconds": meta.get("seconds") or performance_mode.SCENE_SECONDS,
            "setting": meta.get("setting") or "",
            "camera": meta.get("camera") or "",
            "soundscape": meta.get("soundscape") or "",
            "no_wardrobe": bool(meta.get("no_wardrobe")),
            "cast": cast,
            "beats": performance_mode.norm_beats(
                meta.get("beats"), float(meta.get("seconds") or performance_mode.SCENE_SECONDS)),
            "lines": lines,
            # The exact text the model receives, rebuilt from the resolved
            # slots — including the pinned soundtrack artifact's usage note.
            "prompt": performance_mode.build_h3_prompt(
                {**meta, "lines": lines,
                 "track_usage": (refs.get("track") or {}).get("usage", "")},
                style_note=(row.get("style") or ss.get("visual_style") or ""),
                picture_names=refs["pictures"],
                audio_names=[a["name"] for a in refs["audios"]]),
            "pictures": [{**p, "image_url": (f"/api/file?path={p['path']}"
                                             f"&t={int(Path(p['path']).stat().st_mtime)}"
                                             if p.get("path") and Path(p["path"]).exists() else "")}
                         for p in refs["pictures"]],
            "audios": refs["audios"],
            # Speakers with no cast voice: H3 invents one, and it drifts between
            # scenes. Surfaced so it is fixable before rendering.
            "unvoiced": [n for n in performance_mode.speakers_in(lines)
                         if n not in {a["name"] for a in refs["audios"]}],
            "missing_portraits": [n for n in (meta.get("cast") or [])
                                  if n not in {p["name"] for p in refs["pictures"]}],
        })
    from pipeline import engines as eng
    engine = eng.resolve_reference(ss, ss.get("reference_engine"))
    track = wd / "background_music.wav"
    return {"work_dir": str(wd), "style_name": style_name,
            "job_id": job_id_from_work_dir(wd),
            "engine": {"key": engine["key"], "label": engine["label"]},
            "acted_silent": acted_cfg["h3_silent_scenes"],
            # The film's song, when it has one — each singing take pins its
            # own window of this file, so the screen can play exactly the
            # slice a take was generated against.
            "song_url": (f"/api/file?path={track}&t={int(track.stat().st_mtime)}"
                         if track.exists() and (wd / "song.json").exists() else ""),
            "scenes": scenes}


class DuplicateScriptBody(BaseModel):
    work_dir: str
    title: str = ""


@api.post("/api/scripts/duplicate")
def duplicate_script(body: DuplicateScriptBody) -> dict:
    """Copy an existing script into a brand-new work dir so it can be rendered
    again without touching the original. The job id is derived from the folder
    path, so a fresh folder is a fresh job with a fresh final video
    (~/videos/<new-folder>.mp4) — the source film's scenes and output stay intact.
    Cached scene first-frames, the description and cover are copied across too, so
    the editor is pre-filled and the re-render reuses the images (a new LTX pass
    still yields a different take). A music video's song travels with it too, so
    the copy re-renders against the SAME track its scenes are timed to. Returns
    the same payload as /scripts/load, so the duplicate opens straight in the
    Script editor for review."""
    import shutil
    src = Path(body.work_dir)
    if not _safe_under(src, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Script path is outside the output folder.")
    scenes_list = _read_script_scenes(src)

    fallback_title = src.name.replace("-", " ").title()
    src_title, style, music_desc, style_name, create_brief = _script_source_meta(
        job_id_from_work_dir(src), fallback_title)
    title = (body.title or "").strip() or src_title

    new_wd = gapp._script_work_dir(title)
    gapp._persist_script_snapshot(new_wd, scenes_list)
    # Carry cached scene first-frames over so the render reuses them, plus the
    # description/cover so the editor's Cover tab is pre-filled. Best-effort —
    # a source without these just regenerates them on demand.
    for s in scenes_list:
        try:
            sid = int(s.get("id", 0))
        except (TypeError, ValueError):
            continue
        if sid <= 0:
            continue
        for suffix in (f"scene_{sid:02d}_preview.png", f"scene_{sid:02d}_first_frame.png"):
            sp = src / suffix
            if sp.exists():
                shutil.copy2(sp, new_wd / suffix)
    # story.json keeps a story-mode source's prose draft, so the duplicate still
    # shows the Story tab and can redraft/re-divide.
    for extra in ("description.txt", "cover.png", "cover_bg.png", "cover_phrase.txt",
                  "create_brief.json", "story.json"):
        sp = src / extra
        if sp.exists():
            shutil.copy2(sp, new_wd / extra)
    # Prefer the brief we already loaded when the source only had it in the store.
    if create_brief and not _create_brief_path(new_wd).exists():
        brief_copy = dict(create_brief)
        if title and title != src_title:
            brief_copy["video_title"] = title
        _write_create_brief(new_wd, brief_copy)
    # A music video's song comes with it: without song.json + the track the
    # copy stops being a song film and its render sings a fresh, lyric-less
    # ACE-Step track over scenes still timed to the original.
    _copy_song_artifacts(src, new_wd)
    # And the script's reference anchors: the per-script cast with its look
    # images, plus the visuals wall (locations, wardrobe, uploaded refs). The
    # scenes are copied verbatim, so scene-scoped visuals keep their scoping.
    _copy_script_reference_files(src, new_wd, keep_scene_scope=True)

    return _register_script_into(new_wd, scenes_list, video_title=title,
                                 style=style, music_desc=music_desc, style_name=style_name,
                                 create_brief=create_brief)


class DuplicateRenderBody(BaseModel):
    work_dir: str
    resolution: str
    title: str = ""


@api.post("/api/scripts/duplicate-render")
def duplicate_script_and_render(body: DuplicateRenderBody) -> dict:
    """Duplicate a script and queue the copy to render at another resolution.

    One click for what "Duplicate → pick a resolution → Approve" already does by
    hand, so both sizes survive side by side as separate films with their own
    finals. This is a full re-render, NOT an upscale: the copied first frames
    don't match the new dimensions, so the render regenerates them and shoots
    fresh takes (same script, different footage). Renders straight away only
    under the same rules as a Script approval — auto-start on, nothing else
    rendering — otherwise the copy waits in the queue."""
    resolution = (body.resolution or "").strip()
    # Upscale-only targets (QHD/4K) are allowed: the copy renders at the
    # largest render tier and finishes with an upscale (split_render_target).
    if resolution not in gapp._UPSCALE_RESOLUTIONS:
        raise HTTPException(400, "Choose a valid resolution.")
    dup = duplicate_script(DuplicateScriptBody(work_dir=body.work_dir, title=body.title))
    new_wd = Path(dup["work_dir"])
    # The Script editor pre-fills its resolution picker from the brief, so stamp
    # the target there too — otherwise reopening the copy shows the source's
    # resolution and a re-approve would quietly render the old size again.
    brief = _read_create_brief(new_wd)
    if brief:
        _write_create_brief(new_wd, {**brief, "resolution": resolution})
    try:
        minutes = float((dup.get("create_brief") or {}).get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0.0
    queued = queue_from_job(FromJobBody(
        job_id=dup["job_id"], work_dir=dup["work_dir"],
        video_title=dup.get("video_title") or dup.get("title") or "",
        n_scenes=len(dup.get("scenes") or []),
        minutes=minutes,
        style=dup.get("style") or "",
        resolution=resolution,
        voice=dup.get("voice") or "",
        music_desc=dup.get("music_desc") or "",
        style_name=dup.get("style_name") or "",
        approved=True,
    ))
    return {"ok": True, "job_id": dup["job_id"], "work_dir": dup["work_dir"],
            "title": dup.get("video_title") or dup.get("title") or "",
            "resolution": resolution,
            "queue_item_id": queued.get("queue_item_id"),
            "started": queued.get("started")}

# ── Restyle: swap a script's visual style without touching its content ───────
# The visual style is baked into every scene's image_prompt as a leading
# sentence (script generation and the render both prepend the composed style,
# and the editor shows prompts exactly as they render). That makes "same film,
# different look" a prompt-surgery job by hand — and an easy one to get wrong,
# since a re-render then reuses the cached first frames painted in the OLD
# look. _restyle_script does the whole swap in one place.

def _strip_style_prefix(text: str, prefixes: list[str]) -> str:
    """Remove every known style sentence leading *text* (repeatedly — an
    earlier by-hand restyle may have stacked two). Matching is case-insensitive
    and tolerant of the ". " / ", " joiner the prefix was glued on with."""
    out = (text or "").strip()
    cands = sorted({p.strip().rstrip(".").strip() for p in prefixes if p and p.strip()},
                   key=len, reverse=True)
    changed = True
    while changed and out:
        changed = False
        for cand in cands:
            if out.lower().startswith(cand.lower()):
                rest = out[len(cand):].lstrip()
                if rest[:1] in (".", ","):
                    rest = rest[1:]
                out = rest.strip()
                changed = True
                break
    return out


def _restyle_script(wd: Path, *, style_name: str, style: str,
                    repaint_cast: bool = True) -> dict:
    """Re-point an existing script at another visual style, in place.

    Every scene's image_prompt loses its baked style sentence and gains the
    new one (video prompts just lose the old one — the render adds the style
    at shoot time); acted scenes get their H3 prompt re-assembled under the
    new style note (a hand-edited prompt_override is left as written). The
    job's style_name / style sentence are updated everywhere the render reads
    them (store config + metadata, create_brief.json, job_config.json), and
    the images that carried the old look are retired so nothing reuses them:
    scene previews and first frames, the cover, and — when *repaint_cast* —
    the per-script cast portraits (the next render paints fresh ones). All of
    them stay in their version histories. Returns the Script-editor payload."""
    job_id = job_id_from_work_dir(wd)
    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, style_name)
    new_style_name = ss["name"]
    new_style = (style or "").strip().rstrip(".").strip()
    if not new_style:
        new_style = (ss.get("visual_style") or "").strip().rstrip(".").strip()
    new_prefix = gapp._compose_visual_style(new_style, cfg, new_style_name)

    fallback_title = wd.name.replace("-", " ").title()
    _title, old_style, _music, old_style_name, _brief = _script_source_meta(job_id, fallback_title)
    jc_path = wd / "job_config.json"
    jc: dict = {}
    if jc_path.exists():
        try:
            jc = json.loads(jc_path.read_text())
        except Exception:
            jc = {}
    # Everything that may ever have been glued onto a prompt's head: the exact
    # text a render stamped (style_prefix), the script's style sentence, the
    # old profile's visual style, and their compositions — plus the NEW ones,
    # so re-applying is idempotent.
    old_ss = gapp.style_settings(cfg, old_style_name or jc.get("style_name", ""))
    prefixes = [
        jc.get("style_prefix", ""), jc.get("style", ""), old_style,
        old_ss.get("visual_style", ""),
        gapp._compose_visual_style(old_style, cfg, old_ss["name"]),
        gapp._compose_visual_style(jc.get("style", ""), cfg, old_ss["name"]),
        new_style, ss.get("visual_style", ""), new_prefix,
    ]

    rows = _read_script_scenes(wd)
    retired: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if performance_mode.is_performance_mode(meta.get("mode")):
            if not (meta.get("prompt_override") or "").strip():
                acted = performance_mode.acted_meta(
                    {"metadata": meta, "lines": meta.get("lines") or [],
                     "video_prompt": row.get("video_prompt") or "",
                     "image_prompt": row.get("image_prompt") or ""})
                row["video_prompt"] = performance_mode.build_h3_prompt(
                    acted, style_note=new_style,
                    picture_names=list(acted.get("cast") or []),
                    audio_names=performance_mode.speakers_in(
                        acted.get("lines") or [])[:performance_mode.MAX_SPEAKERS_PER_SCENE])
            continue
        image_prompt = _strip_style_prefix(row.get("image_prompt") or "", prefixes)
        row["image_prompt"] = _apply_style_prefix(new_prefix, image_prompt) if image_prompt else ""
        row["video_prompt"] = _strip_style_prefix(row.get("video_prompt") or "", prefixes)
        # The look is in the pixels too: retire this scene's preview and first
        # frame (kept as history versions) so the render paints the new style.
        try:
            sid = int(row.get("id", 0))
        except (TypeError, ValueError):
            continue
        for suffix in ("_preview.png", "_first_frame.png"):
            p = wd / f"scene_{sid:02d}{suffix}"
            if p.exists():
                image_history.capture_current(wd, sid, p)
                p.unlink()
                retired.append(p.name)
    gapp._persist_script_snapshot(wd, rows)

    for name in ("cover.png", "cover_bg.png"):
        p = wd / name
        if p.exists():
            if name == "cover.png":
                image_history.cover_seed_if_empty(wd, p)
            p.unlink()
            retired.append(name)

    if repaint_cast:
        chars = gapp._read_script_characters(wd)
        touched = False
        for c in chars:
            if c.get("ref_image"):
                p = gapp._script_characters_dir(wd) / c["ref_image"]
                if p.exists():
                    image_history.char_seed_if_empty(wd, c["id"], p)
                c["ref_image"] = ""
                touched = True
                retired.append(f"look:{c.get('name') or c['id']}")
        if touched:
            gapp._write_script_characters(wd, chars)

    # The brief is what the Script editor and a re-draft read the style from.
    brief = _read_create_brief(wd)
    if brief:
        _write_create_brief(wd, {**brief, "style_name": new_style_name,
                                 "visual_style": new_style})
    # A rendered film's job_config is what the film editor's scene re-renders
    # and the resume path read; the per-style keys it carries are re-stamped
    # when the film is queued again.
    if jc:
        jc.update({"style": new_style, "style_name": new_style_name,
                   "style_prefix": new_prefix})
        jc_path.write_text(json.dumps(jc, indent=2))

    store = DurableStore.default()
    try:
        job = store.get_job(job_id)
        d = _row_to_dict(job) if job else {}
        config = json.loads(d.get("config_json") or "{}")
        metadata = json.loads(d.get("metadata_json") or "{}")
        config["style_name"] = new_style_name
        if isinstance(config.get("create_brief"), dict):
            config["create_brief"] = {**config["create_brief"],
                                      "style_name": new_style_name,
                                      "visual_style": new_style}
        metadata["style"] = new_style
        # (status is untouched on conflict — a finished film stays finished.)
        store.create_or_update_job(job_id, wd, d.get("title") or fallback_title,
                                   config=config, metadata=metadata)
        store.upsert_scenes(job_id, rows)
        for row in rows:
            try:
                store.update_scene_preview(job_id, int(row.get("id", 0)), "")
            except (TypeError, ValueError):
                pass
    finally:
        store.close()
    gapp.logger.info("Restyled %s → %r (%d images retired)", wd.name, new_style_name, len(retired))
    payload = load_script(work_dir=str(wd))
    payload["retired"] = retired
    return payload


class RestyleBody(BaseModel):
    style_name: str = ""
    # The film's visual-style sentence; blank = the picked style's own.
    style: str = ""
    repaint_cast: bool = True


@api.post("/api/jobs/{job_id}/restyle")
def restyle_job(job_id: str, body: RestyleBody) -> dict:
    """Swap this script's visual style in place (Script screen → Restyle).
    Returns the refreshed Script-editor payload."""
    wd = gapp._job_work_dir(job_id)
    if not wd or not Path(wd).exists():
        raise HTTPException(404, "Script folder not found.")
    return _restyle_script(Path(wd), style_name=body.style_name, style=body.style,
                           repaint_cast=body.repaint_cast)


class DuplicateRestyleBody(BaseModel):
    work_dir: str
    style_name: str = ""
    style: str = ""
    repaint_cast: bool = True
    title: str = ""


@api.post("/api/scripts/duplicate-restyle")
def duplicate_script_and_restyle(body: DuplicateRestyleBody) -> dict:
    """Duplicate a finished film, restyle the copy and queue it to render — the
    same script and cut in another look, kept as its own film (the film
    editor's "Restyle this film"). The original stays exactly as it is."""
    dup = duplicate_script(DuplicateScriptBody(work_dir=body.work_dir, title=body.title))
    new_wd = Path(dup["work_dir"])
    restyled = _restyle_script(new_wd, style_name=body.style_name, style=body.style,
                               repaint_cast=body.repaint_cast)
    brief = restyled.get("create_brief") or {}
    try:
        minutes = float(brief.get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0.0
    queued = queue_from_job(FromJobBody(
        job_id=restyled["job_id"], work_dir=restyled["work_dir"],
        video_title=restyled.get("video_title") or restyled.get("title") or "",
        n_scenes=len(restyled.get("scenes") or []),
        minutes=minutes,
        style=restyled.get("style") or "",
        resolution=restyled.get("resolution") or "",
        voice=restyled.get("voice") or "",
        music_desc=restyled.get("music_desc") or "",
        style_name=restyled.get("style_name") or "",
        approved=True,
    ))
    return {"ok": True, "job_id": restyled["job_id"], "work_dir": restyled["work_dir"],
            "title": restyled.get("video_title") or restyled.get("title") or "",
            "style_name": restyled.get("style_name") or "",
            "retired": restyled.get("retired") or [],
            "queue_item_id": queued.get("queue_item_id"),
            "started": queued.get("started")}


@api.get("/api/jobs/{job_id}/scenes")
def job_scenes(job_id: str) -> dict:
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    return {"scenes": [_scene_to_json(r, gapp._job_work_dir(job_id)) for r in rows]}


class SceneUpdate(BaseModel):
    title: str = ""
    image_prompt: str = ""
    video_prompt: str = ""
    narration: str = ""
    voice: str | None = None
    # Spoken-text override (optional; blank ⟹ TTS speaks the narration text —
    # stored in scene metadata, see pipeline/tts_text.py).
    tts_text: str | None = None
    # Dialogue/performance (optional; absent ⟹ narration — stored in scene metadata).
    mode: str | None = None
    lines: list | None = None
    duration: float | None = None
    # Hand-edited H3 prompt for an acted scene. "" clears the override and the
    # prompt goes back to being assembled from the scene's fields.
    prompt: str | None = None
    # Acted-scene fields — everything build_h3_prompt assembles the prompt FROM.
    # Editing these is how an acted scene is written; the prompt follows.
    setting: str | None = None
    camera: str | None = None
    # Acted scenes: True renders this scene from portraits only (every wardrobe
    # reference stands down); False/None restores the film's wardrobe.
    no_wardrobe: bool | None = None
    soundscape: str | None = None
    cast: list | None = None
    beats: list | None = None
    seconds: float | None = None
    # Singing scenes only: does the cast visibly perform the song on camera?
    # False is stored ("they don't sing in this shot"); True clears the key —
    # performing is the default, so metadata stays sparse.
    performs: bool | None = None
    # This scene picks up the PREVIOUS scene's shot without a cut. True is
    # stored; False clears the key — an ordinary cut is the default.
    continues_previous: bool | None = None


def _scene_style_note(job_id: str) -> str:
    """The style sentence prepended to an acted scene's assembled prompt."""
    try:
        cfg = gapp.load_config()
        store = DurableStore.default()
        try:
            row = store.get_job(job_id)
        finally:
            store.close()
        meta = json.loads(dict(row).get("metadata_json") or "{}") if row else {}
        name = json.loads(dict(row).get("config_json") or "{}").get("style_name", "") if row else ""
        return meta.get("style") or gapp.style_settings(cfg, name).get("visual_style", "") or ""
    except Exception:
        return ""


@api.put("/api/jobs/{job_id}/scenes/{scene_id}")
def update_scene(job_id: str, scene_id: int, body: SceneUpdate) -> dict:
    sid = int(scene_id)
    store = DurableStore.default()
    try:
        current = store.get_scene(job_id, sid) or {}
        meta = dict(current.get("metadata") or {})
        old_dialogue = {k: meta.get(k) for k in ("mode", "lines", "duration", "prompt_override")}
        # A mode change through ANY path — the convert endpoint, an old client,
        # a raw API call — first stashes the content of the mode being left,
        # and restores the target mode's stash when the request brings no
        # content of its own. A bare flip must never destroy a scene again.
        old_mode = str(meta.get("mode") or "narration").strip().lower()
        old_mode = "dialogue" if performance_mode.is_performance_mode(old_mode) else old_mode
        new_mode = (str(body.mode).strip().lower() or "narration") if body.mode is not None else old_mode
        new_mode = "dialogue" if performance_mode.is_performance_mode(new_mode) else new_mode
        if new_mode != old_mode and old_mode in _MODE_STASH_FIELDS:
            _stash_mode_content(meta, current, old_mode)
            stash = (meta.get("mode_stash") or {}).get(new_mode) or {}
            if new_mode == "dialogue" and not (body.lines or meta.get("lines")):
                for k in ("lines", "cast", "setting", "camera", "soundscape",
                          "beats", "seconds", "prompt_override"):
                    if stash.get(k):
                        meta[k] = stash[k]
                if stash.get("lines") and body.lines is not None:
                    body.lines = list(stash["lines"])
            elif new_mode == "narration" and not (body.narration or "").strip():
                body.narration = stash.get("narration") or ""
                if not (body.image_prompt or "").strip():
                    body.image_prompt = stash.get("image_prompt") or ""
                if not (body.video_prompt or "").strip() or \
                        (body.video_prompt or "").startswith("["):
                    body.video_prompt = stash.get("video_prompt") or ""
            elif new_mode == "silent":
                if not (body.image_prompt or "").strip():
                    body.image_prompt = stash.get("image_prompt") or ""
                if not (body.video_prompt or "").strip() or \
                        (body.video_prompt or "").startswith("["):
                    body.video_prompt = stash.get("video_prompt") or ""
        if body.voice is not None:
            voice = (body.voice or "").strip()
            if voice:
                meta["voice"] = voice
            else:
                meta.pop("voice", None)
        if body.tts_text is not None:
            tt = (body.tts_text or "").strip()
            if tt:
                meta["tts_text"] = tt
            else:
                meta.pop("tts_text", None)
        # Dialogue fields: store only when non-default so narration scenes' metadata
        # (and thus script.json) stay byte-identical.
        if body.mode is not None:
            m = (body.mode or "").strip() or "narration"
            if m != "narration":
                meta["mode"] = m
            else:
                meta.pop("mode", None)
        if body.lines is not None:
            ls = _clean_lines(body.lines)
            if ls:
                meta["lines"] = ls
            else:
                meta.pop("lines", None)
        if body.duration is not None:
            d = float(body.duration or 0)
            if d > 0:
                meta["duration"] = d
            else:
                meta.pop("duration", None)
        if body.prompt is not None:
            pr = (body.prompt or "").strip()
            if pr:
                meta["prompt_override"] = pr
            else:
                meta.pop("prompt_override", None)
        for field, value in (("setting", body.setting), ("camera", body.camera),
                             ("soundscape", body.soundscape)):
            if value is not None:
                text = (value or "").strip()
                if text:
                    meta[field] = text
                else:
                    meta.pop(field, None)
        if body.no_wardrobe is not None:
            if body.no_wardrobe:
                meta["no_wardrobe"] = True
            else:
                meta.pop("no_wardrobe", None)
        if body.continues_previous is not None:
            # Stored as sent — "first scene" is a POSITION, not an id (the film
            # editor reorders without renumbering), so validity is the render's
            # call (continuity.continuation_plan drops what cannot be honoured)
            # and the editors hide the toggle at position one.
            if body.continues_previous:
                meta["continues_previous"] = True
            else:
                meta.pop("continues_previous", None)
        if body.cast is not None:
            meta["cast"] = [str(n).strip() for n in body.cast if str(n).strip()]
        if body.beats is not None:
            meta["beats"] = performance_mode.norm_beats(
                body.beats, float(body.seconds or meta.get("seconds")
                                  or performance_mode.SCENE_SECONDS))
        if body.seconds is not None:
            secs = float(body.seconds or 0)
            if secs > 0:
                meta["seconds"] = secs
            else:
                meta.pop("seconds", None)
        if body.performs is not None:
            if body.performs:
                meta.pop("performs", None)
            else:
                meta["performs"] = False
        # An acted scene is written through its FIELDS: what is said becomes the
        # narration text (nothing speaks it — TTS is skipped), and the video
        # prompt is assembled from cast/setting/beats/lines rather than typed,
        # so nothing has to be written twice. A hand-edited prompt still wins
        # (build_h3_prompt honours prompt_override).
        narration, image_prompt, video_prompt = (
            body.narration, body.image_prompt, body.video_prompt)
        if performance_mode.is_performance_mode(meta.get("mode")):
            acted = performance_mode.acted_meta(
                {"metadata": meta, "lines": meta.get("lines") or [],
                 "video_prompt": video_prompt, "image_prompt": image_prompt})
            meta.update({k: acted[k] for k in ("cast", "seconds", "beats", "setting")})
            narration = performance_mode.spoken_text(acted)
            # Cast names stand in for the slots (same as script generation) —
            # the renderer rebuilds with the actually-resolved references.
            video_prompt = performance_mode.build_h3_prompt(
                acted, style_note=_scene_style_note(job_id),
                picture_names=list(acted.get("cast") or []),
                audio_names=performance_mode.speakers_in(
                    acted.get("lines") or [])[:performance_mode.MAX_SPEAKERS_PER_SCENE])
            # No image engine runs for an acted scene — the setting lives in
            # metadata, where the prompt and the scene-visual painter read it.
            image_prompt = ""
        store.upsert_scene(
            job_id,
            sid,
            title=body.title,
            image_prompt=image_prompt,
            video_prompt=video_prompt,
            narration=narration,
            preview_path=current.get("preview_path", ""),
            metadata=meta,
        )
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    work_dir = gapp._job_work_dir(job_id)
    if work_dir:
        gapp._persist_script_snapshot(work_dir, rows)
        # A scene whose mode/lines/duration changed must not reuse its previously
        # rendered files — the resume path skips scenes whose final already exists,
        # which would silently serve the OLD (e.g. narrated) take. A FINISHED film
        # keeps its clips: they are the deliverable, and the film editor re-renders
        # the scene on request rather than leaving a hole in the cut.
        if ({k: meta.get(k) for k in ("mode", "lines", "duration", "prompt_override")} != old_dialogue
                and not (Path(work_dir) / "combined.mp4").exists()):
            wd = Path(work_dir)
            stale = [wd / f"scene_{sid:02d}_final.mp4", wd / f"scene_{sid:02d}_narration.wav",
                     wd / f"scene_{sid:02d}_establish.mp4"]
            stale += list(wd.glob(f"scene_{sid:02d}_line_*"))
            for p in stale:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
    # The saved row, so the editor can adopt the server-assembled prompt and
    # narration without a second fetch.
    fresh = next((r for r in rows if int(r.get("id") or 0) == sid), None)
    return {"ok": True,
            "scene": _scene_to_json(fresh, Path(work_dir) if work_dir else None)
                     if fresh else None}


# ── scene structure: add / remove / reorder (issue #193) ─────────────────────
# The pre-render pipeline treats scene id order as THE order everywhere (render,
# assembly, captions, scene_NN_* filenames), so structural edits renumber ids to
# 1..N and rename every scene-numbered artifact to follow, instead of layering
# an order sidecar over the whole pipeline. The film editor's post-render
# scene_edit_order.json keeps its stable-id convention untouched.

_SCENE_FILE_RE = re.compile(r"^scene_(\d+)_(.+)$")


def _work_dir_render_active(wd: Path) -> bool:
    """True while this work dir's render is in flight (job_config written, no
    combined.mp4 yet, not terminally stamped) — mirrors _is_job_running's
    per-directory logic, including its 24 h staleness cutoff so a phantom dir
    can't lock the editor forever."""
    cfg_file = wd / "job_config.json"
    if not cfg_file.exists() or (wd / "combined.mp4").exists():
        return False
    try:
        if cfg_file.stat().st_mtime <= time.time() - 86400:
            return False
    except OSError:
        return False
    try:
        status = json.loads((wd / "job.json").read_text()).get("status")
    except Exception:
        status = None
    return status not in ("error", "cancelled", "paused", "done")


def _remap_scene_files(wd: Path, id_map: dict) -> None:
    """Rename scene_NN_* files to follow renumbered scene ids, in the work dir
    and any localized copies under localize/*/. *id_map* maps old id → new id
    (None deletes the files — removed scene). Two-phase rename so swapped ids
    never overwrite each other; renamed files are touched so mtime-cache-busted
    URLs change with the content behind them."""
    loc_root = wd / "localize"
    dirs = [wd] + ([d for d in sorted(loc_root.iterdir()) if d.is_dir()] if loc_root.is_dir() else [])
    for d in dirs:
        renames: list[tuple[Path, Path]] = []
        for f in d.iterdir():
            if not f.is_file():
                continue
            m = _SCENE_FILE_RE.match(f.name)
            if not m or int(m.group(1)) not in id_map:
                continue
            new_id = id_map[int(m.group(1))]
            if new_id is None:
                f.unlink(missing_ok=True)
            elif int(m.group(1)) != new_id:
                renames.append((f, d / f"scene_{new_id:02d}_{m.group(2)}"))
        staged = []
        for src, dst in renames:
            tmp = dst.with_name(dst.name + ".remap-tmp")
            src.replace(tmp)
            staged.append((tmp, dst))
        for tmp, dst in staged:
            tmp.replace(dst)
            dst.touch()


def _remap_preview_path(preview_path: str, new_id: int) -> str:
    """Re-point a stored preview_path at its renamed scene_NN_* file."""
    if not preview_path:
        return ""
    p = Path(preview_path)
    m = _SCENE_FILE_RE.match(p.name)
    if not m:
        return preview_path
    return str(p.with_name(f"scene_{new_id:02d}_{m.group(2)}"))


def _restructure_job_scenes(job_id: str, sequence: list) -> list[dict]:
    """Rebuild a job's scenes from *sequence*: existing scene ids in their new
    order, with None marking a brand-new blank scene at that position. Renumbers
    to 1..N, swaps the DB rows, renames the scene-numbered artifacts, remaps the
    image/video history manifests, and rewrites script.json. Returns the fresh
    scene rows."""
    work_dir = gapp._job_work_dir(job_id)
    if not work_dir:
        raise HTTPException(404, "Unknown job.")
    wd = Path(work_dir)
    if _work_dir_render_active(wd):
        raise HTTPException(409, "This script is rendering right now — wait for it to finish (or cancel it) first.")
    if not sequence:
        raise HTTPException(400, "A script needs at least one scene.")

    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
        by_id = {int(r["id"]): r for r in rows}
        old_ids = [i for i in sequence if i is not None]
        if len(set(old_ids)) != len(old_ids) or any(i not in by_id for i in old_ids):
            raise HTTPException(400, "Scene list is out of date — reload the script and try again.")

        id_map = {old: None for old in by_id}
        new_rows = []
        for pos, item in enumerate(sequence, start=1):
            if item is None:
                new_rows.append({"id": pos, "title": "", "image_prompt": "", "video_prompt": "",
                                 "narration": "", "preview_path": "", "metadata": {}})
                continue
            id_map[item] = pos
            r = dict(by_id[item])
            r["id"] = pos
            r["preview_path"] = _remap_preview_path(r.get("preview_path") or "", pos)
            new_rows.append(r)

        store.replace_scenes(job_id, new_rows)
        rows = store.scene_rows(job_id)
    finally:
        store.close()

    _remap_scene_files(wd, id_map)
    image_history.remap_scene_ids(wd, id_map)
    video_history.remap_scene_ids(wd, id_map)
    # Id order IS the order again — a film-editor order sidecar would now point
    # at renumbered ids, so retire it.
    (wd / "scene_edit_order.json").unlink(missing_ok=True)
    gapp._persist_script_snapshot(wd, rows)
    return rows


class SceneAddBody(BaseModel):
    after_scene_id: int = 0  # insert after this scene; 0 ⟹ append at the end


@api.post("/api/jobs/{job_id}/scenes/add")
def add_job_scene(job_id: str, body: SceneAddBody) -> dict:
    store = DurableStore.default()
    try:
        ids = [int(r["id"]) for r in store.scene_rows(job_id)]
    finally:
        store.close()
    sequence: list = list(ids)
    pos = sequence.index(body.after_scene_id) + 1 if body.after_scene_id in sequence else len(sequence)
    sequence.insert(pos, None)
    rows = _restructure_job_scenes(job_id, sequence)
    wd = gapp._job_work_dir(job_id)
    return {"scenes": [_scene_to_json(r, wd) for r in rows], "new_scene_id": pos + 1}


@api.delete("/api/jobs/{job_id}/scenes/{scene_id}")
def delete_job_scene(job_id: str, scene_id: int) -> dict:
    store = DurableStore.default()
    try:
        ids = [int(r["id"]) for r in store.scene_rows(job_id)]
    finally:
        store.close()
    if int(scene_id) not in ids:
        raise HTTPException(404, f"Scene {scene_id} not found.")
    rows = _restructure_job_scenes(job_id, [i for i in ids if i != int(scene_id)])
    wd = gapp._job_work_dir(job_id)
    return {"scenes": [_scene_to_json(r, wd) for r in rows]}


class SceneReorderBody(BaseModel):
    order: list


@api.post("/api/jobs/{job_id}/scenes/reorder")
def reorder_job_scenes(job_id: str, body: SceneReorderBody) -> dict:
    store = DurableStore.default()
    try:
        ids = [int(r["id"]) for r in store.scene_rows(job_id)]
    finally:
        store.close()
    order = [int(x) for x in body.order]
    if sorted(order) != sorted(ids):
        raise HTTPException(400, "Reorder must include every scene exactly once — reload the script and try again.")
    rows = _restructure_job_scenes(job_id, order)
    wd = gapp._job_work_dir(job_id)
    return {"scenes": [_scene_to_json(r, wd) for r in rows]}


# ── scene preview (FLUX first frame) ─────────────────────────────────────────

@api.post("/api/jobs/{job_id}/scenes/{scene_id}/preview")
def regen_scene_preview(job_id: str, scene_id: int, resolution: str = "", style: str = "",
                        instruction: str = "") -> dict:
    try:
        with _track_op("Generating preview", f"scene {scene_id}"):
            out = gapp._generate_active_scene_preview(
                job_id, int(scene_id), resolution, style, "", "", force=True,
                instruction=instruction,
            )
    except Exception as e:
        raise HTTPException(503, f"Preview failed: {str(e).splitlines()[0][:200]}")
    wd = gapp._job_work_dir(job_id)
    hist = image_history.history(wd, int(scene_id)) if wd else None
    return {"ok": True, "preview_path": str(out), "history": hist}


@api.post("/api/jobs/{job_id}/scenes/{scene_id}/preview-remove")
def remove_scene_preview(job_id: str, scene_id: int) -> dict:
    """Delete a scene's current first-frame image. For an acted scene that
    stops it riding as the take's opening-composition reference; kept history
    versions survive, so re-selecting one brings it back."""
    wd = _job_wd_or_404(job_id)
    sid = int(scene_id)
    # Keep the frames being removed as history versions — re-selecting one must
    # work even for a frame painted at render time that was never recorded.
    for f in (wd / f"scene_{sid:02d}_preview.png", wd / f"scene_{sid:02d}_first_frame.png"):
        image_history.capture_current(wd, sid, f)
        f.unlink(missing_ok=True)
    store = DurableStore.default()
    try:
        row = store.get_scene(job_id, sid) or {}
        if row:
            store.upsert_scene(job_id, sid,
                               title=row.get("title") or "",
                               image_prompt=row.get("image_prompt") or "",
                               video_prompt=row.get("video_prompt") or "",
                               narration=row.get("narration") or "",
                               preview_path="",
                               metadata=dict(row.get("metadata") or {}))
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    # A film whose scenes live only in script.json has no store rows — writing
    # the empty snapshot would wipe the script it is the sole copy of.
    if rows:
        gapp._persist_script_snapshot(wd, rows)
    return {"ok": True}


@api.post("/api/jobs/{job_id}/previews")
def generate_all_previews(job_id: str, resolution: str = Query(""), style: str = Query(""), force: bool = Query(False)) -> dict:
    """Generate first-frame previews for all scenes (or only missing ones when force=False).
    Pass force=True to regenerate every scene even if a preview already exists."""
    import concurrent.futures

    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    if not rows:
        return {"scenes": [], "generated": 0, "failed": []}
    wd = gapp._job_work_dir(job_id)
    # An acted scene needs no first frame — it is conditioned on the character
    # portraits — and painting one anyway SUPERSEDES the scene's location
    # references (resolve_performance_references drops them when a frame
    # exists), silently resurrecting a frame the user removed. Guarded with the
    # renderer's own predicate: a singing scene and an acted-silent one carry
    # mode "silent", which a mode-only check misses. A mixed film still gets
    # stills for its narrated scenes.
    ctx = (_acted_scene_ctx(wd) if wd is not None
           else {"acted_cfg": {"h3_silent_scenes": False}, "first_frames": False})
    # …unless the style opens every acted scene on a painted frame
    # (h3_first_frames) — then the acted scenes get one here too, composed from
    # their setting where no image prompt exists.
    needs_frame = [r for r in rows
                   if ctx["first_frames"] or not performance_mode.renders_acted(
                       {"metadata": dict(r.get("metadata") or {})}, ctx["acted_cfg"])]
    if not needs_frame:
        return {"scenes": [], "generated": 0, "failed": [],
                "skipped": "every scene is acted — none has a first frame"}

    to_generate = needs_frame if force else [
        r for r in needs_frame if not (r.get("preview_path") and Path(r["preview_path"]).exists())]
    failed: list[int] = []
    if to_generate:
        worker_urls = gapp._preview_worker_urls()
        if not worker_urls:
            raise HTTPException(503, "No reachable workers for preview generation.")
        pool = gapp.WorkerPool(worker_urls)
        with _track_op("Generating previews", f"{len(to_generate)} scenes"), \
                concurrent.futures.ThreadPoolExecutor(max_workers=min(len(worker_urls), len(to_generate))) as ex:
            futs = {
                ex.submit(gapp._generate_active_scene_preview, job_id, int(r["id"]),
                          resolution, style, r.get("title") or "",
                          r.get("image_prompt") or "", force=force, worker_pool=pool): int(r["id"])
                for r in to_generate
            }
            for fut in concurrent.futures.as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    failed.append(futs[fut])
        store = DurableStore.default()
        try:
            rows = store.scene_rows(job_id)
        finally:
            store.close()

    return {"scenes": [_scene_to_json(r, wd) for r in rows],
            "generated": len(to_generate) - len(failed), "failed": failed}


class PreviewSelectBody(BaseModel):
    version_id: int


@api.post("/api/jobs/{job_id}/scenes/{scene_id}/preview-select")
def select_scene_preview(job_id: str, scene_id: int, body: PreviewSelectBody) -> dict:
    """Make a previously-kept image version the selected one for this scene."""
    wd = gapp._job_work_dir(job_id)
    if wd is None:
        raise HTTPException(404, "No work directory for this job.")
    try:
        out = image_history.select(wd, int(scene_id), int(body.version_id))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    store = DurableStore.default()
    try:
        store.update_scene_preview(job_id, int(scene_id), out)
    finally:
        store.close()
    return {"ok": True, "preview_path": str(out),
            "history": image_history.history(wd, int(scene_id))}


@api.post("/api/jobs/{job_id}/scenes/{scene_id}/preview-delete")
def delete_scene_preview(job_id: str, scene_id: int, body: PreviewSelectBody) -> dict:
    """Delete a kept image version (the one in use can't be deleted)."""
    wd = gapp._job_work_dir(job_id)
    if wd is None:
        raise HTTPException(404, "No work directory for this job.")
    try:
        hist = image_history.delete(wd, int(scene_id), int(body.version_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "history": hist}


# ── masked image edit (FLUX inpaint) ─────────────────────────────────────────

class InpaintBody(BaseModel):
    mask: str                       # base64 PNG data-URL, white = the region to change
    prompt: str                     # plain-language description of the change
    denoise: float | None = None    # edit strength 0.3–1.0 (lower keeps more of the original)


def _decode_data_url(data: str) -> bytes:
    """Decode a base64 data-URL (or bare base64 string) into raw bytes."""
    s = (data or "").strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    return base64.b64decode(s)


def _run_scene_inpaint(wd: Path, sid: int, base: Path, prompt: str, mask_data: str, job_id: str,
                       engine: dict, denoise: float | None = None, op_id: str = "") -> dict:
    """Run a masked image edit on a scene image and record it as a new version.

    Shared by the Script and Film inpaint endpoints. *engine* is the resolved edit
    engine (see pipeline/engines.py). *base* is the image to edit; the result
    overwrites the canonical preview (and the first frame, if one exists) and
    becomes the selected image-history version. *denoise* is the edit strength
    (lower keeps more of the original); ``None`` uses the engine's default."""
    import shutil
    from pipeline.comfyui import edit_with_engine

    mask_bytes = _decode_data_url(mask_data)
    if not mask_bytes:
        raise HTTPException(400, "No mask was provided.")

    worker_urls = gapp._preview_worker_urls()
    if not worker_urls:
        raise HTTPException(503, "No reachable workers for image editing.")

    # Keep the prompt bounded — a very long prompt blows up the text encoder's
    # activation memory on a contended GPU. ~1000 chars is plenty for a local edit.
    prompt = (prompt or "").strip()[:1000]
    dn = None if denoise is None else max(0.3, min(1.0, float(denoise)))

    # Preserve the current images so the user can return to them (mirrors
    # regen/rerender — the edit overwrites the preview and syncs the first
    # frame, and the two can differ).
    image_history.capture_current(wd, sid, wd / f"scene_{sid:02d}_preview.png")
    image_history.capture_current(wd, sid, wd / f"scene_{sid:02d}_first_frame.png")

    out = wd / f"scene_{sid:02d}_preview.png"
    mask_tmp = wd / f"_inpaint_mask_{sid:02d}.png"
    mask_tmp.write_bytes(mask_bytes)

    pool = gapp.WorkerPool(worker_urls)
    url = _acquire_op_worker(pool, op_id)
    try:
        edit_with_engine(engine, prompt, base, mask_tmp, out, denoise=dn, comfy_url=url)
    finally:
        pool.release(url)
        mask_tmp.unlink(missing_ok=True)

    # Keep the first frame in sync so a later video re-render uses the edited image.
    first_frame = wd / f"scene_{sid:02d}_first_frame.png"
    if first_frame.exists():
        shutil.copy2(out, first_frame)

    store = DurableStore.default()
    try:
        store.update_scene_preview(job_id, sid, out)
    finally:
        store.close()
    hist = image_history.record(wd, sid, out)
    return {"ok": True, "preview_path": str(out), "history": hist}


@api.post("/api/jobs/{job_id}/scenes/{scene_id}/inpaint")
def inpaint_scene_preview(job_id: str, scene_id: int, body: InpaintBody) -> dict:
    """Masked FLUX edit of a scene's first-frame image (Script editor)."""
    wd = gapp._job_work_dir(job_id)
    if wd is None:
        raise HTTPException(404, "No work directory for this job.")
    sid = int(scene_id)
    base = wd / f"scene_{sid:02d}_preview.png"
    if not base.exists():
        base = wd / f"scene_{sid:02d}_first_frame.png"
    if not base.exists():
        raise HTTPException(400, "Generate the scene image first, then edit it.")
    edit = (body.prompt or "").strip()[:700]
    if not edit:
        raise HTTPException(400, "Describe the change to make.")

    # Resolve the job's style profile so the edit stays on-style (mirrors preview gen).
    cfg = gapp.load_config()
    style_name = ""
    store = DurableStore.default()
    try:
        job_row = store.get_job(job_id)
    finally:
        store.close()
    if job_row is not None:
        try:
            style_name = json.loads(dict(job_row).get("config_json") or "{}").get("style_name", "")
        except Exception:
            style_name = ""
    combined_style = gapp._compose_visual_style("", cfg, style_name)
    # Lead with the edit (it gets the most weight) and trail the style for coherence.
    prompt = f"{edit}. {combined_style}" if combined_style else edit
    engine = gapp.engines.resolve(cfg, gapp.style_settings(cfg, style_name).get("edit_engine"))

    try:
        with _track_op("Editing image", f"scene {sid} · {engine['key']}") as op_id:
            return _run_scene_inpaint(wd, sid, base, prompt, body.mask, job_id, engine,
                                      denoise=body.denoise, op_id=op_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"Image edit failed: {str(e).splitlines()[0][:300]}")


# ── image engines: list, availability, and automated model install ───────────

_model_install_tasks: dict[str, dict] = {}
_model_install_lock = threading.Lock()


def _comfy_hosts(cfg: dict) -> list[str]:
    """Unique SSH hosts running ComfyUI workers (where model files must land)."""
    hosts: list[str] = []
    for entry in cfg.get("comfy_workers", []) or []:
        h = _host_of(entry)
        if h and h not in hosts:
            hosts.append(h)
    return hosts


@api.get("/api/models/engines")
def list_engines() -> dict:
    """Engine registry for the Settings picker, plus best-effort availability
    (probed on a representative reachable worker via ComfyUI /object_info)."""
    from pipeline import engines as eng
    from pipeline.comfyui import comfy_node_exists, engine_model_present
    from pipeline.worker_pool import queue_depth
    cfg = gapp.load_config()
    probe_url = next((u for u in (cfg.get("comfy_workers") or []) if queue_depth(u, timeout=3) >= 0), None)
    availability = {k: (engine_model_present(probe_url, e.get("probe")) if probe_url else None)
                    for k, e in eng.ENGINES.items()}

    def _video_avail(e: dict) -> bool | None:
        """Model file present AND (when the engine needs newer ComfyUI nodes)
        the node class registered — False pinpoints "worker needs an update"."""
        if not probe_url:
            return None
        ok = engine_model_present(probe_url, e.get("probe"))
        node = e.get("requires_node")
        if ok and node:
            has_node = comfy_node_exists(probe_url, node)
            if has_node is not True:
                return has_node
        # The probe names the engine's UNET, which LoRA engines share with their
        # non-LoRA siblings — so a missing LoRA file (e.g. the converted LightX2V
        # one that only the installer writes) would still read "installed" and
        # the render would fail workflow validation. Any stock lora loader's
        # enum lists the same models/loras folder, so check membership there.
        if ok and e.get("lora"):
            has_lora = engine_model_present(
                probe_url, ("LoraLoaderModelOnly", "lora_name", e["lora"]))
            if has_lora is not True:
                return has_lora
        return ok

    return {
        "engines": eng.public_list(),
        "availability": availability,
        "default_engine": eng.DEFAULT_ENGINE,
        "video_engines": eng.public_list_video(),
        "video_availability": {k: _video_avail(e) for k, e in eng.VIDEO_ENGINES.items()},
        "default_video_engine": eng.DEFAULT_VIDEO_ENGINE,
        # Ref2VA models (performance films) — same availability map above.
        "reference_engines": eng.public_list_reference(),
        "default_reference_engine": eng.DEFAULT_REFERENCE_ENGINE,
        # Background-music models — same availability rule (weights present AND,
        # for MiniMax Music 3, the ComfyUI nodes registered).
        "music_engines": eng.public_list_music(),
        "music_availability": {k: _video_avail(e) for k, e in eng.MUSIC_ENGINES.items()},
        "default_music_engine": eng.DEFAULT_MUSIC_ENGINE,
        "hf_token_set": bool((cfg.get("hf_token") or "").strip()),
        "probed": probe_url,
    }


def _install_engine_worker(task_id: str, engine_key: str, hosts: list[str], hf_token: str) -> None:
    """Background: download an engine's model files onto each ComfyUI host over SSH.

    Reuses the proven scripts/download_models.sh (which resolves the hf CLI from
    ~/github/comfyui-env and flattens split_files/ paths) in its targeted
    ENGINE_MODELS mode — piped to `ssh host bash -s`. Long-running (weights are GBs)."""
    import shlex
    from pipeline import engines as eng
    e = eng.get(engine_key) or eng.get_video(engine_key) or eng.get_music(engine_key) or {}
    spec = ";".join(f'{m["repo"]}|{m["remote"]}|{m["dir"]}' for m in e.get("models", []))
    try:
        script_text = (REPO_ROOT / "scripts" / "download_models.sh").read_text()
    except Exception as ex:
        with _model_install_lock:
            _model_install_tasks[task_id] = {"status": "error", "engine": engine_key,
                                             "hosts": {}, "error": f"download_models.sh unreadable: {ex}"}
        return
    env = f"ENGINE_MODELS={shlex.quote(spec)} "
    if hf_token:
        env += f"HF_TOKEN={shlex.quote(hf_token)} "
    results: dict[str, dict] = {}
    for host in hosts:
        try:
            # localhost workers (single-machine setup) have no SSH — run locally.
            if host in ("localhost", "127.0.0.1"):
                cmd = ["bash", "-c", f"{env}bash -s"]
            else:
                cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", host, f"{env}bash -s"]
            proc = subprocess.run(
                cmd, input=script_text, capture_output=True, text=True, timeout=6 * 3600)
            results[host] = {"ok": proc.returncode == 0,
                             "log": (proc.stdout + proc.stderr).strip()[-3000:]}
        except Exception as ex:
            results[host] = {"ok": False, "log": str(ex)}
        with _model_install_lock:
            _model_install_tasks[task_id] = {"status": "running", "engine": engine_key, "hosts": dict(results)}
    with _model_install_lock:
        ok = bool(results) and all(r["ok"] for r in results.values())
        _model_install_tasks[task_id] = {"status": "done" if ok else "error",
                                         "engine": engine_key, "hosts": results}


class EngineInstallBody(BaseModel):
    engine: str


@api.post("/api/models/install")
def install_engine(body: EngineInstallBody) -> dict:
    """Kick off an async download of an engine's models onto every ComfyUI worker."""
    from pipeline import engines as eng
    e = eng.get(body.engine) or eng.get_video(body.engine) or eng.get_music(body.engine)
    if not e:
        raise HTTPException(400, f"Unknown engine: {body.engine!r}")
    if not e.get("models"):
        raise HTTPException(400, "This engine's models are part of the bulk worker install.")
    cfg = gapp.load_config()
    hosts = _comfy_hosts(cfg)
    if not hosts:
        raise HTTPException(400, "No ComfyUI workers configured.")
    hf_token = (cfg.get("hf_token") or "").strip()
    if any(m.get("gated") for m in e.get("models", [])) and not hf_token:
        raise HTTPException(400, "This engine's models are gated — set a Hugging Face token in Settings first.")
    task_id = f"modelinstall_{body.engine}_{int(time.time())}"
    _model_install_tasks[task_id] = {"status": "running", "engine": body.engine, "hosts": {}}
    threading.Thread(target=_install_engine_worker, args=(task_id, body.engine, hosts, hf_token), daemon=True).start()
    return {"ok": True, "task_id": task_id, "hosts": hosts}


@api.get("/api/models/install/status")
def install_engine_status(task_id: str = Query(...)) -> dict:
    t = _model_install_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "Task not found.")
    return {"ok": True, **t}


# ── TTS narration models (per-style voice engine) ────────────────────────────

@api.get("/api/models/tts-engines")
def list_tts_engines() -> dict:
    """TTS engine registry for the Settings picker, plus per-worker availability
    (which narration models are already downloaded on each tts_worker)."""
    from pipeline import tts_engines as te
    cfg = gapp.load_config()
    availability: dict[str, dict | None] = {}
    for url in (cfg.get("tts_workers") or []):
        if not str(url).startswith(("http://", "https://")):
            continue
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=4) as r:
                availability[url] = json.loads(r.read()).get("cached", {})
        except Exception:
            availability[url] = None
    return {
        "engines": te.public_list(),
        "availability": availability,
        "default_engine": te.DEFAULT_TTS_ENGINE,
    }


class TTSInstallBody(BaseModel):
    engine: str


def _prewarm_tts_worker(task_id: str, engine_key: str, hosts: list[str]) -> None:
    """Background: POST /prewarm to each tts_worker so it downloads the engine's
    weights into its HF cache. Long-running (weights are GBs)."""
    results: dict[str, dict] = {}
    payload = json.dumps({"engine": engine_key}).encode()
    for url in hosts:
        try:
            req = urllib.request.Request(url.rstrip("/") + "/prewarm", data=payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=3 * 3600) as r:
                results[url] = {"ok": True, "log": r.read().decode(errors="replace")[-1000:]}
        except Exception as ex:
            results[url] = {"ok": False, "log": str(ex)}
        with _model_install_lock:
            _model_install_tasks[task_id] = {"status": "running", "engine": engine_key, "hosts": dict(results)}
    with _model_install_lock:
        ok = bool(results) and all(r["ok"] for r in results.values())
        _model_install_tasks[task_id] = {"status": "done" if ok else "error", "engine": engine_key, "hosts": results}


@api.post("/api/models/tts-install")
def install_tts_engine(body: TTSInstallBody) -> dict:
    """Kick off a download (pre-warm) of a TTS engine's weights on every tts_worker."""
    from pipeline import tts_engines as te
    if not te.get(body.engine):
        raise HTTPException(400, f"Unknown TTS engine: {body.engine!r}")
    cfg = gapp.load_config()
    hosts = [u for u in (cfg.get("tts_workers") or []) if str(u).startswith(("http://", "https://"))]
    if not hosts:
        raise HTTPException(400, "No http:// TTS workers configured.")
    task_id = f"ttsinstall_{body.engine}_{int(time.time())}"
    _model_install_tasks[task_id] = {"status": "running", "engine": body.engine, "hosts": {}}
    threading.Thread(target=_prewarm_tts_worker, args=(task_id, body.engine, hosts), daemon=True).start()
    return {"ok": True, "task_id": task_id, "hosts": hosts}


# ── per-field LLM regeneration (Script tab "Re-generate" buttons) ─────────────

_FIELD_INSTRUCTIONS = {
    "title": "Write ONE short, vivid scene title (max ~8 words). Return only the title — no quotes, no label.",
    "narration": "Rewrite the narration for this scene in an engaging documentary voice, consistent with the video topic and the surrounding scenes. Return only the narration text.",
    "image_prompt": "Write a single detailed text-to-image (FLUX) prompt for this scene's first frame: highly detailed, static, incorporating the visual style. Return only the prompt.",
    "video_prompt": "Write a single concise video-motion (LTX) prompt for this scene describing camera movement and motion. Return only the prompt.",
}


def _llm_complete(system: str, user: str, cfg: dict, max_tokens: int = 700) -> str:
    """Lightweight direct LLM call honouring the configured backend.

    Delegates to pipeline.llm._chat_complete so Claude / Grok / local stay in sync.
    """
    from pipeline.llm import _chat_complete
    return _chat_complete(cfg, system, user, max_tokens=max_tokens, label="field_regen")


def _instruction_note(instruction: str, *, label: str = "instruction from the user") -> str:
    """Format an optional free-text steering instruction for an LLM user-prompt.

    Powers the "tell it how" popover on every Re-generate button: the user types
    something like "shorten it" or "make it funnier" and it's appended to the
    prompt. Empty/whitespace returns '' so an un-guided regen is byte-identical to
    before (and stays cacheable)."""
    instruction = (instruction or "").strip()[:500]
    if not instruction:
        return ""
    return (f"\n\nAdditional {label} — follow it, overriding the guidance above "
            f"where they conflict: {instruction}")


class FieldRegenBody(BaseModel):
    title: str = ""
    narration: str = ""
    image_prompt: str = ""
    video_prompt: str = ""
    instruction: str = ""   # optional "tell it how" steering (Re-generate popover)


@api.post("/api/jobs/{job_id}/scenes/{scene_id}/regenerate-field")
def regenerate_field(job_id: str, scene_id: int, field: str = Query(...),
                     body: FieldRegenBody | None = None) -> dict:
    body = body or FieldRegenBody()
    if field not in _FIELD_INSTRUCTIONS:
        raise HTTPException(400, f"Unknown field: {field}")
    cfg = gapp.load_config()

    video_title, topic, style, style_name, outline = "", "", "", "", ""
    jc = {}
    try:
        store = DurableStore.default()
        try:
            job = store.get_job(job_id)
            rows = store.scene_rows(job_id)
        finally:
            store.close()
        if job:
            d = _row_to_dict(job)
            jc = json.loads(d.get("config_json") or "{}")
            jm = json.loads(d.get("metadata_json") or "{}")
            video_title = jc.get("video_title") or d.get("title") or ""
            topic = jc.get("topic") or ""
            style = jm.get("style") or ""
            style_name = jc.get("style_name", "")
        outline = "; ".join(f"{int(r['id'])}. {r.get('title') or ''}" for r in rows)
    except Exception:
        pass

    # A regenerated narration must keep the script's scene word caps (10–15 s
    # at the narrator's cadence) — the plan it was generated with, else the
    # style's current cadence.
    length_note = ""
    if field == "narration":
        plan = (jc.get("create_brief") or {}).get("scene_plan") if isinstance(jc, dict) else None
        if not (isinstance(plan, dict) and plan.get("scene_words_max")):
            plan = gapp.style_script_plan(gapp.style_settings(cfg, style_name))
        length_note = (f" Keep it around {plan['scene_words_target']} words and NEVER more than "
                       f"{plan['scene_words_max']} words — the scene must stay 10-15 seconds "
                       f"spoken. One flowing sentence, or two short ones.")

    system = ("You are a script writer for short, AI-generated films. "
              "Be concise and return ONLY what the task asks for — no preamble, no labels.")
    user = (
        f"Video title: {video_title or topic}\nTopic: {topic}\nVisual style: {style}\n"
        f"Full scene outline: {outline}\n\n"
        f"Scene {scene_id} — current draft:\n"
        f"Title: {body.title}\nNarration: {body.narration}\n"
        f"Image prompt: {body.image_prompt}\nVideo prompt: {body.video_prompt}\n\n"
        f"Task: {_FIELD_INSTRUCTIONS[field]}{length_note}"
        + _instruction_note(body.instruction)
    )
    try:
        with _track_op(f"Regenerating {field}", video_title or topic or f"scene {scene_id}"):
            text = _llm_complete(system, user, cfg).strip().strip('"').strip()
    except Exception as e:
        raise HTTPException(503, f"Regeneration failed: {str(e).splitlines()[0][:200]}")

    # For image_prompt, bake the visual style prefix so the editor shows what renders.
    if field == "image_prompt" and style_name:
        prefix = gapp._compose_visual_style(style, cfg, style_name)
        text = _apply_style_prefix(prefix, text)

    # Persist the regenerated field together with the user's current values.
    fields = {"title": body.title, "narration": body.narration,
              "image_prompt": body.image_prompt, "video_prompt": body.video_prompt}
    fields[field] = text
    try:
        gapp._save_active_scene(job_id, int(scene_id), fields["title"],
                                fields["image_prompt"], fields["video_prompt"], fields["narration"])
    except Exception:
        pass  # the client also persists on blur — never lose the regenerated text
    return {"field": field, "value": text}


# ── scene mode conversion (narration ⇄ dialogue ⇄ silent) ────────────────────
# Switching a scene's type CONVERTS its content (same theme and feel, the other
# shape) instead of leaving mismatched fields — and every mode's last content is
# stashed in the scene metadata, so switching back RESTORES what was there
# rather than regenerating it.

_MODE_STASH_FIELDS = {
    "narration": ("narration", "image_prompt", "video_prompt", "tts_text"),
    "dialogue": ("lines", "cast", "setting", "camera", "soundscape",
                 "beats", "seconds", "prompt_override"),
    "silent": ("image_prompt", "video_prompt", "duration"),
}


def _stash_mode_content(meta: dict, row: dict, mode: str) -> None:
    """Snapshot what *mode* owns into meta["mode_stash"][mode], in place."""
    src = {**meta, "narration": row.get("narration") or "",
           "image_prompt": row.get("image_prompt") or "",
           "video_prompt": row.get("video_prompt") or ""}
    stash = dict(meta.get("mode_stash") or {})
    stash[mode] = {k: src.get(k) for k in _MODE_STASH_FIELDS[mode] if src.get(k)}
    meta["mode_stash"] = stash


class ConvertModeBody(BaseModel):
    mode: str   # "narration" | "dialogue" | "silent"


@api.post("/api/jobs/{job_id}/scenes/{scene_id}/convert-mode")
def convert_scene_mode(job_id: str, scene_id: int, body: ConvertModeBody) -> dict:
    sid = int(scene_id)
    target = (body.mode or "").strip().lower()
    if target not in _MODE_STASH_FIELDS:
        raise HTTPException(400, f"Unknown scene mode: {body.mode!r}")
    cfg = gapp.load_config()

    store = DurableStore.default()
    try:
        job = store.get_job(job_id)
        current = store.get_scene(job_id, sid) or {}
    finally:
        store.close()
    if not current:
        raise HTTPException(404, "Scene not found.")
    meta = dict(current.get("metadata") or {})
    src_mode = str(meta.get("mode") or "narration").strip().lower()
    if performance_mode.is_performance_mode(src_mode):
        src_mode = "dialogue"
    if src_mode == target:
        return {"ok": True, "scene": _scene_to_json(current, gapp._job_work_dir(job_id))}

    # Keep the version being left, so switching back restores it verbatim.
    _stash_mode_content(meta, current, src_mode)
    stashed = (meta.get("mode_stash") or {}).get(target) or {}

    jc = json.loads(_row_to_dict(job).get("config_json") or "{}") if job else {}
    style_name = jc.get("style_name", "")
    title = current.get("title") or ""

    def _save(**fields) -> dict:
        base = dict(title=title, image_prompt="", video_prompt="",
                    narration="", mode=target)
        if target != "dialogue":
            base["lines"] = []      # leftover acted lines must not linger
        if target == "narration":
            base["duration"] = 0    # nor a silent-scene duration
        base.update(fields)
        # carry the updated stash through update_scene's metadata merge
        store = DurableStore.default()
        try:
            store.upsert_scene(job_id, sid, title=title,
                               image_prompt=current.get("image_prompt") or "",
                               video_prompt=current.get("video_prompt") or "",
                               narration=current.get("narration") or "",
                               preview_path=current.get("preview_path", ""),
                               metadata=meta)
        finally:
            store.close()
        return update_scene(job_id, sid, SceneUpdate(**base))

    if stashed:
        # A version of this mode already exists — restore, don't regenerate.
        if target == "dialogue":
            return _save(lines=list(stashed.get("lines") or []),
                         cast=list(stashed.get("cast") or []),
                         setting=stashed.get("setting") or "",
                         camera=stashed.get("camera") or "",
                         soundscape=stashed.get("soundscape") or "",
                         beats=list(stashed.get("beats") or []),
                         seconds=float(stashed.get("seconds") or 0),
                         prompt=stashed.get("prompt_override") or "")
        if target == "silent":
            return _save(image_prompt=stashed.get("image_prompt") or "",
                         video_prompt=stashed.get("video_prompt") or "",
                         duration=float(stashed.get("duration") or 5))
        return _save(narration=stashed.get("narration") or "",
                     image_prompt=stashed.get("image_prompt") or "",
                     video_prompt=stashed.get("video_prompt") or "",
                     tts_text=stashed.get("tts_text") or "")

    # No stash: convert the content with the LLM — same theme, the other shape.
    video_title = jc.get("video_title") or (_row_to_dict(job).get("title") if job else "") or ""
    acted = performance_mode.acted_meta({"metadata": meta,
                                         "lines": meta.get("lines") or [],
                                         "video_prompt": current.get("video_prompt") or "",
                                         "image_prompt": current.get("image_prompt") or ""})
    # An empty scene — one just added in the film editor — has no content to
    # convert: flip the mode and leave the fields blank for the user to write,
    # rather than have the LLM invent a scene out of nothing.
    if not (meta.get("lines") or []) and not str(acted.get("setting") or "").strip() \
            and not any(str(current.get(k) or "").strip()
                        for k in ("narration", "image_prompt", "video_prompt")):
        return _save(duration=5) if target == "silent" else _save()

    if target == "silent":
        # Mechanical: the visuals stay, the voice goes.
        return _save(image_prompt=current.get("image_prompt") or acted.get("setting") or "",
                     video_prompt=(current.get("video_prompt")
                                   if src_mode == "narration" else acted.get("setting") or ""),
                     duration=5)

    system = ("You are a screenwriter converting one scene of a short AI-generated film "
              "between formats WITHOUT changing its content: same beat of the story, same "
              "theme, same feel. Return ONLY a raw JSON object — no markdown, no fences.")
    if target == "dialogue":
        cast_pool = ", ".join(dict.fromkeys(
            [c.get("name", "") for c in gapp._style_characters(cfg, style_name)]
            + [c.get("name", "") for c in gapp._job_characters(cfg, style_name,
                                                               gapp._job_work_dir(job_id)) or []]
        )) or "the story's characters"
        user = (
            f"Video title: {video_title}\nScene title: {title}\n"
            f"NARRATED version to stage as an ACTED scene (characters speak on camera, "
            f"one continuous ~10 second take):\n"
            f"Narration: {current.get('narration') or ''}\n"
            f"Visuals: {current.get('image_prompt') or ''}\n\n"
            "Convey the SAME information and mood through spoken dialogue. Return JSON:\n"
            f'  "cast": array of names on screen, AT MOST 2, chosen from: {cast_pool}\n'
            '  "setting": one sentence — where this happens (derive it from the visuals)\n'
            '  "lines": ordered [{"speaker","delivery","text"}] — AT MOST 3 lines and 22 '
            "spoken words TOTAL\n"
            '  "beats": [{"t0","t1","action"}]\n'
            '  "camera": one sentence, a single shot\n'
            '  "soundscape": diegetic sound only, no music'
        )
        raw = _convert_llm_json(user, system, cfg, video_title or title)
        lines = performance_mode.norm_lines(raw.get("lines"))
        if not lines:
            raise HTTPException(502, "The conversion returned no dialogue — try again.")
        return _save(lines=lines,
                     cast=[str(n) for n in (raw.get("cast") or []) if str(n).strip()],
                     setting=str(raw.get("setting") or ""),
                     camera=str(raw.get("camera") or ""),
                     soundscape=str(raw.get("soundscape") or ""),
                     beats=[b for b in (raw.get("beats") or []) if isinstance(b, dict)],
                     seconds=0, prompt="")

    # target == narration: the acted content becomes a narrated beat.
    plan = (jc.get("create_brief") or {}).get("scene_plan") if isinstance(jc, dict) else None
    if not (isinstance(plan, dict) and plan.get("scene_words_max")):
        plan = gapp.style_script_plan(gapp.style_settings(cfg, style_name))
    lines_txt = "\n".join(f'  {ln.get("speaker")}: "{ln.get("text")}"'
                          for ln in (meta.get("lines") or []))
    user = (
        f"Video title: {video_title}\nScene title: {title}\n"
        f"ACTED version to rewrite as a NARRATED scene (voice-over over a moving still):\n"
        f"Setting: {acted.get('setting') or ''}\nDialogue:\n{lines_txt or '  (none)'}\n\n"
        "Convey the SAME information and mood as narrator voice-over. Return JSON:\n"
        f'  "narration": about {plan["scene_words_target"]} words, NEVER more than '
        f'{plan["scene_words_max"]} — the scene must stay 10-15 seconds spoken\n'
        '  "image_prompt": 60-100 word detailed STATIC first-frame description (FLUX), '
        "no motion verbs, derived from the setting\n"
        '  "video_prompt": 30-50 word motion description (one continuous shot)'
    )
    raw = _convert_llm_json(user, system, cfg, video_title or title)
    narration = str(raw.get("narration") or "").strip()
    if not narration:
        raise HTTPException(502, "The conversion returned no narration — try again.")
    image_prompt = str(raw.get("image_prompt") or acted.get("setting") or "").strip()
    combined = gapp._compose_visual_style(json.loads(
        _row_to_dict(job).get("metadata_json") or "{}").get("style", "") if job else "",
        cfg, style_name)
    return _save(narration=narration,
                 image_prompt=_apply_style_prefix(combined, image_prompt),
                 video_prompt=str(raw.get("video_prompt") or "").strip() or image_prompt)


def _convert_llm_json(user: str, system: str, cfg: dict, label: str) -> dict:
    try:
        with _track_op("Converting scene", label):
            text = _llm_complete(system, user, cfg, max_tokens=900).strip()
    except Exception as e:
        raise HTTPException(503, f"Conversion failed: {str(e).splitlines()[0][:200]}")
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception:
        raise HTTPException(502, "The LLM did not return a valid scene — try again.")


class ActedRegenBody(BaseModel):
    instruction: str = ""   # optional "tell it how" steering (Re-generate popover)


@api.post("/api/jobs/{job_id}/scenes/{scene_id}/regenerate-acted")
def regenerate_acted_scene(job_id: str, scene_id: int,
                           body: ActedRegenBody | None = None) -> dict:
    """Rewrite ONE acted scene with the LLM — setting, dialogue, beats, camera,
    sound — keeping the film's context and cast. The narrated fields have
    per-field regen buttons; an acted scene is one coherent take, so it
    regenerates whole. Persists through update_scene, which reassembles the
    prompt exactly as a hand edit would.

    A PERFORMED silent scene (h3_silent_scenes) is the same take without the
    dialogue, so it rewrites the same way — and comes back with no lines: this
    is where a rewrite must not turn a silent beat into a conversation."""
    body = body or ActedRegenBody()
    sid = int(scene_id)
    cfg = gapp.load_config()

    store = DurableStore.default()
    try:
        job = store.get_job(job_id)
        rows = store.scene_rows(job_id)
        current = store.get_scene(job_id, sid) or {}
    finally:
        store.close()
    meta = dict(current.get("metadata") or {})
    jc = json.loads(_row_to_dict(job).get("config_json") or "{}") if job else {}
    wd = gapp._job_work_dir(job_id)
    silent = performance_mode.is_silent({"metadata": meta})
    # The render config from either place it is stamped — the job row for a
    # script, the work dir for a film — so this agrees with the acted view about
    # what is a take. Disagreeing would offer a rewrite that then refuses.
    if not performance_mode.renders_acted(
            {"metadata": meta},
            _acted_silent_cfg({**(_film_job_config(Path(wd)) if wd else {}), **jc})):
        raise HTTPException(400, "Not an acted scene — use the per-field regenerate buttons.")

    video_title = jc.get("video_title") or (_row_to_dict(job).get("title") if job else "") or ""
    topic = jc.get("topic") or ""
    style_name = jc.get("style_name", "")
    outline = "; ".join(f"{int(r['id'])}. {r.get('title') or ''}" for r in rows)
    cast_names = [c.get("name", "") for c in gapp._style_characters(cfg, style_name)]
    if wd:
        try:
            cast_names += [c.get("name", "") for c in
                           json.loads((Path(wd) / "characters.json").read_text())]
        except Exception:
            pass
    cast_pool = ", ".join(dict.fromkeys(n for n in cast_names if n)) or "the story's characters"

    lines_now = "\n".join(f'  {ln.get("speaker")}: "{ln.get("text")}"'
                          for ln in (meta.get("lines") or []))
    system = ("You are a screenwriter for short, AI-generated films. "
              "Return ONLY a raw JSON object — no markdown, no code fences, no explanation.")
    seconds = float(meta.get("seconds") or meta.get("duration")
                    or performance_mode.SCENE_SECONDS)
    singing = silent and bool(meta.get("singing"))
    if silent:
        task = (f"Rewrite SILENT scene {sid} — one continuous ~{round(seconds)} second take, "
                f"performed on camera, in which NOBODY SPEAKS. Current draft:\n"
                f'Title: {current.get("title") or ""}\n'
                f'Setting: {meta.get("setting") or ""}\n')
        if singing:
            # A music-video beat: the film's song plays over this shot, and by
            # default the cast mimes it on camera. Said outright so the model
            # writes staging that fits — and so an instruction like "they don't
            # sing here" has a switch it can actually flip (the singing
            # directives in the H3 prompt come from the scene's flags, not
            # from the text this model writes).
            task += (f'Sings the song on camera: '
                     f'{"no" if meta.get("performs") is False else "yes"}\n'
                     f"This scene is one beat of a MUSIC VIDEO — the film's "
                     f"song plays over it.\n")
        keys = ('  "lines": [] — this scene is silent, nobody says anything\n'
                '  "beats": array of {"t0": seconds, "t1": seconds, "action": <what happens>} '
                f"— they must fit inside {round(seconds)} seconds\n")
        if singing:
            keys += ('  "performs": true if the cast visibly sings the song on '
                     'camera in this shot, false if nobody sings or mimes (they '
                     'just move with the music)\n')
    else:
        task = (f"Rewrite ACTED scene {sid} — one continuous ~10 second take where the "
                f"characters speak on camera. Current draft:\n"
                f'Title: {current.get("title") or ""}\n'
                f'Setting: {meta.get("setting") or ""}\n'
                f"Dialogue:\n{lines_now or '  (none)'}\n")
        keys = ('  "lines": ordered array of {"speaker": <a cast name>, "delivery": <2-4 words>, '
                '"text": <ONE short spoken sentence>} — AT MOST 3 lines and 22 spoken words TOTAL\n'
                '  "beats": array of {"t0": seconds, "t1": seconds, "action": <what happens>}\n')
    user = (
        f"Video title: {video_title or topic}\nTopic: {topic}\n"
        f"Full scene outline: {outline}\n\n"
        + task + "\n"
        "Return a JSON object with exactly these keys:\n"
        '  "title": 5-10 word scene title\n'
        f'  "cast": array of names on screen, AT MOST 2, chosen from: {cast_pool}'
        + (" (empty for a beat with nobody in it)\n" if silent else "\n") +
        '  "setting": one sentence — where this happens and what is around them\n'
        + keys +
        '  "camera": one sentence — a single shot, at most one move\n'
        '  "soundscape": diegetic sound only, no music'
        + _instruction_note(body.instruction)
    )
    try:
        with _track_op("Regenerating acted scene", video_title or f"scene {sid}"):
            text = _llm_complete(system, user, cfg, max_tokens=900).strip()
    except Exception as e:
        raise HTTPException(503, f"Regeneration failed: {str(e).splitlines()[0][:200]}")
    try:
        raw = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception:
        raise HTTPException(502, "The LLM did not return a valid scene — try again.")

    # A silent beat stays silent whatever the model returns; a dialogue scene
    # with no lines is a failed rewrite (it would render as a held stare).
    lines = [] if silent else performance_mode.norm_lines(raw.get("lines"))
    if not silent and not lines:
        raise HTTPException(502, "The LLM returned a scene with no dialogue — try again.")
    # Whether the cast sings on camera, decided by the rewrite (honouring the
    # user's instruction); when the model stays quiet the scene keeps its
    # current answer. Only a singing scene carries the switch.
    performs = (bool(raw.get("performs", meta.get("performs") is not False))
                if singing else None)
    return update_scene(job_id, sid, SceneUpdate(
        title=str(raw.get("title") or current.get("title") or ""),
        # A silent take opens on the frame its image prompt paints, so the
        # rewrite keeps it; a dialogue scene renders no image at all.
        image_prompt=(current.get("image_prompt") or "") if silent else "",
        video_prompt=current.get("video_prompt") or "",
        narration="",
        mode=meta.get("mode") or "dialogue",
        lines=lines,
        cast=[str(n) for n in (raw.get("cast") or []) if str(n).strip()],
        setting=str(raw.get("setting") or ""),
        camera=str(raw.get("camera") or ""),
        soundscape=str(raw.get("soundscape") or ""),
        beats=[b for b in (raw.get("beats") or []) if isinstance(b, dict)],
        performs=performs,
        # Length follows the new words (update_scene recomputes via acted_meta);
        # a silent take keeps the length it was written for.
        duration=seconds if silent else None,
        seconds=seconds if silent else 0,
        prompt=""  # a rewrite supersedes any pinned prompt
    ))


def _apply_style_prefix(combined_style: str, image_prompt: str) -> str:
    """Prepend combined_style to image_prompt, skipping if already present."""
    ip = (image_prompt or "").strip()
    if combined_style and ip and not ip.startswith(combined_style):
        return f"{combined_style}. {ip}"
    return ip or image_prompt


class BriefImproveBody(BaseModel):
    field: str = "title"           # "title" | "direction"
    title: str = ""
    direction: str = ""
    style_name: str = ""
    instruction: str = ""          # optional "tell it how" steering


@api.post("/api/create/improve")
def create_improve(body: BriefImproveBody) -> dict:
    """Improve the Create brief's title or direction in place (issue #88).

    Standalone (no job yet): takes the current title + direction text and returns
    a sharper version of the requested field. Honours the picked style's title
    phrasing (issue #82) when improving the title."""
    if body.field not in ("title", "direction"):
        raise HTTPException(400, f"Unknown field: {body.field}")
    cfg = gapp.load_config()
    title = (body.title or "").strip()
    direction = (body.direction or "").strip()
    if body.field == "title":
        title_style = gapp.style_settings(cfg, body.style_name).get("title_style", "") if body.style_name else ""
        system = ("You write punchy, click-worthy YouTube video titles. "
                  "Return ONLY the improved title — one line, no quotes, no label.")
        user = (
            f"Current title: {title or '(none yet)'}\n"
            f"Direction / angle: {direction or '(none given)'}\n"
            + (f"Title phrasing style to follow: {title_style}\n" if title_style else "")
            + "\nImprove the title (or write a strong one if it's empty). "
            "Keep it concise and true to the direction."
            + _instruction_note(body.instruction)
        )
    else:
        system = ("You refine the creative-direction brief for a short, AI-generated video. "
                  "Return ONLY the improved direction text — no preamble, no labels.")
        user = (
            f"Video title: {title or '(untitled)'}\n"
            f"Current direction: {direction or '(none yet)'}\n"
            "\nImprove and sharpen the direction: clarify the angle, tone, and what to "
            "emphasise. Keep it to 1–3 sentences."
            + _instruction_note(body.instruction)
        )
    try:
        with _track_op(f"Improving {body.field}", title or direction):
            text = _llm_complete(system, user, cfg, max_tokens=300).strip().strip('"').strip()
    except Exception as e:
        raise HTTPException(503, f"Improve failed: {str(e).splitlines()[0][:200]}")
    return {"value": text}


def _job_music_enabled(job_id: str, ss: dict) -> bool:
    """Whether this film gets a score: the Create-time choice, else the style."""
    stamped = _job_meta_field(job_id, "music_enabled", None)
    return bool(ss.get("music_enabled", True)) if stamped is None else bool(stamped)


# ── approve & generate (launches the background pipeline) ─────────────────────

class GenerateBody(BaseModel):
    job_id: str
    work_dir: str
    video_title: str = ""
    title: str = ""
    n_scenes: int = 0
    voice: str = ""
    resolution: str = ""
    music_desc: str = ""
    style: str = ""
    style_name: str = ""


@api.post("/api/jobs/generate")
def start_generation(body: GenerateBody) -> dict:
    """Port of app.on_generate's Gradio-free core: build the Scene list, write
    job_config.json/progress.json, register the durable generation plan, and
    launch the resumable worker subprocess."""
    job_id, work_dir_str = body.job_id, body.work_dir
    if not job_id or not work_dir_str:
        raise HTTPException(400, "No generated script available. Generate the script again.")
    work_dir = Path(work_dir_str)
    if not _safe_under(work_dir, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")

    store = DurableStore.default()
    try:
        scene_rows = store.scene_rows(job_id)
        job_row = store.get_job(job_id)
    finally:
        store.close()
    if not scene_rows:
        raise HTTPException(400, "No scene data available. Generate the script again.")

    cfg = gapp.load_config()
    # Style profile: explicit request → stamped on the job at script time →
    # default style. Its settings fill anything the request leaves blank and
    # supply the render quality + audio mix for this job.
    style_name = (body.style_name or "").strip()
    if not style_name and job_row is not None:
        try:
            style_name = json.loads(_row_to_dict(job_row).get("config_json") or "{}").get("style_name", "")
        except Exception:
            style_name = ""
    ss = gapp.style_settings(cfg, style_name)

    voice_name = body.voice
    if not voice_name or voice_name == gapp.F5TTS_DEFAULT_OPTION:
        voice_name = ss.get("voice") or voice_name
    voice_ref = gapp.voice_path_for(voice_name)
    resolution = body.resolution or ss.get("resolution") or gapp._DEFAULT_RESOLUTION
    # An upscale-only target (QHD/4K) renders at the largest render-capable
    # tier and the pipeline finishes with an upscale to the target — nothing
    # may render at the target size itself.
    resolution, finish_resolution = gapp.split_render_target(resolution)
    vid_width, vid_height = gapp._RESOLUTIONS.get(resolution, (832, 480))

    style_clean = body.style.strip().rstrip(".") if body.style and body.style.strip() else ""
    combined_style = gapp._compose_visual_style(body.style, cfg, style_name)
    # Per-style LTX video negative prompt; blank falls back to the built-in
    # quality default so styles keep it unless they explicitly override.
    video_neg = (ss.get("video_negative_prompt") or "").strip() or llm.NEGATIVE_PROMPT

    n = int(body.n_scenes) if body.n_scenes else len(scene_rows)
    title = body.title or body.video_title

    def _acted_row(row) -> bool:
        return performance_mode.is_performance_mode(
            (row.get("metadata") or {}).get("mode"))

    scenes = [
        Scene(
            id=int(row["id"]),
            title=row.get("title") or f"Scene {int(row['id'])}",
            # An acted scene has no image prompt BY DESIGN — the title fallback
            # is a classic-scene safety net, and back-filling it here poisoned
            # the cover prompt with the film title (which the model then
            # painted, misspelled, into the "text-free" background).
            image_prompt=("" if _acted_row(row) else
                          _apply_style_prefix(combined_style, row.get("image_prompt") or title)),
            video_prompt=row.get("video_prompt") or ("" if _acted_row(row) else
                                                     row.get("image_prompt") or title),
            narration=row.get("narration") or "",
            negative_prompt=video_neg,
            # Dialogue/performance fields ride in the scene metadata; dropping
            # them here silently turned authored dialogue back into narration.
            mode=str((row.get("metadata") or {}).get("mode") or "narration"),
            lines=list((row.get("metadata") or {}).get("lines") or []),
            duration=float((row.get("metadata") or {}).get("duration") or 0.0),
            metadata_extra=dict(row.get("metadata") or {}),
        )
        for row in scene_rows[:n]
    ]
    gapp._persist_script_snapshot(work_dir, [_scene_snapshot_row(s) for s in scenes])

    # Character-first pre-build (main-character consistency). BEFORE the render
    # plan is registered — so it holds even for a fully headless/automated render
    # — make sure every character in play has a look image, then generate
    # reference-conditioned scene previews. ensure_generation_plan then reuses
    # those previews as scene first frames (it skips the image task of any scene
    # with a preview_path), so the character is built and imaged before the scene
    # images are, and stays consistent across scenes. Runs in-process here
    # (start_generation is only ever called by the automation loop, never a
    # blocking HTTP request). No-op when the job has no characters, and a no-op
    # fast path when previews already exist (the interactive editor already made
    # them). Best-effort: any failure falls back to the normal first-frame path.
    # A performance film conditions on the portraits themselves and renders no
    # first frame, so it needs the character looks but never the scene previews.
    performance_film = bool(scenes) and all(
        performance_mode.is_performance(s) for s in scenes)
    try:
        if gapp._job_characters(cfg, ss["name"], work_dir):
            (work_dir / "progress.json").write_text(json.dumps(
                {"pct": 0, "msg": "Building character looks…", "ts": time.time()}))
            gapp.generate_all_script_portraits(work_dir, ss["name"])
            if not performance_film:
                (work_dir / "progress.json").write_text(json.dumps(
                    {"pct": 0, "msg": "Generating character-consistent scene frames…", "ts": time.time()}))
                # Called as a plain function, so the endpoint's Query(...)
                # defaults don't resolve: force must be passed explicitly —
                # Query(False) is a truthy object, and leaving it repainted
                # every scene's image before each render (issue: images
                # regenerated on approve).
                generate_all_previews(job_id, resolution, body.style or "", force=False)
    except Exception as e:
        gapp.logger.warning("Character pre-build before render failed (non-fatal): %s", e)

    job_cfg = gapp._job_config_snapshot(cfg)
    job_cfg.update({
        "resolution": resolution, "max_clip_secs": 0,
        # Upscale-only target (QHD/4K) this render finishes at, and the
        # upscaler that gets it there ("" = no finishing step). Stamped flat so
        # resume_generation.py reads them like every other render key.
        "finish_resolution": finish_resolution,
        "finish_upscale_mode": ss.get("finish_upscale_mode") or "flashvsr_2x",
        "default_voice": voice_name, "voice_ref": voice_ref or "",
        # 0 = natural; >0 robotizes at that strength (the on/off toggle was
        # folded into the level).
        "voice_robotic_amount": ss.get("voice_robotic_amount", 0.0),
        # Speed multiplier derived from the style's target cadence against the
        # chosen voice's measured natural pace (pipeline/cadence.py); the
        # target rides along so re-voicing can re-derive from fresher data.
        "voice_speed": cadence.resolve_voice_speed({**ss, "voice": voice_name}),
        "voice_cadence_wpm": ss.get("voice_cadence_wpm", 0),
        "tts_engine": gapp.tts_engines.norm(ss.get("tts_engine")),
        "tts_language": gapp._norm_tts_language(ss.get("tts_language")),
        # Sentence gap spliced into narration (pipeline/tts_text.py); stamped so
        # the render and later film-editor re-voicing keep the style's cadence.
        "tts_sentence_pause": gapp._norm_tts_sentence_pause(ss.get("tts_sentence_pause")),
        # Per-style render quality + audio mix (issue #66): the resumable
        # worker reads these flat keys from job_config.json, so resolving them
        # here is what makes the chosen style drive the render and the mix.
        "style_name": ss["name"],
        # The style's image engine drives first frames + the render-time cover
        # in resume_generation.py (previously those fell back to FLUX.1 via the
        # legacy flux_* keys, breaking installs without the opt-in FLUX.1 models).
        "image_engine": ss.get("image_engine"),
        # The style's video engine drives the scene I2V model (LTX / MiniMax H3).
        "video_engine": ss.get("video_engine"),
        # Ref2VA model for performance films (portraits + dialogue, no first
        # frame). Ignored by every narrated render.
        "reference_engine": ss.get("reference_engine"),
        # Chained H3 scenes. Stamped like every other per-style render key —
        # resume_generation reads the merged job config FLAT, so an unstamped
        # key silently falls back to the flat default and a per-style toggle
        # never reaches the render.
        "h3_chain_scenes": gapp._norm_h3_chain_scenes(ss.get("h3_chain_scenes")),
        # Act the silent scenes on H3 Ref2VA (from their cast's portraits)
        # rather than animating them from a first frame. Stamped for the same
        # reason as the flag above — the render reads the job config flat.
        "h3_silent_scenes": gapp._norm_h3_silent_scenes(ss.get("h3_silent_scenes")),
        # Open every acted scene on a painted first frame (the frame then rides
        # the H3 take as its opening-composition reference). Stamped flat for
        # the same reason as the flags above.
        "h3_first_frames": gapp._norm_h3_first_frames(ss.get("h3_first_frames")),
        # Burn the cover into the head of the final video at the end of the
        # render ("none" | "image" | "text") — Shorts pick their own frame —
        # and how long it is held (seconds).
        "first_frame_cover": gapp._norm_first_frame_cover(ss.get("first_frame_cover")),
        "first_frame_cover_seconds": gapp._norm_first_frame_cover_seconds(
            ss.get("first_frame_cover_seconds")),
        # Burn the script's captions into the picture itself (open captions) at
        # the end of the render. Stamped so the render reads it flat AND so
        # every later rebuild (remix/reassemble/localize) re-burns the track.
        "burn_subtitles": gapp._norm_burn_subtitles(ss.get("burn_subtitles")),
        "subtitle_style": gapp._norm_subtitle_style(ss.get("subtitle_style")),
        # Cover typography: text-free background + composited real-font title
        # (pipeline/cover_typography.py). Stamped resolved so the render-time
        # cover uses the style's look without re-resolving the hierarchy.
        "cover_typography": gapp._norm_cover_typography(ss.get("cover_typography")),
        # Resolved per-style LTX video negative (blank → built-in default). Stamped
        # into job_config.json so a resumed render (resume_generation.py) reuses it.
        "video_negative_prompt": video_neg,
        "lora_strength": ss.get("lora_strength"),
        "first_pass_cfg": ss.get("first_pass_cfg"),
        "first_pass_steps": ss.get("first_pass_steps"),
        "second_pass_cfg": ss.get("second_pass_cfg"),
        "second_pass_steps": ss.get("second_pass_steps"),
        # Score this film? The Create-time choice (stamped on the job) wins over
        # the style's default; music is a final-mix ingredient either way.
        "music_enabled": _job_music_enabled(job_id, ss),
        # The style's music engine writes the background bed (ACE-Step / MiniMax
        # Music 3). Stamped like the engines above so a resumed render and a
        # later Remix re-generation both keep the style's choice.
        "music_engine": gapp._norm_music_engine(ss.get("music_engine")),
        "music_vol": ss.get("music_vol"),
        "voice_vol": ss.get("voice_vol"),
        "ambient_vol": ss.get("ambient_vol"),
        "music_desc": body.music_desc or "", "title": title,
        "video_title": (body.video_title or "").strip(), "style": style_clean,
        # The exact text baked onto every image prompt's head — what a later
        # Restyle strips before laying the new style on.
        "style_prefix": combined_style,
    })

    # Link to an originating YouTube queue item, if one matches by final title.
    queue_item_id = ""
    try:
        key = (body.video_title or "").strip().lower()
        if key:
            match = next((q for q in yt.load_queue()
                          if q.get("status") == "pending"
                          and q.get("final_title", "").lower() == key), None)
            if match:
                queue_item_id = match["id"]
    except Exception:
        pass
    job_cfg["queue_item_id"] = queue_item_id

    (work_dir / "job_config.json").write_text(json.dumps(job_cfg, indent=2))
    (work_dir / "progress.json").write_text(
        json.dumps({"pct": 0, "msg": "Generation job queued", "ts": time.time()})
    )

    store = DurableStore.default()
    try:
        store.ensure_generation_plan(job_id, work_dir, title, scenes, {
            **job_cfg, "vid_width": vid_width, "vid_height": vid_height,
            "resource_classes": {"image": "comfy:image", "music": "comfy:music",
                                 "video": "comfy:video", "narration": "tts", "finalize": "local"},
        })
        store.update_job(job_id, status="running", progress_pct=0,
                         progress_message="generation job launched")
    finally:
        store.close()

    gapp._launch_generation_job(work_dir)
    return {"ok": True, "work_dir": str(work_dir)}


# ── progress / orchestration ─────────────────────────────────────────────────

def _display_pct(band_pct, eta, status: str, done: bool):
    """The progress-bar percentage.

    The phase-band pct (from write_progress) and the learned task ETA are two
    independent signals; when they diverge the bar looks broken next to the
    "X left" text (e.g. 49% while ~1 task / under a minute remains). Derive the
    bar from the SAME numbers as the ETA — elapsed fraction = 1 - remaining/total
    — so the two always agree. Fall back to the band pct before durable tasks
    exist (script generation / character pre-build) or when there's no ETA."""
    if done:
        return 100
    if isinstance(eta, dict) and status not in ("error", "cancelled", "failed"):
        total = eta.get("total_seconds") or 0
        rem = eta.get("eta_seconds") or 0
        if total > 0:
            return max(1, min(99, int(round(100.0 * (total - rem) / total))))
    return band_pct


def _ui_reserved_comfy(cfg: dict) -> int:
    """1 while a comfy worker is held for the active UI (issue #98), else 0.
    Recomputed each poll so the ETA tracks the UI going active/idle."""
    timeout = float(cfg.get("ui_idle_timeout_seconds", ui_activity.DEFAULT_IDLE_TIMEOUT))
    return 1 if (len(cfg.get("comfy_workers") or []) >= 2 and ui_activity.is_active(timeout)) else 0


def _reconciled_render_pct(wd) -> int:
    """Render % that agrees with the task ETA — the SAME number the sidebar badge,
    the Activity row and the Progress screen all show. Falls back to the phase
    band pct before durable tasks exist."""
    band = gapp._status_for_work_dir(wd)[0]
    final_path = gapp._final_path_for_work_dir(wd)
    done = final_path.exists() and final_path.stat().st_size > 10_000 and (wd / "combined.mp4").exists()
    if done:
        return 100
    status, eta = "", None
    store = DurableStore.default()
    try:
        job_row = store.get_job_by_work_dir(str(wd))
        if job_row is None:
            return int(round(band))
        job = _row_to_dict(job_row)
        status = job.get("status", "")
        tasks = [_row_to_dict(t) for t in store.task_rows(job["id"])]
        cfg = gapp.load_config()
        eta = estimate_eta(tasks, store.timing_table(), cfg, reserved_comfy=_ui_reserved_comfy(cfg))
    except Exception:
        return int(round(band))
    finally:
        store.close()
    return _display_pct(int(round(band)), eta, status, done)


@api.get("/api/progress")
def progress(work_dir: str = Query("")) -> dict:
    wd = gapp._preferred_work_dir(work_dir)
    if wd is None:
        return {"pct": 0, "msg": "Waiting to start…", "work_dir": "", "done": False,
                "final_url": "", "cover_url": "", "tasks": [], "workers": [], "counts": {}}

    pct, msg = gapp._status_for_work_dir(wd)
    final_path = gapp._final_path_for_work_dir(wd)
    combined = wd / "combined.mp4"
    done = final_path.exists() and final_path.stat().st_size > 10_000 and combined.exists()
    cover = wd / "cover.png"

    tasks, workers, counts, job, eta = [], [], {}, None, None
    store = DurableStore.default()
    try:
        job_row = store.get_job_by_work_dir(str(wd))
        if job_row is None:
            recent = store.recent_jobs(limit=1)
            job_row = recent[0] if recent else None
        if job_row is not None:
            job = _row_to_dict(job_row)
            summary = store.job_summary(job["id"])
            counts = summary.get("counts", {})
            tasks = [_row_to_dict(t) for t in store.task_rows(job["id"])]
            cfg = gapp.load_config()
            # One comfy worker is held for the UI while it is in use, but only when
            # there are ≥2 (the last is never reserved) — mirrors WorkerPool. This
            # is recomputed every poll, so the worker list and ETA track the UI
            # going active/idle mid-render.
            reserved = _ui_reserved_comfy(cfg)
            workers = _worker_activity(cfg, tasks, reserved)
            if not done:
                try:
                    eta = estimate_eta(tasks, store.timing_table(), cfg, reserved_comfy=reserved)
                except Exception:
                    eta = None
    except Exception:
        pass
    finally:
        store.close()

    title = (job or {}).get("title", wd.name)
    pct = _display_pct(pct, eta, (job or {}).get("status", ""), bool(done))

    return {
        "pct": pct, "msg": msg, "work_dir": str(wd), "done": bool(done),
        "final_url": _busted_file_url(final_path) if done else "",
        "cover_url": _busted_file_url(cover) if cover.exists() and cover.stat().st_size > 1000 else "",
        "title": title,
        "status": (job or {}).get("status", ""),
        "tasks": tasks, "workers": workers, "counts": counts,
        "eta": eta,
    }


class JobActionBody(BaseModel):
    work_dir: str = ""


@api.post("/api/jobs/pause")
def pause_job(body: JobActionBody) -> dict:
    return {"message": gapp.on_pause_active_job(body.work_dir)}


@api.post("/api/jobs/resume")
def resume_job(body: JobActionBody) -> dict:
    msg, _tab, wd = gapp.on_resume_active_job(body.work_dir)
    return {"message": msg, "work_dir": wd}


@api.post("/api/jobs/retry")
def retry_job(body: JobActionBody) -> dict:
    return {"message": gapp.on_retry_failed_tasks(body.work_dir)}


@api.post("/api/jobs/cancel")
def cancel_job(body: JobActionBody) -> dict:
    return {"message": gapp.on_cancel_active_job(body.work_dir)}


@api.post("/api/jobs/delete")
def delete_job(body: JobActionBody) -> dict:
    """Stop an active render (kill its process), remove any queue entry pointing to it, then delete its files."""
    work_dir = body.work_dir
    wd = Path(work_dir)
    out = gapp.OUTPUT_DIR.resolve()
    try:
        wd_res = wd.resolve()
    except OSError:
        raise HTTPException(400, "Invalid path.")
    if not work_dir or wd_res == out or wd_res.parent != out:
        raise HTTPException(400, "Refusing to delete outside the videos directory.")
    # Kill the render subprocess before its files vanish. on_cancel_active_job
    # alone only cancels DurableStore tasks — and no-ops entirely when the job
    # has no durable row yet — leaving resume_generation.py running as an
    # orphan that burns ComfyUI worker time on output nobody will see.
    try:
        gapp._terminate_job_process(wd)
    except Exception:
        pass
    try:
        gapp.on_cancel_active_job(work_dir)
    except Exception:
        pass
    # A scene re-render (films editor) may also be writing into this dir.
    _cancel_film_tasks(wd)
    for item in yt.load_queue():
        if item.get("work_dir") == work_dir:
            yt.remove_queue_item(item["id"])
    _finalize_publish_entry_on_delete(wd)
    import shutil
    if wd.exists():
        shutil.rmtree(wd, ignore_errors=True)
    canonical = gapp.OUTPUT_DIR / f"{wd.name}.mp4"
    if canonical.exists():
        canonical.unlink(missing_ok=True)
    return {"ok": True, "deleted": wd.name}


@api.post("/api/jobs/seen")
def mark_job_seen(body: JobActionBody) -> dict:
    """Record that the user opened a finished film to watch it — clears its "New"
    badge in the Library. Stored in the film's job.json (not the browser) so the
    watched state is shared across devices. Idempotent: only the first open writes."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    out = gapp.OUTPUT_DIR.resolve()
    try:
        wd_res = wd.resolve()
    except OSError:
        raise HTTPException(400, "Invalid path.")
    if not body.work_dir or wd_res == out or wd_res.parent != out:
        raise HTTPException(400, "Refusing to write outside the videos directory.")
    jp = wd / "job.json"
    if not jp.exists():
        raise HTTPException(404, "Film not found.")
    try:
        meta = json.loads(jp.read_text())
    except Exception:
        meta = {}
    if not meta.get("viewed_at"):
        gapp._write_job_meta(wd, viewed_at=time.time())
    return {"ok": True}


class FilmMetaBody(BaseModel):
    work_dir: str = ""
    title: str | None = None      # rename the display title (never the folder)
    starred: bool | None = None
    archived: bool | None = None


@api.post("/api/films/meta")
def set_film_meta(body: FilmMetaBody) -> dict:
    """Library metadata for one film: rename (display title), star, archive.
    starred/archived persist in job_config.json — the renderer rewrites job.json
    wholesale on completion/error, so flags there wouldn't survive a re-render.
    The rename shares _save_video_title with the Publish screen's save, so both
    paths land in the same places (durable record, job_config, pending queue
    item); the work FOLDER is never renamed — its name keys the durable job id,
    the published final's path and deep links."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    out = gapp.OUTPUT_DIR.resolve()
    try:
        wd_res = wd.resolve()
    except OSError:
        raise HTTPException(400, "Invalid path.")
    if not body.work_dir or wd_res == out or wd_res.parent != out:
        raise HTTPException(400, "Refusing to write outside the videos directory.")
    if not wd.exists():
        raise HTTPException(404, "Film not found.")
    title = (body.title or "").strip()
    if body.title is not None and not title:
        raise HTTPException(400, "Title cannot be empty.")
    if title:
        _save_video_title(wd, title)
    if body.starred is not None or body.archived is not None:
        jc = _film_job_config(wd)
        if body.starred is not None:
            jc["starred"] = bool(body.starred)
        if body.archived is not None:
            jc["archived"] = bool(body.archived)
        _write_film_job_config(wd, jc)
    return {"ok": True, "title": title}


# ── library / recent jobs ────────────────────────────────────────────────────

def _channel_display_name(cfg: dict, key: str) -> str:
    entry = next((c for c in (cfg.get("youtube_channels") or []) if c.get("id") == key), None)
    return ((entry.get("name") if entry else "") or "").strip() or "YouTube"


def _x_account_display_name(cfg: dict, key: str) -> str:
    entry = next((a for a in (cfg.get("x_accounts") or []) if a.get("id") == key), None)
    return ((entry.get("name") if entry else "") or "").strip() or "X"


def _film_publish_status(wd: Path, meta: dict, cfg: dict) -> dict:
    """Where a finished film has been published. 'New' (published=False) means it
    hasn't been posted anywhere yet. Channel/account names resolve from the film's
    style — the same mapping the Publish screen uses — since job.json records only
    the resulting video/tweet id, not the destination key."""
    dests = []
    if meta.get("youtube_video_id"):
        dests.append({
            "platform": "youtube",
            "name": _channel_display_name(cfg, _channel_for_work_dir(wd)),
            "url": meta.get("youtube_url") or "",
        })
    if meta.get("x_tweet_id"):
        dests.append({
            "platform": "x",
            "name": _x_account_display_name(cfg, _x_account_for_work_dir(wd)),
            "url": meta.get("x_url") or "",
        })
    out = {"published": bool(dests), "destinations": dests}
    # Approval gate (publish_require_approval): a still-unpublished film waits for
    # a thumbs-up in the Films tab. Comment-requested videos bypass it, as does the
    # automation override that publishes unapproved films.
    if cfg.get("publish_require_approval") and not cfg.get("publish_auto_publish_unapproved") and not dests:
        e = pq.item_by_work_dir(str(wd))
        source = (e or {}).get("source") or _publish_source_for(_film_job_config(wd))
        bypass = source == "comment" and cfg.get("publish_schedule_skip_comment_requests", True)
        if not bypass:
            approved = bool(e and e.get("approved"))
            out["approved"] = approved
            out["awaiting_approval"] = not approved
    return out


def _style_names_for_work_dirs(dirs: list[str]) -> dict[str, str]:
    """style_name per work dir, for list payloads. Same resolution order as
    _work_dir_style_name (job_config.json → durable job config), but the
    durable-store fallback runs in ONE store session — the per-item helper
    opens sqlite per call, too slow for the 1000-film library list."""
    out: dict[str, str] = {}
    missing = []
    for d in dirs:
        name = str(_film_job_config(Path(d)).get("style_name") or "")
        if name:
            out[d] = name
        else:
            missing.append(d)
    if missing:
        try:
            store = DurableStore.default()
            try:
                for d in missing:
                    row = store.get_job(job_id_from_work_dir(Path(d)))
                    cfg_json = _row_to_dict(row).get("config_json") if row else ""
                    out[d] = json.loads(cfg_json or "{}").get("style_name", "")
            finally:
                store.close()
        except Exception:
            pass
    for d in missing:
        out.setdefault(d, "")
    return out


def _video_titles_for_work_dirs(dirs: list[str]) -> dict[str, str]:
    """Display title per work dir, for the Films list. Same resolution order as
    _video_title_for (job_config.json → durable job record) but batched into ONE
    store session — the per-item helper opens sqlite per call, too slow for the
    1000-film library list. Returns '' (not the dir name) when no explicit title
    was ever saved, so the caller falls back to the pretty folder label."""
    out: dict[str, str] = {}
    missing = []
    for d in dirs:
        title = str(_film_job_config(Path(d)).get("video_title") or "").strip()
        if title:
            out[d] = title
        else:
            missing.append(d)
    if missing:
        try:
            store = DurableStore.default()
            try:
                for d in missing:
                    row = _row_to_dict(store.get_job(job_id_from_work_dir(Path(d))))
                    cfg_json = json.loads(row.get("config_json") or "{}")
                    out[d] = str(cfg_json.get("video_title") or row.get("title") or "")
            finally:
                store.close()
        except Exception:
            pass
    for d in missing:
        out.setdefault(d, "")
    return out


@api.get("/api/jobs")
def list_jobs() -> dict:
    # The Library lists every finished film (it filters client-side, no paging),
    # so the cap is only a runaway guard — a 50-film cut silently hid older ones
    # that were still live in the publish queue.
    finished_rows = gapp._list_recent_jobs(max_results=1000)
    cfg = gapp.load_config()
    def _cover_url(work_dir: str) -> str:
        cover = Path(work_dir) / "cover.png"
        if cover.exists() and cover.stat().st_size > 1000:
            return _busted_file_url(cover)
        return ""
    # Style + publish-target channel per item, so the Films/Scripts lists can
    # filter by them. Resolved in one batch (the per-item helper hits sqlite).
    script_rows = list(gapp._list_script_jobs())
    styles = _style_names_for_work_dirs(
        [d for _, d in finished_rows] + [d for _, d in script_rows])
    titles = _video_titles_for_work_dirs([d for _, d in finished_rows])
    chan_cache: dict[str, str] = {}
    def _channel_disp(style: str) -> str:
        if style not in chan_cache:
            key = gapp.channel_for_style(cfg, style)
            chan_cache[style] = _channel_display_name(cfg, key) if key else ""
        return chan_cache[style]
    finished = []
    for l, d in finished_rows:
        try:
            meta = json.loads((Path(d) / "job.json").read_text())
        except Exception:
            meta = {}
        jc = _film_job_config(Path(d))
        finished.append({"label": l, "work_dir": d, "cover_url": _cover_url(d),
                         # Saved display title (rename / Publish-screen edit);
                         # the card falls back to the pretty folder label.
                         "title": titles.get(d) or l,
                         "seen": bool(meta.get("viewed_at")),
                         # Rendering one script at a second resolution leaves two
                         # films with the SAME label, so the card names the size.
                         "resolution": (jc.get("resolution") or "").strip(),
                         "style_name": styles.get(d, ""),
                         "channel": _channel_disp(styles.get(d, "")),
                         "starred": bool(jc.get("starred")),
                         "archived": bool(jc.get("archived")),
                         **_film_publish_status(Path(d), meta, cfg)})
    scripts = [{"label": l, "work_dir": d,
                # a finished film exists for this script — same marker the
                # finished list keys on, so "Not rendered" can filter on it
                "rendered": (Path(d) / "combined.mp4").exists(),
                # story drafted but not yet divided into scenes — the Script
                # screen opens these straight into the Story view
                "story_draft": not (Path(d) / "script.json").exists(),
                # a music video whose song is written but whose story is not —
                # it opens on the Song tab instead
                "song_draft": (not (Path(d) / "script.json").exists()
                               and not (Path(d) / "story.json").exists()
                               and (Path(d) / "song.json").exists()),
                "style_name": styles.get(d, ""),
                "channel": _channel_disp(styles.get(d, ""))}
               for l, d in script_rows]
    resumable = []
    active_wd = gapp._preferred_work_dir("")
    active_key = _work_dir_title_key(active_wd) if active_wd else ""
    finished_keys = set()
    for label, work_dir in finished_rows:
        finished_keys.add(_title_key(label))
        finished_keys.add(_work_dir_title_key(Path(work_dir)))
    for label, work_dir in gapp._list_resumable_jobs():
        wd = Path(work_dir)
        title_key = _work_dir_title_key(wd) or _title_key(label)
        is_active = bool(active_wd and wd == active_wd)
        if title_key in finished_keys and not is_active:
            continue
        meta = {}
        try:
            meta = json.loads((wd / "job.json").read_text())
        except Exception:
            pass
        pid = meta.get("pid")
        running = (meta.get("status") == "running" and gapp._process_running(pid)) or (
            is_active and title_key == active_key and meta.get("status") == "running"
        )
        resumable.append({
            "label": label,
            "work_dir": work_dir,
            "status": "running" if running else (meta.get("status") or "incomplete"),
            "running": running,
        })
    return {"finished": finished, "scripts": scripts, "resumable": resumable}


# ── remix ────────────────────────────────────────────────────────────────────

class RemixBody(BaseModel):
    work_dir: str
    voice_vol: float = 100
    music_vol: float = 18
    ambient_vol: float = 0


class RemixNarratorBody(BaseModel):
    work_dir: str
    voice: str = ""


class RemixUpscaleBody(BaseModel):
    work_dir: str
    # Blank for the factor modes (flashvsr_2x/4x, ltx_latent_2x): they finish at
    # the film's size times their factor, so there is no target to choose.
    target_resolution: str = ""
    upscale_mode: str = "flashvsr_2x"


class RemixVideoSelectBody(BaseModel):
    work_dir: str
    version_id: int


def _is_music_video(wd: Path) -> bool:
    """True for a song film — the "Music video" format.

    song.json is written when the song is authored and travels with every fork,
    so it is the same marker the film editor keys its song card on (see
    _remix_song_info)."""
    return (wd / "song.json").exists()


def _mix_volumes(wd: Path, jc: dict | None = None,
                 cfg: dict | None = None) -> tuple[float, float, float]:
    """(voice, music, ambient) percentages for this film's audio mix — the
    film's own saved volumes, falling back to the global config's.

    A music video mixes to MUSIC ONLY: the generated song is the entire
    soundtrack, so voice and ambience are pinned to zero here as well as at
    render time. Anything else — the singing takes' own audio (each carries its
    slice of the song, so the clip can be watched against its music), a stray
    spoken beat the writer left in, a soundscape, an older film rendered before
    the render stamped these volumes — would bleed in under the track."""
    jc = _film_job_config(wd) if jc is None else jc
    cfg = gapp.load_config() if cfg is None else cfg
    if _is_music_video(wd):
        return 0.0, float(jc.get("music_vol", 100)), 0.0
    return (float(jc.get("voice_vol", cfg.get("voice_vol", 100))),
            float(jc.get("music_vol", cfg.get("music_vol", 18))),
            float(jc.get("ambient_vol", cfg.get("ambient_vol", 0))))


def _remix_song_info(wd: Path) -> dict | None:
    """A song film's song, for the film editor — None for every other film."""
    from pipeline import svc

    path = wd / "song.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return {"lyrics": str(data.get("lyrics") or ""),
            "caption": str(data.get("caption") or ""),
            "sung_as": str(data.get("sung_as") or ""),
            # Whether "sing it as" can run at all — seed-vc is an optional
            # controller-local install, so the editor says so rather than
            # offering a button that 503s.
            "svc_available": svc.available(),
            "job_id": job_id_from_work_dir(wd)}


@api.get("/api/remix")
def remix_load(work_dir: str = Query("")) -> dict:
    wd = Path(work_dir) if work_dir else gapp._latest_work_dir()
    if wd is None:
        raise HTTPException(404, "No job available.")
    combined = wd / "combined.mp4"
    music = wd / "background_music.wav"
    # A performance film has no score and no separate narration track — its
    # audio comes out of the video model with the picture — so a missing music
    # track is normal there, not a broken film. The mixer is reported as
    # unavailable instead of 404ing the whole screen.
    if not combined.exists():
        raise HTTPException(404, f"Required files not found in {wd.name}.")
    can_remix = music.exists()
    # Preview the actual published final (full voice/music/ambient mix) — the
    # same file Publish reads. Globbing the work dir returned combined.mp4
    # (narration only, no music) before any remix existed. See issue #14.
    final_vid = gapp._final_path_for_work_dir(wd)
    # Default the sliders to the volumes that produced that final (per-film
    # job_config, then global config) so a no-op re-mix reproduces it.
    cfg = gapp.load_config()
    jc = _film_job_config(wd)
    try:
        meta = json.loads((wd / "job.json").read_text())
    except Exception:
        meta = {}
    _title = _video_title_for(wd)
    mix_vols = _mix_volumes(wd, jc, cfg)
    return {
        "work_dir": str(wd),
        "final_url": _busted_file_url(final_vid),
        # False for performance films: there is no music or narration stem to
        # re-balance, so the mix controls have nothing to act on.
        "can_remix": can_remix,
        # A music video's levels are pinned to music-only (see _mix_volumes) —
        # the mixer card shows and offers exactly that.
        "voice_vol": mix_vols[0],
        "music_vol": mix_vols[1],
        "ambient_vol": mix_vols[2],
        "voice": jc.get("default_voice", ""),
        "voices": gapp.get_voice_choices(),
        "music_desc": jc.get("music_desc", ""),
        "music_history": music_history.history(wd),
        "video_history": final_video_history.history(wd),
        # A song film's song, so the finished film still shows WHAT is being
        # sung (lyrics, current voice) and can re-sing / re-voice from here.
        "song": _remix_song_info(wd),
        # Short text on the cover image + first-frame burn: saved override,
        # else derived from the title (edit it from the cover card).
        "cover_phrase": cover_phrase_for(wd, _title, _cover_typography_for(wd)["accent"]),
        "cover_phrase_default": default_cover_phrase(_title, _cover_typography_for(wd)["accent"]),
        # Whether a text-free background exists, so "Re-apply text" can work
        # (covers that predate typography need one regeneration first).
        "cover_has_bg": (wd / COVER_BASE_NAME).exists(),
        "resolution": jc.get("resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION),
        # Whether the published final is larger than the render tier, and the
        # size's name — the editor's "Upscaled to …" indication.
        "upscale": _final_upscale_info(wd),
        # The look this film was shot in, for the "Restyle this film" card.
        "style_name": jc.get("style_name") or "",
        "style": jc.get("style") or "",
        # Whether this film's final carries burned-in (open) captions, so the
        # Subtitles card offers the opposite action (burn ⇄ remove).
        "burn_subtitles": bool(jc.get("burn_subtitles")),
        # Opening title / end credits (Titles & credits card): the standing
        # settings, pre-filled so the form opens ready, and whether the
        # published cut carries them right now.
        "title_cards": _title_cards_form(wd, jc),
        "title_cards_default_font": _title_cards_default_font(wd),
        "title_cards_applied": bool(_title_cards.applied_title_cards(wd, final_vid)),
        "title_card_images": _title_card_images(wd, jc),
        # Same publish/approval status the Films tab shows, so the review screen
        # can surface the Approve gate (publish_require_approval) inline.
        **_film_publish_status(wd, meta, cfg),
    }


@api.post("/api/remix")
def remix_apply(body: RemixBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    combined = wd / "combined.mp4"
    music = wd / "background_music.wav"
    ambient = wd / "ambient.wav"
    if not music.exists():
        raise HTTPException(400, "This film has no music or narration track to re-mix — "
                                 "its audio was generated with the picture.")
    # A music video's mix is the song alone whatever arrives here: the editor
    # offers it no voice/ambient sliders, so a non-zero level could only come
    # from a stale client.
    voice_vol, ambient_vol = body.voice_vol, body.ambient_vol
    if _is_music_video(wd):
        voice_vol = ambient_vol = 0.0
    with _track_op("Remixing audio", wd.name):
        final_path, message = gapp.on_remix(
            str(combined), str(music),
            str(ambient) if ambient.exists() else "",
            voice_vol=voice_vol, music_vol=body.music_vol, ambient_vol=ambient_vol,
        )
        if final_path:
            _maybe_burn_subtitles(wd, final_path)
            _maybe_burn_first_frame_cover(wd, final_path)
            _maybe_apply_title_cards(wd, final_path)
    if not final_path:
        raise HTTPException(500, message or "Remix failed.")
    return {"message": message, "final_url": _busted_file_url(Path(final_path))}


class RemixSubtitlesBody(BaseModel):
    work_dir: str
    burn: bool = True


def _burn_subtitles_onto_cut(wd: Path, curated: dict) -> dict:
    """Draw the captions straight onto the published cut, in place.

    The rebuild below re-makes the final from the clean scene parts, which
    throws away a derived cut — an upscale, a localized re-voicing — the very
    work the versions list exists to keep. When the picked cut is one of
    those, burn onto it instead. The cues are shifted past any opening title
    card (the rebuild gets that for free, burning before the cards go on) and
    the burnt cut is kept as its own version."""
    if _film_job_config(wd).get("burn_subtitles"):
        raise HTTPException(400, f"“{curated['label']}” already carries burned captions — "
                                 "burning again would print them twice. Remove them first.")
    from pipeline.captions import build_srt, burn_srt_into_video
    final_path = gapp._final_path_for_work_dir(wd)
    orig = _video_language_for_work_dir(wd, "en")
    cut_lang = _published_cut_language(wd, orig)
    localized = None if cut_lang == orig else cut_lang
    style = _subtitle_style_for(wd)
    srt = build_srt(wd, lang=localized, timing_lang=localized,
                    offset=_title_cards_head_seconds(wd), style=style)
    if srt is None:
        raise HTTPException(400, "Nothing to caption — this film has no "
                                 "narration, dialogue or lyrics on its scenes.")
    with _track_op("Burning subtitles", wd.name):
        final_video_history.seed_if_empty(wd, final_path, "Original")
        burn_srt_into_video(final_path, srt, style=style)
        jc = _film_job_config(wd)
        jc["burn_subtitles"] = True
        _write_film_job_config(wd, jc)
        final_video_history.record(wd, final_path, label="Subtitles burned",
                                   lang=curated.get("lang"), kind="subtitles")
    return {
        "message": f"Subtitles burned into the picked cut (“{curated['label']}”), "
                   "which keeps it as it is instead of rebuilding from the scene parts.",
        "final_url": _busted_file_url(final_path),
        "burn_subtitles": True,
        "video_history": final_video_history.history(wd),
    }


@api.post("/api/remix/subtitles")
def remix_subtitles(body: RemixSubtitlesBody) -> dict:
    """Burn the film's captions into the picture after the fact — or remove
    a burn again — by rebuilding the final from the (caption-free) scene
    finals with the film's standing ``burn_subtitles`` flag flipped.

    A picked cut the rebuild cannot reproduce (an upscale, a localized
    re-voicing) is burnt in place instead, so the choice under Versions
    survives — see _burn_subtitles_onto_cut. Removing a burn always rebuilds:
    captions live in the pixels, and only a fresh picture is free of them.

    The flag is persisted to the film's job_config first, so every later
    rebuild (remix, re-voice, reassemble, localize) keeps the choice. Works
    for any film the reassemble path can rebuild — narrated, acted and song
    films alike; the caption track covers narration, an acted scene's
    dialogue lines and a singing scene's lyrics."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    curated = _curated_final_version(wd)
    if body.burn and curated:
        return _burn_subtitles_onto_cut(wd, curated)
    if body.burn:
        from pipeline.captions import build_srt
        if build_srt(wd, style=_subtitle_style_for(wd)) is None:
            raise HTTPException(400, "Nothing to caption — this film has no "
                                     "narration, dialogue or lyrics on its scenes.")
    jc = _film_job_config(wd)
    jc["burn_subtitles"] = bool(body.burn)
    _write_film_job_config(wd, jc)
    try:
        _reassemble_film_core(
            wd, "Burning subtitles" if body.burn else "Removing subtitles")
    except ValueError as e:
        raise HTTPException(400, str(e))
    final_path = gapp._final_path_for_work_dir(wd)
    message = ("Subtitles burned into the picture."
               if body.burn else "Burned subtitles removed.")
    if curated:
        message += (f" This replaced the picked cut “{curated['label']}” with a fresh build "
                    "of the scene parts — captions live in the pixels, so removing them "
                    "means re-drawing the picture. Pick it again under Versions to get it back.")
    return {
        "message": message,
        "final_url": _busted_file_url(final_path),
        "burn_subtitles": bool(body.burn),
        "video_history": final_video_history.history(wd),
    }


def _run_remix_narrator(task_id: str, wd: Path, voice: str) -> None:
    from pipeline.assembler import concatenate_scenes, mix_background_music

    started = _film_task_started_at(task_id) or time.time()
    try:
        voice_name = (voice or "").strip()
        jc = _film_job_config(wd)
        jc["default_voice"] = voice_name
        jc["voice_ref"] = str(_voice_ref_for_name(voice_name) or "")
        _write_film_job_config(wd, jc)

        job_id = job_id_from_work_dir(wd)
        store = DurableStore.default()
        try:
            rows = store.scene_rows(job_id)
            if not rows:
                raise RuntimeError("No scene data found.")
            for row in rows:
                meta = dict(row.get("metadata") or {})
                if voice_name:
                    meta["voice"] = voice_name
                else:
                    meta.pop("voice", None)
                store.upsert_scene(
                    job_id,
                    int(row["id"]),
                    title=row.get("title", ""),
                    image_prompt=row.get("image_prompt", ""),
                    video_prompt=row.get("video_prompt", ""),
                    narration=row.get("narration", ""),
                    preview_path=row.get("preview_path", ""),
                    metadata=meta,
                )
            rows = store.scene_rows(job_id)
        finally:
            store.close()
        gapp._persist_script_snapshot(wd, rows)

        order = _load_scene_order(wd) or [int(r.get("id") or 0) for r in rows]
        row_by_id = {int(r.get("id") or 0): r for r in rows}
        for idx, sid in enumerate(order, start=1):
            row = row_by_id.get(int(sid))
            if not row:
                continue
            _film_tasks[task_id] = {
                "status": "running",
                "step": "narration",
                "scene_id": int(sid),
                "current": idx,
                "total": len(order),
            }
            video_history.capture_current(wd, int(sid), wd / f"scene_{int(sid):02d}_final.mp4")
            _render_scene_narration(task_id, wd, int(sid), jc, row, voice_name)

        scene_finals = [
            wd / f"scene_{int(sid):02d}_final.mp4"
            for sid in order
            if (wd / f"scene_{int(sid):02d}_final.mp4").exists()
            and (wd / f"scene_{int(sid):02d}_final.mp4").stat().st_size > 10_000
        ]
        if not scene_finals:
            raise RuntimeError("No rendered scenes found after narrator regeneration.")

        _film_checkpoint(task_id)
        _film_tasks[task_id] = {"status": "running", "step": "finalize"}
        combined = wd / "combined.mp4"
        final_path = gapp._final_path_for_work_dir(wd)
        music_path = wd / "background_music.wav"
        if not music_path.exists():
            raise RuntimeError("No background music found in this film folder.")
        cfg = gapp.load_config()
        ambient = wd / "ambient.wav"
        concatenate_scenes(scene_finals, combined,
                           hard_boundaries=_film_hard_boundaries(wd, scene_finals))
        voice_vol, music_vol, ambient_vol = _mix_volumes(wd, jc, cfg)
        mix_background_music(
            combined, music_path, final_path,
            volume=music_vol / 100.0,
            voice_volume=voice_vol / 100.0,
            ambient_path=ambient if ambient.exists() else None,
            ambient_volume=ambient_vol / 100.0,
        )
        _maybe_burn_subtitles(wd, final_path)
        _maybe_burn_first_frame_cover(wd, final_path)
        _maybe_apply_title_cards(wd, final_path)
        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "voice": voice_name,
            "scene_count": len(scene_finals),
        }
    except Exception as e:
        _finish_film_task_error(task_id, e)
    finally:
        _record_film_task_activity(
            task_id,
            started=started,
            done_name="Changed narrator",
            failed_name="Narrator change failed",
            cancelled_name="Narrator change cancelled",
            detail=wd.name,
        )


@api.post("/api/remix/narrator")
def remix_narrator(body: RemixNarratorBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if not (wd / "combined.mp4").exists():
        raise HTTPException(404, f"combined.mp4 not found in {wd.name}.")
    voice = (body.voice or "").strip()
    if voice and voice not in gapp.get_voice_choices():
        raise HTTPException(400, f"Unknown narrator: {voice}")

    tid = f"narrator_regen_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": "narration"}
    _film_task_meta[tid] = {
        "work_dir": str(wd), "scene_id": 0, "component": "narrator",
        "started_at": time.time(),
    }
    threading.Thread(
        target=_run_remix_narrator, args=(tid, wd, voice), daemon=True,
    ).start()
    return {"ok": True, "task_id": tid}


class LocalizeFilmBody(BaseModel):
    work_dir: str
    language: str


def _run_localize_film(task_id: str, wd: Path, lang: str) -> None:
    """Background thread: translate narration + re-synthesize it in *lang* with
    Chatterbox Multilingual, reusing the existing scene videos and background
    music untouched. Never overwrites the original-language narration/scene
    finals — those live under localize/{lang}/ until the final assembly step,
    which only replaces the whole-film canonical file (kept as a switchable
    final_video_history version, same mechanism as upscale)."""
    import shutil
    from pipeline.chatterbox import LANGUAGES
    from pipeline.llm import translate_narrations

    started = _film_task_started_at(task_id) or time.time()
    lang_dir = wd / "localize" / lang
    try:
        jc = _film_job_config(wd)
        final_path = gapp._final_path_for_work_dir(wd)
        final_video_history.seed_if_empty(
            wd, final_path, "Original", lang=gapp._norm_tts_language(jc.get("tts_language")),
        )

        rows = gapp._load_scenes_for_work_dir(wd)
        if not rows:
            raise RuntimeError("No scene data found.")
        row_by_id = {int(r.get("id") or 0): r for r in rows}
        order = _load_scene_order(wd) or list(row_by_id.keys())

        _film_checkpoint(task_id)
        _film_tasks[task_id] = {"status": "running", "step": "translate"}
        title = (jc.get("video_title") or jc.get("title") or wd.name).strip()
        translatable = [
            row_by_id[sid] for sid in order
            if sid in row_by_id
            and str((row_by_id[sid].get("metadata") or {}).get("mode") or "narration") != "silent"
            and (row_by_id[sid].get("narration") or "").strip()
        ]
        translations = translate_narrations(translatable, LANGUAGES[lang], title=title) if translatable else {}
        scripts_dir = wd / "localize_scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / f"{lang}.json").write_text(
            json.dumps({"lang": lang, "scenes": {str(k): v for k, v in translations.items()}}, indent=2)
        )
        # Publish metadata (title/description) in the same language, ready for
        # the Publish screen. Best-effort — it translates lazily there if this
        # fails, and a metadata hiccup must not fail the whole localization.
        try:
            _localize_metadata(wd, lang)
        except Exception as exc:
            gapp.logger.warning("Localized metadata for %s failed (deferred): %s", lang, exc)

        lang_dir.mkdir(parents=True, exist_ok=True)
        jobs: dict[int, dict] = {}
        for sid in order:
            row = row_by_id.get(int(sid))
            if not row:
                continue
            src_final = wd / f"scene_{int(sid):02d}_final.mp4"
            if int(sid) not in translations:
                # Silent/dialogue/untranslated scene — carry the original-language
                # scene final through unchanged so the concatenation stays complete.
                if src_final.exists():
                    shutil.copy2(src_final, lang_dir / f"scene_{int(sid):02d}_final.mp4")
                continue
            jobs[int(sid)] = {**row, "narration": translations[int(sid)]}
        if jobs:
            _localize_synthesize_scenes(task_id, wd, jc, lang, jobs)

        _film_checkpoint(task_id)
        _film_tasks[task_id] = {"status": "running", "step": "finalize"}
        history, scene_count = _assemble_localized_final(wd, lang, jc, order)

        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "video_history": history,
            "lang": lang,
            "scene_count": scene_count,
        }
    except Exception as e:
        _finish_film_task_error(task_id, e)
    finally:
        _record_film_task_activity(
            task_id,
            started=started,
            done_name="Localized film",
            failed_name="Localization failed",
            cancelled_name="Localization cancelled",
            detail=f"{wd.name} → {lang}",
        )


@api.post("/api/remix/localize")
def remix_localize(body: LocalizeFilmBody) -> dict:
    from pipeline.chatterbox import LANGUAGES, norm_language

    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if not (wd / "combined.mp4").exists():
        raise HTTPException(404, f"combined.mp4 not found in {wd.name}.")
    lang = norm_language(body.language)
    if body.language not in LANGUAGES:
        raise HTTPException(400, f"Unknown language: {body.language}")
    jc = _film_job_config(wd)
    if lang == gapp._norm_tts_language(jc.get("tts_language")):
        raise HTTPException(400, "That is already this film's original language.")

    tid = f"localize_{lang}_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": "translate"}
    _film_task_meta[tid] = {
        "work_dir": str(wd), "scene_id": 0, "component": "localize",
        "started_at": time.time(),
    }
    threading.Thread(
        target=_run_localize_film, args=(tid, wd, lang), daemon=True,
    ).start()
    return {"ok": True, "task_id": tid}


def _localize_synthesize_scenes(task_id: str, wd: Path, jc: dict, lang: str,
                                jobs: dict[int, dict]) -> None:
    """Fan translated-scene synthesis across the TTS fleet.

    *jobs* maps scene id → its row with the TRANSLATED narration already in
    place. Each scene acquires one TTS worker from a per-URL semaphore pool
    (all reachable ``tts_workers``), so with 3 workers three scenes voice at
    once — same fanout shape as the parallel scene upscale, including per-scene
    Activity sub-jobs. ``_film_tasks`` shows aggregate fanout progress."""
    import concurrent.futures
    from pipeline.chatterbox import LANGUAGES
    from pipeline.tts_worker import worker_alive
    from pipeline.worker_pool import WorkerPool

    cfg = gapp.load_config()
    configured = [h for h in (cfg.get("tts_workers") or []) if str(h).strip()]
    hosts = [h for h in configured if worker_alive(h, timeout=3)]
    if not hosts:
        # No reachable remote worker — keep the sequential single-host behavior
        # (_render_scene_narration falls back to the first configured/localhost).
        hosts = configured[:1] or ["localhost"]
    pool = WorkerPool(hosts)
    n = len(jobs)
    lang_dir = wd / "localize" / lang
    try:
        film_title = _video_title_for(wd)
    except Exception:
        film_title = wd.name
    per_scene_est, _learned = film_timing.estimate("narrator_scene")
    _film_tasks[task_id] = {
        "status": "running", "step": "narration", "fanout": True,
        "current": 0, "total": n,
    }

    def synth_one(sid: int, row: dict) -> int:
        _film_checkpoint(task_id)
        sub_id = f"film:{task_id}#s{sid}"
        sub_fields = dict(
            name=f"Voicing scene {sid} in {LANGUAGES.get(lang, lang)}",
            detail=f"{n} scene{'s' if n != 1 else ''} → {LANGUAGES.get(lang, lang)}",
            work_dir=str(wd),
            title=film_title,
            est_seconds=per_scene_est,
        )
        # In line for a worker until acquire() returns — show it that way.
        _register_film_subjob(sub_id, **sub_fields, queued=True)
        host = pool.acquire()
        # On a GPU now — flip the row to running and start its ETA clock. The
        # timing sample starts here too: the queue wait is not synthesis time,
        # and folding it in would inflate every later scene's ETA.
        _register_film_subjob(sub_id, **sub_fields)
        sub_started = time.time()
        try:
            _render_scene_narration(
                task_id, wd, sid, jc, row,
                voice_name=_scene_voice_name(row, jc),
                language=lang,
                tts_engine_override="chatterbox-multilingual",
                out_dir=lang_dir,
                record_video_history=False,
                tts_host=host,
                update_task=False,
            )
            film_timing.record("narrator_scene", time.time() - sub_started)
            return sid
        finally:
            _clear_film_subjob(sub_id)
            pool.release(host)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(hosts), n)) as executor:
            futures = [executor.submit(synth_one, sid, row) for sid, row in jobs.items()]
            done = 0
            for fut in concurrent.futures.as_completed(futures):
                fut.result()
                done += 1
                _film_tasks[task_id] = {
                    "status": "running", "step": "narration", "fanout": True,
                    "current": done, "total": n,
                }
    finally:
        _clear_film_subjobs_for(task_id)


def _assemble_localized_final(wd: Path, lang: str, jc: dict, order: list[int]) -> tuple[dict, int]:
    """Concatenate the localized scene finals in ``localize/{lang}/``, mix the
    film's music/ambient back in at the film's volumes, swap the canonical
    final, and record it as a switchable history version tagged with *lang*.
    Scenes missing from the language folder fall back to the original cut's
    scene final (silent/dialogue scenes are carried through, not re-voiced).
    Returns ``(history, scene_count)``."""
    import shutil
    from pipeline.assembler import concatenate_scenes, mix_background_music
    from pipeline.chatterbox import LANGUAGES

    lang_dir = wd / "localize" / lang
    final_path = gapp._final_path_for_work_dir(wd)
    for sid in order:
        loc = lang_dir / f"scene_{int(sid):02d}_final.mp4"
        src = wd / f"scene_{int(sid):02d}_final.mp4"
        if not loc.exists() and src.exists():
            shutil.copy2(src, loc)
    scene_finals = [
        lang_dir / f"scene_{int(sid):02d}_final.mp4" for sid in order
        if (lang_dir / f"scene_{int(sid):02d}_final.mp4").exists()
        and (lang_dir / f"scene_{int(sid):02d}_final.mp4").stat().st_size > 10_000
    ]
    if not scene_finals:
        raise RuntimeError("No scene clips produced for this language.")

    combined = lang_dir / "combined.mp4"
    music_path = wd / "background_music.wav"
    if not music_path.exists():
        raise RuntimeError("No background music found in this film folder.")
    ambient = wd / "ambient.wav"
    cfg = gapp.load_config()
    concatenate_scenes(scene_finals, combined,
                       hard_boundaries=_film_hard_boundaries(wd, scene_finals))
    staged_final = wd / "final_localize.staging.mp4"
    voice_vol, music_vol, ambient_vol = _mix_volumes(wd, jc, cfg)
    mix_background_music(
        combined, music_path, staged_final,
        volume=music_vol / 100.0,
        voice_volume=voice_vol / 100.0,
        ambient_path=ambient if ambient.exists() else None,
        ambient_volume=ambient_vol / 100.0,
    )
    staged_final.replace(final_path)
    # Styles that auto-stamp the cover into the opening get the stamp on the
    # localized cut too — with the localized cover (re-titled in *lang*) when
    # one can be rendered, so the burned frame matches the published language.
    loc_cover = None
    try:
        loc_cover = _render_localized_cover(wd, lang)
    except Exception as exc:
        gapp.logger.warning("Localized cover for %s failed (burning the original): %s",
                            lang, exc)
    # A localized cut gets its captions burned in the published language too.
    _maybe_burn_subtitles(wd, final_path, lang=lang)
    _maybe_burn_first_frame_cover(wd, final_path, cover_path=loc_cover)
    _maybe_apply_title_cards(wd, final_path)
    history = final_video_history.record(wd, final_path, label=LANGUAGES[lang], lang=lang,
                                         kind="localize")
    return history, len(scene_finals)


def _run_localize_edit(task_id: str, wd: Path, lang: str, changed: dict[int, str]) -> None:
    """Background thread: re-voice only the scenes whose translated narration
    was edited, then reassemble the localized final as a new history version.
    The edits are persisted to localize_scripts/{lang}.json first, so captions
    and future re-voicing always read what's actually spoken."""
    started = _film_task_started_at(task_id) or time.time()
    lang_dir = wd / "localize" / lang
    try:
        jc = _film_job_config(wd)
        final_path = gapp._final_path_for_work_dir(wd)
        final_video_history.seed_if_empty(
            wd, final_path, "Original", lang=gapp._norm_tts_language(jc.get("tts_language")),
        )
        rows = gapp._load_scenes_for_work_dir(wd)
        if not rows:
            raise RuntimeError("No scene data found.")
        row_by_id = {int(r.get("id") or 0): r for r in rows}
        order = _load_scene_order(wd) or list(row_by_id.keys())

        script_path = wd / "localize_scripts" / f"{lang}.json"
        data = json.loads(script_path.read_text())
        scenes_map = data.get("scenes") or {}
        scenes_map.update({str(k): v for k, v in changed.items()})
        data["scenes"] = scenes_map
        script_path.write_text(json.dumps(data, indent=2))

        jobs = {
            sid: {**row_by_id[sid], "narration": text}
            for sid, text in changed.items() if sid in row_by_id
        }
        if jobs:
            _localize_synthesize_scenes(task_id, wd, jc, lang, jobs)

        _film_checkpoint(task_id)
        _film_tasks[task_id] = {"status": "running", "step": "finalize"}
        history, _count = _assemble_localized_final(wd, lang, jc, order)
        # The narration changed — any previously exported dubbed audio is stale.
        (lang_dir / "dubbed_audio.m4a").unlink(missing_ok=True)

        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "video_history": history,
            "lang": lang,
        }
    except Exception as e:
        _finish_film_task_error(task_id, e)
    finally:
        _record_film_task_activity(
            task_id,
            started=started,
            done_name="Localized narration updated",
            failed_name="Localization edit failed",
            cancelled_name="Localization edit cancelled",
            detail=f"{wd.name} → {lang}",
        )


@api.get("/api/remix/localize/scripts")
def localize_scripts(work_dir: str = Query(...)) -> dict:
    """Every saved localization for a film: per-scene original + translated
    narration (for the edit UI), plus whether a dubbed-audio export exists."""
    from pipeline.chatterbox import LANGUAGES

    wd = Path(work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    jc = _film_job_config(wd)
    orig = gapp._norm_tts_language(jc.get("tts_language"))
    rows = gapp._load_scenes_for_work_dir(wd)
    row_by_id = {int(r.get("id") or 0): r for r in rows}
    order = _load_scene_order(wd) or list(row_by_id.keys())

    localizations = []
    scripts_dir = wd / "localize_scripts"
    for path in sorted(scripts_dir.glob("*.json")) if scripts_dir.exists() else []:
        lang = path.stem
        if lang not in LANGUAGES:
            continue
        try:
            translations = {
                int(k): v for k, v in (json.loads(path.read_text()).get("scenes") or {}).items()
                if isinstance(v, str) and v.strip()
            }
        except Exception:
            continue
        scenes = [
            {
                "id": int(sid),
                "original": (row_by_id[int(sid)].get("narration") or "").strip(),
                "translated": translations[int(sid)],
            }
            for sid in order if int(sid) in translations and int(sid) in row_by_id
        ]
        audio = wd / "localize" / lang / "dubbed_audio.m4a"
        localizations.append({
            "lang": lang,
            "name": LANGUAGES[lang],
            "scenes": scenes,
            "audio_url": f"/api/file?path={audio}" if audio.exists() else "",
        })
    return {
        "original_lang": orig,
        "original_name": LANGUAGES.get(orig, orig.upper()),
        "localizations": localizations,
    }


class LocalizeScriptBody(BaseModel):
    work_dir: str
    language: str
    scenes: dict[str, str]


@api.post("/api/remix/localize/script")
def localize_script_save(body: LocalizeScriptBody) -> dict:
    """Apply edited translated narration lines: persists them, re-voices only
    the changed scenes, and reassembles the localized final."""
    from pipeline.chatterbox import LANGUAGES

    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    lang = body.language
    if lang not in LANGUAGES:
        raise HTTPException(400, f"Unknown language: {lang}")
    script_path = wd / "localize_scripts" / f"{lang}.json"
    if not script_path.exists():
        raise HTTPException(404, f"No {LANGUAGES[lang]} localization exists for this film.")
    try:
        stored = json.loads(script_path.read_text()).get("scenes") or {}
    except Exception:
        stored = {}
    changed = {}
    for key, text in body.scenes.items():
        try:
            sid = int(key)
        except (TypeError, ValueError):
            continue
        text = (text or "").strip()
        # Only re-voice scenes that already have a translation and actually
        # changed — an emptied line falls back to nothing (can't unvoice a scene
        # from here) and an untouched line would waste a TTS round-trip.
        if text and str(sid) in stored and text != (stored.get(str(sid)) or "").strip():
            changed[sid] = text
    if not changed:
        raise HTTPException(400, "No changed lines to apply.")

    tid = f"localize_edit_{lang}_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": "narration"}
    _film_task_meta[tid] = {
        "work_dir": str(wd), "scene_id": 0, "component": "localize",
        "started_at": time.time(),
    }
    threading.Thread(
        target=_run_localize_edit, args=(tid, wd, lang, changed), daemon=True,
    ).start()
    return {"ok": True, "task_id": tid, "changed": sorted(changed)}


def _localize_metadata(wd: Path, lang: str) -> dict[str, str]:
    """Localized publish metadata (title + description + cover phrase) for
    *lang*, translating and caching into localize_scripts/{lang}.json on first
    use. The source is the film's canonical title, cached description, and
    cover phrase at call time."""
    from pipeline.chatterbox import LANGUAGES
    from pipeline.llm import translate_metadata

    script_path = wd / "localize_scripts" / f"{lang}.json"
    if not script_path.exists():
        raise FileNotFoundError(f"No {LANGUAGES.get(lang, lang)} localization exists for this film.")
    data = json.loads(script_path.read_text())
    if (data.get("title") or "").strip():
        return {"title": data["title"], "description": data.get("description") or "",
                "cover_phrase": data.get("cover_phrase") or ""}
    title = _video_title_for(wd)
    translated = translate_metadata(
        title, _cached_description(wd), LANGUAGES.get(lang, lang),
        cover_phrase=cover_phrase_for(wd, title, _cover_typography_for(wd)["accent"]),
    )
    data["title"] = translated["title"]
    data["description"] = translated["description"]
    data["cover_phrase"] = translated.get("cover_phrase") or ""
    script_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return translated


class LocalizeMetadataBody(BaseModel):
    work_dir: str
    language: str


@api.post("/api/remix/localize/metadata")
def localize_metadata(body: LocalizeMetadataBody) -> dict:
    """Localized title + description for publishing a localized cut (translated
    on first request, cached in the localization's script file after that)."""
    from pipeline.chatterbox import LANGUAGES

    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if body.language not in LANGUAGES:
        raise HTTPException(400, f"Unknown language: {body.language}")
    try:
        with _track_op("Translating metadata", wd.name):
            out = _localize_metadata(wd, body.language)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"Metadata translation failed: {str(e).splitlines()[0][:200]}")
    # Localized cover (the same art re-titled with the translated phrase), so
    # the Publish screen's thumbnail preview follows the version switch.
    cover_url = ""
    try:
        loc = _render_localized_cover(wd, body.language)
        if loc and loc.stat().st_size > 1000:
            cover_url = _busted_file_url(loc)
    except Exception as exc:
        gapp.logger.warning("Localized cover for %s failed: %s", body.language, exc)
    return {"ok": True, **out, "cover_url": cover_url}


class LocalizeAudioBody(BaseModel):
    work_dir: str
    language: str


def _build_dubbed_audio(wd: Path, lang: str) -> Path:
    """Audio-only dub of the film in *lang*, time-aligned to the ORIGINAL cut.

    YouTube's Multi-Language Audio replaces the published video's audio track
    wholesale, so the dub must line up with the original video's timeline — but
    the localized cut re-times every scene to its translated narration
    (mux_video_audio sizes each scene to its narration). Realign per scene:
    translated narration is padded with silence (shorter) or gently sped up
    (longer) to the original scene's exact duration; untranslated scenes keep
    their original audio; then the film's music/ambient mix is applied at the
    same volumes. The result drops straight into YouTube Studio ▸ Languages."""
    import shutil
    import subprocess
    from pipeline.assembler import _FFMPEG, _get_duration

    jc = _film_job_config(wd)
    cfg = gapp.load_config()
    lang_dir = wd / "localize" / lang
    row_ids = [int(r.get("id") or 0) for r in gapp._load_scenes_for_work_dir(wd)]
    order = _load_scene_order(wd) or row_ids

    seg_dir = lang_dir / "mla_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []
    try:
        for sid in order:
            orig_final = wd / f"scene_{int(sid):02d}_final.mp4"
            target = _get_duration(orig_final) if orig_final.exists() else 0.0
            if target <= 0:
                continue  # scene absent from the original cut's timeline too
            seg = seg_dir / f"seg_{int(sid):02d}.wav"
            loc_wav = lang_dir / f"scene_{int(sid):02d}_narration.wav"
            common = ["-t", f"{target:.3f}", "-ar", "48000", "-ac", "2", str(seg)]
            if loc_wav.exists():
                dur = _get_duration(loc_wav)
                # >2% over the slot: compress to fit (atempo keeps pitch); the
                # translation prompt targets similar pacing so this stays subtle.
                tempo = dur / target if target and dur > target * 1.02 else 1.0
                filt = (f"atempo={min(tempo, 4.0):.4f}," if tempo > 1.0 else "") + "apad"
                cmd = [_FFMPEG, "-y", "-i", str(loc_wav), "-af", filt, *common]
            else:
                # Untranslated (silent/dialogue) scene — keep its original audio.
                cmd = [_FFMPEG, "-y", "-i", str(orig_final), "-vn", "-af", "apad", *common]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            except subprocess.CalledProcessError:
                # No audio stream on the source (fully silent scene) → silence.
                subprocess.run(
                    [_FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", *common],
                    check=True, capture_output=True, timeout=300,
                )
            segments.append(seg)
        if not segments:
            raise RuntimeError("No scene audio found to build the dubbed track from.")

        concat_list = seg_dir / "segments.txt"
        concat_list.write_text("".join(f"file '{p}'\n" for p in segments))
        voice = seg_dir / "voice.wav"
        subprocess.run(
            [_FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c:a", "pcm_s16le", str(voice)],
            check=True, capture_output=True, timeout=600,
        )

        out = lang_dir / "dubbed_audio.m4a"
        music = wd / "background_music.wav"
        ambient = wd / "ambient.wav"
        voice_vol, music_vol, ambient_vol = (v / 100.0 for v in _mix_volumes(wd, jc, cfg))
        if music.exists():
            # Same mix mix_background_music applies to the published final, so
            # the dub sounds identical to the film — just voiced in *lang*.
            use_ambient = ambient.exists() and ambient_vol > 0
            chains = [f"[0:a]volume={voice_vol:.3f}[voice]", f"[1:a]volume={music_vol:.3f}[bg]"]
            inputs = ["-i", str(voice), "-i", str(music)]
            if use_ambient:
                chains.append(f"[2:a]volume={ambient_vol:.3f}[amb]")
                inputs += ["-i", str(ambient)]
            labels = "[voice][bg][amb]" if use_ambient else "[voice][bg]"
            filt = ";".join(chains) + (
                f";{labels}amix=inputs={3 if use_ambient else 2}"
                ":duration=first:dropout_transition=3:normalize=0[aout]"
            )
            subprocess.run(
                [_FFMPEG, "-y", *inputs, "-filter_complex", filt, "-map", "[aout]",
                 "-c:a", "aac", "-b:a", "192k", str(out)],
                check=True, capture_output=True, timeout=600,
            )
        else:
            subprocess.run(
                [_FFMPEG, "-y", "-i", str(voice), "-c:a", "aac", "-b:a", "192k", str(out)],
                check=True, capture_output=True, timeout=600,
            )
        return out
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)


@api.post("/api/remix/localize/audio")
def localize_audio(body: LocalizeAudioBody) -> dict:
    """Build (or rebuild) the downloadable dubbed audio track for a localization
    — an original-timeline dub ready for YouTube Studio's multi-language audio."""
    from pipeline.chatterbox import LANGUAGES

    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    lang = body.language
    if lang not in LANGUAGES:
        raise HTTPException(400, f"Unknown language: {lang}")
    if not (wd / "localize" / lang).exists():
        raise HTTPException(404, f"No {LANGUAGES[lang]} localization exists for this film.")
    try:
        out = _build_dubbed_audio(wd, lang)
    except Exception as e:
        raise HTTPException(500, f"Dubbed audio build failed: {str(e).splitlines()[0][:200]}")
    return {"ok": True, "audio_url": f"/api/file?path={out}&t={int(time.time())}"}


class MusicRegenBody(BaseModel):
    work_dir: str
    music_desc: str = ""


def _run_music_regen(task_id: str, wd: Path, music_desc: str) -> None:
    """Background thread: regenerate the background music, then re-mux the film.

    Music is a film-level asset (one background_music.wav), so this mirrors the
    scene re-render workers: it generates to a staging file, swaps it in
    atomically, then re-muxes the final with the film's saved volumes. Progress
    is tracked in _film_tasks so the Remix screen can poll /api/films/task."""
    from pipeline.assembler import _get_duration
    from pipeline.comfyui import generate_music
    from pipeline.worker_pool import WorkerPool

    combined = wd / "combined.mp4"
    music_path = wd / "background_music.wav"
    staged = wd / "background_music.staging.wav"
    started = _film_task_started_at(task_id) or time.time()
    try:
        _film_checkpoint(task_id)
        worker_urls = gapp._preview_worker_urls()
        if not worker_urls:
            _film_tasks[task_id] = {"status": "error", "error": "No ComfyUI workers reachable."}
            return
        pool = WorkerPool(worker_urls)

        jc = _film_job_config(wd)
        title = (jc.get("video_title") or jc.get("title") or wd.name).strip()
        # Duration of the narration video the music plays under; fall back to the
        # existing music length if combined.mp4 is missing.
        music_dur = _get_duration(combined) if combined.exists() else _get_duration(music_path)
        # Re-generate on the engine the film was scored with; job dirs from
        # before the setting existed fall back to the style's current choice.
        music_engine = jc.get("music_engine") or gapp.style_settings(
            gapp.load_config(), jc.get("style_name") or "").get("music_engine")
        # A song film re-sings its lyrics (stamped into job_config at render
        # time; song.json is the source for dirs rendered before that stamp).
        lyrics = (jc.get("music_lyrics") or "").strip()
        if not lyrics:
            try:
                lyrics = str(json.loads((wd / "song.json").read_text()).get("lyrics") or "")
            except Exception:
                lyrics = ""

        # The original track was already seeded (with its own prompt) by the endpoint,
        # before the prompt was overwritten — see remix_regen_music.
        _film_tasks[task_id] = {"status": "running", "step": "music"}
        url = _acquire_render_worker(pool, task_id)
        try:
            # acquire() can block behind a busy GPU — re-check before submitting.
            _film_checkpoint(task_id)
            generate_music(title, music_dur, staged, (music_desc or None), comfy_url=url,
                           music_engine=music_engine, lyrics=lyrics or None)
        finally:
            pool.release(url)
        staged.replace(music_path)
        music_history.record(wd, music_path, music_desc)

        # Re-mux the final with the film's current voice/music/ambient volumes so
        # the regenerated track is what gets published.
        _film_checkpoint(task_id)
        _film_tasks[task_id]["step"] = "mux"
        cfg = gapp.load_config()
        ambient = wd / "ambient.wav"
        voice_vol, music_vol, ambient_vol = _mix_volumes(wd, jc, cfg)
        final_path, message = gapp.on_remix(
            str(combined), str(music_path),
            str(ambient) if ambient.exists() else "",
            voice_vol=voice_vol, music_vol=music_vol, ambient_vol=ambient_vol,
        )
        if not final_path:
            raise RuntimeError(message or "Re-mux failed after regenerating music.")
        _maybe_burn_subtitles(wd, final_path)
        _maybe_burn_first_frame_cover(wd, final_path)
        _maybe_apply_title_cards(wd, final_path)
        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "music_history": music_history.history(wd),
        }
    except Exception as e:
        staged.unlink(missing_ok=True)
        _finish_film_task_error(task_id, e)
    finally:
        _record_film_task_activity(
            task_id,
            started=started,
            done_name="Regenerated music",
            failed_name="Music regeneration failed",
            cancelled_name="Music regeneration cancelled",
            detail=wd.name,
        )


@api.post("/api/remix/music")
def remix_regen_music(body: MusicRegenBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    if not (wd / "combined.mp4").exists():
        raise HTTPException(404, f"combined.mp4 not found in {wd.name} — render the film first.")

    # Capture the current track as the first kept version BEFORE overwriting the
    # prompt — otherwise the original gets mislabelled with the new prompt. Read
    # the existing music_desc (what produced that track) and seed with it, then
    # persist the new (possibly edited) prompt for the regen and later re-renders.
    cfg_path = wd / "job_config.json"
    try:
        jc = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    except Exception:
        jc = {}
    try:
        music_history.seed_if_empty(wd, wd / "background_music.wav", jc.get("music_desc", ""))
    except Exception:
        gapp.logger.warning("Could not seed original music into history", exc_info=True)
    try:
        jc["music_desc"] = body.music_desc or ""
        cfg_path.write_text(json.dumps(jc, indent=2))
    except Exception:
        gapp.logger.warning("Could not persist music_desc to job_config", exc_info=True)

    tid = f"music_regen_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": "music"}
    _film_task_meta[tid] = {
        "work_dir": str(wd), "scene_id": 0, "component": "music",
        "started_at": time.time(),
    }
    threading.Thread(
        target=_run_music_regen, args=(tid, wd, body.music_desc or ""), daemon=True,
    ).start()
    return {"ok": True, "task_id": tid}


def _remux_with_current_music(wd: Path) -> str:
    """Re-mux the film's final with whatever background_music.wav now holds, at
    the volumes that produced it. Raises RuntimeError if the mix fails."""
    combined = wd / "combined.mp4"
    if not combined.exists():
        raise RuntimeError(f"combined.mp4 not found in {wd.name} — render the film first.")
    jc = _film_job_config(wd)
    cfg = gapp.load_config()
    ambient = wd / "ambient.wav"
    voice_vol, music_vol, ambient_vol = _mix_volumes(wd, jc, cfg)
    final_path, message = gapp.on_remix(
        str(combined), str(wd / "background_music.wav"),
        str(ambient) if ambient.exists() else "",
        voice_vol=voice_vol, music_vol=music_vol, ambient_vol=ambient_vol,
    )
    if not final_path:
        raise RuntimeError(message or "Re-mux failed.")
    _maybe_burn_subtitles(wd, final_path)
    _maybe_burn_first_frame_cover(wd, final_path)
    _maybe_apply_title_cards(wd, final_path)
    return final_path


class SongRevoiceBody(BaseModel):
    work_dir: str
    voice: str


def _run_song_revoice(task_id: str, wd: Path, voice: str) -> None:
    """Background thread: re-voice a finished song film's song, then re-mux.

    The slow half is seed-vc (minutes), so this runs as a film task like the
    music regen beside it. Both the sung original and the re-voicing stay in
    the music history — the version strip is where you compare them and put
    either one back."""
    started = _film_task_started_at(task_id) or time.time()
    try:
        _film_checkpoint(task_id)
        _film_tasks[task_id] = {"status": "running", "step": "revoice"}
        result = _do_song_convert(wd, voice, track_op=False)
        _film_checkpoint(task_id)
        _film_tasks[task_id]["step"] = "mux"
        final_path = _remux_with_current_music(wd)
        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "sung_as": result.get("sung_as", voice),
            "music_history": music_history.history(wd),
        }
    except Exception as e:
        (wd / "background_music.staging.wav").unlink(missing_ok=True)
        _finish_film_task_error(task_id, e)
    finally:
        _record_film_task_activity(
            task_id,
            started=started,
            done_name=f"Re-voiced the song as {voice}",
            failed_name="Song re-voicing failed",
            cancelled_name="Song re-voicing cancelled",
            detail=wd.name,
        )


@api.post("/api/remix/song-voice")
def remix_song_voice(body: SongRevoiceBody) -> dict:
    """Sing a finished film's song as a library voice, then re-mux the final."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    if not (wd / "song.json").exists():
        raise HTTPException(404, "This film has no song.")
    if not (wd / "background_music.wav").exists():
        raise HTTPException(404, "This film has no song track to re-voice.")
    if not (body.voice or "").strip():
        raise HTTPException(400, "Pick a voice to sing it.")
    from pipeline import svc
    if not svc.available():
        raise HTTPException(503, "Voice conversion is not installed — run "
                                 "scripts/install_svc.sh on the controller.")
    tid = f"song_revoice_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": "revoice"}
    # Its own component (not "music"): a seed-vc conversion is nothing like a
    # music generation, so it must not be labelled as one on Activity nor feed
    # its minutes into the music regen's learned ETA.
    _film_task_meta[tid] = {
        "work_dir": str(wd), "scene_id": 0, "component": "song_revoice",
        "started_at": time.time(),
    }
    threading.Thread(target=_run_song_revoice,
                     args=(tid, wd, body.voice.strip()), daemon=True).start()
    return {"ok": True, "task_id": tid}


class MusicSelectBody(BaseModel):
    work_dir: str
    version_id: int


@api.post("/api/remix/music-select")
def select_music(body: MusicSelectBody) -> dict:
    """Make a previously-generated music track the selected one and re-mux the film.

    Unlike scene image/video selection (which only swaps a canonical file), the
    music is baked into the published final, so selecting a version re-mixes the
    final video with the film's saved volumes — quick, ffmpeg-only, no GPU."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    try:
        music_path = music_history.select(wd, int(body.version_id))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    # A song film labels itself with whoever sings the track in use, so putting
    # the original generation back has to clear a previous re-voicing.
    sung_as = _stamp_song_voice(wd, int(body.version_id))

    combined = wd / "combined.mp4"
    if not combined.exists():
        raise HTTPException(404, f"combined.mp4 not found in {wd.name}.")
    jc = _film_job_config(wd)
    cfg = gapp.load_config()
    ambient = wd / "ambient.wav"
    voice_vol, music_vol, ambient_vol = _mix_volumes(wd, jc, cfg)
    with _track_op("Selecting music", wd.name):
        final_path, message = gapp.on_remix(
            str(combined), str(music_path),
            str(ambient) if ambient.exists() else "",
            voice_vol=voice_vol, music_vol=music_vol, ambient_vol=ambient_vol,
        )
        if final_path:
            _maybe_burn_subtitles(wd, final_path)
            _maybe_burn_first_frame_cover(wd, final_path)
            _maybe_apply_title_cards(wd, final_path)
    if not final_path:
        raise HTTPException(500, message or "Re-mux failed.")
    return {
        "ok": True,
        "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
        "sung_as": sung_as,
        "music_history": music_history.history(wd),
    }


def _run_final_video_upscale(task_id: str, wd: Path, target_name: str, upscale_mode: str) -> None:
    """Background thread: upscale the completed film, preserving selectable masters."""
    from pipeline.assembler import (
        _get_video_dimensions, parse_upscale_mode, upscale_target_dims, upscale_video,
    )

    final_path = gapp._final_path_for_work_dir(wd)
    staged = wd / "final_upscale.staging.mp4"
    started = _film_task_started_at(task_id) or time.time()
    try:
        _film_checkpoint(task_id)
        if not final_path.exists() or final_path.stat().st_size <= 0:
            raise RuntimeError("Final video not found; render the film first.")

        mode = _normalize_upscale_mode(upscale_mode)
        _engine, factor = parse_upscale_mode(mode)
        final_video_history.seed_if_empty(wd, final_path, "Original")
        _hist_before = final_video_history.history(wd)
        actual_w, actual_h = _get_video_dimensions(final_path)
        # A factor mode sizes itself off the film; only the target-sized modes
        # need a resolution picked, and the UI hides that control for the rest.
        if factor is not None:
            # ...and "the film" is the RENDERED film, not whichever version is
            # selected right now: re-running a 2× while its own 2× output is
            # the selected final must not compound to 4× — that stretches the
            # scene finals to the selected size before upscaling and misses the
            # per-scene cache. The newest base version is the plain concat of
            # the scene parts at their rendered size.
            base = next(
                (v for v in reversed(_hist_before["versions"]) if final_video_history.is_base(v)),
                None,
            )
            if base:
                actual_w, actual_h = _get_video_dimensions(Path(base["path"]))
        target_dims = None
        if factor is None:
            target_dims = gapp._UPSCALE_RESOLUTIONS.get((target_name or "").strip())
            if not target_dims:
                raise RuntimeError("Choose a valid upscale resolution.")
        target_w, target_h = upscale_target_dims(actual_w, actual_h, mode, target_dims)
        if actual_w >= target_w and actual_h >= target_h:
            raise RuntimeError(
                f"Final video is already {actual_w}x{actual_h}; choose a larger target than {target_w}x{target_h}."
            )

        # Carry the currently-selected version's language forward, so upscaling
        # a localized cut doesn't lose its language tag in the new entry.
        cur_lang = next(
            (v.get("lang") for v in _hist_before["versions"] if v["id"] == _hist_before["selected"]),
            None,
        )
        _film_tasks[task_id] = {"status": "running", "step": "final_upscale"}
        cfg = gapp.load_config()
        if mode != "fast":
            command_template = cfg.get("temporal_video_upscaler_cmd") or None
            _temporal_upscale_scenes_to_final(
                task_id, wd, staged, target_w, target_h, cfg,
                command_template=command_template,
                engine=mode,
                film_dims=(actual_w, actual_h),
            )
        else:
            upscale_video(final_path, staged, target_w, target_h)

        _film_checkpoint(task_id)
        staged.replace(final_path)
        if mode != "fast":
            # The by-scene upscale is a rebuild from the clean scene clips, so
            # it has to put back what the published final carried on top of
            # them — like every other rebuild. (The fast path upscales the
            # final itself, burns and cards included, so it must NOT re-apply:
            # a second subtitle pass would print the captions twice.)
            _maybe_burn_subtitles(wd, final_path)
            _maybe_burn_first_frame_cover(wd, final_path)
            _maybe_apply_title_cards(wd, final_path)
        mode_label = {
            "fast": "Fast",
            "ltx_latent_2x": "LTX latent 2×",
            "ic_lora": "LTX IC-LoRA",
            "h3_latent": "H3 latent",
            "flashvsr_2x": "FlashVSR 2×",
            "flashvsr_4x": "FlashVSR 4×",
        }.get(mode, mode)
        # The size is the one the film actually came out at, so a version named
        # "FlashVSR 2× 1152x1152" is exactly what FlashVSR produced.
        label = f"{mode_label} {target_w}x{target_h}"
        final_video_history.record(wd, final_path, label=label, lang=cur_lang, kind="upscale")
        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "video_history": final_video_history.history(wd),
        }
    except Exception as e:
        staged.unlink(missing_ok=True)
        _finish_film_task_error(task_id, e)
    finally:
        _record_film_task_activity(
            task_id,
            started=started,
            done_name="Upscaled final video",
            failed_name="Final video upscale failed",
            cancelled_name="Final video upscale cancelled",
            detail=target_name or wd.name,
        )


def _film_hard_boundaries(wd: Path, scene_clips: list[Path]) -> set[int]:
    """Fade-free join positions for a film REBUILD, mirroring the render.

    A scene marked ``continues_previous`` was butt-joined by the original
    render (`resume_generation` passes `hard_boundaries` to the concat); every
    rebuild — re-shoot reassembly, narrator remix, localization, the finishing
    upscale — must keep those joins or the first rebuild puts a fade back in
    the middle of a continued take. Scene ids come off the clip filenames so
    this works on whatever ordered subset a rebuild is joining; any failure
    returns an empty set (all joins keep their fades) rather than blocking.
    """
    try:
        ids: list[int] = []
        for p in scene_clips:
            m = re.match(r"scene_(\d+)_", Path(p).name)
            if not m:
                return set()
            ids.append(int(m.group(1)))
        store = DurableStore.default()
        try:
            rows = {int(r["id"]): r
                    for r in store.scene_rows(job_id_from_work_dir(wd))}
        finally:
            store.close()
        ordered = [rows[i] for i in ids if i in rows]
        if len(ordered) != len(ids):
            return set()
        cfg = _film_job_config(wd)
        return continuity.hard_boundaries(
            ordered, continuity.continuation_plan(ordered, cfg))
    except Exception:
        gapp.logger.warning("Could not compute continued-shot joins for %s",
                            wd.name, exc_info=True)
        return set()


def _rendered_scene_finals(wd: Path) -> list[Path]:
    order = _load_scene_order(wd) or []
    ordered: list[Path] = []
    for sid in order:
        try:
            p = wd / f"scene_{int(sid):02d}_final.mp4"
        except (TypeError, ValueError):
            continue
        if p.exists() and p.stat().st_size > 10_000:
            ordered.append(p)
    if ordered:
        return ordered
    return sorted(
        p for p in wd.glob("scene_*_final.mp4")
        if p.exists() and p.stat().st_size > 10_000
    )


def _normalize_upscale_mode(mode: str | None) -> str:
    """Map UI/API upscale mode strings to canonical keys.

    - fast: ffmpeg scale (simple)
    - ltx_latent: LTXVLatentUpsampler + latent spatial-upscaler-x2
    - ic_lora: LTX-2.3 IC-LoRA Pixel Spatial Upscaler (generative)
    - h3_latent: MiniMax H3 24-channel latent upscaler (learned 3D resize)
    - flashvsr_2x / flashvsr_4x: FlashVSR one-step diffusion video super-resolution
    - ltx_latent_2x: LTXVLatentUpsampler (the only factor it does)

    The factor modes finish at the source times their factor, so they take no
    target resolution — see pipeline.assembler.FACTOR_UPSCALE_MODES. Bare
    ``flashvsr`` / ``ltx_latent`` are the pre-factor spellings and resolve to
    2x; ``temporal_ai`` is an alias of ``ic_lora`` for older clients.
    """
    m = (mode or "fast").strip().lower()
    if m in {"ic_lora", "temporal_ai", "ic-lora", "iclora", "ai_temporal"}:
        return "ic_lora"
    if m in {"ltx_latent", "latent", "latent_ai", "simple_model", "ltx_latent_2x"}:
        return "ltx_latent_2x"
    if m in {"h3_latent", "h3", "minimax_h3_latent"}:
        return "h3_latent"
    if m in {"flashvsr", "flash_vsr", "flashvsr_2x"}:
        return "flashvsr_2x"
    if m == "flashvsr_4x":
        return "flashvsr_4x"
    if m in {"fast", "ffmpeg"}:
        return "fast"
    raise RuntimeError(
        "Choose a valid upscale mode (fast, flashvsr_2x, flashvsr_4x, "
        "ltx_latent_2x, ic_lora, or h3_latent)."
    )


def _temporal_upscale_scenes_to_final(
    task_id: str,
    wd: Path,
    staged_final: Path,
    target_w: int,
    target_h: int,
    cfg: dict,
    command_template: str | None = None,
    engine: str = "ic_lora",
    film_dims: tuple[int, int] | None = None,
) -> Path:
    """Upscale rendered scene clips as separate worker jobs, then rebuild final.

    *film_dims* is the assembled film's size, which the rendered scene clips do
    not all share — a re-shot scene can sit at its own size and only becomes
    uniform when the film is concatenated. A factor mode multiplies the FILM,
    so an odd-sized scene is conformed to the film first and then upscaled;
    that keeps the factor's output exact for every scene instead of leaving
    some to be stretched to match the others afterwards.
    """
    import concurrent.futures
    import shutil
    from pipeline.assembler import (
        _get_video_dimensions,
        _verify_upscale_not_blank,
        concatenate_scenes,
        ensure_video_resolution,
        mix_background_music,
        parse_upscale_mode,
        temporal_ai_upscale_video,
    )

    _engine_name, factor = parse_upscale_mode(engine)
    from pipeline.worker_pool import WorkerPool, alive_workers

    scene_finals = _rendered_scene_finals(wd)
    if not scene_finals:
        raise RuntimeError("No rendered scene clips found for scene-level temporal upscale.")

    # Pool over every reachable worker — not an idle-at-probe-time snapshot.
    # WorkerPool's cross-process lease decides who actually runs where, so a
    # concurrent render or second upscale queues instead of double-booking.
    worker_urls = [] if command_template else alive_workers(cfg.get("comfy_workers") or [])

    timeout = int(cfg.get("temporal_video_upscaler_timeout") or 7200)
    chunk_seconds = float(cfg.get("temporal_video_upscale_chunk_seconds") or 0) or None
    pool = WorkerPool(worker_urls) if worker_urls else None
    n_scenes = len(scene_finals)
    try:
        film_title = _video_title_for(wd)
    except Exception:
        film_title = wd.name
    per_scene_est, _learned = film_timing.estimate("upscale_scene", width=target_w, height=target_h)
    tmp_root = wd / "final_upscale_scenes"
    # Keyed by engine + target so a re-run reuses only work done for the SAME
    # job, and kept on disk — after success as much as after a failure: a film
    # can spend hours upscaling its scenes, and a rebuild (a join that went
    # wrong, a re-mixed score) should not cost that again.
    tmp_dir = tmp_root / f"{engine}-{target_w}x{target_h}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    upscaled_by_index: list[Path | None] = [None] * n_scenes
    # Parent stays an aggregate (fanout) while each scene shows as its own
    # sub-job below, so the Activity screen lists every busy worker instead of
    # one flickering "Upscaling scene N". _film_tasks still drives the Remix
    # poll (step stays "final_upscale"); current/total give it scene progress.
    _film_tasks[task_id] = {
        "status": "running", "step": "final_upscale", "fanout": True,
        "current": 0, "total": n_scenes,
    }

    def _reusable(out: Path, scene_path: Path) -> bool:
        """True when a previous run already produced this scene at this size
        from the scene as it is now — a re-shot or re-voiced scene of the same
        length must not pass off its old upscale."""
        from pipeline.assembler import (
            _get_duration,
            _get_video_dimensions,
            _get_video_stream_duration,
        )
        if not out.exists() or out.stat().st_size <= 10_000:
            return False
        if out.stat().st_mtime < scene_path.stat().st_mtime:
            return False
        try:
            if _get_video_dimensions(out) != (target_w, target_h):
                return False
            if abs(_get_duration(out) - _get_duration(scene_path)) > 0.2:
                return False
            # The PICTURE has to be on length too. A chunk written before the
            # source-clock fix carries full audio over a frames-short video —
            # its container duration matches, but reusing it would put the
            # rebuilt film right back out of sync. Tight on purpose: the
            # drift is cumulative, so even one missing frame (42ms at 24fps)
            # per scene adds up across a film.
            if abs(_get_video_stream_duration(out) - _get_duration(scene_path)) > 0.03:
                return False
            # A run that died mid-write can leave a blank file behind; the
            # same check the upscaler itself uses keeps it from being reused.
            _verify_upscale_not_blank(scene_path, out)
        except Exception:
            return False
        return True

    def upscale_one(index: int, scene_path: Path) -> tuple[int, Path]:
        _film_checkpoint(task_id)
        sub_id = f"film:{task_id}#s{index + 1}"
        sub_fields = dict(
            name=f"Upscaling scene {index + 1}/{n_scenes}",
            detail=f"{engine} → {target_w}×{target_h}",
            work_dir=str(wd),
            title=film_title,
            est_seconds=per_scene_est,
        )
        # In line for a worker until acquire() returns — show it that way.
        _register_film_subjob(sub_id, **sub_fields, queued=bool(pool))
        out = tmp_dir / f"{scene_path.stem}.upscaled.mp4"
        if _reusable(out, scene_path):
            gapp.logger.info("[upscale] reusing scene %d/%d from a previous run (%s)",
                             index + 1, n_scenes, out.name)
            _clear_film_subjob(sub_id)
            return index, out
        url = pool.acquire() if pool else None
        if pool:
            # On a GPU now — flip the row to running and start its ETA clock.
            _register_film_subjob(sub_id, **sub_fields)
        # Started after acquire so film_timing learns GPU time, not queue wait.
        sub_started = time.time()
        src_path = scene_path
        conformed: Path | None = None
        if factor is not None and film_dims:
            # Conform to the film's size BEFORE the factor, not the target size
            # after it: this scene is resampled to the film's size by the concat
            # regardless, and doing it first leaves the upscale itself exact.
            scene_dims = _get_video_dimensions(scene_path)
            if scene_dims != tuple(film_dims):
                conformed = tmp_dir / f"{scene_path.stem}.conformed.mp4"
                shutil.copy2(scene_path, conformed)
                ensure_video_resolution(conformed, film_dims[0], film_dims[1])
                gapp.logger.info(
                    "[upscale] scene %d is %dx%d in a %dx%d film — conformed before the %dx",
                    index + 1, *scene_dims, *film_dims, factor,
                )
                src_path = conformed
        try:
            temporal_ai_upscale_video(
                src_path,
                out,
                target_w,
                target_h,
                command_template=command_template,
                timeout_seconds=timeout,
                comfy_url=url,
                engine=engine,
                chunk_seconds=chunk_seconds,
            )
            film_timing.record(
                "upscale_scene", time.time() - sub_started,
                width=target_w, height=target_h,
            )
            return index, out
        except Exception:
            # Never leave a rejected clip behind for the next run to reuse.
            out.unlink(missing_ok=True)
            raise
        finally:
            if conformed is not None:
                conformed.unlink(missing_ok=True)
            _clear_film_subjob(sub_id)
            if pool and url:
                pool.release(url)

    if command_template:
        max_workers = min(max(1, int(cfg.get("temporal_video_upscaler_jobs") or 1)), n_scenes)
    else:
        max_workers = min(len(worker_urls), n_scenes)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(upscale_one, idx, scene_path)
                for idx, scene_path in enumerate(scene_finals)
            ]
            done = 0
            for fut in concurrent.futures.as_completed(futures):
                idx, out = fut.result()
                upscaled_by_index[idx] = out
                done += 1
                _film_tasks[task_id] = {
                    "status": "running", "step": "final_upscale", "fanout": True,
                    "current": done, "total": n_scenes,
                }
    finally:
        _clear_film_subjobs_for(task_id)

    upscaled = [p for p in upscaled_by_index if p is not None]
    if len(upscaled) != n_scenes:
        raise RuntimeError("Temporal scene upscale did not produce every scene.")

    _film_checkpoint(task_id)
    _film_tasks[task_id] = {"status": "running", "step": "finalize"}
    combined = tmp_dir / "combined.upscaled.mp4"
    # The same join the film itself was assembled with: every clip is decoded
    # and laid on one film-rate timeline. A stream copy of the clips was what
    # assembled one film's 4K version into 21 scenes of 36 collapsing into a
    # few milliseconds each — the concat demuxer takes the clips' timebases to
    # be identical, and upscaled scenes do not all come back at one rate.
    # Same dip-to-black between scenes as the render and every rebuild, too:
    # joined with fade=0 the upscaled film cut hard where the original faded,
    # and a bright-to-bright cut read as a frame flashing between the scenes.
    concatenate_scenes(upscaled, combined,
                       hard_boundaries=_film_hard_boundaries(wd, upscaled))

    jc = _film_job_config(wd)
    music_path = wd / "background_music.wav"
    ambient = wd / "ambient.wav"
    if music_path.exists():
        voice_vol, music_vol, ambient_vol = _mix_volumes(wd, jc, cfg)
        mix_background_music(
            combined,
            music_path,
            staged_final,
            volume=music_vol / 100.0,
            voice_volume=voice_vol / 100.0,
            ambient_path=ambient if ambient.exists() else None,
            ambient_volume=ambient_vol / 100.0,
        )
    else:
        shutil.copy2(combined, staged_final)
    combined.unlink(missing_ok=True)
    return staged_final


@api.post("/api/remix/upscale")
def remix_upscale_video(body: RemixUpscaleBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    target_name = (body.target_resolution or "").strip()
    try:
        mode = _normalize_upscale_mode(body.upscale_mode)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    # A factor mode finishes at the film's size times its factor, so the UI
    # hides the resolution picker and sends nothing — only the target-sized
    # modes need one chosen.
    from pipeline.assembler import parse_upscale_mode
    _engine, factor = parse_upscale_mode(mode)
    if factor is None and target_name not in gapp._UPSCALE_RESOLUTIONS:
        raise HTTPException(400, "Choose a valid upscale resolution.")
    if not gapp._final_path_for_work_dir(wd).exists():
        raise HTTPException(404, f"Final video not found for {wd.name}.")

    tid = f"final_upscale_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": "final_upscale"}
    _film_task_meta[tid] = {
        "work_dir": str(wd), "scene_id": 0, "component": "final_upscale",
        "started_at": time.time(),
    }
    threading.Thread(
        target=_run_final_video_upscale,
        args=(tid, wd, target_name, mode),
        daemon=True,
    ).start()
    return {"ok": True, "task_id": tid}


@api.get("/api/fonts")
def list_fonts(refresh: bool = Query(False)) -> dict:
    """Fonts installed on this machine, for the per-style cover-text picker."""
    from pipeline.cover import available_fonts
    return {"fonts": available_fonts(refresh=refresh), "bundled": bundled_fonts()}


def _subtitle_style_for(wd: Path) -> dict:
    """Look of this film's burned subtitles, resolved LIVE from its style (a
    Settings tweak applies to the very next burn — no re-render needed);
    falls back to the look stamped at start when the style is gone."""
    jc = _film_job_config(wd)
    cfg = gapp.load_config()
    name = jc.get("style_name") or ""
    if name and any(s.get("name") == name for s in cfg.get("styles") or []):
        return gapp._norm_subtitle_style(gapp.style_settings(cfg, name).get("subtitle_style"))
    return gapp._norm_subtitle_style(jc.get("subtitle_style"))


def _first_frame_burn_opts(wd: Path) -> dict:
    """Hold duration for this film's burns.

    Resolved LIVE from the film's style (so a Settings tweak applies to the
    very next burn, no re-render needed); style_settings falls back to the
    default style when the film's style is gone."""
    jc = _film_job_config(wd)
    ss = gapp.style_settings(gapp.load_config(), jc.get("style_name") or "")
    return {
        "seconds": gapp._norm_first_frame_cover_seconds(ss.get("first_frame_cover_seconds")),
    }


def _maybe_burn_subtitles(wd: Path, final_path: Path | str,
                          lang: str | None = None) -> None:
    """Re-burn the job's standing open captions after a final rebuild.

    Burned subtitles live only in the published final — any flow that
    regenerates it from combined.mp4 (remix, narrator/music change,
    reassemble, localized cut) would silently ship a clean picture for a
    style that burns captions into every render (job_config
    "burn_subtitles"). *lang* burns the localized track on a localized cut.
    Call BEFORE _maybe_burn_first_frame_cover so the cover overlays the text
    rather than the text scribbling over the cover.
    Best-effort: a rebuilt film without captions beats a failed rebuild."""
    try:
        if not _film_job_config(wd).get("burn_subtitles"):
            return
        from pipeline.captions import build_srt, burn_srt_into_video
        style = _subtitle_style_for(wd)
        srt = build_srt(wd, lang=lang, timing_lang=lang, style=style)
        if srt:
            burn_srt_into_video(Path(final_path), srt, style=style)
    except Exception as e:
        gapp.logger.warning("Subtitle burn re-apply failed (non-fatal): %s", e)


def _maybe_burn_first_frame_cover(wd: Path, final_path: Path | str,
                                  cover_path: Path | None = None) -> None:
    """Re-apply the job's standing first-frame cover after a final rebuild.

    The burned frame lives only in the published final — any flow that
    regenerates it from combined.mp4 (remix, narrator/music change,
    reassemble) would silently drop the cover of a style that auto-stamps
    every render (job_config "first_frame_cover"). One-off manual stamps
    (the edit-screen button) set no config key, so rebuilds stay pristine.
    *cover_path* overrides the image to stamp (a localized rebuild passes its
    localized cover); default is the film's cover.png.
    Best-effort: a rebuilt film without the stamp beats a failed rebuild."""
    try:
        mode = str(_film_job_config(wd).get("first_frame_cover") or "none").strip().lower()
        if mode not in ("image", "text"):  # legacy "text" burns the cover image too
            return
        from pipeline.cover import burn_cover_into_first_frame
        burn_cover_into_first_frame(
            Path(final_path),
            cover_path=cover_path or wd / "cover.png",
            **_first_frame_burn_opts(wd),
        )
    except Exception as e:
        gapp.logger.warning("First-frame cover re-apply failed (non-fatal): %s", e)


class FirstFrameCoverBody(BaseModel):
    work_dir: str
    mode: str = "image"  # legacy field — the cover image is the only burn now
    seconds: float | None = None  # hold; None = the style's setting


def _run_first_frame_cover(task_id: str, wd: Path, seconds=None) -> None:
    """Background thread: burn the cover image into the head of the final
    video — YouTube Shorts ignore uploaded thumbnails and pick their own
    frame. Keeps the previous cut as a selectable version."""
    from pipeline.cover import burn_cover_into_first_frame

    started = _film_task_started_at(task_id) or time.time()
    final_path = gapp._final_path_for_work_dir(wd)
    try:
        _film_checkpoint(task_id)
        if not final_path.exists() or final_path.stat().st_size <= 0:
            raise RuntimeError("Final video not found; render the film first.")
        final_video_history.seed_if_empty(wd, final_path, "Original")
        # Keep the current cut's language tag on the stamped version (same as
        # upscale) so stamping a localized cut doesn't lose its language.
        _hist = final_video_history.history(wd)
        cur_lang = next(
            (v.get("lang") for v in _hist["versions"] if v["id"] == _hist["selected"]),
            None,
        )
        _film_tasks[task_id] = {"status": "running", "step": "first_frame_cover"}
        opts = _first_frame_burn_opts(wd)
        if seconds is not None:
            opts["seconds"] = gapp._norm_first_frame_cover_seconds(seconds)
        burn_cover_into_first_frame(
            final_path,
            # A localized cut is stamped with its localized (re-titled) cover.
            cover_path=_publish_cover_path(wd),
            **opts,
        )
        final_video_history.record(wd, final_path, label="Cover on first frame",
                                   lang=cur_lang, kind="cover")
        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "video_history": final_video_history.history(wd),
        }
    except Exception as e:
        _finish_film_task_error(task_id, e)
    finally:
        _record_film_task_activity(
            task_id,
            started=started,
            done_name="Burned cover into film's first frame",
            failed_name="First-frame cover failed",
            cancelled_name="First-frame cover cancelled",
            detail=wd.name,
        )


@api.post("/api/remix/first-frame-cover")
def remix_first_frame_cover(body: FirstFrameCoverBody) -> dict:
    """Stamp the cover image onto the head of the final video (Shorts pick
    their own frame, not the uploaded thumbnail)."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    cover = wd / "cover.png"
    if not (cover.exists() and cover.stat().st_size > 1000):
        raise HTTPException(400, "No cover image found — generate the cover first.")
    if not gapp._final_path_for_work_dir(wd).exists():
        raise HTTPException(404, f"Final video not found for {wd.name}.")

    tid = f"first_frame_cover_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": "first_frame_cover"}
    _film_task_meta[tid] = {
        "work_dir": str(wd), "scene_id": 0, "component": "first_frame_cover",
        "started_at": time.time(),
    }
    threading.Thread(
        target=_run_first_frame_cover,
        args=(tid, wd, body.seconds),
        daemon=True,
    ).start()
    return {"ok": True, "task_id": tid}


def _title_cards_default_font(wd: Path) -> str:
    """The style's cover font — the cards share the film's display face
    unless the form picks another."""
    jc = _film_job_config(wd)
    ss = gapp.style_settings(gapp.load_config(), jc.get("style_name") or "")
    return str(gapp._norm_cover_typography(ss.get("cover_typography")).get("font") or "")


def _title_cards_form(wd: Path, jc: dict) -> dict:
    """The saved title-card settings for the editor; a card with no font of
    its own shows the style's cover font, which is what it would render in."""
    cards = _title_cards.norm_title_cards(jc.get("title_cards"))
    default_font = None
    for card in cards["cards"]:
        if not card["font"]:
            if default_font is None:
                default_font = _title_cards_default_font(wd)
            card["font"] = default_font
    return cards


def _title_card_images(wd: Path, jc: dict) -> dict:
    """``{card_id: url}`` for every card that has an uploaded still."""
    out = {}
    for card in _title_cards.norm_title_cards(jc.get("title_cards"))["cards"]:
        path = _title_cards.card_image_path(wd, card["id"])
        if path.exists():
            out[card["id"]] = _busted_file_url(path)
    return out


def _title_cards_head_seconds(wd: Path) -> float:
    """Opening-card length on the published cut (0 when none) — the shift
    soft caption tracks need. Best-effort: a probe failure means no shift."""
    try:
        return _title_cards.head_seconds(wd, gapp._final_path_for_work_dir(wd))
    except Exception:
        return 0.0


def _maybe_apply_title_cards(wd: Path, final_path: Path | str) -> None:
    """Re-stamp the film's standing opening title / end credits after a final
    rebuild. Every rebuild starts from combined.mp4, which never carries the
    cards, so a film that switched them on (job_config "title_cards") would
    otherwise silently lose them on a remix, re-voice or reassemble. Call
    LAST — after the subtitle and cover burns — so captions stay aligned to
    the film and the cover still opens the film itself.
    Best-effort: a rebuilt film without its titles beats a failed rebuild."""
    try:
        cfg = _title_cards.norm_title_cards(_film_job_config(wd).get("title_cards"))
        if not cfg["cards"]:
            return
        _title_cards.apply_title_cards(
            Path(final_path), wd, cfg,
            title=_video_title_for(wd), default_font=_title_cards_default_font(wd))
    except Exception as e:
        gapp.logger.warning("Title cards re-apply failed (non-fatal): %s", e)


class TitleCardsBody(BaseModel):
    work_dir: str
    title_cards: dict


class TitleCardsRemoveBody(BaseModel):
    work_dir: str


class TitleCardImageBody(BaseModel):
    work_dir: str
    card_id: str
    filename: str = ""
    data: str


def _run_title_cards(task_id: str, wd: Path, cfg: dict) -> None:
    """Background thread: prepend the opening title and append the end
    credits to the final video. Keeps the previous cut as a version."""
    started = _film_task_started_at(task_id) or time.time()
    final_path = gapp._final_path_for_work_dir(wd)
    try:
        _film_checkpoint(task_id)
        if not final_path.exists() or final_path.stat().st_size <= 0:
            raise RuntimeError("Final video not found; render the film first.")
        final_video_history.seed_if_empty(wd, final_path, "Original")
        _hist = final_video_history.history(wd)
        cur_lang = next(
            (v.get("lang") for v in _hist["versions"] if v["id"] == _hist["selected"]),
            None,
        )
        _film_tasks[task_id] = {"status": "running", "step": "title_cards"}
        rec = _title_cards.apply_title_cards(
            final_path, wd, cfg,
            title=_video_title_for(wd), default_font=_title_cards_default_font(wd))
        final_video_history.record(wd, final_path, label="Titles & credits",
                                   lang=cur_lang, kind="titles")
        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "video_history": final_video_history.history(wd),
            "title_cards_applied": True,
            "head": rec["head"], "tail": rec["tail"],
        }
    except Exception as e:
        _finish_film_task_error(task_id, e)
    finally:
        _record_film_task_activity(
            task_id,
            started=started,
            done_name="Added titles & credits to the film",
            failed_name="Titles & credits failed",
            cancelled_name="Titles & credits cancelled",
            detail=wd.name,
        )


@api.post("/api/remix/title-cards")
def remix_title_cards(body: TitleCardsBody) -> dict:
    """Add an opening title card and/or end credits to the finished film.

    The settings are persisted to the film's job_config first, so every later
    rebuild (remix, re-voice, reassemble, localize) re-stamps them. A cut
    that already has cards gets them replaced, never doubled."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if not gapp._final_path_for_work_dir(wd).exists():
        raise HTTPException(404, f"Final video not found for {wd.name}.")
    cfg = _title_cards.norm_title_cards(body.title_cards)
    if not cfg["cards"]:
        raise HTTPException(400, "Add a card first.")
    for n, card in enumerate(cfg["cards"], 1):
        if (card["background"] == "image"
                and not _title_cards.card_image_path(wd, card["id"]).exists()):
            raise HTTPException(400, f"Upload a still for card {n} first, "
                                     "or use a solid colour.")
    jc = _film_job_config(wd)
    jc["title_cards"] = cfg
    _write_film_job_config(wd, jc)

    tid = f"title_cards_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": "title_cards"}
    _film_task_meta[tid] = {
        "work_dir": str(wd), "scene_id": 0, "component": "title_cards",
        "started_at": time.time(),
    }
    threading.Thread(target=_run_title_cards, args=(tid, wd, cfg), daemon=True).start()
    return {"ok": True, "task_id": tid}


@api.post("/api/remix/title-cards/remove")
def remix_title_cards_remove(body: TitleCardsRemoveBody) -> dict:
    """Trim the stamped cards off the published cut and clear the list so
    rebuilds stop re-applying them. The titled cut stays selectable as a
    version; uploaded stills stay on disk for a later card."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    final_path = gapp._final_path_for_work_dir(wd)
    jc = _film_job_config(wd)
    cards = _title_cards.norm_title_cards(jc.get("title_cards"))
    cards["cards"] = []
    jc["title_cards"] = cards
    _write_film_job_config(wd, jc)
    trimmed = False
    with _track_op("Removing titles & credits", wd.name):
        if final_path.exists() and _title_cards.applied_title_cards(wd, final_path):
            final_video_history.seed_if_empty(wd, final_path, "Original")
            trimmed = _title_cards.strip_title_cards(final_path, wd)
            final_video_history.record(wd, final_path, label="Titles removed", kind="titles")
    return {
        "ok": True,
        "trimmed": trimmed,
        "message": "Titles & credits removed." if trimmed else "This cut has no titles to remove.",
        "final_url": _busted_file_url(final_path),
        "video_history": final_video_history.history(wd),
        "title_cards_applied": False,
    }


@api.post("/api/remix/title-cards/image")
def remix_title_card_image(body: TitleCardImageBody) -> dict:
    """Save the user's own still as a card background (cover-cropped to the
    film's frame when the card is rendered)."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    card_id = _title_cards.norm_card_id(body.card_id)
    if not card_id:
        raise HTTPException(400, "Which card is this still for?")
    raw = _decode_image(body.data)
    dest = _title_cards.card_image_path(wd, card_id)
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            im.convert("RGB").save(dest, "PNG")
    except Exception as e:
        raise HTTPException(400, f"Could not read that image: {e}")
    return {"ok": True, "card_id": card_id, "url": _busted_file_url(dest)}


class TitleCardPreviewBody(BaseModel):
    work_dir: str
    card: dict


@api.post("/api/remix/title-cards/preview")
def remix_title_card_preview(body: TitleCardPreviewBody) -> Response:
    """One card drawn by the exact code that renders the real thing, at the
    film's aspect (a small frame keeps the round-trip snappy)."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    card = _title_cards.norm_card(body.card)
    # The real card is drawn at the final's own size, so preview at its aspect
    # (the film's configured size when there is no final yet).
    fw = fh = 0
    final_path = gapp._final_path_for_work_dir(wd)
    if final_path.exists():
        try:
            from pipeline.assembler import _get_video_dimensions
            fw, fh = _get_video_dimensions(final_path)
        except Exception:
            fw = fh = 0
    if not (fw and fh):
        jc = _film_job_config(wd)
        res_name = jc.get("resolution") or gapp.load_config().get("resolution", gapp._DEFAULT_RESOLUTION)
        fw, fh = gapp._RESOLUTIONS.get(res_name, gapp._RESOLUTIONS[gapp._DEFAULT_RESOLUTION])
    w = 480
    h = max(2, round(w * fh / fw / 2) * 2)
    with tempfile.TemporaryDirectory(prefix="titlecard-preview-") as td:
        out = Path(td) / "card.png"
        _title_cards.render_card(
            out, w, h, card["text"] or _video_title_for(wd),
            background=card["background"], color=card["color"],
            image_path=_title_cards.card_image_path(wd, card["id"]),
            font=card["font"] or _title_cards_default_font(wd),
            text_color=card["text_color"], scale=card["scale"])
        return Response(content=out.read_bytes(), media_type="image/png")


@api.post("/api/remix/video-select")
def select_remix_video(body: RemixVideoSelectBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    final_path = gapp._final_path_for_work_dir(wd)
    try:
        final_video_history.select(wd, int(body.version_id), final_path)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    return {
        "ok": True,
        "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
        "video_history": final_video_history.history(wd),
    }


@api.post("/api/remix/video-delete")
def delete_remix_video(body: RemixVideoSelectBody) -> dict:
    """Delete a kept final-video version, e.g. a duplicate localization
    (the one in use can't be deleted)."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    try:
        hist = final_video_history.delete(wd, int(body.version_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "video_history": hist}


# ── queue ────────────────────────────────────────────────────────────────────

def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def _queue_title_key(item: dict) -> str:
    return _title_key(item.get("final_title") or item.get("title") or "")


def _work_dir_title_key(work_dir: Path | None) -> str:
    if not work_dir:
        return ""
    try:
        cfg = json.loads((work_dir / "job_config.json").read_text())
        title = cfg.get("video_title") or cfg.get("title")
        if title:
            return _title_key(title)
    except Exception:
        pass
    return _title_key(_video_title_for(work_dir))


def _queue_done(work_dir: str) -> tuple[bool, dict]:
    if not work_dir:
        return False, {}
    p = Path(work_dir)
    try:
        meta = json.loads((p / "job.json").read_text())
    except Exception:
        meta = {}
    final = gapp._final_path_for_work_dir(p)
    done = final.exists() and final.stat().st_size > 10_000 and (p / "combined.mp4").exists()
    return bool(done or meta.get("status") == "done"), meta


def _posted_title_map() -> dict[str, dict]:
    posted: dict[str, dict] = {}
    try:
        for item in yt.load_queue():
            if item.get("status") == "posted":
                key = _queue_title_key(item)
                if key:
                    posted[key] = item
    except Exception:
        pass
    try:
        for label, work_dir in gapp._list_recent_jobs(max_results=100):
            wd = Path(work_dir)
            try:
                meta = json.loads((wd / "job.json").read_text())
            except Exception:
                meta = {}
            if meta.get("youtube_video_id") or meta.get("youtube_url"):
                key = _work_dir_title_key(wd) or _title_key(label)
                if key:
                    posted[key] = {
                        "youtube_video_id": meta.get("youtube_video_id"),
                        "youtube_url": meta.get("youtube_url"),
                    }
    except Exception:
        pass
    return posted


def _link_queue_item_to_work_dir(item: dict, work_dir: Path) -> None:
    item["work_dir"] = str(work_dir)
    try:
        cfg_path = work_dir / "job_config.json"
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        if item.get("id") and cfg.get("queue_item_id") != item.get("id"):
            cfg["queue_item_id"] = item["id"]
            cfg_path.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


def _queue_lifecycle_sort_key(item: dict) -> tuple:
    status_rank = {
        "creating": 0, "running": 0,
        "pending": 1,
        "done": 2, "upload_pending": 2,
        "posted": 3,
        "cancelled": 4, "failed": 4, "superseded": 4,
    }
    if item.get("status") == "pending":
        return (1, 0, 0)  # preserve file order (sort is stable)
    return (status_rank.get(item.get("status"), 5), 0, -(item.get("updated_at") or item.get("created_at") or 0))


def _reconcile_queue() -> list[dict]:
    """Make queue rows reflect real render/upload state before the UI sees them."""
    try:
        queue = yt.load_queue()
    except Exception:
        return []
    active_wd = gapp._preferred_work_dir("")
    active_done = False
    active_running = False
    active_key = ""
    if active_wd:
        try:
            active_meta = json.loads((active_wd / "job.json").read_text())
        except Exception:
            active_meta = {}
        active_done, _ = _queue_done(str(active_wd))
        active_running = active_meta.get("status") == "running" and not active_done
        active_key = _work_dir_title_key(active_wd)
    posted_by_title = _posted_title_map()
    changed = False
    for it in queue:
        status = it.get("status") or "pending"
        title_key = _queue_title_key(it)
        if active_running and active_key and title_key == active_key and status in ("done", "upload_pending", "creating", "running"):
            if it.get("work_dir") != str(active_wd):
                _link_queue_item_to_work_dir(it, active_wd)
                changed = True
            if status in ("done", "upload_pending"):
                it["status"] = "creating"
                changed = True

        wd = it.get("work_dir") or ""
        jid = it.get("video_job_id") or ""
        if not wd and jid:
            try:
                store = DurableStore.default()
                try:
                    row = store.get_job(jid)
                finally:
                    store.close()
                wd = dict(row)["work_dir"] if row else ""
            except Exception:
                wd = ""
        if not wd:
            # "creating" with no work dir yet means script generation is in
            # flight — normal for a couple of minutes. If it stays that way the
            # server died mid-generation and the claim will never resolve; fail
            # it so it stops counting as a running job.
            ts = it.get("updated_at") or it.get("created_at") or 0
            if it.get("status") == "creating" and time.time() - ts > 1800:
                it["status"] = "failed"
                it["error"] = "Script generation was interrupted."
                it["updated_at"] = time.time()
                changed = True
            continue

        p = Path(wd)
        done, meta = _queue_done(wd)
        is_active = bool(active_wd and p == active_wd)
        if meta.get("youtube_video_id"):
            it["status"] = "posted"
            it["youtube_video_id"] = meta["youtube_video_id"]
            if meta.get("youtube_url"):
                it["youtube_url"] = meta["youtube_url"]
            changed = True
        elif done:
            if it.get("status") != "done":
                it["status"] = "done"
                changed = True
        elif it.get("status") in ("creating", "running"):
            posted = posted_by_title.get(title_key) if not is_active else None
            if posted:
                it["status"] = "posted"
                it["youtube_video_id"] = posted.get("youtube_video_id")
                it["youtube_url"] = posted.get("youtube_url")
                changed = True
            elif meta.get("status") == "error":
                # The render recorded a fatal error — surface it on the queue
                # item. This must apply to the active (latest) work dir too:
                # leaving the item "creating" makes _is_job_running treat it as
                # a live job and blocks automation from starting the next one.
                it["status"] = "failed"
                it["error"] = str(meta.get("error") or "Render failed.")[:300]
                it["updated_at"] = time.time()
                changed = True
            elif meta.get("status") == "cancelled":
                # The render was stopped on purpose (on_cancel_active_job stamps
                # the job "cancelled" precisely so it is NOT failed/retried).
                # Close the queue row the same way — left "creating" it showed a
                # phantom render forever.
                it["status"] = "cancelled"
                it["updated_at"] = time.time()
                changed = True
            elif (meta.get("status") == "running"
                    and not gapp._process_running(meta.get("pid"))
                    # Grace period: right around (re)launch job.json can briefly
                    # hold the previous attempt's dead pid.
                    and time.time() - (meta.get("updated_at") or 0) > 120):
                it["status"] = "failed"
                it["error"] = "Render process is no longer running."
                it["updated_at"] = time.time()
                changed = True
    queue = sorted(queue, key=_queue_lifecycle_sort_key)
    if changed:
        yt.save_queue(queue)
    return queue


def _attach_render_estimates(queue: list[dict]) -> None:
    """Stamp pending items with a rough render-duration estimate (est_seconds /
    est_text / est_confidence — response-only, never saved). Mirrors how
    _start_queue_item resolves scene count and resolution, then predicts from
    the learned per-task timing table — the same model as the live render ETA."""
    pending = [it for it in queue if it.get("status") == "pending"]
    if not pending:
        return
    try:
        from pipeline.comfyui import ltx_dimensions
        cfg = gapp.load_config()
        store = DurableStore.default()
        try:
            table = store.timing_table()
        finally:
            store.close()
    except Exception:
        return
    for it in pending:
        try:
            ss = gapp.style_settings(cfg, (it.get("gen_style_name") or "").strip())
            n = gapp.style_script_plan(ss, minutes=_queue_item_minutes(it, ss))["n_scenes"]
            res = it.get("gen_resolution") or ss.get("resolution") or gapp._DEFAULT_RESOLUTION
            # A QHD/4K target renders at its underlying render size — the ETA
            # tracks the render, so estimate at that size.
            w, h = gapp._RESOLUTIONS.get(gapp.split_render_target(res)[0],
                                         gapp._RESOLUTIONS[gapp._DEFAULT_RESOLUTION])
            w, h = ltx_dimensions(w, h)
            eta = estimate_planned_job(n, w, h, table, cfg)
        except Exception:
            continue
        if eta:
            it["est_seconds"] = eta["total_seconds"]
            it["est_text"] = eta["total_text"]
            it["est_confidence"] = eta["confidence"]


@api.get("/api/queue")
def get_queue() -> dict:
    try:
        queue = _reconcile_queue()
        _attach_render_estimates(queue)
        return {"queue": queue}
    except Exception:
        return {"queue": []}


# ── youtube ──────────────────────────────────────────────────────────────────

@api.get("/api/youtube/comments")
def youtube_comments() -> dict:
    # The exact cache-loader name varies; try the known candidates in order.
    for name in ("load_comments_cache", "load_comment_cache", "load_comments",
                 "load_evaluated_comments", "load_cache"):
        fn = getattr(yt, name, None)
        if callable(fn):
            try:
                return {"comments": fn()}
            except Exception:
                break
    return {"comments": []}


def _guided_suggestions(guidance: str, previous: list[str], cfg: dict, n: int = 6,
                        style: dict | None = None,
                        discarded: list[str] | None = None,
                        video_format: str | None = None) -> list[dict]:
    """Generate video ideas steered by a free-text theme (e.g. 'Rock bands of
    the 90s') and, optionally, a style profile with its default film format.
    ``discarded`` topics are shown as a do-not-suggest list. Uses the
    configured LLM backend via _llm_complete."""
    import re
    avoid = "; ".join(previous)
    rejected = "; ".join(discarded or [])
    system = ("You are a content strategist for a YouTube channel. "
              "Return ONLY a JSON array, no prose.")
    user = (
        f'Generate {n} specific, compelling video ideas guided by this theme: "{guidance}".\n'
        f"Each must be a concrete topic that fits both the theme and the channel style below.\n"
        + llm.style_suggestion_context(style, video_format)
        + (f"These titles already exist — use this ONLY to avoid repeats, not as a guide to subject or style: {avoid}\n" if avoid else "")
        + (f"Never suggest these previously discarded ideas or close variations: {rejected}\n" if rejected else "")
        + '\nGive each a simple, plain-language title that captures the real topic.'
        + '\nReturn a JSON array; each item: {"title": string, "reason": one-sentence string, '
        '"suggested_scene_count": integer 6-50, "interestingness": number 0..1}. Output ONLY the JSON array.'
    )
    text = _llm_complete(system, user, cfg, max_tokens=2048)
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise RuntimeError("LLM did not return a JSON array of ideas.")
    arr = json.loads(m.group())
    out = []
    for it in arr if isinstance(arr, list) else []:
        title = str(it.get("title", "")).strip()
        if not title:
            continue
        out.append({
            "title": title,
            "reason": str(it.get("reason", "")),
            "suggested_scene_count": max(6, min(50, int(it.get("suggested_scene_count", 12) or 12))),
            "interestingness": float(it.get("interestingness", 0.7) or 0.7),
            "source": "guided",
        })
    # Guarantee no verbatim repeat of an already-made/queued video slips through.
    made = {_suggestion_key(t) for t in previous}
    return [o for o in out if _suggestion_key(o["title"]) not in made]


def _suggestion_key(title: str) -> str:
    return " ".join((title or "").strip().lower().split())


def _suggestion_title(s: dict) -> str:
    return _suggestion_key(str(s.get("title") or s.get("final_title") or ""))


def _merge_suggestions(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Append freshly generated ideas after the existing ones, dropping any
    fresh idea whose title duplicates one already present (case/space
    insensitive). Lets "Generate more" grow the list instead of replacing it —
    an idea only leaves once the user closes, queues, or creates it."""
    seen = {_suggestion_title(s) for s in existing}
    out = list(existing)
    for s in fresh:
        key = _suggestion_title(s)
        if key and key not in seen:
            out.append(s)
            seen.add(key)
    return out


def _styles_sharing_channel(cfg: dict, target: str) -> set[str]:
    """Style names that publish to the same channel as ``target`` (target
    included). Lets dedup pools be pooled per channel, so two styles on one
    channel don't surface the same idea twice. Uses the EXPLICIT channel (not the
    publish fallback): a style with no channel of its own pools only with itself,
    so a channel-less style never inherits sibling styles' open ideas."""
    scope = gapp._style_channel_explicit(cfg, target)
    names = {target}
    if not scope:
        return names
    for st in (cfg.get("styles") or []):
        nm = str(st.get("name") or "").strip()
        if nm and gapp._style_channel_explicit(cfg, nm) == scope:
            names.add(nm)
    return names


def _existing_idea_titles(cfg: dict, target: str) -> list[str]:
    """Titles of the ideas already shown for the target's channel, so 'Generate
    more' can steer the LLM away from re-suggesting them (otherwise the fresh
    batch is mostly deduped away and few genuinely new ideas surface). Pooled
    across every style on the same channel so a topic open under one style isn't
    re-suggested for a sibling style of the same channel."""
    default_name = cfg.get("default_style", "")
    siblings = _styles_sharing_channel(cfg, target)
    try:
        return [str(s.get("title") or "")
                for s in _visible_suggestions(yt.load_suggestions())
                if _idea_style_key(s, default_name) in siblings and s.get("title")]
    except Exception:
        return []


def _load_dismissed_suggestions() -> dict:
    try:
        data = json.loads(DISMISSED_SUGGESTIONS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_dismissed_suggestions(data: dict) -> None:
    DISMISSED_SUGGESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DISMISSED_SUGGESTIONS_FILE.write_text(json.dumps(data, indent=2))


def _is_suggestion_dismissed(suggestion: dict, dismissed: dict) -> bool:
    sid = str(suggestion.get("id") or "").strip()
    title = _suggestion_key(str(suggestion.get("title") or suggestion.get("final_title") or ""))
    return bool((sid and sid in dismissed) or (title and title in dismissed))


def _visible_suggestions(suggestions: list[dict]) -> list[dict]:
    dismissed = _load_dismissed_suggestions()
    return [
        s for s in _normalize_suggestions(suggestions)
        if not s.get("used") and not _is_suggestion_dismissed(s, dismissed)
    ]


def _normalize_suggestions(raw: list) -> list[dict]:
    """Coerce any suggestion shape into a consistent one that always has a scene
    count, so the UI can always show it."""
    out = []
    for it in raw or []:
        if not isinstance(it, dict):
            it = {"title": str(it)}
        sc = (it.get("suggested_scene_count") or it.get("n_scenes")
              or it.get("scene_count") or it.get("scenes") or 12)
        title = str(it.get("title") or it.get("final_title") or "").strip()
        if not title:
            continue
        used = bool(it.get("used") or it.get("dismissed"))
        out.append({
            "id": str(it.get("id") or title),
            "title": title,
            "reason": str(it.get("reason") or it.get("description") or ""),
            "suggested_scene_count": max(6, min(50, int(sc or 12))),
            "interestingness": float(it.get("interestingness", it.get("interest", 0.7)) or 0.7),
            "source": it.get("source", "ai"),
            "style_name": str(it.get("style_name") or ""),
            "used": used,
            "dismissed": bool(it.get("dismissed")),
        })
    return out


def _idea_style_key(idea: dict, default_name: str) -> str:
    """Which style an idea belongs to — legacy ideas (no stamp) count as the
    default style's."""
    return str(idea.get("style_name") or "") or default_name


def _inflight_video_titles() -> list[str]:
    """Titles of videos already in the pipeline but not yet on the channel —
    queued to be made, rendering, or finished and awaiting publish. Idea
    generation must treat these as 'already covered' so it never suggests a
    topic that's already on its way."""
    titles: list[str] = []
    try:
        for q in yt.load_queue():
            if str(q.get("status") or "") == "posted":
                continue  # already published — counted via the channel title list
            t = str(q.get("final_title") or q.get("title") or "").strip()
            if t:
                titles.append(t)
    except Exception:
        pass
    try:
        for item in pq.load_queue():
            t = str(item.get("title") or "").strip()
            if t:
                titles.append(t)
    except Exception:
        pass
    return titles


def _already_made_titles(cfg: dict, target: str) -> list[str]:
    """Every title idea generation should dedup against: published on the
    channel (cached), queued/rendering/awaiting-publish, finished locally, and
    the style's still-open ideas. ``target`` is the resolved style name."""
    titles: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        t = (t or "").strip()
        k = t.lower()
        if t and k not in seen:
            titles.append(t)
            seen.add(k)

    try:
        for t in gapp._channel_video_titles(cfg, style_name=target):
            add(t)
    except Exception:
        pass
    # In-flight and finished-locally work isn't channel-stamped, so fold it in
    # only for a style that actually has a channel; a channel-less style stays a
    # clean slate and won't dedup against other styles' videos.
    if gapp._style_channel_explicit(cfg, target):
        for t in _inflight_video_titles():
            add(t)
        try:
            for label, _ in gapp._list_recent_jobs(max_results=500):
                add(label)
        except Exception:
            pass
    for t in _existing_idea_titles(cfg, target):
        add(t)
    return titles


# Idea the user silently hid ("Ignore"): kept out of every future suggestion
# like a declined one, but never shown in the reviewable "Declined" list and
# left untouched by the declined-list reset — it just disappears for good.
IGNORED_REASON = "ignored"

# Idea the user accepted: leaves the active cards and lands in the reviewable
# "Accepted" list, from where it can be queued/created (staying listed, marked
# acted-upon), moved to Declined, or removed. Kept out of future suggestions —
# it's committed to, so the LLM must not re-suggest it.
ACCEPTED_REASON = "accepted"


def _is_real_discard(reason: str) -> bool:
    """A dismissal the user made on purpose (Accept, Decline or Ignore), as
    opposed to the 'used' marker the legacy Queue/Create actions reused — those
    become videos and are tracked via the queue, not here."""
    return (reason or "").strip().lower() not in ("used", "queued", "created")


def _is_declined_reason(reason: str) -> bool:
    """A deliberately *declined* idea (the Decline action / legacy Close) — the
    'not accepted' list the user can review and reset. Distinct from an
    'ignored' idea (suppressed silently, never surfaces here) and an 'accepted'
    one (lives in its own reviewable list)."""
    r = (reason or "").strip().lower()
    return _is_real_discard(r) and r not in (IGNORED_REASON, ACCEPTED_REASON)


def _is_accepted_reason(reason: str) -> bool:
    return (reason or "").strip().lower() == ACCEPTED_REASON


def _dismissal_records(cfg: dict, target: str, keep) -> list[dict]:
    """Dismissed-idea records whose reason passes ``keep``, newest first. Rich
    data comes from the suggestions store (title/reason/scene count/style); the
    dismissed-log supplements any dismissal not represented there. ``target``
    filters to a style (legacy/unstamped records fall under the default
    style)."""
    default_name = cfg.get("default_style", "")
    by_title: dict[str, dict] = {}

    def consider(rec: dict, *, rich: bool) -> None:
        title = str(rec.get("title") or rec.get("final_title") or "").strip()
        if not title:
            return
        reason = str(rec.get("dismissed_reason") or rec.get("reason") or "dismissed")
        if not keep(reason):
            return
        key = _suggestion_key(title)
        prev = by_title.get(key)
        if prev is not None and (prev.get("_rich") or not rich):
            return  # keep the richer / first record
        sc = rec.get("suggested_scene_count") or rec.get("n_scenes") or 12
        by_title[key] = {
            "id": str(rec.get("id") or title),
            "title": title,
            "reason": str(rec.get("reason") or ""),
            "style_name": str(rec.get("style_name") or ""),
            "suggested_scene_count": max(6, min(50, int(sc or 12))),
            "interestingness": float(rec.get("interestingness", 0.7) or 0.7),
            "dismissed_at": float(rec.get("dismissed_at") or rec.get("created_at") or 0),
            "size": str(rec.get("size") or ""),
            "acted": bool(rec.get("acted")),
            "acted_via": str(rec.get("acted_via") or ""),
            "acted_at": float(rec.get("acted_at") or 0),
            "_rich": rich,
        }

    try:
        for s in yt.load_suggestions():
            if isinstance(s, dict) and s.get("dismissed"):
                consider(s, rich=True)
    except Exception:
        pass
    try:
        for rec in _load_dismissed_suggestions().values():
            if isinstance(rec, dict):
                consider(rec, rich=False)
    except Exception:
        pass

    out = [{k: v for k, v in r.items() if k != "_rich"} for r in by_title.values()]
    if target:
        out = [r for r in out if (r["style_name"] or default_name) == target]
    out.sort(key=lambda r: r.get("dismissed_at", 0), reverse=True)
    return out


def _discarded_records(cfg: dict, target: str = "",
                       include_ignored: bool = False) -> list[dict]:
    """Ideas the user deliberately turned down, newest first. Ignored (and
    accepted) ideas stay out of this (reviewable) list unless
    ``include_ignored`` is set — the LLM 'do not suggest' list sets it so no
    dismissed topic of any kind resurfaces."""
    keep = _is_real_discard if include_ignored else _is_declined_reason
    return _dismissal_records(cfg, target, keep)


def _accepted_records(cfg: dict, target: str = "") -> list[dict]:
    """Ideas the user accepted, newest first — the reviewable "Accepted" list.
    Each record carries acted/acted_via/acted_at so the UI can separate ideas
    already sent to Queue/Create from those still waiting."""
    return _dismissal_records(cfg, target, _is_accepted_reason)


def _discarded_idea_titles(cfg: dict) -> list[str]:
    """Titles the user has dealt with — passed to the LLM as a 'do not suggest
    again' list so no declined, ignored, or accepted topic resurfaces. Global (a
    handled topic stays out of every style) since the discard log isn't
    reliably style-stamped."""
    return [r["title"] for r in _discarded_records(cfg, include_ignored=True)]


def _suggestion_matches(s: dict, key: str, title_key: str) -> bool:
    sid = str(s.get("id") or "").strip()
    stitle = _suggestion_key(str(s.get("title") or s.get("final_title") or ""))
    return bool((key and sid == key) or (title_key and stitle == title_key))


# AI ideas screen sentinel: generate/show a mix of ideas across every style.
ALL_STYLES = "__all__"


def _interleave(batches: list[list[dict]]) -> list[dict]:
    """Round-robin merge of per-style batches so the result alternates styles."""
    merged = []
    for i in range(max((len(b) for b in batches), default=0)):
        for b in batches:
            if i < len(b):
                merged.append(b[i])
    return merged


def _style_idea_batch(cfg: dict, ss: dict, g: str, previous: list[str],
                      discarded: list[str] | None = None) -> list[dict]:
    """Generate + stamp a batch of ideas for one resolved style profile,
    pitched for the style's default film format."""
    fmt = _auto_format(cfg, ss["name"])
    if g:
        ideas = _guided_suggestions(g, previous, cfg, style=ss, discarded=discarded,
                                    video_format=fmt)
    else:
        ideas = _normalize_suggestions(
            generate_video_suggestions(previous, cfg, style=ss, discarded_titles=discarded,
                                       video_format=fmt))
    return [{**idea, "id": str(idea.get("id") or str(uuid.uuid4())[:8]),
             "style_name": ss["name"], "created_at": time.time(),
             "used": False, "dismissed": False}
            for idea in ideas]


def _all_styles_suggestions(cfg: dict, g: str, refresh: bool) -> dict:
    """AI ideas mixed across the auto-pick styles — the "All styles" option on
    the AI ideas screen. Without guidance/refresh, returns the union of each
    style's cached ideas; otherwise generates a fresh batch per style and
    interleaves them so the user picks from a mix. Styles opted out via
    auto_pick_exclude are dropped from the mix (it follows the same rotation as
    automation), but stay reachable by selecting that style on its own."""
    default_name = cfg.get("default_style", "")
    style_names = gapp._auto_pick_styles(cfg)   # config order, drops opted-out styles
    eligible = set(style_names)
    if not g and not refresh:
        try:
            cached = [s for s in _visible_suggestions(yt.load_suggestions())
                      if _idea_style_key(s, default_name) in eligible]
        except Exception:
            cached = []
        if cached:
            return {"suggestions": cached, "cached": True, "style_name": ALL_STYLES}

    discarded = _discarded_idea_titles(cfg)
    with _track_op("Generating suggestions", g or "all styles"):
        batches = []
        # Titles generated so far in this run, keyed by channel, so sibling
        # styles on the same channel dedup against each other's fresh batch —
        # the saved-state pools don't yet include this run's output.
        fresh_by_channel: dict[str, list[str]] = {}
        for name in style_names:
            ss = gapp.style_settings(cfg, name)
            ch = gapp._dedup_scope(cfg, name)
            try:
                previous = _already_made_titles(cfg, name)
            except Exception:
                previous = []
            previous = previous + fresh_by_channel.get(ch, [])
            try:
                batch = _style_idea_batch(cfg, ss, g, previous, discarded)
            except Exception as e:
                raise HTTPException(503, f"Could not generate suggestions: {str(e).splitlines()[0][:160]}")
            batches.append(batch)
            fresh_by_channel.setdefault(ch, []).extend(
                str(b.get("title") or "") for b in batch if b.get("title"))
    merged = _interleave(batches)

    # Append the fresh batch to the eligible styles' existing ideas so "Generate
    # more" grows the mix instead of replacing it; ideas from opted-out and
    # orphaned styles are kept untouched so nothing is silently lost.
    others, combined = [], merged
    try:
        all_cached = yt.load_suggestions()
        others = [s for s in all_cached if _idea_style_key(s, default_name) not in eligible]
        existing = [s for s in all_cached if _idea_style_key(s, default_name) in eligible]
        combined = _merge_suggestions(existing, merged)
    except Exception:
        others, combined = [], merged
    try:
        yt.save_suggestions(others + combined)
    except Exception:
        pass
    return {"suggestions": _visible_suggestions(combined), "cached": False, "style_name": ALL_STYLES}


@api.get("/api/youtube/suggestions")
def youtube_suggestions(guidance: str = Query(""), refresh: bool = Query(False),
                        style_name: str = Query("")) -> dict:
    """Return AI video ideas for a style profile (issue #66) — ideas belong to
    the style they were generated for, since a children-story channel needs
    different topics than a documentary one. Without guidance or refresh,
    returns the cached set for that style (no LLM call); only generates when
    that style's cache is empty, the user asks (refresh), or a guidance theme
    is given."""
    cfg = gapp.load_config()
    g = guidance.strip()
    if style_name == ALL_STYLES:
        return _all_styles_suggestions(cfg, g, refresh)
    ss = gapp.style_settings(cfg, style_name)
    default_name = cfg.get("default_style", "")
    target = ss["name"] or default_name

    if not g and not refresh:
        try:
            cached = yt.load_suggestions()
        except Exception:
            cached = []
        cached_for_style = [s for s in _visible_suggestions(cached)
                            if _idea_style_key(s, default_name) == target]
        if cached_for_style:
            return {"suggestions": cached_for_style, "cached": True, "style_name": target}

    with _track_op("Generating suggestions", g or target):
        try:
            # Everything already covered (published on the channel, queued/
            # rendering/awaiting-publish, finished locally, or still open as an
            # idea) plus topics the user deliberately discarded — both lists go
            # to the LLM so it neither repeats the library nor revives a
            # thrown-away idea.
            previous = _already_made_titles(cfg, target)
            discarded = _discarded_idea_titles(cfg)
        except Exception:
            previous, discarded = [], []
        try:
            fmt = _auto_format(cfg, target)
            if g:
                ideas = _guided_suggestions(g, previous, cfg, style=ss, discarded=discarded,
                                            video_format=fmt)
            else:
                ideas = _normalize_suggestions(
                    generate_video_suggestions(previous, cfg, style=ss, discarded_titles=discarded,
                                               video_format=fmt))
        except Exception as e:
            raise HTTPException(503, f"Could not generate suggestions: {str(e).splitlines()[0][:160]}")

    ideas = [{**idea, "id": str(idea.get("id") or str(uuid.uuid4())[:8]),
              "style_name": target,
              "created_at": time.time(), "used": False, "dismissed": False}
             for idea in ideas]
    # Cache per style: append the new ideas to this style's existing set (other
    # styles untouched) so "Generate more" grows the list — an idea only leaves
    # once it's closed, queued, or created. Duplicate titles are dropped.
    others, combined = [], ideas
    try:
        all_cached = yt.load_suggestions()
        others = [s for s in all_cached if _idea_style_key(s, default_name) != target]
        existing = [s for s in all_cached if _idea_style_key(s, default_name) == target]
        combined = _merge_suggestions(existing, ideas)
    except Exception:
        others, combined = [], ideas
    try:
        yt.save_suggestions(others + combined)
    except Exception:
        pass
    return {"suggestions": _visible_suggestions(combined), "cached": False, "style_name": target}


class SuggestionDismissBody(BaseModel):
    id: str = ""
    title: str = ""
    reason: str = ""
    size: str = ""   # Small/Medium/Large chosen on the card — kept on acceptance


@api.post("/api/youtube/suggestions/dismiss")
def dismiss_suggestion(body: SuggestionDismissBody) -> dict:
    """Dismiss an idea (accept / decline / ignore) — also how an idea moves
    between the Accepted and Declined lists: re-dismissing with the other
    reason updates the record and resets any acted-upon marker."""
    suggestions = yt.load_suggestions()
    key = (body.id or "").strip()
    title = _suggestion_key(body.title)
    dismissed = _load_dismissed_suggestions()
    dismiss_record = {
        "id": key,
        "title": body.title,
        "reason": body.reason or "dismissed",
        "dismissed_at": time.time(),
    }
    if body.size:
        dismiss_record["size"] = body.size
    dismiss_keys = [k for k in (key, title) if k]
    if dismiss_keys:
        for dismiss_key in dismiss_keys:
            dismissed[dismiss_key] = {
                **dismiss_record,
                "key": dismiss_key,
            }
        _save_dismissed_suggestions(dismissed)

    changed = False
    for suggestion in suggestions:
        suggestion_id = str(suggestion.get("id") or suggestion.get("title") or "")
        suggestion_title = _suggestion_key(str(suggestion.get("title") or suggestion.get("final_title") or ""))
        if (key and suggestion_id == key) or (title and suggestion_title == title):
            suggestion["used"] = True
            suggestion["dismissed"] = True
            suggestion["dismissed_reason"] = body.reason or "dismissed"
            suggestion["dismissed_at"] = time.time()
            if body.size:
                suggestion["size"] = body.size
            for f in ("acted", "acted_via", "acted_at"):
                suggestion.pop(f, None)
            changed = True
            break
    if changed:
        yt.save_suggestions(suggestions)
    return {"ok": bool(dismiss_keys), "suggestions": _visible_suggestions(suggestions)}


@api.get("/api/youtube/suggestions/accepted")
def accepted_suggestions(style_name: str = Query("")) -> dict:
    """Ideas the user accepted, with their acted-upon markers, so the list can
    show which accepted ideas haven't been sent to Queue/Create yet. Filtered
    to a style when one is given (the 'All styles' / blank selection returns
    every accepted idea)."""
    cfg = gapp.load_config()
    target = ""
    if style_name and style_name != ALL_STYLES:
        ss = gapp.style_settings(cfg, style_name)
        target = ss["name"] or cfg.get("default_style", "")
    return {"accepted": _accepted_records(cfg, target)}


class SuggestionActBody(BaseModel):
    id: str = ""
    title: str = ""
    via: str = ""   # "queue" or "create"


@api.post("/api/youtube/suggestions/accepted/act")
def act_on_accepted_suggestion(body: SuggestionActBody) -> dict:
    """Mark an accepted idea as acted upon (sent to Queue or the Create tab).
    The idea stays in the Accepted list — the marker just moves it into the
    'acted on' group so the user can see what's still waiting."""
    key = (body.id or "").strip()
    title_key = _suggestion_key(body.title)
    stamp = {"acted": True, "acted_via": body.via or "queue", "acted_at": time.time()}

    dismissed = _load_dismissed_suggestions()
    changed = False
    for k, rec in dismissed.items():
        if isinstance(rec, dict) and _suggestion_matches(
                {"id": rec.get("id"), "title": rec.get("title") or k}, key, title_key):
            rec.update(stamp)
            changed = True
    if changed:
        _save_dismissed_suggestions(dismissed)

    suggestions = yt.load_suggestions()
    changed = False
    for s in suggestions:
        if _suggestion_matches(s, key, title_key):
            s.update(stamp)
            changed = True
    if changed:
        yt.save_suggestions(suggestions)
    cfg = gapp.load_config()
    return {"ok": True, "accepted": _accepted_records(cfg)}


@api.get("/api/youtube/suggestions/discarded")
def discarded_suggestions(style_name: str = Query("")) -> dict:
    """Ideas the user deliberately discarded, so they can be reviewed, revived,
    or forgotten. Filtered to a style when one is given (the 'All styles' /
    blank selection returns every discard)."""
    cfg = gapp.load_config()
    target = ""
    if style_name and style_name != ALL_STYLES:
        ss = gapp.style_settings(cfg, style_name)
        target = ss["name"] or cfg.get("default_style", "")
    return {"discarded": _discarded_records(cfg, target)}


class SuggestionReviveBody(BaseModel):
    id: str = ""
    title: str = ""


@api.post("/api/youtube/suggestions/revive")
def revive_suggestion(body: SuggestionReviveBody) -> dict:
    """Bring a discarded idea back as an active suggestion: drop it from the
    discard log and clear its dismissed flags so it shows again and is no
    longer fed to the LLM as a rejected topic."""
    key = (body.id or "").strip()
    title_key = _suggestion_key(body.title)
    dismissed = _load_dismissed_suggestions()
    pruned = {k: v for k, v in dismissed.items()
              if not _suggestion_matches({"id": (v or {}).get("id"),
                                          "title": (v or {}).get("title") or k},
                                         key, title_key)}
    if len(pruned) != len(dismissed):
        _save_dismissed_suggestions(pruned)

    suggestions = yt.load_suggestions()
    revived = False
    for s in suggestions:
        if _suggestion_matches(s, key, title_key):
            s["used"] = False
            s["dismissed"] = False
            for f in ("dismissed_reason", "dismissed_at", "acted", "acted_via", "acted_at"):
                s.pop(f, None)
            revived = True
    if revived:
        yt.save_suggestions(suggestions)
    return {"ok": True, "suggestions": _visible_suggestions(suggestions)}


@api.post("/api/youtube/suggestions/forget")
def forget_suggestion(body: SuggestionReviveBody) -> dict:
    """Permanently forget a discarded idea — remove it from both the discard log
    and the suggestions store. It no longer appears anywhere and stops being
    fed to the LLM (so it may resurface organically in a future generation)."""
    key = (body.id or "").strip()
    title_key = _suggestion_key(body.title)
    dismissed = _load_dismissed_suggestions()
    pruned = {k: v for k, v in dismissed.items()
              if not _suggestion_matches({"id": (v or {}).get("id"),
                                          "title": (v or {}).get("title") or k},
                                         key, title_key)}
    if len(pruned) != len(dismissed):
        _save_dismissed_suggestions(pruned)

    suggestions = yt.load_suggestions()
    kept = [s for s in suggestions if not _suggestion_matches(s, key, title_key)]
    if len(kept) != len(suggestions):
        yt.save_suggestions(kept)
    cfg = gapp.load_config()
    return {"ok": True, "discarded": _discarded_records(cfg)}


@api.post("/api/youtube/suggestions/discarded/reset")
def reset_declined_suggestions() -> dict:
    """Empty the declined ('not accepted') ideas list — forget every deliberately
    declined idea so the negative list the LLM steers away from starts fresh
    (those topics may resurface organically later). Ignored ideas stay
    suppressed, and queued/created markers are left untouched."""
    dismissed = _load_dismissed_suggestions()
    pruned = {k: v for k, v in dismissed.items()
              if not _is_declined_reason((v or {}).get("reason"))}
    if len(pruned) != len(dismissed):
        _save_dismissed_suggestions(pruned)

    suggestions = yt.load_suggestions()
    kept = [s for s in suggestions
            if not (s.get("dismissed") and _is_declined_reason(s.get("dismissed_reason")))]
    if len(kept) != len(suggestions):
        yt.save_suggestions(kept)
    cfg = gapp.load_config()
    return {"ok": True, "cleared": len(suggestions) - len(kept),
            "discarded": _discarded_records(cfg)}


# ── sidebar badges ("needs attention" counts) ────────────────────────────────

SEEN_FILE = gapp.CONFIG_FILE.parent / "ui_seen.json"
DISMISSED_SUGGESTIONS_FILE = gapp.CONFIG_FILE.parent / "youtube_dismissed_suggestions.json"


def _load_seen() -> dict:
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return {}


def _save_seen(d: dict) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(d, indent=2))


def _finished_film_count() -> int:
    try:
        return len(gapp._list_recent_jobs(max_results=9999))
    except Exception:
        return 0


def _publishable_dirs() -> list[str]:
    """Finished films that haven't been posted to YouTube yet (by work_dir).
    Deleting a film drops it from this list automatically."""
    dirs = []
    try:
        for _label, wd in gapp._list_recent_jobs(max_results=9999):
            try:
                meta = json.loads((Path(wd) / "job.json").read_text())
            except Exception:
                meta = {}
            if not meta.get("youtube_video_id"):
                dirs.append(wd)
    except Exception:
        pass
    return dirs


@api.get("/api/badges")
def badges() -> dict:
    """Mailbox-style counts for the sidebar: what needs the user's attention."""
    try:
        render_active = bool(gapp._is_job_running())
    except Exception:
        render_active = False

    render_pct = 0
    if render_active:
        try:
            wd = gapp._preferred_work_dir("")
            if wd is not None:
                render_pct = _reconciled_render_pct(wd)
        except Exception:
            render_pct = 0

    try:
        queue = yt.load_queue()
    except Exception:
        queue = []
    queue_pending = sum(1 for q in queue if q.get("status") == "pending")

    try:
        # Pending video requests + drafted community replies awaiting review (issue #84).
        comment_cache = yt.load_comments_cache()
        attention = (len(yt.get_pending_requests(comment_cache))
                     + len(yt.get_pending_community_replies(comment_cache)))
    except Exception:
        attention = 0

    seen = _load_seen()
    # Publishable = finished films not yet posted, minus the ones already seen on
    # the Publish tab (mailbox-style). A deleted film leaves the list automatically.
    seen_pub = set(seen.get("publish_seen", []))
    publishable = sum(1 for wd in _publishable_dirs() if wd not in seen_pub)

    films_total = _finished_film_count()
    films_new = max(0, films_total - int(seen.get("films_total", 0)))

    # Publishing channels whose OAuth token has died but still have videos
    # waiting — drives a global "reconnect" banner so a silent token expiry can't
    # stall publishing unnoticed (guards the dead-token incident).
    yt_disconnected = []
    try:
        waiting_channels = {
            (e.get("youtube") or {}).get("channel") or ""
            for e in pq.load_queue()
            if (e.get("youtube") or {}).get("status") in ("pending", "publishing")
        }
        waiting_channels.discard("")
        if waiting_channels:
            names = {c.get("id"): c.get("name") for c in (gapp.load_config().get("youtube_channels") or [])}
            secrets = _client_secrets_path()
            for ch in waiting_channels:
                if not yt.check_auth_status(secrets, channel=ch).get("connected"):
                    yt_disconnected.append({"channel": ch, "name": names.get(ch) or ch})
    except Exception:
        pass

    # Cheap live-work count for the Activity nav badge (ops + film tasks + render).
    # A fan-out parent (parallel upscale) is hidden in favour of its sub-jobs, so
    # count the sub-jobs instead — matching what the Activity screen actually lists.
    activity_live = 0
    try:
        with _op_lock:
            activity_live += len(_current_ops) + len(_film_subjobs)
        activity_live += sum(
            1 for t in list(_film_tasks.values())
            if t.get("status") == "running" and not t.get("fanout")
        )
        if render_active:
            activity_live += 1
    except Exception:
        activity_live = 1 if render_active else 0

    return {
        "render_active": render_active,
        "render_pct": render_pct,
        "activity_live": activity_live,
        "queue": queue_pending,
        "youtube": attention + publishable,
        "youtube_attention": attention,
        "youtube_publishable": publishable,
        "youtube_disconnected": yt_disconnected,
        "films": films_new,
        "films_total": films_total,
    }


@api.get("/api/activity")
def get_activity(limit: int = 80) -> dict:
    """Everything the system is doing and recently did.

    Returns live items (API ops, film edits, durable renders with ETAs),
    durable history, and pre-built collapsible groups for the Activity screen.
    Backward-compatible fields (active_ops, recent, render_*) keep Home working.
    """
    try:
        limit_n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit_n = 80
    with _op_lock:
        _load_activity_log()
        active_ops = [dict(op) for op in _current_ops.values()]
        log = list(_activity_log[: max(10, limit_n)])

    # A _track_op row is built once, at op start, so its elapsed_s is frozen at
    # ~0 — re-derive it from started_at so a long wait or a long edit reads as
    # one. (Film tasks and sub-jobs build a fresh event per poll already.)
    _now = time.time()
    for _op in active_ops:
        try:
            _op["elapsed_s"] = round(max(0.0, _now - float(_op.get("started_at") or _now)), 1)
        except (TypeError, ValueError):
            pass

    # Film edit tasks run in daemon threads that record progress in _film_tasks
    # rather than _track_op. Surface all running ones so final upscales, music
    # regeneration, narrator changes, and concurrent scene re-renders all appear.
    for key, task in list(_film_tasks.items()):
        op = _film_task_activity_op(key, task)
        if op:
            active_ops.append(op)

    # Concurrent worker sub-jobs (each scene of a parallel upscale) — one row per
    # busy worker, each with its own ETA, instead of a single parent status.
    active_ops.extend(_film_subjob_activity_ops())

    # Durable full-film renders (with learned ETAs when available).
    render_items = _live_render_activity_items()
    # Avoid double-listing if something already tracked the same render.
    render_ids = {r.get("id") for r in render_items}
    active_ops = [op for op in active_ops if op.get("id") not in render_ids]
    active_ops.extend(render_items)

    active_ops.sort(key=lambda item: float(item.get("started_at") or 0.0), reverse=True)
    op = dict(active_ops[0]) if active_ops else {}

    render_active, render_pct, render_msg, render_title = False, 0, "", ""
    render_eta, render_work_dir = None, ""
    for r in render_items:
        render_active = True
        render_pct = int(r.get("pct") or 0)
        render_msg = r.get("detail") or ""
        render_title = r.get("title") or ""
        render_eta = r.get("eta_text")
        render_work_dir = r.get("work_dir") or ""
        break

    try:
        queue_pending = sum(1 for q in yt.load_queue() if q.get("status") == "pending")
    except Exception:
        queue_pending = 0

    # History: completed log entries only (live list is separate).
    history = [e for e in log if e.get("status") != "running"]
    # Combined stream for grouping: live first, then history (dedupe by id).
    seen_ids: set[str] = set()
    combined: list[dict] = []
    for ev in active_ops + history:
        eid = str(ev.get("id") or "")
        if eid and eid in seen_ids:
            continue
        if eid:
            seen_ids.add(eid)
        combined.append(ev)
    groups = _group_activity_events(combined)

    return {
        "current_op": op,
        "active_ops": active_ops,
        "live": active_ops,
        "recent": history[:10],
        "history": history[: limit_n],
        "groups": groups,
        "live_count": len(active_ops),
        "render_active": render_active,
        "render_pct": render_pct,
        "render_msg": render_msg,
        "render_title": render_title,
        "render_eta": render_eta,
        "render_work_dir": render_work_dir,
        "queue_pending": queue_pending,
    }


class SeenBody(BaseModel):
    section: str


@api.post("/api/badges/seen")
def mark_seen(body: SeenBody) -> dict:
    """Clear the 'new' count for a section once the user has looked at it."""
    seen = _load_seen()
    if body.section == "films":
        seen["films_total"] = _finished_film_count()
    elif body.section == "publish":
        seen["publish_seen"] = _publishable_dirs()
    _save_seen(seen)
    return {"ok": True}


# ── YouTube publishing (the Post tab) ────────────────────────────────────────

def _call_matching(fn, **available):
    """Call fn passing only the kwargs whose names match its parameters.

    Keeps the backend robust to the exact signature of the underlying pipeline
    helpers (upload_video / generate_youtube_description) — we supply every
    plausible argument name and only the matching ones are forwarded.
    """
    import inspect
    params = inspect.signature(fn).parameters
    accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in params.values())
    if accepts_kwargs:
        return fn(**available)
    return fn(**{k: v for k, v in available.items() if k in params})


def _client_secrets_path() -> str:
    p = gapp.load_config().get("youtube_client_secrets", "")
    return str(Path(p).expanduser()) if p else ""


# ── Multi-channel support (issue #22) ────────────────────────────────────────
# Channels are configured in Settings → YouTube and referenced by styles; every
# YouTube call resolves which channel's token to use from the style involved.

def _channel_for_style(style_name: str = "") -> str:
    return gapp.channel_for_style(gapp.load_config(), style_name)


def _channel_for_work_dir(wd: Path | None) -> str:
    """Channel key the work dir's video publishes to (via its style profile)."""
    return _channel_for_style(_work_dir_style_name(wd))


def _channel_key_of(item: dict) -> str:
    """Channel key a queue item's source comment lives on ('' = legacy token)."""
    return str(item.get("channel") or "")


def _category_for_channel(cfg: dict, channel_key: str) -> str:
    """Default YouTube category id for a channel: its configured video_category,
    else the global youtube_post_category, else "22" (People & Blogs)."""
    entry = next((c for c in (cfg.get("youtube_channels") or [])
                  if c.get("id") == channel_key), None)
    if entry and entry.get("video_category"):
        return str(entry["video_category"])
    return cfg.get("youtube_post_category", "22")


def _upload_prefs_for_channel(cfg: dict, channel_key: str) -> tuple[str, bool]:
    """(language, attach_captions) for a channel's uploads. Defaults: English,
    captions on — including for channels predating these fields."""
    entry = next((c for c in (cfg.get("youtube_channels") or [])
                  if c.get("id") == channel_key), None) or {}
    language = str(entry.get("language") or "").strip() or "en"
    attach_captions = bool(entry.get("upload_captions", True))
    return language, attach_captions


def _video_language_for_work_dir(wd: Path, fallback: str) -> str:
    """The language the finished video is actually narrated in: the job's
    tts_language (per-style, stamped into job_config.json at render) wins over
    the channel-level upload preference; jobs predating the stamp fall back."""
    try:
        lang = str(json.loads((wd / "job_config.json").read_text())
                   .get("tts_language") or "").strip()
        if lang:
            return lang
    except Exception:
        pass
    return fallback


@api.get("/api/youtube/channels")
def yt_channels() -> dict:
    """Configured channels with live connection status, for Settings/Publish.

    Also backfills each entry's name/channel_id once a status check resolves
    them — that is how the migrated legacy "default" entry learns who it is.
    """
    cfg = gapp.load_config()
    secrets = _client_secrets_path()
    out, dirty = [], False
    for entry in (cfg.get("youtube_channels") or []):
        st = yt.check_auth_status(secrets, channel=entry["id"])
        if st.get("connected") and st.get("channel_id"):
            if (entry.get("name") != st["channel_name"]
                    or entry.get("channel_id") != st["channel_id"]):
                entry["name"] = st["channel_name"]
                entry["channel_id"] = st["channel_id"]
                dirty = True
        out.append({**entry, "connected": bool(st.get("connected")),
                    "error": st.get("error", "")})
    if dirty:
        gapp.save_config(cfg)
    return {"channels": out, "auth_running": yt.poll_auth_flow().get("running", False)}


def _finalize_new_channel(channel_id: str, channel_name: str) -> str:
    """Auth-flow callback: record the just-authorized channel in the config and
    return the key its token is stored under.

    Reconnecting a known channel reuses its entry. If the legacy "default"
    entry hasn't resolved its identity yet, resolve it now (its own token, an
    independent file, may still work) so reconnecting that same channel updates
    the default entry instead of creating a duplicate.
    """
    cfg = gapp.load_config()
    chans = cfg.get("youtube_channels") or []
    key = channel_id or yt.DEFAULT_CHANNEL_KEY
    entry = next((c for c in chans if c.get("id") == key
                  or (channel_id and c.get("channel_id") == channel_id)), None)
    if entry is None and channel_id:
        legacy = next((c for c in chans if c.get("id") == yt.DEFAULT_CHANNEL_KEY
                       and not c.get("channel_id")), None)
        if legacy is not None:
            st = yt.check_auth_status(_client_secrets_path(), force=True,
                                      channel=yt.DEFAULT_CHANNEL_KEY)
            if st.get("channel_id"):
                legacy["channel_id"] = st["channel_id"]
                legacy["name"] = st.get("channel_name", "") or legacy.get("name", "")
            if st.get("channel_id") == channel_id:
                entry = legacy
    if entry is None:
        entry = {"id": key, "name": "", "channel_id": ""}
        chans.append(entry)
    if channel_id:
        entry["channel_id"] = channel_id
    if channel_name:
        entry["name"] = channel_name
    cfg["youtube_channels"] = chans
    gapp.save_config(cfg)
    return entry["id"]


@api.get("/api/youtube/auth")
def yt_auth_status(channel: str = Query("")) -> dict:
    try:
        return yt.check_auth_status(_client_secrets_path(), channel=channel)
    except Exception as e:
        return {"connected": False, "channel_name": "", "error": str(e)[:200]}


@api.get("/api/youtube/playlists")
def yt_playlists(channel: str = Query("")) -> dict:
    """A channel's playlists, for the per-style playlist picker in Settings."""
    try:
        return yt.list_playlists(_client_secrets_path(), channel=channel)
    except Exception as e:
        return {"playlists": [], "error": str(e)[:200]}


@api.post("/api/youtube/auth/start")
def yt_auth_start() -> dict:
    """Start the OAuth flow that connects a (new or re-connected) channel."""
    try:
        msg = yt.start_auth_flow(_client_secrets_path(), finalize=_finalize_new_channel)
        return {"ok": not msg.startswith("Error"), "message": msg}
    except Exception as e:
        raise HTTPException(503, str(e).splitlines()[0][:200])


@api.post("/api/youtube/auth/poll")
def yt_auth_poll() -> dict:
    try:
        return yt.poll_auth_flow()
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


class DisconnectBody(BaseModel):
    channel: str = ""


@api.post("/api/youtube/disconnect")
def yt_disconnect(body: DisconnectBody | None = None) -> dict:
    """Remove a channel: delete its token and drop it from the config (styles
    pointing at it fall back to the first remaining channel)."""
    channel = (body.channel if body else "") or ""
    try:
        yt.disconnect_youtube(channel)
    except Exception:
        pass
    cfg = gapp.load_config()
    chans = [c for c in (cfg.get("youtube_channels") or [])
             if c.get("id") != (channel or yt.DEFAULT_CHANNEL_KEY)]
    cfg["youtube_channels"] = chans
    gapp.save_config(cfg)  # _ensure_channels clears style refs to the removed key
    return {"ok": True, "channels": chans}


class ChannelSettingsBody(BaseModel):
    id: str
    engagement_prompt: str = ""
    auto_respond: bool = False
    video_category: str = ""
    language: str = "en"
    upload_captions: bool = True
    publish_per_day: float = 0          # publish scheduler: videos/day, spaced evenly (0 = no throttle)


@api.post("/api/youtube/channels/settings")
def yt_channel_settings(body: ChannelSettingsBody) -> dict:
    """Save a channel's per-channel settings: the default YouTube category for its
    uploads, the upload language and whether to attach a script-based caption track,
    plus the community-engagement config (issue #84) — the persona/guidance used to
    draft replies to non-request comments, and whether approved drafts post
    immediately or wait for review. Auto-saves, like connect/disconnect."""
    cfg = gapp.load_config()
    entry = next((c for c in (cfg.get("youtube_channels") or []) if c.get("id") == body.id), None)
    if entry is None:
        raise HTTPException(404, "Channel not found.")
    entry["engagement_prompt"] = body.engagement_prompt.strip()
    entry["auto_respond"] = bool(body.auto_respond)
    entry["video_category"] = body.video_category.strip()
    entry["language"] = body.language.strip() or "en"
    entry["upload_captions"] = bool(body.upload_captions)
    entry["publish_per_day"] = gapp._norm_per_day(body.publish_per_day)
    gapp.save_config(cfg)
    return {"ok": True}


@api.get("/api/youtube/analytics")
def yt_analytics(channel: str = Query(""), refresh: bool = Query(False)) -> dict:
    # Cache-first, like comments: serve the persisted per-channel snapshot
    # instantly (fast load across restarts); only hit YouTube on an explicit
    # refresh or a cold cache, then save the fresh result back to disk.
    key = channel or _channel_for_style("")
    cache = yt.load_analytics_cache()
    if not refresh and key in cache:
        return cache[key]
    try:
        data = yt.fetch_channel_analytics(_client_secrets_path(), channel=key)
    except Exception as e:
        if key in cache:
            return cache[key]   # keep the stale snapshot if a refresh fails
        return {"channel": {}, "videos": [], "error": str(e)[:200]}
    if data.get("channel"):     # only persist a real result, not a no-auth/error skeleton
        cache[key] = data
        yt.save_analytics_cache(cache)
    return data


@api.get("/api/youtube/post/options")
def yt_post_options() -> dict:
    cfg = gapp.load_config()
    return {
        "categories": getattr(yt, "CATEGORY_OPTIONS", {"People & Blogs": "22"}),
        "privacy": getattr(yt, "PRIVACY_OPTIONS", ["private", "unlisted", "public"]),
        "default_privacy": cfg.get("youtube_post_privacy", "private"),
        "default_category": cfg.get("youtube_post_category", "22"),
        "finished": [{"label": l, "work_dir": d} for l, d in gapp._list_recent_jobs(max_results=50)],
    }


def _video_title_for(wd: Path) -> str:
    title = wd.name
    try:
        store = DurableStore.default()
        try:
            job = store.get_job(job_id_from_work_dir(wd))
        finally:
            store.close()
        if job:
            d = _row_to_dict(job)
            cfg = json.loads(d.get("config_json") or "{}")
            title = cfg.get("video_title") or d.get("title") or title
    except Exception:
        pass
    return title


@api.get("/api/youtube/post/prefill")
def yt_post_prefill(work_dir: str = Query("")) -> dict:
    wd = Path(work_dir) if work_dir else gapp._latest_work_dir()
    if wd is None or not wd.exists():
        raise HTTPException(404, "No finished film found.")
    final = gapp._final_path_for_work_dir(wd)
    # Cut-aware: a selected localized cut previews its localized cover.
    cover = _publish_cover_path(wd)
    orig_cover = wd / "cover.png"
    meta = {}
    try:
        job_json = wd / "job.json"
        if job_json.exists():
            meta = json.loads(job_json.read_text())
    except Exception:
        pass
    vid_w, vid_h = _film_dimensions(wd)
    orientation = "portrait" if vid_h > vid_w else ("square" if vid_h == vid_w else "landscape")
    # Channel/account this film publishes to, resolved from its style (issue #22/#107).
    channel = _channel_for_work_dir(wd)
    x_account = _x_account_for_work_dir(wd)
    # Final versions + language tags, so Publish can offer the localized cuts.
    from pipeline.chatterbox import LANGUAGES
    jc = _film_job_config(wd)
    raw_lang = jc.get("tts_language")
    original_lang = gapp._norm_tts_language(raw_lang) if raw_lang else "en"
    try:
        video_history = final_video_history.history(wd)
    except Exception:
        video_history = {"versions": [], "selected": None}
    _title = _video_title_for(wd)
    return {
        "work_dir": str(wd),
        "title": _title,
        "final_url": _busted_file_url(final) if final.exists() and final.stat().st_size > 10_000 else "",
        "cover_url": _busted_file_url(cover) if cover.exists() and cover.stat().st_size > 1000 else "",
        # The plain cover.png, so the UI can swap back when the user selects
        # the original cut in the Version picker.
        "original_cover_url": _busted_file_url(orig_cover) if orig_cover.exists() and orig_cover.stat().st_size > 1000 else "",
        # Short text on the cover image + first-frame burn (editable per film).
        "cover_phrase": cover_phrase_for(wd, _title, _cover_typography_for(wd)["accent"]),
        "cover_phrase_default": default_cover_phrase(_title, _cover_typography_for(wd)["accent"]),
        # Whether a text-free background exists — drives "Re-apply title text"
        # (covers that predate typography need one regeneration first).
        "cover_has_bg": (wd / COVER_BASE_NAME).exists(),
        # How long a manual burn holds the cover — prefilled from the film's
        # style; the burn controls let you override it for this film.
        "first_frame_cover_seconds": _first_frame_burn_opts(wd)["seconds"],
        "description": _cached_description(wd),
        "youtube_url": meta.get("youtube_url", ""),
        "youtube_video_id": meta.get("youtube_video_id", ""),
        "channel": channel,
        # The X account this film's style posts to ('' = none → X off by default).
        "x_account": x_account,
        # The target channel's default category (its own, else the global default).
        "category": _category_for_channel(gapp.load_config(), channel),
        "orientation": orientation,
        "vid_width": vid_w,
        "vid_height": vid_h,
        # Shorts (portrait) don't take custom thumbnails — default the upload off.
        "include_thumbnail_default": orientation != "portrait",
        # Version picker: which final cuts exist and what language each speaks.
        "video_history": video_history,
        "original_lang": original_lang,
        "lang_names": LANGUAGES,
    }


class DescribeBody(BaseModel):
    work_dir: str = ""
    title: str = ""
    instruction: str = ""          # optional "tell it how" steering (title regen)


@api.post("/api/youtube/describe")
def yt_describe(body: DescribeBody) -> dict:
    with _track_op("Generating description", body.title):
        desc = _generate_and_cache_description(body.work_dir, body.title)
    return {"description": desc}


@api.post("/api/youtube/post/title")
def yt_post_title(body: DescribeBody) -> dict:
    """Regenerate the YouTube title for a finished film (issue #88), steered by the
    film's scene outline and its style's title phrasing (issue #82)."""
    wd = Path(body.work_dir) if body.work_dir else gapp._latest_work_dir()
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if wd is None or not wd.exists():
        raise HTTPException(404, "No film found.")
    cfg = gapp.load_config()
    current = (body.title or "").strip() or _video_title_for(wd)
    try:
        scenes = gapp._load_scenes_for_work_dir(wd)
    except Exception:
        scenes = []
    outline = "; ".join((s.get("title") or "").strip() for s in scenes if (s.get("title") or "").strip())
    title_style = gapp.style_settings(cfg, _work_dir_style_name(wd)).get("title_style", "")
    system = ("You write punchy, click-worthy YouTube video titles. "
              "Return ONLY the improved title — one line, no quotes, no label.")
    user = (
        f"Current title: {current or '(none)'}\n"
        f"Scene outline: {outline or '(unavailable)'}\n"
        + (f"Title phrasing style to follow: {title_style}\n" if title_style else "")
        + "\nWrite a strong YouTube title for this film. Keep it under 100 characters."
        + _instruction_note(body.instruction)
    )
    try:
        with _track_op("Regenerating title", current):
            text = _llm_complete(system, user, cfg, max_tokens=120).strip().strip('"').strip()
    except Exception as e:
        raise HTTPException(503, f"Title generation failed: {str(e).splitlines()[0][:200]}")
    return {"title": text[:100]}


class CoverSaveBody(BaseModel):
    work_dir: str = ""
    title: str = ""
    description: str = ""
    queue_item_id: str = ""


@api.post("/api/youtube/post/save")
def yt_post_save(body: CoverSaveBody) -> dict:
    """Persist the Cover tab's edited title + description so they survive a reload
    and flow into render/publish. The title goes to the durable job record (read
    back by _video_title_for, the publish prefill and load_script);
    the description is written verbatim to description.txt. A still-pending linked
    queue item keeps its title in sync so the Queue reflects the edit too."""
    wd = Path(body.work_dir) if body.work_dir else None
    if wd is not None and not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if wd is None or not wd.exists():
        raise HTTPException(404, "No script found.")
    title = (body.title or "").strip()
    if title:
        _save_video_title(wd, title, queue_item_id=body.queue_item_id)
    try:
        _description_path(wd).write_text(body.description or "")
    except Exception as e:
        raise HTTPException(500, f"Could not save description: {str(e).splitlines()[0][:200]}")
    return {"ok": True, "title": title}


def _save_video_title(wd: Path, title: str, queue_item_id: str = "") -> None:
    """Persist a film's display title everywhere reads look for it: the durable
    job record (read back by _video_title_for, the publish prefill and
    load_script), job_config.json (the Films list's batched read), and a
    still-pending linked queue item so the Queue reflects the edit too
    (issue #43)."""
    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        # Merge into the existing config/metadata — create_or_update_job
        # overwrites both on conflict, so re-pass them to avoid clobbering
        # phase/style_name/scene_count.
        d = _row_to_dict(store.get_job(job_id))
        cfg = json.loads(d.get("config_json") or "{}")
        meta = json.loads(d.get("metadata_json") or "{}")
        cfg["video_title"] = title
        store.create_or_update_job(
            job_id, wd, title, config=cfg, metadata=meta,
            status=d.get("status") or "pending",
        )
    finally:
        store.close()
    jc = _film_job_config(wd)
    jc["video_title"] = title
    _write_film_job_config(wd, jc)
    qid = (queue_item_id or "").strip() or jc.get("queue_item_id", "")
    if qid:
        item = _queue_item_by_id(qid)
        if item and item.get("status") == "pending":
            yt.update_queue_item(qid, final_title=title)


def _description_path(wd: Path) -> Path:
    return wd / "description.txt"


def _cached_description(wd: Path) -> str:
    """Return the saved description for a work dir, or empty string."""
    p = _description_path(wd)
    try:
        return p.read_text().strip() if p.exists() else ""
    except Exception:
        return ""


def _generate_and_cache_description(work_dir: str, title: str = "") -> str:
    """Generate a YouTube description, save it to description.txt, and return it."""
    desc = _generate_youtube_description(work_dir, title)
    try:
        _description_path(Path(work_dir)).write_text(desc)
    except Exception:
        pass
    return desc


def _work_dir_style_name(wd: Path | None) -> str:
    """The style profile a job belongs to: job_config.json (stamped at render
    time) → durable job config (stamped at script time) → '' (default style)."""
    if wd is None:
        return ""
    name = str(_film_job_config(wd).get("style_name") or "")
    if name:
        return name
    try:
        store = DurableStore.default()
        try:
            row = store.get_job(job_id_from_work_dir(wd))
        finally:
            store.close()
        if row:
            return json.loads(_row_to_dict(row).get("config_json") or "{}").get("style_name", "")
    except Exception:
        pass
    return ""


def _generate_youtube_description(work_dir: str = "", title: str = "") -> str:
    cfg = gapp.load_config()
    wd = Path(work_dir) if work_dir else None
    title = title or (_video_title_for(wd) if wd else "")
    scenes = []
    if wd and wd.exists():
        try:
            scenes = gapp._load_scenes_for_work_dir(wd)
        except Exception:
            scenes = []
    try:
        desc = _call_matching(
            llm.generate_youtube_description,
            title=title, video_title=title, topic=title,
            scenes=scenes, script=scenes, n_scenes=len(scenes),
            cfg=cfg, config=cfg,
        )
    except Exception as e:
        raise HTTPException(503, f"Description generation failed: {str(e).splitlines()[0][:200]}")
    if isinstance(desc, (tuple, list)):
        desc = desc[0]
    # The suffix belongs to the JOB's style profile (issue #66) — the global
    # flat key is just the default style's mirror, and appending it to every
    # video leaked e.g. the Spielbot sign-off into other styles' descriptions.
    ss = gapp.style_settings(cfg, _work_dir_style_name(wd))
    suffix = (ss.get("description_suffix") or "").strip()
    if suffix and suffix not in str(desc):
        desc = f"{desc}\n\n{suffix}"
    # Open-source attribution footer (per-style, defaults to crediting the repo).
    attribution = (ss.get("attribution_description") or "").strip()
    if attribution and attribution not in str(desc):
        desc = f"{desc}\n\n{attribution}"
    return str(desc)


def _describe_in_background(work_dir: str, title: str) -> None:
    """Daemon-thread target: write description.txt for a fresh script so the
    Cover/Publish screens find it pre-filled (no manual Generate click)."""
    try:
        with _track_op("Generating description", title):
            _generate_and_cache_description(work_dir, title)
    except Exception:
        gapp.logger.warning("Background description generation failed for %s", work_dir, exc_info=True)
    try:
        _generate_and_cache_tags(work_dir, title)
    except Exception:
        gapp.logger.warning("Background tag generation failed for %s", work_dir, exc_info=True)


# ── Keyword tags (YouTube tags field + X hashtags) ───────────────────────────
# Topic tags are LLM-generated from the script and cached in tags.json; the
# narrator/style are folded in only at upload time so they never leak into the
# X hashtags (narrator is a YouTube keyword tag only, by user preference).

def _tags_path(wd: Path) -> Path:
    return wd / "tags.json"


def _cached_tags(wd: Path) -> list[str]:
    """Saved topic tags for a work dir, or empty list."""
    p = _tags_path(wd)
    try:
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return [str(t).strip() for t in data if str(t).strip()]
    except Exception:
        pass
    return []


def _generate_youtube_tags(work_dir: str = "", title: str = "") -> list[str]:
    """LLM topic tags from the script's narrations. Best-effort — returns []."""
    cfg = gapp.load_config()
    wd = Path(work_dir) if work_dir else None
    title = title or (_video_title_for(wd) if wd else "")
    scenes = []
    if wd and wd.exists():
        try:
            scenes = gapp._load_scenes_for_work_dir(wd)
        except Exception:
            scenes = []
    try:
        tags = _call_matching(
            llm.generate_youtube_tags,
            title=title, video_title=title, topic=title,
            scenes=scenes, script=scenes, n_scenes=len(scenes),
            cfg=cfg, config=cfg,
        )
    except Exception:
        return []
    return [str(t).strip() for t in (tags or []) if str(t).strip()]


def _generate_and_cache_tags(work_dir: str, title: str = "") -> list[str]:
    """Generate topic tags and cache them in tags.json. Empty results are NOT
    cached, so a transient LLM failure is retried later rather than poisoning the
    cache with no tags (a publish still appends style/narrator regardless)."""
    tags = _generate_youtube_tags(work_dir, title)
    if tags:
        try:
            _tags_path(Path(work_dir)).write_text(json.dumps(tags))
        except Exception:
            pass
    return tags


def _dedupe_cap_tags(tags: list[str], max_count: int = 15, max_chars: int = 480) -> list[str]:
    """De-dupe (case-insensitive), drop empties, and keep within YouTube's tag
    budget (~500 chars total across all tags, commas counted)."""
    out: list[str] = []
    seen: set[str] = set()
    used = 0
    for t in tags:
        t = str(t).strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        cost = len(t) + (1 if out else 0)
        if len(out) >= max_count or used + cost > max_chars:
            continue
        seen.add(k)
        out.append(t)
        used += cost
    return out


def _youtube_tags_for(wd: Path | None, cfg: dict) -> list[str]:
    """Keyword tags for a YouTube upload: LLM topic tags (cached, generated on
    demand) followed by the style name and the narrator (the style's voice
    name). De-duped and capped. Best-effort — returns [] rather than blocking."""
    if wd is None:
        return []
    try:
        topics = _cached_tags(wd) or _generate_and_cache_tags(str(wd))
        style_name = _work_dir_style_name(wd)
        ss = gapp.style_settings(cfg, style_name)
        extra = []
        if style_name:
            extra.append(style_name)
        voice = str(ss.get("voice") or "").strip()
        if voice and voice != gapp.F5TTS_DEFAULT_OPTION:
            extra.append(voice)
        # Open-source attribution keyword tags (per-style, comma/newline list).
        attribution = [t.strip() for t in re.split(r"[,\n]+", str(ss.get("attribution_youtube_tags") or "")) if t.strip()]
        return _dedupe_cap_tags(topics + extra + attribution)
    except Exception:
        return []


def _hashtagify(phrase: str) -> str:
    """Turn a keyword phrase into one hashtag token (alnum only; PascalCase for
    multi-word phrases, single words kept as-is to preserve acronyms)."""
    words = re.findall(r"[A-Za-z0-9]+", phrase or "")
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return "".join(w[:1].upper() + w[1:] for w in words)


def _x_hashtags_for(wd: Path | None, cfg: dict, limit: int = 3) -> str:
    """Up to `limit` hashtags for an X post, from the cached topic tags. The
    narrator is intentionally excluded (it's a YouTube keyword tag only). Returns
    e.g. '#AncientRome #History' or '' — best-effort, never blocks a post."""
    if wd is None:
        return ""
    try:
        tags = _cached_tags(wd) or _generate_and_cache_tags(str(wd))
    except Exception:
        return ""
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        h = _hashtagify(t)
        if not h or len(h) > 30:
            continue
        k = h.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append("#" + h)
        if len(out) >= limit:
            break
    # Open-source attribution hashtag(s) — appended after the capped topic tags
    # so they always land (per-style, defaults to #stephenspielbot).
    ss = gapp.style_settings(cfg, _work_dir_style_name(wd))
    for tok in re.split(r"[\s,]+", str(ss.get("attribution_hashtags") or "")):
        h = _hashtagify(tok.lstrip("#"))
        if not h or h.lower() in seen:
            continue
        seen.add(h.lower())
        out.append("#" + h)
    return " ".join(out)


class CoverBody(BaseModel):
    work_dir: str = ""
    title: str = ""
    style: str = ""
    resolution: str = ""
    instruction: str = ""          # optional "tell it how" steering (cover regen)


def _best_cover_comfy_url() -> str:
    """Pick the best ComfyUI endpoint for cover generation (issue #98).

    Generating a cover means the UI is in use, so we stamp activity — the render
    holds one worker idle for us (see WorkerPool reservation) — and route to a
    free worker. During a render that idle worker is the reserved one; when the
    cluster is idle any worker works. Falls back to the least-busy worker."""
    from pipeline.worker_pool import idle_workers
    cfg = gapp.load_config()
    ui_activity.mark_active()
    comfy = cfg.get("comfy_workers") or []
    try:
        candidates = idle_workers(comfy, timeout=2)
        if candidates:
            return candidates[0]
    except Exception:
        pass
    return comfy[0] if comfy else "http://localhost:8188"


def _track_durable_task(tid: str, name: str, detail: str, poll: float = 1.5) -> None:
    """Background thread: surface a durable-store task (run by an external worker)
    in the Activity panel.

    Cover generation runs in a separate `worker_agent --kind ui` process, so its
    work can't be wrapped by _track_op inline. Instead we poll the task to a
    terminal state inside _track_op — so it shows as in-progress while the worker
    runs it and lands in the recent log when done, like every other operation."""
    terminal = {"succeeded", "failed_terminal", "cancelled"}
    deadline = time.time() + 600  # safety cap so a stuck task can't pin the marker
    try:
        with _track_op(name, detail):
            while time.time() < deadline:
                store = DurableStore.default()
                try:
                    t = store.get_task(tid)
                finally:
                    store.close()
                if t is None or t.status in terminal:
                    return
                time.sleep(poll)
    except Exception:
        pass


def _cover_style_settings(wd: Path, cfg: dict | None = None) -> dict:
    """Effective settings of the style that made this film (job_config
    style_name), resolved LIVE — so Styles-tab typography tweaks apply to
    cover regens and re-texts without re-rendering the film. Falls back to
    the film's job_config snapshot when the style no longer exists."""
    cfg = cfg or gapp.load_config()
    jc = _film_job_config(wd)
    name = str(jc.get("style_name") or "")
    if name and any(s.get("name") == name for s in cfg.get("styles") or []):
        return gapp.style_settings(cfg, name)
    return jc


def _cover_typography_for(wd: Path, cfg: dict | None = None) -> dict:
    """The film's live cover-typography settings, normalized."""
    return gapp._norm_cover_typography(_cover_style_settings(wd, cfg).get("cover_typography"))


def _render_localized_cover(wd: Path, lang: str) -> Path | None:
    """Localized cover for *lang*: the film's text-free background re-titled
    with the translated cover phrase, written to localize/{lang}/cover.png.

    Re-rendered on every call (cheap, PIL-only, no GPU) so later cover-art
    regens, typography tweaks, and phrase edits are always reflected. Returns
    None when the film has no text-free background (legacy covers) or the
    localization has no cached translation to draw — no LLM call is made here,
    the phrase was cached by _localize_metadata."""
    base = wd / COVER_BASE_NAME
    if not base.exists() or base.stat().st_size < 1000:
        return None
    try:
        data = json.loads((wd / "localize_scripts" / f"{lang}.json").read_text())
    except Exception:
        return None
    typo = _cover_typography_for(wd)
    phrase = (data.get("cover_phrase") or "").strip() \
        or default_cover_phrase((data.get("title") or "").strip(), typo["accent"])
    if not phrase:
        return None
    out = wd / "localize" / lang / "cover.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    render_cover_typography(base, out, phrase, typo)
    return out


def _publish_cover_path(wd: Path) -> Path:
    """The cover to publish for the film's currently selected final cut: a
    localized cut gets its re-titled localized cover when one can be rendered;
    everything else (the original cut, legacy covers without a text-free
    background, localizations without cached metadata) uses cover.png."""
    jc = _film_job_config(wd)
    raw = jc.get("tts_language")
    orig = gapp._norm_tts_language(raw) if raw else "en"
    lang = _published_cut_language(wd, orig)
    if lang != orig:
        try:
            loc = _render_localized_cover(wd, lang)
            if loc and loc.exists() and loc.stat().st_size > 1000:
                return loc
        except Exception as exc:
            gapp.logger.warning(
                "Localized cover for %s failed (falling back to the original): %s",
                lang, exc)
    return wd / "cover.png"


@api.post("/api/youtube/cover")
def yt_cover(body: CoverBody) -> dict:
    wd = Path(body.work_dir) if body.work_dir else gapp._latest_work_dir()
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if wd is None or not wd.exists():
        raise HTTPException(404, "No film found.")
    job_id = job_id_from_work_dir(wd)
    title = body.title or _video_title_for(wd)
    cfg = gapp.load_config()
    # Honour the resolution the UI currently shows (e.g. portrait) so the cover
    # matches it. Falls back to the rendered film's saved dimensions when the
    # caller doesn't pass one or the name is unknown.
    vid_width, vid_height = gapp._RESOLUTIONS.get(body.resolution) or _film_dimensions(wd)
    store = DurableStore.default()
    try:
        store.create_or_update_job(job_id, wd, title, status="done")
        # Text-free background + composited title: the style's own image
        # engine paints the artwork (the old FLUX.1-schnell pin existed only
        # because the model had to draw the title itself).
        typo = _cover_typography_for(wd, cfg)
        ss = _cover_style_settings(wd, cfg)
        engine = gapp.engines.resolve(cfg, ss.get("image_engine"))
        # Build the prompt HERE (not in the worker) so the cover carries the
        # film's composed visual style and its characters' reference images —
        # the same conditioning as the scene stills. Scene rows feed the
        # subject hint; the worker just executes what the payload says.
        try:
            _sdata = json.loads((wd / "script.json").read_text())
            scene_rows = _sdata if isinstance(_sdata, list) else (_sdata.get("scenes") or [])
        except Exception:
            scene_rows = []
        style_name = str(_film_job_config(wd).get("style_name") or "")
        prompt, refs = gapp.build_cover_generation(
            wd, cfg, style_name, scenes=scene_rows,
            instruction=body.instruction or "", extra_style=body.style or "",
            text_position=typo["position"], engine=engine)
        tid = make_task_id(job_id, "ui.cover.generate", int(time.time()))
        store.create_task(
            tid, job_id, "ui.cover.generate", f"Cover: {title}",
            worker_kind="ui",
            payload={
                "work_dir": str(wd),
                "title": title,
                "prompt": prompt,
                "reference_images": [str(p) for p in refs],
                # Composed style kept for workers that predate payload prompts.
                "style": gapp._compose_visual_style(body.style or "", cfg, style_name),
                "instruction": body.instruction or "",
                "cover_typography": typo,
                "vid_width": vid_width,
                "vid_height": vid_height,
                "comfy_url": _best_cover_comfy_url(),
                "engine": engine,
                # flux_* kept for back-compat with pre-engine workers.
                "flux_steps": cfg.get("flux_steps", 4),
                "flux_model": cfg.get("flux_model", "flux1-schnell-fp8.safetensors"),
                "flux_clip_t5": cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
                "flux_clip_l": cfg.get("flux_clip_l", "clip_l.safetensors"),
                "flux_vae": cfg.get("flux_vae", "ae.safetensors"),
            },
            priority=10,
            max_attempts=3,
        )
    finally:
        store.close()
    # Surface the worker-run cover task in the Activity panel as it happens.
    threading.Thread(
        target=_track_durable_task,
        args=(tid, "Generating thumbnail", title),
        daemon=True,
    ).start()
    return {"task_id": tid}


@api.get("/api/youtube/cover/status")
def yt_cover_status(task_id: str = Query(...)) -> dict:
    store = DurableStore.default()
    try:
        t = store.get_task(task_id)
    finally:
        store.close()
    if t is None:
        raise HTTPException(404, "Task not found.")
    result: dict = {"status": t.status}
    if t.status == "succeeded":
        cover = Path(t.result.get("path", "")) if t.result else None
        if cover and cover.exists() and cover.stat().st_size > 1000:
            result["cover_url"] = f"/api/file?path={cover}&t={int(time.time())}"
            result["history"] = image_history.cover_history(cover.parent)
    if t.error:
        result["error"] = t.error[:200]
    return result


def _cover_engine_and_style(wd: Path, cfg: dict, slot: str) -> tuple[dict, str]:
    """Resolve the work dir's style engine for *slot* ('image'|'edit') and its
    composed visual style, from the job's stamped style_name."""
    job_id = job_id_from_work_dir(wd)
    style_name = ""
    store = DurableStore.default()
    try:
        job_row = store.get_job(job_id)
    finally:
        store.close()
    if job_row is not None:
        try:
            style_name = json.loads(dict(job_row).get("config_json") or "{}").get("style_name", "")
        except Exception:
            style_name = ""
    key = "edit_engine" if slot == "edit" else "image_engine"
    engine = gapp.engines.resolve(cfg, gapp.style_settings(cfg, style_name).get(key))
    return engine, gapp._compose_visual_style("", cfg, style_name)


class CoverInpaintBody(BaseModel):
    work_dir: str
    mask: str
    prompt: str
    denoise: float | None = None


@api.post("/api/youtube/cover/inpaint")
def inpaint_cover(body: CoverInpaintBody) -> dict:
    """Masked edit of the cover image, using the style's edit engine; keeps a version."""
    from pipeline.comfyui import edit_with_engine
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if not wd.exists():
        raise HTTPException(404, "No work directory.")
    cover = wd / "cover.png"
    if not cover.exists():
        raise HTTPException(400, "Generate the cover first, then edit it.")
    edit = (body.prompt or "").strip()[:700]
    if not edit:
        raise HTTPException(400, "Describe the change to make.")
    mask_bytes = _decode_data_url(body.mask)
    if not mask_bytes:
        raise HTTPException(400, "No mask was provided.")
    worker_urls = gapp._preview_worker_urls()
    if not worker_urls:
        raise HTTPException(503, "No reachable workers for image editing.")

    cfg = gapp.load_config()
    engine, combined_style = _cover_engine_and_style(wd, cfg, "edit")
    prompt = f"{edit}. {combined_style}" if combined_style else edit
    dn = None if body.denoise is None else max(0.3, min(1.0, float(body.denoise)))

    image_history.cover_seed_if_empty(wd, cover)
    mask_tmp = wd / "_cover_inpaint_mask.png"
    mask_tmp.write_bytes(mask_bytes)
    # Edit the TEXT-FREE background when it exists (same dimensions as the
    # displayed cover, so the drawn mask maps 1:1), then re-composite the
    # title — inpainting can't smear or bake the typography. Covers that
    # predate typography have no background; edit the composited file as
    # before.
    bg = wd / COVER_BASE_NAME
    target = bg if bg.exists() and bg.stat().st_size > 1000 else cover
    pool = gapp.WorkerPool(worker_urls)
    url = None
    try:
        with _track_op("Editing cover", f"{engine['key']}") as op_id:
            url = _acquire_op_worker(pool, op_id)
            edit_with_engine(engine, prompt, target, mask_tmp, target, denoise=dn, comfy_url=url)
    except Exception as e:
        raise HTTPException(503, f"Cover edit failed: {str(e).splitlines()[0][:300]}")
    finally:
        if url:
            pool.release(url)
        mask_tmp.unlink(missing_ok=True)

    if target is bg:
        apply_cover_typography(wd, _cover_typography_for(wd), _video_title_for(wd))
    hist = image_history.cover_record(wd, cover)
    return {"ok": True, "cover_url": f"/api/file?path={cover}&t={int(time.time())}", "history": hist}


class CoverSelectBody(BaseModel):
    work_dir: str
    version_id: int


@api.post("/api/youtube/cover/select")
def select_cover(body: CoverSelectBody) -> dict:
    """Make a kept cover version the active cover.png."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    try:
        cover = image_history.cover_select(wd, int(body.version_id))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "cover_url": f"/api/file?path={cover}&t={int(time.time())}",
            "history": image_history.cover_history(wd)}


@api.post("/api/youtube/cover/delete")
def delete_cover_version(body: CoverSelectBody) -> dict:
    """Delete a kept cover version (the one in use can't be deleted)."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    try:
        hist = image_history.cover_delete(wd, int(body.version_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "history": hist}


@api.get("/api/youtube/cover/history")
def cover_history(work_dir: str = Query(...)) -> dict:
    return {"history": image_history.cover_history(Path(work_dir))}


class CoverPhraseBody(BaseModel):
    work_dir: str
    phrase: str = ""


def _clean_cover_phrase(text: str) -> str:
    """Normalise editor text for the cover: whitespace collapsed within each
    line, blank lines dropped, newlines kept (they force line breaks)."""
    lines = [" ".join(ln.split()) for ln in (text or "").replace("\r", "").split("\n")]
    return "\n".join(ln for ln in lines if ln)[:COVER_PHRASE_MAX_CHARS].strip()


@api.post("/api/films/cover-phrase")
def save_cover_phrase(body: CoverPhraseBody) -> dict:
    """Save the film's cover phrase — the short text the cover image prints and
    the first-frame burn stamps. Blank (or exactly the title-derived default)
    clears the override, so the phrase follows the title again. Line breaks
    are kept: each one forces a line on the cover."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")
    title = _video_title_for(wd)
    typo = _cover_typography_for(wd)
    phrase = _clean_cover_phrase(body.phrase)
    path = wd / COVER_PHRASE_FILE
    if not phrase or phrase == default_cover_phrase(title, typo["accent"]):
        path.unlink(missing_ok=True)
    else:
        path.write_text(phrase, encoding="utf-8")
    # The new phrase (incl. *accent* markup + line breaks) is re-composited
    # onto the saved text-free background right away — no image regeneration.
    # The stale-final sweep re-burns any first-frame cover. None = no background
    # yet (a cover that predates typography): the phrase applies on the next regen.
    retexted = apply_cover_typography(wd, typo, title) is not None
    # The old phrase's translations no longer apply — drop them so localized
    # covers fall back to each localization's title-derived phrase instead of
    # keeping a translation of text that's gone. Best-effort.
    scripts_dir = wd / "localize_scripts"
    for sp in (scripts_dir.glob("*.json") if scripts_dir.exists() else ()):
        try:
            data = json.loads(sp.read_text())
            if data.pop("cover_phrase", None) is not None:
                sp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception:
            pass
    return {
        "ok": True,
        "retexted": retexted,
        "cover_url": f"/api/file?path={wd / 'cover.png'}&t={int(time.time())}" if retexted else "",
        "cover_phrase": cover_phrase_for(wd, title, typo["accent"]),
        "cover_phrase_default": default_cover_phrase(title, typo["accent"]),
    }


class CoverRetextBody(BaseModel):
    work_dir: str


@api.post("/api/youtube/cover/retext")
def cover_retext(body: CoverRetextBody) -> dict:
    """Re-composite the title onto the cover's saved text-free background —
    apply phrase or Styles-tab typography changes without regenerating art."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    base = wd / COVER_BASE_NAME
    if not base.exists() or base.stat().st_size < 1000:
        raise HTTPException(400, "This cover has no text-free background yet — "
                                 "regenerate the cover once, then re-text freely.")
    if apply_cover_typography(wd, _cover_typography_for(wd), _video_title_for(wd)) is None:
        raise HTTPException(500, "Re-text failed — the background image is unreadable.")
    return {"ok": True,
            "cover_url": f"/api/file?path={wd / 'cover.png'}&t={int(time.time())}"}


class CoverTypographyPreviewBody(BaseModel):
    cover_typography: dict = {}
    text: str = ""
    orientation: str = "landscape"  # "landscape" | "portrait"


@api.post("/api/cover-typography/preview")
def cover_typography_preview(body: CoverTypographyPreviewBody) -> Response:
    """Styles-tab live preview: the draft typography rendered by the exact code
    that composites real covers, over a stand-in gradient background."""
    dims = (1080, 1920) if body.orientation == "portrait" else (1920, 1080)
    cw, ch = cover_dimensions(*dims)
    # Half resolution keeps the round-trip snappy; the layout scales linearly.
    w, h = max(2, cw // 2), max(2, ch // 2)
    typo = gapp._norm_cover_typography(body.cover_typography)
    text = _clean_cover_phrase(body.text) or "The Secret Story"
    if "*" not in text:  # plain sample: show the rule the way a new film's phrase gets it
        text = mark_accent(text, typo["accent"])
    buf = io.BytesIO()
    render_cover_typography(preview_background(w, h), buf, text, typo)
    return Response(content=buf.getvalue(), media_type="image/png")


class SubtitleStylePreviewBody(BaseModel):
    subtitle_style: dict = {}
    text: str = ""
    orientation: str = "landscape"  # "landscape" | "portrait"


@api.post("/api/subtitle-style/preview")
def subtitle_style_preview(body: SubtitleStylePreviewBody) -> Response:
    """Styles-tab live preview: one frame of the draft subtitle look, drawn by
    the same ffmpeg filter that burns real films, over a stand-in background."""
    import subprocess
    from pipeline.assembler import _FFMPEG
    from pipeline.subtitle_style import subtitles_filter

    w, h = (540, 960) if body.orientation == "portrait" else (960, 540)
    lines = [" ".join(ln.split()) for ln in (body.text or "").splitlines()]
    text = "\n".join(ln[:80] for ln in lines if ln)[:200] or "Nobody has ever filmed this."
    with tempfile.TemporaryDirectory(prefix="subpreview-") as td:
        tdp = Path(td)
        preview_background(w, h).save(tdp / "bg.png")
        (tdp / "captions.srt").write_text(
            f"1\n00:00:00,000 --> 00:00:05,000\n{text}\n", encoding="utf-8")
        vf = subtitles_filter(tdp / "captions.srt",
                              gapp._norm_subtitle_style(body.subtitle_style), tdp / "fonts")
        proc = subprocess.run(
            [_FFMPEG, "-y", "-loglevel", "error", "-i", str(tdp / "bg.png"),
             "-vf", vf, "-frames:v", "1", str(tdp / "out.png")],
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not (tdp / "out.png").exists():
            raise HTTPException(500, f"Preview render failed: {proc.stderr.strip()[-300:]}")
        return Response(content=(tdp / "out.png").read_bytes(), media_type="image/png")


class ThumbnailBody(BaseModel):
    work_dir: str
    video_id: str = ""


@api.post("/api/youtube/thumbnail")
def yt_thumbnail(body: ThumbnailBody) -> dict:
    """Push the current cover to an already-uploaded video's thumbnail (a
    localized cut pushes its localized, re-titled cover)."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")
    cover = _publish_cover_path(wd)
    if not (cover.exists() and cover.stat().st_size > 1000):
        raise HTTPException(400, "No cover image found for this film.")
    video_id = body.video_id
    if not video_id:
        try:
            video_id = json.loads((wd / "job.json").read_text()).get("youtube_video_id", "")
        except Exception:
            video_id = ""
    if not video_id:
        raise HTTPException(400, "This film hasn't been uploaded to YouTube yet.")
    with _track_op("Updating thumbnail", _video_title_for(wd)):
        result = yt.set_thumbnail(_client_secrets_path(), video_id, str(cover),
                                  channel=_channel_for_work_dir(wd))
    if not result.get("success"):
        raise HTTPException(502, result.get("error", "Thumbnail update failed."))
    return {"ok": True}


class PostBody(BaseModel):
    work_dir: str
    title: str
    description: str = ""
    category: str = "22"
    privacy: str = "private"
    include_thumbnail: bool = True
    channel: str = ""   # channel key override; empty → the film's style's channel
    auto: bool = False  # set by the auto-poster/scheduler; a manual post (False) drops the film from the publish queue on success


def _completion_reply_text(title: str, url: str) -> str:
    title = (title or "the video").strip()
    return f"Thanks again for the suggestion - {title} is ready: {url}"


def _queue_item_by_id(queue_item_id: str) -> dict | None:
    if not queue_item_id:
        return None
    try:
        return next((q for q in yt.load_queue() if q.get("id") == queue_item_id), None)
    except Exception:
        return None


def _mark_completion_reply_on_comment(comment_id: str, **updates) -> None:
    if not comment_id:
        return
    try:
        cache = yt.load_comments_cache()
        changed = False
        for comment in cache:
            if comment.get("comment_id") == comment_id:
                comment.update(updates)
                changed = True
                break
        if changed:
            yt.save_comments_cache(cache)
    except Exception:
        pass


def _post_completion_reply(queue_item_id: str, title: str, url: str) -> dict:
    """Reply to the original request comment once the uploaded video has a link."""
    if not queue_item_id or not url:
        return {"attempted": False, "reason": "missing queue item or url"}
    item = _queue_item_by_id(queue_item_id)
    if not item:
        return {"attempted": False, "reason": "queue item not found"}
    comment_id = item.get("comment_id", "")
    if not comment_id:
        return {"attempted": False, "reason": "queue item has no source comment"}
    if item.get("completion_replied"):
        return {"attempted": False, "already_replied": True}

    text = _completion_reply_text(title, url)
    # Reply on the platform + channel/account the request came from (issue #107),
    # not the upload destination.
    if item.get("source_platform") == "x":
        cid, secret = _x_client_creds()
        result = xt.reply_to_tweet(cid, secret, comment_id, text,
                                   account=_channel_key_of(item))
    else:
        result = yt.reply_to_comment(_client_secrets_path(), comment_id, text,
                                     channel=_channel_key_of(item))
    now = time.time()
    if result.get("success"):
        updates = {
            "completion_replied": True,
            "completion_reply_at": now,
            "completion_reply_url": url,
            "completion_reply_error": "",
        }
        yt.update_queue_item(queue_item_id, **updates)
        _mark_completion_reply_on_comment(comment_id, **updates)
        return {"attempted": True, "success": True}

    error = result.get("error", "unknown")[:300]
    updates = {
        "completion_reply_attempted_at": now,
        "completion_reply_url": url,
        "completion_reply_error": error,
    }
    yt.update_queue_item(queue_item_id, **updates)
    _mark_completion_reply_on_comment(comment_id, **updates)
    return {"attempted": True, "success": False, "error": error}


# In-memory store for background upload tasks {task_id -> {status, ...}}
_upload_tasks: dict = {}


def _resolve_upload_playlist(cfg: dict, wd: Path, channel: str) -> str:
    """Concrete playlist id this film should be added to, or "".

    Reads the style's youtube_playlist_id: "" → none; "__auto__" → find-or-create
    a playlist named after the style on its channel; anything else → that id.
    Best-effort — a lookup/creation failure just skips the playlist step.
    """
    style = _work_dir_style_name(wd)
    choice = gapp.playlist_for_style(cfg, style)
    if not choice:
        return ""
    if choice == "__auto__":
        try:
            res = yt.ensure_playlist(_client_secrets_path(), title=style or "Uploads",
                                     channel=channel)
            return res.get("playlist_id", "")
        except Exception as exc:
            gapp.logger.warning("Auto playlist for style %r failed: %s", style, exc)
            return ""
    return choice


def _published_cut_language(wd: Path, fallback: str) -> str:
    """Language of the currently selected final cut: a localized cut carries its
    localization's language; the original cut (or no history) falls back."""
    try:
        hist = final_video_history.history(wd)
        sel = next((v for v in hist["versions"] if v["id"] == hist.get("selected")), None)
        return (sel or {}).get("lang") or fallback
    except Exception:
        return fallback


def _publish_caption_tracks(wd: Path, fallback_lang: str) -> tuple[str | None, str, str, list[dict]]:
    """Caption tracks for publishing the currently selected final cut.

    Returns ``(main_srt, audio_lang, audio_lang_name, extra_tracks)``. The main
    SRT is worded in the published cut's spoken language and timed to its
    timeline; ``extra_tracks`` carries every OTHER language the film has (the
    original + each saved localization) timed to that SAME timeline, since all
    caption tracks overlay the one published video. ``fallback_lang`` (the
    channel's language pref) is used when the film predates language tagging."""
    from pipeline import captions as _captions
    from pipeline.chatterbox import LANGUAGES

    jc = _film_job_config(wd)
    raw = jc.get("tts_language")
    orig = gapp._norm_tts_language(raw) if raw else (fallback_lang or "en")
    cut_lang = _published_cut_language(wd, orig)
    timing = None if cut_lang == orig else cut_lang

    # Soft tracks overlay the published cut — shift them past its opening
    # title card (pipeline/title_cards.py); 0 when the cut carries none.
    offset = _title_cards_head_seconds(wd)

    def _srt(code: str) -> Path | None:
        return _captions.build_srt(
            wd, lang=None if code == orig else code, timing_lang=timing,
            offset=offset, style=_subtitle_style_for(wd),
        )

    main = _srt(cut_lang)
    scripts_dir = wd / "localize_scripts"
    langs = {orig} | ({p.stem for p in scripts_dir.glob("*.json")} if scripts_dir.exists() else set())
    extras = []
    for code in sorted((langs & set(LANGUAGES)) - {cut_lang}):
        srt = _srt(code)
        if srt:
            extras.append({"path": str(srt), "language": code, "name": LANGUAGES[code]})
    return (
        str(main) if main else None,
        cut_lang,
        LANGUAGES.get(cut_lang, cut_lang.upper()),
        extras,
    )


def _caption_tracks_for_download(wd: Path) -> list[dict]:
    """Every caption language the film can produce, timed to its published
    cut — the same tracks publishing attaches — as ``[{lang, name, url}]``."""
    main, lang, name, extras = _publish_caption_tracks(
        wd, _video_language_for_work_dir(wd, "en"))
    tracks = []
    if main:
        tracks.append({"lang": lang, "name": name, "url": ""})
    tracks += [{"lang": t["language"], "name": t["name"], "url": ""} for t in extras]
    for t in tracks:
        t["url"] = f"/api/film/captions.srt?work_dir={urllib.parse.quote(str(wd))}&lang={t['lang']}"
    return tracks


@api.get("/api/film/captions")
def film_caption_tracks(work_dir: str = Query(...)) -> dict:
    """Caption tracks available to download for a film (see captions.srt)."""
    wd = Path(work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    return {"tracks": _caption_tracks_for_download(wd)}


@api.get("/api/film/captions.srt")
def film_captions_srt(work_dir: str = Query(...), lang: str = Query("")) -> FileResponse:
    """Download the film's caption track as an SRT file with timings — worded
    in *lang* (the original narration language, or any saved localization)
    and timed to the published cut, exactly as publishing would attach it."""
    wd = Path(work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    from pipeline import captions as _captions
    orig = _video_language_for_work_dir(wd, "en")
    cut_lang = _published_cut_language(wd, orig)
    code = (lang or cut_lang).strip().lower()
    srt = _captions.build_srt(
        wd, lang=None if code == orig else code,
        timing_lang=None if cut_lang == orig else cut_lang,
        offset=_title_cards_head_seconds(wd), style=_subtitle_style_for(wd))
    if not srt:
        raise HTTPException(404, "Nothing to caption — this film has no narration, "
                                 "dialogue or lyrics on its scenes"
                                 + (f" in {code}." if code != orig else "."))
    stem = re.sub(r"[^\w-]+", "_", _video_title_for(wd)).strip("_") or wd.name
    return FileResponse(str(srt), media_type="application/x-subrip",
                        filename=f"{stem}_{code}.srt")


def _run_upload_task(task_id: str, body_dict: dict, wd: Path, final: Path, thumb) -> None:
    """Background thread: upload to YouTube, then send completion reply."""
    try:
        channel = body_dict.get("channel", "")
        language, attach_captions = _upload_prefs_for_channel(gapp.load_config(), channel)
        # The video's own narration language wins over the channel preference,
        # so a Portuguese-style film is labelled pt on YouTube (metadata +
        # audio language + caption track) even on a mostly-English channel.
        language = _video_language_for_work_dir(wd, language)
        # A localized cut ships with translated title/description, so the
        # metadata language (YouTube's "Title and description language") must
        # follow the published cut too, not just the audio language.
        language = _published_cut_language(wd, language)
        # Per-style playlist the finished video is added to (resolved here so the
        # "__auto__" find-or-create can hit the API off the render path).
        playlist_id = _resolve_upload_playlist(gapp.load_config(), wd, channel)
        # Per-style "Made for Kids" self-declaration.
        made_for_kids = gapp.made_for_kids_for_style(gapp.load_config(), _work_dir_style_name(wd))
        # Build subtitle tracks from the known scripts so YouTube shows accurate
        # captions instead of relying on speech recognition: one track in the
        # published cut's spoken language plus one per other language the film
        # has (original + localizations). Best-effort, and only when the channel
        # has captions enabled.
        caption_file, audio_lang, audio_lang_name, extra_caps = None, language, "English", []
        if attach_captions:
            try:
                caption_file, audio_lang, audio_lang_name, extra_caps = \
                    _publish_caption_tracks(wd, language)
            except Exception:
                caption_file, extra_caps = None, []
        # Keyword tags (topic tags + style + narrator); best-effort.
        yt_tags = _youtube_tags_for(wd, gapp.load_config())
        # Content Credentials (C2PA): sign the final in place before it leaves
        # the machine — the last step, since a later re-encode breaks the
        # manifest. Best-effort, never blocks the upload.
        _c2pa.sign_if_enabled(final, gapp.load_config())
        # Track around the actual upload (the slow part) so it shows as
        # in-progress in the Activity panel and lands in the recent log.
        with _track_op("Uploading to YouTube", body_dict["title"]):
            result = _call_matching(
                yt.upload_video,
                client_secrets_path=_client_secrets_path(), client_secrets=_client_secrets_path(),
                video_path=str(final), path=str(final), video=str(final), file=str(final),
                filename=str(final), video_file=str(final),
                title=body_dict["title"], description=body_dict["description"],
                category=body_dict["category"], category_id=body_dict["category"], categoryId=body_dict["category"],
                privacy=body_dict["privacy"], privacy_status=body_dict["privacy"], privacyStatus=body_dict["privacy"],
                thumbnail=thumb, thumbnail_path=thumb, thumb=thumb,
                channel=channel, tags=yt_tags, keywords=yt_tags,
                captions_path=caption_file, captions=caption_file,
                captions_name=audio_lang_name, extra_captions=extra_caps,
                default_language=language, default_audio_language=audio_lang,
                playlist_id=playlist_id,
                made_for_kids=made_for_kids,
            )
    except Exception as e:
        _upload_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:240]}
        # Release the claim — manual OR scheduled — so a later tick can retry. A
        # scheduled upload that dies on a transient/auth error must not stay
        # claimed forever; the publish-queue reconciler re-pends it from here.
        with gapp._auto_post_lock:
            gapp._auto_post_triggered.discard(str(wd))
        try:
            gapp._write_job_meta(wd, _auto_post_triggered=False)
        except Exception:
            pass
        return

    video_id, url = "", ""
    if isinstance(result, dict):
        video_id = result.get("video_id") or result.get("id") or result.get("videoId") or ""
        url = result.get("url") or result.get("video_url") or ""
    elif isinstance(result, str):
        video_id = result
    if video_id and not url:
        url = f"https://youtu.be/{video_id}"

    queue_item_id = ""
    try:
        gapp._write_job_meta(wd, youtube_video_id=video_id, youtube_url=url, status="done")
    except Exception:
        pass
    try:
        cfg_path = wd / "job_config.json"
        if cfg_path.exists():
            queue_item_id = json.loads(cfg_path.read_text()).get("queue_item_id", "")
        if queue_item_id and hasattr(yt, "update_queue_item"):
            yt.update_queue_item(queue_item_id, status="posted", youtube_video_id=video_id, youtube_url=url)
    except Exception:
        pass

    # A manual publish leaves the queue; the auto-poster/scheduler (auto=True)
    # keeps its entry so reconciliation can record it as published.
    if not body_dict.get("auto"):
        _drop_from_publish_queue(wd)

    completion_reply = _post_completion_reply(queue_item_id, body_dict["title"], url)
    try:
        gapp._write_job_meta(
            wd,
            completion_reply_attempted=bool(completion_reply.get("attempted")),
            completion_replied=bool(completion_reply.get("success") or completion_reply.get("already_replied")),
            completion_reply_error=completion_reply.get("error", ""),
        )
    except Exception:
        pass

    _upload_tasks[task_id] = {
        "status": "done",
        "video_id": video_id,
        "url": url,
        "message": f"Uploaded — {url}" if url else "Uploaded to YouTube.",
        "completion_reply": completion_reply,
    }


@api.post("/api/youtube/post")
def yt_post(body: PostBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")
    final = gapp._final_path_for_work_dir(wd)
    if not (final.exists() and final.stat().st_size > 10_000):
        raise HTTPException(400, "No final video found for this film.")
    # A localized cut publishes with its localized (re-titled) cover.
    cover = _publish_cover_path(wd)
    thumb = (str(cover) if body.include_thumbnail
             and cover.exists() and cover.stat().st_size > 1000 else None)

    task_id = uuid.uuid4().hex[:12]
    _upload_tasks[task_id] = {"status": "uploading", "work_dir": str(wd)}
    channel = body.channel or _channel_for_work_dir(wd)
    # A manual publish (auto=False) claims the job — the same claim
    # _claim_and_post_youtube uses — so the scheduler/immediate auto-poster can't
    # also post it while this upload is in flight. Released on failure below.
    if not body.auto:
        with gapp._auto_post_lock:
            gapp._auto_post_triggered.add(str(wd))
        try:
            gapp._write_job_meta(wd, _auto_post_triggered=True)
        except Exception:
            pass
    threading.Thread(
        target=_run_upload_task,
        args=(task_id, {"title": body.title, "description": body.description,
                        "category": body.category, "privacy": body.privacy,
                        "channel": channel, "auto": body.auto}, wd, final, thumb),
        daemon=True,
    ).start()
    return {"ok": True, "task_id": task_id}


@api.get("/api/youtube/post/status")
def yt_post_status(task_id: str) -> dict:
    task = _upload_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Upload task not found.")
    return {"ok": True, **task}


# ── X (Twitter) posting (issue #107) ──────────────────────────────────────────
# Mirrors the YouTube channel/auth/post routes above. Multi-account, style→account
# mapping, and a Premium-aware post that falls back to the YouTube link when a
# non-Premium account can't take a long video.

def _x_client_creds() -> tuple[str, str]:
    cfg = gapp.load_config()
    return str(cfg.get("x_client_id", "") or ""), str(cfg.get("x_client_secret", "") or "")


def _x_account_for_style(style_name: str = "") -> str:
    return gapp.x_account_for_style(gapp.load_config(), style_name)


def _x_account_for_work_dir(wd: Path | None) -> str:
    """X account key the work dir's video publishes to (via its style profile)."""
    return _x_account_for_style(_work_dir_style_name(wd))


def _youtube_url_for_work_dir(wd: Path) -> str:
    """Best-effort YouTube URL for a film, for the non-Premium X fallback: the
    job meta written after a YouTube upload, else its queue item."""
    try:
        meta = json.loads((wd / "job.json").read_text())
        if meta.get("youtube_url"):
            return str(meta["youtube_url"])
    except Exception:
        pass
    try:
        qid = json.loads((wd / "job_config.json").read_text()).get("queue_item_id", "")
        item = _queue_item_by_id(qid)
        if item and item.get("youtube_url"):
            return str(item["youtube_url"])
    except Exception:
        pass
    return ""


@api.get("/api/x/accounts")
def x_accounts() -> dict:
    """Configured X accounts with live connection status + premium, for Settings/
    Publish. Backfills name/account_id/premium once a status check resolves them."""
    cfg = gapp.load_config()
    cid, secret = str(cfg.get("x_client_id", "") or ""), str(cfg.get("x_client_secret", "") or "")
    out, dirty = [], False
    for entry in (cfg.get("x_accounts") or []):
        st = xt.check_auth_status(cid, secret, account=entry["id"])
        if st.get("connected"):
            if (entry.get("name") != st.get("account_name")
                    or entry.get("account_id") != st.get("account_id")
                    or bool(entry.get("premium")) != bool(st.get("premium"))):
                entry["name"] = st.get("account_name", entry.get("name", ""))
                entry["account_id"] = st.get("account_id", entry.get("account_id", ""))
                entry["premium"] = bool(st.get("premium"))
                dirty = True
        out.append({**entry, "connected": bool(st.get("connected")),
                    "premium": bool(st.get("premium") or entry.get("premium")),
                    "error": st.get("error", "")})
    if dirty:
        gapp.save_config(cfg)
    return {"accounts": out, "auth_running": xt.poll_auth_flow().get("running", False)}


def _finalize_new_x_account(account_id: str, username: str) -> str:
    """Auth-flow callback: record the just-authorized X account and return the key
    its token is stored under. Reconnecting a known account reuses its entry; a
    legacy "default" entry that hasn't resolved its identity gets resolved."""
    cfg = gapp.load_config()
    accts = cfg.get("x_accounts") or []
    key = account_id or xt.DEFAULT_ACCOUNT_KEY
    entry = next((a for a in accts if a.get("id") == key
                  or (account_id and a.get("account_id") == account_id)), None)
    if entry is None and account_id:
        legacy = next((a for a in accts if a.get("id") == xt.DEFAULT_ACCOUNT_KEY
                       and not a.get("account_id")), None)
        if legacy is not None:
            cid, secret = _x_client_creds()
            st = xt.check_auth_status(cid, secret, force=True, account=xt.DEFAULT_ACCOUNT_KEY)
            if st.get("account_id"):
                legacy["account_id"] = st["account_id"]
                legacy["name"] = st.get("account_name", "") or legacy.get("name", "")
            if st.get("account_id") == account_id:
                entry = legacy
    if entry is None:
        entry = {"id": key, "name": "", "account_id": ""}
        accts.append(entry)
    if account_id:
        entry["account_id"] = account_id
    if username:
        entry["name"] = username
    cfg["x_accounts"] = accts
    gapp.save_config(cfg)
    return entry["id"]


@api.get("/api/x/auth")
def x_auth_status(account: str = Query("")) -> dict:
    try:
        return xt.check_auth_status(*_x_client_creds(), account=account)
    except Exception as e:
        return {"connected": False, "account_name": "", "premium": False, "error": str(e)[:200]}


@api.post("/api/x/auth/start")
def x_auth_start() -> dict:
    """Start the OAuth2 PKCE flow that connects a (new or re-connected) X account.

    Also returns the authorize URL so the UI can offer it for an Incognito window
    (a clean session dodges X's logged-in redirect loop); the local listener
    catches the callback regardless of which browser finishes consent."""
    try:
        cid, secret = _x_client_creds()
        msg = xt.start_auth_flow(cid, secret, finalize=_finalize_new_x_account)
        return {"ok": not msg.startswith("Error"), "message": msg,
                "authorize_url": xt.poll_auth_flow().get("authorize_url", "")}
    except Exception as e:
        raise HTTPException(503, str(e).splitlines()[0][:200])


@api.post("/api/x/auth/poll")
def x_auth_poll() -> dict:
    try:
        return xt.poll_auth_flow()
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


class XImportTokensBody(BaseModel):
    access_token: str
    refresh_token: str = ""


@api.post("/api/x/auth/import")
def x_auth_import(body: XImportTokensBody) -> dict:
    """Connect an X account from pasted OAuth2 tokens (access + refresh) — skips
    the browser flow entirely. Validates against X, then registers the account."""
    cid, secret = _x_client_creds()
    if not cid:
        raise HTTPException(400, "Set the X API Client ID first (above).")
    res = xt.import_tokens(cid, secret, body.access_token, body.refresh_token,
                           finalize=_finalize_new_x_account)
    if not res.get("success"):
        raise HTTPException(400, res.get("error", "Could not import the tokens."))
    return {"ok": True, **res}


class XImportKeysBody(BaseModel):
    api_key: str
    api_secret: str
    access_token: str
    access_secret: str


@api.post("/api/x/auth/import-keys")
def x_auth_import_keys(body: XImportKeysBody) -> dict:
    """Connect an X account from OAuth 1.0a API keys (no browser, no scopes) —
    the most reliable path for a self-owned account. Uses the app's Read+Write
    permission, so it can upload media without the media.write OAuth2 scope."""
    res = xt.import_oauth1(body.api_key, body.api_secret, body.access_token,
                           body.access_secret, finalize=_finalize_new_x_account)
    if not res.get("success"):
        raise HTTPException(400, res.get("error", "Could not connect with those keys."))
    return {"ok": True, **res}


@api.post("/api/x/disconnect")
def x_disconnect(body: DisconnectBody | None = None) -> dict:
    """Remove an X account: delete its token and drop it from the config."""
    account = (body.channel if body else "") or ""
    try:
        xt.disconnect_x(account)
    except Exception:
        pass
    cfg = gapp.load_config()
    accts = [a for a in (cfg.get("x_accounts") or [])
             if a.get("id") != (account or xt.DEFAULT_ACCOUNT_KEY)]
    cfg["x_accounts"] = accts
    gapp.save_config(cfg)  # _ensure_x_accounts clears style refs to the removed key
    return {"ok": True, "accounts": accts}


class XAccountSettingsBody(BaseModel):
    id: str
    engagement_prompt: str = ""
    auto_respond: bool = False
    language: str = "en"
    publish_per_day: float = 0          # publish scheduler: videos/day, spaced evenly (0 = no throttle)


@api.post("/api/x/accounts/settings")
def x_account_settings(body: XAccountSettingsBody) -> dict:
    """Save an X account's per-account settings (community-engagement persona +
    auto-respond + language). Auto-saves, like connect/disconnect."""
    cfg = gapp.load_config()
    entry = next((a for a in (cfg.get("x_accounts") or []) if a.get("id") == body.id), None)
    if entry is None:
        raise HTTPException(404, "X account not found.")
    entry["engagement_prompt"] = body.engagement_prompt.strip()
    entry["auto_respond"] = bool(body.auto_respond)
    entry["language"] = body.language.strip() or "en"
    entry["publish_per_day"] = gapp._norm_per_day(body.publish_per_day)
    gapp.save_config(cfg)
    return {"ok": True}


class XPostBody(BaseModel):
    work_dir: str
    text: str = ""              # tweet text; defaults to the film title
    title: str = ""
    account: str = ""           # account key override; empty → the film's style's account
    auto: bool = False          # set by the auto-poster/scheduler; a manual post (False) drops the film from the publish queue on success


# In-memory store for background X post tasks {task_id -> {status, ...}}
_x_post_tasks: dict = {}

# An auto-post failure frees the job for a later retry, but only a few times so a
# permanently-rejected post (duplicate text, unsupported video) doesn't hammer
# the API every tick. Mirrors the render retry cap.
_X_AUTO_POST_MAX_ATTEMPTS = 3


def _strip_description_suffix(text: str, wd: Path, cfg: dict) -> str:
    """Drop the style's description_suffix from the end of a description, leaving
    the body (issue #107). The suffix is the boilerplate sign-off appended to
    YouTube descriptions; the X post wants only the body before it."""
    suffix = str(gapp.style_settings(cfg, _work_dir_style_name(wd)).get("description_suffix") or "").strip()
    if suffix and suffix in text:
        return text.rsplit(suffix, 1)[0].rstrip()
    return text.strip()


def _x_post_text_for(wd: Path, cfg: dict, passed: str = "", fallback: str = "") -> str:
    """X post text: the YouTube description body — everything before the style's
    suffix. Prefers an explicit (possibly edited) description, else the cached
    one; falls back to the title when there's no description."""
    raw = (passed or "").strip() or _cached_description(wd)
    return _strip_description_suffix(raw, wd, cfg) or fallback.strip()


def _x_auto_release_on_failure(wd: Path) -> None:
    """Release an auto-post claim after a failed post so a later tick retries,
    capped at _X_AUTO_POST_MAX_ATTEMPTS (after which the job stays claimed = given
    up). No-op for manual Publish posts, which don't set the claim and surface the
    error in the UI instead.

    The async worker — not _auto_post_x_done's synchronous except — is the only
    place that learns whether the post actually succeeded (x_post returns a
    task_id immediately), so the claim must be released here.
    """
    try:
        meta = json.loads((wd / "job.json").read_text())
    except Exception:
        return
    if not meta.get("_x_auto_post_triggered"):
        return  # manual post, or already released — nothing claimed to free
    attempts = int(meta.get("_x_auto_post_attempts", 0)) + 1
    try:
        if attempts < _X_AUTO_POST_MAX_ATTEMPTS:
            gapp._write_job_meta(wd, _x_auto_post_triggered=False,
                                 _x_auto_post_attempts=attempts)
        else:
            gapp._write_job_meta(wd, _x_auto_post_attempts=attempts)
    except Exception:
        pass


def _run_x_post_task(task_id: str, body_dict: dict, wd: Path, final: Path) -> None:
    """Background thread: post the film to X with Premium-aware length handling."""
    try:
        cid, secret = _x_client_creds()
        account = body_dict.get("account", "")
        cfg = gapp.load_config()
        # The X post is the description body (before the style's suffix), not the
        # title; an explicit description edited on the Publish screen wins.
        text = _x_post_text_for(wd, cfg, passed=body_dict.get("text", ""),
                                fallback=body_dict.get("title", ""))
        suffix = str(cfg.get("x_post_default_text", "") or "").strip()
        if suffix:
            text = f"{text}\n{suffix}".strip()
        # X has no tags field — append a couple of topic hashtags (no narrator).
        hashtags = _x_hashtags_for(wd, cfg)
        if hashtags:
            text = f"{text}\n\n{hashtags}".strip()
        # Premium gates long-video posting; check_auth_status reads it live.
        st = xt.check_auth_status(cid, secret, account=account)
        premium = bool(st.get("premium"))
        youtube_url = _youtube_url_for_work_dir(wd)
        # Attach the same script-based caption track the YouTube upload uses, so the
        # X video carries CC too. Reuses the film's channel language + upload_captions
        # preference (single source of truth); best-effort, like YouTube's captions.
        language, attach_captions = _upload_prefs_for_channel(cfg, _channel_for_work_dir(wd))
        # Same rule as the YouTube upload: the film's own narration language
        # (per-style tts_language stamped at render) is the fallback label;
        # _publish_caption_tracks refines it to the published cut's language.
        language = _video_language_for_work_dir(wd, language)
        caption_file, audio_lang, extra_caps = "", language, []
        if attach_captions:
            try:
                _main, audio_lang, _name, extra_caps = _publish_caption_tracks(wd, language)
                caption_file = _main or ""
            except Exception:
                caption_file, extra_caps = "", []
        # Content Credentials (C2PA): sign the final in place before it leaves
        # the machine — the last step, since a later re-encode breaks the
        # manifest. Best-effort, never blocks the post.
        _c2pa.sign_if_enabled(final, cfg)
        with _track_op("Posting to X", body_dict.get("title", "")):
            result = xt.post_video(
                cid, secret, str(final), text, account=account,
                premium=premium, youtube_url=youtube_url,
                captions_path=caption_file, language=audio_lang,
                extra_captions=extra_caps)
    except Exception as e:
        _x_post_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:240]}
        _x_auto_release_on_failure(wd)
        return

    if result.get("skipped") and not result.get("tweet_id"):
        _x_post_tasks[task_id] = {"status": "warning",
                                  "message": result.get("error") or result.get("reason")
                                  or "Not posted to X."}
        _x_auto_release_on_failure(wd)
        return
    if result.get("error"):
        _x_post_tasks[task_id] = {"status": "error", "error": result["error"][:240]}
        _x_auto_release_on_failure(wd)
        return

    tweet_id, url = result.get("tweet_id", ""), result.get("url", "")
    try:
        gapp._write_job_meta(wd, x_tweet_id=tweet_id, x_url=url)
    except Exception:
        pass
    try:
        qid = json.loads((wd / "job_config.json").read_text()).get("queue_item_id", "")
        if qid:
            yt.update_queue_item(qid, x_tweet_id=tweet_id, x_url=url)
    except Exception:
        pass

    # A manual post leaves the queue; the auto-poster/scheduler keeps its entry.
    if not body_dict.get("auto"):
        _drop_from_publish_queue(wd)

    msg = f"Posted to X — {url}" if url else "Posted to X."
    if result.get("fell_back_to_link"):
        msg = f"Video too long for X — posted the YouTube link instead: {url}"
    _x_post_tasks[task_id] = {"status": "done", "tweet_id": tweet_id, "url": url,
                              "fell_back_to_link": bool(result.get("fell_back_to_link")),
                              "message": msg}


@api.post("/api/x/post")
def x_post(body: XPostBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")
    final = gapp._final_path_for_work_dir(wd)
    if not (final.exists() and final.stat().st_size > 10_000):
        raise HTTPException(400, "No final video found for this film.")
    task_id = uuid.uuid4().hex[:12]
    _x_post_tasks[task_id] = {"status": "posting"}
    account = body.account or _x_account_for_work_dir(wd)
    # A manual post (auto=False) claims the job so the scheduler/immediate
    # auto-poster won't also post it mid-upload (mirrors _claim_and_post_x;
    # released on failure by _x_auto_release_on_failure inside the task).
    if not body.auto:
        try:
            gapp._write_job_meta(wd, _x_auto_post_triggered=True)
        except Exception:
            pass
    # Pass text + title separately so the task can derive the description body
    # (and fall back to the title only when there's no description).
    threading.Thread(
        target=_run_x_post_task,
        args=(task_id, {"text": body.text, "title": body.title, "account": account, "auto": body.auto}, wd, final),
        daemon=True,
    ).start()
    return {"ok": True, "task_id": task_id}


@api.get("/api/x/post/status")
def x_post_status(task_id: str) -> dict:
    task = _x_post_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "X post task not found.")
    return {"ok": True, **task}


# ── Publish scheduler queue API (decoupled publishing) ────────────────────────

def _publish_clock() -> dict:
    """Publishing-clock resets keyed ``(platform, key)`` — see pq.reset_clock."""
    return {tuple(k.split(":", 1)): v for k, v in pq.load_clock().items() if ":" in k}


def _seed_last_releases(q: list[dict], clock: dict, now: float,
                        count: dict | None = None) -> dict[tuple, float]:
    """Newest release timestamp per ``(platform, key)`` — the derived publishing
    clock the cadence spaces from. Releases at or before a key's clock reset are
    voided (the reset re-anchors the cadence); *count*, when given, still tallies
    every release made today, voided or not (it's display, not the clock).
    Errored releases count too: the upload attempt happened, so the slot is
    spent — otherwise a release that errors after the fact (e.g. its film
    deleted right after publishing) refunds the slot and the governor
    burst-releases the rest of the backlog. Genuine upload failures still free
    the slot for a retry: the self-heal re-pend clears released_at."""
    last: dict[tuple, float] = {}
    for e in q:
        for plat, keyf in (("youtube", "channel"), ("x", "account")):
            sub = e.get(plat) or {}
            ts = sub.get("released_at") or sub.get("published_at")
            if sub.get("status") not in ("publishing", "done", "error") or not ts:
                continue
            k = (plat, sub.get(keyf) or "")
            if count is not None and _same_local_day(ts, now):
                count[k] = count.get(k, 0) + 1
            ov = clock.get(k)
            if ov and ts <= float(ov.get("set_at") or 0):
                continue
            last[k] = max(last.get(k, 0.0), ts)
    return last


def _publish_cadence_status(cfg: dict, q: list[dict], now: float) -> tuple[dict, dict]:
    """Per channel/account cadence summary for the UI: configured videos/day and
    the derived spacing, last release time, today's release count, and the next
    time a release is allowed. Derived from the queue so it matches the governor."""
    clock = _publish_clock()
    count: dict[tuple, int] = {}
    last = _seed_last_releases(q, clock, now, count)

    def _summary(listed: str, plat: str) -> dict:
        out: dict = {}
        for c in (cfg.get(listed) or []):
            key = c.get("id") or ""
            per_day = float(c.get("publish_per_day") or 0)
            interval = round(1440 / per_day) if per_day > 0 else 0
            k = (plat, key)
            l, cnt = last.get(k, 0.0), count.get(k, 0)
            ov = clock.get(k)
            if ov and not l:    # reset pending — the clock starts at its chosen time
                nxt = max(now, float(ov.get("next_at") or 0))
            else:
                nxt = max(now, l + interval * 60) if (interval and l) else now
            out[key] = {"per_day": c.get("publish_per_day") or 0, "interval_minutes": interval,
                        "last_released": l or None, "count_today": cnt, "next_eligible": nxt,
                        "reset_pending": bool(ov and not l)}
        return out

    return _summary("youtube_channels", "youtube"), _summary("x_accounts", "x")


def _publish_entry_scores(e: dict, cfg: dict) -> tuple[float, float | None]:
    """(predicted_views, interestingness) for a publish-queue entry, cached on the
    entry after first computation so polling/ticks don't re-run the model. Returns
    predicted_views=-1.0 / interestingness=None when unavailable. Mirrors the
    Queue page's scoring: prefer the linked render-queue item (carries the exact
    gen_* inputs and the interestingness rating), else fall back to the rendered
    film's job_config."""
    if "predicted_views" in e and "interestingness" in e:
        pv = e.get("predicted_views")
        return (float(pv) if pv is not None else -1.0), e.get("interestingness")
    item = _queue_item_by_id(e.get("queue_item_id", "")) or {}
    interest = item.get("interestingness")
    if item:
        pv = _predicted_views_for_item(item, cfg)
    else:
        p = Path(e.get("work_dir", ""))
        jc = _film_job_config(p)
        try:
            r = eng.predict(jc.get("video_title") or e.get("title") or "",
                            jc.get("video_prompt") or jc.get("description") or "",
                            _is_portrait_film(p),
                            channel=_engagement_channel("", jc.get("gen_style_name") or ""))
            pv = float(r.get("predicted_views") or 0) if r.get("available") else -1.0
        except Exception:
            pv = -1.0
    pq.update_item(e["id"], predicted_views=pv, interestingness=interest)
    e["predicted_views"], e["interestingness"] = pv, interest
    return float(pv if pv is not None else -1.0), interest


def _ordered_publish_queue(cfg: dict, q: list[dict]) -> list[dict]:
    """Publish-queue entries in release order — the publish_sort_order chosen on
    the Publishing page. "queue" (default) and unknown values keep the manual
    file order; sorts are stable so ties fall back to it. The scheduler consumes
    this same order, so the top waiting entry for each channel is genuinely next."""
    mode = cfg.get("publish_sort_order") or "queue"
    items = list(q)
    if mode in ("interest", "views"):
        for e in items:
            _publish_entry_scores(e, cfg)
    if mode == "newest":
        items.sort(key=lambda e: -(e.get("created_at") or 0))
    elif mode == "oldest":
        items.sort(key=lambda e: e.get("created_at") or 0)
    elif mode == "interest":
        items.sort(key=lambda e: -(e["interestingness"] if e.get("interestingness") is not None else -1.0))
    elif mode == "views":
        items.sort(key=lambda e: -(e["predicted_views"] if e.get("predicted_views") is not None else -1.0))
    return items


def _publish_entry_active(e: dict) -> bool:
    """True while an entry still belongs on the Publishing queue view — a
    release waiting or in flight. Everything terminal (published, skipped,
    errored out) is history, served by /api/publish/history."""
    return any((e.get(p) or {}).get("status") in ("pending", "publishing")
               for p in ("youtube", "x"))


@api.get("/api/publish/queue")
def publish_queue_list() -> dict:
    cfg = gapp.load_config()
    _reconcile_publish_queue()
    q = pq.load_queue()
    now = time.time()
    # Cadence summaries need the WHOLE queue (done history seeds the clocks),
    # but the view — and the per-entry scoring below — only pays for the
    # entries still in play; published/skipped history has its own endpoint.
    chans, accts = _publish_cadence_status(cfg, q, now)
    skip_comment = bool(cfg.get("publish_schedule_skip_comment_requests", True))
    ordered = _ordered_publish_queue(cfg, [e for e in q if _publish_entry_active(e)])
    for e in ordered:          # ensure interest/views chips render for every entry
        _publish_entry_scores(e, cfg)
        # Surface the approval hold so the UI can mark it instead of showing it as
        # a normal queued item. Mirrors _awaiting_approval: comment requests that
        # bypass the schedule also bypass approval.
        e["awaiting_approval"] = bool(
            cfg.get("publish_require_approval")
            and not cfg.get("publish_auto_publish_unapproved")
            and not (skip_comment and e.get("source") == "comment")
            and not e.get("approved"))
    # Project a concrete release time per waiting target: the j-th still-waiting
    # entry for a channel/account posts j cadence-steps after that key's next
    # eligible time. Comment requests that bypass the schedule go on the next
    # tick. Tag the single earliest entry as the next to publish. These fields are
    # response-only (added to the in-memory copy, never saved).
    cad = {("youtube", k): v for k, v in chans.items()}
    cad.update({("x", k): v for k, v in accts.items()})
    counters: dict = {}
    best: tuple | None = None  # (projected_at, entry)
    for e in ordered:
        if e.get("awaiting_approval"):
            continue  # held for approval — no cadence slot, no projected time, not "next up"
        bypass = skip_comment and e.get("source") == "comment"
        for plat, keyf in (("youtube", "channel"), ("x", "account")):
            sub = e.get(plat) or {}
            if sub.get("status") != "pending":
                continue
            k = (plat, sub.get(keyf) or "")
            info = cad.get(k) or {}
            iv = int(info.get("interval_minutes") or 0)
            base = info.get("next_eligible") or now
            j = counters.get(k, 0)
            counters[k] = j + 1
            proj = now if bypass else (base + j * iv * 60)
            sub["projected_at"] = proj
            sub["position"] = j + 1
            if best is None or proj < best[0]:
                best = (proj, e)
    if best is not None:
        best[1]["is_next"] = True
    published_total = sum(1 for e in q if any(
        (e.get(p) or {}).get("status") == "done" for p in ("youtube", "x")))
    return {"items": ordered, "channels": chans, "accounts": accts,
            "enabled": bool(cfg.get("publish_schedule_enabled")),
            "skip_comment": skip_comment,
            "sort": cfg.get("publish_sort_order") or "queue",
            "published_total": published_total,
            "now": now}


@api.get("/api/publish/history")
def publish_history_list() -> dict:
    """Terminal publish-queue entries — the published (and skipped or errored)
    record, newest first. Split out of /api/publish/queue so the queue view
    stays light; the Publishing page shows these on its own Published tab."""
    _reconcile_publish_queue()
    hist = [e for e in pq.load_queue() if not _publish_entry_active(e)]

    def _ts(e: dict) -> float:
        subs = (e.get("youtube") or {}, e.get("x") or {})
        return max([s.get("published_at") or s.get("released_at") or 0 for s in subs]
                   + [e.get("updated_at") or e.get("created_at") or 0])

    hist.sort(key=_ts, reverse=True)
    return {"items": hist, "now": time.time()}


@api.get("/api/publish/clock")
def publish_clock_status() -> dict:
    """Lightweight per-channel/account cadence clocks for the Settings cards —
    the /api/publish/queue summaries without reconciling or scoring the queue."""
    cfg = gapp.load_config()
    now = time.time()
    chans, accts = _publish_cadence_status(cfg, pq.load_queue(), now)
    return {"channels": chans, "accounts": accts, "now": now}


class PublishClockBody(BaseModel):
    platform: str    # "youtube" | "x"
    key: str         # channel/account id
    next_at: float = 0   # epoch seconds the next release is allowed; 0 = right away


@api.post("/api/publish/clock")
def publish_clock_reset(body: PublishClockBody) -> dict:
    """Re-anchor a channel/account's publishing clock. Releases made before the
    reset stop counting against the cadence; the next one is allowed at *next_at*
    (or immediately) and later ones space from whenever it actually goes out.
    Setting the same time on a YouTube channel and an X account syncs them."""
    listed = {"youtube": "youtube_channels", "x": "x_accounts"}.get(body.platform)
    if not listed:
        raise HTTPException(400, "platform must be 'youtube' or 'x'.")
    cfg = gapp.load_config()
    if not any(c.get("id") == body.key for c in (cfg.get(listed) or [])):
        raise HTTPException(404, "Channel/account not found.")
    now = time.time()
    pq.reset_clock(body.platform, body.key, next_at=float(body.next_at) or now)
    chans, accts = _publish_cadence_status(cfg, pq.load_queue(), now)
    return {"ok": True, "channels": chans, "accounts": accts, "now": now}


@api.post("/api/publish/scan")
def publish_scan() -> dict:
    """Import the whole backlog of finished-but-unpublished videos (ignores the
    recency window the automation tick uses). Entries the user removed stay out."""
    return {"ok": True, "added": _enqueue_finished_for_publish(recent_only=False)}


class PublishItemBody(BaseModel):
    id: str
    platform: str = ""   # "youtube" | "x" | "" (the whole entry)


class PublishMoveBody(BaseModel):
    id: str
    direction: int = -1   # -1 = up (sooner), 1 = down (later)


@api.post("/api/publish/remove")
def publish_remove(body: PublishItemBody) -> dict:
    """Drop an entry (or just one platform target) from the publish queue. The
    work dir is remembered so a later scan won't re-add it."""
    if body.platform in ("youtube", "x"):
        e = next((x for x in pq.load_queue() if x.get("id") == body.id), None)
        if e is None:
            raise HTTPException(404, "Publish entry not found.")
        sub = e.get(body.platform) or {}
        sub.update(enabled=False, status="skipped")
        pq.update_item(body.id, **{body.platform: sub})
        return {"ok": True}
    if not pq.remove_item(body.id):
        raise HTTPException(404, "Publish entry not found.")
    return {"ok": True}


@api.post("/api/publish/now")
def publish_now(body: PublishItemBody) -> dict:
    """Release one entry immediately, ignoring its channel/account cadence."""
    return {"ok": True, "released": _release_scheduled_publishes(force_id=body.id)}


@api.post("/api/publish/move")
def publish_move(body: PublishMoveBody) -> dict:
    """Reorder a waiting entry by hand (only meaningful in 'queue'/manual sort)."""
    return {"ok": pq.move_item(body.id, body.direction)}


class PublishApproveBody(BaseModel):
    work_dir: str
    approved: bool = True


@api.post("/api/publish/approve")
def publish_approve(body: PublishApproveBody) -> dict:
    """Approve (or un-approve) a finished film for publishing — the Films-tab gate
    for publish_require_approval. Ensures the film has a publish-queue entry
    (creating a held one if the automation tick hasn't enqueued it yet), then
    flags it. Approved entries then release on the normal schedule/cadence."""
    p = Path(body.work_dir)
    if not _safe_under(p, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    e = _ensure_publish_entry(p)
    if e is None:
        raise HTTPException(404, "Nothing to publish for this film.")
    pq.update_item(e["id"], approved=bool(body.approved),
                   approved_at=(time.time() if body.approved else None))
    return {"ok": True, "approved": bool(body.approved)}


@api.get("/api/x/analytics")
def x_analytics(account: str = Query(""), refresh: bool = Query(False)) -> dict:
    # Cache-first, mirroring yt_analytics: serve the persisted per-account
    # snapshot instantly; only hit X on an explicit refresh or a cold cache.
    key = account or _x_account_for_style("")
    cache = xt.load_analytics_cache()
    if not refresh and key in cache:
        return cache[key]
    cid, secret = _x_client_creds()
    try:
        data = xt.fetch_x_analytics(cid, secret, account=key)
    except Exception as e:
        if key in cache:
            return cache[key]
        return {"channel": {}, "videos": [], "error": str(e)[:200]}
    if data.get("channel"):
        cache[key] = data
        xt.save_analytics_cache(cache)
    return data


# ── YouTube comment actions (fetch / evaluate / approve / reject / reply) ─────
# Mirrors app.py's _yt_fetch_new_comments + _yt_evaluate_unevaluated + on_yt_approve,
# reusing the pipeline.youtube primitives so behaviour matches the classic app.

_AUTO_APPROVE_THRESHOLD = 0.7


def _fetch_and_evaluate(auto_approve: bool) -> dict:
    cfg = gapp.load_config()
    secrets = cfg.get("youtube_client_secrets", "")
    # Sweep every connected channel (issue #22); each cached comment is stamped
    # with the channel it came from so replies go out as that channel.
    channels = [c.get("id", "") for c in (cfg.get("youtube_channels") or [])] or [""]
    new_count = 0
    errors: list[str] = []
    fetched_any = False
    cache = yt.load_comments_cache()
    by_id = {c.get("comment_id"): c for c in cache}
    for ch in channels:
        try:
            fetched = yt.fetch_channel_comments(secrets, channel=ch)
            fetched_any = True
        except Exception as e:
            errors.append(f"{ch or 'default'}: {str(e).splitlines()[0][:120]}")
            # Quota is per project, shared by every channel — once one sweep hits
            # quotaExceeded the rest are doomed too, so stop the round here.
            if yt.note_quota_error(e):
                break
            continue
        for fc in fetched:
            cur = by_id.get(fc.get("comment_id"))
            if cur is None:
                entry = {**fc, "channel": ch, "evaluated": False, "is_request": False,
                         "suggested_title": "", "confidence": 0.0,
                         "interestingness": 0.0, "reason": "", "status": "new",
                         "engagement_status": "", "engagement_draft": "",
                         "engagement_reason": "", "engagement_anchor": ""}
                cache.insert(0, entry)
                by_id[fc.get("comment_id")] = entry
                new_count += 1
            else:
                # Refresh thread state so replies that arrived after the first fetch —
                # including a viewer replying to our reply — are captured. The
                # engagement pass below then re-opens the thread if a viewer spoke last.
                cur["replies"] = fc.get("replies", cur.get("replies", []))
                cur["total_reply_count"] = fc.get("total_reply_count", cur.get("total_reply_count", 0))
                cur["like_count"] = fc.get("like_count", cur.get("like_count", 0))
    yt.save_comments_cache(cache)
    if not fetched_any:
        raise HTTPException(503, f"Fetch failed: {errors[0] if errors else 'no channels configured'}")

    approved = thanked = 0
    for c in [x for x in cache if not x.get("evaluated")]:
        r = yt.evaluate_comment(c.get("text", ""), c.get("commenter", ""), cfg)
        c.update({"evaluated": True, "is_request": r["is_request"],
                  "suggested_title": r["suggested_title"], "confidence": r["confidence"],
                  "interestingness": r.get("interestingness", 0.0), "reason": r["reason"],
                  "status": "evaluated" if c.get("status") == "new" else c.get("status")})
        if r["is_request"] and not c.get("thanked"):
            rep = yt.reply_to_comment(secrets, c.get("comment_id", ""),
                                      "Thanks for the suggestion! We'll look into making a video about this. 🎬",
                                      channel=c.get("channel", ""))
            if rep.get("success"):
                c["thanked"] = True
                thanked += 1
        if (auto_approve and r["is_request"] and r["confidence"] >= _AUTO_APPROVE_THRESHOLD
                and c.get("status") not in ("approved", "rejected")):
            c["status"] = "approved"
            qi = yt.add_to_queue(c, r["suggested_title"], source="comment")
            if qi:
                try:
                    vp = llm.generate_video_prompt(r["suggested_title"], c.get("text", ""))
                    if vp:
                        yt.update_queue_item(qi["id"], video_prompt=vp)
                except Exception:
                    pass
            approved += 1

    # Community engagement (issue #84): for non-request comments, draft a reply per
    # the comment's channel config and — if that channel auto-responds — post it now.
    # Runs over the whole cache (not just new comments) so enabling a channel's
    # engagement later picks up its backlog; the engagement_status stamp makes it
    # run-once per comment. Channels with no guidance prompt stay untouched (status "").
    chan_cfg = {c.get("id", ""): c for c in (cfg.get("youtube_channels") or [])}
    drafted = sent = 0
    for c in cache:
        if c.get("is_request"):
            continue
        entry = chan_cfg.get(c.get("channel", ""), {})
        guidance = str(entry.get("engagement_prompt") or "").strip()
        if not guidance:
            continue
        st = yt.thread_anchor(c)
        # Nothing to answer if the channel spoke last, or there's no viewer message.
        if st["last_is_owner"] or not st["anchor_id"]:
            continue
        # Run-once per viewer message: a brand-new reply (new anchor) re-opens the
        # thread, but we never re-draft the same message twice (issue #84).
        # Legacy records carry a status but no anchor — the old one-shot pass only
        # ever acted on the top-level comment, so seed the anchor there. This keeps
        # already-skipped/dismissed backlog from being resurrected (only a genuinely
        # new viewer reply, with a different anchor id, re-opens the thread).
        prior_anchor = c.get("engagement_anchor") or (c.get("comment_id", "") if c.get("engagement_status") else "")
        if prior_anchor == st["anchor_id"] and c.get("engagement_status"):
            continue
        r = llm.generate_community_reply(st["anchor_text"], st["anchor_author"],
                                         st["thread_text"], guidance, cfg)
        c["engagement_anchor"] = st["anchor_id"]
        c["engagement_reason"] = r.get("reason", "")
        if not (r.get("should_reply") and r.get("reply")):
            c["engagement_status"] = "skip"
            continue
        c["engagement_draft"] = r["reply"]
        if entry.get("auto_respond"):
            rep = yt.reply_to_comment(secrets, c.get("comment_id", ""), r["reply"],
                                      channel=c.get("channel", ""))
            if rep.get("success"):
                c["engagement_status"] = "sent"
                sent += 1
                continue
        c["engagement_status"] = "draft"
        drafted += 1

    yt.save_comments_cache(cache)
    return {"new": new_count, "thanked": thanked, "auto_approved": approved,
            "community_drafted": drafted, "community_sent": sent}


class FetchBody(BaseModel):
    auto_approve: bool | None = None


@api.post("/api/youtube/comments/fetch")
def youtube_fetch(body: FetchBody | None = None) -> dict:
    body = body or FetchBody()
    cfg = gapp.load_config()
    aa = cfg.get("youtube_auto_approve_comments", False) if body.auto_approve is None else body.auto_approve
    with _track_op("Fetching comments"):
        result = _fetch_and_evaluate(aa)
    return {**result, "comments": yt.load_comments_cache()}


class CommentActionBody(BaseModel):
    comment_id: str
    final_title: str = ""
    text: str = ""
    instruction: str = ""          # optional "tell it how" steering (draft-reply)


@api.post("/api/youtube/comments/approve")
def youtube_approve(body: CommentActionBody) -> dict:
    cache = yt.load_comments_cache()
    c = next((x for x in cache if x.get("comment_id") == body.comment_id), None)
    if not c:
        raise HTTPException(404, "Comment not found.")
    c["status"] = "approved"
    yt.save_comments_cache(cache)
    title = (body.final_title or "").strip() or c.get("suggested_title", "")
    qi = yt.add_to_queue(c, title, source="comment")
    if qi:
        try:
            vp = llm.generate_video_prompt(title, c.get("text", ""))
            if vp:
                yt.update_queue_item(qi["id"], video_prompt=vp)
        except Exception:
            pass
    return {"ok": True, "queued": bool(qi), "final_title": title}


@api.post("/api/youtube/comments/reject")
def youtube_reject(body: CommentActionBody) -> dict:
    cache = yt.load_comments_cache()
    c = next((x for x in cache if x.get("comment_id") == body.comment_id), None)
    if not c:
        raise HTTPException(404, "Comment not found.")
    c["status"] = "rejected"
    yt.save_comments_cache(cache)
    return {"ok": True}


@api.post("/api/youtube/comments/reply")
def youtube_reply(body: CommentActionBody) -> dict:
    if not body.text.strip():
        raise HTTPException(400, "Reply text required.")
    # Reply as the channel the comment was fetched from.
    c = next((x for x in yt.load_comments_cache()
              if x.get("comment_id") == body.comment_id), None)
    res = yt.reply_to_comment(gapp.load_config().get("youtube_client_secrets", ""),
                              body.comment_id, body.text.strip(),
                              channel=(c or {}).get("channel", ""))
    if not res.get("success"):
        raise HTTPException(502, f"Reply failed: {res.get('error', 'unknown')[:160]}")
    return {"ok": True}


@api.post("/api/youtube/comments/community/send")
def youtube_community_send(body: CommentActionBody) -> dict:
    """Post a drafted community reply (optionally edited) as the comment's channel,
    then mark it sent (issue #84). No-op unless the draft is still pending."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Reply text required.")
    cache = yt.load_comments_cache()
    c = next((x for x in cache if x.get("comment_id") == body.comment_id), None)
    if not c:
        raise HTTPException(404, "Comment not found.")
    if c.get("engagement_status") != "draft":
        raise HTTPException(409, "No pending draft for this comment.")
    res = yt.reply_to_comment(gapp.load_config().get("youtube_client_secrets", ""),
                              body.comment_id, text, channel=c.get("channel", ""))
    if not res.get("success"):
        raise HTTPException(502, f"Reply failed: {res.get('error', 'unknown')[:160]}")
    c["engagement_status"] = "sent"
    c["engagement_draft"] = text
    yt.save_comments_cache(cache)
    return {"ok": True}


@api.post("/api/youtube/comments/community/dismiss")
def youtube_community_dismiss(body: CommentActionBody) -> dict:
    """Discard a pending community-reply draft (issue #84)."""
    cache = yt.load_comments_cache()
    c = next((x for x in cache if x.get("comment_id") == body.comment_id), None)
    if not c:
        raise HTTPException(404, "Comment not found.")
    if c.get("engagement_status") != "draft":
        raise HTTPException(409, "No pending draft for this comment.")
    c["engagement_status"] = "dismissed"
    yt.save_comments_cache(cache)
    return {"ok": True}


@api.post("/api/youtube/comments/draft-reply")
def youtube_draft_reply(body: CommentActionBody) -> dict:
    """Draft a reply to any comment with the LLM (issue #88) — powers both the
    manual reply composer and the "regenerate" on a community-engagement draft.
    Always returns a reply; honours the comment's channel engagement voice when
    one is configured. Does not touch the cache — the client holds the draft until
    it sends."""
    c = next((x for x in yt.load_comments_cache()
              if x.get("comment_id") == body.comment_id), None)
    if not c:
        raise HTTPException(404, "Comment not found.")
    cfg = gapp.load_config()
    chan_cfg = {ch.get("id", ""): ch for ch in (cfg.get("youtube_channels") or [])}
    guidance = str(chan_cfg.get(c.get("channel", ""), {}).get("engagement_prompt") or "").strip()
    thread = "\n".join(
        f"{r.get('commenter', 'viewer')}: {r.get('text', '')}"
        for r in (c.get("replies") or [])
    )
    system = ("You write short, friendly replies to YouTube comments as the channel owner. "
              "Return ONLY the reply text — no preamble, no quotes, no labels.")
    user = (
        (f"Channel voice / guidance: {guidance}\n" if guidance else "Use a warm, friendly, on-brand tone.\n")
        + f"Commenter: {c.get('commenter', 'viewer')}\n"
        + f"Their comment: {c.get('text', '')}\n"
        + (f"Earlier replies in this thread:\n{thread}\n" if thread else "")
        + "\nWrite a concise reply (1–3 sentences)."
        + _instruction_note(body.instruction)
    )
    try:
        with _track_op("Drafting reply", c.get("commenter", "")):
            text = _llm_complete(system, user, cfg, max_tokens=300).strip().strip('"').strip()
    except Exception as e:
        raise HTTPException(503, f"Reply draft failed: {str(e).splitlines()[0][:200]}")
    return {"reply": text}


# ── X (Twitter) mention actions (issue #107) ─────────────────────────────────
# The X mirror of the YouTube comment actions above. Mentions are the "comments";
# requests feed the SAME generation queue (source_platform="x"); the LLM
# evaluation, community-reply drafting and thread_anchor are reused from the
# platform-agnostic helpers — only the fetch/reply/cache are X-specific.

_X_THANKS = "Thanks for the suggestion! We'll look into making a video about this. 🎬"


def _fetch_and_evaluate_x(auto_approve: bool) -> dict:
    cfg = gapp.load_config()
    cid, secret = _x_client_creds()
    accounts = [a.get("id", "") for a in (cfg.get("x_accounts") or [])] or [""]
    new_count = 0
    errors: list[str] = []
    fetched_any = False
    cache = xt.load_comments_cache()
    by_id = {c.get("comment_id"): c for c in cache}
    for acc in accounts:
        try:
            fetched = xt.fetch_mentions(cid, secret, account=acc)
            fetched_any = True
        except Exception as e:
            errors.append(f"{acc or 'default'}: {str(e).splitlines()[0][:120]}")
            continue
        for fc in fetched:
            cur = by_id.get(fc.get("comment_id"))
            if cur is None:
                entry = {**fc, "channel": acc, "evaluated": False, "is_request": False,
                         "suggested_title": "", "confidence": 0.0,
                         "interestingness": 0.0, "reason": "", "status": "new",
                         "engagement_status": "", "engagement_draft": "",
                         "engagement_reason": "", "engagement_anchor": ""}
                cache.insert(0, entry)
                by_id[fc.get("comment_id")] = entry
                new_count += 1
            else:
                cur["like_count"] = fc.get("like_count", cur.get("like_count", 0))
                cur["total_reply_count"] = fc.get("total_reply_count", cur.get("total_reply_count", 0))
    xt.save_comments_cache(cache)
    if not fetched_any:
        raise HTTPException(503, f"Fetch failed: {errors[0] if errors else 'no accounts configured'}")

    approved = thanked = 0
    for c in [x for x in cache if not x.get("evaluated")]:
        r = yt.evaluate_comment(c.get("text", ""), c.get("commenter", ""), cfg)
        c.update({"evaluated": True, "is_request": r["is_request"],
                  "suggested_title": r["suggested_title"], "confidence": r["confidence"],
                  "interestingness": r.get("interestingness", 0.0), "reason": r["reason"],
                  "status": "evaluated" if c.get("status") == "new" else c.get("status")})
        if r["is_request"] and not c.get("thanked"):
            rep = xt.reply_to_tweet(cid, secret, c.get("comment_id", ""), _X_THANKS,
                                    account=c.get("channel", ""))
            if rep.get("success"):
                c["thanked"] = True
                thanked += 1
        if (auto_approve and r["is_request"] and r["confidence"] >= _AUTO_APPROVE_THRESHOLD
                and c.get("status") not in ("approved", "rejected")):
            c["status"] = "approved"
            qi = yt.add_to_queue(c, r["suggested_title"], source="comment", source_platform="x")
            if qi:
                try:
                    vp = llm.generate_video_prompt(r["suggested_title"], c.get("text", ""))
                    if vp:
                        yt.update_queue_item(qi["id"], video_prompt=vp)
                except Exception:
                    pass
            approved += 1

    # Community engagement, per X account (mirrors the YouTube pass).
    acct_cfg = {a.get("id", ""): a for a in (cfg.get("x_accounts") or [])}
    drafted = sent = 0
    for c in cache:
        if c.get("is_request"):
            continue
        entry = acct_cfg.get(c.get("channel", ""), {})
        guidance = str(entry.get("engagement_prompt") or "").strip()
        if not guidance:
            continue
        st = yt.thread_anchor(c)
        if st["last_is_owner"] or not st["anchor_id"]:
            continue
        prior_anchor = c.get("engagement_anchor") or (c.get("comment_id", "") if c.get("engagement_status") else "")
        if prior_anchor == st["anchor_id"] and c.get("engagement_status"):
            continue
        r = llm.generate_community_reply(st["anchor_text"], st["anchor_author"],
                                         st["thread_text"], guidance, cfg)
        c["engagement_anchor"] = st["anchor_id"]
        c["engagement_reason"] = r.get("reason", "")
        if not (r.get("should_reply") and r.get("reply")):
            c["engagement_status"] = "skip"
            continue
        c["engagement_draft"] = r["reply"]
        if entry.get("auto_respond"):
            rep = xt.reply_to_tweet(cid, secret, c.get("comment_id", ""), r["reply"],
                                    account=c.get("channel", ""))
            if rep.get("success"):
                c["engagement_status"] = "sent"
                sent += 1
                continue
        c["engagement_status"] = "draft"
        drafted += 1

    xt.save_comments_cache(cache)
    return {"new": new_count, "thanked": thanked, "auto_approved": approved,
            "community_drafted": drafted, "community_sent": sent}


@api.get("/api/x/comments")
def x_comments() -> dict:
    return {"comments": xt.load_comments_cache()}


@api.post("/api/x/comments/fetch")
def x_fetch(body: FetchBody | None = None) -> dict:
    body = body or FetchBody()
    cfg = gapp.load_config()
    aa = cfg.get("x_auto_approve_comments", False) if body.auto_approve is None else body.auto_approve
    with _track_op("Fetching X mentions"):
        result = _fetch_and_evaluate_x(aa)
    return {**result, "comments": xt.load_comments_cache()}


@api.post("/api/x/comments/approve")
def x_approve(body: CommentActionBody) -> dict:
    cache = xt.load_comments_cache()
    c = next((x for x in cache if x.get("comment_id") == body.comment_id), None)
    if not c:
        raise HTTPException(404, "Mention not found.")
    c["status"] = "approved"
    xt.save_comments_cache(cache)
    title = (body.final_title or "").strip() or c.get("suggested_title", "")
    qi = yt.add_to_queue(c, title, source="comment", source_platform="x")
    if qi:
        try:
            vp = llm.generate_video_prompt(title, c.get("text", ""))
            if vp:
                yt.update_queue_item(qi["id"], video_prompt=vp)
        except Exception:
            pass
    return {"ok": True, "queued": bool(qi), "final_title": title}


@api.post("/api/x/comments/reject")
def x_reject(body: CommentActionBody) -> dict:
    cache = xt.load_comments_cache()
    c = next((x for x in cache if x.get("comment_id") == body.comment_id), None)
    if not c:
        raise HTTPException(404, "Mention not found.")
    c["status"] = "rejected"
    xt.save_comments_cache(cache)
    return {"ok": True}


@api.post("/api/x/comments/reply")
def x_reply(body: CommentActionBody) -> dict:
    if not body.text.strip():
        raise HTTPException(400, "Reply text required.")
    c = next((x for x in xt.load_comments_cache()
              if x.get("comment_id") == body.comment_id), None)
    cid, secret = _x_client_creds()
    res = xt.reply_to_tweet(cid, secret, body.comment_id, body.text.strip(),
                            account=(c or {}).get("channel", ""))
    if not res.get("success"):
        raise HTTPException(502, f"Reply failed: {res.get('error', 'unknown')[:160]}")
    return {"ok": True}


@api.post("/api/x/comments/community/send")
def x_community_send(body: CommentActionBody) -> dict:
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Reply text required.")
    cache = xt.load_comments_cache()
    c = next((x for x in cache if x.get("comment_id") == body.comment_id), None)
    if not c:
        raise HTTPException(404, "Mention not found.")
    if c.get("engagement_status") != "draft":
        raise HTTPException(409, "No pending draft for this mention.")
    cid, secret = _x_client_creds()
    res = xt.reply_to_tweet(cid, secret, body.comment_id, text, account=c.get("channel", ""))
    if not res.get("success"):
        raise HTTPException(502, f"Reply failed: {res.get('error', 'unknown')[:160]}")
    c["engagement_status"] = "sent"
    c["engagement_draft"] = text
    xt.save_comments_cache(cache)
    return {"ok": True}


@api.post("/api/x/comments/community/dismiss")
def x_community_dismiss(body: CommentActionBody) -> dict:
    cache = xt.load_comments_cache()
    c = next((x for x in cache if x.get("comment_id") == body.comment_id), None)
    if not c:
        raise HTTPException(404, "Mention not found.")
    if c.get("engagement_status") != "draft":
        raise HTTPException(409, "No pending draft for this mention.")
    c["engagement_status"] = "dismissed"
    xt.save_comments_cache(cache)
    return {"ok": True}


# ── Queue management ──────────────────────────────────────────────────────────

class QueueMoveBody(BaseModel):
    id: str
    direction: int = -1


@api.post("/api/queue/move")
def queue_move(body: QueueMoveBody) -> dict:
    ok = yt.move_queue_item(body.id, body.direction)
    return {"ok": ok, "queue": yt.load_queue()}


class QueueIdBody(BaseModel):
    id: str


@api.post("/api/queue/remove")
def queue_remove(body: QueueIdBody) -> dict:
    ok = yt.remove_queue_item(body.id)
    return {"ok": ok, "queue": yt.load_queue()}


@api.post("/api/queue/abandon")
def queue_abandon(body: QueueIdBody) -> dict:
    """Cancel a stuck 'creating' queue item: stop any associated render and remove from queue."""
    item = next((q for q in yt.load_queue() if q.get("id") == body.id), None)
    if not item:
        raise HTTPException(404, "Queue item not found.")
    work_dir = item.get("work_dir", "")
    if work_dir:
        try:
            gapp.on_cancel_active_job(work_dir)
        except Exception:
            pass
    yt.remove_queue_item(body.id)
    return {"ok": True, "queue": yt.load_queue()}


@api.post("/api/queue/retry-reply")
def queue_retry_reply(body: QueueIdBody) -> dict:
    """Re-attempt the completion reply for a posted queue item (e.g. if it failed on first publish)."""
    item = _queue_item_by_id(body.id)
    if not item:
        raise HTTPException(404, "Queue item not found.")
    if item.get("status") != "posted":
        raise HTTPException(400, "Queue item is not in 'posted' state.")
    url = item.get("youtube_url", "")
    if not url:
        raise HTTPException(400, "Queue item has no YouTube URL yet.")
    title = item.get("final_title") or item.get("title") or ""
    # Clear any previous failed attempt so _post_completion_reply will retry.
    if not item.get("completion_replied"):
        yt.update_queue_item(body.id, completion_reply_attempted_at=None, completion_reply_error="")
    result = _post_completion_reply(body.id, title, url)
    if result.get("already_replied"):
        return {"ok": True, "message": "Already replied."}
    if result.get("success"):
        return {"ok": True, "message": "Reply sent."}
    if not result.get("attempted"):
        raise HTTPException(400, result.get("reason", "Could not send reply."))
    raise HTTPException(502, f"Reply failed: {result.get('error', 'unknown error')}")


def _queue_item_minutes(item: dict, ss: dict) -> float:
    """Target length for a queue item: its explicit minutes, else its legacy
    scene-count suggestion (~9 s scenes), else the style's own length."""
    try:
        m = float(item.get("suggested_minutes") or 0)
    except (TypeError, ValueError):
        m = 0.0
    if m > 0:
        return m
    try:
        sc = int(item.get("suggested_scene_count") or 0)
    except (TypeError, ValueError):
        sc = 0
    if sc > 0:
        return cadence.minutes_for_scenes(sc)
    return gapp.style_video_minutes(ss)


class QueueAddBody(BaseModel):
    title: str
    # Target video length in minutes (preferred). 0 falls back to the legacy
    # n_scenes, then the style's own length.
    minutes: float = 0.0
    n_scenes: int = 0
    prompt: str = ""
    resolution: str = ""
    style_name: str = ""


@api.post("/api/queue/add")
def queue_add(body: QueueAddBody) -> dict:
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "Title is required.")
    cfg = gapp.load_config()
    ss = gapp.style_settings(cfg, body.style_name)
    try:
        minutes = float(body.minutes or 0)
    except (TypeError, ValueError):
        minutes = 0.0
    if minutes <= 0:
        minutes = (cadence.minutes_for_scenes(body.n_scenes) if body.n_scenes
                   else gapp.style_video_minutes(ss))
    plan = gapp.style_script_plan(ss, minutes=minutes)
    comment = {"comment_id": "", "text": body.prompt, "commenter": "you",
               "suggested_scene_count": plan["n_scenes"]}
    entry = yt.add_to_queue(comment, title, source="manual")
    if entry:
        updates = {"suggested_minutes": plan["minutes"]}
        if body.prompt.strip():
            updates["video_prompt"] = body.prompt.strip()
        if body.resolution.strip():
            updates["gen_resolution"] = body.resolution.strip()
        if body.style_name.strip():
            updates["gen_style_name"] = body.style_name.strip()
        if updates:
            yt.update_queue_item(entry["id"], **updates)
    return {"ok": bool(entry), "queue": yt.load_queue()}


class QueueUpdateBody(BaseModel):
    id: str
    final_title: str | None = None
    video_prompt: str | None = None
    suggested_minutes: float | None = None
    suggested_scene_count: int | None = None
    gen_resolution: str | None = None
    gen_style_name: str | None = None


@api.post("/api/queue/update")
def queue_update(body: QueueUpdateBody) -> dict:
    """Edit a pending queue item's basic fields in place — title, prompt, scene
    count, resolution (issue #43). Only pending items are editable; anything
    already rendering/posted is left untouched. Fields left as None are not
    changed."""
    item = next((q for q in yt.load_queue() if q.get("id") == body.id), None)
    if not item:
        raise HTTPException(404, "Queue item not found.")
    if item.get("status") != "pending":
        raise HTTPException(400, "Only queued (pending) items can be edited.")
    updates: dict = {}
    if body.final_title is not None:
        title = body.final_title.strip()
        if not title:
            raise HTTPException(400, "Title cannot be empty.")
        updates["final_title"] = title
    if body.video_prompt is not None:
        updates["video_prompt"] = body.video_prompt.strip()
    if body.suggested_minutes is not None:
        updates["suggested_minutes"] = round(max(0.25, min(cadence.MAX_MINUTES,
                                                           float(body.suggested_minutes))), 2)
    if body.suggested_scene_count is not None:
        updates["suggested_scene_count"] = max(1, min(200, int(body.suggested_scene_count)))
    if body.gen_resolution is not None:
        updates["gen_resolution"] = body.gen_resolution.strip()
    if body.gen_style_name is not None:
        updates["gen_style_name"] = body.gen_style_name.strip()
    if updates:
        yt.update_queue_item(body.id, **updates)
    return {"ok": True, "queue": yt.load_queue()}


class QueueApproveBody(BaseModel):
    id: str
    approved: bool = True


@api.post("/api/queue/approve")
def queue_approve(body: QueueApproveBody) -> dict:
    """Approve (or un-approve) a pending queue item for rendering. With
    auto-start on, the automation loop only renders approved items, so this is
    the gate between "script written" and "okay to render". Only pending items
    are gated; anything already rendering/finished is left untouched."""
    item = next((q for q in yt.load_queue() if q.get("id") == body.id), None)
    if not item:
        raise HTTPException(404, "Queue item not found.")
    if item.get("status") != "pending":
        raise HTTPException(400, "Only queued (pending) items can be approved.")
    yt.update_queue_item(body.id, approved=bool(body.approved))
    return {"ok": True, "queue": yt.load_queue()}


def _job_meta_field(job_id: str, key: str, default=""):
    try:
        store = DurableStore.default()
        try:
            row = store.get_job(job_id)
        finally:
            store.close()
        if row:
            return json.loads(dict(row).get("metadata_json") or "{}").get(key, default)
    except Exception:
        pass
    return default


def _start_queue_item(item: dict) -> dict:
    """Launch the render for a queue item. If the item already has a ready
    script (script_ready + work_dir + video_job_id) we render it directly;
    otherwise we generate the script first. Reuses script_generate +
    start_generation."""
    cfg = gapp.load_config()
    # Claim the item BEFORE the slow script generation. Automation
    # (_auto_start_best via _ordered_pending) only picks status=="pending"
    # items, so flipping the status away from pending now is the claim that stops
    # a concurrent tick from starting the same item and creating a duplicate
    # work folder. Without it, the item stays "pending" for the whole
    # ~45s script_generate, which is exactly the window that produced two folders.
    item_id = item.get("id")
    if item_id:
        cur = next((q for q in yt.load_queue() if q.get("id") == item_id), None)
        # Block only items that are already being worked on or finished; a
        # failed/errored item may still be retried.
        if cur is not None and cur.get("status") in ("creating", "upload_pending", "posted", "done"):
            raise HTTPException(409, f"Queue item is already {cur.get('status')}.")
        # Starting a render is itself an approval (manual "Render now" or an
        # auto-start of an approved item) — stamp it so a later failed-retry
        # still satisfies the review gate.
        yt.update_queue_item(item_id, status="creating", approved=True)
    title = item.get("final_title", "")
    # The item's style profile (or the default style) supplies every setting
    # the queue item doesn't carry itself. An empty style_name is passed
    # through so start_generation can still prefer the name stamped on the job
    # at script time.
    style_name = (item.get("gen_style_name") or "").strip()
    ss = gapp.style_settings(cfg, style_name)
    minutes = _queue_item_minutes(item, ss)

    # The claim above flipped the item to "creating". If the work below fails, flip it
    # to "failed" so it doesn't sit forever showing "Rendering" — and so automation,
    # which only picks "pending" items, won't loop retrying a poison item. Re-raise so
    # the manual queue_start path still surfaces the error to the caller.
    try:
        if item.get("script_ready") and item.get("work_dir") and item.get("video_job_id"):
            job_id = item["video_job_id"]
            wd = item["work_dir"]
            # Blank voice/resolution fall through to start_generation, which
            # resolves them from the job's stamped style profile (then the default).
            start_generation(GenerateBody(
                # n_scenes=0: render every scene the script actually has (the
                # cadence-driven count can differ from the item's suggestion).
                job_id=job_id, work_dir=wd, video_title=title, title=title, n_scenes=0,
                voice=item.get("gen_voice") or "",
                resolution=item.get("gen_resolution") or "",
                music_desc=item.get("gen_music") or _job_meta_field(job_id, "music_desc"),
                style=item.get("gen_style") or _job_meta_field(job_id, "style"),
                style_name=style_name))
            # start_generation wrote queue_item_id="" (the item is already "creating",
            # not "pending", so its title-match misses). Stamp the reverse link now so
            # _auto_post_done recognises this as a queue-driven job and posts it.
            _link_queue_item_to_work_dir(item, Path(wd))
            yt.update_queue_item(item["id"], status="creating")
            return {"job_id": job_id, "work_dir": wd, "title": title}

        topic = item.get("video_prompt") or title
        resolution = item.get("gen_resolution") or ss.get("resolution") or gapp._DEFAULT_RESOLUTION
        # Server-side automation: run generation inline (no browser connection to
        # protect) and use the result directly — the HTTP endpoint is the polling
        # wrapper around this same call.
        # No script yet: write one in automation's format. A music video needs
        # its song rendered BEFORE the story is divided (see _auto_song_first),
        # so that runs inline here too — the song review gate doesn't apply on
        # this path, which only ever runs for an item already cleared to render.
        auto = gapp.automation_settings(cfg, style_name)
        fmt = auto["auto_format"]
        song_wd = ""
        if fmt == "song" and auto["auto_song"]:
            # An item parked at the song gate already has its track rendered —
            # draft the story from that one rather than singing a second.
            if item.get("song_parked") and (item.get("work_dir") or "") \
                    and Path(item["work_dir"]).is_dir():
                song_wd = item["work_dir"]
            else:
                song_wd = _auto_song_first(
                    cfg, title=title, topic=topic, minutes=minutes,
                    style_name=style_name, n_scenes=gapp.style_video_scenes(ss),
                    queue_item_id=item.get("id") or "")["work_dir"]
        gen = _do_script_generate(GenerateScriptBody(
            video_title=title, topic=topic, minutes=minutes, resolution=resolution,
            style_name=style_name, format=fmt, work_dir=song_wd,
            n_scenes=gapp.style_video_scenes(ss) if fmt == "song" else 0,
            auto_critic=auto["auto_critic"]))
        start_generation(GenerateBody(
            job_id=gen["job_id"], work_dir=gen["work_dir"], video_title=title, title=title,
            n_scenes=0, voice=ss.get("voice", ""),
            resolution=resolution,
            music_desc=gen.get("music_desc", ""), style=gen.get("style", ""),
            style_name=gen.get("style_name", "")))
        # See above: re-link the work dir to this queue item so auto-post finds it.
        _link_queue_item_to_work_dir(item, Path(gen["work_dir"]))
        yt.update_queue_item(item["id"], status="creating", song_parked=False,
                             video_job_id=gen["job_id"], work_dir=gen["work_dir"])
        return {"job_id": gen["job_id"], "work_dir": gen["work_dir"], "title": title}
    except Exception as exc:
        if item_id:
            try:
                yt.update_queue_item(item_id, status="failed", error=str(exc)[:300])
            except Exception:
                pass
        raise


@api.post("/api/queue/start")
def queue_start(body: QueueIdBody) -> dict:
    if gapp._is_job_running():
        raise HTTPException(409, "A render is already running — wait for it to finish.")
    item = next((q for q in yt.load_queue() if q.get("id") == body.id), None)
    if not item:
        raise HTTPException(404, "Queue item not found.")
    return {"ok": True, **_start_queue_item(item)}


class FromJobBody(BaseModel):
    job_id: str
    work_dir: str
    video_title: str = ""
    # Target length in minutes the script was generated for (display/restart).
    minutes: float = 0.0
    n_scenes: int = 0
    style: str = ""
    resolution: str = ""
    voice: str = ""
    music_desc: str = ""
    queue_item_id: str = ""
    style_name: str = ""
    # True when the caller has reviewed the script and OKs it to render
    # (Script-screen "Approve", or Create's auto-approve). Merely linking a
    # script to a queued slot (Edit-script flow) leaves this False so the item
    # waits for an explicit approval before auto-start picks it up.
    approved: bool = False


@api.post("/api/queue/from-job")
def queue_from_job(body: FromJobBody) -> dict:
    """Add an already-generated script to the queue. Does NOT render unless
    'auto-start next' (youtube_auto_start_job) is on, the script is approved
    (body.approved, or the global auto-approve mode), and nothing is currently
    rendering. An unapproved script waits for the Queue's Approve action.

    If body.queue_item_id points at a still-pending slot, update THAT item in
    place (keeping its position) instead of appending a new entry. This is how
    the Queue "Edit" flow attaches a ready script to an existing slot so it
    renders fast (issue #43). A slot already claimed as "creating" (automation
    starts the item before writing its script) is likewise updated in place —
    never duplicated, and never un-approved. In-place updates never auto-start —
    the item stays queued exactly where it was."""
    cfg = gapp.load_config()
    title = (body.video_title or "").strip() or Path(body.work_dir).name
    n = body.n_scenes
    if not n:
        try:
            store = DurableStore.default()
            try:
                n = store.scene_count(body.job_id)
            finally:
                store.close()
        except Exception:
            n = cfg.get("default_n_scenes", 6)
    n = max(1, int(n or 1))
    try:
        minutes = round(float(body.minutes or 0), 2)
    except (TypeError, ValueError):
        minutes = 0.0

    script_fields = dict(
        video_job_id=body.job_id, work_dir=body.work_dir, script_ready=True,
        approved=bool(body.approved),
        gen_style=body.style, gen_resolution=body.resolution,
        gen_voice=body.voice, gen_music=body.music_desc,
        gen_style_name=body.style_name,
    )
    if minutes > 0:
        script_fields["suggested_minutes"] = minutes

    # In-place update of an existing pending or creating slot — keep its queue
    # position. Prefer the explicit queue_item_id; otherwise fall back to a row
    # that already points at this job/work_dir, so a second approve of the same
    # script fills that slot instead of appending a duplicate. Both rows would
    # share one work_dir, and a Library delete removes every row matching it —
    # which is why a duplicate also made deleting one wipe both.
    queue = yt.load_queue()
    existing = None
    if body.queue_item_id:
        existing = next((q for q in queue if q.get("id") == body.queue_item_id), None)
    if existing is None:
        existing = next((q for q in queue
                         if q.get("status") in ("pending", "creating")
                         and ((body.job_id and q.get("video_job_id") == body.job_id)
                              or (body.work_dir and q.get("work_dir") == body.work_dir))), None)
    if existing is not None and existing.get("status") == "pending":
        yt.update_queue_item(existing["id"], final_title=title,
                             suggested_scene_count=n, **script_fields)
        return {"ok": True, "queue_item_id": existing["id"],
                "started": None, "updated_in_place": True}
    if existing is not None and existing.get("status") == "creating":
        # The slot is already claimed by a running start: automation's
        # _start_queue_item flips the row to "creating" BEFORE the ~45s script
        # generation, and a music video's script gen then reaches here via the
        # queue_item_id carried in the create brief. Appending would spawn a
        # duplicate row for the same work_dir (which auto-approve would later
        # re-render). Link the fresh script to the claimed row instead — and
        # never downgrade its approval: starting was itself the approval.
        if not body.approved:
            script_fields.pop("approved", None)
        yt.update_queue_item(existing["id"], final_title=title,
                             suggested_scene_count=n, **script_fields)
        return {"ok": True, "queue_item_id": existing["id"],
                "started": None, "updated_in_place": True}

    entry = yt.add_to_queue({"comment_id": "", "text": "", "commenter": "you",
                             "suggested_scene_count": n}, title, source="script")
    if not entry:
        raise HTTPException(500, "Could not enqueue the script.")
    yt.update_queue_item(entry["id"], **script_fields)

    started = None
    # Only fire an immediate render when the script is explicitly approved (or
    # the global auto-approve mode is on). In review mode an enqueued-but-
    # unapproved script waits for the Queue's Approve action — enqueuing is not
    # approving.
    _auto = gapp.automation_settings(cfg, body.style_name)
    if (_auto["auto_start_job"] and not gapp._is_job_running()
            and (body.approved or _auto["auto_approve_script"])):
        item = next((q for q in yt.load_queue() if q.get("id") == entry["id"]), None)
        if item:
            try:
                started = _start_queue_item(item)
            except Exception:
                started = None
    return {"ok": True, "queue_item_id": entry["id"], "started": started}


# ── film scene editor (post-render) ──────────────────────────────────────────

# In-memory background task store for re-render jobs (similar to _upload_tasks)
_film_tasks: dict = {}
# Side registry: tid -> {work_dir, scene_id, component}. Set once at task
# creation and never overwritten by the worker threads (which reassign
# _film_tasks[tid] wholesale), so it survives long enough to map a running task
# back to its film when the edit page reloads.
_film_task_meta: dict = {}
# Task ids whose film was deleted while they ran. Re-render workers are
# in-process daemon threads — there is no pid to SIGTERM (unlike PR #76's
# resume_generation.py) — so deletion flags them here and they abort at the
# next checkpoint instead of feeding ComfyUI/TTS more work for a dead film.
_film_cancelled_tids: set = set()
# How long a failed re-render stays visible on the edit page after the fact, so a
# failure that happened while the user was on another screen is still surfaced on
# their return rather than silently vanishing. In-memory only — a backend restart
# clears it, and starting a fresh re-render for the scene clears it immediately.
_FILM_ERROR_TTL_S = 6 * 3600

# One process-wide ComfyUI pool shared by every edit-screen scene re-render.
# Without it, each re-render built its own WorkerPool, whose per-worker
# semaphore then gated nothing across requests: clicking regenerate on many
# scenes submitted them all to ComfyUI at once, piling extra jobs onto the busy
# workers. Anything left *pending* behind a running job for >_PENDING_TIMEOUT
# (180s, see pipeline/comfyui.py) is deleted by the worker-stuck safety valve,
# so every re-render past the worker count failed silently. Sharing one pool
# makes acquire() a real FIFO queue — extra re-renders wait for a free worker
# instead of being killed.
_edit_render_pool = None
_edit_render_pool_key: tuple = ()
_edit_render_pool_lock = threading.Lock()


def _shared_edit_render_pool():
    """Get-or-create the shared re-render WorkerPool over the reachable workers,
    or None when none are reachable. Rebuilt only when the reachable set changes."""
    global _edit_render_pool, _edit_render_pool_key
    from pipeline.worker_pool import WorkerPool, alive_workers
    # Re-rendering means the UI is in use — keep the main render reserving a
    # worker for UI work (issue #98), mirroring _preview_worker_urls().
    ui_activity.mark_active()
    cfg = gapp.load_config()
    try:
        urls = alive_workers(cfg.get("comfy_workers", []))
    except Exception as exc:
        gapp.logger.warning("Re-render worker probe failed: %s", exc)
        return None
    key = tuple(sorted(urls))
    with _edit_render_pool_lock:
        if _edit_render_pool is None or _edit_render_pool_key != key:
            _edit_render_pool = WorkerPool(list(urls))
            _edit_render_pool_key = key
        return _edit_render_pool


def _acquire_render_worker(pool, task_id: str) -> str:
    """pool.acquire() that surfaces the wait: the shared re-render pool is a
    FIFO, so re-renders past the worker count block here for minutes. While
    blocked the task carries queued=True, which the Activity screen shows as a
    "queued" row instead of a green "running" one."""
    task = _film_tasks.get(task_id)
    if isinstance(task, dict):
        task["queued"] = True
    try:
        return pool.acquire()
    finally:
        task = _film_tasks.get(task_id)
        if isinstance(task, dict):
            task.pop("queued", None)


class _FilmTaskCancelled(Exception):
    """Raised inside a re-render worker when its film was deleted mid-task."""


def _film_checkpoint(task_id: str) -> None:
    if task_id in _film_cancelled_tids:
        raise _FilmTaskCancelled()


def _finish_film_task_error(task_id: str, e: Exception) -> None:
    """Record a worker failure — as "cancelled" when the film was deleted
    mid-task (the in-flight step then fails on the vanished dir, which is not
    a real error), as "error" otherwise."""
    if task_id in _film_cancelled_tids:
        _film_tasks[task_id] = {"status": "cancelled"}
    else:
        # finished_at lets /films/tasks re-surface this failure (bounded by
        # _FILM_ERROR_TTL_S) when the user returns to the editor, instead of the
        # scene silently showing its old frame/clip.
        # Logged as well as stored: _film_tasks is in-memory, so a restart (or a
        # user who closed the tab) otherwise loses the only record of WHY a
        # multi-hour job failed.
        gapp.logger.error("[film] task %s failed: %s", task_id, e, exc_info=True)
        _film_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:200],
                                "finished_at": time.time()}


def _clear_finished_film_tasks(work_dir: str, scene_id: int) -> None:
    """Drop terminal (error/cancelled/done) re-render records for a scene. Called
    when a fresh re-render starts so it supersedes any earlier failed attempt on
    that scene (clears the stale 'failed' badge) and terminal records don't pile
    up in the in-memory task stores."""
    for tid, meta in list(_film_task_meta.items()):
        if meta.get("work_dir") != work_dir or meta.get("scene_id") != scene_id:
            continue
        if (_film_tasks.get(tid) or {}).get("status") == "running":
            continue
        _film_tasks.pop(tid, None)
        _film_task_meta.pop(tid, None)
        _film_cancelled_tids.discard(tid)


def _cancel_film_tasks(work_dir: Path) -> int:
    """Flag every running re-render task for *work_dir* so its thread stops.

    Cooperative: a step already in flight on a remote worker still runs to
    completion there, but the thread aborts at the next checkpoint (or when
    the vanished dir fails its write) rather than starting the next step.
    Returns the number of tasks flagged."""
    targets = {str(work_dir)}
    try:
        targets.add(str(work_dir.resolve()))
    except OSError:
        pass
    n = 0
    for tid, meta in list(_film_task_meta.items()):
        if meta.get("work_dir") not in targets:
            continue
        if (_film_tasks.get(tid) or {}).get("status") != "running":
            continue
        _film_cancelled_tids.add(tid)
        n += 1
    return n


def _film_scene_files(work_dir: Path, sid: int) -> dict:
    narration = work_dir / f"scene_{sid:02d}_narration.wav"
    raw_video = work_dir / f"scene_{sid:02d}_video.mp4"
    clip = work_dir / f"scene_{sid:02d}_clip_01.mp4"
    final = work_dir / f"scene_{sid:02d}_final.mp4"
    first_frame = work_dir / f"scene_{sid:02d}_first_frame.png"
    preview = work_dir / f"scene_{sid:02d}_preview.png"

    has_nar = narration.exists() and narration.stat().st_size > 1000
    has_final = final.exists() and final.stat().st_size > 10_000
    actual_video = (
        raw_video if (raw_video.exists() and raw_video.stat().st_size > 10_000)
        else clip if (clip.exists() and clip.stat().st_size > 10_000)
        else None
    )
    preview_img = first_frame if first_frame.exists() else (preview if preview.exists() else None)
    preview_mtime = int(preview_img.stat().st_mtime) if preview_img else 0

    # Cache-bust the video URL with the file's mtime so a re-rendered clip at the
    # same path isn't served stale from the browser cache (the path alone never
    # changes, so without &t= the <video> element keeps the old file).
    video_file = final if has_final else actual_video
    video_mtime = int(video_file.stat().st_mtime) if video_file else 0

    return {
        "has_narration": has_nar,
        "has_video": actual_video is not None,
        "has_final": has_final,
        # Whether the take on screen is one H3 can still be continued from
        # (an acted scene, shot with its context saved, not since replaced).
        "can_continue": has_final and scene_context.continuable(work_dir, sid, final),
        "narration_url": f"/api/file?path={narration}" if has_nar else "",
        "video_url": f"/api/file?path={video_file}&t={video_mtime}" if video_file else "",
        "preview_url": f"/api/file?path={preview_img}&t={preview_mtime}" if preview_img else "",
    }


def _load_scene_order(work_dir: Path) -> list | None:
    order_file = work_dir / "scene_edit_order.json"
    if order_file.exists():
        try:
            return json.loads(order_file.read_text())
        except Exception:
            pass
    return None


def _save_scene_order(work_dir: Path, order: list) -> None:
    (work_dir / "scene_edit_order.json").write_text(json.dumps(order))


def _last_scene_id(work_dir: Path) -> int | None:
    """Id of the film's closing scene in display order, or None if unknown."""
    order = _load_scene_order(work_dir)
    if not order:
        try:
            order = [int(r.get("id") or 0) for r in (gapp._load_scenes_for_work_dir(work_dir) or [])]
        except Exception:
            return None
    return int(order[-1]) if order else None


def _film_job_config(work_dir: Path) -> dict:
    try:
        return json.loads((work_dir / "job_config.json").read_text())
    except Exception:
        return {}


def _write_film_job_config(work_dir: Path, jc: dict) -> None:
    (work_dir / "job_config.json").write_text(json.dumps(jc, indent=2))


def _scene_voice_name(row: dict, jc: dict) -> str:
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    voice = str(meta.get("voice") or "").strip()
    if voice:
        return voice
    return str(jc.get("default_voice") or "").strip()


def _voice_ref_for_name(name: str) -> Path | None:
    if not name or name == gapp.F5TTS_DEFAULT_OPTION:
        return None
    ref = gapp.voice_path_for(name)
    return Path(ref).expanduser() if ref else None


def _voice_label(name: str) -> str:
    return name if name and name != gapp.F5TTS_DEFAULT_OPTION else gapp.F5TTS_DEFAULT_OPTION


def _film_dimensions(work_dir: Path) -> tuple[int, int]:
    """Best-effort (width, height) of the rendered video for a work dir.

    Prefers explicit vid_width/vid_height in job_config.json, then falls back to
    the named resolution, then the configured default. Used to orient the cover
    image and to decide the default thumbnail-upload behaviour.
    """
    jc = _film_job_config(work_dir)
    w, h = int(jc.get("vid_width") or 0), int(jc.get("vid_height") or 0)
    if w > 0 and h > 0:
        return w, h
    cfg = gapp.load_config()
    resolution = jc.get("resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)
    return gapp._RESOLUTIONS.get(resolution, (1920, 1080))


def _is_portrait_film(work_dir: Path) -> bool:
    """True when the rendered video is taller than wide (a YouTube Short)."""
    w, h = _film_dimensions(work_dir)
    return h > w


def _final_upscale_info(wd: Path) -> dict:
    """Whether the published final is larger than the render tier, and what to
    call that size.

    The scene clips always stay at the render resolution — only the assembled
    final is upscaled (the render-time finishing step or a Remix upscale) — so
    without this the editor gives no hint the film was upscaled at all, and a
    scene re-render looks like it would throw the whole upscale away.
    """
    fw = fh = 0
    final_path = gapp._final_path_for_work_dir(wd)
    if final_path.exists():
        try:
            from pipeline.assembler import _get_video_dimensions
            fw, fh = _get_video_dimensions(final_path)
        except Exception:
            fw = fh = 0
    rw, rh = _film_dimensions(wd)
    upscaled = bool(fw and fh and fw * fh > rw * rh)
    label = ""
    if upscaled:
        # The friendly tier name ("Portrait 4K (2160×3840)") when the final
        # matches one; a hand-sized cut falls back to raw pixels.
        label = next((name for name, dims in gapp._UPSCALE_RESOLUTIONS.items()
                      if dims == (fw, fh)), "") or f"{fw}×{fh}"
    return {"upscaled": upscaled, "final_width": fw, "final_height": fh,
            "label": label}


@api.get("/api/films/scenes")
def film_scenes(work_dir: str = Query(...)) -> dict:
    wd = Path(work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")

    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
        job_row = store.get_job(job_id)
    finally:
        store.close()

    if not rows:
        script_path = wd / "script.json"
        if script_path.exists():
            try:
                raw = json.loads(script_path.read_text())
                rows = raw if isinstance(raw, list) else []
            except Exception:
                rows = []

    order = _load_scene_order(wd)
    scene_map = {int(r.get("id") or r.get("scene_id") or 0): r for r in rows}

    if order is not None:
        ordered = [scene_map[sid] for sid in order if sid in scene_map]
    else:
        ordered = rows

    jc = _film_job_config(wd)
    result = []
    for r in ordered:
        sid = int(r.get("id") or r.get("scene_id") or 0)
        scene_json = {**_scene_to_json(r, wd), **_film_scene_files(wd, sid)}
        scene_json["effective_voice"] = _voice_label(_scene_voice_name(r, jc))
        result.append(scene_json)

    title = ""
    style = ""
    if job_row:
        d = _row_to_dict(job_row)
        cfg_j = json.loads(d.get("config_json") or "{}")
        meta_j = json.loads(d.get("metadata_json") or "{}")
        title = cfg_j.get("video_title") or d.get("title") or wd.name
        style = meta_j.get("style") or jc.get("style") or ""
    else:
        title = jc.get("video_title") or jc.get("title") or wd.name

    cfg = gapp.load_config()
    resolution = jc.get("resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)

    # A song film's takes carry their own slice of the track, so the tiles play
    # with music. The whole song still goes over: the wall offers each singing
    # tile its window on its own, to check against what the take performs.
    track = wd / "background_music.wav"
    song_url = (_busted_file_url(track)
                if track.exists() and (wd / "song.json").exists() else "")

    return {
        "scenes": result,
        "song_url": song_url,
        "job_id": job_id,
        "work_dir": str(wd),
        "title": title,
        "style": style,
        # Are this film's SILENT scenes performed on H3 (h3_silent_scenes)? They
        # are then written through the same fields as an acted scene, so the
        # editor gives them the acted setup rather than a bare duration.
        "acted_silent": _acted_silent_cfg(jc)["h3_silent_scenes"],
        "resolution": resolution,
        # The final may be bigger than the scene clips (finishing/Remix
        # upscale): say so, or a scene re-render looks like it costs the
        # whole upscale.
        "upscale": _final_upscale_info(wd),
        "voice": jc.get("default_voice", ""),
        "voices": gapp.get_voice_choices(),
    }


class DeleteFilmSceneBody(BaseModel):
    work_dir: str
    scene_id: int


@api.post("/api/films/scenes/delete")
def delete_film_scene(body: DeleteFilmSceneBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    sid = body.scene_id
    for fname in [
        f"scene_{sid:02d}_narration.wav",
        f"scene_{sid:02d}_video.mp4",
        f"scene_{sid:02d}_clip_01.mp4",
        f"scene_{sid:02d}_clip_02.mp4",
        f"scene_{sid:02d}_final.mp4",
        f"scene_{sid:02d}_first_frame.png",
        f"scene_{sid:02d}_preview.png",
    ]:
        (wd / fname).unlink(missing_ok=True)

    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()

    all_ids = [int(r.get("id") or r.get("scene_id") or 0) for r in rows]
    order = _load_scene_order(wd) or all_ids
    new_order = [i for i in order if i != sid]
    _save_scene_order(wd, new_order)
    return {"ok": True, "order": new_order}


class AddFilmSceneBody(BaseModel):
    work_dir: str
    after_scene_id: int = 0  # insert after this scene in display order; 0 ⟹ end


@api.post("/api/films/scenes/add")
def add_film_scene(body: AddFilmSceneBody) -> dict:
    """Add a blank scene to a rendered film (issue #193). Post-render scene ids
    are stable filename keys, so the new scene takes max(id)+1 and its display
    position lives in scene_edit_order.json — content is then built with the
    existing per-scene re-render endpoints (narration → image → video) and
    lands in the film on the next reassemble."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")

    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        if not store.get_job(job_id):
            # Pre-durable-store film: scenes.job_id has an enforced FK to jobs.
            jc = _film_job_config(wd)
            store.create_or_update_job(job_id, wd, jc.get("video_title") or wd.name,
                                       config=jc, metadata={})
        rows = store.scene_rows(job_id)
        if not rows and (wd / "script.json").exists():
            # Heal the scene rows from script.json first, so the new row doesn't
            # become the only one the editor can see.
            store.upsert_scenes(job_id, _read_script_scenes(wd))
            rows = store.scene_rows(job_id)
        all_ids = [int(r["id"]) for r in rows]
        new_id = (max(all_ids) if all_ids else 0) + 1
        store.upsert_scene(job_id, new_id)
        rows = store.scene_rows(job_id)
    finally:
        store.close()

    gapp._persist_script_snapshot(wd, rows)
    order = _load_scene_order(wd) or all_ids
    if body.after_scene_id and body.after_scene_id in order:
        order.insert(order.index(body.after_scene_id) + 1, new_id)
    else:
        order.append(new_id)
    _save_scene_order(wd, order)
    return {"ok": True, "scene_id": new_id, "order": order}


class ReorderFilmScenesBody(BaseModel):
    work_dir: str
    order: list


@api.post("/api/films/scenes/reorder")
def reorder_film_scenes(body: ReorderFilmScenesBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    _save_scene_order(wd, [int(x) for x in body.order])
    return {"ok": True}


class ReassembleBody(BaseModel):
    work_dir: str


# One reassembly per film at a time: a user click and the automation loop's
# stale-final sweep must not interleave two ffmpeg writes to the same
# combined.mp4/final. Locks are per-work-dir and never removed (tiny).
_reassemble_locks: dict = {}
_reassemble_locks_guard = threading.Lock()


def _reassemble_lock(wd: Path) -> threading.Lock:
    with _reassemble_locks_guard:
        return _reassemble_locks.setdefault(str(wd), threading.Lock())


_REASSEMBLED_LABEL = "Rebuilt from scenes"


def _curated_final_version(wd: Path) -> dict | None:
    """The picked final-video version when it is a derived cut — an upscale, a
    localized re-voicing, a hand-burnt cover — rather than the plain concat of
    the scene parts. Reassembly rebuilds the plain concat and overwrites the
    published file, so a derived pick is work it silently throws away."""
    try:
        hist = final_video_history.history(wd)
    except Exception:
        return None
    selected = hist.get("selected")
    entry = next((v for v in (hist.get("versions") or [])
                  if int(v.get("id") or 0) == selected), None)
    if entry is None or final_video_history.is_base(entry):
        return None
    return entry


def _reassemble_film_core(wd: Path, op_name: str = "Reassembling film") -> int:
    """Concat the scene finals in display order and re-mix music/ambient into
    the published final (re-applying any standing subtitle and first-frame
    cover burns).
    Returns the scene count. Raises ValueError when the film has nothing to
    assemble; ffmpeg failures propagate as-is."""
    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()

    if not rows:
        raise ValueError("No scene data found.")

    all_ids = [int(r.get("id") or r.get("scene_id") or 0) for r in rows]
    order = _load_scene_order(wd) or all_ids

    scene_finals = [
        wd / f"scene_{sid:02d}_final.mp4"
        for sid in order
        if (wd / f"scene_{sid:02d}_final.mp4").exists()
        and (wd / f"scene_{sid:02d}_final.mp4").stat().st_size > 10_000
    ]
    if not scene_finals:
        raise ValueError("No rendered scenes found. Re-render scenes first.")

    # Music is optional: acted films carry their voices in-picture and never
    # get a score, and any film can switch music off — the final is then the
    # concatenation itself. Refusing here left those films with no way to
    # rebuild after a scene re-shoot.
    music_path = wd / "background_music.wav"
    music_on = bool(_film_job_config(wd).get("music_enabled", True)) and music_path.exists()

    final_path = gapp._final_path_for_work_dir(wd)
    combined = wd / "combined.mp4"

    jc = _film_job_config(wd)
    cfg = gapp.load_config()
    voice_vol, music_vol, ambient_vol = (v / 100.0 for v in _mix_volumes(wd, jc, cfg))

    with _reassemble_lock(wd), _track_op(op_name, wd.name):
        from pipeline.assembler import (concatenate_scenes, ensure_video_resolution,
                                        mix_background_music, _get_video_dimensions)
        from pipeline.comfyui import ltx_dimensions
        # The concat filter refuses mixed sizes, and one odd clip (e.g. a
        # re-shoot made before dimension rounding was unified) used to fail the
        # whole rebuild. Normalize stragglers to the film's grid first.
        res_name = jc.get("resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)
        vid_w, vid_h = gapp._RESOLUTIONS.get(res_name, gapp._RESOLUTIONS[gapp._DEFAULT_RESOLUTION])
        vid_w, vid_h = ltx_dimensions(vid_w, vid_h)
        for clip in scene_finals:
            try:
                if _get_video_dimensions(clip) != (vid_w, vid_h):
                    gapp.logger.info("Reassemble: normalizing %s to %dx%d",
                                     clip.name, vid_w, vid_h)
                    ensure_video_resolution(clip, vid_w, vid_h)
            except Exception:
                pass  # let the concat report it if the clip is truly broken
        concatenate_scenes(scene_finals, combined,
                           hard_boundaries=_film_hard_boundaries(wd, scene_finals))
        ambient = wd / "ambient.wav"
        if music_on:
            mix_background_music(
                combined, music_path, final_path,
                volume=music_vol,
                voice_volume=voice_vol,
                ambient_path=ambient if ambient.exists() else None,
                ambient_volume=ambient_vol,
            )
        else:
            # No score: the clips already carry their own audio.
            import shutil
            shutil.copy2(combined, final_path)
        # Same normalisation the full render applies after mixing. No-op when
        # the concat already matches.
        ensure_video_resolution(final_path, vid_w, vid_h)
        # A film whose target is an upscale-only size (QHD/4K) must not shrink
        # back to its render size on a rebuild. The fast path keeps the size
        # without re-running an AI upscale on every edit; the Remix card can
        # restore AI-upscale quality afterwards.
        # The size the render's finishing upscale actually reached, when it
        # recorded one — a factor mode ends up at its factor's size, not at the
        # requested finishing size, and restoring the latter would stretch the
        # film past what the upscaler ever produced.
        _achieved = jc.get("finish_achieved_dims")
        finish_dims = (
            (int(_achieved[0]), int(_achieved[1]))
            if isinstance(_achieved, (list, tuple)) and len(_achieved) == 2
            else gapp._UPSCALE_RESOLUTIONS.get(
                str(jc.get("finish_resolution") or "").strip())
        )
        if finish_dims and finish_dims != (vid_w, vid_h):
            from pipeline.assembler import upscale_video
            target_w, target_h = finish_dims
            actual_w, actual_h = _get_video_dimensions(final_path)
            if actual_w < target_w or actual_h < target_h:
                staged = wd / "finish_upscale.staging.mp4"
                try:
                    upscale_video(final_path, staged, target_w, target_h)
                    staged.replace(final_path)
                except Exception as e:
                    staged.unlink(missing_ok=True)
                    gapp.logger.warning(
                        "Reassemble: finishing upscale to %s failed (kept %dx%d): %s",
                        jc.get("finish_resolution"), vid_w, vid_h, e)
        # After the upscale, so open captions are drawn crisp at the target
        # size instead of being resampled with the frame.
        _maybe_burn_subtitles(wd, final_path)
        _maybe_burn_first_frame_cover(wd, final_path)
        _maybe_apply_title_cards(wd, final_path)
        # Films with kept versions: the published file is now the plain concat,
        # so say so instead of leaving the manifest pointing at the upscale (or
        # other derived cut) this just replaced. Films with no history — almost
        # all of them — stay untouched rather than gaining a 40 MB copy.
        if (final_video_history.history(wd).get("versions") or []):
            final_video_history.record_or_replace(
                wd, final_path, label=_REASSEMBLED_LABEL,
                lang=gapp._norm_tts_language(jc.get("tts_language")),
            )
    return len(scene_finals)


@api.post("/api/films/reassemble")
def reassemble_film(body: ReassembleBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")

    # Read the pick before the rebuild overwrites the published file, so the
    # answer can say what the fresh cut replaced.
    curated = _curated_final_version(wd)
    try:
        scene_count = _reassemble_film_core(wd)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Reassembly failed: {str(e).splitlines()[0][:200]}")

    note = ""
    if curated:
        note = (f"This replaced the picked cut “{curated['label']}” with a fresh build of the "
                f"scene parts. Pick it again under Versions to get it back.")
    return {
        "ok": True,
        "final_url": _busted_file_url(gapp._final_path_for_work_dir(wd)),
        "scene_count": scene_count,
        "note": note,
    }


# ── stale-final auto-reassembly ──────────────────────────────────────────────
# Editing a rendered film (per-scene re-renders, take picks, reorders, adds,
# deletes) refreshes the scene parts but leaves the published final untouched
# until "Reassemble film" is clicked — easy to forget, and the stale final is
# indistinguishable in the player. The automation loop sweeps finished films
# and reassembles any whose parts are newer than the final, once the film has
# gone quiet: no edit task running, nothing publishing, and the parts stable
# for a few minutes so a mid-session film isn't churned between every tweak.

_REASSEMBLE_QUIET_S = 300  # parts must stop changing this long before a sweep

# work dir → the parts mtime we last announced a curated-final skip for, so the
# every-tick sweep says it once per round of edits instead of every 15 seconds.
_curated_skips: dict = {}


def _film_edit_busy(wd: Path) -> bool:
    """True while an edit-screen task or an in-flight publish touches this film
    — either makes rewriting the final right now unsafe."""
    swd = str(wd)
    for tid, tmeta in list(_film_task_meta.items()):
        if tmeta.get("work_dir") != swd:
            continue
        task = _film_tasks.get(tid)
        if isinstance(task, dict) and task.get("status") == "running":
            return True
    for task in list(_upload_tasks.values()):
        if isinstance(task, dict) and task.get("status") == "uploading" \
                and task.get("work_dir") == swd:
            return True
    entry = pq.item_by_work_dir(swd)
    if entry:
        for platform in ("youtube", "x"):
            if (entry.get(platform) or {}).get("status") == "publishing":
                return True
    return False


def _stale_final_films() -> list:
    """Finished films whose scene parts or display order are newer than the
    published final, stable for _REASSEMBLE_QUIET_S, and not busy."""
    now = time.time()
    stale = []
    for _label, wd in gapp._list_recent_jobs(max_results=50):
        p = Path(wd)
        try:
            meta = json.loads((p / "job.json").read_text())
        except Exception:
            continue
        if meta.get("status") != "done":
            continue
        final_path = gapp._final_path_for_work_dir(p)
        if not final_path.exists():
            continue
        try:
            newest = max((f.stat().st_mtime for f in p.glob("scene_*_final.mp4")),
                         default=0.0)
            order_file = p / "scene_edit_order.json"
            if order_file.exists():
                newest = max(newest, order_file.stat().st_mtime)
            # Styles that burn the cover into the opening (job_config
            # "first_frame_cover": "image") show cover.png inside the final
            # itself, so regenerating / re-selecting the cover also makes the
            # final stale — the rebuild re-burns the current cover.
            if str(_film_job_config(p).get("first_frame_cover") or "").strip().lower() == "image":
                cover = p / "cover.png"
                if cover.exists() and cover.stat().st_size > 1000:
                    newest = max(newest, cover.stat().st_mtime)
            if not newest or newest <= final_path.stat().st_mtime + 1.0:
                continue  # final already reflects the parts
        except OSError:
            continue
        if now - newest < _REASSEMBLE_QUIET_S:
            continue  # still being edited — wait for the film to go quiet
        if _film_edit_busy(p):
            continue
        curated = _curated_final_version(p)
        if curated:
            # The published cut is an upscale / localized re-voicing / hand-burnt
            # cover, none of which a rebuild from the scene parts can reproduce.
            # Unattended, that is silent data loss — leave it for the button.
            # The sweep runs every tick, so say it once per round of edits.
            if _curated_skips.get(str(p)) != newest:
                _curated_skips[str(p)] = newest
                gapp.logger.info(
                    "Skipping auto-reassembly of %s: the published cut is “%s”, "
                    "which rebuilding from the scene parts would discard. "
                    "Use “Reassemble film” to rebuild it anyway.",
                    p.name, curated.get("label"))
            continue
        _curated_skips.pop(str(p), None)
        stale.append(p)
    return stale


def _reassemble_stale_finals() -> None:
    for wd in _stale_final_films():
        try:
            count = _reassemble_film_core(wd, op_name="Auto-reassembling film")
            gapp.logger.info(
                "Auto-reassembled %s (%d scenes; parts were newer than the final)",
                wd.name, count)
        except ValueError:
            pass  # nothing assemblable (no rows/music) — same films the button rejects
        except Exception as exc:
            gapp.logger.warning("Auto-reassembly failed for %s: %s", wd.name, exc)


class RerenderSceneBody(BaseModel):
    work_dir: str
    component: str  # "narration", "image", or "video"
    instruction: str = ""   # optional "tell it how" steering (image/video re-render)


def _render_scene_narration(task_id: str, wd: Path, sid: int, jc: dict, row: dict,
                            voice_name: str | None = None,
                            language: str | None = None,
                            tts_engine_override: str | None = None,
                            out_dir: Path | None = None,
                            record_video_history: bool = True,
                            tts_host: str | None = None,
                            update_task: bool = True) -> None:
    from pipeline.assembler import FINAL_SCENE_TAIL_SECS, mux_video_audio
    from pipeline.tts_worker import generate_narration, resolve_robotic_amount

    # out_dir scopes the output to a language-specific subdirectory (localize
    # feature) instead of the film's canonical top-level files; the raw scene
    # video is always read from wd itself, since it's shared across languages.
    target_dir = out_dir or wd
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
    narration_path = target_dir / f"scene_{sid:02d}_narration.wav"
    final_path = target_dir / f"scene_{sid:02d}_final.mp4"
    cfg = gapp.load_config()

    _film_checkpoint(task_id)
    narration_text = (row.get("narration") or row.get("title") or f"Scene {sid}").strip()
    # Spoken-text override (metadata.tts_text) — original cut only: it is
    # authored for the original narration language, so localized re-voicing
    # (language/out_dir set) sticks to the translated narration text.
    if language is None and out_dir is None:
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        override = str(meta.get("tts_text") or "").strip()
        if override:
            narration_text = override
    selected_voice = voice_name if voice_name is not None else _scene_voice_name(row, jc)
    voice_ref = _voice_ref_for_name(selected_voice)
    if voice_ref is None and not selected_voice:
        voice_ref_str = jc.get("voice_ref") or ""
        voice_ref = Path(voice_ref_str).expanduser() if voice_ref_str else None
    voice_robotic_amount = resolve_robotic_amount(jc)  # 0 = natural; legacy toggle honored
    tts_engine = tts_engine_override or jc.get("tts_engine", cfg.get("default_tts_engine", "openf5"))
    tts_language = language or jc.get("tts_language", cfg.get("default_tts_language", "en"))
    # Re-derive the speed for the ACTUAL voice speaking this scene: a per-scene
    # voice differs from the narrator the job's stored multiplier was derived
    # for, and cadence measurements may have sharpened since the render.
    cadence_voice = selected_voice or jc.get("default_voice", "")
    voice_speed = cadence.resolve_voice_speed({
        "voice": cadence_voice,
        "tts_engine": tts_engine,
        "voice_cadence_wpm": jc.get("voice_cadence_wpm", 0),
        "voice_speed": jc.get("voice_speed", cfg.get("default_voice_speed", 1.0)),
    })
    if tts_host is None:
        # tts_host lets fanout callers spread scenes across the TTS fleet; the
        # single-scene paths pick the first *reachable* worker so a downed lead
        # worker (e.g. s1) doesn't block narration when others are up.
        tts_host = _first_live_tts_host(cfg)

    if update_task:
        _film_tasks[task_id] = {"status": "running", "step": "narration", "scene_id": sid}
    generate_narration(narration_text, narration_path, reference_wav=voice_ref, host=tts_host, robotic_amount=voice_robotic_amount, speed=voice_speed, tts_engine=tts_engine, language=tts_language,
                       sentence_pause=gapp._norm_tts_sentence_pause(jc.get("tts_sentence_pause", cfg.get("default_tts_sentence_pause"))),
                       cadence_voice=(cadence_voice if tts_language == jc.get("tts_language", "en") else None))

    video_path = wd / f"scene_{sid:02d}_video.mp4"
    clip_path = wd / f"scene_{sid:02d}_clip_01.mp4"
    actual_video = (
        video_path if (video_path.exists() and video_path.stat().st_size > 10_000)
        else clip_path if (clip_path.exists() and clip_path.stat().st_size > 10_000)
        else None
    )
    if actual_video:
        _film_checkpoint(task_id)
        if update_task:
            _film_tasks[task_id]["step"] = "mux"
        staged_final = target_dir / f"scene_{sid:02d}_final.staging.mp4"
        # The closing scene holds its last frame past the narration so the film
        # doesn't cut dead on its last word — the full render does this too. The
        # tail lives inside the scene part, so re-rendering the closing scene
        # without it makes the ending abrupt from the next reassembly onwards.
        is_last = _last_scene_id(wd) == int(sid)
        mux_video_audio(actual_video, narration_path, staged_final,
                        extra_tail_secs=FINAL_SCENE_TAIL_SECS if is_last else 0.0)
        staged_final.replace(final_path)
        if record_video_history:
            video_history.record(wd, sid, final_path)


def _run_narration_rerender(task_id: str, wd: Path, sid: int, jc: dict, row: dict,
                            instruction: str = "") -> None:
    """Background thread: re-render narration then re-mux the scene.

    (instruction is accepted for a uniform worker signature but unused — narration
    is audio-only; steering applies to the image/video re-renders.)"""
    try:
        _render_scene_narration(task_id, wd, sid, jc, row)

        _film_tasks[task_id] = {"status": "done"}
    except Exception as e:
        (wd / f"scene_{sid:02d}_final.staging.mp4").unlink(missing_ok=True)
        _finish_film_task_error(task_id, e)


def _film_scene_image_prompt(jc: dict, row: dict, cfg: dict, wd: Path,
                             engine: dict | None = None) -> tuple[str, list[Path]]:
    """Styled image prompt + character reference images for a film-scene re-render.

    Mirrors _generate_active_scene_preview so a frame regenerated from the edit
    video screen keeps its recurring characters' looks: re-inject each featured
    character's canonical appearance into the prompt, gather their reference
    images, and (FLUX.2) bind each name to its reference so the render actually
    follows the uploaded look. work_dir folds in the script's own per-script
    characters, not just the catalogue ones the style opted into. The style
    prefix is applied last, matching the plain (character-less) re-render path."""
    style_name = jc.get("style_name", "")
    base_prompt = (row.get("image_prompt") or "").strip()
    if not base_prompt:
        # An acted scene is written through its fields, not an image prompt —
        # compose the frame from its setting (the same fallback every other
        # painter of an acted scene's frame uses).
        base_prompt = performance_mode.opening_frame_prompt(
            row.get("metadata") if isinstance(row.get("metadata"), dict) else {})
    base_prompt, reference_images = gapp._characters_prompt_and_refs(
        base_prompt, row, cfg, style_name, wd, engine=engine)
    style_clean = (jc.get("style") or "").strip().rstrip(".")
    if style_clean and base_prompt and not base_prompt.startswith(style_clean):
        base_prompt = f"{style_clean}. {base_prompt}"
    return base_prompt, reference_images


def _run_image_rerender(task_id: str, wd: Path, sid: int, jc: dict, row: dict,
                        instruction: str = "") -> None:
    """Background thread: re-render first-frame image only (no video)."""
    import shutil
    import secrets
    from pipeline.comfyui import generate_with_engine, ltx_dimensions

    cfg = gapp.load_config()
    engine = gapp.engines.resolve(cfg, gapp.style_settings(cfg, jc.get("style_name", "")).get("image_engine"))
    pool = _shared_edit_render_pool()
    if pool is None:
        _film_tasks[task_id] = {"status": "error", "error": "No ComfyUI workers reachable."}
        return

    first_frame = wd / f"scene_{sid:02d}_first_frame.png"
    preview = wd / f"scene_{sid:02d}_preview.png"

    try:
        _film_checkpoint(task_id)
        image_prompt, reference_images = _film_scene_image_prompt(jc, row, cfg, wd, engine)
        # One-off user steering from the Re-generate popover (not persisted).
        image_prompt = gapp._apply_prompt_instruction(image_prompt, instruction)

        resolution = jc.get("resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)
        vid_w, vid_h = gapp._RESOLUTIONS.get(resolution, (int(jc.get("vid_width", 832)), int(jc.get("vid_height", 480))))
        vid_w, vid_h = ltx_dimensions(vid_w, vid_h)

        new_seed = secrets.randbelow(2 ** 32)
        _film_tasks[task_id] = {"status": "running", "step": "image"}
        url = _acquire_render_worker(pool, task_id)
        try:
            # acquire() can block a long time behind a busy GPU — re-check
            # before submitting work the film may no longer exist to receive.
            _film_checkpoint(task_id)
            generate_with_engine(
                engine,
                image_prompt or row.get("title") or f"Scene {sid}",
                first_frame,
                width=vid_w, height=vid_h,
                seed=new_seed,
                reference_images=reference_images,
                comfy_url=url,
            )
        finally:
            pool.release(url)

        if first_frame.exists():
            shutil.copy2(first_frame, preview)
            image_history.record(wd, sid, preview)

        preview_mtime = int(preview.stat().st_mtime) if preview.exists() else int(time.time())
        preview_url = f"/api/file?path={preview}&t={preview_mtime}" if preview.exists() else ""
        _film_tasks[task_id] = {"status": "done", "preview_url": preview_url}
    except Exception as e:
        _finish_film_task_error(task_id, e)


def _image_matches_resolution(path: Path, width: int, height: int) -> bool:
    """True only if *path* is a readable image with exactly width×height pixels."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size == (width, height)
    except Exception:
        return False


def _resolve_video_for_job(cfg: dict, jc: dict) -> dict:
    """Video engine for a work-dir job: the job-config snapshot wins, then the
    style's current settings — including the optional per-style ``video_steps``
    override (0 = engine default)."""
    ss = gapp.style_settings(cfg, jc.get("style_name") or "")
    return gapp.engines.resolve_video(
        {"video_steps": jc.get("video_steps") or ss.get("video_steps")},
        jc.get("video_engine") or ss.get("video_engine"))


def _chain_scenes_for_job(cfg: dict, jc: dict) -> bool:
    """Whether a work-dir job's scenes are chained H3 clips.

    Re-rendering one scene must chain exactly as the original render did, or
    the take comes back at roughly half the length and desyncs the film."""
    ss = gapp.style_settings(cfg, jc.get("style_name") or "")
    flag = jc.get("h3_chain_scenes")
    if flag is None:
        flag = ss.get("h3_chain_scenes")
    return (bool(flag)
            and _resolve_video_for_job(cfg, jc).get("family") == "minimax")


def _acted_silent_cfg(jc: dict) -> dict:
    """The ``h3_silent_scenes`` flag for a work-dir job, as a cfg for
    performance.renders_acted: the job-config snapshot the film was rendered
    with wins, then the style's current setting (older job dirs stamp neither).
    """
    flag = jc.get("h3_silent_scenes")
    if flag is None:
        cfg = gapp.load_config()
        flag = gapp.style_settings(cfg, jc.get("style_name") or "").get("h3_silent_scenes")
    return {"h3_silent_scenes": gapp._norm_h3_silent_scenes(flag)}


def _run_video_rerender(task_id: str, wd: Path, sid: int, jc: dict, row: dict,
                        instruction: str = "") -> None:
    """Background thread: re-render video from the existing first frame → mux.

    Reuses the already-made first frame; only regenerates it when none is usable."""
    from pipeline.assembler import mux_video_audio, _get_duration
    from pipeline.comfyui import generate_with_engine, ltx_dimensions
    from pipeline.llm import Scene
    from pipeline.scene_video import generate_scene_video as gen_scene_video

    cfg = gapp.load_config()
    engine = gapp.engines.resolve(cfg, gapp.style_settings(cfg, jc.get("style_name", "")).get("image_engine"))
    pool = _shared_edit_render_pool()
    if pool is None:
        _film_tasks[task_id] = {"status": "error", "error": "No ComfyUI workers reachable."}
        return

    first_frame = wd / f"scene_{sid:02d}_first_frame.png"
    final_path = wd / f"scene_{sid:02d}_final.mp4"

    try:
        _film_checkpoint(task_id)
        # Character-consistent, styled image prompt + reference images, so a
        # regenerated first frame keeps the scene's recurring characters' looks.
        image_prompt, reference_images = _film_scene_image_prompt(jc, row, cfg, wd, engine)
        video_prompt = (row.get("video_prompt") or row.get("image_prompt") or "").strip()
        style_clean = jc.get("style", "").strip().rstrip(".")
        if style_clean and video_prompt and not video_prompt.startswith(style_clean):
            video_prompt = f"{style_clean}. {video_prompt}"
        # One-off user steering from the Re-generate popover (not persisted). Applied
        # to the motion prompt and to the first-frame prompt used if the frame is
        # regenerated below (a reused first frame is unaffected).
        video_prompt = gapp._apply_prompt_instruction(video_prompt, instruction)
        image_prompt = gapp._apply_prompt_instruction(image_prompt, instruction)

        resolution = jc.get("resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)
        vid_w, vid_h = gapp._RESOLUTIONS.get(resolution, (int(jc.get("vid_width", 832)), int(jc.get("vid_height", 480))))
        vid_w, vid_h = ltx_dimensions(vid_w, vid_h)

        # Reuse the already-made first frame instead of regenerating it. Prefer the
        # image the edit screen shows (preview_path), then any on-disk frame matching
        # the render resolution; only regenerate when none is usable.
        scene_first_frame = None
        candidates = []
        pv = (row.get("preview_path") or "").strip()
        if pv:
            candidates.append(Path(pv))
        candidates += [wd / f"scene_{sid:02d}_preview.png", first_frame]
        for p in candidates:
            if p.exists() and _image_matches_resolution(p, vid_w, vid_h):
                scene_first_frame = p
                break

        if scene_first_frame is None:
            # Keep the frame we're about to replace (it may never have been
            # recorded — e.g. painted at render time), and record the new one
            # so the version strip shows the frame this take was shot from.
            image_history.capture_current(wd, sid, first_frame)
            _film_tasks[task_id] = {"status": "running", "step": "image"}
            url = _acquire_render_worker(pool, task_id)
            try:
                _film_checkpoint(task_id)
                generate_with_engine(
                    engine,
                    image_prompt or row.get("title") or f"Scene {sid}",
                    first_frame,
                    width=vid_w, height=vid_h,
                    reference_images=reference_images,
                    comfy_url=url,
                )
            finally:
                pool.release(url)
            scene_first_frame = first_frame
            image_history.record(wd, sid, first_frame)

        # Determine narration duration from existing narration wav
        narration_path = wd / f"scene_{sid:02d}_narration.wav"
        if not narration_path.exists():
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if str((meta or {}).get("mode") or "narration") == "silent":
                # A silent scene's "narration" is a silent track of its duration
                # (same trick as the full render) so the mux path runs unchanged —
                # a silent scene added post-render never had a wav to begin with.
                from pipeline.assembler import write_silence_wav
                write_silence_wav(narration_path, float((meta or {}).get("duration") or 0) or 5.0)
            else:
                raise RuntimeError(f"Narration file missing: {narration_path.name} — re-render narration first.")
        nar_dur = _get_duration(narration_path)

        _film_tasks[task_id]["step"] = "video"
        scene = Scene(
            id=sid,
            title=row.get("title") or f"Scene {sid}",
            image_prompt=image_prompt,
            video_prompt=video_prompt,
            narration=row.get("narration") or "",
            # Per-style video negative (blank → built-in default); job_config carries
            # the value stamped at render time so a re-render stays consistent.
            negative_prompt=(jc.get("video_negative_prompt") or "").strip() or llm.NEGATIVE_PROMPT,
        )
        url = _acquire_render_worker(pool, task_id)
        try:
            # acquire() can block a long time behind a busy GPU — re-check
            # before submitting work the film may no longer exist to receive.
            _film_checkpoint(task_id)
            scene_video, _ = gen_scene_video(
                scene, wd, nar_dur, vid_w, vid_h,
                float(jc.get("max_clip_secs", 12.0)),
                float(jc.get("lora_strength", cfg.get("lora_strength", 0.5))),
                float(jc.get("first_pass_cfg", cfg.get("first_pass_cfg", 1.0))),
                int(jc.get("first_pass_steps", cfg.get("first_pass_steps", 8))),
                float(jc.get("second_pass_cfg", cfg.get("second_pass_cfg", 3.0))),
                int(jc.get("second_pass_steps", cfg.get("second_pass_steps", 6))),
                url,
                scene_first_frame if scene_first_frame.exists() else None,
                gapp.engines.resolve(cfg, jc.get("image_engine")
                                     or gapp.style_settings(cfg, jc.get("style_name") or "").get("image_engine")),
                video_engine=_resolve_video_for_job(cfg, jc),
                chained=_chain_scenes_for_job(cfg, jc),
            )
        finally:
            pool.release(url)

        _film_checkpoint(task_id)
        _film_tasks[task_id]["step"] = "mux"
        # Mux to a staging file and swap it in atomically, so an interrupted mux
        # (backend restart / crash) never destroys the existing final.mp4 — the
        # scene keeps its old video until the new one is fully written. The staging
        # name keeps a .mp4 extension so ffmpeg infers the container format.
        staged_final = wd / f"scene_{sid:02d}_final.staging.mp4"
        mux_video_audio(scene_video, narration_path, staged_final)
        staged_final.replace(final_path)
        video_history.record(wd, sid, final_path)
        # The fresh single clip + final supersede any legacy multi-clip artifacts.
        for legacy in (f"scene_{sid:02d}_video.mp4", f"scene_{sid:02d}_clip_02.mp4"):
            (wd / legacy).unlink(missing_ok=True)

        _film_tasks[task_id] = {"status": "done"}
    except Exception as e:
        (wd / f"scene_{sid:02d}_final.staging.mp4").unlink(missing_ok=True)
        _finish_film_task_error(task_id, e)


def _run_acted_rerender(task_id: str, wd: Path, sid: int, jc: dict, row: dict,
                        instruction: str = "") -> None:
    """Background thread: re-render an ACTED scene as one H3 Ref2VA generation.

    Acted scenes carry their own voices in-picture, so there is no
    scene_NN_narration.wav for the classic worker (_run_video_rerender) to mux
    — the whole scene is regenerated from its cast portraits and lines."""
    from pipeline.llm import Scene
    from resume_generation import render_performance_scene

    cfg = gapp.load_config()
    md = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    final_path = wd / f"scene_{sid:02d}_final.mp4"

    try:
        pool = _shared_edit_render_pool()
        if pool is None:
            raise RuntimeError("No ComfyUI workers reachable.")

        from pipeline.comfyui import ltx_dimensions
        resolution = jc.get("resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)
        vid_w, vid_h = gapp._RESOLUTIONS.get(
            resolution, (int(jc.get("vid_width", 832)), int(jc.get("vid_height", 480))))
        # The full render rounds to the models' ×64 grid (1920×1080 → 1920×1024);
        # a re-shoot at the raw size concats against the original scenes with a
        # mismatched height and the whole reassembly fails.
        vid_w, vid_h = ltx_dimensions(vid_w, vid_h)

        scene = Scene(
            id=sid,
            title=row.get("title") or "",
            image_prompt=row.get("image_prompt") or "",
            video_prompt=row.get("video_prompt") or "",
            narration=row.get("narration") or "",
            mode=md.get("mode") or "dialogue",
            lines=[ln for ln in (md.get("lines") or []) if isinstance(ln, dict)],
            metadata_extra=md,
        )
        # Live config UNDER the job's overrides — the same merge the full render
        # uses (resume_generation.load_job_config). job_config.json alone has no
        # "styles" list, and without the style hierarchy a catalogue character
        # scoped to a parent style (e.g. BHOB's David Attenbot re-shot from a
        # child style's film) resolves NO portrait and Ref2VA refuses the scene.
        scene_cfg = {**cfg, **jc}
        scene_cfg["style_name"] = jc.get("style_name") or ""

        _film_checkpoint(task_id)
        _film_tasks[task_id] = {"status": "running", "step": "acted scene"}
        url = _acquire_render_worker(pool, task_id)
        try:
            # Guided re-generation: the user's note steers this take only, as
            # the prompt's [DIRECTION] block.
            render_performance_scene(scene, wd, scene_cfg, comfy_url=url,
                                     vid_width=vid_w, vid_height=vid_h,
                                     style_name=scene_cfg["style_name"],
                                     direction=instruction)
        finally:
            pool.release(url)

        _film_checkpoint(task_id)
        video_history.record(wd, sid, final_path)
        # The fresh final supersedes any legacy narration-style artifacts.
        for legacy in (f"scene_{sid:02d}_video.mp4", f"scene_{sid:02d}_clip_01.mp4",
                       f"scene_{sid:02d}_clip_02.mp4"):
            (wd / legacy).unlink(missing_ok=True)

        _film_tasks[task_id] = {"status": "done"}
    except Exception as e:
        _finish_film_task_error(task_id, e)


_RERENDER_JOURNAL_PATH = _ACTIVITY_LOG_PATH.parent / "rerender_journal.json"
_rerender_journal_lock = threading.Lock()


def _load_rerender_journal() -> list[dict]:
    try:
        data = json.loads(_RERENDER_JOURNAL_PATH.read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_rerender_journal_locked(entries: list[dict]) -> None:
    _RERENDER_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _RERENDER_JOURNAL_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(_RERENDER_JOURNAL_PATH)


def _journal_rerender_add(tid: str, wd: Path, sid: int, component: str, instruction: str) -> None:
    with _rerender_journal_lock:
        entries = [e for e in _load_rerender_journal() if e.get("task_id") != tid]
        entries.append({"task_id": tid, "work_dir": str(wd), "scene_id": sid,
                        "component": component, "instruction": instruction or "",
                        "created_at": time.time()})
        _save_rerender_journal_locked(entries)


def _journal_rerender_remove(tid: str) -> None:
    with _rerender_journal_lock:
        entries = _load_rerender_journal()
        kept = [e for e in entries if e.get("task_id") != tid]
        if len(kept) != len(entries):
            _save_rerender_journal_locked(kept)


def _run_rerender_logged(target, tid: str, wd: Path, sid: int, component: str, jc: dict, row: dict,
                         instruction: str = "") -> None:
    """Run a re-render worker, then record a completion entry in the Activity log.

    The workers only update _film_tasks (so the live "Re-rendering…" indicator can
    read their step), so this wrapper adds the "Recent" history entry that
    _track_op gives every other operation."""
    started = time.time()
    try:
        target(tid, wd, sid, jc, row, instruction)
    finally:
        # Terminal (done, error, or cancelled) — the journal only requeues
        # tasks the process died under, never ones that finished.
        _journal_rerender_remove(tid)
        _film_cancelled_tids.discard(tid)
        end = time.time()
        status = (_film_tasks.get(tid) or {}).get("status")
        if status == "error":
            name, st = f"Re-render failed — scene {sid}", "error"
        elif status == "cancelled":
            name, st = f"Re-render cancelled — scene {sid}", "cancelled"
        else:
            name, st = f"Re-rendered scene {sid}", "done"
            # Learn this op's duration so the next re-render predicts its ETA.
            # Acted scenes render as one H3 generation and take far longer
            # than an LTX clip — keep their timings out of the "video" average.
            timing_key = "dialogue" if target is _run_acted_rerender else component
            w, h = _film_job_dims(str(wd))
            film_timing.record(f"rerender_{timing_key}", end - started, width=w, height=h)
        # Full grouped/persisted history entry (under the film), like every other
        # op — not a bare dict that also truncated the shared log to 20 rows.
        with _op_lock:
            _append_activity_locked(
                name, component, end, started,
                work_dir=str(wd), status=st, category="film",
            )


def _start_scene_rerender(wd: Path, sid: int, component: str, instruction: str = "") -> str:
    """Validate and dispatch one scene re-render worker thread; returns the task id.

    Shared by the endpoint and the startup requeue of journaled re-renders, so it
    raises ValueError (bad request) / LookupError (unknown scene) instead of HTTP
    errors."""
    if component not in ("narration", "image", "video"):
        raise ValueError(f"Unknown component: {component!r}")

    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()

    row = next((r for r in rows if int(r.get("id") or r.get("scene_id") or 0) == sid), None)
    if not row:
        raise LookupError(f"Scene {sid} not found.")

    scene_meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    scene_mode = str((scene_meta or {}).get("mode") or "narration")
    if component == "narration" and scene_mode != "narration":
        # Dialogue audio is voiced per line by the Video render; silent scenes
        # have no voice-over at all — voicing the scene here would be wrong.
        raise ValueError(f"A {scene_mode} scene has no scene narration — "
                         "use Video to re-render its shots.")

    jc = _film_job_config(wd)

    # Delete stale files for the component and its dependents
    if component == "narration":
        # Don't delete anything up front: generate_narration overwrites the wav,
        # and the new final.mp4 is swapped in atomically (see _run_narration_rerender).
        # Pre-deleting final.mp4 would leave the scene with no video if the re-mux is
        # interrupted (backend restart / crash mid-render). Keep the current video as a
        # take so the re-mux can be reverted.
        video_history.capture_current(wd, sid, wd / f"scene_{sid:02d}_final.mp4")
    elif component == "image":
        # Preserve the current images before deleting them so the user can
        # return to them — both canonical files, since the two can differ (a
        # continuation handoff rewrites the first frame alone) and
        # capture_current skips content that is already kept.
        image_history.capture_current(wd, sid, wd / f"scene_{sid:02d}_preview.png")
        image_history.capture_current(wd, sid, wd / f"scene_{sid:02d}_first_frame.png")
        for f in [f"scene_{sid:02d}_first_frame.png", f"scene_{sid:02d}_preview.png"]:
            (wd / f).unlink(missing_ok=True)
    elif component == "video":
        # Keep the existing first frame AND the existing video. The new clip/final
        # are rendered to staging paths and swapped in atomically only on success
        # (see _run_video_rerender). Deleting the old video here would lose it if the
        # render is interrupted mid-flight — e.g. a backend restart while the LTX
        # render runs — leaving the scene with no video at all. Snapshot the current
        # video as a take so the user can flip back to it.
        video_history.capture_current(wd, sid, wd / f"scene_{sid:02d}_final.mp4")

    # A fresh re-render supersedes any earlier attempt shown on this scene —
    # clear a stale "failed" badge and stop terminal records accumulating.
    _clear_finished_film_tasks(str(wd), sid)

    tid = f"rerender_{sid:02d}_{component}_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": component}
    _film_task_meta[tid] = {"work_dir": str(wd), "scene_id": sid, "component": component}

    if component == "narration":
        target = _run_narration_rerender
    elif component == "image":
        target = _run_image_rerender
    elif performance_mode.renders_acted({"metadata": scene_meta},
                                        _acted_silent_cfg(jc)):
        # Acted scenes re-render whole: one H3 generation carrying its own
        # sound (there is no first frame or narration wav to mux). Silent
        # scenes shot on Ref2VA come back the same way — re-shooting one down
        # the classic path would look nothing like the take in the cut.
        target = _run_acted_rerender
    else:
        target = _run_video_rerender
    # Persist the intent before the thread starts, so a restart in any window
    # of the render requeues it (_run_rerender_logged clears it when terminal).
    _journal_rerender_add(tid, wd, sid, component, instruction)
    threading.Thread(
        target=_run_rerender_logged,
        args=(target, tid, wd, sid, component, jc, row, instruction),
        daemon=True,
    ).start()
    return tid


@api.post("/api/films/scenes/{scene_id}/rerender")
def rerender_film_scene(scene_id: int, body: RerenderSceneBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    try:
        tid = _start_scene_rerender(wd, scene_id, body.component, body.instruction)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "task_id": tid}


def _resume_interrupted_rerenders() -> None:
    """Requeue scene re-renders a dead backend left in the journal.

    Runs once at startup. Entries are re-dispatched through the normal path (new
    task id, fresh journal entry) so they queue on the shared pool like any other
    re-render; entries whose film or scene vanished are dropped."""
    entries = _load_rerender_journal()
    if not entries:
        return
    for e in entries:
        _journal_rerender_remove(str(e.get("task_id") or ""))
    for e in entries:
        wd = Path(str(e.get("work_dir") or ""))
        try:
            sid = int(e.get("scene_id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid <= 0 or not wd.is_dir() or not _safe_under(wd, gapp.OUTPUT_DIR):
            continue
        component = str(e.get("component") or "")
        try:
            tid = _start_scene_rerender(wd, sid, component, str(e.get("instruction") or ""))
            gapp.logger.info("Requeued interrupted re-render %s (%s scene %s, %s)",
                             tid, wd.name, sid, component)
        except Exception as exc:
            gapp.logger.warning("Could not requeue re-render for %s scene %s: %s",
                                wd.name, sid, exc)


class FilmPreviewSelectBody(BaseModel):
    work_dir: str
    version_id: int


@api.post("/api/films/scenes/{scene_id}/preview-select")
def select_film_preview(scene_id: int, body: FilmPreviewSelectBody) -> dict:
    """Make a previously-kept image version the selected one for a film scene."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    try:
        out = image_history.select(wd, int(scene_id), int(body.version_id))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        store.update_scene_preview(job_id, int(scene_id), out)
    finally:
        store.close()
    return {"ok": True, "preview_path": str(out),
            "history": image_history.history(wd, int(scene_id))}


@api.post("/api/films/scenes/{scene_id}/video-select")
def select_film_video(scene_id: int, body: FilmPreviewSelectBody) -> dict:
    """Make a previously-kept video take the selected one for a film scene."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    try:
        out = video_history.select(wd, int(scene_id), int(body.version_id))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "final_path": str(out),
            "video_history": video_history.history(wd, int(scene_id))}


@api.post("/api/films/scenes/{scene_id}/preview-delete")
def delete_film_preview(scene_id: int, body: FilmPreviewSelectBody) -> dict:
    """Delete a kept image version for a film scene (the one in use can't be deleted)."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    try:
        hist = image_history.delete(wd, int(scene_id), int(body.version_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "history": hist}


@api.post("/api/films/scenes/{scene_id}/video-delete")
def delete_film_video(scene_id: int, body: FilmPreviewSelectBody) -> dict:
    """Delete a kept video take for a film scene (the one in use can't be deleted)."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    try:
        hist = video_history.delete(wd, int(scene_id), int(body.version_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "video_history": hist}


class FilmPreviewUploadBody(BaseModel):
    work_dir: str
    filename: str = ""
    data: str


@api.post("/api/films/scenes/{scene_id}/preview-upload")
def upload_film_preview(scene_id: int, body: FilmPreviewUploadBody) -> dict:
    """Use the user's own image (pasted or a file) as a scene's first frame.

    Saved as PNG at the film's render size (cover-crop when it differs) so the
    classic I2V re-render accepts it as frame zero; an acted take rides it as
    its opening-composition reference either way. The replaced image is kept as
    a history version."""
    import shutil
    from PIL import Image, ImageOps
    from pipeline.comfyui import ltx_dimensions

    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    sid = int(scene_id)
    raw = _decode_image(body.data)

    first_frame = wd / f"scene_{sid:02d}_first_frame.png"
    preview = wd / f"scene_{sid:02d}_preview.png"
    # Preserve the images we're about to overwrite so the user can return to
    # them (both — they can differ, and capture skips content already kept).
    image_history.capture_current(wd, sid, preview)
    image_history.capture_current(wd, sid, first_frame)

    jc = _film_job_config(wd)
    resolution = jc.get("resolution") or ""
    dims = gapp._RESOLUTIONS.get(resolution)
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            if dims:
                tw, th = ltx_dimensions(*dims)
                if im.size != (tw, th):
                    im = ImageOps.fit(im, (tw, th))
            im.save(first_frame, "PNG")
    except Exception as e:
        raise HTTPException(400, f"Could not read that image: {e}")
    shutil.copy2(first_frame, preview)

    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        store.update_scene_preview(job_id, sid, preview)
    finally:
        store.close()
    image_history.record(wd, sid, preview)
    return {"ok": True, "preview_path": str(preview),
            "history": image_history.history(wd, sid)}


class FilmTrimBody(BaseModel):
    work_dir: str
    end_seconds: float


# Shortest a trimmed scene may be — below this the clip is a flash, and a stray
# slider drag shouldn't be able to destroy a scene's video.
_MIN_TRIM_SECONDS = 0.5


@api.post("/api/films/scenes/{scene_id}/trim")
def trim_film_scene(scene_id: int, body: FilmTrimBody) -> dict:
    """Cut the tail off a rendered scene clip (video and audio together).

    The trimmed cut is recorded as a new video take, so the untrimmed one stays
    one click away in the takes strip."""
    from pipeline.assembler import _get_duration, trim_video

    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    sid = int(scene_id)
    final_path = wd / f"scene_{sid:02d}_final.mp4"
    if not final_path.exists():
        raise HTTPException(400, "This scene has no rendered video yet.")

    end = float(body.end_seconds)
    try:
        duration = _get_duration(final_path)
    except Exception as e:
        raise HTTPException(503, f"Could not read the scene clip: {str(e).splitlines()[0][:200]}")
    if end < _MIN_TRIM_SECONDS:
        raise HTTPException(400, f"Keep at least {_MIN_TRIM_SECONDS:g}s of the scene.")
    if end >= duration - 0.05:
        raise HTTPException(400, f"That is the full clip ({duration:.1f}s) — drag the handle back to trim.")

    # Snapshot the current final first, so the untrimmed take survives even if
    # it was never recorded as a take (a full render writes finals directly).
    video_history.capture_current(wd, sid, final_path)
    staged = wd / f"scene_{sid:02d}_final.staging.mp4"
    try:
        with _track_op("Trimming scene", f"scene {sid} · {end:.1f}s", work_dir=str(wd)):
            trim_video(final_path, staged, end)
            staged.replace(final_path)
    except Exception as e:
        staged.unlink(missing_ok=True)
        raise HTTPException(503, f"Trim failed: {str(e).splitlines()[0][:300]}")
    return {"ok": True, "duration": end,
            "video_history": video_history.record(wd, sid, final_path)}


class FilmContinueBody(BaseModel):
    work_dir: str
    seconds: float | None = None
    direction: str = ""
    lines: list | None = None


def _run_scene_continue(task_id: str, wd: Path, sid: int, jc: dict, row: dict,
                        body: FilmContinueBody) -> None:
    """Background thread: shoot MORE of an acted scene and join it on.

    The continuation is rendered to its own file and only swapped into the cut
    once the join succeeds, so a failure anywhere leaves the film exactly as it
    was — and the take it started from is kept either way."""
    from pipeline.assembler import _concat_video_chunks
    from pipeline.comfyui import context_latent_name
    from pipeline.llm import Scene
    from resume_generation import continue_performance_scene

    started = time.time()
    cfg = gapp.load_config()
    md = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    final_path = wd / f"scene_{sid:02d}_final.mp4"
    ctx = scene_context.load(wd, sid) or {}
    staged = wd / f"scene_{sid:02d}_final.staging.mp4"
    clip = None

    try:
        pool = _shared_edit_render_pool()
        if pool is None:
            raise RuntimeError("No ComfyUI workers reachable.")
        worker = str(ctx.get("comfy_url") or "")
        if worker not in pool.urls:
            raise RuntimeError(
                f"The worker that shot this take ({worker or 'unknown'}) is not "
                "available — a continuation can only be rendered where the take's "
                "motion context is saved. Bring it back, or re-shoot the scene.")

        from pipeline.comfyui import ltx_dimensions
        resolution = jc.get("resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)
        vid_w, vid_h = gapp._RESOLUTIONS.get(
            resolution, (int(jc.get("vid_width", 832)), int(jc.get("vid_height", 480))))
        vid_w, vid_h = ltx_dimensions(vid_w, vid_h)

        lines = performance_mode.norm_lines(body.lines or [])
        seconds = float(body.seconds or 0) or performance_mode.render_seconds({"lines": lines})
        scene = Scene(
            id=sid,
            title=row.get("title") or "",
            image_prompt=row.get("image_prompt") or "",
            video_prompt=row.get("video_prompt") or "",
            narration=row.get("narration") or "",
            mode=md.get("mode") or "dialogue",
            lines=[ln for ln in (md.get("lines") or []) if isinstance(ln, dict)],
            metadata_extra=md,
        )
        # Live config UNDER the job's overrides — the same merge the full render
        # uses, so a catalogue character scoped to a parent style still resolves.
        scene_cfg = {**cfg, **jc}
        scene_cfg["style_name"] = jc.get("style_name") or ""

        _film_checkpoint(task_id)
        _film_tasks[task_id] = {"status": "running", "step": "continuing"}
        url = _acquire_render_worker_only(pool, task_id, worker)
        try:
            clip = continue_performance_scene(
                scene, wd, scene_cfg, comfy_url=url, vid_width=vid_w, vid_height=vid_h,
                style_name=scene_cfg["style_name"], ctx=ctx, lines=lines,
                seconds=seconds, direction=body.direction or "")
        finally:
            pool.release(url)

        _film_checkpoint(task_id)
        # Keep the shorter take before the join — "that went on too long" is the
        # other half of "that finished too early".
        video_history.capture_current(wd, sid, final_path)
        _concat_video_chunks([final_path, clip], staged)
        staged.replace(final_path)
        clip.unlink(missing_ok=True)

        # The scene now ENDS where this clip ends: the next continuation carries
        # on from the context this one saved, and the note is re-bound to the
        # longer cut.
        index = int(ctx.get("next_index") or 2)
        scene_context.save(wd, sid,
                           latent=context_latent_name(str(ctx.get("token") or ""), index),
                           next_index=index + 1)
        scene_context.stamp_final(wd, sid, final_path)
        video_history.record(wd, sid, final_path)
        if lines:
            _append_scene_lines(wd, sid, lines)

        _film_tasks[task_id] = {"status": "done"}
        _log_continue_activity(wd, sid, started, "done")
    except Exception as e:
        staged.unlink(missing_ok=True)
        if clip is not None:
            clip.unlink(missing_ok=True)
        _finish_film_task_error(task_id, e)
        _log_continue_activity(wd, sid, started,
                               (_film_tasks.get(task_id) or {}).get("status") or "error")


def _log_continue_activity(wd: Path, sid: int, started: float, status: str) -> None:
    """The Recent entry every other film operation gets."""
    name = {"done": f"Continued scene {sid}",
            "cancelled": f"Continue cancelled — scene {sid}"}.get(
                status, f"Continue failed — scene {sid}")
    with _op_lock:
        _append_activity_locked(name, "continue", time.time(), started,
                                work_dir=str(wd), status=status, category="film")


def _append_scene_lines(wd: Path, sid: int, lines: list[dict]) -> None:
    """Add what was just said to the scene's own dialogue.

    The continuation is spoken in the film, so it belongs in the script the
    captions and the description are written from — a scene whose video says
    more than its text is a scene whose subtitles stop early.
    """
    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        current = store.get_scene(job_id, sid) or {}
        meta = dict(current.get("metadata") or {})
        meta["lines"] = [*(meta.get("lines") or []), *lines]
        acted = performance_mode.acted_meta({"metadata": meta, "lines": meta["lines"]})
        meta.update({k: acted[k] for k in ("cast", "seconds", "beats")})
        store.upsert_scene(
            job_id, sid,
            title=current.get("title") or "",
            image_prompt=current.get("image_prompt") or "",
            # The stored prompt is a preview of the take that was shot; the
            # continuation was prompted separately and must not overwrite it.
            video_prompt=current.get("video_prompt") or "",
            narration=performance_mode.spoken_text(acted),
            preview_path=current.get("preview_path", ""),
            metadata=meta,
        )
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    gapp._persist_script_snapshot(wd, rows)


def _acquire_render_worker_only(pool, task_id: str, url: str) -> str:
    """_acquire_render_worker for work that can run on ONE named worker."""
    task = _film_tasks.get(task_id)
    if isinstance(task, dict):
        task["queued"] = True
    try:
        return pool.acquire(only=url)
    finally:
        task = _film_tasks.get(task_id)
        if isinstance(task, dict):
            task.pop("queued", None)


@api.post("/api/films/scenes/{scene_id}/continue")
def continue_film_scene(scene_id: int, body: FilmContinueBody) -> dict:
    """Shoot a few more seconds of an acted scene, continuing the same take."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    sid = int(scene_id)
    final_path = wd / f"scene_{sid:02d}_final.mp4"
    if not final_path.exists():
        raise HTTPException(400, "This scene has no rendered video yet.")
    if not scene_context.load(wd, sid):
        raise HTTPException(
            400, "This take was shot before continuations were possible — "
                 "shoot the scene again and it can be continued from then on.")
    if not scene_context.continuable(wd, sid, final_path):
        raise HTTPException(
            400, "The clip in the cut is not the take the continuation point "
                 "belongs to (a different take was selected, or this one was "
                 "trimmed). Shoot the scene again to continue from it.")

    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    row = next((r for r in rows if int(r.get("id") or r.get("scene_id") or 0) == sid), None)
    if not row:
        raise HTTPException(404, f"Scene {sid} not found.")
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    if not performance_mode.is_performance_mode((meta or {}).get("mode")):
        raise HTTPException(400, "Only acted (dialogue) scenes can be continued.")

    _clear_finished_film_tasks(str(wd), sid)
    tid = f"continue_{sid:02d}_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": "continuing"}
    _film_task_meta[tid] = {"work_dir": str(wd), "scene_id": sid, "component": "continue"}
    threading.Thread(
        target=_run_scene_continue,
        args=(tid, wd, sid, _film_job_config(wd), row, body),
        daemon=True,
    ).start()
    return {"ok": True, "task_id": tid}


class FilmInpaintBody(BaseModel):
    work_dir: str
    mask: str
    prompt: str
    denoise: float | None = None


@api.post("/api/films/scenes/{scene_id}/inpaint")
def inpaint_film_scene(scene_id: int, body: FilmInpaintBody) -> dict:
    """Masked FLUX edit of a rendered film scene's image (Film editor)."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    sid = int(scene_id)
    base = wd / f"scene_{sid:02d}_first_frame.png"
    if not base.exists():
        base = wd / f"scene_{sid:02d}_preview.png"
    if not base.exists():
        raise HTTPException(400, "This scene has no image to edit yet.")
    edit = (body.prompt or "").strip()[:700]
    if not edit:
        raise HTTPException(400, "Describe the change to make.")

    jc = _film_job_config(wd)
    style_clean = (jc.get("style") or "").strip().rstrip(".")
    # Lead with the edit (it gets the most weight) and trail the style for coherence.
    prompt = f"{edit}. {style_clean}" if style_clean else edit
    cfg = gapp.load_config()
    engine = gapp.engines.resolve(cfg, gapp.style_settings(cfg, jc.get("style_name", "")).get("edit_engine"))

    job_id = job_id_from_work_dir(wd)
    try:
        with _track_op("Editing image", f"scene {sid} · {engine['key']}") as op_id:
            return _run_scene_inpaint(wd, sid, base, prompt, body.mask, job_id, engine,
                                      denoise=body.denoise, op_id=op_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"Image edit failed: {str(e).splitlines()[0][:300]}")


@api.get("/api/films/task")
def film_task_status(task_id: str = Query(...)) -> dict:
    task = _film_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found.")
    return {"ok": True, **task}


@api.get("/api/films/tasks")
def film_tasks_for_work_dir(work_dir: str = Query(...)) -> dict:
    """Re-render tasks for a film so the edit page can restore state after a
    reload (the task ids live only in client state otherwise): running ones
    resume their spinner, and recently-failed ones re-surface their error so a
    failure that happened while the user was on another screen isn't lost —
    otherwise the scene silently shows its old frame/clip."""
    wd = str(Path(work_dir))
    now = time.time()
    out = []
    for tid, meta in list(_film_task_meta.items()):
        if meta.get("work_dir") != wd:
            continue
        task = _film_tasks.get(tid)
        if not task:
            continue
        status = task.get("status")
        if status == "running":
            out.append({
                "task_id": tid,
                "scene_id": meta.get("scene_id"),
                "component": meta.get("component"),
                "status": "running",
                "step": task.get("step", ""),
            })
        elif status == "error" and now - task.get("finished_at", 0) < _FILM_ERROR_TTL_S:
            out.append({
                "task_id": tid,
                "scene_id": meta.get("scene_id"),
                "component": meta.get("component"),
                "status": "error",
                "error": task.get("error", "Re-render failed"),
            })
    return {"ok": True, "tasks": out}


@api.post("/api/films/delete")
def delete_film(body: JobActionBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    out = gapp.OUTPUT_DIR.resolve()
    try:
        wd_res = wd.resolve()
    except OSError:
        raise HTTPException(400, "Invalid path.")
    # Only allow deleting a direct child of the videos directory.
    if not body.work_dir or wd_res == out or wd_res.parent != out:
        raise HTTPException(400, "Refusing to delete outside the videos directory.")
    # Stop running scene re-renders before their files vanish — same bug class
    # as the orphaned resume_generation.py in /api/jobs/delete (PR #76), but
    # these are in-process threads, so they are flagged rather than SIGTERMed.
    _cancel_film_tasks(wd)
    _finalize_publish_entry_on_delete(wd)
    import shutil
    if wd.exists():
        shutil.rmtree(wd, ignore_errors=True)
    canonical = gapp.OUTPUT_DIR / f"{wd.name}.mp4"
    if canonical.exists():
        canonical.unlink(missing_ok=True)
    return {"ok": True, "deleted": wd.name}


# ── Automation (Gap 2): on-demand step endpoints + opt-in background loop ──────

def _predicted_views_for_item(it: dict, cfg: dict) -> float:
    """Predicted 3-day views for a pending item — same model and inputs as the
    Queue page's per-item badges. -1 when the model is unavailable."""
    try:
        style_name = (it.get("gen_style_name") or "").strip()
        ss = gapp.style_settings(cfg, style_name)
        res = it.get("gen_resolution") or ss.get("resolution") or gapp._DEFAULT_RESOLUTION
        r = eng.predict(it.get("final_title") or it.get("title") or "",
                        it.get("video_prompt") or it.get("comment_text") or "",
                        str(res).startswith("Portrait"),
                        channel=_engagement_channel("", style_name))
        if r.get("available"):
            return float(r.get("predicted_views") or 0)
    except Exception:
        pass
    return -1.0


def _ordered_pending(cfg: dict) -> list[dict]:
    """Pending queue items in consumption order: the sort picked on the Queue
    page (queue_sort_order). The top of that view is the next video to start.
    "queue" (default) and unknown values keep the manual file order; sorts are
    stable, so ties fall back to it too."""
    pending = [q for q in yt.load_queue() if q.get("status") == "pending"]
    mode = cfg.get("queue_sort_order") or "queue"
    if mode == "newest":
        pending.sort(key=lambda q: -(q.get("created_at") or 0))
    elif mode == "oldest":
        pending.sort(key=lambda q: q.get("created_at") or 0)
    elif mode == "interest":
        pending.sort(key=lambda q: -(q["interestingness"] if q.get("interestingness") is not None else -1.0))
    elif mode == "views":
        pending.sort(key=lambda q: -_predicted_views_for_item(q, cfg))
    elif mode == "fastest":
        _attach_render_estimates(pending)
        pending.sort(key=lambda q: q.get("est_seconds", float("inf")))
    return pending


_MAX_AUTO_RETRIES = 3


def _retryable_failed(cfg: dict) -> dict | None:
    """Next failed queue item automation should retry. Only consulted when no
    pending item is startable, so retries never delay fresh work. Renders are
    resumable (finished scenes are skipped), so a retry is usually cheap. Each
    attempt stamps retry_count before launching; after _MAX_AUTO_RETRIES the
    item stays failed for a human to look at — a deterministic failure would
    otherwise retry forever."""
    failed = [q for q in yt.load_queue()
              if q.get("status") == "failed"
              and int(q.get("retry_count") or 0) < _MAX_AUTO_RETRIES]
    # Review gate on (for the item's OWN style): only retry items the user
    # approved — the same rule fresh starts follow. (A started render stamps
    # approved=True, so any item that ran and failed already qualifies.)
    failed = [q for q in failed
              if gapp.automation_settings(cfg, (q.get("gen_style_name") or "").strip())["auto_approve_script"]
              or (q.get("approved") and q.get("script_ready")
                  and q.get("work_dir") and q.get("video_job_id"))]
    if not failed:
        return None
    failed.sort(key=lambda q: q.get("updated_at") or q.get("created_at") or 0)
    item = {**failed[0]}  # fresh copy
    item["retry_count"] = int(item.get("retry_count") or 0) + 1
    yt.update_queue_item(item["id"], retry_count=item["retry_count"])
    return item


def _auto_format(cfg: dict, style_name: str = "") -> str:
    """The style's default film format (Settings → Styles → Default format;
    the Global baseline under Settings → Automation) — globally, or per style
    where a style overrides it.

    The Create screen starts its Format picker on this default (a human can
    still switch it per film); unattended runs have nobody to ask, so they
    film in it as-is."""
    return gapp.automation_settings(cfg, style_name)["auto_format"]


def _auto_song_needed(cfg: dict, style_name: str = "") -> bool:
    """Is the song step part of this style's automation? Music videos only."""
    auto = gapp.automation_settings(cfg, style_name)
    return auto["auto_format"] == "song" and auto["auto_song"]


def _auto_song_first(cfg: dict, *, title: str, topic: str, minutes: float,
                     style_name: str, n_scenes: int, queue_item_id: str) -> dict:
    """Do unattended what the Song tab does by hand: write the song, QC the
    lyrics, render the track, and re-voice it — into a fresh work dir the story
    is then drafted FROM.

    Order is the point, not convenience. A music video's scene windows are
    timed against the REAL track and each singing take has its stretch of it
    pinned in (audio-driven H3, resume_generation.scene_track_audio), so the
    track has to exist before the story is divided. Left to the render, the
    music task runs alongside the video tasks and the takes are generated with
    no track to sing to.

    Returns the song's ``{"work_dir", "job_id"}``. Raises on failure — the
    caller leaves the queue item pending and tries again next tick."""
    auto = gapp.automation_settings(cfg, style_name)
    drafted = song_draft(SongDraftBody(
        video_title=title, topic=topic, minutes=minutes, style_name=style_name,
        voice=auto["auto_song_voice"],
        n_scenes=n_scenes, queue_item_id=queue_item_id))
    wd = Path(drafted["work_dir"])

    # Lyric QC before the track is rendered: a bad song caught here costs one
    # LLM call, caught after the render it costs a worker slot and a re-voice.
    passes = auto["auto_song_critic_passes"]
    if passes:
        data = json.loads((wd / "song.json").read_text())
        secs = float(data.get("seconds") or 0)
        for _ in range(passes):
            issues = story_mode.critique_song(
                data, secs, topic=topic, video_title=title)
            if not issues:
                break
            gapp.logger.info("Song critic: revising %s — %s", wd.name, issues[:120])
            try:
                revised = story_mode.write_song(
                    None, secs,
                    language=gapp._norm_tts_language(
                        gapp.style_settings(cfg, style_name).get("tts_language")),
                    topic=topic, video_title=title,
                    # The critic judges words, not casting — the vocalist the
                    # draft cast stays pinned through every re-write.
                    singer_note=(data.get("vocalist") or "").strip(),
                    instruction=issues)
            except Exception:
                gapp.logger.warning("Song rewrite failed — keeping the draft",
                                    exc_info=True)
                break
            data.update(revised)
            (wd / "song.json").write_text(json.dumps(data, indent=2))

    _do_song_generate(wd)

    # Re-voice the finished track as a library voice (seed-vc). The engine's
    # own vocalist is kept as a version either way, so this is recoverable
    # from the Song tab.
    voice = auto["auto_song_voice"]
    if voice and auto["auto_song_revoice"]:
        try:
            _do_song_convert(wd, voice)
        except Exception:
            gapp.logger.warning("Auto re-voice as %s failed — keeping the sung "
                                "original", voice, exc_info=True)
    return {"work_dir": str(wd), "job_id": drafted["job_id"]}


def _song_hold_released(q: dict, auto: dict) -> bool:
    """Is a parked song's review hold lifted for this item?

    Parking is a hold, not a state an item is stuck in for good: a style that
    starts auto-approving its songs releases the ones already waiting on that
    flag, and the story is then drafted from the track already on disk rather
    than from a second one nobody asked for. A style that has since left the
    song format behind keeps its parked items for the Song tab — song approval
    has nothing to say about a film that is no longer a music video."""
    return bool(q.get("song_parked") and q.get("work_dir")
                and auto["auto_format"] == "song"
                and auto["auto_song"] and auto["auto_song_approve"])


def _auto_write_scripts(cfg: dict) -> int:
    """Write — but DON'T render — a script for every pending queue item that
    lacks one, leaving it unapproved so the user can review / edit / approve it
    before it renders. This is the "prepare and park" mode: it only fills in
    missing scripts and never starts a render (script generation is an LLM call,
    not a GPU job, so it can even run while a render is in progress).

    Independent of auto-start: with auto-start also on (review mode), the user
    approves a parked script and the next tick renders it. Items keep their
    queue position — each script is linked in place. Returns the count written.

    Every flag is resolved against the ITEM's style, so one style can prepare
    scripts (or music videos) while another is left alone."""
    written = 0
    for q in _ordered_pending(cfg):
        if q.get("script_ready") and q.get("work_dir") and q.get("video_job_id"):
            continue  # already has a parked script
        item_id = q.get("id")
        title = q.get("final_title", "")
        style_name = (q.get("gen_style_name") or "").strip()
        auto = gapp.automation_settings(cfg, style_name)
        released = _song_hold_released(q, auto)
        if q.get("song_parked") and not released:
            continue  # its song is waiting in the Song tab to be reviewed
        if not auto["auto_write_scripts"]:
            continue  # this style prepares nothing unattended
        ss = gapp.style_settings(cfg, style_name)
        minutes = _queue_item_minutes(q, ss)
        topic = q.get("video_prompt") or title
        resolution = q.get("gen_resolution") or ss.get("resolution") or gapp._DEFAULT_RESOLUTION
        fmt = auto["auto_format"]
        song_wd = ""
        try:
            if fmt == "song" and auto["auto_song"]:
                # Music video: the song comes first and the story follows it.
                # A released hold already has its track — carry on from that one
                # instead of singing a second. If its folder has since been
                # deleted there is nothing to carry on from, so it sings again.
                if released and Path(q["work_dir"]).is_dir():
                    song_wd = q["work_dir"]
                else:
                    song = _auto_song_first(
                        cfg, title=title, topic=topic, minutes=minutes,
                        style_name=style_name, n_scenes=gapp.style_video_scenes(ss),
                        queue_item_id=item_id)
                    song_wd = song["work_dir"]
                if not auto["auto_song_approve"]:
                    # Song review gate: stop here. Nothing — story, scenes or
                    # render — gets built on a song nobody has heard yet. The
                    # Song tab's "Draft the story" is the human continuation,
                    # and it links the script back to this slot from the brief.
                    cur = next((x for x in yt.load_queue() if x.get("id") == item_id), None)
                    if cur and cur.get("status") == "pending":
                        yt.update_queue_item(
                            item_id, video_job_id=song["job_id"], work_dir=song_wd,
                            song_parked=True, script_ready=False, approved=False,
                            suggested_minutes=round(minutes, 2),
                            gen_resolution=resolution, gen_style_name=ss["name"])
                    continue
            gen = _do_script_generate(GenerateScriptBody(
                video_title=title, topic=topic, minutes=minutes, resolution=resolution,
                style_name=style_name, format=fmt, work_dir=song_wd,
                n_scenes=gapp.style_video_scenes(ss) if fmt == "song" else 0,
                auto_critic=auto["auto_critic"]))
        except Exception:
            continue
        # Re-check the slot is still pending before attaching: script generation
        # takes ~45s, in which a manual "Render now" (or a concurrent claim) could
        # have flipped the item to "creating" and written its own script. Stamping
        # our work_dir/job over a rendering item would corrupt it — and queue_from_job
        # would otherwise spawn a duplicate row. If it's no longer pending, drop the
        # just-written script rather than risk that.
        cur = next((x for x in yt.load_queue() if x.get("id") == item_id), None)
        if not cur or cur.get("status") != "pending":
            continue
        # Park it: link the script and mark it ready, but leave approved=False so
        # neither this nor _auto_start_best renders it until the user approves.
        yt.update_queue_item(
            item_id, video_job_id=gen["job_id"], work_dir=gen["work_dir"],
            # The song hold, if there was one, is spent: the story is written.
            script_ready=True, approved=False, song_parked=False,
            suggested_minutes=round(minutes, 2),
            suggested_scene_count=len(gen.get("scenes") or []) or None,
            gen_style=gen.get("style", ""), gen_resolution=resolution,
            gen_voice=ss.get("voice", ""),
            gen_music=gen.get("music_desc", ""), gen_style_name=gen.get("style_name", ""))
        written += 1
    return written


def _auto_start_best() -> dict | None:
    if gapp._is_job_running():
        return None
    cfg = gapp.load_config()

    def _startable(q: dict) -> bool:
        """Can automation start THIS item, under its own style's rules?"""
        auto = gapp.automation_settings(cfg, (q.get("gen_style_name") or "").strip())
        if not auto["auto_start_job"]:
            return False   # this style's films wait to be started by hand
        if (q.get("song_parked") and not q.get("script_ready")
                and not _song_hold_released(q, auto)):
            # Its song is parked for review: the film waits for the song to be
            # approved — by hand in the Song tab, or by the style itself
            # switching to auto-approve — however freely it approves scripts.
            return False
        if auto["auto_approve_script"]:
            # Auto-approve on: render it end-to-end, writing the script first
            # when the item doesn't have one.
            return True
        # Review gate on: only render an item the user has explicitly approved
        # (and that has a written script). A script being present is not
        # enough — approval is a separate, deliberate action. Never generate
        # scripts here either; script-less items wait for the user.
        return bool(q.get("approved") and q.get("script_ready")
                    and q.get("work_dir") and q.get("video_job_id"))

    item = next(({**q} for q in _ordered_pending(cfg) if _startable(q)), None)
    if not item:
        # Nothing fresh to start — retry a failed item before inventing new work.
        item = _retryable_failed(cfg)
    if not item and gapp.automation_enabled_anywhere(cfg, "auto_ai_ideas"):
        # Queue idle — opt-in fallback: invent an AI idea to keep the channel
        # fed. _auto_pick_suggestion picks the best unused idea, marks it used
        # (so it's closed and never re-picked), and generates a fresh batch
        # when none remain. Per style now (_auto_feed_styles): it only invents
        # ideas for styles whose OWN automation asks for them and renders them
        # unattended — a review-mode style never rides on another style's
        # auto-approve. The anywhere-check is just the cheap pre-gate.
        try:
            item = gapp._auto_pick_suggestion(cfg, discarded=_discarded_idea_titles(cfg))
        except Exception:
            item = None
    if not item:
        return None
    try:
        return _start_queue_item(item)
    except Exception:
        return None


def _claim_and_post_youtube(p: Path, jc: dict, cfg: dict) -> str | None:
    """Atomically claim a finished job for YouTube and trigger its upload.

    The job is claimed on disk (_auto_post_triggered in job.json) before its
    upload starts, so two overlapping web ticks can't upload the same video
    twice. The marker that closes the job permanently (youtube_video_id) is only
    written after the slow upload finishes, which is why a pre-upload claim is
    needed rather than relying on that marker alone. Returns the upload task_id,
    or None if the job was already claimed/posted/unfinished or it failed to
    start. Used by both the immediate auto-poster and the scheduled governor.
    """
    with gapp._auto_post_lock:
        if str(p) in gapp._auto_post_triggered:
            return None
        try:
            meta = json.loads((p / "job.json").read_text())
        except Exception:
            return None
        if (meta.get("status") != "done" or meta.get("youtube_video_id")
                or meta.get("_auto_post_triggered")):
            return None
        gapp._auto_post_triggered.add(str(p))
        gapp._write_job_meta(p, _auto_post_triggered=True)
    try:
        title = jc.get("video_title") or _video_title_for(p)
        # The description was cached at script time; only generate if missing.
        description = _cached_description(p) or _generate_and_cache_description(str(p), title)
        channel = _channel_for_work_dir(p)
        res = yt_post(PostBody(
            work_dir=str(p), title=title,
            description=description, category=_category_for_channel(cfg, channel),
            privacy=cfg.get("youtube_post_privacy", "private"),
            channel=channel, auto=True,
            # Shorts (portrait) don't take custom thumbnails — skip by default.
            include_thumbnail=not _is_portrait_film(p)))
        return res.get("task_id")
    except Exception:
        # Upload failed to start — release the claim so a later tick can retry.
        with gapp._auto_post_lock:
            gapp._auto_post_triggered.discard(str(p))
        try:
            gapp._write_job_meta(p, _auto_post_triggered=False)
        except Exception:
            pass
        return None


def _claim_and_post_x(p: Path, jc: dict, require_yt_link: bool) -> str | None:
    """Atomically claim a finished job for X and trigger its post (issue #107).

    A job is X-posted once (claimed via the ``_x_auto_post_triggered`` job-meta
    marker; ``x_tweet_id`` is the permanent "already on X" marker). When
    *require_yt_link* is set (the job also publishes to YouTube and that upload
    isn't done yet), we wait so a non-Premium X post can fall back to the fresh
    YouTube link. Returns the X post task_id, or None. Shared by the immediate
    auto-poster and the scheduled governor.
    """
    with gapp._auto_post_lock:
        try:
            meta = json.loads((p / "job.json").read_text())
        except Exception:
            return None
        if (meta.get("status") != "done" or meta.get("x_tweet_id")
                or meta.get("_x_auto_post_triggered")):
            return None
        if require_yt_link and not meta.get("youtube_video_id"):
            return None  # let the YouTube upload finish first (link fallback)
        acct = _x_account_for_work_dir(p)
        # Claim it either way: with no X account there's nothing to post, and
        # marking it avoids re-checking the same job every tick.
        gapp._write_job_meta(p, _x_auto_post_triggered=True)
    if not acct:
        return None
    try:
        title = jc.get("video_title") or _video_title_for(p)
        res = x_post(XPostBody(work_dir=str(p), title=title, account=acct, auto=True))
        return res.get("task_id")
    except Exception:
        # Posting failed to even start — release the claim so a later tick retries.
        try:
            gapp._write_job_meta(p, _x_auto_post_triggered=False)
        except Exception:
            pass
        return None


def _auto_post_done() -> list[str]:
    """Auto-post finished, queue-driven jobs to YouTube that aren't posted yet.
    Immediate path: posts the moment a render finishes (no cadence). Only acts
    on videos that came from the queue."""
    cfg = gapp.load_config()
    posted: list[str] = []
    for _label, wd in gapp._list_recent_jobs(max_results=50):
        p = Path(wd)
        try:
            jc = json.loads((p / "job_config.json").read_text())
        except Exception:
            jc = {}
        if not jc.get("queue_item_id"):
            continue  # only auto-post videos that came from the queue
        if _awaiting_approval(p, cfg, _publish_source_for(jc)):
            _ensure_publish_entry(p)  # surface it in the Films tab for approval
            continue
        tid = _claim_and_post_youtube(p, jc, cfg)
        if tid:
            posted.append(tid)
    return posted


def _auto_post_x_done() -> list[str]:
    """Auto-post finished, queue-driven jobs to X (issue #107). Immediate path,
    mirrors _auto_post_done. When YouTube auto-post is ALSO on, each job waits
    for its YouTube upload so a non-Premium X post can use the fresh link."""
    cfg = gapp.load_config()
    yt_on = bool(cfg.get("youtube_auto_post"))
    posted: list[str] = []
    for _label, wd in gapp._list_recent_jobs(max_results=50):
        p = Path(wd)
        try:
            jc = json.loads((p / "job_config.json").read_text())
        except Exception:
            jc = {}
        if not jc.get("queue_item_id"):
            continue  # only auto-post videos that came from the queue
        if _awaiting_approval(p, cfg, _publish_source_for(jc)):
            _ensure_publish_entry(p)  # surface it in the Films tab for approval
            continue
        tid = _claim_and_post_x(p, jc, require_yt_link=yt_on)
        if tid:
            posted.append(tid)
    return posted


# ── Publishing scheduler (decoupled publish queue) ────────────────────────────
# When publish_schedule_enabled is on, finished videos are NOT posted the moment
# they render. They enter publish_queue.json and are released on each
# channel/account's own cadence (publish_interval_minutes / publish_daily_cap).
# Comment-driven requests bypass the cadence so requesters get a prompt reply.
# The release itself reuses the same claim helpers as the immediate path, so the
# description caching, thumbnail rules and X→YouTube-link fallback all carry over.

_PUBLISH_AUTOENQUEUE_WINDOW = 6 * 3600  # the tick only auto-adds jobs finished this recently


def _same_local_day(a: float, b: float) -> bool:
    la, lb = time.localtime(a), time.localtime(b)
    return (la.tm_year, la.tm_yday) == (lb.tm_year, lb.tm_yday)


def _publish_targets_for_job(p: Path, meta: dict) -> tuple[dict, dict]:
    """Build the YouTube + X publish sub-states for a finished job from its
    job.json meta. A platform is 'enabled' (needs publishing) only if it has a
    target and isn't already posted; otherwise it starts done/skipped."""
    channel = _channel_for_work_dir(p)
    acct = _x_account_for_work_dir(p)
    yt_done, x_done = meta.get("youtube_video_id"), meta.get("x_tweet_id")
    youtube = {
        "enabled": not yt_done, "channel": channel,
        "status": "pending" if not yt_done else "done",
        "video_id": yt_done, "url": meta.get("youtube_url"),
        "released_at": None, "published_at": None, "error": None,
    }
    x = {
        "enabled": bool(acct) and not x_done, "account": acct,
        "status": ("pending" if (acct and not x_done) else ("done" if x_done else "skipped")),
        "tweet_id": x_done, "url": meta.get("x_url"),
        "released_at": None, "published_at": None, "error": None,
    }
    return youtube, x


def _publish_source_for(jc: dict) -> str:
    """Provenance of a finished film for the publish queue: 'comment' for a
    viewer-requested video (so it can bypass cadence/approval), else 'manual'."""
    item = _queue_item_by_id(jc.get("queue_item_id", "")) or {}
    return item.get("source") or ("comment" if item.get("comment_id") else "manual")


def _enqueue_finished_for_publish(recent_only: bool = True) -> int:
    """Add finished, unpublished videos to the publish queue. *recent_only* (the
    automation-tick path) only adds jobs finished within the last few hours, so
    turning scheduling on doesn't silently swallow the whole backlog; the
    explicit 'Scan' button passes recent_only=False to import everything. An
    entry that already exists for a work dir — including one the user removed —
    is never re-added. Returns the count added."""
    now = time.time()
    added = 0
    for _label, wd in gapp._list_recent_jobs(max_results=50):
        p = Path(wd)
        try:
            meta = json.loads((p / "job.json").read_text())
        except Exception:
            continue
        if meta.get("status") != "done":
            continue
        if recent_only and (now - float(meta.get("updated_at") or 0)) > _PUBLISH_AUTOENQUEUE_WINDOW:
            continue
        jc = _film_job_config(p)
        if jc.get("archived"):
            continue  # archived films sit out the auto-publish sweep
        if pq.item_by_work_dir(str(p)) is not None:
            continue  # already queued (or explicitly removed) — never resurrect
        youtube, x = _publish_targets_for_job(p, meta)
        if not (youtube["enabled"] or x["enabled"]):
            continue  # nothing left to publish
        source = _publish_source_for(jc)
        title = jc.get("video_title") or _video_title_for(p)
        if pq.add_item(str(p), title=title, source=source,
                       queue_item_id=jc.get("queue_item_id", ""),
                       youtube=youtube, x=x):
            added += 1
    return added


def _ensure_publish_entry(p: Path) -> dict | None:
    """Return the publish-queue entry for a finished film, creating a held one if
    none exists yet. Used by the Films-tab approval, which can run before the
    automation tick has enqueued the film. Returns None if there's nothing to
    publish (not finished, or already posted everywhere)."""
    existing = pq.item_by_work_dir(str(p))
    if existing is not None:
        return existing
    try:
        meta = json.loads((p / "job.json").read_text())
    except Exception:
        return None
    if meta.get("status") != "done":
        return None
    youtube, x = _publish_targets_for_job(p, meta)
    if not (youtube["enabled"] or x["enabled"]):
        return None
    jc = _film_job_config(p)
    source = _publish_source_for(jc)
    title = jc.get("video_title") or _video_title_for(p)
    pq.add_item(str(p), title=title, source=source,
                queue_item_id=jc.get("queue_item_id", ""),
                youtube=youtube, x=x)
    return pq.item_by_work_dir(str(p))


def _awaiting_approval(p: Path, cfg: dict, source: str = "") -> bool:
    """True if the approval toggle is on and this film hasn't been approved yet.
    Comment-requested videos bypass approval (they also bypass the cadence), so
    requesters still get a prompt reply. The automation override
    (publish_auto_publish_unapproved) lets everything through without changing the
    per-film approved flag, so flipping it back off re-holds the unapproved ones."""
    if not cfg.get("publish_require_approval"):
        return False
    if cfg.get("publish_auto_publish_unapproved"):
        return False
    if source == "comment" and cfg.get("publish_schedule_skip_comment_requests", True):
        return False
    e = pq.item_by_work_dir(str(p))
    return not (e and e.get("approved"))


# Self-healing knobs for the publish queue — mirror the render-queue retry cap so
# a failed publish recovers headlessly instead of stranding the video.
_PUBLISH_STUCK_SECONDS = 1800   # 'publishing' with no id after this == failed upload
_PUBLISH_MAX_ATTEMPTS = 3       # genuine upload retries before giving up


def _youtube_channel_connected(channel: str) -> bool:
    """Cheap (60s-cached) check used to gate scheduled releases and to tell a
    dead token apart from a real upload failure. Never blocks publishing on a
    flaky check — assume connected on error."""
    try:
        return bool(yt.check_auth_status(_client_secrets_path(), channel=channel).get("connected"))
    except Exception:
        return True


def _publish_sub_droppable(sub: dict, plat: str, cfg: dict, now: float) -> bool:
    """True when a platform sub of a deleted film can be forgotten: nothing was
    published, and any release attempt is past its channel/account's cadence
    spacing — so dropping it can't refund a slot and burst-release the backlog
    (the 2026-08-21 bug _seed_last_releases guards against)."""
    if sub.get("status") == "done":
        return False
    ts = sub.get("released_at") or sub.get("published_at") or 0
    if not ts or sub.get("status") not in ("publishing", "error"):
        return True   # never released — seeds no clock
    listed, keyf = (("youtube_channels", "channel") if plat == "youtube"
                    else ("x_accounts", "account"))
    iv = _interval_minutes_for(cfg, listed, sub.get(keyf) or "")
    return (now - ts) >= iv * 60


def _reconcile_publish_queue() -> None:
    """Sync each entry's platform sub-state from job.json — the async upload
    threads write youtube_video_id / x_tweet_id when they finish — drop entries
    whose work dir vanished (a deleted film isn't news; only real published
    history and releases still holding a cadence slot are kept), and re-pend
    uploads stuck mid-flight so the governor stops waiting on a release that
    already died."""
    cfg = gapp.load_config()
    q = pq.load_queue()
    keep: list[dict] = []
    changed = False
    for e in q:
        p = Path(e.get("work_dir", ""))
        try:
            meta = json.loads((p / "job.json").read_text())
        except Exception:
            meta = None
        yt_sub, x_sub = e.get("youtube") or {}, e.get("x") or {}
        if meta is None and e.get("work_dir") and p.exists():
            # job.json unreadable but the directory is there — a transient read
            # (e.g. racing a job.json rewrite), not a deletion. Stamping 'work
            # dir missing' here is how entries for live films got stuck in
            # error forever. Touch nothing this pass.
            keep.append(e)
            continue
        if meta is None:
            # The film's files are gone (deleted by hand, or before the delete
            # endpoints closed entries out). Keep the entry only for what still
            # matters: a published upload (history), or a fresh release attempt
            # whose cadence slot must stay spent. Everything else is noise.
            now = time.time()
            has_done = any(s.get("status") == "done" for s in (yt_sub, x_sub))
            if not has_done and all(
                    _publish_sub_droppable(s, plat, cfg, now)
                    for s, plat in ((yt_sub, "youtube"), (x_sub, "x"))):
                changed = True
                continue
            keep.append(e)
            for sub, plat in ((yt_sub, "youtube"), (x_sub, "x")):
                droppable = _publish_sub_droppable(sub, plat, cfg, now)
                if sub.get("status") in ("pending", "publishing"):
                    if droppable:
                        sub.update(status="skipped")
                    else:
                        sub.update(status="error", error="work dir missing")
                    changed = True
                elif (sub.get("status") == "error" and droppable
                        and sub.get("error") == "work dir missing"):
                    sub.update(status="skipped", error=None)
                    changed = True
            continue
        keep.append(e)
        # A 'work dir missing' error on a film whose dir is readable is
        # disproven — the dir came back (a re-render reusing the name) or the
        # stamp was a transient read failure. Nothing ever published → close as
        # skipped so it leaves the schedule; ids in job.json → it did publish.
        for sub, id_key, meta_id, meta_url in (
                (yt_sub, "video_id", "youtube_video_id", "youtube_url"),
                (x_sub, "tweet_id", "x_tweet_id", "x_url")):
            if not (sub.get("status") == "error" and sub.get("error") == "work dir missing"):
                continue
            if meta.get(meta_id):
                sub.update(status="done", url=meta.get(meta_url), error=None,
                           published_at=sub.get("released_at") or time.time())
                sub[id_key] = meta.get(meta_id)
            else:
                sub.update(status="skipped", error=None)
            changed = True
        # The publish target is resolved live at upload time (_claim_and_post_*),
        # so re-resolve it here while the release is still ahead of us. An entry
        # enqueued before its style had its own channel froze the first-channel
        # fallback, and would otherwise show — and pace against — the wrong
        # channel forever.
        for sub, keyf, resolve in ((yt_sub, "channel", _channel_for_work_dir),
                                   (x_sub, "account", _x_account_for_work_dir)):
            if not (sub.get("enabled") and sub.get("status") in ("pending", "publishing")):
                continue
            live = resolve(p)
            if live and live != (sub.get(keyf) or ""):
                sub[keyf] = live
                changed = True
        if yt_sub.get("status") in ("pending", "publishing") and meta.get("youtube_video_id"):
            yt_sub.update(status="done", video_id=meta.get("youtube_video_id"),
                          url=meta.get("youtube_url"),
                          published_at=yt_sub.get("released_at") or time.time())
            changed = True
        if x_sub.get("status") in ("pending", "publishing") and meta.get("x_tweet_id"):
            x_sub.update(status="done", tweet_id=meta.get("x_tweet_id"),
                         url=meta.get("x_url"),
                         published_at=x_sub.get("released_at") or time.time())
            changed = True
        # Self-heal stalled releases: a sub stuck in 'publishing' with no id long
        # after release means its async upload died (errored, or lost to a server
        # restart). Free the claim and re-pend so the governor retries — but only
        # count the attempt when the channel is actually connected, so a dead
        # token re-pends indefinitely (drains on reconnect) instead of burning the
        # retry cap and erroring out the whole backlog.
        now = time.time()
        for plat, claim_key, id_field in (
                ("youtube", "_auto_post_triggered", "youtube_video_id"),
                ("x", "_x_auto_post_triggered", "x_tweet_id")):
            sub = e.get(plat) or {}
            if sub.get("status") != "publishing" or meta.get(id_field):
                continue
            released = sub.get("released_at") or 0
            if released and (now - released) < _PUBLISH_STUCK_SECONDS:
                continue  # still within the normal upload window — leave it
            task = _upload_tasks.get(sub.get("task_id") or "")
            if task and task.get("status") != "error":
                continue  # task still running — don't yank it
            try:
                gapp._write_job_meta(p, **{claim_key: False})
            except Exception:
                pass
            if plat == "youtube":
                with gapp._auto_post_lock:
                    gapp._auto_post_triggered.discard(str(p))
            connected = _youtube_channel_connected(sub.get("channel") or "") if plat == "youtube" else True
            attempts = int(sub.get("attempts") or 0) + (1 if connected else 0)
            if connected and attempts >= _PUBLISH_MAX_ATTEMPTS:
                sub.update(status="error", attempts=attempts,
                           error=(task or {}).get("error") or "upload failed")
            else:
                sub.update(status="pending", task_id=None, released_at=None, attempts=attempts)
            changed = True
    if changed:
        pq.save_queue(keep)


def _finalize_publish_entry_on_delete(work_dir) -> None:
    """Close out a film's publish-queue entry before its files are deleted.

    Deleting the local film doesn't undo an upload that already went out, so a
    released platform keeps its history: sync the ids the async upload wrote
    into job.json while the dir is still readable and mark the sub done.
    Everything else leaves the queue with the film — except a release attempt
    still inside its cadence spacing, which is kept (as an error) so deleting
    a film right after its release can't refund the slot and burst-release
    the backlog."""
    try:
        entry = pq.item_by_work_dir(str(work_dir))
        if not entry:
            return
        try:
            meta = json.loads((Path(work_dir) / "job.json").read_text())
        except Exception:
            meta = {}
        for plat, id_key, meta_id, meta_url in (
                ("youtube", "video_id", "youtube_video_id", "youtube_url"),
                ("x", "tweet_id", "x_tweet_id", "x_url")):
            sub = entry.get(plat) or {}
            if not sub.get("released_at") or sub.get("status") not in ("pending", "publishing"):
                continue  # never released, or already terminal — keep as recorded
            if meta.get(meta_id):
                sub.update(status="done", url=meta.get(meta_url),
                           published_at=sub.get("released_at") or time.time())
                sub[id_key] = meta.get(meta_id)
            else:
                sub.update(status="error", error="film deleted during upload")
            entry[plat] = sub
        cfg = gapp.load_config()
        now = time.time()
        if any((entry.get(plat) or {}).get("status") == "done"
               or not _publish_sub_droppable(entry.get(plat) or {}, plat, cfg, now)
               for plat in ("youtube", "x")):
            # A platform still waiting its turn can never publish now — close it
            # as skipped rather than letting it decay to 'work dir missing'.
            for plat in ("youtube", "x"):
                sub = entry.get(plat) or {}
                if sub.get("status") == "pending":
                    sub.update(status="skipped")
                    entry[plat] = sub
            pq.update_item(entry["id"], youtube=entry.get("youtube"), x=entry.get("x"))
        else:
            pq.remove_item(entry["id"])
    except Exception:
        pass


def _drop_from_publish_queue(work_dir) -> None:
    """Remove a film's publish-queue entry — used when it's published manually,
    so it leaves the queue. The auto-poster and the scheduled governor keep their
    entry instead (reconciliation moves it to 'done' history); only manual posts
    drop it. No-op if the film isn't queued."""
    try:
        entry = pq.item_by_work_dir(str(work_dir))
        if entry:
            pq.remove_item(entry["id"])
    except Exception:
        pass


def _interval_minutes_for(cfg: dict, listed: str, key: str) -> int:
    """Minutes to space releases for a channel/account key, derived from its
    publish_per_day (2/day → 720 min, evenly spaced). 0 = no throttle."""
    entry = next((c for c in (cfg.get(listed) or []) if c.get("id") == key), None) or {}
    per_day = float(entry.get("publish_per_day") or 0)
    return round(1440 / per_day) if per_day > 0 else 0


def _release_scheduled_publishes(force_id: str = "") -> dict:
    """Release due entries from the publish queue. Each platform target is gated
    independently by its channel/account cadence (publish_per_day → even
    spacing), except comment-driven requests (bypass) and an explicit *force_id*
    ('Publish now'), which ignore the cadence. At most one entry per
    channel/account is released per call, so a no-throttle backlog drains
    steadily rather than firing dozens of uploads at once.
    Returns {"youtube": [ids], "x": [ids]} of what was triggered."""
    _reconcile_publish_queue()
    cfg = gapp.load_config()
    q = pq.load_queue()
    if not q:
        return {}
    now = time.time()
    skip_comment = bool(cfg.get("publish_schedule_skip_comment_requests", True))
    require_approval = bool(cfg.get("publish_require_approval"))
    auto_pub_unapproved = bool(cfg.get("publish_auto_publish_unapproved"))

    # Seed each key's last release time from entries already released, so the
    # spacing survives restarts. A clock reset voids older releases (and holds
    # the key until its chosen next_at) — same rules the status endpoint shows.
    clock = _publish_clock()
    last = _seed_last_releases(q, clock, now)

    def _eligible(k: tuple, interval_min: int) -> bool:
        ov = clock.get(k)
        if ov and not last.get(k):  # reset pending — wait for its chosen time
            return now >= float(ov.get("next_at") or 0)
        return not (interval_min and last.get(k, 0.0) and (now - last[k]) < interval_min * 60)

    released = {"youtube": [], "x": []}
    fired: set = set()  # (plat, key) already released this call — one per key per call
    for e in _ordered_publish_queue(cfg, q):
        if force_id and e.get("id") != force_id:
            continue
        p = Path(e.get("work_dir", ""))
        jc = _film_job_config(p)
        bypass = bool(force_id) or (skip_comment and e.get("source") == "comment")
        # Approval gate: hold entries the user hasn't approved in the Films tab.
        # A bypass (comment request or explicit 'Publish now') counts as approval;
        # so does the automation override (publish_auto_publish_unapproved).
        if require_approval and not bypass and not auto_pub_unapproved and not e.get("approved"):
            continue
        yt_sub, x_sub = e.get("youtube") or {}, e.get("x") or {}
        if yt_sub.get("enabled") and yt_sub.get("status") == "pending":
            key = yt_sub.get("channel") or ""
            k = ("youtube", key)
            interval_min = _interval_minutes_for(cfg, "youtube_channels", key)
            eligible = bypass or (k not in fired and _eligible(k, interval_min))
            # Don't start a scheduled upload we know will fail on a dead token —
            # hold it pending so it drains on reconnect (the badge alerts the
            # user). An explicit 'Publish now' (bypass) still tries.
            if eligible and not bypass and not _youtube_channel_connected(key):
                eligible = False
            if eligible:
                tid = _claim_and_post_youtube(p, jc, cfg)
                if tid:
                    yt_sub.update(status="publishing", released_at=now, task_id=tid)
                    pq.update_item(e["id"], youtube=yt_sub)
                    released["youtube"].append(e["id"])
                    fired.add(k)
                    last[k] = now
        if x_sub.get("enabled") and x_sub.get("status") == "pending":
            key = x_sub.get("account") or ""
            k = ("x", key)
            interval_min = _interval_minutes_for(cfg, "x_accounts", key)
            # Still wait for the YouTube link when this video also goes to YouTube
            # (non-Premium long-video fallback) — bypass skips cadence, not this.
            require_link = bool(yt_sub.get("enabled") and yt_sub.get("status") != "done")
            # "Publish now" forces X too. But when the same call also starts the
            # YouTube upload, that upload finishes asynchronously, so X is deferred
            # here (require_link, no video_id yet) and posts on a later tick. A
            # one-shot force would then lose to cadence and sink behind the backlog
            # — X never posts "now". So make the force sticky: persist it while X
            # waits for the link, honour it on later ticks (bypassing cadence), and
            # clear it once X is released.
            x_bypass = bypass or bool(x_sub.get("force"))
            if x_bypass or (k not in fired and _eligible(k, interval_min)):
                tid = _claim_and_post_x(p, jc, require_yt_link=require_link)
                if tid:
                    x_sub.update(status="publishing", released_at=now, task_id=tid, force=False)
                    pq.update_item(e["id"], x=x_sub)
                    released["x"].append(e["id"])
                    fired.add(k)
                    last[k] = now
                elif x_bypass:
                    # Keep the force only while genuinely waiting for the YT link; a
                    # terminal failure (no account, etc.) must not be force-retried
                    # every tick — cadence and the attempt cap handle real retries.
                    want_force = bool(require_link)
                    if bool(x_sub.get("force")) != want_force:
                        x_sub.update(force=want_force)
                        pq.update_item(e["id"], x=x_sub)
    return {kk: v for kk, v in released.items() if v}


def _ensure_descriptions() -> int:
    """Cache YouTube descriptions for completed jobs that don't have one yet.
    Called from the automation loop so it runs server-side, not on browser polls."""
    cfg = gapp.load_config()
    from pipeline.llm import llm_backend_ready
    if not llm_backend_ready(cfg):
        return 0
    count = 0
    try:
        for _label, wd_str in gapp._list_recent_jobs(max_results=50):
            wd = Path(wd_str)
            if _description_path(wd).exists():
                continue
            title = _video_title_for(wd)
            try:
                _generate_and_cache_description(wd_str, title)
                count += 1
                if count >= 3:  # cap per tick to avoid long blocking
                    break
            except Exception:
                pass
    except Exception:
        pass
    return count


def _ensure_tags() -> int:
    """Cache keyword tags (tags.json) for queued/recent jobs that lack them, so a
    publish never has to wait on tag generation. Runs server-side from the
    automation loop. The publish queue (imminent videos) is warmed first, then
    recent jobs; on-demand generation at publish time is the backstop for
    anything not yet warmed."""
    cfg = gapp.load_config()
    from pipeline.llm import llm_backend_ready
    if not llm_backend_ready(cfg):
        return 0
    seen: set[str] = set()
    candidates: list[str] = []
    try:
        for e in pq.load_queue():
            wd_str = str(e.get("work_dir") or "")
            if wd_str and wd_str not in seen:
                seen.add(wd_str)
                candidates.append(wd_str)
    except Exception:
        pass
    try:
        for _label, wd_str in gapp._list_recent_jobs(max_results=50):
            if wd_str and wd_str not in seen:
                seen.add(wd_str)
                candidates.append(wd_str)
    except Exception:
        pass
    count = 0
    for wd_str in candidates:
        wd = Path(wd_str)
        if not wd.exists() or _tags_path(wd).exists():
            continue
        try:
            if _generate_and_cache_tags(wd_str):
                count += 1
                if count >= 5:  # cap per tick to avoid long blocking
                    break
        except Exception:
            pass
    return count


def _automation_tick() -> dict:
    # One tick at a time: the scheduled loop, the render-finished trigger and the
    # manual endpoint can all fire one, and a tick can block for minutes on an
    # upload. The per-item claims inside each step make overlap survivable, but
    # there's no point stacking ticks — skip instead.
    if not _tick_lock.acquire(blocking=False):
        return {"skipped": "tick already running"}
    try:
        cfg = gapp.load_config()
        # Each step is gated solely by its own toggle. "Fully automated mode" is just
        # the UI shorthand for "all of these on" — it never forces a step whose own
        # flag is off, so the behaviour can't contradict what's ticked.
        out: dict = {}
        with _track_op("Automation tick"):
            # Self-heal queue rows first (e.g. fail items whose render errored)
            # so the steps below see real state. Without this, reconciliation
            # only ran on browser polls — overnight, a failed render left its
            # item "creating" forever and blocked every later start.
            try:
                _reconcile_queue()
            except Exception:
                pass
            # Comment/mention sweeps run on their own, much slower cadence than the
            # tick: each YouTube sweep costs 2+ quota units per channel, so at the
            # tick's 3-minute pace six channels alone burn most of the 10k daily
            # quota (issue observed 2026-08-29). Default one sweep per hour.
            global _last_comment_fetch
            poll_secs = max(1.0, float(cfg.get("comment_poll_minutes", 60) or 60)) * 60.0
            comments_due = time.time() - _last_comment_fetch >= poll_secs
            if comments_due and (cfg.get("youtube_auto_fetch_evaluate") or cfg.get("x_auto_fetch_evaluate")):
                _last_comment_fetch = time.time()
            if cfg.get("youtube_auto_fetch_evaluate") and comments_due and not yt.quota_blocked():
                try:
                    out["fetch"] = _fetch_and_evaluate(cfg.get("youtube_auto_approve_comments", False))
                except Exception as e:
                    out["fetch_error"] = str(e)[:120]
            if cfg.get("x_auto_fetch_evaluate") and comments_due:
                try:
                    out["x_fetch"] = _fetch_and_evaluate_x(cfg.get("x_auto_approve_comments", False))
                except Exception as e:
                    out["x_fetch_error"] = str(e)[:120]
            # Prepare-and-park: write scripts for pending items but leave them
            # unapproved. Runs before auto-start so the freshly written scripts
            # are visible to it — they stay unapproved, so review-mode auto-start
            # won't pick them up until the user approves.
            if gapp.automation_enabled_anywhere(cfg, "auto_write_scripts"):
                try:
                    out["scripts_written"] = _auto_write_scripts(cfg)
                except Exception as e:
                    out["scripts_error"] = str(e)[:120]
            if gapp.automation_enabled_anywhere(cfg, "auto_start_job"):
                out["started"] = _auto_start_best()
            # Publishing. Finished videos are enqueued in the loop regardless of
            # mode (the queue is the canonical inbox), so here we only *release*
            # them. Scheduled mode spaces releases over each channel/account's
            # cadence; immediate mode posts the moment a film finishes. The two
            # are mutually exclusive (enforced in Settings; schedule wins here as
            # a backstop so a stale config can never double-post).
            if cfg.get("publish_schedule_enabled"):
                out["released"] = _release_scheduled_publishes()
            elif cfg.get("youtube_auto_post") or cfg.get("x_auto_post"):
                if cfg.get("youtube_auto_post"):
                    out["posted"] = _auto_post_done()
                if cfg.get("x_auto_post"):
                    out["x_posted"] = _auto_post_x_done()
        return out
    finally:
        _tick_lock.release()


@api.post("/api/automation/fetch")
def automation_fetch() -> dict:
    cfg = gapp.load_config()
    return {**_fetch_and_evaluate(cfg.get("youtube_auto_approve_comments", False)),
            "comments": yt.load_comments_cache()}


@api.post("/api/automation/start")
def automation_start() -> dict:
    started = _auto_start_best()
    return {"started": started, "running": gapp._is_job_running()}


@api.post("/api/automation/post")
def automation_post() -> dict:
    return {"posted": _auto_post_done()}


@api.post("/api/automation/tick")
def automation_tick_endpoint() -> dict:
    return _automation_tick()


# Opt-in background loop: runs a tick periodically. Each automation step is gated
# by its own config toggle; with all toggles off it only keeps the publish queue
# populated (finished videos always collect there for manual publishing).
# The loop wakes often only to watch for a render finishing, so the next queue
# item chains within seconds instead of waiting out the full interval — still ONE
# engine, just an event-driven nudge on top of the scheduled cadence.
import threading  # noqa: E402

_AUTOMATION_INTERVAL = 180  # seconds between full scheduled ticks
_COMPLETION_POLL = 15       # seconds between cheap "did the render finish?" checks
_tick_lock = threading.Lock()
# Comment/mention sweeps run far less often than the tick (see _automation_tick);
# 0.0 makes the first tick after startup sweep immediately.
_last_comment_fetch = 0.0
_automation_started = False


def _automation_loop():
    last_full = 0.0
    was_running = False
    while True:
        time.sleep(_COMPLETION_POLL)
        try:
            running = gapp._is_job_running()
        except Exception:
            running = was_running
        render_finished = was_running and not running
        was_running = running
        due = time.time() - last_full >= _AUTOMATION_INTERVAL
        if not (due or render_finished):
            continue
        if due:
            last_full = time.time()
            # Always cache descriptions for completed jobs — independent of
            # automation flags and browser connections.
            try:
                if not any(t.name == "ensure_descriptions" for t in threading.enumerate()):
                    threading.Thread(target=_ensure_descriptions, daemon=True,
                                     name="ensure_descriptions").start()
            except Exception:
                pass
            # Same idea for keyword tags — warm the queue/backlog server-side so
            # publishes (especially the queued ones) don't wait on tag generation.
            try:
                if not any(t.name == "ensure_tags" for t in threading.enumerate()):
                    threading.Thread(target=_ensure_tags, daemon=True,
                                     name="ensure_tags").start()
            except Exception:
                pass
            # Keep published finals in sync with edited scene parts — always on,
            # like the publish-queue population (consistency, not automation).
            try:
                if not any(t.name == "reassemble_stale" for t in threading.enumerate()):
                    threading.Thread(target=_reassemble_stale_finals, daemon=True,
                                     name="reassemble_stale").start()
            except Exception:
                pass
        try:
            cfg = gapp.load_config()
            # The publish queue is the canonical inbox of finished videos — keep
            # it populated regardless of automation flags, so manual users see
            # their films there too. Idempotent; the 6h window caps the backlog
            # (the "Scan" button imports everything).
            try:
                _enqueue_finished_for_publish(recent_only=True)
            except Exception:
                pass
            if (any(cfg.get(k) for k in (
                    "youtube_auto_fetch_evaluate", "youtube_auto_post", "x_auto_fetch_evaluate",
                    "x_auto_post", "publish_schedule_enabled"))
                    # Per-style flags: a style overriding one on is enough to
                    # make the tick worth running, whatever the global says.
                    or gapp.automation_enabled_anywhere(cfg, "auto_start_job")
                    or gapp.automation_enabled_anywhere(cfg, "auto_write_scripts")):
                _automation_tick()
        except Exception:
            pass


def _start_automation_loop():
    global _automation_started
    if not _automation_started:
        _automation_started = True
        threading.Thread(target=_automation_loop, daemon=True).start()


# ── engagement prediction (issue #50) ────────────────────────────────────────
# Estimates first-3-day views for an idea from its title+description, trained on
# the channel's own history. The build is a slow, in-process background job — it
# loads the embedding model into THIS process, where predict()/best_times() need
# it warm — so it reuses the in-memory-task pattern from the YouTube upload above
# rather than a separate worker.

_engagement_tasks: dict = {}


class EngagementBody(BaseModel):
    title: str = ""
    description: str = ""
    is_short: bool = False
    # Which channel's model to use (issue #22): an explicit channel key wins,
    # else the channel of the named style, else the first connected channel.
    channel: str = ""
    style_name: str = ""


def _engagement_channel(channel: str = "", style_name: str = "") -> str:
    return channel or _channel_for_style(style_name)


class EngagementBuildBody(BaseModel):
    channel: str = ""


def _run_engagement_build(task_id: str, channel: str) -> None:
    """Background thread: fetch history → embed → train → evaluate → persist."""
    def phase(p: str) -> None:
        _engagement_tasks[task_id] = {"status": "building", "phase": p}

    try:
        with _track_op("Building engagement model", channel):
            result = eng.build(_client_secrets_path(), on_phase=phase, channel=channel)
    except Exception as e:
        _engagement_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:240]}
        return
    if not result.get("available"):
        _engagement_tasks[task_id] = {
            "status": "error",
            "error": result.get("error", "Could not build a model."),
            "result": result,
        }
        return
    _engagement_tasks[task_id] = {"status": "done", "result": result}


@api.post("/api/engagement/build")
def engagement_build(body: EngagementBuildBody | None = None) -> dict:
    # Reject a second build while one runs — avoids loading two embedders at once.
    if any(t.get("status") == "building" for t in _engagement_tasks.values()):
        raise HTTPException(409, "A model build is already in progress.")
    channel = _engagement_channel((body.channel if body else "") or "")
    task_id = uuid.uuid4().hex[:12]
    _engagement_tasks[task_id] = {"status": "building", "phase": "fetching"}
    threading.Thread(target=_run_engagement_build, args=(task_id, channel), daemon=True).start()
    return {"ok": True, "task_id": task_id}


@api.get("/api/engagement/build/status")
def engagement_build_status(task_id: str = Query(...)) -> dict:
    task = _engagement_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Build task not found.")
    return {"ok": True, **task}


@api.get("/api/engagement/status")
def engagement_status(channel: str = Query("")) -> dict:
    return eng.status(channel=_engagement_channel(channel))


@api.post("/api/engagement/predict")
def engagement_predict(body: EngagementBody) -> dict:
    return eng.predict(body.title, body.description, body.is_short,
                       channel=_engagement_channel(body.channel, body.style_name))


@api.post("/api/engagement/best-times")
def engagement_best_times(body: EngagementBody) -> dict:
    return eng.best_times(body.title, body.description, body.is_short,
                          channel=_engagement_channel(body.channel, body.style_name))


# ── file serving (videos, previews, covers) ──────────────────────────────────

@api.get("/api/file")
def serve_file(path: str = Query(...)):
    p = Path(path)
    roots = [gapp.OUTPUT_DIR, gapp.VOICES_DIR, gapp.CONFIG_FILE.parent]
    if not _safe_under(p, *roots) or not p.exists():
        raise HTTPException(404, "File not found.")
    return FileResponse(str(p))


@api.get("/api/health")
def health() -> dict:
    return {"ok": True}


# ── serve built frontend (production) ────────────────────────────────────────

if FRONTEND_DIST.exists():
    from fastapi.staticfiles import StaticFiles

    class _SpaStaticFiles(StaticFiles):
        """index.html must never be cached: it is the pointer to the hashed
        bundle, and a browser holding a stale copy keeps loading last week's
        app after every deploy ("do you need to restart the web server?").
        The hashed assets themselves are immutable and can cache forever."""

        def file_response(self, full_path, stat_result, scope, status_code=200):
            resp = super().file_response(full_path, stat_result, scope, status_code)
            name = str(full_path)
            if name.endswith((".html", "/")) or name.endswith("index.html"):
                resp.headers["Cache-Control"] = "no-cache"
            elif "/assets/" in name.replace("\\", "/"):
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp

    api.mount("/", _SpaStaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
