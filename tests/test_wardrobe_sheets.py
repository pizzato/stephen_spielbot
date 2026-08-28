"""Wardrobe sheets: worn turnaround painting, copying a character's sheet in as
a wardrobe reference, and the per-scene "portrait clothes only" off-switch.

The generators are mocked — under test is which references and prompts each path
produces, and whether the scene resolver honours the off-switch."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from PIL import Image

import app as gapp
from test_styles import _style


class WardrobeSheetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-ward-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.config_file = tmp / "config" / "config.yaml"
        self.config_file.parent.mkdir(parents=True)
        self.work_dir = tmp / "film"
        self.work_dir.mkdir()
        for attr, value in [("CONFIG_FILE", self.config_file),
                            ("OUTPUT_DIR", tmp / "videos")]:
            patcher = mock.patch.object(gapp, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (tmp / "videos").mkdir()
        self.config_file.write_text(yaml.safe_dump({
            "styles": [_style("Hero")], "default_style": "Hero",
            "characters": [{"id": "char_a", "name": "Ada", "aliases": ["Miss A"],
                            "description": "an engineer", "ref_image": "char_a.png",
                            "style": ""}],
            "assets": [{"id": "ast_w", "name": "Gala outfit", "kind": "wardrobe",
                        "character": "Ada", "description": "emerald velvet jacket",
                        "style": "", "enabled": True}],
            "characters_migrated_v2": True, "characters_scoped_v3": True,
        }))
        d = gapp._characters_dir(); d.mkdir(parents=True)
        Image.new("RGB", (64, 64), "white").save(d / "char_a.png")
        workers = mock.patch.object(gapp, "_preview_worker_urls", lambda: ["http://w:8188"])
        workers.start(); self.addCleanup(workers.stop)

    def _fake_engine(self, engine, prompt, out, **kw):
        Image.new("RGB", (64, 32), "grey").save(out)
        self.prompt, self.refs = prompt, kw.get("reference_images")
        return out

    # ── worn painting ───────────────────────────────────────────────────────
    def test_worn_asset_paints_the_character_wearing_the_outfit(self):
        with mock.patch.object(gapp, "generate_with_engine", self._fake_engine):
            cfg = gapp.generate_asset_image("ast_w", "Hero", "gold lapels", worn=True)
        # Portrait-conditioned, and the outfit text reaches the sheet prompt.
        self.assertEqual([p.name for p in self.refs], ["char_a.png"])
        self.assertIn("emerald velvet jacket", self.prompt)
        self.assertIn("gold lapels", self.prompt)
        self.assertIn("turnaround", self.prompt.lower())
        asset = gapp._norm_assets(cfg.get("assets"))[0]
        self.assertEqual(asset["ref_image"], "ast_w.png")

    def test_worn_needs_a_character(self):
        cfg = gapp.load_config()
        cfg["assets"][0]["character"] = ""
        gapp.save_config(cfg)
        with self.assertRaisesRegex(ValueError, "character"):
            gapp.generate_asset_image("ast_w", "Hero", worn=True)

    def test_worn_visual_matches_character_by_alias(self):
        gapp.write_script_visuals(self.work_dir, [{
            "id": "vis_1", "name": "Gala outfit", "kind": "wardrobe",
            "character": "Miss A", "description": "emerald jacket", "scenes": []}])
        with mock.patch.object(gapp, "generate_with_engine", self._fake_engine):
            gapp.generate_script_visual_image(self.work_dir, "vis_1", "Hero", worn=True)
        self.assertEqual([p.name for p in self.refs], ["char_a.png"])

    # ── copying the character's sheet in ────────────────────────────────────
    def test_asset_takes_the_character_sheet(self):
        d = gapp._character_sheet_dir("char_a"); d.mkdir(parents=True)
        Image.new("RGB", (40, 10), "blue").save(d / "sheet.png")
        cfg = gapp.asset_use_character_sheet("ast_w")
        out = gapp._assets_dir() / "ast_w.png"
        self.assertTrue(out.exists())
        self.assertEqual(gapp._norm_assets(cfg.get("assets"))[0]["ref_image"], "ast_w.png")

    def test_missing_sheet_reports_where_to_build_one(self):
        with self.assertRaisesRegex(ValueError, "no turnaround sheet"):
            gapp.asset_use_character_sheet("ast_w")

    def test_film_visual_takes_the_character_sheet(self):
        d = gapp._character_sheet_dir("char_a"); d.mkdir(parents=True)
        Image.new("RGB", (40, 10), "blue").save(d / "sheet.png")
        gapp.write_script_visuals(self.work_dir, [{
            "id": "vis_1", "name": "Outfit", "kind": "wardrobe",
            "character": "Ada", "scenes": []}])
        vis = gapp.script_visual_use_character_sheet(self.work_dir, "vis_1", "Hero")
        self.assertEqual(vis[0]["ref_image"], "vis_1.png")
        self.assertTrue((gapp._script_visuals_dir(self.work_dir) / "vis_1.png").exists())

    # ── the per-scene off-switch ────────────────────────────────────────────
    def test_no_wardrobe_drops_only_the_wardrobe_reference(self):
        img = gapp._assets_dir(); img.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), "red").save(img / "ast_w.png")
        cfg = gapp.load_config()
        cfg["assets"][0]["ref_image"] = "ast_w.png"
        gapp.save_config(cfg)
        cfg = gapp.load_config()
        meta = {"cast": ["Ada"], "lines": []}
        refs = gapp.resolve_performance_references(meta, cfg, self.work_dir, "Hero", scene_id=1)
        self.assertEqual([p["kind"] for p in refs["pictures"]], ["character", "wardrobe"])
        refs = gapp.resolve_performance_references({**meta, "no_wardrobe": True},
                                                   cfg, self.work_dir, "Hero", scene_id=1)
        self.assertEqual([p["kind"] for p in refs["pictures"]], ["character"],
                         "portrait keeps full authority when the scene opts out")


if __name__ == "__main__":
    unittest.main()
