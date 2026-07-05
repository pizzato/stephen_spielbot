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
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager, contextmanager
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
from pipeline.llm import generate_script, generate_video_suggestions, Scene  # noqa: E402
from pipeline.orchestrator import DurableStore, job_id_from_work_dir, task_id as make_task_id, worker_id  # noqa: E402
from pipeline.timing import estimate_eta, estimate_planned_job, humanize_eta, next_worker_free_seconds  # noqa: E402
from pipeline import ui_activity  # noqa: E402
from pipeline import image_history  # noqa: E402
from pipeline import video_history  # noqa: E402
from pipeline import music_history  # noqa: E402
from pipeline import final_video_history  # noqa: E402

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
        "voice": meta.get("voice", ""),
        "preview_path": preview if has_preview else "",
        "has_preview": has_preview,
    }
    if wd is not None:
        out["history"] = image_history.history(wd, sid)
        out["video_history"] = video_history.history(wd, sid)
    return out


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
    "final_upscale": "upscaling final video",
    "mux": "muxing audio",
}


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
        "default_resolution": gapp._DEFAULT_RESOLUTION,
        # Structured selectors so the UI can offer an orientation + pixel toggle.
        "orientations": gapp._ORIENTATIONS,
        "pixel_tiers": [{"key": t["key"], "label": t["label"]} for t in gapp._PIXEL_TIERS],
        "default_orientation": gapp._DEFAULT_ORIENTATION,
        "default_pixels": gapp._DEFAULT_PIXELS,
        # Small/Medium/Large size buckets and their fallback presets, so the
        # Settings editor and AI-ideas screen can render a per-style size picker.
        "size_buckets": list(gapp._SIZE_BUCKETS),
        "default_size_presets": gapp.DEFAULT_CFG["default_size_presets"],
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


@api.post("/api/characters/portrait")
def characters_portrait(body: CharacterPortrait) -> dict:
    try:
        cfg = gapp.generate_character_portrait(body.char_id, body.extra_prompt)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return _character_response(cfg)


class VoiceTest(BaseModel):
    voice: str = ""
    robotic: bool = False
    robotic_amount: float | None = None
    speed: float | None = None
    text: str = ""
    engine: str = ""


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
    engine = gapp.tts_engines.norm(body.engine or gapp.style_settings(cfg).get("tts_engine"))

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
        f"{voice}|{engine}|{robotic}|{round(amount, 3)}|{round(speed, 3)}|{text}|{ref_stamp}".encode()
    ).hexdigest()[:16]
    out = gapp.CONFIG_FILE.parent / f"voice_test_{key}.wav"

    cached = out.exists() and out.stat().st_size > 1000
    if not cached:
        tts_hosts = cfg.get("tts_workers") or []
        tts_host = tts_hosts[0] if tts_hosts else "localhost"
        try:
            with _track_op("Testing voice", spoken):
                generate_narration(text, out, reference_wav=ref, host=tts_host,
                                   robotic=robotic, robotic_amount=amount, speed=speed,
                                   tts_engine=engine)
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
    n_scenes: int = 6
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
    video_style_hint = ss.get("video_style", "") or None
    character_sheet = gapp._character_sheet(gapp._style_characters(cfg, body.style_name)) or None
    display_topic = (body.video_title or "").strip() or topic.splitlines()[0][:80]
    try:
        with _track_op("Generating script", display_topic):
            scenes, music_desc, style = generate_script(
                topic, int(body.n_scenes), style_hint, (body.video_title or "").strip() or None,
                video_style_hint=video_style_hint, character_sheet=character_sheet,
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
        "scenes": [_scene_to_json(s, work_dir) for s in scenes_list],
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


def _script_source_meta(src_job_id: str, fallback_title: str) -> tuple[str, str, str, str]:
    """Resolve (video_title, style, music_desc, style_name) for an existing job
    from the durable store, falling back to the folder-derived title."""
    video_title, style, music_desc, style_name = fallback_title, "", "", ""
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
    finally:
        store.close()
    return video_title, style, music_desc, style_name


def _register_script_into(wd: Path, scenes_list: list, *, video_title: str,
                          style: str, music_desc: str, style_name: str) -> dict:
    """Register `scenes_list` as the script of work dir `wd` (a reload of its own
    folder, or a fresh duplicate) and return the Script-editor payload. Back-fills
    each scene's preview_path from any matching image already in `wd`, so a first
    frame produced by an earlier render (or copied from a source script) is reused
    instead of regenerated. Shared by /scripts/load and /scripts/duplicate."""
    job_id = job_id_from_work_dir(wd)
    store = DurableStore.default()
    try:
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
        "scenes": [_scene_to_json(r, wd) for r in rows],
    }


@api.get("/api/scripts/load")
def load_script(work_dir: str = Query("")) -> dict:
    if not work_dir:
        raise HTTPException(400, "Choose a saved script.")
    wd = Path(work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Script path is outside the output folder.")
    scenes_list = _read_script_scenes(wd)
    fallback_title = wd.name.replace("-", " ").title()
    video_title, style, music_desc, style_name = _script_source_meta(
        job_id_from_work_dir(wd), fallback_title)
    return _register_script_into(wd, scenes_list, video_title=video_title,
                                 style=style, music_desc=music_desc, style_name=style_name)


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
    still yields a different take). Returns the same payload as /scripts/load, so
    the duplicate opens straight in the Script editor for review."""
    import shutil
    src = Path(body.work_dir)
    if not _safe_under(src, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Script path is outside the output folder.")
    scenes_list = _read_script_scenes(src)

    fallback_title = src.name.replace("-", " ").title()
    src_title, style, music_desc, style_name = _script_source_meta(
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
    for extra in ("description.txt", "cover.png"):
        sp = src / extra
        if sp.exists():
            shutil.copy2(sp, new_wd / extra)

    return _register_script_into(new_wd, scenes_list, video_title=title,
                                 style=style, music_desc=music_desc, style_name=style_name)


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


@api.put("/api/jobs/{job_id}/scenes/{scene_id}")
def update_scene(job_id: str, scene_id: int, body: SceneUpdate) -> dict:
    sid = int(scene_id)
    store = DurableStore.default()
    try:
        current = store.get_scene(job_id, sid) or {}
        meta = dict(current.get("metadata") or {})
        if body.voice is not None:
            voice = (body.voice or "").strip()
            if voice:
                meta["voice"] = voice
            else:
                meta.pop("voice", None)
        store.upsert_scene(
            job_id,
            sid,
            title=body.title,
            image_prompt=body.image_prompt,
            video_prompt=body.video_prompt,
            narration=body.narration,
            preview_path=current.get("preview_path", ""),
            metadata=meta,
        )
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    work_dir = gapp._job_work_dir(job_id)
    if work_dir:
        gapp._persist_script_snapshot(work_dir, rows)
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
    wd = gapp._job_work_dir(job_id)
    hist = image_history.history(wd, int(scene_id)) if wd else None
    return {"ok": True, "preview_path": str(out), "history": hist}


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

    wd = gapp._job_work_dir(job_id)
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
                       engine: dict, denoise: float | None = None) -> dict:
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

    # Preserve the current image so the user can return to it (mirrors regen/rerender).
    image_history.seed_if_empty(wd, sid, base)

    out = wd / f"scene_{sid:02d}_preview.png"
    mask_tmp = wd / f"_inpaint_mask_{sid:02d}.png"
    mask_tmp.write_bytes(mask_bytes)

    pool = gapp.WorkerPool(worker_urls)
    url = pool.acquire()
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
        with _track_op("Editing image", f"scene {sid} · {engine['key']}"):
            return _run_scene_inpaint(wd, sid, base, prompt, body.mask, job_id, engine, denoise=body.denoise)
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
    from pipeline.comfyui import engine_model_present
    from pipeline.worker_pool import queue_depth
    cfg = gapp.load_config()
    probe_url = next((u for u in (cfg.get("comfy_workers") or []) if queue_depth(u, timeout=3) >= 0), None)
    availability = {k: (engine_model_present(probe_url, e.get("probe")) if probe_url else None)
                    for k, e in eng.ENGINES.items()}
    return {
        "engines": eng.public_list(),
        "availability": availability,
        "default_engine": eng.DEFAULT_ENGINE,
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
    e = eng.get(engine_key) or {}
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
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", host, f"{env}bash -s"],
                input=script_text, capture_output=True, text=True, timeout=6 * 3600)
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
    e = eng.get(body.engine)
    if not e:
        raise HTTPException(400, f"Unknown engine: {body.engine!r}")
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
        "tts_engine": gapp.tts_engines.norm(ss.get("tts_engine")),
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


@api.get("/api/jobs")
def list_jobs() -> dict:
    finished_rows = gapp._list_recent_jobs(max_results=50)
    cfg = gapp.load_config()
    def _cover_url(work_dir: str) -> str:
        cover = Path(work_dir) / "cover.png"
        if cover.exists() and cover.stat().st_size > 1000:
            return f"/api/file?path={cover}"
        return ""
    finished = []
    for l, d in finished_rows:
        try:
            meta = json.loads((Path(d) / "job.json").read_text())
        except Exception:
            meta = {}
        finished.append({"label": l, "work_dir": d, "cover_url": _cover_url(d),
                         "seen": bool(meta.get("viewed_at")),
                         **_film_publish_status(Path(d), meta, cfg)})
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


class RemixNarratorBody(BaseModel):
    work_dir: str
    voice: str = ""


class RemixUpscaleBody(BaseModel):
    work_dir: str
    target_resolution: str
    upscale_mode: str = "fast"


class RemixVideoSelectBody(BaseModel):
    work_dir: str
    version_id: int


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
    try:
        meta = json.loads((wd / "job.json").read_text())
    except Exception:
        meta = {}
    return {
        "work_dir": str(wd),
        "final_url": f"/api/file?path={final_vid}",
        "voice_vol": jc.get("voice_vol", cfg.get("voice_vol", 100)),
        "music_vol": jc.get("music_vol", cfg.get("music_vol", 18)),
        "ambient_vol": jc.get("ambient_vol", cfg.get("ambient_vol", 0)),
        "voice": jc.get("default_voice", ""),
        "voices": gapp.get_voice_choices(),
        "music_desc": jc.get("music_desc", ""),
        "music_history": music_history.history(wd),
        "video_history": final_video_history.history(wd),
        "resolution": jc.get("resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION),
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
    with _track_op("Remixing audio", wd.name):
        final_path, message = gapp.on_remix(
            str(combined), str(music),
            str(ambient) if ambient.exists() else "",
            voice_vol=body.voice_vol, music_vol=body.music_vol, ambient_vol=body.ambient_vol,
        )
    if not final_path:
        raise HTTPException(500, message or "Remix failed.")
    return {"message": message, "final_url": f"/api/file?path={final_path}"}


def _run_remix_narrator(task_id: str, wd: Path, voice: str) -> None:
    from pipeline.assembler import concatenate_scenes, mix_background_music

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
            video_history.seed_if_empty(wd, int(sid), wd / f"scene_{int(sid):02d}_final.mp4")
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
        concatenate_scenes(scene_finals, combined)
        mix_background_music(
            combined, music_path, final_path,
            volume=float(jc.get("music_vol", cfg.get("music_vol", 18))) / 100.0,
            voice_volume=float(jc.get("voice_vol", cfg.get("voice_vol", 100))) / 100.0,
            ambient_path=ambient if ambient.exists() else None,
            ambient_volume=float(jc.get("ambient_vol", cfg.get("ambient_vol", 0))) / 100.0,
        )
        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "voice": voice_name,
            "scene_count": len(scene_finals),
        }
    except Exception as e:
        _finish_film_task_error(task_id, e)


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
    _film_task_meta[tid] = {"work_dir": str(wd), "scene_id": 0, "component": "narrator"}
    threading.Thread(
        target=_run_remix_narrator, args=(tid, wd, voice), daemon=True,
    ).start()
    return {"ok": True, "task_id": tid}


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

        # The original track was already seeded (with its own prompt) by the endpoint,
        # before the prompt was overwritten — see remix_regen_music.
        _film_tasks[task_id] = {"status": "running", "step": "music"}
        url = pool.acquire()
        try:
            # acquire() can block behind a busy GPU — re-check before submitting.
            _film_checkpoint(task_id)
            generate_music(title, music_dur, staged, (music_desc or None), comfy_url=url)
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
        final_path, message = gapp.on_remix(
            str(combined), str(music_path),
            str(ambient) if ambient.exists() else "",
            voice_vol=float(jc.get("voice_vol", cfg.get("voice_vol", 100))),
            music_vol=float(jc.get("music_vol", cfg.get("music_vol", 18))),
            ambient_vol=float(jc.get("ambient_vol", cfg.get("ambient_vol", 0))),
        )
        if not final_path:
            raise RuntimeError(message or "Re-mux failed after regenerating music.")
        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "music_history": music_history.history(wd),
        }
    except Exception as e:
        staged.unlink(missing_ok=True)
        _finish_film_task_error(task_id, e)


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
    _film_task_meta[tid] = {"work_dir": str(wd), "scene_id": 0, "component": "music"}
    threading.Thread(
        target=_run_music_regen, args=(tid, wd, body.music_desc or ""), daemon=True,
    ).start()
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

    combined = wd / "combined.mp4"
    if not combined.exists():
        raise HTTPException(404, f"combined.mp4 not found in {wd.name}.")
    jc = _film_job_config(wd)
    cfg = gapp.load_config()
    ambient = wd / "ambient.wav"
    with _track_op("Selecting music", wd.name):
        final_path, message = gapp.on_remix(
            str(combined), str(music_path),
            str(ambient) if ambient.exists() else "",
            voice_vol=float(jc.get("voice_vol", cfg.get("voice_vol", 100))),
            music_vol=float(jc.get("music_vol", cfg.get("music_vol", 18))),
            ambient_vol=float(jc.get("ambient_vol", cfg.get("ambient_vol", 0))),
        )
    if not final_path:
        raise HTTPException(500, message or "Re-mux failed.")
    return {
        "ok": True,
        "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
        "music_history": music_history.history(wd),
    }


def _run_final_video_upscale(task_id: str, wd: Path, target_name: str, upscale_mode: str) -> None:
    """Background thread: upscale the completed film, preserving selectable masters."""
    from pipeline.assembler import _get_video_dimensions, temporal_ai_upscale_video, upscale_video

    final_path = gapp._final_path_for_work_dir(wd)
    staged = wd / "final_upscale.staging.mp4"
    try:
        _film_checkpoint(task_id)
        if not final_path.exists() or final_path.stat().st_size <= 0:
            raise RuntimeError("Final video not found; render the film first.")

        target_dims = gapp._RESOLUTIONS.get((target_name or "").strip())
        if not target_dims:
            raise RuntimeError("Choose a valid upscale resolution.")
        mode = (upscale_mode or "fast").strip().lower()
        if mode not in {"fast", "temporal_ai"}:
            raise RuntimeError("Choose a valid upscale mode.")

        target_w, target_h = target_dims
        actual_w, actual_h = _get_video_dimensions(final_path)
        if actual_w >= target_w and actual_h >= target_h:
            raise RuntimeError(
                f"Final video is already {actual_w}x{actual_h}; choose a larger target than {target_w}x{target_h}."
            )

        final_video_history.seed_if_empty(wd, final_path, "Original")
        _film_tasks[task_id] = {"status": "running", "step": "final_upscale"}
        cfg = gapp.load_config()
        if mode == "temporal_ai":
            command_template = cfg.get("temporal_video_upscaler_cmd") or None
            if command_template:
                temporal_ai_upscale_video(
                    final_path,
                    staged,
                    target_w,
                    target_h,
                    command_template=command_template,
                    timeout_seconds=int(cfg.get("temporal_video_upscaler_timeout") or 7200),
                )
            else:
                worker_urls = gapp._preview_worker_urls()
                if not worker_urls:
                    raise RuntimeError("No ComfyUI workers reachable for temporal AI upscale.")
                from pipeline.worker_pool import WorkerPool

                pool = WorkerPool(worker_urls)
                url = pool.acquire()
                try:
                    _film_checkpoint(task_id)
                    temporal_ai_upscale_video(
                        final_path,
                        staged,
                        target_w,
                        target_h,
                        timeout_seconds=int(cfg.get("temporal_video_upscaler_timeout") or 7200),
                        comfy_url=url,
                    )
                finally:
                    pool.release(url)
        else:
            upscale_video(final_path, staged, target_w, target_h)

        _film_checkpoint(task_id)
        staged.replace(final_path)
        label = f"{'AI temporal' if mode == 'temporal_ai' else 'Fast'} {target_w}x{target_h}"
        final_video_history.record(wd, final_path, label=label)
        _film_tasks[task_id] = {
            "status": "done",
            "final_url": f"/api/file?path={final_path}&t={int(time.time())}",
            "video_history": final_video_history.history(wd),
        }
    except Exception as e:
        staged.unlink(missing_ok=True)
        _finish_film_task_error(task_id, e)


@api.post("/api/remix/upscale")
def remix_upscale_video(body: RemixUpscaleBody) -> dict:
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
    target_name = (body.target_resolution or "").strip()
    if target_name not in gapp._RESOLUTIONS:
        raise HTTPException(400, "Choose a valid upscale resolution.")
    mode = (body.upscale_mode or "fast").strip().lower()
    if mode not in {"fast", "temporal_ai"}:
        raise HTTPException(400, "Choose a valid upscale mode.")
    if not gapp._final_path_for_work_dir(wd).exists():
        raise HTTPException(404, f"Final video not found for {wd.name}.")

    tid = f"final_upscale_{int(time.time())}"
    _film_tasks[tid] = {"status": "running", "step": "final_upscale"}
    _film_task_meta[tid] = {"work_dir": str(wd), "scene_id": 0, "component": "final_upscale"}
    threading.Thread(
        target=_run_final_video_upscale,
        args=(tid, wd, target_name, mode),
        daemon=True,
    ).start()
    return {"ok": True, "task_id": tid}


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
                        style: dict | None = None,
                        discarded: list[str] | None = None) -> list[dict]:
    """Generate video ideas steered by a free-text theme (e.g. 'Rock bands of
    the 90s') and, optionally, a style profile. ``discarded`` topics are shown
    as a do-not-suggest list. Uses the configured LLM backend via _llm_complete."""
    import re
    avoid = "; ".join(previous)
    rejected = "; ".join(discarded or [])
    system = ("You are a content strategist for a YouTube channel. "
              "Return ONLY a JSON array, no prose.")
    user = (
        f'Generate {n} specific, compelling video ideas guided by this theme: "{guidance}".\n'
        f"Each must be a concrete topic that fits both the theme and the channel style below.\n"
        + llm.style_suggestion_context(style)
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


def _is_real_discard(reason: str) -> bool:
    """A discard the user made on purpose (Decline or Ignore), as opposed to the
    'used' marker the Queue/Create actions reuse — those become videos and are
    tracked via the queue, not the discard list."""
    return (reason or "").strip().lower() not in ("used", "queued", "created")


def _is_declined_reason(reason: str) -> bool:
    """A deliberately *declined* idea (the Decline action / legacy Close) — the
    'not accepted' list the user can review and reset. Distinct from an
    'ignored' idea, which is suppressed silently and never surfaces here."""
    r = (reason or "").strip().lower()
    return _is_real_discard(r) and r != IGNORED_REASON


def _discarded_records(cfg: dict, target: str = "",
                       include_ignored: bool = False) -> list[dict]:
    """Ideas the user deliberately discarded, newest first. Rich data comes from
    the suggestions store (title/reason/scene count/style); the dismissed-log
    supplements any discard not represented there. ``target`` filters to a
    style (legacy/unstamped discards fall under the default style). Ignored
    ideas stay out of this (reviewable) list unless ``include_ignored`` is set —
    the LLM 'do not suggest' list sets it so an ignored topic never resurfaces."""
    default_name = cfg.get("default_style", "")
    by_title: dict[str, dict] = {}

    def consider(rec: dict, *, rich: bool) -> None:
        title = str(rec.get("title") or rec.get("final_title") or "").strip()
        if not title:
            return
        reason = str(rec.get("dismissed_reason") or rec.get("reason") or "dismissed")
        if not _is_real_discard(reason):
            return
        if not include_ignored and not _is_declined_reason(reason):
            return  # an ignored idea — suppressed, but never shown in the list
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


def _discarded_idea_titles(cfg: dict) -> list[str]:
    """Titles the user has thrown away — passed to the LLM as a 'do not suggest
    again' list so neither a declined nor an ignored topic resurfaces. Global (a
    thrown-away topic stays out of every style) since the discard log isn't
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
    """Generate + stamp a batch of ideas for one resolved style profile."""
    if g:
        ideas = _guided_suggestions(g, previous, cfg, style=ss, discarded=discarded)
    else:
        ideas = _normalize_suggestions(
            generate_video_suggestions(previous, cfg, style=ss, discarded_titles=discarded))
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
            if g:
                ideas = _guided_suggestions(g, previous, cfg, style=ss, discarded=discarded)
            else:
                ideas = _normalize_suggestions(
                    generate_video_suggestions(previous, cfg, style=ss, discarded_titles=discarded))
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
            s.pop("dismissed_reason", None)
            s.pop("dismissed_at", None)
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

    return {
        "render_active": render_active,
        "render_pct": render_pct,
        "queue": queue_pending,
        "youtube": attention + publishable,
        "youtube_attention": attention,
        "youtube_publishable": publishable,
        "youtube_disconnected": yt_disconnected,
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


def _upload_prefs_for_channel(cfg: dict, channel_key: str) -> tuple[str, bool]:
    """(language, attach_captions) for a channel's uploads. Defaults: English,
    captions on — including for channels predating these fields."""
    entry = next((c for c in (cfg.get("youtube_channels") or [])
                  if c.get("id") == channel_key), None) or {}
    language = str(entry.get("language") or "").strip() or "en"
    attach_captions = bool(entry.get("upload_captions", True))
    return language, attach_captions


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
    # Channel/account this film publishes to, resolved from its style (issue #22/#107).
    channel = _channel_for_work_dir(wd)
    x_account = _x_account_for_work_dir(wd)
    return {
        "work_dir": str(wd),
        "title": _video_title_for(wd),
        "final_url": f"/api/file?path={final}" if final.exists() and final.stat().st_size > 10_000 else "",
        "cover_url": f"/api/file?path={cover}" if cover.exists() and cover.stat().st_size > 1000 else "",
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
        return _dedupe_cap_tags(topics + extra)
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
    return " ".join(out)


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
        # Generate the cover with the style's selected image engine (same as scenes).
        style_name = ""
        job_row = store.get_job(job_id)
        if job_row is not None:
            try:
                style_name = json.loads(dict(job_row).get("config_json") or "{}").get("style_name", "")
            except Exception:
                style_name = ""
        engine = gapp.engines.resolve(cfg, gapp.style_settings(cfg, style_name).get("image_engine"))
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
    pool = gapp.WorkerPool(worker_urls)
    url = pool.acquire()
    try:
        with _track_op("Editing cover", f"{engine['key']}"):
            edit_with_engine(engine, prompt, cover, mask_tmp, cover, denoise=dn, comfy_url=url)
    except Exception as e:
        raise HTTPException(503, f"Cover edit failed: {str(e).splitlines()[0][:300]}")
    finally:
        pool.release(url)
        mask_tmp.unlink(missing_ok=True)

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


@api.get("/api/youtube/cover/history")
def cover_history(work_dir: str = Query(...)) -> dict:
    return {"history": image_history.cover_history(Path(work_dir))}


class ThumbnailBody(BaseModel):
    work_dir: str
    video_id: str = ""


@api.post("/api/youtube/thumbnail")
def yt_thumbnail(body: ThumbnailBody) -> dict:
    """Push the current cover.png to an already-uploaded video's thumbnail."""
    wd = Path(body.work_dir)
    if not _safe_under(wd, gapp.OUTPUT_DIR):
        raise HTTPException(400, "Work path is outside the output folder.")
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


def _run_upload_task(task_id: str, body_dict: dict, wd: Path, final: Path, thumb) -> None:
    """Background thread: upload to YouTube, then send completion reply."""
    try:
        channel = body_dict.get("channel", "")
        language, attach_captions = _upload_prefs_for_channel(gapp.load_config(), channel)
        # Build a subtitle track from the known script so YouTube shows accurate
        # captions instead of relying on speech recognition. Best-effort, and
        # only when the channel has captions enabled.
        caption_file = None
        if attach_captions:
            try:
                from pipeline import captions as _captions
                _srt = _captions.build_srt(wd)
                caption_file = str(_srt) if _srt else None
            except Exception:
                caption_file = None
        # Keyword tags (topic tags + style + narrator); best-effort.
        yt_tags = _youtube_tags_for(wd, gapp.load_config())
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
                default_language=language, default_audio_language=language,
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
    cover = wd / "cover.png"
    thumb = (str(cover) if body.include_thumbnail
             and cover.exists() and cover.stat().st_size > 1000 else None)

    task_id = uuid.uuid4().hex[:12]
    _upload_tasks[task_id] = {"status": "uploading"}
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
        caption_file = ""
        if attach_captions:
            try:
                from pipeline import captions as _captions
                _srt = _captions.build_srt(wd)
                caption_file = str(_srt) if _srt else ""
            except Exception:
                caption_file = ""
        with _track_op("Posting to X", body_dict.get("title", "")):
            result = xt.post_video(
                cid, secret, str(final), text, account=account,
                premium=premium, youtube_url=youtube_url,
                captions_path=caption_file, language=language)
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

def _publish_cadence_status(cfg: dict, q: list[dict], now: float) -> tuple[dict, dict]:
    """Per channel/account cadence summary for the UI: configured videos/day and
    the derived spacing, last release time, today's release count, and the next
    time a release is allowed. Derived from the queue so it matches the governor."""
    last: dict[tuple, float] = {}
    count: dict[tuple, int] = {}
    for e in q:
        for plat, keyf in (("youtube", "channel"), ("x", "account")):
            sub = e.get(plat) or {}
            ts = sub.get("released_at") or sub.get("published_at")
            if sub.get("status") in ("publishing", "done") and ts:
                k = (plat, sub.get(keyf) or "")
                last[k] = max(last.get(k, 0.0), ts)
                if _same_local_day(ts, now):
                    count[k] = count.get(k, 0) + 1

    def _summary(listed: str, plat: str) -> dict:
        out: dict = {}
        for c in (cfg.get(listed) or []):
            key = c.get("id") or ""
            per_day = float(c.get("publish_per_day") or 0)
            interval = round(1440 / per_day) if per_day > 0 else 0
            k = (plat, key)
            l, cnt = last.get(k, 0.0), count.get(k, 0)
            nxt = max(now, l + interval * 60) if (interval and l) else now
            out[key] = {"per_day": c.get("publish_per_day") or 0, "interval_minutes": interval,
                        "last_released": l or None, "count_today": cnt, "next_eligible": nxt}
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


@api.get("/api/publish/queue")
def publish_queue_list() -> dict:
    cfg = gapp.load_config()
    _reconcile_publish_queue()
    q = pq.load_queue()
    now = time.time()
    chans, accts = _publish_cadence_status(cfg, q, now)
    skip_comment = bool(cfg.get("publish_schedule_skip_comment_requests", True))
    ordered = _ordered_publish_queue(cfg, q)
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
    return {"items": ordered, "channels": chans, "accounts": accts,
            "enabled": bool(cfg.get("publish_schedule_enabled")),
            "skip_comment": skip_comment,
            "sort": cfg.get("publish_sort_order") or "queue",
            "now": now}


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
        approved=bool(body.approved),
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
    # Only fire an immediate render when the script is explicitly approved (or
    # the global auto-approve mode is on). In review mode an enqueued-but-
    # unapproved script waits for the Queue's Approve action — enqueuing is not
    # approving.
    if (cfg.get("youtube_auto_start_job") and not gapp._is_job_running()
            and (body.approved or cfg.get("youtube_auto_approve_script"))):
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

    return {
        "scenes": result,
        "job_id": job_id,
        "work_dir": str(wd),
        "title": title,
        "style": style,
        "resolution": resolution,
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


def _render_scene_narration(task_id: str, wd: Path, sid: int, jc: dict, row: dict,
                            voice_name: str | None = None) -> None:
    from pipeline.assembler import mux_video_audio
    from pipeline.tts_worker import generate_narration

    narration_path = wd / f"scene_{sid:02d}_narration.wav"
    final_path = wd / f"scene_{sid:02d}_final.mp4"
    cfg = gapp.load_config()

    _film_checkpoint(task_id)
    narration_text = (row.get("narration") or row.get("title") or f"Scene {sid}").strip()
    selected_voice = voice_name if voice_name is not None else _scene_voice_name(row, jc)
    voice_ref = _voice_ref_for_name(selected_voice)
    if voice_ref is None and not selected_voice:
        voice_ref_str = jc.get("voice_ref") or ""
        voice_ref = Path(voice_ref_str).expanduser() if voice_ref_str else None
    voice_robotic = bool(jc.get("voice_robotic", False))
    voice_robotic_amount = jc.get("voice_robotic_amount", cfg.get("default_voice_robotic_amount", 0.35))
    voice_speed = jc.get("voice_speed", cfg.get("default_voice_speed", 1.0))
    tts_engine = jc.get("tts_engine", cfg.get("default_tts_engine", "openf5"))
    tts_hosts = cfg.get("tts_workers") or []
    tts_host = tts_hosts[0] if tts_hosts else "localhost"

    _film_tasks[task_id] = {"status": "running", "step": "narration", "scene_id": sid}
    generate_narration(narration_text, narration_path, reference_wav=voice_ref, host=tts_host, robotic=voice_robotic, robotic_amount=voice_robotic_amount, speed=voice_speed, tts_engine=tts_engine)

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
        staged_final = wd / f"scene_{sid:02d}_final.staging.mp4"
        mux_video_audio(actual_video, narration_path, staged_final)
        staged_final.replace(final_path)
        video_history.record(wd, sid, final_path)


def _run_narration_rerender(task_id: str, wd: Path, sid: int, jc: dict, row: dict) -> None:
    """Background thread: re-render narration then re-mux the scene."""
    try:
        _render_scene_narration(task_id, wd, sid, jc, row)

        _film_tasks[task_id] = {"status": "done"}
    except Exception as e:
        (wd / f"scene_{sid:02d}_final.staging.mp4").unlink(missing_ok=True)
        _finish_film_task_error(task_id, e)


def _run_image_rerender(task_id: str, wd: Path, sid: int, jc: dict, row: dict) -> None:
    """Background thread: re-render first-frame image only (no video)."""
    import shutil
    import secrets
    from pipeline.comfyui import generate_with_engine, ltx_dimensions

    cfg = gapp.load_config()
    engine = gapp.engines.resolve(cfg, gapp.style_settings(cfg, jc.get("style_name", "")).get("image_engine"))
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
            generate_with_engine(
                engine,
                image_prompt or row.get("title") or f"Scene {sid}",
                first_frame,
                width=vid_w, height=vid_h,
                seed=new_seed,
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
        # Don't delete anything up front: generate_narration overwrites the wav,
        # and the new final.mp4 is swapped in atomically (see _run_narration_rerender).
        # Pre-deleting final.mp4 would leave the scene with no video if the re-mux is
        # interrupted (backend restart / crash mid-render). Keep the current video as a
        # take so the re-mux can be reverted.
        video_history.seed_if_empty(wd, sid, wd / f"scene_{sid:02d}_final.mp4")
    elif body.component == "image":
        # Preserve the current image before deleting it so the user can return to it.
        cur = wd / f"scene_{sid:02d}_preview.png"
        if not cur.exists():
            cur = wd / f"scene_{sid:02d}_first_frame.png"
        image_history.seed_if_empty(wd, sid, cur)
        for f in [f"scene_{sid:02d}_first_frame.png", f"scene_{sid:02d}_preview.png"]:
            (wd / f).unlink(missing_ok=True)
    elif body.component == "video":
        # Keep the existing first frame AND the existing video. The new clip/final
        # are rendered to staging paths and swapped in atomically only on success
        # (see _run_video_rerender). Deleting the old video here would lose it if the
        # render is interrupted mid-flight — e.g. a backend restart while the LTX
        # render runs — leaving the scene with no video at all. Snapshot the current
        # video as a take so the user can flip back to it.
        video_history.seed_if_empty(wd, sid, wd / f"scene_{sid:02d}_final.mp4")

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
        with _track_op("Editing image", f"scene {sid} · {engine['key']}"):
            return _run_scene_inpaint(wd, sid, base, prompt, body.mask, job_id, engine, denoise=body.denoise)
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
        # Review gate on: only retry items the user approved — the same rule
        # fresh starts follow. (A started render stamps approved=True, so any
        # item that ran and failed already qualifies.)
        failed = [q for q in failed if q.get("approved") and q.get("script_ready")
                  and q.get("work_dir") and q.get("video_job_id")]
    if not failed:
        return None
    failed.sort(key=lambda q: q.get("updated_at") or q.get("created_at") or 0)
    item = {**failed[0]}  # fresh copy
    item["retry_count"] = int(item.get("retry_count") or 0) + 1
    yt.update_queue_item(item["id"], retry_count=item["retry_count"])
    return item


def _auto_write_scripts(cfg: dict) -> int:
    """Write — but DON'T render — a script for every pending queue item that
    lacks one, leaving it unapproved so the user can review / edit / approve it
    before it renders. This is the "prepare and park" mode: it only fills in
    missing scripts and never starts a render (script generation is an LLM call,
    not a GPU job, so it can even run while a render is in progress).

    Independent of auto-start: with auto-start also on (review mode), the user
    approves a parked script and the next tick renders it. Items keep their
    queue position — each script is linked in place. Returns the count written."""
    written = 0
    for q in _ordered_pending(cfg):
        if q.get("script_ready") and q.get("work_dir") and q.get("video_job_id"):
            continue  # already has a parked script
        item_id = q.get("id")
        title = q.get("final_title", "")
        style_name = (q.get("gen_style_name") or "").strip()
        ss = gapp.style_settings(cfg, style_name)
        n = max(6, int(q.get("suggested_scene_count") or ss.get("n_scenes") or 6))
        topic = q.get("video_prompt") or title
        resolution = q.get("gen_resolution") or ss.get("resolution") or gapp._DEFAULT_RESOLUTION
        try:
            gen = _do_script_generate(GenerateScriptBody(
                video_title=title, topic=topic, n_scenes=n, resolution=resolution,
                style_name=style_name))
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
            script_ready=True, approved=False, suggested_scene_count=n,
            gen_style=gen.get("style", ""), gen_resolution=resolution,
            gen_voice=ss.get("voice", ""), gen_voice_robotic=q.get("gen_voice_robotic"),
            gen_music=gen.get("music_desc", ""), gen_style_name=gen.get("style_name", ""))
        written += 1
    return written


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
        # Review gate on: only render the next item the user has explicitly
        # approved (and that has a written script). A script being present is
        # not enough — approval is a separate, deliberate action, so a freshly
        # written/linked script waits until the user Approves it. Never generate
        # scripts here either; script-less items wait for the user.
        item = next((q for q in pending
                     if q.get("approved") and q.get("script_ready")
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
        if pq.item_by_work_dir(str(p)) is not None:
            continue  # already queued (or explicitly removed) — never resurrect
        youtube, x = _publish_targets_for_job(p, meta)
        if not (youtube["enabled"] or x["enabled"]):
            continue  # nothing left to publish
        jc = _film_job_config(p)
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


def _reconcile_publish_queue() -> None:
    """Sync each entry's platform sub-state from job.json — the async upload
    threads write youtube_video_id / x_tweet_id when they finish — error out
    entries whose work dir vanished, and re-pend uploads stuck mid-flight so the
    governor stops waiting on a release that already died."""
    q = pq.load_queue()
    changed = False
    for e in q:
        p = Path(e.get("work_dir", ""))
        try:
            meta = json.loads((p / "job.json").read_text())
        except Exception:
            meta = None
        yt_sub, x_sub = e.get("youtube") or {}, e.get("x") or {}
        if meta is None:
            for sub in (yt_sub, x_sub):
                if sub.get("status") in ("pending", "publishing"):
                    sub.update(status="error", error="work dir missing")
                    changed = True
            continue
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
        pq.save_queue(q)


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
    # spacing survives restarts.
    last: dict[tuple, float] = {}
    for e in q:
        for plat, keyf in (("youtube", "channel"), ("x", "account")):
            sub = e.get(plat) or {}
            ts = sub.get("released_at") or sub.get("published_at")
            if sub.get("status") in ("publishing", "done") and ts:
                k = (plat, sub.get(keyf) or "")
                last[k] = max(last.get(k, 0.0), ts)

    def _eligible(k: tuple, interval_min: int) -> bool:
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


def _ensure_tags() -> int:
    """Cache keyword tags (tags.json) for queued/recent jobs that lack them, so a
    publish never has to wait on tag generation. Runs server-side from the
    automation loop. The publish queue (imminent videos) is warmed first, then
    recent jobs; on-demand generation at publish time is the backstop for
    anything not yet warmed."""
    cfg = gapp.load_config()
    backend = cfg.get("llm_backend", "local")
    if backend == "claude" and not cfg.get("claude_api_key", ""):
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
            if cfg.get("youtube_auto_fetch_evaluate"):
                try:
                    out["fetch"] = _fetch_and_evaluate(cfg.get("youtube_auto_approve_comments", False))
                except Exception as e:
                    out["fetch_error"] = str(e)[:120]
            if cfg.get("x_auto_fetch_evaluate"):
                try:
                    out["x_fetch"] = _fetch_and_evaluate_x(cfg.get("x_auto_approve_comments", False))
                except Exception as e:
                    out["x_fetch_error"] = str(e)[:120]
            # Prepare-and-park: write scripts for pending items but leave them
            # unapproved. Runs before auto-start so the freshly written scripts
            # are visible to it — they stay unapproved, so review-mode auto-start
            # won't pick them up until the user approves.
            if cfg.get("youtube_auto_write_scripts"):
                try:
                    out["scripts_written"] = _auto_write_scripts(cfg)
                except Exception as e:
                    out["scripts_error"] = str(e)[:120]
            if cfg.get("youtube_auto_start_job"):
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
            if any(cfg.get(k) for k in (
                    "youtube_auto_fetch_evaluate", "youtube_auto_start_job", "youtube_auto_write_scripts",
                    "youtube_auto_post", "x_auto_fetch_evaluate", "x_auto_post", "publish_schedule_enabled")):
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
