"""Scene image matching must not cast the artist named in its style prefix."""
import json
from unittest import mock

from PIL import Image

import app
from pipeline import scene_image
from test_styles import TempConfigCase


class SceneImageCharacterTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        self.wd = self.output_dir / "music-video"
        self.wd.mkdir()
        self.cfg = {
            "default_style": "Mick",
            "styles": [
                {"name": "Music", "visual_style": "Cinematic photography"},
                {"name": "Mick", "parent": "Music",
                 "visual_style": "{parent}. Mick Dargo: grounded pub-rock imagery."},
            ],
            "characters": [{
                "id": "mick", "name": "Mick Dargo", "style": "Mick",
                "description": "dark mullet and thick moustache", "ref_image": "mick.png",
            }],
        }
        self.style = app._compose_visual_style("", self.cfg, "Mick")
        self.ref = app._character_image_path("mick.png")
        self.ref.parent.mkdir(parents=True)
        Image.new("RGB", (8, 8), "navy").save(self.ref)
        (self.wd / "job_config.json").write_text(json.dumps({"style": self.style}))
        self.engine = {"family": "qwen-image", "t2i_ref_workflow": "reference.json"}

    def _prompt(self, shot, cast=None):
        text = f"{self.style}. {shot}"
        scene = {"image_prompt": text, "metadata": {"cast": cast or [], "singing": True}}
        return app._characters_prompt_and_refs(
            text, scene, self.cfg, "Mick", self.wd, engine=self.engine)

    def test_artist_free_shot_ignores_artist_in_inherited_style(self):
        prompt, refs = self._prompt("An empty timber workshop. No people.")
        self.assertEqual(refs, [])
        self.assertNotIn("appears EXACTLY", prompt)
        self.assertNotIn("dark mullet and thick moustache", prompt)
        self.assertNotIn(self.style, prompt)
        self.assertNotIn("Mick Dargo", prompt)

    def test_named_subject_gets_reference_even_when_cast_was_empty(self):
        prompt, refs = self._prompt("Mick Dargo beside a timber workbench.")
        self.assertEqual(refs, [self.ref])
        self.assertNotIn("dark mullet and thick moustache", prompt)
        self.assertIn("Mick Dargo appears EXACTLY as the character in <image1>", prompt)

    def test_authored_cast_keeps_reference_when_shot_uses_pronoun(self):
        prompt, refs = self._prompt("He waits beside a timber workbench.", ["Mick Dargo"])
        self.assertEqual(refs, [self.ref])
        self.assertIn("Mick Dargo appears EXACTLY", prompt)

    def test_saved_style_is_removed_from_matching_after_profile_changes(self):
        self.cfg["styles"][1]["visual_style"] = "Restrained photographic lighting"
        prompt, refs = self._prompt("An empty workshop. No people.")
        self.assertEqual(refs, [])
        self.assertNotIn("appears EXACTLY", prompt)

    def test_repeated_style_does_not_supply_a_character(self):
        prompt, refs = self._prompt(f"{self.style}. An empty workshop.")
        self.assertEqual(refs, [])
        self.assertNotIn("appears EXACTLY", prompt)

    def test_cover_without_scene_keeps_existing_character_binding(self):
        prompt, refs = app._characters_prompt_and_refs(
            "Mick Dargo beside a workbench.", {}, self.cfg, "Mick", self.wd,
            engine=self.engine, allow_appearance_override=True)
        self.assertEqual(refs, [self.ref])
        self.assertIn("for identity and unchanged features", prompt)

    def test_missing_portrait_refuses_descriptive_fallback(self):
        for missing in ("", "missing.png"):
            with self.subTest(missing=missing):
                self.cfg["characters"][0]["ref_image"] = missing
                with self.assertRaisesRegex(RuntimeError, "Missing character reference.*Mick Dargo"):
                    self._prompt("Mick Dargo beside a workbench.")

    def test_unknown_cast_refuses_a_lookalike(self):
        with self.assertRaisesRegex(RuntimeError, "Missing character reference.*Stranger"):
            self._prompt("He stands at the window.", ["Stranger"])

    def test_music_performer_requires_reference_capable_engine(self):
        self.engine = {"family": "flux1"}
        with self.assertRaisesRegex(RuntimeError, "reference conditioning"):
            self._prompt("Mick Dargo beside a workbench.")

    def test_film_rerender_removes_old_style_and_uses_actual_portrait(self):
        from webapp.backend.main import _film_scene_image_prompt
        for shot, expected in (("An empty street. No people.", []),
                               ("Mick Dargo beside a workbench.", [self.ref])):
            text = f"{self.style}. {shot}"
            row = {"image_prompt": text, "metadata": {"singing": True}}
            prompt, refs = _film_scene_image_prompt(
                {"style": self.style, "style_name": "Mick"}, row,
                self.cfg, self.wd, self.engine)
            self.assertNotIn(self.style, prompt)
            self.assertNotIn("dark mullet", prompt)
            self.assertEqual(refs, expected)

    def test_known_subject_without_portrait_is_recorded_and_attached(self):
        self.cfg["characters"][0]["ref_image"] = ""
        scene = {"id": 3, "image_prompt": f"{self.style}. Mick Dargo at a workbench.",
                 "metadata": {"cast": []}}
        image = self.wd / "scene_03_first_frame.png"
        Image.new("RGB", (8, 8), "grey").save(image)
        with mock.patch.object(app, "sync_scene_frame_cast") as sync:
            app.record_scene_frame_cast(image, scene, self.cfg, self.wd, "Mick")
        self.assertEqual(scene_image.read_cast(image, self.cfg["characters"]), ["Mick Dargo"])
        sync.assert_called_once_with(self.wd, 3, self.cfg, "Mick", image=image)

    def test_preview_uses_style_once_and_records_only_shot_characters(self):
        for shot, instruction, expected_names in (
            ("An empty workshop. No people.", "", []),
            ("Mick Dargo beside a workbench.", "", ["Mick Dargo"]),
            ("A timber workbench.", "Add Mick Dargo beside the workbench.", ["Mick Dargo"]),
        ):
            with self.subTest(shot=shot):
                text = f"{self.style}. {shot}"
                scene = {"image_prompt": text, "metadata": {"cast": [], "singing": True}}
                store = mock.Mock()
                store.get_scene.return_value = scene
                store.get_job.return_value = {
                    "config_json": json.dumps({"style_name": "Mick"})}
                pool = mock.Mock()
                pool.acquire.return_value = "http://worker"

                def paint(engine, prompt, out, **kwargs):
                    Image.new("RGB", (8, 8), "grey").save(out)

                with mock.patch.object(app, "_job_work_dir", return_value=self.wd), \
                        mock.patch.object(app, "load_config", return_value=self.cfg), \
                        mock.patch.object(app.DurableStore, "default", return_value=store), \
                        mock.patch.object(app.engines, "resolve", return_value=self.engine), \
                        mock.patch.object(app, "generate_with_engine", side_effect=paint) as gen, \
                        mock.patch.object(app, "sync_scene_frame_cast") as sync:
                    out = app._generate_active_scene_preview(
                        "job", 1, "", self.style, "Workshop", text,
                        force=True, worker_pool=pool, instruction=instruction)
                prompt = gen.call_args.args[1]
                self.assertEqual(prompt.count(self.style), 0)
                if instruction:
                    self.assertIn(instruction, prompt)
                self.assertEqual(
                    scene_image.read_cast(out, self.cfg["characters"]), expected_names)
                self.assertEqual(
                    gen.call_args.kwargs["reference_images"], [self.ref] if expected_names else [])
                sync.assert_called_once_with(self.wd, 1, self.cfg, "Mick", image=out)
