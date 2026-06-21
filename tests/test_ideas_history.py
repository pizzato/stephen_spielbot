"""Idea generation now dedups against everything already in flight and against
ideas the user threw away.

Two lists go to the LLM: (1) videos already made/queued/awaiting-publish and
still-open ideas, and (2) topics deliberately discarded. Discards are revivable
or forgettable, and queued/created ideas (reason="used") must never be mistaken
for discards. The published-title list is cached so generation doesn't re-hit
the YouTube API on every call.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-ideas-home-"))

import app
import pipeline.youtube as yt
import webapp.backend.main as backend


class IdeaHistoryCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-ideas-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.suggestions = tmp / "youtube_suggestions.json"
        self.dismissed = tmp / "youtube_dismissed_suggestions.json"
        self.queue = tmp / "youtube_queue.json"
        self.pq = tmp / "publish_queue.json"
        self.titles_cache = tmp / "youtube_video_titles.json"
        for target, attr, value in [
            (yt, "SUGGESTIONS_PATH", self.suggestions),
            (yt, "QUEUE_PATH", self.queue),
            (yt, "VIDEO_TITLES_CACHE_PATH", self.titles_cache),
            (backend.pq, "PUBLISH_QUEUE_PATH", self.pq),
            (backend, "DISMISSED_SUGGESTIONS_FILE", self.dismissed),
        ]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def write(self, path: Path, data) -> None:
        path.write_text(json.dumps(data))

    # ── discarded records ────────────────────────────────────────────────────
    def test_discarded_excludes_used_and_dedups(self):
        # One real discard (Close), one "used" (Queue/Create). Only the discard counts.
        self.write(self.suggestions, [
            {"id": "a1", "title": "Lost City of Petra", "reason": "gap", "style_name": "doc",
             "dismissed": True, "dismissed_reason": "dismissed"},
            {"id": "b2", "title": "Queued Topic", "dismissed": True, "dismissed_reason": "used"},
        ])
        # Dismissed log keys the same discard under id AND normalized title.
        self.write(self.dismissed, {
            "a1": {"id": "a1", "title": "Lost City of Petra", "reason": "dismissed"},
            "lost city of petra": {"id": "a1", "title": "Lost City of Petra", "reason": "dismissed"},
        })
        recs = backend._discarded_records({"default_style": "doc"})
        self.assertEqual([r["title"] for r in recs], ["Lost City of Petra"])
        self.assertEqual(backend._discarded_idea_titles({"default_style": "doc"}),
                         ["Lost City of Petra"])

    def test_discarded_style_filter(self):
        self.write(self.suggestions, [
            {"id": "a1", "title": "A", "style_name": "doc", "dismissed": True, "dismissed_reason": "dismissed"},
            {"id": "b2", "title": "B", "style_name": "kids", "dismissed": True, "dismissed_reason": "dismissed"},
        ])
        self.write(self.dismissed, {})
        doc = backend._discarded_records({"default_style": "doc"}, target="doc")
        self.assertEqual([r["title"] for r in doc], ["A"])

    # ── already-made list ────────────────────────────────────────────────────
    def test_already_made_includes_pending_queue(self):
        self.write(self.queue, [
            {"final_title": "Pending One", "status": "pending"},
            {"final_title": "Already Posted", "status": "posted"},  # comes via channel list, not inflight
        ])
        self.write(self.pq, [])
        with mock.patch.object(app, "_channel_video_titles", return_value=["Already Posted"]), \
             mock.patch.object(app, "_list_recent_jobs", return_value=[]):
            titles = backend._already_made_titles({"default_style": "doc"}, "doc")
        self.assertIn("Pending One", titles)
        self.assertIn("Already Posted", titles)
        self.assertEqual(len(titles), len(set(t.lower() for t in titles)))  # deduped

    # ── revive / forget ──────────────────────────────────────────────────────
    def test_revive_unhides_and_clears_log(self):
        self.write(self.suggestions, [
            {"id": "a1", "title": "Revive Me", "style_name": "doc",
             "used": True, "dismissed": True, "dismissed_reason": "dismissed"},
        ])
        self.write(self.dismissed, {
            "a1": {"id": "a1", "title": "Revive Me", "reason": "dismissed"},
            "revive me": {"id": "a1", "title": "Revive Me", "reason": "dismissed"},
        })
        backend.revive_suggestion(backend.SuggestionReviveBody(id="a1", title="Revive Me"))
        store = json.loads(self.suggestions.read_text())
        self.assertFalse(store[0]["used"])
        self.assertFalse(store[0]["dismissed"])
        self.assertEqual(json.loads(self.dismissed.read_text()), {})
        # No longer treated as a discard.
        self.assertEqual(backend._discarded_idea_titles({"default_style": "doc"}), [])

    def test_forget_removes_everywhere(self):
        self.write(self.suggestions, [
            {"id": "a1", "title": "Forget Me", "dismissed": True, "dismissed_reason": "dismissed"},
        ])
        self.write(self.dismissed, {
            "a1": {"id": "a1", "title": "Forget Me", "reason": "dismissed"},
            "forget me": {"id": "a1", "title": "Forget Me", "reason": "dismissed"},
        })
        with mock.patch.object(app, "load_config", return_value={"default_style": "doc"}):
            backend.forget_suggestion(backend.SuggestionReviveBody(id="a1", title="Forget Me"))
        self.assertEqual(json.loads(self.suggestions.read_text()), [])
        self.assertEqual(json.loads(self.dismissed.read_text()), {})


class VideoTitlesCacheCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-titles-")
        self.addCleanup(self._tmp.cleanup)
        cache = Path(self._tmp.name) / "youtube_video_titles.json"
        p = mock.patch.object(yt, "VIDEO_TITLES_CACHE_PATH", cache)
        p.start()
        self.addCleanup(p.stop)

    def test_cache_avoids_refetch_within_ttl(self):
        with mock.patch.object(yt, "fetch_channel_video_titles", return_value=["X", "Y"]) as f:
            a = yt.cached_channel_video_titles("secrets", channel="c1")
            b = yt.cached_channel_video_titles("secrets", channel="c1")
        self.assertEqual(a, ["X", "Y"])
        self.assertEqual(b, ["X", "Y"])
        self.assertEqual(f.call_count, 1)  # second call served from cache

    def test_force_refetches(self):
        with mock.patch.object(yt, "fetch_channel_video_titles", return_value=["X"]) as f:
            yt.cached_channel_video_titles("secrets", channel="c1")
            yt.cached_channel_video_titles("secrets", channel="c1", force=True)
        self.assertEqual(f.call_count, 2)

    def test_falls_back_to_stale_on_empty_fetch(self):
        with mock.patch.object(yt, "fetch_channel_video_titles", return_value=["X", "Y"]):
            yt.cached_channel_video_titles("secrets", channel="c1")
        # A later fetch that fails (returns []) must not wipe the cached list.
        with mock.patch.object(yt, "fetch_channel_video_titles", return_value=[]):
            out = yt.cached_channel_video_titles("secrets", channel="c1", force=True)
        self.assertEqual(out, ["X", "Y"])


if __name__ == "__main__":
    unittest.main()
