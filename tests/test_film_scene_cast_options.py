"""Film scene character choices stay within the style and the film's script."""
import json
from unittest import mock

import app
import webapp.backend.main as backend
from pipeline.orchestrator import DurableStore, job_id_from_work_dir
from test_styles import TempConfigCase


class FilmSceneCastOptionsTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        self.cfg = {
            "default_style": "Other",
            "styles": [{"name": "Parent"}, {"name": "Music", "parent": "Parent"},
                       {"name": "Other"}],
            "characters": [
                {"name": "Global", "style": ""},
                {"name": "Parent cast", "style": "Parent"},
                {"name": "Singer", "style": "Music"},
                {"name": "Bandmate", "style": "Music"},
                {"name": "Unrelated", "style": "Other"},
            ],
        }
        patch = mock.patch.object(app, "load_config", return_value=self.cfg)
        patch.start()
        self.addCleanup(patch.stop)
        self.wd = self.output_dir / "music-video"
        self.wd.mkdir()
        self.job_id = job_id_from_work_dir(self.wd)
        app._write_script_characters(self.wd, [
            {"name": "Script extra", "description": "a dancer"},
            {"name": "Singer", "description": "this film's singer"},
        ])

    def _load(self, style_name="Music", *, disk_config=True, metadata=None):
        jc = {"style_name": style_name, "style": "Visual prose, not a style name"}
        if disk_config:
            (self.wd / "job_config.json").write_text(json.dumps(jc))
        store = DurableStore.default()
        try:
            store.create_or_update_job(self.job_id, self.wd, "Music video", config=jc)
            store.upsert_scene(self.job_id, 1, metadata=metadata or {
                "mode": "silent", "singing": True, "cast": ["Singer"],
            })
        finally:
            store.close()
        with mock.patch.object(backend, "_final_upscale_info", return_value={}), \
             mock.patch.object(app, "get_voice_choices", return_value=[]):
            return backend.film_scenes(work_dir=str(self.wd))["cast_options"]

    def test_music_scene_choices_include_only_style_and_script_characters(self):
        self.assertEqual(self._load(), ["Global", "Parent cast", "Bandmate", "Script extra", "Singer"])

    def test_style_inheritance_opt_out_keeps_own_and_script_characters(self):
        self.cfg["styles"][1]["exclude_inherited_characters"] = True
        self.assertEqual(self._load(), ["Bandmate", "Script extra", "Singer"])

    def test_no_style_has_only_global_and_script_characters(self):
        self.assertEqual(self._load(app.NO_STYLE), ["Global", "Script extra", "Singer"])

    def test_store_style_name_is_used_when_disk_config_is_missing(self):
        self.assertNotIn("Unrelated", self._load(disk_config=False))

    def test_existing_scene_cast_and_speakers_remain_selectable(self):
        choices = self._load(metadata={
            "mode": "dialogue", "cast": ["Silent extra", "Singer"],
            "lines": [{"speaker": "Speaker only", "text": "Hello"},
                      {"speaker": "Singer", "text": "Hi"}],
        })
        self.assertEqual(choices, ["Global", "Parent cast", "Bandmate", "Script extra", "Singer",
                                   "Silent extra", "Speaker only"])

    def test_empty_style_and_script_do_not_fall_back_to_whole_catalogue(self):
        self.cfg["styles"].append({"name": "Empty", "exclude_inherited_characters": True})
        app._write_script_characters(self.wd, [])
        self.assertEqual(self._load("Empty", metadata={"mode": "silent"}), [])

    def test_reloading_picks_up_new_script_characters(self):
        self._load()
        app._write_script_characters(self.wd, [{"name": "New dancer", "description": "a dancer"}])
        self.assertIn("New dancer", self._load())
