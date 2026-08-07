"""Performance-film script generation (script_mode = "performance")."""
import json
import unittest
from unittest import mock

from pipeline import performance


class PromptAssemblyTests(unittest.TestCase):
    def _meta(self, **over):
        meta = {
            "seconds": 10,
            "setting": "a rainforest clearing at dusk",
            "camera": "locked off at chest height",
            "soundscape": "cicadas throughout, one branch snap at 7s",
            "beats": [{"t0": 0, "t1": 4, "action": "CHICO faces DARLY across a felled log"},
                      {"t0": 4, "t1": 10, "action": "smoke drifts between them"}],
            "lines": [{"speaker": "CHICO", "delivery": "steady and quiet",
                       "text": "You can burn the trees."},
                      {"speaker": "DARLY", "delivery": "low and flat",
                       "text": "Then remember this one."}],
        }
        meta.update(over)
        return meta

    def test_six_blocks_present(self):
        p = performance.build_h3_prompt(
            self._meta(), style_note="35mm grain",
            picture_names=["CHICO", "DARLY"], audio_names=["CHICO", "DARLY"])
        # 1 reference roles, in slot order
        self.assertIn("<Picture 1> is CHICO", p)
        self.assertIn("<Picture 2> is DARLY", p)
        self.assertIn("<Audio 1> is CHICO's voice", p)
        # 2 style, 3 beats + quoted dialogue, 4 camera, 5 audio, 6 refusals
        self.assertIn("35mm grain", p)
        self.assertIn("[0s-4s]", p)
        self.assertIn('says exactly, steady and quiet: "You can burn the trees."', p)
        self.assertIn("Camera: locked off at chest height", p)
        self.assertIn("cicadas throughout", p)
        self.assertIn("Do not add subtitles", p)

    def test_every_line_closes_the_lips(self):
        # Without this the mouth keeps moving through the tail of the clip.
        p = performance.build_h3_prompt(self._meta())
        self.assertEqual(p.count("lips close and all mouth movement stops"), 2)

    def test_music_is_always_refused(self):
        # Performance films carry no score — the model must not invent one.
        p = performance.build_h3_prompt(self._meta(soundscape="rain on a tin roof"))
        self.assertIn("no music of any kind", p)
        self.assertIn("no music.", p)

    def test_refusals_survive_an_empty_scene(self):
        p = performance.build_h3_prompt({})
        self.assertIn("Do not add subtitles", p)
        self.assertIn("no music", p)

    def test_quotes_inside_the_line_are_not_doubled(self):
        p = performance.build_h3_prompt(self._meta(
            lines=[{"speaker": "X", "delivery": "flat", "text": "“hello”"}]))
        self.assertIn('says exactly, flat: "hello"', p)


class NormalizationTests(unittest.TestCase):
    def test_lines_drop_incomplete_entries(self):
        lines = performance.norm_lines([
            {"speaker": "A", "text": "hi"},
            {"speaker": "", "text": "no speaker"},
            {"speaker": "B", "text": ""},
            "not a dict",
        ])
        self.assertEqual([line["speaker"] for line in lines], ["A"])
        self.assertTrue(lines[0]["delivery"])  # defaulted, never blank

    def test_beats_are_clipped_and_ordered(self):
        beats = performance.norm_beats(
            [{"t0": -3, "t1": 99, "action": "x"}, {"t0": 5, "t1": 2, "action": "y"}], 10.0)
        self.assertEqual(beats[0], {"t0": 0.0, "t1": 10.0, "action": "x"})
        self.assertGreater(beats[1]["t1"], beats[1]["t0"])
        self.assertLessEqual(beats[1]["t1"], 10.0)

    def test_beat_starting_past_the_clip_never_inverts(self):
        # Seen from a real generation: a beat at [18s-14s] on a 14 s clip.
        # Clamping only the end leaves t1 < t0, which reaches the model as a
        # backwards time window.
        for raw in ({"t0": 18, "t1": 14, "action": "x"},
                    {"t0": 99, "t1": 0, "action": "x"},
                    {"t0": 14, "t1": 14, "action": "x"}):
            beat = performance.norm_beats([raw], 14.0)[0]
            self.assertLess(beat["t0"], beat["t1"], beat)
            self.assertLessEqual(beat["t1"], 14.0)
            self.assertGreaterEqual(beat["t0"], 0.0)

    def test_beats_never_invert_for_any_input(self):
        for t0 in (-5, 0, 3, 9.5, 10, 40):
            for t1 in (-5, 0, 1, 9.5, 10, 40):
                beat = performance.norm_beats([{"t0": t0, "t1": t1, "action": "x"}], 10.0)[0]
                self.assertLess(beat["t0"], beat["t1"], f"{t0},{t1} -> {beat}")

    def test_speakers_in_first_spoken_order(self):
        lines = performance.norm_lines([
            {"speaker": "B", "text": "1"}, {"speaker": "A", "text": "2"},
            {"speaker": "B", "text": "3"}])
        self.assertEqual(performance.speakers_in(lines), ["B", "A"])

    def test_seconds_clamped_to_the_model_window(self):
        for raw, expected in ((0, performance.SCENE_SECONDS),
                              ("junk", performance.SCENE_SECONDS),
                              (2, performance.MIN_SCENE_SECONDS),
                              (30, performance.MAX_SCENE_SECONDS)):
            self.assertEqual(performance._clamp_seconds(raw), expected)


_LLM_REPLY = json.dumps({
    "style": "handheld 16mm documentary, humid greens",
    "characters": [
        {"name": "CHICO", "description": "stocky man, thick moustache, white shirt",
         "gender": "male", "age": "adult"},
        {"name": "DARLY", "description": "broad rancher, wide-brimmed hat",
         "gender": "male", "age": "mature"},
    ],
    "scenes": [
        {"title": "The felled log", "setting": "a burnt clearing at dusk", "seconds": 10,
         "cast": ["CHICO", "DARLY"], "camera": "locked off",
         "soundscape": "cicadas, distant fire",
         "beats": [{"t0": 0, "t1": 10, "action": "the two men square up"}],
         "lines": [{"speaker": "CHICO", "delivery": "quiet", "text": "You can burn the trees."},
                   {"speaker": "DARLY", "delivery": "flat", "text": "Then remember this one."}]},
        {"title": "After", "setting": "the same clearing, night", "seconds": 30,
         "cast": ["CHICO"], "camera": "slow push",
         "soundscape": "insects",
         "beats": [{"t0": 0, "t1": 8, "action": "CHICO walks away"}],
         "lines": [{"speaker": "CHICO", "delivery": "tired", "text": "Not tonight."}]},
    ],
})


class GenerationTests(unittest.TestCase):
    def _generate(self, reply=_LLM_REPLY, **kwargs):
        with mock.patch.object(performance, "_chat_complete", return_value=reply) as call:
            scenes, style, characters = performance.generate_performance_script(
                "Chico Mendes", 2, cfg={}, **kwargs)
        return scenes, style, characters, call

    def test_scene_shape(self):
        scenes, style, characters, _ = self._generate()
        self.assertEqual(len(scenes), 2)
        self.assertEqual(style, "handheld 16mm documentary, humid greens")
        self.assertEqual([c["name"] for c in characters], ["CHICO", "DARLY"])
        s = scenes[0]
        self.assertEqual(s.id, 1)
        self.assertEqual(s.mode, "performance")
        # No image engine runs for a performance scene.
        self.assertEqual(s.image_prompt, "")
        # The editable video_prompt IS the assembled H3 prompt.
        self.assertIn("<Picture 1> is CHICO", s.video_prompt)
        self.assertIn("Do not add subtitles", s.video_prompt)
        # Narration mirrors the spoken words (captions/description), never TTS input.
        self.assertEqual(s.narration, "You can burn the trees. Then remember this one.")

    def test_metadata_carries_the_structure(self):
        scenes, _, _, _ = self._generate()
        meta = scenes[0].metadata
        self.assertEqual(meta["mode"], "performance")
        self.assertEqual(meta["cast"], ["CHICO", "DARLY"])
        self.assertEqual(len(meta["lines"]), 2)
        self.assertEqual(meta["beats"][0]["action"], "the two men square up")
        self.assertEqual(meta["seconds"], 10.0)

    def test_overlong_scene_is_clamped(self):
        scenes, _, _, _ = self._generate()
        self.assertEqual(scenes[1].metadata["seconds"], performance.MAX_SCENE_SECONDS)
        self.assertEqual(scenes[1].duration, performance.MAX_SCENE_SECONDS)

    def test_hints_reach_the_prompt(self):
        _, _, _, call = self._generate(style_hint="claymation", avoid_hint="gore",
                                       language="Portuguese")
        user_msg = call.call_args[0][2]
        self.assertIn("claymation", user_msg)
        self.assertIn("gore", user_msg)
        self.assertIn("Portuguese", user_msg)

    def test_empty_reply_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self._generate(reply=json.dumps({"style": "x", "scenes": []}))

    def test_is_performance_detects_scenes_and_rows(self):
        scenes, _, _, _ = self._generate()
        self.assertTrue(performance.is_performance(scenes[0]))
        self.assertTrue(performance.is_performance({"metadata": {"mode": "performance"}}))
        self.assertFalse(performance.is_performance({"metadata": {"mode": "narration"}}))
        self.assertFalse(performance.is_performance({}))


if __name__ == "__main__":
    unittest.main()
