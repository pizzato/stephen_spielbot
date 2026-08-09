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
from scriptstub import stub_script
from pipeline.orchestrator import DurableStore, job_id_from_work_dir


def _style(name, **overrides):
    """A fully-populated style dict with recognizable per-name values."""
    base = {
        "name": name,
        "description": f"{name} look",
        "visual_style": f"{name} visual",
        "video_style": f"{name} motion",
        "extra_instructions": f"{name} instructions",
        "title_style": f"{name} title style",
        "voice": f"{name}-voice",
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
        # SPIELBOT_ORCHESTRATOR_DB, not VIDEO_GEN_DB — the wrong name meant every
        # test through this base wrote its jobs into the REAL orchestrator DB.
        db = mock.patch.dict(
            os.environ, {"SPIELBOT_ORCHESTRATOR_DB": str(tmp / "orchestrator.sqlite3")})
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

    def test_negative_prompt_and_script_avoid_default_blank_and_mirror(self):
        cfg = app.load_config()
        st = cfg["styles"][0]
        self.assertEqual(st["video_negative_prompt"], "")
        self.assertEqual(st["script_avoid"], "")
        # flat keys mirror the (blank) default style
        self.assertEqual(cfg["default_video_negative_prompt"], "")
        self.assertEqual(cfg["script_avoid"], "")

    def test_music_enabled_defaults_on_and_mirrors(self):
        self.write_config({
            "styles": [_style("A"), _style("B", music_enabled=False)],
            "default_style": "B",
        })
        cfg = app.load_config()
        by_name = {s["name"]: s for s in cfg["styles"]}
        self.assertTrue(by_name["A"]["music_enabled"])     # absent → scored
        self.assertFalse(by_name["B"]["music_enabled"])    # preserved
        # flat key mirrors the default style (B)
        self.assertFalse(cfg["music_enabled"])

    def test_install_seeded_worker_lists_still_count_as_fresh(self):
        self.write_config({"comfy_workers": ["http://s1:8188"], "tts_workers": ["s1"]})
        cfg = app.load_config()
        self.assertEqual([s["name"] for s in cfg["styles"]], [app.BLANK_STYLE_NAME])

    def test_legacy_config_becomes_stephen_spielbot_style(self):
        self.write_config({
            "default_visual_style": "Cel-shaded graphic novel",
            "script_extra_instructions": "Always sign off.",
            "default_voice": "Narrator",
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
        self.assertEqual(st["voice"], "Narrator")
        # The legacy on-toggle folds into the level: 0.5 survives, the toggle
        # keys are gone (style and flat).
        self.assertNotIn("voice_robotic", st)
        self.assertEqual(st["voice_robotic_amount"], 0.5)
        self.assertNotIn("default_voice_robotic", cfg)
        self.assertEqual(st["n_scenes"], 6)
        self.assertEqual(st["resolution"], "Landscape 720p (1280×720)")
        self.assertEqual(st["lora_strength"], 0.7)
        self.assertEqual(st["music_vol"], 2)
        self.assertEqual(cfg["default_style"], app.LEGACY_STYLE_NAME)
        # mirror unchanged: the flat keys still expose the same values
        self.assertEqual(cfg["default_visual_style"], "Cel-shaded graphic novel")
        self.assertEqual(cfg["voice_vol"], 200)

    def test_robotic_toggle_folds_into_level(self):
        # The removed "Robotic voice" toggle folds into the level once: a style
        # whose toggle resolved off gets an explicit 0 (natural), one that was
        # on keeps its effective level, sparse children keep inheriting, and
        # the legacy keys disappear from styles and flat mirror alike.
        self.write_config({
            "styles": [
                _style("A", voice_robotic=False, voice_robotic_amount=0.4),
                _style("B", voice_robotic=True, voice_robotic_amount=0.8),
                {"name": "B kid", "parent": "B", "voice_robotic": False},
                {"name": "B grandkid", "parent": "B kid"},
            ],
            "default_style": "B",
            "default_voice_robotic": True,
            "default_voice_robotic_amount": 0.8,
        })
        cfg = app.load_config()
        by = {s["name"]: s for s in cfg["styles"]}
        for s in cfg["styles"]:
            self.assertNotIn("voice_robotic", s)
        self.assertNotIn("default_voice_robotic", cfg)
        self.assertEqual(by["A"]["voice_robotic_amount"], 0.0)      # was off
        self.assertEqual(by["B"]["voice_robotic_amount"], 0.8)      # was on
        self.assertEqual(by["B kid"]["voice_robotic_amount"], 0.0)  # child opted out
        self.assertNotIn("voice_robotic_amount", by["B grandkid"])  # stays sparse
        self.assertEqual(app.style_settings(cfg, "B grandkid")["voice_robotic_amount"], 0.0)
        self.assertEqual(cfg["default_voice_robotic_amount"], 0.8)  # mirror of B

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


class RoboticLevelResolverTests(unittest.TestCase):
    """resolve_robotic_amount: new job configs/payloads carry only the level
    (0 = off); legacy ones gated it behind the removed voice_robotic toggle."""

    def test_resolver_handles_new_and_legacy_shapes(self):
        from pipeline.tts_worker import resolve_robotic_amount as resolve
        self.assertEqual(resolve({}), 0.0)
        self.assertEqual(resolve({"voice_robotic_amount": 0.5}), 0.5)
        self.assertEqual(resolve({"voice_robotic": False, "voice_robotic_amount": 0.5}), 0.0)
        self.assertEqual(resolve({"voice_robotic": True, "voice_robotic_amount": 0.5}), 0.5)
        self.assertEqual(resolve({"voice_robotic": True}), 0.35)   # legacy default level
        self.assertEqual(resolve({"default_voice_robotic": False,
                                  "default_voice_robotic_amount": 0.5}), 0.0)
        self.assertEqual(resolve({"voice_robotic_amount": 7}), 1.0)      # clamped
        self.assertEqual(resolve({"voice_robotic_amount": "bad"}), 0.0)  # junk-safe


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
        self.assertEqual(ss["video_style"], "")
        self.assertEqual(ss["extra_instructions"], "")
        self.assertEqual(ss["title_style"], "")
        self.assertEqual(ss["voice"], "")
        self.assertEqual(ss["voice_robotic_amount"], 0.0)
        # …including the open-source attribution family (footer / X hashtags /
        # YouTube keyword tags): experiment mode carries no "Generated with…" credit
        self.assertEqual(ss["attribution_description"], "")
        self.assertEqual(ss["attribution_hashtags"], "")
        self.assertEqual(ss["attribution_youtube_tags"], "")
        # but a real style still gets the default attribution — only NO_STYLE is affected
        real = app.style_settings(cfg, "A")
        self.assertTrue(real["attribution_description"])
        self.assertTrue(real["attribution_hashtags"])
        self.assertTrue(real["attribution_youtube_tags"])
        # …but render quality + audio mix still come from the default style
        self.assertEqual(ss["music_vol"], 77)
        self.assertEqual(ss["resolution"], "Landscape HD (1024×576)")
        self.assertEqual(ss["lora_strength"], 0.4)

    def test_negative_prompt_and_script_avoid_resolve_per_style(self):
        self.write_config({
            "styles": [_style("A"),
                       _style("B", video_negative_prompt="blurry, watermark",
                              script_avoid="no politics")],
            "default_style": "A",
        })
        cfg = app.load_config()
        ss = app.style_settings(cfg, "B")
        self.assertEqual(ss["video_negative_prompt"], "blurry, watermark")
        self.assertEqual(ss["script_avoid"], "no politics")
        # NO_STYLE imposes nothing content-shaped: both blank here (the render-time
        # video negative then falls back to the built-in default downstream).
        ns = app.style_settings(cfg, app.NO_STYLE)
        self.assertEqual(ns["script_avoid"], "")
        self.assertEqual(ns["video_negative_prompt"], "")

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
                # Legacy-shaped on purpose: the stored on-toggle must fold into
                # the level so an already-robotic style keeps its voice.
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
        self.assertNotIn("voice_robotic", jc)   # toggle folded into the level
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
        self.assertEqual(jc["voice_robotic_amount"], 0.0)   # natural voice
        self.assertEqual(jc["voice_speed"], 1.0)     # natural pace, not style A's

    def _seed_with_negative(self, negative):
        """Seed a job whose style carries a custom video negative prompt."""
        self.write_config({
            "styles": [_style("A"), _style("B", video_negative_prompt=negative)],
            "default_style": "A",
        })
        work_dir = self.output_dir / "neg-job-20260610-101010"
        work_dir.mkdir()
        job_id = job_id_from_work_dir(work_dir)
        store = DurableStore.default()
        try:
            store.create_or_update_job(job_id, work_dir, "Neg job", config={"style_name": "B"})
            store.upsert_scenes(job_id, [{
                "id": 1, "title": "One", "image_prompt": "a frame",
                "video_prompt": "a move", "narration": "words",
            }])
        finally:
            store.close()
        return job_id, work_dir

    def test_job_config_carries_the_styles_video_negative(self):
        job_id, work_dir = self._seed_with_negative("blurry, watermark, logo")
        with mock.patch.object(app, "_launch_generation_job", return_value={}):
            backend.start_generation(backend.GenerateBody(
                job_id=job_id, work_dir=str(work_dir), video_title="Neg job",
            ))
        jc = json.loads((work_dir / "job_config.json").read_text())
        self.assertEqual(jc["video_negative_prompt"], "blurry, watermark, logo")

    def test_blank_video_negative_falls_back_to_builtin_default(self):
        job_id, work_dir = self._seed_with_negative("")   # style B leaves it blank
        with mock.patch.object(app, "_launch_generation_job", return_value={}):
            backend.start_generation(backend.GenerateBody(
                job_id=job_id, work_dir=str(work_dir), video_title="Neg job",
            ))
        jc = json.loads((work_dir / "job_config.json").read_text())
        self.assertEqual(jc["video_negative_prompt"], backend.llm.NEGATIVE_PROMPT)


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

        with stub_script(scenes, music="calm piano", style="B-vision") as (draft, divide), \
             mock.patch.object(backend.yt, "load_queue", return_value=[dict(item)]), \
             mock.patch.object(backend.yt, "update_queue_item", side_effect=fake_update), \
             mock.patch.object(backend.gapp, "_launch_generation_job") as launch:
            out = backend._start_queue_item(dict(item))

        # The story prompt carried style B's extra instructions and visuals,
        # and the division carried its motion style.
        topic_arg, _n = draft.call_args[0][:2]
        self.assertIn("Speak like B.", topic_arg)
        self.assertEqual(draft.call_args.kwargs["style_hint"], "B-vision")
        self.assertEqual(divide.call_args.kwargs["video_style_hint"], "B motion")
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
        with stub_script(scenes, music="calm piano", style="A visual"), \
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

    def test_generate_appends_to_existing_style_cache(self):
        # "Generate more" grows the list: the existing idea survives and the
        # fresh one is appended, rather than replacing the set.
        self._two_styles()
        existing = [{"id": "a0", "title": "Old A", "style_name": "A",
                     "used": False, "created_at": 1.0}]
        raw = [{"title": "New A", "reason": "r", "interestingness": 0.7}]
        saved = {}
        with mock.patch.object(backend, "generate_video_suggestions", return_value=raw), \
             mock.patch.object(backend.gapp, "_channel_video_titles", return_value=[]), \
             mock.patch.object(backend.gapp, "_list_recent_jobs", return_value=[]), \
             mock.patch.object(backend.yt, "load_suggestions", return_value=list(existing)), \
             mock.patch.object(backend.yt, "save_suggestions",
                               side_effect=lambda v: saved.setdefault("v", list(v))):
            out = backend.youtube_suggestions(guidance="", refresh=True, style_name="A")
        self.assertEqual([s["title"] for s in out["suggestions"]], ["Old A", "New A"])
        self.assertEqual({s["title"] for s in saved["v"]}, {"Old A", "New A"})

    def test_generate_dedups_repeated_titles(self):
        # A regenerated title that already exists (case/space-insensitive) is
        # dropped, so repeated "Generate more" clicks don't pile up duplicates.
        self._two_styles()
        existing = [{"id": "a0", "title": "Dragon Tale", "style_name": "A", "used": False}]
        raw = [{"title": "  dragon   tale ", "reason": "r", "interestingness": 0.7},
               {"title": "Fresh One", "reason": "r", "interestingness": 0.8}]
        saved = {}
        with mock.patch.object(backend, "generate_video_suggestions", return_value=raw), \
             mock.patch.object(backend.gapp, "_channel_video_titles", return_value=[]), \
             mock.patch.object(backend.gapp, "_list_recent_jobs", return_value=[]), \
             mock.patch.object(backend.yt, "load_suggestions", return_value=list(existing)), \
             mock.patch.object(backend.yt, "save_suggestions",
                               side_effect=lambda v: saved.setdefault("v", list(v))):
            out = backend.youtube_suggestions(guidance="", refresh=True, style_name="A")
        self.assertEqual([s["title"] for s in out["suggestions"]], ["Dragon Tale", "Fresh One"])

    def test_generate_more_hands_existing_titles_to_the_generator(self):
        # Existing idea titles (and channel titles) are passed to the LLM so
        # "Generate more" produces new ideas instead of re-suggesting the list.
        self._two_styles()
        existing = [{"id": "a0", "title": "Old A", "style_name": "A", "used": False}]
        captured = {}

        def fake_gen(prev, cfg, style=None, discarded_titles=None):
            captured["previous"] = list(prev)
            return [{"title": "New A", "reason": "r", "interestingness": 0.7}]

        with mock.patch.object(backend, "generate_video_suggestions", side_effect=fake_gen), \
             mock.patch.object(backend.gapp, "_channel_video_titles", return_value=["Channel vid"]), \
             mock.patch.object(backend.gapp, "_list_recent_jobs", return_value=[]), \
             mock.patch.object(backend.yt, "load_suggestions", return_value=list(existing)), \
             mock.patch.object(backend.yt, "save_suggestions"):
            backend.youtube_suggestions(guidance="", refresh=True, style_name="A")
        self.assertIn("Old A", captured["previous"])
        self.assertIn("Channel vid", captured["previous"])

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


class AutoPickMixTests(TempConfigCase):
    """Auto-picked queue top-ups rotate across the eligible styles to mix them,
    a style opts out of the rotation via auto_pick_exclude (which also drops it
    from the AI ideas "All styles" mix, while it stays reachable on its own
    style page), and the AI ideas screen can show / generate that mix (#117)."""

    def _styles(self, *names, default=None, exclude=()):
        styles = [_style(n) for n in names]
        for s in styles:
            if s["name"] in exclude:
                s["auto_pick_exclude"] = True
        self.write_config({"styles": styles, "default_style": default or names[0]})

    # ── per-style opt-out field ──
    def test_auto_pick_exclude_round_trips_and_defaults_false(self):
        self._styles("A", "B", "C", exclude=("B",))
        cfg = app.load_config()
        by = {s["name"]: s for s in cfg["styles"]}
        self.assertFalse(by["A"]["auto_pick_exclude"])
        self.assertTrue(by["B"]["auto_pick_exclude"])
        self.assertFalse(by["C"]["auto_pick_exclude"])
        # mirror: the default style's flag lands on the flat key
        self.assertEqual(cfg["default_auto_pick_exclude"], by["A"]["auto_pick_exclude"])
        # eligible list, in config order, drops the excluded style
        self.assertEqual(app._auto_pick_styles(cfg), ["A", "C"])

    def test_every_style_excluded_means_no_auto_pick(self):
        self._styles("A", "B", exclude=("A", "B"))
        with mock.patch.object(app, "generate_video_suggestions") as gen:
            self.assertIsNone(app._auto_pick_suggestion(app.load_config()))
        gen.assert_not_called()

    # ── mixing: generation + rotation across eligible styles ──
    def _auto_pick(self, *, cached=None, queue=None, gen=None):
        """Run _auto_pick_suggestion with the queue/suggestion IO mocked."""
        mocks = [
            mock.patch.object(app, "_channel_video_titles", return_value=[]),
            mock.patch.object(app.yt, "load_suggestions",
                              return_value=[dict(s) for s in (cached or [])]),
            mock.patch.object(app.yt, "save_suggestions"),
            mock.patch.object(app.yt, "load_queue", return_value=list(queue or [])),
            mock.patch.object(app.yt, "add_to_queue", return_value={"id": "q9"}),
            mock.patch.object(app.yt, "update_queue_item"),
            mock.patch.object(app, "generate_video_prompt", return_value=""),
        ]
        if gen is not None:
            mocks.append(mock.patch.object(app, "generate_video_suggestions", side_effect=gen))
        for m in mocks:
            m.start()
            self.addCleanup(m.stop)
        return app._auto_pick_suggestion(app.load_config())

    @staticmethod
    def _idea(style, used=False):
        return {"id": f"{style}1", "title": f"{style}1", "reason": "r",
                "interestingness": 0.7, "style_name": style, "used": used}

    def test_auto_pick_generates_only_for_eligible_styles(self):
        self._styles("A", "B", "C", exclude=("B",))
        seen = []

        def fake_gen(titles, cfg, style=None, discarded_titles=None):
            seen.append(style["name"])
            return [{"title": f"{style['name']} idea", "reason": "r", "interestingness": 0.7}]

        item = self._auto_pick(cached=[], gen=fake_gen)
        self.assertEqual(set(seen), {"A", "C"})            # B is never generated
        self.assertIn(item["gen_style_name"], {"A", "C"})

    def test_auto_pick_rotates_to_next_style(self):
        self._styles("A", "B", "C")
        cached = [self._idea("A"), self._idea("B"), self._idea("C")]
        # Last auto-picked style was A → the next pick rotates to B.
        queue = [{"id": "q0", "source": "suggestion", "gen_style_name": "A", "created_at": 100.0}]
        item = self._auto_pick(cached=cached, queue=queue)
        self.assertEqual(item["gen_style_name"], "B")

    def test_auto_pick_rotation_skips_styles_without_ideas(self):
        # Last pick A, but only C has an unused idea → walk the rotation past B
        # (which has none) to C, rather than falling back to A.
        self._styles("A", "B", "C")
        queue = [{"id": "q0", "source": "suggestion", "gen_style_name": "A", "created_at": 1.0}]
        item = self._auto_pick(cached=[self._idea("C")], queue=queue)
        self.assertEqual(item["gen_style_name"], "C")

    def test_auto_pick_ignores_unused_idea_from_excluded_style(self):
        # B is excluded; its waiting idea must NOT be picked — a fresh batch is
        # generated for the eligible styles instead.
        self._styles("A", "B", exclude=("B",))

        def fake_gen(titles, cfg, style=None, discarded_titles=None):
            return [{"title": f"{style['name']} fresh", "reason": "r", "interestingness": 0.7}]

        item = self._auto_pick(cached=[self._idea("B")], gen=fake_gen)
        self.assertEqual(item["gen_style_name"], "A")

    # ── "All styles" AI ideas endpoint ──
    def test_all_styles_endpoint_unions_cached(self):
        self._styles("A", "B")
        cached = [
            {"id": "a1", "title": "A idea", "style_name": "A", "used": False},
            {"id": "b1", "title": "B idea", "style_name": "B", "used": False},
        ]
        with mock.patch.object(backend.yt, "load_suggestions", return_value=cached):
            out = backend.youtube_suggestions(guidance="", refresh=False, style_name=backend.ALL_STYLES)
        self.assertTrue(out["cached"])
        self.assertEqual(out["style_name"], backend.ALL_STYLES)
        self.assertEqual({s["title"] for s in out["suggestions"]}, {"A idea", "B idea"})

    def test_all_styles_endpoint_generates_across_styles(self):
        self._styles("A", "B")
        saved = {}

        def fake_gen(titles, cfg, style=None, discarded_titles=None):
            return [{"title": f"{style['name']} fresh", "reason": "r", "interestingness": 0.7}]

        with mock.patch.object(backend, "generate_video_suggestions", side_effect=fake_gen), \
             mock.patch.object(backend.gapp, "_channel_video_titles", return_value=[]), \
             mock.patch.object(backend.yt, "load_suggestions", return_value=[]), \
             mock.patch.object(backend.yt, "save_suggestions", side_effect=lambda s: saved.update(items=list(s))):
            out = backend.youtube_suggestions(guidance="", refresh=True, style_name=backend.ALL_STYLES)
        self.assertFalse(out["cached"])
        self.assertEqual({s["title"] for s in out["suggestions"]}, {"A fresh", "B fresh"})
        self.assertEqual({s["style_name"] for s in out["suggestions"]}, {"A", "B"})
        self.assertEqual({s["style_name"] for s in saved["items"]}, {"A", "B"})  # both persisted

    def test_all_styles_mix_drops_excluded_from_cached(self):
        # B is opted out → its cached idea must not surface in the "All styles"
        # mix, even though A's (eligible) does.
        self._styles("A", "B", exclude=("B",))
        cached = [
            {"id": "a1", "title": "A idea", "style_name": "A", "used": False},
            {"id": "b1", "title": "B idea", "style_name": "B", "used": False},
        ]
        with mock.patch.object(backend.yt, "load_suggestions", return_value=cached):
            out = backend.youtube_suggestions(guidance="", refresh=False, style_name=backend.ALL_STYLES)
        self.assertTrue(out["cached"])
        self.assertEqual({s["title"] for s in out["suggestions"]}, {"A idea"})

    def test_all_styles_mix_generates_only_for_eligible_and_keeps_excluded_cache(self):
        # B is opted out → the mix generates for A only; B's previously cached
        # idea survives in the pool so its own style page can still show it.
        self._styles("A", "B", exclude=("B",))
        saved, seen = {}, []

        def fake_gen(titles, cfg, style=None, discarded_titles=None):
            seen.append(style["name"])
            return [{"title": f"{style['name']} fresh", "reason": "r", "interestingness": 0.7}]

        existing = [{"id": "b1", "title": "B idea", "style_name": "B", "used": False}]
        with mock.patch.object(backend, "generate_video_suggestions", side_effect=fake_gen), \
             mock.patch.object(backend.gapp, "_channel_video_titles", return_value=[]), \
             mock.patch.object(backend.yt, "load_suggestions", return_value=existing), \
             mock.patch.object(backend.yt, "save_suggestions", side_effect=lambda s: saved.update(items=list(s))):
            out = backend.youtube_suggestions(guidance="", refresh=True, style_name=backend.ALL_STYLES)
        self.assertEqual(seen, ["A"])                                  # B is never generated
        self.assertEqual({s["title"] for s in out["suggestions"]}, {"A fresh"})
        # B's cached idea is preserved alongside the freshly generated A idea.
        self.assertEqual({s["title"] for s in saved["items"]}, {"B idea", "A fresh"})

    def test_excluded_style_still_pickable_on_its_own_page(self):
        # Selecting the opted-out style directly still returns its ideas — the
        # exclusion only hides it from the mix, not from manual selection.
        self._styles("A", "B", exclude=("B",))
        cached = [{"id": "b1", "title": "B idea", "style_name": "B", "used": False}]
        with mock.patch.object(backend.yt, "load_suggestions", return_value=cached):
            out = backend.youtube_suggestions(guidance="", refresh=False, style_name="B")
        self.assertTrue(out["cached"])
        self.assertEqual(out["style_name"], "B")
        self.assertEqual({s["title"] for s in out["suggestions"]}, {"B idea"})

    def test_all_styles_mix_appends_to_existing(self):
        # Generating the mix keeps the eligible styles' existing ideas and
        # appends the fresh batch — the mix grows instead of being replaced.
        self._styles("A", "B")
        existing = [{"id": "a0", "title": "Old A", "style_name": "A", "used": False}]
        saved = {}

        def fake_gen(titles, cfg, style=None, discarded_titles=None):
            return [{"title": f"{style['name']} fresh", "reason": "r", "interestingness": 0.7}]

        with mock.patch.object(backend, "generate_video_suggestions", side_effect=fake_gen), \
             mock.patch.object(backend.gapp, "_channel_video_titles", return_value=[]), \
             mock.patch.object(backend.yt, "load_suggestions", return_value=list(existing)), \
             mock.patch.object(backend.yt, "save_suggestions",
                               side_effect=lambda v: saved.setdefault("v", list(v))):
            out = backend.youtube_suggestions(guidance="", refresh=True, style_name=backend.ALL_STYLES)
        self.assertEqual({s["title"] for s in out["suggestions"]}, {"Old A", "A fresh", "B fresh"})
        self.assertEqual({s["title"] for s in saved["v"]}, {"Old A", "A fresh", "B fresh"})


class SizePresetsTests(TempConfigCase):
    """Per-style Small/Medium/Large size presets: each bucket pairs a scene
    count with a resolution, drives the AI-ideas one-tap size, and mirrors the
    default style onto the flat default_size_presets key like every other field."""

    def test_fresh_style_gets_default_size_presets(self):
        cfg = app.load_config()
        sp = cfg["styles"][0]["size_presets"]
        self.assertEqual(set(sp), {"small", "medium", "large"})
        self.assertEqual(sp["small"]["scenes"], 6)
        self.assertEqual(sp["medium"]["scenes"], 12)
        self.assertEqual(sp["large"]["scenes"], 20)
        self.assertTrue(all(b["resolution"] in app._RESOLUTIONS for b in sp.values()))
        # mirror: the default style's presets land on the flat key
        self.assertEqual(cfg["default_size_presets"], sp)

    def test_per_style_presets_persist_and_normalize(self):
        self.write_config({
            "styles": [
                _style("A", size_presets={
                    "small":  {"scenes": 3,   "resolution": "Portrait (480×832)"},
                    "medium": {"scenes": 9999, "resolution": "bogus"},  # clamp + bad res
                    "large":  {"scenes": 15,  "resolution": "Landscape FHD (1920×1080)"},
                }),
                _style("B"),
            ],
            "default_style": "A",
        })
        app.save_config(app.load_config())
        on_disk = app.load_config()
        a = next(s for s in on_disk["styles"] if s["name"] == "A")
        # legacy scenes-only preset: minutes derived from the ~9 s scene length
        self.assertEqual(a["size_presets"]["small"],
                         {"minutes": 0.45, "scenes": 3, "resolution": "Portrait (480×832)"})
        # 9999 clamps to MAX_SCENES; an unknown resolution falls back to the default
        self.assertEqual(a["size_presets"]["medium"]["scenes"], app.MAX_SCENES)
        self.assertEqual(a["size_presets"]["medium"]["resolution"],
                         app._DEFAULT_SIZE_PRESETS["medium"]["resolution"])
        self.assertEqual(a["size_presets"]["large"]["scenes"], 15)
        # default style A mirrors onto the flat key
        self.assertEqual(on_disk["default_size_presets"], a["size_presets"])

    def test_missing_buckets_filled_from_defaults(self):
        self.write_config({
            "styles": [_style("A", size_presets={
                "small": {"scenes": 4, "resolution": "Portrait (480×832)"}})],
            "default_style": "A",
        })
        cfg = app.load_config()
        sp = cfg["styles"][0]["size_presets"]
        self.assertEqual(sp["small"], {"minutes": 0.6, "scenes": 4,
                                       "resolution": "Portrait (480×832)"})
        self.assertEqual(sp["medium"], app._DEFAULT_SIZE_PRESETS["medium"])
        self.assertEqual(sp["large"], app._DEFAULT_SIZE_PRESETS["large"])

    def test_each_style_keeps_its_own_presets_object(self):
        # A legacy-migrated config gives every style the built-in default; editing
        # one must not bleed into another (no shared dict aliasing).
        cfg = app.load_config()
        cfg["styles"] = [_style("A"), _style("B")]
        for s in cfg["styles"]:
            s.pop("size_presets", None)
        app.save_config(cfg)
        cfg = app.load_config()
        a, b = cfg["styles"]
        a["size_presets"]["small"]["scenes"] = 1
        self.assertEqual(b["size_presets"]["small"]["scenes"],
                         app._DEFAULT_SIZE_PRESETS["small"]["scenes"])


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


class CharacterTests(TempConfigCase):
    """Recurring-character registry: normalization, the LLM sheet, and the
    deterministic injection into a scene's image prompt."""

    def test_norm_characters_assigns_ids_and_drops_blank(self):
        rows = app._norm_characters([
            {"name": "Robot XYZ", "description": "matte-black chassis"},
            {"name": "  ", "description": "no name — dropped"},
            "not a dict",
            {"description": "also no name"},
        ])
        self.assertEqual([c["name"] for c in rows], ["Robot XYZ"])
        self.assertTrue(rows[0]["id"].startswith("char_"))
        self.assertEqual(rows[0]["aliases"], [])
        self.assertTrue(rows[0]["enabled"])

    def test_norm_characters_coerces_and_clamps(self):
        rows = app._norm_characters([{
            "id": "keep-me", "name": "Bob", "aliases": ["Bobby", "  ", 42],
            "description": " a man ", "ref_image": "bob.png",
            "ref_strength": 5.0, "enabled": 0,
        }])
        c = rows[0]
        self.assertEqual(c["id"], "keep-me")
        self.assertEqual(c["aliases"], ["Bobby", "42"])
        self.assertEqual(c["description"], "a man")
        self.assertEqual(c["ref_strength"], 1.0)  # clamped 5.0 -> 1.0
        self.assertFalse(c["enabled"])

    def test_norm_characters_dedupes_ids(self):
        rows = app._norm_characters([
            {"id": "dup", "name": "A", "description": "a"},
            {"id": "dup", "name": "B", "description": "b"},
        ])
        self.assertEqual(len({c["id"] for c in rows}), 2)

    def test_norm_characters_strips_path_like_ids(self):
        # Ids become filenames (<id>.png), so path-y input must not survive
        # normalization — a traversal id would otherwise write outside the
        # characters dir.
        rows = app._norm_characters([
            {"id": "../../../tmp/evil", "name": "Sneaky", "description": "x"},
            {"id": "fine_id-1", "name": "Ok", "description": "y"},
        ])
        self.assertEqual(rows[0]["id"], "tmpevil")
        self.assertEqual(rows[1]["id"], "fine_id-1")

    def test_norm_characters_regenerates_fully_pathy_id(self):
        rows = app._norm_characters([{"id": "../..", "name": "A", "description": "a"}])
        self.assertTrue(rows[0]["id"].startswith("char_"))

    def test_characters_default_empty_on_fresh_install(self):
        cfg = app.load_config()
        self.assertEqual(cfg["characters"], [])
        self.assertNotIn("character_ids", cfg["styles"][0])
        self.assertTrue(cfg["characters_scoped_v3"])

    def test_legacy_per_style_characters_migrate_to_scoped_library(self):
        # A pre-global config kept characters on each style; loading hoists
        # them into the shared library (v2) and, since each was used by exactly
        # one style, scopes them to that style (v3).
        self.write_config({
            "styles": [_style("Hero", characters=[
                {"name": "Robot XYZ", "aliases": ["XYZ"],
                 "description": "matte-black humanoid chassis, cyan optic"},
            ])],
            "default_style": "Hero",
        })
        cfg = app.load_config()
        chars = cfg["characters"]
        self.assertEqual(len(chars), 1)
        self.assertEqual(chars[0]["name"], "Robot XYZ")
        self.assertEqual(chars[0]["aliases"], ["XYZ"])
        self.assertTrue(chars[0]["id"])
        self.assertEqual(chars[0]["style"], "Hero")
        # the per-style "characters" field and the old opt-in list are gone
        self.assertNotIn("characters", cfg["styles"][0])
        self.assertNotIn("character_ids", cfg["styles"][0])
        self.assertNotIn("default_character_ids", cfg)

    def test_scope_migration_from_opt_in_lists(self):
        # v3: a character listed by exactly one style becomes that style's; one
        # listed by several styles stays global; one listed by nobody but named
        # exactly like a style (narrator personas) goes to that style; the old
        # opt-in fields are consumed everywhere.
        self.write_config({
            "characters": [
                {"id": "char_a", "name": "Ana", "description": "a woman"},
                {"id": "char_b", "name": "Ben", "description": "a man"},
                {"id": "char_h", "name": "Hero", "description": "the persona"},
                {"id": "char_g", "name": "Ghost", "description": "unlisted"},
            ],
            "styles": [_style("Hero", character_ids=["char_a", "char_b", "char_gone"]),
                       _style("Villain", character_ids=["char_b"],
                              auto_accept_characters=True)],
            "default_style": "Hero",
            "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        scopes = {c["id"]: c["style"] for c in cfg["characters"]}
        self.assertEqual(scopes, {"char_a": "Hero",     # single owner
                                  "char_b": "",         # shared → global
                                  "char_h": "Hero",     # name matches a style
                                  "char_g": ""})        # unlisted → global
        for s in cfg["styles"]:
            self.assertNotIn("character_ids", s)
            self.assertNotIn("auto_accept_characters", s)
        self.assertNotIn("default_character_ids", cfg)
        self.assertNotIn("default_auto_accept_characters", cfg)
        self.assertTrue(cfg["characters_scoped_v3"])

    def test_scope_migration_runs_once_and_keeps_hand_scopes(self):
        self.write_config({
            "characters": [{"id": "char_a", "name": "Ana", "description": "a",
                            "style": "Villain"}],
            "styles": [_style("Hero", character_ids=["char_a"]), _style("Villain")],
            "default_style": "Hero",
            "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        # An entry already carrying a scope is never re-scoped by the migration.
        self.assertEqual(cfg["characters"][0]["style"], "Villain")

    def test_no_style_imposes_no_style_cast_but_keeps_the_global_pool(self):
        # Global characters belong to the library, not to a style: asking for
        # one by name in experiment mode must reuse it, not invent a duplicate
        # with a fresh look and a randomly cast voice.
        self.write_config({
            "characters": [{"id": "char_bob", "name": "Bob", "description": "a man"},
                           {"id": "char_vil", "name": "Villain", "description": "bad",
                            "style": "Hero"}],
            "styles": [_style("Hero")],
            "default_style": "Hero",
            "characters_migrated_v2": True, "characters_scoped_v3": True,
        })
        cfg = app.load_config()
        visible = [c["name"] for c in app._style_characters(cfg, app.NO_STYLE)]
        self.assertEqual(visible, ["Bob"])

    def test_no_style_reuses_a_global_character_instead_of_duplicating_it(self):
        self.write_config({
            "characters": [{"id": "char_k", "name": "Kinho", "description": "a man",
                            "enabled": True}],
            "styles": [_style("Hero")],
            "default_style": "Hero",
            "characters_migrated_v2": True, "characters_scoped_v3": True,
        })
        cfg = app.load_config()
        kept = app._filter_identified_against_style(
            [{"name": "Kinho", "description": "reinvented"},
             {"name": "Newcomer", "description": "genuinely new"}],
            cfg, app.NO_STYLE)
        self.assertEqual([c["name"] for c in kept], ["Newcomer"])

    def test_character_sheet_lists_enabled_described_only(self):
        sheet = app._character_sheet([
            {"name": "Robot XYZ", "description": "matte-black chassis", "enabled": True},
            {"name": "Ghost", "description": "", "enabled": True},        # no description
            {"name": "Bob", "description": "a man", "enabled": False},     # disabled
        ])
        self.assertIn("Robot XYZ: matte-black chassis", sheet)
        self.assertNotIn("Ghost", sheet)
        self.assertNotIn("Bob", sheet)

    def test_character_sheet_empty_without_usable_characters(self):
        self.assertEqual(app._character_sheet([]), "")
        self.assertEqual(app._character_sheet([{"name": "X", "description": ""}]), "")

    def _cfg_with_hero(self, **char):
        base = {"name": "Robot XYZ", "description": "matte-black humanoid chassis"}
        base.update(char)
        self.write_config({
            "styles": [_style("Hero", characters=[base])],
            "default_style": "Hero",
        })
        return app.load_config()

    def test_inject_appends_description_on_name_match(self):
        cfg = self._cfg_with_hero()
        scene = {"image_prompt": "Robot XYZ stands on a ridge.", "narration": ""}
        out = app._inject_characters(scene["image_prompt"], scene, cfg, "Hero")
        self.assertIn("matte-black humanoid chassis", out)
        self.assertTrue(out.startswith("Robot XYZ stands on a ridge."))

    def test_inject_matches_alias_in_image_prompt(self):
        cfg = self._cfg_with_hero(aliases=["the machine"])
        scene = {"image_prompt": "The machine crosses a wide desert vista.",
                 "narration": ""}
        out = app._inject_characters(scene["image_prompt"], scene, cfg, "Hero")
        self.assertIn("matte-black humanoid chassis", out)

    def test_inject_ignores_narration_only_mention(self):
        cfg = self._cfg_with_hero(aliases=["the machine"])
        # narration names people who are talked about, not shown — no injection
        scene = {"image_prompt": "A wide desert vista.",
                 "narration": "Then the machine appeared."}
        out = app._inject_characters(scene["image_prompt"], scene, cfg, "Hero")
        self.assertEqual(out, "A wide desert vista.")

    def test_inject_noop_when_character_absent(self):
        cfg = self._cfg_with_hero()
        scene = {"image_prompt": "An empty canyon at dawn.", "narration": "Silence."}
        out = app._inject_characters(scene["image_prompt"], scene, cfg, "Hero")
        self.assertEqual(out, "An empty canyon at dawn.")

    def test_inject_does_not_double_stack_description(self):
        cfg = self._cfg_with_hero()
        base = "Robot XYZ, matte-black humanoid chassis, walks forward."
        scene = {"image_prompt": base, "narration": ""}
        out = app._inject_characters(base, scene, cfg, "Hero")
        self.assertEqual(out, base)  # description already present → unchanged

    def test_inject_skips_disabled_character(self):
        cfg = self._cfg_with_hero(enabled=False)
        scene = {"image_prompt": "Robot XYZ stands still.", "narration": ""}
        out = app._inject_characters(scene["image_prompt"], scene, cfg, "Hero")
        self.assertEqual(out, "Robot XYZ stands still.")

    def test_inject_respects_character_scope(self):
        # A character scoped to "Hero" injects there but not in the unrelated
        # "Villain" style, even for the same scene text.
        self.write_config({
            "characters": [{"id": "char_xyz", "name": "Robot XYZ",
                            "description": "matte-black humanoid chassis",
                            "style": "Hero"}],
            "styles": [_style("Hero"), _style("Villain")],
            "default_style": "Hero",
            "characters_migrated_v2": True, "characters_scoped_v3": True,
        })
        cfg = app.load_config()
        scene = {"image_prompt": "Robot XYZ stands on a ridge.", "narration": ""}
        self.assertIn("matte-black humanoid chassis",
                      app._inject_characters(scene["image_prompt"], scene, cfg, "Hero"))
        self.assertEqual(app._inject_characters(scene["image_prompt"], scene, cfg, "Villain"),
                         "Robot XYZ stands on a ridge.")

    def test_style_characters_resolves_scopes(self):
        # Global pool → everyone; own scope → that style; another style's
        # scope → hidden; a scope naming a deleted style → dormant everywhere.
        self.write_config({
            "characters": [{"id": "char_g", "name": "Glo", "description": "g"},
                           {"id": "char_h", "name": "Own", "description": "h", "style": "Hero"},
                           {"id": "char_v", "name": "Foe", "description": "v", "style": "Villain"},
                           {"id": "char_d", "name": "Lost", "description": "d", "style": "Deleted"}],
            "styles": [_style("Hero"), _style("Villain")],
            "default_style": "Hero",
            "characters_migrated_v2": True, "characters_scoped_v3": True,
        })
        cfg = app.load_config()
        self.assertEqual([c["id"] for c in app._style_characters(cfg, "Hero")],
                         ["char_g", "char_h"])
        self.assertEqual([c["id"] for c in app._style_characters(cfg, "Villain")],
                         ["char_g", "char_v"])
        # A dangling scope is kept in the library (heals if the style returns)
        # but resolves nowhere meanwhile.
        self.assertEqual(cfg["characters"][3]["style"], "Deleted")

    def test_portrait_uses_owning_styles_look_not_default(self):
        # The reported bug: portraits always rendered with the DEFAULT style's
        # visual look, so a character made for another style came out with the
        # default style's characteristics. The portrait must anchor to the
        # character's owning style; only global-pool characters use the default.
        self.write_config({
            "characters": [
                {"id": "char_v", "name": "Foe", "description": "a masked figure",
                 "style": "Villain"},
                {"id": "char_g", "name": "Glo", "description": "a mascot"},
            ],
            "styles": [_style("Hero", visual_style="sunny watercolor world"),
                       _style("Villain", visual_style="grim neon noir world")],
            "default_style": "Hero",
            "characters_migrated_v2": True, "characters_scoped_v3": True,
        })
        prompts = []

        def fake_generate(engine, prompt, out, **kw):
            prompts.append(prompt)
            Path(out).write_bytes(b"png")

        with mock.patch.object(app, "generate_with_engine", side_effect=fake_generate), \
             mock.patch.object(app, "_preview_worker_urls", return_value=["http://w1"]):
            app.generate_character_portrait("char_v")
            app.generate_character_portrait("char_g")
        self.assertIn("grim neon noir world", prompts[0])      # owning style
        self.assertNotIn("sunny watercolor world", prompts[0])
        self.assertIn("sunny watercolor world", prompts[1])    # global → default

    def test_style_characters_unknown_style_falls_back_to_default(self):
        self.write_config({
            "characters": [{"id": "char_h", "name": "Own", "description": "h", "style": "Hero"}],
            "styles": [_style("Hero"), _style("Villain")],
            "default_style": "Hero",
            "characters_migrated_v2": True, "characters_scoped_v3": True,
        })
        cfg = app.load_config()
        # Unknown/blank style names resolve like style_settings: the default.
        self.assertEqual([c["id"] for c in app._style_characters(cfg, "Nope")], ["char_h"])
        self.assertEqual([c["id"] for c in app._style_characters(cfg, "")], ["char_h"])


class CharacterReferenceImageTests(TempConfigCase):
    """Phase 2 — reference-image conditioning: workflow builder, scene matching,
    and the image store/clear helpers."""

    _REPL = {
        "FLUX_MODEL": "m", "CLIP_T5": "c", "FLUX_VAE": "v", "WEIGHT_DTYPE": "default",
        "POSITIVE_PROMPT": "a scene", "WIDTH": 1024, "HEIGHT": 1024,
        "STEPS": 4, "GUIDANCE": 4.0, "SEED": 1,
    }

    def test_ref_workflow_single_reference(self):
        from pipeline import comfyui, engines
        wf = comfyui._build_flux2_ref_workflow(engines.get("flux2-klein"), self._REPL, ["bob.png"])
        self.assertEqual(wf["20"]["inputs"]["image"], "bob.png")
        # BasicGuider (8) is driven by the single ReferenceLatent (22)
        self.assertEqual(wf["8"]["inputs"]["conditioning"], ["22", 0])
        self.assertNotIn("23", wf)

    def test_ref_workflow_chains_multiple_references(self):
        from pipeline import comfyui, engines
        wf = comfyui._build_flux2_ref_workflow(engines.get("flux2-klein"), self._REPL, ["bob.png", "xyz.png"])
        self.assertEqual(wf["20"]["inputs"]["image"], "bob.png")
        self.assertEqual(wf["23"]["inputs"]["image"], "xyz.png")           # second ref loaded
        self.assertEqual(wf["24"]["inputs"]["pixels"], ["23", 0])           # encoded
        self.assertEqual(wf["25"]["inputs"]["conditioning"], ["22", 0])     # chained after first
        self.assertEqual(wf["25"]["inputs"]["latent"], ["24", 0])
        self.assertEqual(wf["8"]["inputs"]["conditioning"], ["25", 0])      # guider uses the last

    def test_ref_workflow_is_valid_and_complete(self):
        from pipeline import comfyui, engines
        wf = comfyui._build_flux2_ref_workflow(engines.get("flux2-klein"), self._REPL, ["bob.png"])
        # every placeholder filled — no unresolved {{...}} survives in the JSON
        self.assertNotIn("{{", json.dumps(wf))
        self.assertEqual(wf["1"]["class_type"], "UNETLoader")

    def test_klein_engine_declares_ref_workflow(self):
        from pipeline import engines
        self.assertEqual(engines.get("flux2-klein")["t2i_ref_workflow"], "flux2_t2i_ref.json")

    def test_character_image_path_is_basename_only(self):
        p = app._character_image_path("../../etc/passwd")
        self.assertEqual(p.name, "passwd")
        self.assertEqual(p.parent, app._characters_dir())
        self.assertIsNone(app._character_image_path(""))

    def _hero_with_chars(self, chars):
        # Explicit ids mirror a saved config: ids are minted on save and then
        # stable, which is what the image ops (gated on a saved form) rely on.
        # Characters live in the global library; "Hero" opts into all of them.
        chars = [{"id": f"char_test_{i}", **c} for i, c in enumerate(chars)]
        self.write_config({
            "characters": chars,
            "styles": [_style("Hero", character_ids=[c["id"] for c in chars])],
            "default_style": "Hero",
            "characters_migrated_v2": True,
        })
        return app.load_config()

    def _write_ref(self, char_id):
        from PIL import Image
        d = app._characters_dir()
        d.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (0, 128, 255)).save(d / f"{char_id}.png", "PNG")

    def test_scene_reference_images_matches_by_name(self):
        cfg = self._hero_with_chars([
            {"name": "Bob", "description": "a man", "ref_image": "x"},
        ])
        cid = cfg["characters"][0]["id"]
        # ensure the stored filename is the canonical <id>.png and the file exists
        cfg = app.set_character_image(cid, self._png_bytes())
        scene = {"image_prompt": "Bob waves.", "narration": ""}
        paths = app._scene_reference_images(scene["image_prompt"], scene, cfg, "Hero")
        self.assertEqual([p.name for p in paths], [f"{cid}.png"])

    def test_scene_reference_images_ignores_unmatched_and_missing_file(self):
        cfg = self._hero_with_chars([
            {"name": "Bob", "description": "a man", "ref_image": "ghost.png"},  # file never written
        ])
        scene = {"image_prompt": "Bob waves.", "narration": ""}
        self.assertEqual(app._scene_reference_images("Bob waves.", scene, cfg, "Hero"), [])
        # unmatched name → empty even when the file exists
        cfg = self._hero_with_chars([{"name": "Bob", "description": "a man", "ref_image": "x"}])
        cid = cfg["characters"][0]["id"]
        cfg = app.set_character_image(cid, self._png_bytes())
        scene = {"image_prompt": "An empty room.", "narration": "Nobody."}
        self.assertEqual(app._scene_reference_images("An empty room.", scene, cfg, "Hero"), [])

    def test_scene_reference_images_caps_at_two(self):
        cfg = self._hero_with_chars([
            {"name": "Ana", "description": "a", "ref_image": "x"},
            {"name": "Ben", "description": "b", "ref_image": "x"},
            {"name": "Cid", "description": "c", "ref_image": "x"},
        ])
        for ch in cfg["characters"]:
            cfg = app.set_character_image(ch["id"], self._png_bytes())
        scene = {"image_prompt": "Ana, Ben and Cid meet.", "narration": ""}
        paths = app._scene_reference_images(scene["image_prompt"], scene, cfg, "Hero")
        self.assertEqual(len(paths), app._MAX_SCENE_REFERENCES)

    _FLUX2 = {"t2i_ref_workflow": "flux2_t2i_ref.json"}

    def test_prompt_and_refs_appends_named_note_on_flux2(self):
        cfg = self._hero_with_chars([{"name": "Bob", "description": "a man", "ref_image": "x"}])
        cfg = app.set_character_image(cfg["characters"][0]["id"], self._png_bytes())
        scene = {"image_prompt": "Bob waves.", "narration": ""}
        prompt, refs = app._characters_prompt_and_refs(
            scene["image_prompt"], scene, cfg, "Hero", engine=self._FLUX2)
        self.assertIn("a man", prompt)  # description still injected
        self.assertIn("Bob appears EXACTLY as the character", prompt)
        self.assertEqual(len(refs), 1)

    def test_prompt_and_refs_notes_even_without_description(self):
        # A character with only an uploaded look (no description) still gets the
        # name→reference binding — the exact case a bare upload used to lose.
        cfg = self._hero_with_chars([{"name": "Bob", "description": "", "ref_image": "x"}])
        cfg = app.set_character_image(cfg["characters"][0]["id"], self._png_bytes())
        scene = {"image_prompt": "Bob waves.", "narration": ""}
        prompt, refs = app._characters_prompt_and_refs(
            scene["image_prompt"], scene, cfg, "Hero", engine=self._FLUX2)
        self.assertIn("Bob appears EXACTLY as the character", prompt)
        self.assertEqual(len(refs), 1)

    def test_prompt_and_refs_no_note_without_reference_support(self):
        cfg = self._hero_with_chars([{"name": "Bob", "description": "a man", "ref_image": "x"}])
        cfg = app.set_character_image(cfg["characters"][0]["id"], self._png_bytes())
        scene = {"image_prompt": "Bob waves.", "narration": ""}
        for engine in (None, {}):  # flux1 has no t2i_ref_workflow
            prompt, refs = app._characters_prompt_and_refs(
                scene["image_prompt"], scene, cfg, "Hero", engine=engine)
            self.assertNotIn("appears EXACTLY", prompt)
            self.assertEqual(len(refs), 1)

    def test_prompt_and_refs_no_note_when_nothing_matches(self):
        cfg = self._hero_with_chars([{"name": "Bob", "description": "a man"}])  # no image
        scene = {"image_prompt": "Bob waves.", "narration": ""}
        prompt, refs = app._characters_prompt_and_refs(
            scene["image_prompt"], scene, cfg, "Hero", engine=self._FLUX2)
        self.assertNotIn("appears EXACTLY", prompt)
        self.assertEqual(refs, [])

    def test_set_and_clear_character_image(self):
        cfg = self._hero_with_chars([{"name": "Bob", "description": "a man"}])
        cid = cfg["characters"][0]["id"]
        cfg = app.set_character_image(cid, self._png_bytes())
        char = cfg["characters"][0]
        self.assertEqual(char["ref_image"], f"{cid}.png")
        p = app._character_image_path(char["ref_image"])
        self.assertTrue(p.exists())
        cfg = app.clear_character_image(cid)
        self.assertEqual(cfg["characters"][0]["ref_image"], "")
        self.assertFalse(p.exists())

    def test_image_ops_reject_unknown_character(self):
        self._hero_with_chars([{"name": "Bob", "description": "a man"}])
        with self.assertRaises(ValueError):
            app.set_character_image("not-an-id", self._png_bytes())
        with self.assertRaises(ValueError):
            app.clear_character_image("nope")

    def test_ref_strength_defaults_to_one(self):
        cfg = self._hero_with_chars([{"name": "Bob", "description": "a man"}])
        self.assertEqual(cfg["characters"][0]["ref_strength"], 1.0)

    @staticmethod
    def _png_bytes() -> bytes:
        from PIL import Image
        import io as _io
        buf = _io.BytesIO()
        Image.new("RGB", (8, 8), (255, 0, 0)).save(buf, "PNG")
        return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
