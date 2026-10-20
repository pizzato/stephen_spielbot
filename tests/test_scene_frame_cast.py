"""The cast of a generated opening image must reach the scene and its take."""
import json
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

import app
from pipeline import scene_image
from pipeline.orchestrator import DurableStore, job_id_from_work_dir
from test_styles import TempConfigCase, _style


class SceneFrameCastTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        self.write_config({"styles": [_style("Music")], "default_style": "Music",
                           "characters": [], "characters_migrated_v2": True})
        self.cfg = app.load_config()
        self.wd = self.output_dir / "song"
        self.wd.mkdir()
        self.job_id = job_id_from_work_dir(self.wd)
        self.characters = [
            {"id": "mick", "name": "Mick", "description": "a rocker",
             "ref_image": "mick.png", "enabled": True},
            {"id": "ana", "name": "Ana", "description": "a singer",
             "ref_image": "ana.png", "enabled": True},
        ]
        (self.wd / "characters.json").write_text(json.dumps(self.characters))
        (self.wd / "characters").mkdir()
        for char in self.characters:
            Image.new("RGB", (8, 8)).save(self.wd / "characters" / char["ref_image"])
        self.rows = [
            {"id": sid, "title": f"Scene {sid}", "image_prompt": "A street.",
             "video_prompt": "A slow pan.", "narration": "",
             "metadata": {"mode": "silent", "cast": [], "performs": False}}
            for sid in (1, 2)
        ]
        app._persist_script_snapshot(self.wd, self.rows)
        store = DurableStore.default()
        try:
            store.create_or_update_job(self.job_id, self.wd, "Song")
            store.upsert_scenes(self.job_id, self.rows)
        finally:
            store.close()

    def frame(self, sid=1, cast=None):
        path = self.wd / f"scene_{sid:02d}_preview.png"
        Image.new("RGB", (8, 8)).save(path)
        scene_image.record_cast(path, self.characters[:1] if cast is None else cast)
        return path

    def stored(self, sid=1):
        store = DurableStore.default()
        try:
            return store.get_scene(self.job_id, sid)
        finally:
            store.close()

    def test_frame_character_gets_portrait_and_persists_without_becoming_singer(self):
        self.frame()
        meta = dict(self.rows[0]["metadata"])
        refs = app.resolve_performance_references(
            meta, self.cfg, self.wd, "Music", scene_id=1)
        self.assertEqual(meta["cast"], ["Mick"])
        self.assertEqual([(p["kind"], p["name"]) for p in refs["pictures"]],
                         [("character", "Mick"), ("frame", "First frame")])
        self.assertFalse(self.stored()["metadata"]["performs"])
        saved = json.loads((self.wd / "script.json").read_text())
        self.assertEqual(saved[0]["metadata"]["cast"], ["Mick"])
        self.assertEqual(saved[1], self.rows[1])

    def test_parallel_previews_preserve_both_casts(self):
        self.frame(1)
        self.frame(2, self.characters[1:])
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda sid: app.sync_scene_frame_cast(
                self.wd, sid, self.cfg, "Music"), [1, 2]))
        saved = json.loads((self.wd / "script.json").read_text())
        self.assertEqual([r["metadata"]["cast"] for r in saved], [["Mick"], ["Ana"]])

    def test_existing_alias_or_case_keeps_one_identity_and_original_label(self):
        self.characters[0]["aliases"] = ["Mick Dargo"]
        (self.wd / "characters.json").write_text(json.dumps(self.characters))
        self.frame()
        for label in ("Mick Dargo", "MICK"):
            with self.subTest(label=label):
                row = {**self.rows[0], "metadata": {
                    **self.rows[0]["metadata"], "cast": ["Ana", label]}}
                store = DurableStore.default()
                try:
                    store.upsert_scenes(self.job_id, [row])
                finally:
                    store.close()
                app._persist_script_snapshot(self.wd, [row, self.rows[1]])
                meta = dict(row["metadata"])
                refs = app.resolve_performance_references(
                    meta, self.cfg, self.wd, "Music", scene_id=1)
                self.assertEqual(meta["cast"], ["Ana", label])
                portraits = [p for p in refs["pictures"] if p["kind"] == "character"]
                self.assertEqual([p["name"] for p in portraits], ["Ana", label])
                self.assertEqual(len({p["path"] for p in portraits}), 2)
                self.assertEqual(self.stored()["metadata"]["cast"], ["Ana", label])
                saved = json.loads((self.wd / "script.json").read_text())
                self.assertEqual(saved[0]["metadata"]["cast"], ["Ana", label])

    def test_snapshot_is_repaired_even_when_store_is_already_correct(self):
        self.frame()
        app.sync_scene_frame_cast(self.wd, 1, self.cfg, "Music")
        app._persist_script_snapshot(self.wd, self.rows)
        app.sync_scene_frame_cast(self.wd, 1, self.cfg, "Music")
        saved = json.loads((self.wd / "script.json").read_text())
        self.assertEqual(saved[0]["metadata"]["cast"], ["Mick"])

    def test_script_only_scene_keeps_other_rows(self):
        self.frame(3)
        rows = [*self.rows, {**self.rows[0], "id": 3}]
        app._persist_script_snapshot(self.wd, rows)
        app.sync_scene_frame_cast(self.wd, 3, self.cfg, "Music")
        saved = json.loads((self.wd / "script.json").read_text())
        self.assertEqual(len(saved), 3)
        self.assertEqual(saved[2]["metadata"]["cast"], ["Mick"])

    def test_disabled_frame_does_not_add_a_character(self):
        self.frame()
        meta = {"mode": "silent", "cast": [], "no_first_frame": True}
        refs = app.resolve_performance_references(
            meta, self.cfg, self.wd, "Music", scene_id=1)
        self.assertEqual(meta["cast"], [])
        self.assertEqual(refs["pictures"], [])

    def test_solo_shot_and_unused_continuation_frame_do_not_expand_cast(self):
        self.frame(cast=self.characters)
        for options in ({"solo": True}, {"include_frame_cast": False}):
            meta = {"cast": ["Ana"], "solo": options.get("solo", False)}
            app.resolve_performance_references(
                meta, self.cfg, self.wd, "Music", scene_id=1,
                include_frame_cast=options.get("include_frame_cast", True))
            self.assertEqual(meta["cast"], ["Ana"])

    def test_unknown_upload_does_not_guess_cast(self):
        Image.new("RGB", (8, 8)).save(self.wd / "scene_01_preview.png")
        meta = app.sync_scene_frame_cast(self.wd, 1, self.cfg, "Music")
        self.assertEqual(meta["cast"], [])
