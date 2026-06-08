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
from pipeline.orchestrator import DurableStore, job_id_from_work_dir, task_id as make_task_id  # noqa: E402
from pipeline.timing import estimate_eta  # noqa: E402

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


def _worker_in_config(w: dict, cfg: dict) -> bool:
    """True if a registered worker still matches the live config.

    The durable `workers` table is an append-only registry keyed by
    (kind, endpoint); it is never reconciled against config. When a host is
    reassigned — e.g. a UI worker moved back into the render pool — the old
    registration lingers, so the same endpoint shows up under two kinds. Filter
    to the currently-configured pools so the render page reflects reality.
    Internal `local` workers (e.g. the assembler) are never in config — keep them.
    """
    if w.get("kind") == "local":
        return True
    return w.get("endpoint") in (cfg.get(f"{w.get('kind')}_workers") or [])


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
              else float(cfg.get("default_voice_robotic_amount", 0.35)))
    robotic = bool(body.robotic)

    # Content-addressed cache key: a given (voice, robotic level, text, source
    # clip) always maps to the same file, so F5-TTS never re-runs for a setup
    # we've already rendered. Folding in the clip's mtime+size means replacing a
    # voice's reference audio busts its cached sample.
    try:
        st = (ref or DEFAULT_REF).stat()
        ref_stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        ref_stamp = ""
    key = hashlib.md5(
        f"{voice}|{robotic}|{round(amount, 3)}|{text}|{ref_stamp}".encode()
    ).hexdigest()[:16]
    out = gapp.CONFIG_FILE.parent / f"voice_test_{key}.wav"

    cached = out.exists() and out.stat().st_size > 1000
    if not cached:
        tts_hosts = cfg.get("tts_workers") or []
        tts_host = tts_hosts[0] if tts_hosts else "localhost"
        try:
            with _track_op("Testing voice", spoken):
                generate_narration(text, out, reference_wav=ref, host=tts_host,
                                   robotic=robotic, robotic_amount=amount)
        except Exception as e:
            raise HTTPException(503, f"Voice test failed: {str(e).splitlines()[0][:200]}")

    return {"ok": True, "url": f"/api/file?path={out}&t={int(out.stat().st_mtime)}", "cached": cached}


@api.get("/api/workers/status")
def workers_status() -> dict:
    """Live, read-only health of the configured workers.

    comfy/ui endpoints are HTTP-probed (ComfyUI /system_stats); tts is listed
    (reachability needs SSH, not probed here). ui_worker_running reports whether
    a local `worker_agent --kind ui` daemon is up. Never raises — an
    unreachable host is reported as up:false.
    """
    from pipeline.worker_pool import check_alive
    cfg = gapp.load_config()

    def probe(urls: list[str]) -> list[dict]:
        out = []
        for u in urls or []:
            try:
                up = check_alive(u, timeout=3)
            except Exception:
                up = False
            out.append({"endpoint": u, "up": up})
        return out

    ui_running = False
    try:
        import subprocess
        ui_running = subprocess.run(
            ["pgrep", "-f", "worker_agent.py --kind ui"],
            capture_output=True,
        ).returncode == 0
    except Exception:
        ui_running = False

    return {
        "comfy": probe(cfg.get("comfy_workers", [])),
        "tts": [{"host": h} for h in cfg.get("tts_workers", [])],
        "ui": probe(cfg.get("ui_workers", [])),
        "ui_worker_running": ui_running,
    }


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


@api.post("/api/script/generate")
def script_generate(body: GenerateScriptBody) -> dict:
    """Run the LLM script generation and persist a durable job (mirrors
    app.on_generate_script, minus the Gradio plumbing)."""
    topic = (body.topic or "").strip() or (body.video_title or "").strip()
    if not topic:
        raise HTTPException(400, "Enter a video title or describe what you want to create.")

    cfg = gapp.load_config()
    extra = cfg.get("script_extra_instructions", "").strip()
    if extra:
        topic = f"{topic}\n\n{extra}"

    style_hint = body.visual_style or cfg.get("default_visual_style", "") or None
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
    scenes_list = [
        {"id": s.id, "title": s.title, "image_prompt": s.image_prompt,
         "video_prompt": s.video_prompt, "narration": s.narration}
        for s in scenes
    ]
    gapp._persist_script_snapshot(work_dir, scenes_list)

    store = DurableStore.default()
    try:
        store.create_or_update_job(
            job_id, work_dir, display_title,
            config={"title": display_title, "video_title": (body.video_title or "").strip(),
                    "topic": topic, "phase": "script_review"},
            metadata={"scene_count": len(scenes_list), "music_desc": music_desc, "style": style},
        )
        store.upsert_scenes(job_id, scenes_list)
    finally:
        store.close()

    result = {
        "job_id": job_id,
        "work_dir": str(work_dir),
        "title": display_title,
        "video_title": (body.video_title or "").strip(),
        "style": style,
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
            resolution=body.resolution or cfg.get("resolution", gapp._DEFAULT_RESOLUTION),
            voice=body.voice or cfg.get("default_voice", ""),
            voice_robotic=body.voice_robotic,
            music_desc=music_desc,
            queue_item_id=body.queue_item_id,
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
        store.create_or_update_job(
            job_id, wd, video_title,
            config={"video_title": video_title, "phase": "script_review"},
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
    return {
        "job_id": job_id,
        "work_dir": str(wd),
        "title": video_title,
        "video_title": video_title,
        "style": style,
        "music_desc": music_desc,
        "voice": cfg.get("default_voice", ""),
        "voice_robotic": bool(cfg.get("default_voice_robotic", False)),
        "resolution": cfg.get("resolution", gapp._DEFAULT_RESOLUTION),
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


def _llm_complete(system: str, user: str, cfg: dict) -> str:
    """Lightweight direct LLM call honouring the configured backend.

    NOTE: kept self-contained (stdlib urllib) rather than importing
    pipeline.llm's internals. If pipeline.llm later changes models/prompting,
    this can be unified with it.
    """
    import urllib.request
    if cfg.get("llm_backend", "local") == "claude":
        key = cfg.get("claude_api_key", "")
        if not key:
            raise RuntimeError("No Claude API key configured (Settings → LLM backend).")
        payload = json.dumps({
            "model": cfg.get("claude_model", "claude-sonnet-4-6"),
            "max_tokens": 700, "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=payload,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return "".join(b.get("text", "") for b in data.get("content", [])).strip()

    url = cfg.get("local_llm_url", "http://localhost:8000/v1/chat/completions")
    payload = json.dumps({
        "model": cfg.get("local_llm_model", ""),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.9, "max_tokens": 700,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


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

    video_title, topic, style, outline = "", "", "", ""
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
    finally:
        store.close()
    if not scene_rows:
        raise HTTPException(400, "No scene data available. Generate the script again.")

    cfg = gapp.load_config()
    voice_name = body.voice
    if not voice_name or voice_name == gapp.F5TTS_DEFAULT_OPTION:
        voice_name = cfg.get("default_voice", voice_name)
    voice_ref = gapp.voice_path_for(voice_name)
    voice_robotic = (body.voice_robotic if body.voice_robotic is not None
                     else bool(cfg.get("default_voice_robotic", False)))
    resolution = body.resolution or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)
    vid_width, vid_height = gapp._RESOLUTIONS.get(resolution, (832, 480))

    style_clean = body.style.strip().rstrip(".") if body.style and body.style.strip() else ""
    combined_style = gapp._compose_visual_style(body.style, cfg)

    n = int(body.n_scenes) if body.n_scenes else len(scene_rows)
    title = body.title or body.video_title
    scenes = [
        Scene(
            id=int(row["id"]),
            title=row.get("title") or f"Scene {int(row['id'])}",
            image_prompt=(f"{combined_style}. {row.get('image_prompt') or title}"
                          if combined_style else (row.get("image_prompt") or title)),
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
        "voice_robotic_amount": cfg.get("default_voice_robotic_amount", 0.35),
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
            workers = [w for w in (_row_to_dict(r) for r in store.worker_rows())
                       if _worker_in_config(w, cfg)]
            if not done:
                try:
                    eta = estimate_eta(tasks, store.timing_table(), cfg)
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
    """Cancel an active render, remove any queue entry pointing to it, then delete its files."""
    work_dir = body.work_dir
    try:
        gapp.on_cancel_active_job(work_dir)
    except Exception:
        pass
    for item in yt.load_queue():
        if item.get("work_dir") == work_dir:
            yt.remove_queue_item(item["id"])
    wd = Path(work_dir)
    out = gapp.OUTPUT_DIR.resolve()
    try:
        wd_res = wd.resolve()
    except OSError:
        raise HTTPException(400, "Invalid path.")
    if not work_dir or wd_res == out or wd_res.parent != out:
        raise HTTPException(400, "Refusing to delete outside the videos directory.")
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
        elif it.get("status") in ("creating", "running") and not is_active:
            posted = posted_by_title.get(title_key)
            if posted:
                it["status"] = "posted"
                it["youtube_video_id"] = posted.get("youtube_video_id")
                it["youtube_url"] = posted.get("youtube_url")
                changed = True
            elif meta.get("status") == "running" and not gapp._process_running(meta.get("pid")):
                it["status"] = "failed"
                it["error"] = "Render process is no longer running."
                changed = True
    queue = sorted(queue, key=_queue_lifecycle_sort_key)
    if changed:
        yt.save_queue(queue)
    return queue


@api.get("/api/queue")
def get_queue() -> dict:
    try:
        return {"queue": _reconcile_queue()}
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


def _guided_suggestions(guidance: str, previous: list[str], cfg: dict, n: int = 6) -> list[dict]:
    """Generate video ideas steered by a free-text theme (e.g. 'Rock bands of
    the 90s'). Uses the configured LLM backend via _llm_complete."""
    import re
    avoid = "; ".join(previous)
    system = ("You are a content strategist for an educational/documentary YouTube channel. "
              "Return ONLY a JSON array, no prose.")
    user = (
        f'Generate {n} specific, compelling video ideas guided by this theme: "{guidance}".\n'
        f"Each must be a concrete documentary topic that clearly fits the theme.\n"
        + (f"Avoid duplicating these existing titles: {avoid}\n" if avoid else "")
        + '\nReturn a JSON array; each item: {"title": string, "reason": one-sentence string, '
        '"suggested_scene_count": integer 6-50, "interestingness": number 0..1}. Output ONLY the JSON array.'
    )
    text = _llm_complete(system, user, cfg)
    m = re.search(r"\[.*\]", text, re.DOTALL)
    arr = json.loads(m.group()) if m else []
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
            "used": used,
            "dismissed": bool(it.get("dismissed")),
        })
    return out


@api.get("/api/youtube/suggestions")
def youtube_suggestions(guidance: str = Query(""), refresh: bool = Query(False)) -> dict:
    """Return AI video ideas. Without guidance or refresh, returns the last
    cached set (no LLM call) so reopening the tab is instant; only generates when
    the cache is empty, the user asks (refresh), or a guidance theme is given."""
    cfg = gapp.load_config()
    g = guidance.strip()

    if not g and not refresh:
        try:
            cached = yt.load_suggestions()
        except Exception:
            cached = []
        if cached:
            return {"suggestions": _visible_suggestions(cached), "cached": True}

    with _track_op("Generating suggestions", g):
        try:
            # Channel titles (YouTube API + posted queue) come first; supplement with
            # any local completed jobs not yet published to the channel.
            previous = gapp._channel_video_titles(cfg)
            seen = {t.lower() for t in previous}
            for label, _ in gapp._list_recent_jobs(max_results=500):
                if label.lower() not in seen:
                    previous.append(label)
                    seen.add(label.lower())
        except Exception:
            previous = []
        try:
            if g:
                ideas = _guided_suggestions(g, previous, cfg)
            else:
                ideas = _normalize_suggestions(generate_video_suggestions(previous, cfg))
        except Exception as e:
            raise HTTPException(503, f"Could not generate suggestions: {str(e).splitlines()[0][:160]}")

    try:
        ideas = [{**idea, "id": str(idea.get("id") or str(uuid.uuid4())[:8]),
                  "created_at": time.time(), "used": False, "dismissed": False}
                 for idea in ideas]
        yt.save_suggestions(ideas)  # cache the last generated set
    except Exception:
        pass
    return {"suggestions": _visible_suggestions(ideas), "cached": False}


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
        attention = len(yt.get_pending_requests())
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


@api.get("/api/youtube/auth")
def yt_auth_status() -> dict:
    try:
        return yt.check_auth_status(_client_secrets_path())
    except Exception as e:
        return {"connected": False, "channel_name": "", "error": str(e)[:200]}


@api.post("/api/youtube/auth/start")
def yt_auth_start() -> dict:
    try:
        return {"auth_url": yt.start_auth_flow(_client_secrets_path())}
    except Exception as e:
        raise HTTPException(503, str(e).splitlines()[0][:200])


@api.post("/api/youtube/auth/poll")
def yt_auth_poll() -> dict:
    try:
        return yt.poll_auth_flow()
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


@api.post("/api/youtube/disconnect")
def yt_disconnect() -> dict:
    try:
        yt.disconnect_youtube()
    except Exception:
        pass
    return {"ok": True}


@api.get("/api/youtube/analytics")
def yt_analytics() -> dict:
    try:
        return yt.fetch_channel_analytics(_client_secrets_path())
    except Exception as e:
        return {"channel": {}, "videos": [], "error": str(e)[:200]}


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
    return {
        "work_dir": str(wd),
        "title": _video_title_for(wd),
        "final_url": f"/api/file?path={final}" if final.exists() and final.stat().st_size > 10_000 else "",
        "cover_url": f"/api/file?path={cover}" if cover.exists() and cover.stat().st_size > 1000 else "",
        "description": _cached_description(wd),
        "youtube_url": meta.get("youtube_url", ""),
        "youtube_video_id": meta.get("youtube_video_id", ""),
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
    suffix = cfg.get("description_suffix", "").strip()
    if suffix and suffix not in str(desc):
        desc = f"{desc}\n\n{suffix}"
    return str(desc)


class CoverBody(BaseModel):
    work_dir: str = ""
    title: str = ""
    style: str = ""


def _best_cover_comfy_url() -> str:
    """Pick the fastest available ComfyUI endpoint for cover generation.

    When a render job is active the render workers are busy, so we use the
    dedicated UI worker (MPS/local) to avoid competing with them.
    When the cluster is idle we use the first live render worker instead —
    those have CUDA and are significantly faster.
    """
    from pipeline.worker_pool import idle_workers
    cfg = gapp.load_config()
    ui_url = (cfg.get("ui_workers") or ["http://localhost:8188"])[0]

    if gapp._is_job_running():
        return ui_url  # render in progress — keep cover work off the render cluster

    # Cluster idle — pick a least-busy render worker, fall back to UI worker
    try:
        candidates = idle_workers(cfg.get("comfy_workers") or [], timeout=2)
        if candidates:
            return candidates[0]
    except Exception:
        pass
    return ui_url


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
    vid_width, vid_height = _film_dimensions(wd)
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
                "flux_model": cfg.get("ui_flux_model") or cfg.get("flux_model", "flux1-schnell-fp8.safetensors"),
                "flux_clip_t5": cfg.get("ui_flux_clip_t5") or cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
                "flux_clip_l": cfg.get("ui_flux_clip_l") or cfg.get("flux_clip_l", "clip_l.safetensors"),
                "flux_vae": cfg.get("ui_flux_vae") or cfg.get("flux_vae", "ae.safetensors"),
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
        result = yt.set_thumbnail(_client_secrets_path(), video_id, str(cover))
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
    result = yt.reply_to_comment(_client_secrets_path(), comment_id, text)
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
    threading.Thread(
        target=_run_upload_task,
        args=(task_id, {"title": body.title, "description": body.description,
                        "category": body.category, "privacy": body.privacy}, wd, final, thumb),
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
    new_count = 0
    try:
        fetched = yt.fetch_channel_comments(secrets)
        cache = yt.load_comments_cache()
        existing = {c.get("comment_id") for c in cache}
        for fc in fetched:
            if fc.get("comment_id") not in existing:
                cache.insert(0, {**fc, "evaluated": False, "is_request": False,
                                 "suggested_title": "", "confidence": 0.0,
                                 "interestingness": 0.0, "reason": "", "status": "new"})
                new_count += 1
        yt.save_comments_cache(cache)
    except Exception as e:
        cache = yt.load_comments_cache()
        raise HTTPException(503, f"Fetch failed: {str(e).splitlines()[0][:160]}")

    approved = thanked = 0
    for c in [x for x in cache if not x.get("evaluated")]:
        r = yt.evaluate_comment(c.get("text", ""), c.get("commenter", ""), cfg)
        c.update({"evaluated": True, "is_request": r["is_request"],
                  "suggested_title": r["suggested_title"], "confidence": r["confidence"],
                  "interestingness": r.get("interestingness", 0.0), "reason": r["reason"],
                  "status": "evaluated" if c.get("status") == "new" else c.get("status")})
        if r["is_request"] and not c.get("thanked"):
            rep = yt.reply_to_comment(secrets, c.get("comment_id", ""),
                                      "Thanks for the suggestion! We'll look into making a video about this. 🎬")
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
    yt.save_comments_cache(cache)
    return {"new": new_count, "thanked": thanked, "auto_approved": approved}


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
    res = yt.reply_to_comment(gapp.load_config().get("youtube_client_secrets", ""),
                              body.comment_id, body.text.strip())
    if not res.get("success"):
        raise HTTPException(502, f"Reply failed: {res.get('error', 'unknown')[:160]}")
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


@api.post("/api/queue/add")
def queue_add(body: QueueAddBody) -> dict:
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "Title is required.")
    n = max(6, min(50, body.n_scenes or gapp.load_config().get("default_n_scenes", 6)))
    comment = {"comment_id": "", "text": body.prompt, "commenter": "you",
               "suggested_scene_count": n}
    entry = yt.add_to_queue(comment, title, source="manual")
    if entry:
        updates = {}
        if body.prompt.strip():
            updates["video_prompt"] = body.prompt.strip()
        if body.resolution.strip():
            updates["gen_resolution"] = body.resolution.strip()
        if updates:
            yt.update_queue_item(entry["id"], **updates)
    return {"ok": bool(entry), "queue": yt.load_queue()}


class QueueUpdateBody(BaseModel):
    id: str
    final_title: str | None = None
    video_prompt: str | None = None
    suggested_scene_count: int | None = None
    gen_resolution: str | None = None


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
    """Launch the render for a queue item. If the item already has an approved
    script (script_ready + work_dir + video_job_id) we render it directly;
    otherwise we generate the script first. Reuses script_generate +
    start_generation."""
    cfg = gapp.load_config()
    # Claim the item BEFORE the slow script generation. _best_pending_queue_item
    # — used by this backend's automation AND the classic Gradio app (a separate
    # process sharing the same queue file) — only returns status=="pending"
    # items, so flipping the status away from pending now is the claim that stops
    # a concurrent tick or the other app from starting the same item and creating
    # a duplicate work folder. Without it, the item stays "pending" for the whole
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
    n = max(6, int(item.get("suggested_scene_count") or cfg.get("default_n_scenes", 6)))

    if item.get("script_ready") and item.get("work_dir") and item.get("video_job_id"):
        job_id = item["video_job_id"]
        wd = item["work_dir"]
        start_generation(GenerateBody(
            job_id=job_id, work_dir=wd, video_title=title, title=title, n_scenes=n,
            voice=item.get("gen_voice") or cfg.get("default_voice", ""),
            voice_robotic=item.get("gen_voice_robotic"),
            resolution=item.get("gen_resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION),
            music_desc=item.get("gen_music") or _job_meta_field(job_id, "music_desc"),
            style=item.get("gen_style") or _job_meta_field(job_id, "style")))
        # start_generation wrote queue_item_id="" (the item is already "creating",
        # not "pending", so its title-match misses). Stamp the reverse link now so
        # _auto_post_done recognises this as a queue-driven job and posts it.
        _link_queue_item_to_work_dir(item, Path(wd))
        yt.update_queue_item(item["id"], status="creating")
        return {"job_id": job_id, "work_dir": wd, "title": title}

    topic = item.get("video_prompt") or title
    resolution = item.get("gen_resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)
    gen = script_generate(GenerateScriptBody(
        video_title=title, topic=topic, n_scenes=n, resolution=resolution,
        visual_style=cfg.get("default_visual_style") or None))
    start_generation(GenerateBody(
        job_id=gen["job_id"], work_dir=gen["work_dir"], video_title=title, title=title,
        n_scenes=n, voice=cfg.get("default_voice", ""),
        voice_robotic=item.get("gen_voice_robotic"),
        resolution=resolution,
        music_desc=gen.get("music_desc", ""), style=gen.get("style", "")))
    # See above: re-link the work dir to this queue item so auto-post finds it.
    _link_queue_item_to_work_dir(item, Path(gen["work_dir"]))
    yt.update_queue_item(item["id"], status="creating",
                         video_job_id=gen["job_id"], work_dir=gen["work_dir"])
    return {"job_id": gen["job_id"], "work_dir": gen["work_dir"], "title": title}


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
    )

    # In-place update of an existing pending slot — keep its queue position.
    existing = None
    if body.queue_item_id:
        existing = next((q for q in yt.load_queue() if q.get("id") == body.queue_item_id), None)
    if existing is not None and existing.get("status") == "pending":
        yt.update_queue_item(body.queue_item_id, final_title=title,
                             suggested_scene_count=n, **script_fields)
        return {"ok": True, "queue_item_id": body.queue_item_id,
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
        narration_text = (row.get("narration") or row.get("title") or f"Scene {sid}").strip()
        voice_ref_str = jc.get("voice_ref") or ""
        voice_ref = Path(voice_ref_str).expanduser() if voice_ref_str else None
        voice_robotic = bool(jc.get("voice_robotic", False))
        voice_robotic_amount = jc.get("voice_robotic_amount", cfg.get("default_voice_robotic_amount", 0.35))
        tts_hosts = cfg.get("tts_workers") or []
        tts_host = tts_hosts[0] if tts_hosts else "localhost"

        _film_tasks[task_id] = {"status": "running", "step": "narration"}
        generate_narration(narration_text, narration_path, reference_wav=voice_ref, host=tts_host, robotic=voice_robotic, robotic_amount=voice_robotic_amount)

        # Re-mux narration with the existing scene video
        video_path = wd / f"scene_{sid:02d}_video.mp4"
        clip_path = wd / f"scene_{sid:02d}_clip_01.mp4"
        actual_video = (
            video_path if (video_path.exists() and video_path.stat().st_size > 10_000)
            else clip_path if (clip_path.exists() and clip_path.stat().st_size > 10_000)
            else None
        )
        if actual_video:
            _film_tasks[task_id]["step"] = "mux"
            mux_video_audio(actual_video, narration_path, final_path)

        _film_tasks[task_id] = {"status": "done"}
    except Exception as e:
        _film_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:200]}


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
        _film_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:200]}


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

        _film_tasks[task_id]["step"] = "mux"
        mux_video_audio(scene_video, narration_path, final_path)

        _film_tasks[task_id] = {"status": "done"}
    except Exception as e:
        _film_tasks[task_id] = {"status": "error", "error": str(e).splitlines()[0][:200]}


def _run_rerender_logged(target, tid: str, wd: Path, sid: int, component: str, jc: dict, row: dict) -> None:
    """Run a re-render worker, then record a completion entry in the Activity log.

    The workers only update _film_tasks (so the live "Re-rendering…" indicator can
    read their step), so this wrapper adds the "Recent" history entry that
    _track_op gives every other operation."""
    started = time.time()
    try:
        target(tid, wd, sid, jc, row)
    finally:
        end = time.time()
        status = (_film_tasks.get(tid) or {}).get("status")
        name = f"Re-render failed — scene {sid}" if status == "error" else f"Re-rendered scene {sid}"
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
    import shutil
    if wd.exists():
        shutil.rmtree(wd, ignore_errors=True)
    canonical = gapp.OUTPUT_DIR / f"{wd.name}.mp4"
    if canonical.exists():
        canonical.unlink(missing_ok=True)
    return {"ok": True, "deleted": wd.name}


# ── Automation (Gap 2): on-demand step endpoints + opt-in background loop ──────

def _auto_start_best() -> dict | None:
    if gapp._is_job_running():
        return None
    item = gapp._best_pending_queue_item()
    if not item:
        # Nothing queued manually — fall back to AI ideas. _auto_pick_suggestion
        # picks the best unused idea, marks it used (so it's closed and never
        # re-picked), and generates a fresh batch when none remain.
        try:
            item = gapp._auto_pick_suggestion(gapp.load_config())
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
            description = _generate_youtube_description(str(p), title)
            res = yt_post(PostBody(
                work_dir=str(p), title=title,
                description=description, category=cfg.get("youtube_post_category", "22"),
                privacy=cfg.get("youtube_post_privacy", "private"),
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
    cfg = gapp.load_config()
    # The master toggle implies every per-step flag below. It's only honored here
    # (and in the loop gate); nothing expands it into the individual flags on save.
    full = bool(cfg.get("youtube_fully_automated"))
    out: dict = {}
    with _track_op("Automation tick"):
        if full or cfg.get("youtube_auto_fetch_evaluate"):
            try:
                out["fetch"] = _fetch_and_evaluate(full or cfg.get("youtube_auto_approve_comments", False))
            except Exception as e:
                out["fetch_error"] = str(e)[:120]
        if full or cfg.get("youtube_auto_start_job"):
            out["started"] = _auto_start_best()
        if full or cfg.get("youtube_auto_post"):
            out["posted"] = _auto_post_done()
    return out


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
import threading  # noqa: E402

_AUTOMATION_INTERVAL = 180  # seconds
_automation_started = False


def _automation_loop():
    while True:
        time.sleep(_AUTOMATION_INTERVAL)
        # Always cache descriptions for completed jobs — independent of automation flags
        # and browser connections.
        try:
            if not any(t.name == "ensure_descriptions" for t in threading.enumerate()):
                threading.Thread(target=_ensure_descriptions, daemon=True,
                                 name="ensure_descriptions").start()
        except Exception:
            pass
        try:
            cfg = gapp.load_config()
            if cfg.get("youtube_fully_automated") or any(cfg.get(k) for k in (
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


def _run_engagement_build(task_id: str) -> None:
    """Background thread: fetch history → embed → train → evaluate → persist."""
    def phase(p: str) -> None:
        _engagement_tasks[task_id] = {"status": "building", "phase": p}

    try:
        with _track_op("Building engagement model"):
            result = eng.build(_client_secrets_path(), on_phase=phase)
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
def engagement_build() -> dict:
    # Reject a second build while one runs — avoids loading two embedders at once.
    if any(t.get("status") == "building" for t in _engagement_tasks.values()):
        raise HTTPException(409, "A model build is already in progress.")
    task_id = uuid.uuid4().hex[:12]
    _engagement_tasks[task_id] = {"status": "building", "phase": "fetching"}
    threading.Thread(target=_run_engagement_build, args=(task_id,), daemon=True).start()
    return {"ok": True, "task_id": task_id}


@api.get("/api/engagement/build/status")
def engagement_build_status(task_id: str = Query(...)) -> dict:
    task = _engagement_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Build task not found.")
    return {"ok": True, **task}


@api.get("/api/engagement/status")
def engagement_status() -> dict:
    return eng.status()


@api.post("/api/engagement/predict")
def engagement_predict(body: EngagementBody) -> dict:
    return eng.predict(body.title, body.description)


@api.post("/api/engagement/best-times")
def engagement_best_times(body: EngagementBody) -> dict:
    return eng.best_times(body.title, body.description)


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
