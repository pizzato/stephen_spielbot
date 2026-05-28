#!/usr/bin/env python3
"""Gradio web interface for the AI video generator."""

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

import gradio as gr

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
from pipeline.worker_pool import WorkerPool, alive_workers
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
CONFIG_FILE  = Path.home() / ".config" / "video-generator" / "config.json"
VOICES_DIR   = CONFIG_FILE.parent / "voices"
SESSION_FILE = CONFIG_FILE.parent / "last_session.json"
REPO_ROOT    = Path(__file__).parent
RESUME_SCRIPT = REPO_ROOT / "resume_generation.py"

_RESOLUTIONS = {
    # Landscape 16:9
    "Landscape Fast (512×288)":    (512, 288),
    "Landscape (832×480)":         (832, 480),
    "Landscape HD (1024×576)":     (1024, 576),
    "Landscape FHD (1920×1080)":   (1920, 1080),
    # Portrait 9:16
    "Portrait Fast (288×512)":     (288, 512),
    "Portrait (480×832)":          (480, 832),
    "Portrait HD (576×1024)":      (576, 1024),
    "Portrait FHD (1080×1920)":    (1080, 1920),
    # Square 1:1
    "Square (512×512)":            (512, 512),
    "Square HD (576×576)":         (576, 576),
    "Square FHD (1080×1080)":      (1080, 1080),
}
_DEFAULT_RESOLUTION = "Landscape FHD (1920×1080)"


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
    # ComfyUI worker URLs — one per line in the config UI.
    # Each worker handles one video generation job at a time.
    "comfy_workers": [],
    "tts_workers":   [],
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
    "youtube_fully_automated": False,          # master toggle — sets all five flags above
    "youtube_post_privacy": "private",
    "youtube_post_category": "22",
    "description_suffix": "",
}

F5TTS_DEFAULT_OPTION = "Default (F5-TTS)"

CLUSTER_CONF = Path(__file__).parent / "cluster.conf"
COMFYUI_PORT = 8188

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
                            if job_data.get("status") in ("error", "cancelled"):
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
        return {**first} if first else None  # fresh copy so Gradio detects the change
    except Exception:
        return None


# ── Config helpers ───────────────────────────────────────────────────────────

def _hosts_from_cluster_conf() -> list[str]:
    """Return non-comment, non-empty hostnames from cluster.conf."""
    if not CLUSTER_CONF.exists():
        return []
    hosts = []
    for line in CLUSTER_CONF.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            hosts.append(line)
    return hosts


def _default_workers() -> tuple[list[str], list[str]]:
    """Derive comfy_workers and tts_workers from cluster.conf."""
    hosts = _hosts_from_cluster_conf()
    if not hosts:
        return [], []
    comfy = [f"http://{h}:{COMFYUI_PORT}" for h in hosts]
    return comfy, hosts


def load_config() -> dict:
    cfg = DEFAULT_CFG.copy()
    # Seed worker defaults from cluster.conf before applying saved overrides
    comfy, tts = _default_workers()
    cfg["comfy_workers"] = comfy
    cfg["tts_workers"]   = tts
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def save_session(combined: str, music: str, ambient: str,
                 voice_vol: float, music_vol: float, ambient_vol: float) -> None:
    """Persist remix source paths so they survive browser reconnects."""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({
        "combined": combined,
        "music":    music,
        "ambient":  ambient,
        "voice_vol":   voice_vol,
        "music_vol":   music_vol,
        "ambient_vol": ambient_vol,
    }, indent=2))


def load_session() -> dict | None:
    """Return the last saved session, or None if unavailable."""
    try:
        data = json.loads(SESSION_FILE.read_text())
        # Verify the key files still exist on disk
        if Path(data.get("combined", "")).exists() and Path(data.get("music", "")).exists():
            return data
    except Exception:
        pass
    return None


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


def _work_dir_marker_mtime(work_dir: Path) -> float:
    return max(
        (p.stat().st_mtime for p in (work_dir / "progress.json", work_dir / "script.json", work_dir / "job.json")
         if p.exists()),
        default=0.0,
    )


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


def _collect_job_outputs(work_dir: Path):
    final_path = _final_path_for_work_dir(work_dir)
    combined = work_dir / "combined.mp4"
    music = work_dir / "background_music.wav"
    ambient = work_dir / "ambient.wav"

    # combined.mp4 must exist — it's produced by this job's own assembly step.
    # Without it, final_path may have been found by the recency heuristic and
    # could belong to a different job, causing a false "done" detection.
    if not (final_path.exists() and final_path.stat().st_size > 10_000) or not combined.exists():
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    try:
        meta = _read_json(_job_meta_path(work_dir))
    except Exception:
        meta = {}
    # already_done: the job was marked done in a previous session — don't re-trigger tab switch
    already_done = meta.get("status") == "done"
    remix_already_selected = bool(meta.get("ui_remix_selected_at"))

    amb_str = str(ambient) if ambient.exists() else ""
    if combined.exists() and music.exists():
        save_session(str(combined), str(music), amb_str,
                     load_config().get("voice_vol", 100),
                     load_config().get("music_vol", 18),
                     load_config().get("ambient_vol", 0))
    meta_updates = {"status": "done", "final_path": str(final_path)}
    if not remix_already_selected:
        meta_updates["ui_remix_selected_at"] = time.time()
    _write_job_meta(work_dir, **meta_updates)
    return (
        gr.update(value=str(final_path), visible=True),
        str(combined) if combined.exists() else "",
        str(music) if music.exists() else "",
        amb_str,
        # Select Remix only when transitioning to done in this session.
        # If already_done, this is a page reload seeing an old completed job — skip.
        gr.update() if (remix_already_selected or already_done) else gr.update(selected="output"),
    )


def get_voice_choices() -> list[str]:
    return [F5TTS_DEFAULT_OPTION] + [v["name"] for v in load_config().get("voices", [])]


def voice_path_for(name: str) -> str | None:
    if not name or name == F5TTS_DEFAULT_OPTION:
        return None
    for v in load_config().get("voices", []):
        if v["name"] == name:
            return v["path"]
    return None


def voices_as_rows() -> list[list[str]]:
    return [[v["name"], v["path"]] for v in load_config().get("voices", [])]


# ── UI helpers ───────────────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-")


def _progress_html(pct: float, msg: str) -> str:
    pct   = max(0.0, min(100.0, pct))
    color = "#22c55e" if pct >= 100 else "#7c3aed"
    return (
        f'<div style="padding:6px 0">'
        f'<div style="font-size:14px;margin-bottom:6px;color:#374151">{msg}</div>'
        f'<div style="background:#e5e7eb;border-radius:6px;height:10px;overflow:hidden">'
        f'<div style="background:{color};width:{pct:.1f}%;height:100%;border-radius:6px;transition:width 0.4s"></div>'
        f'</div>'
        f'<div style="font-size:12px;color:#9ca3af;margin-top:3px;text-align:right">{pct:.0f}%</div>'
        f'</div>'
    )


def _error_html(msg: str) -> str:
    return (
        f'<div style="padding:6px 0">'
        f'<div style="font-size:14px;margin-bottom:6px;color:#dc2626">{msg}</div>'
        f'<div style="background:#fee2e2;border-radius:6px;height:10px;overflow:hidden">'
        f'<div style="background:#dc2626;width:100%;height:100%;border-radius:6px"></div>'
        f'</div>'
        f'<div style="font-size:12px;color:#dc2626;margin-top:3px">Generation stopped — fix the issue and retry.</div>'
        f'</div>'
    )


def _poll_progress(active_job_dir: str = "") -> str:
    """Read progress.json for the active job, falling back to the latest job."""
    try:
        work_dir = _preferred_work_dir(active_job_dir)
        if work_dir is None:
            return _progress_html(0, "Waiting to start…")
        pct, msg = _status_for_work_dir(work_dir)
        return _progress_html(pct, msg)
    except Exception:
        return _progress_html(0, "Waiting to start…")


def _poll_job_outputs(active_job_dir: str):
    work_dir = _preferred_work_dir(active_job_dir)
    if work_dir is None:
        return (_progress_html(0, "Waiting to start…"), gr.update(), gr.update(),
                gr.update(), gr.update(), gr.update(), "", False)
    final, comb, mus, amb, tabs_upd = _collect_job_outputs(work_dir)

    # Detect a fresh completion transition (status just became "done" this tick)
    auto_post_trigger = False
    try:
        now = time.time()
        meta = _read_json(_job_meta_path(work_dir))
        paused_until = float(meta.get("_auto_post_paused_until") or 0)
        if paused_until and now >= paused_until:
            # Pause window expired — clear it and unblock the in-memory set so
            # the job can be auto-posted on the next eligible tick.
            _write_job_meta(work_dir, _auto_post_paused_until=None)
            with _auto_post_lock:
                _auto_post_triggered.discard(str(work_dir))
            meta = _read_json(_job_meta_path(work_dir))  # re-read after update
        just_done = (
            meta.get("status") == "done"
            and not meta.get("youtube_video_id")
            and not meta.get("_auto_post_triggered")
            and not (paused_until and now < paused_until)
        )
        if just_done and load_config().get("youtube_auto_post"):
            # Use an in-process lock + set to guard against two rapid timer
            # ticks both reading _auto_post_triggered=False before the first
            # write reaches disk (race condition → double upload).
            with _auto_post_lock:
                if str(work_dir) not in _auto_post_triggered:
                    _auto_post_triggered.add(str(work_dir))
                    _write_job_meta(work_dir, _auto_post_triggered=True)
                    tabs_upd = gr.update(selected="post")
                    auto_post_trigger = True
    except Exception:
        pass

    return (_poll_progress(str(work_dir)), final, comb, mus, amb, tabs_upd, str(work_dir), auto_post_trigger)


def _poll_cover_image(active_job_dir: str):
    """Return a gr.update for the progress-tab cover image, showing it as soon as it exists."""
    try:
        work_dir = _preferred_work_dir(active_job_dir)
        if work_dir is None:
            return gr.update()
        cover = work_dir / "cover.png"
        if cover.exists() and cover.stat().st_size > 1000:
            return gr.update(value=str(cover), visible=True)
    except Exception:
        pass
    return gr.update()


def _mark_existing_scene_previews_succeeded(store: DurableStore, job_id: str) -> None:
    for row in store.scene_rows(job_id):
        preview = row.get("preview_path") or ""
        if preview and Path(preview).exists():
            store.skip_task_if_artifact_exists(
                task_id(job_id, "scene", int(row["id"]), "image"),
                preview,
                artifact_kind="image",
                result={"source": "script_preview"},
            )


def _orchestration_html(active_job_dir: str = "") -> str:
    """Render durable job/task/worker state for the Progress tab."""
    store = None
    try:
        store = DurableStore.default()
        preferred = _preferred_work_dir(active_job_dir)
        job = store.get_job_by_work_dir(str(preferred)) if preferred else None
        if job is None:
            recent = store.recent_jobs(limit=1)
            job = recent[0] if recent else None
        if job is None:
            return (
                '<div style="font-size:13px;color:#6b7280;padding:6px 0">'
                "No durable jobs recorded yet.</div>"
            )

        _mark_existing_scene_previews_succeeded(store, job["id"])
        summary = store.job_summary(job["id"])
        counts = summary["counts"]
        tasks = store.task_rows(job["id"])
        workers = store.worker_rows()
        count_text = " &middot; ".join(
            f"{html.escape(str(k))}: {v}" for k, v in sorted(counts.items())
        ) or "no tasks"

        # Sort tasks: active/pending first (by execution order), succeeded last
        _STATUS_SORT = {
            "running": 0, "leased": 1, "queued": 2,
            "failed_retryable": 3, "lost": 4,
            "failed_terminal": 5, "cancelled": 6, "succeeded": 7,
        }
        tasks_sorted = sorted(
            tasks, key=lambda r: (_STATUS_SORT.get(r["status"], 99), int(r["priority"]))
        )

        _STATUS_COLOR = {
            "running": "#16a34a", "leased": "#0ea5e9", "queued": "#9333ea",
            "succeeded": "#6b7280", "failed_retryable": "#f59e0b",
            "failed_terminal": "#dc2626", "cancelled": "#9ca3af", "lost": "#f97316",
        }

        task_rows = []
        for row in tasks_sorted:
            err = row["error"] or ""
            color = _STATUS_COLOR.get(row["status"], "#374151")
            task_rows.append(
                "<tr>"
                f"<td>{html.escape(row['name'])}</td>"
                f"<td style='color:{color};font-weight:600'>{html.escape(row['status'])}</td>"
                f"<td>{html.escape(row['worker_kind'])}</td>"
                f"<td>{int(row['attempt'])}/{int(row['max_attempts'])}</td>"
                f"<td>{html.escape(err[:120])}</td>"
                "</tr>"
            )

        # Look up active task from lease_owner so it shows even without heartbeat_worker
        running_task_by_worker = {
            r["lease_owner"]: r["name"]
            for r in tasks
            if r["status"] in ("running", "leased") and r["lease_owner"]
        }

        worker_rows = []
        for row in workers:
            active_task = running_task_by_worker.get(row["id"]) or row["active_task_id"] or ""
            if active_task and len(active_task) > 60:
                active_task = "…" + active_task[-60:]
            worker_rows.append(
                "<tr>"
                f"<td><code style='font-size:10px;user-select:all'>{html.escape(row['id'])}</code></td>"
                f"<td>{html.escape(row['kind'])}</td>"
                f"<td>{html.escape(row['endpoint'])}</td>"
                f"<td>{html.escape(row['status'])}</td>"
                f"<td style='font-size:11px'>{html.escape(active_task)}</td>"
                "</tr>"
            )

        return f"""
        <div style="font-size:13px;color:#374151;padding:4px 0">
          <div><strong>{html.escape(job['title'] or Path(job['work_dir']).name)}</strong></div>
          <div style="color:#6b7280">status: {html.escape(job['status'])} &middot; {count_text}</div>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:8px">
          <thead><tr style="text-align:left;color:#6b7280">
            <th>Task</th><th>Status</th><th>Worker</th><th>Attempts</th><th>Error</th>
          </tr></thead>
          <tbody>{''.join(task_rows)}</tbody>
        </table>
        <div style="font-size:13px;font-weight:600;margin-top:14px">Workers</div>
        <div style="font-size:11px;color:#9ca3af;margin-bottom:4px">
          Copy a worker ID below and paste it into the "Worker ID" field above to change its status.
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px">
          <thead><tr style="text-align:left;color:#6b7280">
            <th>ID</th><th>Kind</th><th>Endpoint</th><th>Status</th><th>Active Task</th>
          </tr></thead>
          <tbody>{''.join(worker_rows) or '<tr><td colspan="5">No workers registered yet.</td></tr>'}</tbody>
        </table>
        """
    except Exception as exc:
        logger.debug("Could not render orchestration status", exc_info=True)
        return (
            '<div style="font-size:13px;color:#dc2626;padding:6px 0">'
            f"Could not load durable orchestration state: {html.escape(str(exc)[:160])}</div>"
        )
    finally:
        if store is not None:
            store.close()


def _active_job_row(active_job_dir: str = ""):
    store = DurableStore.default()
    try:
        job = store.get_job_by_work_dir(active_job_dir) if active_job_dir else None
        if job is None:
            recent = store.recent_jobs(limit=1)
            job = recent[0] if recent else None
        return dict(job) if job else None
    finally:
        store.close()


def on_resume_active_job(active_job_dir: str):
    job = _active_job_row(active_job_dir)
    if not job:
        return "No durable job available to resume.", gr.update(), active_job_dir
    work_dir = Path(job["work_dir"])
    if not work_dir.exists():
        return f"Work directory missing: {work_dir}", gr.update(), active_job_dir
    store = DurableStore.default()
    try:
        store.recover_incomplete_tasks(job["id"])
        store.update_job(job["id"], status="running", progress_message="resume requested")
    finally:
        store.close()
    _launch_generation_job(work_dir)
    return f"Resume launched for {work_dir.name}", gr.update(selected="progress"), str(work_dir)


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


def on_load_and_resume_job(work_dir_str: str):
    """Load a paused/interrupted job, re-queue its incomplete tasks, and restart generation."""
    if not work_dir_str:
        return "No job selected.", gr.update(), gr.update()
    work_dir = Path(work_dir_str)
    if not work_dir.exists():
        return f"Directory not found: {work_dir_str}", gr.update(), gr.update()
    store = DurableStore.default()
    try:
        job = store.get_job_by_work_dir(str(work_dir))
        if job:
            store.recover_incomplete_tasks(job["id"])
            store.update_job(job["id"], status="running", progress_message="resumed")
    finally:
        store.close()
    _launch_generation_job(work_dir)
    _write_job_meta(work_dir, status="running")
    return (
        f"Resumed {work_dir.name}",
        gr.update(selected="progress"),
        str(work_dir),
    )


def on_set_worker_status(worker_id_value: str, status: str):
    worker_id_value = (worker_id_value or "").strip()
    if not worker_id_value:
        return "Enter a worker id from the Durable Orchestration table."
    store = DurableStore.default()
    try:
        updated = store.set_worker_status(worker_id_value, status)
    finally:
        store.close()
    if not updated:
        return f"Worker not found: {worker_id_value}"
    return f"Worker {worker_id_value} marked {status}."


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


def _scene_summary_html(job_id: str, current_scene_id: int) -> str:
    if not job_id:
        return ""
    store = DurableStore.default()
    try:
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    items = []
    for row in rows:
        sid = int(row["id"])
        title = html.escape(row.get("title") or f"Scene {sid}")
        current = sid == int(current_scene_id or 1)
        bg = "#eef2ff" if current else "#fff"
        border = "#6366f1" if current else "#e5e7eb"
        preview = " · preview" if row.get("preview_path") else ""
        items.append(
            f'<div style="border:1px solid {border};background:{bg};'
            f'border-radius:6px;padding:6px 8px;font-size:12px">'
            f'<strong>{sid:02d}</strong> {title}{preview}</div>'
        )
    return (
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));'
        f'gap:6px;max-height:220px;overflow:auto">{"".join(items)}</div>'
    )


def _load_scene_editor(job_id: str, scene_id: int) -> tuple:
    if not job_id:
        return (
            gr.update(value=1, minimum=1, maximum=2),
            "**Scene 0/0**",
            "", "", "", "",
            gr.update(value=None, visible=False),
            "",
        )
    store = DurableStore.default()
    try:
        total = max(1, store.scene_count(job_id))
        sid = min(max(1, int(scene_id or 1)), total)
        scene = store.get_scene(job_id, sid) or {}
    finally:
        store.close()
    preview = scene.get("preview_path") or ""
    preview_update = (
        gr.update(value=preview, visible=True)
        if preview and Path(preview).exists()
        else gr.update(value=None, visible=False)
    )
    return (
        gr.update(value=sid, minimum=1, maximum=max(2, total)),
        f"**Scene {sid}/{total}**",
        scene.get("title", ""),
        scene.get("image_prompt", ""),
        scene.get("video_prompt", ""),
        scene.get("narration", ""),
        preview_update,
        _scene_summary_html(job_id, sid),
    )


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


def _navigate_scene(
    direction: int,
    job_id: str,
    current_scene_id: int,
    title: str,
    image_prompt: str,
    video_prompt: str,
    narration: str,
) -> tuple:
    _save_active_scene(job_id, current_scene_id, title, image_prompt, video_prompt, narration)
    store = DurableStore.default()
    try:
        total = max(1, store.scene_count(job_id)) if job_id else 1
    finally:
        store.close()
    next_id = min(max(1, int(current_scene_id or 1) + int(direction)), total)
    return (next_id, *_load_scene_editor(job_id, next_id))


def _jump_scene(
    target_scene_id: int,
    job_id: str,
    current_scene_id: int,
    title: str,
    image_prompt: str,
    video_prompt: str,
    narration: str,
) -> tuple:
    _save_active_scene(job_id, current_scene_id, title, image_prompt, video_prompt, narration)
    store = DurableStore.default()
    try:
        total = max(1, store.scene_count(job_id)) if job_id else 1
    finally:
        store.close()
    next_id = min(max(1, int(target_scene_id or 1)), total)
    return (next_id, *_load_scene_editor(job_id, next_id))


# ── YouTube cover image ──────────────────────────────────────────────────────
# _overlay_title_on_image and _cover_prompt are imported from pipeline.cover


def on_generate_cover_image(video_title: str, style: str, job_id: str):
    """Generate a YouTube cover image for the current video.

    If the pipeline already produced cover.png during generation, show it immediately
    without hitting the ComfyUI worker again.
    """
    title = (video_title or "").strip()
    if not title:
        yield gr.update(value="Enter a Video Title first."), gr.update(visible=False)
        return

    work_dir = _job_work_dir(job_id) if job_id else None
    if work_dir is None:
        work_dir = _latest_work_dir()
    if work_dir is None:
        yield gr.update(value="No job found. Generate a script first."), gr.update(visible=False)
        return

    cover_path = work_dir / "cover.png"

    # If the pipeline already generated the cover image, return it immediately.
    if cover_path.exists() and cover_path.stat().st_size > 1000:
        yield (
            gr.update(value="Cover image ready (generated during video production)."),
            gr.update(value=str(cover_path), visible=True),
        )
        return

    worker_urls = _preview_worker_urls()
    if not worker_urls:
        yield gr.update(value="No cluster workers reachable — add workers in Settings."), gr.update(visible=False)
        return

    cfg = load_config()
    # Load scenes so the cover incorporates actual video aspects, not just the title.
    scenes_for_cover = _load_scenes_for_work_dir(work_dir)
    logger.info("Cover gen: loaded %d scenes from %s", len(scenes_for_cover), work_dir.name)
    prompt = _cover_prompt(_shorten_title(title), style or "", scenes=scenes_for_cover)

    yield (
        gr.update(value=f"Generating cover image for '{title}'…"),
        gr.update(visible=False),
    )

    try:
        worker_pool = WorkerPool(worker_urls)
        base_path = work_dir / "cover_base.png"
        url = worker_pool.acquire()
        try:
            generate_scene_image(
                prompt, base_path,
                width=_COVER_W, height=_COVER_H,
                steps=int(cfg.get("flux_steps", 4)),
                flux_model=cfg.get("flux_model", "flux1-schnell-fp8.safetensors"),
                clip_t5=cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
                clip_l=cfg.get("flux_clip_l", "clip_l.safetensors"),
                flux_vae=cfg.get("flux_vae", "ae.safetensors"),
                comfy_url=url,
            )
        finally:
            worker_pool.release(url)

        _overlay_title_on_image(base_path, cover_path, title)
        yield (
            gr.update(value=f"Cover image saved: {cover_path.name}"),
            gr.update(value=str(cover_path), visible=True),
        )
    except Exception as e:
        logger.warning("Cover image generation failed: %s", e)
        yield (
            gr.update(value=f"Cover image failed: {html.escape(str(e)[:160])}"),
            gr.update(visible=False),
        )


# ── TTS wrapper ──────────────────────────────────────────────────────────────

def _tts(text: str, out: Path, voice_ref: str | None, host: str = "localhost") -> None:
    from pipeline.tts_worker import generate_narration
    ref = Path(voice_ref) if voice_ref and Path(voice_ref).exists() else None
    generate_narration(text, out, reference_wav=ref, host=host)


# ── Script generation ────────────────────────────────────────────────────────

def on_generate_script(video_title: str, title: str, n_scenes: int, auto_approve: bool):
    topic = title.strip() or video_title.strip()
    if not topic:
        raise gr.Error("Please enter a Video Title or describe what you want to create.")

    logger.info("on_generate_script — video_title=%r title=%r n_scenes=%d auto_approve=%s",
                video_title, title, n_scenes, auto_approve)

    _no_op = (gr.update(),) * 17

    cfg = load_config()
    extra = cfg.get("script_extra_instructions", "").strip()
    if extra:
        topic = f"{topic}\n\n{extra}"

    # Run generate_script in a thread so we can yield keep-alives while waiting.
    # Without this, long Claude API calls (30 scenes ≈ 3 min) cause Gradio's
    # WebSocket to time out and the page resets to the Create tab.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(
            generate_script, topic, int(n_scenes),
            cfg.get("default_visual_style", "") or None,  # style_hint
            video_title.strip() or None,
        )
        while not fut.done():
            yield _no_op
            time.sleep(3)

    try:
        scenes, music_desc, style = fut.result()
    except Exception as e:
        logger.exception("generate_script failed")
        first_line = str(e).split("\n")[0][:300]
        gr.Warning(f"Script generation failed: {first_line}")
        yield _no_op
        return

    logger.info("Script generated — %d scenes, music: %r, style: %r", len(scenes), music_desc, style)

    try:
        next_tab = "progress" if auto_approve else "script"
        display_title = video_title.strip() or topic

        work_dir = _script_work_dir(display_title)
        job_id = job_id_from_work_dir(work_dir)
        scenes_list = [
            {"id": s.id, "title": s.title, "image_prompt": s.image_prompt,
             "video_prompt": s.video_prompt, "narration": s.narration}
            for s in scenes
        ]
        _persist_script_snapshot(work_dir, scenes_list)

        store = DurableStore.default()
        try:
            store.create_or_update_job(
                job_id,
                work_dir,
                display_title,
                config={"title": display_title, "video_title": video_title.strip(),
                        "topic": topic, "phase": "script_review"},
                metadata={"scene_count": len(scenes_list), "music_desc": music_desc, "style": style},
            )
            store.upsert_scenes(job_id, scenes_list)
        finally:
            store.close()

        scene_outputs = _load_scene_editor(job_id, 1)
        result = (
            gr.update(selected=next_tab),
            gr.update(visible=True),
            job_id,
            str(work_dir),
            1,
            music_desc,   # → music_desc_state
            style,        # → style_box
            style,        # → style_state
            f"### {display_title}\n\n{len(scenes_list)} scenes · {work_dir.name}",
            *scene_outputs,
        )
        logger.info("on_generate_script returning %d scenes, next_tab=%r", len(scenes), next_tab)
        yield result
    except Exception as e:
        logger.exception("on_generate_script failed assembling return value")
        gr.Warning(f"Failed to process script: {str(e)[:200]}")
        yield _no_op


# ── Scene image generation — one active scene only ───────────────────────────

_IMG_GEN_OUT_COUNT = 3


def _preview_worker_urls() -> list[str]:
    cfg = load_config()
    all_workers = cfg.get("comfy_workers", [])
    try:
        return alive_workers(all_workers)
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


def on_generate_scene_previews(job_id: str, scene_id: int, resolution: str, style: str,
                               title: str, image_prompt: str, video_prompt: str,
                               narration: str, auto_approve: bool = False):
    """Generate missing first-frame previews for every scene in the script."""
    no_op = (gr.update(),) * _IMG_GEN_OUT_COUNT
    if not job_id:
        yield no_op
        return
    try:
        _save_active_scene(job_id, scene_id, title, image_prompt, video_prompt, narration)
        store = DurableStore.default()
        try:
            rows = store.scene_rows(job_id)
            scene = store.get_scene(job_id, int(scene_id or 1)) or {}
        finally:
            store.close()
        if not rows:
            yield no_op
            return

        selected_id = int(scene_id or 1)
        selected_existing = scene.get("preview_path") or ""
        missing = [
            row for row in rows
            if not (row.get("preview_path") and Path(row["preview_path"]).exists())
        ]
        if not missing:
            yield (
                gr.update(value="", visible=False),
                gr.update(value=selected_existing, visible=True, label="Scene Preview (first frame)")
                if selected_existing and Path(selected_existing).exists()
                else gr.update(),
                gr.update(value=_scene_summary_html(job_id, selected_id)),
            )
            return

        worker_urls = _preview_worker_urls()
        if not worker_urls:
            raise RuntimeError("No cluster workers reachable for scene preview generation.")
        worker_pool = WorkerPool(worker_urls)
        total = len(rows)
        done = total - len(missing)

        yield (
            gr.update(
                value=f'<div style="color:#7c3aed;padding:4px 0;font-size:13px">'
                      f'Generating initial scene previews {done}/{total}...</div>',
                visible=True,
            ),
            gr.update(),
            gr.update(),
        )

        max_workers = min(len(worker_urls), len(missing))
        completed = 0
        selected_preview: Path | None = (
            Path(selected_existing)
            if selected_existing and Path(selected_existing).exists()
            else None
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            failures: list[int] = []
            future_map = {
                pool.submit(
                    _generate_active_scene_preview,
                    job_id,
                    int(row["id"]),
                    resolution,
                    style,
                    row.get("title") or f"Scene {int(row['id'])}",
                    row.get("image_prompt") or row.get("title") or f"Scene {int(row['id'])}",
                    force=False,
                    worker_pool=worker_pool,
                ): int(row["id"])
                for row in missing
            }
            for fut in concurrent.futures.as_completed(future_map):
                sid = future_map[fut]
                completed += 1
                try:
                    out = fut.result()
                    if sid == selected_id:
                        selected_preview = out
                except Exception as exc:
                    logger.warning("Scene %d initial preview failed: %s", sid, exc)
                    failures.append(sid)
                preview_update = (
                    gr.update(value=str(selected_preview), visible=True, label="Scene Preview (first frame)")
                    if selected_preview and selected_preview.exists()
                    else gr.update()
                )
                yield (
                    gr.update(
                        value=f'<div style="color:#7c3aed;padding:4px 0;font-size:13px">'
                              f'Generating initial scene previews {done + completed}/{total}...</div>',
                        visible=True,
                    ),
                    preview_update,
                    gr.update(value=_scene_summary_html(job_id, selected_id)),
                )

        store = DurableStore.default()
        try:
            selected = store.get_scene(job_id, selected_id) or {}
            selected_path = selected.get("preview_path") or ""
        finally:
            store.close()
        yield (
            gr.update(
                value=f'<div style="color:#ef4444;padding:4px 0;font-size:13px">'
                      f'Preview generation failed for scenes: '
                      f'{html.escape(", ".join(str(s) for s in failures))}</div>',
                visible=True,
            ) if failures else gr.update(value="", visible=False),
            gr.update(value=selected_path, visible=True, label="Scene Preview (first frame)")
            if selected_path and Path(selected_path).exists()
            else gr.update(),
            gr.update(value=_scene_summary_html(job_id, selected_id)),
        )
    except Exception as e:
        logger.warning("Initial scene preview generation failed: %s", e)
        yield (
            gr.update(
                value=f'<div style="color:#ef4444;padding:4px 0;font-size:13px">'
                      f'Initial scene previews failed: {html.escape(str(e)[:120])}</div>',
                visible=True,
            ),
            gr.update(),
            gr.update(),
        )


def on_regen_active_scene(job_id: str, scene_id: int, resolution: str, style: str,
                          title: str, image_prompt: str, video_prompt: str, narration: str):
    """Regenerate the FLUX preview image for the active scene only."""
    no_op = (gr.update(),) * _IMG_GEN_OUT_COUNT
    if not job_id:
        yield no_op
        return

    _save_active_scene(job_id, scene_id, title, image_prompt, video_prompt, narration)
    try:
        yield (
            gr.update(
                value=f'<div style="color:#7c3aed;padding:4px 0;font-size:13px">'
                      f'Regenerating scene {int(scene_id)} preview...</div>',
                visible=True,
            ),
            gr.update(),
            gr.update(),
        )
        out = _generate_active_scene_preview(
            job_id,
            int(scene_id),
            resolution,
            style,
            title,
            image_prompt,
            force=True,
        )
        yield (
            gr.update(value="", visible=False),
            gr.update(value=str(out), visible=True, label="Scene Preview (first frame)"),
            gr.update(value=_scene_summary_html(job_id, int(scene_id))),
        )
    except Exception as e:
        logger.warning("Scene %d regen failed: %s", int(scene_id), e)
        yield (
            gr.update(
                value=f'<div style="color:#ef4444;padding:4px 0;font-size:13px">'
                      f'Scene {int(scene_id)} preview failed: {html.escape(str(e)[:120])}</div>',
                visible=True,
            ),
            gr.update(),
            gr.update(),
        )


# ── Video generation — generator, yields progressive UI updates ──────────────

# gen_outputs count: progress, music, final, combined_state, music_state,
# ambient_state, tabs, active_job
_GEN_OUT_COUNT = 8


def on_generate(video_title, title, n_scenes_val, voice_name, resolution, music_desc, style, auto_approve,
                job_id: str, work_dir_str: str, current_scene_id: int,
                scene_title: str, image_prompt: str, video_prompt: str, narration: str):
    active_job_dir = work_dir_str or ""
    try:
        if not job_id or not work_dir_str:
            raise gr.Error("No generated script is available. Generate the script again.")

        _save_active_scene(
            job_id,
            current_scene_id,
            scene_title,
            image_prompt,
            video_prompt,
            narration,
        )
        work_dir = Path(work_dir_str)
        store = DurableStore.default()
        try:
            scene_rows = store.scene_rows(job_id)
        finally:
            store.close()
        if not scene_rows:
            raise gr.Error("No scene data is available. Generate the script again.")

        cfg = load_config()
        if not voice_name or voice_name == F5TTS_DEFAULT_OPTION:
            voice_name = cfg.get("default_voice", voice_name)
        voice_ref = voice_path_for(voice_name)
        vid_width, vid_height = _RESOLUTIONS.get(
            resolution or cfg.get("resolution", _DEFAULT_RESOLUTION), (832, 480)
        )
        style_clean = style.strip().rstrip(".") if style and style.strip() else ""
        default_style = cfg.get("default_visual_style", "").strip().rstrip(".")
        combined_parts = [p for p in [style_clean, default_style] if p]
        combined_style = ". ".join(combined_parts)
        scenes = [
            Scene(
                id=int(row["id"]),
                title=row.get("title") or f"Scene {int(row['id'])}",
                image_prompt=(
                    f"{combined_style}. {row.get('image_prompt') or title}"
                    if combined_style
                    else (row.get("image_prompt") or title)
                ),
                video_prompt=row.get("video_prompt") or row.get("image_prompt") or title,
                narration=row.get("narration") or "",
            )
            for row in scene_rows[: int(n_scenes_val)]
        ]
        _persist_script_snapshot(
            work_dir,
            [
                {
                    "id": s.id,
                    "title": s.title,
                    "image_prompt": s.image_prompt,
                    "video_prompt": s.video_prompt,
                    "narration": s.narration,
                }
                for s in scenes
            ],
        )

        job_cfg = _job_config_snapshot(cfg)
        job_cfg["resolution"] = resolution or cfg.get("resolution", _DEFAULT_RESOLUTION)
        job_cfg["max_clip_secs"] = 0
        job_cfg["default_voice"] = voice_name
        job_cfg["voice_ref"] = voice_ref or ""
        job_cfg["music_desc"] = music_desc or ""
        job_cfg["title"] = title
        job_cfg["video_title"] = (video_title or "").strip()
        job_cfg["style"] = style_clean

        # Link this job to its originating YouTube queue item (if any) so that
        # _notify_comment_requester can find the right queue entry by stable ID
        # even if the video title is later changed before posting.
        _queue_item_id_for_job = ""
        try:
            _title_key = (video_title or "").strip().lower()
            if _title_key:
                _queue_snapshot = yt.load_queue()
                _q_match = next(
                    (q for q in _queue_snapshot
                     if q.get("status") == "pending"
                     and q.get("final_title", "").lower() == _title_key),
                    None,
                )
                if _q_match:
                    _queue_item_id_for_job = _q_match["id"]
                    logger.info("Linked job to queue item %s (%r)", _queue_item_id_for_job, video_title)
        except Exception:
            pass
        job_cfg["queue_item_id"] = _queue_item_id_for_job

        (work_dir / "job_config.json").write_text(json.dumps(job_cfg, indent=2))
        (work_dir / "progress.json").write_text(
            json.dumps({"pct": 0, "msg": "Generation job queued", "ts": time.time()})
        )

        durable_store = DurableStore.default()
        try:
            durable_store.ensure_generation_plan(
                job_id,
                work_dir,
                title,
                scenes,
                {
                    **job_cfg,
                    "vid_width": vid_width,
                    "vid_height": vid_height,
                    "resource_classes": {
                        "image": "comfy:image",
                        "music": "comfy:music",
                        "video": "comfy:video",
                        "narration": "tts",
                        "finalize": "local",
                    },
                },
            )
            durable_store.update_job(
                job_id,
                status="running",
                progress_pct=0,
                progress_message="generation job launched",
            )
        finally:
            durable_store.close()

        _launch_generation_job(work_dir)
        yield (
            _progress_html(0, f"Generation job started - {work_dir.name}"),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(selected="progress"),
            str(work_dir),
        )
    except Exception as e:
        logger.exception("on_generate failed")
        first_line = str(e).split("\n")[0][:300]
        yield (
            _error_html(f"Error: {first_line}"),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            active_job_dir,
        )


def _auto_generate(video_title, title, n_scenes_val, voice_name, resolution, music_desc, style, auto_approve,
                   job_id: str, work_dir_str: str, current_scene_id: int,
                   scene_title: str, image_prompt: str, video_prompt: str, narration: str):
    if not auto_approve:
        yield (gr.update(),) * _GEN_OUT_COUNT
        return
    # Guard against duplicate auto-start chains (e.g. demo.load startup trigger
    # AND a button click both fire before the first job is running).  By the
    # time this function is reached (after on_generate_script + scene previews),
    # the first chain will already have written job_config.json, so
    # _is_job_running() reliably returns True for any second chain.
    if _is_job_running():
        logger.info(
            "_auto_generate: a job is already running — dropping duplicate auto-start for %r",
            video_title,
        )
        # Clean up the orphaned script folder this chain created so the
        # ~/videos directory does not accumulate spurious timestamped dirs.
        orphan = Path(work_dir_str) if work_dir_str else None
        if orphan and orphan.exists() and not (orphan / "job_config.json").exists():
            try:
                shutil.rmtree(orphan)
                logger.info("_auto_generate: removed orphaned script dir %s", orphan.name)
            except Exception as exc:
                logger.warning("_auto_generate: could not remove orphan %s: %s", orphan, exc)
        yield (gr.update(),) * _GEN_OUT_COUNT
        return
    yield from on_generate(
        video_title,
        title,
        n_scenes_val,
        voice_name,
        resolution,
        music_desc,
        style,
        auto_approve,
        job_id,
        work_dir_str,
        current_scene_id,
        scene_title,
        image_prompt,
        video_prompt,
        narration,
    )


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


def on_load_script(work_dir_str: str):
    """Load a saved script into the Script tab for review/editing."""
    _no_op = (gr.update(),) * 19
    if not work_dir_str:
        return _no_op
    work_dir = Path(work_dir_str)
    script_path = work_dir / "script.json"
    if not script_path.exists():
        gr.Warning("No script found in the selected folder.")
        return _no_op

    scenes_list = json.loads(script_path.read_text())
    n_scenes = len(scenes_list)

    # Read metadata from DurableStore (written at script-generation time)
    job_id = job_id_from_work_dir(work_dir)
    video_title = work_dir.name.replace("-", " ").title()
    style = ""
    music_desc = ""
    store = DurableStore.default()
    try:
        job = store.get_job(job_id)
        if job:
            d = dict(job)
            cfg = json.loads(d.get("config_json") or "{}")
            meta = json.loads(d.get("metadata_json") or "{}")
            video_title = cfg.get("video_title") or d.get("title") or video_title
            style = meta.get("style", "")
            music_desc = meta.get("music_desc", "")
        store.create_or_update_job(
            job_id, work_dir, video_title,
            config={"video_title": video_title, "phase": "script_review"},
            metadata={"scene_count": n_scenes, "music_desc": music_desc, "style": style},
        )
        store.upsert_scenes(job_id, scenes_list)
    finally:
        store.close()

    scene_outputs = _load_scene_editor(job_id, 1)
    return (
        gr.update(selected="script"),   # tabs
        gr.update(visible=True),        # script_col
        job_id,                         # script_job_id_state
        str(work_dir),                  # script_work_dir_state
        1,                              # current_scene_state
        music_desc,                     # music_desc_state
        style,                          # style_box
        style,                          # style_state
        f"### {video_title}\n\n{n_scenes} scenes · {work_dir.name} *(loaded)*",
        *scene_outputs,                 # scene_picker … scene_summary_html (8 values)
        video_title,                    # video_title_in
        n_scenes,                       # n_scenes_in
    )


def _get_scene_df_rows(work_dir: Path) -> list[list]:
    """Return DataFrame rows [[include, order, label], ...] for the remix scene table."""
    found = sorted(work_dir.glob("scene_??_final.mp4"))
    if not found:
        return []
    store = DurableStore()
    job_id = job_id_from_work_dir(work_dir)
    scene_titles: dict[int, str] = {}
    try:
        db_rows = store.scene_rows(job_id)
        for row in db_rows:
            scene_titles[int(row["id"])] = row.get("title", "")
    except Exception:
        pass
    rows = []
    for order, p in enumerate(found, start=1):
        try:
            num = int(p.stem.split("_")[1])
        except (IndexError, ValueError):
            num = order
        title = scene_titles.get(num, "")
        label = f"Scene {num:02d}: {title}" if title else f"Scene {num:02d}"
        rows.append([True, order, label])
    return rows


# Keep for backward compat with _remix_select_all
def _get_scene_choices(work_dir: Path) -> tuple[list, list]:
    rows = _get_scene_df_rows(work_dir)
    choices = [(r[2], r[2]) for r in rows]
    values = [r[2] for r in rows]
    return choices, values


def _load_job_for_remix(work_dir_str: str):
    """Load a job directory into Remix tab. Returns same shape as on_restore_session."""
    if not work_dir_str:
        return (gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(), gr.update(), gr.update(),
                gr.update(value=[]),
                "No job selected.")
    work_dir = Path(work_dir_str)
    combined = work_dir / "combined.mp4"
    music = work_dir / "background_music.wav"
    ambient = work_dir / "ambient.wav"
    if not combined.exists() or not music.exists():
        return (gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(), gr.update(), gr.update(),
                gr.update(value=[]),
                f"Required files not found in {work_dir.name}.")

    candidates = sorted(work_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    final_vid = next(
        (p for p in candidates if not p.name.startswith("scene_") and not p.name.startswith("remixed")),
        None,
    )
    cfg = load_config()
    amb_str = str(ambient) if ambient.exists() else ""
    save_session(str(combined), str(music), amb_str,
                 cfg.get("voice_vol", 100), cfg.get("music_vol", 18), cfg.get("ambient_vol", 0))
    df_rows = _get_scene_df_rows(work_dir)
    logger.info("Remix: loaded job from %s (%d scenes)", work_dir, len(df_rows))
    return (
        gr.update(value=str(final_vid), visible=True) if final_vid else gr.update(visible=False),
        str(combined),
        str(music),
        amb_str,
        gr.update(value=cfg.get("voice_vol", 100)),
        gr.update(value=cfg.get("music_vol", 18)),
        gr.update(value=cfg.get("ambient_vol", 0)),
        gr.update(value=df_rows),
        f"Loaded: {work_dir.name}",
    )


def on_restore_session():
    session = load_session()
    if not session:
        return (gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(), gr.update(), gr.update(),
                gr.update(value=[]),
                "No saved session found.")
    # Locate the most recent final video in the same work directory
    combined = Path(session["combined"])
    work_dir = combined.parent
    candidates = sorted(work_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    final_candidates = [p for p in candidates if not p.name.startswith("scene_")
                        and not p.name.startswith("remixed")]
    final_vid = final_candidates[0] if final_candidates else None

    df_rows = _get_scene_df_rows(work_dir)
    logger.info("Restoring session from %s (%d scenes)", work_dir, len(df_rows))
    return (
        gr.update(value=str(final_vid), visible=True) if final_vid else gr.update(visible=False),
        session["combined"],          # → combined_state
        session["music"],             # → music_state
        session.get("ambient", ""),   # → ambient_state
        gr.update(value=session.get("voice_vol", 100)),    # → remix_voice_vol
        gr.update(value=session.get("music_vol", 18)),     # → remix_music_vol
        gr.update(value=session.get("ambient_vol", 0)),    # → remix_ambient_vol
        gr.update(value=df_rows),
        f"Session restored from {work_dir.name}",
    )


# ── Remix ────────────────────────────────────────────────────────────────────

def on_remix(combined_path_str: str, music_path_str: str, ambient_path_str: str,
             voice_vol_pct: float, music_vol_pct: float, ambient_vol_pct: float,
             scenes_df=None):
    import pandas as pd

    # Fall back to persisted session if Gradio state is empty (reconnect / first remix)
    if not combined_path_str or not music_path_str:
        session = load_session()
        if not session:
            raise gr.Error("No video to remix — generate a video first.")
        logger.info("Remix: restoring paths from saved session")
        combined_path_str  = session["combined"]
        music_path_str     = session["music"]
        ambient_path_str   = session.get("ambient", "")

    combined     = Path(combined_path_str)
    music_path   = Path(music_path_str)
    ambient_path = Path(ambient_path_str) if ambient_path_str and Path(ambient_path_str).exists() else None

    if not combined.exists():
        raise gr.Error(f"Source video not found: {combined}\nRe-generate the video.")
    if not music_path.exists():
        raise gr.Error(f"Music file not found: {music_path}\nRe-generate the video.")

    work_dir = combined.parent
    all_scene_finals = sorted(work_dir.glob("scene_??_final.mp4"))
    total_scenes = len(all_scene_finals)

    # Parse the scenes DataFrame → ordered list of included scene paths
    source = combined
    temp_combined: Path | None = None
    scene_subset: list[Path] | None = None

    if scenes_df is not None and total_scenes > 0:
        if isinstance(scenes_df, pd.DataFrame) and not scenes_df.empty:
            rows = scenes_df.values.tolist()
        elif isinstance(scenes_df, list) and scenes_df:
            rows = scenes_df
        else:
            rows = []

        if rows:
            # Each row: [include(bool), order(num), label(str)]
            included = []
            for row in rows:
                include = row[0]
                if include is True or include == "true" or str(include).lower() == "true":
                    try:
                        order = float(row[1])
                    except (TypeError, ValueError):
                        order = 999
                    label = str(row[2])
                    m = re.search(r'Scene\s+(\d+)', label)
                    num = int(m.group(1)) if m else 0
                    path = work_dir / f"scene_{num:02d}_final.mp4"
                    if path.exists():
                        included.append((order, path))

            included.sort(key=lambda x: x[0])
            scene_subset = [p for _, p in included]

    needs_reconcat = scene_subset is not None and (
        len(scene_subset) != total_scenes
        or [p.name for p in scene_subset] != [p.name for p in all_scene_finals]
    )

    if needs_reconcat:
        if not scene_subset:
            raise gr.Error("No valid scenes selected — select at least one scene.")
        stamp_pre = datetime.now().strftime("%Y%m%d-%H%M%S")
        temp_combined = work_dir / f"remixed_combined_{stamp_pre}.mp4"
        logger.info("Remix: re-concatenating %d/%d scenes (reordered=%s) → %s",
                    len(scene_subset), total_scenes,
                    [p.name for p in scene_subset] != [p.name for p in all_scene_finals],
                    temp_combined.name)
        concatenate_scenes(scene_subset, temp_combined)
        source = temp_combined

    # Write to a temp file first, then atomically replace the canonical final video.
    final_video = OUTPUT_DIR / f"{work_dir.name}.mp4"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_out = work_dir / f"remixed_tmp_{stamp}.mp4"
    logger.info("Remixing: voice=%.0f%% music=%.0f%% ambient=%.0f%% source=%s → %s",
                voice_vol_pct, music_vol_pct, ambient_vol_pct, source.name, final_video.name)
    try:
        mix_background_music(
            source, music_path, tmp_out,
            volume=music_vol_pct / 100.0,
            voice_volume=voice_vol_pct / 100.0,
            ambient_path=ambient_path,
            ambient_volume=ambient_vol_pct / 100.0,
        )
    except Exception as e:
        logger.exception("on_remix failed")
        first_line = str(e).split("\n")[0][:200]
        if temp_combined and temp_combined.exists():
            temp_combined.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)
        return (
            gr.update(),
            str(combined_path_str),
            str(music_path_str),
            str(ambient_path_str),
            f"❌ Remix failed: {first_line}",
        )
    if temp_combined and temp_combined.exists():
        temp_combined.unlink(missing_ok=True)
    shutil.move(str(tmp_out), str(final_video))
    size_mb = final_video.stat().st_size / 1024 / 1024
    n_included = len(scene_subset) if scene_subset is not None else total_scenes
    scene_note = f" ({n_included}/{total_scenes} scenes)" if n_included < total_scenes else ""
    logger.info("Remix done: %s (%.1f MB)%s", final_video.name, size_mb, scene_note)
    return (
        gr.update(value=str(final_video), visible=True),
        str(combined_path_str),   # → combined_state
        str(music_path_str),      # → music_state
        str(ambient_path_str),    # → ambient_state
        f"Remixed: {final_video.name} ({size_mb:.1f} MB){scene_note}",
    )


# ── LTX Upscale ──────────────────────────────────────────────────────────────

def on_load_for_upscale():
    """Find the latest final video from the saved session."""
    session = load_session()
    if not session:
        return gr.update(), "No session found — generate a video first."
    combined = Path(session["combined"])
    work_dir = combined.parent
    candidates = sorted(
        [p for p in work_dir.glob("*.mp4")
         if not p.name.startswith("scene_") and not p.name.startswith("combined")],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if candidates:
        return gr.update(value=str(candidates[0])), f"Loaded: {candidates[0].name}"
    return gr.update(), "No final video found in session."


def on_upscale(video_path_str: str):
    from pipeline.comfyui import ltx_upscale_video
    if not video_path_str:
        yield gr.update(visible=False), "❌ No video to upscale — load a video or generate one first."
        return
    input_path = Path(video_path_str)
    if not input_path.exists():
        yield gr.update(visible=False), f"❌ Video file not found: {input_path}"
        return

    cfg         = load_config()
    workers     = alive_workers(cfg.get("comfy_workers", []))
    if not workers:
        yield gr.update(visible=False), "❌ No ComfyUI workers reachable — add workers in Settings."
        return
    comfy_url   = workers[0]

    stamp       = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = OUTPUT_DIR / f"{input_path.stem}_ltx_upscale_{stamp}.mp4"
    logger.info("LTX upscale: %s → %s via %s", input_path.name, output_path.name, comfy_url)

    yield gr.update(visible=False), f"Upscaling with LTX spatial upscaler via {comfy_url}…"
    fut   = _executor.submit(ltx_upscale_video, input_path, output_path, comfy_url)
    start = time.monotonic()
    while not fut.done():
        time.sleep(5)
        elapsed = time.monotonic() - start
        yield gr.update(visible=False), f"Upscaling… {elapsed:.0f}s"
    try:
        fut.result()
    except Exception as e:
        logger.exception("LTX upscale failed")
        first_line = str(e).split("\n")[0][:300]
        yield gr.update(visible=False), f"❌ Upscale failed: {first_line}"
        return

    size_mb = output_path.stat().st_size / 1024 / 1024
    logger.info("LTX upscale done: %s (%.1f MB)", output_path.name, size_mb)
    yield gr.update(value=str(output_path), visible=True), f"Done: {output_path.name} ({size_mb:.1f} MB)"


# ── Config management ────────────────────────────────────────────────────────

def on_add_voice(name: str, file_path: str):
    name = name.strip()
    if not name:
        raise gr.Error("Voice name cannot be empty.")
    if not file_path or not Path(file_path).exists():
        raise gr.Error("Please upload a valid WAV or MP3 file.")

    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9_-]", "_", name.lower())
    dest = VOICES_DIR / f"{safe}{Path(file_path).suffix}"
    shutil.copy2(file_path, dest)

    cfg    = load_config()
    voices = [v for v in cfg.get("voices", []) if v["name"] != name]
    voices.append({"name": name, "path": str(dest)})
    cfg["voices"] = voices
    save_config(cfg)
    logger.info("Added voice: %s → %s", name, dest)

    choices = get_voice_choices()
    rows    = [[v["name"], v["path"]] for v in cfg["voices"]]
    return (
        gr.update(value=rows),
        gr.update(choices=choices),
        gr.update(choices=choices[1:], value=None),
        "",
        None,
        "Voice added ✓",
    )


def on_remove_voice(name: str):
    if not name:
        raise gr.Error("Select a voice to remove.")
    cfg    = load_config()
    cfg["voices"] = [v for v in cfg.get("voices", []) if v["name"] != name]
    save_config(cfg)
    choices = get_voice_choices()
    rows    = [[v["name"], v["path"]] for v in cfg["voices"]]
    return (
        gr.update(value=rows),
        gr.update(choices=choices),
        gr.update(choices=choices[1:], value=None),
        f"Removed '{name}' ✓",
    )


def on_voices_load():
    """Refresh voice components from disk on each page load."""
    choices = get_voice_choices()
    rows    = voices_as_rows()
    return gr.update(value=rows), gr.update(choices=choices), gr.update(choices=choices[1:])


def on_save_config(music_vol: float, voice_vol: float, ambient_vol: float,
                   resolution: str, max_clip_secs: float, lora_strength: float,
                   first_pass_cfg: float, first_pass_steps: int,
                   second_pass_cfg: float, second_pass_steps: int,
                   workers_text: str, tts_workers_text: str,
                   llm_backend: str,
                   local_llm_url: str, local_llm_model: str,
                   claude_api_key: str, claude_model: str,
                   flux_model: str, flux_vae: str,
                   flux_clip_t5: str, flux_clip_l: str,
                   flux_steps: int,
                   default_visual_style: str = "",
                   default_voice: str = "",
                   default_n_scenes: int = 20,
                   youtube_client_secrets: str = "",
                   youtube_auto_fetch_evaluate: bool = False,
                   youtube_auto_approve_comments: bool = False,
                   youtube_auto_start_job: bool = False,
                   youtube_auto_approve_script: bool = False,
                   youtube_auto_post: bool = False,
                   youtube_fully_automated: bool = False,
                   youtube_post_privacy: str = "private",
                   youtube_post_category: str = "People & Blogs",
                   script_extra_instructions: str = "",
                   description_suffix: str = ""):
    cfg = load_config()
    cfg["music_vol"]          = int(music_vol)
    cfg["voice_vol"]          = int(voice_vol)
    cfg["ambient_vol"]        = int(ambient_vol)
    cfg["resolution"]         = resolution
    cfg["max_clip_secs"]      = float(max_clip_secs)
    cfg["lora_strength"]      = float(lora_strength)
    cfg["first_pass_cfg"]     = float(first_pass_cfg)
    cfg["first_pass_steps"]   = int(first_pass_steps)
    cfg["second_pass_cfg"]    = float(second_pass_cfg)
    cfg["second_pass_steps"]  = int(second_pass_steps)
    workers = [u.strip() for u in workers_text.splitlines() if u.strip()]
    cfg["comfy_workers"]      = workers
    tts_workers = [h.strip() for h in tts_workers_text.splitlines() if h.strip()]
    cfg["tts_workers"]        = tts_workers
    cfg["llm_backend"]        = llm_backend
    cfg["local_llm_url"]      = local_llm_url.strip() or "http://localhost:8000/v1/chat/completions"
    cfg["local_llm_model"]    = local_llm_model.strip() or "openai/gpt-oss-120b"
    cfg["claude_api_key"]     = claude_api_key.strip()
    cfg["claude_model"]       = claude_model.strip() or "claude-sonnet-4-6"
    cfg["flux_model"]         = flux_model.strip() or "flux1-schnell-fp8.safetensors"
    cfg["flux_vae"]           = flux_vae.strip() or "ae.safetensors"
    cfg["flux_clip_t5"]       = flux_clip_t5.strip() or "t5xxl_fp8_e4m3fn.safetensors"
    cfg["flux_clip_l"]        = flux_clip_l.strip() or "clip_l.safetensors"
    cfg["flux_steps"]         = int(flux_steps)
    cfg["default_visual_style"]            = (default_visual_style or "").strip()
    cfg["default_voice"]                   = "" if (default_voice or "").strip() == F5TTS_DEFAULT_OPTION else (default_voice or "").strip()
    cfg["default_n_scenes"]               = max(1, int(default_n_scenes))
    cfg["youtube_client_secrets"]          = (youtube_client_secrets or "").strip()
    cfg["youtube_auto_fetch_evaluate"]     = bool(youtube_auto_fetch_evaluate)
    cfg["youtube_auto_approve_comments"]   = bool(youtube_auto_approve_comments)
    cfg["youtube_auto_start_job"]          = bool(youtube_auto_start_job)
    cfg["youtube_auto_approve_script"]     = bool(youtube_auto_approve_script)
    cfg["youtube_auto_post"]               = bool(youtube_auto_post)
    cfg["youtube_fully_automated"]         = bool(youtube_fully_automated)
    cfg["youtube_post_privacy"]            = youtube_post_privacy or "private"
    # Store category as ID for API use
    cfg["youtube_post_category"] = yt.CATEGORY_OPTIONS.get(youtube_post_category, youtube_post_category) or "22"
    cfg["script_extra_instructions"] = (script_extra_instructions or "").strip()
    cfg["description_suffix"]        = (description_suffix or "").strip()
    save_config(cfg)
    logger.info("Config saved: lora=%.2f workers=%s tts=%s",
                lora_strength, cfg["comfy_workers"], cfg["tts_workers"])
    status = f"Settings saved ✓  ({len(cfg['comfy_workers'])} video, {len(cfg['tts_workers'])} TTS worker(s))"
    voice_val = cfg["default_voice"] or F5TTS_DEFAULT_OPTION
    voice_choices = get_voice_choices()
    return status, gr.update(value=voice_val, choices=voice_choices), gr.update(value=cfg["default_n_scenes"])


# ── YouTube tab handlers ─────────────────────────────────────────────────────

def _yt_auth_html(status: dict) -> str:
    if status["connected"]:
        name = html.escape(status["channel_name"])
        return (
            f'<div style="padding:6px 8px;border-radius:6px;background:#dcfce7;'
            f'border:1px solid #86efac;font-size:13px;color:#15803d">'
            f'Connected to <strong>{name}</strong></div>'
        )
    err = html.escape(status["error"] or "Not connected")
    return (
        f'<div style="padding:6px 8px;border-radius:6px;background:#fef2f2;'
        f'border:1px solid #fca5a5;font-size:13px;color:#dc2626">{err}</div>'
    )


def _comments_html(cache: list[dict]) -> str:
    if not cache:
        return '<div style="color:#6b7280;font-size:13px;padding:8px 0">No comments fetched yet. Click Fetch Comments.</div>'
    parts = []
    for i, c in enumerate(cache):
        is_req = c.get("is_request")
        status = c.get("status", "new")
        evaluated = c.get("evaluated", False)

        if is_req is True and status not in ("rejected",):
            border = "#22c55e"
            badge_bg = "#dcfce7"
            badge_color = "#15803d"
            badge = "Video Request"
        elif is_req is False:
            border = "#d1d5db"
            badge_bg = "#f3f4f6"
            badge_color = "#6b7280"
            badge = "Not a Request"
        else:
            border = "#fde68a"
            badge_bg = "#fef9c3"
            badge_color = "#a16207"
            badge = "Pending Evaluation"

        if status == "rejected":
            border = "#e5e7eb"
            badge_bg = "#f3f4f6"
            badge_color = "#9ca3af"
            badge = "Rejected"

        commenter = html.escape(c.get("commenter", "Unknown"))
        text = html.escape((c.get("text", "") or "")[:200])
        if len(c.get("text", "")) > 200:
            text += "…"
        suggested = html.escape(c.get("suggested_title", "") or "")
        confidence = c.get("confidence", 0.0)
        conf_str = f" ({confidence:.0%})" if evaluated and is_req else ""

        suggested_html = (
            f'<div style="font-size:11px;color:#374151;margin-top:2px">'
            f'Suggested title: <em>{suggested}</em>{conf_str}</div>'
            if suggested else ""
        )
        reason = html.escape((c.get("reason", "") or "")[:120])
        reason_html = (
            f'<div style="font-size:11px;color:#6b7280;margin-top:2px">{reason}</div>'
            if reason and evaluated else ""
        )

        parts.append(
            f'<div style="border-left:4px solid {border};padding:8px 12px;margin-bottom:8px;'
            f'background:#fafafa;border-radius:0 6px 6px 0">'
            f'<div style="display:flex;justify-content:space-between;align-items:center">'
            f'<span style="font-size:12px;font-weight:600;color:#374151">'
            f'<span style="color:#6b7280;margin-right:6px">#{len(cache) - i}</span>{commenter}</span>'
            f'<span style="font-size:11px;padding:2px 8px;border-radius:9999px;'
            f'background:{badge_bg};color:{badge_color}">{badge}</span>'
            f'</div>'
            f'<div style="font-size:13px;color:#374151;margin-top:4px">{text}</div>'
            f'{suggested_html}{reason_html}'
            f'</div>'
        )
    return (
        '<div style="max-height:400px;overflow-y:auto;padding-right:4px">'
        + "".join(parts)
        + "</div>"
    )


def _queue_html(queue: list[dict]) -> str:
    if not queue:
        return '<div style="color:#6b7280;font-size:13px;padding:4px 0">Queue is empty.</div>'
    _STATUS_COLOR = {
        "pending": "#9333ea",
        "creating": "#0ea5e9",
        "done": "#22c55e",
        "upload_pending": "#f59e0b",
        "posted": "#16a34a",
    }
    _SOURCE_BADGE = {
        "comment": '<span style="font-size:10px;margin-left:6px;padding:1px 5px;border-radius:9999px;background:#dbeafe;color:#1d4ed8">💬 request</span>',
        "suggestion": '<span style="font-size:10px;margin-left:6px;padding:1px 5px;border-radius:9999px;background:#f3f4f6;color:#374151">🤖 suggestion</span>',
        "manual": '<span style="font-size:10px;margin-left:6px;padding:1px 5px;border-radius:9999px;background:#fef9c3;color:#854d0e">✏️ manual</span>',
    }
    rows = []
    removable_idx = 0  # 1-based counter for non-posted items (matches Queue # input)
    for item in queue:
        title = html.escape(item.get("final_title", "") or "")
        commenter = html.escape(item.get("commenter", "") or "")
        status = item.get("status", "pending")
        source = item.get("source", "")
        color = _STATUS_COLOR.get(status, "#6b7280")
        yt_url = item.get("youtube_url", "")
        url_html = (
            f' — <a href="{html.escape(yt_url)}" target="_blank" '
            f'style="color:#2563eb">View on YouTube</a>'
            if yt_url else ""
        )
        source_badge = _SOURCE_BADGE.get(source, "")
        if status != "posted":
            removable_idx += 1
            num_label = f'<span style="color:#6b7280;margin-right:6px">#{removable_idx}</span>'
        else:
            num_label = '<span style="color:#d1d5db;margin-right:6px">—</span>'
        rows.append(
            f'<div style="padding:6px 8px;border-bottom:1px solid #e5e7eb;font-size:13px">'
            f'{num_label}'
            f'<strong>{title}</strong>'
            f'<span style="font-size:11px;margin-left:8px;padding:1px 6px;border-radius:9999px;'
            f'background:{color}22;color:{color}">{status}</span>'
            f'{source_badge}'
            f'<span style="color:#9ca3af;font-size:11px;margin-left:6px">from {commenter}</span>'
            f'{url_html}'
            f'</div>'
        )
    return (
        '<div style="border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;max-height:240px;overflow-y:auto">'
        + "".join(rows)
        + "</div>"
    )


def _pending_requests_html(cache: list[dict]) -> str:
    """Numbered list of video requests sorted by interestingness (highest first)."""
    pending = [
        c for c in cache
        if c.get("is_request") and c.get("status") not in ("approved", "rejected")
    ]
    if not pending:
        return '<div style="color:#6b7280;font-size:13px;padding:4px 0">No pending requests. Fetch and evaluate comments first.</div>'
    # Sort: highest interestingness first, then confidence as tiebreaker
    pending = sorted(
        pending,
        key=lambda c: (c.get("interestingness", 0.0), c.get("confidence", 0.0)),
        reverse=True,
    )
    rows = []
    for i, c in enumerate(pending):
        title = html.escape(c.get("suggested_title", "") or "")
        commenter = html.escape(c.get("commenter", "") or "")
        conf = c.get("confidence", 0)
        interest = c.get("interestingness", 0.0)
        interest_color = "#15803d" if interest >= 0.7 else ("#92400e" if interest >= 0.5 else "#6b7280")
        rows.append(
            f'<div style="padding:5px 8px;border-bottom:1px solid #e5e7eb;font-size:13px">'
            f'<span style="color:#6b7280;margin-right:6px">#{i+1}</span>'
            f'<strong>{title}</strong>'
            f'<span style="color:#9ca3af;font-size:11px;margin-left:6px">from {commenter} · {conf:.0%} confidence</span>'
            f'<span style="font-size:11px;margin-left:8px;color:{interest_color}">★ {interest:.0%} interest</span>'
            f'</div>'
        )
    return (
        '<div style="border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;max-height:180px;overflow-y:auto">'
        + "".join(rows)
        + "</div>"
    )


def on_yt_check_status() -> str:
    cfg = load_config()
    status = yt.check_auth_status(cfg.get("youtube_client_secrets", ""))
    return _yt_auth_html(status)


def on_yt_connect() -> tuple:
    cfg = load_config()
    msg = yt.start_auth_flow(cfg.get("youtube_client_secrets", ""))
    status_html = (
        f'<div style="padding:6px 8px;border-radius:6px;background:#fef3c7;'
        f'border:1px solid #fcd34d;font-size:13px;color:#92400e">{html.escape(msg)}</div>'
    )
    return status_html, gr.update(active=True)


def on_yt_poll_auth() -> tuple:
    result = yt.poll_auth_flow()
    if result["running"]:
        status_html = (
            '<div style="padding:6px 8px;border-radius:6px;background:#fef3c7;'
            'border:1px solid #fcd34d;font-size:13px;color:#92400e">'
            'Waiting for browser authorization…</div>'
        )
        return status_html, gr.update(active=True)
    # Auth flow complete — force a fresh API check to get channel name
    cfg = load_config()
    auth_status = yt.check_auth_status(cfg.get("youtube_client_secrets", ""), force=True)
    return _yt_auth_html(auth_status), gr.update(active=False)


def on_yt_disconnect() -> str:
    yt.disconnect_youtube()
    return _yt_auth_html({"connected": False, "channel_name": "", "error": "Disconnected."})


def _yt_refresh_outputs(cache: list[dict], queue: list[dict], status: str) -> tuple:
    return _comments_html(cache), _pending_requests_html(cache), _queue_html(queue), status


def _yt_fetch_new_comments(cfg: dict) -> tuple[list, int]:
    """Fetch comments from YouTube and merge into cache. Returns (cache, new_count)."""
    secrets = cfg.get("youtube_client_secrets", "")
    fetched = yt.fetch_channel_comments(secrets, max_results=50)
    cache = yt.load_comments_cache()
    existing_ids = {c["comment_id"] for c in cache}
    new_count = 0
    for c in fetched:
        if c["comment_id"] not in existing_ids:
            cache.insert(0, {
                **c,
                "evaluated": False,
                "is_request": None,
                "suggested_title": "",
                "confidence": 0.0,
                "interestingness": 0.0,
                "reason": "",
                "status": "new",
            })
            new_count += 1
    yt.save_comments_cache(cache)
    return cache, new_count


def _yt_evaluate_unevaluated(cache: list, cfg: dict, auto_approve: bool) -> tuple[list, str]:
    """Evaluate unevaluated comments, auto-approve if requested. Returns (updated_cache, status_msg)."""
    secrets = cfg.get("youtube_client_secrets", "")
    auto_threshold = 0.7
    unevaluated = [c for c in cache if not c.get("evaluated")]
    if not unevaluated:
        return cache, "All comments already evaluated."

    auto_approved_count = 0
    thank_replied_count = 0
    for comment in unevaluated:
        result = yt.evaluate_comment(comment.get("text", ""), comment.get("commenter", ""), cfg)
        comment.update({
            "evaluated": True,
            "is_request": result["is_request"],
            "suggested_title": result["suggested_title"],
            "confidence": result["confidence"],
            "interestingness": result.get("interestingness", 0.0),
            "reason": result["reason"],
            "status": "evaluated" if comment.get("status") == "new" else comment.get("status"),
        })
        # Auto-reply "Thanks for the suggestion!" to new requests (once per comment)
        if result["is_request"] and not comment.get("thanked"):
            reply = yt.reply_to_comment(
                secrets,
                comment.get("comment_id", ""),
                "Thanks for the suggestion! We'll look into making a video about this. 🎬",
            )
            if reply.get("success"):
                comment["thanked"] = True
                thank_replied_count += 1
        if (auto_approve and result["is_request"]
                and result["confidence"] >= auto_threshold
                and comment.get("status") not in ("approved", "rejected")):
            comment["status"] = "approved"
            queue_item = yt.add_to_queue(comment, result["suggested_title"], source="comment")
            if queue_item:
                threading.Thread(
                    target=_prefetch_video_prompt,
                    args=(queue_item["id"], result["suggested_title"], comment.get("text", "")),
                    daemon=True,
                ).start()
            auto_approved_count += 1

    yt.save_comments_cache(cache)
    msg = f"Evaluated {len(unevaluated)} comment(s)."
    if thank_replied_count:
        msg += f" Thanked {thank_replied_count} requester(s)."
    if auto_approved_count:
        msg += f" Auto-approved {auto_approved_count} request(s)."
    return cache, msg


def on_yt_fetch_and_evaluate(auto_approve: bool) -> tuple:
    """Fetch new comments + evaluate all unevaluated ones in one action.
    Returns 5-tuple: (comments_html, pending_html, queue_html, status, auto_start_trigger).
    """
    cfg = load_config()
    try:
        cache, new_count = _yt_fetch_new_comments(cfg)
        fetch_msg = f"Fetched {new_count} new comment(s). "
    except Exception as exc:
        logger.warning("Fetch comments failed: %s", exc)
        cache = yt.load_comments_cache()
        fetch_msg = f"Fetch error: {str(exc)[:120]}. "

    cache, eval_msg = _yt_evaluate_unevaluated(cache, cfg, auto_approve)
    queue = yt.load_queue()
    msg = fetch_msg + eval_msg

    # Determine auto-start trigger
    trigger = None
    if cfg.get("youtube_auto_start_job", False) and not _is_job_running():
        trigger = _best_pending_queue_item()
        if trigger:
            msg += f" Auto-starting: {trigger.get('final_title', '')}."
        else:
            # No pending user requests — fall back to AI-generated suggestions
            suggestion_item = _auto_pick_suggestion(cfg)
            if suggestion_item:
                trigger = suggestion_item
                msg += f" No requests — auto-starting from AI suggestion: {trigger.get('final_title', '')}."

    return _yt_refresh_outputs(cache, queue, msg) + (trigger,)


def _prefetch_video_prompt(queue_item_id: str, title: str, comment_text: str) -> None:
    """Background thread: generate directorial brief and store in queue item."""
    try:
        prompt = generate_video_prompt(title, comment_text)  # comment_text now ignored by LLM
        if prompt:
            yt.update_queue_item(queue_item_id, video_prompt=prompt)
    except Exception as exc:
        logger.warning("Background prompt generation failed for %s: %s", queue_item_id, exc)


def on_yt_approve(row_idx: int, title_override: str) -> tuple:
    """Approve a pending request. Returns 5-tuple including auto_start_trigger."""
    cfg = load_config()
    cache = yt.load_comments_cache()
    # Sort pending requests the same way the UI does (interestingness DESC)
    requests = sorted(
        [c for c in cache if c.get("is_request") and c.get("status") not in ("approved", "rejected")],
        key=lambda c: (c.get("interestingness", 0.0), c.get("confidence", 0.0)),
        reverse=True,
    )
    idx = int(row_idx or 1) - 1
    if idx < 0 or idx >= len(requests):
        queue = yt.load_queue()
        return _yt_refresh_outputs(cache, queue, f"Row {row_idx} not found in pending requests.") + (None,)
    comment = requests[idx]
    final_title = (title_override or "").strip() or comment.get("suggested_title", "")
    comment["status"] = "approved"
    yt.save_comments_cache(cache)
    queue_item = yt.add_to_queue(comment, final_title, source="comment")

    # Generate the directorial brief synchronously so it's ready before navigating
    # to the Create tab.  Previous approach used a background thread which meant
    # the queue_item had no video_prompt yet when on_yt_approve returned.
    video_brief = generate_video_prompt(final_title, comment.get("text", ""))
    if video_brief:
        yt.update_queue_item(queue_item["id"], video_prompt=video_brief)

    queue = yt.load_queue()
    msg = f"Approved: {html.escape(final_title)}"

    # Auto-start trigger
    trigger = None
    if cfg.get("youtube_auto_start_job", False) and not _is_job_running():
        trigger = _best_pending_queue_item()
        if trigger:
            msg += " — auto-starting job."

    return _yt_refresh_outputs(cache, queue, msg) + (trigger,)


def on_yt_reject(row_idx: int) -> tuple:
    cache = yt.load_comments_cache()
    requests = [c for c in cache if c.get("is_request") and c.get("status") not in ("approved", "rejected")]
    idx = int(row_idx or 1) - 1
    if idx < 0 or idx >= len(requests):
        queue = yt.load_queue()
        return _yt_refresh_outputs(cache, queue, f"Row {row_idx} not found in pending requests.")
    comment = requests[idx]
    comment["status"] = "rejected"
    yt.save_comments_cache(cache)
    queue = yt.load_queue()
    return _yt_refresh_outputs(cache, queue, f"Rejected comment from {html.escape(comment.get('commenter', ''))}")


def _resolution_for_scene_count(n_scenes: int) -> str:
    """Return the appropriate resolution key based on scene count tier.
    Short  (6–11):  Portrait FHD  — vertical format for brief focused content.
    Medium (12–39): Landscape FHD — standard documentary format.
    Large  (40–50): Landscape FHD — epic multi-scene format.
    """
    if n_scenes < 12:
        return "Portrait FHD (1080×1920)"
    return "Landscape FHD (1920×1080)"


def _load_queue_item_into_create(item: dict) -> tuple:
    """Return Create-tab navigation tuple for a queue item.
    Outputs: (tabs, video_title_in, title_in, yt_action_status,
              style_box, style_state, n_scenes_in, voice_dropdown, resolution_in)
    """
    cfg = load_config()
    title = item.get("final_title", "")
    video_prompt = item.get("video_prompt") or ""
    default_style = cfg.get("default_visual_style", "")
    n_scenes = max(6, item.get("suggested_scene_count") or cfg.get("default_n_scenes", 20))
    voice = cfg.get("default_voice") or F5TTS_DEFAULT_OPTION
    resolution = _resolution_for_scene_count(n_scenes)
    return (
        gr.update(selected="create"),
        gr.update(value=title),
        gr.update(value=video_prompt),
        f"Loaded: {html.escape(title)} ({n_scenes} scenes)",
        gr.update(value=default_style),
        default_style,
        gr.update(value=int(n_scenes)),
        gr.update(value=voice, choices=get_voice_choices()),
        gr.update(value=resolution),
    )


def on_yt_start_next_video() -> dict | None:
    """Trigger the next pending queue item through the full auto-start chain.

    Returns a queue-item dict (to set yt_auto_start_trigger) or None if there
    is nothing to start or a job is already running.
    """
    if _is_job_running():
        logger.info("on_yt_start_next_video: job already running, skipping")
        return None
    item = _best_pending_queue_item()
    if not item:
        # No pending user requests — fall back to AI suggestions
        cfg = load_config()
        item = _auto_pick_suggestion(cfg)
    return item  # None or a queue item dict → drives yt_auto_start_trigger


def on_yt_manual_add_to_queue(title: str, prompt: str, n_scenes: int) -> tuple:
    """Add a manually specified video to the queue. Returns (queue_html, status, title_clear, prompt_clear)."""
    import uuid as _uuid
    title = (title or "").strip()
    if not title:
        queue = yt.load_queue()
        return _queue_html(queue), "Title is required.", gr.update(), gr.update()
    n_scenes = max(6, min(50, int(n_scenes or 20)))
    prompt = (prompt or "").strip()
    queue = yt.load_queue()
    if any(q.get("final_title", "").lower() == title.lower()
           and q.get("status") not in ("posted",) for q in queue):
        return _queue_html(queue), f"'{title}' is already in the queue.", gr.update(), gr.update()
    entry = {
        "id": _uuid.uuid4().hex[:8],
        "comment_id": "",
        "video_id": "",
        "commenter": "Manual",
        "comment_text": "",
        "final_title": title,
        "video_prompt": prompt,
        "suggested_scene_count": n_scenes,
        "interestingness": 0.95,
        "source": "manual",
        "status": "pending",
        "created_at": time.time(),
        "video_job_id": None,
        "youtube_video_id": None,
        "youtube_url": None,
    }
    queue.insert(0, entry)
    yt.save_queue(queue)
    return _queue_html(queue), f"Added to queue: {title}", gr.update(value=""), gr.update(value="")


def on_yt_remove_from_queue(row_idx: int) -> tuple:
    """Remove a queue item by position (1-based). Returns updated queue HTML + status."""
    queue = yt.load_queue()
    # Only non-posted items are numbered in the UI
    removable = [q for q in queue if q.get("status") not in ("posted",)]
    idx = int(row_idx or 1) - 1
    if idx < 0 or idx >= len(removable):
        return _queue_html(queue), f"Row {row_idx} not found in queue."
    item = removable[idx]
    title = item.get("final_title", "")
    # Remove from queue list and save
    queue = [q for q in queue if q.get("id") != item.get("id")]
    yt.save_queue(queue)
    # Also revert the comment status so it can be re-approved later if needed
    cache = yt.load_comments_cache()
    for c in cache:
        if c.get("comment_id") == item.get("comment_id"):
            c["status"] = "evaluated"
            break
    yt.save_comments_cache(cache)
    return _queue_html(queue), f"Removed from queue: {html.escape(title)}"


def on_yt_move_in_queue(row_idx: int, direction: int) -> tuple:
    """Move a pending queue item up (direction=-1) or down (direction=1). row_idx is 1-based."""
    queue = yt.load_queue()
    removable = [q for q in queue if q.get("status") not in ("posted",)]
    idx = int(row_idx or 1) - 1
    if idx < 0 or idx >= len(removable):
        return _queue_html(queue), f"Row {row_idx} not found in queue."
    item = removable[idx]
    if item.get("status") != "pending":
        return _queue_html(queue), "Only pending items can be reordered."
    moved = yt.move_queue_item(item["id"], direction)
    queue = yt.load_queue()
    if moved:
        label = "up" if direction == -1 else "down"
        return _queue_html(queue), f"Moved #{row_idx} {label}."
    return _queue_html(queue), "Cannot move — already at the boundary."


def on_yt_sort_queue() -> tuple:
    """Sort the queue so comment requests come first (oldest first), then suggestions."""
    queue = yt.load_queue()
    sorted_q = yt.sort_queue_by_priority(queue)
    yt.save_queue(sorted_q)
    return _queue_html(sorted_q), "Queue sorted: comment requests first, then suggestions."


def _prepare_auto_start(queue_item: dict | None) -> tuple:
    """Populate Create-tab fields from a queue item, forcing auto-approve=True.
    Returns (video_title_in, title_in, n_scenes_in, auto_approve_in, resolution_in) updates.
    """
    if not queue_item:
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
    # Guard against duplicate chains (e.g. demo.load startup fetch + button
    # click firing simultaneously).  The first caller sets the event; any
    # second concurrent call sees it already set and bails out as a no-op.
    if _auto_start_in_progress.is_set():
        logger.info("_prepare_auto_start: another chain already in progress, skipping")
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
    _auto_start_in_progress.set()
    title = queue_item.get("final_title", "")
    prompt = queue_item.get("video_prompt") or ""  # never use raw comment text
    n_scenes = max(6, queue_item.get("suggested_scene_count") or load_config().get("default_n_scenes", 20))
    resolution = _resolution_for_scene_count(n_scenes)
    logger.info("Auto-starting job: %r (%d scenes, %s)", title, n_scenes, resolution)
    return (
        gr.update(value=title),
        gr.update(value=prompt),
        gr.update(value=int(n_scenes)),
        gr.update(value=True),  # force auto-approve so the pipeline runs unattended
        gr.update(value=resolution),
    )


# ── Post tab handlers ─────────────────────────────────────────────────────────

def _list_completed_jobs() -> list[tuple[str, str]]:
    """Return (label, work_dir_str) pairs for all job dirs that have a final mp4.

    Labels include a status icon:
      🎬  ready to post  (done, no youtube_video_id)
      ⏳  upload pending (upload_pending queue status or _auto_post_paused_until set)
      ✅  already posted (has youtube_video_id)
    Only jobs whose mp4 still exists on disk are included.
    Sorted: ready-to-post first, then newest-by-mtime.
    """
    import datetime as _dt
    jobs = []
    try:
        for job_dir in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not job_dir.is_dir():
                continue
            final_path = _final_path_for_work_dir(job_dir)
            if not final_path or not final_path.exists() or final_path.stat().st_size < 10_000:
                continue

            # Read title from job_config.json
            job_cfg_path = job_dir / "job_config.json"
            title = job_dir.name
            if job_cfg_path.exists():
                try:
                    cfg_data = _read_json(job_cfg_path)
                    title = cfg_data.get("video_title", "") or cfg_data.get("title", "") or title
                except Exception:
                    pass

            # Read upload/post status from job.json
            yt_id = ""
            job_status = ""
            paused = False
            try:
                meta = _read_json(_job_meta_path(job_dir))
                yt_id = meta.get("youtube_video_id", "") or ""
                job_status = meta.get("status", "")
                paused_until = float(meta.get("_auto_post_paused_until") or 0)
                paused = bool(paused_until and time.time() < paused_until)
            except Exception:
                pass

            # Skip cancelled/error jobs
            if job_status in ("cancelled", "error"):
                continue

            # Determine status icon
            if yt_id:
                icon = "✅"
                sort_key = 2
            elif paused or job_status == "upload_pending":
                icon = "⏳"
                sort_key = 1
            else:
                icon = "🎬"
                sort_key = 0

            # Size and date
            try:
                size_mb = round(final_path.stat().st_size / 1_048_576)
                mtime = job_dir.stat().st_mtime
                date_str = _dt.datetime.fromtimestamp(mtime).strftime("%b %d %H:%M")
            except Exception:
                size_mb = 0
                date_str = ""

            label = f"{icon} {title}  ({date_str}, {size_mb} MB)"
            jobs.append((sort_key, job_dir.stat().st_mtime, label, str(job_dir)))
    except Exception:
        pass

    # Sort: ready first (sort_key 0), then upload-pending (1), then posted (2);
    # within each group newest first.
    jobs.sort(key=lambda x: (x[0], -x[1]))
    return [(label, wdir) for _, _, label, wdir in jobs]


def _post_status_html(msg: str, kind: str = "info") -> str:
    colors = {
        "info":    ("#eff6ff", "#bfdbfe", "#1d4ed8"),
        "success": ("#dcfce7", "#86efac", "#15803d"),
        "error":   ("#fef2f2", "#fca5a5", "#dc2626"),
        "working": ("#fef3c7", "#fcd34d", "#92400e"),
    }
    bg, border, text = colors.get(kind, colors["info"])
    return (
        f'<div style="padding:8px 12px;border-radius:6px;background:{bg};'
        f'border:1px solid {border};font-size:13px;color:{text}">'
        f'{html.escape(msg)}</div>'
    )


def on_post_load_from_selection(selected_dir: str):
    """Load Post tab fields from a specific work_dir chosen in the dropdown."""
    if not selected_dir:
        yield from on_post_load("")
        return
    yield from _post_load_work_dir(Path(selected_dir))


def on_post_load(active_job_dir: str):
    """Load Post tab fields from the active job.

    Generator: yields immediately with video/title/cover, then a second yield
    adds the LLM-generated description so the cover is never blocked by the LLM.
    """
    work_dir = _preferred_work_dir(active_job_dir)
    yield from _post_load_work_dir(work_dir)


def _post_load_work_dir(work_dir):
    if work_dir is None:
        yield (
            gr.update(value=""),
            gr.update(value="No active job — generate a video first."),
            gr.update(value=""),
            gr.update(value=None, visible=False),
            _post_status_html("No active job found. Generate a video first.", "info"),
            gr.update(value=""),
            "",  # post_cover_path_state
        )
        return
    try:
        final_path = _final_path_for_work_dir(work_dir)
        video_path = str(final_path) if final_path.exists() else ""

        job_cfg_path = work_dir / "job_config.json"
        job_cfg = {}
        if job_cfg_path.exists():
            try:
                job_cfg = _read_json(job_cfg_path)
            except Exception:
                pass
        video_title = job_cfg.get("video_title", "") or job_cfg.get("title", "") or work_dir.name
        style = job_cfg.get("style", "")
        music_desc = job_cfg.get("music_desc", "")

        cover_path = work_dir / "cover.png"
        cover_str = str(cover_path) if cover_path.exists() and cover_path.stat().st_size > 1000 else ""
        cover_update = (
            gr.update(value=cover_str, visible=True)
            if cover_str
            else gr.update(value=None, visible=False)
        )

        status_msg = f"Loaded from {work_dir.name}" + ("" if video_path else " (video not ready yet)")

        # First yield — immediately shows video path, title, cover.
        yield (
            gr.update(value=video_path),
            gr.update(value=video_title),
            gr.update(value=""),
            cover_update,
            _post_status_html(status_msg + " — generating description…", "success" if video_path else "info"),
            gr.update(value=""),
            cover_str,
        )

        # Second yield — LLM description (may take a few seconds but cover is already visible).
        description = ""
        store = DurableStore.default()
        try:
            job = store.get_job_by_work_dir(str(work_dir))
            if job:
                scenes = store.scene_rows(job["id"])
                if scenes:
                    description = generate_youtube_description(
                        title=video_title,
                        scenes=scenes,
                        style=style,
                        music_desc=music_desc,
                    )
        finally:
            store.close()
        suffix = load_config().get("description_suffix", "").strip()
        if suffix and description:
            description = f"{description}\n\n{suffix}"

        yield (
            gr.update(),
            gr.update(),
            gr.update(value=description),
            gr.update(),
            _post_status_html(status_msg, "success" if video_path else "info"),
            gr.update(),
            gr.update(),
        )

    except Exception as exc:
        logger.warning("on_post_load failed: %s", exc)
        yield (
            gr.update(value=""),
            gr.update(value=""),
            gr.update(value=""),
            gr.update(value=None, visible=False),
            _post_status_html(f"Error loading job: {str(exc)[:200]}", "error"),
            gr.update(value=""),
            "",  # post_cover_path_state
        )


def _post_tab_work_dir(post_video_path: str, active_job_dir: str) -> Path | None:
    """Derive the work_dir for the video CURRENTLY displayed on the Post tab.

    Prefers the post tab's video path (what the user actually sees), and only
    falls back to active_job_state when the post tab has no video loaded.
    """
    if post_video_path:
        p = Path(post_video_path).expanduser()
        if p.exists():
            return p.parent
        if p.parent.exists():
            return p.parent
    return _preferred_work_dir(active_job_dir)


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


def on_post_regen_description(post_video_path: str, active_job_dir: str, video_title: str) -> tuple:
    work_dir = _post_tab_work_dir(post_video_path, active_job_dir)
    if work_dir is None:
        return gr.update(), _post_status_html("No video selected.", "info")
    try:
        job_cfg_path = work_dir / "job_config.json"
        job_cfg = _read_json(job_cfg_path) if job_cfg_path.exists() else {}
        style = job_cfg.get("style", "")
        music_desc = job_cfg.get("music_desc", "")
        store = DurableStore.default()
        try:
            job = store.get_job_by_work_dir(str(work_dir))
            scenes = store.scene_rows(job["id"]) if job else []
        finally:
            store.close()
        description = generate_youtube_description(
            title=video_title or job_cfg.get("video_title", ""),
            scenes=scenes,
            style=style,
            music_desc=music_desc,
        )
        suffix = load_config().get("description_suffix", "").strip()
        if suffix and description:
            description = f"{description}\n\n{suffix}"
        return gr.update(value=description), _post_status_html("Description regenerated.", "success")
    except Exception as exc:
        return gr.update(), _post_status_html(f"Error: {str(exc)[:200]}", "error")


def _notify_comment_requester(secrets: str, active_job_dir: str, video_title: str, yt_url: str) -> None:
    """If this video was generated from a comment request, reply to that comment with the video link.

    Identification is STRICT — only uses the stable queue_item_id written into
    job_config.json at job-start time. No title-based fallback: replying to the
    wrong commenter (because of a coincidental title match or any cached state)
    is a worse outcome than not replying at all.
    """
    try:
        work_dir = _preferred_work_dir(active_job_dir)
        if not work_dir:
            return
        queue = yt.load_queue()

        queue_item_id = ""
        job_cfg_path = work_dir / "job_config.json"
        if job_cfg_path.exists():
            try:
                job_cfg_data = _read_json(job_cfg_path)
                queue_item_id = job_cfg_data.get("queue_item_id", "")
            except Exception:
                pass

        if not queue_item_id:
            logger.info("_notify_comment_requester: no queue_item_id in %s/job_config.json — "
                        "skipping comment reply (no safe way to identify originating comment)",
                        work_dir.name)
            return

        match = next((q for q in queue if q.get("id") == queue_item_id), None)
        if not match:
            logger.warning("_notify_comment_requester: queue_item_id=%s in %s/job_config.json "
                           "does not match any current queue entry — skipping reply",
                           queue_item_id, work_dir.name)
            return

        # Verify the match really is for THIS uploaded video: the queue item's title
        # must match the video title (case-insensitive). If not, the link was likely
        # corrupted by an earlier bug — refuse to reply rather than reply to the wrong commenter.
        queue_title = (match.get("final_title", "") or "").strip().lower()
        actual_title = (video_title or "").strip().lower()
        if queue_title and actual_title and queue_title != actual_title:
            logger.warning(
                "_notify_comment_requester: queue_item_id=%s links to title %r but this video "
                "is %r — refusing to reply (mismatch suggests corrupted link)",
                queue_item_id, match.get("final_title"), video_title,
            )
            return
        logger.debug("_notify_comment_requester: matched by queue_item_id=%s", queue_item_id)
        # Always mark the queue item as posted with the URL
        yt.update_queue_item(match["id"], status="posted", youtube_url=yt_url)
        comment_id = match.get("comment_id", "")
        if not comment_id or match.get("notified"):
            return  # no comment to reply to, or already replied
        reply_text = (
            f"Your suggested video is now live! 🎬 Watch it here: {yt_url}\n"
            f"Thanks again for the great suggestion!"
        )
        result = yt.reply_to_comment(secrets, comment_id, reply_text)
        if result.get("success"):
            yt.update_queue_item(match["id"], notified=True)
            logger.info("Notified comment requester for %s", video_title)
        else:
            logger.warning("Failed to notify comment requester: %s", result.get("error"))
    except Exception as exc:
        logger.warning("_notify_comment_requester error: %s", exc)


def _find_queue_item_for_job(work_dir: Path, video_title: str) -> dict | None:
    """Return the queue item linked to this job (by queue_item_id or title), or None."""
    try:
        queue_item_id = ""
        job_cfg_path = work_dir / "job_config.json"
        if job_cfg_path.exists():
            try:
                queue_item_id = _read_json(job_cfg_path).get("queue_item_id", "")
            except Exception:
                pass
        queue = yt.load_queue()
        if queue_item_id:
            match = next((q for q in queue if q.get("id") == queue_item_id), None)
            if match:
                return match
        # Title fallback: only when the match is UNAMBIGUOUS (exactly one non-posted
        # queue entry with this title). Multiple matches → bail, to avoid touching
        # the wrong queue item (which previously caused replies to go to the wrong commenter).
        title_key = (video_title or "").strip().lower()
        if not title_key:
            return None
        candidates = [
            q for q in queue
            if q.get("final_title", "").strip().lower() == title_key
            and q.get("status") not in ("posted",)
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            logger.warning("_find_queue_item_for_job: %d ambiguous title matches for %r — bailing",
                           len(candidates), video_title)
        return None
    except Exception:
        return None


def on_post_upload(
    active_job_dir: str,
    video_path: str,
    title: str,
    description: str,
    privacy: str,
    category: str,
    cover_image_path: str,
    tags_str: str,
):
    """Generator: yields (status_html, url_html) progress updates then final result."""
    cfg = load_config()
    secrets = cfg.get("youtube_client_secrets", "")

    if not secrets or not Path(secrets).expanduser().exists():
        yield (
            _post_status_html("YouTube not connected. Configure client_secrets.json in Config tab.", "error"),
            gr.update(value="", visible=False),
        )
        return

    # Guard: refuse to re-upload a job that already has a YouTube video ID, and claim
    # the upload slot under _auto_post_lock so the background auto-post thread can't
    # race us to upload the same video at the same time.
    # CRITICAL: derive work_dir from the video being uploaded, NOT active_job_state —
    # the user may have selected a different completed video on the Post tab while a
    # newer job is the active one. Using active_job_dir here would write youtube_video_id
    # to the wrong job.json, leaving the actual uploaded video marked as "needs upload"
    # which then triggers a duplicate upload from the auto-post loop.
    work_dir = _post_tab_work_dir(video_path, active_job_dir)
    manual_claimed = False
    if work_dir:
        with _auto_post_lock:
            meta_path = _job_meta_path(work_dir)
            try:
                existing_meta = _read_json(meta_path)
            except Exception:
                existing_meta = {}
            if existing_meta.get("youtube_video_id"):
                existing_url = existing_meta.get("youtube_url", "")
                yield (
                    _post_status_html(
                        f"Already uploaded — video ID {existing_meta['youtube_video_id']} exists. "
                        f"Delete the job record to force re-upload.",
                        "error",
                    ),
                    gr.update(
                        value=(
                            f'<a href="{html.escape(existing_url)}" target="_blank">'
                            f'{html.escape(existing_url)}</a>'
                        ) if existing_url else "",
                        visible=bool(existing_url),
                    ),
                )
                return
            if str(work_dir) in _auto_post_triggered:
                yield (
                    _post_status_html(
                        "Auto-post is currently uploading this video — please wait.", "error"
                    ),
                    gr.update(value="", visible=False),
                )
                return
            # Claim the slot so the background thread won't start a concurrent upload.
            _auto_post_triggered.add(str(work_dir))
            manual_claimed = True

    if not video_path or not Path(video_path).exists():
        if manual_claimed and work_dir:
            with _auto_post_lock:
                _auto_post_triggered.discard(str(work_dir))
        yield (
            _post_status_html("No video file selected. Load from current job or select a file.", "error"),
            gr.update(value="", visible=False),
        )
        return

    # Check upload backoff (set when YouTube returns a rate-limit error)
    remaining = yt.upload_backoff_remaining()
    if remaining > 0:
        mins = int(remaining / 60) + 1
        if manual_claimed and work_dir:
            with _auto_post_lock:
                _auto_post_triggered.discard(str(work_dir))
        yield (
            _post_status_html(
                f"YouTube upload blocked after a previous error — retry in ~{mins} min.", "error"
            ),
            gr.update(value="", visible=False),
        )
        return

    tags = [t.strip() for t in (tags_str or "").split(",") if t.strip()]
    category_id = yt.CATEGORY_OPTIONS.get(category, "22") if category in yt.CATEGORY_OPTIONS else category

    yield (
        _post_status_html(f"Starting upload of {Path(video_path).name}…", "working"),
        gr.update(value="", visible=False),
    )

    last_pct = [0.0]

    def _progress(pct, msg):
        last_pct[0] = pct

    result = yt.upload_video(
        client_secrets_path=secrets,
        video_path=video_path,
        title=title or "Untitled Video",
        description=description or "",
        tags=tags,
        category_id=category_id,
        privacy_status=privacy or "private",
        thumbnail_path=cover_image_path if cover_image_path and Path(cover_image_path).exists() else None,
        progress_callback=_progress,
    )

    if result["error"]:
        # Release the claim so auto-post can retry later if needed.
        if manual_claimed and work_dir:
            with _auto_post_lock:
                _auto_post_triggered.discard(str(work_dir))
        if result["error"].startswith("UPLOAD_LIMIT_EXCEEDED:"):
            yield (
                _post_status_html(
                    "YouTube rate limit hit — uploads blocked for ~2 hours. Auto-post will retry automatically.", "error"
                ),
                gr.update(value="", visible=False),
            )
        else:
            yield (
                _post_status_html(f"Upload failed: {result['error']}", "error"),
                gr.update(value="", visible=False),
            )
        return

    # Persist youtube metadata to job.json.
    # Keep work_dir in _auto_post_triggered so the background loop won't attempt a duplicate upload.
    if work_dir:
        _write_job_meta(
            work_dir,
            youtube_video_id=result["video_id"],
            youtube_url=result["url"],
            youtube_upload_status="uploaded",
            youtube_privacy=privacy,
        )

    yt_url = result["url"]

    # Reply to the originating comment (if any) with the video link.
    # Pass the resolved work_dir so the queue lookup uses the uploaded video's job,
    # not the unrelated active job.
    _notify_comment_requester(
        secrets,
        str(work_dir) if work_dir else active_job_dir,
        title or "Untitled Video",
        yt_url,
    )

    url_html = (
        f'<div style="padding:8px 12px;border-radius:6px;background:#dcfce7;'
        f'border:1px solid #86efac;font-size:14px;font-weight:600;color:#15803d">'
        f'Uploaded! <a href="{html.escape(yt_url)}" target="_blank" '
        f'style="color:#2563eb;text-decoration:underline">{html.escape(yt_url)}</a></div>'
    )
    yield (
        _post_status_html(f"Upload complete: {title}", "success"),
        gr.update(value=url_html, visible=True),
    )


def _auto_post_chain(active_job_dir: str):
    """Called when auto-post is triggered. Loads job data and uploads. Generator."""
    cfg = load_config()
    work_dir = _preferred_work_dir(active_job_dir)
    if work_dir is None:
        yield (
            _post_status_html("Auto-post: no active job found.", "error"),
            gr.update(value="", visible=False),
        )
        return

    # Check daily upload limit before doing anything else
    remaining = yt.upload_backoff_remaining()
    if remaining > 0:
        pause_until = time.time() + remaining
        _write_job_meta(work_dir, _auto_post_paused_until=pause_until)
        with _auto_post_lock:
            _auto_post_triggered.add(str(work_dir))
        mins = int(remaining / 60) + 1
        logger.info("Auto-post paused for %s — upload backoff active for ~%d more min",
                    work_dir.name, mins)
        yield (
            _post_status_html(
                f"YouTube upload blocked after a previous error — auto-post will retry in ~{mins} min.", "error"
            ),
            gr.update(value="", visible=False),
        )
        return

    try:
        final_path = _final_path_for_work_dir(work_dir)
        video_path = str(final_path) if final_path.exists() else ""
        job_cfg_path = work_dir / "job_config.json"
        job_cfg = _read_json(job_cfg_path) if job_cfg_path.exists() else {}
        video_title = job_cfg.get("video_title", "") or job_cfg.get("title", "") or work_dir.name
        style = job_cfg.get("style", "")
        music_desc = job_cfg.get("music_desc", "")
        cover_path = work_dir / "cover.png"
        thumbnail = str(cover_path) if cover_path.exists() else ""

        # Mark the queue item upload_pending immediately — prevents auto-start from
        # regenerating this video if the upload fails or is paused.
        try:
            q_item = _find_queue_item_for_job(work_dir, video_title)
            if q_item and q_item.get("status") == "pending":
                yt.update_queue_item(q_item["id"], status="upload_pending")
                logger.info("Marked queue item %r as upload_pending", video_title)
        except Exception as _qe:
            logger.warning("Could not mark queue item upload_pending: %s", _qe)

        store = DurableStore.default()
        try:
            job = store.get_job_by_work_dir(str(work_dir))
            scenes = store.scene_rows(job["id"]) if job else []
        finally:
            store.close()
        description = generate_youtube_description(
            title=video_title, scenes=scenes, style=style, music_desc=music_desc
        )
    except Exception as exc:
        yield (
            _post_status_html(f"Auto-post prep failed: {str(exc)[:200]}", "error"),
            gr.update(value="", visible=False),
        )
        return

    yield from on_post_upload(
        active_job_dir=active_job_dir,
        video_path=video_path,
        title=video_title,
        description=description,
        privacy=cfg.get("youtube_post_privacy", "private"),
        category=cfg.get("youtube_post_category", "22"),
        cover_image_path=thumbnail,
        tags_str="",
    )

    # If a backoff is active after the upload attempt, schedule a per-job retry
    # and reset _auto_post_triggered so _poll_job_outputs can re-fire when the backoff clears.
    remaining = yt.upload_backoff_remaining()
    if remaining > 0:
        pause_until = time.time() + remaining
        _write_job_meta(work_dir, _auto_post_paused_until=pause_until, _auto_post_triggered=False)
        with _auto_post_lock:
            _auto_post_triggered.discard(str(work_dir))
        mins = int(remaining / 60) + 1
        logger.info("Auto-post will retry for %s in ~%d min", work_dir.name, mins)


def _background_start_next_video() -> bool:
    """Start the next pending queue item from the background thread.

    Consumes on_generate_script as a generator (Gradio update objects are
    discarded; the file-I/O and process-launch side-effects still happen).
    Returns True if a job was started, False if nothing to do.
    """
    # Atomically check all guards and claim the queue item under the lock so
    # concurrent background threads can't both pick the same item.
    item = None
    with _auto_post_lock:
        if _is_job_running():
            logger.info("Background auto-start: a job is already running, skipping")
            return False
        if _auto_start_in_progress.is_set():
            logger.info("Background auto-start: another start already in progress, skipping")
            return False
        item = _best_pending_queue_item()
        if not item:
            cfg = load_config()
            item = _auto_pick_suggestion(cfg)
        if not item:
            logger.info("Background auto-start: no pending queue items or suggestions")
            return False
        # Mark the queue item generating immediately so no other thread re-picks it
        # before the generation subprocess creates job_config.json.
        item_id = item.get("id")
        if item_id:
            try:
                yt.update_queue_item(item_id, status="upload_pending")
            except Exception:
                pass
        _auto_start_in_progress.set()

    title = item.get("final_title", "")
    prompt = item.get("video_prompt") or ""
    cfg = load_config()
    n_scenes = max(6, item.get("suggested_scene_count") or cfg.get("default_n_scenes", 20))
    resolution = _resolution_for_scene_count(n_scenes)
    voice_name = cfg.get("default_voice", "")
    logger.info("Background auto-start: starting %r (%d scenes, %s)", title, n_scenes, resolution)
    try:
        # Phase 1: generate script and capture the work_dir/job_id from the last yield.
        # on_generate_script yields Gradio update objects; index 2 = job_id, 3 = work_dir_str,
        # 5 = music_desc, 7 = style (see result tuple in on_generate_script).
        last_yield = None
        for result in on_generate_script(title, prompt, int(n_scenes), auto_approve=True):
            last_yield = result

        if last_yield is None or not isinstance(last_yield, tuple) or len(last_yield) < 8:
            logger.error("Background auto-start: on_generate_script returned no usable result for %r", title)
            raise RuntimeError("on_generate_script produced no output")

        job_id      = last_yield[2]
        work_dir_str = last_yield[3]
        music_desc  = last_yield[5]
        style       = last_yield[7]

        if not job_id or not work_dir_str:
            raise RuntimeError(f"on_generate_script did not produce job_id/work_dir (got {job_id!r}, {work_dir_str!r})")

        logger.info("Background auto-start: script done for %r, work_dir=%s — launching generation", title, work_dir_str)

        # Phase 2: launch the generation subprocess (mirrors what _auto_generate does in the Gradio chain).
        # on_generate reads scene data from DurableStore; scene_title/image_prompt/video_prompt/narration
        # are for saving the "currently edited scene 1" — empty strings are fine here.
        for _ in on_generate(
            title, prompt, int(n_scenes), voice_name, resolution,
            music_desc, style, True,
            job_id, work_dir_str, 1,
            "", "", "", "",
        ):
            pass

        logger.info("Background auto-start: generation subprocess launched for %r", title)
        # The subprocess is now running — _is_job_running() returns True, so the
        # _auto_start_in_progress guard is no longer needed.  Clear it so that if
        # the generation finishes and another video needs to start, the flag does
        # not block the next _background_start_next_video call.
        _auto_start_in_progress.clear()
        return True

    except Exception as exc:
        logger.exception("Background auto-start: failed for %r: %s", title, exc)
        # Restore queue item to pending so it can be retried.
        if item_id:
            try:
                yt.update_queue_item(item_id, status="pending")
            except Exception:
                pass
        _auto_start_in_progress.clear()
        return False


def _do_upload_for_job(work_dir: Path) -> bool:
    """Upload a completed job to YouTube. Called from the background thread.

    Returns True on successful upload, False on skip/failure.
    Uses the same guard set as the Gradio-timer path to prevent double-uploads.
    """
    cfg = load_config()
    try:
        # Atomically claim this job so neither the timer nor a concurrent
        # background scan fires a second upload.
        with _auto_post_lock:
            if str(work_dir) in _auto_post_triggered:
                return False
            meta = _read_json(_job_meta_path(work_dir))
            if (meta.get("status") != "done"
                    or meta.get("youtube_video_id")):
                return False
            now = time.time()
            paused_until = float(meta.get("_auto_post_paused_until") or 0)
            if paused_until and now < paused_until:
                return False
            if paused_until and now >= paused_until:
                _write_job_meta(work_dir, _auto_post_paused_until=None)
            _auto_post_triggered.add(str(work_dir))
            _write_job_meta(work_dir, _auto_post_triggered=True)

        job_cfg_path = work_dir / "job_config.json"
        job_cfg = _read_json(job_cfg_path) if job_cfg_path.exists() else {}
        video_title = job_cfg.get("video_title", "") or job_cfg.get("title", "") or work_dir.name

        remaining = yt.upload_backoff_remaining()
        if remaining > 0:
            pause_until = time.time() + remaining
            _write_job_meta(work_dir, _auto_post_paused_until=pause_until)
            mins = int(remaining / 60) + 1
            logger.info("Background auto-post paused — upload backoff active for ~%d more min", mins)
            return False
        style = job_cfg.get("style", "")
        music_desc = job_cfg.get("music_desc", "")
        cover_path = work_dir / "cover.png"
        thumbnail = str(cover_path) if cover_path.exists() else ""
        final_path = _final_path_for_work_dir(work_dir)
        video_path = str(final_path) if final_path.exists() else ""

        if not video_path:
            logger.warning("Background auto-post: no video file for %s — clearing trigger so it can retry",
                           work_dir.name)
            with _auto_post_lock:
                _auto_post_triggered.discard(str(work_dir))
            _write_job_meta(work_dir, _auto_post_triggered=False)
            return False

        try:
            q_item = _find_queue_item_for_job(work_dir, video_title)
            if q_item and q_item.get("status") == "pending":
                yt.update_queue_item(q_item["id"], status="upload_pending")
                logger.info("Background auto-post: marked queue item %r upload_pending", video_title)
        except Exception as _qe:
            logger.warning("Background auto-post: could not mark queue item upload_pending: %s", _qe)

        try:
            store = DurableStore.default()
            try:
                job = store.get_job_by_work_dir(str(work_dir))
                scenes = store.scene_rows(job["id"]) if job else []
            finally:
                store.close()
            description = generate_youtube_description(
                title=video_title, scenes=scenes, style=style, music_desc=music_desc,
            )
        except Exception as exc:
            logger.warning("Background auto-post: description generation failed for %s: %s",
                           work_dir.name, exc)
            description = ""

        secrets = cfg.get("youtube_client_secrets", "")
        if not secrets or not Path(secrets).expanduser().exists():
            logger.warning("Background auto-post: YouTube not connected — skipping %s", work_dir.name)
            with _auto_post_lock:
                _auto_post_triggered.discard(str(work_dir))
            _write_job_meta(work_dir, _auto_post_triggered=False)
            return False

        privacy = cfg.get("youtube_post_privacy", "private")
        category = cfg.get("youtube_post_category", "22")
        category_id = (yt.CATEGORY_OPTIONS.get(category, "22")
                       if category in yt.CATEGORY_OPTIONS else category)

        # Final guard: re-read meta in case a manual upload completed while we were setting up.
        if _read_json(_job_meta_path(work_dir)).get("youtube_video_id"):
            logger.info("Background auto-post: %s was manually uploaded — skipping", work_dir.name)
            with _auto_post_lock:
                _auto_post_triggered.discard(str(work_dir))
            return False

        logger.info("Background auto-post: uploading %r (%s)", video_title, work_dir.name)
        result = yt.upload_video(
            client_secrets_path=secrets,
            video_path=video_path,
            title=video_title or "Untitled Video",
            description=description or "",
            tags=[],
            category_id=category_id,
            privacy_status=privacy or "private",
            thumbnail_path=thumbnail or None,
            progress_callback=None,
        )

        if result["error"]:
            logger.error("Background auto-post: upload failed for %s: %s", work_dir.name, result["error"])
            if result["error"].startswith("UPLOAD_LIMIT_EXCEEDED:"):
                remaining = yt.upload_backoff_remaining()
                pause_until = time.time() + max(remaining, 1)
                _write_job_meta(work_dir, _auto_post_paused_until=pause_until, _auto_post_triggered=False)
                with _auto_post_lock:
                    _auto_post_triggered.discard(str(work_dir))
                mins = int(max(remaining, 1) / 60) + 1
                logger.info("Background auto-post: rate limit hit — retrying for %s in ~%d min",
                            work_dir.name, mins)
            else:
                with _auto_post_lock:
                    _auto_post_triggered.discard(str(work_dir))
                _write_job_meta(work_dir, _auto_post_triggered=False)
            return False

        _write_job_meta(
            work_dir,
            youtube_video_id=result["video_id"],
            youtube_url=result["url"],
            youtube_upload_status="uploaded",
            youtube_privacy=privacy,
        )
        _notify_comment_requester(secrets, str(work_dir), video_title, result["url"])
        logger.info("Background auto-post: uploaded %r → %s", video_title, result["url"])

        if cfg.get("youtube_auto_start_job"):
            threading.Thread(
                target=_background_start_next_video, daemon=True, name="auto-start-bg"
            ).start()

        return True

    except Exception as exc:
        logger.exception("Background auto-post: unexpected error for %s: %s", work_dir.name, exc)
        with _auto_post_lock:
            _auto_post_triggered.discard(str(work_dir))
        try:
            _write_job_meta(work_dir, _auto_post_triggered=False)
        except Exception:
            pass
        return False


def _background_auto_post_loop() -> None:
    """Daemon thread: uploads finished jobs regardless of browser-session state.

    Scans OUTPUT_DIR every 60 s for jobs with status=done and no youtube_video_id.
    Delegates to _do_upload_for_job which shares the same in-process lock as the
    Gradio-timer path, so the two paths never race each other.
    """
    logger.info("Background auto-post thread started (interval=60s)")
    while True:
        try:
            cfg = load_config()
            if cfg.get("youtube_auto_post"):
                now = time.time()
                candidates = []
                for job_dir in OUTPUT_DIR.iterdir():
                    if not job_dir.is_dir():
                        continue
                    meta_path = _job_meta_path(job_dir)
                    if not meta_path.exists():
                        continue
                    try:
                        meta = _read_json(meta_path)
                    except Exception:
                        continue
                    if meta.get("status") != "done":
                        continue
                    if meta.get("youtube_video_id"):
                        continue
                    with _auto_post_lock:
                        if str(job_dir) in _auto_post_triggered:
                            continue
                    paused_until = float(meta.get("_auto_post_paused_until") or 0)
                    if paused_until and now < paused_until:
                        continue
                    candidates.append(job_dir)

                if candidates:
                    # Upload the most-recently-modified job first (most likely the just-finished one)
                    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    logger.info("Background auto-post: %d candidate(s) — trying %s",
                                len(candidates), candidates[0].name)
                    _do_upload_for_job(candidates[0])
                elif cfg.get("youtube_auto_start_job") and not _is_job_running():
                    # No completed jobs waiting to upload and no generation running —
                    # kick off the next pending queue item so the pipeline keeps moving
                    # even when no browser session is connected.
                    if _best_pending_queue_item() or _auto_pick_suggestion(cfg):
                        logger.info("Background auto-post: idle — starting next video in queue")
                        threading.Thread(
                            target=_background_start_next_video, daemon=True, name="auto-start-bg"
                        ).start()
        except Exception as exc:
            logger.exception("Background auto-post loop: unexpected error: %s", exc)
        time.sleep(60)


# ── UI ────────────────────────────────────────────────────────────────────────



# ── Video Suggestions ─────────────────────────────────────────────────────────

def _suggestions_html(suggestions: list[dict]) -> str:
    if not suggestions:
        return (
            '<div style="color:#6b7280;font-size:13px;padding:4px 0">'
            'No suggestions yet. Click <strong>Refresh Suggestions</strong> to generate ideas.</div>'
        )
    rows = []
    unused_idx = 0
    for s in suggestions:
        used = s.get("used", False)
        title = html.escape(s.get("title", ""))
        reason = html.escape(s.get("reason", ""))
        interest = s.get("interestingness", 0.7)
        interest_color = "#15803d" if interest >= 0.7 else ("#92400e" if interest >= 0.5 else "#6b7280")
        if not used:
            unused_idx += 1
            num_label = f'<span style="color:#6b7280;margin-right:6px">#{unused_idx}</span>'
            used_badge = ""
            opacity = ""
        else:
            num_label = '<span style="color:#d1d5db;margin-right:6px">—</span>'
            used_badge = '<span style="font-size:10px;color:#9ca3af;margin-left:6px">[queued]</span>'
            opacity = "opacity:0.55;"
        rows.append(
            f'<div style="padding:6px 8px;border-bottom:1px solid #e5e7eb;font-size:13px;{opacity}">'
            f'{num_label}'
            f'<strong>{title}</strong>'
            f'<span style="font-size:11px;margin-left:8px;color:{interest_color}">★ {interest:.0%}</span>'
            f'{used_badge}'
            f'<div style="font-size:11px;color:#6b7280;margin-top:2px">{reason}</div>'
            f'</div>'
        )
    return (
        '<div style="border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;'
        'max-height:260px;overflow-y:auto">'
        + "".join(rows)
        + "</div>"
    )


def _channel_video_titles(cfg: dict) -> list[str]:
    """Collect known video titles: YouTube API first, posted queue items as fallback."""
    secrets = cfg.get("youtube_client_secrets", "")
    titles: list[str] = []
    try:
        titles = yt.fetch_channel_video_titles(secrets, max_results=50)
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


def on_refresh_suggestions() -> tuple:
    """Fetch channel titles and ask the LLM for 5 fresh video topic suggestions.
    Returns (suggestions_html, status_msg).
    """
    cfg = load_config()
    existing_titles = _channel_video_titles(cfg)
    suggestions_data = generate_video_suggestions(existing_titles, cfg)
    if not suggestions_data:
        current = yt.load_suggestions()
        return _suggestions_html(current), "⚠️ Failed to generate suggestions — check LLM settings."

    suggestions = [
        {
            "id": str(uuid.uuid4())[:8],
            "title": s["title"],
            "reason": s["reason"],
            "interestingness": s["interestingness"],
            "created_at": time.time(),
            "used": False,
        }
        for s in suggestions_data
    ]
    yt.save_suggestions(suggestions)
    logger.info("Refreshed %d video suggestions", len(suggestions))
    return _suggestions_html(suggestions), f"Generated {len(suggestions)} suggestions."


def on_add_suggestion_to_queue(row_idx: int) -> tuple:
    """Add a suggestion (by 1-based unused-row index) to the video queue.
    Returns (suggestions_html, queue_html, status_msg).
    """
    suggestions = yt.load_suggestions()
    unused = [s for s in suggestions if not s.get("used")]
    idx = int(row_idx or 1) - 1
    if idx < 0 or idx >= len(unused):
        queue = yt.load_queue()
        return (
            _suggestions_html(suggestions),
            _queue_html(queue),
            f"Row {row_idx} not found — only {len(unused)} unused suggestion(s).",
        )
    suggestion = unused[idx]
    # Mark used
    for s in suggestions:
        if s.get("id") == suggestion.get("id"):
            s["used"] = True
            break
    yt.save_suggestions(suggestions)

    # Add to queue (no originating comment)
    fake_comment = {
        "comment_id": "",
        "video_id": "",
        "commenter": "AI Suggestion",
        "text": suggestion.get("reason", ""),
        "suggested_scene_count": 20,
        "interestingness": suggestion.get("interestingness", 0.7),
    }
    queue_item = yt.add_to_queue(fake_comment, suggestion["title"], source="suggestion")
    if queue_item:
        threading.Thread(
            target=_prefetch_video_prompt,
            args=(queue_item["id"], suggestion["title"], suggestion.get("reason", "")),
            daemon=True,
        ).start()

    queue = yt.load_queue()
    return (
        _suggestions_html(suggestions),
        _queue_html(queue),
        f"Added to queue: {html.escape(suggestion['title'])}",
    )


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
    # Mark as used in the persisted list
    all_suggestions = yt.load_suggestions()
    for s in all_suggestions:
        if s.get("id") == suggestion.get("id"):
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


def _sync_queue_state_on_startup() -> None:
    """Mark queue items upload_pending for any completed jobs that haven't been uploaded.

    Called once at process start (before Gradio event handlers fire) so that the
    auto-start logic never re-generates a video that is already sitting completed
    and waiting for the daily upload limit to reset.
    """
    try:
        now = time.time()
        for job_dir in OUTPUT_DIR.iterdir():
            if not job_dir.is_dir():
                continue
            meta_path = _job_meta_path(job_dir)
            if not meta_path.exists():
                continue
            try:
                meta = _read_json(meta_path)
            except Exception:
                continue
            if meta.get("status") != "done":
                continue
            if meta.get("youtube_video_id"):
                continue
            # Completed job with no upload — find its queue item
            job_cfg_path = job_dir / "job_config.json"
            if not job_cfg_path.exists():
                continue
            job_cfg = _read_json(job_cfg_path)
            video_title = (job_cfg.get("video_title", "") or job_cfg.get("title", "") or "")
            if not video_title:
                continue
            try:
                q_item = _find_queue_item_for_job(job_dir, video_title)
                if q_item and q_item.get("status") == "pending":
                    yt.update_queue_item(q_item["id"], status="upload_pending")
                    logger.info("Startup sync: marked %r upload_pending (completed job waiting)", video_title)
            except Exception as exc:
                logger.warning("Startup sync: failed for %s: %s", job_dir.name, exc)
    except Exception as exc:
        logger.warning("Startup queue sync failed: %s", exc)


def _on_startup_auto_fetch() -> tuple:
    """Runs on app load. Fetch+evaluate if auto_fetch_evaluate is configured."""
    cfg = load_config()
    if not cfg.get("youtube_auto_fetch_evaluate", False):
        cache = yt.load_comments_cache()
        queue = yt.load_queue()
        return _yt_refresh_outputs(cache, queue, "") + (None,)
    auto_approve = cfg.get("youtube_auto_approve_comments", False)
    return on_yt_fetch_and_evaluate(auto_approve)


def _on_post_done_refetch() -> tuple:
    """Runs after a post completes. Fetch+evaluate new comments if configured.

    Always attempts to continue the automation loop:
    - If youtube_auto_fetch_evaluate is on  → full fetch + evaluate + pick next
    - If only youtube_auto_start_job is on  → skip API fetch, pick from current queue/suggestions
    - Otherwise                             → no-op
    """
    cfg = load_config()
    auto_start = cfg.get("youtube_auto_start_job", False)
    auto_fetch  = cfg.get("youtube_auto_fetch_evaluate", False)

    if auto_fetch:
        auto_approve = cfg.get("youtube_auto_approve_comments", False)
        logger.info("_on_post_done_refetch: fetching + evaluating new YouTube comments")
        return on_yt_fetch_and_evaluate(auto_approve)

    if auto_start and not _is_job_running():
        # Fetch not enabled — skip the API call but still pick the next job
        logger.info("_on_post_done_refetch: auto_fetch off, checking queue/suggestions for next job")
        queue = yt.load_queue()
        cache = yt.load_comments_cache()
        trigger = _best_pending_queue_item()
        if not trigger:
            trigger = _auto_pick_suggestion(cfg)
        msg = (
            f"Auto-starting: {trigger.get('final_title', '')}." if trigger
            else "No pending items or suggestions."
        )
        return _yt_refresh_outputs(cache, queue, msg) + (trigger,)

    return (gr.update(),) * 5


def build_ui() -> gr.Blocks:
    cfg = load_config()

    with gr.Blocks(title="Stephen Spielbot") as demo:
        with gr.Row(elem_id="header_row", equal_height=True):
            with gr.Column(scale=0, min_width=130):
                gr.Image(
                    value=str(Path(__file__).parent / "assets" / "StephenSpielbot.png"),
                    show_label=False, buttons=[], container=False, width=120, height=120,
                )
            with gr.Column(scale=1):
                gr.HTML("""
                <div style="display:flex;flex-direction:column;justify-content:space-between;height:120px;padding:4px 0">
                  <div style="font-size:2rem;font-weight:700;line-height:1.2;margin:auto 0">Stephen Spielbot</div>
                  <div style="font-size:1rem;color:#6b7280">AI slop video director</div>
                </div>
                """)

        # Shared state for remix and the active script review session.
        combined_state          = gr.State("")
        music_state             = gr.State("")
        ambient_state           = gr.State("")
        music_desc_state        = gr.State("")
        style_state             = gr.State("")
        script_job_id_state     = gr.State("")
        script_work_dir_state   = gr.State("")
        current_scene_state     = gr.State(1)
        active_job_state        = gr.State("")
        post_auto_trigger_state = gr.State(False)
        post_cover_path_state   = gr.State("")

        with gr.Tabs(elem_id="main_tabs") as tabs:

            # ── YouTube ──────────────────────────────────────────────────
            with gr.Tab("📺 YouTube", id="youtube"):
                gr.Markdown("### Channel Comments & Video Requests")
                gr.Markdown(
                    "Fetch recent comments from your YouTube channel, evaluate them with AI, "
                    "and approve video requests to add them to the creation queue."
                )

                yt_auth_status = gr.HTML(value=on_yt_check_status())
                with gr.Row():
                    yt_connect_btn    = gr.Button("Connect YouTube", variant="primary", scale=2)
                    yt_disconnect_btn = gr.Button("Disconnect", variant="stop", scale=1)
                yt_auth_timer = gr.Timer(value=2, active=False)

                gr.Markdown("---")
                gr.Markdown("### 🤖 Automation")
                yt_fully_automated_cb = gr.Checkbox(
                    label="⚡ Fully automated mode — enables all automation below",
                    value=cfg.get("youtube_fully_automated", False),
                )
                with gr.Row():
                    yt_auto_fetch_cb = gr.Checkbox(
                        label="Auto fetch & evaluate on startup and after each post",
                        value=cfg.get("youtube_auto_fetch_evaluate", False),
                        scale=1,
                    )
                    yt_auto_approve_cb = gr.Checkbox(
                        label="Auto-approve requests (confidence ≥ 70%)",
                        value=cfg.get("youtube_auto_approve_comments", False),
                        scale=1,
                    )
                with gr.Row():
                    yt_auto_start_cb = gr.Checkbox(
                        label="Auto-start job when approved (highest interest first)",
                        value=cfg.get("youtube_auto_start_job", False),
                        scale=1,
                    )
                    yt_auto_script_cb = gr.Checkbox(
                        label="Auto-approve script (skip review)",
                        value=cfg.get("youtube_auto_approve_script", False),
                        scale=1,
                    )
                    yt_auto_post_cb = gr.Checkbox(
                        label="Auto-post to YouTube when generation completes",
                        value=cfg.get("youtube_auto_post", False),
                        scale=1,
                    )

                gr.Markdown("---")
                gr.Markdown("### Recent Comments")
                yt_fetch_evaluate_btn = gr.Button("🔄 Fetch & Evaluate Comments", variant="primary")
                yt_comments_status = gr.Markdown("")
                yt_comments_html = gr.HTML(value=_comments_html(yt.load_comments_cache()))

                gr.Markdown("### Pending Requests")
                gr.Markdown("_Sorted by interestingness ★ — highest first. Row number used for Approve/Reject._")
                yt_pending_html = gr.HTML(value=_pending_requests_html(yt.load_comments_cache()))
                with gr.Row():
                    yt_row_num      = gr.Number(value=1, label="Request #", minimum=1, step=1, scale=1)
                    yt_title_override = gr.Textbox(
                        label="Title override (leave blank to use suggested)", scale=3
                    )
                with gr.Row():
                    yt_approve_btn  = gr.Button("Approve", variant="primary", scale=1)
                    yt_reject_btn   = gr.Button("Reject", variant="stop", scale=1)
                yt_action_status = gr.Markdown("")

                gr.Markdown("### 💡 AI Video Suggestions")
                gr.Markdown(
                    "_LLM-generated topic ideas that complement your existing videos. "
                    "Use row # to add any to the queue._"
                )
                yt_suggestions_html = gr.HTML(value=_suggestions_html(yt.load_suggestions()))
                with gr.Row():
                    yt_refresh_suggestions_btn = gr.Button(
                        "↺ Refresh Suggestions", variant="secondary", scale=2
                    )
                    yt_suggestion_row_num = gr.Number(
                        value=1, label="Suggestion #", minimum=1, step=1, scale=1
                    )
                    yt_add_suggestion_btn = gr.Button(
                        "➕ Add to Queue", variant="primary", scale=1
                    )
                yt_suggestions_status = gr.Markdown("")

                gr.Markdown("### ✍️ Add Manually")
                gr.Markdown("_Add any video topic directly to the queue without needing a comment._")
                yt_manual_title = gr.Textbox(
                    label="Video Title",
                    placeholder="e.g.  The History of the Roman Colosseum",
                )
                yt_manual_prompt = gr.Textbox(
                    label="Directorial Prompt (optional)",
                    placeholder="e.g.  Focus on the gladiatorial games and the social role of public spectacle.",
                    lines=2,
                )
                with gr.Row():
                    yt_manual_n_scenes = gr.Number(
                        value=20, label="Number of scenes", minimum=6, maximum=50, step=1, scale=1
                    )
                    yt_manual_add_btn = gr.Button("➕ Add to Queue", variant="primary", scale=2)
                yt_manual_status = gr.Markdown("")

                gr.Markdown("### Video Queue")
                yt_queue_html = gr.HTML(value=_queue_html(yt.load_queue()))
                with gr.Row():
                    yt_start_next_btn = gr.Button("▶ Start Next Video in Queue", variant="primary", scale=2)
                    yt_queue_row_num  = gr.Number(value=1, label="Queue #", minimum=1, step=1, scale=1)
                    yt_move_up_btn = gr.Button("↑ Move Up", scale=1)
                    yt_move_down_btn = gr.Button("↓ Move Down", scale=1)
                    yt_remove_queue_btn = gr.Button("🗑 Remove", variant="stop", scale=1)
                with gr.Row():
                    yt_sort_priority_btn = gr.Button("🔀 Sort by Priority (comments first)", scale=2)
                yt_queue_action_status = gr.Markdown("")

            # ── Create ───────────────────────────────────────────────────
            with gr.Tab("🎬 Create", id="create"):
                with gr.Row():
                    with gr.Column(scale=3):
                        video_title_in = gr.Textbox(
                            label="Video Title",
                            placeholder="e.g.  The Rise and Fall of the Roman Empire",
                            elem_id="video_title_input",
                        )
                    with gr.Column(scale=1):
                        n_scenes_in = gr.Slider(1, MAX_SCENES, value=cfg.get("default_n_scenes", 20), step=1, label="Scenes")
                with gr.Row():
                    with gr.Column(scale=3):
                        title_in = gr.Textbox(
                            label="Prompt / Description (optional — adds detail beyond the title)",
                            placeholder="e.g.  Focus on the economic decline, military defeats, and rise of Christianity",
                            elem_id="title_input",
                        )

                with gr.Row():
                    voice_dropdown = gr.Dropdown(
                        label="Narrator Voice",
                        choices=get_voice_choices(),
                        value=cfg.get("default_voice") or F5TTS_DEFAULT_OPTION,
                    )
                    resolution_in = gr.Dropdown(
                        label="Resolution",
                        choices=list(_RESOLUTIONS.keys()),
                        value=cfg.get("resolution", _DEFAULT_RESOLUTION),
                    )
                with gr.Row():
                    auto_approve_in = gr.Checkbox(
                        label="Auto-approve script (skip review)",
                        value=cfg.get("youtube_auto_approve_script", False),
                    )

                gen_script_btn = gr.Button(
                    "1. Generate Script →", variant="primary", size="lg"
                )

                with gr.Row():
                    load_script_dropdown = gr.Dropdown(
                        label="Or load an existing script",
                        choices=[(lbl, wdir) for lbl, wdir in _list_script_jobs()],
                        value=None,
                        allow_custom_value=False,
                        scale=4,
                    )
                    load_script_btn = gr.Button("Load Script →", variant="secondary", scale=1)

            # ── Script Review ────────────────────────────────────────────
            with gr.Tab("📝 Script", id="script"):
                with gr.Column(visible=False) as script_col:
                    script_overview_md = gr.Markdown("")
                    style_box = gr.Textbox(
                        label="Visual Style (applied to all scenes)",
                        lines=1,
                        placeholder="Generated or entered style will appear here — edit freely",
                    )

                    image_gen_status = gr.HTML(value="", visible=False)

                    with gr.Row():
                        script_resolution_in = gr.Dropdown(
                            label="Preview Image Resolution",
                            choices=list(_RESOLUTIONS.keys()),
                            value=cfg.get("resolution", _DEFAULT_RESOLUTION),
                        )
                        regen_scene_btn = gr.Button(
                            "↺ Regenerate current scene image", variant="secondary"
                        )

                    with gr.Row():
                        prev_scene_btn = gr.Button("◀ Prev", scale=0)
                        scene_picker = gr.Slider(1, 2, value=1, step=1, label="Scene")
                        next_scene_btn = gr.Button("Next ▶", scale=0)

                    scene_info_md = gr.Markdown("**Scene 0/0**")
                    scene_title_box = gr.Textbox(label="Title", lines=1)
                    with gr.Row():
                        scene_image_prompt_box = gr.Textbox(
                            label="Image Prompt  (FLUX - static, highly detailed)",
                            lines=5,
                        )
                        scene_video_prompt_box = gr.Textbox(
                            label="Video Prompt  (LTX - motion & camera)",
                            lines=5,
                        )
                    scene_narration_box = gr.Textbox(label="Narration", lines=6)
                    selected_scene_preview = gr.Image(
                        label="Scene Preview (first frame)",
                        visible=False,
                        height=260,
                        interactive=False,
                    )
                    scene_summary_html = gr.HTML("")

                    approve_btn = gr.Button(
                        "2. Approve & Generate Video →", variant="primary", size="lg"
                    )

            # ── Progress ─────────────────────────────────────────────────
            with gr.Tab("⏳ Progress", id="progress"):
                progress_bar = gr.HTML(value=_progress_html(0, "Waiting to start…"))
                progress_timer = gr.Timer(value=3, active=True)

                with gr.Row():
                    pause_job_btn = gr.Button("⏸ Pause", variant="secondary")
                    stop_job_btn  = gr.Button("⏹ Stop",  variant="stop")
                pause_stop_status = gr.Markdown("")

                gr.Markdown("---")
                gr.Markdown("#### Resume a Paused Job")
                with gr.Row():
                    resumable_job_dropdown = gr.Dropdown(
                        label="Select job to resume",
                        choices=[(lbl, wdir) for lbl, wdir in _list_resumable_jobs()],
                        value=None,
                        allow_custom_value=False,
                        scale=4,
                    )
                    load_resume_btn = gr.Button("▶ Load & Resume", variant="primary", scale=1)

                gr.Markdown("---")
                gr.Markdown("#### Durable Orchestration")
                orchestration_status = gr.HTML(value=_orchestration_html(""))
                with gr.Row():
                    resume_job_btn = gr.Button("Resume Job", variant="secondary")
                    retry_failed_btn = gr.Button("Retry Failed Tasks", variant="secondary")
                    cancel_job_btn = gr.Button("Cancel Job", variant="stop")
                recovery_status = gr.Markdown("")
                with gr.Row():
                    worker_status_id = gr.Textbox(
                        label="Worker ID",
                        placeholder="Paste worker id from the table",
                        scale=3,
                    )
                    mark_worker_online_btn = gr.Button("Mark Online", variant="secondary", scale=1)
                    mark_worker_offline_btn = gr.Button("Mark Offline", variant="secondary", scale=1)

                music_audio_out = gr.Audio(
                    label="Background Music", visible=False, interactive=False
                )

                gr.Markdown("#### YouTube Cover Image")
                gr.Markdown(
                    "_Generated automatically alongside the video — "
                    "appears here once ready (usually after music generation)._"
                )
                progress_cover_image = gr.Image(
                    label="Cover Image (1280×720)",
                    visible=False,
                    height=280,
                    interactive=False,
                )

            # ── Remix (formerly Output) ──────────────────────────────────
            with gr.Tab("🎛️ Remix", id="output"):
                final_video_out = gr.Video(
                    label="Final Video", visible=False, height=480
                )
                gr.Markdown("_Final video will appear here when generation completes._")
                with gr.Row():
                    restore_btn    = gr.Button("↩ Restore Last Session", variant="secondary", size="sm")
                    restore_status = gr.Markdown("")
                with gr.Row():
                    _recent_jobs = _list_recent_jobs()
                    recent_job_dropdown = gr.Dropdown(
                        choices=[(lbl, wdir) for lbl, wdir in _recent_jobs],
                        label="Load recent video",
                        value=None,
                        interactive=True,
                        scale=4,
                    )
                    load_recent_btn = gr.Button("Load", variant="secondary", size="sm", scale=1)

                gr.Markdown("### Scene Selection & Order")
                gr.Markdown(
                    "Check/uncheck scenes to include or exclude them. "
                    "Edit the **Order** column numbers to reorder — lower numbers come first."
                )
                with gr.Row():
                    remix_select_all_btn   = gr.Button("Select All",   size="sm", variant="secondary")
                    remix_deselect_all_btn = gr.Button("Deselect All", size="sm", variant="secondary")
                    remix_reset_order_btn  = gr.Button("Reset Order",  size="sm", variant="secondary")
                remix_scenes_check = gr.Dataframe(
                    headers=["Include", "Order", "Scene"],
                    datatype=["bool", "number", "str"],
                    col_count=(3, "fixed"),
                    row_count=(0, "dynamic"),
                    value=[],
                    interactive=True,
                    label="Scenes",
                    column_widths=["80px", "80px", None],
                )

                gr.Markdown("### Re-mix Audio")
                gr.Markdown(
                    "Adjust volume levels and re-mix without re-generating video."
                )
                with gr.Row():
                    remix_voice_vol = gr.Slider(
                        0, 200, value=cfg.get("voice_vol", 100), step=5,
                        label="Voice Volume %",
                    )
                    remix_music_vol = gr.Slider(
                        0, 100, value=cfg.get("music_vol", 18), step=1,
                        label="Music Volume %",
                    )
                    remix_ambient_vol = gr.Slider(
                        0, 100, value=cfg.get("ambient_vol", 0), step=1,
                        label="LTX Ambient Volume %",
                    )
                remix_btn    = gr.Button("Re-mix Video", variant="primary")
                remix_status = gr.Markdown("")


            # ── Upscale ──────────────────────────────────────────────────
            with gr.Tab("🔍 Upscale", id="upscale"):
                gr.Markdown(
                    "**LTX Spatial Upscale** — encodes the video to LTX latent space, "
                    "applies the `ltx-2.3-spatial-upscaler-x2-1.1` model, then decodes back. "
                    "This is the same neural upscaler used during generation (2nd pass). "
                    "Takes a few minutes. Load the current generation or upload any video."
                )
                upscale_src_vid = gr.Video(
                    label="Source Video (upload or load from current session)",
                    interactive=True, height=300,
                )
                with gr.Row():
                    load_for_upscale_btn = gr.Button(
                        "Load Current Generation", variant="secondary"
                    )
                    upscale_btn = gr.Button(
                        "Upscale with LTX Model", variant="primary"
                    )
                upscale_status  = gr.Markdown("")
                upscale_out_vid = gr.Video(
                    label="Upscaled Result", visible=False, height=480
                )

            # ── Post to YouTube ──────────────────────────────────────────
            with gr.Tab("📤 Post", id="post"):
                gr.Markdown("### Post Video to YouTube")

                with gr.Row():
                    post_job_dropdown = gr.Dropdown(
                        label="Select Video",
                        choices=[(lbl, wdir) for lbl, wdir in _list_completed_jobs()],
                        value=None,
                        allow_custom_value=False,
                        scale=4,
                    )
                    post_load_btn = gr.Button("Load from Current Job", variant="secondary", scale=1)

                with gr.Row():
                    post_video_path = gr.Textbox(
                        label="Video File Path",
                        placeholder="Auto-populated from selected job…",
                        scale=4,
                        interactive=True,
                    )

                post_title = gr.Textbox(label="Title", placeholder="Video title…", max_lines=1)

                with gr.Row():
                    post_description = gr.Textbox(
                        label="Description (AI-generated, editable)",
                        placeholder="Auto-generated from scene narrations…",
                        lines=8,
                        scale=4,
                    )
                post_regen_desc_btn = gr.Button("↺ Regenerate Description", variant="secondary", size="sm")

                gr.Markdown("#### Cover Image / Thumbnail")
                post_cover_image = gr.Image(
                    label="Thumbnail (auto-populated from cover.png)",
                    visible=False,
                    height=280,
                    interactive=False,
                )
                post_regen_cover_btn = gr.Button("↺ Regenerate Cover Image", variant="secondary", size="sm")

                gr.Markdown("#### Publishing Options")
                with gr.Row():
                    post_privacy = gr.Dropdown(
                        label="Privacy",
                        choices=["private", "unlisted", "public"],
                        value=cfg.get("youtube_post_privacy", "private"),
                    )
                    post_category = gr.Dropdown(
                        label="Category",
                        choices=list(yt.CATEGORY_OPTIONS.keys()),
                        value=next(
                            (k for k, v in yt.CATEGORY_OPTIONS.items() if v == cfg.get("youtube_post_category", "22")),
                            "People & Blogs",
                        ),
                    )
                post_tags = gr.Textbox(
                    label="Tags (comma-separated)",
                    placeholder="documentary, ai, history",
                    max_lines=1,
                )

                post_btn = gr.Button("Post to YouTube", variant="primary", size="lg")
                post_status_html = gr.HTML(value="")
                post_url_html = gr.HTML(value="", visible=False)

            # ── Config ───────────────────────────────────────────────────
            with gr.Tab("⚙️ Config", id="config"):
                save_cfg_btn = gr.Button("Save Defaults", variant="secondary", visible=False)
                cfg_status   = gr.Markdown("")

                # ── Script & Content ──────────────────────────────────────
                gr.Markdown("### Script & Content")
                with gr.Row():
                    cfg_default_n_scenes = gr.Slider(
                        1, MAX_SCENES, value=cfg.get("default_n_scenes", 20), step=1,
                        label="Default Number of Scenes",
                    )
                cfg_default_visual_style = gr.Textbox(
                    label="Default Visual Style — prepended to every image prompt",
                    value=cfg.get("default_visual_style", ""),
                    lines=2,
                    placeholder="e.g.  photorealistic, 8K resolution, cinematic lighting, no text, no watermarks, film grain",
                )
                cfg_script_extra = gr.Textbox(
                    label="Script Extra Instructions — always appended to every script generation prompt",
                    value=cfg.get("script_extra_instructions", ""),
                    lines=3,
                    placeholder="e.g.  Always include a strong call to action at the end of each video. Avoid mentioning competitor brands.",
                )

                # ── Narrator & Audio ──────────────────────────────────────
                gr.Markdown("### Narrator & Audio")
                cfg_default_voice = gr.Dropdown(
                    label="Default Narrator Voice",
                    choices=get_voice_choices(),
                    value=cfg.get("default_voice") or F5TTS_DEFAULT_OPTION,
                )
                with gr.Row():
                    cfg_music_vol = gr.Slider(
                        0, 100, value=cfg.get("music_vol", 18), step=1,
                        label="Default Music Volume %",
                    )
                    cfg_voice_vol = gr.Slider(
                        0, 200, value=cfg.get("voice_vol", 100), step=5,
                        label="Default Voice Volume %",
                    )
                    cfg_ambient_vol = gr.Slider(
                        0, 100, value=cfg.get("ambient_vol", 0), step=1,
                        label="Default Ambient Volume %",
                    )

                # ── Video Generation ──────────────────────────────────────
                gr.Markdown("### Video Generation")
                cfg_resolution = gr.Dropdown(
                    choices=list(_RESOLUTIONS.keys()),
                    value=cfg.get("resolution", _DEFAULT_RESOLUTION),
                    label="Resolution  —  higher = better quality, slower",
                )
                gr.Markdown(
                    "**First-pass (distilled LoRA mode)** — "
                    "LoRA Strength > 0 with Steps=8, CFG=1.0 is the fast distilled mode. "
                    "To use the pure dev model: set LoRA Strength=0, Steps=20–30, CFG=3–5."
                )
                with gr.Row():
                    cfg_lora = gr.Slider(
                        0.0, 1.0, value=cfg.get("lora_strength", 0.5), step=0.05,
                        label="LoRA Strength  —  0 = pure dev model, 0.5–1.0 = distilled",
                    )
                    cfg_first_pass_cfg = gr.Slider(
                        1.0, 8.0, value=cfg.get("first_pass_cfg", 1.0), step=0.5,
                        label="First-pass CFG  —  1.0 for distilled, 3–5 for dev model",
                    )
                    cfg_first_pass_steps = gr.Slider(
                        4, 40, value=cfg.get("first_pass_steps", 8), step=1,
                        label="First-pass Steps  —  8 for distilled, 20–30 for dev model",
                    )
                gr.Markdown(
                    "**Second-pass refinement** — runs after the spatial upscaler. "
                    "Higher CFG and more steps add detail and sharpness at the cost of generation time."
                )
                with gr.Row():
                    cfg_second_pass_cfg = gr.Slider(
                        1.0, 8.0, value=cfg.get("second_pass_cfg", 3.0), step=0.5,
                        label="Refinement CFG  —  3–5 = more detail",
                    )
                    cfg_second_pass_steps = gr.Slider(
                        3, 8, value=cfg.get("second_pass_steps", 6), step=1,
                        label="Refinement Steps  —  3 = fast, 6 = balanced, 8 = best",
                    )
                    cfg_max_clip = gr.Slider(
                        0, 120, value=cfg.get("max_clip_secs", 0), step=1,
                        label="Max Clip Duration (s)  —  0 = one clip per scene",
                    )

                # ── Infrastructure ────────────────────────────────────────
                gr.Markdown("### Infrastructure")
                with gr.Row():
                    cfg_workers = gr.Textbox(
                        label="ComfyUI Worker URLs (one per line)",
                        value="\n".join(cfg.get("comfy_workers", ["http://localhost:8188"])),
                        lines=4,
                        placeholder="http://localhost:8188\nhttp://s1:8188\nhttp://s2:8188",
                    )
                    cfg_tts_workers = gr.Textbox(
                        label="TTS Hosts (one per line)",
                        value="\n".join(cfg.get("tts_workers", ["localhost"])),
                        lines=4,
                        placeholder="localhost\ns1\ns2",
                    )

                # ── LLM Backend ───────────────────────────────────────────
                gr.Markdown("### LLM Backend")
                cfg_llm_backend = gr.Radio(
                    choices=["local", "claude"],
                    value=cfg.get("llm_backend", "local"),
                    label="Script generation backend",
                )
                with gr.Accordion("Local LLM settings", open=cfg.get("llm_backend", "local") == "local") as local_cfg_group:
                    cfg_local_llm_url = gr.Textbox(
                        label="Local LLM URL",
                        value=cfg.get("local_llm_url", "http://localhost:8000/v1/chat/completions"),
                        placeholder="http://localhost:8000/v1/chat/completions",
                    )
                    cfg_local_llm_model = gr.Textbox(
                        label="Local LLM Model",
                        value=cfg.get("local_llm_model", "openai/gpt-oss-120b"),
                        placeholder="openai/gpt-oss-120b",
                    )
                with gr.Accordion("Claude settings", open=cfg.get("llm_backend", "local") == "claude") as claude_cfg_group:
                    cfg_claude_key = gr.Textbox(
                        label="Claude API Key",
                        value=cfg.get("claude_api_key", ""),
                        placeholder="sk-ant-...",
                        type="password",
                    )
                    cfg_claude_model = gr.Textbox(
                        label="Claude Model",
                        value=cfg.get("claude_model", "claude-sonnet-4-6"),
                        placeholder="claude-sonnet-4-6",
                    )

                # ── FLUX Image Models ─────────────────────────────────────
                gr.Markdown("### FLUX Image Models")
                gr.Markdown(
                    "Used for scene preview images and the video cover/thumbnail. "
                    "Model files must be in each ComfyUI worker's models directory — "
                    "run `make download-flux` to download them."
                )
                with gr.Row():
                    cfg_flux_model = gr.Textbox(
                        label="FLUX UNet (models/unet/)",
                        value=cfg.get("flux_model", "flux1-schnell-fp8.safetensors"),
                        placeholder="flux1-schnell-fp8.safetensors",
                    )
                    cfg_flux_vae = gr.Textbox(
                        label="FLUX VAE (models/vae/)",
                        value=cfg.get("flux_vae", "ae.safetensors"),
                        placeholder="ae.safetensors",
                    )
                with gr.Row():
                    cfg_flux_clip_t5 = gr.Textbox(
                        label="T5 text encoder (models/clip/)",
                        value=cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
                        placeholder="t5xxl_fp8_e4m3fn.safetensors",
                    )
                    cfg_flux_clip_l = gr.Textbox(
                        label="CLIP-L text encoder (models/clip/)",
                        value=cfg.get("flux_clip_l", "clip_l.safetensors"),
                        placeholder="clip_l.safetensors",
                    )
                cfg_flux_steps = gr.Slider(
                    1, 20, value=cfg.get("flux_steps", 4), step=1,
                    label="FLUX steps  —  4 = schnell (fast), 20 = dev (slow)",
                )

                # ── YouTube ───────────────────────────────────────────────
                gr.Markdown("### YouTube")
                gr.Markdown(
                    "Connect to your YouTube channel to fetch comments and post videos. "
                    "See [docs/youtube_setup.md](docs/youtube_setup.md) for OAuth setup."
                )
                cfg_yt_secrets = gr.Textbox(
                    label="client_secrets.json path",
                    value=cfg.get("youtube_client_secrets", ""),
                    placeholder="~/.config/video-generator/client_secrets.json",
                )
                with gr.Row():
                    cfg_yt_privacy = gr.Dropdown(
                        label="Default Privacy",
                        choices=["private", "unlisted", "public"],
                        value=cfg.get("youtube_post_privacy", "private"),
                    )
                    cfg_yt_category = gr.Dropdown(
                        label="Default Category",
                        choices=list(yt.CATEGORY_OPTIONS.keys()),
                        value=next(
                            (k for k, v in yt.CATEGORY_OPTIONS.items() if v == cfg.get("youtube_post_category", "22")),
                            "People & Blogs",
                        ),
                    )
                gr.Markdown("**Automation defaults** — control individual steps of the pipeline:")
                cfg_yt_fully_automated = gr.Checkbox(
                    label="⚡ Fully automated mode (enables all automation below)",
                    value=cfg.get("youtube_fully_automated", False),
                )
                with gr.Row():
                    cfg_yt_auto_fetch = gr.Checkbox(
                        label="Auto fetch & evaluate on startup and after posting",
                        value=cfg.get("youtube_auto_fetch_evaluate", False),
                    )
                    cfg_yt_auto_approve = gr.Checkbox(
                        label="Auto-approve requests (confidence ≥ 70%)",
                        value=cfg.get("youtube_auto_approve_comments", False),
                    )
                with gr.Row():
                    cfg_yt_auto_start = gr.Checkbox(
                        label="Auto-start job when approved",
                        value=cfg.get("youtube_auto_start_job", False),
                    )
                    cfg_yt_auto_script = gr.Checkbox(
                        label="Auto-approve script (skip review)",
                        value=cfg.get("youtube_auto_approve_script", False),
                    )
                    cfg_yt_auto_post = gr.Checkbox(
                        label="Auto-post to YouTube when generation completes",
                        value=cfg.get("youtube_auto_post", False),
                    )
                cfg_description_suffix = gr.Textbox(
                    label="Description Suffix — always appended to every YouTube description",
                    value=cfg.get("description_suffix", ""),
                    lines=3,
                    placeholder="e.g.  Subscribe for more AI documentaries → https://youtube.com/@yourchannel\n\n#documentary #aigenerated",
                )

                gr.Markdown("### Voice Library")
                voices_table = gr.Dataframe(
                    headers=["Name", "Path"],
                    datatype=["str", "str"],
                    value=voices_as_rows(),
                    interactive=False,
                    label="Saved Voices",
                )

                gr.Markdown("**Add voice**")
                with gr.Row():
                    new_voice_name = gr.Textbox(
                        label="Voice Name", placeholder="e.g. My Voice", scale=2
                    )
                    new_voice_file = gr.File(
                        label="Reference WAV / MP3",
                        file_types=[".wav", ".mp3"],
                        type="filepath",
                        scale=3,
                    )
                add_voice_btn = gr.Button("Add Voice", variant="secondary")

                gr.Markdown("**Remove voice**")
                with gr.Row():
                    remove_voice_dd = gr.Dropdown(
                        label="Select voice to remove",
                        choices=[v["name"] for v in cfg.get("voices", [])],
                        value=None,
                        scale=4,
                    )
                    remove_voice_btn = gr.Button(
                        "Remove", variant="stop", scale=1
                    )

        # ── Event wiring ─────────────────────────────────────────────────────

        script_outputs = [
            tabs, script_col,
            script_job_id_state, script_work_dir_state, current_scene_state,
            music_desc_state,
            style_box, style_state,
            script_overview_md,
            scene_picker, scene_info_md,
            scene_title_box, scene_image_prompt_box,
            scene_video_prompt_box, scene_narration_box,
            selected_scene_preview, scene_summary_html,
        ]

        _load_script_outputs = script_outputs + [video_title_in, n_scenes_in]

        # Outputs for the scene image generation step
        img_gen_outputs = [
            image_gen_status,
            selected_scene_preview,
            scene_summary_html,
        ]

        gen_outputs = [
            progress_bar,
            music_audio_out,
            final_video_out,
            combined_state,
            music_state,
            ambient_state,
            tabs,
            active_job_state,
        ]

        progress_timer.tick(
            fn=_poll_job_outputs,
            inputs=[active_job_state],
            outputs=[progress_bar, final_video_out, combined_state,
                     music_state, ambient_state, tabs, active_job_state,
                     post_auto_trigger_state],
            concurrency_limit=1,  # drop extra ticks instead of queuing them up
        )
        progress_timer.tick(
            fn=_orchestration_html,
            inputs=[active_job_state],
            outputs=[orchestration_status],
            concurrency_limit=1,
        )
        progress_timer.tick(
            fn=_poll_cover_image,
            inputs=[active_job_state],
            outputs=[progress_cover_image],
            concurrency_limit=1,
        )
        pause_job_btn.click(
            fn=on_pause_active_job,
            inputs=[active_job_state],
            outputs=[pause_stop_status],
        ).then(
            fn=lambda: gr.update(choices=[(lbl, wdir) for lbl, wdir in _list_resumable_jobs()]),
            inputs=[],
            outputs=[resumable_job_dropdown],
        )
        stop_job_btn.click(
            fn=on_cancel_active_job,
            inputs=[active_job_state],
            outputs=[pause_stop_status],
        )
        load_resume_btn.click(
            fn=on_load_and_resume_job,
            inputs=[resumable_job_dropdown],
            outputs=[pause_stop_status, tabs, active_job_state],
        )
        resume_job_btn.click(
            fn=on_resume_active_job,
            inputs=[active_job_state],
            outputs=[recovery_status, tabs, active_job_state],
        )
        retry_failed_btn.click(
            fn=on_retry_failed_tasks,
            inputs=[active_job_state],
            outputs=[recovery_status],
        )
        cancel_job_btn.click(
            fn=on_cancel_active_job,
            inputs=[active_job_state],
            outputs=[recovery_status],
        )
        mark_worker_online_btn.click(
            fn=lambda wid: on_set_worker_status(wid, "online"),
            inputs=[worker_status_id],
            outputs=[recovery_status],
        )
        mark_worker_offline_btn.click(
            fn=lambda wid: on_set_worker_status(wid, "offline"),
            inputs=[worker_status_id],
            outputs=[recovery_status],
        )

        gen_script_btn.click(
            fn=lambda: gr.update(interactive=False, value="⏳ Generating script…"),
            inputs=[], outputs=[gen_script_btn],
        ).then(
            fn=on_generate_script,
            inputs=[video_title_in, title_in, n_scenes_in, auto_approve_in],
            outputs=script_outputs,
        ).then(
            fn=on_generate_scene_previews,
            inputs=[
                script_job_id_state, current_scene_state, script_resolution_in,
                style_state, scene_title_box, scene_image_prompt_box,
                scene_video_prompt_box, scene_narration_box, auto_approve_in,
            ],
            outputs=img_gen_outputs,
        ).then(
            fn=_auto_generate,
            inputs=[
                video_title_in, title_in, n_scenes_in, voice_dropdown, resolution_in,
                music_desc_state, style_state, auto_approve_in,
                script_job_id_state, script_work_dir_state, current_scene_state,
                scene_title_box, scene_image_prompt_box,
                scene_video_prompt_box, scene_narration_box,
            ],
            outputs=gen_outputs,
        ).then(
            fn=lambda auto: gr.update(selected="progress") if auto else gr.update(),
            inputs=[auto_approve_in], outputs=[tabs],
        ).then(
            fn=lambda: gr.update(interactive=True, value="1. Generate Script →"),
            inputs=[], outputs=[gen_script_btn],
        )

        load_script_btn.click(
            fn=on_load_script,
            inputs=[load_script_dropdown],
            outputs=_load_script_outputs,
        )

        approve_btn.click(
            fn=lambda: gr.update(interactive=False, value="⏳ Generating video…"),
            inputs=[], outputs=[approve_btn],
        ).then(
            fn=lambda: gr.update(selected="progress"),
            inputs=[], outputs=[tabs],
        ).then(
            fn=on_generate,
            inputs=[
                video_title_in, title_in, n_scenes_in, voice_dropdown, resolution_in,
                music_desc_state, style_state, auto_approve_in,
                script_job_id_state, script_work_dir_state, current_scene_state,
                scene_title_box, scene_image_prompt_box,
                scene_video_prompt_box, scene_narration_box,
            ],
            outputs=gen_outputs,
        ).then(
            fn=lambda: gr.update(interactive=True, value="2. Approve & Generate Video →"),
            inputs=[], outputs=[approve_btn],
        )

        # Keep style_state in sync when user edits style_box directly
        style_box.change(fn=lambda v: v, inputs=[style_box], outputs=[style_state])

        # Auto-sync Script-tab preview resolution to match the video resolution.
        resolution_in.change(
            fn=lambda r: r,
            inputs=[resolution_in],
            outputs=[script_resolution_in],
        )

        regen_scene_btn.click(
            fn=on_regen_active_scene,
            inputs=[
                script_job_id_state, current_scene_state, script_resolution_in,
                style_state, scene_title_box, scene_image_prompt_box,
                scene_video_prompt_box, scene_narration_box,
            ],
            outputs=img_gen_outputs,
        )

        scene_nav_inputs = [
            script_job_id_state, current_scene_state,
            scene_title_box, scene_image_prompt_box,
            scene_video_prompt_box, scene_narration_box,
        ]
        scene_nav_outputs = [
            current_scene_state,
            scene_picker, scene_info_md,
            scene_title_box, scene_image_prompt_box,
            scene_video_prompt_box, scene_narration_box,
            selected_scene_preview, scene_summary_html,
        ]
        prev_scene_btn.click(
            fn=lambda job_id, sid, title, ip, vp, nr: _navigate_scene(-1, job_id, sid, title, ip, vp, nr),
            inputs=scene_nav_inputs,
            outputs=scene_nav_outputs,
        ).then(
            fn=on_generate_scene_previews,
            inputs=[
                script_job_id_state, current_scene_state, script_resolution_in,
                style_state, scene_title_box, scene_image_prompt_box,
                scene_video_prompt_box, scene_narration_box,
            ],
            outputs=img_gen_outputs,
        )
        next_scene_btn.click(
            fn=lambda job_id, sid, title, ip, vp, nr: _navigate_scene(1, job_id, sid, title, ip, vp, nr),
            inputs=scene_nav_inputs,
            outputs=scene_nav_outputs,
        ).then(
            fn=on_generate_scene_previews,
            inputs=[
                script_job_id_state, current_scene_state, script_resolution_in,
                style_state, scene_title_box, scene_image_prompt_box,
                scene_video_prompt_box, scene_narration_box,
            ],
            outputs=img_gen_outputs,
        )
        scene_picker.release(
            fn=_jump_scene,
            inputs=[
                scene_picker,
                script_job_id_state, current_scene_state,
                scene_title_box, scene_image_prompt_box,
                scene_video_prompt_box, scene_narration_box,
            ],
            outputs=scene_nav_outputs,
        ).then(
            fn=on_generate_scene_previews,
            inputs=[
                script_job_id_state, current_scene_state, script_resolution_in,
                style_state, scene_title_box, scene_image_prompt_box,
                scene_video_prompt_box, scene_narration_box,
            ],
            outputs=img_gen_outputs,
        )

        remix_btn.click(
            fn=on_remix,
            inputs=[combined_state, music_state, ambient_state,
                    remix_voice_vol, remix_music_vol, remix_ambient_vol,
                    remix_scenes_check],
            outputs=[final_video_out, combined_state, music_state, ambient_state, remix_status],
        )

        load_for_upscale_btn.click(
            fn=on_load_for_upscale,
            inputs=[],
            outputs=[upscale_src_vid, upscale_status],
        )

        upscale_btn.click(
            fn=on_upscale,
            inputs=[upscale_src_vid],
            outputs=[upscale_out_vid, upscale_status],
        )

        restore_btn.click(
            fn=on_restore_session,
            inputs=[],
            outputs=[final_video_out, combined_state, music_state, ambient_state,
                     remix_voice_vol, remix_music_vol, remix_ambient_vol,
                     remix_scenes_check, restore_status],
        )

        _remix_load_outputs = [final_video_out, combined_state, music_state, ambient_state,
                               remix_voice_vol, remix_music_vol, remix_ambient_vol,
                               remix_scenes_check, restore_status]

        load_recent_btn.click(
            fn=_load_job_for_remix,
            inputs=[recent_job_dropdown],
            outputs=_remix_load_outputs,
        )

        import pandas as pd

        def _remix_select_all(df):
            if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                return gr.update()
            rows = df.values.tolist() if isinstance(df, pd.DataFrame) else (df or [])
            updated = [[True, r[1], r[2]] for r in rows]
            return gr.update(value=updated)

        def _remix_deselect_all(df):
            if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                return gr.update()
            rows = df.values.tolist() if isinstance(df, pd.DataFrame) else (df or [])
            updated = [[False, r[1], r[2]] for r in rows]
            return gr.update(value=updated)

        def _remix_reset_order(df):
            if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                return gr.update()
            rows = df.values.tolist() if isinstance(df, pd.DataFrame) else (df or [])
            # Sort by scene number extracted from label, reassign sequential order
            def scene_num(row):
                m = re.search(r'Scene\s+(\d+)', str(row[2]))
                return int(m.group(1)) if m else 999
            rows_sorted = sorted(rows, key=scene_num)
            updated = [[r[0], i + 1, r[2]] for i, r in enumerate(rows_sorted)]
            return gr.update(value=updated)

        remix_select_all_btn.click(
            fn=_remix_select_all,
            inputs=[remix_scenes_check],
            outputs=[remix_scenes_check],
        )

        remix_deselect_all_btn.click(
            fn=_remix_deselect_all,
            inputs=[remix_scenes_check],
            outputs=[remix_scenes_check],
        )

        remix_reset_order_btn.click(
            fn=_remix_reset_order,
            inputs=[remix_scenes_check],
            outputs=[remix_scenes_check],
        )

        cfg_llm_backend.change(
            fn=lambda b: (gr.update(open=(b == "local")), gr.update(open=(b == "claude"))),
            inputs=[cfg_llm_backend],
            outputs=[local_cfg_group, claude_cfg_group],
        )

        _cfg_inputs = [cfg_music_vol, cfg_voice_vol, cfg_ambient_vol,
                       cfg_resolution, cfg_max_clip, cfg_lora,
                       cfg_first_pass_cfg, cfg_first_pass_steps,
                       cfg_second_pass_cfg, cfg_second_pass_steps,
                       cfg_workers, cfg_tts_workers,
                       cfg_llm_backend,
                       cfg_local_llm_url, cfg_local_llm_model,
                       cfg_claude_key, cfg_claude_model,
                       cfg_flux_model, cfg_flux_vae,
                       cfg_flux_clip_t5, cfg_flux_clip_l,
                       cfg_flux_steps,
                       cfg_default_visual_style,
                       cfg_default_voice, cfg_default_n_scenes,
                       cfg_yt_secrets,
                       cfg_yt_auto_fetch, cfg_yt_auto_approve,
                       cfg_yt_auto_start, cfg_yt_auto_script, cfg_yt_auto_post,
                       cfg_yt_fully_automated,
                       cfg_yt_privacy, cfg_yt_category,
                       cfg_script_extra, cfg_description_suffix]

        _cfg_outputs = [cfg_status, voice_dropdown, n_scenes_in]

        for _inp in _cfg_inputs:
            _inp.change(fn=on_save_config, inputs=_cfg_inputs, outputs=_cfg_outputs)

        save_cfg_btn.click(fn=on_save_config, inputs=_cfg_inputs, outputs=_cfg_outputs)

        add_voice_btn.click(
            fn=on_add_voice,
            inputs=[new_voice_name, new_voice_file],
            outputs=[voices_table, voice_dropdown, remove_voice_dd,
                     new_voice_name, new_voice_file, cfg_status],
        )

        remove_voice_btn.click(
            fn=on_remove_voice,
            inputs=[remove_voice_dd],
            outputs=[voices_table, voice_dropdown, remove_voice_dd, cfg_status],
        )

        # ── YouTube tab wiring ────────────────────────────────────────────────

        # State that holds the queue item to auto-start (None = no pending auto-start)
        yt_auto_start_trigger = gr.State(None)

        _yt_comment_outputs = [yt_comments_html, yt_pending_html, yt_queue_html, yt_comments_status]
        _yt_approve_outputs = [yt_comments_html, yt_pending_html, yt_queue_html, yt_action_status]
        _yt_comment_outputs_ext = _yt_comment_outputs + [yt_auto_start_trigger]
        _yt_approve_outputs_ext = _yt_approve_outputs + [yt_auto_start_trigger]

        yt_connect_btn.click(
            fn=on_yt_connect,
            inputs=[],
            outputs=[yt_auth_status, yt_auth_timer],
        )
        yt_auth_timer.tick(
            fn=on_yt_poll_auth,
            inputs=[],
            outputs=[yt_auth_status, yt_auth_timer],
        )
        yt_disconnect_btn.click(
            fn=on_yt_disconnect,
            inputs=[],
            outputs=[yt_auth_status],
        )
        def _refresh_suggestions_html():
            return _suggestions_html(yt.load_suggestions())

        yt_fetch_evaluate_btn.click(
            fn=on_yt_fetch_and_evaluate,
            inputs=[yt_auto_approve_cb],
            outputs=_yt_comment_outputs_ext,
        ).then(
            fn=_refresh_suggestions_html,
            inputs=[],
            outputs=[yt_suggestions_html],
        )
        yt_approve_btn.click(
            fn=on_yt_approve,
            inputs=[yt_row_num, yt_title_override],
            outputs=_yt_approve_outputs_ext,
        )
        yt_reject_btn.click(
            fn=on_yt_reject,
            inputs=[yt_row_num],
            outputs=_yt_approve_outputs,
        )
        yt_start_next_btn.click(
            fn=on_yt_start_next_video,
            inputs=[],
            outputs=[yt_auto_start_trigger],
        )
        yt_remove_queue_btn.click(
            fn=on_yt_remove_from_queue,
            inputs=[yt_queue_row_num],
            outputs=[yt_queue_html, yt_queue_action_status],
        )
        yt_move_up_btn.click(
            fn=lambda row: on_yt_move_in_queue(row, -1),
            inputs=[yt_queue_row_num],
            outputs=[yt_queue_html, yt_queue_action_status],
        )
        yt_move_down_btn.click(
            fn=lambda row: on_yt_move_in_queue(row, 1),
            inputs=[yt_queue_row_num],
            outputs=[yt_queue_html, yt_queue_action_status],
        )
        yt_sort_priority_btn.click(
            fn=on_yt_sort_queue,
            inputs=[],
            outputs=[yt_queue_html, yt_queue_action_status],
        )

        yt_refresh_suggestions_btn.click(
            fn=on_refresh_suggestions,
            inputs=[],
            outputs=[yt_suggestions_html, yt_suggestions_status],
        )
        yt_add_suggestion_btn.click(
            fn=on_add_suggestion_to_queue,
            inputs=[yt_suggestion_row_num],
            outputs=[yt_suggestions_html, yt_queue_html, yt_suggestions_status],
        )
        yt_manual_add_btn.click(
            fn=on_yt_manual_add_to_queue,
            inputs=[yt_manual_title, yt_manual_prompt, yt_manual_n_scenes],
            outputs=[yt_queue_html, yt_manual_status, yt_manual_title, yt_manual_prompt],
        )

        # Auto-start chain: when yt_auto_start_trigger changes, populate Create tab
        # and run the full script→video pipeline unattended.
        yt_auto_start_trigger.change(
            fn=_prepare_auto_start,
            inputs=[yt_auto_start_trigger],
            outputs=[video_title_in, title_in, n_scenes_in, auto_approve_in, resolution_in],
        ).then(
            fn=on_generate_script,
            inputs=[video_title_in, title_in, n_scenes_in, auto_approve_in],
            outputs=script_outputs,
        ).then(
            fn=on_generate_scene_previews,
            inputs=[
                script_job_id_state, current_scene_state, script_resolution_in,
                style_state, scene_title_box, scene_image_prompt_box,
                scene_video_prompt_box, scene_narration_box, auto_approve_in,
            ],
            outputs=img_gen_outputs,
        ).then(
            fn=_auto_generate,
            inputs=[
                video_title_in, title_in, n_scenes_in, voice_dropdown, resolution_in,
                music_desc_state, style_state, auto_approve_in,
                script_job_id_state, script_work_dir_state, current_scene_state,
                scene_title_box, scene_image_prompt_box,
                scene_video_prompt_box, scene_narration_box,
            ],
            outputs=gen_outputs,
        ).then(
            fn=lambda: gr.update(selected="progress"),
            inputs=[], outputs=[tabs],
        ).then(
            fn=lambda: None,  # reset trigger so next auto-start fires cleanly
            inputs=[], outputs=[yt_auto_start_trigger],
        ).then(
            fn=lambda: _auto_start_in_progress.clear(),  # allow next auto-start chain
            inputs=[], outputs=[],
        )

        # Keep YouTube-tab automation checkboxes in sync with Config tab (bidirectional)
        def _sync_auto_flags(*vals):
            fetch, approve, start, script, post, fully = vals
            return fetch, approve, start, script, post, fully

        _auto_flag_yt  = [yt_auto_fetch_cb, yt_auto_approve_cb, yt_auto_start_cb,
                          yt_auto_script_cb, yt_auto_post_cb, yt_fully_automated_cb]
        _auto_flag_cfg = [cfg_yt_auto_fetch, cfg_yt_auto_approve, cfg_yt_auto_start,
                          cfg_yt_auto_script, cfg_yt_auto_post, cfg_yt_fully_automated]

        for _yt_cb, _cfg_cb in zip(_auto_flag_yt, _auto_flag_cfg):
            _yt_cb.change(fn=lambda v: v, inputs=[_yt_cb], outputs=[_cfg_cb])
            _cfg_cb.change(fn=lambda v: v, inputs=[_cfg_cb], outputs=[_yt_cb])

        # "Fully automated" master toggle sets all five individual flags in BOTH tabs,
        # then forces a config save so the values are persisted immediately.
        # Updating both tabs in one step avoids a race where on_save_config reads the
        # old (False) values before the cfg checkboxes have been written.
        def _on_fully_auto(enabled: bool):
            return tuple(gr.update(value=enabled) for _ in range(10))

        _fully_auto_yt_outputs = [
            yt_auto_fetch_cb, yt_auto_approve_cb, yt_auto_start_cb,
            yt_auto_script_cb, yt_auto_post_cb,
            cfg_yt_auto_fetch, cfg_yt_auto_approve, cfg_yt_auto_start,
            cfg_yt_auto_script, cfg_yt_auto_post,
        ]
        _fully_auto_cfg_outputs = [
            cfg_yt_auto_fetch, cfg_yt_auto_approve, cfg_yt_auto_start,
            cfg_yt_auto_script, cfg_yt_auto_post,
            yt_auto_fetch_cb, yt_auto_approve_cb, yt_auto_start_cb,
            yt_auto_script_cb, yt_auto_post_cb,
        ]

        yt_fully_automated_cb.change(
            fn=_on_fully_auto,
            inputs=[yt_fully_automated_cb],
            outputs=_fully_auto_yt_outputs,
        ).then(
            fn=on_save_config,
            inputs=_cfg_inputs,
            outputs=_cfg_outputs,
        )
        cfg_yt_fully_automated.change(
            fn=_on_fully_auto,
            inputs=[cfg_yt_fully_automated],
            outputs=_fully_auto_cfg_outputs,
        ).then(
            fn=on_save_config,
            inputs=_cfg_inputs,
            outputs=_cfg_outputs,
        )

        # ── Post tab wiring ───────────────────────────────────────────────────

        _post_load_outputs = [
            post_video_path, post_title, post_description,
            post_cover_image, post_status_html, post_url_html,
            post_cover_path_state,
        ]

        post_load_btn.click(
            fn=on_post_load,
            inputs=[active_job_state],
            outputs=_post_load_outputs,
        )

        post_job_dropdown.change(
            fn=on_post_load_from_selection,
            inputs=[post_job_dropdown],
            outputs=_post_load_outputs,
        )

        # Single combined tab-select handler — handles per-tab auto-fills in one round-trip.
        # Must be a generator so the Post tab can yield twice (instant + description).
        def _on_tab_select(evt: gr.SelectData, job_dir: str, combined_path: str):
            # evt.value may be the tab label (str) or tab index (int) depending
            # on Gradio version — normalise to str to avoid TypeError in `in` checks.
            selected = str(getattr(evt, "value", "") or "")
            yt_html = (
                on_yt_check_status()
                if ("YouTube" in selected or selected == "youtube")
                else gr.update()
            )
            on_remix_tab = "Remix" in selected or selected == "output"
            recent_choices = (
                gr.update(choices=[(lbl, wdir) for lbl, wdir in _list_recent_jobs()])
                if on_remix_tab
                else gr.update()
            )
            # Auto-populate scene list when switching to Remix tab
            if on_remix_tab and combined_path and Path(combined_path).exists():
                work_dir = Path(combined_path).parent
                df_rows = _get_scene_df_rows(work_dir)
                scenes_update = gr.update(value=df_rows)
            else:
                scenes_update = gr.update()
            load_script_dropdown_update = (
                gr.update(choices=[(lbl, wdir) for lbl, wdir in _list_script_jobs()])
                if ("Create" in selected or selected == "create")
                else gr.update()
            )
            resumable_dropdown_update = (
                gr.update(choices=[(lbl, wdir) for lbl, wdir in _list_resumable_jobs()])
                if ("Progress" in selected or selected == "progress")
                else gr.update()
            )

            on_post_tab = "Post" in selected or selected == "post"
            if not on_post_tab:
                yield (yt_html, recent_choices, scenes_update, gr.update(),
                       load_script_dropdown_update, resumable_dropdown_update,
                       *(gr.update() for _ in _post_load_outputs))
                return

            # Post tab: refresh choices, pick the best job, set the value, and load
            # all content inline so the user sees it immediately without any manual step.
            completed = list(_list_completed_jobs())
            # Prefer the currently active job if it's in the completed list; else take first.
            best_dir = ""
            if job_dir and any(wdir == job_dir for _, wdir in completed):
                best_dir = job_dir
            elif completed:
                best_dir = completed[0][1]

            post_dropdown_update = gr.update(choices=completed, value=best_dir or None)
            non_post_base = (yt_html, recent_choices, scenes_update, post_dropdown_update,
                             load_script_dropdown_update, resumable_dropdown_update)

            if not best_dir:
                yield non_post_base + (
                    gr.update(value=""),
                    gr.update(value="No completed videos found."),
                    gr.update(value=""),
                    gr.update(value=None, visible=False),
                    _post_status_html("No completed videos found. Generate a video first.", "info"),
                    gr.update(value=""),
                    "",
                )
                return

            first = True
            for post_outputs in _post_load_work_dir(Path(best_dir)):
                if first:
                    yield non_post_base + post_outputs
                    first = False
                else:
                    yield (*(gr.update() for _ in non_post_base), *post_outputs)

        tabs.select(
            fn=_on_tab_select,
            inputs=[active_job_state, combined_state],
            outputs=[yt_auth_status, recent_job_dropdown, remix_scenes_check, post_job_dropdown,
                     load_script_dropdown, resumable_job_dropdown,
                     *_post_load_outputs],
        )

        post_regen_desc_btn.click(
            fn=on_post_regen_description,
            inputs=[post_video_path, active_job_state, post_title],
            outputs=[post_description, post_status_html],
        )

        def _on_post_regen_cover(title: str, post_video_path: str, active_job_dir: str):
            # STRICT: cover regen on the Post tab MUST use the video the user is
            # actually viewing. Refuse to fall back to active_job_state — that
            # fallback caused covers to be generated from a completely different
            # video's scenes (e.g. KISS title + Cold War content).
            if not post_video_path or not Path(post_video_path).expanduser().exists():
                yield (
                    _post_status_html(
                        "No video loaded on the Post tab — select a video from the dropdown first.",
                        "error",
                    ),
                    gr.update(),
                    "",
                )
                return
            work_dir = Path(post_video_path).expanduser().parent
            logger.info("Cover regen using work_dir=%s (post_video_path=%s)",
                        work_dir, post_video_path)
            job_cfg = {}
            if (work_dir / "job_config.json").exists():
                try:
                    job_cfg = _read_json(work_dir / "job_config.json")
                except Exception:
                    pass
            style = job_cfg.get("style", "")
            title = (title or job_cfg.get("video_title", "") or work_dir.name).strip()

            worker_urls = _preview_worker_urls()
            if not worker_urls:
                yield _post_status_html("No cluster workers reachable — add workers in Config.", "error"), gr.update(), ""
                return

            cfg = load_config()
            cover_path = work_dir / "cover.png"
            cover_base = work_dir / "cover_base.png"
            scenes_for_cover = _load_scenes_for_work_dir(work_dir)
            logger.info("Cover regen: loaded %d scenes from %s", len(scenes_for_cover), work_dir.name)
            prompt = _cover_prompt(_shorten_title(title), style, scenes=scenes_for_cover)

            yield (
                _post_status_html(
                    f"Generating cover for '{title}' from <code>{html.escape(work_dir.name)}</code> "
                    f"({len(scenes_for_cover)} scenes)…",
                    "info",
                ),
                gr.update(),
                "",
            )

            try:
                worker_pool = WorkerPool(worker_urls)
                url = worker_pool.acquire()
                try:
                    generate_scene_image(
                        prompt, cover_base,
                        width=_COVER_W, height=_COVER_H,
                        steps=int(cfg.get("flux_steps", 4)),
                        flux_model=cfg.get("flux_model", "flux1-schnell-fp8.safetensors"),
                        clip_t5=cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
                        clip_l=cfg.get("flux_clip_l", "clip_l.safetensors"),
                        flux_vae=cfg.get("flux_vae", "ae.safetensors"),
                        comfy_url=url,
                    )
                finally:
                    worker_pool.release(url)
                shutil.copy2(cover_base, cover_path)
                cover_str = str(cover_path)
                yield (
                    _post_status_html("Cover image ready.", "success"),
                    gr.update(value=cover_str, visible=True),
                    cover_str,
                )
            except Exception as exc:
                logger.warning("Post cover regen failed: %s", exc)
                yield _post_status_html(f"Cover generation failed: {str(exc)[:200]}", "error"), gr.update(), ""

        post_regen_cover_btn.click(
            fn=_on_post_regen_cover,
            inputs=[post_title, post_video_path, active_job_state],
            outputs=[post_status_html, post_cover_image, post_cover_path_state],
        )

        post_btn.click(
            fn=on_post_upload,
            inputs=[
                active_job_state, post_video_path, post_title,
                post_description, post_privacy, post_category,
                post_cover_path_state, post_tags,
            ],
            outputs=[post_status_html, post_url_html],
        ).then(
            fn=lambda: (gr.update(selected="youtube"),
                        "⏳ Video posted! Fetching new comments and evaluating requests…"),
            inputs=[],
            outputs=[tabs, yt_comments_status],
        ).then(
            fn=_on_post_done_refetch,
            inputs=[],
            outputs=_yt_comment_outputs_ext,
        ).then(
            fn=_refresh_suggestions_html,
            inputs=[],
            outputs=[yt_suggestions_html],
        )

        # Auto-post: fires when post_auto_trigger_state flips to True
        def _maybe_auto_post(trigger: bool, job_dir: str):
            if trigger:
                yield from _auto_post_chain(job_dir)
            else:
                yield gr.update(), gr.update()

        def _navigate_to_yt_after_autopost(auto_post_active: bool):
            """Navigate to the YouTube tab so the user can see the fetch-evaluate
            cycle happening.  Skipped on the reset-to-False trigger firing."""
            if not auto_post_active:
                return gr.update(), gr.update()
            cfg = load_config()
            if not cfg.get("youtube_auto_fetch_evaluate", False):
                return gr.update(), gr.update()
            return (
                gr.update(selected="youtube"),
                "⏳ Video posted! Fetching new comments and evaluating requests…",
            )

        post_auto_trigger_state.change(
            fn=_maybe_auto_post,
            inputs=[post_auto_trigger_state, active_job_state],
            outputs=[post_status_html, post_url_html],
        ).then(
            # Navigate to YouTube tab so the full loop is visually confirmed.
            # Reads post_auto_trigger_state: if it's already False (the reset
            # firing) this is a no-op, preventing a spurious tab switch.
            fn=_navigate_to_yt_after_autopost,
            inputs=[post_auto_trigger_state],
            outputs=[tabs, yt_comments_status],
        ).then(
            fn=_on_post_done_refetch,
            inputs=[],
            outputs=_yt_comment_outputs_ext,
        ).then(
            fn=_refresh_suggestions_html,
            inputs=[],
            outputs=[yt_suggestions_html],
        ).then(
            # Reset the trigger back to False so the NEXT job's completion
            # produces a False→True transition and fires .change() again.
            # Without this reset the state stays True, and True→True is a
            # no-op in Gradio, breaking the infinite automation loop.
            fn=lambda: False,
            inputs=[],
            outputs=[post_auto_trigger_state],
        )

        # On startup: refresh dropdowns that depend on the videos folder, then
        # fetch & evaluate if auto_fetch_evaluate is configured.
        demo.load(
            fn=lambda: (
                gr.update(choices=[(lbl, wdir) for lbl, wdir in _list_recent_jobs()]),
                gr.update(choices=[(lbl, wdir) for lbl, wdir in _list_script_jobs()]),
                gr.update(choices=[(lbl, wdir) for lbl, wdir in _list_resumable_jobs()]),
            ),
            inputs=[],
            outputs=[recent_job_dropdown, load_script_dropdown, resumable_job_dropdown],
        ).then(
            fn=_on_startup_auto_fetch,
            inputs=[],
            outputs=_yt_comment_outputs_ext,
        ).then(
            fn=_refresh_suggestions_html,
            inputs=[],
            outputs=[yt_suggestions_html],
        )

    return demo


if __name__ == "__main__":
    import argparse

    # Gradio trusts files in os.getcwd() — run from home so ~/videos is always served
    os.chdir(Path.home())

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--share", action="store_true")
    args = parser.parse_args()

    _sync_queue_state_on_startup()

    _bg_thread = threading.Thread(target=_background_auto_post_loop, daemon=True, name="auto-post-bg")
    _bg_thread.start()

    demo = build_ui()
    demo.queue(status_update_rate=2)
    demo.launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        theme=gr.themes.Soft(),
        allowed_paths=[str(OUTPUT_DIR), str(VOICES_DIR)],
    )
