"""Repair chained-scene clips whose frame timestamps were scrambled by the join.

Between 2026-08-20 and the fix in pipeline.assembler._concat_video_chunks, the
stream-copy join of an H3 chained scene's halves moved PTS and DTS by different
amounts. Every frame is still in the clip, in the right order — only the
timestamps are wrong — and the scene mux then dropped a quarter of them.

For each film given, this re-stamps every scrambled scene_XX_clip_01.mp4 at its
nominal frame rate (the original is kept under repair_backup/), re-muxes the
scene finals from the repaired clips with their narration, and records each as
a new take. The film itself is NOT rebuilt here: run Rebuild in the film editor
(or POST /api/films/reassemble) afterwards — the auto-reassemble sweep also
picks up parts newer than the final.

Usage: .venv/bin/python scripts/repair_chained_clips.py [--dry-run] WORK_DIR [WORK_DIR ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import video_history  # noqa: E402
from pipeline.assembler import (  # noqa: E402
    _FFMPEG, _FFPROBE, _run, FINAL_SCENE_TAIL_SECS, mux_video_audio,
)


def _probe(path: Path) -> tuple[float, int, list[int]]:
    """(nominal fps, timebase denominator, every video packet's pts in ticks)."""
    out = subprocess.run(
        [_FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate,time_base:packet=pts",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    data = json.loads(out)
    stream = data["streams"][0]
    num, den = stream["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    tb_num, tb_den = stream["time_base"].split("/")
    pts = [int(p["pts"]) for p in data.get("packets", []) if "pts" in p]
    return fps, int(tb_den) // int(tb_num), pts


def _scrambled(path: Path) -> tuple[bool, float, int]:
    """Whether the clip's frames sit off its nominal-rate grid; also its fps and frame count."""
    fps, ticks_per_second, pts = _probe(path)
    period = ticks_per_second / fps
    if abs(period - round(period)) > 1e-6:
        return False, fps, len(pts)  # a rate the grid test cannot judge; leave it alone
    period = int(round(period))
    return any(p % period for p in pts), fps, len(pts)


def _restamp(clip: Path, fps: float, frames: int) -> None:
    """Re-time every frame by its position in decode order, which the H.264
    picture order in the bitstream keeps correct whatever the timestamps say."""
    fixed = clip.with_name(f"{clip.stem}.restamped{clip.suffix}")
    _run([
        _FFMPEG, "-y",
        "-i", str(clip),
        "-fps_mode", "passthrough",
        "-vf", f"setpts=N/({fps:g}*TB)",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(fixed),
    ], timeout=1800)
    still_scrambled, _, got = _scrambled(fixed)
    if still_scrambled or got != frames:
        fixed.unlink(missing_ok=True)
        raise RuntimeError(f"{clip.name}: re-stamp gave {got} frames for {frames}")
    fixed.replace(clip)


def _last_scene_id(wd: Path) -> int | None:
    order_file = wd / "scene_edit_order.json"
    if order_file.exists():
        order = json.loads(order_file.read_text())
        return int(order[-1]) if order else None
    try:
        scenes = json.loads((wd / "script.json").read_text())
    except Exception:
        return None
    return int(scenes[-1]["id"]) if scenes else None


def repair_film(wd: Path, dry_run: bool) -> int:
    clips = sorted(wd.glob("scene_*_clip_01.mp4"))
    backup_dir = wd / "repair_backup"
    last_id = _last_scene_id(wd)
    repaired = 0
    for clip in clips:
        sid = int(clip.name.split("_")[1])
        scrambled, fps, frames = _scrambled(clip)
        if not scrambled:
            print(f"  scene {sid:02d}: clean")
            continue
        narration = wd / f"scene_{sid:02d}_narration.wav"
        final = wd / f"scene_{sid:02d}_final.mp4"
        print(f"  scene {sid:02d}: scrambled ({frames} frames @ {fps:g} fps)"
              f"{'' if narration.exists() else ' — no narration, clip only'}")
        if dry_run:
            repaired += 1
            continue
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / clip.name
        if not backup.exists():
            backup.write_bytes(clip.read_bytes())
        _restamp(clip, fps, frames)
        if narration.exists():
            staged = wd / f"scene_{sid:02d}_final.staging.mp4"
            try:
                mux_video_audio(clip, narration, staged,
                                extra_tail_secs=FINAL_SCENE_TAIL_SECS if sid == last_id else 0.0)
                staged.replace(final)
            finally:
                staged.unlink(missing_ok=True)
            video_history.record(wd, sid, final)
        repaired += 1
    return repaired


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    dirs = [Path(a) for a in argv if a != "--dry-run"]
    if not dirs:
        print(__doc__)
        return 2
    for wd in dirs:
        if not wd.is_dir():
            print(f"{wd}: not a directory")
            return 1
        print(f"{wd.name}:")
        n = repair_film(wd, dry_run)
        print(f"  {n} scene(s) {'would be' if dry_run else ''} repaired")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
