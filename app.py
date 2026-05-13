#!/usr/bin/env python3
"""Gradio web interface for the AI video generator."""

import concurrent.futures
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import gradio as gr
import gradio.processing_utils as _gpu

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

logging.basicConfig(level=logging.DEBUG, handlers=[_file_handler, _stream_handler], force=True)
logger = logging.getLogger("video_gen")
logger.info("Logging to %s", LOG_FILE)

# ── Intercept Gradio's _check_allowed so we see every file-path decision ────
_orig_check_allowed = _gpu._check_allowed

def _patched_check_allowed(path, check_in_upload_folder):
    logger.debug("_check_allowed path=%s cwd=%s upload=%s", path, os.getcwd(), check_in_upload_folder)
    try:
        _orig_check_allowed(path, check_in_upload_folder)
        logger.debug("_check_allowed ALLOWED: %s", path)
    except Exception as exc:
        logger.error("_check_allowed BLOCKED: %s — %s", path, exc)
        raise

_gpu._check_allowed = _patched_check_allowed

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.llm import generate_script, Scene, NEGATIVE_PROMPT
from pipeline.comfyui import (
    generate_video_clip, generate_video_continuation, generate_music, COMFYUI_URL,
)
from pipeline.assembler import (
    _get_duration, concat_clips, mux_video_audio, extract_last_frame,
    extract_audio, concat_audio, concatenate_scenes, mix_background_music,
)
from pipeline.worker_pool import WorkerPool, alive_workers

MAX_SCENES    = 12
MAX_CLIP_SECS = 12.0  # LTX 2.3 hard limit (~301 frames at 25fps)
OUTPUT_DIR   = Path.home() / "videos"
OUTPUT_DIR.mkdir(exist_ok=True)
CONFIG_FILE  = Path.home() / ".config" / "video-generator" / "config.json"
VOICES_DIR   = CONFIG_FILE.parent / "voices"
SESSION_FILE = CONFIG_FILE.parent / "last_session.json"

_RESOLUTIONS = {
    # Landscape 16:9
    "Landscape Fast (512×288)":    (512, 288),
    "Landscape (832×480)":         (832, 480),
    "Landscape HD (1024×576)":     (1024, 576),
    # Portrait 9:16
    "Portrait Fast (288×512)":     (288, 512),
    "Portrait (480×832)":          (480, 832),
    "Portrait HD (576×1024)":      (576, 1024),
    # Square 1:1
    "Square (512×512)":            (512, 512),
    "Square HD (576×576)":         (576, 576),
}
_DEFAULT_RESOLUTION = "Landscape (832×480)"

DEFAULT_CFG = {
    "music_vol": 18,
    "voice_vol": 100,
    "ambient_vol": 0,
    "resolution": _DEFAULT_RESOLUTION,
    "max_clip_secs": 12,
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
    "voices": [],
    # ComfyUI worker URLs — one per line in the config UI.
    # Each worker handles one video generation job at a time.
    "comfy_workers": ["http://localhost:8188"],
    "tts_workers":   ["localhost"],
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
    """Derive comfy_workers and tts_workers from cluster.conf, falling back to localhost."""
    hosts = _hosts_from_cluster_conf()
    if not hosts:
        return ["http://localhost:8188"], ["localhost"]
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

def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


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


# ── TTS wrapper ──────────────────────────────────────────────────────────────

def _tts(text: str, out: Path, voice_ref: str | None, host: str = "localhost") -> None:
    from pipeline.tts_worker import generate_narration
    ref = Path(voice_ref) if voice_ref and Path(voice_ref).exists() else None
    generate_narration(text, out, reference_wav=ref, host=host)


# ── Script generation ────────────────────────────────────────────────────────

def on_generate_script(title: str, n_scenes: int, auto_approve: bool, style_hint: str):
    if not title.strip():
        raise gr.Error("Please enter a topic title.")

    logger.info("on_generate_script — title=%r n_scenes=%d auto_approve=%s style_hint=%r",
                title, n_scenes, auto_approve, style_hint)
    try:
        scenes, music_desc, style = generate_script(
            title.strip(), int(n_scenes),
            style_hint=style_hint.strip() if style_hint else None,
        )
    except Exception as e:
        logger.exception("generate_script failed")
        first_line = str(e).split("\n")[0][:300]
        gr.Warning(f"Script generation failed: {first_line}")
        return (gr.update(),) * 53

    logger.info("Script generated — %d scenes, music: %r, style: %r", len(scenes), music_desc, style)

    try:
        titles  = [s.title         for s in scenes] + [""] * (MAX_SCENES - len(scenes))
        visuals = [s.visual_prompt for s in scenes] + [""] * (MAX_SCENES - len(scenes))
        narrs   = [s.narration     for s in scenes] + [""] * (MAX_SCENES - len(scenes))
        blocks  = [gr.update(visible=(i < len(scenes))) for i in range(MAX_SCENES)]

        next_tab = "progress" if auto_approve else "script"

        result = (
            gr.update(selected=next_tab),
            gr.update(visible=True),
            *titles, *visuals, *narrs,
            *blocks,
            music_desc,   # → music_desc_state
            style,        # → style_box (visible text input)
            style,        # → style_state
        )
        logger.info("on_generate_script returning %d values, next_tab=%r", len(result), next_tab)
        return result
    except Exception as e:
        logger.exception("on_generate_script failed assembling return value")
        gr.Warning(f"Failed to process script: {str(e)[:200]}")
        return (gr.update(),) * 53


# ── Per-scene video generation (runs in a worker thread) ─────────────────────

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
) -> tuple[Path, Path | None]:
    """Generate all video clips for a scene, return (raw_video, ambient_wav).
    Raw video retains the original LTX audio. Narration is muxed later at assembly.
    Runs entirely in a background thread — no yields."""
    clips: list[Path] = []
    ambient_clips: list[Path] = []
    last_frame: Path | None = None
    remaining = narration_dur
    seg_idx = 0

    while remaining > 0.5:
        clip_dur  = min(remaining, max_clip_secs)
        clip_path = work_dir / f"scene_{scene.id:02d}_clip_{seg_idx+1:02d}.mp4"

        if last_frame is None:
            generate_video_clip(
                scene.visual_prompt, scene.negative_prompt, clip_path,
                width=vid_width, height=vid_height,
                duration_seconds=clip_dur,
                lora_strength=lora_strength,
                first_pass_cfg=first_pass_cfg,
                first_pass_steps=first_pass_steps,
                second_pass_cfg=second_pass_cfg,
                second_pass_steps=second_pass_steps,
                comfy_url=comfy_url,
            )
        else:
            generate_video_continuation(
                scene.visual_prompt, scene.negative_prompt, last_frame, clip_path,
                width=vid_width, height=vid_height,
                duration_seconds=clip_dur,
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

    # Concatenate clips if scene needed more than one
    if len(clips) == 1:
        raw = clips[0]
    else:
        raw = work_dir / f"scene_{scene.id:02d}_video.mp4"
        concat_clips(clips, raw)

    # Assemble per-scene ambient audio
    scene_amb: Path | None = None
    if ambient_clips:
        scene_amb_path = work_dir / f"scene_{scene.id:02d}_ambient.wav"
        if len(ambient_clips) == 1:
            shutil.copy2(ambient_clips[0], scene_amb_path)
        else:
            concat_audio(ambient_clips, scene_amb_path)
        scene_amb = scene_amb_path

    return raw, scene_amb


# ── Video generation — generator, yields progressive UI updates ──────────────

# gen_outputs count: 1 (progress_bar) + MAX_SCENES (audios) + 1 (music)
#                  + MAX_SCENES (videos) + 1 (final_video) + 1 (combined_state)
#                  + 1 (music_state) + 1 (ambient_state) + 1 (tabs)
_GEN_OUT_COUNT = 1 + MAX_SCENES + 1 + MAX_SCENES + 1 + 1 + 1 + 1 + 1  # = 31


def on_generate(title, n_scenes_val, voice_name, resolution, music_desc, style, auto_approve, *scene_fields):
    n       = int(n_scenes_val)
    titles  = list(scene_fields[0           : MAX_SCENES])
    visuals = list(scene_fields[MAX_SCENES  : MAX_SCENES * 2])
    narrs   = list(scene_fields[MAX_SCENES*2: MAX_SCENES * 3])

    cfg               = load_config()
    music_vol         = cfg.get("music_vol", 18) / 100.0
    voice_vol         = cfg.get("voice_vol", 100) / 100.0
    voice_ref         = voice_path_for(voice_name)
    max_clip_secs     = float(cfg.get("max_clip_secs", MAX_CLIP_SECS))
    lora_strength     = float(cfg.get("lora_strength", 0.5))
    first_pass_cfg    = float(cfg.get("first_pass_cfg", 1.0))
    first_pass_steps  = int(cfg.get("first_pass_steps", 8))
    second_pass_cfg   = float(cfg.get("second_pass_cfg", 3.0))
    second_pass_steps = int(cfg.get("second_pass_steps", 6))
    worker_urls       = alive_workers(cfg.get("comfy_workers", [COMFYUI_URL]))
    worker_pool       = WorkerPool(worker_urls)
    logger.info("Workers: %s", worker_urls)
    # resolution from Create tab overrides config default
    vid_width, vid_height = _RESOLUTIONS.get(
        resolution or cfg.get("resolution", _DEFAULT_RESOLUTION), (832, 480)
    )

    # Prepend style as a leading sentence so LTX's Gemma encoder processes the
    # visual style context before the scene-specific description.
    style_clean = style.strip().rstrip(".") if style and style.strip() else ""
    logger.info("on_generate START — title=%r n=%d voice=%r music_vol=%.2f voice_vol=%.2f style=%r",
                title, n, voice_name, music_vol, voice_vol, style)

    scenes = [
        Scene(
            id=i + 1,
            title=titles[i] or f"Scene {i+1}",
            visual_prompt=(f"{style_clean}. {visuals[i] or title}" if style_clean
                           else (visuals[i] or title)),
            narration=narrs[i] or "",
        )
        for i in range(n)
    ]

    stamp    = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug     = slugify(title)
    work_dir = OUTPUT_DIR / f"{slug}-{stamp}"
    work_dir.mkdir(parents=True)
    logger.info("Work dir: %s | CWD: %s", work_dir, os.getcwd())

    (work_dir / "script.json").write_text(
        json.dumps([{"id": s.id, "title": s.title,
                     "visual_prompt": s.visual_prompt,
                     "narration": s.narration} for s in scenes], indent=2)
    )

    # Mutable state buffers for incremental UI updates
    audio_upd = [gr.update(visible=False)] * MAX_SCENES
    music_upd = gr.update(visible=False)
    video_upd = [gr.update(visible=False)] * MAX_SCENES

    def emit(msg: str, pct: float, final=gr.update(), comb=gr.update(), mus=gr.update(), amb=gr.update(), tabs_upd=gr.update()):
        logger.debug("YIELD %.0f%% — %s", pct, msg)
        return (_progress_html(pct, msg), *audio_upd, music_upd, *video_upd, final, comb, mus, amb, tabs_upd)

    def _run_bg(label: str, fn, *args, pct: float, **kwargs):
        """Run fn(*args, **kwargs) in thread; yield heartbeat every 5s to keep SSE alive."""
        fut   = _executor.submit(fn, *args, **kwargs)
        start = time.monotonic()
        while not fut.done():
            time.sleep(5)
            elapsed = time.monotonic() - start
            logger.debug("heartbeat %s — %.0fs", label, elapsed)
            yield emit(f"{label} ({elapsed:.0f}s…)", pct)
        fut.result()  # re-raise any exception

    try:
        yield emit("Starting…", 0)

        # ── Narrations (0–20%) — parallel across TTS workers ────────────────
        narration_paths: dict[int, Path] = {}
        narration_durs:  dict[int, float] = {}

        tts_hosts = cfg.get("tts_workers", ["localhost"])
        if not tts_hosts:
            tts_hosts = ["localhost"]

        def _tts_scene(scene: Scene, primary_host: str) -> tuple[int, Path]:
            out = work_dir / f"scene_{scene.id:02d}_narration.wav"
            hosts_to_try = [primary_host] + [h for h in tts_hosts if h != primary_host]
            last_err: Exception | None = None
            for host in hosts_to_try:
                try:
                    _tts(scene.narration, out, voice_ref, host=host)
                    return scene.id, out
                except Exception as e:
                    logger.warning("TTS failed on %s for scene %d, trying next: %s", host, scene.id, e)
                    last_err = e
            raise RuntimeError(f"TTS failed on all hosts for scene {scene.id}: {last_err}")

        yield emit(f"Generating {n} narration(s) across {len(tts_hosts)} TTS worker(s)…", 0)
        tts_pool = concurrent.futures.ThreadPoolExecutor(max_workers=n)
        tts_pending: dict[concurrent.futures.Future, Scene] = {
            tts_pool.submit(_tts_scene, scene, tts_hosts[i % len(tts_hosts)]): scene
            for i, scene in enumerate(scenes)
        }
        tts_done_count = 0
        try:
            while tts_pending:
                done_futs, _ = concurrent.futures.wait(
                    list(tts_pending.keys()), timeout=5,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done_futs:
                    scene = tts_pending.pop(fut)
                    sid, out = fut.result()  # re-raises on error
                    dur = _get_duration(out)
                    narration_paths[sid] = out
                    narration_durs[sid]  = dur
                    tts_done_count += 1
                    logger.info("  narration done: %s — %.1fs", out.name, dur)
                    audio_upd = list(audio_upd)
                    audio_upd[sid - 1] = gr.update(value=str(out), visible=True)
                    pct = 20.0 * tts_done_count / n
                    yield emit(f"Narration {sid}/{n} done — {dur:.1f}s ({tts_done_count}/{n})", pct)
                if tts_pending:
                    yield emit(
                        f"Narrations generating… ({tts_done_count}/{n} done)",
                        20.0 * tts_done_count / n,
                    )
        finally:
            tts_pool.shutdown(wait=False)

        total_dur   = sum(narration_durs.values())
        total_clips = sum(math.ceil(d / max_clip_secs) for d in narration_durs.values())
        logger.info("All narrations done — %.1fs total, %d video segment(s) to generate", total_dur, total_clips)
        yield emit(f"Narrations done — {total_dur:.0f}s, generating video…", 20)

        # ── Background music (20–35%) ────────────────────────────────────────
        music_dur  = max(total_dur * 1.05, 30.0)
        music_path = work_dir / "background_music.wav"
        label = f"Background music ({music_dur:.0f}s)"
        yield emit(f"Generating {label}…", 20)
        try:
            yield from _run_bg(label, generate_music, title, music_dur, music_path, music_desc or None, pct=22)
        except Exception:
            logger.exception("Music generation failed")
            raise
        logger.info("Music ready: %s (%.1f MB)", music_path.name,
                    music_path.stat().st_size / 1024 / 1024)
        music_upd = gr.update(value=str(music_path), visible=True)
        yield emit("Music ready.", 35)

        # ── Video generation (35–90%) — parallel across workers ─────────────────
        scene_raws_map: dict[int, Path] = {}
        scene_ambient_map: dict[int, Path | None] = {}

        _WORKER_ERR_KEYWORDS = ("timed out", "not reachable", "URLError", "Connection refused",
                                "ConnectionRefused", "RemoteDisconnected")

        def _run_scene(scene: Scene) -> tuple[int, Path, Path | None]:
            last_err: Exception | None = None
            while True:
                if not worker_pool.has_healthy():
                    raise RuntimeError(
                        f"All workers failed — last error: {last_err}"
                    )
                url = worker_pool.acquire()
                try:
                    logger.info("Scene %d starting on %s", scene.id, url)
                    sf, sa = _generate_scene_video(
                        scene, work_dir,
                        narration_durs[scene.id],
                        vid_width, vid_height, max_clip_secs,
                        lora_strength, first_pass_cfg, first_pass_steps,
                        second_pass_cfg, second_pass_steps,
                        comfy_url=url,
                    )
                    return scene.id, sf, sa
                except Exception as e:
                    if any(kw in str(e) for kw in _WORKER_ERR_KEYWORDS):
                        logger.warning(
                            "Worker %s failed for scene %d, retrying on another worker: %s",
                            url, scene.id, e,
                        )
                        worker_pool.mark_failed(url)
                        last_err = e
                        # mark_failed deleted the semaphore; finally release() is a no-op
                    else:
                        raise
                finally:
                    # Releases on success and non-worker errors.
                    # No-op after mark_failed (semaphore already deleted).
                    worker_pool.release(url)

        n_workers = len(worker_pool.urls)
        yield emit(
            f"Generating {n} scene(s) across {n_workers} worker(s): {', '.join(worker_pool.urls)}…",
            35,
        )

        scene_pool = concurrent.futures.ThreadPoolExecutor(max_workers=n)
        pending: dict[concurrent.futures.Future, Scene] = {
            scene_pool.submit(_run_scene, scene): scene for scene in scenes
        }
        completed = 0
        first_error: Exception | None = None
        try:
            while pending:
                done, _ = concurrent.futures.wait(
                    list(pending.keys()), timeout=5,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    scene = pending.pop(fut)
                    try:
                        sid, scene_raw, scene_amb = fut.result()
                        scene_raws_map[sid]    = scene_raw
                        scene_ambient_map[sid] = scene_amb
                        completed += 1
                        video_upd = list(video_upd)
                        video_upd[sid - 1] = gr.update(value=str(scene_raw), visible=True)
                        pct = 35 + 55 * completed / n
                        yield emit(f"Scene {sid}/{n} complete ✓  ({completed}/{n} done)", pct)
                    except Exception as e:
                        logger.error("Scene %d failed: %s", scene.id, e)
                        if first_error is None:
                            first_error = e
                        for f in list(pending.keys()):
                            f.cancel()
                        yield emit(f"Scene {scene.id} failed: {e}", 35 + 55 * completed / n)
                if pending and first_error is None:
                    running = [pending[f].id for f in pending]
                    yield emit(
                        f"Scenes {running} generating… ({completed}/{n} done)",
                        35 + 55 * completed / n,
                    )
        finally:
            scene_pool.shutdown(wait=False)

        if first_error is not None:
            raise first_error

        # Mux narration into each raw scene video now (kept separate so progress bar shows original audio)
        scene_finals: list[Path] = []
        for s in scenes:
            raw = scene_raws_map[s.id]
            scene_final = work_dir / f"scene_{s.id:02d}_final.mp4"
            mux_video_audio(raw, narration_paths[s.id], scene_final)
            scene_finals.append(scene_final)

        scene_ambient_wavs = [scene_ambient_map[s.id] for s in scenes if scene_ambient_map.get(s.id)]

        # ── Final assembly (90–100%) ──────────────────────────────────────────
        combined     = work_dir / "combined.mp4"
        ambient_path = work_dir / "ambient.wav"
        final_path   = OUTPUT_DIR / f"{slug}-{stamp}.mp4"

        if scene_ambient_wavs:
            try:
                if len(scene_ambient_wavs) == 1:
                    import shutil as _shutil
                    _shutil.copy2(scene_ambient_wavs[0], ambient_path)
                else:
                    concat_audio(scene_ambient_wavs, ambient_path)
            except Exception:
                logger.warning("Could not assemble ambient.wav")
                ambient_path = None
        else:
            ambient_path = None

        yield emit("Concatenating scenes…", 90)
        try:
            concatenate_scenes(scene_finals, combined)
        except Exception:
            logger.exception("concatenate_scenes failed")
            raise

        ambient_vol = cfg.get("ambient_vol", 0) / 100.0
        yield emit(f"Mixing audio (voice {voice_vol*100:.0f}%, music {music_vol*100:.0f}%)…", 95)
        try:
            mix_background_music(
                combined, music_path, final_path,
                volume=music_vol, voice_volume=voice_vol,
                ambient_path=ambient_path, ambient_volume=ambient_vol,
            )
        except Exception:
            logger.exception("mix_background_music failed")
            raise

        size_mb = final_path.stat().st_size / 1024 / 1024
        logger.info("DONE — %s (%.1f MB)", final_path.name, size_mb)

        amb_str = str(ambient_path) if ambient_path else ""
        save_session(str(combined), str(music_path), amb_str,
                     voice_vol * 100, music_vol * 100, ambient_vol * 100)

        yield emit(
            f"✅ Done — {final_path.name} ({size_mb:.1f} MB)",
            100,
            final=gr.update(value=str(final_path), visible=True),
            comb=str(combined),
            mus=str(music_path),
            amb=amb_str,
            tabs_upd=gr.update(selected="output"),
        )

    except Exception as e:
        logger.exception("on_generate CRASHED")
        first_line = str(e).split("\n")[0][:300]
        yield (_error_html(f"❌ Error: {first_line}"), *audio_upd, music_upd, *video_upd,
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update())


def _auto_generate(title, n_scenes_val, voice_name, resolution, music_desc, style, auto_approve, *scene_fields):
    if not auto_approve:
        yield (gr.update(),) * _GEN_OUT_COUNT
        return
    yield from on_generate(title, n_scenes_val, voice_name, resolution, music_desc, style, auto_approve, *scene_fields)


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

    stamp       = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = OUTPUT_DIR / f"{input_path.stem}_ltx_upscale_{stamp}.mp4"
    logger.info("LTX upscale: %s → %s", input_path.name, output_path.name)

    yield gr.update(visible=False), "Upscaling with LTX spatial upscaler…"
    fut   = _executor.submit(ltx_upscale_video, input_path, output_path)
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
                   claude_api_key: str, claude_model: str):
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
    cfg["comfy_workers"]      = workers or ["http://localhost:8188"]
    tts_workers = [h.strip() for h in tts_workers_text.splitlines() if h.strip()]
    cfg["tts_workers"]        = tts_workers or ["localhost"]
    cfg["llm_backend"]        = llm_backend
    cfg["local_llm_url"]      = local_llm_url.strip() or "http://localhost:8000/v1/chat/completions"
    cfg["local_llm_model"]    = local_llm_model.strip() or "openai/gpt-oss-120b"
    cfg["claude_api_key"]     = claude_api_key.strip()
    cfg["claude_model"]       = claude_model.strip() or "claude-sonnet-4-6"
    save_config(cfg)
    logger.info("Config saved: lora=%.2f workers=%s tts=%s",
                lora_strength, cfg["comfy_workers"], cfg["tts_workers"])
    return f"Settings saved ✓  ({len(cfg['comfy_workers'])} video, {len(cfg['tts_workers'])} TTS worker(s))"


# ── UI ────────────────────────────────────────────────────────────────────────

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

        # Shared state for remix, music description, and visual style
        combined_state   = gr.State("")
        music_state      = gr.State("")
        ambient_state    = gr.State("")
        music_desc_state = gr.State("")
        style_state      = gr.State("")

        with gr.Tabs(elem_id="main_tabs") as tabs:

            # ── Create ───────────────────────────────────────────────────
            with gr.Tab("🎬 Create", id="create"):
                with gr.Row():
                    with gr.Column(scale=3):
                        title_in = gr.Textbox(
                            label="Topic / Title",
                            placeholder="e.g.  The History of the Roman Empire",
                        )
                    with gr.Column(scale=1):
                        n_scenes_in = gr.Slider(2, MAX_SCENES, value=5, step=1, label="Scenes")

                style_in = gr.Textbox(
                    label="Visual Style (optional — leave blank for LLM to generate)",
                    placeholder="e.g.  cinematic 35mm film, warm golden tones, shallow depth of field",
                    lines=1,
                )

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
                    auto_approve_in = gr.Checkbox(
                        label="Auto-approve script (skip review)", value=False,
                    )

                gen_script_btn = gr.Button(
                    "1. Generate Script →", variant="primary", size="lg"
                )

            # ── Script Review ────────────────────────────────────────────
            with gr.Tab("📝 Script", id="script"):
                with gr.Column(visible=False) as script_col:
                    style_box = gr.Textbox(
                        label="Visual Style (applied to all scenes)",
                        lines=1,
                        placeholder="Generated or entered style will appear here — edit freely",
                    )

                    scene_titles:  list[gr.Textbox] = []
                    scene_visuals: list[gr.Textbox] = []
                    scene_narrs:   list[gr.Textbox] = []
                    scene_blocks:  list[gr.Group]   = []

                    for i in range(MAX_SCENES):
                        with gr.Group(visible=False) as blk:
                            gr.Markdown(f"### Scene {i + 1}")
                            t = gr.Textbox(label="Title", lines=1)
                            with gr.Row():
                                v  = gr.Textbox(label="Visual Prompt", lines=3)
                                nr = gr.Textbox(label="Narration", lines=3)
                        scene_titles.append(t)
                        scene_visuals.append(v)
                        scene_narrs.append(nr)
                        scene_blocks.append(blk)

                    approve_btn = gr.Button(
                        "2. Approve & Generate Video →", variant="primary", size="lg"
                    )

            # ── Progress ─────────────────────────────────────────────────
            with gr.Tab("⏳ Progress", id="progress"):
                progress_bar = gr.HTML(value=_progress_html(0, "Waiting to start…"))

                gr.Markdown("#### Narrations")
                with gr.Row():
                    scene_audios = [
                        gr.Audio(label=f"Scene {i+1}", visible=False,
                                 interactive=False, scale=1)
                        for i in range(MAX_SCENES)
                    ]

                gr.Markdown("#### Background Music")
                music_audio_out = gr.Audio(
                    label="Background Music", visible=False, interactive=False
                )

                gr.Markdown("#### Scene Videos")
                scene_videos: list[gr.Video] = []
                for row_start in range(0, MAX_SCENES, 3):
                    with gr.Row():
                        for i in range(row_start, min(row_start + 3, MAX_SCENES)):
                            scene_videos.append(
                                gr.Video(label=f"Scene {i+1}", visible=False, height=220)
                            )
                while len(scene_videos) < MAX_SCENES:
                    scene_videos.append(gr.Video(visible=False))

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
                        3, 12, value=cfg.get("max_clip_secs", 12), step=1,
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
                with gr.Group(visible=cfg.get("llm_backend", "local") == "local") as local_cfg_group:
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
                with gr.Group(visible=cfg.get("llm_backend", "local") == "claude") as claude_cfg_group:
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

                save_cfg_btn = gr.Button("Save Defaults", variant="secondary")
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
            *scene_titles, *scene_visuals, *scene_narrs,
            *scene_blocks,
            music_desc_state,
            style_box, style_state,   # style shown in Script tab + stored in state
        ]

        gen_outputs = [
            progress_bar,
            *scene_audios,
            music_audio_out,
            *scene_videos,
            final_video_out,
            combined_state,
            music_state,
            ambient_state,
            tabs,
        ]

        gen_script_btn.click(
            fn=lambda: gr.update(interactive=False, value="⏳ Generating script…"),
            inputs=[], outputs=[gen_script_btn],
        ).then(
            fn=on_generate_script,
            inputs=[title_in, n_scenes_in, auto_approve_in, style_in],
            outputs=script_outputs,
        ).then(
            fn=_auto_generate,
            inputs=[
                title_in, n_scenes_in, voice_dropdown, resolution_in,
                music_desc_state, style_state, auto_approve_in,
                *scene_titles, *scene_visuals, *scene_narrs,
            ],
            outputs=gen_outputs,
        ).then(
            fn=lambda auto: gr.update(selected="output") if auto else gr.update(),
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
                *scene_titles, *scene_visuals, *scene_narrs,
            ],
            outputs=gen_outputs,
        ).then(
            fn=lambda: gr.update(interactive=True, value="2. Approve & Generate Video →"),
            inputs=[], outputs=[approve_btn],
        )

        # Keep style_state in sync when user edits style_box directly
        style_box.change(fn=lambda v: v, inputs=[style_box], outputs=[style_state])

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
            fn=lambda b: (gr.update(visible=(b == "local")), gr.update(visible=(b == "claude"))),
            inputs=[cfg_llm_backend],
            outputs=[local_cfg_group, claude_cfg_group],
        )

        save_cfg_btn.click(
            fn=on_save_config,
            inputs=[cfg_music_vol, cfg_voice_vol, cfg_ambient_vol,
                    cfg_resolution, cfg_max_clip, cfg_lora,
                    cfg_first_pass_cfg, cfg_first_pass_steps,
                    cfg_second_pass_cfg, cfg_second_pass_steps,
                    cfg_workers, cfg_tts_workers,
                    cfg_llm_backend,
                    cfg_local_llm_url, cfg_local_llm_model,
                    cfg_claude_key, cfg_claude_model],
            outputs=[cfg_status],
        )

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

        demo.load(
            fn=on_voices_load,
            inputs=[],
            outputs=[voices_table, voice_dropdown, remove_voice_dd],
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

    demo = build_ui()
    demo.queue(status_update_rate=2)
    demo.launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        theme=gr.themes.Soft(),
        allowed_paths=[str(OUTPUT_DIR), str(VOICES_DIR)],
    )
