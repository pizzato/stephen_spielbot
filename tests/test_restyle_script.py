"""Restyle: swap a script's visual style without touching its content.

The visual style is baked onto the head of every scene's image_prompt, so
changing styles by hand meant editing each prompt — and a re-render still
reused first frames painted in the old look. _restyle_script strips every
known style sentence, lays the new one on, re-points the job, and retires the
style-conditioned images (keeping them in history).
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import webapp.backend.main as backend  # noqa: E402
from pipeline.orchestrator import DurableStore, job_id_from_work_dir  # noqa: E402

OLD = "Cinematic 35mm film, warm golden tones"
NEW = "Flat 2D cel animation, bold ink outlines"
CFG = {
    "default_style": "Film",
    "styles": [
        {"name": "Film", "visual_style": OLD},
        {"name": "Cartoon", "visual_style": NEW},
    ],
}


class StripPrefixTests(unittest.TestCase):
    def test_strips_known_prefixes_repeatedly_and_case_insensitively(self):
        text = f"{OLD}. {OLD.lower()}, a fox in a field"
        self.assertEqual(backend._strip_style_prefix(text, [OLD]), "a fox in a field")

    def test_leaves_unprefixed_text_alone(self):
        self.assertEqual(backend._strip_style_prefix("a fox in a field", [OLD, ""]),
                         "a fox in a field")


class RestyleScriptTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="spielbot-videos-"))
        # A per-test durable store: set as an env patch (not a module-level
        # assignment) so the sandbox DB never leaks into other test modules
        # collected in the same process.
        mock.patch.dict(os.environ, {"SPIELBOT_ORCHESTRATOR_DB": str(self.out / "orchestrator.sqlite3")}).start()
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.out).start()
        mock.patch.object(backend.gapp, "load_config", return_value=CFG).start()
        self.addCleanup(mock.patch.stopall)

        self.wd = self.out / "fox-film-20260101-100000"
        self.wd.mkdir(parents=True)
        scenes = [
            {"id": 1, "title": "One", "image_prompt": f"{OLD}. a fox in a field",
             "video_prompt": "the fox turns", "narration": "n1"},
            {"id": 2, "title": "Two",
             "image_prompt": f"{OLD}. {OLD}. a fox by a river",
             "video_prompt": f"{OLD}. water flows", "narration": "n2"},
        ]
        (self.wd / "script.json").write_text(json.dumps(scenes))
        (self.wd / "scene_01_preview.png").write_bytes(b"img1")
        (self.wd / "scene_02_first_frame.png").write_bytes(b"img2")
        (self.wd / "cover.png").write_bytes(b"cover")
        (self.wd / "job_config.json").write_text(json.dumps(
            {"style": OLD, "style_name": "Film", "style_prefix": OLD}))
        backend._write_create_brief(self.wd, {"video_title": "Fox", "style_name": "Film",
                                              "visual_style": OLD, "minutes": 1})
        chars = self.wd / "characters"
        chars.mkdir()
        (chars / "fox.png").write_bytes(b"fox")
        (self.wd / "characters.json").write_text(json.dumps(
            [{"id": "fox", "name": "Fox", "description": "a red fox", "ref_image": "fox.png"}]))
        self.job_id = job_id_from_work_dir(self.wd)
        store = DurableStore.default()
        try:
            store.create_or_update_job(self.job_id, self.wd, "Fox",
                                       config={"style_name": "Film", "video_title": "Fox"},
                                       metadata={"style": OLD, "scene_count": 2})
            store.upsert_scenes(self.job_id, scenes)
            store.update_scene_preview(self.job_id, 1, self.wd / "scene_01_preview.png")
        finally:
            store.close()

    def test_restyle_rewrites_prompts_job_and_retires_images(self):
        out = backend._restyle_script(self.wd, style_name="Cartoon", style="")

        rows = json.loads((self.wd / "script.json").read_text())
        self.assertEqual(rows[0]["image_prompt"], f"{NEW}. a fox in a field")
        self.assertEqual(rows[0]["video_prompt"], "the fox turns")
        # A stacked double prefix collapses to the single new one.
        self.assertEqual(rows[1]["image_prompt"], f"{NEW}. a fox by a river")
        self.assertEqual(rows[1]["video_prompt"], "water flows")

        self.assertEqual(out["style_name"], "Cartoon")
        self.assertEqual(out["style"], NEW)
        self.assertFalse(any(s.get("has_preview") for s in out["scenes"]))

        # Style-conditioned images are gone from their canonical paths but kept
        # as history versions.
        self.assertFalse((self.wd / "scene_01_preview.png").exists())
        self.assertFalse((self.wd / "scene_02_first_frame.png").exists())
        self.assertFalse((self.wd / "cover.png").exists())
        self.assertTrue((self.wd / "image_history").is_dir())
        self.assertEqual(sorted(out["retired"]),
                         ["cover.png", "look:Fox", "scene_01_preview.png", "scene_02_first_frame.png"])
        chars = json.loads((self.wd / "characters.json").read_text())
        self.assertEqual(chars[0].get("ref_image", ""), "")

        jc = json.loads((self.wd / "job_config.json").read_text())
        self.assertEqual((jc["style"], jc["style_name"], jc["style_prefix"]), (NEW, "Cartoon", NEW))
        brief = backend._read_create_brief(self.wd)
        self.assertEqual((brief["style_name"], brief["visual_style"]), ("Cartoon", NEW))
        store = DurableStore.default()
        try:
            d = backend._row_to_dict(store.get_job(self.job_id))
            self.assertEqual(json.loads(d["config_json"])["style_name"], "Cartoon")
            self.assertEqual(json.loads(d["metadata_json"])["style"], NEW)
            self.assertEqual(store.get_scene(self.job_id, 1)["preview_path"], "")
        finally:
            store.close()

    def test_restyle_is_idempotent_and_keeps_cast_when_asked(self):
        backend._restyle_script(self.wd, style_name="Cartoon", style="", repaint_cast=False)
        chars = json.loads((self.wd / "characters.json").read_text())
        self.assertEqual(chars[0]["ref_image"], "fox.png")
        backend._restyle_script(self.wd, style_name="Cartoon", style="", repaint_cast=False)
        rows = json.loads((self.wd / "script.json").read_text())
        self.assertEqual(rows[0]["image_prompt"], f"{NEW}. a fox in a field")

    def test_no_style_takes_the_written_sentence_alone(self):
        backend._restyle_script(self.wd, style_name="(none)", style="Muted pastel palette")
        rows = json.loads((self.wd / "script.json").read_text())
        self.assertEqual(rows[0]["image_prompt"], "Muted pastel palette. a fox in a field")
        self.assertEqual(backend._read_create_brief(self.wd)["style_name"], "(none)")

    def test_no_style_and_no_text_leaves_prompts_bare(self):
        backend._restyle_script(self.wd, style_name="(none)", style="")
        rows = json.loads((self.wd / "script.json").read_text())
        self.assertEqual(rows[0]["image_prompt"], "a fox in a field")

if __name__ == "__main__":
    unittest.main()
