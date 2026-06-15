"""Per-style video / motion style.

Each style carries a ``video_style`` field — free-text motion/cinematography
guidance that steers every scene's ``video_prompt`` (camera + subject movement).
These tests cover that the guidance actually reaches the rendered LLM prompt on
BOTH backends (Claude single-call, local two-stage), and that a blank field
injects nothing.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

from pipeline import llm
from pipeline import prompts as _prompts

MOTION = "Favour dynamic action and visible movement over static shots and slow pans."


class ClaudeBackendTests(unittest.TestCase):
    """The Claude backend generates video_prompts in the initial (and any
    continuation) batch, so the motion direction must ride along in the user
    message of those calls."""

    def _run(self, video_style_hint):
        captured = []

        def fake_call(client, model, system, user_msg, *a, **k):
            captured.append(user_msg)
            return json.dumps({
                "style": "S", "music": "M",
                "scenes": [{"id": 1, "title": "T", "image_prompt": "I",
                            "video_prompt": "V", "narration": "N"}],
            })

        with mock.patch.object(llm, "_claude_call", side_effect=fake_call):
            llm._claude_generate("Topic", 1, None, "dummy-key", "claude-x",
                                 video_style_hint=video_style_hint)
        return captured

    def test_motion_direction_reaches_the_prompt(self):
        captured = self._run(MOTION)
        self.assertTrue(any(MOTION in u for u in captured),
                        f"motion direction missing from prompt(s): {captured!r}")

    def test_blank_video_style_injects_nothing(self):
        for blank in (None, "", "   "):
            captured = self._run(blank)
            self.assertFalse(any("MOTION DIRECTION" in u for u in captured),
                             f"unexpected motion note for {blank!r}")


class LocalBackendTests(unittest.TestCase):
    """The local backend writes the VIDEO line in stage 2
    (_local_generate_visual), so the motion direction belongs in that call."""

    def _run(self, video_style_hint):
        captured = []

        def fake_llm(messages, *a, **k):
            captured.append(messages[-1]["content"])
            return "IMAGE: a frozen still\nVIDEO: it moves"

        with mock.patch.object(llm, "_local_llm", side_effect=fake_llm):
            llm._local_generate_visual("Topic", "S", 1, "Scene", "Narr",
                                        url="u", model="m",
                                        video_style_hint=video_style_hint)
        return captured[0]

    def test_motion_direction_reaches_the_prompt(self):
        self.assertIn(MOTION, self._run(MOTION))

    def test_blank_video_style_injects_nothing(self):
        for blank in (None, "", "   "):
            self.assertNotIn("MOTION DIRECTION", self._run(blank))


class TemplateTests(unittest.TestCase):
    """Every video_prompt-producing template must expose the placeholder so the
    note substitutes (a missing ${video_style_note} would silently drop it)."""

    def test_continuation_template_substitutes_the_note(self):
        rendered = _prompts.user("script_claude_continuation", n_scenes=2,
                                 topic_ref='"X"', batch_start=2, batch_end=2,
                                 ctx_str="ctx", video_style_note="\nNOTE-HERE",
                                 conclusion_note="")
        self.assertIn("NOTE-HERE", rendered)


if __name__ == "__main__":
    unittest.main()
