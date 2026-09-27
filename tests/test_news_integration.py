"""Durable per-style news polling, queue handoff, and script reference flow."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

import app
from pipeline import news, news_people, x as xt, youtube as yt
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

    def test_all_styles_search_with_global_account_regardless_of_publishing_account(self):
        self.write_config({
            "x_accounts": [{"id": "shared"}, {"id": "publisher"}],
            "x_client_id": "client-id", "news": {"x_account": "shared"},
            "styles": [
                _style(name, x_account=publisher, news_monitor={"enabled": True, "query": name})
                for name, publisher in (("First", "publisher"), ("Second", ""), ("Third", "shared"))
            ], "default_style": "First",
        })
        cfg = app.load_config()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.iter_content.return_value = [b'{"data": []}']
        token = {"access_token": "shared-secret", "expires_at": time.time() + 3600}
        with mock.patch.object(xt, "_load_token", return_value=token) as load, \
                mock.patch.object(xt.requests, "request", return_value=response) as send:
            for name in ("First", "Second", "Third"):
                result = news_monitor.check(cfg, name, force=True)
                self.assertEqual(result["account"], "shared")
                self.assertTrue(result["configured"])
                self.assertEqual(result["styles"][0]["last_outcome"], "no_posts")
        self.assertEqual(send.call_count, 3)
        self.assertEqual([call.kwargs["params"]["query"] for call in send.call_args_list],
                         ["First", "Second", "Third"])
        for call in send.call_args_list:
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer shared-secret")
        self.assertTrue(all(call.args == ("shared",) for call in load.call_args_list))
        self.assertEqual([s["x_account"] for s in cfg["styles"]], ["publisher", "", "shared"])

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
        self.assertEqual(saved["news"]["directions"], _idea()["reason"])
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

    def test_complete_directions_survive_review_and_manual_or_automatic_queueing(self):
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                yt.save_suggestions([])
                yt.save_queue([])
                news_monitor._save_state({})
                cfg = self._config(auto_accept=automatic, auto_queue=automatic)
                treatment = "Sing from a student's view; the chorus imagines joining the new music program."
                self._check(cfg, generated=[_idea(directions=treatment)])
                if not automatic:
                    visible = backend.news_ideas("News")["suggestions"][0]
                    self.assertTrue(visible["directions"].startswith(treatment))
                    backend.dismiss_suggestion(backend.SuggestionDismissBody(
                        id=visible["id"], title=visible["title"], reason="accepted"))
                accepted = backend.accepted_suggestions("News")["accepted"][0]
                brief = accepted["directions"]
                for expected in (treatment, POST["text"], POST["url"], POST["created_at"],
                                 "@news", "Named news subject", "Do not browse", "citations only"):
                    self.assertIn(expected, brief)
                if not automatic:
                    backend.queue_add(backend.QueueAddBody(
                        title=accepted["title"], idea_id=accepted["id"],
                        style_name="News", prompt=brief))
                queued = yt.load_queue()[0]
                self.assertEqual(queued["video_prompt"], brief)
                self.assertEqual(queued["news"]["directions"], treatment)
                yt.save_suggestions([])
                source = news_monitor.source_for("", queued["id"], "News")
                self.assertEqual(news_monitor.topic_with_sources(brief, source), brief)

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

    def test_automatic_news_song_keeps_nickname_subjects_and_builds_fallback_portraits_before_render(self):
        self._config(auto_accept=True, auto_queue=True)
        raw = self.read_config()
        raw["styles"][0]["automation"] = {
            "auto_format": "song", "auto_song": True,
            "auto_song_critic_passes": 0, "auto_critic": False,
        }
        self.write_config(raw)
        cfg = app.load_config()
        post = {**POST, "text": "ALBO warned about dark forces and Pauline Hanson responded."}
        reply = [_idea(people=[
            {"name": "Anthony Albanese", "source_name": "ALBO", "description": "Principal news subject"},
            {"name": "Pauline Hanson", "source_name": "Pauline Hanson", "description": "Responded to ALBO"},
        ])]
        with mock.patch.object(news, "fetch_recent_posts", return_value={"posts": [post], "newest_id": "101"}), \
                mock.patch.object(news, "_chat_complete", return_value=json.dumps(reply)):
            news_monitor.check(cfg, force=True)
        queued = yt.load_queue()[0]
        self.assertEqual([p["name"] for p in queued["news"]["people"]], ["Anthony Albanese", "Pauline Hanson"])
        self.assertEqual(queued["news"]["people"][0]["aliases"], ["ALBO"])
        yt.save_suggestions([])  # unattended generation must work from the durable queue snapshot
        scene = Scene(id=1, title="The chorus", narration="", mode="silent",
                      image_prompt="ALBO and Pauline Hanson sing", video_prompt="A musical duet")
        events = []

        def paint(engine, prompt, out, **kwargs):
            out.write_bytes(b"generated portrait fixture")
            events.append("portrait")

        with stub_script([scene]) as (draft, divide), \
                mock.patch.object(news_people, "_identity", side_effect=ValueError("Ambiguous identity")), \
                mock.patch.object(backend.threading.Thread, "start"), \
                mock.patch.object(backend.story_mode, "write_song", return_value={
                    "caption": "A folk song", "lyrics": "[Verse]\nSing the news"}) as song, \
                mock.patch.object(backend, "_do_song_generate"), \
                mock.patch.object(app, "pick_song_singer", side_effect=AssertionError("Catalogue fallback")), \
                mock.patch.object(app, "_preview_worker_urls", return_value=["http://worker"]), \
                mock.patch.object(app.engines, "resolve", return_value={"family": "qwen-image"}), \
                mock.patch.object(app, "generate_with_engine", side_effect=paint) as generate, \
                mock.patch.object(backend, "generate_all_previews"), \
                mock.patch.object(backend.DurableStore, "ensure_generation_plan",
                                  side_effect=lambda *a, **k: events.append("plan")), \
                mock.patch.object(app, "_launch_generation_job", return_value={}) as launch:
            result = backend._start_queue_item(queued)
            self.assertEqual(app.generate_all_script_portraits(result["work_dir"], "News"), 0)
        self.assertEqual(events, ["portrait", "portrait", "plan"])
        self.assertEqual(generate.call_count, 2)
        launch.assert_called_once()
        self.assertIn("Anthony Albanese", song.call_args.kwargs["singer_note"])
        for call in (draft.call_args, divide.call_args):
            self.assertIn("Anthony Albanese", call.kwargs["character_sheet"])
            self.assertIn("Pauline Hanson", call.kwargs["character_sheet"])
        wd = Path(result["work_dir"])
        characters = app._read_script_characters(wd)
        self.assertEqual([c["name"] for c in characters], ["Anthony Albanese", "Pauline Hanson"])
        self.assertTrue(all((wd / "characters" / c["ref_image"]).is_file() for c in characters))
        self.assertEqual(json.loads((wd / "song.json").read_text())["singer"], "Anthony Albanese")
        warnings = json.loads((wd / "news_people.json").read_text())["unresolved"]
        self.assertEqual({p["name"] for p in warnings}, {"Anthony Albanese", "Pauline Hanson"})
        self.assertEqual(len(app._scene_reference_images("ALBO and Pauline Hanson sing", {}, cfg, "News", wd)), 2)
