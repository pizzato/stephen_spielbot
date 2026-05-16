"""ffmpeg-based video assembly operations."""

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("video_gen")

# Per-call ffmpeg timeout in seconds. Override with FFMPEG_TIMEOUT env var.
_FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "600"))


def _run(cmd: list[str], timeout: int = _FFMPEG_TIMEOUT, max_retries: int = 2) -> None:
    label = f"ffmpeg {' '.join(str(a) for a in cmd[1:4])}…"
    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        logger.debug("[ffmpeg] attempt %d/%d: %s", attempt, max_retries, label)
        t0 = time.monotonic()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            last_err = RuntimeError(f"ffmpeg timed out after {elapsed:.0f}s: {label}")
            logger.warning("[ffmpeg] %s (attempt %d/%d)", last_err, attempt, max_retries)
            if attempt < max_retries:
                time.sleep(3 * attempt)
                continue
            raise last_err
        elapsed = time.monotonic() - t0
        if result.returncode != 0:
            last_err = RuntimeError(f"ffmpeg failed ({elapsed:.1f}s):\n{result.stderr[-2000:]}")
            logger.warning("[ffmpeg] failed (attempt %d/%d): %s", attempt, max_retries, str(last_err)[:300])
            if attempt < max_retries:
                time.sleep(3 * attempt)
                continue
            raise last_err
        logger.debug("[ffmpeg] done: %s (%.1fs)", label, elapsed)
        return


def _get_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float(result.stdout.strip())


def concat_clips(clip_paths: list[Path], output_path: Path) -> Path:
    """Concatenate multiple short clips with stream copy (hard cuts, no re-encode)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{p.resolve()}'\n")
        concat_list = Path(f.name)

    _run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(output_path),
    ])
    concat_list.unlink(missing_ok=True)
    return output_path


def extract_first_frame(video_path: Path, output_path: Path) -> Path:
    """Extract the first frame of a video to an image file."""
    _run([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_path),
    ], timeout=60)
    return output_path


def extract_last_frame(video_path: Path, output_path: Path) -> Path:
    """Extract the last frame of a video to an image file."""
    _run([
        "ffmpeg", "-y",
        "-sseof", "-1",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_path),
    ], timeout=60)
    return output_path


def extract_audio(video_path: Path, output_path: Path, duration: float | None = None) -> Path:
    """Extract the audio track from a video file, optionally trimmed to duration."""
    dur_flag = ["-t", str(duration)] if duration is not None else []
    _run([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-map", "0:a:0",
        *dur_flag,
        "-c:a", "pcm_s16le",
        str(output_path),
    ])
    return output_path


def concat_audio(audio_paths: list[Path], output_path: Path) -> Path:
    """Concatenate multiple audio files sequentially."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in audio_paths:
            f.write(f"file '{p.resolve()}'\n")
        concat_list = Path(f.name)
    _run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(output_path),
    ])
    concat_list.unlink(missing_ok=True)
    return output_path


def mux_video_audio(video_path: Path, audio_path: Path, output_path: Path) -> Path:
    """Mux video with narration audio, discarding any audio track on the video."""
    audio_dur = _get_duration(audio_path)
    video_dur = _get_duration(video_path)

    if video_dur >= audio_dur:
        # Video covers the full narration — simple stream copy, trim to audio length.
        _run([
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", str(audio_dur),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ])
    else:
        # Video is slightly shorter than narration (ComfyUI frame-count rounding).
        # Freeze the last frame to fill the gap instead of looping from the start —
        # looping caused the beginning of the scene to flash before the next scene.
        gap = audio_dur - video_dur
        logger.info(
            "[ffmpeg] mux_video_audio: video=%.3fs audio=%.3fs — freezing last frame %.3fs",
            video_dur, audio_dur, gap,
        )
        _run([
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={gap:.3f}[vout]",
            "-map", "[vout]",
            "-map", "1:a:0",
            "-t", str(audio_dur),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ])
    return output_path


def concatenate_scenes(scene_paths: list[Path], output_path: Path, fade: float = 0.3) -> Path:
    """Concatenate scenes with fade-out/fade-in between them."""
    if len(scene_paths) == 1:
        shutil.copy2(scene_paths[0], output_path)
        return output_path

    n = len(scene_paths)
    durations = [_get_duration(p) for p in scene_paths]
    logger.info("[ffmpeg] concatenate_scenes: %d scenes, total %.1fs", n, sum(durations))

    inputs = []
    for p in scene_paths:
        inputs += ["-i", str(p)]

    filters = []
    fade_a = 0.05

    for i in range(n):
        dur = durations[i]

        # setpts=PTS-STARTPTS normalises each clip's PTS to start at 0 so that
        # fade=t=in:st=0 actually covers the very first frame.  Without this,
        # clips muxed with -c:v copy retain the original ComfyUI PTS (which may
        # be non-zero), causing the first frame(s) to flash through un-faded.
        vf = ["setpts=PTS-STARTPTS"]
        if i > 0:
            vf.append(f"fade=t=in:st=0:d={fade:.2f}")
        if i < n - 1:
            vf.append(f"fade=t=out:st={dur - fade:.3f}:d={fade:.2f}")
        filters.append(f"[{i}:v]{','.join(vf)}[fv{i}]")

        af = ["asetpts=PTS-STARTPTS"]
        if i > 0:
            af.append(f"afade=t=in:st=0:d={fade_a:.2f}")
        if i < n - 1:
            af.append(f"afade=t=out:st={dur - fade_a:.3f}:d={fade_a:.2f}")
        filters.append(f"[{i}:a]{','.join(af)}[fa{i}]")

    v_in = "".join(f"[fv{i}]" for i in range(n))
    a_in = "".join(f"[fa{i}]" for i in range(n))
    filters.append(f"{v_in}concat=n={n}:v=1:a=0[vout]")
    filters.append(f"{a_in}concat=n={n}:v=0:a=1[aout]")

    # Allow up to 30 min for full re-encode of many scenes
    _run([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ], timeout=1800)
    return output_path


def upscale_video(input_path: Path, output_path: Path, scale: int = 2) -> Path:
    """Upscale video by an integer factor using lanczos resampling."""
    _run([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf", f"scale=iw*{scale}:ih*{scale}:flags=lanczos",
        "-c:v", "libx264", "-crf", "16", "-preset", "slow",
        "-c:a", "copy",
        str(output_path),
    ], timeout=3600)
    return output_path


def mix_background_music(
    video_path: Path,
    music_path: Path,
    output_path: Path,
    volume: float = 0.18,
    voice_volume: float = 1.0,
    ambient_path: Path | None = None,
    ambient_volume: float = 0.0,
) -> Path:
    """Mix narration voice, background music, and optional LTX ambient audio."""
    video_dur = _get_duration(video_path)
    music_dur = _get_duration(music_path)
    logger.info("[ffmpeg] mix_background_music: video=%.1fs music=%.1fs voice_vol=%.2f music_vol=%.2f",
                video_dur, music_dur, voice_volume, volume)

    use_ambient = ambient_path and Path(ambient_path).exists() and ambient_volume > 0
    if use_ambient:
        filter_str = (
            f"[0:a]volume={voice_volume:.3f}[voice];"
            f"[1:a]volume={volume:.3f}[bg];"
            f"[2:a]volume={ambient_volume:.3f}[amb];"
            "[voice][bg][amb]amix=inputs=3:duration=first:dropout_transition=3:normalize=0[aout]"
        )
        inputs = ["-i", str(video_path), "-i", str(music_path), "-i", str(ambient_path)]
    else:
        filter_str = (
            f"[0:a]volume={voice_volume:.3f}[voice];"
            f"[1:a]volume={volume:.3f}[bg];"
            "[voice][bg]amix=inputs=2:duration=first:dropout_transition=3:normalize=0[aout]"
        )
        inputs = ["-i", str(video_path), "-i", str(music_path)]

    try:
        _run([
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", filter_str,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ])
    except Exception as e:
        logger.warning("[ffmpeg] mix_background_music failed after retries: %s — copying video without music", e)
        shutil.copy2(video_path, output_path)
    return output_path
