#!/usr/bin/env python3
"""Standalone resume script — resumes video generation without Gradio.

Usage:
    .venv/bin/python resume_generation.py <work_dir>
"""
import concurrent.futures
import json
import logging
import logging.handlers
import shutil
import sys
import time
from pathlib import Path

import yaml

LOG_DIR = Path.home() / ".local" / "share" / "video-generator" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"  # Append to same log as main app

_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_fmt)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_log_fmt)
logging.basicConfig(level=logging.WARNING, handlers=[_file_handler, _stream_handler], force=True)
logger = logging.getLogger("video_gen")
logger.setLevel(logging.DEBUG)

from PIL import Image as _PILImage

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.llm import Scene
from pipeline import prompts as _prompts
from pipeline.comfyui import generate_music, generate_scene_image, ltx_dimensions, StuckJobError
from pipeline.assembler import (
    _get_duration, mux_video_audio,
    concat_audio, concatenate_scenes,
    ensure_video_resolution, mix_background_music,
)
from pipeline.tts_worker import generate_narration
from pipeline.orchestrator import (
    DurableStore, TaskRun,
    JOB_DONE, JOB_ERROR, JOB_RUNNING,
    job_id_from_work_dir, task_id, worker_id,
)
from pipeline.scene_video import generate_scene_video as _generate_scene_video
from pipeline.worker_pool import WorkerPool, alive_workers
# Resolution name → (w, h) map. Import the canonical table from app rather than
# keeping a copy here — a stale local copy silently dropped the 720p tier and
# rendered every 720p job at the 1920×1080 fallback (wrong size and orientation).
from app import _RESOLUTIONS, _DEFAULT_RESOLUTION
from pipeline.cover import (
    build_cover_prompt as _cover_prompt,
    cover_dimensions as _cover_dimensions,
    shorten_title_for_cover as _shorten_title,
)

CONFIG_FILE = Path.home() / ".config" / "video-generator" / "config.yaml"
OUTPUT_DIR  = Path.home() / "videos"


def _image_matches_resolution(path: Path, width: int, height: int) -> bool:
    """Return True only if *path* is a readable image with exactly width×height pixels."""
    try:
        with _PILImage.open(path) as img:
            return img.size == (width, height)
    except Exception:
        return False

_WORKER_ERR_KEYWORDS = ("timed out", "not reachable", "URLError", "Connection refused",
                        "ConnectionRefused", "RemoteDisconnected")
_PROGRESS_STORE: DurableStore | None = None
_PROGRESS_JOB_ID: str | None = None


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return yaml.safe_load(CONFIG_FILE.read_text()) or {}
    raise RuntimeError(f"Config not found: {CONFIG_FILE}")


def load_job_config(work_dir: Path) -> dict:
    cfg = load_config()
    job_cfg = work_dir / "job_config.json"
    if job_cfg.exists():
        cfg.update(json.loads(job_cfg.read_text()))
    return cfg


def _heal_empty_scenes(scenes: list[Scene], title: str, cfg: dict, work_dir: Path) -> None:
    """Fill missing narration/image_prompt/video_prompt fields before audio generation.

    A scene with empty narration would produce a 0-byte audio clip and a silent video.
    This recovers from any upstream save bug by calling the LLM to fill what's missing,
    and falls back to scene title text so generation NEVER produces a silent scene.
    """
    bad = [s for s in scenes if not (s.narration or "").strip()
           or not (s.image_prompt or "").strip()]
    if not bad:
        return
    logger.warning("Self-heal: %d scene(s) with empty fields — filling: %s",
                   len(bad), [(s.id, not s.narration, not s.image_prompt) for s in bad])

    video_title = cfg.get("video_title") or cfg.get("title") or title
    # Build minimal context from neighbouring scenes' narrations so the LLM keeps continuity.
    backend = cfg.get("llm_backend", "claude")
    try:
        if backend == "claude" and cfg.get("claude_api_key"):
            _fill_via_claude(scenes, title, video_title, cfg)
        else:
            _fill_via_local(scenes, title, video_title, cfg)
    except Exception as exc:
        logger.warning("Self-heal LLM fill failed: %s — falling back to title text", exc)

    # Absolute last resort: never leave a scene with empty narration or image_prompt.
    for s in scenes:
        if not (s.narration or "").strip():
            s.narration = f"{s.title or f'Scene {s.id}'}."
            logger.warning("Self-heal: scene %d narration still empty after LLM — used title", s.id)
        if not (s.image_prompt or "").strip():
            s.image_prompt = s.title or f"Scene {s.id}: {title}"
            logger.warning("Self-heal: scene %d image_prompt still empty after LLM — used title", s.id)
        if not (s.video_prompt or "").strip():
            s.video_prompt = s.image_prompt

    # Persist the healed script so the next resume doesn't have to redo this work.
    try:
        (work_dir / "script.json").write_text(json.dumps([
            {"id": s.id, "title": s.title, "image_prompt": s.image_prompt,
             "video_prompt": s.video_prompt, "narration": s.narration}
            for s in scenes
        ], indent=2))
        logger.info("Self-heal: rewrote script.json with %d filled scenes", len(scenes))
    except Exception as exc:
        logger.warning("Self-heal: could not rewrite script.json: %s", exc)


def _fill_via_claude(scenes: list[Scene], title: str, video_title: str, cfg: dict) -> None:
    """Use Claude to fill empty narration/image_prompt fields per scene."""
    import anthropic, httpx
    api_key = cfg.get("claude_api_key", "")
    if not api_key:
        return
    model = cfg.get("claude_model", "claude-sonnet-4-6")
    timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=30.0)
    client = anthropic.Anthropic(api_key=api_key, http_client=httpx.Client(http2=False, timeout=timeout))
    for s in scenes:
        if (s.narration or "").strip() and (s.image_prompt or "").strip():
            continue
        prev_narr = next((p.narration for p in scenes if p.id == s.id - 1 and p.narration), "")
        next_narr = next((p.narration for p in scenes if p.id == s.id + 1 and p.narration), "")
        ctx = [f'Video topic: "{video_title}"', f'Scene {s.id} title: "{s.title or "(no title)"}"']
        if prev_narr: ctx.append(f'Previous scene: "{prev_narr}"')
        if next_narr: ctx.append(f'Next scene: "{next_narr}"')
        need = []
        if not (s.narration or "").strip():
            need.append('"narration": exactly 2 sentences, ~18-22 words, calm documentary tone')
        if not (s.image_prompt or "").strip():
            need.append('"image_prompt": 60-100 word static scene description for FLUX, no motion verbs')
        if not need:
            continue
        try:
            with client.messages.stream(
                model=model, max_tokens=400,
                system=_prompts.system("heal_claude"),
                messages=[{"role": "user", "content": _prompts.user(
                    "heal_claude",
                    ctx="\n".join(ctx),
                    needed_keys=", ".join(need),
                )}],
            ) as stream:
                text = "".join(stream.text_stream).strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            data = json.loads(text)
            if not (s.narration or "").strip() and data.get("narration"):
                s.narration = data["narration"].strip()
                logger.info("Self-heal: filled scene %d narration via Claude: %r", s.id, s.narration[:60])
            if not (s.image_prompt or "").strip() and data.get("image_prompt"):
                s.image_prompt = data["image_prompt"].strip()
                logger.info("Self-heal: filled scene %d image_prompt via Claude", s.id)
        except Exception as exc:
            logger.warning("Self-heal: Claude fill failed for scene %d: %s", s.id, exc)


def _fill_via_local(scenes: list[Scene], title: str, video_title: str, cfg: dict) -> None:
    """Use the local LLM to fill empty narration/image_prompt fields per scene."""
    import urllib.request
    url = cfg.get("local_llm_url", "http://localhost:8000/v1/chat/completions")
    model = cfg.get("local_llm_model", "openai/gpt-oss-120b")
    for s in scenes:
        if (s.narration or "").strip() and (s.image_prompt or "").strip():
            continue
        ctx = [f'Video topic: "{video_title}"', f'Scene {s.id} title: "{s.title or "(no title)"}"']
        prev_narr = next((p.narration for p in scenes if p.id == s.id - 1 and p.narration), "")
        next_narr = next((p.narration for p in scenes if p.id == s.id + 1 and p.narration), "")
        if prev_narr: ctx.append(f'Previous scene: "{prev_narr}"')
        if next_narr: ctx.append(f'Next scene: "{next_narr}"')
        if not (s.narration or "").strip():
            payload = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": _prompts.system("heal_local_narration")},
                    {"role": "user", "content": _prompts.user("heal_local_narration", ctx="\n".join(ctx))},
                ],
                "max_tokens": 120, "temperature": 0.7,
            }).encode()
            try:
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                text = data["choices"][0]["message"]["content"].strip()
                if text:
                    s.narration = text
                    logger.info("Self-heal: filled scene %d narration via local LLM: %r", s.id, text[:60])
            except Exception as exc:
                logger.warning("Self-heal: local LLM narration fill failed for scene %d: %s", s.id, exc)
        if not (s.image_prompt or "").strip():
            payload = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": _prompts.system("heal_local_image_prompt")},
                    {"role": "user", "content": _prompts.user("heal_local_image_prompt", ctx="\n".join(ctx[:2]))},
                ],
                "max_tokens": 250, "temperature": 0.7,
            }).encode()
            try:
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                text = data["choices"][0]["message"]["content"].strip()
                if text:
                    s.image_prompt = text
                    logger.info("Self-heal: filled scene %d image_prompt via local LLM", s.id)
            except Exception as exc:
                logger.warning("Self-heal: local LLM image_prompt fill failed for scene %d: %s", s.id, exc)


def write_progress(status_file: Path, pct: float, msg: str) -> None:
    try:
        status_file.write_text(json.dumps({"pct": round(pct, 1), "msg": msg, "ts": time.time()}))
    except Exception:
        pass
    try:
        if _PROGRESS_STORE is not None and _PROGRESS_JOB_ID is not None:
            status = JOB_DONE if pct >= 100 else JOB_RUNNING
            _PROGRESS_STORE.update_job(
                _PROGRESS_JOB_ID,
                status=status,
                progress_pct=pct,
                progress_message=msg,
            )
    except Exception:
        logger.debug("Could not mirror progress to durable store", exc_info=True)
    logger.info("PROGRESS %.0f%% — %s", pct, msg)


def main(work_dir: Path) -> None:
    global _PROGRESS_STORE, _PROGRESS_JOB_ID
    cfg = load_job_config(work_dir)

    # Load script
    script_data = json.loads((work_dir / "script.json").read_text())
    scenes = [
        Scene(
            id=s["id"],
            title=s["title"],
            image_prompt=s.get("image_prompt") or s.get("visual_prompt", s["title"]),
            video_prompt=s.get("video_prompt") or s.get("visual_prompt", s["title"]),
            narration=s.get("narration", ""),
        )
        for s in script_data
    ]
    n = len(scenes)
    logger.info("Loaded %d scenes from %s", n, work_dir / "script.json")
    title = cfg.get("title") or (scenes[0].title.split(":")[0] if scenes else work_dir.name)

    # Self-heal: any scene with empty narration would produce a 0-byte audio clip
    # and a silent video. Fill missing fields from the LLM before audio generation.
    _heal_empty_scenes(scenes, title, cfg, work_dir)

    store = DurableStore.default()
    durable_job_id = job_id_from_work_dir(work_dir)
    _PROGRESS_STORE = store
    _PROGRESS_JOB_ID = durable_job_id

    # Config
    music_vol         = cfg.get("music_vol", 18) / 100.0
    voice_vol         = cfg.get("voice_vol", 100) / 100.0
    voice_name        = cfg.get("default_voice", "Thomas")
    voice_robotic     = bool(cfg.get("voice_robotic", cfg.get("default_voice_robotic", False)))
    voice_robotic_amount = float(cfg.get("voice_robotic_amount", cfg.get("default_voice_robotic_amount", 0.35)))
    voice_ref_str     = None
    for v in cfg.get("voices", []):
        if v["name"] == voice_name:
            voice_ref_str = v["path"]
            break
    max_clip_secs     = float(cfg.get("max_clip_secs", 12.0))
    lora_strength     = float(cfg.get("lora_strength", 0.5))
    first_pass_cfg    = float(cfg.get("first_pass_cfg", 1.0))
    first_pass_steps  = int(cfg.get("first_pass_steps", 8))
    second_pass_cfg   = float(cfg.get("second_pass_cfg", 1.5))
    second_pass_steps = int(cfg.get("second_pass_steps", 3))
    flux_cfg = {
        "model":  cfg.get("flux_model",   "flux1-schnell-fp8.safetensors"),
        "clip_t5": cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
        "clip_l":  cfg.get("flux_clip_l",  "clip_l.safetensors"),
        "vae":     cfg.get("flux_vae",     "ae.safetensors"),
        "steps":   int(cfg.get("flux_steps", 4)),
    }
    tts_hosts     = cfg.get("tts_workers", [])
    worker_urls   = alive_workers(cfg.get("comfy_workers", []))

    res_name = cfg.get("resolution", _DEFAULT_RESOLUTION)
    vid_width, vid_height = _RESOLUTIONS.get(res_name, _RESOLUTIONS[_DEFAULT_RESOLUTION])
    # Snap to LTX's renderable grid so the FLUX first frame, the LTX clips and the
    # final video are all the same size (no silent shrink + rescale). See ltx_dimensions.
    vid_width, vid_height = ltx_dimensions(vid_width, vid_height)
    logger.info("Resolution: %s → %dx%d", res_name, vid_width, vid_height)
    logger.info("Workers: %s", worker_urls)
    logger.info("TTS hosts: %s", tts_hosts)

    plan_cfg = {
        **cfg,
        "title": title,
        "resolution": res_name,
        "vid_width": vid_width,
        "vid_height": vid_height,
        "voice_ref": voice_ref_str or "",
        "voice_robotic": voice_robotic,
        "voice_robotic_amount": voice_robotic_amount,
    }
    store.ensure_generation_plan(durable_job_id, work_dir, title, scenes, plan_cfg)
    store.recover_incomplete_tasks(durable_job_id)
    store.update_job(durable_job_id, status=JOB_RUNNING, progress_pct=0, progress_message="starting")

    worker_pool = WorkerPool(worker_urls)
    status_file = work_dir / "progress.json"

    # Work dir name → derive slug and stamp for final output path
    dir_name = work_dir.name
    # Find slug and stamp — the format is {slug}-{stamp} where stamp is YYYYMMDD-HHMMSS
    import re
    m = re.match(r"^(.+)-(\d{8}-\d{6})$", dir_name)
    if m:
        slug, stamp = m.group(1), m.group(2)
    else:
        slug, stamp = dir_name, "resumed"

    write_progress(status_file, 0, "Resume: starting…")

    # ── Narrations (0–20%) ───────────────────────────────────────────────────
    narration_paths: dict[int, Path] = {}
    narration_durs:  dict[int, float] = {}

    def _tts_scene(scene: Scene, primary_host: str) -> tuple[int, Path]:
        out = work_dir / f"scene_{scene.id:02d}_narration.wav"
        narration_task = task_id(durable_job_id, "scene", scene.id, "narration")
        if out.exists() and out.stat().st_size > 1000:
            logger.info("Scene %d narration exists (%d KB), skipping TTS",
                        scene.id, out.stat().st_size // 1024)
            dur = _get_duration(out)
            store.complete_task(narration_task, result={"path": str(out), "duration": dur, "skipped": True})
            store.record_artifact(durable_job_id, narration_task, "narration", out, duration_seconds=dur)
            return scene.id, out
        hosts_to_try = [primary_host] + [h for h in tts_hosts if h != primary_host]
        last_err: Exception | None = None
        for host in hosts_to_try:
            wid = worker_id("tts", host)
            store.register_worker(wid, "tts", host)
            try:
                ref = Path(voice_ref_str) if voice_ref_str and Path(voice_ref_str).exists() else None
                with TaskRun(
                    store,
                    narration_task,
                    worker_id_value=wid,
                    lease_seconds=600,
                    start_message=f"TTS on {host}",
                ) as run:
                    generate_narration(scene.narration, out, reference_wav=ref, host=host, robotic=voice_robotic, robotic_amount=voice_robotic_amount)
                    dur = _get_duration(out)
                    store.record_artifact(
                        durable_job_id,
                        narration_task,
                        "narration",
                        out,
                        duration_seconds=dur,
                    )
                    run.complete({"path": str(out), "duration": dur, "host": host}, "narration ready")
                return scene.id, out
            except Exception as e:
                logger.warning("TTS failed on %s for scene %d: %s", host, scene.id, e)
                last_err = e
        raise RuntimeError(f"TTS failed on all hosts for scene {scene.id}: {last_err}")

    write_progress(status_file, 0, f"Generating {n} narrations…")
    if not tts_hosts:
        raise RuntimeError("No TTS workers configured")
    tts_pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(n, len(tts_hosts)))
    tts_pending = {
        tts_pool.submit(_tts_scene, scene, tts_hosts[i % len(tts_hosts)]): scene
        for i, scene in enumerate(scenes)
    }
    tts_done = 0
    try:
        while tts_pending:
            done_futs, _ = concurrent.futures.wait(
                list(tts_pending.keys()), timeout=30,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done_futs:
                scene = tts_pending.pop(fut)
                sid, out = fut.result()
                dur = _get_duration(out)
                narration_paths[sid] = out
                narration_durs[sid]  = dur
                tts_done += 1
                pct = 20.0 * tts_done / n
                write_progress(status_file, pct, f"Narration {sid}/{n} done — {dur:.1f}s ({tts_done}/{n})")
    finally:
        tts_pool.shutdown(wait=False)

    total_dur = sum(narration_durs.values())
    logger.info("All narrations done — %.1fs total", total_dur)
    write_progress(status_file, 20, f"Narrations done — {total_dur:.0f}s, generating video…")

    # ── Background music (20–35%) ────────────────────────────────────────────
    music_dur  = max(total_dur * 1.05, 30.0)
    music_path = work_dir / "background_music.wav"
    title = title or (scenes[0].title.split(":")[0] if scenes else "Australia")

    if music_path.exists() and music_path.stat().st_size > 10_000:
        logger.info("Music already exists (%.1f MB), skipping", music_path.stat().st_size / 1024 / 1024)
        music_task = task_id(durable_job_id, "music")
        store.complete_task(
            music_task,
            result={"path": str(music_path), "duration": _get_duration(music_path), "skipped": True},
        )
        store.record_artifact(
            durable_job_id,
            music_task,
            "music",
            music_path,
            duration_seconds=_get_duration(music_path),
        )
    else:
        write_progress(status_file, 20, f"Generating background music ({music_dur:.0f}s)…")
        _MAX_MUSIC_ATTEMPTS = 3
        music_task = task_id(durable_job_id, "music")
        store.update_task_payload(
            music_task,
            {
                "duration_seconds": music_dur,
                "output_path": str(music_path),
                "music_desc": cfg.get("music_desc") or "",
            },
        )
        for attempt in range(1, _MAX_MUSIC_ATTEMPTS + 1):
            music_url = worker_pool.acquire()
            music_worker = worker_id("comfy", music_url)
            store.register_worker(music_worker, "comfy", music_url)
            write_progress(
                status_file,
                20,
                f"Generating background music ({music_dur:.0f}s) on {music_url} "
                f"(attempt {attempt}/{_MAX_MUSIC_ATTEMPTS})…",
            )
            try:
                with TaskRun(
                    store,
                    music_task,
                    worker_id_value=music_worker,
                    lease_seconds=900,
                    start_message=f"music on {music_url}",
                ) as run:
                    generate_music(title, music_dur, music_path, cfg.get("music_desc") or None, comfy_url=music_url)
                    actual_music_dur = _get_duration(music_path)
                    store.record_artifact(
                        durable_job_id,
                        music_task,
                        "music",
                        music_path,
                        duration_seconds=actual_music_dur,
                    )
                    run.complete(
                        {"path": str(music_path), "duration": actual_music_dur, "worker": music_url},
                        "music ready",
                    )
                worker_pool.release(music_url)
                break
            except Exception as e:
                logger.warning("Music attempt %d/%d failed on %s: %s", attempt, _MAX_MUSIC_ATTEMPTS, music_url, e)
                first_line = str(e).splitlines()[0][:180]
                if isinstance(e, StuckJobError) or any(kw in str(e) for kw in _WORKER_ERR_KEYWORDS):
                    worker_pool.mark_failed(music_url)
                else:
                    worker_pool.release(music_url)
                if attempt == _MAX_MUSIC_ATTEMPTS:
                    write_progress(status_file, 20, f"Background music failed on {music_url}: {first_line}")
                    raise
                write_progress(
                    status_file,
                    20,
                    f"Background music attempt {attempt}/{_MAX_MUSIC_ATTEMPTS} failed on {music_url}; "
                    f"retrying. {first_line}",
                )

    write_progress(status_file, 35, "Music ready. Generating cover image and scene videos…")

    # ── Cover image (at ~35%, non-blocking, non-fatal) ───────────────────────
    cover_path = work_dir / "cover.png"
    cover_base = work_dir / "cover_base.png"
    video_title = cfg.get("video_title", "").strip() or title
    style_clean = cfg.get("style", "").strip()

    if not cover_path.exists():
        logger.info("Generating YouTube cover image for '%s'", video_title)
        _cover_url: str | None = None
        cover_w, cover_h = _cover_dimensions(vid_width, vid_height)
        try:
            _cover_url = worker_pool.acquire()
            generate_scene_image(
                _cover_prompt(_shorten_title(video_title), style_clean, scenes=scenes),
                cover_base,
                width=cover_w,
                height=cover_h,
                steps=flux_cfg["steps"],
                flux_model=flux_cfg["model"],
                clip_t5=flux_cfg["clip_t5"],
                clip_l=flux_cfg["clip_l"],
                flux_vae=flux_cfg["vae"],
                comfy_url=_cover_url,
            )
            worker_pool.release(_cover_url)
            _cover_url = None
            shutil.copy2(cover_base, cover_path)
            logger.info("Cover image saved: %s", cover_path)
        except Exception as _cover_err:
            logger.warning("Cover image generation failed (non-fatal): %s", _cover_err)
            if _cover_url is not None:
                try:
                    worker_pool.release(_cover_url)
                except Exception:
                    pass
    else:
        logger.info("Cover image already exists, skipping: %s", cover_path)

    write_progress(status_file, 35, "Music ready. Generating scene videos…")

    # ── Video generation (35–90%) ────────────────────────────────────────────
    scene_raws_map: dict[int, Path] = {}
    scene_ambient_map: dict[int, Path | None] = {}
    _MAX_SCENE_ATTEMPTS = 3

    def _run_scene(scene: Scene) -> tuple[int, Path, Path | None]:
        existing = work_dir / f"scene_{scene.id:02d}_video.mp4"
        image_task = task_id(durable_job_id, "scene", scene.id, "image")
        video_task = task_id(durable_job_id, "scene", scene.id, "video")
        first_frame_path = work_dir / f"scene_{scene.id:02d}_first_frame.png"

        def _persist_first_frame(frame: Path) -> None:
            """Record the first-frame image the video actually used as this scene's
            preview, so loading the script shows exactly that frame and doesn't
            regenerate it. Keyed by the same job_id load_script reads."""
            try:
                store.update_scene_preview(durable_job_id, scene.id, frame)
            except Exception:
                logger.debug("Could not persist preview for scene %d", scene.id, exc_info=True)

        if existing.exists() and existing.stat().st_size > 10_000:
            logger.info("Scene %d video exists (%d KB), skipping", scene.id, existing.stat().st_size // 1024)
            if first_frame_path.exists():
                store.complete_task(image_task, result={"path": str(first_frame_path), "skipped": True})
                store.record_artifact(durable_job_id, image_task, "image", first_frame_path)
                _persist_first_frame(first_frame_path)
            store.complete_task(video_task, result={"path": str(existing), "skipped": True})
            store.record_artifact(durable_job_id, video_task, "scene_video", existing, duration_seconds=_get_duration(existing))
            return scene.id, existing, None

        single_clip = work_dir / f"scene_{scene.id:02d}_clip_01.mp4"
        clip_02 = work_dir / f"scene_{scene.id:02d}_clip_02.mp4"
        if (single_clip.exists() and single_clip.stat().st_size > 10_000
                and not clip_02.exists()):
            amb = work_dir / f"scene_{scene.id:02d}_ambient.wav"
            logger.info("Scene %d single-clip exists (%d KB), skipping",
                        scene.id, single_clip.stat().st_size // 1024)
            if first_frame_path.exists():
                store.complete_task(image_task, result={"path": str(first_frame_path), "skipped": True})
                store.record_artifact(durable_job_id, image_task, "image", first_frame_path)
                _persist_first_frame(first_frame_path)
            store.complete_task(video_task, result={"path": str(single_clip), "skipped": True})
            store.record_artifact(durable_job_id, video_task, "scene_video", single_clip, duration_seconds=_get_duration(single_clip))
            return scene.id, single_clip, amb if amb.exists() else None

        # Check if a preview image exists for this scene and matches the target resolution.
        # Images generated during Script review may be at a different (landscape) resolution —
        # if they don't match we force regeneration at the correct resolution.
        scene_first_frame: Path | None = None
        for ext in ("_preview.png", "_first_frame.png"):
            p = work_dir / f"scene_{scene.id:02d}{ext}"
            if not p.exists():
                continue
            if _image_matches_resolution(p, vid_width, vid_height):
                scene_first_frame = p
                break
            logger.info(
                "Scene %d: ignoring %s — dimensions don't match target %dx%d, will regenerate",
                scene.id, p.name, vid_width, vid_height,
            )

        last_err: Exception | None = None
        for attempt in range(1, _MAX_SCENE_ATTEMPTS + 1):
            if not worker_pool.has_healthy():
                raise RuntimeError(f"All workers failed — last error: {last_err}")
            url = worker_pool.acquire()
            comfy_worker = worker_id("comfy", url)
            store.register_worker(comfy_worker, "comfy", url)
            try:
                logger.info("Scene %d attempt %d/%d on %s", scene.id, attempt, _MAX_SCENE_ATTEMPTS, url)
                store.update_task_payload(
                    video_task,
                    {
                        "narration_duration": narration_durs[scene.id],
                        "vid_width": vid_width,
                        "vid_height": vid_height,
                        "max_clip_secs": max_clip_secs,
                        "lora_strength": lora_strength,
                        "first_pass_cfg": first_pass_cfg,
                        "first_pass_steps": first_pass_steps,
                        "second_pass_cfg": second_pass_cfg,
                        "second_pass_steps": second_pass_steps,
                    },
                )
                if scene_first_frame and scene_first_frame.exists():
                    store.complete_task(image_task, result={"path": str(scene_first_frame), "skipped": True})
                    store.record_artifact(durable_job_id, image_task, "image", scene_first_frame)
                    _persist_first_frame(scene_first_frame)
                else:
                    store.start_task(
                        image_task,
                        worker_id_value=comfy_worker,
                        lease_seconds=900,
                        message=f"first frame on {url}",
                    )
                with TaskRun(
                    store,
                    video_task,
                    worker_id_value=comfy_worker,
                    lease_seconds=3600,
                    start_message=f"video on {url}",
                ) as run:
                    sf, sa = _generate_scene_video(
                        scene, work_dir,
                        narration_durs[scene.id],
                        vid_width, vid_height, max_clip_secs,
                        lora_strength, first_pass_cfg, first_pass_steps,
                        second_pass_cfg, second_pass_steps,
                        comfy_url=url,
                        scene_first_frame=scene_first_frame,
                        flux_cfg=flux_cfg,
                    )
                    produced_first_frame = scene_first_frame or first_frame_path
                    if produced_first_frame and produced_first_frame.exists():
                        store.complete_task(image_task, result={"path": str(produced_first_frame)})
                        store.record_artifact(durable_job_id, image_task, "image", produced_first_frame)
                        _persist_first_frame(produced_first_frame)
                    store.record_artifact(
                        durable_job_id,
                        video_task,
                        "scene_video",
                        sf,
                        duration_seconds=_get_duration(sf),
                    )
                    if sa:
                        store.record_artifact(
                            durable_job_id,
                            video_task,
                            "ambient",
                            sa,
                            duration_seconds=_get_duration(sa),
                        )
                    run.complete(
                        {"path": str(sf), "ambient_path": str(sa) if sa else "", "worker": url},
                        "scene video ready",
                    )
                return scene.id, sf, sa
            except Exception as e:
                if first_frame_path.exists():
                    store.complete_task(image_task, result={"path": str(first_frame_path)})
                    store.record_artifact(durable_job_id, image_task, "image", first_frame_path)
                else:
                    store.fail_task(image_task, e, retryable=True)
                last_err = e
                is_worker_fault = (
                    isinstance(e, StuckJobError)
                    or any(kw in str(e) for kw in _WORKER_ERR_KEYWORDS)
                )
                if is_worker_fault:
                    logger.warning("Worker %s unhealthy for scene %d (attempt %d/%d): %s — removing",
                                   url, scene.id, attempt, _MAX_SCENE_ATTEMPTS, e)
                    worker_pool.mark_failed(url)
                else:
                    logger.warning("Scene %d failed on %s (attempt %d/%d): %s — retrying",
                                   scene.id, url, attempt, _MAX_SCENE_ATTEMPTS, e)
                    if attempt < _MAX_SCENE_ATTEMPTS:
                        time.sleep(5)
            finally:
                worker_pool.release(url)
        raise RuntimeError(f"Scene {scene.id} failed after {_MAX_SCENE_ATTEMPTS} attempts: {last_err}")

    n_workers = len(worker_pool.urls)
    write_progress(status_file, 35, f"Generating {n} scenes across {n_workers} worker(s)…")

    scene_pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(n, max(1, len(worker_pool.urls))))
    pending: dict[concurrent.futures.Future, Scene] = {
        scene_pool.submit(_run_scene, scene): scene for scene in scenes
    }
    completed = 0
    first_error: Exception | None = None
    last_yield = time.time()

    try:
        while pending:
            done, _ = concurrent.futures.wait(
                list(pending.keys()), timeout=30,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done:
                scene = pending.pop(fut)
                try:
                    sid, scene_raw, scene_amb = fut.result()
                    scene_raws_map[sid]    = scene_raw
                    scene_ambient_map[sid] = scene_amb
                    completed += 1
                    pct = 35 + 55 * completed / n
                    write_progress(status_file, pct, f"Scene {sid}/{n} complete ✓  ({completed}/{n} done)")
                    last_yield = time.time()
                except Exception as e:
                    logger.error("Scene %d failed permanently: %s", scene.id, e)
                    if first_error is None:
                        first_error = e
                    write_progress(status_file, 35 + 55 * completed / n,
                                   f"Scene {scene.id} FAILED: {e}")
                    last_yield = time.time()
            now = time.time()
            if pending and first_error is None and (now - last_yield >= 30):
                running = sorted(pending[f].id for f in pending)
                write_progress(status_file, 35 + 55 * completed / n,
                               f"Scenes {running} generating… ({completed}/{n} done)")
                last_yield = now
    finally:
        scene_pool.shutdown(wait=False)

    if first_error is not None:
        raise first_error

    # ── Mux narrations into scene videos ────────────────────────────────────
    scene_finals: list[Path] = []
    for s in scenes:
        raw = scene_raws_map[s.id]
        scene_final = work_dir / f"scene_{s.id:02d}_final.mp4"
        mux_task = task_id(durable_job_id, "scene", s.id, "mux")
        is_last = s.id == scenes[-1].id
        if scene_final.exists() and scene_final.stat().st_size > 10_000:
            logger.info("Scene %d muxed final exists, skipping", s.id)
            store.complete_task(mux_task, result={"path": str(scene_final), "skipped": True})
            store.record_artifact(durable_job_id, mux_task, "scene_final", scene_final, duration_seconds=_get_duration(scene_final))
        else:
            with TaskRun(
                store,
                mux_task,
                worker_id_value=worker_id("local", "assembler"),
                lease_seconds=600,
                start_message="muxing scene",
            ) as run:
                mux_video_audio(raw, narration_paths[s.id], scene_final, extra_tail_secs=2.0 if is_last else 0.0)
                store.record_artifact(
                    durable_job_id,
                    mux_task,
                    "scene_final",
                    scene_final,
                    duration_seconds=_get_duration(scene_final),
                )
                run.complete({"path": str(scene_final)}, "scene muxed")
        scene_finals.append(scene_final)

    scene_ambient_wavs = [scene_ambient_map[s.id] for s in scenes if scene_ambient_map.get(s.id)]

    # ── Final assembly (90–100%) ─────────────────────────────────────────────
    combined     = work_dir / "combined.mp4"
    ambient_path: Path | None = work_dir / "ambient.wav"
    final_path   = OUTPUT_DIR / f"{slug}-{stamp}.mp4"

    if scene_ambient_wavs:
        try:
            if len(scene_ambient_wavs) == 1:
                shutil.copy2(scene_ambient_wavs[0], ambient_path)
            else:
                concat_audio(scene_ambient_wavs, ambient_path)
        except Exception:
            logger.warning("Could not assemble ambient.wav")
            ambient_path = None
    else:
        ambient_path = None

    ambient_vol = cfg.get("ambient_vol", 0) / 100.0
    final_task = task_id(durable_job_id, "final")
    store.update_task_payload(
        final_task,
        {
            "scene_count": len(scenes),
            "final_path": str(final_path),
            "music_path": str(music_path),
            "music_vol": music_vol,
            "voice_vol": voice_vol,
            "ambient_vol": ambient_vol,
            "vid_width": vid_width,
            "vid_height": vid_height,
            "ambient_path": str(ambient_path) if ambient_path else "",
        },
    )
    write_progress(status_file, 90, "Concatenating scenes…")
    with TaskRun(
        store,
        final_task,
        worker_id_value=worker_id("local", "assembler"),
        lease_seconds=900,
        start_message="assembling final video",
    ) as final_run:
        concatenate_scenes(scene_finals, combined)

        write_progress(status_file, 95, f"Mixing audio (voice {voice_vol*100:.0f}%, music {music_vol*100:.0f}%)…")
        mix_background_music(
            combined, music_path, final_path,
            volume=music_vol, voice_volume=voice_vol,
            ambient_path=ambient_path, ambient_volume=ambient_vol,
        )
        ensure_video_resolution(final_path, vid_width, vid_height)
        store.record_artifact(
            durable_job_id,
            final_task,
            "final_video",
            final_path,
            duration_seconds=_get_duration(final_path),
        )
        final_run.complete({"path": str(final_path), "combined": str(combined)}, "final video ready")

    size_mb = final_path.stat().st_size / 1024 / 1024
    logger.info("DONE — %s (%.1f MB)", final_path.name, size_mb)
    write_progress(status_file, 100, f"✅ Done — {final_path.name} ({size_mb:.1f} MB)")
    store.update_job(durable_job_id, status=JOB_DONE, progress_pct=100, progress_message=f"Done - {final_path.name}", final_path=final_path)
    try:
        (work_dir / "job.json").write_text(json.dumps({
            "work_dir": str(work_dir),
            "status": "done",
            "final_path": str(final_path),
            "updated_at": time.time(),
        }, indent=2))
    except Exception:
        pass
    print(f"\n✅ DONE: {final_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <work_dir>", file=sys.stderr)
        sys.exit(1)
    work_dir = Path(sys.argv[1]).expanduser().resolve()
    if not work_dir.is_dir():
        print(f"Not a directory: {work_dir}", file=sys.stderr)
        sys.exit(1)
    try:
        main(work_dir)
    except Exception as exc:
        try:
            if _PROGRESS_STORE is not None and _PROGRESS_JOB_ID is not None:
                _PROGRESS_STORE.update_job(
                    _PROGRESS_JOB_ID,
                    status=JOB_ERROR,
                    progress_message=str(exc).splitlines()[0][:300],
                    error=str(exc),
                )
            (work_dir / "job.json").write_text(json.dumps({
                "work_dir": str(work_dir),
                "status": "error",
                "error": str(exc),
                "updated_at": time.time(),
            }, indent=2))
            write_progress(work_dir / "progress.json", 0, f"Error: {str(exc).splitlines()[0][:300]}")
        except Exception:
            pass
        raise
