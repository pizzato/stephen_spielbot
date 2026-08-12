"""Chained H3 scenes: the h3_chain_scenes toggle, end to end.

The contract the feature exists for: with chaining on, the SAME runtime is
planned as fewer, longer scenes carrying proportionally more narration each —
and the render splits that scene back into clips the model can actually make.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import cadence, comfyui  # noqa: E402


class TestSceneWindow(unittest.TestCase):
    def test_unchained_is_the_existing_contract(self):
        self.assertEqual(cadence.scene_window(False),
                         (cadence.SCENE_MIN_SECS, cadence.SCENE_TARGET_SECS,
                          cadence.SCENE_MAX_SECS))

    def test_chained_window_pays_for_each_join(self):
        lo, mid, hi = cadence.scene_window(True)
        lost = cadence.CHAIN_JOIN_SECS * (cadence.CHAIN_CLIPS - 1)
        self.assertAlmostEqual(hi, cadence.SCENE_MAX_SECS * cadence.CHAIN_CLIPS - lost)
        # A chained scene is longer than an unchained one but short of a clean
        # multiple — the pinned frames are the difference.
        self.assertGreater(mid, cadence.SCENE_TARGET_SECS)
        self.assertLess(mid, cadence.SCENE_TARGET_SECS * cadence.CHAIN_CLIPS)
        self.assertLess(lo, mid)


class TestPlanning(unittest.TestCase):
    """The user-visible promise: fewer scenes, more words in each."""

    def setUp(self):
        self.plain = cadence.plan_script(4.0, 150.0)
        self.chained = cadence.plan_script(4.0, 150.0, chained=True)

    def test_fewer_scenes(self):
        self.assertLess(self.chained["n_scenes"], self.plain["n_scenes"])

    def test_more_narration_per_scene(self):
        self.assertGreater(self.chained["scene_words_target"],
                           self.plain["scene_words_target"])
        self.assertGreater(self.chained["scene_words_max"],
                           self.plain["scene_words_max"])

    def test_total_narration_is_preserved(self):
        # Same runtime should buy the same amount of script, give or take the
        # seconds spent on joins.
        self.assertAlmostEqual(self.chained["words_total"],
                               self.plain["words_total"], delta=0.1 * self.plain["words_total"])

    def test_prompts_are_told_the_longer_window(self):
        v = cadence.prompt_vars(self.chained)
        self.assertGreater(v["scene_secs_max"], int(cadence.SCENE_MAX_SECS))

    def test_plan_records_the_mode(self):
        self.assertTrue(self.chained["chained"])
        self.assertFalse(self.plain["chained"])

    def test_explicit_scene_count_still_honoured(self):
        plan = cadence.plan_for_scenes(7, 150.0, chained=True)
        self.assertEqual(plan["n_scenes"], 7)
        self.assertGreater(plan["minutes"], 7 * cadence.SCENE_TARGET_SECS / 60.0)


class TestChainSplit(unittest.TestCase):
    def test_single_clip_when_chaining_off(self):
        cadence_clips = cadence.CHAIN_CLIPS
        self.assertGreater(cadence_clips, 1)  # guard: this test assumes >1
        self.assertEqual(len(comfyui.h3_chain_split(24.0)), cadence_clips)

    def test_later_clips_sample_longer_than_they_deliver(self):
        parts = comfyui.h3_chain_split(24.0)
        self.assertAlmostEqual(parts[1] - parts[0], cadence.CHAIN_JOIN_SECS, places=6)

    def test_delivered_length_matches_the_request(self):
        want = 24.0
        parts = comfyui.h3_chain_split(want)
        frames = [comfyui.h3_frame_count(p) for p in parts]
        trimmed = sum(frames) - comfyui.H3_CHAIN_CONTEXT_FRAMES * (len(parts) - 1)
        delivered = trimmed / comfyui.H3_FPS
        # Within one frame-quantisation step (h3_frame_count rounds to n%17==5).
        self.assertAlmostEqual(delivered, want, delta=1.0)

    def test_chained_scene_beats_the_single_clip_ceiling(self):
        parts = comfyui.h3_chain_split(cadence.scene_window(True)[2])
        frames = [comfyui.h3_frame_count(p) for p in parts]
        delivered = (sum(frames) - comfyui.H3_CHAIN_CONTEXT_FRAMES
                     * (len(parts) - 1)) / comfyui.H3_FPS
        self.assertGreater(delivered, comfyui.H3_MAX_SECONDS)


if __name__ == "__main__":
    unittest.main()


class TestActedChaining(unittest.TestCase):
    """h3_chain_scenes covers acted scenes too — reference engines are always
    MiniMax, so the toggle alone decides (no LTX carve-out)."""

    def _long_meta(self):
        w = "word " * 14
        return {"title": "Test", "lines": [
            {"speaker": "Kinho", "text": w}, {"speaker": "Joe", "text": w},
            {"speaker": "Kinho", "text": w}]}

    def test_acted_limits_stretch_like_narrated(self):
        from pipeline import performance as p
        lo, hi = p.acted_limits(True)
        lost = cadence.CHAIN_JOIN_SECS * (cadence.CHAIN_CLIPS - 1)
        self.assertAlmostEqual(lo, p.MAX_SCENE_SECONDS * cadence.CHAIN_CLIPS - lost)
        self.assertAlmostEqual(hi, p.H3_CEILING_SECONDS * cadence.CHAIN_CLIPS - lost)

    def test_long_dialogue_stays_one_scene_when_chained(self):
        from pipeline import performance as p
        meta = self._long_meta()
        self.assertGreater(len(p.split_overloaded(meta)), 1)
        self.assertEqual(len(p.split_overloaded(meta, chained=True)), 1)

    def test_chain_halves_carry_all_lines_in_order(self):
        from pipeline import performance as p
        meta = self._long_meta()
        halves = p.split_lines_for_chain(meta)
        self.assertEqual(len(halves), 2)
        from pipeline.performance import norm_lines
        rejoined = [l["text"] for h in halves for l in h["lines"]]
        self.assertEqual(rejoined, [l["text"] for l in norm_lines(meta["lines"])])
        # continuation clips must not restate the opening action beat
        self.assertEqual(halves[1].get("beats"), [])

    def test_render_seconds_uses_chained_ceiling(self):
        from pipeline import performance as p
        meta = self._long_meta()
        self.assertEqual(p.render_seconds(meta), p.H3_CEILING_SECONDS)
        self.assertGreater(p.render_seconds(meta, chained=True), p.H3_CEILING_SECONDS)

    def test_plan_carries_the_acted_flag_independently(self):
        # An LTX narrated style with the toggle on: narration stays unchained,
        # acted scenes chain.
        import app as gapp
        ss = {"h3_chain_scenes": True, "video_engine": "ltx25",
              "video_minutes": 4.0}
        plan = gapp.style_script_plan(ss)
        self.assertFalse(plan["chained"])
        self.assertTrue(plan["chained_acted"])
