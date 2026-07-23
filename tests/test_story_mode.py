"""Story-first script mode (pipeline/story.py).

All LLM traffic goes through pipeline.story._chat_complete, so a fake keyed on
the call label exercises the whole outline → chapters → critique → revise →
divide pipeline with zero API spend — including a 200-scene case that proves
the chapter batching and positional-id math at scale.
"""
import json
import math
import os
import re
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

from pipeline import story
from pipeline.llm import Scene


def _outline_json(n_chapters, n_scenes, style="Test cinematic style", characters=None):
    per = math.ceil(n_scenes / n_chapters)
    counts = [per] * n_chapters  # deliberately may over-sum: pro-rating must fix it
    return json.dumps({
        "style": style,
        "music": "test music, slow strings",
        "characters": characters or [],
        "chapters": [{"title": f"Chapter {i + 1}", "summary": f"Summary {i + 1}.",
                      "scenes": counts[i]} for i in range(n_chapters)],
    })


def _scene_items(start, end):
    return json.dumps([
        {"id": i, "title": f"Scene {i} title", "image_prompt": f"image {i}",
         "video_prompt": f"video {i}", "narration": f"Narration {i}a. Narration {i}b."}
        for i in range(start, end + 1)
    ])


def make_fake(critique=None, divide_fail_over=None, divide_short_chunk=None):
    """A _chat_complete stand-in keyed on the label argument.

    critique: JSON-able dict returned by the critique call (default: pass), or
      an Exception instance to raise.
    divide_fail_over: chunk width above which divide calls return garbage
      (exercises halve-and-retry).
    divide_short_chunk: (start, end) chunk that under-delivers by one scene
      (exercises stub padding + narration fill).
    """
    calls = []

    def fake(cfg, system, user_msg, max_tokens, label, retries=3):
        calls.append(label)
        if label == "story outline":
            return fake.outline_json
        m = re.match(r"story chapter (\d+)$", label)
        if m:
            return f"Prose for chapter {m.group(1)}. " * 5
        if label == "story critique":
            if isinstance(critique, Exception):
                raise critique
            return json.dumps(critique or {"verdict": "pass", "notes": [], "chapters": []})
        m = re.match(r"story revise chapter (\d+)$", label)
        if m:
            return f"REVISED prose for chapter {m.group(1)}. " * 5
        m = re.match(r"divide scenes (\d+)–(\d+)$", label)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if divide_fail_over and (end - start + 1) > divide_fail_over:
                return "definitely not json"
            if divide_short_chunk == (start, end):
                return _scene_items(start, end - 1)  # one scene short
            return _scene_items(start, end)
        m = re.match(r"fill narration scene (\d+)$", label)
        if m:
            return f"Filled {m.group(1)}a. Filled {m.group(1)}b."
        if label == "recurring characters":
            return "[]"
        raise AssertionError(f"unexpected LLM label: {label}")

    fake.calls = calls
    fake.outline_json = None
    return fake


class ProRateBudgetTests(unittest.TestCase):
    def test_sums_exactly_and_keeps_minimum_one(self):
        self.assertEqual(sum(story._pro_rate_budgets([10, 10], 12)), 12)
        self.assertEqual(story._pro_rate_budgets([5, 5, 5], 15), [5, 5, 5])
        self.assertTrue(all(b >= 1 for b in story._pro_rate_budgets([1, 50], 6)))
        self.assertEqual(sum(story._pro_rate_budgets([1, 50], 6)), 6)

    def test_garbage_counts_and_more_chapters_than_scenes(self):
        self.assertEqual(sum(story._pro_rate_budgets(["x", None, 3], 7)), 7)
        # 5 chapters but only 3 scenes → truncated to 3 chapters of 1
        self.assertEqual(story._pro_rate_budgets([2, 2, 2, 2, 2], 3), [1, 1, 1])
        self.assertEqual(story._pro_rate_budgets([], 4), [4])


class GenerateStoryTests(unittest.TestCase):
    def _generate(self, n_scenes, fake, **kw):
        fake.outline_json = fake.outline_json or _outline_json(
            max(1, math.ceil(n_scenes / story._SCENES_PER_CHAPTER)), n_scenes)
        with mock.patch.object(story, "_chat_complete", fake):
            return story.generate_story("Test topic", n_scenes, **kw)

    def test_basic_story_shape(self):
        fake = make_fake()
        s = self._generate(12, fake, video_title="Video title")
        self.assertEqual(s["status"], "draft")
        self.assertEqual(s["n_scenes"], 12)
        self.assertEqual(len(s["chapters"]), 2)
        self.assertEqual(sum(c["scenes"] for c in s["chapters"]), 12)
        self.assertTrue(all(c["text"].strip() for c in s["chapters"]))
        self.assertEqual(s["critique"]["verdict"], "pass")
        self.assertEqual(s["style"], "Test cinematic style")
        self.assertEqual(s["video_title"], "Video title")
        # no revision calls on a pass verdict
        self.assertFalse([c for c in fake.calls if c.startswith("story revise")])

    def test_style_hint_overrides_llm_style(self):
        s = self._generate(6, make_fake(), style_hint="My fixed style")
        self.assertEqual(s["style"], "My fixed style")

    def test_revise_rewrites_only_flagged_chapters(self):
        fake = make_fake(critique={
            "verdict": "revise", "notes": ["repetition in ch2"],
            "chapters": [{"chapter": 2, "issues": "repeats chapter 1"}]})
        s = self._generate(20, fake)
        self.assertEqual(s["critique"]["verdict"], "revise")
        self.assertIn("REVISED", s["chapters"][1]["text"])
        self.assertNotIn("REVISED", s["chapters"][0]["text"])
        self.assertEqual([c for c in fake.calls if c.startswith("story revise")],
                         ["story revise chapter 2"])

    def test_critique_failure_degrades_to_skipped(self):
        fake = make_fake(critique=RuntimeError("context too long"))
        s = self._generate(12, fake)
        self.assertEqual(s["critique"]["verdict"], "skipped")
        self.assertTrue(all(c["text"].strip() for c in s["chapters"]))


class DivideStoryTests(unittest.TestCase):
    def _story(self, n_scenes):
        n_chapters = max(1, math.ceil(n_scenes / story._SCENES_PER_CHAPTER))
        budgets = story._pro_rate_budgets([story._SCENES_PER_CHAPTER] * n_chapters, n_scenes)
        return {
            "version": 1, "topic": "Test topic", "video_title": "", "n_scenes": n_scenes,
            "outline": [], "critique": {"verdict": "pass", "notes": [], "chapters": []},
            "chapters": [{"chapter": i + 1, "title": f"Chapter {i + 1}",
                          "summary": "", "scenes": budgets[i],
                          "text": f"Chapter {i + 1} sentence one. Sentence two. "
                                  f"Sentence three. Sentence four."}
                         for i in range(n_chapters)],
            "style": "Story style", "music": "Story music", "characters": [],
            "status": "draft",
        }

    def _divide(self, n_scenes, fake, **kw):
        with mock.patch.object(story, "_chat_complete", fake):
            return story.divide_story(self._story(n_scenes), **kw)

    def test_contract_matches_generate_script(self):
        scenes, music, style_, chars = self._divide(12, make_fake())
        self.assertEqual(len(scenes), 12)
        self.assertEqual([s.id for s in scenes], list(range(1, 13)))
        self.assertTrue(all(isinstance(s, Scene) for s in scenes))
        self.assertTrue(all(s.mode == "narration" for s in scenes))
        self.assertTrue(all(s.narration.strip() for s in scenes))
        self.assertEqual(music, "Story music")
        self.assertEqual(style_, "Story style")
        self.assertEqual(chars, [])

    def test_style_hint_override(self):
        _, _, style_, _ = self._divide(6, make_fake(), style_hint="Divide style")
        self.assertEqual(style_, "Divide style")

    def test_malformed_json_halves_and_retries(self):
        # chunks wider than 5 scenes return garbage → each 10-scene chapter
        # must split into halves and still deliver every scene in order
        fake = make_fake(divide_fail_over=5)
        scenes, *_ = self._divide(20, fake)
        self.assertEqual([s.id for s in scenes], list(range(1, 21)))
        self.assertTrue(all(s.narration.strip() for s in scenes))

    def test_under_delivery_pads_stub_and_fill_pass_completes(self):
        fake = make_fake(divide_short_chunk=(1, 10))
        scenes, *_ = self._divide(20, fake)
        self.assertEqual([s.id for s in scenes], list(range(1, 21)))
        # scene 10 was stubbed, then completed by the fill pass
        self.assertIn("Filled 10", scenes[9].narration)

    def test_scale_200_scenes(self):
        fake = make_fake()
        fake.outline_json = _outline_json(20, 200)
        with mock.patch.object(story, "_chat_complete", fake):
            s = story.generate_story("Big topic", 200)
            self.assertEqual(len(s["chapters"]), 20)
            self.assertEqual(sum(c["scenes"] for c in s["chapters"]), 200)
            scenes, *_ = story.divide_story(s)
        self.assertEqual([sc.id for sc in scenes], list(range(1, 201)))
        self.assertTrue(all(sc.narration.strip() for sc in scenes))
        # exactly one divide call per chapter, none failed
        divides = [c for c in fake.calls if c.startswith("divide scenes")]
        self.assertEqual(len(divides), 20)


if __name__ == "__main__":
    unittest.main()
