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
import json
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
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
import pipeline.llm as llm  # noqa: E402
import pipeline.engagement as eng  # noqa: E402
from pipeline.llm import generate_script, generate_video_suggestions, Scene  # noqa: E402
from pipeline.orchestrator import DurableStore, job_id_from_work_dir, task_id as make_task_id, worker_id  # noqa: E402
from pipeline.timing import estimate_eta, estimate_planned_job, humanize_eta, next_worker_free_seconds  # noqa: E402
from pipeline import ui_activity  # noqa: E402

@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    # Startup: launch the opt-in background automation loop (defined near the
    # bottom of this module; the name resolves at startup, not import). Replaces
    # the deprecated @app.on_event("startup") handler.
    _start_automation_loop()
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


def _scene_to_json(row: dict) -> dict:
    sid = int(row["id"])
    preview = row.get("preview_path") or ""
    has_preview = bool(preview and Path(preview).exists())
    return {
        "id": sid,
        "title": row.get("title", ""),
        "image_prompt": row.get("image_prompt", ""),
        "video_prompt": row.get("video_prompt", ""),
        "narration": row.get("narration", ""),
        "preview_path": preview if has_preview else "",
        "has_preview": has_preview,
    }


# ── activity tracker ─────────────────────────────────────────────────────────

_op_lock = threading.Lock()
_current_op: dict = {}    # {name, detail, started_at} — cleared when done
_activity_log: list = []  # [{name, detail, ts, duration_s}], newest first, max 20


@contextmanager
def _track_op(name: str, detail: str = ""):
    started = time.time()
    with _op_lock:
        _current_op.clear()
        _current_op.update({"name": name, "detail": detail, "started_at": started})
    try:
        yield
    finally:
        end = time.time()
        with _op_lock:
            _current_op.clear()
            _activity_log.insert(0, {
                "name": name, "detail": detail,
                "ts": end, "duration_s": round(end - started, 1),
            })
            del _activity_log[20:]


# Live sub-phase labels for scene re-render tasks (keyed by _film_tasks["step"]).
_RERENDER_STEP_LABELS = {
    "narration": "recording narration",
    "image": "painting first frame",
    "video": "rendering video",
    "mux": "muxing audio",
}


# ── config ───────────────────────────────────────────────────────────────────

@api.get("/api/config")
def get_config() -> dict:
    cfg = gapp.load_config()
    return {
        "config": cfg,
        "voices": gapp.get_voice_choices(),
        # Root of every work_dir; the frontend joins it with a URL slug to
        # reconstruct a full path for deep links (issue #32).
        "videos_dir": str(gapp.OUTPUT_DIR),
        # Kept for backward compatibility (composed name strings stay canonical).
        "resolutions": list(gapp._RESOLUTIONS.keys()),
        "default_resolution": gapp._DEFAULT_RESOLUTION,
        # Structured selectors so the UI can offer an orientation + pixel toggle.
        "orientations": gapp._ORIENTATIONS,
        "pixel_tiers": [{"key": t["key"], "label": t["label"]} for t in gapp._PIXEL_TIERS],
        "default_orientation": gapp._DEFAULT_ORIENTATION,
        "default_pixels": gapp._DEFAULT_PIXELS,
    }


class ConfigUpdate(BaseModel):
    config: dict


@api.post("/api/config")
def post_config(body: ConfigUpdate) -> dict:
    cfg = gapp.load_config()
    cfg.update(body.config)
    gapp.save_config(cfg)
    return {"ok": True, "config": gapp.load_config()}


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
    return {"ok": True, "config": cfg, "voices": gapp.get_voice_choices()}


class VoiceAdd(BaseModel):
    name: str
    filename: str = ""
    data: str


class VoiceUpdate(BaseModel):
    name: str
    new_name: str | None = None
    filename: str = ""
    data: str | None = None


class VoiceDelete(BaseModel):
    name: str


@api.post("/api/voices/add")
def voices_add(body: VoiceAdd) -> dict:
    raw, ext = _decode_audio(body.data, body.filename)
    try:
        cfg = gapp.add_voice(body.name, raw, ext)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _voice_response(cfg)


@api.post("/api/voices/update")
def voices_update(body: VoiceUpdate) -> dict:
    audio, ext = None, ".wav"
    if body.data:
        audio, ext = _decode_audio(body.data, body.filename)
    try:
        cfg = gapp.update_voice(body.name, new_name=body.new_name, audio=audio, ext=ext)
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


class VoiceTest(BaseModel):
    voice: str = ""
    robotic: bool = False
    robotic_amount: float | None = None
    speed: float | None = None
    text: str = ""


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

    amount = (body.robotic_amount if body.robotic_amount is not None
              else float(gapp.style_settings(cfg).get("voice_robotic_amount", 0.35)))
    robotic = bool(body.robotic)
    speed = (body.speed if body.speed is not None
             else float(gapp.style_settings(cfg).get("voice_speed", 1.0) or 1.0))

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
        f"{voice}|{robotic}|{round(amount, 3)}|{round(speed, 3)}|{text}|{ref_stamp}".encode()
    ).hexdigest()[:16]
    out = gapp.CONFIG_FILE.parent / f"voice_test_{key}.wav"

    cached = out.exists() and out.stat().st_size > 1000
    if not cached:
        tts_hosts = cfg.get("tts_workers") or []
        tts_host = tts_hosts[0] if tts_hosts else "localhost"
        try:
            with _track_op("Testing voice", spoken):
                generate_narration(text, out, reference_wav=ref, host=tts_host,
                                   robotic=robotic, robotic_amount=amount, speed=speed)
        except Exception as e:
            raise HTTPException(503, f"Voice test failed: {str(e).splitlines()[0][:200]}")

    return {"ok": True, "url": f"/api/file?path={out}&t={int(out.stat().st_mtime)}", "cached": cached}


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
    n_scenes: int = 12
    visual_style: str | None = None
    auto_approve: bool = False
    voice: str = ""
    voice_robotic: bool = False
    resolution: str = ""
    queue_item_id: str = ""
    style_name: str = ""


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


def _do_script_generate(body: GenerateScriptBody) -> dict:
    """Run the LLM script generation and persist a durable job (mirrors
    app.on_generate_script, minus the Gradio plumbing). Synchronous: the API runs
    it inside _run_script_task; tests call it directly."""
    topic = (body.topic or "").strip() or (body.video_title or "").strip()
    if not topic:
        raise HTTPException(400, "Enter a video title or describe what you want to create.")

    cfg = gapp.load_config()
    # Style profile (issue #66): drives extra instructions + visual style here,
    # and is stamped on the job so the render step uses the same profile.
    ss = gapp.style_settings(cfg, body.style_name)
    extra = (ss.get("extra_instructions") or "").strip()
    if extra:
        topic = f"{topic}\n\n{extra}"

    style_hint = body.visual_style or ss.get("visual_style", "") or None
    display_topic = (body.video_title or "").strip() or topic.splitlines()[0][:80]
    try:
        with _track_op("Generating script", display_topic):
            scenes, music_desc, style = generate_script(
                topic, int(body.n_scenes), style_hint, (body.video_title or "").strip() or None
            )
    except Exception as e:  # surface a clean message to the client
        raise HTTPException(500, f"Script generation failed: {str(e).splitlines()[0][:300]}")

    display_title = (body.video_title or "").strip() or topic
    work_dir = gapp._script_work_dir(display_title)
    job_id = job_id_from_work_dir(work_dir)
    # Bake the visual style prefix into each image_prompt so it's visible in the
    # scene editor and consistent even if the style profile is later renamed/edited.
    # The render step guards against re-adding a prefix that's already present.
    combined_style = gapp._compose_visual_style(style, cfg, ss["name"])
    scenes_list = [
        {"id": s.id, "title": s.title,
         "image_prompt": (f"{combined_style}. {s.image_prompt}"
                          if combined_style and s.image_prompt
                          and not s.image_prompt.startswith(combined_style)
                          else s.image_prompt),
         "video_prompt": s.video_prompt, "narration": s.narration}
        for s in scenes
    ]
    gapp._persist_script_snapshot(work_dir, scenes_list)

    store = DurableStore.default()
    try:
        store.create_or_update_job(
            job_id, work_dir, display_title,
            config={"title": display_title, "video_title": (body.video_title or "").strip(),
                    "topic": topic, "phase": "script_review", "style_name": ss["name"]},
            metadata={"scene_count": len(scenes_list), "music_desc": music_desc, "style": style},
        )
        store.upsert_scenes(job_id, scenes_list)
    finally:
        store.close()

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
        "style": style,
        "style_name": ss["name"],
        "music_desc": music_desc,
        "scenes": [_scene_to_json(s) for s in scenes_list],
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
            n_scenes=len(scenes_list),
            style=style,
            resolution=body.resolution or ss.get("resolution") or gapp._DEFAULT_RESOLUTION,
            voice=body.voice or ss.get("voice", ""),
            voice_robotic=body.voice_robotic,
            music_desc=music_desc,
            queue_item_id=body.queue_item_id,
            style_name=ss["name"],
        ))
        result.update({
            "auto_approved": bool(body.auto_approve),
            "queue_item_id": queued.get("queue_item_id", ""),
            "started": queued.get("started"),
        })
    return result


@api.get("/api/scripts/load")
def load_script(work_dir: str = Query("")) -> dict:
    if not work_dir:
        raise HTTPException(400, "Choose a saved script.")
    wd = Path(work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Script path is outside the output folder.")
    script_path = wd / "script.json"
    if not script_path.exists():
        raise HTTPException(404, "No script found in the selected folder.")

    try:
        scenes_list = json.loads(script_path.read_text())
    except Exception as e:
        raise HTTPException(500, f"Could not read script: {str(e).splitlines()[0][:200]}")
    if not isinstance(scenes_list, list):
        raise HTTPException(400, "Saved script has an unexpected format.")

    job_id = job_id_from_work_dir(wd)
    fallback_title = wd.name.replace("-", " ").title()
    video_title = fallback_title
    style = ""
    music_desc = ""
    style_name = ""

    store = DurableStore.default()
    try:
        job = store.get_job(job_id)
        if job:
            d = _row_to_dict(job)
            cfg = json.loads(d.get("config_json") or "{}")
            meta = json.loads(d.get("metadata_json") or "{}")
            video_title = cfg.get("video_title") or d.get("title") or fallback_title
            style = meta.get("style", "")
            music_desc = meta.get("music_desc", "")
            style_name = cfg.get("style_name", "")
        store.create_or_update_job(
            job_id, wd, video_title,
            config={"video_title": video_title, "phase": "script_review",
                    "style_name": style_name},
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
    return {
        "job_id": job_id,
        "work_dir": str(wd),
        "title": video_title,
        "video_title": video_title,
        "style": style,
        "style_name": ss["name"],
        "music_desc": music_desc,
        "voice": ss.get("voice", ""),
        "voice_robotic": bool(ss.get("voice_robotic", False)),
        "resolution": ss.get("resolution") or gapp._DEFAULT_RESOLUTION,
        "scenes": [_scene_to_json(r) for r in rows],
    }


@api.get("/api/jobs/{job_id}/scenes")
def job_scenes(job_id: str) -> dict:
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    return {"scenes": [_scene_to_json(r) for r in rows]}


class SceneUpdate(BaseModel):
    title: str = ""
    image_prompt: str = ""
    video_prompt: str = ""
    narration: str = ""


@api.put("/api/jobs/{job_id}/scenes/{scene_id}")
def update_scene(job_id: str, scene_id: int, body: SceneUpdate) -> dict:
    # Reuse app's saver — it both upserts the DB row and rewrites script.json.
    gapp._save_active_scene(job_id, scene_id, body.title, body.image_prompt,
                            body.video_prompt, body.narration)
    return {"ok": True}


# ── scene preview (FLUX first frame) ─────────────────────────────────────────

@api.post("/api/jobs/{job_id}/scenes/{scene_id}/preview")
def regen_scene_preview(job_id: str, scene_id: int, resolution: str = "", style: str = "") -> dict:
    try:
        with _track_op("Generating preview", f"scene {scene_id}"):
            out = gapp._generate_active_scene_preview(
                job_id, int(scene_id), resolution, style, "", "", force=True
            )
    except Exception as e:
        raise HTTPException(503, f"Preview failed: {str(e).splitlines()[0][:200]}")
    return {"ok": True, "preview_path": str(out)}


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

    to_generate = rows if force else [r for r in rows if not (r.get("preview_path") and Path(r["preview_path"]).exists())]
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

    return {"scenes": [_scene_to_json(r) for r in rows],
            "generated": len(to_generate) - len(failed), "failed": failed}


# ── per-field LLM regeneration (Script tab "Re-generate" buttons) ─────────────

_FIELD_INSTRUCTIONS = {
    "title": "Write ONE short, vivid scene title (max ~8 words). Return only the title — no quotes, no label.",
    "narration": "Rewrite the narration for this scene: 2–4 sentences in an engaging documentary voice, consistent with the video topic and the surrounding scenes. Return only the narration text.",
    "image_prompt": "Write a single detailed text-to-image (FLUX) prompt for this scene's first frame: highly detailed, static, incorporating the visual style. Return only the prompt.",
    "video_prompt": "Write a single concise video-motion (LTX) prompt for this scene describing camera movement and motion. Return only the prompt.",
}


def _llm_complete(system: str, user: str, cfg: dict, max_tokens: int = 700) -> str:
    """Lightweight direct LLM call honouring the configured backend.

    NOTE: kept self-contained (stdlib urllib) rather than importing
    pipeline.llm's internals. If pipeline.llm later changes models/prompting,
    this can be unified with it.

    Raises if the model hit ``max_tokens`` before finishing, so callers that
    parse the output (e.g. JSON) fail loudly instead of silently dropping a
    truncated response.
    """
    import urllib.request
    if cfg.get("llm_backend", "local") == "claude":
        key = cfg.get("claude_api_key", "")
        if not key:
            raise RuntimeError("No Claude API key configured (Settings → LLM backend).")
        payload = json.dumps({
            "model": cfg.get("claude_model", "claude-sonnet-4-6"),
            "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=payload,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        if data.get("stop_reason") == "max_tokens":
            raise RuntimeError(
                f"LLM response was truncated at the {max_tokens}-token limit.")
        return "".join(b.get("text", "") for b in data.get("content", [])).strip()

    url = cfg.get("local_llm_url", "http://localhost:8000/v1/chat/completions")
    payload = json.dumps({
        "model": cfg.get("local_llm_model", ""),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.9, "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError(
            f"LLM response was truncated at the {max_tokens}-token limit.")
    return choice["message"]["content"].strip()


class FieldRegenBody(BaseModel):
    title: str = ""
    narration: str = ""
    image_prompt: str = ""
    video_prompt: str = ""


@api.post("/api/jobs/{job_id}/scenes/{scene_id}/regenerate-field")
def regenerate_field(job_id: str, scene_id: int, field: str = Query(...),
                     body: FieldRegenBody | None = None) -> dict:
    body = body or FieldRegenBody()
    if field not in _FIELD_INSTRUCTIONS:
        raise HTTPException(400, f"Unknown field: {field}")
    cfg = gapp.load_config()

    video_title, topic, style, style_name, outline = "", "", "", "", ""
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

    system = ("You are a documentary script writer for short, AI-generated videos. "
              "Be concise and return ONLY what the task asks for — no preamble, no labels.")
    user = (
        f"Video title: {video_title or topic}\nTopic: {topic}\nVisual style: {style}\n"
        f"Full scene outline: {outline}\n\n"
        f"Scene {scene_id} — current draft:\n"
        f"Title: {body.title}\nNarration: {body.narration}\n"
        f"Image prompt: {body.image_prompt}\nVideo prompt: {body.video_prompt}\n\n"
        f"Task: {_FIELD_INSTRUCTIONS[field]}"
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
        )
    else:
        system = ("You refine the creative-direction brief for a short, AI-generated video. "
                  "Return ONLY the improved direction text — no preamble, no labels.")
        user = (
            f"Video title: {title or '(untitled)'}\n"
            f"Current direction: {direction or '(none yet)'}\n"
            "\nImprove and sharpen the direction: clarify the angle, tone, and what to "
            "emphasise. Keep it to 1–3 sentences."
        )
    try:
        with _track_op(f"Improving {body.field}", title or direction):
            text = _llm_complete(system, user, cfg, max_tokens=300).strip().strip('"').strip()
    except Exception as e:
        raise HTTPException(503, f"Improve failed: {str(e).splitlines()[0][:200]}")
    return {"value": text}


# ── approve & generate (launches the background pipeline) ─────────────────────

class GenerateBody(BaseModel):
    job_id: str
    work_dir: str
    video_title: str = ""
    title: str = ""
    n_scenes: int = 0
    voice: str = ""
    voice_robotic: bool | None = None
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
    voice_robotic = (body.voice_robotic if body.voice_robotic is not None
                     else bool(ss.get("voice_robotic", False)))
    resolution = body.resolution or ss.get("resolution") or gapp._DEFAULT_RESOLUTION
    vid_width, vid_height = gapp._RESOLUTIONS.get(resolution, (832, 480))

    style_clean = body.style.strip().rstrip(".") if body.style and body.style.strip() else ""
    combined_style = gapp._compose_visual_style(body.style, cfg, style_name)

    n = int(body.n_scenes) if body.n_scenes else len(scene_rows)
    title = body.title or body.video_title
    scenes = [
        Scene(
            id=int(row["id"]),
            title=row.get("title") or f"Scene {int(row['id'])}",
            image_prompt=_apply_style_prefix(combined_style, row.get("image_prompt") or title),
            video_prompt=row.get("video_prompt") or row.get("image_prompt") or title,
            narration=row.get("narration") or "",
        )
        for row in scene_rows[:n]
    ]
    gapp._persist_script_snapshot(work_dir, [
        {"id": s.id, "title": s.title, "image_prompt": s.image_prompt,
         "video_prompt": s.video_prompt, "narration": s.narration} for s in scenes
    ])

    job_cfg = gapp._job_config_snapshot(cfg)
    job_cfg.update({
        "resolution": resolution, "max_clip_secs": 0,
        "default_voice": voice_name, "voice_ref": voice_ref or "",
        "voice_robotic": voice_robotic,
        "voice_robotic_amount": ss.get("voice_robotic_amount", 0.35),
        "voice_speed": ss.get("voice_speed", 1.0),
        # Per-style render quality + audio mix (issue #66): the resumable
        # worker reads these flat keys from job_config.json, so resolving them
        # here is what makes the chosen style drive the render and the mix.
        "style_name": ss["name"],
        "lora_strength": ss.get("lora_strength"),
        "first_pass_cfg": ss.get("first_pass_cfg"),
        "first_pass_steps": ss.get("first_pass_steps"),
        "second_pass_cfg": ss.get("second_pass_cfg"),
        "second_pass_steps": ss.get("second_pass_steps"),
        "music_vol": ss.get("music_vol"),
        "voice_vol": ss.get("voice_vol"),
        "ambient_vol": ss.get("ambient_vol"),
        "music_desc": body.music_desc or "", "title": title,
        "video_title": (body.video_title or "").strip(), "style": style_clean,
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
            timeout = float(cfg.get("ui_idle_timeout_seconds", ui_activity.DEFAULT_IDLE_TIMEOUT))
            reserved = 1 if (len(cfg.get("comfy_workers") or []) >= 2 and ui_activity.is_active(timeout)) else 0
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

    return {
        "pct": pct, "msg": msg, "work_dir": str(wd), "done": bool(done),
        "final_url": f"/api/file?path={final_path}" if done else "",
        "cover_url": f"/api/file?path={cover}" if cover.exists() and cover.stat().st_size > 1000 else "",
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
    import shutil
    if wd.exists():
        shutil.rmtree(wd, ignore_errors=True)
    canonical = gapp.OUTPUT_DIR / f"{wd.name}.mp4"
    if canonical.exists():
        canonical.unlink(missing_ok=True)
    return {"ok": True, "deleted": wd.name}


# ── library / recent jobs ────────────────────────────────────────────────────

@api.get("/api/jobs")
def list_jobs() -> dict:
    finished_rows = gapp._list_recent_jobs(max_results=50)
    def _cover_url(work_dir: str) -> str:
        cover = Path(work_dir) / "cover.png"
        if cover.exists() and cover.stat().st_size > 1000:
            return f"/api/file?path={cover}"
        return ""
    finished = [{"label": l, "work_dir": d, "cover_url": _cover_url(d)} for l, d in finished_rows]
    scripts = [{"label": l, "work_dir": d} for l, d in gapp._list_script_jobs()]
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


@api.get("/api/remix")
def remix_load(work_dir: str = Query("")) -> dict:
    wd = Path(work_dir) if work_dir else gapp._latest_work_dir()
    if wd is None:
        raise HTTPException(404, "No job available.")
    combined = wd / "combined.mp4"
    music = wd / "background_music.wav"
    if not combined.exists() or not music.exists():
        raise HTTPException(404, f"Required files not found in {wd.name}.")
    # Preview the actual published final (full voice/music/ambient mix) — the
    # same file Publish reads. Globbing the work dir returned combined.mp4
    # (narration only, no music) before any remix existed. See issue #14.
    final_vid = gapp._final_path_for_work_dir(wd)
    # Default the sliders to the volumes that produced that final (per-film
    # job_config, then global config) so a no-op re-mix reproduces it.
    cfg = gapp.load_config()
    jc = _film_job_config(wd)
    return {
        "work_dir": str(wd),
        "final_url": f"/api/file?path={final_vid}",
        "voice_vol": jc.get("voice_vol", cfg.get("voice_vol", 100)),
        "music_vol": jc.get("music_vol", cfg.get("music_vol", 18)),
        "ambient_vol": jc.get("ambient_vol", cfg.get("ambient_vol", 0)),
    }


@api.post("/api/remix")
def remix_apply(body: RemixBody) -> dict:
    wd = Path(body.work_dir)
    combined = wd / "combined.mp4"
    music = wd / "background_music.wav"
    ambient = wd / "ambient.wav"
    with _track_op("Remixing audio", wd.name):
        final_path, message = gapp.on_remix(
            str(combined), str(music),
            str(ambient) if ambient.exists() else "",
            voice_vol=body.voice_vol, music_vol=body.music_vol, ambient_vol=body.ambient_vol,
        )
    if not final_path:
        raise HTTPException(500, message or "Remix failed.")
    return {"message": message, "final_url": f"/api/file?path={final_path}"}


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
            n = max(6, int(it.get("suggested_scene_count") or ss.get("n_scenes") or 6))
            res = it.get("gen_resolution") or ss.get("resolution") or gapp._DEFAULT_RESOLUTION
            w, h = gapp._RESOLUTIONS.get(res, gapp._RESOLUTIONS[gapp._DEFAULT_RESOLUTION])
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
                        style: dict | None = None) -> list[dict]:
    """Generate video ideas steered by a free-text theme (e.g. 'Rock bands of
    the 90s') and, optionally, a style profile. Uses the configured LLM
    backend via _llm_complete."""
    import re
    avoid = "; ".join(previous)
    system = ("You are a content strategist for an educational/documentary YouTube channel. "
              "Return ONLY a JSON array, no prose.")
    user = (
        f'Generate {n} specific, compelling video ideas guided by this theme: "{guidance}".\n'
        f"Each must be a concrete documentary topic that clearly fits the theme.\n"
        + llm.style_suggestion_context(style)
        + (f"Avoid duplicating these existing titles: {avoid}\n" if avoid else "")
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
    return out


def _suggestion_key(title: str) -> str:
    return " ".join((title or "").strip().lower().split())


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
            # Channel titles (YouTube API + posted queue) come first; supplement with
            # any local completed jobs not yet published to the channel. Titles come
            # from the channel this style publishes to (issue #22).
            previous = gapp._channel_video_titles(cfg, style_name=target)
            seen = {t.lower() for t in previous}
            for label, _ in gapp._list_recent_jobs(max_results=500):
                if label.lower() not in seen:
                    previous.append(label)
                    seen.add(label.lower())
        except Exception:
            previous = []
        try:
            if g:
                ideas = _guided_suggestions(g, previous, cfg, style=ss)
            else:
                ideas = _normalize_suggestions(generate_video_suggestions(previous, cfg, style=ss))
        except Exception as e:
            raise HTTPException(503, f"Could not generate suggestions: {str(e).splitlines()[0][:160]}")

    try:
        ideas = [{**idea, "id": str(idea.get("id") or str(uuid.uuid4())[:8]),
                  "style_name": target,
                  "created_at": time.time(), "used": False, "dismissed": False}
                 for idea in ideas]
        # Cache per style: replace this style's set, keep the other styles'.
        try:
            others = [s for s in yt.load_suggestions()
                      if _idea_style_key(s, default_name) != target]
        except Exception:
            others = []
        yt.save_suggestions(others + ideas)
    except Exception:
        pass
    return {"suggestions": _visible_suggestions(ideas), "cached": False, "style_name": target}


class SuggestionDismissBody(BaseModel):
    id: str = ""
    title: str = ""
    reason: str = ""


@api.post("/api/youtube/suggestions/dismiss")
def dismiss_suggestion(body: SuggestionDismissBody) -> dict:
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
            changed = True
            break
    if changed:
        yt.save_suggestions(suggestions)
    return {"ok": bool(dismiss_keys), "suggestions": _visible_suggestions(suggestions)}


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
                render_pct = int(round(gapp._status_for_work_dir(wd)[0]))
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

    return {
        "render_active": render_active,
        "render_pct": render_pct,
        "queue": queue_pending,
        "youtube": attention + publishable,
        "youtube_attention": attention,
        "youtube_publishable": publishable,
        "films": films_new,
        "films_total": films_total,
    }


@api.get("/api/activity")
def get_activity() -> dict:
    """What the system is doing right now, plus recent event log."""
    with _op_lock:
        op = dict(_current_op)
        log = list(_activity_log[:10])

    # Scene re-renders run in daemon threads that record progress in _film_tasks
    # (not via _track_op), so surface the most recent running one as the current
    # op when nothing else is tracked — otherwise the Activity panel shows nothing.
    if not op:
        best = None  # (started_ts, scene_id, step)
        for key, task in list(_film_tasks.items()):
            if not key.startswith("rerender_") or task.get("status") != "running":
                continue
            parts = key.split("_")  # rerender_<sid>_<component>_<ts>
            if len(parts) < 4:
                continue
            try:
                sid, started = int(parts[1]), int(parts[3])
            except ValueError:
                continue
            if best is None or started > best[0]:
                best = (started, sid, task.get("step", ""))
        if best is not None:
            started, sid, step = best
            op = {
                "name": f"Re-rendering scene {sid}",
                "detail": _RERENDER_STEP_LABELS.get(step, step),
                "started_at": started,
            }

    render_active, render_pct, render_msg, render_title = False, 0, "", ""
    try:
        render_active = bool(gapp._is_job_running())
        if render_active:
            wd = gapp._preferred_work_dir("")
            if wd is not None:
                pct, msg = gapp._status_for_work_dir(wd)
                render_pct = int(round(pct))
                render_msg = msg
                store = DurableStore.default()
                try:
                    job_row = store.get_job_by_work_dir(str(wd))
                    render_title = (_row_to_dict(job_row).get("title") or wd.name) if job_row else wd.name
                finally:
                    store.close()
    except Exception:
        pass

    try:
        queue_pending = sum(1 for q in yt.load_queue() if q.get("status") == "pending")
    except Exception:
        queue_pending = 0

    return {
        "current_op": op,
        "recent": log,
        "render_active": render_active,
        "render_pct": render_pct,
        "render_msg": render_msg,
        "render_title": render_title,
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


@api.post("/api/youtube/channels/settings")
def yt_channel_settings(body: ChannelSettingsBody) -> dict:
    """Save a channel's per-channel settings: the default YouTube category for its
    uploads, plus the community-engagement config (issue #84) — the persona/guidance
    used to draft replies to non-request comments, and whether approved drafts post
    immediately or wait for review. Auto-saves, like connect/disconnect."""
    cfg = gapp.load_config()
    entry = next((c for c in (cfg.get("youtube_channels") or []) if c.get("id") == body.id), None)
    if entry is None:
        raise HTTPException(404, "Channel not found.")
    entry["engagement_prompt"] = body.engagement_prompt.strip()
    entry["auto_respond"] = bool(body.auto_respond)
    entry["video_category"] = body.video_category.strip()
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
    cover = wd / "cover.png"
    meta = {}
    try:
        job_json = wd / "job.json"
        if job_json.exists():
            meta = json.loads(job_json.read_text())
    except Exception:
        pass
    vid_w, vid_h = _film_dimensions(wd)
    orientation = "portrait" if vid_h > vid_w else ("square" if vid_h == vid_w else "landscape")
    # Channel this film publishes to, resolved from its style (issue #22).
    channel = _channel_for_work_dir(wd)
    return {
        "work_dir": str(wd),
        "title": _video_title_for(wd),
        "final_url": f"/api/file?path={final}" if final.exists() and final.stat().st_size > 10_000 else "",
        "cover_url": f"/api/file?path={cover}" if cover.exists() and cover.stat().st_size > 1000 else "",
        "description": _cached_description(wd),
        "youtube_url": meta.get("youtube_url", ""),
        "youtube_video_id": meta.get("youtube_video_id", ""),
        "channel": channel,
        # The target channel's default category (its own, else the global default).
        "category": _category_for_channel(gapp.load_config(), channel),
        "orientation": orientation,
        "vid_width": vid_w,
        "vid_height": vid_h,
        # Shorts (portrait) don't take custom thumbnails — default the upload off.
        "include_thumbnail_default": orientation != "portrait",
    }


class DescribeBody(BaseModel):
    work_dir: str = ""
    title: str = ""


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
    if wd is None or not wd.exists():
        raise HTTPException(404, "No script found.")
    title = (body.title or "").strip()
    if title:
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
    try:
        _description_path(wd).write_text(body.description or "")
    except Exception as e:
        raise HTTPException(500, f"Could not save description: {str(e).splitlines()[0][:200]}")
    # Keep a still-pending linked queue item's title in sync (issue #43).
    qid = (body.queue_item_id or "").strip() or _film_job_config(wd).get("queue_item_id", "")
    if qid and title:
        item = _queue_item_by_id(qid)
        if item and item.get("status") == "pending":
            yt.update_queue_item(qid, final_title=title)
    return {"ok": True, "title": title}


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
    return str(desc)


def _describe_in_background(work_dir: str, title: str) -> None:
    """Daemon-thread target: write description.txt for a fresh script so the
    Cover/Publish screens find it pre-filled (no manual Generate click)."""
    try:
        with _track_op("Generating description", title):
            _generate_and_cache_description(work_dir, title)
    except Exception:
        gapp.logger.warning("Background description generation failed for %s", work_dir, exc_info=True)


class CoverBody(BaseModel):
    work_dir: str = ""
    title: str = ""
    style: str = ""
    resolution: str = ""


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


@api.post("/api/youtube/cover")
def yt_cover(body: CoverBody) -> dict:
    wd = Path(body.work_dir) if body.work_dir else gapp._latest_work_dir()
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
        tid = make_task_id(job_id, "ui.cover.generate", int(time.time()))
        store.create_task(
            tid, job_id, "ui.cover.generate", f"Cover: {title}",
            worker_kind="ui",
            payload={
                "work_dir": str(wd),
                "title": title,
                "style": body.style or "",
                "vid_width": vid_width,
                "vid_height": vid_height,
                "comfy_url": _best_cover_comfy_url(),
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
    if t.error:
        result["error"] = t.error[:200]
    return result


class ThumbnailBody(BaseModel):
    work_dir: str
    video_id: str = ""


@api.post("/api/youtube/thumbnail")
def yt_thumbnail(body: ThumbnailBody) -> dict:
    """Push the current cover.png to an already-uploaded video's thumbnail."""
    wd = Path(body.work_dir)
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")
    cover = wd / "cover.png"
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
    # Reply as the channel the comment was posted on, not the upload channel.
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


def _run_upload_task(task_id: str, body_dict: dict, wd: Path, final: Path, thumb) -> None:
    """Background thread: upload to YouTube, then send completion reply."""
    try:
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
                channel=body_dict.get("channel", ""),
            )
    except Exception as e:
        _upload_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:240]}
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
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")
    final = gapp._final_path_for_work_dir(wd)
    if not (final.exists() and final.stat().st_size > 10_000):
        raise HTTPException(400, "No final video found for this film.")
    cover = wd / "cover.png"
    thumb = (str(cover) if body.include_thumbnail
             and cover.exists() and cover.stat().st_size > 1000 else None)

    task_id = uuid.uuid4().hex[:12]
    _upload_tasks[task_id] = {"status": "uploading"}
    channel = body.channel or _channel_for_work_dir(wd)
    threading.Thread(
        target=_run_upload_task,
        args=(task_id, {"title": body.title, "description": body.description,
                        "category": body.category, "privacy": body.privacy,
                        "channel": channel}, wd, final, thumb),
        daemon=True,
    ).start()
    return {"ok": True, "task_id": task_id}


@api.get("/api/youtube/post/status")
def yt_post_status(task_id: str) -> dict:
    task = _upload_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Upload task not found.")
    return {"ok": True, **task}


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
    )
    try:
        with _track_op("Drafting reply", c.get("commenter", "")):
            text = _llm_complete(system, user, cfg, max_tokens=300).strip().strip('"').strip()
    except Exception as e:
        raise HTTPException(503, f"Reply draft failed: {str(e).splitlines()[0][:200]}")
    return {"reply": text}


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


class QueueAddBody(BaseModel):
    title: str
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
    n = max(6, min(50, body.n_scenes
                   or gapp.style_settings(cfg, body.style_name).get("n_scenes") or 6))
    comment = {"comment_id": "", "text": body.prompt, "commenter": "you",
               "suggested_scene_count": n}
    entry = yt.add_to_queue(comment, title, source="manual")
    if entry:
        updates = {}
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
    if body.suggested_scene_count is not None:
        updates["suggested_scene_count"] = max(6, min(50, int(body.suggested_scene_count)))
    if body.gen_resolution is not None:
        updates["gen_resolution"] = body.gen_resolution.strip()
    if body.gen_style_name is not None:
        updates["gen_style_name"] = body.gen_style_name.strip()
    if updates:
        yt.update_queue_item(body.id, **updates)
    return {"ok": True, "queue": yt.load_queue()}


def _job_meta_field(job_id: str, key: str, default: str = "") -> str:
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
        yt.update_queue_item(item_id, status="creating")
    title = item.get("final_title", "")
    # The item's style profile (or the default style) supplies every setting
    # the queue item doesn't carry itself. An empty style_name is passed
    # through so start_generation can still prefer the name stamped on the job
    # at script time.
    style_name = (item.get("gen_style_name") or "").strip()
    ss = gapp.style_settings(cfg, style_name)
    n = max(6, int(item.get("suggested_scene_count") or ss.get("n_scenes") or 6))

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
                job_id=job_id, work_dir=wd, video_title=title, title=title, n_scenes=n,
                voice=item.get("gen_voice") or "",
                voice_robotic=item.get("gen_voice_robotic"),
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
        gen = _do_script_generate(GenerateScriptBody(
            video_title=title, topic=topic, n_scenes=n, resolution=resolution,
            style_name=style_name))
        start_generation(GenerateBody(
            job_id=gen["job_id"], work_dir=gen["work_dir"], video_title=title, title=title,
            n_scenes=n, voice=ss.get("voice", ""),
            voice_robotic=item.get("gen_voice_robotic"),
            resolution=resolution,
            music_desc=gen.get("music_desc", ""), style=gen.get("style", ""),
            style_name=gen.get("style_name", "")))
        # See above: re-link the work dir to this queue item so auto-post finds it.
        _link_queue_item_to_work_dir(item, Path(gen["work_dir"]))
        yt.update_queue_item(item["id"], status="creating",
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
    n_scenes: int = 0
    style: str = ""
    resolution: str = ""
    voice: str = ""
    voice_robotic: bool | None = None
    music_desc: str = ""
    queue_item_id: str = ""
    style_name: str = ""


@api.post("/api/queue/from-job")
def queue_from_job(body: FromJobBody) -> dict:
    """Add an approved (already-generated) script to the queue. Does NOT render
    unless 'auto-start next' (youtube_auto_start_job) is on and nothing is
    currently rendering.

    If body.queue_item_id points at a still-pending slot, update THAT item in
    place (keeping its position) instead of appending a new entry. This is how
    the Queue "Edit" flow attaches a ready script to an existing slot so it
    renders fast (issue #43). In-place updates never auto-start — the item stays
    queued exactly where it was."""
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
    n = max(6, int(n or 6))

    script_fields = dict(
        video_job_id=body.job_id, work_dir=body.work_dir, script_ready=True,
        gen_style=body.style, gen_resolution=body.resolution,
        gen_voice=body.voice, gen_voice_robotic=body.voice_robotic, gen_music=body.music_desc,
        gen_style_name=body.style_name,
    )

    # In-place update of an existing pending slot — keep its queue position.
    # Prefer the explicit queue_item_id; otherwise fall back to a pending row
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
                         if q.get("status") == "pending"
                         and ((body.job_id and q.get("video_job_id") == body.job_id)
                              or (body.work_dir and q.get("work_dir") == body.work_dir))), None)
    if existing is not None and existing.get("status") == "pending":
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
    if cfg.get("youtube_auto_start_job") and not gapp._is_job_running():
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
        _film_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:200]}


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


def _film_job_config(work_dir: Path) -> dict:
    try:
        return json.loads((work_dir / "job_config.json").read_text())
    except Exception:
        return {}


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

    result = []
    for r in ordered:
        sid = int(r.get("id") or r.get("scene_id") or 0)
        result.append({**_scene_to_json(r), **_film_scene_files(wd, sid)})

    jc = _film_job_config(wd)
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

    return {
        "scenes": result,
        "job_id": job_id,
        "work_dir": str(wd),
        "title": title,
        "style": style,
        "resolution": resolution,
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


@api.post("/api/films/reassemble")
def reassemble_film(body: ReassembleBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")

    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()

    if not rows:
        raise HTTPException(400, "No scene data found.")

    all_ids = [int(r.get("id") or r.get("scene_id") or 0) for r in rows]
    order = _load_scene_order(wd) or all_ids

    scene_finals = [
        wd / f"scene_{sid:02d}_final.mp4"
        for sid in order
        if (wd / f"scene_{sid:02d}_final.mp4").exists()
        and (wd / f"scene_{sid:02d}_final.mp4").stat().st_size > 10_000
    ]
    if not scene_finals:
        raise HTTPException(400, "No rendered scenes found. Re-render scenes first.")

    music_path = wd / "background_music.wav"
    if not music_path.exists():
        raise HTTPException(400, "No background music found in this film folder.")

    final_path = gapp._final_path_for_work_dir(wd)
    combined = wd / "combined.mp4"

    jc = _film_job_config(wd)
    cfg = gapp.load_config()
    voice_vol = float(jc.get("voice_vol", cfg.get("voice_vol", 100))) / 100.0
    music_vol = float(jc.get("music_vol", cfg.get("music_vol", 18))) / 100.0
    ambient_vol = float(jc.get("ambient_vol", cfg.get("ambient_vol", 0))) / 100.0

    try:
        with _track_op("Reassembling film", wd.name):
            from pipeline.assembler import concatenate_scenes, mix_background_music
            concatenate_scenes(scene_finals, combined)
            ambient = wd / "ambient.wav"
            mix_background_music(
                combined, music_path, final_path,
                volume=music_vol,
                voice_volume=voice_vol,
                ambient_path=ambient if ambient.exists() else None,
                ambient_volume=ambient_vol,
            )
    except Exception as e:
        raise HTTPException(500, f"Reassembly failed: {str(e).splitlines()[0][:200]}")

    return {
        "ok": True,
        "final_url": f"/api/file?path={final_path}",
        "scene_count": len(scene_finals),
    }


class RerenderSceneBody(BaseModel):
    work_dir: str
    component: str  # "narration", "image", or "video"


def _run_narration_rerender(task_id: str, wd: Path, sid: int, jc: dict, row: dict) -> None:
    """Background thread: re-render narration then re-mux the scene."""
    from pipeline.assembler import mux_video_audio
    from pipeline.tts_worker import generate_narration

    narration_path = wd / f"scene_{sid:02d}_narration.wav"
    final_path = wd / f"scene_{sid:02d}_final.mp4"
    cfg = gapp.load_config()

    try:
        _film_checkpoint(task_id)
        narration_text = (row.get("narration") or row.get("title") or f"Scene {sid}").strip()
        voice_ref_str = jc.get("voice_ref") or ""
        voice_ref = Path(voice_ref_str).expanduser() if voice_ref_str else None
        voice_robotic = bool(jc.get("voice_robotic", False))
        voice_robotic_amount = jc.get("voice_robotic_amount", cfg.get("default_voice_robotic_amount", 0.35))
        voice_speed = jc.get("voice_speed", cfg.get("default_voice_speed", 1.0))
        tts_hosts = cfg.get("tts_workers") or []
        tts_host = tts_hosts[0] if tts_hosts else "localhost"

        _film_tasks[task_id] = {"status": "running", "step": "narration"}
        generate_narration(narration_text, narration_path, reference_wav=voice_ref, host=tts_host, robotic=voice_robotic, robotic_amount=voice_robotic_amount, speed=voice_speed)

        # Re-mux narration with the existing scene video
        video_path = wd / f"scene_{sid:02d}_video.mp4"
        clip_path = wd / f"scene_{sid:02d}_clip_01.mp4"
        actual_video = (
            video_path if (video_path.exists() and video_path.stat().st_size > 10_000)
            else clip_path if (clip_path.exists() and clip_path.stat().st_size > 10_000)
            else None
        )
        if actual_video:
            _film_checkpoint(task_id)
            _film_tasks[task_id]["step"] = "mux"
            mux_video_audio(actual_video, narration_path, final_path)

        _film_tasks[task_id] = {"status": "done"}
    except Exception as e:
        _finish_film_task_error(task_id, e)


def _run_image_rerender(task_id: str, wd: Path, sid: int, jc: dict, row: dict) -> None:
    """Background thread: re-render first-frame image only (no video)."""
    import shutil
    import secrets
    from pipeline.comfyui import generate_scene_image, ltx_dimensions

    cfg = gapp.load_config()
    worker_urls = gapp._preview_worker_urls()
    if not worker_urls:
        _film_tasks[task_id] = {"status": "error", "error": "No ComfyUI workers reachable."}
        return

    from pipeline.worker_pool import WorkerPool
    pool = WorkerPool(worker_urls)

    first_frame = wd / f"scene_{sid:02d}_first_frame.png"
    preview = wd / f"scene_{sid:02d}_preview.png"

    try:
        _film_checkpoint(task_id)
        image_prompt = (row.get("image_prompt") or "").strip()
        style_clean = jc.get("style", "").strip().rstrip(".")
        if style_clean and image_prompt and not image_prompt.startswith(style_clean):
            image_prompt = f"{style_clean}. {image_prompt}"

        resolution = jc.get("resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)
        vid_w, vid_h = gapp._RESOLUTIONS.get(resolution, (int(jc.get("vid_width", 832)), int(jc.get("vid_height", 480))))
        vid_w, vid_h = ltx_dimensions(vid_w, vid_h)

        new_seed = secrets.randbelow(2 ** 32)
        _film_tasks[task_id] = {"status": "running", "step": "image"}
        url = pool.acquire()
        try:
            # acquire() can block a long time behind a busy GPU — re-check
            # before submitting work the film may no longer exist to receive.
            _film_checkpoint(task_id)
            generate_scene_image(
                image_prompt or row.get("title") or f"Scene {sid}",
                first_frame,
                width=vid_w, height=vid_h,
                seed=new_seed,
                steps=int(jc.get("flux_steps", cfg.get("flux_steps", 4))),
                flux_model=jc.get("flux_model") or cfg.get("flux_model", "flux1-schnell-fp8.safetensors"),
                clip_t5=jc.get("flux_clip_t5") or cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
                clip_l=jc.get("flux_clip_l") or cfg.get("flux_clip_l", "clip_l.safetensors"),
                flux_vae=jc.get("flux_vae") or cfg.get("flux_vae", "ae.safetensors"),
                comfy_url=url,
            )
        finally:
            pool.release(url)

        if first_frame.exists():
            shutil.copy2(first_frame, preview)

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


def _run_video_rerender(task_id: str, wd: Path, sid: int, jc: dict, row: dict) -> None:
    """Background thread: re-render video from the existing first frame → mux.

    Reuses the already-made first frame; only regenerates it when none is usable."""
    from pipeline.assembler import mux_video_audio, _get_duration
    from pipeline.comfyui import generate_scene_image, ltx_dimensions
    from pipeline.llm import Scene
    from pipeline.scene_video import generate_scene_video as gen_scene_video

    cfg = gapp.load_config()
    worker_urls = gapp._preview_worker_urls()
    if not worker_urls:
        _film_tasks[task_id] = {"status": "error", "error": "No ComfyUI workers reachable."}
        return

    from pipeline.worker_pool import WorkerPool
    pool = WorkerPool(worker_urls)

    first_frame = wd / f"scene_{sid:02d}_first_frame.png"
    final_path = wd / f"scene_{sid:02d}_final.mp4"

    try:
        _film_checkpoint(task_id)
        image_prompt = (row.get("image_prompt") or "").strip()
        video_prompt = (row.get("video_prompt") or row.get("image_prompt") or "").strip()
        style_clean = jc.get("style", "").strip().rstrip(".")
        if style_clean:
            if image_prompt and not image_prompt.startswith(style_clean):
                image_prompt = f"{style_clean}. {image_prompt}"
            if video_prompt and not video_prompt.startswith(style_clean):
                video_prompt = f"{style_clean}. {video_prompt}"

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
            _film_tasks[task_id] = {"status": "running", "step": "image"}
            url = pool.acquire()
            try:
                _film_checkpoint(task_id)
                generate_scene_image(
                    image_prompt or row.get("title") or f"Scene {sid}",
                    first_frame,
                    width=vid_w, height=vid_h,
                    steps=int(jc.get("flux_steps", cfg.get("flux_steps", 4))),
                    flux_model=jc.get("flux_model") or cfg.get("flux_model", "flux1-schnell-fp8.safetensors"),
                    clip_t5=jc.get("flux_clip_t5") or cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
                    clip_l=jc.get("flux_clip_l") or cfg.get("flux_clip_l", "clip_l.safetensors"),
                    flux_vae=jc.get("flux_vae") or cfg.get("flux_vae", "ae.safetensors"),
                    comfy_url=url,
                )
            finally:
                pool.release(url)
            scene_first_frame = first_frame

        # Determine narration duration from existing narration wav
        narration_path = wd / f"scene_{sid:02d}_narration.wav"
        if not narration_path.exists():
            raise RuntimeError(f"Narration file missing: {narration_path.name} — re-render narration first.")
        nar_dur = _get_duration(narration_path)

        _film_tasks[task_id]["step"] = "video"
        scene = Scene(
            id=sid,
            title=row.get("title") or f"Scene {sid}",
            image_prompt=image_prompt,
            video_prompt=video_prompt,
            narration=row.get("narration") or "",
        )
        url = pool.acquire()
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
                {
                    "model": jc.get("flux_model") or cfg.get("flux_model", "flux1-schnell-fp8.safetensors"),
                    "clip_t5": jc.get("flux_clip_t5") or cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
                    "clip_l": jc.get("flux_clip_l") or cfg.get("flux_clip_l", "clip_l.safetensors"),
                    "vae": jc.get("flux_vae") or cfg.get("flux_vae", "ae.safetensors"),
                    "steps": int(jc.get("flux_steps", cfg.get("flux_steps", 4))),
                },
            )
        finally:
            pool.release(url)

        _film_checkpoint(task_id)
        _film_tasks[task_id]["step"] = "mux"
        mux_video_audio(scene_video, narration_path, final_path)

        _film_tasks[task_id] = {"status": "done"}
    except Exception as e:
        _finish_film_task_error(task_id, e)


def _run_rerender_logged(target, tid: str, wd: Path, sid: int, component: str, jc: dict, row: dict) -> None:
    """Run a re-render worker, then record a completion entry in the Activity log.

    The workers only update _film_tasks (so the live "Re-rendering…" indicator can
    read their step), so this wrapper adds the "Recent" history entry that
    _track_op gives every other operation."""
    started = time.time()
    try:
        target(tid, wd, sid, jc, row)
    finally:
        _film_cancelled_tids.discard(tid)
        end = time.time()
        status = (_film_tasks.get(tid) or {}).get("status")
        if status == "error":
            name = f"Re-render failed — scene {sid}"
        elif status == "cancelled":
            name = f"Re-render cancelled — scene {sid}"
        else:
            name = f"Re-rendered scene {sid}"
        with _op_lock:
            _activity_log.insert(0, {
                "name": name, "detail": component,
                "ts": end, "duration_s": round(end - started, 1),
            })
            del _activity_log[20:]


@api.post("/api/films/scenes/{scene_id}/rerender")
def rerender_film_scene(scene_id: int, body: RerenderSceneBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Path is outside the output folder.")
    if body.component not in ("narration", "image", "video"):
        raise HTTPException(400, f"Unknown component: {body.component!r}")

    sid = scene_id
    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()

    row = next((r for r in rows if int(r.get("id") or r.get("scene_id") or 0) == sid), None)
    if not row:
        raise HTTPException(404, f"Scene {sid} not found.")

    jc = _film_job_config(wd)

    # Delete stale files for the component and its dependents
    if body.component == "narration":
        for f in [f"scene_{sid:02d}_narration.wav", f"scene_{sid:02d}_final.mp4"]:
            (wd / f).unlink(missing_ok=True)
    elif body.component == "image":
        for f in [f"scene_{sid:02d}_first_frame.png", f"scene_{sid:02d}_preview.png"]:
            (wd / f).unlink(missing_ok=True)
    elif body.component == "video":
        # Keep the existing first frame — re-rendering video reuses it, doesn't
        # regenerate it. Only clear the video clips and muxed output.
        for f in [
            f"scene_{sid:02d}_video.mp4", f"scene_{sid:02d}_clip_01.mp4",
            f"scene_{sid:02d}_clip_02.mp4", f"scene_{sid:02d}_final.mp4",
        ]:
            (wd / f).unlink(missing_ok=True)

    tid = f"rerender_{sid:02d}_{body.component}_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": body.component}
    _film_task_meta[tid] = {"work_dir": str(wd), "scene_id": sid, "component": body.component}

    if body.component == "narration":
        target = _run_narration_rerender
    elif body.component == "image":
        target = _run_image_rerender
    else:
        target = _run_video_rerender
    threading.Thread(
        target=_run_rerender_logged,
        args=(target, tid, wd, sid, body.component, jc, row),
        daemon=True,
    ).start()
    return {"ok": True, "task_id": tid}


@api.get("/api/films/task")
def film_task_status(task_id: str = Query(...)) -> dict:
    task = _film_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found.")
    return {"ok": True, **task}


@api.get("/api/films/tasks")
def film_tasks_for_work_dir(work_dir: str = Query(...)) -> dict:
    """Running re-render tasks for a film, so the edit page can resume its
    progress indicators after a reload (the task ids live only in client state
    otherwise)."""
    wd = str(Path(work_dir))
    out = []
    for tid, meta in list(_film_task_meta.items()):
        if meta.get("work_dir") != wd:
            continue
        task = _film_tasks.get(tid)
        if not task or task.get("status") != "running":
            continue
        out.append({
            "task_id": tid,
            "scene_id": meta.get("scene_id"),
            "component": meta.get("component"),
            "step": task.get("step", ""),
        })
    return {"ok": True, "tasks": out}


@api.post("/api/films/delete")
def delete_film(body: JobActionBody) -> dict:
    wd = Path(body.work_dir)
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
    if not cfg.get("youtube_auto_approve_script"):
        # Review gate on: only retry items whose script a human already saw —
        # the same rule fresh starts follow.
        failed = [q for q in failed if q.get("script_ready")
                  and q.get("work_dir") and q.get("video_job_id")]
    if not failed:
        return None
    failed.sort(key=lambda q: q.get("updated_at") or q.get("created_at") or 0)
    item = {**failed[0]}  # fresh copy
    item["retry_count"] = int(item.get("retry_count") or 0) + 1
    yt.update_queue_item(item["id"], retry_count=item["retry_count"])
    return item


def _auto_start_best() -> dict | None:
    if gapp._is_job_running():
        return None
    cfg = gapp.load_config()
    pending = _ordered_pending(cfg)
    if cfg.get("youtube_auto_approve_script"):
        # Auto-approve on: take the next pending item and render it end-to-end,
        # writing the script first when the item doesn't have one.
        item = {**pending[0]} if pending else None  # fresh copy
    else:
        # Review gate on: only render the next item whose script is already
        # written (added from the Script screen or via the queue's Edit-script
        # flow — i.e. a human has seen it). Never generate scripts here: an
        # unreviewed script must not render itself, so script-less items wait
        # for the user.
        item = next((q for q in pending
                     if q.get("script_ready")
                     and q.get("work_dir") and q.get("video_job_id")), None)
    if not item:
        # Nothing fresh to start — retry a failed item before inventing new work.
        item = _retryable_failed(cfg)
    if not item and cfg.get("youtube_auto_approve_script") and cfg.get("youtube_auto_ai_ideas"):
        # Queue idle — opt-in fallback: invent an AI idea to keep the channel
        # fed. _auto_pick_suggestion picks the best unused idea, marks it used
        # (so it's closed and never re-picked), and generates a fresh batch
        # when none remain.
        try:
            item = gapp._auto_pick_suggestion(cfg)
        except Exception:
            item = None
    if not item:
        return None
    try:
        return _start_queue_item(item)
    except Exception:
        return None


def _auto_post_done() -> list[str]:
    """Auto-post finished, queue-driven jobs that haven't been posted yet.

    Each job is claimed on disk (_auto_post_triggered in job.json) before its
    upload starts, so neither two overlapping web ticks nor the classic Gradio
    app's auto-poster — a separate process that scans the same job dirs — can
    upload the same video twice. The marker that closes the job permanently
    (youtube_video_id) is only written after the slow upload finishes, which is
    why a pre-upload claim is needed rather than relying on that marker alone.
    """
    cfg = gapp.load_config()
    posted: list[str] = []
    for _label, wd in gapp._list_recent_jobs(max_results=50):
        p = Path(wd)
        jc: dict = {}
        # Atomically claim the job: re-check state and stamp the claim under the
        # lock so two ticks can't both pass the check before either writes.
        with gapp._auto_post_lock:
            if str(p) in gapp._auto_post_triggered:
                continue
            try:
                meta = json.loads((p / "job.json").read_text())
            except Exception:
                continue
            if (meta.get("status") != "done" or meta.get("youtube_video_id")
                    or meta.get("_auto_post_triggered")):
                continue
            try:
                jc = json.loads((p / "job_config.json").read_text())
            except Exception:
                jc = {}
            if not jc.get("queue_item_id"):
                continue  # only auto-post videos that came from the queue
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
                channel=channel,
                # Shorts (portrait) don't take custom thumbnails — skip by default.
                include_thumbnail=not _is_portrait_film(p)))
            if res.get("video_id"):
                posted.append(res["video_id"])
        except Exception:
            # Upload failed — release the claim so a later tick can retry.
            with gapp._auto_post_lock:
                gapp._auto_post_triggered.discard(str(p))
            try:
                gapp._write_job_meta(p, _auto_post_triggered=False)
            except Exception:
                pass
    return posted


def _ensure_descriptions() -> int:
    """Cache YouTube descriptions for completed jobs that don't have one yet.
    Called from the automation loop so it runs server-side, not on browser polls."""
    cfg = gapp.load_config()
    backend = cfg.get("llm_backend", "local")
    if backend == "claude" and not cfg.get("claude_api_key", ""):
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
            if cfg.get("youtube_auto_fetch_evaluate"):
                try:
                    out["fetch"] = _fetch_and_evaluate(cfg.get("youtube_auto_approve_comments", False))
                except Exception as e:
                    out["fetch_error"] = str(e)[:120]
            if cfg.get("youtube_auto_start_job"):
                out["started"] = _auto_start_best()
            if cfg.get("youtube_auto_post"):
                out["posted"] = _auto_post_done()
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


# Opt-in background loop: runs a tick periodically. Each step is gated by its own
# config toggle, so with all toggles off (the default) this is a no-op heartbeat.
# The loop wakes often only to watch for a render finishing, so the next queue
# item chains within seconds instead of waiting out the full interval — still ONE
# engine, just an event-driven nudge on top of the scheduled cadence.
import threading  # noqa: E402

_AUTOMATION_INTERVAL = 180  # seconds between full scheduled ticks
_COMPLETION_POLL = 15       # seconds between cheap "did the render finish?" checks
_tick_lock = threading.Lock()
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
        try:
            cfg = gapp.load_config()
            if any(cfg.get(k) for k in (
                    "youtube_auto_fetch_evaluate", "youtube_auto_start_job", "youtube_auto_post")):
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
    api.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
