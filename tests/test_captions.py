import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline.captions as captions


def _film(scenes, durations):
    """Temp work dir with script.json; `durations` maps filename -> seconds."""
    wd = Path(tempfile.mkdtemp(prefix="spielbot-cap-"))
    (wd / "script.json").write_text(json.dumps(scenes))
    patch = mock.patch.object(
        captions, "_duration",
        side_effect=lambda p: durations.get(Path(p).name, 0.0),
    )
    return wd, patch


class TimestampTests(unittest.TestCase):
    def test_formats_hms_millis(self):
        self.assertEqual(captions._timestamp(0), "00:00:00,000")
        self.assertEqual(captions._timestamp(3.5), "00:00:03,500")
        self.assertEqual(captions._timestamp(3661.25), "01:01:01,250")

    def test_negative_clamps_to_zero(self):
        self.assertEqual(captions._timestamp(-5), "00:00:00,000")


class SentenceSplitTests(unittest.TestCase):
    def test_splits_on_sentence_enders(self):
        self.assertEqual(
            captions._split_sentences("Hello there. How are you?  I am fine!"),
            ["Hello there.", "How are you?", "I am fine!"],
        )

    def test_empty(self):
        self.assertEqual(captions._split_sentences("   "), [])


class BuildSrtTests(unittest.TestCase):
    def test_returns_none_without_script(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-cap-"))
        self.assertIsNone(captions.build_srt(wd))

    def test_cumulative_timing_across_scenes(self):
        wd, patch = _film(
            [{"id": 1, "narration": "First scene."},
             {"id": 2, "narration": "Second scene."}],
            {"scene_01_narration.wav": 2.0, "scene_02_narration.wav": 3.0},
        )
        with patch:
            path = captions.build_srt(wd)
        content = path.read_text()
        # Scene 1: 0 -> 2.0 ; scene 2 starts where scene 1 ends: 2.0 -> 5.0
        self.assertIn("00:00:00,000 --> 00:00:02,000", content)
        self.assertIn("00:00:02,000 --> 00:00:05,000", content)
        self.assertIn("First scene.", content)
        self.assertIn("Second scene.", content)

    def test_sentences_share_the_scene_window_by_length(self):
        wd, patch = _film(
            [{"id": 1, "narration": "AAAA. BBBB."}],  # equal length -> half each
            {"scene_01_narration.wav": 4.0},
        )
        with patch:
            path = captions.build_srt(wd)
        content = path.read_text()
        self.assertIn("00:00:00,000 --> 00:00:02,000", content)
        self.assertIn("00:00:02,000 --> 00:00:04,000", content)

    def test_falls_back_to_scene_final_when_wav_missing(self):
        wd, patch = _film(
            [{"id": 1, "narration": "Only video."}],
            {"scene_01_final.mp4": 2.5},  # no narration wav
        )
        with patch:
            path = captions.build_srt(wd)
        self.assertIn("00:00:00,000 --> 00:00:02,500", path.read_text())

    def test_returns_none_when_nothing_on_disk(self):
        wd, patch = _film([{"id": 1, "narration": "Nothing on disk."}], {})
        with patch:
            self.assertIsNone(captions.build_srt(wd))


if __name__ == "__main__":
    unittest.main()
