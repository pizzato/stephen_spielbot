"""News provenance, style boundaries and real reference images at creation."""
import io
import json
import os
import urllib.request
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from PIL import Image

import app
import webapp.backend.main as backend
from pipeline import news_people, youtube as yt
from webapp.backend import news_monitor
from test_story_endpoints import _fake_scenes, _fake_story
from test_styles import TempConfigCase, _style


class NewsCreationTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        self.write_config({
            "styles": [_style("News"), _style("Other")], "default_style": "News",
            "characters": [], "characters_migrated_v2": True,
            "news": {"x_bearer_token": "private-search-token"},
        })
        for target, attr, value in [
            (yt, "SUGGESTIONS_PATH", self.config_file.parent / "ideas.json"),
            (yt, "QUEUE_PATH", self.config_file.parent / "queue.json"),
        ]:
            patch = mock.patch.object(target, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        for patch in [
            mock.patch.object(backend, "_describe_in_background"),
            mock.patch.object(backend, "_portraits_in_background"),
            mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("Unexpected network call")),
            mock.patch.object(news_people.requests, "get", side_effect=AssertionError("Unexpected network call")),
        ]:
            patch.start()
            self.addCleanup(patch.stop)
        self.source = {
            "summary": "A reporter says Anthony Albanese announced a rail project.",
            "sources": [{"id": "123", "url": "https://x.com/reporter/status/123",
                         "text": "Anthony Albanese announces a rail project.",
                         "article_urls": ["https://example.com/rail"]}],
            "people": [{"name": "Anthony Albanese", "description": "Named in the report."}],
            "include_people": False,
        }
        self.idea = {"id": "news-123", "title": "The rail refrain", "reason": "A topical rail song",
                     "source": "news", "style_name": "News", "news": self.source}
        yt.save_suggestions([self.idea])

    def _assert_source(self, result, actual_topic):
        wd = Path(result["work_dir"])
        self.assertEqual(json.loads((wd / "news_source.json").read_text()), self.source)
        self.assertIn("NEWS SOURCE MATERIAL", actual_topic)
        self.assertIn("reported claims, not verified facts", actual_topic)
        self.assertIn("https://x.com/reporter/status/123", actual_topic)
        self.assertIn("Anthony Albanese announces", actual_topic)
        self.assertIn("Do not browse", actual_topic)
        self.assertIn("linked articles have not been read", actual_topic)
        self.assertNotIn("private-search-token", actual_topic)
        self.assertEqual(result["create_brief"]["topic"], actual_topic.split("\n\nNews instructions")[0])
        self.assertFalse((wd / "news_people.json").exists())

    def test_story_receives_idea_provenance(self):
        body = backend.GenerateScriptBody(video_title=self.idea["title"], topic="Rail news",
                                          style_name="News", idea_id=self.idea["id"], n_scenes=2)
        with mock.patch.object(backend.story_mode, "generate_story", return_value=_fake_story(2)) as generate:
            result = backend._do_story_generate(body)
        self._assert_source(result, generate.call_args.args[0])

    def test_existing_news_idea_gets_complete_visible_directions_without_regeneration(self):
        with mock.patch.object(backend.news_monitor.news, "generate_news_ideas") as generate:
            idea = backend.news_ideas("News")["suggestions"][0]
        self.assertTrue(idea["directions"].startswith(self.idea["reason"]))
        self.assertIn(self.source["summary"], idea["directions"])
        self.assertIn(self.source["sources"][0]["text"], idea["directions"])
        self.assertIn("Reference-photo lookup is disabled", idea["directions"])
        generate.assert_not_called()

    def test_edited_directions_keep_user_angle_and_restore_source_material_once(self):
        original = news_monitor.idea_directions(self.idea)
        edited = original.replace(self.idea["reason"], "Use a hopeful tone.")
        body = backend.GenerateScriptBody(video_title=self.idea["title"], topic=edited,
                                          style_name="News", idea_id=self.idea["id"], n_scenes=2)
        with mock.patch.object(backend.story_mode, "generate_story", return_value=_fake_story(2)) as generate:
            result = backend._do_story_generate(body)
        topic = generate.call_args.args[0]
        self.assertTrue(topic.startswith("Use a hopeful tone."))
        self.assertEqual(topic.count("NEWS SOURCE MATERIAL"), 1)
        self._assert_source(result, topic)

    def test_existing_queue_prompt_gets_source_text_without_changing_ordinary_items(self):
        queue = [{"id": "legacy-news", "video_prompt": "Keep my angle", "news": self.source},
                 {"id": "ordinary", "video_prompt": "My unrelated video"}]
        with mock.patch.object(backend, "_reconcile_queue", return_value=queue), \
                mock.patch.object(backend, "_attach_render_estimates"):
            rows = backend.get_queue()["queue"]
        self.assertTrue(rows[0]["video_prompt"].startswith("Keep my angle"))
        self.assertIn(self.source["sources"][0]["text"], rows[0]["video_prompt"])
        self.assertEqual(rows[1]["video_prompt"], "My unrelated video")

    def test_story_uses_queue_snapshot_after_idea_is_gone(self):
        entry = news_monitor.queue_idea(self.idea, app.load_config())
        yt.save_suggestions([])
        body = backend.GenerateScriptBody(video_title=self.idea["title"], topic="Rail news",
                                          style_name="News", queue_item_id=entry["id"],
                                          idea_id=self.idea["id"], n_scenes=2)
        with mock.patch.object(backend.story_mode, "generate_story", return_value=_fake_story(2)) as generate:
            result = backend._do_story_generate(body)
        self._assert_source(result, generate.call_args.args[0])
        self.assertEqual(result["create_brief"]["queue_item_id"], entry["id"])

    def test_song_receives_idea_and_queue_provenance(self):
        for transport in ("idea", "queue"):
            with self.subTest(transport=transport):
                kwargs = {"idea_id": self.idea["id"]}
                if transport == "queue":
                    entry = news_monitor.queue_idea(self.idea, app.load_config())
                    yt.save_suggestions([])
                    kwargs = {"queue_item_id": entry["id"]}
                body = backend.SongDraftBody(video_title=self.idea["title"], topic="Rail news",
                                              style_name="News", minutes=1, **kwargs)
                with mock.patch.object(backend.story_mode, "write_song", return_value={
                        "caption": "Folk song", "lyrics": "[Verse]\nDown the new railway"}) as write:
                    result = backend.song_draft(body)
                self._assert_source(result, write.call_args.kwargs["topic"])
                self.assertTrue((Path(result["work_dir"]) / "song.json").exists())

    def test_story_and_song_reject_another_styles_idea_and_queue(self):
        entry = news_monitor.queue_idea(self.idea, app.load_config())
        with mock.patch.object(backend.story_mode, "generate_story") as story, \
                mock.patch.object(backend.story_mode, "write_song") as song:
            for kwargs in ({"idea_id": self.idea["id"]}, {"queue_item_id": entry["id"]}):
                for endpoint, cls in ((backend._do_story_generate, backend.GenerateScriptBody),
                                       (backend.song_draft, backend.SongDraftBody)):
                    with self.subTest(endpoint=endpoint.__name__, kwargs=kwargs):
                        with self.assertRaises(HTTPException) as caught:
                            endpoint(cls(video_title="Wrong style", style_name="Other", **kwargs))
                        self.assertEqual(caught.exception.status_code, 400)
                        self.assertIn("different style", caught.exception.detail)
            story.assert_not_called()
            song.assert_not_called()

    def test_resolved_news_portrait_reaches_scene_reference_conditioning(self):
        self.source["include_people"] = True
        yt.save_suggestions([self.idea])
        image_bytes = io.BytesIO()
        Image.new("RGB", (128, 128), "navy").save(image_bytes, "PNG")
        reference = {
            "source_url": "https://commons.wikimedia.org/wiki/File:Person.png",
            "image_url": "https://upload.wikimedia.org/person.png",
            "license": "CC BY 4.0", "attribution": "Test photographer", "credit": "",
        }
        with mock.patch.object(backend.story_mode, "generate_story", return_value=_fake_story(2)), \
                mock.patch.object(news_people, "_identity", return_value={"id": "Q100"}), \
                mock.patch.object(news_people, "_reference", return_value=reference), \
                mock.patch.object(news_people, "_read_response", return_value=image_bytes.getvalue()):
            draft = backend._do_story_generate(backend.GenerateScriptBody(
                video_title=self.idea["title"], topic="Rail news", style_name="News",
                idea_id=self.idea["id"], n_scenes=2))
        wd = Path(draft["work_dir"])
        chars = app._read_script_characters(wd)
        self.assertEqual([c["name"] for c in chars], ["Anthony Albanese"])
        photo = wd / "characters" / chars[0]["ref_image"]
        self.assertTrue(photo.is_file())
        scenes = _fake_scenes(2)
        scenes[0].image_prompt = "Anthony Albanese at a railway station"
        generated_char = {"id": "generated", "name": "Anthony Albanese", "description": "An invented look"}
        with mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(scenes, "music", "style", [generated_char])) as divide:
            result = backend._do_story_divide(backend.DivideStoryBody(work_dir=str(wd)))
        self.assertIn("match this person's face", divide.call_args.kwargs["character_sheet"])
        self.assertEqual(app._read_script_characters(wd)[0]["ref_image"], photo.name)
        prompt, refs = app._characters_prompt_and_refs(
            scenes[0].image_prompt, result["scenes"][0], app.load_config(), "News", wd,
            engine={"family": "qwen-image", "t2i_ref_workflow": "reference.json"})
        self.assertEqual(refs, [photo])
        self.assertIn("Anthony Albanese appears EXACTLY as the character in <image1>", prompt)
        self.assertIn("Test photographer", prompt)

    def test_unresolved_news_person_does_not_get_an_invented_automatic_portrait(self):
        wd = self.output_dir / "unresolved"
        wd.mkdir()
        self.source["include_people"] = True
        with mock.patch.object(news_people, "_identity", side_effect=ValueError("Ambiguous identity")):
            news_monitor.attach_source(wd, self.source)
        app._write_script_characters(wd, [{"id": "albanese", "name": "Albanese",
                                          "description": "The prime minister; appearance unknown."}])
        with mock.patch.object(app, "_preview_worker_urls", return_value=["http://worker"]) as workers, \
                mock.patch.object(app, "_generate_script_portrait") as paint:
            count = app.generate_all_script_portraits(wd, "News")
        self.assertEqual(count, 0)
        workers.assert_not_called()
        paint.assert_not_called()
        self.assertEqual(json.loads((wd / "news_people.json").read_text())["unresolved"][0]["name"],
                         "Anthony Albanese")

    def test_config_redaction_and_blank_token_preservation(self):
        cfg = app.load_config()
        safe = app.public_config(cfg)
        self.assertEqual(safe["news"]["x_bearer_token"], "")
        self.assertTrue(safe["news"]["x_bearer_token_set"])
        self.assertNotIn("private-search-token", json.dumps(safe))
        self.assertEqual(cfg["news"]["x_bearer_token"], "private-search-token")
        preserved = app.merge_config_update(cfg, {"news": safe["news"]})
        self.assertEqual(preserved["news"], {"x_bearer_token": "private-search-token"})
        replaced = app.merge_config_update(cfg, {"news": {"x_bearer_token": " replacement "}})
        self.assertEqual(replaced["news"], {"x_bearer_token": "replacement"})
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "environment-search-token"}):
            public = app.public_config({"news": {"x_bearer_token": ""}})
        self.assertTrue(public["news"]["x_bearer_token_set"])
        self.assertNotIn("environment-search-token", json.dumps(public))

    def test_render_snapshot_excludes_nested_news_credential(self):
        cfg = app.load_config()
        snapshot = app._job_config_snapshot(cfg)
        self.assertNotIn("news", snapshot)
        self.assertNotIn("private-search-token", json.dumps(snapshot))
        self.assertEqual(cfg["news"]["x_bearer_token"], "private-search-token")

    def test_ordinary_auto_picker_cannot_consume_news_ideas(self):
        with mock.patch.object(app, "_auto_feed_styles", return_value=["News"]), \
                mock.patch.object(app, "_generate_mixed_suggestions", return_value=[]), \
                mock.patch.object(yt, "add_to_queue") as add:
            self.assertIsNone(app._auto_pick_suggestion(app.load_config()))
        add.assert_not_called()
        self.assertFalse(yt.load_suggestions()[0].get("used", False))

    def test_ordinary_auto_picker_preserves_same_title_news_review_state(self):
        ordinary = {"id": "ordinary-123", "title": self.idea["title"], "reason": "An ordinary suggestion",
                    "style_name": "News", "interestingness": 0.7}
        yt.save_suggestions([self.idea, ordinary])
        with mock.patch.object(app, "_auto_feed_styles", return_value=["News"]), \
                mock.patch.object(app, "generate_video_prompt", return_value=""), \
                mock.patch.object(yt, "add_to_queue", return_value={"id": "queue-ordinary"}), \
                mock.patch.object(yt, "update_queue_item"):
            self.assertIsNotNone(app._auto_pick_suggestion(app.load_config()))
        saved = {row["id"]: row for row in yt.load_suggestions()}
        self.assertFalse(saved[self.idea["id"]].get("used", False))
        self.assertTrue(saved[ordinary["id"]].get("used"))

    def _configure_song_automation(self, **overrides):
        automation = {"auto_write_scripts": True, "auto_format": "song", "auto_song": True,
                      "auto_song_approve": True, "auto_start_job": True,
                      "auto_approve_script": True, "auto_ai_ideas": False, **overrides}
        self.write_config({"styles": [_style("News", automation=automation)], "default_style": "News",
                           "characters": [], "characters_migrated_v2": True,
                           "youtube_auto_ai_ideas": False})
        return app.load_config()

    def test_news_song_preparation_respects_auto_song_toggle(self):
        cfg = self._configure_song_automation(auto_song=False)
        news_monitor.queue_idea(self.idea, cfg)
        with mock.patch.object(backend, "_auto_song_first") as song, \
                mock.patch.object(backend, "_do_script_generate") as script:
            self.assertEqual(backend._auto_write_scripts(cfg), 0)
        song.assert_not_called()
        script.assert_not_called()

    def test_news_song_start_and_retry_respect_song_and_render_gates(self):
        for status in ("pending", "failed"):
            for disabled in ("auto_song", "auto_song_approve", "auto_start_job"):
                with self.subTest(status=status, disabled=disabled):
                    cfg = self._configure_song_automation(**{disabled: False})
                    yt.save_queue([])
                    entry = news_monitor.queue_idea(self.idea, cfg)
                    yt.update_queue_item(entry["id"], status=status)
                    with mock.patch.object(app, "_is_job_running", return_value=False), \
                            mock.patch.object(backend, "_start_queue_item") as start:
                        self.assertIsNone(backend._auto_start_best())
                    start.assert_not_called()
