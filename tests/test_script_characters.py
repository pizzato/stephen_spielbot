"""Per-script character identification at script-generation time.

The LLM identifies 0-2 recurring MAIN characters for a video and returns them
alongside the scenes; freshly-identified characters must be carried into the
later batches / visual stage so the same figure stays consistent across scenes.
Covers both backends (Claude JSON + local plain-text)."""
import json
import unittest
from unittest import mock

from pipeline import llm


class NormalizeIdentifiedTests(unittest.TestCase):
    def test_drops_nameless_and_caps_at_two(self):
        rows = [
            {"name": "Caesar", "description": "a Roman general", "aliases": ["Julius"]},
            {"name": "", "description": "ignored"},
            {"description": "no name either"},
            {"name": "Brutus", "description": "a senator"},
            {"name": "Third", "description": "over the cap"},
        ]
        out = llm._norm_identified_characters(rows)
        self.assertEqual([c["name"] for c in out], ["Caesar", "Brutus"])
        self.assertEqual(out[0]["aliases"], ["Julius"])

    def test_comma_string_aliases_coerced_to_list(self):
        out = llm._norm_identified_characters([{"name": "Caesar", "aliases": "Julius, Imperator"}])
        self.assertEqual(out[0]["aliases"], ["Julius", "Imperator"])

    def test_empty_input_is_empty(self):
        self.assertEqual(llm._norm_identified_characters(None), [])
        self.assertEqual(llm._norm_identified_characters("nope"), [])


class ClaudeIdentifyTests(unittest.TestCase):
    """The initial Claude batch returns a `characters` array; those characters
    must be returned to the caller AND injected into continuation batches."""

    def _run(self, n_scenes):
        captured = []
        batch = {"n": 0}

        def fake_call(client, model, system, user_msg, *a, **k):
            captured.append(user_msg)
            batch["n"] += 1
            if batch["n"] == 1:
                return json.dumps({
                    "style": "S", "music": "M",
                    "characters": [{"name": "Julius Caesar",
                                    "aliases": ["Caesar"],
                                    "description": "a lean Roman general in a red-bordered toga"}],
                    "scenes": [{"id": i, "title": f"T{i}", "image_prompt": "I",
                                "video_prompt": "V", "narration": "N"}
                               for i in range(1, min(10, n_scenes) + 1)],
                })
            # continuation batch → a plain list of scenes
            return json.dumps([{"id": 11, "title": "T11", "image_prompt": "I",
                                "video_prompt": "V", "narration": "N"}])

        with mock.patch.object(llm, "_claude_call", side_effect=fake_call):
            scenes, music, style, chars = llm._claude_generate(
                "Julius Caesar", n_scenes, None, "dummy-key", "claude-x")
        return scenes, chars, captured

    def test_characters_returned(self):
        _, chars, _ = self._run(1)
        self.assertEqual(len(chars), 1)
        self.assertEqual(chars[0]["name"], "Julius Caesar")
        self.assertIn("red-bordered toga", chars[0]["description"])

    def test_identified_character_carried_into_continuation(self):
        # 11 scenes → one continuation batch. The character established in batch 1
        # must appear in the batch-2 prompt so later scenes stay consistent.
        _, _, captured = self._run(11)
        self.assertGreaterEqual(len(captured), 2)
        self.assertIn("Julius Caesar", captured[1])

    def test_no_characters_key_yields_empty(self):
        def fake_call(client, model, system, user_msg, *a, **k):
            return json.dumps({"style": "S", "music": "M",
                               "scenes": [{"id": 1, "title": "T", "image_prompt": "I",
                                           "video_prompt": "V", "narration": "N"}]})
        with mock.patch.object(llm, "_claude_call", side_effect=fake_call):
            _, _, _, chars = llm._claude_generate("Topic", 1, None, "k", "m")
        self.assertEqual(chars, [])


class LocalIdentifyTests(unittest.TestCase):
    """The local story stage lists CHARACTER_i_* lines; they must be parsed and
    the resulting sheet must reach the per-scene visual call."""

    def test_story_parses_character_block(self):
        raw = (
            "STYLE: cinematic\n"
            "MUSIC: tense strings\n"
            "CHARACTER_1_NAME: Julius Caesar\n"
            "CHARACTER_1_ALIASES: Caesar, Imperator\n"
            "CHARACTER_1_DESC: a lean Roman general in a red-bordered toga\n"
            "TITLE_1: Opening\n"
            "NARRATION_1: Caesar crosses the Rubicon. History turns.\n"
        )
        with mock.patch.object(llm, "_local_llm", return_value=raw):
            story = llm._local_generate_story("Caesar", 1, None, "u", "m")
        self.assertEqual(len(story["characters"]), 1)
        c = story["characters"][0]
        self.assertEqual(c["name"], "Julius Caesar")
        self.assertEqual(c["aliases"], ["Caesar", "Imperator"])
        self.assertIn("toga", c["description"])

    def test_identified_sheet_reaches_visual_stage(self):
        story_raw = (
            "STYLE: cinematic\nMUSIC: tense\n"
            "CHARACTER_1_NAME: Julius Caesar\n"
            "CHARACTER_1_DESC: a lean Roman general in a red-bordered toga\n"
            "TITLE_1: Opening\nNARRATION_1: Caesar acts. Rome watches.\n"
        )
        visual_prompts = []

        def fake_llm(messages, *a, **k):
            content = messages[-1]["content"]
            if "IMAGE and VIDEO" in content or "Generate the IMAGE" in content:
                visual_prompts.append(content)
                return "IMAGE: a still of Caesar\nVIDEO: he turns"
            return story_raw

        with mock.patch.object(llm, "_check_local_available", return_value=True), \
             mock.patch.object(llm, "_local_llm", side_effect=fake_llm):
            scenes, music, style, chars = llm._local_generate("Caesar", 1, None)

        self.assertEqual(len(chars), 1)
        self.assertTrue(visual_prompts, "visual stage was never called")
        self.assertIn("Julius Caesar", visual_prompts[0])


if __name__ == "__main__":
    unittest.main()
