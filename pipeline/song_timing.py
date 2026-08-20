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
library. What the mix-level split cannot do is tell a loud instrumental solo
from a sung line, because both are simply loud — a distorted-guitar intro
read as sung end to end, and the lead mouthed a verse over it.

That is why ``measure_regions`` separates the VOCAL STEM first (demucs — the
same pass every re-voicing opens with) and measures THAT: on a stem, an
intro, a solo and an outro are real silence, and the level split becomes
near-exact. The mix-level split stays as the fallback when demucs is not
installed. Force-aligning the lyric sheet against the stem (whisper) remains
the upgrade beyond this — it would date each LINE by transcription instead
of even pacing.
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
                  min_dynamic_db: float = 4.0,
                  min_level_db: float | None = None) -> list[tuple[float, float]]:
    """The stretches of *track* where someone is singing, as [(start, end), …].

    An empty list means "could not tell" — callers fall back to their previous
    behaviour rather than acting on a guess.

    *min_level_db* is the separated-stem mode: on a vocal stem the scale IS
    absolute (silence is silence), so frames quieter than this are never a
    voice however the percentiles fall — they are separation bleed. On the
    full mix no absolute floor makes sense; leave it None there."""
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
    if min_level_db is not None and loud < min_level_db:
        # A stem that never reaches voice level: nothing on it is singing.
        return []
    if loud - quiet < min_dynamic_db:
        # One level throughout: the track never drops to a bare instrumental,
        # so it is sung end to end. Inventing a split here would silence a
        # mouth that should be moving.
        return [(0.0, round(len(levels) * step, 2))]
    threshold = quiet + (loud - quiet) / 2.0
    if min_level_db is not None:
        threshold = max(threshold, min_level_db)

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


# Quieter than this on a separated vocal stem is bleed, not a voice.
_STEM_FLOOR_DB = -45.0

# The film is cut on H3's 24 fps grid, and a window that is not a whole number
# of frames cannot be trimmed to exactly: ffmpeg keeps the frame straddling the
# end, so the take comes out up to 42 ms LONGER than its own stretch of the
# song. Nothing ever gives that back, so the error adds up scene by scene and
# the picture slides behind the overlaid track — measured at 0.44 s by the last
# shot of a 14-scene film. Snapping the cuts themselves is what removes it:
# on-grid windows trim exactly, so the concatenated takes stay sample-aligned
# with the song from the first frame to the last.
FPS = 24.0


def frame_snap(seconds: float, *, down: bool = False) -> float:
    """*seconds* placed on the film's frame grid.

    Forward to the next frame by default: a cut candidate is the moment a line
    ENDED (or the middle of a gap), and rounding back across it would put the
    seam inside the line the cut exists to protect. *down* floors instead, for
    the track's own end — the film must never outrun the song."""
    frames = math.floor(seconds * FPS) if down else math.ceil(seconds * FPS)
    return round(frames / FPS, 6)


def vocal_stem(track: Path) -> Path | None:
    """A separated vocal stem for *track*, cached beside it as
    ``<name>_vocals.wav``.

    demucs (from the seed-vc install every re-voicing already uses) when it is
    present; None when it is not, or separation fails — callers measure the
    mix instead. The cache is keyed on the track's mtime, so picking or
    uploading another song version re-separates."""
    stem = track.with_name(f"{track.stem}_vocals.wav")
    try:
        if stem.exists() and stem.stat().st_mtime >= track.stat().st_mtime:
            return stem
    except OSError:
        return None
    try:
        from pipeline import svc
        logger.info("Separating the vocal stem of %s for lyric timing…", track.name)
        out = svc.separate_vocals(track, stem)
        if out is None:
            logger.info("demucs is not installed — vocal timing will be "
                        "measured on the mix (scripts/install_svc.sh adds it)")
        return out
    except Exception:  # noqa: BLE001 — a failed separation must not fail the divide
        logger.warning("Vocal stem separation failed for %s — measuring the mix",
                       track, exc_info=True)
        return None


def measure_regions(track: Path | str) -> list[tuple[float, float]]:
    """Where the singing is, measured as directly as the install allows.

    With demucs present the level split runs on the separated VOCAL stem,
    where silence is real silence — a distorted-guitar intro no longer reads
    as a voice (on the mix it did, and the lead mouthed a verse over it).
    Without demucs, the mix-level split stands unchanged."""
    track = Path(track)
    stem = vocal_stem(track)
    if stem is not None:
        return vocal_regions(stem, min_level_db=_STEM_FLOOR_DB)
    return vocal_regions(track)


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


def cut_candidates(spans: list[tuple[float, float]]) -> list[float]:
    """Where the track can be cut without splitting a sung line.

    One candidate per seam between consecutive lines — their shared boundary
    when they run back to back, the middle of the instrumental break when one
    sits between them — plus the vocal onset and the end of the last line, so
    a long intro or outro may stand as a scene of its own."""
    if not spans:
        return []
    cands = [spans[0][0]]
    for (_, prev_end), (start, _) in zip(spans, spans[1:]):
        cands.append((prev_end + start) / 2 if start - prev_end > 0.1 else start)
    cands.append(spans[-1][1])
    return cands


# How much farther from the even-grid target a MEASURED instrumental gap may
# sit and still beat an estimated line seam. A cut in a gap is exact — the
# audio there is silence — while a seam cut trusts the even-pacing estimate,
# which was observed to graze a sung word by ~0.4s while a clean break sat
# 1.65s away. Trading up to this much evenness for a guaranteed-clean cut.
GAP_PREFER_SECS = 2.0


def snap_cuts(n_scenes: int, total: float,
              spans: list[tuple[float, float]],
              regions: list[tuple[float, float]] | None = None, *,
              min_secs: float, max_secs: float) -> list[float]:
    """Cut points tiling [0, *total*] into *n_scenes* windows whose seams sit
    between lyric lines instead of through them.

    Each seam snaps to the nearest candidate that keeps every window near the
    planner's even length — within ±40 % of it, held inside [*min_secs*,
    *max_secs*] — while leaving room for the windows still to come. Candidates
    are the estimated line seams (cut_candidates) plus, when *regions* is
    given, the middle of every MEASURED gap between vocal regions; a gap is
    preferred over a seam up to GAP_PREFER_SECS farther from the even grid,
    because cutting silence is exact while the seams are estimates. A seam
    with no candidate in reach falls back to the even grid, so the result is
    never worse than the plain division; takes at the *min_secs* floor have
    no slack at all and keep the grid exactly. Every cut lands on the film's
    frame grid (frame_snap) so each window is a whole number of frames and the
    takes cannot drift against the track. Empty when there is nothing to snap
    to or only one scene."""
    if n_scenes < 2 or total <= 0 or not spans:
        return []
    per = total / n_scenes
    lo = min(per, max(min_secs, 0.6 * per))
    hi = max(per, min(max_secs, 1.4 * per))
    cands = [(c, 0.0) for c in cut_candidates(spans)]
    for (_, a_end), (b_start, _) in zip(regions or [], (regions or [])[1:]):
        if b_start - a_end > 0.2:
            cands.append(((a_end + b_start) / 2, GAP_PREFER_SECS))
    cands.sort()
    cuts = [0.0]
    for i in range(1, n_scenes):
        target = i * per
        left = n_scenes - i  # windows still to place after this seam
        earliest = max(cuts[-1] + lo, total - left * hi)
        latest = min(cuts[-1] + hi, total - left * lo)
        near = [(c, bonus) for c, bonus in cands
                if earliest - 0.01 <= c <= latest + 0.01]
        if near:
            best = min(near, key=lambda cb: abs(cb[0] - target) - cb[1])[0]
        else:
            best = min(max(target, earliest), latest)
        cuts.append(frame_snap(best))
    cuts.append(frame_snap(total, down=True))
    return cuts


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
    """The lyric lines whose sung span overlaps the window [*t0*, *t1*).

    A line has to be in the window by more than a FRAME to count as sung in
    it. Consecutive lines share a boundary that rarely lands on the frame grid
    the seams snap to, so without the margin the line ending at the seam is
    handed to both scenes and each is told to mouth a line the other sings."""
    return [line for line, (start, end) in zip(lines, spans)
            if min(end, t1) - max(start, t0) > 1.0 / FPS]
