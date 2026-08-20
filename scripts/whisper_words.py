"""Word-level timestamps for a WAV (a separated vocal stem), as JSON on stdout.

Runs INSIDE the seed-vc venv — faster-whisper lives there beside demucs
(scripts/install_svc.sh) — and is invoked by pipeline/lyric_align.py with that
venv's python. Model weights (Systran/faster-whisper-small, int8) download
from Hugging Face on the first run, same as seed-vc's and demucs's own.

Usage: python whisper_words.py <wav> [language]
Output: {"language": "en", "words": [{"word", "start", "end"}, ...]}
"""
import json
import sys


def main() -> None:
    path = sys.argv[1]
    language = sys.argv[2] if len(sys.argv) > 2 else None
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    # No VAD filter: silero VAD is speech-trained and can drop sung stretches.
    # Hallucinations over instrumental sections are handled by the caller,
    # which gates words by the measured vocal regions and by the lyric sheet.
    #
    # temperature=0 pins the decode to greedy/beam rather than whisper's
    # default fallback ladder, which SAMPLES (temperature 0.2…1.0) whenever a
    # segment trips the compression-ratio or logprob threshold. A sung vocal
    # stem trips it constantly, and the sampling made the transcript a coin
    # flip: back-to-back runs over one 2-minute song returned 82 words then
    # 119, one of them missing the first 30 seconds entirely — so the lines
    # each scene was told to mouth changed from render to render.
    segments, info = model.transcribe(
        path, language=language or None, word_timestamps=True,
        vad_filter=False, condition_on_previous_text=False, beam_size=5,
        temperature=0.0)
    words = []
    for segment in segments:
        for w in segment.words or []:
            words.append({"word": w.word.strip(),
                          "start": round(float(w.start), 2),
                          "end": round(float(w.end), 2)})
    json.dump({"language": info.language, "words": words}, sys.stdout)


if __name__ == "__main__":
    main()
