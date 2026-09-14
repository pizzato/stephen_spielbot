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
import tempfile
from pathlib import Path

logger = logging.getLogger("video_gen")

_RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "whisper_words.py"
# Below this fraction of lyric words matched, the alignment is a guess of its
# own — fall back to even pacing rather than trust it.
_MIN_MATCH = 0.5
# A transcript word may sit this far outside a measured vocal region before
# it is discarded as hallucination.
_REGION_SLACK = 0.75
# A stretch shorter than this is not re-transcribed on its own before being
# dropped: it cannot hold a mimed phrase, and the pass is not free.
_REASK_SECS = 1.5


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


def _words_in_slice(stem: Path, start: float, end: float,
                    language: str) -> list[tuple[str, float, float]]:
    """Transcribe [*start*, *end*) of *stem* ALONE, on the track's own clock.

    A whole-track pass reads a stretch in the context of its 30-second window
    and skips a wordless vocal in it — an "ooh-ooh" intro came back empty
    beside the verses and as 83 "whoa"s when handed over on its own. That gap
    is the whole difference between separation bleed and a voice with no
    lyrics, so the stretches nothing was heard in are asked again by
    themselves. Failure returns nothing, which reads as bleed."""
    from pipeline.assembler import _resolve_media_tool
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cut = Path(tmp) / "region.wav"
            subprocess.run(
                [_resolve_media_tool("ffmpeg"), "-y", "-v", "error",
                 "-i", str(stem), "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                 "-c:a", "pcm_s16le", str(cut)],
                check=True, capture_output=True)
            words = word_times(cut, language) or []
    except Exception:  # noqa: BLE001 — a failed re-ask must not fail the divide
        logger.warning("Could not re-transcribe %.2f–%.2fs of %s",
                       start, end, stem.name, exc_info=True)
        return []
    return [(w, t0 + start, t1 + start) for w, t0, t1 in words]


def voiced_regions(regions: list[tuple[float, float]],
                   words: list[tuple[str, float, float]] | None,
                   *, stem: Path | None = None,
                   language: str = "") -> list[tuple[float, float]]:
    """The measured vocal regions, trimmed to the singing actually heard.

    The level split answers "is the stem loud here", which is not the same
    question as "is someone singing here": separation bleed rides an
    instrumental intro or an outro loudly enough to clear the floor, and the
    scene handed one is told a voice sings over it while the lyric sheet has
    no words to give it — the cast mimed a fingerpicked guitar for the first
    20 s of a film, and 13 s of wordless outro at the end of it.

    Each region is cut back to the stretch its words occupy, _REGION_SLACK
    either side so a held note or a breath survives. Before any stretch worth
    more than _REASK_SECS is thrown away it is transcribed ON ITS OWN, because
    that is exactly where a wordless vocal hides: an "ooh-ooh" intro comes
    back empty beside the verses and as 130 "oh"s when handed over alone,
    while a fingerpicked outro stays empty either way. Whatever the second
    pass hears is kept on the same terms — a 13 s outro that yields one stray
    word keeps a second of singing, not thirteen.

    With no transcript at all every measured region stands, unchanged."""
    if not words:
        return list(regions)
    out = []
    for start, end in regions:
        heard = _within(words, start, end)
        if stem is not None:
            for gap0, gap1 in _unheard(start, end, heard):
                if gap1 - gap0 >= _REASK_SECS:
                    heard += _within(_words_in_slice(stem, gap0, gap1, language),
                                     gap0, gap1)
        if not heard:
            continue
        lo = max(start, min(w[1] for w in heard) - _REGION_SLACK)
        hi = min(end, max(w[2] for w in heard) + _REGION_SLACK)
        if hi - lo > 0.2:
            out.append((round(lo, 2), round(hi, 2)))
    return out


def _within(words: list[tuple[str, float, float]], start: float,
            end: float) -> list[tuple[str, float, float]]:
    """The words whose middle falls inside [*start*, *end*], plus slack."""
    return [w for w in words
            if start - _REGION_SLACK <= (w[1] + w[2]) / 2 <= end + _REGION_SLACK]


def _unheard(start: float, end: float,
             heard: list[tuple[str, float, float]]) -> list[tuple[float, float]]:
    """The head and tail of a region no word claimed — the whole of it when
    none did. Gaps BETWEEN words are breaths and instrumental breaks the
    region split already accounts for; only the edges are about to be lost."""
    if not heard:
        return [(start, end)]
    lo = min(w[1] for w in heard) - _REGION_SLACK
    hi = max(w[2] for w in heard) + _REGION_SLACK
    return [(start, min(lo, end)), (max(hi, start), end)]


def _match(lines: list[str], words: list[tuple[str, float, float]],
           regions: list[tuple[float, float]] | None = None,
           ) -> tuple[float, list[list[tuple[str, float | None, float | None]]],
                      list[tuple[str, float, float]]]:
    """Match the lyric sheet against the (region-gated) transcript.

    Returns ``(ratio, per_line, transcript)``. *per_line[i]* is each original
    lyric word of line *i* with its matched (start, end), or Nones when that
    word was not in the transcript. *transcript* is the gated
    ``(norm, t0, t1)`` list used for matching."""
    if regions:
        words = [w for w in words
                 if any(a - _REGION_SLACK <= (w[1] + w[2]) / 2 <= b + _REGION_SLACK
                        for a, b in regions)]
    transcript = [(_norm_word(w), t0, t1) for w, t0, t1 in words if _norm_word(w)]
    lyric: list[tuple[int, str]] = []  # (line index, original word)
    for i, line in enumerate(lines):
        lyric.extend((i, w) for w in str(line).split() if _norm_word(w))
    per_line: list[list[tuple[str, float | None, float | None]]] = [
        [] for _ in lines]
    if not transcript or not lyric:
        return 0.0, per_line, transcript
    times: list[tuple[float, float] | None] = [None] * len(lyric)
    matcher = difflib.SequenceMatcher(
        a=[_norm_word(w) for _, w in lyric],
        b=[w for w, _, _ in transcript], autojunk=False)
    matched = 0
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            times[block.a + k] = (transcript[block.b + k][1],
                                  transcript[block.b + k][2])
            matched += 1
    for (line_idx, word), t in zip(lyric, times):
        if t is None:
            per_line[line_idx].append((word, None, None))
        else:
            per_line[line_idx].append((word, t[0], t[1]))
    return matched / len(lyric), per_line, transcript


def _fill_untimed(words: list[list]) -> None:
    """Give unmatched words in a line the gap between their matched neighbours."""
    timed = [i for i, w in enumerate(words) if w[1] is not None]
    if not timed:
        return
    durs = [words[i][2] - words[i][1] for i in timed
            if words[i][2] is not None and words[i][2] > words[i][1]]
    dur = (sum(durs) / len(durs)) if durs else 0.3
    end = words[timed[0]][1]
    for j in range(timed[0] - 1, -1, -1):
        words[j][2] = end
        words[j][1] = end - dur
        end = words[j][1]
    start = words[timed[-1]][2]
    for j in range(timed[-1] + 1, len(words)):
        words[j][1] = start
        words[j][2] = start + dur
        start = words[j][2]
    for a, b in zip(timed, timed[1:]):
        n = b - a - 1
        if n <= 0:
            continue
        gap0, gap1 = words[a][2], words[b][1]
        width = max(gap1 - gap0, 0.2 * n) / n
        for k in range(n):
            i = a + 1 + k
            words[i][1] = gap0 + k * width
            words[i][2] = gap0 + (k + 1) * width


def line_word_times(lines: list[str],
                    words: list[tuple[str, float, float]] | None, *,
                    regions: list[tuple[float, float]] | None = None,
                    ) -> list[list[tuple[str, float, float]]] | None:
    """Each lyric line's words with their sung times, or None when the match
    is too weak to trust.

    A word whisper garbled is interpolated between its matched neighbours
    so a line straddling a scene seam can be split at the words it actually
    carries. A line no word matched comes back empty — the caller then
    splits it by its line span instead."""
    if not words:
        return None
    ratio, per_line, _ = _match(lines, words, regions)
    if not lines or ratio < _MIN_MATCH:
        return None
    out: list[list[tuple[str, float, float]]] = []
    for ws in per_line:
        filled = [[w, t0, t1] for w, t0, t1 in ws]
        _fill_untimed(filled)
        out.append([(w, round(float(t0), 2), round(float(t1), 2))
                    for w, t0, t1 in filled if t0 is not None and t1 is not None])
    return out


def align_lines(stem: Path, lines: list[str], *, language: str = "",
                regions: list[tuple[float, float]] | None = None,
                words: list[tuple[str, float, float]] | None = None,
                ) -> list[tuple[float, float]] | None:
    """Measured (start, end) for each lyric line, or None to keep the estimate.

    *regions* are the energy-measured vocal stretches: transcript words
    outside them (plus slack) are hallucinations and are dropped before
    matching. Lines whisper garbled are interpolated between their matched
    neighbours; the result is monotonic and non-overlapping, which is what
    lines_in_window/snap_cuts assume.

    *words* is an already-fetched transcript (word_times), so a caller that
    also needs it — voiced_regions does — transcribes the stem once."""
    if words is None:
        words = word_times(stem, language)
    if words is None:
        return None
    ratio, per_line, transcript = _match(lines, words, regions)
    lyric_n = sum(len(ws) for ws in per_line)
    if not transcript or lyric_n == 0:
        return None
    found: dict[int, list[float]] = {}
    for i, ws in enumerate(per_line):
        timed = [(a, b) for _, a, b in ws if a is not None]
        if timed:
            found[i] = [min(a for a, _ in timed), max(b for _, b in timed)]
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
