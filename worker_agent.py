#!/usr/bin/env python3
"""Durable worker agent for ComfyUI, TTS, and local assembly tasks.

The web app can launch the resumable generator directly.  This agent is the
durable execution path for running workers as independent daemons:
each agent leases one ready task from the SQLite controller, heartbeats while it
runs, and records artifacts before taking another task.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.assembler import (  # noqa: E402
    _get_duration,
    FINAL_SCENE_TAIL_SECS,
    concatenate_scenes,
    ensure_video_resolution,
    mix_background_music,
    mux_video_audio,
)
from pipeline import engines  # noqa: E402
from pipeline.comfyui import generate_music, generate_with_engine  # noqa: E402
from pipeline import image_history  # noqa: E402
from pipeline.cover import (  # noqa: E402
    build_cover_prompt,
    cover_dimensions,
)
from pipeline.cover_typography import COVER_BASE_NAME, apply_cover_typography  # noqa: E402
from pipeline.llm import Scene  # noqa: E402
from pipeline.orchestrator import (  # noqa: E402
    DurableStore,
    TaskRecord,
    TaskRun,
    default_db_path,
    worker_id,
)
from pipeline.scene_video import generate_scene_video as generate_scene_video_task  # noqa: E402
from pipeline.tts_text import spoken_source  # noqa: E402
from pipeline.tts_worker import generate_narration, resolve_robotic_amount  # noqa: E402


LOG = logging.getLogger("spielbot.worker_agent")


class RetryLater(RuntimeError):
    """Task payload is not ready yet, but the task should remain retryable."""


def _path(payload: dict, key: str, default: Path) -> Path:
    value = payload.get(key)
    return Path(value).expanduser() if value else default


def _execute_scene_image(store: DurableStore, task: TaskRecord, endpoint: str) -> None:
    p = task.payload
    work_dir = Path(p["work_dir"]).expanduser()
    sid = int(p["scene_id"])
    output = _path(p, "output_path", work_dir / f"scene_{sid:02d}_first_frame.png")
    if store.skip_task_if_artifact_exists(task.id, output, artifact_kind="image", min_size=1000):
        return

    # The payload carries the style's engine key; resolve() falls back to the
    # default engine, with flux1-schnell's filenames overridable by the
    # payload's legacy flux_* keys.
    generate_with_engine(
        engines.resolve(p, p.get("image_engine")),
        p.get("image_prompt", ""),
        output,
        width=int(p.get("width", p.get("vid_width", 1024))),
        height=int(p.get("height", p.get("vid_height", 576))),
        comfy_url=endpoint,
    )
    store.record_artifact(task.job_id, task.id, "image", output, width=int(p.get("width", 0)) or None, height=int(p.get("height", 0)) or None)
    store.complete_task(task.id, result={"path": str(output)}, message="image ready")


def _execute_narration(store: DurableStore, task: TaskRecord, endpoint: str) -> None:
    p = task.payload
    work_dir = Path(p["work_dir"]).expanduser()
    sid = int(p["scene_id"])
    output = _path(p, "output_path", work_dir / f"scene_{sid:02d}_narration.wav")
    if store.skip_task_if_artifact_exists(task.id, output, artifact_kind="narration", min_size=1000):
        return

    # Spoken-text override (metadata.tts_text) wins over the narration/caption
    # text; see pipeline/tts_text.py for the disentanglement.
    narration_text = spoken_source(p.get("narration") or "", p.get("tts_text")).strip()
    if not narration_text:
        # Fall back to the scene title so TTS never receives blank text,
        # which causes F5-TTS to emit boilerplate audio.
        narration_text = (p.get("title") or f"Scene {sid}").strip()
        LOG.warning("Scene %d has empty narration — using title as TTS fallback: %r", sid, narration_text)

    ref = Path(p["voice_ref"]).expanduser() if p.get("voice_ref") else None
    generate_narration(narration_text, output, reference_wav=ref, host=endpoint,
                       robotic_amount=resolve_robotic_amount(p),
                       speed=p.get("voice_speed"),
                       tts_engine=p.get("tts_engine") or "openf5",
                       language=p.get("tts_language") or "en",
                       sentence_pause=p.get("tts_sentence_pause"),
                       cadence_voice=p.get("voice_name"))
    duration = _get_duration(output)
    store.record_artifact(task.job_id, task.id, "narration", output, duration_seconds=duration)
    store.complete_task(task.id, result={"path": str(output), "duration": duration}, message="narration ready")


def _execute_music(store: DurableStore, task: TaskRecord, endpoint: str) -> None:
    p = task.payload
    duration = p.get("duration_seconds")
    if not duration:
        raise RetryLater("music duration is not available yet")
    work_dir = Path(p["work_dir"]).expanduser()
    output = _path(p, "output_path", work_dir / "background_music.wav")
    if store.skip_task_if_artifact_exists(task.id, output, artifact_kind="music", min_size=10_000):
        return

    generate_music(
        p.get("title", "Stephen Spielbot"),
        float(duration),
        output,
        p.get("music_desc") or None,
        comfy_url=endpoint,
        music_engine=p.get("music_engine"),
        lyrics=p.get("music_lyrics") or None,
    )
    actual_duration = _get_duration(output)
    store.record_artifact(task.job_id, task.id, "music", output, duration_seconds=actual_duration)
    store.complete_task(task.id, result={"path": str(output), "duration": actual_duration}, message="music ready")


def _execute_scene_video(store: DurableStore, task: TaskRecord, endpoint: str) -> None:
    p = task.payload
    work_dir = Path(p["work_dir"]).expanduser()
    sid = int(p["scene_id"])
    narration_duration = p.get("narration_duration")
    if not narration_duration:
        narration_path = work_dir / f"scene_{sid:02d}_narration.wav"
        if not narration_path.exists():
            raise RetryLater(f"narration missing for scene {sid}")
        narration_duration = _get_duration(narration_path)

    output = work_dir / f"scene_{sid:02d}_video.mp4"
    single_clip = work_dir / f"scene_{sid:02d}_clip_01.mp4"
    if output.exists() and output.stat().st_size > 10_000:
        store.record_artifact(task.job_id, task.id, "scene_video", output, duration_seconds=_get_duration(output))
        store.complete_task(task.id, result={"path": str(output), "skipped": True})
        return
    if single_clip.exists() and single_clip.stat().st_size > 10_000 and not (work_dir / f"scene_{sid:02d}_clip_02.mp4").exists():
        store.record_artifact(task.job_id, task.id, "scene_video", single_clip, duration_seconds=_get_duration(single_clip))
        store.complete_task(task.id, result={"path": str(single_clip), "skipped": True})
        return

    scene = Scene(
        id=sid,
        title=p.get("title", f"Scene {sid}"),
        image_prompt=p.get("image_prompt", ""),
        video_prompt=p.get("video_prompt", ""),
        narration=p.get("narration", ""),
        negative_prompt=p.get("negative_prompt", ""),
    )
    first_frame = Path(p["first_frame"]).expanduser() if p.get("first_frame") else work_dir / f"scene_{sid:02d}_first_frame.png"
    image_engine = engines.resolve(p, p.get("image_engine"))
    video_engine = engines.resolve_video(p, p.get("video_engine"))
    if video_engine.get("lease_seconds"):
        # Slow engines (MiniMax H3) outlive the agent's claim-time lease; extend
        # it so the controller doesn't re-lease this scene mid-render.
        store.heartbeat_task(task.id, lease_seconds=int(video_engine["lease_seconds"]))
    scene_video, ambient = generate_scene_video_task(
        scene,
        work_dir,
        float(narration_duration),
        int(p.get("vid_width", 832)),
        int(p.get("vid_height", 480)),
        float(p.get("max_clip_secs", 12.0)),
        float(p.get("lora_strength", 0.5)),
        float(p.get("first_pass_cfg", 1.0)),
        int(p.get("first_pass_steps", 8)),
        float(p.get("second_pass_cfg", 3.0)),
        int(p.get("second_pass_steps", 6)),
        endpoint,
        first_frame if first_frame.exists() else None,
        image_engine,
        video_engine=video_engine,
    )
    store.record_artifact(task.job_id, task.id, "scene_video", scene_video, duration_seconds=_get_duration(scene_video))
    if ambient:
        store.record_artifact(task.job_id, task.id, "ambient", ambient, duration_seconds=_get_duration(ambient))
    store.complete_task(
        task.id,
        result={"path": str(scene_video), "ambient_path": str(ambient) if ambient else ""},
        message="scene video ready",
    )


def _execute_scene_mux(store: DurableStore, task: TaskRecord) -> None:
    p = task.payload
    work_dir = Path(p["work_dir"]).expanduser()
    sid = int(p["scene_id"])
    output = work_dir / f"scene_{sid:02d}_final.mp4"
    if store.skip_task_if_artifact_exists(task.id, output, artifact_kind="scene_final", min_size=10_000):
        return
    raw = Path(p["video_path"]).expanduser() if p.get("video_path") else work_dir / f"scene_{sid:02d}_video.mp4"
    if not raw.exists():
        raw = work_dir / f"scene_{sid:02d}_clip_01.mp4"
    narration = Path(p["narration_path"]).expanduser() if p.get("narration_path") else work_dir / f"scene_{sid:02d}_narration.wav"
    extra_tail = FINAL_SCENE_TAIL_SECS if p.get("is_last_scene") else 0.0
    mux_video_audio(raw, narration, output, extra_tail_secs=extra_tail)
    store.record_artifact(task.job_id, task.id, "scene_final", output, duration_seconds=_get_duration(output))
    store.complete_task(task.id, result={"path": str(output)}, message="scene muxed")


def _execute_final(store: DurableStore, task: TaskRecord) -> None:
    p = task.payload
    work_dir = Path(p["work_dir"]).expanduser()
    scene_count = int(p.get("scene_count", 0))
    if not scene_count:
        raise RetryLater("final assembly needs scene_count")
    final_path = Path(p["final_path"]).expanduser()
    if store.skip_task_if_artifact_exists(task.id, final_path, artifact_kind="final_video", min_size=10_000):
        return

    scene_finals = [work_dir / f"scene_{i:02d}_final.mp4" for i in range(1, scene_count + 1)]
    combined = work_dir / "combined.mp4"
    music_path = Path(p.get("music_path", work_dir / "background_music.wav")).expanduser()
    concatenate_scenes(scene_finals, combined)
    mix_background_music(
        combined,
        music_path,
        final_path,
        volume=float(p.get("music_vol", 0.18)),
        voice_volume=float(p.get("voice_vol", 1.0)),
        ambient_path=Path(p["ambient_path"]).expanduser() if p.get("ambient_path") else None,
        ambient_volume=float(p.get("ambient_vol", 0.0)),
    )
    if p.get("vid_width") and p.get("vid_height"):
        ensure_video_resolution(final_path, int(p["vid_width"]), int(p["vid_height"]))
    store.record_artifact(task.job_id, task.id, "final_video", final_path, duration_seconds=_get_duration(final_path))
    store.complete_task(task.id, result={"path": str(final_path)}, message="final video ready")


def _execute_ui_cover(store: DurableStore, task: TaskRecord, endpoint: str) -> None:
    p = task.payload
    work_dir = Path(p["work_dir"]).expanduser()
    title = (p.get("title") or "").strip()

    # Load scenes for richer prompt context.
    try:
        rows = store.scene_rows(task.job_id) or []
    except Exception:
        rows = []
    if not rows:
        # Fallback: read script.json from disk.
        script_path = work_dir / "script.json"
        if script_path.exists():
            import json as _json
            try:
                data = _json.loads(script_path.read_text())
                rows = data if isinstance(data, list) else (data.get("scenes") or [])
            except Exception:
                rows = []

    # Cover typography (the only cover mode): the model paints a TEXT-FREE
    # background and the film's cover phrase is composited on top with real
    # fonts — regenerating rerolls only the artwork. The payload carries the
    # style's settings and engine; tasks queued by an older backend fall back
    # to the film's stamped job_config (else the defaults).
    import json as _json
    try:
        jc = _json.loads((work_dir / "job_config.json").read_text())
    except Exception:
        jc = {}
    typo = p.get("cover_typography")
    if not isinstance(typo, dict) or not typo:
        typo = jc.get("cover_typography") or {}
    # The backend builds the prompt (composed visual style + character
    # reference notes) and attaches the reference image paths — the cover gets
    # the same conditioning as scene stills. Payloads from older backends fall
    # back to a locally built prompt without references.
    prompt = p.get("prompt") or build_cover_prompt(
        p.get("style") or "", scenes=rows,
        instruction=p.get("instruction") or "",
        text_position=str(typo.get("position") or ""))
    ref_images = [Path(r) for r in (p.get("reference_images") or []) if r]

    cover_path = work_dir / "cover.png"
    # Match the cover orientation to the rendered video (portrait/landscape/square).
    cover_w, cover_h = cover_dimensions(
        int(p.get("vid_width") or 0), int(p.get("vid_height") or 0)
    )
    # Use the endpoint selected at task-creation time (render-aware routing);
    # fall back to the worker's own endpoint if not set.
    comfy_url = p.get("comfy_url") or endpoint
    # Keep the previous cover so the user can return to it (same as scenes).
    image_history.cover_seed_if_empty(work_dir, cover_path)
    engine = p.get("engine")
    # Engine-less payloads (tasks queued before per-style engines) resolve the
    # film's stamped image engine — the cover must match the scenes' model —
    # with flux1-schnell filename overrides from the flux_* keys.
    if not isinstance(engine, dict) or not engine:
        engine = engines.resolve(p, p.get("image_engine") or jc.get("image_engine"))
    generate_with_engine(
        engine, prompt, work_dir / COVER_BASE_NAME,
        width=cover_w, height=cover_h, comfy_url=comfy_url,
        reference_images=ref_images or None,
    )
    apply_cover_typography(work_dir, typo, title)
    image_history.cover_record(work_dir, cover_path)
    store.record_artifact(task.job_id, task.id, "cover_image", cover_path)
    store.complete_task(task.id, result={"path": str(cover_path)}, message="cover ready")


def execute_task(store: DurableStore, task: TaskRecord, endpoint: str, lease_seconds: int) -> None:
    with TaskRun(
        store,
        task.id,
        worker_id_value=task.lease_owner,
        lease_seconds=lease_seconds,
        retryable=True,
        start_message=f"running {task.kind}",
    ):
        if task.kind == "scene.image.generate":
            _execute_scene_image(store, task, endpoint)
        elif task.kind == "scene.narration.generate":
            _execute_narration(store, task, endpoint)
        elif task.kind == "music.generate":
            _execute_music(store, task, endpoint)
        elif task.kind == "scene.video.generate":
            _execute_scene_video(store, task, endpoint)
        elif task.kind == "scene.video.mux":
            _execute_scene_mux(store, task)
        elif task.kind == "video.finalize":
            _execute_final(store, task)
        elif task.kind == "ui.cover.generate":
            _execute_ui_cover(store, task, endpoint)
        else:
            raise RuntimeError(f"unsupported task kind: {task.kind}")


def run_agent(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO if not args.debug else logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    store = DurableStore(args.db)
    wid = args.worker_id or worker_id(args.kind, args.endpoint)
    store.register_worker(wid, args.kind, args.endpoint, metadata={"pid": os.getpid()})
    LOG.info("worker %s online kind=%s endpoint=%s", wid, args.kind, args.endpoint)

    try:
        while True:
            store.heartbeat_worker(wid)
            task = store.acquire_next_task(wid, args.kind, lease_seconds=args.lease_seconds)
            if task is None:
                if args.once:
                    return 0
                time.sleep(args.poll_interval)
                continue
            LOG.info("leased %s (%s attempt %d/%d)", task.id, task.kind, task.attempt, task.max_attempts)
            try:
                store.heartbeat_worker(wid, active_task_id=task.id)
                execute_task(store, task, args.endpoint, args.lease_seconds)
            except RetryLater as exc:
                LOG.info("retry later %s: %s", task.id, exc)
                store.fail_task(task.id, exc, retryable=True)
            except Exception as exc:
                LOG.exception("task failed: %s", task.id)
                store.fail_task(task.id, exc, retryable=True)
            finally:
                store.heartbeat_worker(wid, active_task_id=None)
            if args.once:
                return 0
    finally:
        store.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a durable Stephen Spielbot worker agent.")
    parser.add_argument("--db", type=Path, default=default_db_path(), help="SQLite orchestration DB path")
    parser.add_argument("--kind", choices=["comfy", "tts", "local", "ui"], required=True, help="Task kind this worker leases")
    parser.add_argument("--endpoint", required=True, help="ComfyUI URL, TTS host, or local label")
    parser.add_argument("--worker-id", default="", help="Stable worker id override")
    parser.add_argument("--lease-seconds", type=int, default=900)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="Lease at most one task, then exit")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run_agent(parse_args(sys.argv[1:])))
