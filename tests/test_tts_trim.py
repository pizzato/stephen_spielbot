"""_trim_trailing_artifacts: cut Chatterbox's post-speech junk, leave good
audio alone. Synthetic WAVs — no TTS, no network."""
import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from pipeline.tts_worker import _trim_trailing_artifacts

RATE = 24000


def _write_wav(path: Path, segments):
    """segments = [(duration_secs, amplitude 0..1)] → mono 16-bit WAV of sines."""
    samples = array("h")
    for dur, amp in segments:
        peak = amp * 32767
        for i in range(int(dur * RATE)):
            samples.append(int(peak * math.sin(2 * math.pi * 220 * i / RATE)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())


def _duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


class TrimTrailingArtifactsTests(unittest.TestCase):
    def test_junk_tail_is_cut(self):
        # 2s speech, then Chatterbox-style tail: near-silence, a quiet babble
        # burst well below the speech level, more silence.
        p = Path(tempfile.mkdtemp(prefix="spielbot-trim-")) / "n.wav"
        _write_wav(p, [(2.0, 0.2), (1.5, 0.002), (0.4, 0.008), (1.2, 0.002)])
        self.assertAlmostEqual(_duration(p), 5.1, places=1)
        _trim_trailing_artifacts(p)
        self.assertAlmostEqual(_duration(p), 2.4, delta=0.15)  # speech + 0.4s pad

    def test_loud_babble_after_long_silence_is_cut(self):
        # Junk isn't always quiet: some takes hallucinate babble as loud as
        # speech, after seconds of dead silence. Loudness can't spot it, the
        # silent gap can — cut at the last loud window before the gap.
        p = Path(tempfile.mkdtemp(prefix="spielbot-trim-")) / "n.wav"
        _write_wav(p, [(2.0, 0.2), (4.0, 0.001), (3.0, 0.15)])
        self.assertAlmostEqual(_duration(p), 9.0, places=1)
        _trim_trailing_artifacts(p)
        self.assertAlmostEqual(_duration(p), 2.4, delta=0.15)  # speech + 0.4s pad

    def test_babble_swell_after_short_gap_is_cut(self):
        # The pipe-organ/de scene-1 shape: speech, ~1.5s of near-silence with
        # a lone click in it, then a babble swell almost as loud as speech
        # running to EOF. The gap is too short for the old >2s rule and the
        # swell too loud for the quiet-tail rule — the stretch+short-remainder
        # test must still catch it.
        p = Path(tempfile.mkdtemp(prefix="spielbot-trim-")) / "n.wav"
        _write_wav(p, [(3.0, 0.2), (0.7, 0.0005), (0.1, 0.15), (0.8, 0.0005),
                       (0.9, 0.12)])
        _trim_trailing_artifacts(p)
        self.assertAlmostEqual(_duration(p), 3.4, delta=0.15)  # speech + 0.4s pad

    def test_long_pause_resumed_by_narration_survives(self):
        # Chatterbox renders real pauses past 2s (measured 2.3s in the wild).
        # A long pause followed by several seconds of actual narration is NOT
        # junk — the old blanket >2s-gap rule would have deleted half the
        # scene here.
        p = Path(tempfile.mkdtemp(prefix="spielbot-trim-")) / "n.wav"
        _write_wav(p, [(3.0, 0.2), (2.5, 0.001), (5.0, 0.2)])
        before = _duration(p)
        _trim_trailing_artifacts(p)
        self.assertEqual(_duration(p), before)

    def test_quiet_voice_junk_tail_is_cut(self):
        # Speech level is per-take (quiet voices sit ~-29 dB RMS, near the old
        # absolute -32 dB threshold). The loudness cutoff must adapt or a
        # quiet take reads as all-silence.
        p = Path(tempfile.mkdtemp(prefix="spielbot-trim-")) / "n.wav"
        _write_wav(p, [(2.0, 0.05), (2.0, 0.0008)])
        _trim_trailing_artifacts(p)
        self.assertAlmostEqual(_duration(p), 2.4, delta=0.15)

    def test_quiet_voice_pauses_survive(self):
        # A quiet voice's real sentence pause must not become a "gap": the
        # relative threshold keeps its speech loud and the pause short.
        p = Path(tempfile.mkdtemp(prefix="spielbot-trim-")) / "n.wav"
        _write_wav(p, [(2.0, 0.05), (0.8, 0.0008), (2.0, 0.05)])
        before = _duration(p)
        _trim_trailing_artifacts(p)
        self.assertEqual(_duration(p), before)

    def test_sentence_pause_does_not_trigger_gap_cut(self):
        # A ~1s pause between sentences is normal speech — the speech after it
        # must survive untouched.
        p = Path(tempfile.mkdtemp(prefix="spielbot-trim-")) / "n.wav"
        _write_wav(p, [(2.0, 0.2), (1.0, 0.001), (2.0, 0.2)])
        before = _duration(p)
        _trim_trailing_artifacts(p)
        self.assertEqual(_duration(p), before)

    def test_speech_to_the_end_is_untouched(self):
        p = Path(tempfile.mkdtemp(prefix="spielbot-trim-")) / "n.wav"
        _write_wav(p, [(3.0, 0.2)])
        before = _duration(p)
        _trim_trailing_artifacts(p)
        self.assertEqual(_duration(p), before)

    def test_short_natural_tail_is_untouched(self):
        # 0.5s of quiet decay after speech is within pad+slack — no rewrite.
        p = Path(tempfile.mkdtemp(prefix="spielbot-trim-")) / "n.wav"
        _write_wav(p, [(2.0, 0.2), (0.5, 0.002)])
        before = _duration(p)
        _trim_trailing_artifacts(p)
        self.assertEqual(_duration(p), before)

    def test_all_quiet_file_is_untouched(self):
        # No window ever clears the threshold — don't guess, don't truncate.
        p = Path(tempfile.mkdtemp(prefix="spielbot-trim-")) / "n.wav"
        _write_wav(p, [(3.0, 0.005)])
        before = _duration(p)
        _trim_trailing_artifacts(p)
        self.assertEqual(_duration(p), before)

    def test_missing_file_is_a_noop(self):
        _trim_trailing_artifacts(Path("/nonexistent/narration.wav"))  # must not raise


if __name__ == "__main__":
    unittest.main()
