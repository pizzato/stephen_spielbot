"""Story-first backend flow (webapp/backend/main.py) — the only way a script
is written.

Phase 1 (_do_story_generate) persists story.json + brief with phase
"story_review"; phase 2 (_do_story_divide) merges review edits and persists the
scenes into the SAME work dir. _do_script_generate chains both headless for
automation callers. The FORMAT decides how the story is staged: narrated,
acted, or a mix.
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
from scriptstub import stub_script  # noqa: E402
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
            "styles": [_style("Hero"), _style("Plain")],
            "default_style": "Plain",
            "characters": [],
            "characters_migrated_v2": True,
        })
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        mock.patch.object(backend, "_describe_in_background").start()
        self.addCleanup(mock.patch.stopall)

    # ── the format decides how the story is staged ───────────────────────────

    def test_narration_asks_for_no_dialogue(self):
        self.assertIsNone(backend._build_dialogue_note("narration", []))
        self.assertIsNone(backend._story_format_note("narration"))

    def test_an_acted_format_carries_its_note_into_both_phases(self):
        body = backend.GenerateScriptBody(video_title="Dlg", topic="A topic",
                                          n_scenes=2, style_name="Hero",
                                          format="dialogue")
        with mock.patch.object(backend.story_mode, "generate_story",
                               return_value=_fake_story(2)) as gen, \
             mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(2), "m", "st", [])) as div:
            backend._do_script_generate(body)
        # the draft learns only WHO tells the story — people who can speak —
        # with none of the scene schema or clip budgets …
        draft_note = gen.call_args.kwargs["dialogue_note"]
        self.assertIn("PERFORMED STORY", draft_note)
        self.assertNotIn("HARD BUDGET", draft_note)
        # … the division is where the acted scene schema and budgets bind
        divide_note = div.call_args.kwargs["dialogue_note"]
        self.assertIn("ACTED SCENES", divide_note)
        self.assertIn("HARD BUDGET", divide_note)

    def test_divide_note_keeps_topic_instructions_and_mixing(self):
        # the narrator's own beats (e.g. a topic-requested self-introduction)
        # survive the mode balance, and a mixed film must actually mix
        dlg = backend._build_dialogue_note("dialogue", ["Kinho"])
        self.assertIn("TOPIC/DIRECTION outranks", dlg)
        mixed = backend._build_dialogue_note("mixed", ["Kinho"])
        self.assertIn("TOPIC/DIRECTION outranks", mixed)
        self.assertIn("actually MIX", mixed)
        self.assertIn("MIXED STORY", backend._story_format_note("mixed"))

    def test_an_all_acted_film_is_measured_in_clips_not_words(self):
        # 1 minute of narration is ~10 scenes of words; 1 minute of acted film
        # is 6 clips of ten seconds.
        body = backend.GenerateScriptBody(video_title="Dlg", topic="t",
                                          minutes=1.0, style_name="Hero",
                                          format="dialogue")
        with mock.patch.object(backend.story_mode, "generate_story",
                               return_value=_fake_story(6)) as gen, \
             mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(6), "m", "st", [])):
            backend._do_script_generate(body)
        self.assertEqual(gen.call_args.args[1], 6)

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
        self.assertEqual(brief["format"], "narration")
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
        # same work dir and job id as the draft
        self.assertEqual(res["work_dir"], str(wd))
        self.assertEqual(res["job_id"], draft["job_id"])
        self.assertEqual(len(res["scenes"]), 4)
        self.assertTrue((wd / "script.json").exists())
        self.assertEqual(res["create_brief"]["format"], "narration")

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

    # ── redraft: retell the story at a new scene count ───────────────────────

    def test_redraft_merges_edits_persists_story_and_brief(self):
        draft = self._draft(4)
        wd = Path(draft["work_dir"])
        body = backend.StoryRedraftBody(
            n_scenes=10,
            chapters=[backend.StoryChapterEdit(chapter=1, text="EDITED before redraft.")])
        with mock.patch.object(backend.story_mode, "redraft_story",
                               return_value=_fake_story(10)) as rd:
            res = backend._do_story_redraft(draft["job_id"], body)
        # the (edited) story and the new count reached redraft_story
        story_arg, n_arg = rd.call_args.args
        self.assertEqual(story_arg["chapters"][0]["text"], "EDITED before redraft.")
        self.assertEqual(n_arg, 10)
        # redrafted story persisted; the brief follows the new count
        on_disk = json.loads((wd / "story.json").read_text())
        self.assertEqual(on_disk["n_scenes"], 10)
        brief = json.loads((wd / "create_brief.json").read_text())
        self.assertEqual(brief["n_scenes"], 10)
        self.assertEqual(res["n_scenes"], 10)

    def test_redraft_rejects_bad_count_and_missing_draft(self):
        draft = self._draft(4)
        with self.assertRaises(HTTPException) as ctx:
            backend._do_story_redraft(draft["job_id"],
                                      backend.StoryRedraftBody(n_scenes=0))
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            backend._do_story_redraft(draft["job_id"],
                                      backend.StoryRedraftBody(n_scenes=201))
        self.assertEqual(ctx.exception.status_code, 400)
        # a classic script (no story.json) 404s
        wd = self.output_dir / "classic-script"
        wd.mkdir()
        (wd / "script.json").write_text("[]")
        job_id = backend.job_id_from_work_dir(wd)
        with self.assertRaises(HTTPException) as ctx:
            backend._do_story_redraft(job_id, backend.StoryRedraftBody(n_scenes=10))
        self.assertEqual(ctx.exception.status_code, 404)

    # ── headless chain ───────────────────────────────────────────────────────

    def test_do_script_generate_chains_both_phases_headless(self):
        body = backend.GenerateScriptBody(video_title="Auto Story", topic="A topic",
                                          n_scenes=4, style_name="Hero")
        with mock.patch.object(backend.story_mode, "generate_story",
                               return_value=_fake_story(4)) as gen, \
             mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(4), "m", "st", [])) as div:
            res = backend._do_script_generate(body)
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(div.call_count, 1)
        wd = Path(res["work_dir"])
        self.assertTrue((wd / "story.json").exists())
        self.assertTrue((wd / "script.json").exists())
        self.assertEqual(len(res["scenes"]), 4)

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

    def test_duplicate_carries_story_draft_across(self):
        draft = self._draft(4)
        wd = Path(draft["work_dir"])
        with mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(4), "m", "st", [])):
            backend._do_story_divide(backend.DivideStoryBody(work_dir=str(wd)))
        dup = backend.duplicate_script(backend.DuplicateScriptBody(work_dir=str(wd)))
        new_wd = Path(dup["work_dir"])
        self.assertNotEqual(new_wd, wd)
        # the prose draft travels with the copy (status "divided" is fine — the
        # Story tab shows for any story.json)
        story = json.loads((new_wd / "story.json").read_text())
        self.assertEqual(story["chapters"][0]["text"], "Original chapter prose.")
        self.assertEqual(story["status"], "divided")

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
        def fake(scenes, title, video_title=None, avoid_hint=None, pass_num=1, **kw):
            seen.append(pass_num)
            return {"changed": True, "notes": [], "order": None, "inserts": [],
                    "deletes": [],
                    "rewrites": [{"id": 1, "narration": f"Pass {pass_num}."}]}
        with mock.patch.object(backend.story_mode, "critique_scenes", side_effect=fake):
            backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
            backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        self.assertEqual(seen, [1, 2])

    def test_critic_guardrail_rewrites_visual_prompt_only(self):
        job = self._divided_job(3)
        ops = {"changed": True, "notes": ["human in a robots-only world"],
               "order": None, "inserts": [], "deletes": [],
               "rewrites": [{"id": 2, "image_prompt": "Chrome robot workers on the line."}]}
        captured = {}
        def fake(scenes, title, video_title=None, avoid_hint=None, pass_num=1,
                 direction="", dup_note="", **kw):
            captured["scenes"] = scenes
            if captured.get("done"):
                return {"changed": False, "notes": [], "rewrites": [],
                        "deletes": [], "inserts": [], "order": None}
            captured["done"] = True
            return ops
        with mock.patch.object(backend.story_mode, "critique_scenes", side_effect=fake):
            res = backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        # the critic sees the visual prompts (guardrail input) — including the
        # baked-in visual-style prefix it is told to preserve
        self.assertTrue(captured["scenes"][0]["image_prompt"].endswith("img"))
        self.assertEqual(captured["scenes"][0]["video_prompt"], "vid")
        # ...and a visual-only rewrite lands without touching the narration
        self.assertEqual(res["scenes"][1]["image_prompt"], "Chrome robot workers on the line.")
        self.assertEqual(res["scenes"][1]["narration"], "Narration 2.")

    def test_critic_receives_detected_duplicates_and_direction(self):
        job = self._divided_job(3)
        # plant an unmistakable near-duplicate pair involving the final scene
        store = backend.DurableStore.default()
        try:
            for sid in (1, 3):
                cur = store.get_scene(job["job_id"], sid)
                store.upsert_scene(job["job_id"], sid,
                                   title=cur.get("title") or "",
                                   image_prompt=cur.get("image_prompt") or "",
                                   video_prompt=cur.get("video_prompt") or "",
                                   narration="The empire endures for a thousand more years of light.",
                                   metadata=cur.get("metadata") or {})
        finally:
            store.close()
        captured = {}
        def fake(scenes, title, video_title=None, avoid_hint=None, pass_num=1,
                 direction="", dup_note="", **kw):
            captured.update(direction=direction, dup_note=dup_note)
            return {"changed": False, "notes": [], "rewrites": [], "deletes": [],
                    "inserts": [], "order": None}
        with mock.patch.object(backend.story_mode, "critique_scenes", side_effect=fake):
            res = backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        self.assertIn("scenes 1 and 3", captured["dup_note"])
        self.assertTrue(captured["direction"])
        # critic declined despite the hint → surfaced in the report notes
        self.assertTrue(any("duplicate detector still flags" in n_
                            for n_ in res["passes"][0]["notes"]))

    def test_critic_json_keeps_a_run_history(self):
        job = self._divided_job(4)
        run1 = {"changed": True, "notes": ["tightened scene 2"], "order": None,
                "inserts": [], "deletes": [],
                "rewrites": [{"id": 2, "narration": "Rewritten by run one."}]}
        run2 = {"changed": False, "notes": ["clean"], "rewrites": [],
                "deletes": [], "inserts": [], "order": None}
        with mock.patch.object(backend.story_mode, "critique_scenes", return_value=run1):
            backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        with mock.patch.object(backend.story_mode, "critique_scenes", return_value=run2):
            backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        crit = json.loads((Path(job["work_dir"]) / "critic.json").read_text())
        # the earlier, non-converged run survives the later one
        self.assertEqual(len(crit["runs"]), 2)
        self.assertFalse(crit["runs"][0]["converged"])
        self.assertEqual(crit["runs"][0]["passes"][0]["rewrites"], 1)
        self.assertEqual(crit["runs"][0]["passes"][0]["notes"], ["tightened scene 2"])
        self.assertTrue(crit["runs"][1]["converged"])
        # top-level still describes the latest run
        self.assertTrue(crit["converged"])
        self.assertEqual(crit["total_passes"], 2)

    def test_critic_order_omission_is_treated_as_deletion(self):
        # The critic drops scene 3 by omitting it from `order` (no explicit
        # `deletes`). The omission must take effect, not be silently ignored.
        job = self._divided_job(4)
        ops = {"changed": True, "notes": ["removed the redundant scene 3"],
               "rewrites": [], "deletes": [], "inserts": [], "order": [1, 2, 4]}
        with mock.patch.object(backend.story_mode, "critique_scenes", return_value=ops):
            res = backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        self.assertEqual(res["passes"][0]["deleted"], [3])
        self.assertEqual([s["id"] for s in res["scenes"]], [1, 2, 3])
        self.assertEqual([s["narration"] for s in res["scenes"]],
                         ["Narration 1.", "Narration 2.", "Narration 4."])

    def test_critic_order_omission_cannot_drop_first_or_final_scene(self):
        job = self._divided_job(4)
        # order omits scene 1 (hook) and scene 4 (payoff) — both protected
        ops = {"changed": True, "notes": [], "rewrites": [], "deletes": [],
               "inserts": [], "order": [2, 3]}
        with mock.patch.object(backend.story_mode, "critique_scenes", return_value=ops):
            res = backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        self.assertEqual(len(res["scenes"]), 4)
        self.assertEqual(res["passes"][0]["deleted"], [])

    def test_critic_explicit_delete_of_final_scene_is_refused(self):
        job = self._divided_job(3)
        ops = {"changed": True, "notes": [], "rewrites": [], "inserts": [],
               "order": None, "deletes": [3]}
        with mock.patch.object(backend.story_mode, "critique_scenes", return_value=ops):
            res = backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        self.assertEqual(len(res["scenes"]), 3)
        self.assertEqual(res["passes"][0]["deleted"], [])

    def test_critic_cannot_wipe_the_script_hook_and_payoff_survive(self):
        # A degenerate "delete everything" response: the first and final scenes
        # are protected, so the script can never be emptied.
        job = self._divided_job(4)
        ops = {"changed": True, "notes": [], "rewrites": [], "inserts": [],
               "deletes": [1, 2, 3, 4], "order": None}
        with mock.patch.object(backend.story_mode, "critique_scenes", return_value=ops):
            res = backend._do_critic_run(job["job_id"], backend.CriticRunBody(passes=1))
        self.assertEqual(res["passes"][0]["deleted"], [2, 3])
        self.assertEqual([s["narration"] for s in res["scenes"]],
                         ["Narration 1.", "Narration 4."])

    # ── automation auto-critic ───────────────────────────────────────────────

    def test_auto_critic_runs_before_result_when_flagged(self):
        body = backend.GenerateScriptBody(video_title="Auto QC", topic="t", n_scenes=3,
                                          style_name="Plain", auto_critic=True)
        ops = {"changed": True, "notes": [], "order": None, "inserts": [],
               "deletes": [2],   # a middle scene: first/final are protected
               "rewrites": [{"id": 1, "narration": "Critiqued."}]}
        with stub_script(_fake_scenes(3)), \
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
        with stub_script(_fake_scenes(2)), \
             mock.patch.object(backend.story_mode, "critique_scenes") as crit:
            res = backend._do_script_generate(backend.GenerateScriptBody(
                video_title="No QC", topic="t", n_scenes=2, style_name="Plain"))
        crit.assert_not_called()
        self.assertEqual(len(res["scenes"]), 2)
        # and a critic crash never fails script creation
        with stub_script(_fake_scenes(2)), \
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

    def test_get_job_story_roundtrip_and_404_without_a_draft(self):
        draft = self._draft(4)
        story = backend.get_job_story(draft["job_id"])
        self.assertEqual(story["status"], "draft")
        # a script folder with no story.json — e.g. written before story-first
        wd = self.output_dir / "older-script"
        wd.mkdir()
        (wd / "script.json").write_text("[]")
        with self.assertRaises(HTTPException) as ctx:
            backend.get_job_story(backend.job_id_from_work_dir(wd))
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
