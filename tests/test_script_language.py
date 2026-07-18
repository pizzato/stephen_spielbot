"""Per-style narration language reaches the script-generation prompts (issue #176 part 2).

The style's tts_language already picks the spoken TTS language; these tests
cover the LLM side: narration and dialogue lines are requested in that
language while prompts/titles/style/music stay in English.
"""
import json
import unittest
from unittest import mock

from pipeline import llm


def _scene_json(n):
    return {
        "style": "cinematic",
        "music": "orchestral",
        "characters": [],
        "scenes": [
            {"id": i + 1, "title": f"Scene {i + 1}", "image_prompt": "img",
             "video_prompt": "vid", "narration": "Uma frase. Outra frase."}
            for i in range(n)
        ],
    }


class ScriptLanguageTests(unittest.TestCase):
    def test_language_name_lookup(self):
        self.assertEqual(llm.narration_language_name("pt"), "Portuguese")
        self.assertEqual(llm.narration_language_name("de"), "German")
        self.assertIsNone(llm.narration_language_name("en"))
        self.assertIsNone(llm.narration_language_name(None))
        self.assertIsNone(llm.narration_language_name("bogus"))

    def test_cloud_prompt_carries_language_note(self):
        captured = []

        def call_fn(system, user_msg, max_tokens, label, retries=2):
            captured.append(user_msg)
            return json.dumps(_scene_json(3)) if len(captured) == 1 else "[]"

        scenes, _, _, _ = llm._json_script_generate(
            "Topic", 3, None, call_fn, language="pt")
        self.assertIn("Portuguese", captured[0])
        self.assertIn("NARRATION LANGUAGE", captured[0])
        self.assertEqual(len(scenes), 3)

    def test_cloud_prompt_english_has_no_note(self):
        captured = []

        def call_fn(system, user_msg, max_tokens, label, retries=2):
            captured.append(user_msg)
            return json.dumps(_scene_json(2)) if len(captured) == 1 else "[]"

        llm._json_script_generate("Topic", 2, None, call_fn, language="en")
        self.assertNotIn("NARRATION LANGUAGE", captured[0])

    def test_cloud_continuation_carries_language_note(self):
        captured = []

        def call_fn(system, user_msg, max_tokens, label, retries=2):
            captured.append(user_msg)
            if len(captured) == 1:
                return json.dumps(_scene_json(llm._CLAUDE_BATCH_SIZE))
            return json.dumps([
                {"id": llm._CLAUDE_BATCH_SIZE + 1, "title": "t", "image_prompt": "i",
                 "video_prompt": "v", "narration": "Mais uma frase. E outra."}
            ])

        llm._json_script_generate(
            "Topic", llm._CLAUDE_BATCH_SIZE + 1, None, call_fn, language="pt")
        self.assertGreaterEqual(len(captured), 2)
        self.assertIn("Portuguese", captured[1])

    def test_fill_empty_narrations_carries_language(self):
        captured = []

        def call_fn(system, user_msg, max_tokens, label, retries=2):
            captured.append(user_msg)
            return "Uma frase. Outra frase."

        scenes = [llm.Scene(id=1, title="T", image_prompt="i", video_prompt="v", narration="")]
        llm._fill_empty_narrations(call_fn, scenes, "Topic", None, language="pt")
        self.assertIn("Portuguese", captured[0])
        self.assertEqual(scenes[0].narration, "Uma frase. Outra frase.")

    def test_local_story_prompt_carries_language_note(self):
        captured = {}

        def fake_local_llm(messages, max_tokens, url, model, retries=3):
            captured["user"] = messages[1]["content"]
            return ("STYLE: cinematic\nMUSIC: calm\n"
                    "TITLE_1: A scene\nNARRATION_1: Uma frase. Outra frase.")

        with mock.patch.object(llm, "_local_llm", side_effect=fake_local_llm):
            llm._local_generate_story("Topic", 1, None, "http://x", "m", language="pt")
        self.assertIn("Portuguese", captured["user"])
        self.assertIn("NARRATION LANGUAGE", captured["user"])

    def test_generate_script_threads_language_to_backend(self):
        fake = (["scene"], "music", "style", [])
        with mock.patch.object(llm, "_load_cfg", return_value={
            "llm_backend": "claude", "claude_api_key": "sk-1",
        }), mock.patch.object(llm, "_claude_generate", return_value=fake) as gen:
            llm.generate_script("Topic", 3, language="pt")
        self.assertEqual(gen.call_args.kwargs.get("language"), "pt")


if __name__ == "__main__":
    unittest.main()
