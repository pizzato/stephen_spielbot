"""Build a timed caption (SRT) track from a finished film's work directory.

The narration text is known exactly — it's the script we fed to TTS — so we can
hand YouTube accurate subtitles instead of relying on its speech recognition.

Timing is reconstructed from the per-scene narration durations. ``mux_video_audio``
makes every scene's video exactly as long as its narration (only the last scene
gets a short freeze tail, after the final spoken word), and the published video is
a straight back-to-back concatenation of those scenes — the cover is a thumbnail
only and the music mux copies the video stream unchanged — so the cumulative
narration durations line up with the published video's timeline.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from pipeline.assembler import _FFPROBE

logger = logging.getLogger("video_gen")


def _duration(path: Path) -> float:
    """Media duration in seconds, or 0.0 if it can't be read."""
    try:
        out = subprocess.run(
            [_FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def _load_scenes(work_dir: Path) -> list[dict]:
    """Scenes (id + narration) from the work dir's script.json, in order."""
    path = work_dir / "script.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    scenes = data if isinstance(data, list) else (data.get("scenes") or [])
    return [s for s in scenes if isinstance(s, dict)]


def _split_sentences(text: str) -> list[str]:
    """One caption line per sentence; collapses whitespace."""
    text = " ".join(text.split())
    if not text:
        return []
    return [s for s in re.split(r"(?<=[.!?…])\s+", text) if s]


def _timestamp(seconds: float) -> str:
    """SRT timestamp: HH:MM:SS,mmm."""
    ms = int(round(max(seconds, 0.0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(work_dir: Path) -> Path | None:
    """Write ``captions.srt`` for the film in *work_dir*.

    Returns the file path, or ``None`` if there's nothing to caption (no script,
    or none of the scenes have measurable narration on disk). Best-effort: a
    ``None`` means "skip captions" — callers should still upload the video.
    """
    work_dir = Path(work_dir)
    scenes = _load_scenes(work_dir)
    if not scenes:
        return None

    cues: list[tuple[float, float, str]] = []
    cursor = 0.0  # start of the current scene on the final timeline
    for scene in scenes:
        sid = int(scene.get("id") or 0)
        dur = _duration(work_dir / f"scene_{sid:02d}_narration.wav")
        if dur <= 0:  # fall back to the muxed scene video (same length by design)
            dur = _duration(work_dir / f"scene_{sid:02d}_final.mp4")
        if dur <= 0:
            continue  # scene not on disk — can't place it on the timeline

        sentences = _split_sentences(str(scene.get("narration") or ""))
        if sentences:
            weights = [len(s) for s in sentences]
            total = sum(weights) or 1
            start = cursor
            for sentence, weight in zip(sentences, weights):
                span = dur * weight / total
                cues.append((start, start + span, sentence))
                start += span
        cursor += dur

    if not cues:
        return None

    blocks = [
        f"{i}\n{_timestamp(start)} --> {_timestamp(end)}\n{text}\n"
        for i, (start, end, text) in enumerate(cues, 1)
    ]
    out_path = work_dir / "captions.srt"
    out_path.write_text("\n".join(blocks), encoding="utf-8")
    logger.info("Built %d caption cues → %s", len(cues), out_path)
    return out_path
