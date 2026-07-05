"""Shared scene video generation helper for durable runners and worker agents."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from pipeline.assembler import extract_audio, _get_duration
from pipeline.comfyui import generate_scene_image, generate_video_continuation
from pipeline.llm import Scene


logger = logging.getLogger("video_gen")
CLIP_BUFFER_SECS = 1.0


def generate_scene_video(
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
    on_first_frame: Callable[[Path], None] | None = None,
    scene_end_frame: Path | None = None,
    previous_scene_end_frame: Path | None = None,
    on_end_frame: Callable[[Path], None] | None = None,
) -> tuple[Path, Path | None]:
    """Generate one video clip for the full scene using FLUX first frame plus LTX I2V.

    ``on_first_frame`` (if given) is invoked with the first-frame path the
    instant FLUX finishes — before the much longer I2V step — so callers can
    mark a separate image task done without conflating it with the video.
    """

    fx = flux_cfg or {}
    first_frame_path = work_dir / f"scene_{scene.id:02d}_first_frame.png"
    if (
        scene.use_previous_scene_end_image
        and previous_scene_end_frame is not None
        and previous_scene_end_frame.exists()
    ):
        logger.info(
            "  [%s] scene %d: copying previous scene end frame as first frame",
            comfy_url,
            scene.id,
        )
        shutil.copy2(previous_scene_end_frame, first_frame_path)
        scene_first_frame = first_frame_path
        if on_first_frame is not None:
            on_first_frame(scene_first_frame)

    if scene_first_frame is None or not scene_first_frame.exists():
        logger.info("  [%s] scene %d: generating FLUX first frame inline", comfy_url, scene.id)
        generate_scene_image(
            scene.image_prompt,
            first_frame_path,
            width=vid_width,
            height=vid_height,
            flux_model=fx.get("model", "flux1-schnell-fp8.safetensors"),
            clip_t5=fx.get("clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
            clip_l=fx.get("clip_l", "clip_l.safetensors"),
            flux_vae=fx.get("vae", "ae.safetensors"),
            steps=fx.get("steps", 4),
            comfy_url=comfy_url,
        )
        scene_first_frame = first_frame_path
        if on_first_frame is not None:
            on_first_frame(scene_first_frame)

    if scene.end_image_prompt and (scene_end_frame is None or not scene_end_frame.exists()):
        end_frame_path = work_dir / f"scene_{scene.id:02d}_end_frame.png"
        logger.info("  [%s] scene %d: generating FLUX end frame inline", comfy_url, scene.id)
        generate_scene_image(
            scene.end_image_prompt,
            end_frame_path,
            width=vid_width,
            height=vid_height,
            flux_model=fx.get("model", "flux1-schnell-fp8.safetensors"),
            clip_t5=fx.get("clip_t5", "t5xxl_fp8_e4m3fn.safetensors"),
            clip_l=fx.get("clip_l", "clip_l.safetensors"),
            flux_vae=fx.get("vae", "ae.safetensors"),
            steps=fx.get("steps", 4),
            comfy_url=comfy_url,
        )
        scene_end_frame = end_frame_path
        if on_end_frame is not None:
            on_end_frame(scene_end_frame)

    request_dur = max(narration_dur, 0.5) + CLIP_BUFFER_SECS
    raw = work_dir / f"scene_{scene.id:02d}_clip_01.mp4"

    logger.info(
        "  [%s] scene %d: I2V full scene from preview image, requested %.1fs",
        comfy_url,
        scene.id,
        request_dur,
    )
    generate_video_continuation(
        scene.video_prompt,
        scene.negative_prompt,
        scene_first_frame,
        raw,
        width=vid_width,
        height=vid_height,
        duration_seconds=request_dur,
        lora_strength=lora_strength,
        first_pass_cfg=first_pass_cfg,
        first_pass_steps=first_pass_steps,
        second_pass_cfg=second_pass_cfg,
        second_pass_steps=second_pass_steps,
        comfy_url=comfy_url,
    )

    scene_amb: Path | None = None
    actual_dur = _get_duration(raw)
    logger.info(
        "  [%s] scene %d: %.1fs (%.1f MB)",
        comfy_url,
        scene.id,
        actual_dur,
        raw.stat().st_size / 1024 / 1024,
    )
    scene_amb_path = work_dir / f"scene_{scene.id:02d}_ambient.wav"
    try:
        extract_audio(raw, scene_amb_path, duration=actual_dur)
        scene_amb = scene_amb_path
    except Exception:
        logger.warning("Could not extract ambient audio from %s", raw.name)

    return raw, scene_amb
