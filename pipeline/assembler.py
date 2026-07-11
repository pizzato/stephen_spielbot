"""ffmpeg-based video assembly operations."""

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("video_gen")

# Per-call ffmpeg timeout in seconds. Override with FFMPEG_TIMEOUT env var.
_FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "600"))


def _resolve_media_tool(name: str) -> str:
    env_name = f"{name.upper()}_PATH"
    configured = os.environ.get(env_name)
    if configured:
        return configured

    found = shutil.which(name)
    if found:
        return found

    for candidate in (
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
        Path("/opt/local/bin") / name,
        Path("/usr/bin") / name,
    ):
        if candidate.exists():
            return str(candidate)

    return name


_FFMPEG = _resolve_media_tool("ffmpeg")
_FFPROBE = _resolve_media_tool("ffprobe")
_TEMPORAL_UPSCALE_CHUNK_SECONDS = float(os.environ.get("TEMPORAL_VIDEO_UPSCALE_CHUNK_SECONDS", "4"))

# Open-source attribution stamped into the published final video's container
# metadata. A plain `comment` tag survives the downstream re-encode/upscale
# steps (ffmpeg copies global metadata from the first input by default).
_ATTRIBUTION_COMMENT = "Generated with Stephen Spielbot (https://github.com/pizzato/stephen_spielbot)"


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
        [_FFPROBE, "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float(result.stdout.strip())


def _get_video_dimensions(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            _FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed:\n{result.stderr[-2000:]}")
    width_s, height_s = result.stdout.strip().split("x", 1)
    return int(width_s), int(height_s)


def _get_video_fps(path: Path) -> float:
    result = subprocess.run(
        [
            _FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate,r_frame_rate",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed:\n{result.stderr[-2000:]}")
    try:
        import json

        streams = json.loads(result.stdout or "{}").get("streams") or []
        stream = streams[0] if streams else {}
        for key in ("avg_frame_rate", "r_frame_rate"):
            value = str(stream.get(key) or "")
            if "/" in value:
                num, den = value.split("/", 1)
                fps = float(num) / float(den)
            else:
                fps = float(value)
            if fps > 0:
                return fps
    except Exception:
        pass
    return 25.0


def ensure_video_resolution(video_path: Path, width: int, height: int) -> Path:
    """Reframe final output to the exact selected resolution when a backend rounds it."""
    actual_w, actual_h = _get_video_dimensions(video_path)
    if (actual_w, actual_h) == (width, height):
        return video_path

    tmp_path = video_path.with_name(f"{video_path.stem}.{width}x{height}.tmp{video_path.suffix}")
    logger.info(
        "[ffmpeg] normalize_resolution: %dx%d → %dx%d (%s)",
        actual_w, actual_h, width, height, video_path.name,
    )
    _run([
        _FFMPEG, "-y",
        "-i", str(video_path),
        "-vf", (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1"
        ),
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(tmp_path),
    ], timeout=1800)
    tmp_path.replace(video_path)
    return video_path


def upscale_video(input_path: Path, output_path: Path, width: int, height: int) -> Path:
    """Upscale a rendered video to an exact target frame size.

    This is a post-render path: it preserves timing/audio, uses high-quality
    Lanczos scaling, and writes to a separate output so callers can atomically
    swap it in only after ffmpeg succeeds.
    """
    actual_w, actual_h = _get_video_dimensions(input_path)
    if actual_w >= width and actual_h >= height:
        raise ValueError(
            f"Target {width}x{height} is not larger than source {actual_w}x{actual_h}."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "[ffmpeg] upscale_video: %dx%d -> %dx%d (%s)",
        actual_w, actual_h, width, height, input_path.name,
    )
    _run([
        _FFMPEG, "-y",
        "-i", str(input_path),
        "-vf", (
            f"scale={width}:{height}:flags=lanczos:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1"
        ),
        "-c:v", "libx264", "-crf", "16", "-preset", "slow",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ], timeout=3600)
    return output_path


def temporal_ai_upscale_video(
    input_path: Path,
    output_path: Path,
    width: int,
    height: int,
    command_template: str | None = None,
    timeout_seconds: int | None = None,
    comfy_url: str | None = None,
    *,
    engine: str = "ic_lora",
) -> Path:
    """Run a ComfyUI (or custom CLI) AI video upscaler on one clip.

    *engine*:
      - ``ic_lora`` — Lightricks LTX-2.3 IC-LoRA Pixel Spatial Upscaler
        (generative 2×/4×; https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler)
      - ``ltx_latent`` — simpler LTXVLatentUpsampler + latent spatial-upscaler-x2 model

    An explicit *command_template* (or TEMPORAL_VIDEO_UPSCALER_CMD) overrides both
    and runs as a shell command instead.
    """
    actual_w, actual_h = _get_video_dimensions(input_path)
    if actual_w >= width and actual_h >= height:
        raise ValueError(
            f"Target {width}x{height} is not larger than source {actual_w}x{actual_h}."
        )

    eng = (engine or "ic_lora").strip().lower()
    if eng in {"temporal_ai", "ic-lora", "iclora"}:
        eng = "ic_lora"
    if eng in {"latent", "latent_ai", "ltx_latent_upsampler"}:
        eng = "ltx_latent"
    if eng not in {"ic_lora", "ltx_latent"}:
        raise ValueError(f"Unknown AI upscale engine: {engine!r}")

    template = command_template or os.environ.get("TEMPORAL_VIDEO_UPSCALER_CMD", "")
    timeout = timeout_seconds or int(os.environ.get("TEMPORAL_VIDEO_UPSCALER_TIMEOUT", "7200"))
    if not template.strip():
        from pipeline.comfyui import upscale_video_ltx, upscale_video_ltx_latent

        duration = _get_duration(input_path)
        fps = _get_video_fps(input_path)
        chunk_seconds = max(1.0, float(os.environ.get(
            "TEMPORAL_VIDEO_UPSCALE_CHUNK_SECONDS",
            str(_TEMPORAL_UPSCALE_CHUNK_SECONDS),
        )))
        url = comfy_url or "http://localhost:8188"

        def _comfy_upscale(inp, out, w, h, fps=fps, timeout_seconds=timeout, comfy_url=None, **_kw):
            cu = comfy_url or url
            if eng == "ltx_latent":
                return upscale_video_ltx_latent(
                    inp, out, w, h,
                    fps=fps,
                    timeout_seconds=timeout_seconds,
                    comfy_url=cu,
                )
            return upscale_video_ltx(
                inp, out, w, h,
                fps=fps,
                timeout_seconds=timeout_seconds,
                comfy_url=cu,
                source_width=actual_w,
                source_height=actual_h,
            )

        if duration > chunk_seconds + 0.25:
            return _chunked_comfy_temporal_upscale(
                input_path,
                output_path,
                width,
                height,
                fps=fps,
                timeout_seconds=timeout,
                comfy_url=url,
                chunk_seconds=chunk_seconds,
                upscale_fn=_comfy_upscale,
            )

        if eng == "ltx_latent":
            return upscale_video_ltx_latent(
                input_path, output_path, width, height,
                fps=fps, timeout_seconds=timeout, comfy_url=url,
            )
        return upscale_video_ltx(
            input_path, output_path, width, height,
            fps=fps, timeout_seconds=timeout, comfy_url=url,
            source_width=actual_w, source_height=actual_h,
            duration_seconds=duration,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "input": shlex.quote(str(input_path)),
        "output": shlex.quote(str(output_path)),
        "width": str(int(width)),
        "height": str(int(height)),
    }
    try:
        cmd = shlex.split(template.format(**values))
    except KeyError as exc:
        raise RuntimeError(f"Unknown temporal upscaler placeholder: {exc}") from exc
    if not cmd:
        raise RuntimeError("Temporal AI upscaler command is empty.")

    logger.info(
        "[upscale] %s (cli): %dx%d -> %dx%d (%s)",
        eng, actual_w, actual_h, width, height, input_path.name,
    )
    _run(cmd, timeout=timeout, max_retries=1)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Temporal AI upscaler did not produce an output video.")
    return output_path


def _chunked_comfy_temporal_upscale(
    input_path: Path,
    output_path: Path,
    width: int,
    height: int,
    *,
    fps: float,
    timeout_seconds: int,
    comfy_url: str,
    chunk_seconds: float,
    upscale_fn,
) -> Path:
    """Upscale long videos in bounded chunks so ComfyUI does not hold all frames."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = _get_duration(input_path)
    n_chunks = max(1, int((duration + chunk_seconds - 0.001) // chunk_seconds))
    logger.info(
        "[comfy] chunked temporal upscale: %.1fs in %d chunks of %.1fs",
        duration, n_chunks, chunk_seconds,
    )
    with tempfile.TemporaryDirectory(prefix="spielbot-upscale-", dir=str(output_path.parent)) as tmp:
        tmp_dir = Path(tmp)
        upscaled: list[Path] = []
        for idx in range(n_chunks):
            start = idx * chunk_seconds
            remaining = max(0.0, duration - start)
            if remaining <= 0:
                break
            segment_duration = min(chunk_seconds, remaining)
            src_chunk = tmp_dir / f"chunk_{idx:04d}.mp4"
            out_chunk = tmp_dir / f"chunk_{idx:04d}.upscaled.mp4"
            _extract_temporal_chunk(input_path, src_chunk, start, segment_duration)
            upscale_fn(
                src_chunk,
                out_chunk,
                width,
                height,
                fps=fps,
                timeout_seconds=timeout_seconds,
                comfy_url=comfy_url,
            )
            upscaled.append(out_chunk)
        if not upscaled:
            raise RuntimeError("Temporal AI upscaler did not produce any chunks.")
        _concat_video_chunks(upscaled, output_path)
    return output_path


def _extract_temporal_chunk(input_path: Path, output_path: Path, start: float, duration: float) -> Path:
    _run([
        _FFMPEG, "-y",
        "-ss", f"{start:.3f}",
        "-i", str(input_path),
        "-t", f"{duration:.3f}",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ], timeout=1800)
    return output_path


def _concat_video_chunks(chunks: list[Path], output_path: Path) -> Path:
    list_path = output_path.with_suffix(".concat.txt")
    try:
        list_path.write_text(
            "".join(f"file {shlex.quote(str(chunk))}\n" for chunk in chunks),
            encoding="utf-8",
        )
        _run([
            _FFMPEG, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ], timeout=1800)
    finally:
        list_path.unlink(missing_ok=True)
    return output_path


def concatenate_scenes_hard_cut(scene_paths: list[Path], output_path: Path) -> Path:
    """Join scene clips back-to-back without fades or overlap."""
    if len(scene_paths) == 1:
        shutil.copy2(scene_paths[0], output_path)
        return output_path
    try:
        return _concat_video_chunks(scene_paths, output_path)
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        logger.warning("[ffmpeg] hard-cut stream concat failed; retrying with no-fade re-encode: %s", exc)
        return concatenate_scenes(scene_paths, output_path, fade=0.0)


def concat_clips(clip_paths: list[Path], output_path: Path) -> Path:
    """Concatenate multiple short clips with stream copy (hard cuts, no re-encode)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{p.resolve()}'\n")
        concat_list = Path(f.name)

    _run([
        _FFMPEG, "-y",
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
        _FFMPEG, "-y",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_path),
    ], timeout=60)
    return output_path


def extract_last_frame(video_path: Path, output_path: Path) -> Path:
    """Extract the last frame of a video to an image file."""
    _run([
        _FFMPEG, "-y",
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
        _FFMPEG, "-y",
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
        _FFMPEG, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(output_path),
    ])
    concat_list.unlink(missing_ok=True)
    return output_path


def mux_video_audio(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    extra_tail_secs: float = 0.0,
) -> Path:
    """Mux video with narration audio, discarding any audio track on the video.

    Both branches re-encode the video so the output duration exactly matches the
    audio duration.  Stream copy (-c:v copy) cannot cut at non-keyframe
    boundaries, leaving the video track a full frame (~40 ms at 25 fps) longer
    than the audio track.  Across many scenes this drift accumulates: narration
    starts noticeably early relative to the video.

    extra_tail_secs: freeze the last video frame for this many additional seconds
    after narration ends (useful for the final scene to avoid an abrupt cut).
    """
    audio_dur = _get_duration(audio_path)
    video_dur = _get_duration(video_path)
    target_dur = audio_dur + extra_tail_secs

    if video_dur >= target_dur:
        logger.debug(
            "[ffmpeg] mux_video_audio: video=%.3fs audio=%.3fs target=%.3fs — trimming to target",
            video_dur, audio_dur, target_dur,
        )
        _run([
            _FFMPEG, "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", str(target_dur),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ])
    else:
        # Video is shorter than the target duration (ComfyUI frame-count rounding,
        # or intentional extra tail). Freeze the last frame to fill the gap instead
        # of looping from the start — looping caused the beginning of the scene to
        # flash before the next scene.
        gap = target_dur - video_dur
        logger.info(
            "[ffmpeg] mux_video_audio: video=%.3fs audio=%.3fs target=%.3fs — freezing last frame %.3fs",
            video_dur, audio_dur, target_dur, gap,
        )
        _run([
            _FFMPEG, "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={gap:.3f}[vout]",
            "-map", "[vout]",
            "-map", "1:a:0",
            "-t", str(target_dur),
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
    fade_a = 0.05 if fade > 0 else 0.0

    for i in range(n):
        dur = durations[i]

        # setpts=PTS-STARTPTS normalises each clip's PTS to start at 0 so that
        # fade=t=in:st=0 actually covers the very first frame.  Without this,
        # clips muxed with -c:v copy retain the original ComfyUI PTS (which may
        # be non-zero), causing the first frame(s) to flash through un-faded.
        vf = ["setpts=PTS-STARTPTS"]
        if fade > 0 and i > 0:
            vf.append(f"fade=t=in:st=0:d={fade:.2f}")
        if fade > 0 and i < n - 1:
            vf.append(f"fade=t=out:st={dur - fade:.3f}:d={fade:.2f}")
        filters.append(f"[{i}:v]{','.join(vf)}[fv{i}]")

        af = ["asetpts=PTS-STARTPTS"]
        if fade_a > 0 and i > 0:
            af.append(f"afade=t=in:st=0:d={fade_a:.2f}")
        if fade_a > 0 and i < n - 1:
            af.append(f"afade=t=out:st={dur - fade_a:.3f}:d={fade_a:.2f}")
        filters.append(f"[{i}:a]{','.join(af)}[fa{i}]")

    v_in = "".join(f"[fv{i}]" for i in range(n))
    a_in = "".join(f"[fa{i}]" for i in range(n))
    filters.append(f"{v_in}concat=n={n}:v=1:a=0[vout]")
    filters.append(f"{a_in}concat=n={n}:v=0:a=1[aout]")

    # Allow up to 30 min for full re-encode of many scenes
    _run([
        _FFMPEG, "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ], timeout=1800)
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
            _FFMPEG, "-y",
            *inputs,
            "-filter_complex", filter_str,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-metadata", f"comment={_ATTRIBUTION_COMMENT}",
            str(output_path),
        ])
    except Exception as e:
        logger.warning("[ffmpeg] mix_background_music failed after retries: %s — copying video without music", e)
        shutil.copy2(video_path, output_path)
    return output_path
