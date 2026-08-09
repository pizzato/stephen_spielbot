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
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).split())


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


def verify(clip: Path, expected: str) -> tuple[float, str]:
    """(similarity, transcript) for a rendered shot against its scripted line."""
    transcript = transcribe(clip)
    return score(transcript, expected), transcript
