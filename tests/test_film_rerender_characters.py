"""The Film (edit video) editor's re-render must pick up character looks.

_run_image_rerender / _run_video_rerender used to build their prompt from the raw
image_prompt only — no character description injected, no reference images — so
regenerating a scene's image or video from the edit video screen lost the
recurring character's appearance. Both now go through _film_scene_image_prompt,
which mirrors the Script editor's preview path (_generate_active_scene_preview):
inject each featured character's canonical look and gather their reference images,
folding in the script's own per-script characters via the work dir."""
import io
import unittest

import app
import webapp.backend.main as backend
from test_styles import TempConfigCase, _style


def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


class FilmSceneImagePromptTests(TempConfigCase):
    def _work_dir(self):
        wd = self.output_dir / "vid-20260101-000000"
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    def _script_character_with_image(self, wd):
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
        return cid

    def test_injects_description_and_reference_image(self):
        self.write_config({
            "characters": [], "styles": [_style("Hero")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        wd = self._work_dir()
        cid = self._script_character_with_image(wd)

        jc = {"style": "Hero visual", "style_name": "Hero"}
        row = {"image_prompt": "Caesar crosses the Rubicon.", "narration": ""}
        prompt, refs = backend._film_scene_image_prompt(jc, row, cfg, wd)

        # Character look folded into the prompt so the subject stays consistent.
        self.assertIn("red toga", prompt)
        # Reference image gathered for FLUX.2 conditioning.
        self.assertEqual([p.name for p in refs], [f"{cid}.png"])
        # Style prefix still applied, and applied once, at the front.
        self.assertTrue(prompt.startswith("Hero visual. "))

    def test_no_matching_character_leaves_prompt_styled_only(self):
        self.write_config({
            "characters": [], "styles": [_style("Hero")],
            "default_style": "Hero", "characters_migrated_v2": True,
        })
        cfg = app.load_config()
        wd = self._work_dir()
        self._script_character_with_image(wd)

        jc = {"style": "Hero visual", "style_name": "Hero"}
        row = {"image_prompt": "An empty landscape at dawn.", "narration": ""}
        prompt, refs = backend._film_scene_image_prompt(jc, row, cfg, wd)

        self.assertEqual(prompt, "Hero visual. An empty landscape at dawn.")
        self.assertEqual(refs, [])


if __name__ == "__main__":
    unittest.main()
