"""Shared scene video generation helper for durable runners and worker agents."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from pipeline import engines
from pipeline.assembler import ensure_video_resolution, extract_audio, _get_duration
from pipeline.comfyui import generate_video_with_engine, generate_with_engine
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
    image_engine: dict | None = None,
    on_first_frame: Callable[[Path], None] | None = None,
    video_engine: dict | None = None,
) -> tuple[Path, Path | None]:
    """Generate one video clip for the full scene: image-engine first frame plus I2V.

    *image_engine* is an engine dict from :func:`pipeline.engines.resolve`
    (the style's selected image engine); None falls back to the default engine.
    *video_engine* is a dict from :func:`pipeline.engines.resolve_video` picking
    the I2V model (None = the default LTX 2.3 path).

    ``on_first_frame`` (if given) is invoked with the first-frame path the
    instant the image engine finishes — before the much longer I2V step — so
    callers can mark a separate image task done without conflating it with the
    video.
    """

    if scene_first_frame is None or not scene_first_frame.exists():
        engine = image_engine or engines.resolve({}, None)
        first_frame_path = work_dir / f"scene_{scene.id:02d}_first_frame.png"
        logger.info("  [%s] scene %d: generating first frame inline (%s)",
                    comfy_url, scene.id, engine.get("key"))
        generate_with_engine(
            engine,
            scene.image_prompt,
            first_frame_path,
            width=vid_width,
            height=vid_height,
            comfy_url=comfy_url,
        )
        scene_first_frame = first_frame_path
        if on_first_frame is not None:
            on_first_frame(scene_first_frame)

    request_dur = max(narration_dur, 0.5) + CLIP_BUFFER_SECS
    raw = work_dir / f"scene_{scene.id:02d}_clip_01.mp4"

    logger.info(
        "  [%s] scene %d: I2V full scene from preview image, requested %.1fs",
        comfy_url,
        scene.id,
        request_dur,
    )
    generate_video_with_engine(
        video_engine,
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
    if video_engine and video_engine.get("family") == "minimax":
        # H3 renders under its pixel cap; reframe the clip to the plan dims so
        # every downstream file keeps the style resolution (no-op when equal).
        ensure_video_resolution(raw, vid_width, vid_height)

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
