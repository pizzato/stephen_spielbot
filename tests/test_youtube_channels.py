"""Multi-channel YouTube support (issue #22).

Covers: per-channel token paths, config channel normalization + legacy
migration, style→channel resolution, upload/reply channel routing, the
multi-channel comment sweep, and per-channel engagement model paths.
No network — everything is mocked at the pipeline boundary.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app as gapp
import pipeline.engagement as eng
import pipeline.youtube as yt
import webapp.backend.main as backend


class TokenPathTests(unittest.TestCase):
    def test_default_and_empty_map_to_legacy_token(self):
        self.assertEqual(yt._token_path(""), yt._TOKEN_PATH)
        self.assertEqual(yt._token_path("default"), yt._TOKEN_PATH)

    def test_channel_key_gets_its_own_file(self):
        p = yt._token_path("UCabc-123")
        self.assertEqual(p.name, "youtube_token_UCabc-123.json")
        self.assertEqual(p.parent, yt._TOKEN_PATH.parent)

    def test_unsafe_characters_are_sanitized(self):
        self.assertEqual(yt._token_path("a/b c").name, "youtube_token_a_b_c.json")


class EnsureChannelsTests(unittest.TestCase):
    def test_seeds_default_entry_from_legacy_token(self):
        cfg = {"youtube_channels": []}
        with mock.patch.object(Path, "exists", return_value=True):
            gapp._ensure_channels(cfg)
        self.assertEqual(cfg["youtube_channels"],
                         [{"id": "default", "name": "", "channel_id": "",
                           "engagement_prompt": "", "auto_respond": False,
                           "video_category": "", "language": "en",
                           "upload_captions": True, "publish_per_day": 0}])

    def test_no_seed_without_legacy_token(self):
        cfg = {"youtube_channels": []}
        with mock.patch.object(Path, "exists", return_value=False):
            gapp._ensure_channels(cfg)
        self.assertEqual(cfg["youtube_channels"], [])

    def test_normalizes_and_dedupes_entries(self):
        cfg = {"youtube_channels": [
            {"id": "UC1", "name": "One", "channel_id": "UC1"},
            {"id": "UC1", "name": "dupe"},
            {"name": "no key"},
            "garbage",
        ]}
        gapp._ensure_channels(cfg)
        self.assertEqual(cfg["youtube_channels"],
                         [{"id": "UC1", "name": "One", "channel_id": "UC1",
                           "engagement_prompt": "", "auto_respond": False,
                           "video_category": "", "language": "en",
                           "upload_captions": True, "publish_per_day": 0}])

    def test_preserves_explicit_language_and_caption_toggle(self):
        cfg = {"youtube_channels": [
            {"id": "UC1", "language": "es", "upload_captions": False},
        ]}
        gapp._ensure_channels(cfg)
        entry = cfg["youtube_channels"][0]
        self.assertEqual(entry["language"], "es")
        self.assertFalse(entry["upload_captions"])

    def test_normalizes_publish_per_day(self):
        # Whole numbers (and numeric strings) stay ints, fractions stay floats,
        # and anything <= 0 or unparseable collapses to 0 (no throttle).
        cfg = {"youtube_channels": [
            {"id": "UC1", "publish_per_day": 3},
            {"id": "UC2", "publish_per_day": "2"},
            {"id": "UC3", "publish_per_day": 2.5},
            {"id": "UC4", "publish_per_day": -1},
            {"id": "UC5", "publish_per_day": "nope"},
        ]}
        gapp._ensure_channels(cfg)
        self.assertEqual([c["publish_per_day"] for c in cfg["youtube_channels"]],
                         [3, 2, 2.5, 0, 0])

    def test_clears_dangling_style_refs(self):
        cfg = {
            "youtube_channels": [{"id": "UC1", "name": "", "channel_id": ""}],
            "youtube_channel": "GONE",
            "styles": [{"name": "A", "channel": "UC1"}, {"name": "B", "channel": "GONE"}],
        }
        gapp._ensure_channels(cfg)
        self.assertEqual(cfg["styles"][0]["channel"], "UC1")
        self.assertEqual(cfg["styles"][1]["channel"], "")
        self.assertEqual(cfg["youtube_channel"], "")


class ChannelForStyleTests(unittest.TestCase):
    def _cfg(self):
        cfg = dict(gapp.DEFAULT_CFG)
        cfg["youtube_channels"] = [{"id": "UC1", "name": "", "channel_id": ""},
                                   {"id": "UC2", "name": "", "channel_id": ""}]
        cfg["styles"] = [{"name": "Docs", "channel": "UC2"}, {"name": "Kids", "channel": ""}]
        cfg["default_style"] = "Docs"
        return gapp._ensure_styles(cfg)

    def test_uses_the_styles_channel(self):
        self.assertEqual(gapp.channel_for_style(self._cfg(), "Docs"), "UC2")

    def test_blank_style_channel_falls_back_to_first(self):
        self.assertEqual(gapp.channel_for_style(self._cfg(), "Kids"), "UC1")

    def test_unknown_style_uses_default_style(self):
        self.assertEqual(gapp.channel_for_style(self._cfg(), "nope"), "UC2")

    def test_no_channels_resolves_to_legacy_empty(self):
        cfg = self._cfg()
        cfg["youtube_channels"] = []
        self.assertEqual(gapp.channel_for_style(cfg, "Docs"), "")

    def test_channel_survives_style_save_roundtrip(self):
        cfg = self._cfg()
        gapp.save_config(cfg)
        loaded = gapp.load_config()
        docs = next(s for s in loaded["styles"] if s["name"] == "Docs")
        self.assertEqual(docs["channel"], "UC2")


class FinalizeNewChannelTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(gapp.DEFAULT_CFG)
        self.cfg["youtube_channels"] = []
        self.saved = None

    def _run(self, channel_id, name, status=None):
        def save(cfg):
            self.saved = cfg
        with mock.patch.object(backend.gapp, "load_config", return_value=self.cfg), \
             mock.patch.object(backend.gapp, "save_config", side_effect=save), \
             mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/s.json"), \
             mock.patch.object(backend.yt, "check_auth_status",
                               return_value=status or {"connected": False, "channel_id": ""}):
            return backend._finalize_new_channel(channel_id, name)

    def test_new_channel_appends_entry(self):
        key = self._run("UC9", "Nine")
        self.assertEqual(key, "UC9")
        self.assertEqual(self.saved["youtube_channels"],
                         [{"id": "UC9", "name": "Nine", "channel_id": "UC9"}])

    def test_reconnect_known_channel_reuses_entry(self):
        self.cfg["youtube_channels"] = [{"id": "UC9", "name": "Old", "channel_id": "UC9"}]
        key = self._run("UC9", "New Name")
        self.assertEqual(key, "UC9")
        self.assertEqual(len(self.saved["youtube_channels"]), 1)
        self.assertEqual(self.saved["youtube_channels"][0]["name"], "New Name")

    def test_reconnecting_the_legacy_channel_updates_default_entry(self):
        self.cfg["youtube_channels"] = [{"id": "default", "name": "", "channel_id": ""}]
        key = self._run("UC9", "Nine",
                        status={"connected": True, "channel_id": "UC9", "channel_name": "Nine"})
        self.assertEqual(key, "default")
        self.assertEqual(len(self.saved["youtube_channels"]), 1)
        self.assertEqual(self.saved["youtube_channels"][0]["channel_id"], "UC9")

    def test_distinct_new_channel_keeps_legacy_entry(self):
        self.cfg["youtube_channels"] = [{"id": "default", "name": "", "channel_id": ""}]
        key = self._run("UC9", "Nine",
                        status={"connected": True, "channel_id": "UCother", "channel_name": "Other"})
        self.assertEqual(key, "UC9")
        ids = [c["id"] for c in self.saved["youtube_channels"]]
        self.assertEqual(ids, ["default", "UC9"])


class UploadRoutingTests(unittest.TestCase):
    def test_upload_task_passes_the_channel_through(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-test-film-"))
        final = wd / "final.mp4"
        final.write_bytes(b"\0" * 20_000)
        with mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/s.json"), \
             mock.patch.object(backend.yt, "upload_video",
                               return_value={"video_id": "v1", "url": "u", "error": ""}) as up, \
             mock.patch.object(backend.gapp, "_write_job_meta"), \
             mock.patch.object(backend, "_post_completion_reply",
                               return_value={"attempted": False}):
            backend._run_upload_task(
                "t1", {"title": "T", "description": "", "category": "22",
                       "privacy": "private", "channel": "UC9"}, wd, final, None)
        self.assertEqual(up.call_args.kwargs.get("channel"), "UC9")
        self.assertEqual(backend._upload_tasks["t1"]["status"], "done")

    def test_upload_prefs_default_to_english_with_captions(self):
        cfg = {"youtube_channels": [{"id": "UC1"}]}
        self.assertEqual(backend._upload_prefs_for_channel(cfg, "UC1"), ("en", True))
        # An unknown channel key also falls back to the defaults.
        self.assertEqual(backend._upload_prefs_for_channel(cfg, "GONE"), ("en", True))

    def test_upload_prefs_reflect_channel_config(self):
        cfg = {"youtube_channels": [
            {"id": "UC1", "language": "pt", "upload_captions": False}]}
        self.assertEqual(backend._upload_prefs_for_channel(cfg, "UC1"), ("pt", False))

    def test_upload_task_honors_language_and_skips_disabled_captions(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-test-film-"))
        final = wd / "final.mp4"
        final.write_bytes(b"\0" * 20_000)
        cfg = {"youtube_channels": [
            {"id": "UC9", "language": "fr", "upload_captions": False}]}
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/s.json"), \
             mock.patch.object(backend.yt, "upload_video",
                               return_value={"video_id": "v1", "url": "u", "error": ""}) as up, \
             mock.patch.object(backend.gapp, "_write_job_meta"), \
             mock.patch.object(backend, "_post_completion_reply",
                               return_value={"attempted": False}):
            backend._run_upload_task(
                "t2", {"title": "T", "description": "", "category": "22",
                       "privacy": "private", "channel": "UC9"}, wd, final, None)
        kw = up.call_args.kwargs
        self.assertEqual(kw.get("default_language"), "fr")
        self.assertEqual(kw.get("default_audio_language"), "fr")
        self.assertIsNone(kw.get("captions_path"))

    def test_completion_reply_uses_the_comments_channel(self):
        item = {"id": "q1", "comment_id": "c1", "channel": "UC2"}
        with mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/s.json"), \
             mock.patch.object(backend.yt, "load_queue", return_value=[item]), \
             mock.patch.object(backend.yt, "reply_to_comment",
                               return_value={"success": True, "error": ""}) as reply, \
             mock.patch.object(backend.yt, "update_queue_item", return_value=True), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=[]), \
             mock.patch.object(backend.yt, "save_comments_cache"):
            backend._post_completion_reply("q1", "T", "https://youtu.be/v1")
        self.assertEqual(reply.call_args.kwargs.get("channel"), "UC2")


class FetchAndEvaluateTests(unittest.TestCase):
    def test_sweeps_all_channels_and_stamps_comments(self):
        cfg = dict(gapp.DEFAULT_CFG)
        cfg["youtube_channels"] = [{"id": "UC1", "name": "", "channel_id": ""},
                                   {"id": "UC2", "name": "", "channel_id": ""}]
        per_channel = {
            "UC1": [{"comment_id": "a", "text": "make a video about Rome", "commenter": "x"}],
            "UC2": [{"comment_id": "b", "text": "nice!", "commenter": "y"}],
        }
        saved = {}
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend.yt, "fetch_channel_comments",
                               side_effect=lambda s, channel="": per_channel[channel]), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=[]), \
             mock.patch.object(backend.yt, "save_comments_cache",
                               side_effect=lambda c: saved.update(cache=c)), \
             mock.patch.object(backend.yt, "evaluate_comment",
                               return_value={"is_request": False, "suggested_title": "",
                                             "confidence": 0.0, "interestingness": 0.0,
                                             "reason": "", "suggested_scene_count": 20}):
            out = backend._fetch_and_evaluate(auto_approve=False)
        self.assertEqual(out["new"], 2)
        channels = {c["comment_id"]: c["channel"] for c in saved["cache"]}
        self.assertEqual(channels, {"a": "UC1", "b": "UC2"})

    def test_one_dead_channel_does_not_kill_the_sweep(self):
        cfg = dict(gapp.DEFAULT_CFG)
        cfg["youtube_channels"] = [{"id": "UC1", "name": "", "channel_id": ""},
                                   {"id": "UC2", "name": "", "channel_id": ""}]

        def fetch(s, channel=""):
            if channel == "UC1":
                raise RuntimeError("token expired")
            return [{"comment_id": "b", "text": "hi", "commenter": "y"}]

        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend.yt, "fetch_channel_comments", side_effect=fetch), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=[]), \
             mock.patch.object(backend.yt, "save_comments_cache"), \
             mock.patch.object(backend.yt, "evaluate_comment",
                               return_value={"is_request": False, "suggested_title": "",
                                             "confidence": 0.0, "interestingness": 0.0,
                                             "reason": "", "suggested_scene_count": 20}):
            out = backend._fetch_and_evaluate(auto_approve=False)
        self.assertEqual(out["new"], 1)

    def test_all_channels_failing_raises(self):
        cfg = dict(gapp.DEFAULT_CFG)
        cfg["youtube_channels"] = [{"id": "UC1", "name": "", "channel_id": ""}]
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend.yt, "fetch_channel_comments",
                               side_effect=RuntimeError("nope")), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=[]), \
             mock.patch.object(backend.yt, "save_comments_cache"):
            with self.assertRaises(backend.HTTPException):
                backend._fetch_and_evaluate(auto_approve=False)


class QueueChannelTests(unittest.TestCase):
    def test_queue_entry_inherits_the_comments_channel(self):
        with mock.patch.object(yt, "load_queue", return_value=[]), \
             mock.patch.object(yt, "save_queue") as save:
            entry = yt.add_to_queue({"comment_id": "c1", "channel": "UC2",
                                     "text": "t", "commenter": "x"}, "Title")
        self.assertEqual(entry["channel"], "UC2")
        save.assert_called_once()


class EngagementChannelPathTests(unittest.TestCase):
    def test_default_channel_keeps_legacy_paths(self):
        for key in ("", "default"):
            content, timing, metrics = eng._paths(key)
            self.assertEqual(content, eng._CONTENT_PATH)
            self.assertEqual(timing, eng._TIMING_PATH)
            self.assertEqual(metrics, eng._METRICS_PATH)

    def test_other_channels_get_suffixed_paths(self):
        content, timing, metrics = eng._paths("UC9")
        self.assertEqual(content.name, "model_content_UC9.pkl")
        self.assertEqual(timing.name, "model_timing_UC9.pkl")
        self.assertEqual(metrics.name, "metrics_UC9.json")


if __name__ == "__main__":
    unittest.main()
