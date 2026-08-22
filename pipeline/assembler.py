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
# Canonical film format for cross-scene concatenation — the pipeline renders at
# 25fps throughout (LTX, H3, Ken Burns), and heterogeneous scene finals
# (e.g. a silent establishing track vs talking clips) are normalised to this
# fps + audio rate/layout so the concat filter accepts them.
_FILM_FPS = 25
_FILM_AR = 48000
# Chunk length used to RECOVER from a failed upscale — clips are always tried
# whole first, and only split into overlapped, xfade-joined pieces when the
# worker returns black frames (see temporal_ai_upscale_video).
#
# The LTX graph's own ceiling is 1000 frames (~40s at 25fps), but well under
# that sampling can overflow to NaN and return a *solid black clip while
# reporting success* — observed on a GB10 at 505 frames (20.2s) after 78
# minutes, and again at 174 frames once the target was 2160x2160. It scales
# with frames x pixels per pass rather than clip length alone, so this is the
# size to fall back to, not a length that is always safe.
# Raise it with TEMPORAL_VIDEO_UPSCALE_CHUNK_SECONDS on a roomier GPU.
_TEMPORAL_UPSCALE_CHUNK_SECONDS = float(os.environ.get(
    "TEMPORAL_VIDEO_UPSCALE_CHUNK_SECONDS", "12.0",
))
_TEMPORAL_UPSCALE_CHUNK_OVERLAP = float(os.environ.get("TEMPORAL_VIDEO_UPSCALE_CHUNK_OVERLAP", "0.5"))
# Floor for the halve-and-retry after a blank result: below this the overlapped
# pieces cost more to stitch than the retry saves.
_TEMPORAL_UPSCALE_MIN_CHUNK_SECONDS = 2.0
# A silently-failed upscale is uniformly black, so compare brightness against
# the source rather than a fixed floor — a genuinely dark scene must still pass.
_UPSCALE_MIN_LUMA_RATIO = 0.35
_UPSCALE_DARK_SOURCE_LUMA = 8.0  # below this the source has no brightness to compare
_UPSCALE_PROBES = 6

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


def write_silence_wav(path: Path, seconds: float, rate: int = 24000) -> Path:
    """A silent WAV of *seconds* — the 'narration' of a silent scene, so the
    normal duration/mux/concat pipeline runs unchanged with no spoken audio."""
    import wave
    n = max(1, int(seconds * rate))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n)
    return path


def transcode_to_wav(input_path: Path, output_path: Path) -> Path:
    """Re-encode any audio file the user hands us into the 16-bit WAV every
    downstream step reads (mux, pinned per-scene slices, seed-vc).

    Used for an uploaded song: what arrives may be an mp3, an m4a or a WAV at
    some other bit depth, and the pipeline only ever opens one kind of file.
    """
    logger.info("[ffmpeg] transcode_to_wav: %s", input_path.name)
    _run([
        _FFMPEG, "-y",
        "-i", str(input_path),
        "-vn",
        "-c:a", "pcm_s16le",
        str(output_path),
    ])
    return output_path


def extend_audio_tail(input_path: Path, output_path: Path, extra_seconds: float,
                      fade: float = 2.0) -> Path:
    """Give a track a softer, longer ending: fade its last *fade* seconds out and
    pad *extra_seconds* of silence after them.

    A song that stops mid-bar sounds like a cut cable. Nothing is re-generated —
    the take is untouched up to the fade — so this is the cheap fix for an
    ending that is merely blunt (a song that never got to *finish* has to be
    generated longer instead). The fade is capped at a quarter of the track so
    a short one doesn't become one long decay.
    """
    duration = _get_duration(input_path)
    fade = max(0.0, min(fade, duration / 4))
    filters = []
    if fade > 0.05:
        filters.append(f"afade=t=out:st={duration - fade:.3f}:d={fade:.3f}")
    filters.append(f"apad=pad_dur={extra_seconds:.3f}")
    logger.info("[ffmpeg] extend_audio_tail: %.1fs +%.1fs (fade %.1fs)",
                duration, extra_seconds, fade)
    _run([
        _FFMPEG, "-y",
        "-i", str(input_path),
        "-af", ",".join(filters),
        "-c:a", "pcm_s16le",
        str(output_path),
    ])
    return output_path


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


def _video_frame_rates(path: Path) -> tuple[float, float]:
    """(nominal, average) frame rate of the first video stream — ffprobe's
    r_frame_rate and avg_frame_rate. They agree on a constant-rate clip; they
    differ when frames are missing or unevenly timed. 25.0 stands in for a
    value ffprobe cannot give."""
    result = subprocess.run(
        [
            _FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate,avg_frame_rate",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed:\n{result.stderr[-2000:]}")
    rates = []
    try:
        import json

        streams = json.loads(result.stdout or "{}").get("streams") or []
        stream = streams[0] if streams else {}
        for key in ("r_frame_rate", "avg_frame_rate"):
            value = str(stream.get(key) or "")
            if "/" in value:
                num, den = value.split("/", 1)
                fps = float(num) / float(den)
            else:
                fps = float(value)
            rates.append(fps if fps > 0 else 25.0)
    except Exception:
        rates = []
    while len(rates) < 2:
        rates.append(25.0)
    return rates[0], rates[1]


def _constant_rate_source(input_path: Path, output_path: Path) -> tuple[Path, float]:
    """The clip to hand an AI upscaler, and the frame rate to write its result at.

    The ComfyUI upscale graphs load the source as a constant-rate frame
    sequence and write the result at whatever rate we name. Naming the AVERAGE
    rate of an unevenly timed clip (one that had frames dropped at the mux, for
    instance) stretches it: 24 fps of frames replayed at 18 fps ran every scene
    of a film a third long. An uneven clip is first re-encoded at its nominal
    rate — holes filled, length unchanged — so the rate we name is the rate the
    frames were loaded at. A clip that is already constant-rate is returned
    untouched.
    """
    nominal, average = _video_frame_rates(input_path)
    if abs(nominal - average) <= 0.01 * nominal:
        return input_path, nominal
    # A stream whose timestamps are already scrambled can report an absurd
    # nominal rate; the average is then the better estimate of its real rate.
    rate = nominal if nominal <= 120 else round(average)
    cfr = output_path.with_name(f"{output_path.stem}.cfr{output_path.suffix}")
    logger.info("[ffmpeg] constant-rate copy of %s at %.3f fps (nominal %.3f, average %.3f)",
                input_path.name, rate, nominal, average)
    _run([
        _FFMPEG, "-y",
        "-i", str(input_path),
        "-vf", f"fps={rate}",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(cfr),
    ], timeout=1800)
    return cfr, float(rate)


def _restore_source_clock(video_path: Path, source_path: Path, output_path: Path,
                          duration: float) -> Path:
    """Put an upscaled clip back on its source's clock: the source's audio and
    exactly the source's length.

    Every upscaler round-trips through a VAE, which does not preserve frame
    count, so a clip comes back a few frames long or short — and the
    reassembled film drifts out of sync with its captions if that is left
    alone. The audio is muxed first (a cheap stream copy that also trims a
    long result); only a length that is still off is re-encoded to conform.
    """
    _mux_source_audio(video_path, source_path, output_path, target_duration=duration)
    if abs(_get_duration(output_path) - duration) > 0.01:
        conformed = output_path.with_name(f"{output_path.stem}.conformed{output_path.suffix}")
        try:
            conform_video_to_source(output_path, source_path, conformed, duration)
            conformed.replace(output_path)
        finally:
            conformed.unlink(missing_ok=True)
    return output_path


def _luma_profile(path: Path, probes: int = _UPSCALE_PROBES) -> list[float]:
    """Frame brightness (0–255) at evenly spaced points through a clip.

    Probes sit strictly inside the clip, so a legitimate fade to black at
    either end is never sampled.
    """
    try:
        duration = _get_duration(path)
    except Exception:
        return []  # unreadable — the caller treats "can't measure" as "can't judge"
    if duration <= 0:
        return []
    values = []
    for i in range(probes):
        at = duration * (i + 1) / (probes + 1)
        result = subprocess.run(
            [
                _FFMPEG, "-v", "info",
                "-ss", f"{at:.3f}", "-i", str(path),
                "-vframes", "1",
                "-vf", "signalstats,metadata=print",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )
        for line in result.stderr.splitlines():
            _, _, value = line.partition("lavfi.signalstats.YAVG=")
            if value:
                try:
                    values.append(float(value.strip()))
                except ValueError:
                    pass
                break
    return values


def _verify_upscale_not_blank(source: Path, result: Path) -> None:
    """Reject an AI upscale that came back blank.

    ComfyUI reports success and hands back a solid black clip when sampling
    overflows to NaN, which the frame writer then casts to zeros ("invalid value
    encountered in cast"). Nothing downstream notices, so the black scene is
    concatenated into the film and published over the good final.

    Measured on scene_05 of a 1024x1024 film upscaled to 2160x2160: the whole
    clip came back at brightness 16.0 against 118.2 in the source, reproducibly,
    across three seeds and two workers. It is NOT memory and NOT the decoder —
    a tiled VAE decode returns the same black clip, so the NaN is already in the
    latent when it arrives. Fewer frames per sampling pass is what avoids it,
    which is why the chunked retry works.

    Compared probe by probe at matching positions (the upscale preserves
    duration), not on the average: a single black chunk out of several would
    survive an averaged check.
    """
    if not result.exists() or result.stat().st_size == 0:
        raise RuntimeError("AI upscaler did not produce an output video.")
    source_luma = _luma_profile(source)
    result_luma = _luma_profile(result)
    if not source_luma or len(result_luma) != len(source_luma):
        return  # can't compare like for like — don't fail a good upscale on it
    for at, (want, got) in enumerate(zip(source_luma, result_luma), start=1):
        # A dark moment in the source has no brightness to compare against.
        if want < _UPSCALE_DARK_SOURCE_LUMA or got >= want * _UPSCALE_MIN_LUMA_RATIO:
            continue
        raise RuntimeError(
            f"AI upscale of {source.name} came back blank {at}/{len(source_luma)} "
            f"of the way in — brightness {got:.1f} against {want:.1f} in the "
            f"source. Sampling overflowed to NaN on this "
            f"{_get_duration(source):.0f}s clip at this size (not a memory "
            f"limit, and not the decoder — a tiled decode fails the same way). "
            f"It is retried automatically in smaller pieces; lower the upscale "
            f"chunk length in Settings if the retries also fail."
        )


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


def replace_first_frame(
    video_path: Path, frame_image: Path, output_path: Path, *, frames: int = 1,
) -> Path:
    """Show *frame_image* over the video's first *frames* frames.

    YouTube Shorts ignore uploaded thumbnails and pick their own frame from the
    video, so the cover is burned into the opening itself. A single frame (40ms
    at 25fps) is a flash that frame samplers discard — holding it ~1s makes it
    its own shot, which is why callers pass a duration (see pipeline/cover.py).
    Nothing is prepended, so duration and caption timing are untouched; audio is
    stream-copied. An RGBA *frame_image* composites over the moving video (the
    "text" cover mode); an opaque one covers it. Writes to a separate output so
    callers can atomically swap it in after ffmpeg succeeds.
    """
    width, height = _get_video_dimensions(video_path)
    frames = max(1, int(frames))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "[ffmpeg] replace_first_frame: %s ← %s (%d frame(s))",
        video_path.name, frame_image.name, frames,
    )
    _run([
        _FFMPEG, "-y",
        "-i", str(video_path),
        "-i", str(frame_image),
        "-filter_complex", (
            f"[1:v]format=rgba,scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1[cover];"
            f"[0:v][cover]overlay=enable='lt(n,{frames})',format=yuv420p[out]"
        ),
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ], timeout=3600)
    return output_path


def burn_subtitles(video_path: Path, srt_path: Path, output_path: Path) -> Path:
    """Render an SRT track into the picture itself (open captions).

    The subtitles filter reads its filename through two levels of filtergraph
    escaping; copying the track to a temp path with no special characters
    sidesteps both, whatever the film is called. Audio is stream-copied.
    Writes to a separate output so callers can atomically swap it in after
    ffmpeg succeeds.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("[ffmpeg] burn_subtitles: %s ← %s", video_path.name, srt_path.name)
    with tempfile.TemporaryDirectory(prefix="subburn-") as td:
        safe_srt = Path(td) / "captions.srt"
        shutil.copy2(srt_path, safe_srt)
        _run([
            _FFMPEG, "-y",
            "-i", str(video_path),
            "-vf", f"subtitles={safe_srt}",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
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
    chunk_seconds: float | None = None,
) -> Path:
    """Run a ComfyUI (or custom CLI) AI video upscaler on one clip.

    *engine*:
      - ``ic_lora`` — Lightricks LTX-2.3 IC-LoRA Pixel Spatial Upscaler
        (generative 2×/4×; https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler)
      - ``ltx_latent`` — simpler LTXVLatentUpsampler + latent spatial-upscaler-x2 model
      - ``h3_latent`` — MiniMax H3 24-channel latent upscaler (learned 3D resize;
        https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler)

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
    if eng in {"h3", "h3_latent_upscaler", "minimax_h3_latent"}:
        eng = "h3_latent"
    if eng not in {"ic_lora", "ltx_latent", "h3_latent"}:
        raise ValueError(f"Unknown AI upscale engine: {engine!r}")

    template = command_template or os.environ.get("TEMPORAL_VIDEO_UPSCALER_CMD", "")
    timeout = timeout_seconds or int(os.environ.get("TEMPORAL_VIDEO_UPSCALER_TIMEOUT", "7200"))
    if not template.strip():
        from pipeline.comfyui import (
            upscale_video_h3_latent,
            upscale_video_ltx,
            upscale_video_ltx_latent,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        source, fps = _constant_rate_source(input_path, output_path)
        duration = _get_duration(source)
        # Config wins, then the env override, then the packaged default.
        chunk_seconds = max(1.0, float(chunk_seconds or os.environ.get(
            "TEMPORAL_VIDEO_UPSCALE_CHUNK_SECONDS",
            str(_TEMPORAL_UPSCALE_CHUNK_SECONDS),
        )))
        chunk_overlap = max(0.0, float(os.environ.get(
            "TEMPORAL_VIDEO_UPSCALE_CHUNK_OVERLAP",
            str(_TEMPORAL_UPSCALE_CHUNK_OVERLAP),
        )))
        # Overlap cannot exceed half the body or the xfade math collapses.
        chunk_overlap = min(chunk_overlap, max(0.0, chunk_seconds * 0.45))
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
            if eng == "h3_latent":
                return upscale_video_h3_latent(
                    inp, out, w, h,
                    fps=fps,
                    timeout_seconds=timeout_seconds,
                    comfy_url=cu,
                    source_width=actual_w,
                    source_height=actual_h,
                )
            return upscale_video_ltx(
                inp, out, w, h,
                fps=fps,
                timeout_seconds=timeout_seconds,
                comfy_url=cu,
                source_width=actual_w,
                source_height=actual_h,
            )

        def _run_once(chunk: float | None) -> Path:
            """*chunk* None upscales the clip whole, in one piece."""
            if chunk is not None and duration > chunk + 0.25:
                return _chunked_comfy_temporal_upscale(
                    source,
                    output_path,
                    width,
                    height,
                    fps=fps,
                    timeout_seconds=timeout,
                    comfy_url=url,
                    chunk_seconds=chunk,
                    overlap_seconds=min(chunk_overlap, max(0.0, chunk * 0.45)),
                    upscale_fn=_comfy_upscale,
                )
            if eng == "ltx_latent":
                upscale_video_ltx_latent(
                    source, output_path, width, height,
                    fps=fps, timeout_seconds=timeout, comfy_url=url,
                )
            elif eng == "h3_latent":
                upscale_video_h3_latent(
                    source, output_path, width, height,
                    fps=fps, timeout_seconds=timeout, comfy_url=url,
                    source_width=actual_w, source_height=actual_h,
                )
            else:
                upscale_video_ltx(
                    source, output_path, width, height,
                    fps=fps, timeout_seconds=timeout, comfy_url=url,
                    source_width=actual_w, source_height=actual_h,
                    duration_seconds=duration,
                )
            # The chunked path does this per join; the whole clip needs it just
            # the same — ComfyUI hands back its own frame count and a padded
            # audio track, not the source's.
            staged = output_path.with_name(f"{output_path.stem}.clock{output_path.suffix}")
            try:
                _restore_source_clock(output_path, source, staged, duration)
                staged.replace(output_path)
            finally:
                staged.unlink(missing_ok=True)
            return output_path

        # The whole clip first, ALWAYS, however long it is. Splitting a clip and
        # xfade-joining the pieces breaks continuity at every seam — badly so
        # with the generative IC-LoRA upscaler, whose pieces diverge from each
        # other. Chunking is how we recover from a worker running out of memory
        # (the upscale comes back black), never a mode we choose up front.
        attempts: list[float | None] = [None]
        chunk = min(chunk_seconds, duration / 2)
        while chunk >= _TEMPORAL_UPSCALE_MIN_CHUNK_SECONDS:
            attempts.append(chunk)
            chunk /= 2
        last_blank: RuntimeError | None = None
        try:
            for i, attempt_chunk in enumerate(attempts):
                result = _run_once(attempt_chunk)
                try:
                    _verify_upscale_not_blank(source, result)
                    return result
                except RuntimeError as exc:
                    last_blank = exc
                    if i + 1 >= len(attempts):
                        break
                    logger.warning(
                        "[upscale] %s came back blank %s; retrying in %.1fs chunks (%s)",
                        input_path.name,
                        "whole" if attempt_chunk is None else f"at {attempt_chunk:.1f}s chunks",
                        attempts[i + 1], eng,
                    )
        finally:
            if source != input_path:
                source.unlink(missing_ok=True)
        raise last_blank

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
    _verify_upscale_not_blank(input_path, output_path)
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
    overlap_seconds: float = 0.5,
) -> Path:
    """Upscale long videos in bounded chunks so ComfyUI does not hold all frames.

    Chunks share a short trailing/leading overlap that is crossfaded so
    generative (IC-LoRA) and latent upscalers don't leave a hard cut every
    *chunk_seconds*. Video is upscaled without audio; the source audio is
    remuxed onto the joined result so voice/music stay continuous.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = _get_duration(input_path)
    body = max(1.0, float(chunk_seconds))
    overlap = max(0.0, min(float(overlap_seconds), body * 0.45))
    n_chunks = max(1, int((duration + body - 0.001) // body))
    logger.info(
        "[comfy] chunked temporal upscale: %.1fs in %d chunks of %.1fs (overlap %.2fs)",
        duration, n_chunks, body, overlap,
    )
    with tempfile.TemporaryDirectory(prefix="spielbot-upscale-", dir=str(output_path.parent)) as tmp:
        tmp_dir = Path(tmp)
        upscaled: list[Path] = []
        for idx in range(n_chunks):
            start = idx * body
            remaining = max(0.0, duration - start)
            if remaining <= 0.01:
                break
            is_last = idx == n_chunks - 1 or remaining <= body + 0.05
            # Non-final chunks extend past the body by *overlap* so the next
            # chunk can xfade over the same source-time region (aligned).
            if is_last:
                segment_duration = remaining
            else:
                segment_duration = min(body + overlap, remaining)
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
            if is_last:
                break
        if not upscaled:
            raise RuntimeError("Temporal AI upscaler did not produce any chunks.")
        video_only = tmp_dir / "joined.video.mp4"
        if len(upscaled) == 1 or overlap <= 0.001:
            _concat_video_chunks(upscaled, video_only)
        else:
            _xfade_video_chunks(upscaled, video_only, body_seconds=body, overlap_seconds=overlap)
        # Each chunk returns slightly short and the join compounds it (measured:
        # 6.250s of source came back as 5.917s over two chunks).
        _restore_source_clock(video_only, input_path, output_path, duration)
    return output_path


def _extract_temporal_chunk(input_path: Path, output_path: Path, start: float, duration: float) -> Path:
    """Extract a segment (frame-accurate) for ComfyUI upscaling.

    The chunk keeps its audio even though only the picture is upscaled and the
    source audio is muxed back over the join: every packaged upscale workflow
    feeds VHS_LoadVideoFFmpeg's audio output into VideoCombine, and VHS fails
    the whole prompt with "failed to extract audio" when handed a silent file.
    """
    # -ss after -i is slower but frame-accurate — hard cuts at chunk boundaries
    # were worsened by keyframe-inaccurate input seeking.
    _run([
        _FFMPEG, "-y",
        "-i", str(input_path),
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ], timeout=1800)
    return output_path


def _concat_video_chunks(chunks: list[Path], output_path: Path) -> Path:
    """Stream-copy join of clips that share one codec, size, frame rate and
    timebase (an H3 chain's halves, a take and its continuation). The concat
    demuxer does not rescale timestamps between files, so clips at different
    rates must go through concatenate_scenes instead."""
    list_path = output_path.with_suffix(".concat.txt")
    try:
        list_path.write_text(
            "".join(f"file {shlex.quote(str(chunk))}\n" for chunk in chunks),
            encoding="utf-8",
        )
        # setts pins the joined VIDEO timeline back to zero. The concat demuxer
        # offsets every video packet by the first segment's AAC priming (+23 ms
        # here), keeping combined.mp4 self-consistent — but a music video's mix
        # then throws that audio away and lays the song at 0, so the shift
        # became a constant audio-leads-video error across the whole film: each
        # take was in sync on its own and every mouth ran late in the merge.
        # With the picture starting at 0, scene N's first frame sits at exactly
        # the film time its song window says. (-avoid_negative_ts make_zero is
        # NOT a substitute: measured, it moved the picture 83 ms the same way.)
        # PTS and DTS must move by the SAME amount. The first DTS sits two
        # frames before the first PTS (x264's B-frame delay), so shifting DTS by
        # STARTDTS put every B-frame's DTS after its PTS; ffmpeg "replaces by
        # guess", stamps those frames one tick apart, and the next mux drops a
        # quarter of the picture (a chained H3 scene lost 19 of 79 frames).
        _run([
            _FFMPEG, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            "-bsf:v", "setts=pts=PTS-STARTPTS:dts=DTS-STARTPTS",
            "-movflags", "+faststart",
            str(output_path),
        ], timeout=1800)
    finally:
        list_path.unlink(missing_ok=True)
    return output_path


def _xfade_video_chunks(
    chunks: list[Path],
    output_path: Path,
    *,
    body_seconds: float,
    overlap_seconds: float,
) -> Path:
    """Join upscaled chunks with an xfade over the shared source-time overlap.

    Each non-final chunk is body+overlap long and starts at i*body; the next
    chunk starts at (i+1)*body. Crossfading for *overlap_seconds* at offset
    *body_seconds* therefore blends two independent upscales of the same
    source interval, preserving total duration.
    """
    if len(chunks) == 1:
        shutil.copy2(chunks[0], output_path)
        return output_path

    fade = max(0.05, float(overlap_seconds))
    durs = [_get_duration(p) for p in chunks]

    inputs: list[str] = []
    for p in chunks:
        inputs.extend(["-i", str(p)])

    filters: list[str] = []
    # Running length of the joined stream before the next xfade.
    # First xfade: offset ≈ body (start of shared region on chunk 0).
    # Next offset = previous_length - fade (measured from durations).
    prev_label = "0:v"
    acc_len = durs[0]
    for i in range(1, len(chunks)):
        join_fade = min(fade, durs[i] - 0.05, acc_len - 0.05)
        if join_fade < 0.05:
            # Degenerate short chunk — hard-append via concat filter.
            out_label = f"v{i}"
            filters.append(
                f"[{prev_label}][{i}:v]concat=n=2:v=1:a=0[{out_label}]"
            )
            acc_len = acc_len + durs[i]
            prev_label = out_label
            continue
        offset = max(0.0, acc_len - join_fade)
        out_label = f"v{i}"
        filters.append(
            f"[{prev_label}][{i}:v]xfade=transition=fade:duration={join_fade:.3f}:offset={offset:.3f}[{out_label}]"
        )
        acc_len = acc_len + durs[i] - join_fade
        prev_label = out_label

    _run([
        _FFMPEG, "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", f"[{prev_label}]",
        "-an",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ], timeout=3600)
    return output_path


def _has_audio_stream(path: Path) -> bool:
    result = subprocess.run(
        [
            _FFPROBE, "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    return bool((result.stdout or "").strip())


def _mux_source_audio(
    video_path: Path,
    source_path: Path,
    output_path: Path,
    *,
    target_duration: float | None = None,
) -> Path:
    """Attach the original clip's audio to an upscaled video-only stream.

    Re-using the pre-upscale audio avoids clicks/gaps from per-chunk AAC
    re-encodes. Duration is clamped to the source so lip-sync stays intact.
    """
    dur = target_duration if target_duration is not None else _get_duration(source_path)
    if not _has_audio_stream(source_path):
        _run([
            _FFMPEG, "-y",
            "-i", str(video_path),
            "-t", f"{dur:.3f}",
            "-c:v", "copy",
            "-an",
            "-movflags", "+faststart",
            str(output_path),
        ], timeout=1800)
        return output_path
    _run([
        _FFMPEG, "-y",
        "-i", str(video_path),
        "-i", str(source_path),
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-t", f"{dur:.3f}",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ], timeout=1800)
    return output_path


def ken_burns_clip(still_path: Path, out_path: Path, seconds: float,
                   width: int, height: int, zoom_to: float = 1.12, fps: int = 25) -> Path:
    """A held still with a gentle push-in (Ken Burns) + a silent audio track,
    sized to width×height. Used as a dialogue scene's establishing shot — the
    wide frame is shown for a beat and slowly zoomed before the talking close-ups,
    so the scene's setting is established and never lost. Pre-upscales the still
    to keep the zoom smooth (zoompan jitters at native resolution)."""
    frames = max(1, int(round(seconds * fps)))
    step = max(0.0, (zoom_to - 1.0)) / frames
    vf = (
        f"scale={width * 4}:{height * 4},"
        f"zoompan=z='min(zoom+{step:.6f},{zoom_to})':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':fps={fps}:s={width}x{height},"
        f"setsar=1"
    )
    _run([
        _FFMPEG, "-y",
        "-loop", "1", "-i", str(still_path),
        "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
        "-t", f"{seconds}",
        "-vf", vf,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
        str(out_path),
    ], timeout=600)
    return out_path


def fit_video_canvas(video_path: Path, width: int, height: int) -> Path:
    """Fit a clip onto an exact width×height canvas without cropping content.

    Scales to fit inside the canvas and fills the leftover area with a blurred,
    zoomed copy of the frame (the standard pillarbox/letterbox treatment) — used
    for square clips inside a landscape/portrait film, where
    ensure_video_resolution's center-crop would cut the speaker's face. In-place
    (atomic swap); no-op when the clip already matches."""
    actual_w, actual_h = _get_video_dimensions(video_path)
    if (actual_w, actual_h) == (width, height):
        return video_path
    tmp_path = video_path.with_name(f"{video_path.stem}.fit{width}x{height}.tmp{video_path.suffix}")
    logger.info("[ffmpeg] fit_video_canvas: %dx%d → %dx%d (%s)",
                actual_w, actual_h, width, height, video_path.name)
    _run([
        _FFMPEG, "-y",
        "-i", str(video_path),
        "-filter_complex",
        (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:5[bg];"
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
        ),
        "-map", "[vout]",
        "-map", "0:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(tmp_path),
    ], timeout=1800)
    tmp_path.replace(video_path)
    return video_path


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


# How long the closing scene holds on its last frame after the narration ends,
# so a film doesn't cut dead on its last word. Every path that muxes the closing
# scene — first render, worker render, editor re-render — must use this.
FINAL_SCENE_TAIL_SECS = 2.0


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


def conform_video_to_source(
    video_path: Path, source_path: Path, output_path: Path, duration: float,
) -> Path:
    """Force a processed clip back to its source's exact length, with source audio.

    A VAE round-trip does not preserve frame count: MiniMax H3's video VAE
    encodes in overlapping temporal chunks, so a clip comes back a few frames
    long or short depending on where the chunk boundaries land. Either way the
    scene no longer matches the length the rest of the film assumes.

    Trims when the result ran long, and holds the final frame when it ran short.
    The audio is taken from *source_path* rather than the processed copy — it was
    never upscaled, only carried along, and the round-trip can clip its tail.
    """
    pad = max(0.0, duration - _get_duration(video_path))
    cmd = [_FFMPEG, "-y", "-i", str(video_path), "-i", str(source_path)]
    if pad > 0.001:
        cmd += ["-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={pad:.3f}[v]",
                "-map", "[v]"]
    else:
        cmd += ["-map", "0:v:0"]
    cmd += [
        "-map", "1:a?",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    _run(cmd)
    return output_path


def trim_video(input_path: Path, output_path: Path, duration: float) -> Path:
    """Keep the first *duration* seconds of a clip, audio track included.

    Re-encoded rather than stream-copied so the cut lands on the requested frame
    instead of the preceding keyframe (same reason as ``mux_video_audio``)."""
    _run([
        _FFMPEG, "-y",
        "-i", str(input_path),
        "-map", "0:v:0", "-map", "0:a?",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
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

    # A video-only clip (e.g. the silent LTX keyframe establishing shot) has no
    # audio stream, so the per-input [i:a] below would match nothing and fail the
    # concat. Give each such clip a silent stereo source of its own duration.
    # Clips that already carry audio (all narration scenes) are untouched.
    has_audio = [_has_audio_stream(p) for p in scene_paths]
    silence_idx: dict[int, int] = {}
    next_input = n
    for i in range(n):
        if not has_audio[i]:
            inputs += ["-f", "lavfi", "-t", f"{durations[i]:.3f}",
                       "-i", f"anullsrc=r={_FILM_AR}:cl=stereo"]
            silence_idx[i] = next_input
            next_input += 1

    filters = []
    fade_a = 0.05 if fade > 0 else 0.0

    for i in range(n):
        dur = durations[i]

        # setpts=PTS-STARTPTS normalises each clip's PTS to start at 0 so that
        # fade=t=in:st=0 actually covers the very first frame.  Without this,
        # clips muxed with -c:v copy retain the original ComfyUI PTS (which may
        # be non-zero), causing the first frame(s) to flash through un-faded.
        # Normalise fps + SAR before concat: the concat filter demands identical
        # video params across inputs, but scene finals can differ (e.g. a dialogue
        # scene's within-concat re-encode lands at 50fps while narration is 25).
        vf = [f"fps={_FILM_FPS}", "setsar=1", "setpts=PTS-STARTPTS"]
        if fade > 0 and i > 0:
            vf.append(f"fade=t=in:st=0:d={fade:.2f}")
        if fade > 0 and i < n - 1:
            vf.append(f"fade=t=out:st={dur - fade:.3f}:d={fade:.2f}")
        filters.append(f"[{i}:v]{','.join(vf)}[fv{i}]")

        # Likewise normalise audio rate/layout — a silent establishing track
        # (24k mono) mixed with talking clips (44.1k stereo) otherwise fails concat.
        af = [f"aresample={_FILM_AR}", f"aformat=sample_rates={_FILM_AR}:channel_layouts=stereo",
              "asetpts=PTS-STARTPTS"]
        if fade_a > 0 and i > 0:
            af.append(f"afade=t=in:st=0:d={fade_a:.2f}")
        if fade_a > 0 and i < n - 1:
            af.append(f"afade=t=out:st={dur - fade_a:.3f}:d={fade_a:.2f}")
        a_src = i if has_audio[i] else silence_idx[i]
        filters.append(f"[{a_src}:a]{','.join(af)}[fa{i}]")

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

    # A bed shorter than the picture would leave the tail silent (amix stops at
    # the video's length but the music input has already run dry). Beds used to
    # be generated over-length, so this never fired; MiniMax Music 3 caps at
    # 6 minutes and may end a song early, so loop it back to the top instead.
    loop_music = music_dur > 0 and music_dur < video_dur - 1
    if loop_music:
        logger.info("[ffmpeg] music (%.1fs) is shorter than the film (%.1fs) — looping the bed",
                    music_dur, video_dur)
    music_in = (["-stream_loop", "-1"] if loop_music else []) + ["-i", str(music_path)]

    # The film's audio arrives from a hard-cut stream copy with each scene's
    # AAC padding OVERLAPPED at the seams (two packets stamped microseconds
    # apart) — self-correcting when played, because what a player drops there
    # is padding. But amix passes those timestamps through to its OUTPUT, so
    # the mixed track — now carrying the continuous song — kept stamps that
    # tell a player to drop ~23 ms of SONG at every seam. Sample decoders
    # (and sample-order checks) read it intact; a real player loses a frame
    # of song per scene, so the voice pulled ahead of every mouth, worse each
    # scene — ~440 ms early by the 20th. aresample rebuilds the voice stream
    # as the uniform timeline its stamps describe BEFORE the mix, so the mix
    # inherits clean time.
    _VOICE_NORM = "aresample=async=1000:first_pts=0"
    use_ambient = ambient_path and Path(ambient_path).exists() and ambient_volume > 0
    if use_ambient:
        filter_str = (
            f"[0:a]{_VOICE_NORM},volume={voice_volume:.3f}[voice];"
            f"[1:a]volume={volume:.3f}[bg];"
            f"[2:a]volume={ambient_volume:.3f}[amb];"
            "[voice][bg][amb]amix=inputs=3:duration=first:dropout_transition=3:normalize=0[aout]"
        )
        inputs = ["-i", str(video_path), *music_in, "-i", str(ambient_path)]
    else:
        filter_str = (
            f"[0:a]{_VOICE_NORM},volume={voice_volume:.3f}[voice];"
            f"[1:a]volume={volume:.3f}[bg];"
            "[voice][bg]amix=inputs=2:duration=first:dropout_transition=3:normalize=0[aout]"
        )
        inputs = ["-i", str(video_path), *music_in]

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
