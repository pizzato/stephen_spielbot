"""Spoken-text layer (pipeline/tts_text.py) + its wiring into generate_narration.

The narration string used to be both the caption text and the literal TTS
input. These tests cover the disentanglement: the per-scene spoken-text split,
[pause] markers becoming real spliced silence, per-sentence gaps, and the
fallback to a single marker-less take when chunk splicing fails.
"""
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import pipeline.tts_text as tts_text
import pipeline.tts_worker as tts_worker


class SpokenSourceTests(unittest.TestCase):
    def test_override_wins(self):
        self.assertEqual(tts_text.spoken_source("caption text", "spoken text"), "spoken text")

    def test_blank_override_falls_back_to_narration(self):
        self.assertEqual(tts_text.spoken_source("caption text", "   "), "caption text")
        self.assertEqual(tts_text.spoken_source("caption text", None), "caption text")


class StripMarkerTests(unittest.TestCase):
    def test_strips_and_tidies_spacing(self):
        self.assertEqual(
            tts_text.strip_pause_markers("Wait. [pause:1.5] Something still lives."),
            "Wait. Something still lives.",
        )
        self.assertEqual(
            tts_text.strip_pause_markers("Hello [pause] world"),
            "Hello world",
        )
        self.assertEqual(
            tts_text.strip_pause_markers("Hello[pause]. World"),
            "Hello. World",
        )

    def test_no_markers_untouched(self):
        self.assertEqual(tts_text.strip_pause_markers("Plain text."), "Plain text.")


class SplitPauseChunkTests(unittest.TestCase):
    def test_plain_text_single_chunk(self):
        self.assertEqual(tts_text.split_pause_chunks("Hello there."),
                         [("Hello there.", 0.0)])

    def test_default_and_explicit_seconds(self):
        self.assertEqual(
            tts_text.split_pause_chunks("A.[pause]B.[pause:2]C."),
            [("A.", tts_text.PAUSE_DEFAULT_SECS), ("B.", 2.0), ("C.", 0.0)],
        )

    def test_marker_variants_and_clamp(self):
        self.assertEqual(
            tts_text.split_pause_chunks("A.[PAUSE : 1.5]B.[pause=99]C."),
            [("A.", 1.5), ("B.", tts_text.PAUSE_MAX_SECS), ("C.", 0.0)],
        )

    def test_leading_and_trailing_pauses(self):
        self.assertEqual(tts_text.split_pause_chunks("[pause:1]Hi."), [("", 1.0), ("Hi.", 0.0)])
        self.assertEqual(tts_text.split_pause_chunks("Bye.[pause:2]"), [("Bye.", 2.0)])

    def test_consecutive_markers_accumulate(self):
        self.assertEqual(
            tts_text.split_pause_chunks("A.[pause:1][pause:2]B."),
            [("A.", 3.0), ("B.", 0.0)],
        )

    def test_sentence_pause_splits_sentences(self):
        self.assertEqual(
            tts_text.split_pause_chunks("One. Two! Three?", sentence_pause=0.4),
            [("One.", 0.4), ("Two!", 0.4), ("Three?", 0.0)],
        )

    def test_sentence_pause_does_not_double_explicit_markers(self):
        self.assertEqual(
            tts_text.split_pause_chunks("One. [pause:2] Two.", sentence_pause=0.4),
            [("One.", 2.0), ("Two.", 0.0)],
        )

    def test_only_markers_yields_silence_entry(self):
        self.assertEqual(tts_text.split_pause_chunks("[pause:3]"), [("", 3.0)])


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


class GenerateNarrationSplicingTests(unittest.TestCase):
    """generate_narration with a fake local F5 backend writing real WAVs."""

    CHUNK_SECS = 0.5
    RATE = 24000

    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        f.write(b"RIFFfake-reference")
        f.close()
        self.ref = Path(f.name)
        self.out = Path(tempfile.mkdtemp()) / "narration.wav"
        self.texts = []

    def _fake_f5_local(self, text, ref, output_path, speed=1.0,
                       tts_engine="openf5", language="en"):
        self.texts.append(text)
        with wave.open(str(output_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(b"\x01\x00" * int(self.RATE * self.CHUNK_SECS))

    def _generate(self, text, **kwargs):
        with mock.patch.object(tts_worker, "_f5_local", side_effect=self._fake_f5_local):
            tts_worker.generate_narration(text, self.out, reference_wav=self.ref,
                                          host="localhost", **kwargs)

    def test_plain_text_is_a_single_take(self):
        self._generate("Nothing special here.")
        self.assertEqual(self.texts, ["Nothing special here."])
        self.assertAlmostEqual(_wav_seconds(self.out), self.CHUNK_SECS, delta=0.02)

    def test_pause_marker_splices_real_silence(self):
        self._generate("Wait.[pause:1.5]Something still lives.")
        self.assertEqual(self.texts, ["Wait.", "Something still lives."])
        self.assertAlmostEqual(_wav_seconds(self.out), 2 * self.CHUNK_SECS + 1.5, delta=0.02)

    def test_sentence_pause_splices_between_sentences(self):
        self._generate("One. Two.", sentence_pause=0.4)
        self.assertEqual(self.texts, ["One.", "Two."])
        self.assertAlmostEqual(_wav_seconds(self.out), 2 * self.CHUNK_SECS + 0.4, delta=0.02)

    def test_leading_and_trailing_pause(self):
        self._generate("[pause:1]Held beat.[pause:2]")
        self.assertEqual(self.texts, ["Held beat."])
        self.assertAlmostEqual(_wav_seconds(self.out), self.CHUNK_SECS + 3.0, delta=0.02)

    def test_respelled_spoken_text_reaches_the_engine_verbatim(self):
        # The per-scene split stores the respelling directly; TTS must receive
        # exactly those characters.
        self._generate("The led pipes burst.")
        self.assertEqual(self.texts, ["The led pipes burst."])

    def test_chunk_temp_files_cleaned_up(self):
        self._generate("A.[pause]B.")
        leftovers = list(self.out.parent.glob("*.chunk*.wav"))
        self.assertEqual(leftovers, [])

    def test_chunk_failure_falls_back_to_single_take(self):
        calls = {"n": 0}

        def flaky(text, ref, output_path, speed=1.0, tts_engine="openf5", language="en"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("worker hiccup")
            self._fake_f5_local(text, ref, output_path, speed, tts_engine, language)

        with mock.patch.object(tts_worker, "_f5_local", side_effect=flaky):
            tts_worker.generate_narration("A.[pause:1]B.", self.out,
                                          reference_wav=self.ref, host="localhost")
        # chunk A ok, chunk B fails, then one marker-less take of the whole text
        self.assertEqual(self.texts, ["A.", "A. B."])
        self.assertAlmostEqual(_wav_seconds(self.out), self.CHUNK_SECS, delta=0.02)


if __name__ == "__main__":
    unittest.main()
