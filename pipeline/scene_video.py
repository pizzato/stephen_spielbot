"""Shared scene video generation helper for durable runners and worker agents."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from pipeline.assembler import concat_audio, concat_clips, extract_audio, extract_last_frame, _get_duration
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
) -> tuple[Path, Path | None]:
    """Generate all video clips for a scene using FLUX first frame plus LTX I2V."""
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

    while remaining > 0.5:
        clip_dur = min(remaining, max_clip_secs)
        request_dur = clip_dur + CLIP_BUFFER_SECS
        clip_path = work_dir / f"scene_{scene.id:02d}_clip_{seg_idx + 1:02d}.mp4"

        anchor = scene_first_frame if last_frame is None else last_frame
        logger.info(
            "  [%s] scene %d seg %d: I2V from %s",
            comfy_url,
            scene.id,
            seg_idx + 1,
            "preview image" if last_frame is None else "last frame",
        )
        generate_video_continuation(
            scene.video_prompt,
            scene.negative_prompt,
            anchor,
            clip_path,
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

        actual_dur = _get_duration(clip_path)
        logger.info(
            "  [%s] scene %d seg %d: %.1fs (%.1f MB)",
            comfy_url,
            scene.id,
            seg_idx + 1,
            actual_dur,
            clip_path.stat().st_size / 1024 / 1024,
        )
        clips.append(clip_path)

        amb_clip = work_dir / f"scene_{scene.id:02d}_clip_{seg_idx + 1:02d}_ambient.wav"
        try:
            extract_audio(clip_path, amb_clip, duration=actual_dur)
            ambient_clips.append(amb_clip)
        except Exception:
            logger.warning("Could not extract ambient audio from %s", clip_path.name)

        remaining -= actual_dur
        if remaining > 0.5:
            last_frame = work_dir / f"scene_{scene.id:02d}_clip_{seg_idx + 1:02d}_last.jpg"
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

