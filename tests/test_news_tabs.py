"""Separate news ideas from topic ideas and explain empty news checks."""
import os
from unittest import mock

import app
from pipeline import news, youtube as yt
from test_news_integration import POST, _idea
from test_styles import TempConfigCase, _style
from webapp.backend import main as backend, news_monitor


class NewsTabTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        for target, attr, filename in (
            (yt, "SUGGESTIONS_PATH", "suggestions.json"),
            (yt, "QUEUE_PATH", "queue.json"),
            (backend, "DISMISSED_SUGGESTIONS_FILE", "dismissed.json"),
        ):
            patcher = mock.patch.object(target, attr, self.config_file.parent / filename)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write_config({
            "styles": [
                _style("News", news_monitor={"enabled": True, "query": "Australian politics"}),
                _style("Excluded", auto_pick_exclude=True),
            ],
            "default_style": "News", "news": {"x_bearer_token": "test-token"},
        })

    def _saved_news(self, **overrides):
        return {
            "id": "news-101", "title": "A topical song", "reason": "The story",
            "source": "news", "style_name": "News",
            "news": {"summary": "Reported news", "sources": [POST]},
            **overrides,
        }

    def _check(self, *, posts=None, generated=None):
        fetched = [POST] if posts is None else posts
        with mock.patch.object(news, "fetch_recent_posts", return_value={
            "posts": fetched, "newest_id": fetched[-1]["id"] if fetched else "",
        }), mock.patch.object(news, "generate_news_ideas", return_value=(
            [_idea()] if generated is None else generated
        )):
            return news_monitor.check(app.load_config(), "News", force=True)

    def test_news_endpoint_is_cached_only_and_filters_source_style_and_visibility(self):
        yt.save_suggestions([
            self._saved_news(),
            self._saved_news(id="other-news", style_name="Excluded"),
            self._saved_news(id="accepted", used=True, dismissed=True),
            self._saved_news(id="declined", dismissed=True),
            self._saved_news(id="topic", source="ai", title="Evergreen topic"),
        ])
        with mock.patch.object(news, "fetch_recent_posts") as fetch, \
                mock.patch.object(news, "generate_news_ideas") as news_generate, \
                mock.patch.object(backend, "generate_video_suggestions") as topic_generate:
            result = backend.news_ideas(style_name="News")
        self.assertEqual([s["id"] for s in result["suggestions"]], ["news-101"])
        self.assertEqual(result["suggestions"][0]["news"]["sources"], [POST])
        fetch.assert_not_called()
        news_generate.assert_not_called()
        topic_generate.assert_not_called()

    def test_all_news_includes_styles_excluded_from_topic_rotation(self):
        yt.save_suggestions([self._saved_news(), self._saved_news(
            id="excluded-news", style_name="Excluded")])
        result = backend.news_ideas(style_name="__all__")
        self.assertEqual({s["id"] for s in result["suggestions"]}, {"news-101", "excluded-news"})

    def test_empty_news_endpoint_does_not_trigger_provider_calls(self):
        with mock.patch.object(news, "fetch_recent_posts") as fetch, \
                mock.patch.object(news, "generate_news_ideas") as generate:
            self.assertEqual(backend.news_ideas(style_name="News")["suggestions"], [])
        fetch.assert_not_called()
        generate.assert_not_called()

    def test_topic_cache_never_includes_news(self):
        yt.save_suggestions([
            self._saved_news(),
            {"id": "topic", "title": "An evergreen idea", "style_name": "News", "source": "ai"},
        ])
        for target in ("News", "__all__"):
            with self.subTest(style_name=target):
                result = backend.youtube_suggestions(guidance="", refresh=False, style_name=target)
                self.assertEqual([s["id"] for s in result["suggestions"]], ["topic"])

    def test_empty_topic_cache_still_generates_when_news_monitor_is_enabled(self):
        for target in ("News", "__all__"):
            with self.subTest(style_name=target):
                saved = self._saved_news()
                yt.save_suggestions([saved])
                with mock.patch.object(backend, "generate_video_suggestions", return_value=[{
                    "id": "topic", "title": "A fresh topic", "source": "ai",
                }]) as generate, mock.patch.object(app, "_channel_video_titles", return_value=[]):
                    result = backend.youtube_suggestions(guidance="", refresh=False, style_name=target)
                generate.assert_called_once()
                self.assertEqual([s["id"] for s in result["suggestions"]], ["topic"])
                self.assertEqual(next(s for s in yt.load_suggestions() if s["id"] == saved["id"]), saved)

    def test_refresh_preserves_news_and_topic_with_same_title(self):
        for target in ("News", "__all__"):
            with self.subTest(style_name=target):
                saved = self._saved_news()
                excluded = self._saved_news(id="excluded-news", style_name="Excluded")
                yt.save_suggestions([saved, excluded])
                with mock.patch.object(backend, "generate_video_suggestions", return_value=[{
                    "id": "topic", "title": saved["title"], "source": "ai",
                }]), mock.patch.object(app, "_channel_video_titles", return_value=[]):
                    result = backend.youtube_suggestions(guidance="", refresh=True, style_name=target)
                self.assertEqual([s["id"] for s in result["suggestions"]], ["topic"])
                stored = {s["id"]: s for s in yt.load_suggestions()}
                self.assertEqual(set(stored), {"news-101", "excluded-news", "topic"})
                self.assertEqual(stored["news-101"], saved)
                self.assertEqual(stored["excluded-news"], excluded)

    def test_accepted_and_declined_news_keep_source_for_tab_filtering(self):
        yt.save_suggestions([self._saved_news(), self._saved_news(id="news-declined", title="Other news")])
        backend.dismiss_suggestion(backend.SuggestionDismissBody(id="news-101", reason="accepted"))
        backend.dismiss_suggestion(backend.SuggestionDismissBody(id="news-declined", reason="declined"))
        accepted = backend.accepted_suggestions(style_name="News")["accepted"]
        declined = backend.discarded_suggestions(style_name="News")["discarded"]
        for records, expected in ((accepted, "news-101"), (declined, "news-declined")):
            self.assertEqual([s["id"] for s in records], [expected])
            self.assertEqual(records[0]["source"], "news")
            self.assertEqual(records[0]["news"]["sources"], [POST])
        self.assertEqual(backend.news_ideas(style_name="News")["suggestions"], [])

    def test_reviewing_news_does_not_hide_same_title_topic(self):
        for reason in ("accepted", "declined"):
            with self.subTest(reason=reason):
                saved = self._saved_news()
                topic = {"id": "topic", "title": saved["title"], "style_name": "News", "source": "ai"}
                yt.save_suggestions([saved, topic])
                backend._save_dismissed_suggestions({})
                backend.dismiss_suggestion(backend.SuggestionDismissBody(
                    id=saved["id"], title=saved["title"], reason=reason))
                result = backend.youtube_suggestions(guidance="", refresh=False, style_name="News")
                self.assertEqual([s["id"] for s in result["suggestions"]], ["topic"])
                self.assertEqual(set(backend._load_dismissed_suggestions()), {saved["id"]})
                self.assertEqual(backend.news_ideas(style_name="News")["suggestions"], [])

    def test_legacy_news_title_dismissal_does_not_hide_topic(self):
        saved = self._saved_news()
        yt.save_suggestions([saved, {
            "id": "topic", "title": saved["title"], "style_name": "News", "source": "ai",
        }])
        dismissed = {**saved, "reason": "accepted"}
        backend._save_dismissed_suggestions({
            saved["id"]: dismissed, backend._suggestion_key(saved["title"]): dismissed,
        })
        result = backend.youtube_suggestions(guidance="", refresh=False, style_name="News")
        self.assertEqual([s["id"] for s in result["suggestions"]], ["topic"])
        self.assertEqual(backend.news_ideas(style_name="News")["suggestions"], [])

    def test_zero_posts_is_distinct_from_no_relevant_ideas(self):
        result = self._check(posts=[])
        row = result["styles"][0]
        self.assertEqual((row["posts_fetched"], row["posts_new"], row["ideas_added"]), (0, 0, 0))
        self.assertEqual(row["last_outcome"], "no_posts")
        result = self._check(generated=[])
        row = result["styles"][0]
        self.assertEqual((row["posts_fetched"], row["posts_new"], row["ideas_added"]), (1, 1, 0))
        self.assertEqual(row["last_outcome"], "no_ideas")

    def test_success_then_repost_explains_no_new_posts(self):
        result = self._check()
        row = result["styles"][0]
        self.assertEqual((row["posts_fetched"], row["posts_new"], row["ideas_added"]), (1, 1, 1))
        self.assertEqual(row["last_outcome"], "ideas_added")
        result = self._check(posts=[POST, {**POST, "id": "102"}])
        row = result["styles"][0]
        self.assertEqual((row["posts_fetched"], row["posts_new"], row["ideas_added"]), (2, 0, 0))
        self.assertEqual(row["last_outcome"], "no_new_posts")
        self.assertEqual(len(yt.load_suggestions()), 1)

    def test_existing_source_idea_reports_duplicates_after_cursor_retry(self):
        self._check()
        news_monitor._save_state({})
        result = self._check(generated=[_idea(title="New wording for the same source")])
        row = result["styles"][0]
        self.assertEqual((row["posts_fetched"], row["posts_new"], row["ideas_added"]), (1, 1, 0))
        self.assertEqual(row["last_outcome"], "duplicates")
        self.assertEqual(len(yt.load_suggestions()), 1)

    def test_query_edit_fetches_new_topic_without_previous_cursor(self):
        self._check()
        cfg = self.read_config()
        cfg["styles"][0]["news_monitor"]["query"] = "Trump"
        self.write_config(cfg)
        new_post = {**POST, "id": "202", "url": "https://x.com/news/status/202",
                    "text": "Trump announced a new policy.", "article_urls": ["https://example.org/new-policy"]}
        with mock.patch.object(news, "fetch_recent_posts", return_value={
            "posts": [new_post], "newest_id": "202",
        }) as fetch, mock.patch.object(news, "generate_news_ideas", return_value=[_idea(
            title="A song about the policy", source_ids=["202"], sources=[new_post],
        )]) as generate:
            result = news_monitor.check(app.load_config(), "News")
        self.assertEqual(fetch.call_args.args[1], "Trump")
        self.assertIsNone(fetch.call_args.kwargs["since_id"])
        self.assertEqual(generate.call_args.args[0], [new_post])
        self.assertEqual(result["ideas_added"], 1)
        state = news_monitor._read_state()["News"]
        self.assertEqual((state["query"], state["since_id"], state["last_outcome"]),
                         ("Trump", "202", "ideas_added"))

    def test_provider_error_is_not_reported_as_empty_success(self):
        message = "X news search failed (HTTP 403). Check recent-search access."
        with mock.patch.object(news, "fetch_recent_posts", side_effect=news.NewsError(message)):
            result = news_monitor.check(app.load_config(), "News", force=True)
        row = result["styles"][0]
        self.assertEqual(row["last_outcome"], "error")
        self.assertEqual(row["last_error"], message)
        self.assertFalse(row["last_success"])
        self.assertEqual(result["ideas_added"], 0)

    def test_llm_failure_preserves_cursor_and_fetched_counts_without_leaking_exception(self):
        news_monitor._save_state({"News": {"query": "Australian politics", "since_id": "99"}})
        with mock.patch.object(news, "fetch_recent_posts", return_value={"posts": [POST], "newest_id": "101"}), \
                mock.patch.object(news, "generate_news_ideas", side_effect=RuntimeError("credential-secret")):
            result = news_monitor.check(app.load_config(), "News", force=True)
        row = result["styles"][0]
        self.assertEqual((row["posts_fetched"], row["posts_new"], row["ideas_added"]), (1, 1, 0))
        self.assertEqual(row["last_outcome"], "error")
        self.assertNotIn("credential-secret", row["last_error"])
        self.assertTrue(row["last_error"])
        self.assertFalse(row["last_success"])
        self.assertEqual(news_monitor._read_state()["News"]["since_id"], "99")

    def test_background_disabled_status_matches_preview_server(self):
        with mock.patch.dict(os.environ, {"SPIELBOT_NO_BACKGROUND": "1"}):
            self.assertFalse(news_monitor.status(app.load_config())["background_enabled"])
