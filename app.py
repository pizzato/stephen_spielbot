#!/usr/bin/env python3
"""Core helpers for the AI video generator.

Formerly the Gradio app; the Gradio UI has been removed and the React +
FastAPI web app (``webapp/``) is the only interface. This module is now a
helper library that ``webapp/backend/main.py`` imports for config I/O,
work-dir bookkeeping, job launching, progress polling, and automation.
"""

import concurrent.futures
import threading
import html
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import yaml


LOG_DIR = Path.home() / ".local" / "share" / "video-generator" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_fmt)

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_log_fmt)

# Keep third-party libraries quiet; only our own logger runs at DEBUG.
logging.basicConfig(level=logging.WARNING, handlers=[_file_handler, _stream_handler], force=True)
logger = logging.getLogger("video_gen")
logger.setLevel(logging.DEBUG)
logger.info("Logging to %s", LOG_FILE)

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.llm import generate_script, generate_youtube_description, generate_video_prompt, generate_video_suggestions, Scene, NEGATIVE_PROMPT
import pipeline.youtube as yt
from pipeline.comfyui import (
    generate_video_clip, generate_video_continuation, generate_music,
    generate_scene_image, StuckJobError,
)
from pipeline.assembler import (
    _get_duration, concat_clips, mux_video_audio, extract_first_frame,
    extract_last_frame, extract_audio, concat_audio, concatenate_scenes,
    ensure_video_resolution, mix_background_music,
)
from pipeline.orchestrator import DurableStore, job_id_from_work_dir, task_id
from pipeline.scene_video import generate_scene_video as _generate_scene_video
from pipeline.worker_pool import WorkerPool, alive_workers, idle_workers
from pipeline.cover import (
    overlay_title_on_image as _overlay_title_on_image,
    build_cover_prompt as _cover_prompt,
    shorten_title_for_cover as _shorten_title,
    COVER_WIDTH as _COVER_W,
    COVER_HEIGHT as _COVER_H,
)

MAX_SCENES    = 100
MAX_CLIP_SECS = 0.0  # 0 means request one clip for the full scene duration.
OUTPUT_DIR   = Path.home() / "videos"
OUTPUT_DIR.mkdir(exist_ok=True)
CONFIG_FILE  = Path.home() / ".config" / "video-generator" / "config.yaml"
VOICES_DIR   = CONFIG_FILE.parent / "voices"
SESSION_FILE = CONFIG_FILE.parent / "last_session.json"
REPO_ROOT    = Path(__file__).parent
RESUME_SCRIPT = REPO_ROOT / "resume_generation.py"

# Resolution is chosen as (orientation × pixel tier).  Each tier defines a
# "long" and "short" edge; orientation decides which axis each maps to (square
# uses the long edge for both).  The composed name strings below are the
# canonical keys stored in config.yaml / job_config.json, so they must stay
# byte-for-byte stable — old saved configs resolve through _RESOLUTIONS.get(name).
_ORIENTATIONS = ["Landscape", "Portrait", "Square"]

# Ordered low→high.  ``label`` is the in-name tag ("" = the base tier, which has
# no tag).  (long_edge, short_edge) are the 16:9 dimensions; square uses
# (long_edge, long_edge).
_PIXEL_TIERS = [
    {"key": "fast", "label": "Fast", "long": 512,  "short": 288},
    {"key": "base", "label": "",     "long": 832,  "short": 480},
    {"key": "hd",   "label": "HD",   "long": 1024, "short": 576},
    {"key": "720p", "label": "720p", "long": 1280, "short": 720},
    {"key": "fhd",  "label": "FHD",  "long": 1920, "short": 1080},
]


def _resolution_dims(orientation: str, tier: dict) -> tuple[int, int]:
    """(width, height) for an orientation × pixel tier."""
    long_e, short_e = tier["long"], tier["short"]
    if orientation == "Portrait":
        return (short_e, long_e)
    if orientation == "Square":
        # Square uses a single edge per tier (the historical 1:1 sizes).
        side = {"fast": 288, "base": 512, "hd": 576, "720p": 720, "fhd": 1080}[tier["key"]]
        return (side, side)
    return (long_e, short_e)  # Landscape


def compose_resolution(orientation: str, tier_key: str) -> str:
    """Compose the canonical resolution name string from orientation + tier key.

    Returns "" if either selector is unknown.
    """
    tier = next((t for t in _PIXEL_TIERS if t["key"] == tier_key), None)
    if orientation not in _ORIENTATIONS or tier is None:
        return ""
    w, h = _resolution_dims(orientation, tier)
    tag = f" {tier['label']}" if tier["label"] else ""
    return f"{orientation}{tag} ({w}×{h})"


def _build_resolutions() -> dict:
    """Build the canonical {name: (w, h)} map from orientations × pixel tiers."""
    out = {}
    for orientation in _ORIENTATIONS:
        for tier in _PIXEL_TIERS:
            out[compose_resolution(orientation, tier["key"])] = _resolution_dims(orientation, tier)
    return out


_RESOLUTIONS = _build_resolutions()
_DEFAULT_ORIENTATION = "Landscape"
_DEFAULT_PIXELS = "fhd"
_DEFAULT_RESOLUTION = compose_resolution(_DEFAULT_ORIENTATION, _DEFAULT_PIXELS)


DEFAULT_CFG = {
    "music_vol": 18,
    "voice_vol": 100,
    "ambient_vol": 0,
    "resolution": _DEFAULT_RESOLUTION,
    "max_clip_secs": 0,
    "lora_strength": 0.5,
    # First-pass (distilled LoRA) settings — set steps=8 + cfg=1.0 for distilled mode;
    # set lora_strength=0 + steps=20-30 + cfg=3-5 for pure dev model mode.
    "first_pass_cfg": 1.0,
    "first_pass_steps": 8,
    # Second-pass (refinement) quality: higher CFG and more steps = better detail
    "second_pass_cfg": 3.0,
    "second_pass_steps": 6,
    "llm_backend": "local",
    "local_llm_url":   "http://localhost:8000/v1/chat/completions",
    "local_llm_model": "openai/gpt-oss-120b",
    "claude_api_key": "",
    "claude_model": "claude-sonnet-4-6",
    # FLUX image generation for scene previews
    "flux_model":    "flux1-schnell-fp8.safetensors",
    "flux_clip_t5":  "t5xxl_fp8_e4m3fn.safetensors",
    "flux_clip_l":   "clip_l.safetensors",
    "flux_vae":      "ae.safetensors",
    "flux_steps":    4,
    "voices": [],
    # Worker lists — edited from the Settings screen, stored in config.yaml.
    # comfy_workers: ComfyUI URLs (image/video/music). One job at a time each.
    # tts_workers:   hostnames for F5-TTS narration.
    # ui_workers:    ComfyUI URLs the lightweight "ui" worker renders covers on.
    "comfy_workers": [],
    "tts_workers":   [],
    "ui_workers":    [],
    # Generation defaults
    "default_voice": "",
    "default_n_scenes": 20,
    "default_visual_style": "",
    "script_extra_instructions": "",
    # YouTube integration
    "youtube_client_secrets": "~/.config/video-generator/client_secrets.json",
    "youtube_auto_fetch_evaluate": False,      # fetch+evaluate on startup and after each post
    "youtube_auto_approve_comments": False,    # auto-approve requests with confidence ≥ threshold
    "youtube_auto_start_job": False,           # auto-launch best pending job after approval
    "youtube_auto_approve_script": False,      # skip script review, go straight to video gen
    "youtube_auto_post": False,               # auto-publish when video generation completes
    "youtube_fully_automated": False,          # master toggle — implies the auto_* steps above (honored in _automation_tick)
    "youtube_post_privacy": "private",
    "youtube_post_category": "22",
    "description_suffix": "",
}

F5TTS_DEFAULT_OPTION = "Default (F5-TTS)"

# Thread pool for long blocking operations — keeps SSE alive via heartbeat yields
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# Set of work-dir paths that have already fired the auto-post trigger this
# process lifetime.  Guards against two rapid timer ticks both claiming the
# trigger before the first one's _write_job_meta() flush reaches disk.
_auto_post_triggered: set[str] = set()
_auto_post_lock = threading.Lock()

# Flag that prevents two simultaneous yt_auto_start_trigger.change chains
# (e.g. one from demo.load startup fetch, one from the user pressing the
# button at the same time) from both launching full pipelines concurrently.
_auto_start_in_progress = threading.Event()


def _is_job_running() -> bool:
    """Return True if any video generation job is currently in progress."""
    # Check the queue first: a "creating" item means script_generate() is running
    # but job_config.json may not exist yet — the filesystem check below would miss it.
    try:
        cutoff = time.time() - 86400
        for q in yt.load_queue():
            if q.get("status") == "creating":
                ts = q.get("updated_at") or q.get("created_at") or time.time()
                if ts > cutoff:
                    return True
    except Exception:
        pass
    try:
        cutoff = time.time() - 86400  # ignore stale jobs older than 24 h
        for d in OUTPUT_DIR.iterdir():
            if not d.is_dir():
                continue
            cfg_file = d / "job_config.json"
            combined = d / "combined.mp4"
            if cfg_file.exists() and not combined.exists():
                if cfg_file.stat().st_mtime > cutoff:
                    # Skip jobs that have already errored or been cancelled —
                    # they have no combined.mp4 but are not "running".
                    job_file = d / "job.json"
                    if job_file.exists():
                        try:
                            job_data = json.loads(job_file.read_text())
                            if job_data.get("status") in ("error", "cancelled", "paused"):
                                continue
                        except Exception:
                            pass
                    return True
    except Exception:
        pass
    return False


def _best_pending_queue_item() -> dict | None:
    """Return the first pending queue item (respecting queue order)."""
    try:
        queue = yt.load_queue()
        first = next((q for q in queue if q.get("status") == "pending"), None)
        return {**first} if first else None  # fresh copy
    except Exception:
        return None


# ── Config helpers ───────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load the single YAML config. Saved values are authoritative — worker
    lists (comfy_workers/tts_workers/ui_workers) live here and are edited from
    the Settings screen."""
    cfg = DEFAULT_CFG.copy()
    if CONFIG_FILE.exists():
        data = yaml.safe_load(CONFIG_FILE.read_text()) or {}
        if isinstance(data, dict):
            cfg.update(data)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))






def _job_meta_path(work_dir: Path) -> Path:
    return work_dir / "job.json"


def _final_path_for_work_dir(work_dir: Path) -> Path:
    meta_path = _job_meta_path(work_dir)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            final_path = Path(meta.get("final_path", ""))
            if final_path.exists() and final_path.stat().st_size > 10_000:
                return final_path
        except Exception:
            pass

    canonical = OUTPUT_DIR / f"{work_dir.name}.mp4"
    if canonical.exists() and canonical.stat().st_size > 10_000:
        return canonical

    status_path = work_dir / "progress.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text())
            msg = str(status.get("msg", ""))
            match = re.search(r"Done\s+[—-]\s+(.+?\.mp4)\s+\(([\d.]+)\s+MB\)", msg)
            if match:
                size_mb = float(match.group(2))
                for candidate in OUTPUT_DIR.glob("*.mp4"):
                    try:
                        actual_mb = candidate.stat().st_size / 1024 / 1024
                        if abs(actual_mb - size_mb) < max(2.0, size_mb * 0.02):
                            return candidate
                    except OSError:
                        continue
        except Exception:
            pass

    try:
        marker_mtime = max(
            (p.stat().st_mtime for p in (meta_path, status_path) if p.exists()),
            default=0.0,
        )
        # Only use the recency heuristic when we have a real anchor time.
        # If marker_mtime == 0 it means neither job.json nor progress.json
        # exists yet (job just started), so marker_mtime - 600 == -600 and
        # every .mp4 in OUTPUT_DIR would match — returning the wrong video.
        if marker_mtime > 0:
            recent = [
                p for p in OUTPUT_DIR.glob("*.mp4")
                if p.stat().st_size > 10_000 and p.stat().st_mtime >= marker_mtime - 600
            ]
            if recent:
                return max(recent, key=lambda p: p.stat().st_mtime)
    except Exception:
        pass

    return canonical


def _latest_work_dir() -> Path | None:
    candidates: dict[Path, float] = {}
    for pattern in ("*/progress.json", "*/script.json", "*/job.json"):
        for path in OUTPUT_DIR.glob(pattern):
            if path.parent.exists():
                candidates[path.parent] = max(candidates.get(path.parent, 0.0), path.stat().st_mtime)
    if not candidates:
        return None
    return max(candidates, key=lambda p: candidates[p])




def _preferred_work_dir(active_job_dir: str = "") -> Path | None:
    if active_job_dir:
        active = Path(active_job_dir)
        if active.exists():
            return active
    return _latest_work_dir()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _process_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_job_meta(work_dir: Path, **updates) -> dict:
    meta_path = _job_meta_path(work_dir)
    try:
        meta = _read_json(meta_path)
    except Exception:
        meta = {"work_dir": str(work_dir), "created_at": time.time()}
    meta.update(updates)
    if "error" in updates and updates["error"] is None:
        meta.pop("error", None)
    meta["updated_at"] = time.time()
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta


def _launch_generation_job(work_dir: Path) -> dict:
    """Start the resumable generator in its own process."""
    meta_path = _job_meta_path(work_dir)
    if meta_path.exists():
        try:
            meta = _read_json(meta_path)
            if _process_running(meta.get("pid")):
                return meta
        except Exception:
            pass

    log_path = work_dir / "job.log"
    log_f = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(RESUME_SCRIPT), str(work_dir)],
        cwd=str(REPO_ROOT),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    logger.info("Launched generation job pid=%s work_dir=%s", proc.pid, work_dir)
    return _write_job_meta(
        work_dir,
        pid=proc.pid,
        status="running",
        error=None,
        log_path=str(log_path),
        command=[sys.executable, str(RESUME_SCRIPT), str(work_dir)],
    )


def _job_config_snapshot(cfg: dict) -> dict:
    """Persist only non-secret runtime settings needed by the background worker."""
    job_cfg = cfg.copy()
    for key in list(job_cfg):
        lowered = key.lower()
        if "api_key" in lowered or "token" in lowered or "secret" in lowered:
            job_cfg.pop(key, None)
    return job_cfg


def _status_for_work_dir(work_dir: Path) -> tuple[float, str]:
    status_file = work_dir / "progress.json"
    pct = 0.0
    msg = "Waiting to start..."
    if status_file.exists():
        try:
            data = _read_json(status_file)
            pct = float(data.get("pct", 0))
            msg = str(data.get("msg", "..."))
        except Exception:
            pass

    final_path = _final_path_for_work_dir(work_dir)
    combined_check = work_dir / "combined.mp4"
    if final_path.exists() and final_path.stat().st_size > 10_000 and combined_check.exists():
        return 100.0, f"Done - {final_path.name} ({final_path.stat().st_size / 1024 / 1024:.1f} MB)"

    try:
        meta = _read_json(_job_meta_path(work_dir))
        if meta.get("status") == "error":
            return pct, f"Generation failed: {str(meta.get('error', 'unknown error')).splitlines()[0][:300]}"
        pid = meta.get("pid")
        if pid and not _process_running(pid) and pct < 100:
            return pct, f"Generation process exited before completion. Check {work_dir / 'job.log'}"
    except Exception:
        pass

    return pct, msg




def get_voice_choices() -> list[str]:
    return [F5TTS_DEFAULT_OPTION] + [v["name"] for v in load_config().get("voices", [])]


def voice_path_for(name: str) -> str | None:
    if not name or name == F5TTS_DEFAULT_OPTION:
        return None
    for v in load_config().get("voices", []):
        if v["name"] == name:
            return v["path"]
    return None




# ── UI helpers ───────────────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-")
















def _active_job_row(active_job_dir: str = ""):
    store = DurableStore.default()
    try:
        if active_job_dir:
            # A specific job was named: act on exactly that one, never a fallback.
            # (A script/partial with no durable row must NOT resolve to the
            # currently-running render — deleting it would cancel that render.)
            job = store.get_job_by_work_dir(active_job_dir)
        else:
            recent = store.recent_jobs(limit=1)
            job = recent[0] if recent else None
        return dict(job) if job else None
    finally:
        store.close()


def on_resume_active_job(active_job_dir: str):
    job = _active_job_row(active_job_dir)
    if not job:
        return "No durable job available to resume.", None, active_job_dir
    work_dir = Path(job["work_dir"])
    if not work_dir.exists():
        return f"Work directory missing: {work_dir}", None, active_job_dir
    store = DurableStore.default()
    try:
        store.recover_incomplete_tasks(job["id"])
        store.update_job(job["id"], status="running", progress_message="resume requested")
    finally:
        store.close()
    _launch_generation_job(work_dir)
    return f"Resume launched for {work_dir.name}", None, str(work_dir)


def on_retry_failed_tasks(active_job_dir: str):
    job = _active_job_row(active_job_dir)
    if not job:
        return "No durable job available."
    store = DurableStore.default()
    try:
        count = store.retry_failed_tasks(job["id"])
    finally:
        store.close()
    return f"Requeued {count} failed/lost task(s)."


def on_cancel_active_job(active_job_dir: str):
    job = _active_job_row(active_job_dir)
    if not job:
        return "No durable job available."
    store = DurableStore.default()
    try:
        count = store.cancel_job(job["id"])
    finally:
        store.close()
    return f"Cancelled {count} pending/running task(s)."


def on_pause_active_job(active_job_dir: str) -> str:
    """Kill the generation subprocess and re-queue in-progress tasks so the job can be resumed later."""
    work_dir = _preferred_work_dir(active_job_dir)
    if not work_dir:
        return "No active job found."
    try:
        meta = _read_json(_job_meta_path(work_dir))
    except Exception:
        meta = {}
    pid = meta.get("pid")
    killed = False
    if pid and _process_running(pid):
        try:
            os.kill(int(pid), 15)  # SIGTERM
            killed = True
        except OSError as exc:
            logger.warning("Could not signal pid %d: %s", pid, exc)
    job = _active_job_row(active_job_dir)
    if job:
        store = DurableStore.default()
        try:
            store.recover_incomplete_tasks(job["id"])
        finally:
            store.close()
    _write_job_meta(work_dir, status="paused", pid=None)
    return f"Paused {work_dir.name}" + (" — process terminated" if killed else " — process was not running")


def _list_resumable_jobs() -> list[tuple[str, str]]:
    """Jobs that were started (have job.json) but not yet completed (no combined.mp4)."""
    results = []
    try:
        dirs = sorted(
            (d for d in OUTPUT_DIR.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for d in dirs:
            if (d / "job.json").exists() and not (d / "combined.mp4").exists():
                try:
                    meta = _read_json(d / "job.json")
                    if meta.get("status") == "cancelled":
                        continue
                except Exception:
                    pass
                results.append((_job_folder_label(d), str(d)))
    except Exception:
        pass
    return results






def _script_work_dir(title: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work_dir = OUTPUT_DIR / f"{slugify(title)}-{stamp}"
    suffix = 1
    while work_dir.exists():
        suffix += 1
        work_dir = OUTPUT_DIR / f"{slugify(title)}-{stamp}-{suffix}"
    work_dir.mkdir(parents=True, exist_ok=False)
    return work_dir


def _persist_script_snapshot(work_dir: Path, scenes: list[dict[str, str]]) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "script.json").write_text(json.dumps(scenes, indent=2))


def _job_work_dir(job_id: str) -> Path | None:
    if not job_id:
        return None
    store = DurableStore.default()
    try:
        row = store.get_job(job_id)
        return Path(row["work_dir"]) if row else None
    finally:
        store.close()






def _save_active_scene(
    job_id: str,
    scene_id: int,
    title: str,
    image_prompt: str,
    video_prompt: str,
    narration: str,
) -> None:
    if not job_id:
        return
    store = DurableStore.default()
    try:
        sid = int(scene_id or 1)
        current = store.get_scene(job_id, sid) or {}
        store.upsert_scene(
            job_id,
            sid,
            title=title,
            image_prompt=image_prompt,
            video_prompt=video_prompt,
            narration=narration,
            preview_path=current.get("preview_path", ""),
            metadata=current.get("metadata", {}),
        )
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    work_dir = _job_work_dir(job_id)
    if work_dir:
        _persist_script_snapshot(work_dir, rows)






# ── YouTube cover image ──────────────────────────────────────────────────────
# _overlay_title_on_image and _cover_prompt are imported from pipeline.cover




# ── TTS wrapper ──────────────────────────────────────────────────────────────



# ── Script generation ────────────────────────────────────────────────────────



# ── Scene image generation — one active scene only ───────────────────────────

_IMG_GEN_OUT_COUNT = 3


def _preview_worker_urls() -> list[str]:
    cfg = load_config()
    all_workers = cfg.get("comfy_workers", [])
    try:
        # Prefer idle workers so previews don't queue behind a video render on a
        # busy worker; falls back to all reachable workers if every one is busy.
        return idle_workers(all_workers)
    except Exception as exc:
        logger.warning("Scene image generation worker probe failed: %s", exc)
        return []


def _generate_active_scene_preview(
    job_id: str,
    scene_id: int,
    resolution: str,
    style: str,
    title: str,
    image_prompt: str,
    *,
    force: bool = False,
    worker_pool: WorkerPool | None = None,
) -> Path:
    work_dir = _job_work_dir(job_id)
    if work_dir is None:
        raise RuntimeError("No script work directory is available.")
    sid = int(scene_id)
    store = DurableStore.default()
    try:
        scene = store.get_scene(job_id, sid) or {}
    finally:
        store.close()
    existing = scene.get("preview_path") or ""
    if existing and Path(existing).exists() and not force:
        return Path(existing)

    cfg = load_config()
    if worker_pool is None:
        worker_urls = _preview_worker_urls()
        if worker_urls:
            worker_pool = WorkerPool(worker_urls)
    if worker_pool is None:
        raise RuntimeError("No cluster workers reachable for scene preview generation.")

    out = work_dir / f"scene_{sid:02d}_preview.png"
    flux_model = cfg.get("flux_model", "flux1-schnell-fp8.safetensors")
    flux_clip_t5 = cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors")
    flux_clip_l = cfg.get("flux_clip_l", "clip_l.safetensors")
    flux_vae = cfg.get("flux_vae", "ae.safetensors")
    flux_steps = int(cfg.get("flux_steps", 4))
    img_width, img_height = _RESOLUTIONS.get(
        resolution or cfg.get("resolution", _DEFAULT_RESOLUTION), (1024, 576)
    )
    style_clean = style.strip().rstrip(".") if style and style.strip() else ""
    default_style = cfg.get("default_visual_style", "").strip().rstrip(".")
    combined_parts = [p for p in [style_clean, default_style] if p]
    combined_style = ". ".join(combined_parts)
    base_prompt = image_prompt or scene.get("image_prompt") or title
    prompt = f"{combined_style}. {base_prompt}" if combined_style else base_prompt

    url = worker_pool.acquire()
    try:
        generate_scene_image(
            prompt,
            out,
            width=img_width,
            height=img_height,
            steps=flux_steps,
            flux_model=flux_model,
            clip_t5=flux_clip_t5,
            clip_l=flux_clip_l,
            flux_vae=flux_vae,
            comfy_url=url,
        )
        store = DurableStore.default()
        try:
            store.update_scene_preview(job_id, sid, out)
        finally:
            store.close()
        return out
    finally:
        worker_pool.release(url)






# ── Video generation — generator, yields progressive UI updates ──────────────

# gen_outputs count: progress, music, final, combined_state, music_state,
# ambient_state, tabs, active_job
_GEN_OUT_COUNT = 8






# ── Session restore ──────────────────────────────────────────────────────────

def _job_folder_label(d: Path) -> str:
    """Human-readable label for a job folder: title without the trailing timestamp."""
    # Folder names look like: my-great-video-20260528-082307
    # Strip the trailing -YYYYMMDD-HHMMSS suffix so only the title part remains.
    name = re.sub(r"-\d{8}-\d{6}(-\d+)?$", "", d.name)
    return name.replace("-", " ").title()


def _list_recent_jobs(max_results: int = 10) -> list[tuple[str, str]]:
    """Return list of (label, work_dir_str) for completed jobs, newest first."""
    results = []
    try:
        dirs = sorted(
            (d for d in OUTPUT_DIR.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for d in dirs:
            combined = d / "combined.mp4"
            if combined.exists():
                results.append((_job_folder_label(d), str(d)))
            if len(results) >= max_results:
                break
    except Exception:
        pass
    return results


def _list_script_jobs() -> list[tuple[str, str]]:
    """Return list of (label, work_dir_str) for all jobs with a saved script, newest first."""
    results = []
    try:
        dirs = sorted(
            (d for d in OUTPUT_DIR.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for d in dirs:
            if (d / "script.json").exists():
                results.append((_job_folder_label(d), str(d)))
    except Exception:
        pass
    return results






# Keep for backward compat with _remix_select_all






# ── Remix ────────────────────────────────────────────────────────────────────

def on_remix(
    combined_path_str: str,
    music_path_str: str,
    ambient_path_str: str,
    voice_vol: float,
    music_vol: float,
    ambient_vol: float,
):
    """Re-mux the final video with new voice/music/ambient volumes.

    Volumes are percentages (100 = unchanged), matching the UI sliders and
    job_config; they're divided by 100 into the fractional gains that
    mix_background_music expects. Returns ``(final_video_path, message)`` — the
    path is ``""`` on failure and ``message`` explains the outcome.
    """
    combined_path = Path(combined_path_str)
    if not combined_path.exists():
        return "", "combined.mp4 not found — nothing to remix."
    work_dir = combined_path.parent
    final_video = work_dir / f"{work_dir.name}.mp4"
    music_path = Path(music_path_str) if music_path_str else None
    ambient_path = Path(ambient_path_str) if ambient_path_str else None
    try:
        from pipeline.assembler import mix_background_music
        mix_background_music(
            video_path=combined_path,
            music_path=music_path,
            output_path=final_video,
            volume=music_vol / 100.0,
            voice_volume=voice_vol / 100.0,
            ambient_path=ambient_path,
            ambient_volume=ambient_vol / 100.0,
        )
        # Persist the new volumes to job_config so a later re-render keeps them.
        try:
            cfg_path = work_dir / "job_config.json"
            jc = json.loads(cfg_path.read_text())
            jc.update(voice_vol=voice_vol, music_vol=music_vol, ambient_vol=ambient_vol)
            cfg_path.write_text(json.dumps(jc, indent=2))
        except Exception:
            logger.warning("Could not persist remix volumes to job_config", exc_info=True)
        return str(final_video), "Re-mixed the audio and re-muxed the film."
    except Exception as e:
        logger.exception("Remix failed")
        return "", f"Remix failed: {str(e).splitlines()[0][:200]}"


# ── LTX Upscale ──────────────────────────────────────────────────────────────





# ── Config management ────────────────────────────────────────────────────────











# ── YouTube tab handlers ─────────────────────────────────────────────────────















































# ── Post tab handlers ─────────────────────────────────────────────────────────













def _load_scenes_for_work_dir(work_dir: Path) -> list[dict]:
    """Return scene dicts for a work_dir.

    Tries DurableStore first; falls back to reading script.json from disk so
    older jobs (predating the DB) or jobs after a DB wipe still get usable
    image_prompts for cover regeneration.
    """
    # DurableStore path
    try:
        store = DurableStore.default()
        try:
            job = store.get_job_by_work_dir(str(work_dir))
            if job:
                rows = store.scene_rows(job["id"]) or []
                if rows:
                    return rows
        finally:
            store.close()
    except Exception as exc:
        logger.debug("DurableStore scene lookup failed for %s: %s", work_dir, exc)

    # script.json fallback
    script_path = work_dir / "script.json"
    if script_path.exists():
        try:
            data = json.loads(script_path.read_text())
            if isinstance(data, list):
                return data
        except Exception as exc:
            logger.debug("script.json read failed for %s: %s", work_dir, exc)
    return []




















# ── UI ────────────────────────────────────────────────────────────────────────



# ── Video Suggestions ─────────────────────────────────────────────────────────



def _channel_video_titles(cfg: dict) -> list[str]:
    """Collect known video titles: YouTube API first, posted queue items as fallback."""
    secrets = cfg.get("youtube_client_secrets", "")
    titles: list[str] = []
    try:
        titles = yt.fetch_channel_video_titles(secrets, max_results=500)
    except Exception as exc:
        logger.warning("fetch_channel_video_titles error: %s", exc)
    # Supplement with posted queue items in case the API call failed or is partial
    try:
        queue = yt.load_queue()
        for q in queue:
            t = q.get("final_title", "")
            if q.get("status") == "posted" and t and t not in titles:
                titles.append(t)
    except Exception:
        pass
    return titles






def _auto_pick_suggestion(cfg: dict) -> dict | None:
    """Pick the first unused suggestion (generating new ones if needed) and add it to the queue.

    Returns the new pending queue item dict, or None on failure.
    Called only when there are no pending user requests and auto-start is enabled.
    """
    suggestions = yt.load_suggestions()
    unused = [s for s in suggestions if not s.get("used")]

    if not unused:
        # Ask the LLM for a fresh batch of 5
        logger.info("No unused suggestions — generating a new batch")
        existing_titles = _channel_video_titles(cfg)
        new_data = generate_video_suggestions(existing_titles, cfg)
        if not new_data:
            logger.warning("_auto_pick_suggestion: LLM suggestion generation failed")
            return None
        suggestions = [
            {
                "id": str(uuid.uuid4())[:8],
                "title": s["title"],
                "reason": s["reason"],
                "interestingness": s["interestingness"],
                "created_at": time.time(),
                "used": False,
            }
            for s in new_data
        ]
        yt.save_suggestions(suggestions)
        unused = suggestions

    suggestion = unused[0]
    suggestion["used"] = True
    # Persist the 'used' flag so this idea is closed and never re-picked. Match
    # by id, fall back to title, and never re-mark an already-used row — a
    # missing/duplicate id must not close the wrong idea and leave this one open
    # (which made automation pick the same idea over and over).
    sid = str(suggestion.get("id") or "")
    stitle = (suggestion.get("title") or "").strip().lower()
    all_suggestions = yt.load_suggestions()
    for s in all_suggestions:
        if s.get("used"):
            continue
        if (sid and str(s.get("id") or "") == sid) or ((s.get("title") or "").strip().lower() == stitle):
            s["used"] = True
            break
    yt.save_suggestions(all_suggestions)

    # Add to queue
    fake_comment = {
        "comment_id": "",
        "video_id": "",
        "commenter": "AI Suggestion",
        "text": suggestion.get("reason", ""),
        "suggested_scene_count": 20,
        "interestingness": suggestion.get("interestingness", 0.7),
    }
    queue_item = yt.add_to_queue(fake_comment, suggestion["title"], source="suggestion")
    if not queue_item:
        logger.warning("_auto_pick_suggestion: add_to_queue returned empty for %r", suggestion["title"])
        return None

    # Generate directorial brief synchronously (we're already in a background context)
    try:
        prompt = generate_video_prompt(suggestion["title"], suggestion.get("reason", ""))
        if prompt:
            yt.update_queue_item(queue_item["id"], video_prompt=prompt)
            queue_item["video_prompt"] = prompt
    except Exception as exc:
        logger.warning("_auto_pick_suggestion: prompt generation failed: %s", exc)

    logger.info("Auto-picked suggestion: %r (id=%s)", suggestion["title"], queue_item.get("id"))
    return queue_item








