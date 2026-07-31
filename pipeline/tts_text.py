"""Spoken-text layer: what TTS pronounces vs what captions/readers see.

The narration string plays two roles that used to be entangled: it is the
caption/display text AND the exact characters handed to the TTS engine. The
engines (F5/Chatterbox) offer no SSML, so the only way to steer pronunciation
and pacing is to change the text itself — which would corrupt the captions.
This module disentangles the two:

* **Per-scene split** — a scene normally has ONE text. When the user splits
  it (film editor toggle), ``metadata.tts_text`` is what gets spoken —
  respellings like "led pipes" live there — while ``narration`` stays the
  caption/display text. Absent ⟹ the narration is spoken verbatim.
* **Pause markers** — ``[pause]`` / ``[pause:1.5]`` in spoken text become REAL
  silence: the text is split into chunks, each synthesized separately, and the
  chunks are joined with the requested silence in between (tts_worker.py).
  Markers are valid in narration text too — they are stripped from captions.
* **Sentence gaps** — per-style ``tts_sentence_pause`` (seconds) splices a
  fixed silence between sentences, enforcing narration cadence the models
  don't reliably produce on their own. 0 (the default) keeps model pacing.
"""
from __future__ import annotations

import re

# [pause] / [pause:1.5] / [pause=2] — the explicit break marker. Case-insensitive,
# tolerant of spaces. Seconds are clamped so a typo can't inject minutes of dead air.
PAUSE_DEFAULT_SECS = 0.6
PAUSE_MAX_SECS = 10.0
_PAUSE_RE = re.compile(r"\[\s*pause(?:\s*[:=]\s*(\d+(?:\.\d+)?))?\s*\]", re.IGNORECASE)

# Sentence enders for the auto sentence-gap mode (latin + CJK punctuation, the
# same family captions.py splits cues on).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…。！？])\s+")


def spoken_source(narration: str, tts_text: str | None) -> str:
    """The text TTS should start from: the scene's spoken-text override when
    set, else its narration (caption) text."""
    return (tts_text or "").strip() or (narration or "")


def strip_pause_markers(text: str) -> str:
    """Remove ``[pause]`` markers (for captions, display, and the no-chunking
    TTS fallback), collapsing the space they leave behind."""
    out = _PAUSE_RE.sub(" ", text or "")
    out = re.sub(r" {2,}", " ", out)
    return re.sub(r" +([\n.,;:!?…])", r"\1", out).strip()


def split_pause_chunks(text: str, sentence_pause: float = 0.0) -> list[tuple[str, float]]:
    """Split spoken text into ``[(chunk_text, silence_secs_after), …]``.

    Explicit ``[pause]`` markers split chunks with their requested silence;
    ``sentence_pause > 0`` additionally splices that much silence between
    sentences. A leading marker yields a ``("", secs)`` head entry (silence
    before the first word); a trailing marker leaves its silence on the last
    chunk — narration length times the scene, so a trailing pause is a held
    visual beat. Consecutive markers accumulate. A single-entry result with no
    silence means "nothing to do" — synthesize the text in one take as before.
    """
    items: list[tuple[str, float]] = []
    last = 0
    src = text or ""
    for m in _PAUSE_RE.finditer(src):
        secs = float(m.group(1)) if m.group(1) else PAUSE_DEFAULT_SECS
        items.append((src[last:m.start()], max(0.0, min(PAUSE_MAX_SECS, secs))))
        last = m.end()
    items.append((src[last:], 0.0))

    if sentence_pause and sentence_pause > 0:
        expanded: list[tuple[str, float]] = []
        for seg, gap in items:
            sentences = [s for s in _SENTENCE_SPLIT_RE.split(seg) if s.strip()]
            if not sentences:
                expanded.append((seg, gap))
                continue
            for i, sentence in enumerate(sentences):
                expanded.append((sentence, sentence_pause if i < len(sentences) - 1 else gap))
        items = expanded

    # Normalize: trim chunk text, fold empty chunks' silence into the previous
    # chunk (or a leading silence entry), drop empty zero-gap fragments.
    out: list[tuple[str, float]] = []
    for seg, gap in items:
        seg = seg.strip()
        if seg:
            out.append((seg, gap))
        elif out:
            prev_text, prev_gap = out[-1]
            out[-1] = (prev_text, prev_gap + gap)
        elif gap > 0:
            out.append(("", gap))
    return out
