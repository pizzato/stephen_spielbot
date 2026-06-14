"""Style profiles (issue #66).

Covers the config-level machinery (migration of a pre-styles config, fresh
installs, normalization + flat-key mirroring, style_settings resolution,
voice rename/delete propagation) and the two job-creation paths that consume
a profile: start_generation (render quality + audio mix into job_config.json)
and _start_queue_item (queue items carrying gen_style_name).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import yaml

import app
import webapp.backend.main as backend
from pipeline.orchestrator import DurableStore, job_id_from_work_dir


def _style(name, **overrides):
    """A fully-populated style dict with recognizable per-name values."""
    base = {
        "name": name,
        "description": f"{name} look",
        "visual_style": f"{name} visual",
        "extra_instructions": f"{name} instructions",
        "title_style": f"{name} title style",
        "voice": f"{name}-voice",
        "voice_robotic": False,
        "voice_robotic_amount": 0.2,
        "n_scenes": 7,
        "resolution": "Landscape HD (1024×576)",
        "lora_strength": 0.4,
        "first_pass_cfg": 1.0,
        "first_pass_steps": 8,
        "second_pass_cfg": 2.0,
        "second_pass_steps": 4,
        "music_vol": 11,
        "voice_vol": 110,
        "ambient_vol": 1,
    }
    base.update(overrides)
    return base


class TempConfigCase(unittest.TestCase):
    """Point app.CONFIG_FILE (and friends) at per-test temp locations.

    pytest imports every test module into one process, so the module-level
    HOME override only binds for whichever module imports app first — patch
    the already-imported module's paths explicitly instead.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-styles-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.config_file = tmp / "config" / "config.yaml"
        self.config_file.parent.mkdir(parents=True)
        self.output_dir = tmp / "videos"
        self.output_dir.mkdir()
        for target, attr, value in [
            (app, "CONFIG_FILE", self.config_file),
            (app, "VOICES_DIR", self.config_file.parent / "voices"),
            (app, "OUTPUT_DIR", self.output_dir),
        ]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        db = mock.patch.dict(os.environ, {"VIDEO_GEN_DB": str(tmp / "orchestrator.sqlite3")})
        db.start()
        self.addCleanup(db.stop)

    def write_config(self, data: dict) -> None:
        self.config_file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def read_config(self) -> dict:
        return yaml.safe_load(self.config_file.read_text())


class MigrationTests(TempConfigCase):
    def test_fresh_install_gets_blank_default_style(self):
        cfg = app.load_config()
        self.assertEqual([s["name"] for s in cfg["styles"]], [app.BLANK_STYLE_NAME])
        self.assertEqual(cfg["default_style"], app.BLANK_STYLE_NAME)
        st = cfg["styles"][0]
        self.assertEqual(st["visual_style"], "")
        self.assertEqual(st["extra_instructions"], "")
        self.assertEqual(st["voice"], "")
        # mirror matches the (blank) default style
        self.assertEqual(cfg["default_visual_style"], "")
        self.assertEqual(cfg["music_vol"], app.DEFAULT_CFG["music_vol"])

    def test_install_seeded_worker_lists_still_count_as_fresh(self):
        self.write_config({"comfy_workers": ["http://s1:8188"], "tts_workers": ["s1"]})
        cfg = app.load_config()
        self.assertEqual([s["name"] for s in cfg["styles"]], [app.BLANK_STYLE_NAME])

    def test_legacy_config_becomes_stephen_spielbot_style(self):
        self.write_config({
            "default_visual_style": "Cel-shaded graphic novel",
            "script_extra_instructions": "Always sign off.",
            "default_voice": "Luiz",
            "default_voice_robotic": True,
            "default_voice_robotic_amount": 0.5,
            "default_n_scenes": 6,
            "resolution": "Landscape 720p (1280×720)",
            "lora_strength": 0.7,
            "music_vol": 2,
            "voice_vol": 200,
            "ambient_vol": 1,
        })
        cfg = app.load_config()
        self.assertEqual([s["name"] for s in cfg["styles"]], [app.LEGACY_STYLE_NAME])
        st = cfg["styles"][0]
        self.assertEqual(st["visual_style"], "Cel-shaded graphic novel")
        self.assertEqual(st["extra_instructions"], "Always sign off.")
        self.assertEqual(st["voice"], "Luiz")
        self.assertTrue(st["voice_robotic"])
        self.assertEqual(st["voice_robotic_amount"], 0.5)
        self.assertEqual(st["n_scenes"], 6)
        self.assertEqual(st["resolution"], "Landscape 720p (1280×720)")
        self.assertEqual(st["lora_strength"], 0.7)
        self.assertEqual(st["music_vol"], 2)
        self.assertEqual(cfg["default_style"], app.LEGACY_STYLE_NAME)
        # mirror unchanged: the flat keys still expose the same values
        self.assertEqual(cfg["default_visual_style"], "Cel-shaded graphic novel")
        self.assertEqual(cfg["voice_vol"], 200)

    def test_default_style_settings_win_over_stale_flat_keys(self):
        self.write_config({
            "styles": [_style("A"), _style("B", music_vol=99)],
            "default_style": "B",
            # stale mirror from before an external edit
            "music_vol": 1,
            "default_visual_style": "stale",
        })
        cfg = app.load_config()
        self.assertEqual(cfg["music_vol"], 99)
        self.assertEqual(cfg["default_visual_style"], "B visual")

    def test_save_config_normalizes_names_and_default(self):
        cfg = app.load_config()
        cfg["styles"] = [_style("Dup"), _style("Dup"), {"name": "  ", "music_vol": 5}]
        cfg["default_style"] = "missing"
        app.save_config(cfg)
        on_disk = self.read_config()
        self.assertEqual([s["name"] for s in on_disk["styles"]], ["Dup", "Dup 2"])
        self.assertEqual(on_disk["default_style"], "Dup")
        # mirror follows the default style
        self.assertEqual(on_disk["music_vol"], 11)


class StyleSettingsTests(TempConfigCase):
    def test_named_style_resolves(self):
        self.write_config({"styles": [_style("A"), _style("B", n_scenes=33)], "default_style": "A"})
        cfg = app.load_config()
        ss = app.style_settings(cfg, "B")
        self.assertEqual(ss["name"], "B")
        self.assertEqual(ss["n_scenes"], 33)
        self.assertEqual(ss["voice"], "B-voice")

    def test_unknown_or_blank_name_falls_back_to_default_style(self):
        self.write_config({"styles": [_style("A"), _style("B")], "default_style": "B"})
        cfg = app.load_config()
        self.assertEqual(app.style_settings(cfg, "nope")["name"], "B")
        self.assertEqual(app.style_settings(cfg)["name"], "B")

    def test_flat_keys_back_fill_when_no_styles(self):
        # legacy-shaped dict (e.g. an old test fixture or job config)
        ss = app.style_settings({"default_visual_style": "Noir"})
        self.assertEqual(ss["visual_style"], "Noir")
        self.assertEqual(ss["n_scenes"], app.DEFAULT_CFG["default_n_scenes"])

    def test_compose_visual_style_uses_named_profile(self):
        self.write_config({
            "styles": [_style("A", visual_style="Cinematic"), _style("B", visual_style="Anime")],
            "default_style": "A",
        })
        cfg = app.load_config()
        self.assertEqual(app._compose_visual_style("", cfg, "B"), "Anime")
        self.assertEqual(app._compose_visual_style("Anime", cfg, "B"), "Anime")
        self.assertEqual(app._compose_visual_style("", cfg), "Cinematic")
        self.assertEqual(app._compose_visual_style("Noir", cfg, "B"), "Noir. Anime")

    def test_no_style_blanks_content_but_keeps_render_and_mix(self):
        self.write_config({
            "styles": [_style("A", music_vol=77), _style("B")],
            "default_style": "A",
        })
        cfg = app.load_config()
        ss = app.style_settings(cfg, app.NO_STYLE)
        self.assertEqual(ss["name"], app.NO_STYLE)
        # nothing content-shaped is imposed…
        self.assertEqual(ss["visual_style"], "")
        self.assertEqual(ss["extra_instructions"], "")
        self.assertEqual(ss["title_style"], "")
        self.assertEqual(ss["voice"], "")
        self.assertFalse(ss["voice_robotic"])
        # …but render quality + audio mix still come from the default style
        self.assertEqual(ss["music_vol"], 77)
        self.assertEqual(ss["resolution"], "Landscape HD (1024×576)")
        self.assertEqual(ss["lora_strength"], 0.4)

    def test_no_style_suppresses_profile_visual_style_merge(self):
        self.write_config({
            "styles": [_style("A", visual_style="Cinematic")],
            "default_style": "A",
        })
        cfg = app.load_config()
        self.assertEqual(app._compose_visual_style("Noir", cfg, app.NO_STYLE), "Noir")
        self.assertEqual(app._compose_visual_style("", cfg, app.NO_STYLE), "")

    def test_reserved_no_style_name_is_not_claimable(self):
        self.write_config({
            "styles": [_style(app.NO_STYLE), _style("B")],
            "default_style": app.NO_STYLE,
        })
        cfg = app.load_config()
        names = [s["name"] for s in cfg["styles"]]
        self.assertNotIn(app.NO_STYLE, names)   # renamed away from the sentinel
        self.assertIn("B", names)
        self.assertNotEqual(cfg["default_style"], app.NO_STYLE)


class VoicePropagationTests(TempConfigCase):
    def _seed_voices(self):
        voice_path = self.config_file.parent / "voices" / "old.wav"
        voice_path.parent.mkdir(parents=True, exist_ok=True)
        voice_path.write_bytes(b"x" * 10)
        self.write_config({
            "voices": [{"name": "Old", "path": str(voice_path)}],
            "styles": [_style("A", voice="Old"), _style("B", voice="Other")],
            "default_style": "A",
        })

    def test_rename_voice_updates_styles(self):
        self._seed_voices()
        cfg = app.update_voice("Old", new_name="New")
        by_name = {s["name"]: s for s in cfg["styles"]}
        self.assertEqual(by_name["A"]["voice"], "New")
        self.assertEqual(by_name["B"]["voice"], "Other")
        self.assertEqual(cfg["default_voice"], "New")  # mirror of default style

    def test_delete_voice_clears_styles(self):
        self._seed_voices()
        cfg = app.delete_voice("Old")
        by_name = {s["name"]: s for s in cfg["styles"]}
        self.assertEqual(by_name["A"]["voice"], "")
        self.assertEqual(by_name["B"]["voice"], "Other")


class StartGenerationStyleTests(TempConfigCase):
    """The chosen style's render quality + audio mix must land in
    job_config.json — that file is what the resumable render worker reads."""

    def _seed(self):
        self.write_config({
            "styles": [
                _style("A"),
                _style("B", music_vol=42, voice_vol=142, ambient_vol=3,
                       lora_strength=0.9, first_pass_steps=20, second_pass_steps=9,
                       resolution="Landscape HD (1024×576)", voice_robotic=True,
                       voice_robotic_amount=0.8, voice_speed=0.85),
            ],
            "default_style": "A",
        })
        work_dir = self.output_dir / "styled-job-20260610-101010"
        work_dir.mkdir()
        job_id = job_id_from_work_dir(work_dir)
        store = DurableStore.default()
        try:
            store.create_or_update_job(job_id, work_dir, "Styled job",
                                       config={"style_name": "B"})
            store.upsert_scenes(job_id, [{
                "id": 1, "title": "One", "image_prompt": "a frame",
                "video_prompt": "a move", "narration": "words",
            }])
        finally:
            store.close()
        return job_id, work_dir

    def test_job_config_carries_the_jobs_style(self):
        job_id, work_dir = self._seed()
        with mock.patch.object(app, "_launch_generation_job", return_value={}):
            backend.start_generation(backend.GenerateBody(
                job_id=job_id, work_dir=str(work_dir), video_title="Styled job",
            ))
        jc = json.loads((work_dir / "job_config.json").read_text())
        self.assertEqual(jc["style_name"], "B")
        self.assertEqual(jc["music_vol"], 42)
        self.assertEqual(jc["voice_vol"], 142)
        self.assertEqual(jc["ambient_vol"], 3)
        self.assertEqual(jc["lora_strength"], 0.9)
        self.assertEqual(jc["first_pass_steps"], 20)
        self.assertEqual(jc["second_pass_steps"], 9)
        self.assertEqual(jc["resolution"], "Landscape HD (1024×576)")
        self.assertTrue(jc["voice_robotic"])
        self.assertEqual(jc["voice_robotic_amount"], 0.8)
        self.assertEqual(jc["voice_speed"], 0.85)
        self.assertNotIn("styles", jc)  # the snapshot stores resolved values only

    def test_explicit_style_name_overrides_stored_one(self):
        job_id, work_dir = self._seed()
        with mock.patch.object(app, "_launch_generation_job", return_value={}):
            backend.start_generation(backend.GenerateBody(
                job_id=job_id, work_dir=str(work_dir), video_title="Styled job",
                style_name="A",
            ))
        jc = json.loads((work_dir / "job_config.json").read_text())
        self.assertEqual(jc["style_name"], "A")
        self.assertEqual(jc["music_vol"], 11)

    def test_no_style_render_keeps_default_mix_but_imposes_nothing(self):
        job_id, work_dir = self._seed()
        with mock.patch.object(app, "_launch_generation_job", return_value={}):
            backend.start_generation(backend.GenerateBody(
                job_id=job_id, work_dir=str(work_dir), video_title="Styled job",
                style_name=app.NO_STYLE,
            ))
        jc = json.loads((work_dir / "job_config.json").read_text())
        self.assertEqual(jc["style_name"], app.NO_STYLE)
        self.assertEqual(jc["music_vol"], 11)        # default style A's mix
        self.assertEqual(jc["default_voice"], "")    # no voice imposed → F5 default
        self.assertFalse(jc["voice_robotic"])
        self.assertEqual(jc["voice_speed"], 1.0)     # natural pace, not style A's


class QueueItemStyleTests(TempConfigCase):
    def test_queue_item_render_uses_its_style_profile(self):
        self.write_config({
            "styles": [
                _style("A"),
                _style("B", extra_instructions="Speak like B.", visual_style="B-vision"),
            ],
            "default_style": "A",
        })
        item = {"id": "q1", "final_title": "Styled request", "status": "pending",
                "gen_style_name": "B"}
        scenes = [backend.Scene(id=1, title="One", image_prompt="i", video_prompt="v",
                                narration="n")]
        updates = {}

        def fake_update(item_id, **kw):
            updates.setdefault(item_id, {}).update(kw)

        with mock.patch.object(backend, "generate_script",
                               return_value=(scenes, "calm piano", "B-vision")) as gen, \
             mock.patch.object(backend.yt, "load_queue", return_value=[dict(item)]), \
             mock.patch.object(backend.yt, "update_queue_item", side_effect=fake_update), \
             mock.patch.object(backend.gapp, "_launch_generation_job") as launch:
            out = backend._start_queue_item(dict(item))

        # The LLM prompt carried style B's extra instructions and visual style.
        topic_arg, _n, style_hint = gen.call_args[0][:3]
        self.assertIn("Speak like B.", topic_arg)
        self.assertEqual(style_hint, "B-vision")
        # The render launched straight away and its job_config carries the
        # profile: style B's name, voice and resolution drive the worker.
        launch.assert_called_once()
        self.assertEqual(updates["q1"]["status"], "creating")
        jc = json.loads((Path(out["work_dir"]) / "job_config.json").read_text())
        self.assertEqual(jc["style_name"], "B")
        self.assertEqual(jc["default_voice"], "B-voice")
        self.assertEqual(jc["resolution"], "Landscape HD (1024×576)")


class DescriptionSuffixTests(TempConfigCase):
    """description_suffix is per-style: each video's description gets ITS
    style's suffix, and a config migrated before the field existed hands the
    flat value to the default style only."""

    def _seed(self, style_name_on_job: str):
        self.write_config({
            "styles": [
                _style("A", description_suffix="SPIELBOT SIGNOFF"),
                _style("B", description_suffix="KIDS SIGNOFF"),
            ],
            "default_style": "A",
        })
        wd = self.output_dir / "suffix-job-20260610-121212"
        wd.mkdir()
        (wd / "job_config.json").write_text(json.dumps({"style_name": style_name_on_job}))
        return wd

    def test_description_uses_the_jobs_style_suffix(self):
        wd = self._seed("B")
        with mock.patch.object(backend.llm, "generate_youtube_description", return_value="BODY"):
            desc = backend._generate_youtube_description(str(wd), "Kids film")
        self.assertIn("KIDS SIGNOFF", desc)
        self.assertNotIn("SPIELBOT SIGNOFF", desc)

    def test_no_style_job_gets_no_suffix(self):
        wd = self._seed(app.NO_STYLE)
        with mock.patch.object(backend.llm, "generate_youtube_description", return_value="BODY"):
            desc = backend._generate_youtube_description(str(wd), "Experiment")
        self.assertEqual(desc, "BODY")

    def test_late_added_style_field_backfills_default_style_only(self):
        # A config that migrated BEFORE description_suffix became per-style:
        # styles carry no suffix field, the flat key still holds the value.
        styles = [_style("A"), _style("B")]
        for s in styles:
            s.pop("description_suffix", None)
        self.write_config({
            "styles": styles,
            "default_style": "A",
            "description_suffix": "SPIELBOT SIGNOFF",
        })
        cfg = app.load_config()
        by_name = {s["name"]: s for s in cfg["styles"]}
        self.assertEqual(by_name["A"]["description_suffix"], "SPIELBOT SIGNOFF")
        self.assertEqual(by_name["B"]["description_suffix"], "")
        self.assertEqual(cfg["description_suffix"], "SPIELBOT SIGNOFF")  # mirror intact

    def test_script_generate_spawns_background_description(self):
        self.write_config({"styles": [_style("A")], "default_style": "A"})
        scenes = [backend.Scene(id=1, title="One", image_prompt="i", video_prompt="v",
                                narration="n")]
        with mock.patch.object(backend, "generate_script",
                               return_value=(scenes, "calm piano", "A visual")), \
             mock.patch.object(backend.threading, "Thread") as Thread:
            backend._do_script_generate(backend.GenerateScriptBody(
                video_title="Threaded", topic="Threaded", n_scenes=1))
        targets = [c.kwargs.get("target") for c in Thread.call_args_list]
        self.assertIn(backend._describe_in_background, targets)


class StyleAwareIdeasTests(TempConfigCase):
    """AI ideas belong to a style profile: generation is steered by it, ideas
    are stamped with it, and the cache keeps one set per style."""

    def _two_styles(self):
        self.write_config({
            "styles": [_style("A"), _style("B", description="Bedtime tales for kids")],
            "default_style": "A",
        })

    def test_style_suggestion_context_lines(self):
        from pipeline.llm import style_suggestion_context
        ctx = style_suggestion_context({"name": "Kids", "description": "Bedtime tales",
                                        "visual_style": "Soft pastel",
                                        "title_style": "pose a question"})
        self.assertIn('"Kids" style', ctx)
        self.assertIn("Bedtime tales", ctx)
        self.assertIn("Soft pastel", ctx)
        self.assertIn("pose a question", ctx)
        self.assertEqual(style_suggestion_context(None), "")
        self.assertEqual(style_suggestion_context({"name": "(none)"}), "")

    def test_title_style_steers_even_without_other_context(self):
        # A profile whose ONLY distinctive field is title_style still yields a
        # steering line — title wording is independent of topic suitability.
        from pipeline.llm import style_suggestion_context
        ctx = style_suggestion_context({"name": "(none)", "title_style": "short and punchy"})
        self.assertIn("short and punchy", ctx)
        self.assertNotIn("must suit", ctx)   # no topic-suitability line without descriptors

    def test_title_style_backfills_default_style_only(self):
        # A config migrated BEFORE title_style became per-style: styles carry no
        # title_style field, the flat key still holds the value. Only the default
        # style inherits it; others get the built-in blank (no cross-style leak).
        styles = [_style("A"), _style("B")]
        for s in styles:
            s.pop("title_style", None)
        self.write_config({
            "styles": styles,
            "default_style": "A",
            "title_style": "pose an intriguing question",
        })
        cfg = app.load_config()
        by_name = {s["name"]: s for s in cfg["styles"]}
        self.assertEqual(by_name["A"]["title_style"], "pose an intriguing question")
        self.assertEqual(by_name["B"]["title_style"], "")
        self.assertEqual(cfg["title_style"], "pose an intriguing question")  # mirror intact

    def test_generation_is_steered_and_stamped_per_style(self):
        self._two_styles()
        raw = [{"title": "Sleepy Dragon", "reason": "fits", "interestingness": 0.8}]
        saved = {}
        cached = [{"id": "old", "title": "Old A idea", "style_name": "A", "used": False}]
        with mock.patch.object(backend, "generate_video_suggestions", return_value=raw) as gen, \
             mock.patch.object(backend.gapp, "_channel_video_titles", return_value=[]), \
             mock.patch.object(backend.yt, "load_suggestions", return_value=list(cached)), \
             mock.patch.object(backend.yt, "save_suggestions", side_effect=lambda v: saved.setdefault("v", v)):
            out = backend.youtube_suggestions(guidance="", refresh=False, style_name="B")
        # Cache held only style-A ideas, so a request for B generates fresh ones…
        self.assertFalse(out["cached"])
        self.assertEqual(out["style_name"], "B")
        self.assertEqual(gen.call_args.kwargs["style"]["name"], "B")
        self.assertEqual([s["style_name"] for s in out["suggestions"]], ["B"])
        # …and the save merges: A's cached set survives alongside B's new one.
        styles_in_cache = {s.get("style_name") for s in saved["v"]}
        self.assertEqual(styles_in_cache, {"A", "B"})

    def test_cached_ideas_are_filtered_by_style(self):
        self._two_styles()
        cached = [
            {"id": "a1", "title": "A idea", "style_name": "A", "used": False},
            {"id": "b1", "title": "B idea", "style_name": "B", "used": False},
            {"id": "l1", "title": "Legacy idea", "used": False},  # pre-#66 → default style
        ]
        with mock.patch.object(backend.yt, "load_suggestions", return_value=cached):
            out_a = backend.youtube_suggestions(guidance="", refresh=False, style_name="A")
            out_b = backend.youtube_suggestions(guidance="", refresh=False, style_name="B")
        self.assertTrue(out_a["cached"] and out_b["cached"])
        self.assertEqual({s["title"] for s in out_a["suggestions"]}, {"A idea", "Legacy idea"})
        self.assertEqual({s["title"] for s in out_b["suggestions"]}, {"B idea"})

    def test_auto_pick_carries_idea_style_onto_queue_item(self):
        self._two_styles()
        suggestion = {"id": "s1", "title": "Sleepy Dragon", "reason": "fits",
                      "interestingness": 0.8, "style_name": "B", "used": False}
        entry = {"id": "q9"}
        updates = {}
        with mock.patch.object(app.yt, "load_suggestions", return_value=[dict(suggestion)]), \
             mock.patch.object(app.yt, "save_suggestions"), \
             mock.patch.object(app.yt, "add_to_queue", return_value=dict(entry)), \
             mock.patch.object(app.yt, "update_queue_item",
                               side_effect=lambda i, **kw: updates.setdefault(i, {}).update(kw)), \
             mock.patch.object(app, "generate_video_prompt", return_value=""):
            item = app._auto_pick_suggestion(app.load_config())
        self.assertEqual(item["gen_style_name"], "B")
        self.assertEqual(updates["q9"]["gen_style_name"], "B")


class ScriptGenerateTaskTests(unittest.TestCase):
    """The /api/script/generate endpoint kicks the (slow, multi-call) generation
    off in a background thread and returns a task id to poll — so a blip on the
    long connection no longer shows up as a NetworkError. The work itself lives in
    _do_script_generate, which the endpoint and server-side automation share."""

    def _poll(self, task_id):
        import time
        for _ in range(200):
            st = backend.script_generate_status(task_id=task_id)
            if st["status"] != "running":
                return st
            time.sleep(0.01)
        self.fail("task never left 'running'")

    def test_kickoff_returns_task_id_then_polls_to_done(self):
        result = {"job_id": "job_x", "work_dir": "/tmp/x", "scenes": [{"id": 1}], "style_name": "A"}
        with mock.patch.object(backend, "_do_script_generate", return_value=result):
            kicked = backend.script_generate(backend.GenerateScriptBody(video_title="T", n_scenes=1))
            self.assertIn("task_id", kicked)
            st = self._poll(kicked["task_id"])
        self.assertEqual(st["status"], "done")
        self.assertEqual(st["job_id"], "job_x")
        self.assertEqual(st["scenes"], [{"id": 1}])

    def test_failure_surfaces_a_clean_one_line_error(self):
        with mock.patch.object(backend, "_do_script_generate",
                               side_effect=RuntimeError("boom line1\nline2")):
            tid = backend.script_generate(backend.GenerateScriptBody(video_title="T"))["task_id"]
            st = self._poll(tid)
        self.assertEqual(st["status"], "error")
        self.assertEqual(st["error"], "boom line1")

    def test_unknown_task_is_404(self):
        with self.assertRaises(backend.HTTPException) as cm:
            backend.script_generate_status(task_id="does-not-exist")
        self.assertEqual(cm.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
