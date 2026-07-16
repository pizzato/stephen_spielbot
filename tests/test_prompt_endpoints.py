"""Prompt editor endpoint wiring — the Settings → Prompts screen."""
import os
import tempfile
import unittest

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend  # noqa: E402
from pipeline import prompts as _prompts  # noqa: E402

NAME = "script_claude_initial"


class PromptEndpointTests(unittest.TestCase):
    def setUp(self):
        _prompts.reset()

    def tearDown(self):
        _prompts.reset()

    def _entry(self, payload, name=NAME):
        return next(p for p in payload["prompts"] if p["name"] == name)

    def test_get_lists_every_prompt_unmodified(self):
        r = backend.get_prompts()
        self.assertEqual(len(r["prompts"]), len(_prompts.defaults()))
        self.assertFalse(any(p["modified"] for p in r["prompts"]))
        self.assertTrue(r["override_path"].endswith("prompts.yaml"))

    def test_save_then_reset_round_trip(self):
        body = backend.PromptUpdate(name=NAME, fields={"system": "Be brief."})
        r = backend.post_prompt(body)
        self.assertTrue(self._entry(r)["modified"])
        self.assertEqual(_prompts.system(NAME), "Be brief.")

        r = backend.post_prompt_reset(backend.PromptReset(name=NAME))
        self.assertFalse(self._entry(r)["modified"])
        self.assertEqual(_prompts.system(NAME), _prompts.defaults()[NAME]["system"].rstrip("\n"))

    def test_reset_without_a_name_reverts_everything(self):
        backend.post_prompt(backend.PromptUpdate(name=NAME, fields={"system": "A"}))
        backend.post_prompt(backend.PromptUpdate(name="youtube_tags", fields={"system": "B"}))
        r = backend.post_prompt_reset(backend.PromptReset())
        self.assertFalse(any(p["modified"] for p in r["prompts"]))

    def test_save_takes_effect_without_a_restart(self):
        # The loader caches the merged prompts for the process lifetime, so the
        # save path must invalidate it — otherwise an edit looks saved in the UI
        # but the running backend keeps sending the old text to the model.
        _prompts.system(NAME)  # prime the cache
        backend.post_prompt(backend.PromptUpdate(name=NAME, fields={"system": "Fresh."}))
        self.assertEqual(_prompts.system(NAME), "Fresh.")

    def test_unknown_prompt_is_404(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.post_prompt(backend.PromptUpdate(name="nope", fields={"system": "x"}))
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.post_prompt_reset(backend.PromptReset(name="nope"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_empty_prompt_is_rejected(self):
        for bad in ({}, {"system": "   "}):
            with self.assertRaises(backend.HTTPException) as ctx:
                backend.post_prompt(backend.PromptUpdate(name=NAME, fields=bad))
            self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(_prompts._OVERRIDE_PATH.exists())


if __name__ == "__main__":
    unittest.main()
