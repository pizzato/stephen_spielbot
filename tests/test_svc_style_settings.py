"""Per-style singing voice conversion selection and sparse inheritance."""
import app
import webapp.backend.main as backend
from test_styles import TempConfigCase, _style


class SvcStyleSettingsTests(TempConfigCase):
    def test_fresh_and_older_configs_default_to_soulx_without_revoicing(self):
        for old in ({}, {"styles": [_style("Old")], "default_style": "Old"}):
            with self.subTest(old=bool(old)):
                self.write_config(old)
                cfg = app.load_config()
                self.assertEqual(cfg["default_svc_engine"], "soulx-svc")
                self.assertEqual(app.style_settings(cfg)["svc_engine"], "soulx-svc")
                self.assertFalse(app.automation_settings(cfg)["auto_song_revoice"])

    def test_legacy_flat_seed_choice_migrates_and_survives_save(self):
        self.write_config({"default_svc_engine": "seed-vc"})
        cfg = app.load_config()
        self.assertEqual(cfg["styles"][0]["svc_engine"], "seed-vc")
        app.save_config(cfg)
        saved = app.load_config()
        self.assertEqual(saved["default_svc_engine"], "seed-vc")
        self.assertEqual(app.style_settings(saved)["svc_engine"], "seed-vc")

    def test_existing_default_adopts_flat_choice_without_leaking_to_other_roots(self):
        self.write_config({
            "styles": [_style("Default"), _style("Other")],
            "default_style": "Default", "default_svc_engine": "seed-vc",
        })
        cfg = app.load_config()
        self.assertEqual(app.style_settings(cfg, "Default")["svc_engine"], "seed-vc")
        self.assertEqual(app.style_settings(cfg, "Other")["svc_engine"], "soulx-svc")

    def test_default_child_inherits_and_mirrors_parent_selection(self):
        self.write_config({
            "styles": [_style("Parent", svc_engine="seed-vc"),
                       {"name": "Child", "parent": "Parent"}],
            "default_style": "Child",
        })
        cfg = app.load_config()
        self.assertEqual(app.style_settings(cfg, "Child")["svc_engine"], "seed-vc")
        self.assertEqual(cfg["default_svc_engine"], "seed-vc")
        self.assertNotIn("svc_engine", cfg["styles"][1])
        cfg["styles"][0]["svc_engine"] = "soulx-svc"
        app.save_config(cfg)
        saved = app.load_config()
        self.assertEqual(app.style_settings(saved, "Child")["svc_engine"], "soulx-svc")
        self.assertEqual(saved["default_svc_engine"], "soulx-svc")
        self.assertNotIn("svc_engine", saved["styles"][1])

    def test_explicit_seed_override_persists_until_reset_to_inherited(self):
        self.write_config({
            "styles": [_style("Parent", svc_engine="soulx-svc"),
                       {"name": "Child", "parent": "Parent", "svc_engine": "seed-vc"}],
            "default_style": "Child",
        })
        cfg = app.load_config()
        app.save_config(cfg)
        saved = app.load_config()
        self.assertEqual(saved["styles"][1]["svc_engine"], "seed-vc")
        self.assertEqual(saved["default_svc_engine"], "seed-vc")
        saved["styles"][1].pop("svc_engine")
        app.save_config(saved)
        reset = app.load_config()
        self.assertNotIn("svc_engine", reset["styles"][1])
        self.assertEqual(app.style_settings(reset, "Child")["svc_engine"], "soulx-svc")

    def test_settings_api_saves_converter_and_preserves_sparse_inheritance(self):
        self.write_config({
            "styles": [_style("Parent"), {"name": "Child", "parent": "Parent"}],
            "default_style": "Child",
        })
        posted = app.public_config(app.load_config())
        posted["styles"][0]["svc_engine"] = "seed-vc"
        response = backend.post_config(backend.ConfigUpdate(config=posted))
        saved = response["config"]
        self.assertEqual(saved["default_svc_engine"], "seed-vc")
        self.assertEqual(saved["styles"][1], {"name": "Child", "parent": "Parent"})
        self.assertEqual(self.read_config()["styles"][0]["svc_engine"], "seed-vc")
        self.assertFalse(saved["youtube_auto_song_revoice"])

    def test_invalid_explicit_engine_normalizes_without_densifying_child(self):
        for value in (None, "", "unknown", "soulx-svs"):
            with self.subTest(value=value):
                self.write_config({
                    "styles": [_style("Parent", svc_engine="seed-vc"),
                               {"name": "Child", "parent": "Parent", "svc_engine": value}],
                    "default_style": "Child",
                })
                cfg = app.load_config()
                self.assertEqual(cfg["styles"][1], {
                    "name": "Child", "parent": "Parent", "svc_engine": "soulx-svc",
                })
                self.assertEqual(cfg["default_svc_engine"], "soulx-svc")

    def test_engine_change_preserves_explicit_automation_preferences(self):
        self.write_config({
            "styles": [_style("On", svc_engine="seed-vc",
                              automation={"auto_song_revoice": True}),
                       _style("Off", svc_engine="seed-vc",
                              automation={"auto_song_revoice": False})],
            "default_style": "Off",
        })
        cfg = app.load_config()
        for style in cfg["styles"]:
            style["svc_engine"] = "soulx-svc"
        app.save_config(cfg)
        saved = app.load_config()
        self.assertTrue(app.automation_settings(saved, "On")["auto_song_revoice"])
        self.assertFalse(app.automation_settings(saved, "Off")["auto_song_revoice"])
        self.assertFalse(saved["youtube_auto_song_revoice"])
