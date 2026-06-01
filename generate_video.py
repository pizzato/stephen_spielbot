#!/usr/bin/env python3
"""AI video generator — generates a narrated MP4 from a title prompt."""

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from pipeline.llm import generate_script
from pipeline.comfyui import generate_video_clip, generate_music
from pipeline.tts import generate_narration, DEFAULT_VOICE
from pipeline.assembler import _get_duration, concat_clips, mux_video_audio, concatenate_scenes, mix_background_music


CLIP_DURATION = 5.0  # seconds per generated video clip (natural Wan 2.2 length)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def step(msg: str) -> None:
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a narrated video from a title.")
    parser.add_argument("title", help="Video topic / title")
    parser.add_argument("--scenes", type=int, default=8, help="Number of scenes (default: 8)")
    parser.add_argument("--output", type=Path, default=Path.home() / "videos", help="Output directory")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Kokoro voice ID (ignored if --voice-ref is set)")
    parser.add_argument("--music-vol", type=float, default=0.18, metavar="0-1",
                        help="Background music volume 0.0-1.0 (default: 0.18)")
    parser.add_argument("--voice-ref", type=Path, default=None, metavar="WAV",
                        help="Reference WAV for voice cloning via XTTS v2 (overrides --voice)")
    args = parser.parse_args()

    if args.voice_ref and not args.voice_ref.exists():
        print(f"[ERROR] Voice reference file not found: {args.voice_ref}", file=sys.stderr)
        sys.exit(1)

    args.output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = slugify(args.title)
    work_dir = args.output / f"{slug}-{stamp}"
    work_dir.mkdir()

    # --- Step 1: Generate script ---
    step(f"Generating {args.scenes}-scene script for: {args.title!r}")
    scenes = generate_script(args.title, args.scenes)
    print(f"  → {len(scenes)} scenes generated")
    for s in scenes:
        print(f"     Scene {s.id}: {s.title}")

    script_path = work_dir / "script.json"
    script_path.write_text(json.dumps(
        [{"id": s.id, "title": s.title,
          "image_prompt": s.image_prompt, "video_prompt": s.video_prompt,
          "narration": s.narration}
         for s in scenes],
        indent=2,
    ))

    # --- Step 2: Generate ALL narrations first, measure durations, plan clips ---
    if args.voice_ref:
        from pipeline.tts_xtts import generate_narration_cloned
        step(f"Generating narrations with cloned voice ({args.voice_ref.name}) and planning clips")
    else:
        step("Generating narrations and planning video clips")

    narration_paths: dict[int, Path] = {}
    narration_durs: dict[int, float] = {}
    clip_counts: dict[int, int] = {}

    for scene in scenes:
        narration_path = work_dir / f"scene_{scene.id:02d}_narration.wav"
        if args.voice_ref:
            generate_narration_cloned(scene.narration, args.voice_ref, narration_path)
        else:
            generate_narration(scene.narration, narration_path, voice_id=args.voice)
        dur = _get_duration(narration_path)
        n_clips = max(1, math.ceil(dur / CLIP_DURATION))
        narration_paths[scene.id] = narration_path
        narration_durs[scene.id] = dur
        clip_counts[scene.id] = n_clips
        print(f"  Scene {scene.id}: {dur:.1f}s → {n_clips} clip{'s' if n_clips > 1 else ''}", flush=True)

    total_dur = sum(narration_durs.values())
    total_clips = sum(clip_counts.values())
    print(f"\n  Total: {total_dur:.1f}s narration — {total_clips} clips to generate")

    # --- Step 3: Generate background music (exact duration now known) ---
    music_duration = max(total_dur * 1.05, 30.0)
    step(f"Generating background music ({music_duration:.0f}s)")
    music_path = work_dir / "background_music.wav"
    generate_music(args.title, music_duration, music_path)
    print(f"  → {music_path.name}")

    # --- Step 4: Generate video clips for each scene ---
    scene_finals: list[Path] = []
    for scene in scenes:
        n_clips = clip_counts[scene.id]
        narration_dur = narration_durs[scene.id]
        step(f"Scene {scene.id}/{len(scenes)}: {scene.title}  ({n_clips} clip{'s' if n_clips > 1 else ''} × {CLIP_DURATION:.0f}s)")

        clips: list[Path] = []
        for k in range(n_clips):
            print(f"  Clip {k+1}/{n_clips}…", flush=True)
            clip_path = work_dir / f"scene_{scene.id:02d}_clip_{k+1:02d}.mp4"
            generate_video_clip(
                positive_prompt=scene.video_prompt,
                negative_prompt=scene.negative_prompt,
                output_path=clip_path,
            )
            clips.append(clip_path)

        # Concatenate clips (hard cuts) then trim to exact narration length
        if len(clips) == 1:
            raw_video = clips[0]
        else:
            raw_video = work_dir / f"scene_{scene.id:02d}_video.mp4"
            concat_clips(clips, raw_video)

        scene_final = work_dir / f"scene_{scene.id:02d}_final.mp4"
        extra_tail = 2.0 if scene is scenes[-1] else 0.0
        mux_video_audio(raw_video, narration_paths[scene.id], scene_final, extra_tail_secs=extra_tail)
        scene_finals.append(scene_final)
        print(f"  → {scene_final.name}")

    # --- Step 5: Assemble final video ---
    step("Concatenating scenes with crossfades")
    combined = work_dir / "combined.mp4"
    concatenate_scenes(scene_finals, combined)

    step("Mixing background music")
    final_path = args.output / f"{slug}-{stamp}.mp4"
    mix_background_music(combined, music_path, final_path, volume=args.music_vol)

    step(f"Done! Output: {final_path}")
    size_mb = final_path.stat().st_size / 1024 / 1024
    print(f"  Size: {size_mb:.1f} MB")
    print(f"  Script: {script_path}")


if __name__ == "__main__":
    t0 = time.time()
    main()
    elapsed = time.time() - t0
    print(f"\nTotal time: {elapsed/60:.1f} min")
