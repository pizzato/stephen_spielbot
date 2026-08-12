"""Quality gate for performance shots: did they say the line?

H3 is stochastic — a shot can come out with the wrong words, invented tail
speech, or garbled delivery, and until now nothing checked. The gate
transcribes each rendered shot (faster-whisper, CPU, ~2s per clip against a
~6 minute render) and scores the transcript against the scripted line; a
failing shot is retaken with a fresh seed and the better take kept. That is
what turns a hit-rate into consistency: misses get caught, not shipped.

faster-whisper is a soft dependency — without it the gate silently stands
down (available() is False) and renders behave exactly as before.
"""
from __future__ import annotations

import logging
import re
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger("video_gen")

# Below this transcript-similarity a shot is considered a miss. 0.6 tolerates
# ASR quirks and accents while catching wrong/invented/missing speech.
DEFAULT_THRESHOLD = 0.6

_model = None


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        # base.en int8 on CPU: ~74 MB, loads once per render process.
        _model = WhisperModel("base.en", device="cpu", compute_type="int8")
    return _model


def _norm_words(text: str) -> str:
    # Scripts are written with typographic apostrophes and ASR writes plain
    # ones, so "I’m" has to normalize to the same token as "I'm" — otherwise
    # every contraction splits into two words, which both depresses the score
    # of a perfectly good take and inflates the word count a retake is sized
    # from ("I’m David" reads as 3 words, not 2).
    text = (text or "").lower().replace("’", "'").replace("ʼ", "'")
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text).split())


def transcribe(clip: Path) -> str:
    """The words spoken in a clip, via its own audio track."""
    from pipeline.assembler import extract_audio
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "gate.wav"
        extract_audio(Path(clip), wav)
        segments, _info = _get_model().transcribe(str(wav), beam_size=5)
        return " ".join(s.text.strip() for s in segments)


def score(transcript: str, expected: str) -> float:
    """Word-level similarity between what was said and what was scripted."""
    a, b = _norm_words(transcript), _norm_words(expected)
    if not b:
        return 1.0  # nothing scripted — nothing to fail
    if not a:
        return 0.0
    return SequenceMatcher(None, a.split(), b.split()).ratio()


def word_count(transcript: str) -> int:
    return len(_norm_words(transcript).split())


# Air after the last word on a retake that bought time, so the delivery has
# somewhere to land instead of ending on the final frame.
RETAKE_AIR_SECONDS = 1.0


def truncated(transcript: str, expected: str, threshold: float = DEFAULT_THRESHOLD) -> bool:
    """True when the take said the START of its line and then ran out of clip.

    The overall score is a miss, but the words that DID come out match the head
    of the line — the signature of a last frame arriving mid-sentence. Retaking
    on a fresh seed cannot fix that: the clip is simply too short for the pace
    the model chose. Only a longer clip can.
    """
    heard = _norm_words(transcript).split()
    want = _norm_words(expected).split()
    if not heard or len(heard) >= len(want):
        return False
    return score(transcript, " ".join(want[:len(heard)])) >= threshold


def seconds_for_full_line(transcript: str, expected: str, clip_seconds: float) -> float:
    """Clip length the WHOLE line needs, at the pace the take actually spoke.

    A truncated take is its own measurement: it delivered *heard* words in
    *clip_seconds*, so the rest needs the same seconds per word, plus a beat of
    air. Sizing the retake from the delivery beats sizing it from a constant,
    because H3's pace varies more than 2:1 with the line — short dramatic
    sentences run at half the rate of flowing prose, and it is exactly those
    that overrun. Returns 0.0 when there is nothing to measure.
    """
    heard = word_count(transcript)
    want = word_count(expected)
    if heard <= 0 or want <= heard or clip_seconds <= 0:
        return 0.0
    return clip_seconds * want / heard + RETAKE_AIR_SECONDS


# A "silent" shot may carry a stray ASR token or two from room tone; three or
# more recognized words is speech the model invented against instructions.
SILENCE_MAX_WORDS = 2


def verify(clip: Path, expected: str) -> tuple[float, str]:
    """(similarity, transcript) for a rendered shot against its scripted line."""
    transcript = transcribe(clip)
    return score(transcript, expected), transcript
