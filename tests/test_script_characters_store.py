"""Per-script character storage, render-pool merge, and promote-to-catalogue.

Per-script characters live in the work dir (characters.json + characters/), stay
OUT of the global catalogue, and ride into that job's renders via _job_characters.
Promotion copies one into the catalogue on demand (non-destructive)."""
import io
import unittest

import app
from test_styles import TempConfigCase, _style


def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


class ScriptCharacterStoreTests(TempConfigCase):
    def _work_dir(self):
        wd = self.output_dir / "my-video-20260101-000000"
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    def test_write_read_roundtrip_assigns_ids(self):
        wd = self._work_dir()
        saved = app._write_script_characters(wd, [
            {"name": "Julius Caesar", "aliases": ["Caesar"], "description": "a lean general"},
            {"name": "", "description": "dropped"},  # nameless dropped by _norm_characters
        ])
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["id"].startswith("char_"))
        again = app._read_script_characters(wd)
        self.assertEqual(again[0]["name"], "Julius Caesar")
        self.assertEqual(again[0]["id"], saved[0]["id"])

    def test_read_missing_is_empty(self):
        self.assertEqual(app._read_script_characters(self._work_dir()), [])

    def test_add_blank_character_persists_editable_placeholder(self):
        # The editor's "Add character" posts a blank row; it must survive
        # normalization (which drops nameless entries) as an editable placeholder
        # with a stable id — otherwise the button appears to do nothing.
        wd = self._work_dir()
        saved = app.add_script_character(wd, "", [], "")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["name"], "New character")
        cid = saved[0]["id"]
        # The returned id round-trips through an edit (rename to a real character).
        edited = app.update_script_character(wd, cid, name="George Washington",
                                             description="tall, powdered wig, blue coat")
        self.assertEqual(edited[0]["name"], "George Washington")
        self.assertEqual(app._read_script_characters(wd)[0]["id"], cid)

    def test_script_character_image_path_basename_only(self):
        wd = self._work_dir()
        p = app._script_character_image_path(wd, "../../etc/passwd")
        self.assertEqual(p.name, "passwd")
        self.assertEqual(p.parent, app._script_characters_dir(wd))
        self.assertIsNone(app._script_character_image_path(wd, ""))


class FilterIdentifiedAgainstStyleTests(TempConfigCase):
    """LLM-identified cast members that already live in the style catalogue
    must not become per-script characters (which would shadow the global look)."""

    def test_exact_name_match_dropped(self):
        self.write_config({
            "characters": [{"id": "char_c", "name": "Julius Caesar", "aliases": ["Caesar"],
                            "description": "canonical red toga", "style": "Hero"}],
            "styles": [_style("Hero")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        identified = [
            {"name": "Julius Caesar", "aliases": [], "description": "LLM paraphrased look"},
            {"name": "Brutus", "aliases": [], "description": "a senator"},
        ]
        kept = app._filter_identified_against_style(identified, cfg, "Hero")
        self.assertEqual([c["name"] for c in kept], ["Brutus"])

    def test_alias_and_whole_word_match_dropped(self):
        self.write_config({
            "characters": [{"id": "char_c", "name": "Julius Caesar", "aliases": ["Caesar"],
                            "description": "canonical", "style": "Hero"}],
            "styles": [_style("Hero")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        # LLM only returned the short name — still the same person.
        kept = app._filter_identified_against_style(
            [{"name": "Caesar", "description": "other"}], cfg, "Hero")
        self.assertEqual(kept, [])

    def test_catalogue_scoped_to_other_style_is_not_filtered(self):
        # The catalogue entry belongs to an unrelated style — Hero can't see
        # it, so the LLM's copy still becomes a per-script character.
        self.write_config({
            "characters": [{"id": "char_c", "name": "Julius Caesar", "description": "canonical",
                            "style": "Other"}],
            "styles": [_style("Hero"), _style("Other")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        identified = [{"name": "Julius Caesar", "description": "llm"}]
        kept = app._filter_identified_against_style(identified, cfg, "Hero")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["name"], "Julius Caesar")

    def test_disabled_catalogue_character_is_not_filtered(self):
        self.write_config({
            "characters": [{"id": "char_c", "name": "Caesar", "description": "x", "enabled": False,
                            "style": "Hero"}],
            "styles": [_style("Hero")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        kept = app._filter_identified_against_style(
            [{"name": "Caesar", "description": "llm"}], cfg, "Hero")
        self.assertEqual(len(kept), 1)


class JobCharactersMergeTests(TempConfigCase):
    def _work_dir(self):
        wd = self.output_dir / "vid-20260101-000000"
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    def test_merges_catalogue_and_script(self):
        self.write_config({
            "characters": [{"id": "char_cat", "name": "Ana", "description": "a", "style": "Hero"}],
            "styles": [_style("Hero")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        wd = self._work_dir()
        app._write_script_characters(wd, [{"name": "Ben", "description": "b"}])
        names = {c["name"] for c in app._job_characters(cfg, "Hero", wd)}
        self.assertEqual(names, {"Ana", "Ben"})

    def test_script_character_shadows_same_name_catalogue(self):
        self.write_config({
            "characters": [{"id": "char_cat", "name": "Ana", "description": "OLD", "style": "Hero"}],
            "styles": [_style("Hero")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        wd = self._work_dir()
        app._write_script_characters(wd, [{"name": "Ana", "description": "NEW"}])
        anas = [c for c in app._job_characters(cfg, "Hero", wd) if c["name"] == "Ana"]
        self.assertEqual(len(anas), 1)
        self.assertEqual(anas[0]["description"], "NEW")

    def test_inject_and_reference_images_use_script_characters(self):
        self.write_config({
            "characters": [], "styles": [_style("Hero")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        wd = self._work_dir()
        saved = app._write_script_characters(wd, [
            {"name": "Julius Caesar", "aliases": ["Caesar"],
             "description": "a lean Roman general in a red toga"},
        ])
        cid = saved[0]["id"]
        d = app._script_characters_dir(wd)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.png").write_bytes(_png_bytes())
        saved[0]["ref_image"] = f"{cid}.png"
        app._write_script_characters(wd, saved)

        scene = {"image_prompt": "Caesar crosses the Rubicon.", "narration": ""}
        out = app._inject_characters(scene["image_prompt"], scene, cfg, "Hero", wd)
        self.assertIn("red toga", out)
        paths = app._scene_reference_images(scene["image_prompt"], scene, cfg, "Hero", wd)
        self.assertEqual([p.name for p in paths], [f"{cid}.png"])
        # Per-script characters are NOT global: without a work_dir, nothing applies.
        self.assertEqual(app._inject_characters(scene["image_prompt"], scene, cfg, "Hero"),
                         scene["image_prompt"])
        self.assertEqual(app._scene_reference_images(scene["image_prompt"], scene, cfg, "Hero"), [])


class PromoteScriptCharacterTests(TempConfigCase):
    def _work_dir(self):
        wd = self.output_dir / "vid-20260101-000000"
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    def test_promote_copies_into_catalogue_scoped_to_style(self):
        self.write_config({
            "characters": [], "styles": [_style("Hero")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        wd = self._work_dir()
        saved = app._write_script_characters(wd, [{"name": "Caesar", "description": "a lean general"}])
        cid = saved[0]["id"]
        d = app._script_characters_dir(wd)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.png").write_bytes(_png_bytes())
        saved[0]["ref_image"] = f"{cid}.png"
        app._write_script_characters(wd, saved)

        cfg = app.promote_script_character(wd, cid, "Hero")
        lib = cfg["characters"]
        self.assertEqual(len(lib), 1)
        self.assertEqual(lib[0]["name"], "Caesar")
        new_id = lib[0]["id"]
        self.assertNotEqual(new_id, cid)  # fresh catalogue id, independent of the script copy
        self.assertTrue((app._characters_dir() / f"{new_id}.png").exists())
        self.assertEqual(lib[0]["ref_image"], f"{new_id}.png")
        # Scoped to the job's style, so Hero (and future children) reuse it.
        self.assertEqual(lib[0]["style"], "Hero")
        # Non-destructive: the per-script copy stays put.
        self.assertEqual(len(app._read_script_characters(wd)), 1)

    def test_promote_unknown_character_raises(self):
        self.write_config({
            "characters": [], "styles": [_style("Hero")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        with self.assertRaises(ValueError):
            app.promote_script_character(self._work_dir(), "char_missing", "Hero")


if __name__ == "__main__":
    unittest.main()
