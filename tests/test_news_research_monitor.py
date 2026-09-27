"""No paid network calls: shared research, durable limits, and complete video handoff."""
import copy
import json
from unittest import mock

import app
from pipeline import news, news_research, youtube as yt
from test_styles import TempConfigCase, _style
from webapp.backend import main as backend, news_monitor


BRIEF = {
    "text": "Anthony Albanese announced a Medicare proposal in Australia on 27 September 2026. "
            "The announcement says $20 million; independent reporting disputes the delivery date.",
    "sources": [{"url": "https://example.org/announcement", "title": "Original announcement"},
                {"url": "https://example.org/report", "title": "Independent report"}],
    "provider": "openai", "model": "research-model", "searched": True,
    "usage": {"input_tokens": 1234, "output_tokens": 500, "web_search_calls": 2},
}


def _ideas(posts, cfg, style, **kwargs):
    return [{"title": style["name"] + " Medicare song", "reason": "A timely song",
             "directions": "Albanese sings about the proposal; distinguish satire from reporting.",
             "summary": "A Medicare proposal announced by Anthony Albanese.",
             "source_ids": [p["id"] for p in posts], "sources": posts,
             "people": [{"name": "Anthony Albanese", "description": "Principal news subject"}]}]


class ResearchMonitorTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        for target, name, filename in ((yt, "SUGGESTIONS_PATH", "ideas.json"),
                                       (yt, "QUEUE_PATH", "queue.json")):
            patcher = mock.patch.object(target, name, self.config_file.parent / filename)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, target, attr, kwargs in (
            ("research", news_research, "research", {"return_value": BRIEF}),
            ("generate", news, "generate_news_ideas", {"side_effect": _ideas}),
            ("clock", news_monitor.time, "time", {"return_value": 1_000_000}),
            ("x_fetch", news, "fetch_recent_posts", {"side_effect": AssertionError("Unexpected X charge")}),
        ):
            patcher = mock.patch.object(target, attr, **kwargs)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def config(self, *, limit=3, mode="manual", children=True, **monitor):
        self.write_config({
            "llm_backend": "openai", "openai_api_key": "dummy-test-key", "openai_model": "research-model",
            "news": {"source": "llm", "max_checks_per_day": limit},
            "styles": [_style("News", news_monitor={"enabled": True, "query": "Australian healthcare",
                "check_mode": mode, "include_people": True, **monitor})]
                + ([{"name": "Child", "parent": "News"}] if children else []),
            "default_style": "News",
        })
        return app.load_config()

    def ledger(self):
        return [json.loads(line) for line in (self.config_file.parent / "news_research_usage.jsonl").read_text().splitlines()]

    def test_inherited_styles_share_one_request_and_have_separate_ideas(self):
        cfg = self.config()
        result = news_monitor.check(cfg, force=True)
        self.research.assert_called_once()
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(result["ideas_added"], 2)
        self.assertEqual(result["daily_checks_used"], 1)
        self.assertEqual([s["research_cached"] for s in result["styles"]], [False, True])
        self.assertEqual([s["sources_fetched"] for s in result["styles"]], [2, 2])
        self.assertEqual({i["style_name"] for i in yt.load_suggestions()}, {"News", "Child"})
        events = self.ledger()
        self.assertEqual([e["event"] for e in events], ["started", "finished"])
        self.assertEqual(events[0]["attempt_id"], events[1]["attempt_id"])
        self.assertEqual(events[1]["usage"], BRIEF["usage"])
        self.assertNotIn("dummy-test-key", json.dumps(events))

    def test_manual_checks_reuse_disk_cache_and_skip_already_processed_brief(self):
        cfg = self.config(children=False)
        news_monitor.check(cfg, force=True)
        self.clock.return_value += 60
        result = news_monitor.check(copy.deepcopy(cfg), force=True)
        self.research.assert_called_once()
        self.generate.assert_called_once()
        self.assertEqual(result["styles"][0]["last_outcome"], "no_new_sources")
        self.assertTrue(news_monitor._read_research()["cache"])

    def test_writer_failure_does_not_refetch_successful_research(self):
        cfg = self.config(children=False)
        self.generate.side_effect = RuntimeError("private error contents")
        result = news_monitor.check(cfg, force=True)
        self.assertNotIn("private", result["styles"][0]["last_error"])
        self.generate.side_effect = _ideas
        result = news_monitor.check(cfg, force=True)
        self.assertEqual(result["ideas_added"], 1)
        self.research.assert_called_once()
        self.assertEqual(result["daily_checks_used"], 1)

    def test_failure_counts_once_across_styles_and_retries_after_cooldown(self):
        cfg = self.config()
        self.research.side_effect = news_research.ResearchError("Provider tool limit", {"input_tokens": 10})
        result = news_monitor.check(cfg, force=True)
        self.assertEqual(result["daily_checks_used"], 1)
        self.research.assert_called_once()
        self.assertEqual(self.ledger()[1]["usage"], {"input_tokens": 10})
        news_monitor.check(cfg, force=True)
        self.research.assert_called_once()
        self.clock.return_value += 300
        news_monitor.check(cfg, force=True)
        self.assertEqual(self.research.call_count, 2)

    def test_global_limit_blocks_new_subject_but_allows_cached_result(self):
        cfg = self.config(limit=1)
        news_monitor.check(cfg, "News", force=True)
        result = news_monitor.check(cfg, "Child", force=True)
        self.assertEqual(result["ideas_added"], 1)
        cfg["styles"][0]["news_monitor"]["query"] = "OpenAI announcements"
        result = news_monitor.check(cfg, "News", force=True)
        self.assertIn("Daily news research limit", result["styles"][0]["last_error"])
        self.research.assert_called_once()
        self.clock.return_value += 86400
        news_monitor.check(cfg, "News", force=True)
        self.assertEqual(self.research.call_count, 2)
        self.assertEqual(news_monitor.status(cfg)["daily_checks_used"], 1)

    def test_reservation_survives_interrupted_request_without_automatic_retry(self):
        cfg = self.config(children=False)
        self.research.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            news_monitor.check(cfg, force=True)
        self.assertEqual(news_monitor.status(cfg)["daily_checks_used"], 1)
        self.assertEqual(len(self.ledger()), 1)
        result = news_monitor.check(cfg, force=True)
        self.assertIn("interrupted", result["styles"][0]["last_error"])
        self.research.assert_called_once()

    def test_updated_facts_at_same_urls_are_new_after_cache_expires(self):
        cfg = self.config(children=False)
        news_monitor.check(cfg, force=True)
        original = yt.load_suggestions()[0]["news"]["sources"][0]
        self.clock.return_value += 3600
        self.research.return_value = {**BRIEF, "text": BRIEF["text"] + " The proposal passed the next vote."}
        result = news_monitor.check(cfg, force=True)
        self.assertEqual(self.research.call_count, 2)
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(result["styles"][0]["posts_new"], 1)
        self.assertNotEqual(self.generate.call_args.args[0][0]["id"], original["id"])

    def test_cached_research_keeps_original_timestamp(self):
        cfg = self.config()
        news_monitor.check(cfg, "News", force=True)
        self.clock.return_value += 60
        news_monitor.check(cfg, "Child", force=True)
        a, b = yt.load_suggestions()
        self.assertEqual(a["news"]["sources"][0]["created_at"], b["news"]["sources"][0]["created_at"])

    def test_no_news_result_is_cached_and_counted_without_idea_call(self):
        cfg = self.config()
        self.research.return_value = {**BRIEF, "text": "", "sources": []}
        result = news_monitor.check(cfg, force=True)
        self.research.assert_called_once()
        self.generate.assert_not_called()
        self.assertEqual(result["daily_checks_used"], 1)
        self.assertTrue(all(row["last_outcome"] == "no_news" for row in result["styles"]))

    def test_status_and_cached_ideas_endpoints_never_research(self):
        cfg = self.config()
        news_monitor.status(cfg)
        backend.news_ideas(style_name="News")
        self.research.assert_not_called()
        self.generate.assert_not_called()

    def test_source_switch_preserves_the_provenance_of_last_attempt(self):
        cfg = self.config(children=False)
        news_monitor._save_state({"News": {"last_checked": 999_999, "posts_fetched": 50, "last_outcome": "no_ideas"}})
        self.assertEqual(news_monitor.status(cfg)["styles"][0]["last_source"], "x")
        news_monitor.check(cfg, force=True)
        cfg["news"]["source"] = "x"
        result = news_monitor.status(cfg)
        self.assertEqual(result["source"], "x")
        self.assertEqual(result["styles"][0]["last_source"], "llm")
        self.assertEqual(result["styles"][0]["sources_fetched"], 2)

    def test_missing_subject_or_credentials_does_not_consume_budget(self):
        cfg = self.config(query="")
        result = news_monitor.check(cfg, force=True)
        self.assertEqual(result["daily_checks_used"], 0)
        cfg["styles"][0]["news_monitor"]["query"] = "Healthcare"
        with mock.patch.object(news_research, "_key", return_value=""):
            result = news_monitor.check(cfg, force=True)
        self.assertEqual(result["daily_checks_used"], 0)
        self.research.assert_not_called()

    def test_all_trigger_modes_apply_to_llm_research(self):
        cfg = self.config(children=False, mode="page_open")
        news_monitor.check(cfg)
        self.research.assert_not_called()
        news_monitor.check(cfg, page_open=True)
        self.research.assert_called_once()
        cfg["styles"][0]["news_monitor"]["enabled"] = False
        self.clock.return_value += 86400
        news_monitor.check(cfg, force=True)
        self.research.assert_called_once()

    def test_complete_evidence_and_principal_person_survive_auto_queue(self):
        cfg = self.config(children=False, auto_accept=True, auto_queue=True)
        news_monitor.check(cfg, force=True)
        queued = yt.load_queue()[0]
        self.assertEqual(queued["source_platform"], "web")
        for content in (BRIEF["text"], "https://example.org/announcement", "https://example.org/report",
                        "Albanese sings", "Do not browse", "not verbatim article text"):
            self.assertIn(content, queued["video_prompt"])
        self.assertNotIn("linked articles have not been read", queued["video_prompt"])
        source = news_monitor.source_for("", queued["id"], "News")
        self.assertEqual(source["people"][0]["name"], "Anthony Albanese")
        (self.output_dir / "news_source.json").write_text(json.dumps(source))
        lead = {"name": "Anthony Albanese", "enabled": True}
        with mock.patch.object(app, "_read_script_characters", return_value=[{"name": "LuizPizzato"}, lead]):
            self.assertEqual(news_monitor.singer_candidates(self.output_dir), [lead])

    def test_research_settings_save_without_erasing_existing_x_secret(self):
        cfg = self.config()
        cfg["news"]["x_bearer_token"] = "saved-secret"
        updated = app.merge_config_update(cfg, {"news": {"research_provider": "claude", "country": "GB",
                    "max_checks_per_day": 7, "x_bearer_token": "", "x_bearer_token_set": True}})
        self.assertEqual(updated["news"]["x_bearer_token"], "saved-secret")
        self.assertEqual(updated["news"]["research_provider"], "claude")
        self.assertEqual(updated["news"]["country"], "GB")
        self.assertEqual(updated["news"]["max_checks_per_day"], 7)
        self.assertNotIn("x_bearer_token_set", updated["news"])
        self.assertNotIn("news", app._job_config_snapshot(updated))

    def test_multiple_distinct_story_ideas_can_share_a_research_bundle(self):
        cfg = self.config(children=False, max_ideas=3)
        posts = news_monitor._research_posts(BRIEF, "Healthcare", self.clock.return_value)
        rows = [dict(title=title, reason="News", summary="Brief", directions="Complete treatment",
                     source_ids=[posts[0]["id"]], people=[]) for title in ("Funding proposal", "Delivery debate")]
        # Exercise the real editor parser rather than the monitor's test stub.
        self.generate.side_effect = None
        with mock.patch.object(news, "_chat_complete", return_value=json.dumps(rows)):
            parsed = _REAL_GENERATE(posts, cfg, app.style_settings(cfg, "News"))
        self.assertEqual(len(parsed), 2)
        self.generate.return_value = parsed
        result = news_monitor.check(cfg, force=True)
        self.assertEqual(result["ideas_added"], 2)
        self.assertEqual(len({i["id"] for i in yt.load_suggestions()}), 2)


_REAL_GENERATE = news.generate_news_ideas
