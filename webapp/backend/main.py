#!/usr/bin/env python3
"""FastAPI backend for the modern Stephen Spielbot web UI.

This is a thin REST/JSON layer over the EXISTING pipeline. It imports the
original ``app`` module (the Gradio app) purely to reuse its Gradio-free helper
functions (config, work-dir bookkeeping, job launching, progress polling) plus
the ``pipeline`` package directly. No Gradio UI is built here — the Gradio app
in ``app.py`` keeps working untouched and can run side by side.

Run it from the repo root:

    uvicorn webapp.backend.main:app --port 8001 --reload
"""

import json
import sys
import time
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
from pipeline.llm import generate_script, generate_video_suggestions, Scene  # noqa: E402
from pipeline.orchestrator import DurableStore, job_id_from_work_dir  # noqa: E402

api = FastAPI(title="Stephen Spielbot API")

# Where the built frontend lives (after `npm run build`). Optional in dev — the
# Vite dev server proxies /api to this process instead.
FRONTEND_DIST = REPO_ROOT / "webapp" / "frontend" / "dist"


# ── helpers ──────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    """sqlite3.Row → plain dict (JSON-serialisable)."""
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


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


# ── config ───────────────────────────────────────────────────────────────────

@api.get("/api/config")
def get_config() -> dict:
    cfg = gapp.load_config()
    return {
        "config": cfg,
        "voices": gapp.get_voice_choices(),
        "resolutions": list(gapp._RESOLUTIONS.keys()),
        "default_resolution": gapp._DEFAULT_RESOLUTION,
    }


class ConfigUpdate(BaseModel):
    config: dict


@api.post("/api/config")
def post_config(body: ConfigUpdate) -> dict:
    cfg = gapp.load_config()
    cfg.update(body.config)
    gapp.save_config(cfg)
    return {"ok": True, "config": gapp.load_config()}


# ── script generation ────────────────────────────────────────────────────────

class GenerateScriptBody(BaseModel):
    video_title: str = ""
    topic: str = ""
    n_scenes: int = 12
    visual_style: str | None = None


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
    try:
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

    return {
        "job_id": job_id,
        "work_dir": str(work_dir),
        "title": display_title,
        "video_title": (body.video_title or "").strip(),
        "style": style,
        "music_desc": music_desc,
        "scenes": [_scene_to_json(s) for s in scenes_list],
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
        out = gapp._generate_active_scene_preview(
            job_id, int(scene_id), resolution, style, "", "", force=True
        )
    except Exception as e:
        raise HTTPException(503, f"Preview failed: {str(e).splitlines()[0][:200]}")
    return {"ok": True, "preview_path": str(out)}


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
    resolution = body.resolution or cfg.get("resolution", gapp._DEFAULT_RESOLUTION)
    vid_width, vid_height = gapp._RESOLUTIONS.get(resolution, (832, 480))

    style_clean = body.style.strip().rstrip(".") if body.style and body.style.strip() else ""
    default_style = cfg.get("default_visual_style", "").strip().rstrip(".")
    combined_style = ". ".join(p for p in [style_clean, default_style] if p)

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

    tasks, workers, counts, job = [], [], {}, None
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
            workers = [_row_to_dict(w) for w in store.worker_rows()]
    except Exception:
        pass
    finally:
        store.close()

    return {
        "pct": pct, "msg": msg, "work_dir": str(wd), "done": bool(done),
        "final_url": f"/api/file?path={final_path}" if done else "",
        "cover_url": f"/api/file?path={cover}" if cover.exists() and cover.stat().st_size > 1000 else "",
        "title": (job or {}).get("title", wd.name),
        "status": (job or {}).get("status", ""),
        "tasks": tasks, "workers": workers, "counts": counts,
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


# ── library / recent jobs ────────────────────────────────────────────────────

@api.get("/api/jobs")
def list_jobs() -> dict:
    finished = [{"label": l, "work_dir": d} for l, d in gapp._list_recent_jobs(max_results=50)]
    scripts = [{"label": l, "work_dir": d} for l, d in gapp._list_script_jobs()]
    resumable = [{"label": l, "work_dir": d} for l, d in gapp._list_resumable_jobs()]
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
    candidates = sorted(wd.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    final_vid = next((p for p in candidates if not p.name.startswith("scene_")
                      and not p.name.startswith("remixed")), combined)
    cfg = gapp.load_config()
    return {
        "work_dir": str(wd),
        "final_url": f"/api/file?path={final_vid}",
        "voice_vol": cfg.get("voice_vol", 100),
        "music_vol": cfg.get("music_vol", 18),
        "ambient_vol": cfg.get("ambient_vol", 0),
    }


@api.post("/api/remix")
def remix_apply(body: RemixBody) -> dict:
    wd = Path(body.work_dir)
    combined = wd / "combined.mp4"
    music = wd / "background_music.wav"
    ambient = wd / "ambient.wav"
    result = gapp.on_remix(str(combined), str(music),
                           str(ambient) if ambient.exists() else "",
                           body.voice_vol, body.music_vol, body.ambient_vol, None)
    # on_remix returns a gr tuple; element 0 is an update with the final path.
    final_update = result[0]
    final_path = getattr(final_update, "get", lambda *_: None)("value") if hasattr(final_update, "get") else None
    return {"message": result[4], "final_url": f"/api/file?path={final_path}" if final_path else ""}


# ── queue ────────────────────────────────────────────────────────────────────

@api.get("/api/queue")
def get_queue() -> dict:
    try:
        return {"queue": yt.load_queue()}
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


@api.get("/api/youtube/suggestions")
def youtube_suggestions() -> dict:
    # generate_video_suggestions(previous_titles, cfg) wants the channel's prior
    # titles so it can avoid repeats. Derive them from finished job folders
    # (works without YouTube OAuth); fall back to an empty list.
    try:
        previous = [label for label, _ in gapp._list_recent_jobs(max_results=50)]
    except Exception:
        previous = []
    try:
        ideas = generate_video_suggestions(previous, gapp.load_config())
    except Exception as e:
        raise HTTPException(503, f"Could not generate suggestions: {str(e)[:160]}")
    return {"suggestions": ideas}


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
