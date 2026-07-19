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


class EditedFilmOrderTests(unittest.TestCase):
    """Films edited after render (scene delete/reorder/add, issue #193) publish
    in scene_edit_order.json order — cues must be laid on that timeline."""

    def test_cues_follow_scene_edit_order(self):
        wd, patch = _film(
            [{"id": 1, "narration": "First scene."},
             {"id": 2, "narration": "Second scene."}],
            {"scene_01_narration.wav": 2.0, "scene_02_narration.wav": 3.0},
        )
        (wd / "scene_edit_order.json").write_text("[2, 1]")
        with patch:
            content = captions.build_srt(wd).read_text()
        # Scene 2 opens the film: 0 -> 3.0, then scene 1: 3.0 -> 5.0.
        self.assertIn("00:00:00,000 --> 00:00:03,000\nSecond scene.", content)
        self.assertIn("00:00:03,000 --> 00:00:05,000\nFirst scene.", content)

    def test_scene_removed_from_order_is_skipped(self):
        wd, patch = _film(
            [{"id": 1, "narration": "First scene."},
             {"id": 2, "narration": "Second scene."},
             {"id": 3, "narration": "Third scene."}],
            {"scene_01_narration.wav": 2.0, "scene_02_narration.wav": 3.0,
             "scene_03_narration.wav": 4.0},
        )
        (wd / "scene_edit_order.json").write_text("[1, 3]")
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertNotIn("Second scene.", content)
        self.assertIn("00:00:02,000 --> 00:00:06,000\nThird scene.", content)

    def test_unknown_ids_in_order_are_ignored(self):
        wd, patch = _film(
            [{"id": 1, "narration": "First scene."}],
            {"scene_01_narration.wav": 2.0},
        )
        (wd / "scene_edit_order.json").write_text("[1, 9]")
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("First scene.", content)

    def test_final_duration_governs_over_wav(self):
        # The originally-last scene's final carries a ~2 s freeze tail its wav
        # doesn't. Moved off the end, that tail still occupies the timeline, so
        # the final's duration must win.
        wd, patch = _film(
            [{"id": 1, "narration": "First scene."},
             {"id": 2, "narration": "Second scene."}],
            {"scene_01_narration.wav": 2.0,
             "scene_02_narration.wav": 3.0, "scene_02_final.mp4": 5.0},
        )
        (wd / "scene_edit_order.json").write_text("[2, 1]")
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("00:00:00,000 --> 00:00:05,000\nSecond scene.", content)
        self.assertIn("00:00:05,000 --> 00:00:07,000\nFirst scene.", content)


def _localized_film():
    """Two-scene film with a Spanish localization whose scenes run at different
    durations than the original cut (translated narration re-times scenes)."""
    wd = Path(tempfile.mkdtemp(prefix="spielbot-cap-"))
    (wd / "script.json").write_text(json.dumps(
        [{"id": 1, "narration": "First scene."}, {"id": 2, "narration": "Second scene."}]
    ))
    (wd / "localize_scripts").mkdir()
    (wd / "localize_scripts" / "es.json").write_text(json.dumps(
        {"lang": "es", "scenes": {"1": "Primera escena.", "2": "Segunda escena."}}
    ))
    durations = {  # keyed on a path suffix so original vs localized differ
        "localize/es/scene_01_narration.wav": 3.0,
        "localize/es/scene_02_narration.wav": 4.0,
        "scene_01_narration.wav": 2.0,
        "scene_02_narration.wav": 3.0,
    }

    def _lookup(p):
        p = str(p)
        for suffix, dur in durations.items():
            if p.endswith(suffix) and (("localize" in suffix) == ("localize" in p)):
                return dur
        return 0.0

    return wd, mock.patch.object(captions, "_duration", side_effect=_lookup)


class LocalizedSrtTests(unittest.TestCase):
    def test_translated_text_on_original_timeline(self):
        # Publishing the ORIGINAL cut with a Spanish caption track: Spanish
        # words, original-cut timings.
        wd, patch = _localized_film()
        with patch:
            path = captions.build_srt(wd, lang="es")
        self.assertEqual(path.name, "captions_es.srt")
        content = path.read_text()
        self.assertIn("Primera escena.", content)
        self.assertIn("00:00:00,000 --> 00:00:02,000", content)
        self.assertIn("00:00:02,000 --> 00:00:05,000", content)

    def test_localized_timeline_retimes_cues(self):
        # Publishing the SPANISH cut: same words, timings from the localized
        # scene durations (3s + 4s, not 2s + 3s).
        wd, patch = _localized_film()
        with patch:
            path = captions.build_srt(wd, lang="es", timing_lang="es")
        self.assertEqual(path.name, "captions_es_t-es.srt")
        content = path.read_text()
        self.assertIn("00:00:00,000 --> 00:00:03,000", content)
        self.assertIn("00:00:03,000 --> 00:00:07,000", content)

    def test_original_text_on_localized_timeline(self):
        # English captions overlaying the Spanish cut.
        wd, patch = _localized_film()
        with patch:
            path = captions.build_srt(wd, timing_lang="es")
        content = path.read_text()
        self.assertIn("First scene.", content)
        self.assertIn("00:00:00,000 --> 00:00:03,000", content)

    def test_unknown_language_returns_none(self):
        wd, patch = _localized_film()
        with patch:
            self.assertIsNone(captions.build_srt(wd, lang="fr"))

    def test_untranslated_scene_falls_back_to_original_text(self):
        wd, patch = _localized_film()
        (wd / "localize_scripts" / "es.json").write_text(json.dumps(
            {"lang": "es", "scenes": {"1": "Primera escena."}}  # scene 2 untranslated
        ))
        with patch:
            content = captions.build_srt(wd, lang="es").read_text()
        self.assertIn("Primera escena.", content)
        self.assertIn("Second scene.", content)


if __name__ == "__main__":
    unittest.main()
