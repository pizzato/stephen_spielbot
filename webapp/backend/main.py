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
import pipeline.llm as llm  # noqa: E402
from pipeline.llm import generate_script, generate_video_suggestions, Scene  # noqa: E402
from pipeline.orchestrator import DurableStore, job_id_from_work_dir  # noqa: E402

api = FastAPI(title="Stephen Spielbot API")
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


@api.post("/api/jobs/{job_id}/previews")
def generate_all_previews(job_id: str, resolution: str = Query(""), style: str = Query("")) -> dict:
    """Generate first-frame previews for every scene that doesn't already have one.
    Existing images are reused (cached on disk via _generate_active_scene_preview's
    force=False short-circuit), so revisiting the script is cheap."""
    import concurrent.futures

    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    if not rows:
        return {"scenes": [], "generated": 0, "failed": []}

    missing = [r for r in rows if not (r.get("preview_path") and Path(r["preview_path"]).exists())]
    failed: list[int] = []
    if missing:
        worker_urls = gapp._preview_worker_urls()
        if not worker_urls:
            raise HTTPException(503, "No reachable workers for preview generation.")
        pool = gapp.WorkerPool(worker_urls)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(worker_urls), len(missing))) as ex:
            futs = {
                ex.submit(gapp._generate_active_scene_preview, job_id, int(r["id"]),
                          resolution, style, r.get("title") or "",
                          r.get("image_prompt") or "", force=False, worker_pool=pool): int(r["id"])
                for r in missing
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
            "generated": len(missing) - len(failed), "failed": failed}


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

def _reconcile_queue() -> list[dict]:
    """Update queue items stuck at 'creating' whose underlying render actually
    finished (or was posted), so the queue reflects reality."""
    try:
        queue = yt.load_queue()
    except Exception:
        return []
    changed = False
    for it in queue:
        if it.get("status") != "creating":
            continue
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
        try:
            meta = json.loads((p / "job.json").read_text())
        except Exception:
            meta = {}
        final = gapp._final_path_for_work_dir(p)
        done = final.exists() and final.stat().st_size > 10_000 and (p / "combined.mp4").exists()
        if meta.get("youtube_video_id"):
            it["status"] = "posted"; it["youtube_video_id"] = meta["youtube_video_id"]; changed = True
        elif done or meta.get("status") == "done":
            it["status"] = "done"; changed = True
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
    avoid = "; ".join(previous[:30])
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


@api.get("/api/youtube/suggestions")
def youtube_suggestions(guidance: str = Query("")) -> dict:
    cfg = gapp.load_config()
    try:
        previous = [label for label, _ in gapp._list_recent_jobs(max_results=50)]
    except Exception:
        previous = []
    try:
        if guidance.strip():
            ideas = _guided_suggestions(guidance.strip(), previous, cfg)
        else:
            ideas = generate_video_suggestions(previous, cfg)
    except Exception as e:
        raise HTTPException(503, f"Could not generate suggestions: {str(e).splitlines()[0][:160]}")
    return {"suggestions": ideas}


# ── sidebar badges ("needs attention" counts) ────────────────────────────────

SEEN_FILE = gapp.CONFIG_FILE.parent / "ui_seen.json"


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
    publishable = sum(1 for q in queue if q.get("status") in ("done", "upload_pending"))

    try:
        attention = len(yt.get_pending_requests())
    except Exception:
        attention = 0

    films_total = _finished_film_count()
    seen = _load_seen()
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


class SeenBody(BaseModel):
    section: str


@api.post("/api/badges/seen")
def mark_seen(body: SeenBody) -> dict:
    """Clear the 'new' count for a section once the user has looked at it."""
    seen = _load_seen()
    if body.section == "films":
        seen["films_total"] = _finished_film_count()
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
    return {
        "work_dir": str(wd),
        "title": _video_title_for(wd),
        "final_url": f"/api/file?path={final}" if final.exists() and final.stat().st_size > 10_000 else "",
        "cover_url": f"/api/file?path={cover}" if cover.exists() and cover.stat().st_size > 1000 else "",
    }


class DescribeBody(BaseModel):
    work_dir: str = ""
    title: str = ""


@api.post("/api/youtube/describe")
def yt_describe(body: DescribeBody) -> dict:
    cfg = gapp.load_config()
    wd = Path(body.work_dir) if body.work_dir else None
    title = body.title or (_video_title_for(wd) if wd else "")
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
    return {"description": str(desc)}


class CoverBody(BaseModel):
    work_dir: str = ""
    title: str = ""
    style: str = ""


@api.post("/api/youtube/cover")
def yt_cover(body: CoverBody) -> dict:
    wd = Path(body.work_dir) if body.work_dir else gapp._latest_work_dir()
    if wd is None or not wd.exists():
        raise HTTPException(404, "No film found.")
    job_id = job_id_from_work_dir(wd)
    try:
        for _ in gapp.on_generate_cover_image(body.title or _video_title_for(wd), body.style or "", job_id):
            pass  # drive the generator to completion
    except Exception as e:
        raise HTTPException(503, f"Cover generation failed: {str(e).splitlines()[0][:200]}")
    cover = wd / "cover.png"
    if cover.exists() and cover.stat().st_size > 1000:
        return {"cover_url": f"/api/file?path={cover}&t={int(time.time())}"}
    raise HTTPException(503, "Cover image was not produced (no reachable workers?).")


class PostBody(BaseModel):
    work_dir: str
    title: str
    description: str = ""
    category: str = "22"
    privacy: str = "private"


@api.post("/api/youtube/post")
def yt_post(body: PostBody) -> dict:
    wd = Path(body.work_dir)
    if not wd.exists():
        raise HTTPException(404, "Film directory not found.")
    final = gapp._final_path_for_work_dir(wd)
    if not (final.exists() and final.stat().st_size > 10_000):
        raise HTTPException(400, "No final video found for this film.")
    cover = wd / "cover.png"
    thumb = str(cover) if cover.exists() and cover.stat().st_size > 1000 else None

    try:
        result = _call_matching(
            yt.upload_video,
            client_secrets_path=_client_secrets_path(), client_secrets=_client_secrets_path(),
            video_path=str(final), path=str(final), video=str(final), file=str(final),
            filename=str(final), video_file=str(final),
            title=body.title, description=body.description,
            category=body.category, category_id=body.category, categoryId=body.category,
            privacy=body.privacy, privacy_status=body.privacy, privacyStatus=body.privacy,
            thumbnail=thumb, thumbnail_path=thumb, thumb=thumb,
        )
    except Exception as e:
        raise HTTPException(502, f"Upload failed: {str(e).splitlines()[0][:240]}")

    # Normalise the return into {video_id, url}.
    video_id, url = "", ""
    if isinstance(result, dict):
        video_id = result.get("video_id") or result.get("id") or result.get("videoId") or ""
        url = result.get("url") or result.get("video_url") or ""
    elif isinstance(result, str):
        video_id = result
    if video_id and not url:
        url = f"https://youtu.be/{video_id}"

    # Side effects: stamp the job and mark its queue item posted.
    try:
        gapp._write_job_meta(wd, youtube_video_id=video_id, youtube_url=url, status="done")
    except Exception:
        pass
    try:
        cfg_path = wd / "job_config.json"
        queue_item_id = ""
        if cfg_path.exists():
            queue_item_id = json.loads(cfg_path.read_text()).get("queue_item_id", "")
        if queue_item_id and hasattr(yt, "update_queue_item"):
            yt.update_queue_item(queue_item_id, status="posted", youtube_video_id=video_id, youtube_url=url)
    except Exception:
        pass

    return {"ok": True, "video_id": video_id, "url": url,
            "message": f"Uploaded — {url}" if url else "Uploaded to YouTube."}


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


class QueueAddBody(BaseModel):
    title: str
    n_scenes: int = 0
    prompt: str = ""


@api.post("/api/queue/add")
def queue_add(body: QueueAddBody) -> dict:
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "Title is required.")
    n = max(6, min(50, body.n_scenes or gapp.load_config().get("default_n_scenes", 6)))
    comment = {"comment_id": "", "text": body.prompt, "commenter": "you",
               "suggested_scene_count": n}
    entry = yt.add_to_queue(comment, title, source="manual")
    if entry and body.prompt.strip():
        yt.update_queue_item(entry["id"], video_prompt=body.prompt.strip())
    return {"ok": bool(entry), "queue": yt.load_queue()}


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
    title = item.get("final_title", "")
    n = max(6, int(item.get("suggested_scene_count") or cfg.get("default_n_scenes", 6)))

    if item.get("script_ready") and item.get("work_dir") and item.get("video_job_id"):
        job_id = item["video_job_id"]
        wd = item["work_dir"]
        start_generation(GenerateBody(
            job_id=job_id, work_dir=wd, video_title=title, title=title, n_scenes=n,
            voice=item.get("gen_voice") or cfg.get("default_voice", ""),
            resolution=item.get("gen_resolution") or cfg.get("resolution", gapp._DEFAULT_RESOLUTION),
            music_desc=item.get("gen_music") or _job_meta_field(job_id, "music_desc"),
            style=item.get("gen_style") or _job_meta_field(job_id, "style")))
        yt.update_queue_item(item["id"], status="creating")
        return {"job_id": job_id, "work_dir": wd, "title": title}

    topic = item.get("video_prompt") or title
    gen = script_generate(GenerateScriptBody(
        video_title=title, topic=topic, n_scenes=n,
        visual_style=cfg.get("default_visual_style") or None))
    start_generation(GenerateBody(
        job_id=gen["job_id"], work_dir=gen["work_dir"], video_title=title, title=title,
        n_scenes=n, voice=cfg.get("default_voice", ""),
        resolution=cfg.get("resolution", gapp._DEFAULT_RESOLUTION),
        music_desc=gen.get("music_desc", ""), style=gen.get("style", "")))
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
    music_desc: str = ""


@api.post("/api/queue/from-job")
def queue_from_job(body: FromJobBody) -> dict:
    """Add an approved (already-generated) script to the queue. Does NOT render
    unless 'auto-start next' (youtube_auto_start_job) is on and nothing is
    currently rendering."""
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

    entry = yt.add_to_queue({"comment_id": "", "text": "", "commenter": "you",
                             "suggested_scene_count": n}, title, source="script")
    if not entry:
        raise HTTPException(500, "Could not enqueue the script.")
    yt.update_queue_item(entry["id"], video_job_id=body.job_id, work_dir=body.work_dir,
                         script_ready=True, gen_style=body.style, gen_resolution=body.resolution,
                         gen_voice=body.voice, gen_music=body.music_desc)

    started = None
    if cfg.get("youtube_auto_start_job") and not gapp._is_job_running():
        item = next((q for q in yt.load_queue() if q.get("id") == entry["id"]), None)
        if item:
            try:
                started = _start_queue_item(item)
            except Exception:
                started = None
    return {"ok": True, "queue_item_id": entry["id"], "started": started}


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
        return None
    try:
        return _start_queue_item(item)
    except Exception:
        return None


def _auto_post_done() -> list[str]:
    """Auto-post finished, queue-driven jobs that haven't been posted yet."""
    cfg = gapp.load_config()
    posted: list[str] = []
    for _label, wd in gapp._list_recent_jobs(max_results=50):
        p = Path(wd)
        try:
            meta = json.loads((p / "job.json").read_text())
        except Exception:
            continue
        if meta.get("status") != "done" or meta.get("youtube_video_id"):
            continue
        try:
            jc = json.loads((p / "job_config.json").read_text())
        except Exception:
            jc = {}
        if not jc.get("queue_item_id"):
            continue  # only auto-post videos that came from the queue
        try:
            res = yt_post(PostBody(
                work_dir=str(p), title=jc.get("video_title") or _video_title_for(p),
                description="", category=cfg.get("youtube_post_category", "22"),
                privacy=cfg.get("youtube_post_privacy", "private")))
            if res.get("video_id"):
                posted.append(res["video_id"])
        except Exception:
            continue
    return posted


def _automation_tick() -> dict:
    cfg = gapp.load_config()
    out: dict = {}
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
        try:
            cfg = gapp.load_config()
            if cfg.get("youtube_fully_automated") or any(cfg.get(k) for k in (
                    "youtube_auto_fetch_evaluate", "youtube_auto_start_job", "youtube_auto_post")):
                _automation_tick()
        except Exception:
            pass


@api.on_event("startup")
def _start_automation_loop():
    global _automation_started
    if not _automation_started:
        _automation_started = True
        threading.Thread(target=_automation_loop, daemon=True).start()


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
