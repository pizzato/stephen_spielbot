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
        # A channel-ful style folds in in-flight work + the channel back-catalog.
        cfg = {"default_style": "doc", "youtube_channels": [{"id": "ch1"}],
               "styles": [{"name": "doc", "channel": "ch1"}]}
        self.write(self.queue, [
            {"final_title": "Pending One", "status": "pending"},
            {"final_title": "Already Posted", "status": "posted"},  # comes via channel list, not inflight
        ])
        self.write(self.pq, [])
        with mock.patch.object(app, "_channel_video_titles", return_value=["Already Posted"]), \
             mock.patch.object(app, "_list_recent_jobs", return_value=[]):
            titles = backend._already_made_titles(cfg, "doc")
        self.assertIn("Pending One", titles)
        self.assertIn("Already Posted", titles)
        self.assertEqual(len(titles), len(set(t.lower() for t in titles)))  # deduped

    def test_channel_less_style_is_clean_slate(self):
        # A style with no channel must NOT inherit the first channel's catalog,
        # other styles' in-flight work, or recent local jobs (issue: new "Bands"
        # style pulled an unrelated channel's history via the publish fallback).
        cfg = {"default_style": "doc", "youtube_channels": [{"id": "ch1"}],
               "styles": [{"name": "doc", "channel": "ch1"}, {"name": "Bands", "channel": ""}]}
        self.write(self.queue, [{"final_title": "Other Inflight", "status": "pending"}])
        self.write(self.pq, [])
        self.write(self.suggestions, [{"id": "1", "title": "Doc Idea", "style_name": "doc"}])
        self.write(self.dismissed, {})
        with mock.patch.object(app, "_list_recent_jobs", return_value=[("Recent Job", "/x")]):
            bands = backend._already_made_titles(cfg, "Bands")
            doc = backend._already_made_titles(cfg, "doc")
        self.assertEqual(bands, [])  # clean slate — nothing from doc's channel
        # The channel-ful sibling still dedups against in-flight + recent work.
        self.assertIn("Other Inflight", doc)
        self.assertIn("Recent Job", doc)

    def test_channel_less_style_pools_open_ideas_only_with_itself(self):
        cfg = {"default_style": "doc",
               "youtube_channels": [{"id": "ch1"}],
               "styles": [{"name": "doc", "channel": "ch1"},
                          {"name": "history", "channel": "ch1"},
                          {"name": "Bands", "channel": ""}]}
        self.write(self.suggestions, [
            {"id": "1", "title": "Doc Idea", "style_name": "doc"},
            {"id": "2", "title": "Bands Idea", "style_name": "Bands"},
        ])
        self.write(self.dismissed, {})
        # Bands pools only with itself — not the first channel's styles…
        self.assertEqual(backend._styles_sharing_channel(cfg, "Bands"), {"Bands"})
        self.assertEqual(backend._existing_idea_titles(cfg, "Bands"), ["Bands Idea"])
        # …and the channel-ful styles no longer pull Bands in via the fallback.
        self.assertEqual(backend._styles_sharing_channel(cfg, "doc"), {"doc", "history"})

    # ── channel-pooled open ideas ────────────────────────────────────────────
    def test_existing_ideas_pooled_across_styles_on_same_channel(self):
        # doc + history publish to ch1; kids publishes to ch2.
        cfg = {
            "default_style": "doc",
            "youtube_channels": [{"id": "ch1"}, {"id": "ch2"}],
            "styles": [
                {"name": "doc", "channel": "ch1"},
                {"name": "history", "channel": "ch1"},
                {"name": "kids", "channel": "ch2"},
            ],
        }
        self.write(self.suggestions, [
            {"id": "1", "title": "Doc Idea", "style_name": "doc"},
            {"id": "2", "title": "History Idea", "style_name": "history"},
            {"id": "3", "title": "Kids Idea", "style_name": "kids"},
        ])
        self.write(self.dismissed, {})
        # Generating for doc sees its channel-mate history's open idea too...
        self.assertEqual(set(backend._existing_idea_titles(cfg, "doc")),
                         {"Doc Idea", "History Idea"})
        # ...but not the other channel's.
        self.assertEqual(backend._existing_idea_titles(cfg, "kids"), ["Kids Idea"])
        self.assertEqual(backend._styles_sharing_channel(cfg, "doc"), {"doc", "history"})

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

    # ── decline vs ignore ────────────────────────────────────────────────────
    def test_ignore_hidden_from_list_but_still_steers_llm(self):
        # Decline → reviewable "not accepted" list; Ignore → hidden for good and
        # never listed, yet both keep the topic out of future suggestions.
        self.write(self.suggestions, [
            {"id": "d1", "title": "Declined One", "style_name": "doc",
             "dismissed": True, "dismissed_reason": "declined"},
            {"id": "i1", "title": "Ignored One", "style_name": "doc",
             "dismissed": True, "dismissed_reason": "ignored"},
        ])
        self.write(self.dismissed, {})
        cfg = {"default_style": "doc"}
        # The reviewable list shows only the declined idea...
        self.assertEqual([r["title"] for r in backend._discarded_records(cfg)],
                         ["Declined One"])
        # ...but the LLM "do not suggest" list covers both, so neither resurfaces.
        self.assertEqual(set(backend._discarded_idea_titles(cfg)),
                         {"Declined One", "Ignored One"})

    # ── accepted list ────────────────────────────────────────────────────────
    def test_accept_lands_in_accepted_list_not_declined(self):
        backend.dismiss_suggestion(backend.SuggestionDismissBody(
            id="a1", title="Take Me", reason="accepted", size="medium"))
        cfg = {"default_style": "doc"}
        acc = backend._accepted_records(cfg)
        self.assertEqual([r["title"] for r in acc], ["Take Me"])
        self.assertEqual(acc[0]["size"], "medium")
        self.assertFalse(acc[0]["acted"])
        # Not in the Declined list, but still steers the LLM away.
        self.assertEqual(backend._discarded_records(cfg), [])
        self.assertEqual(backend._discarded_idea_titles(cfg), ["Take Me"])

    def test_act_marks_accepted_idea_but_keeps_it_listed(self):
        self.write(self.suggestions, [
            {"id": "a1", "title": "Make Me", "style_name": "doc",
             "dismissed": True, "dismissed_reason": "accepted"},
        ])
        self.write(self.dismissed, {
            "a1": {"id": "a1", "title": "Make Me", "reason": "accepted"},
        })
        with mock.patch.object(app, "load_config", return_value={"default_style": "doc"}):
            out = backend.act_on_accepted_suggestion(
                backend.SuggestionActBody(id="a1", title="Make Me", via="queue"))
        recs = out["accepted"]
        self.assertEqual([r["title"] for r in recs], ["Make Me"])
        self.assertTrue(recs[0]["acted"])
        self.assertEqual(recs[0]["acted_via"], "queue")

    def test_move_accepted_to_declined_resets_acted(self):
        self.write(self.suggestions, [
            {"id": "a1", "title": "Move Me", "style_name": "doc", "dismissed": True,
             "dismissed_reason": "accepted", "acted": True, "acted_via": "queue"},
        ])
        self.write(self.dismissed, {
            "a1": {"id": "a1", "title": "Move Me", "reason": "accepted", "acted": True},
        })
        backend.dismiss_suggestion(backend.SuggestionDismissBody(
            id="a1", title="Move Me", reason="declined"))
        cfg = {"default_style": "doc"}
        self.assertEqual(backend._accepted_records(cfg), [])
        dec = backend._discarded_records(cfg)
        self.assertEqual([r["title"] for r in dec], ["Move Me"])
        self.assertFalse(dec[0]["acted"])
        # Moving back to Accepted starts fresh — no stale acted marker.
        backend.dismiss_suggestion(backend.SuggestionDismissBody(
            id="a1", title="Move Me", reason="accepted"))
        acc = backend._accepted_records(cfg)
        self.assertEqual([r["title"] for r in acc], ["Move Me"])
        self.assertFalse(acc[0]["acted"])

    # ── reset the declined ("negative") list ─────────────────────────────────
    def test_reset_declined_clears_declined_keeps_ignored(self):
        self.write(self.suggestions, [
            {"id": "d1", "title": "Declined New", "dismissed": True, "dismissed_reason": "declined"},
            {"id": "d2", "title": "Legacy Closed", "dismissed": True, "dismissed_reason": "dismissed"},
            {"id": "i1", "title": "Ignored Topic", "dismissed": True, "dismissed_reason": "ignored"},
            {"id": "u1", "title": "Queued Topic", "used": True, "dismissed": True, "dismissed_reason": "used"},
            {"id": "a1", "title": "Active Topic"},
        ])
        self.write(self.dismissed, {
            "d1": {"id": "d1", "title": "Declined New", "reason": "declined"},
            "declined new": {"id": "d1", "title": "Declined New", "reason": "declined"},
            "i1": {"id": "i1", "title": "Ignored Topic", "reason": "ignored"},
            "ignored topic": {"id": "i1", "title": "Ignored Topic", "reason": "ignored"},
        })
        with mock.patch.object(app, "load_config", return_value={"default_style": "doc"}):
            out = backend.reset_declined_suggestions()
        self.assertEqual(out["cleared"], 2)  # both declined rows forgotten
        # Store keeps the ignored, queued, and active ideas; declined ones gone.
        store_titles = {s["title"] for s in json.loads(self.suggestions.read_text())}
        self.assertEqual(store_titles, {"Ignored Topic", "Queued Topic", "Active Topic"})
        # Dismissed log keeps only the ignored entries.
        self.assertEqual(set(json.loads(self.dismissed.read_text())), {"i1", "ignored topic"})
        # The reviewable list is now empty; the ignored topic still steers the LLM.
        cfg = {"default_style": "doc"}
        self.assertEqual(backend._discarded_records(cfg), [])
        self.assertEqual(backend._discarded_idea_titles(cfg), ["Ignored Topic"])

    def test_reset_declined_keeps_accepted(self):
        self.write(self.suggestions, [
            {"id": "d1", "title": "Declined One", "dismissed": True, "dismissed_reason": "declined"},
            {"id": "a1", "title": "Accepted One", "dismissed": True, "dismissed_reason": "accepted"},
        ])
        self.write(self.dismissed, {
            "d1": {"id": "d1", "title": "Declined One", "reason": "declined"},
            "a1": {"id": "a1", "title": "Accepted One", "reason": "accepted"},
        })
        with mock.patch.object(app, "load_config", return_value={"default_style": "doc"}):
            out = backend.reset_declined_suggestions()
        self.assertEqual(out["cleared"], 1)
        cfg = {"default_style": "doc"}
        self.assertEqual([r["title"] for r in backend._accepted_records(cfg)],
                         ["Accepted One"])


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


class SuggestionRepeatFilterCase(unittest.TestCase):
    """Idea generation must never re-propose an already-made video, even if the
    LLM ignores the prompt and returns one — a hard programmatic guarantee."""

    def test_generate_video_suggestions_drops_verbatim_repeats(self):
        import pipeline.llm as llm
        payload = json.dumps([
            {"title": "Already Made Video", "reason": "x", "interestingness": 0.8},
            {"title": "A Brand New Topic", "reason": "y", "interestingness": 0.9},
        ])
        with mock.patch.object(llm, "_local_llm", return_value=payload):
            # "already made video" differs only by case/space — still a repeat.
            out = llm.generate_video_suggestions(["Already  Made Video"], cfg={"llm_backend": "local"})
        self.assertEqual([s["title"] for s in out], ["A Brand New Topic"])

    def test_guided_suggestions_drops_verbatim_repeats(self):
        payload = json.dumps([
            {"title": "Existing One", "reason": "x", "suggested_scene_count": 10, "interestingness": 0.8},
            {"title": "Fresh Idea", "reason": "y", "suggested_scene_count": 12, "interestingness": 0.9},
        ])
        with mock.patch.object(backend, "_llm_complete", return_value=payload):
            out = backend._guided_suggestions("rock bands", ["existing one"], {"llm_backend": "local"})
        self.assertEqual([s["title"] for s in out], ["Fresh Idea"])


if __name__ == "__main__":
    unittest.main()
