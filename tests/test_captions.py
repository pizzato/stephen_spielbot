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


class SpokenTextTests(unittest.TestCase):
    def test_pause_markers_never_reach_captions(self):
        # [pause] markers are TTS pacing directives (pipeline/tts_text.py);
        # typed into the narration they must vanish from the caption text.
        wd, patch = _film(
            [{"id": 1, "narration": "Wait. [pause:1.5] Something still lives."}],
            {"scene_01_narration.wav": 4.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertNotIn("pause", content.lower())
        self.assertIn("Something still lives.", content)

    def test_tts_text_override_does_not_change_captions(self):
        # The spoken-text override (metadata.tts_text) feeds TTS only — the
        # captions keep showing the narration text.
        wd, patch = _film(
            [{"id": 1, "narration": "The lead pipes burst.",
              "metadata": {"tts_text": "The led pipes burst."}}],
            {"scene_01_narration.wav": 3.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("The lead pipes burst.", content)
        self.assertNotIn("led pipes", content)


class DialogueCueTests(unittest.TestCase):
    """Acted scenes caption their spoken lines, paced by length across the
    take — with speaker names only when the scene has more than one voice."""

    def test_multi_speaker_lines_share_the_take_with_names(self):
        wd, patch = _film(
            [{"id": 1, "narration": "", "metadata": {"mode": "dialogue", "lines": [
                {"speaker": "Ana", "text": "AAAA."},
                {"speaker": "Ben", "text": "BBBB."}]}}],
            {"scene_01_final.mp4": 4.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("00:00:00,000 --> 00:00:02,000\nAna: AAAA.", content)
        self.assertIn("00:00:02,000 --> 00:00:04,000\nBen: BBBB.", content)

    def test_single_speaker_drops_the_name(self):
        wd, patch = _film(
            [{"id": 1, "narration": "", "metadata": {"mode": "dialogue", "lines": [
                {"speaker": "Ana", "text": "Just me talking."}]}}],
            {"scene_01_final.mp4": 3.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("00:00:00,000 --> 00:00:03,000\nJust me talking.", content)
        self.assertNotIn("Ana:", content)

    def test_legacy_performance_mode_counts_as_dialogue(self):
        wd, patch = _film(
            [{"id": 1, "narration": "", "metadata": {"mode": "performance", "lines": [
                {"speaker": "Ana", "text": "Old script."}]}}],
            {"scene_01_final.mp4": 2.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("Old script.", content)

    def test_lingering_narration_on_a_dialogue_scene_never_shows(self):
        wd, patch = _film(
            [{"id": 1, "narration": "Leftover voice-over.",
              "metadata": {"mode": "dialogue", "lines": [
                  {"speaker": "Ana", "text": "The line."}]}}],
            {"scene_01_final.mp4": 2.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("The line.", content)
        self.assertNotIn("Leftover", content)


class LyricCueTests(unittest.TestCase):
    """Singing scenes caption their lyric slice (metadata ``sings``), paced
    through the measured vocal ranges so instrumentals stay caption-free."""

    def test_lines_paced_inside_the_vocal_ranges(self):
        wd, patch = _film(
            [{"id": 1, "narration": "", "metadata": {
                "mode": "silent", "singing": True,
                "sings": "LaLaLa\nLoLoLo", "vocal_ranges": [[1.0, 3.0]]}}],
            {"scene_01_final.mp4": 4.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        # 2 s of singing inside a 4 s take: equal-length lines get 1 s each,
        # starting at the voice's onset — the instrumental second stays clean.
        self.assertIn("00:00:01,000 --> 00:00:02,000\nLaLaLa", content)
        self.assertIn("00:00:02,000 --> 00:00:03,000\nLoLoLo", content)

    def test_no_ranges_treats_the_whole_take_as_sung(self):
        wd, patch = _film(
            [{"id": 1, "narration": "", "metadata": {
                "mode": "silent", "singing": True,
                "sings": "LaLaLa\nLoLoLo", "vocal_ranges": []}}],
            {"scene_01_final.mp4": 4.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("00:00:00,000 --> 00:00:02,000\nLaLaLa", content)
        self.assertIn("00:00:02,000 --> 00:00:04,000\nLoLoLo", content)

    def test_instrumental_scene_advances_the_clock_without_cues(self):
        wd, patch = _film(
            [{"id": 1, "narration": "", "metadata": {
                "mode": "silent", "singing": True, "sings": ""}},
             {"id": 2, "narration": "Back to narration."}],
            {"scene_01_final.mp4": 3.0, "scene_02_narration.wav": 2.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("00:00:03,000 --> 00:00:05,000\nBack to narration.", content)

    def test_seam_straddling_line_becomes_one_cue(self):
        # A line sung across the cut between two takes is stamped on both
        # scenes' slices — the captions must show it once, spanning the seam.
        wd, patch = _film(
            [{"id": 1, "narration": "", "metadata": {
                "mode": "silent", "singing": True, "sings": "Same line"}},
             {"id": 2, "narration": "", "metadata": {
                 "mode": "silent", "singing": True,
                 "sings": "Same line\nNext line"}}],
            {"scene_01_final.mp4": 2.0, "scene_02_final.mp4": 4.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertEqual(content.count("Same line"), 1)
        self.assertIn("00:00:00,000 --> 00:00:04,000\nSame line", content)
        self.assertIn("00:00:04,000 --> 00:00:06,000\nNext line", content)

    def test_cue_offsets_ride_the_film_timeline(self):
        wd, patch = _film(
            [{"id": 1, "narration": "Intro first."},
             {"id": 2, "narration": "", "metadata": {
                 "mode": "silent", "singing": True,
                 "sings": "LaLaLa", "vocal_ranges": [[1.0, 2.0]]}}],
            {"scene_01_narration.wav": 2.0, "scene_02_final.mp4": 3.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        # The range is clip-relative: 1–2 s into a scene that starts at 2 s.
        self.assertIn("00:00:03,000 --> 00:00:04,000\nLaLaLa", content)


if __name__ == "__main__":
    unittest.main()


class MeasuredLineTimeTests(unittest.TestCase):
    """A singing scene whose divide stamped ``line_times`` dates each cue off
    the measured span instead of pacing the lines by length."""

    def test_line_times_win_over_pacing(self):
        wd, patch = _film(
            [{"id": 1, "narration": "", "metadata": {
                "mode": "silent", "singing": True,
                "sings": "LaLaLa\nLoLoLo", "vocal_ranges": [[0.0, 4.0]],
                "line_times": [[0.5, 1.2], [2.8, 3.9]]}}],
            {"scene_01_final.mp4": 4.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("00:00:00,500 --> 00:00:01,200\nLaLaLa", content)
        self.assertIn("00:00:02,800 --> 00:00:03,900\nLoLoLo", content)

    def test_mismatched_line_times_fall_back_to_pacing(self):
        wd, patch = _film(
            [{"id": 1, "narration": "", "metadata": {
                "mode": "silent", "singing": True,
                "sings": "LaLaLa\nLoLoLo", "line_times": [[0.5, 1.2]]}}],
            {"scene_01_final.mp4": 4.0},
        )
        with patch:
            content = captions.build_srt(wd).read_text()
        self.assertIn("00:00:00,000 --> 00:00:02,000\nLaLaLa", content)

    def test_divide_stamps_line_times_relative_to_the_window(self):
        from pipeline.song_timing import window_lines
        spans = [(0.0, 2.0), (2.0, 5.5), (5.5, 8.0), (9.0, 12.0)]
        self.assertEqual(window_lines(spans, 5.0, 10.0),
                         [[0.0, 0.5], [0.5, 3.0], [4.0, 5.0]])


class ReadabilityTests(unittest.TestCase):
    """The style's ``min_seconds`` holds short cues on screen: merge with the
    next cue on the same scene into a two-line cue, else extend."""

    def _srt(self, scenes, durations, style):
        wd, patch = _film(scenes, durations)
        with patch:
            return captions.build_srt(wd, style=style).read_text()

    def test_short_lyric_lines_become_couplets(self):
        content = self._srt(
            [{"id": 1, "narration": "", "metadata": {
                "mode": "silent", "singing": True,
                "sings": "Rain leaves the ocean,\nrises to the sky.\nFalls upon the mountain,\nfinds the sea in time."}}],
            {"scene_01_final.mp4": 8.0},
            {"min_seconds": 2.5},
        )
        self.assertIn("00:00:00,000 --> 00:00:03,6", content)
        self.assertIn("Rain leaves the ocean,\nrises to the sky.\n", content)
        self.assertIn("Falls upon the mountain,\nfinds the sea in time.\n", content)
        self.assertEqual(content.count("-->"), 2)

    def test_zero_keeps_every_line(self):
        content = self._srt(
            [{"id": 1, "narration": "", "metadata": {
                "mode": "silent", "singing": True, "sings": "LaLaLa\nLoLoLo"}}],
            {"scene_01_final.mp4": 2.0},
            {"min_seconds": 0},
        )
        self.assertEqual(content.count("-->"), 2)

    def test_merges_across_a_scene_seam(self):
        content = self._srt(
            [{"id": 1, "narration": "One."}, {"id": 2, "narration": "Two."}],
            {"scene_01_narration.wav": 1.0, "scene_02_narration.wav": 1.0},
            {"min_seconds": 2.5},
        )
        # One continuous timeline: the couplet spans the seam, then is held.
        self.assertIn("00:00:00,000 --> 00:00:02,500\nOne.\nTwo.", content)
        self.assertEqual(content.count("-->"), 1)

    def test_held_cue_stops_at_the_next_one(self):
        content = self._srt(
            [{"id": 1, "narration": "A" * 50 + ". Next."}],
            {"scene_01_narration.wav": 3.0},
            {"min_seconds": 4},
        )
        self.assertIn("00:00:02,7", content)  # first cue held only to the second's start
        self.assertEqual(content.count("-->"), 2)

    def test_long_lines_are_held_not_stacked(self):
        long = "A" * 50 + "."
        content = self._srt(
            [{"id": 1, "narration": f"{long} Short."}],
            {"scene_01_narration.wav": 3.0},
            {"min_seconds": 2.5},
        )
        self.assertEqual(content.count("-->"), 2)
        self.assertNotIn(f"{long}\nShort.", content)

    def test_no_merge_across_an_instrumental_gap(self):
        content = self._srt(
            [{"id": 1, "narration": "", "metadata": {
                "mode": "silent", "singing": True, "sings": "LaLaLa\nLoLoLo",
                "line_times": [[0.0, 1.0], [4.0, 5.0]]}}],
            {"scene_01_final.mp4": 6.0},
            {"min_seconds": 2.5},
        )
        self.assertIn("00:00:00,000 --> 00:00:02,500\nLaLaLa", content)
        self.assertIn("00:00:04,000 --> 00:00:06,500\nLoLoLo", content)

    def test_delay_shifts_the_track(self):
        content = self._srt(
            [{"id": 1, "narration": "Hello there."}],
            {"scene_01_narration.wav": 3.0},
            {"delay": 0.4},
        )
        self.assertIn("00:00:00,400 --> 00:00:03,400\nHello there.", content)
        content = self._srt(
            [{"id": 1, "narration": "Hello there."}],
            {"scene_01_narration.wav": 3.0},
            {"delay": -0.5},
        )
        self.assertIn("00:00:00,000 --> 00:00:02,500\nHello there.", content)
