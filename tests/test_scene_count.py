"""An explicit scene count: the length divided into longer or shorter scenes.

A film is commissioned by LENGTH; how many scenes that becomes is normally the
scene contract's business (~12 s of narration, ~10 s a take when the scenes are
clips). A count — from the brief, or the style's own ``video_scenes`` default —
divides the length instead, so fewer scenes are longer ones. The count is what
holds: when a scene cannot stretch that far on the style's video engine, the
LENGTH gives way and the film comes out at what the scenes add up to.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app as gapp  # noqa: E402
import webapp.backend.main as backend  # noqa: E402
from pipeline import performance  # noqa: E402
from pipeline import story as story_mode  # noqa: E402
from scriptstub import STORY  # noqa: E402
from test_styles import TempConfigCase, _style  # noqa: E402


class StylePlanTests(unittest.TestCase):
    """app.style_script_plan resolves the count for the narrated pathway."""

    def test_a_style_count_divides_the_style_length(self):
        ss = {"video_minutes": 4.0, "video_engine": "ltx25", "voice_cadence_wpm": 150}
        auto = gapp.style_script_plan(ss)
        pinned = gapp.style_script_plan({**ss, "video_scenes": 10})
        self.assertEqual(auto["n_scenes"], 20)
        self.assertEqual(pinned["n_scenes"], 10)
        self.assertAlmostEqual(pinned["scene_secs_target"], 24.0)
        self.assertAlmostEqual(pinned["minutes"], 4.0)
        # Twice the room per scene, so twice the narration in it.
        self.assertEqual(pinned["scene_words_target"], 2 * auto["scene_words_target"])

    def test_the_brief_outranks_the_style_default(self):
        ss = {"video_minutes": 4.0, "video_scenes": 10, "video_engine": "ltx25"}
        self.assertEqual(gapp.style_script_plan(ss, minutes=4.0, n_scenes=5)["n_scenes"], 5)

    def test_a_scene_never_outruns_the_video_engine(self):
        # MiniMax holds 12 s in a take: 3 scenes of a 4-minute film cannot be
        # 80 s each, so the count stands and the film runs 36 s.
        ss = {"video_minutes": 4.0, "video_scenes": 3, "video_engine": "minimax-h3"}
        plan = gapp.style_script_plan(ss)
        self.assertEqual(plan["n_scenes"], 3)
        self.assertAlmostEqual(plan["scene_secs_target"], 12.0)
        self.assertAlmostEqual(plan["minutes"], 0.6)

    def test_a_count_with_no_length_still_sets_the_length(self):
        # The legacy redraft path ("retell this in 7 scenes") is unchanged.
        plan = gapp.style_script_plan({"video_engine": "ltx25"}, n_scenes=7)
        self.assertEqual(plan["n_scenes"], 7)
        self.assertAlmostEqual(plan["minutes"], 7 * 12 / 60.0, places=2)


class ActedPlanTests(TempConfigCase):
    """Films made of clips count their scenes by takes, not by words."""

    def setUp(self):
        super().setUp()
        self.write_config({
            "styles": [_style("Acted", video_minutes=2.0, voice="Narrator")],
            "default_style": "Acted",
        })

    def _ss(self, **over):
        return {**gapp.style_settings(gapp.load_config(), "Acted"), **over}

    def test_a_style_count_stretches_the_takes(self):
        body = backend.GenerateScriptBody(video_title="X", topic="y", format="silent")
        n, secs = backend._acted_scene_plan(body, self._ss(video_scenes=10))
        self.assertEqual(n, 10)
        self.assertAlmostEqual(secs, 12.0)   # 2 min ÷ 10 = 12 s, at the ceiling

    def test_the_brief_outranks_the_style_default(self):
        body = backend.GenerateScriptBody(video_title="X", topic="y", minutes=2.0,
                                          n_scenes=16, format="silent")
        n, secs = backend._acted_scene_plan(body, self._ss(video_scenes=10))
        self.assertEqual(n, 16)
        self.assertAlmostEqual(secs, 7.5)

    def test_no_count_leaves_the_take_at_its_contract_length(self):
        body = backend.GenerateScriptBody(video_title="X", topic="y", minutes=2.0,
                                          format="silent")
        self.assertEqual(backend._acted_scene_plan(body, self._ss()),
                         (12, performance.SCENE_SECONDS))

    def test_the_writer_is_given_the_take_it_will_be_shot_at(self):
        note = backend._build_dialogue_note("silent", ["Ana"], acted_silent=True,
                                            scene_secs=12.0)
        self.assertIn('"seconds" of about 12 (never below 5 or above 12)', note)
        # A dialogue scene's budget is words, and it follows the same take.
        short = backend._build_dialogue_note("dialogue", ["Ana"], scene_secs=6.0)
        self.assertIn("the take runs about 6 seconds", short)
        self.assertIn("AT MOST 2 lines and 14 spoken words", short)

    def test_a_narrated_plan_cannot_overfill_an_acted_take(self):
        # A mixed film plans 24 s narrated scenes; its acted takes still get
        # the model's own budget rather than that.
        note = backend._build_dialogue_note("mixed", ["Ana"], scene_secs=24.0)
        self.assertIn("the take runs about 12 seconds", note)

    def test_the_count_reaches_the_story_and_the_brief(self):
        with mock.patch.object(story_mode, "generate_story",
                               return_value={**STORY}) as gen:
            result = backend._do_story_generate(backend.GenerateScriptBody(
                video_title="X", topic="y", style_name="Acted", format="silent",
                minutes=2.0, n_scenes=10))
        plan = gen.call_args.kwargs["scene_plan"]
        self.assertEqual(gen.call_args.args[1], 10)      # n_scenes asked of the draft
        self.assertEqual(plan["n_scenes"], 10)
        self.assertAlmostEqual(plan["scene_secs_target"], 12.0)
        self.assertEqual(result["create_brief"]["n_scenes"], 10)
        self.assertAlmostEqual(result["create_brief"]["minutes"], 2.0)


if __name__ == "__main__":
    unittest.main()
