"""Community-comment engagement (issue #84).

Covers: per-channel engagement config (preserve/default/roundtrip), the
non-request drafting + auto-send pass in `_fetch_and_evaluate`, run-once
semantics, and the channel-settings / send / dismiss endpoints.
No network — everything is mocked at the pipeline boundary.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app as gapp
import webapp.backend.main as backend


def _cfg(engagement_prompt="", auto_respond=False):
    cfg = dict(gapp.DEFAULT_CFG)
    cfg["youtube_channels"] = [{"id": "UC1", "name": "", "channel_id": "",
                                "engagement_prompt": engagement_prompt,
                                "auto_respond": auto_respond}]
    return cfg


def _non_request_eval():
    return {"is_request": False, "suggested_title": "", "confidence": 0.0,
            "interestingness": 0.0, "reason": "", "suggested_scene_count": 20}


class EngagementConfigTests(unittest.TestCase):
    def test_ensure_channels_preserves_engagement_fields(self):
        cfg = {"youtube_channels": [{"id": "UC1", "name": "N", "channel_id": "UC1",
                                     "engagement_prompt": "Be kind", "auto_respond": True}]}
        gapp._ensure_channels(cfg)
        e = cfg["youtube_channels"][0]
        self.assertEqual(e["engagement_prompt"], "Be kind")
        self.assertTrue(e["auto_respond"])

    def test_ensure_channels_defaults_engagement_fields(self):
        cfg = {"youtube_channels": [{"id": "UC1"}]}
        gapp._ensure_channels(cfg)
        e = cfg["youtube_channels"][0]
        self.assertEqual(e["engagement_prompt"], "")
        self.assertFalse(e["auto_respond"])

    def test_ensure_channels_preserves_video_category(self):
        cfg = {"youtube_channels": [{"id": "UC1", "video_category": "27"}]}
        gapp._ensure_channels(cfg)
        self.assertEqual(cfg["youtube_channels"][0]["video_category"], "27")
        # Unset defaults to "" (fall back to the global category at upload time).
        cfg2 = {"youtube_channels": [{"id": "UC2"}]}
        gapp._ensure_channels(cfg2)
        self.assertEqual(cfg2["youtube_channels"][0]["video_category"], "")

    def test_save_load_roundtrip(self):
        # Isolated config file — never write the process-wide CONFIG_FILE, or the
        # persisted channels leak into other suites' load_config() (channel resolution).
        tmp = Path(tempfile.mkdtemp(prefix="spielbot-test-cfg-")) / "config.yaml"
        cfg = dict(gapp.DEFAULT_CFG)
        cfg["youtube_channels"] = [{"id": "UC1", "name": "N", "channel_id": "UC1",
                                    "engagement_prompt": "Hello", "auto_respond": True}]
        with mock.patch.object(gapp, "CONFIG_FILE", tmp):
            gapp.save_config(cfg)
            loaded = gapp.load_config()
        e = next(c for c in loaded["youtube_channels"] if c["id"] == "UC1")
        self.assertEqual(e["engagement_prompt"], "Hello")
        self.assertTrue(e["auto_respond"])


class FetchEngagementTests(unittest.TestCase):
    def _run(self, cfg, gen_return, reply_success=True, comments=None):
        comments = comments or [{"comment_id": "a", "text": "great video!",
                                 "commenter": "x", "replies": []}]
        cache_store = []
        saved = {}

        def save(c):
            cache_store[:] = c
            saved["cache"] = c

        gen = mock.MagicMock(return_value=gen_return)
        reply = mock.MagicMock(return_value={"success": reply_success, "error": ""})
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend.yt, "fetch_channel_comments",
                               side_effect=lambda s, channel="": [dict(c) for c in comments]), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=cache_store), \
             mock.patch.object(backend.yt, "save_comments_cache", side_effect=save), \
             mock.patch.object(backend.yt, "evaluate_comment", return_value=_non_request_eval()), \
             mock.patch.object(backend.yt, "reply_to_comment", reply), \
             mock.patch.object(backend.llm, "generate_community_reply", gen):
            out = backend._fetch_and_evaluate(auto_approve=False)
        return out, saved.get("cache", cache_store), gen, reply

    def test_drafts_when_not_auto_respond(self):
        out, cache, gen, reply = self._run(
            _cfg(engagement_prompt="Be friendly", auto_respond=False),
            {"should_reply": True, "reply": "Thanks for watching!", "reason": "praise"})
        c = cache[0]
        self.assertEqual(c["engagement_status"], "draft")
        self.assertEqual(c["engagement_draft"], "Thanks for watching!")
        self.assertEqual(out["community_drafted"], 1)
        self.assertEqual(out["community_sent"], 0)
        reply.assert_not_called()
        gen.assert_called_once()

    def test_auto_sends_when_enabled(self):
        out, cache, gen, reply = self._run(
            _cfg(engagement_prompt="Be friendly", auto_respond=True),
            {"should_reply": True, "reply": "Cheers!", "reason": "praise"})
        c = cache[0]
        self.assertEqual(c["engagement_status"], "sent")
        self.assertEqual(out["community_sent"], 1)
        self.assertEqual(reply.call_args.kwargs.get("channel"), "UC1")

    def test_failed_auto_send_falls_back_to_draft(self):
        out, cache, gen, reply = self._run(
            _cfg(engagement_prompt="Be friendly", auto_respond=True),
            {"should_reply": True, "reply": "Cheers!", "reason": "p"}, reply_success=False)
        self.assertEqual(cache[0]["engagement_status"], "draft")
        self.assertEqual(out["community_drafted"], 1)
        self.assertEqual(out["community_sent"], 0)

    def test_skip_when_should_reply_false(self):
        out, cache, gen, reply = self._run(
            _cfg(engagement_prompt="Be friendly"),
            {"should_reply": False, "reply": "", "reason": "low effort"})
        self.assertEqual(cache[0]["engagement_status"], "skip")
        self.assertEqual(out["community_drafted"], 0)

    def test_no_prompt_means_no_llm_call(self):
        out, cache, gen, reply = self._run(
            _cfg(engagement_prompt=""),
            {"should_reply": True, "reply": "x", "reason": ""})
        self.assertEqual(cache[0]["engagement_status"], "")
        gen.assert_not_called()

    def test_run_once_does_not_redraft(self):
        cfg = _cfg(engagement_prompt="Be friendly", auto_respond=False)
        cache_store = []
        gen = mock.MagicMock(return_value={"should_reply": True, "reply": "hi", "reason": "r"})
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend.yt, "fetch_channel_comments",
                               side_effect=lambda s, channel="": [{"comment_id": "a", "text": "nice",
                                                                   "commenter": "x", "replies": []}]), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=cache_store), \
             mock.patch.object(backend.yt, "save_comments_cache",
                               side_effect=lambda c: cache_store.__setitem__(slice(None), c)), \
             mock.patch.object(backend.yt, "evaluate_comment", return_value=_non_request_eval()), \
             mock.patch.object(backend.yt, "reply_to_comment", return_value={"success": True, "error": ""}), \
             mock.patch.object(backend.llm, "generate_community_reply", gen):
            backend._fetch_and_evaluate(auto_approve=False)
            backend._fetch_and_evaluate(auto_approve=False)
        self.assertEqual(gen.call_count, 1)

    def test_new_viewer_reply_reopens_thread(self):
        # A reply landing on an already-engaged thread (a viewer answering our
        # reply) must re-open it and re-draft against the new message (issue #84).
        cfg = _cfg(engagement_prompt="Be friendly", auto_respond=False)
        cache_store = []
        gen = mock.MagicMock(return_value={"should_reply": True, "reply": "hi", "reason": "r"})
        fetches = iter([
            [{"comment_id": "a", "text": "nice", "commenter": "x",
              "published_at": "2026-01-01T00:00:00Z", "replies": []}],
            [{"comment_id": "a", "text": "nice", "commenter": "x",
              "published_at": "2026-01-01T00:00:00Z", "total_reply_count": 2, "replies": [
                  {"reply_id": "r1", "commenter": "Chan", "text": "thanks",
                   "published_at": "2026-01-02T00:00:00Z", "is_owner": True},
                  {"reply_id": "r2", "commenter": "x", "text": "one more thing",
                   "published_at": "2026-01-03T00:00:00Z", "is_owner": False}]}],
        ])
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend.yt, "fetch_channel_comments",
                               side_effect=lambda s, channel="": [dict(c) for c in next(fetches)]), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=cache_store), \
             mock.patch.object(backend.yt, "save_comments_cache",
                               side_effect=lambda c: cache_store.__setitem__(slice(None), c)), \
             mock.patch.object(backend.yt, "evaluate_comment", return_value=_non_request_eval()), \
             mock.patch.object(backend.yt, "reply_to_comment", return_value={"success": True, "error": ""}), \
             mock.patch.object(backend.llm, "generate_community_reply", gen):
            backend._fetch_and_evaluate(auto_approve=False)   # drafts vs the top-level comment
            self.assertEqual(cache_store[0]["engagement_anchor"], "a")
            backend._fetch_and_evaluate(auto_approve=False)   # viewer replied → re-draft vs r2
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(cache_store[0]["engagement_anchor"], "r2")
        self.assertEqual(cache_store[0]["replies"][-1]["text"], "one more thing")
        # the re-draft answers the latest viewer reply, with our prior reply as context
        self.assertEqual(gen.call_args.args[0], "one more thing")
        self.assertIn("Channel: thanks", gen.call_args.args[2])

    def test_pre_existing_dismissed_not_resurrected(self):
        # A legacy record (status but no engagement_anchor) with no new viewer reply
        # must not be re-evaluated or auto-sent — respect the earlier dismiss.
        cfg = _cfg(engagement_prompt="Be friendly", auto_respond=True)
        cache_store = [{"comment_id": "a", "channel": "UC1", "text": "meh", "commenter": "x",
                        "published_at": "2026-01-01T00:00:00Z", "replies": [],
                        "is_request": False, "evaluated": True, "engagement_status": "dismissed"}]
        gen = mock.MagicMock(return_value={"should_reply": True, "reply": "hi", "reason": "r"})
        reply = mock.MagicMock(return_value={"success": True, "error": ""})
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend.yt, "fetch_channel_comments",
                               side_effect=lambda s, channel="": [{"comment_id": "a", "text": "meh",
                                   "commenter": "x", "published_at": "2026-01-01T00:00:00Z", "replies": []}]), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=cache_store), \
             mock.patch.object(backend.yt, "save_comments_cache",
                               side_effect=lambda c: cache_store.__setitem__(slice(None), c)), \
             mock.patch.object(backend.yt, "evaluate_comment", return_value=_non_request_eval()), \
             mock.patch.object(backend.yt, "reply_to_comment", reply), \
             mock.patch.object(backend.llm, "generate_community_reply", gen):
            backend._fetch_and_evaluate(auto_approve=False)
        self.assertEqual(cache_store[0]["engagement_status"], "dismissed")
        gen.assert_not_called()
        reply.assert_not_called()

    def test_no_redraft_when_channel_spoke_last(self):
        # Our own reply is the most recent message → nothing to answer; no LLM call.
        cfg = _cfg(engagement_prompt="Be friendly")
        comments = [{"comment_id": "a", "text": "nice", "commenter": "x",
                     "published_at": "2026-01-01T00:00:00Z", "replies": [
                         {"reply_id": "r1", "commenter": "Chan", "text": "thanks",
                          "published_at": "2026-01-02T00:00:00Z", "is_owner": True}]}]
        out, cache, gen, reply = self._run(
            cfg, {"should_reply": True, "reply": "x", "reason": ""}, comments=comments)
        self.assertEqual(cache[0].get("engagement_status", ""), "")
        gen.assert_not_called()


class ThreadAnchorTests(unittest.TestCase):
    A = staticmethod(lambda **kw: backend.yt.thread_anchor(kw))

    def test_top_level_only(self):
        a = self.A(comment_id="a", commenter="x", text="hi", published_at="t1")
        self.assertFalse(a["last_is_owner"])
        self.assertEqual(a["anchor_id"], "a")
        self.assertEqual(a["anchor_text"], "hi")
        self.assertEqual(a["thread_text"], "")

    def test_viewer_reply_is_latest(self):
        a = self.A(comment_id="a", commenter="x", text="hi", published_at="t1", replies=[
            {"reply_id": "r1", "commenter": "Chan", "text": "ty", "published_at": "t2", "is_owner": True},
            {"reply_id": "r2", "commenter": "x", "text": "more", "published_at": "t3", "is_owner": False}])
        self.assertFalse(a["last_is_owner"])
        self.assertEqual(a["anchor_id"], "r2")
        self.assertIn("Channel: ty", a["thread_text"])
        self.assertIn("x: hi", a["thread_text"])
        self.assertNotIn("more", a["thread_text"])   # the anchor itself is excluded from context

    def test_owner_reply_is_latest(self):
        a = self.A(comment_id="a", commenter="x", text="hi", published_at="t1", replies=[
            {"reply_id": "r1", "commenter": "Chan", "text": "ty", "published_at": "t2", "is_owner": True}])
        self.assertTrue(a["last_is_owner"])

    def test_out_of_order_replies_sort_by_time(self):
        a = self.A(comment_id="a", commenter="x", text="hi", published_at="t1", replies=[
            {"reply_id": "r2", "commenter": "x", "text": "newer", "published_at": "t3", "is_owner": False},
            {"reply_id": "r1", "commenter": "Chan", "text": "older", "published_at": "t2", "is_owner": True}])
        self.assertEqual(a["anchor_id"], "r2")
        self.assertFalse(a["last_is_owner"])


class ChannelSettingsEndpointTests(unittest.TestCase):
    def test_saves_engagement_fields(self):
        cfg = _cfg()
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend.gapp, "save_config") as save:
            out = backend.yt_channel_settings(
                backend.ChannelSettingsBody(id="UC1", engagement_prompt="Be kind", auto_respond=True))
        self.assertTrue(out["ok"])
        self.assertEqual(cfg["youtube_channels"][0]["engagement_prompt"], "Be kind")
        self.assertTrue(cfg["youtube_channels"][0]["auto_respond"])
        save.assert_called_once()

    def test_saves_video_category(self):
        cfg = _cfg()
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend.gapp, "save_config") as save:
            out = backend.yt_channel_settings(
                backend.ChannelSettingsBody(id="UC1", video_category="27"))
        self.assertTrue(out["ok"])
        self.assertEqual(cfg["youtube_channels"][0]["video_category"], "27")
        save.assert_called_once()

    def test_unknown_channel_404(self):
        with mock.patch.object(backend.gapp, "load_config", return_value={"youtube_channels": []}):
            with self.assertRaises(backend.HTTPException):
                backend.yt_channel_settings(backend.ChannelSettingsBody(id="X"))


class CategoryForChannelTests(unittest.TestCase):
    def test_uses_channel_category_when_set(self):
        cfg = {"youtube_channels": [{"id": "UC1", "video_category": "27"}]}
        self.assertEqual(backend._category_for_channel(cfg, "UC1"), "27")

    def test_falls_back_to_global_then_default(self):
        cfg = {"youtube_channels": [{"id": "UC1", "video_category": ""}],
               "youtube_post_category": "24"}
        self.assertEqual(backend._category_for_channel(cfg, "UC1"), "24")
        # No global set, unknown channel → hard default "22".
        self.assertEqual(backend._category_for_channel({"youtube_channels": []}, "UC1"), "22")


class CommunityActionEndpointTests(unittest.TestCase):
    def test_send_requires_draft_status(self):
        cache = [{"comment_id": "a", "channel": "UC1", "engagement_status": "skip"}]
        with mock.patch.object(backend.yt, "load_comments_cache", return_value=cache), \
             mock.patch.object(backend.yt, "save_comments_cache") as save, \
             mock.patch.object(backend.yt, "reply_to_comment") as reply:
            with self.assertRaises(backend.HTTPException):
                backend.youtube_community_send(backend.CommentActionBody(comment_id="a", text="hi"))
        reply.assert_not_called()
        save.assert_not_called()

    def test_send_posts_and_marks_sent(self):
        cache = [{"comment_id": "a", "channel": "UC1", "engagement_status": "draft",
                  "engagement_draft": "d"}]
        with mock.patch.object(backend.gapp, "load_config", return_value={"youtube_client_secrets": "/s"}), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=cache), \
             mock.patch.object(backend.yt, "save_comments_cache") as save, \
             mock.patch.object(backend.yt, "reply_to_comment",
                               return_value={"success": True, "error": ""}) as reply:
            out = backend.youtube_community_send(
                backend.CommentActionBody(comment_id="a", text="edited reply"))
        self.assertTrue(out["ok"])
        self.assertEqual(cache[0]["engagement_status"], "sent")
        self.assertEqual(cache[0]["engagement_draft"], "edited reply")
        self.assertEqual(reply.call_args.kwargs.get("channel"), "UC1")
        save.assert_called_once()

    def test_dismiss_requires_draft(self):
        cache = [{"comment_id": "a", "engagement_status": "sent"}]
        with mock.patch.object(backend.yt, "load_comments_cache", return_value=cache), \
             mock.patch.object(backend.yt, "save_comments_cache") as save:
            with self.assertRaises(backend.HTTPException):
                backend.youtube_community_dismiss(backend.CommentActionBody(comment_id="a"))
        save.assert_not_called()

    def test_dismiss_marks_dismissed(self):
        cache = [{"comment_id": "a", "engagement_status": "draft"}]
        with mock.patch.object(backend.yt, "load_comments_cache", return_value=cache), \
             mock.patch.object(backend.yt, "save_comments_cache") as save:
            out = backend.youtube_community_dismiss(backend.CommentActionBody(comment_id="a"))
        self.assertTrue(out["ok"])
        self.assertEqual(cache[0]["engagement_status"], "dismissed")
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
