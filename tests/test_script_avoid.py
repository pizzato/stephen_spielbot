"""Per-style script "avoid" instruction.

Each style carries a ``script_avoid`` field — free-text guidance telling the
script writer what to keep OUT of the generated script (topics, words, tropes).
These tests cover that the guidance actually reaches the rendered LLM prompt on
BOTH backends (Claude single-call, local two-stage story) and that a blank field
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

AVOID = "no politics, avoid the word journey, don't be preachy"


class ClaudeBackendTests(unittest.TestCase):
    """The Claude backend writes narration + prompts in the initial (and any
    continuation) batch, so the avoid directive must ride along in the user
    message of those calls."""

    def _run(self, avoid_hint):
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
                                 avoid_hint=avoid_hint)
        return captured

    def test_avoid_reaches_the_prompt(self):
        captured = self._run(AVOID)
        self.assertTrue(any(AVOID in u for u in captured),
                        f"avoid directive missing from prompt(s): {captured!r}")

    def test_blank_avoid_injects_nothing(self):
        for blank in (None, "", "   "):
            captured = self._run(blank)
            self.assertFalse(any("AVOID —" in u for u in captured),
                             f"unexpected avoid note for {blank!r}")


class LocalBackendTests(unittest.TestCase):
    """The local backend writes the narrated story in stage 1
    (_local_generate_story), so the avoid directive belongs in that call."""

    def _run(self, avoid_hint):
        captured = []

        def fake_llm(messages, *a, **k):
            captured.append(messages[-1]["content"])
            return "STYLE: S\nMUSIC: M\nTITLE_1: T\nNARRATION_1: One sentence. Two sentence."

        with mock.patch.object(llm, "_local_llm", side_effect=fake_llm):
            llm._local_generate_story("Topic", 1, None, "u", "m",
                                      avoid_hint=avoid_hint)
        return captured[0]

    def test_avoid_reaches_the_prompt(self):
        self.assertIn(AVOID, self._run(AVOID))

    def test_blank_avoid_injects_nothing(self):
        for blank in (None, "", "   "):
            self.assertNotIn("AVOID —", self._run(blank))


class TemplateTests(unittest.TestCase):
    """Every script-writing template must expose the placeholder so the note
    substitutes (a missing ${avoid_note} would silently drop it)."""

    def test_continuation_template_substitutes_the_note(self):
        rendered = _prompts.user("script_claude_continuation", n_scenes=2,
                                 topic_ref='"X"', batch_start=2, batch_end=2,
                                 ctx_str="ctx", avoid_note="\nAVOID-HERE",
                                 conclusion_note="")
        self.assertIn("AVOID-HERE", rendered)


if __name__ == "__main__":
    unittest.main()
