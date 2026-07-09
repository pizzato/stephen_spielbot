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


def _scene(i, narration):
    return llm.Scene(id=i, title=f"T{i}", image_prompt="I", video_prompt="V",
                     narration=narration)


class RecurringCharacterPassTests(unittest.TestCase):
    """The second pass over the FULL script catches recurring characters the
    first-batch identify (scenes 1-10, 1-2 central subjects) missed."""

    def test_norm_respects_custom_cap(self):
        rows = [{"name": f"C{i}", "description": "d"} for i in range(6)]
        self.assertEqual(len(llm._norm_identified_characters(rows)), 2)          # default cap
        self.assertEqual(len(llm._norm_identified_characters(rows, cap=5)), 5)   # widened

    def test_merge_dedups_by_name_and_alias_batch1_wins(self):
        identified = [{"name": "George Washington", "aliases": ["Washington"],
                       "description": "batch-1 look"}]
        found = [
            {"name": "Washington", "aliases": [], "description": "duplicate — dropped"},
            {"name": "King George", "aliases": [], "description": "a stout king"},
        ]
        merged = llm._merge_recurring(identified, found)
        self.assertEqual([c["name"] for c in merged], ["George Washington", "King George"])
        self.assertEqual(merged[0]["description"], "batch-1 look")  # batch-1 not overwritten

    def test_detect_drops_single_scene_keeps_recurring(self):
        scenes = [_scene(1, "Washington takes command."),
                  _scene(2, "Redcoats march."),
                  _scene(3, "Washington crosses the river.")]

        def fake_call(system, user_msg, max_tokens, label, retries=2):
            self.assertIn("Scene 3:", user_msg)
            return json.dumps([
                {"name": "Washington", "aliases": ["the General"],
                 "description": "tall man, powdered wig", "scenes": [1, 3]},
                {"name": "Cornwallis", "description": "an officer", "scenes": [2]},  # 1 scene
            ])

        out = llm._detect_recurring_characters(fake_call, scenes, [])
        self.assertEqual([c["name"] for c in out], ["Washington"])

    def test_detect_early_returns_under_two_scenes(self):
        called = {"n": 0}

        def fake_call(*a, **k):
            called["n"] += 1
            return "[]"

        out = llm._detect_recurring_characters(fake_call, [_scene(1, "Solo.")], [])
        self.assertEqual(out, [])
        self.assertEqual(called["n"], 0, "must not call the LLM for a single scene")

    def test_detect_survives_bad_json(self):
        scenes = [_scene(1, "A."), _scene(2, "B.")]
        identified = [{"name": "Kept", "aliases": [], "description": "unchanged"}]
        out = llm._detect_recurring_characters(
            lambda *a, **k: "not json at all", scenes, identified)
        self.assertEqual(out, identified)  # best-effort: batch-1 list preserved

    def test_claude_generate_picks_up_recurring_character_batch1_missed(self):
        # Batch-1 returns NO characters (the miss the user reported); the recurring
        # pass over all 11 scenes surfaces Washington.
        batch = {"n": 0}

        def fake_call(client, model, system, user_msg, *a, **k):
            batch["n"] += 1
            if batch["n"] == 1:
                return json.dumps({
                    "style": "S", "music": "M", "characters": [],
                    "scenes": [{"id": i, "title": f"T{i}", "image_prompt": "I",
                                "video_prompt": "V", "narration": f"Washington acts in scene {i}."}
                               for i in range(1, 11)],
                })
            # The recurring pass is the only call whose prompt lists every scene,
            # so scene 1 appears there (continuation context shows only the last 3).
            if "Scene 1:" in (user_msg or ""):
                return json.dumps([{"name": "George Washington", "aliases": ["Washington"],
                                    "description": "tall man, powdered wig, blue coat",
                                    "scenes": [1, 5, 11]}])
            return json.dumps([{"id": 11, "title": "T11", "image_prompt": "I",
                                "video_prompt": "V", "narration": "Washington wins."}])

        with mock.patch.object(llm, "_claude_call", side_effect=fake_call):
            _, _, _, chars = llm._claude_generate("Revolution", 11, None, "k", "m")
        self.assertEqual([c["name"] for c in chars], ["George Washington"])


if __name__ == "__main__":
    unittest.main()
