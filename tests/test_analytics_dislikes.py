"""Most-disliked analytics: per-video negative-comment overlay served by
/api/youtube/analytics, and the LLM comment-sentiment classifier. No network."""
import os
import tempfile
import unittest
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import pipeline.youtube as yt
import webapp.backend.main as backend


class NegativeCommentOverlayTests(unittest.TestCase):
    COMMENTS = [
        {"comment_id": "c1", "video_id": "v1", "sentiment": "negative"},
        {"comment_id": "c2", "video_id": "v1", "sentiment": "negative"},
        {"comment_id": "c3", "video_id": "v1", "sentiment": "positive"},
        {"comment_id": "c4", "video_id": "v2", "sentiment": "neutral"},
        {"comment_id": "c5", "video_id": "", "sentiment": "negative"},   # no video → ignored
        {"comment_id": "c6", "video_id": "v9"},                          # unstamped → ignored
    ]

    def test_overlay_counts_negative_threads_per_video(self):
        data = {"channel": {"name": "ch"}, "videos": [{"video_id": "v1"}, {"video_id": "v2"}]}
        with mock.patch.object(backend.yt, "load_comments_cache", return_value=self.COMMENTS):
            out = backend._overlay_negative_comments(data)
        self.assertEqual(out["videos"][0]["negative_comment_count"], 2)
        self.assertEqual(out["videos"][1]["negative_comment_count"], 0)

    def test_analytics_endpoint_overlays_cached_snapshot(self):
        snapshot = {"channel": {"name": "ch"}, "videos": [{"video_id": "v1"}]}
        with mock.patch.object(backend.yt, "load_analytics_cache", return_value={"ch1": snapshot}), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=self.COMMENTS):
            out = backend.yt_analytics(channel="ch1", refresh=False)
        self.assertEqual(out["videos"][0]["negative_comment_count"], 2)


class CommentSentimentTests(unittest.TestCase):
    def _classify(self, llm_reply):
        with mock.patch("pipeline.llm._chat_complete", return_value=llm_reply):
            return yt.comment_sentiment("some comment", {})

    def test_parses_each_label(self):
        self.assertEqual(self._classify("negative"), "negative")
        self.assertEqual(self._classify('"Positive"'), "positive")
        self.assertEqual(self._classify("Neutral."), "neutral")

    def test_empty_comment_is_neutral_without_llm(self):
        with mock.patch("pipeline.llm._chat_complete") as cc:
            self.assertEqual(yt.comment_sentiment("   ", {}), "neutral")
        cc.assert_not_called()

    def test_llm_failure_returns_blank_for_retry(self):
        with mock.patch("pipeline.llm._chat_complete", side_effect=RuntimeError("down")):
            self.assertEqual(yt.comment_sentiment("bad video", {}), "")
        self.assertEqual(self._classify("I cannot classify this"), "")


if __name__ == "__main__":
    unittest.main()
