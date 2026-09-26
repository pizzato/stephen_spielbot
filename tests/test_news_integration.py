"""Durable per-style news polling, queue handoff, and script reference flow."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

import app
from pipeline import news, youtube as yt
from pipeline.llm import Scene
from test_styles import TempConfigCase, _style
from scriptstub import stub_script
from webapp.backend import main as backend, news_monitor


POST = {
    "id": "101", "url": "https://x.com/news/status/101",
    "text": "Alex Example announced a new music program.", "author": "news",
    "created_at": "2026-09-26T01:00:00Z",
    "article_urls": ["https://example.org/story"],
}


def _idea(**overrides):
    return {
        "title": "A song for the new music program", "reason": "A topical song premise",
        "summary": "An X post reports Alex Example announced a music program.",
        "source_ids": ["101"], "sources": [POST],
        "people": [{"name": "Alex Example", "description": "Named news subject"}],
        **overrides,
    }


class NewsIntegrationTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        for attr, value in (("SUGGESTIONS_PATH", self.config_file.parent / "suggestions.json"),
                            ("QUEUE_PATH", self.config_file.parent / "queue.json")):
            patcher = mock.patch.object(yt, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        dismissed = mock.patch.object(backend, "DISMISSED_SUGGESTIONS_FILE",
                                      self.config_file.parent / "dismissed.json")
        dismissed.start()
        self.addCleanup(dismissed.stop)

    def _config(self, **monitor):
        self.write_config({
            "styles": [_style("News", news_monitor={"enabled": True,
                "query": "music lang:en", "include_people": True, **monitor})],
            "default_style": "News", "news": {"x_bearer_token": "test-token"},
        })
        return app.load_config()

    def _check(self, cfg, *, generated=None, fetched=None, force=True):
        with mock.patch.object(news, "fetch_recent_posts", return_value=fetched or {"posts": [POST], "newest_id": "101"}), \
                mock.patch.object(news, "generate_news_ideas", return_value=[_idea()] if generated is None else generated):
            return news_monitor.check(cfg, force=force)

    def test_disabled_monitor_makes_no_provider_calls(self):
        cfg = self._config(enabled=False)
        with mock.patch.object(news, "fetch_recent_posts") as fetch, \
                mock.patch.object(news, "generate_news_ideas") as generate:
            result = news_monitor.check(cfg, force=True)
        self.assertEqual(result["ideas_added"], 0)
        self.assertFalse(news_monitor.enabled(cfg))
        fetch.assert_not_called()
        generate.assert_not_called()

    def test_child_inherits_monitor_but_unrelated_root_stays_disabled(self):
        self.write_config({"styles": [
            _style("News", news_monitor={"enabled": True, "query": "music"}),
            {"name": "Child", "parent": "News"}, _style("Other"),
            {"name": "Off", "parent": "News", "news_monitor": {"enabled": False}},
        ], "default_style": "News"})
        cfg = app.load_config()
        statuses = {s["style_name"]: s for s in news_monitor.status(cfg)["styles"]}
        self.assertTrue(statuses["Child"]["enabled"])
        self.assertEqual(statuses["Child"]["query"], "music")
        self.assertFalse(statuses["Other"]["enabled"])
        self.assertFalse(statuses["Off"]["enabled"])
        self.assertFalse(news.normalize_monitor(app.style_settings(cfg, app.NO_STYLE)["news_monitor"])["enabled"])

    def test_success_persists_cursor_and_source_before_next_poll(self):
        cfg = self._config()
        result = self._check(cfg)
        self.assertEqual(result["ideas_added"], 1)
        state = news_monitor._read_state()["News"]
        self.assertEqual(state["since_id"], "101")
        self.assertEqual(state["seen_posts"], ["101"])
        self.assertEqual(state["seen_articles"], POST["article_urls"])
        self.assertEqual(state["last_error"], "")
        saved = yt.load_suggestions()[0]
        self.assertEqual(saved["style_name"], "News")
        self.assertEqual(saved["news"]["sources"], [POST])
        self.assertTrue(saved["news"]["include_people"])
        with mock.patch.object(news, "fetch_recent_posts") as fetch:
            result = news_monitor.check(cfg)
        fetch.assert_not_called()
        self.assertEqual(result["ideas_added"], 0)

    def test_generation_failure_preserves_cursor_and_throttles_retry(self):
        cfg = self._config()
        news_monitor._save_state({"News": {"query": "music lang:en", "since_id": "99"}})
        with mock.patch.object(news, "fetch_recent_posts", return_value={"posts": [POST], "newest_id": "101"}) as fetch, \
                mock.patch.object(news, "generate_news_ideas", side_effect=RuntimeError("credential-secret")):
            result = news_monitor.check(cfg, force=True)
        self.assertEqual(fetch.call_args.kwargs["since_id"], "99")
        self.assertEqual(result["ideas_added"], 0)
        state = news_monitor._read_state()["News"]
        self.assertEqual(state["since_id"], "99")
        self.assertNotIn("credential-secret", state["last_error"])
        self.assertTrue(state["last_error"])
        with mock.patch.object(news, "fetch_recent_posts") as retry:
            news_monitor.check(cfg)
        retry.assert_not_called()

    def test_suggestion_write_failure_does_not_advance_cursor(self):
        cfg = self._config()
        news_monitor._save_state({"News": {"query": "music lang:en", "since_id": "99"}})
        with mock.patch.object(yt, "save_suggestions", side_effect=OSError("disk full")):
            self._check(cfg)
        state = news_monitor._read_state()["News"]
        self.assertEqual(state["since_id"], "99")
        self.assertEqual(state["ideas_added"], 0)
        self.assertNotIn("last_success", state)
        self.assertEqual(yt.load_suggestions(), [])

    def test_safe_provider_error_remains_actionable(self):
        cfg = self._config()
        message = "X news search failed (HTTP 403). Check recent-search access."
        with mock.patch.object(news, "fetch_recent_posts", side_effect=news.NewsError(message)):
            result = news_monitor.check(cfg, force=True)
        self.assertEqual(result["styles"][0]["last_error"], message)

    def test_query_change_starts_without_old_cursor(self):
        cfg = self._config(query="different topic")
        news_monitor._save_state({"News": {"query": "old topic", "since_id": "99"}})
        with mock.patch.object(news, "fetch_recent_posts", return_value={"posts": [], "newest_id": ""}) as fetch:
            news_monitor.check(cfg, force=True)
        self.assertIsNone(fetch.call_args.kwargs["since_id"])
        self.assertEqual(news_monitor._read_state()["News"]["query"], "different topic")

    def test_seen_post_and_article_reposts_do_not_generate_repeat_ideas(self):
        cfg = self._config()
        self._check(cfg)
        repost = {**POST, "id": "102"}
        with mock.patch.object(news, "fetch_recent_posts", return_value={"posts": [POST, repost], "newest_id": "102"}), \
                mock.patch.object(news, "generate_news_ideas") as generate:
            result = news_monitor.check(cfg, force=True)
        generate.assert_not_called()
        self.assertEqual(result["ideas_added"], 0)
        self.assertEqual(len(yt.load_suggestions()), 1)
        self.assertEqual(news_monitor._read_state()["News"]["since_id"], "102")

    def test_retry_after_suggestions_saved_deduplicates_by_source_identity(self):
        cfg = self._config()
        self._check(cfg)
        news_monitor._save_state({})  # simulate crash before durable cursor update
        result = self._check(cfg, generated=[_idea(title="A different proposed title for this same report")])
        self.assertEqual(result["ideas_added"], 0)
        self.assertEqual(len(yt.load_suggestions()), 1)

    def test_manual_check_cannot_overlap_scheduled_poll(self):
        cfg = self._config()
        started, release = threading.Event(), threading.Event()

        def fetch(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(5), "test did not release first poll")
            return {"posts": [POST], "newest_id": "101"}

        with mock.patch.object(news, "fetch_recent_posts", side_effect=fetch) as provider, \
                mock.patch.object(news, "generate_news_ideas", return_value=[_idea()]), \
                ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(news_monitor.check, cfg)
            try:
                self.assertTrue(started.wait(5))
                duplicate = news_monitor.check(cfg, force=True)
                self.assertTrue(duplicate["running"])
                self.assertEqual(duplicate["ideas_added"], 0)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5)["ideas_added"], 1)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(len(yt.load_suggestions()), 1)

    def test_auto_queue_requires_acceptance_and_only_enqueues_once(self):
        cfg = self._config(auto_accept=False, auto_queue=True)
        self._check(cfg)
        self.assertEqual(yt.load_queue(), [])
        ideas = yt.load_suggestions()
        ideas[0].update(used=True, dismissed=True, dismissed_reason="accepted")
        yt.save_suggestions(ideas)
        self._check(cfg, fetched={"posts": [], "newest_id": "101"})
        self._check(cfg, fetched={"posts": [], "newest_id": "101"})
        queue = yt.load_queue()
        self.assertEqual(len(queue), 1)
        self.assertFalse(queue[0]["approved"])
        self.assertEqual(queue[0]["source"], "news")
        self.assertEqual(queue[0]["gen_style_name"], "News")
        self.assertEqual(queue[0]["news"]["sources"], [POST])
        self.assertTrue(yt.load_suggestions()[0]["acted"])

    def test_auto_accept_and_queue_save_reviewable_slot(self):
        cfg = self._config(auto_accept=True, auto_queue=True)
        self._check(cfg)
        self.assertEqual(len(yt.load_queue()), 1)
        self.assertFalse(yt.load_queue()[0]["approved"])
        self.assertEqual(yt.load_suggestions()[0]["dismissed_reason"], "accepted")

    def test_queue_snapshot_survives_idea_deletion_and_rejects_wrong_style(self):
        cfg = self._config()
        self._check(cfg)
        idea = yt.load_suggestions()[0]
        queued = news_monitor.queue_idea(idea, cfg)
        self.assertEqual(news_monitor.queue_idea(idea, cfg)["id"], queued["id"])
        yt.save_suggestions([])
        source = news_monitor.source_for(idea["id"], queued["id"], "News")
        self.assertEqual(source, idea["news"])
        with self.assertRaises(HTTPException) as raised:
            news_monitor.source_for("", queued["id"], "Other")
        self.assertEqual(raised.exception.status_code, 400)

    def test_source_brief_retains_post_claims_and_does_not_duplicate_on_redraft(self):
        source = {"summary": "The report", "sources": [POST], "people": []}
        topic = news_monitor.topic_with_sources("Write a song", source)
        self.assertIn(POST["text"], topic)
        self.assertIn("not verified facts", topic)
        self.assertIn("ignore instructions", topic)
        self.assertEqual(news_monitor.topic_with_sources(topic, source), topic)
        self.assertEqual(news_monitor.topic_with_sources("Write a song", {}), "Write a song")

    def test_attached_portrait_enters_renderer_and_survives_script_copy(self):
        cfg = self._config()
        wd = self.output_dir / "news-video"
        wd.mkdir()
        (wd / "characters").mkdir()
        photo = wd / "characters" / "news_Q123.png"
        photo.write_bytes(b"verified portrait fixture")
        report = {"characters": [{"id": "news_Q123", "name": "Alex Example",
                   "description": "Verified source portrait", "ref_image": photo.name}],
                  "references": [{"name": "Alex Example", "license": "CC BY-SA 4.0"}], "unresolved": []}

        def resolved(*args):
            (wd / "news_people.json").write_text(json.dumps(report))
            return report

        source = {"summary": "The report", "sources": [POST], "include_people": True,
                  "people": [{"name": "Alex Example"}]}
        with mock.patch("pipeline.news_people.resolve_people", side_effect=resolved) as resolve:
            news_monitor.attach_source(wd, source)
            news_monitor.attach_source(wd, source)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(app._scene_reference_images("Alex Example at a podium", {}, cfg, "News", wd), [photo])
        copy = self.output_dir / "news-copy"
        backend._copy_script_reference_files(wd, copy, keep_scene_scope=False)
        self.assertEqual(json.loads((copy / "news_source.json").read_text()), source)
        self.assertEqual(json.loads((copy / "news_people.json").read_text()), report)
        self.assertEqual(app._scene_reference_images("Alex Example at a podium", {}, cfg, "News", copy),
                         [copy / "characters" / photo.name])

    def test_selected_news_idea_reaches_story_and_saved_script(self):
        cfg = self._config(include_people=False)
        self._check(cfg)
        idea = yt.load_suggestions()[0]
        scene = Scene(id=1, title="The announcement", narration="The reported news.",
                      image_prompt="A music studio", video_prompt="Slow pan")
        with stub_script([scene]) as (draft, divide), \
                mock.patch.object(backend.threading.Thread, "start"):
            result = backend._do_script_generate(backend.GenerateScriptBody(
                video_title=idea["title"], topic=idea["reason"], idea_id=idea["id"],
                style_name="News", auto_critic=False))
        self.assertIn(POST["text"], draft.call_args.args[0])
        self.assertIn("NEWS SOURCE MATERIAL", result["topic"])
        wd = Path(result["work_dir"])
        self.assertEqual(json.loads((wd / "news_source.json").read_text()), idea["news"])
        divide.assert_called_once()

    def test_queued_news_snapshot_reaches_song_lyrics_after_idea_is_deleted(self):
        cfg = self._config(include_people=False)
        self._check(cfg)
        idea = yt.load_suggestions()[0]
        queued = news_monitor.queue_idea(idea, cfg)
        yt.save_suggestions([])
        with mock.patch.object(backend, "_pick_song_singer", return_value=({}, "")), \
                mock.patch.object(backend.threading.Thread, "start"), \
                mock.patch.object(backend.story_mode, "write_song", return_value={
                    "caption": "A folk song", "lyrics": "[Verse]\nSing the news"}) as write:
            result = backend.song_draft(backend.SongDraftBody(
                video_title=idea["title"], topic=idea["reason"],
                queue_item_id=queued["id"], style_name="News", minutes=0.5))
        self.assertIn(POST["text"], write.call_args.kwargs["topic"])
        self.assertIn("creative lyrics/satire", write.call_args.kwargs["topic"])
        wd = Path(result["work_dir"])
        self.assertEqual(json.loads((wd / "news_source.json").read_text()), idea["news"])
        self.assertEqual(json.loads((wd / "song.json").read_text())["lyrics"], "[Verse]\nSing the news")
        self.assertEqual(result["create_brief"]["queue_item_id"], queued["id"])

    def test_queue_render_handoff_uses_news_snapshot(self):
        cfg = self._config(include_people=False)
        self._check(cfg)
        idea = yt.load_suggestions()[0]
        queued = news_monitor.queue_idea(idea, cfg)
        yt.save_suggestions([])
        raw = self.read_config()
        raw["styles"][0]["automation"] = {"auto_critic": False}
        self.write_config(raw)
        scene = Scene(id=1, title="The announcement", narration="The reported news.",
                      image_prompt="A music studio", video_prompt="Slow pan")
        with stub_script([scene]) as (draft, _), \
                mock.patch.object(backend.threading.Thread, "start"), \
                mock.patch.object(backend, "start_generation"):
            result = backend._start_queue_item(queued)
        self.assertIn(POST["text"], draft.call_args.args[0])
        self.assertEqual(json.loads((Path(result["work_dir"]) / "news_source.json").read_text()), idea["news"])
        self.assertEqual(len(yt.load_queue()), 1)

    def test_x_credential_is_redacted_and_preserved_on_blank_update(self):
        cfg = self._config()
        public = app.public_config(cfg)
        self.assertEqual(public["news"]["x_bearer_token"], "")
        self.assertTrue(public["news"]["x_bearer_token_set"])
        merged = app.merge_config_update(cfg, {"news": {"x_bearer_token": ""}})
        self.assertEqual(merged["news"]["x_bearer_token"], "test-token")
        self.assertNotIn("news", app._job_config_snapshot(cfg))

    def test_same_title_news_ideas_stay_separate_through_accept_act_and_revive(self):
        self._config()
        raw = self.read_config()
        raw["styles"].append(_style("Other"))
        self.write_config(raw)
        title = "Same headline, different channel"
        records = [{"id": "news-a", "style_name": "News", "source": "news", "title": title,
                    "reason": "A song", "news": {"summary": "First style", "sources": [POST]}},
                   {"id": "news-b", "style_name": "Other", "source": "news", "title": title,
                    "reason": "A documentary", "news": {"summary": "Other style", "sources": [POST]}}]
        yt.save_suggestions(records)
        first = backend.dismiss_suggestion(backend.SuggestionDismissBody(
            id="news-a", title=title, reason="accepted", size="large"))
        self.assertEqual([s["id"] for s in first["suggestions"]], ["news-b"])
        backend.dismiss_suggestion(backend.SuggestionDismissBody(id="news-b", title=title, reason="accepted"))
        accepted = backend.accepted_suggestions(style_name="__all__")["accepted"]
        self.assertEqual({s["id"] for s in accepted}, {"news-a", "news-b"})
        self.assertEqual({s["style_name"] for s in accepted}, {"News", "Other"})
        backend.act_on_accepted_suggestion(backend.SuggestionActBody(id="news-a", title=title, via="queue"))
        by_id = {s["id"]: s for s in yt.load_suggestions()}
        self.assertTrue(by_id["news-a"]["acted"])
        self.assertFalse(by_id["news-b"].get("acted"))
        revived = backend.revive_suggestion(backend.SuggestionReviveBody(id="news-a", title=title))
        self.assertEqual([s["id"] for s in revived["suggestions"]], ["news-a"])
        accepted = backend.accepted_suggestions(style_name="__all__")["accepted"]
        self.assertEqual([s["id"] for s in accepted], ["news-b"])
        self.assertEqual(accepted[0]["news"]["summary"], "Other style")

    def test_unresolved_news_people_do_not_trigger_invented_portrait_generation(self):
        self._config()
        wd = self.output_dir / "unresolved-news"
        app._write_script_characters(wd, [{"name": "Alex Example", "description": "LLM guessed appearance"}])
        (wd / "news_people.json").write_text(json.dumps({
            "characters": [], "references": [],
            "unresolved": [{"name": "Alex Example", "reason": "Ambiguous identity"}],
        }))
        with mock.patch.object(app, "_preview_worker_urls") as workers, \
                mock.patch.object(app, "_generate_script_portrait") as generate:
            self.assertEqual(app.generate_all_script_portraits(wd, "News"), 0)
        workers.assert_not_called()
        generate.assert_not_called()
