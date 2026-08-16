"""Where the singing actually is inside a generated song.

A music video pins each scene's slice of the track into its H3 take, so the
prompt has to AGREE with that slice. Two ways it used not to: naming lyrics
that are not in the slice, and asking for a moving mouth over an instrumental
intro. Both pull the model off the very track it was handed — a film whose
song opened with a 7.5 s intro had its lead singing from frame 0, to nothing.

The measurement here is deliberately coarse. A sung vocal sits ON TOP of the
instrumental bed, so the track carries two levels — bed alone, and bed plus
voice — and splitting on the midpoint between them finds the intro, the
instrumental breaks and the outro reliably, with nothing beyond the standard
library. What it cannot do is tell a loud instrumental solo from a sung line,
because both are simply loud. That distinction needs the lyric sheet
force-aligned against a separated vocal stem (demucs + whisper), which is the
planned upgrade on top of this rather than a correction of it.
"""
from __future__ import annotations

import logging
import math
import wave
from array import array
from pathlib import Path

logger = logging.getLogger("video_gen")

# Below this a frame is treated as pure digital silence rather than a level.
_SILENCE_DB = -90.0
# Enough samples per frame for a stable level reading; striding to roughly this
# many keeps the whole scan cheap without numpy (undeclared as a dependency).
_SAMPLES_PER_FRAME = 200


def _frame_levels(track: Path, frame_secs: float) -> tuple[list[float], float]:
    """Per-frame RMS of *track* in dBFS, and the seconds each frame covers.

    Returns ``([], 0.0)`` for anything not 16-bit PCM — the pipeline writes
    16-bit WAVs, and guessing at other widths is not worth the code."""
    with wave.open(str(track), "rb") as handle:
        if handle.getsampwidth() != 2:
            return [], 0.0
        channels = handle.getnchannels() or 1
        rate = handle.getframerate() or 44100
        raw = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    if not samples:
        return [], 0.0
    per_frame = max(1, int(rate * frame_secs)) * channels
    stride = max(1, per_frame // _SAMPLES_PER_FRAME)
    levels = []
    for start in range(0, len(samples), per_frame):
        chunk = samples[start:start + per_frame]
        if not chunk:
            break
        total, count = 0.0, 0
        for i in range(0, len(chunk), stride):
            value = chunk[i] / 32768.0
            total += value * value
            count += 1
        rms = math.sqrt(total / count) if count else 0.0
        levels.append(20 * math.log10(rms) if rms > 1e-6 else _SILENCE_DB)
    return levels, frame_secs


def vocal_regions(track: Path | str, *, frame_secs: float = 0.25,
                  min_gap: float = 1.5, min_region: float = 1.0,
                  min_dynamic_db: float = 4.0) -> list[tuple[float, float]]:
    """The stretches of *track* where someone is singing, as [(start, end), …].

    An empty list means "could not tell" — callers fall back to their previous
    behaviour rather than acting on a guess."""
    try:
        levels, step = _frame_levels(Path(track), frame_secs)
    except Exception:  # noqa: BLE001 — a bad WAV must not fail the divide
        logger.warning("Vocal detection could not read %s", track, exc_info=True)
        return []
    if len(levels) < 4 or step <= 0:
        return []
    ordered = sorted(levels)
    quiet = ordered[int(len(ordered) * 0.10)]
    loud = ordered[int(len(ordered) * 0.90)]
    if loud - quiet < min_dynamic_db:
        # One level throughout: the track never drops to a bare instrumental,
        # so it is sung end to end. Inventing a split here would silence a
        # mouth that should be moving.
        return [(0.0, round(len(levels) * step, 2))]
    threshold = quiet + (loud - quiet) / 2.0

    runs: list[list[float]] = []
    start: int | None = None
    for i, level in enumerate(levels):
        if level > threshold and start is None:
            start = i
        elif level <= threshold and start is not None:
            runs.append([start * step, i * step])
            start = None
    if start is not None:
        runs.append([start * step, len(levels) * step])

    # A breath between two lines is not an instrumental break.
    merged: list[list[float]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] < min_gap:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    return [(round(a, 2), round(b, 2)) for a, b in merged if b - a >= min_region]


def _absolute(regions: list[tuple[float, float]], offset: float, *,
              ends: bool = False) -> float:
    """The wall-clock second sitting *offset* seconds into the SINGING.

    An offset landing exactly on the seam between two regions is ambiguous: it
    is both the end of one and the start of the next. *ends* picks which —
    without it a line that begins right after an instrumental break would be
    dated to before the break and stretched across it."""
    remaining = offset
    for start, end in regions:
        span = end - start
        if remaining < span or (ends and remaining <= span):
            return round(start + remaining, 2)
        remaining -= span
    return regions[-1][1]


def line_times(regions: list[tuple[float, float]],
               n_lines: int) -> list[tuple[float, float]]:
    """Where each of *n_lines* lyric lines falls, in wall-clock seconds.

    The lines are paced evenly through the SINGING time rather than through
    the whole track, so an intro, a mid-song break and an outro no longer drag
    every line out of position. Even pacing within the vocal stretches is
    still an assumption — a good one for sung delivery, and the one that
    force-alignment would later replace with measurement."""
    if not regions or n_lines <= 0:
        return []
    total = sum(end - start for start, end in regions)
    if total <= 0:
        return []
    per = total / n_lines
    return [(_absolute(regions, i * per),
             _absolute(regions, (i + 1) * per, ends=True))
            for i in range(n_lines)]


def window_vocals(regions: list[tuple[float, float]], t0: float,
                  t1: float) -> list[list[float]]:
    """The singing inside the window [*t0*, *t1*), as offsets RELATIVE to t0.

    This is what a scene carries into its prompt: the model is told when in
    ITS OWN clip a voice is heard, so the mouth can follow the audio instead
    of being ordered to move throughout."""
    out = []
    for start, end in regions:
        lo, hi = max(start, t0), min(end, t1)
        if hi - lo > 0.2:
            out.append([round(lo - t0, 2), round(hi - t0, 2)])
    return out


def lines_in_window(lines: list[str], spans: list[tuple[float, float]],
                    t0: float, t1: float) -> list[str]:
    """The lyric lines whose sung span overlaps the window [*t0*, *t1*)."""
    return [line for line, (start, end) in zip(lines, spans)
            if end > t0 and start < t1]
