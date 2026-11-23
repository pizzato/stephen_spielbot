"""Render the silent, vertical loop delivered alongside a song film."""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from pipeline.assembler import _FFMPEG, _get_duration, _run

FILENAME = "spotify_canvas.mp4"
WIDTH, HEIGHT, FPS = 576, 1024, 24
HALF_FRAMES = 3 * FPS
_CROP = "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'"
logger = logging.getLogger(__name__)


def reference_frame(work_dir: Path) -> Path | None:
    """Pick a text-free film image that is retained by working-file cleanup."""
    for path in (*sorted(work_dir.glob("scene_*_first_frame.png")), work_dir / "cover_bg.png"):
        if not path.is_symlink() and path.is_file() and path.stat().st_size:
            return path
    return None


def render_loop(source: Path, output: Path, *, still: bool = False) -> None:
    """Mirror a three-second shot and verify the decoded loop before publishing it."""
    if still:
        # A cosine ease slows the push to a stop before reversing. Work at 2x
        # before the final downscale so the moving crop has finer increments.
        half = (f"{_CROP},scale={2 * WIDTH}:{2 * HEIGHT}:flags=lanczos,setsar=1,"
                f"zoompan=z='1+0.05*(1-cos(PI*on/{HALF_FRAMES - 1}))':"
                "x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                f"d={HALF_FRAMES}:s={2 * WIDTH}x{2 * HEIGHT}:fps={FPS},"
                f"scale={WIDTH}:{HEIGHT}:flags=lanczos")
    else:
        half = (f"fps={FPS},{_CROP},scale={WIDTH}:{HEIGHT}:flags=lanczos,"
                "setsar=1,tpad=stop_mode=clone:stop_duration=3")
    loop = (f"[0:v]{half},format=yuv420p,trim=end_frame={HALF_FRAMES},"
            f"setpts=N/({FPS}*TB),split[forward][back];"
            f"[back]reverse,setpts=N/({FPS}*TB)[reverse];"
            "[forward][reverse]concat=n=2:v=1:a=0[out]")
    # Encode both endpoint frames as IDRs with the same quantizer so the
    # matching source frames also decode identically.
    _run([_FFMPEG, "-v", "error", "-y", "-i", str(source), "-filter_complex", loop,
          "-map", "[out]", "-an", "-map_metadata", "-1", "-c:v", "libx264",
          "-preset", "fast", "-qp", "18", "-sc_threshold", "0", "-forced-idr", "1",
          "-force_key_frames", f"0,{(2 * HALF_FRAMES - 1) / FPS:.6f}",
          "-movflags", "+faststart", str(output)], max_retries=1)
    decoded = subprocess.run([_FFMPEG, "-v", "error", "-xerror", "-i", str(output),
                              "-map", "0:v:0", "-f", "framemd5", "-"],
                             check=True, capture_output=True, text=True, timeout=60)
    hashes = [line.rsplit(",", 1)[-1].strip()
              for line in decoded.stdout.splitlines() if line and not line.startswith("#")]
    if len(hashes) != 2 * HALF_FRAMES or hashes[0] != hashes[-1] or len(set(hashes)) < 2:
        raise RuntimeError("Spotify Canvas must have motion and identical first and last frames.")


def generate_default(work_dir: Path, final: Path) -> None:
    """Create the initial Canvas once. An existing Canvas takes precedence."""
    output = work_dir / FILENAME
    if output.exists():
        return
    with tempfile.TemporaryDirectory(prefix=".spotify-canvas-", dir=work_dir) as temp:
        temp_dir = Path(temp)
        source = reference_frame(work_dir)
        if source is None:
            source = temp_dir / "frame.png"
            _run([_FFMPEG, "-v", "error", "-ss", str(_get_duration(final) / 2),
                  "-i", str(final), "-frames:v", "1", str(source)], max_retries=1)
        staged = temp_dir / FILENAME
        render_loop(source, staged, still=True)
        try:
            os.link(staged, output)  # Publish atomically, without replacing a concurrent render.
        except FileExistsError:
            pass


def finish_music_video(work_dir: Path, final: Path) -> None:
    """A Canvas failure must not turn a completed film into a failed render."""
    if (work_dir / "song.json").is_file():
        try:
            generate_default(work_dir, final)
        except Exception:
            logger.warning("Spotify Canvas generation failed for %s", work_dir, exc_info=True)
