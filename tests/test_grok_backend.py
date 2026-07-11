"""Grok LLM backend wiring."""
import unittest
from unittest import mock

from pipeline import llm


class GrokBackendTests(unittest.TestCase):
    def test_backend_ready(self):
        self.assertTrue(llm.llm_backend_ready({"llm_backend": "local"}))
        self.assertFalse(llm.llm_backend_ready({"llm_backend": "grok"}))
        self.assertTrue(llm.llm_backend_ready({"llm_backend": "grok", "grok_api_key": "xai-1"}))
        self.assertFalse(llm.llm_backend_ready({"llm_backend": "claude"}))
        self.assertTrue(llm.llm_backend_ready({"llm_backend": "claude", "claude_api_key": "sk-1"}))
        self.assertFalse(llm.llm_backend_ready({"llm_backend": "openai"}))
        self.assertTrue(llm.llm_backend_ready({"llm_backend": "openai", "openai_api_key": "sk-1"}))

    def test_generate_script_routes_to_grok(self):
        fake = (["scene"], "music", "style", [])
        with mock.patch.object(llm, "_load_cfg", return_value={
            "llm_backend": "grok", "grok_api_key": "xai-test", "grok_model": "grok-4.5",
        }), mock.patch.object(llm, "_grok_generate", return_value=fake) as gen:
            out = llm.generate_script("Topic", 3)
        self.assertEqual(out, fake)
        gen.assert_called_once()
        self.assertEqual(gen.call_args.args[3], "xai-test")
        self.assertEqual(gen.call_args.args[4], "grok-4.5")

    def test_generate_script_routes_to_openai(self):
        fake = (["scene"], "music", "style", [])
        with mock.patch.object(llm, "_load_cfg", return_value={
            "llm_backend": "openai", "openai_api_key": "sk-test", "openai_model": "gpt-4o",
        }), mock.patch.object(llm, "_openai_generate", return_value=fake) as gen:
            out = llm.generate_script("Topic", 3)
        self.assertEqual(out, fake)
        gen.assert_called_once()
        self.assertEqual(gen.call_args.args[3], "sk-test")
        self.assertEqual(gen.call_args.args[4], "gpt-4o")

    def test_openai_compatible_call_parses_content(self):
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "choices": [{"message": {"content": " hello "}, "finish_reason": "stop"}]
                }).encode()
        with mock.patch.object(llm.urllib.request, "urlopen", return_value=_Resp()):
            text = llm._openai_compatible_call(
                "https://api.x.ai/v1/chat/completions", "key", "grok-4.5",
                "sys", "user", 100, "test", retries=1,
            )
        self.assertEqual(text, "hello")


if __name__ == "__main__":
    unittest.main()
