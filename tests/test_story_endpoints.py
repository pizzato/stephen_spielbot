"""Story-mode backend flow (webapp/backend/main.py).

Phase 1 (_do_story_generate) persists story.json + brief with phase
"story_review"; phase 2 (_do_story_divide) merges review edits and persists the
scenes into the SAME work dir through the classic path. _do_script_generate
chains both headless for automation callers, and dialogue formats fall back to
classic generation.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import webapp.backend.main as backend  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pipeline.llm import Scene  # noqa: E402
from pipeline.orchestrator import DurableStore  # noqa: E402
from test_styles import TempConfigCase, _style  # noqa: E402


def _fake_story(n_scenes=4):
    return {
        "version": 1, "topic": "Test topic", "video_title": "", "n_scenes": n_scenes,
        "outline": [{"chapter": 1, "title": "Ch 1", "summary": "s", "scenes": n_scenes}],
        "chapters": [{"chapter": 1, "title": "Ch 1", "summary": "s",
                      "scenes": n_scenes, "text": "Original chapter prose."}],
        "critique": {"verdict": "pass", "notes": [], "chapters": []},
        "style": "story style", "music": "story music", "characters": [],
        "status": "draft", "created_at": 1.0, "updated_at": 1.0,
    }


def _fake_scenes(n):
    return [Scene(id=i, title=f"S{i}", image_prompt="img", video_prompt="vid",
                  narration=f"Narration {i}.") for i in range(1, n + 1)]


class StoryEndpointTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        self.write_config({
            "styles": [_style("Hero", script_mode="story"),
                       _style("Plain", script_mode="classic")],
            "default_style": "Plain",
            "characters": [],
            "characters_migrated_v2": True,
        })
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        mock.patch.object(backend, "_describe_in_background").start()
        self.addCleanup(mock.patch.stopall)

    # ── mode resolution ──────────────────────────────────────────────────────

    def test_effective_mode_style_default_and_override(self):
        cfg = backend.gapp.load_config()
        hero = backend.gapp.style_settings(cfg, "Hero")
        plain = backend.gapp.style_settings(cfg, "Plain")
        body = backend.GenerateScriptBody(topic="t")
        self.assertEqual(backend._effective_script_mode(body, hero), "story")
        self.assertEqual(backend._effective_script_mode(body, plain), "classic")
        body = backend.GenerateScriptBody(topic="t", script_mode="story")
        self.assertEqual(backend._effective_script_mode(body, plain), "story")
        body = backend.GenerateScriptBody(topic="t", script_mode="classic")
        self.assertEqual(backend._effective_script_mode(body, hero), "classic")

    def test_dialogue_format_falls_back_to_classic(self):
        cfg = backend.gapp.load_config()
        hero = backend.gapp.style_settings(cfg, "Hero")
        body = backend.GenerateScriptBody(topic="t", format="dialogue")
        self.assertEqual(backend._effective_script_mode(body, hero), "classic")

    # ── phase 1: story generate ──────────────────────────────────────────────

    def test_story_generate_persists_draft(self):
        body = backend.GenerateScriptBody(video_title="My Story", topic="A topic",
                                          n_scenes=4, style_name="Hero")
        with mock.patch.object(backend.story_mode, "generate_story",
                               return_value=_fake_story(4)) as gen:
            res = backend._do_story_generate(body)
        self.assertEqual(gen.call_count, 1)
        wd = Path(res["work_dir"])
        story = json.loads((wd / "story.json").read_text())
        self.assertEqual(story["status"], "draft")
        brief = json.loads((wd / "create_brief.json").read_text())
        self.assertEqual(brief["script_mode"], "story")
        self.assertEqual(brief["n_scenes"], 4)
        self.assertEqual(res["story"]["chapters"][0]["text"], "Original chapter prose.")
        store = DurableStore.default()
        try:
            job = store.get_job(res["job_id"])
            self.assertEqual(json.loads(job["config_json"])["phase"], "story_review")
        finally:
            store.close()
        # no scenes yet — the divide step writes script.json
        self.assertFalse((wd / "script.json").exists())

    # ── phase 2: divide ──────────────────────────────────────────────────────

    def _draft(self, n_scenes=4):
        body = backend.GenerateScriptBody(video_title="My Story", topic="A topic",
                                          n_scenes=n_scenes, style_name="Hero")
        with mock.patch.object(backend.story_mode, "generate_story",
                               return_value=_fake_story(n_scenes)):
            return backend._do_story_generate(body)

    def test_divide_merges_edits_and_persists_same_work_dir(self):
        draft = self._draft(4)
        wd = Path(draft["work_dir"])
        divide_body = backend.DivideStoryBody(
            work_dir=str(wd),
            chapters=[backend.StoryChapterEdit(chapter=1, text="EDITED prose.")])
        with mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(4), "m", "st", [])) as div:
            res = backend._do_story_divide(divide_body)
        # edited text reached divide_story and was persisted
        story_arg = div.call_args.args[0]
        self.assertEqual(story_arg["chapters"][0]["text"], "EDITED prose.")
        story = json.loads((wd / "story.json").read_text())
        self.assertEqual(story["chapters"][0]["text"], "EDITED prose.")
        self.assertEqual(story["status"], "divided")
        # classic payload shape, same work dir and job id
        self.assertEqual(res["work_dir"], str(wd))
        self.assertEqual(res["job_id"], draft["job_id"])
        self.assertEqual(len(res["scenes"]), 4)
        self.assertTrue((wd / "script.json").exists())
        self.assertEqual(res["create_brief"]["script_mode"], "story")

    def test_divide_without_draft_404s(self):
        wd = self.output_dir / "no-story-here"
        wd.mkdir()
        with self.assertRaises(HTTPException) as ctx:
            backend._do_story_divide(backend.DivideStoryBody(work_dir=str(wd)))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_divide_rejects_path_outside_output_dir(self):
        with self.assertRaises(HTTPException) as ctx:
            backend._do_story_divide(backend.DivideStoryBody(work_dir="/etc"))
        self.assertEqual(ctx.exception.status_code, 400)

    # ── headless chain + classic fallback ────────────────────────────────────

    def test_do_script_generate_chains_story_mode_headless(self):
        body = backend.GenerateScriptBody(video_title="Auto Story", topic="A topic",
                                          n_scenes=4, style_name="Hero")
        with mock.patch.object(backend.story_mode, "generate_story",
                               return_value=_fake_story(4)) as gen, \
             mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(4), "m", "st", [])) as div, \
             mock.patch.object(backend, "generate_script") as classic:
            res = backend._do_script_generate(body)
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(div.call_count, 1)
        classic.assert_not_called()
        wd = Path(res["work_dir"])
        self.assertTrue((wd / "story.json").exists())
        self.assertTrue((wd / "script.json").exists())
        self.assertEqual(len(res["scenes"]), 4)

    def test_do_script_generate_dialogue_format_uses_classic(self):
        body = backend.GenerateScriptBody(video_title="Dlg", topic="A topic",
                                          n_scenes=2, style_name="Hero", format="dialogue")
        with mock.patch.object(backend, "generate_script",
                               return_value=(_fake_scenes(2), "m", "st", [])) as classic, \
             mock.patch.object(backend.story_mode, "generate_story") as gen:
            res = backend._do_script_generate(body)
        classic.assert_called_once()
        gen.assert_not_called()
        self.assertFalse((Path(res["work_dir"]) / "story.json").exists())

    # ── draft persistence: resume, listing, loading, forking ─────────────────

    def test_save_story_persists_edits_and_ignores_blank(self):
        draft = self._draft(4)
        wd = Path(draft["work_dir"])
        res = backend.save_job_story(draft["job_id"], backend.StorySaveBody(
            chapters=[backend.StoryChapterEdit(chapter=1, text="Kept for later."),
                      backend.StoryChapterEdit(chapter=99, text="no such chapter")]))
        self.assertEqual(res["chapters"][0]["text"], "Kept for later.")
        on_disk = json.loads((wd / "story.json").read_text())
        self.assertEqual(on_disk["chapters"][0]["text"], "Kept for later.")
        # a blank edit must not wipe the saved text
        backend.save_job_story(draft["job_id"], backend.StorySaveBody(
            chapters=[backend.StoryChapterEdit(chapter=1, text="  ")]))
        on_disk = json.loads((wd / "story.json").read_text())
        self.assertEqual(on_disk["chapters"][0]["text"], "Kept for later.")

    def test_list_jobs_marks_story_drafts(self):
        draft = self._draft(4)
        rows = backend.list_jobs()["scripts"]
        by_wd = {r["work_dir"]: r for r in rows}
        self.assertTrue(by_wd[draft["work_dir"]]["story_draft"])
        with mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(4), "m", "st", [])):
            backend._do_story_divide(backend.DivideStoryBody(work_dir=draft["work_dir"]))
        rows = backend.list_jobs()["scripts"]
        by_wd = {r["work_dir"]: r for r in rows}
        self.assertFalse(by_wd[draft["work_dir"]]["story_draft"])

    def test_load_script_opens_a_draft_with_zero_scenes(self):
        draft = self._draft(4)
        res = backend.load_script(work_dir=draft["work_dir"])
        self.assertEqual(res["scenes"], [])
        self.assertEqual(res["work_dir"], draft["work_dir"])

    def test_divide_forks_when_scenes_already_exist(self):
        draft = self._draft(4)
        wd = Path(draft["work_dir"])
        with mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(4), "m", "st", [])):
            first = backend._do_story_divide(backend.DivideStoryBody(work_dir=str(wd)))
            original_script = (wd / "script.json").read_text()
            second = backend._do_story_divide(backend.DivideStoryBody(
                work_dir=str(wd),
                chapters=[backend.StoryChapterEdit(chapter=1, text="FORKED prose.")]))
        # the re-divide landed in a fresh work dir; the original is untouched
        self.assertNotEqual(second["work_dir"], first["work_dir"])
        self.assertEqual((wd / "script.json").read_text(), original_script)
        self.assertEqual(json.loads((wd / "story.json").read_text())["chapters"][0]["text"],
                         "Original chapter prose.")
        fork_wd = Path(second["work_dir"])
        self.assertTrue((fork_wd / "script.json").exists())
        fork_story = json.loads((fork_wd / "story.json").read_text())
        self.assertEqual(fork_story["chapters"][0]["text"], "FORKED prose.")
        self.assertEqual(fork_story["status"], "divided")
        self.assertNotEqual(second["job_id"], first["job_id"])

    # ── script critic ────────────────────────────────────────────────────────

    def _divided_job(self, n=4):
        draft = self._draft(n)
        with mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(n), "m", "st", [])):
            return backend._do_story_divide(backend.DivideStoryBody(work_dir=draft["work_dir"]))

    def test_critic_applies_rewrite_delete_reorder_then_converges(self):
        job = self._divided_job(4)
        ops_per_pass = [
            {"changed": True, "notes": ["scene 3 repeats scene 2"],
             "rewrites": [{"id": 2, "narration": "Sharper narration."}],
             "deletes": [3], "order": [1, 4, 2]},
            {"changed": False, "notes": ["clean now"], "rewrites": [], "deletes": [], "order": None},
        ]
        with mock.patch.object(backend.story_mode, "critique_scenes",
                               side_effect=ops_per_pass) as crit:
            res = backend._do_critic_run(job["job_id"],
                                         backend.CriticRunBody(until_converged=True))
        self.assertEqual(crit.call_count, 2)
        self.assertTrue(res["converged"])
        self.assertEqual(len(res["passes"]), 2)
        self.assertEqual(res["passes"][0]["deleted"], [3])
        self.assertTrue(res["passes"][0]["reordered"])
        # scenes renumbered 1..3 in the critic's order: old 1, old 4, old 2 (rewritten)
        self.assertEqual([s["id"] for s in res["scenes"]], [1, 2, 3])
        self.assertEqual([s["narration"] for s in res["scenes"]],
                         ["Narration 1.", "Narration 4.", "Sharper narration."])
        script = json.loads((Path(job["work_dir"]) / "script.json").read_text())
        self.assertEqual(len(script), 3)
        self.assertTrue((Path(job["work_dir"]) / "critic.json").exists())

    def test_critic_single_pass_stops_after_one(self):
        job = self._divided_job(4)
        ops = {"changed": True, "notes": [],
               "rewrites": [{"id": 1, "narration": "Pass one."}], "deletes": [], "order": None}
        with mock.patch.object(backend.story_mode, "critique_scenes",
                               return_value=ops) as crit:
            res = backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        self.assertEqual(crit.call_count, 1)
        self.assertFalse(res["converged"])
        self.assertEqual(res["scenes"][0]["narration"], "Pass one.")

    def test_critic_insert_adds_scene_in_place(self):
        job = self._divided_job(3)
        ops = {"changed": True, "notes": [], "rewrites": [], "deletes": [], "order": None,
               "inserts": [{"after": 2, "title": "Bridge", "narration": "Bridge narration.",
                            "image_prompt": "bridge img", "video_prompt": "bridge vid"}]}
        with mock.patch.object(backend.story_mode, "critique_scenes", return_value=ops):
            res = backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        self.assertEqual(res["passes"][0]["added"], 1)
        self.assertEqual([s["id"] for s in res["scenes"]], [1, 2, 3, 4])
        self.assertEqual(res["scenes"][2]["title"], "Bridge")
        self.assertEqual(res["scenes"][2]["narration"], "Bridge narration.")
        self.assertEqual(res["scenes"][3]["narration"], "Narration 3.")

    def test_critic_snapshots_versions_and_restore_rolls_back(self):
        job = self._divided_job(3)
        wd = Path(job["work_dir"])
        ops = {"changed": True, "notes": [], "order": None, "inserts": [],
               "rewrites": [{"id": 1, "narration": "Critic changed this."}], "deletes": [2]}
        with mock.patch.object(backend.story_mode, "critique_scenes", return_value=ops):
            backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        versions = backend.list_job_script_versions(job["job_id"])["versions"]
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["label"], "before critic pass 1")
        self.assertEqual(versions[0]["scene_count"], 3)
        res = backend.restore_job_script_version(
            job["job_id"], backend.RestoreVersionBody(file=versions[0]["file"]))
        self.assertEqual([s["narration"] for s in res["scenes"]],
                         ["Narration 1.", "Narration 2.", "Narration 3."])
        # the pre-restore state was snapshotted too, so the restore is undoable
        labels = [v["label"] for v in res["versions"]]
        self.assertIn("before restore", labels)
        script = json.loads((wd / "script.json").read_text())
        self.assertEqual(len(script), 3)

    def test_critic_pass_number_accumulates_across_runs(self):
        job = self._divided_job(3)
        seen = []
        def fake(scenes, title, video_title=None, avoid_hint=None, pass_num=1):
            seen.append(pass_num)
            return {"changed": True, "notes": [], "order": None, "inserts": [],
                    "deletes": [],
                    "rewrites": [{"id": 1, "narration": f"Pass {pass_num}."}]}
        with mock.patch.object(backend.story_mode, "critique_scenes", side_effect=fake):
            backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
            backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        self.assertEqual(seen, [1, 2])

    def test_critic_refuses_to_delete_every_scene(self):
        job = self._divided_job(3)
        ops = {"changed": True, "notes": [], "rewrites": [],
               "deletes": [1, 2, 3], "order": None}
        with mock.patch.object(backend.story_mode, "critique_scenes", return_value=ops):
            res = backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        self.assertEqual(len(res["scenes"]), 3)
        self.assertEqual(res["passes"][0]["deleted"], [])

    # ── automation auto-critic ───────────────────────────────────────────────

    def test_auto_critic_runs_before_result_when_flagged(self):
        body = backend.GenerateScriptBody(video_title="Auto QC", topic="t", n_scenes=3,
                                          style_name="Plain", auto_critic=True)
        ops = {"changed": True, "notes": [], "order": None, "inserts": [],
               "deletes": [3],
               "rewrites": [{"id": 1, "narration": "Critiqued."}]}
        with mock.patch.object(backend, "generate_script",
                               return_value=(_fake_scenes(3), "m", "st", [])), \
             mock.patch.object(backend.story_mode, "critique_scenes",
                               side_effect=[ops, {"changed": False, "notes": [],
                                                  "rewrites": [], "deletes": [],
                                                  "inserts": [], "order": None}]) as crit:
            res = backend._do_script_generate(body)
        self.assertGreaterEqual(crit.call_count, 1)
        # the returned payload reflects the critic's edits (rewrite + delete)
        self.assertEqual(len(res["scenes"]), 2)
        self.assertEqual(res["scenes"][0]["narration"], "Critiqued.")

    def test_auto_critic_off_by_default_and_failure_is_non_fatal(self):
        with mock.patch.object(backend, "generate_script",
                               return_value=(_fake_scenes(2), "m", "st", [])), \
             mock.patch.object(backend.story_mode, "critique_scenes") as crit:
            res = backend._do_script_generate(backend.GenerateScriptBody(
                video_title="No QC", topic="t", n_scenes=2, style_name="Plain"))
        crit.assert_not_called()
        self.assertEqual(len(res["scenes"]), 2)
        # and a critic crash never fails script creation
        with mock.patch.object(backend, "generate_script",
                               return_value=(_fake_scenes(2), "m", "st", [])), \
             mock.patch.object(backend.story_mode, "critique_scenes",
                               side_effect=RuntimeError("boom")):
            res = backend._do_script_generate(backend.GenerateScriptBody(
                video_title="QC crash", topic="t", n_scenes=2,
                style_name="Plain", auto_critic=True))
        self.assertEqual(len(res["scenes"]), 2)

    def test_auto_critic_covers_headless_story_mode(self):
        body = backend.GenerateScriptBody(video_title="Story QC", topic="t", n_scenes=4,
                                          style_name="Hero", auto_critic=True)
        ops = {"changed": True, "notes": [], "order": None, "inserts": [],
               "deletes": [], "rewrites": [{"id": 2, "narration": "Story critiqued."}]}
        with mock.patch.object(backend.story_mode, "generate_story",
                               return_value=_fake_story(4)), \
             mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(4), "m", "st", [])), \
             mock.patch.object(backend.story_mode, "critique_scenes",
                               side_effect=[ops, {"changed": False, "notes": [],
                                                  "rewrites": [], "deletes": [],
                                                  "inserts": [], "order": None}]):
            res = backend._do_script_generate(body)
        self.assertEqual(res["scenes"][1]["narration"], "Story critiqued.")

    # ── story fetch endpoint ─────────────────────────────────────────────────

    def test_get_job_story_roundtrip_and_404_for_classic(self):
        draft = self._draft(4)
        story = backend.get_job_story(draft["job_id"])
        self.assertEqual(story["status"], "draft")
        # a classic script has no story.json
        body = backend.GenerateScriptBody(video_title="Classic One", topic="t",
                                          n_scenes=2, style_name="Plain")
        with mock.patch.object(backend, "generate_script",
                               return_value=(_fake_scenes(2), "m", "st", [])):
            res = backend._do_script_generate(body)
        with self.assertRaises(HTTPException) as ctx:
            backend.get_job_story(res["job_id"])
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
