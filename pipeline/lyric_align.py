"""Force-align the KNOWN lyric sheet against the separated vocal stem.

Energy measurement (pipeline/song_timing.py) knows *where* singing is but
never *which line* is being sung — two lines delivered back to back in one
phrase are indistinguishable to it, so line times are estimated by even
pacing and a line can smear across a phrase gap into the neighbouring scene.

This module removes that guess: faster-whisper (in the seed-vc venv, beside
demucs) transcribes the vocal stem with word timestamps, and the transcript
is sequence-matched against the lyric sheet we already know — alignment, not
transcription, so a garbled word here and there just interpolates, and the
fixed line ORDER is what disambiguates a chorus sung four times. Words that
fall outside the measured vocal regions are dropped before matching, so a
hallucination over an instrumental break cannot claim a line.

Every failure path returns None and the caller keeps the energy-paced
estimate: no whisper install, transcription error, or a match too weak to
trust (below _MIN_MATCH of the lyric words).
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("video_gen")

_RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "whisper_words.py"
# Below this fraction of lyric words matched, the alignment is a guess of its
# own — fall back to even pacing rather than trust it.
_MIN_MATCH = 0.5
# A transcript word may sit this far outside a measured vocal region before
# it is discarded as hallucination.
_REGION_SLACK = 0.75


def _norm_word(word: str) -> str:
    return re.sub(r"[^\w']+", "", str(word or "").lower())


def word_times(stem: Path, language: str = "") -> list[tuple[str, float, float]] | None:
    """The stem's words with their sung times, or None when whisper cannot run.

    Invokes scripts/whisper_words.py with the seed-vc venv's python — the one
    place faster-whisper is installed (scripts/install_svc.sh)."""
    from pipeline import svc
    python = svc.SVC_DIR / ".venv" / "bin" / "python"
    if not python.exists():
        logger.info("No seed-vc venv — lyric alignment unavailable "
                    "(scripts/install_svc.sh installs it)")
        return None
    proc = subprocess.run(
        [str(python), str(_RUNNER), str(stem)] + ([language] if language else []),
        capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        logger.warning("Lyric alignment transcription failed: %s", " | ".join(tail))
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        logger.warning("Lyric alignment returned unreadable output")
        return None
    return [(str(w.get("word") or ""), float(w.get("start") or 0),
             float(w.get("end") or 0)) for w in data.get("words") or []]


def align_lines(stem: Path, lines: list[str], *, language: str = "",
                regions: list[tuple[float, float]] | None = None,
                ) -> list[tuple[float, float]] | None:
    """Measured (start, end) for each lyric line, or None to keep the estimate.

    *regions* are the energy-measured vocal stretches: transcript words
    outside them (plus slack) are hallucinations and are dropped before
    matching. Lines whisper garbled are interpolated between their matched
    neighbours; the result is monotonic and non-overlapping, which is what
    lines_in_window/snap_cuts assume."""
    words = word_times(stem, language)
    if words is None:
        return None
    if regions:
        words = [w for w in words
                 if any(a - _REGION_SLACK <= (w[1] + w[2]) / 2 <= b + _REGION_SLACK
                        for a, b in regions)]
    transcript = [(_norm_word(w), t0, t1) for w, t0, t1 in words if _norm_word(w)]
    lyric: list[tuple[int, str]] = []  # (line index, normalized word)
    for i, line in enumerate(lines):
        lyric.extend((i, _norm_word(w)) for w in line.split() if _norm_word(w))
    if not transcript or not lyric:
        return None

    matcher = difflib.SequenceMatcher(
        a=[w for _, w in lyric], b=[w for w, _, _ in transcript], autojunk=False)
    found: dict[int, list[float]] = {}
    matched = 0
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            line_idx = lyric[block.a + k][0]
            t0, t1 = transcript[block.b + k][1], transcript[block.b + k][2]
            span = found.setdefault(line_idx, [t0, t1])
            span[0], span[1] = min(span[0], t0), max(span[1], t1)
            matched += 1
    ratio = matched / len(lyric)
    if ratio < _MIN_MATCH:
        logger.info("Lyric alignment matched only %.0f%% of the words — "
                    "keeping the energy-paced estimate", ratio * 100)
        return None
    logger.info("Lyric alignment matched %.0f%% of the words across %d/%d lines",
                ratio * 100, len(found), len(lines))

    # Unmatched lines get the gap between their matched neighbours, split
    # evenly; the edges fall back to the measured singing's own bounds.
    first = regions[0][0] if regions else transcript[0][1]
    last = regions[-1][1] if regions else transcript[-1][2]
    spans: list[tuple[float, float]] = [(0.0, 0.0)] * len(lines)
    missing: list[int] = []

    def _fill(gap_start: float, gap_end: float) -> None:
        if not missing:
            return
        width = max(gap_end - gap_start, 0.2 * len(missing)) / len(missing)
        for j, idx in enumerate(missing):
            spans[idx] = (round(gap_start + j * width, 2),
                          round(gap_start + (j + 1) * width, 2))
        missing.clear()

    prev_end = first
    for i in range(len(lines)):
        if i in found:
            _fill(prev_end, found[i][0])
            spans[i] = (round(found[i][0], 2), round(found[i][1], 2))
            prev_end = found[i][1]
        else:
            missing.append(i)
    _fill(prev_end, last)

    # Monotonic, non-overlapping, never zero-width.
    clock = 0.0
    out: list[tuple[float, float]] = []
    for start, end in spans:
        start = max(start, clock)
        end = max(end, start + 0.2)
        out.append((round(start, 2), round(end, 2)))
        clock = end
    return out
