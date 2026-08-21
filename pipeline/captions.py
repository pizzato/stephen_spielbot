"""Build a timed caption (SRT) track from a finished film's work directory.

The narration text is known exactly — it's the script we fed to TTS — so we can
hand YouTube accurate subtitles instead of relying on its speech recognition.

Timing is reconstructed from the per-scene durations, measured from the scene
finals the published video actually concatenates (falling back to the narration
wav, which ``mux_video_audio`` makes the video exactly as long as — except the
originally-last scene's short freeze tail, which only the final carries). The
published video is a straight back-to-back concatenation of those scenes — the
cover is a thumbnail only and the music mux copies the video stream unchanged —
so the cumulative scene durations line up with the published timeline. Scenes
are walked in the published cut's order: ``scene_edit_order.json`` when the film
editor reordered/removed/added scenes (the same file reassembly concatenates
by), else script.json order.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from pipeline.assembler import _FFPROBE
from pipeline.tts_text import strip_pause_markers

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


def _published_order(work_dir: Path, scenes: list[dict]) -> list[dict]:
    """*scenes* in the published cut's order. The film editor's scene
    delete/reorder/add write ``scene_edit_order.json`` and reassembly (original
    and localized) concatenates by it, so the cues must be laid down in that
    order too — a scene the editor removed is absent from it on purpose."""
    path = work_dir / "scene_edit_order.json"
    if not path.exists():
        return scenes
    try:
        order = [int(x) for x in json.loads(path.read_text())]
    except Exception:
        return scenes
    by_id = {int(s.get("id") or 0): s for s in scenes}
    return [by_id[sid] for sid in order if sid in by_id]


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


def _load_translations(work_dir: Path, lang: str) -> dict[int, str]:
    """{scene_id: translated narration} saved by the localize feature, or {}."""
    path = work_dir / "localize_scripts" / f"{lang}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {int(k): v for k, v in (data.get("scenes") or {}).items()
                if isinstance(v, str) and v.strip()}
    except Exception:
        return {}


def _scene_duration(work_dir: Path, sid: int, timing_lang: str | None) -> float:
    """Duration of scene *sid* on the published cut's timeline.

    ``timing_lang`` names the localized cut whose scene files govern timing
    (``localize/{lang}/``); ``None`` means the original cut. Localized cuts fall
    back to the original files for scenes carried through untranslated (silent/
    dialogue scenes are copied, not re-synthesized). The scene final is preferred
    over the narration wav: the published video concatenates the finals, and the
    two differ on the originally-last scene (its final carries a freeze tail the
    wav doesn't) — which matters once the editor moves that scene off the end."""
    candidates: list[Path] = []
    if timing_lang:
        loc = work_dir / "localize" / timing_lang
        candidates += [loc / f"scene_{sid:02d}_final.mp4", loc / f"scene_{sid:02d}_narration.wav"]
    candidates += [work_dir / f"scene_{sid:02d}_final.mp4", work_dir / f"scene_{sid:02d}_narration.wav"]
    for path in candidates:
        dur = _duration(path)
        if dur > 0:
            return dur
    return 0.0


def build_srt(work_dir: Path, lang: str | None = None,
              timing_lang: str | None = None) -> Path | None:
    """Write an SRT caption track for the film in *work_dir*.

    ``lang`` selects the caption TEXT: ``None`` for the original narration, or a
    language code whose translations were saved by the localize feature
    (``localize_scripts/{lang}.json``). ``timing_lang`` selects the TIMELINE the
    cues are placed on: ``None`` for the original cut, or the localized cut
    whose per-scene narration durations differ (translated narration re-times
    every scene). The two are independent because one published video (one
    timeline) can carry caption tracks in several languages.

    Returns the file path, or ``None`` if there's nothing to caption (no script,
    or none of the scenes have measurable narration on disk). Best-effort: a
    ``None`` means "skip captions" — callers should still upload the video.
    """
    work_dir = Path(work_dir)
    scenes = _published_order(work_dir, _load_scenes(work_dir))
    if not scenes:
        return None
    translations = _load_translations(work_dir, lang) if lang else {}
    if lang and not translations:
        return None  # no saved translation for this language — nothing to caption

    cues: list[tuple[float, float, str]] = []
    cursor = 0.0  # start of the current scene on the final timeline
    for scene in scenes:
        sid = int(scene.get("id") or 0)
        dur = _scene_duration(work_dir, sid, timing_lang)
        if dur <= 0:
            continue  # scene not on disk — can't place it on the timeline

        # Dialogue/silent scenes have no narration voice-over — any lingering
        # narration text must not become captions. Their duration still advances
        # the timeline so later narrated scenes stay in sync. (Per-line dialogue
        # captions are a future improvement.)
        mode = str((scene.get("metadata") or {}).get("mode") or "narration")
        if mode != "narration":
            cursor += dur
            continue

        # Translated text when captioning a localized language; the original
        # narration is the fallback so the track stays complete if a scene was
        # never translated (it plays in the original language on every cut).
        # [pause] markers are TTS pacing directives, never caption text.
        text = strip_pause_markers(translations.get(sid) or str(scene.get("narration") or ""))
        sentences = _split_sentences(text)
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
    suffix = (f"_{lang}" if lang else "") + (f"_t-{timing_lang}" if timing_lang else "")
    out_path = work_dir / f"captions{suffix}.srt"
    out_path.write_text("\n".join(blocks), encoding="utf-8")
    logger.info("Built %d caption cues → %s", len(cues), out_path)
    return out_path


def burn_srt_into_video(video_path: Path, srt_path: Path) -> Path:
    """Burn *srt_path* into *video_path*'s picture, in place (open captions).

    Timing stays valid because the frames are re-encoded, never re-timed.
    Callers burn subtitles BEFORE any first-frame cover so the cover overlays
    the text rather than the text scribbling over the cover.
    """
    from pipeline.assembler import burn_subtitles

    if not (srt_path and srt_path.exists() and srt_path.stat().st_size > 0):
        raise FileNotFoundError("No caption track to burn — build the SRT first.")
    staged = video_path.with_name(f"{video_path.stem}.subburn.tmp{video_path.suffix}")
    try:
        burn_subtitles(video_path, srt_path, staged)
        staged.replace(video_path)
    finally:
        staged.unlink(missing_ok=True)
    return video_path
