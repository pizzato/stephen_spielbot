"""The web API must never return credential values to the browser, and a
blank secret on save must keep the stored one ('leave blank to keep')."""
import unittest

import app


class PublicConfigTests(unittest.TestCase):
    def test_secret_values_are_blanked_with_set_flags(self):
        cfg = {
            "claude_api_key": "sk-ant-SECRET",
            "grok_api_key": "xai-SECRET",
            "hf_token": "hf_SECRET",
            "x_client_secret": "",
        }
        pub = app.public_config(cfg)
        self.assertEqual(pub["claude_api_key"], "")
        self.assertEqual(pub["grok_api_key"], "")
        self.assertEqual(pub["hf_token"], "")
        self.assertTrue(pub["claude_api_key_set"])
        self.assertTrue(pub["grok_api_key_set"])
        self.assertTrue(pub["hf_token_set"])
        self.assertFalse(pub["x_client_secret_set"])

    def test_client_secrets_path_is_not_treated_as_secret(self):
        cfg = {"youtube_client_secrets": "~/.config/video-generator/client_secrets.json"}
        pub = app.public_config(cfg)
        self.assertEqual(pub["youtube_client_secrets"], cfg["youtube_client_secrets"])

    def test_public_config_does_not_mutate_input(self):
        cfg = {"claude_api_key": "sk-ant-SECRET"}
        app.public_config(cfg)
        self.assertEqual(cfg["claude_api_key"], "sk-ant-SECRET")


class MergeConfigUpdateTests(unittest.TestCase):
    def test_blank_secret_keeps_stored_value(self):
        current = {"claude_api_key": "sk-ant-SECRET", "grok_api_key": "xai-SECRET", "hf_token": "hf_SECRET"}
        update = {"claude_api_key": "", "grok_api_key": "", "hf_token": ""}
        merged = app.merge_config_update(current, update)
        self.assertEqual(merged["claude_api_key"], "sk-ant-SECRET")
        self.assertEqual(merged["grok_api_key"], "xai-SECRET")
        self.assertEqual(merged["hf_token"], "hf_SECRET")

    def test_new_secret_value_overwrites(self):
        current = {"claude_api_key": "sk-ant-OLD"}
        merged = app.merge_config_update(current, {"claude_api_key": "sk-ant-NEW"})
        self.assertEqual(merged["claude_api_key"], "sk-ant-NEW")

    def test_set_flags_are_never_persisted(self):
        merged = app.merge_config_update({}, {"claude_api_key_set": True, "hf_token_set": False})
        self.assertNotIn("claude_api_key_set", merged)
        self.assertNotIn("hf_token_set", merged)

    def test_non_secret_updates_apply(self):
        merged = app.merge_config_update({"comfy_workers": ["a"]}, {"comfy_workers": ["a", "b"]})
        self.assertEqual(merged["comfy_workers"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
