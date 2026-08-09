"""Length-driven generation: target minutes → cadence plan → scene count,
threaded through script generation, the estimate endpoint, and queue items."""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import webapp.backend.main as backend
from scriptstub import STORY  # noqa: E402
from pipeline import cadence  # noqa: E402
from pipeline.llm import (  # noqa: E402
    Scene,
    condense_long_narrations,
    enforce_scene_word_caps,
    regen_split_scene_visuals,
)
from test_styles import TempConfigCase, _style  # noqa: E402


class LengthPlanCase(TempConfigCase):
    def setUp(self):
        super().setUp()
        # Isolate the cadence store per test (no measured voices → DEFAULT_WPM).
        p = mock.patch.dict(os.environ, {
            "VOICE_CADENCE_FILE": str(self.config_file.parent / "voice_cadence.json")})
        p.start()
        self.addCleanup(p.stop)
        # Isolate the request queue — backend.queue_add writes it for real.
        q = mock.patch.object(backend.yt, "QUEUE_PATH",
                              self.config_file.parent / "youtube_queue.json")
        q.start()
        self.addCleanup(q.stop)
        self.write_config({
            "styles": [_style("Hero", n_scenes=8, voice="",
                              resolution="Landscape FHD (1920×1080)")],
            "default_style": "Hero",
            "characters": [],
            "characters_migrated_v2": True,
        })
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        self.addCleanup(mock.patch.stopall)


class GenerateWithMinutesTests(LengthPlanCase):
    def _generate(self, **body_kwargs):
        captured = {}

        def fake_story(title, n_scenes, *a, **kw):
            captured["n_scenes"] = n_scenes
            captured["scene_plan"] = kw.get("scene_plan")
            return {**STORY, "n_scenes": n_scenes,
                    "scene_plan": kw.get("scene_plan")}

        def fake_divide(story, *a, **kw):
            n = int(story.get("n_scenes") or 1)
            return ([Scene(id=i + 1, title=f"S{i+1}", image_prompt="p",
                           video_prompt="v", narration="n")
                     for i in range(n)], "music", "vis", [])

        body = backend.GenerateScriptBody(video_title="T", topic="topic",
                                          style_name="Hero", **body_kwargs)
        with mock.patch.object(backend.story_mode, "generate_story", side_effect=fake_story), \
             mock.patch.object(backend.story_mode, "divide_story", side_effect=fake_divide), \
             mock.patch.object(backend, "_describe_in_background"):
            res = backend._do_script_generate(body)
        return res, captured

    def test_minutes_drive_scene_count_and_plan(self):
        # 2 min at the 150 wpm default → 10 scenes of ~30 words (25–37 = 10–15 s).
        res, cap = self._generate(minutes=2.0)
        self.assertEqual(cap["n_scenes"], 10)
        plan = cap["scene_plan"]
        self.assertEqual(plan["minutes"], 2.0)
        self.assertEqual(plan["scene_words_target"], 30)
        self.assertEqual(plan["scene_words_max"], 37)
        brief = res["create_brief"]
        self.assertEqual(brief["minutes"], 2.0)
        self.assertEqual(brief["scene_plan"]["n_scenes"], 10)

    def test_explicit_scene_count_still_pins(self):
        res, cap = self._generate(n_scenes=7)
        self.assertEqual(cap["n_scenes"], 7)
        self.assertEqual(cap["scene_plan"]["n_scenes"], 7)

    def test_style_length_used_when_nothing_given(self):
        # Style n_scenes=8 (legacy ~9 s each → 1.2 min) → 6 cadence scenes.
        _, cap = self._generate()
        self.assertEqual(cap["scene_plan"]["minutes"], 1.2)
        self.assertEqual(cap["n_scenes"], cap["scene_plan"]["n_scenes"])

    def test_measured_cadence_scales_word_budget(self):
        cadence.set_measured("", "openf5", 200.0)
        _, cap = self._generate(minutes=1.0)
        plan = cap["scene_plan"]
        self.assertEqual(plan["wpm"], 200.0)
        self.assertEqual(plan["scene_words_target"], 40)  # 200 wpm × 12 s


class LengthEstimateEndpointTests(LengthPlanCase):
    def test_estimate_returns_words_and_scenes(self):
        out = backend.script_length_estimate(style_name="Hero", minutes=2.0, voice="")
        self.assertTrue(out["ok"])
        self.assertEqual(out["n_scenes"], 10)
        self.assertEqual(out["words_total"], 300)
        self.assertEqual(out["scene_secs_max"], 15.0)

    def test_estimate_defaults_to_style_length(self):
        out = backend.script_length_estimate(style_name="Hero", minutes=0.0, voice="")
        self.assertEqual(out["minutes"], 1.2)  # 8 legacy scenes × 9 s


class QueueMinutesTests(LengthPlanCase):
    def test_queue_add_stores_minutes_and_derived_scene_count(self):
        out = backend.queue_add(backend.QueueAddBody(title="Vid", minutes=2.0,
                                                     style_name="Hero"))
        self.assertTrue(out["ok"])
        item = out["queue"][-1]
        self.assertEqual(item["suggested_minutes"], 2.0)
        self.assertEqual(item["suggested_scene_count"], 10)

    def test_queue_add_legacy_scenes_become_minutes(self):
        out = backend.queue_add(backend.QueueAddBody(title="Vid", n_scenes=20,
                                                     style_name="Hero"))
        item = out["queue"][-1]
        self.assertEqual(item["suggested_minutes"], 3.0)  # 20 × 9 s

    def test_queue_item_minutes_precedence(self):
        ss = backend.gapp.style_settings(backend.gapp.load_config(), "Hero")
        self.assertEqual(backend._queue_item_minutes({"suggested_minutes": 2.5}, ss), 2.5)
        self.assertEqual(backend._queue_item_minutes({"suggested_scene_count": 20}, ss), 3.0)
        self.assertEqual(backend._queue_item_minutes({}, ss), 1.2)  # style's 8 × 9 s


class SceneContractTests(unittest.TestCase):
    """Over-budget narrations must be condensed to the cap — keeping the scene
    count and video length as planned — and any scene the splitting backstop
    does create must get its own visuals, never a copy of its source's."""

    def setUp(self):
        self.plan = cadence.plan_script(1.0, 150.0)  # cap = 37 words/scene
        # A ~60-word narration with natural pauses, so the splitter can cut it.
        self.long = ("The cat sat inside the sealed box, neither alive nor dead, "
                     "while the scientists argued about what it meant, and the "
                     "equations said both outcomes were true at once, which felt "
                     "impossible to everyone in the room, yet the mathematics held "
                     "firm, and the debate about measurement spread far beyond "
                     "physics into philosophy itself, changing the question forever.")

    def test_condense_keeps_scene_count_and_length(self):
        scenes = [Scene(id=1, title="T", image_prompt="img", video_prompt="vid",
                        narration=self.long)]
        short = "The cat sat in the sealed box, both alive and dead at once."

        def call(system, user, max_tokens, label, retries=2):
            self.assertIn("37", user)  # the cap reaches the prompt
            return short

        condense_long_narrations(call, scenes, self.plan)
        self.assertEqual(scenes[0].narration, short)
        out = enforce_scene_word_caps(scenes, self.plan)
        self.assertEqual(len(out), 1)  # no split, no duplicate visuals

    def test_condense_rejects_a_longer_rewrite(self):
        scenes = [Scene(id=1, title="T", image_prompt="img", video_prompt="vid",
                        narration=self.long)]
        condense_long_narrations(
            lambda *a, **k: self.long + " And even more words follow here now.",
            scenes, self.plan)
        self.assertEqual(scenes[0].narration, self.long)

    def test_split_pieces_get_their_own_visuals(self):
        scenes = [Scene(id=1, title="T", image_prompt="img", video_prompt="vid",
                        narration=self.long)]
        out = enforce_scene_word_caps(scenes, self.plan)
        self.assertGreater(len(out), 1)
        # Stand-in visuals inherited, continuation pieces marked for regen.
        self.assertTrue(all(s.image_prompt == "img" for s in out))
        self.assertFalse(getattr(out[0], "_split_clone", False))
        self.assertTrue(all(getattr(s, "_split_clone", False) for s in out[1:]))

        def call(system, user, max_tokens, label, retries=2):
            return "IMAGE: a fresh still for this moment\nVIDEO: a fresh motion for this moment"

        regen_split_scene_visuals(call, out, "Title", "style")
        self.assertEqual(out[0].image_prompt, "img")  # first piece keeps the source's
        for s in out[1:]:
            self.assertEqual(s.image_prompt, "a fresh still for this moment")
            self.assertEqual(s.video_prompt, "a fresh motion for this moment")

    def test_regen_failure_keeps_standin_visuals(self):
        scenes = [Scene(id=1, title="T", image_prompt="img", video_prompt="vid",
                        narration=self.long)]
        out = enforce_scene_word_caps(scenes, self.plan)

        def boom(*a, **k):
            raise RuntimeError("llm down")

        regen_split_scene_visuals(boom, out, "Title", "style")
        self.assertTrue(all(s.image_prompt == "img" for s in out))


if __name__ == "__main__":
    unittest.main()
