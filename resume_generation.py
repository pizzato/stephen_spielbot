#!/usr/bin/env python3
"""Standalone resume script — resumes video generation without Gradio.

Usage:
    .venv/bin/python resume_generation.py <work_dir>
"""
import concurrent.futures
import json
import logging
import logging.handlers
import math
import shutil
import sys
import time
from pathlib import Path

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

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.llm import Scene, NEGATIVE_PROMPT
from pipeline.comfyui import (
    generate_video_continuation, generate_music,
    generate_scene_image, StuckJobError,
)
from pipeline.assembler import (
    _get_duration, concat_clips, mux_video_audio,
    extract_last_frame, extract_audio, concat_audio, concatenate_scenes,
    mix_background_music,
)
from pipeline.tts_worker import generate_narration
from pipeline.worker_pool import WorkerPool, alive_workers

CONFIG_FILE = Path.home() / ".config" / "video-generator" / "config.json"
OUTPUT_DIR  = Path.home() / "videos"

_CLIP_BUFFER_SECS = 1.0
_WORKER_ERR_KEYWORDS = ("timed out", "not reachable", "URLError", "Connection refused",
                        "ConnectionRefused", "RemoteDisconnected")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    raise RuntimeError(f"Config not found: {CONFIG_FILE}")


def write_progress(status_file: Path, pct: float, msg: str) -> None:
    try:
        status_file.write_text(json.dumps({"pct": round(pct, 1), "msg": msg, "ts": time.time()}))
    except Exception:
        pass
    logger.info("PROGRESS %.0f%% — %s", pct, msg)


def _generate_scene_video(
    scene: Scene,
    work_dir: Path,
    narration_dur: float,
    vid_width: int,
    vid_height: int,
    max_clip_secs: float,
    lora_strength: float,
    first_pass_cfg: float,
    first_pass_steps: int,
    second_pass_cfg: float,
    second_pass_steps: int,
    comfy_url: str,
    scene_first_frame: Path | None = None,
    flux_cfg: dict | None = None,
) -> tuple[Path, Path | None]:
    clips: list[Path] = []
    ambient_clips: list[Path] = []
    last_frame: Path | None = None
    remaining = narration_dur
    seg_idx = 0

    if scene_first_frame is None or not scene_first_frame.exists():
        fx = flux_cfg or {}
        first_frame_path = work_dir / f"scene_{scene.id:02d}_first_frame.png"
        logger.info("  [%s] scene %d: generating FLUX first frame inline", comfy_url, scene.id)
        generate_scene_image(
            scene.image_prompt, first_frame_path,
            width=vid_width, height=vid_height,
            flux_model=fx.get("model", "flux1-schnell-fp8.safetensors"),
            clip_t5=fx.get("clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
            clip_l=fx.get("clip_l", "clip_l.safetensors"),
            flux_vae=fx.get("vae", "ae.safetensors"),
            steps=fx.get("steps", 4),
            comfy_url=comfy_url,
        )
        scene_first_frame = first_frame_path

    while remaining > 0.5:
        clip_dur    = min(remaining, max_clip_secs)
        request_dur = clip_dur + _CLIP_BUFFER_SECS
        clip_path   = work_dir / f"scene_{scene.id:02d}_clip_{seg_idx+1:02d}.mp4"

        anchor = scene_first_frame if last_frame is None else last_frame
        logger.info("  [%s] scene %d seg %d: I2V from %s",
                    comfy_url, scene.id, seg_idx + 1,
                    "preview image" if last_frame is None else "last frame")
        generate_video_continuation(
            scene.video_prompt, scene.negative_prompt, anchor, clip_path,
            width=vid_width, height=vid_height,
            duration_seconds=request_dur,
            lora_strength=lora_strength,
            first_pass_cfg=first_pass_cfg,
            first_pass_steps=first_pass_steps,
            second_pass_cfg=second_pass_cfg,
            second_pass_steps=second_pass_steps,
            comfy_url=comfy_url,
        )

        actual_dur = _get_duration(clip_path)
        logger.info("  [%s] scene %d seg %d: %.1fs (%.1f MB)",
                    comfy_url, scene.id, seg_idx + 1, actual_dur,
                    clip_path.stat().st_size / 1024 / 1024)
        clips.append(clip_path)

        amb_clip = work_dir / f"scene_{scene.id:02d}_clip_{seg_idx+1:02d}_ambient.wav"
        try:
            extract_audio(clip_path, amb_clip, duration=actual_dur)
            ambient_clips.append(amb_clip)
        except Exception:
            logger.warning("Could not extract ambient audio from %s", clip_path.name)

        remaining -= actual_dur
        if remaining > 0.5:
            last_frame = work_dir / f"scene_{scene.id:02d}_clip_{seg_idx+1:02d}_last.jpg"
            extract_last_frame(clip_path, last_frame)

        seg_idx += 1

    if len(clips) == 1:
        raw = clips[0]
    else:
        raw = work_dir / f"scene_{scene.id:02d}_video.mp4"
        concat_clips(clips, raw)

    scene_amb: Path | None = None
    if ambient_clips:
        scene_amb_path = work_dir / f"scene_{scene.id:02d}_ambient.wav"
        if len(ambient_clips) == 1:
            shutil.copy2(ambient_clips[0], scene_amb_path)
        else:
            concat_audio(ambient_clips, scene_amb_path)
        scene_amb = scene_amb_path

    return raw, scene_amb


def main(work_dir: Path) -> None:
    cfg = load_config()

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

    # Config
    music_vol         = cfg.get("music_vol", 18) / 100.0
    voice_vol         = cfg.get("voice_vol", 100) / 100.0
    voice_name        = cfg.get("default_voice", "Thomas")
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

    res_name = cfg.get("resolution", "Landscape FHD (1920×1080)")
    _RESOLUTIONS = {
        "Landscape Fast (512×288)":    (512, 288),
        "Landscape (832×480)":         (832, 480),
        "Landscape HD (1024×576)":     (1024, 576),
        "Landscape FHD (1920×1080)":   (1920, 1080),
    }
    vid_width, vid_height = _RESOLUTIONS.get(res_name, (1920, 1080))
    logger.info("Resolution: %s → %dx%d", res_name, vid_width, vid_height)
    logger.info("Workers: %s", worker_urls)
    logger.info("TTS hosts: %s", tts_hosts)

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
        if out.exists() and out.stat().st_size > 1000:
            logger.info("Scene %d narration exists (%d KB), skipping TTS",
                        scene.id, out.stat().st_size // 1024)
            return scene.id, out
        hosts_to_try = [primary_host] + [h for h in tts_hosts if h != primary_host]
        last_err: Exception | None = None
        for host in hosts_to_try:
            try:
                ref = Path(voice_ref_str) if voice_ref_str and Path(voice_ref_str).exists() else None
                generate_narration(scene.narration, out, reference_wav=ref, host=host)
                return scene.id, out
            except Exception as e:
                logger.warning("TTS failed on %s for scene %d: %s", host, scene.id, e)
                last_err = e
        raise RuntimeError(f"TTS failed on all hosts for scene {scene.id}: {last_err}")

    write_progress(status_file, 0, f"Generating {n} narrations…")
    tts_pool = concurrent.futures.ThreadPoolExecutor(max_workers=n)
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
    title = scenes[0].title.split(":")[0] if scenes else "Australia"

    if music_path.exists() and music_path.stat().st_size > 10_000:
        logger.info("Music already exists (%.1f MB), skipping", music_path.stat().st_size / 1024 / 1024)
    else:
        write_progress(status_file, 20, f"Generating background music ({music_dur:.0f}s)…")
        _MAX_MUSIC_ATTEMPTS = 3
        for attempt in range(1, _MAX_MUSIC_ATTEMPTS + 1):
            music_url = worker_pool.acquire()
            try:
                generate_music(title, music_dur, music_path, None, comfy_url=music_url)
                worker_pool.release(music_url)
                break
            except Exception as e:
                logger.warning("Music attempt %d/%d failed on %s: %s", attempt, _MAX_MUSIC_ATTEMPTS, music_url, e)
                if isinstance(e, StuckJobError) or any(kw in str(e) for kw in _WORKER_ERR_KEYWORDS):
                    worker_pool.mark_failed(music_url)
                else:
                    worker_pool.release(music_url)
                if attempt == _MAX_MUSIC_ATTEMPTS:
                    raise

    write_progress(status_file, 35, "Music ready. Generating scene videos…")

    # ── Video generation (35–90%) ────────────────────────────────────────────
    scene_raws_map: dict[int, Path] = {}
    scene_ambient_map: dict[int, Path | None] = {}
    _MAX_SCENE_ATTEMPTS = 3

    def _run_scene(scene: Scene) -> tuple[int, Path, Path | None]:
        existing = work_dir / f"scene_{scene.id:02d}_video.mp4"
        if existing.exists() and existing.stat().st_size > 10_000:
            logger.info("Scene %d video exists (%d KB), skipping", scene.id, existing.stat().st_size // 1024)
            return scene.id, existing, None

        # Check if a preview image exists for this scene
        scene_first_frame: Path | None = None
        for ext in ("_preview.png", "_first_frame.png"):
            p = work_dir / f"scene_{scene.id:02d}{ext}"
            if p.exists():
                scene_first_frame = p
                break

        last_err: Exception | None = None
        for attempt in range(1, _MAX_SCENE_ATTEMPTS + 1):
            if not worker_pool.has_healthy():
                raise RuntimeError(f"All workers failed — last error: {last_err}")
            url = worker_pool.acquire()
            try:
                logger.info("Scene %d attempt %d/%d on %s", scene.id, attempt, _MAX_SCENE_ATTEMPTS, url)
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
                return scene.id, sf, sa
            except Exception as e:
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

    scene_pool = concurrent.futures.ThreadPoolExecutor(max_workers=n)
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
        if scene_final.exists() and scene_final.stat().st_size > 10_000:
            logger.info("Scene %d muxed final exists, skipping", s.id)
        else:
            mux_video_audio(raw, narration_paths[s.id], scene_final)
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

    write_progress(status_file, 90, "Concatenating scenes…")
    concatenate_scenes(scene_finals, combined)

    ambient_vol = cfg.get("ambient_vol", 0) / 100.0
    write_progress(status_file, 95, f"Mixing audio (voice {voice_vol*100:.0f}%, music {music_vol*100:.0f}%)…")
    mix_background_music(
        combined, music_path, final_path,
        volume=music_vol, voice_volume=voice_vol,
        ambient_path=ambient_path, ambient_volume=ambient_vol,
    )

    size_mb = final_path.stat().st_size / 1024 / 1024
    logger.info("DONE — %s (%.1f MB)", final_path.name, size_mb)
    write_progress(status_file, 100, f"✅ Done — {final_path.name} ({size_mb:.1f} MB)")
    print(f"\n✅ DONE: {final_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <work_dir>", file=sys.stderr)
        sys.exit(1)
    work_dir = Path(sys.argv[1]).expanduser().resolve()
    if not work_dir.is_dir():
        print(f"Not a directory: {work_dir}", file=sys.stderr)
        sys.exit(1)
    main(work_dir)
