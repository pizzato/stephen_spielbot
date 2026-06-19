"""X (Twitter) support — accounts, config normalization, premium detection and
the video-length/Premium post decision (issue #107).

Mirrors tests/test_youtube_channels.py. No network — the post decision and
premium parse are pure, and config normalization is filesystem-mocked.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app as gapp
import pipeline.x as xt


class TokenPathTests(unittest.TestCase):
    def test_default_and_empty_map_to_legacy_token(self):
        self.assertEqual(xt._token_path(""), xt._X_TOKEN_PATH)
        self.assertEqual(xt._token_path("default"), xt._X_TOKEN_PATH)

    def test_account_key_gets_its_own_file(self):
        p = xt._token_path("12345")
        self.assertEqual(p.name, "x_token_12345.json")
        self.assertEqual(p.parent, xt._X_TOKEN_PATH.parent)

    def test_unsafe_characters_are_sanitized(self):
        self.assertEqual(xt._token_path("a/b c").name, "x_token_a_b_c.json")


class EnsureXAccountsTests(unittest.TestCase):
    def test_seeds_default_entry_from_legacy_token(self):
        cfg = {"x_accounts": []}
        with mock.patch.object(Path, "exists", return_value=True):
            gapp._ensure_x_accounts(cfg)
        self.assertEqual(cfg["x_accounts"],
                         [{"id": "default", "name": "", "account_id": "",
                           "premium": False, "engagement_prompt": "",
                           "auto_respond": False, "language": "en",
                           "publish_per_day": 0}])

    def test_no_seed_without_legacy_token(self):
        cfg = {"x_accounts": []}
        with mock.patch.object(Path, "exists", return_value=False):
            gapp._ensure_x_accounts(cfg)
        self.assertEqual(cfg["x_accounts"], [])

    def test_normalizes_and_dedupes_entries(self):
        cfg = {"x_accounts": [
            {"id": "1", "name": "one", "account_id": "1", "premium": True},
            {"id": "1", "name": "dupe"},
            {"name": "no key"},
            "garbage",
        ]}
        with mock.patch.object(Path, "exists", return_value=False):
            gapp._ensure_x_accounts(cfg)
        self.assertEqual(cfg["x_accounts"],
                         [{"id": "1", "name": "one", "account_id": "1", "premium": True,
                           "engagement_prompt": "", "auto_respond": False, "language": "en",
                           "publish_per_day": 0}])

    def test_preserves_engagement_and_language(self):
        cfg = {"x_accounts": [
            {"id": "1", "engagement_prompt": "be witty", "auto_respond": True, "language": "es"}]}
        with mock.patch.object(Path, "exists", return_value=False):
            gapp._ensure_x_accounts(cfg)
        entry = cfg["x_accounts"][0]
        self.assertEqual(entry["engagement_prompt"], "be witty")
        self.assertTrue(entry["auto_respond"])
        self.assertEqual(entry["language"], "es")

    def test_normalizes_publish_per_day(self):
        # Whole numbers (and numeric strings) stay ints, fractions stay floats,
        # and anything <= 0 or unparseable collapses to 0 (no throttle).
        cfg = {"x_accounts": [
            {"id": "1", "publish_per_day": 3},
            {"id": "2", "publish_per_day": "2"},
            {"id": "3", "publish_per_day": 2.5},
            {"id": "4", "publish_per_day": -1},
            {"id": "5", "publish_per_day": "nope"},
        ]}
        with mock.patch.object(Path, "exists", return_value=False):
            gapp._ensure_x_accounts(cfg)
        self.assertEqual([a["publish_per_day"] for a in cfg["x_accounts"]],
                         [3, 2, 2.5, 0, 0])

    def test_clears_dangling_style_refs(self):
        cfg = {
            "x_accounts": [{"id": "1"}],
            "x_account": "GONE",
            "styles": [{"name": "A", "x_account": "1"}, {"name": "B", "x_account": "GONE"}],
        }
        with mock.patch.object(Path, "exists", return_value=False):
            gapp._ensure_x_accounts(cfg)
        self.assertEqual(cfg["styles"][0]["x_account"], "1")
        self.assertEqual(cfg["styles"][1]["x_account"], "")
        self.assertEqual(cfg["x_account"], "")


class XAccountForStyleTests(unittest.TestCase):
    def _cfg(self):
        cfg = dict(gapp.DEFAULT_CFG)
        cfg["x_accounts"] = [{"id": "1"}, {"id": "2"}]
        cfg["styles"] = [{"name": "Docs", "x_account": "2"}, {"name": "Kids", "x_account": ""}]
        cfg["default_style"] = "Docs"
        with mock.patch.object(Path, "exists", return_value=False):
            return gapp._ensure_styles(cfg)

    def test_uses_the_styles_account(self):
        self.assertEqual(gapp.x_account_for_style(self._cfg(), "Docs"), "2")

    def test_blank_style_account_is_none(self):
        # X is opt-in per style — a blank choice means "don't post to X".
        self.assertEqual(gapp.x_account_for_style(self._cfg(), "Kids"), "")

    def test_unknown_style_uses_default_style(self):
        self.assertEqual(gapp.x_account_for_style(self._cfg(), "nope"), "2")

    def test_no_accounts_resolves_to_empty(self):
        cfg = self._cfg()
        cfg["x_accounts"] = []
        self.assertEqual(gapp.x_account_for_style(cfg, "Docs"), "")


class PremiumDetectionTests(unittest.TestCase):
    def test_subscription_type_means_premium(self):
        self.assertTrue(xt._premium_from_me({"data": {"subscription_type": "Premium"}}))
        self.assertTrue(xt._premium_from_me({"data": {"subscription_type": "Basic"}}))
        self.assertTrue(xt._premium_from_me({"data": {"subscription_type": "Premium+"}}))

    def test_none_or_free_is_not_premium(self):
        self.assertFalse(xt._premium_from_me({"data": {"subscription_type": "None"}}))
        self.assertFalse(xt._premium_from_me({"data": {"subscription_type": ""}}))
        self.assertFalse(xt._premium_from_me({"data": {}}))

    def test_legacy_blue_verified_type_counts(self):
        self.assertTrue(xt._premium_from_me({"data": {"verified_type": "blue"}}))
        self.assertFalse(xt._premium_from_me({"data": {"verified_type": "business"}}))

    def test_accepts_unwrapped_payload(self):
        self.assertTrue(xt._premium_from_me({"subscription_type": "Premium"}))


class DecidePostTargetTests(unittest.TestCase):
    """The core video-length/Premium rule (issue #107). Inject duration/size so
    the pure decision is exercised without ffprobe."""

    def test_premium_posts_full_even_when_long(self):
        d = xt.decide_post_target("/v.mp4", premium=True, youtube_url="",
                                  duration_secs=600, size_bytes=2 * 1024**3)
        self.assertEqual(d["action"], "post_full")

    def test_short_video_posts_full_without_premium(self):
        d = xt.decide_post_target("/v.mp4", premium=False, youtube_url="",
                                  duration_secs=120, size_bytes=10 * 1024**2)
        self.assertEqual(d["action"], "post_full")

    def test_long_video_with_link_falls_back_to_link(self):
        d = xt.decide_post_target("/v.mp4", premium=False,
                                  youtube_url="https://youtu.be/abc",
                                  duration_secs=300, size_bytes=10 * 1024**2)
        self.assertEqual(d["action"], "post_link")

    def test_long_video_without_link_is_skipped(self):
        d = xt.decide_post_target("/v.mp4", premium=False, youtube_url="",
                                  duration_secs=300, size_bytes=10 * 1024**2)
        self.assertEqual(d["action"], "skip")
        self.assertIn("limit", d["reason"].lower())

    def test_oversize_short_video_without_link_is_skipped(self):
        d = xt.decide_post_target("/v.mp4", premium=False, youtube_url="",
                                  duration_secs=60, size_bytes=600 * 1024**2)
        self.assertEqual(d["action"], "skip")

    def test_oversize_short_video_with_link_falls_back(self):
        d = xt.decide_post_target("/v.mp4", premium=False,
                                  youtube_url="https://youtu.be/abc",
                                  duration_secs=60, size_bytes=600 * 1024**2)
        self.assertEqual(d["action"], "post_link")


if __name__ == "__main__":
    unittest.main()
