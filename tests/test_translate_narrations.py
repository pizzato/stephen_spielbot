"""translate_narrations batching — large scripts must never hit an LLM's
output-token ceiling. No network: _chat_complete is mocked."""
import json
import unittest
from unittest import mock

import pipeline.llm as llm


def _scenes(n, text="A narration line that is reasonably long for a scene."):
    return [{"id": i, "narration": text} for i in range(1, n + 1)]


def _echo_translation(cfg, system, user_msg, max_tokens, label):
    """Fake backend: 'translates' by prefixing, answering whatever ids the
    prompt asked for."""
    payload = json.loads(user_msg[user_msg.index("{"):user_msg.rindex("}") + 1])
    return json.dumps({k: f"XL:{v}" for k, v in payload.items()})


class TranslateNarrationsTests(unittest.TestCase):
    def test_small_script_is_one_call(self):
        with mock.patch.object(llm, "_chat_complete", side_effect=_echo_translation) as call:
            out = llm.translate_narrations(_scenes(5), "Spanish", cfg={})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(len(out), 5)
        self.assertTrue(out[1].startswith("XL:"))

    def test_large_script_batches(self):
        with mock.patch.object(llm, "_chat_complete", side_effect=_echo_translation) as call:
            out = llm.translate_narrations(_scenes(30), "Spanish", cfg={})
        self.assertEqual(call.call_count, 3)  # 12 + 12 + 6
        self.assertEqual(len(out), 30)
        self.assertEqual(sorted(out), list(range(1, 31)))

    def test_budget_scales_with_source_length(self):
        long_text = "word " * 400  # ~2000 chars per scene
        with mock.patch.object(llm, "_chat_complete", side_effect=_echo_translation) as call:
            llm.translate_narrations(_scenes(12, long_text), "Spanish", cfg={})
        budget = call.call_args.kwargs["max_tokens"]
        # 12 scenes × ~2000 chars → src ≈ 8000 tokens; budget must comfortably
        # exceed the source so the translation never truncates.
        self.assertGreater(budget, 12_000)

    def test_token_limit_splits_and_retries(self):
        calls = []

        def flaky(cfg, system, user_msg, max_tokens, label):
            payload = json.loads(user_msg[user_msg.index("{"):user_msg.rindex("}") + 1])
            calls.append(len(payload))
            if len(payload) > 6:
                raise RuntimeError(f"Claude hit the token limit ({max_tokens}) for {label}.")
            return json.dumps({k: f"XL:{v}" for k, v in payload.items()})

        with mock.patch.object(llm, "_chat_complete", side_effect=flaky):
            out = llm.translate_narrations(_scenes(12), "Spanish", cfg={})
        self.assertEqual(len(out), 12)
        self.assertEqual(calls, [12, 6, 6])  # failed full batch, then two halves

    def test_translate_metadata_parses_and_caps_title(self):
        def fake(cfg, system, user_msg, max_tokens, label):
            return json.dumps({"title": "T" * 150, "description": "Uma descrição."})

        with mock.patch.object(llm, "_chat_complete", side_effect=fake):
            out = llm.translate_metadata("A Title", "A description.", "Portuguese", cfg={})
        self.assertEqual(len(out["title"]), 100)  # YouTube's hard cap
        self.assertEqual(out["description"], "Uma descrição.")

    def test_translate_metadata_carries_cover_phrase(self):
        def fake(cfg, system, user_msg, max_tokens, label):
            self.assertIn("A *Cover* Phrase", user_msg)  # source phrase in prompt
            return json.dumps({"title": "Um Título", "description": "",
                               "cover_phrase": "Uma *Frase* " + "x" * 100})

        with mock.patch.object(llm, "_chat_complete", side_effect=fake):
            out = llm.translate_metadata("A Title", "", "Portuguese", cfg={},
                                         cover_phrase="A *Cover* Phrase")
        self.assertTrue(out["cover_phrase"].startswith("Uma *Frase*"))
        self.assertEqual(len(out["cover_phrase"]), 80)  # cover-phrase hard cap

    def test_translate_metadata_missing_title_raises(self):
        with mock.patch.object(llm, "_chat_complete", return_value='{"description": "x"}'):
            with self.assertRaises(RuntimeError):
                llm.translate_metadata("A Title", "", "Portuguese", cfg={})

    def test_missing_scene_raises(self):
        def drops_one(cfg, system, user_msg, max_tokens, label):
            payload = json.loads(user_msg[user_msg.index("{"):user_msg.rindex("}") + 1])
            payload.pop(next(iter(payload)))
            return json.dumps({k: f"XL:{v}" for k, v in payload.items()})

        with mock.patch.object(llm, "_chat_complete", side_effect=drops_one):
            with self.assertRaises(RuntimeError):
                llm.translate_narrations(_scenes(3), "Spanish", cfg={})


if __name__ == "__main__":
    unittest.main()
