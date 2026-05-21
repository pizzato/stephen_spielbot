#!/usr/bin/env python3
"""Gradio web interface for the AI video generator."""

import concurrent.futures
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

from pipeline.llm import generate_script, Scene, NEGATIVE_PROMPT
from pipeline.comfyui import (
    generate_video_clip, generate_video_continuation, generate_music,
    generate_scene_image, StuckJobError,
)
from pipeline.assembler import (
    _get_duration, concat_clips, mux_video_audio, extract_first_frame,
    extract_last_frame, extract_audio, concat_audio, concatenate_scenes,
    ensure_video_resolution, mix_background_music,
)
from pipeline.orchestrator import DurableStore, job_id_from_work_dir
from pipeline.scene_video import generate_scene_video as _generate_scene_video
from pipeline.worker_pool import WorkerPool, alive_workers

MAX_SCENES    = 100
MAX_CLIP_SECS = 12.0  # LTX 2.3 hard limit (~301 frames at 25fps)
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
    "max_clip_secs": 20,
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
}

F5TTS_DEFAULT_OPTION = "Default (F5-TTS)"

CLUSTER_CONF = Path(__file__).parent / "cluster.conf"
COMFYUI_PORT = 8188

# Thread pool for long blocking operations — keeps SSE alive via heartbeat yields
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)


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
    return OUTPUT_DIR / f"{work_dir.name}.mp4"


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
    if final_path.exists() and final_path.stat().st_size > 10_000:
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

    if not (final_path.exists() and final_path.stat().st_size > 10_000):
        return gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

    try:
        meta = _read_json(_job_meta_path(work_dir))
    except Exception:
        meta = {}
    already_done = meta.get("status") == "done" and meta.get("final_path") == str(final_path)

    amb_str = str(ambient) if ambient.exists() else ""
    if combined.exists() and music.exists():
        save_session(str(combined), str(music), amb_str,
                     load_config().get("voice_vol", 100),
                     load_config().get("music_vol", 18),
                     load_config().get("ambient_vol", 0))
    _write_job_meta(work_dir, status="done", final_path=str(final_path))
    return (
        gr.update(value=str(final_path), visible=True),
        str(combined) if combined.exists() else "",
        str(music) if music.exists() else "",
        amb_str,
        # Select Remix once when a job first completes. After that, let users
        # move around without the polling timer pulling them back to Remix.
        gr.update() if already_done else gr.update(selected="output"),
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
        if active_job_dir:
            work_dir = Path(active_job_dir)
            if work_dir.exists():
                pct, msg = _status_for_work_dir(work_dir)
                return _progress_html(pct, msg)

        candidates = sorted(
            OUTPUT_DIR.glob("*/progress.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return _progress_html(0, "Waiting to start…")
        pct, msg = _status_for_work_dir(candidates[0].parent)
        return _progress_html(pct, msg)
    except Exception:
        return _progress_html(0, "Waiting to start…")


def _poll_job_outputs(active_job_dir: str):
    if not active_job_dir:
        return (_progress_html(0, "Waiting to start…"), gr.update(), gr.update(),
                gr.update(), gr.update(), gr.update())
    work_dir = Path(active_job_dir)
    final, comb, mus, amb, tabs_upd = _collect_job_outputs(work_dir)
    return (_poll_progress(active_job_dir), final, comb, mus, amb, tabs_upd)


def _orchestration_html(active_job_dir: str = "") -> str:
    """Render durable job/task/worker state for the Progress tab."""
    store = None
    try:
        store = DurableStore.default()
        job = store.get_job_by_work_dir(active_job_dir) if active_job_dir else None
        if job is None:
            recent = store.recent_jobs(limit=1)
            job = recent[0] if recent else None
        if job is None:
            return (
                '<div style="font-size:13px;color:#6b7280;padding:6px 0">'
                "No durable jobs recorded yet.</div>"
            )

        summary = store.job_summary(job["id"])
        counts = summary["counts"]
        tasks = store.task_rows(job["id"])
        workers = store.worker_rows()
        count_text = " &middot; ".join(
            f"{html.escape(str(k))}: {v}" for k, v in sorted(counts.items())
        ) or "no tasks"

        task_rows = []
        for row in tasks:
            err = row["error"] or ""
            task_rows.append(
                "<tr>"
                f"<td>{html.escape(row['name'])}</td>"
                f"<td>{html.escape(row['status'])}</td>"
                f"<td>{html.escape(row['worker_kind'])}</td>"
                f"<td>{int(row['attempt'])}/{int(row['max_attempts'])}</td>"
                f"<td>{html.escape(err[:120])}</td>"
                "</tr>"
            )

        worker_rows = []
        for row in workers:
            active = row["active_task_id"] or ""
            worker_rows.append(
                "<tr>"
                f"<td>{html.escape(row['kind'])}</td>"
                f"<td>{html.escape(row['endpoint'])}</td>"
                f"<td>{html.escape(row['status'])}</td>"
                f"<td>{html.escape(active[-36:])}</td>"
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
        <table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px">
          <thead><tr style="text-align:left;color:#6b7280">
            <th>Kind</th><th>Endpoint</th><th>Status</th><th>Active Task</th>
          </tr></thead>
          <tbody>{''.join(worker_rows) or '<tr><td colspan="4">No workers registered yet.</td></tr>'}</tbody>
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


# ── TTS wrapper ──────────────────────────────────────────────────────────────

def _tts(text: str, out: Path, voice_ref: str | None, host: str = "localhost") -> None:
    from pipeline.tts_worker import generate_narration
    ref = Path(voice_ref) if voice_ref and Path(voice_ref).exists() else None
    generate_narration(text, out, reference_wav=ref, host=host)


# ── Script generation ────────────────────────────────────────────────────────

def on_generate_script(title: str, n_scenes: int, auto_approve: bool):
    if not title.strip():
        raise gr.Error("Please describe what you want to create.")

    logger.info("on_generate_script — title=%r n_scenes=%d auto_approve=%s",
                title, n_scenes, auto_approve)

    _no_op = (gr.update(),) * 17

    # Run generate_script in a thread so we can yield keep-alives while waiting.
    # Without this, long Claude API calls (30 scenes ≈ 3 min) cause Gradio's
    # WebSocket to time out and the page resets to the Create tab.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(generate_script, title.strip(), int(n_scenes))
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

        work_dir = _script_work_dir(title.strip())
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
                title.strip(),
                config={"title": title.strip(), "phase": "script_review"},
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
            f"### {title.strip()}\n\n{len(scenes_list)} scenes · {work_dir.name}",
            *scene_outputs,
        )
        logger.info("on_generate_script returning %d scenes, next_tab=%r", len(scenes), next_tab)
        yield result
    except Exception as e:
        logger.exception("on_generate_script failed assembling return value")
        gr.Warning(f"Failed to process script: {str(e)[:200]}")
        yield _no_op


# ── Scene image generation — one active scene only ───────────────────────────

_IMG_GEN_OUT_COUNT = 2


def on_regen_active_scene(job_id: str, scene_id: int, resolution: str, style: str,
                          title: str, image_prompt: str, video_prompt: str, narration: str):
    """Regenerate the FLUX preview image for the active scene only."""
    no_op = (gr.update(),) * _IMG_GEN_OUT_COUNT
    if not job_id:
        yield no_op
        return

    _save_active_scene(job_id, scene_id, title, image_prompt, video_prompt, narration)
    cfg = load_config()
    all_workers = cfg.get("comfy_workers", [])
    cluster_urls = [u for u in all_workers
                    if not any(lh in u for lh in ("localhost", "127.0.0.1"))]
    try:
        worker_urls = alive_workers(cluster_urls)
    except Exception as exc:
        logger.warning("Scene image generation worker probe failed: %s", exc)
        worker_urls = []
    if not worker_urls:
        status_html = (
            '<div style="color:#f59e0b;padding:4px 0;font-size:13px">'
            'No cluster workers reachable - cannot regenerate this scene image.</div>'
        )
        yield (gr.update(value=status_html, visible=True), gr.update())
        return

    work_dir = _job_work_dir(job_id) or _script_work_dir(title or "preview")
    out = work_dir / f"scene_{int(scene_id):02d}_preview.png"
    flux_model = cfg.get("flux_model", "flux1-schnell-fp8.safetensors")
    flux_clip_t5 = cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors")
    flux_clip_l = cfg.get("flux_clip_l", "clip_l.safetensors")
    flux_vae = cfg.get("flux_vae", "ae.safetensors")
    flux_steps = int(cfg.get("flux_steps", 4))
    img_width, img_height = _RESOLUTIONS.get(
        resolution or cfg.get("resolution", _DEFAULT_RESOLUTION), (1024, 576)
    )
    style_clean = style.strip().rstrip(".") if style and style.strip() else ""
    prompt = f"{style_clean}. {image_prompt}" if style_clean else image_prompt

    yield (
        gr.update(
            value=f'<div style="color:#7c3aed;padding:4px 0;font-size:13px">'
                  f'Generating scene {int(scene_id)} preview...</div>',
            visible=True,
        ),
        gr.update(),
    )

    worker_pool = WorkerPool(worker_urls)
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
            store.update_scene_preview(job_id, int(scene_id), out)
        finally:
            store.close()
        yield (
            gr.update(value="", visible=False),
            gr.update(value=str(out), visible=True, label="Scene Preview (first frame)"),
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
        )
    finally:
        worker_pool.release(url)


# ── Video generation — generator, yields progressive UI updates ──────────────

# gen_outputs count: progress, music, final, combined_state, music_state,
# ambient_state, tabs, active_job
_GEN_OUT_COUNT = 8


def on_generate(title, n_scenes_val, voice_name, resolution, music_desc, style, auto_approve,
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
        scenes = [
            Scene(
                id=int(row["id"]),
                title=row.get("title") or f"Scene {int(row['id'])}",
                image_prompt=(
                    f"{style_clean}. {row.get('image_prompt') or title}"
                    if style_clean
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
        job_cfg["default_voice"] = voice_name
        job_cfg["voice_ref"] = voice_ref or ""
        job_cfg["music_desc"] = music_desc or ""
        job_cfg["title"] = title
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


def _auto_generate(title, n_scenes_val, voice_name, resolution, music_desc, style, auto_approve,
                   job_id: str, work_dir_str: str, current_scene_id: int,
                   scene_title: str, image_prompt: str, video_prompt: str, narration: str):
    if not auto_approve:
        yield (gr.update(),) * _GEN_OUT_COUNT
        return
    yield from on_generate(
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

def on_restore_session():
    session = load_session()
    if not session:
        return (gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(), gr.update(), gr.update(),
                "No saved session found.")
    # Locate the most recent final video in the same work directory
    combined = Path(session["combined"])
    work_dir = combined.parent
    candidates = sorted(work_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    final_candidates = [p for p in candidates if not p.name.startswith("scene_")
                        and not p.name.startswith("remixed")]
    final_vid = final_candidates[0] if final_candidates else None

    logger.info("Restoring session from %s", work_dir)
    return (
        gr.update(value=str(final_vid), visible=True) if final_vid else gr.update(visible=False),
        session["combined"],          # → combined_state
        session["music"],             # → music_state
        session.get("ambient", ""),   # → ambient_state
        gr.update(value=session.get("voice_vol", 100)),    # → remix_voice_vol
        gr.update(value=session.get("music_vol", 18)),     # → remix_music_vol
        gr.update(value=session.get("ambient_vol", 0)),    # → remix_ambient_vol
        f"Session restored from {work_dir.name}",
    )


# ── Remix ────────────────────────────────────────────────────────────────────

def on_remix(combined_path_str: str, music_path_str: str, ambient_path_str: str,
             voice_vol_pct: float, music_vol_pct: float, ambient_vol_pct: float):
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

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out   = combined.parent / f"remixed-{stamp}.mp4"
    logger.info("Remixing: voice=%.0f%% music=%.0f%% ambient=%.0f%% combined=%s",
                voice_vol_pct, music_vol_pct, ambient_vol_pct, combined.name)
    try:
        mix_background_music(
            combined, music_path, out,
            volume=music_vol_pct / 100.0,
            voice_volume=voice_vol_pct / 100.0,
            ambient_path=ambient_path,
            ambient_volume=ambient_vol_pct / 100.0,
        )
    except Exception as e:
        logger.exception("on_remix failed")
        first_line = str(e).split("\n")[0][:200]
        return (
            gr.update(),
            str(combined_path_str),
            str(music_path_str),
            str(ambient_path_str),
            f"❌ Remix failed: {first_line}",
        )
    size_mb = out.stat().st_size / 1024 / 1024
    logger.info("Remix done: %s (%.1f MB)", out.name, size_mb)
    return (
        gr.update(value=str(out), visible=True),
        str(combined_path_str),   # → combined_state
        str(music_path_str),      # → music_state
        str(ambient_path_str),    # → ambient_state
        f"Remixed: {out.name} ({size_mb:.1f} MB)",
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
                   flux_steps: int):
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
    save_config(cfg)
    logger.info("Config saved: lora=%.2f workers=%s tts=%s",
                lora_strength, cfg["comfy_workers"], cfg["tts_workers"])
    return f"Settings saved ✓  ({len(cfg['comfy_workers'])} video, {len(cfg['tts_workers'])} TTS worker(s))"


# ── UI ────────────────────────────────────────────────────────────────────────

_PERSIST_JS = """
() => {
    const KEY = 'spielbot_title';
    function setup() {
        const el = document.getElementById('title_input');
        if (!el) { setTimeout(setup, 150); return; }
        const ta = el.querySelector('textarea');
        if (!ta) { setTimeout(setup, 150); return; }

        // Save on every keystroke
        ta.addEventListener('input', () => { if (ta.value) localStorage.setItem(KEY, ta.value); });

        // Restore: poll until value is stable (Gradio has finished initialising)
        const saved = localStorage.getItem(KEY);
        if (!saved) return;
        let prev = ta.value, ticks = 0;
        const iv = setInterval(() => {
            if (ta.value === prev) {
                ticks++;
                if (ticks >= 4) {          // stable for ~400 ms
                    clearInterval(iv);
                    if (!ta.value) {       // only restore if Gradio left it empty
                        ta.value = saved;
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }
            } else {
                prev = ta.value; ticks = 0;
            }
        }, 100);
    }
    setup();
    return [];
}
"""


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
        combined_state      = gr.State("")
        music_state         = gr.State("")
        ambient_state       = gr.State("")
        music_desc_state    = gr.State("")
        style_state         = gr.State("")
        script_job_id_state = gr.State("")
        script_work_dir_state = gr.State("")
        current_scene_state = gr.State(1)
        active_job_state    = gr.State("")

        with gr.Tabs(elem_id="main_tabs") as tabs:

            # ── Create ───────────────────────────────────────────────────
            with gr.Tab("🎬 Create", id="create"):
                with gr.Row():
                    with gr.Column(scale=3):
                        title_in = gr.Textbox(
                            label="Describe what you want to create",
                            placeholder="e.g.  The History of the Roman Empire",
                            elem_id="title_input",
                        )
                    with gr.Column(scale=1):
                        n_scenes_in = gr.Slider(1, MAX_SCENES, value=5, step=1, label="Scenes")

                with gr.Row():
                    voice_dropdown = gr.Dropdown(
                        label="Narrator Voice",
                        choices=get_voice_choices(),
                        value=F5TTS_DEFAULT_OPTION,
                    )
                    resolution_in = gr.Dropdown(
                        label="Resolution",
                        choices=list(_RESOLUTIONS.keys()),
                        value=cfg.get("resolution", _DEFAULT_RESOLUTION),
                    )
                with gr.Row():
                    auto_approve_in = gr.Checkbox(
                        label="Auto-approve script (skip review)", value=False,
                    )

                gen_script_btn = gr.Button(
                    "1. Generate Script →", variant="primary", size="lg"
                )

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

                gr.Markdown("#### Background Music")
                music_audio_out = gr.Audio(
                    label="Background Music", visible=False, interactive=False
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

            # ── Config ───────────────────────────────────────────────────
            with gr.Tab("⚙️ Config", id="config"):
                gr.Markdown("### Default Volume Settings")
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

                gr.Markdown("### Generation Quality & Speed")
                cfg_resolution = gr.Dropdown(
                    choices=list(_RESOLUTIONS.keys()),
                    value=cfg.get("resolution", _DEFAULT_RESOLUTION),
                    label="Resolution  —  higher = better quality, slower",
                )
                with gr.Row():
                    cfg_max_clip = gr.Slider(
                        3, 20, value=cfg.get("max_clip_secs", 20), step=1,
                        label="Max Clip Duration (s)  —  shorter = faster per segment",
                    )
                    cfg_lora = gr.Slider(
                        0.0, 1.0, value=cfg.get("lora_strength", 0.5), step=0.05,
                        label="LoRA Strength  —  0 = pure dev model, 0.5–1.0 = distilled",
                    )
                gr.Markdown(
                    "**First-pass (distilled LoRA mode)** — "
                    "LoRA Strength > 0 with Steps=8, CFG=1.0 is the fast distilled mode. "
                    "To use the pure dev model: set LoRA Strength=0, Steps=20–30, CFG=3–5."
                )
                with gr.Row():
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
                        label="Refinement CFG  —  1.0 = current default, 3–5 = more detail",
                    )
                    cfg_second_pass_steps = gr.Slider(
                        3, 8, value=cfg.get("second_pass_steps", 6), step=1,
                        label="Refinement Steps  —  3 = fast, 6 = balanced, 8 = best",
                    )

                gr.Markdown("### ComfyUI Workers")
                gr.Markdown(
                    "One URL per line. Each worker handles one video generation job at a time. "
                    "Scenes are distributed across all online workers in parallel. "
                    "Run `bash scripts/install_comfyui_worker.sh <hostname>` to provision a new worker."
                )
                cfg_workers = gr.Textbox(
                    label="ComfyUI Worker URLs (one per line)",
                    value="\n".join(cfg.get("comfy_workers", ["http://localhost:8188"])),
                    lines=5,
                    placeholder="http://localhost:8188\nhttp://s1:8188\nhttp://s2:8188",
                )
                gr.Markdown("### TTS Workers")
                gr.Markdown(
                    "Hostnames for parallel narration generation. "
                    "Each host must have F5-TTS installed in `~/f5tts-env`."
                )
                cfg_tts_workers = gr.Textbox(
                    label="TTS Hosts (one per line)",
                    value="\n".join(cfg.get("tts_workers", ["localhost"])),
                    lines=4,
                    placeholder="localhost\ns1\ns2",
                )

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

                gr.Markdown("### Scene Preview Images (FLUX.1-schnell)")
                gr.Markdown(
                    "FLUX.1-schnell generates a first-frame image for every scene before video generation. "
                    "Video is always I2V (image-to-video) — T2V is never used. "
                    "The checkbox in the Create tab controls whether images are shown here; "
                    "generation always runs. Model files must be in ComfyUI's models directory — "
                    "run `make download-flux` to download them."
                )
                with gr.Row():
                    cfg_flux_model = gr.Textbox(
                        label="FLUX UNet model (models/unet/)",
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
                with gr.Row():
                    cfg_flux_steps = gr.Slider(
                        1, 20, value=cfg.get("flux_steps", 4), step=1,
                        label="FLUX steps  —  4 = schnell (fast), 20 = dev (slow)",
                    )

                save_cfg_btn = gr.Button("Save Defaults", variant="secondary", visible=False)
                cfg_status   = gr.Markdown("")

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

        # Outputs for the scene image generation step
        img_gen_outputs = [
            image_gen_status,
            selected_scene_preview,
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
                     music_state, ambient_state, tabs],
        )
        progress_timer.tick(
            fn=_orchestration_html,
            inputs=[active_job_state],
            outputs=[orchestration_status],
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
            inputs=[title_in, n_scenes_in, auto_approve_in],
            outputs=script_outputs,
        ).then(
            fn=_auto_generate,
            inputs=[
                title_in, n_scenes_in, voice_dropdown, resolution_in,
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

        approve_btn.click(
            fn=lambda: gr.update(interactive=False, value="⏳ Generating video…"),
            inputs=[], outputs=[approve_btn],
        ).then(
            fn=lambda: gr.update(selected="progress"),
            inputs=[], outputs=[tabs],
        ).then(
            fn=on_generate,
            inputs=[
                title_in, n_scenes_in, voice_dropdown, resolution_in,
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

        # Bidirectional sync between Create-tab resolution and Script-tab preview resolution
        script_resolution_in.change(
            fn=lambda v: v, inputs=[script_resolution_in], outputs=[resolution_in]
        )
        resolution_in.change(
            fn=lambda v: v, inputs=[resolution_in], outputs=[script_resolution_in]
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
        )
        next_scene_btn.click(
            fn=lambda job_id, sid, title, ip, vp, nr: _navigate_scene(1, job_id, sid, title, ip, vp, nr),
            inputs=scene_nav_inputs,
            outputs=scene_nav_outputs,
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
        )

        remix_btn.click(
            fn=on_remix,
            inputs=[combined_state, music_state, ambient_state,
                    remix_voice_vol, remix_music_vol, remix_ambient_vol],
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
                     restore_status],
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
                       cfg_flux_steps]

        for _inp in _cfg_inputs:
            _inp.change(fn=on_save_config, inputs=_cfg_inputs, outputs=[cfg_status])

        save_cfg_btn.click(fn=on_save_config, inputs=_cfg_inputs, outputs=[cfg_status])

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

        demo.load(fn=None, js=_PERSIST_JS)

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

    demo = build_ui()
    demo.queue(status_update_rate=2)
    demo.launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        theme=gr.themes.Soft(),
        allowed_paths=[str(OUTPUT_DIR), str(VOICES_DIR)],
    )
