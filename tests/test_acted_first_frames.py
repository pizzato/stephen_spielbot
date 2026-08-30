"""Acted scenes opening on a painted first frame: the h3_first_frames toggle.

The contract: an acted (dialogue) scene normally renders from its cast's
portraits alone — no frame is painted. With the style's h3_first_frames on,
EVERY acted scene gets an opening frame first (from its image prompt, or
composed from its setting), and the frame rides the take as its
opening-composition reference. Silent scenes keep their existing behaviour
either way, and a hand-picked location reference still stops the paint.
"""
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import performance as perf  # noqa: E402
from pipeline.llm import Scene  # noqa: E402


def _dialogue(image_prompt="", setting="", cast=("Ana",)):
    md = {"mode": "dialogue", "cast": list(cast)}
    if setting:
        md["setting"] = setting
    return Scene(id=3, title="talk", image_prompt=image_prompt, video_prompt="v",
                 narration="", mode="dialogue",
                 lines=[{"speaker": "Ana", "text": "You came."}], metadata_extra=md)


ON = {"h3_first_frames": True}


class FlagTests(unittest.TestCase):
    def test_flat_key_wins_then_styles_fall_back(self):
        import resume_generation as rg
        self.assertTrue(rg.first_frames_flag(ON))
        self.assertFalse(rg.first_frames_flag({"h3_first_frames": False}))
        # Older job dirs stamp nothing — the style's current setting decides.
        styled = {"styles": [{"name": "S", "h3_first_frames": True}], "style_name": "S"}
        self.assertTrue(rg.first_frames_flag(styled))
        self.assertFalse(rg.first_frames_flag({"styles": [{"name": "S"}]}, "S"))


class OpeningFrameTests(unittest.TestCase):
    def _run(self, scene, cfg, files=()):
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            for name in files:
                (wd / name).write_bytes(b"x")
            made = []

            def _fake_gen(engine, prompt, out, **kw):
                Path(out).write_bytes(b"png")
                made.append((prompt, kw.get("width"), kw.get("height")))
                return out

            with unittest.mock.patch.object(rg, "generate_with_engine", _fake_gen):
                got = rg.ensure_opening_frame(scene, wd, cfg, comfy_url="http://x",
                                              vid_width=512, vid_height=256)
            return got, made

    def test_a_dialogue_scene_is_untouched_by_default(self):
        got, made = self._run(_dialogue(image_prompt="i"), {})
        self.assertIsNone(got)
        self.assertEqual(made, [])

    def test_the_toggle_paints_a_dialogue_frame_from_its_image_prompt(self):
        got, made = self._run(_dialogue(image_prompt="i"), ON)
        self.assertTrue(str(got).endswith("scene_03_first_frame.png"))
        self.assertEqual(made, [("i", 512, 256)])

    def test_without_an_image_prompt_the_frame_is_composed_from_the_setting(self):
        got, made = self._run(_dialogue(setting="a wharf at dusk"), ON)
        self.assertTrue(str(got).endswith("scene_03_first_frame.png"))
        self.assertEqual(made, [(
            "a wharf at dusk. Ana is in the scene. "
            "The very first moment of the scene, nobody speaking yet.", 512, 256)])

    def test_no_prompt_and_no_setting_is_not_an_error(self):
        got, made = self._run(_dialogue(), ON)
        self.assertIsNone(got)
        self.assertEqual(made, [])

    def test_an_existing_preview_is_reused(self):
        got, made = self._run(_dialogue(image_prompt="i"), ON,
                              files=["scene_03_preview.png"])
        self.assertTrue(str(got).endswith("scene_03_preview.png"))
        self.assertEqual(made, [])   # nothing regenerated

    def test_the_cast_is_anchored_to_their_reference_portraits(self):
        """The painted frame binds each named character to their portrait —
        the same anchoring the Create screen and the film editor use. Without
        it this painter invents a face and the take opens on a stranger."""
        import app
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            saved = app._write_script_characters(
                wd, [{"name": "Ana", "description": "a woman with green eyes"}])
            cid = saved[0]["id"]
            d = app._script_characters_dir(wd)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{cid}.png").write_bytes(b"png")
            saved[0]["ref_image"] = f"{cid}.png"
            app._write_script_characters(wd, saved)
            seen = {}

            def _fake_gen(engine, prompt, out, **kw):
                Path(out).write_bytes(b"png")
                seen.update(prompt=prompt, refs=kw.get("reference_images"))
                return out

            with unittest.mock.patch.object(rg, "generate_with_engine", _fake_gen):
                rg.ensure_opening_frame(_dialogue(image_prompt="Ana waits."), wd, ON,
                                        comfy_url="http://x",
                                        vid_width=512, vid_height=256)
        self.assertEqual([Path(p).name for p in seen["refs"]], [f"{cid}.png"])
        self.assertIn("green eyes", seen["prompt"])

    def test_a_location_reference_still_stops_the_paint(self):
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "visuals").mkdir()
            (wd / "visuals" / "loc.png").write_bytes(b"png")
            (wd / "visuals.json").write_text(json.dumps([
                {"id": "vis_1", "name": "Bar", "kind": "location",
                 "description": "a bar", "scenes": [3], "ref_image": "loc.png",
                 "enabled": True}]))
            made = []

            def _fake_gen(engine, prompt, out, **kw):
                made.append(prompt)

            with unittest.mock.patch.object(rg, "generate_with_engine", _fake_gen):
                got = rg.ensure_opening_frame(_dialogue(image_prompt="i"), wd, ON,
                                              comfy_url="http://x",
                                              vid_width=512, vid_height=256)
        self.assertIsNone(got)
        self.assertEqual(made, [])


class PromptTests(unittest.TestCase):
    """One helper composes the fallback frame prompt for every painter
    (Create-screen preview, render-time frame, film-editor image re-render)."""

    def test_setting_and_cast_compose_the_prompt(self):
        self.assertEqual(
            perf.opening_frame_prompt(
                {"mode": "dialogue", "setting": "a bar", "cast": ["Ana", "Bo"]}),
            "a bar. Ana and Bo are in the scene, both fully visible. "
            "The very first moment of the scene, nobody speaking yet.")

    def test_a_castless_scene_gets_just_the_setting(self):
        self.assertEqual(
            perf.opening_frame_prompt({"mode": "silent", "setting": "a bar"}),
            "a bar. The very first moment of the scene, nobody speaking yet.")

    def test_narrated_scenes_and_missing_settings_stay_empty(self):
        self.assertEqual(perf.opening_frame_prompt(
            {"mode": "narration", "setting": "a bar"}), "")
        self.assertEqual(perf.opening_frame_prompt({"mode": "dialogue"}), "")
        self.assertEqual(perf.opening_frame_prompt({}), "")


class UploadEndpointTests(unittest.TestCase):
    """Bring-your-own first frame: the film editor's upload/paste endpoint
    saves the image as the scene's canonical frame files, at the film's render
    size, and keeps it in the image history like a painted one."""

    def _call(self, data, resolution=""):
        import base64
        import webapp.backend.main as m
        mock = unittest.mock
        tmp = tempfile.TemporaryDirectory(prefix="spielbot-frame-upload-")
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "videos"
        out.mkdir()
        wd = out / "film-20260821-120000"
        wd.mkdir()
        if resolution:
            (wd / "job_config.json").write_text(json.dumps({"resolution": resolution}))
        store = mock.Mock()
        body = m.FilmPreviewUploadBody(
            work_dir=str(wd), filename="mine.png",
            data="data:image/png;base64," + base64.b64encode(data).decode())
        with mock.patch.object(m.gapp, "OUTPUT_DIR", out), \
             mock.patch.object(m.DurableStore, "default", classmethod(lambda cls: store)):
            payload = m.upload_film_preview(2, body)
        return payload, wd, store

    @staticmethod
    def _png(w, h):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "red").save(buf, "PNG")
        return buf.getvalue()

    def test_upload_writes_both_frame_files_and_records_history(self):
        from PIL import Image
        payload, wd, store = self._call(self._png(30, 20))
        self.assertTrue(payload["ok"])
        for name in ("scene_02_first_frame.png", "scene_02_preview.png"):
            self.assertTrue((wd / name).exists())
        # No render resolution on file → the image keeps its own size.
        with Image.open(wd / "scene_02_first_frame.png") as im:
            self.assertEqual(im.size, (30, 20))
        self.assertEqual(len(payload["history"]["versions"]), 1)
        self.assertEqual(payload["history"]["selected"],
                         payload["history"]["versions"][0]["id"])
        store.update_scene_preview.assert_called_once()

    def test_upload_is_fitted_to_the_films_render_size(self):
        import app as gapp
        from PIL import Image
        from pipeline.comfyui import ltx_dimensions
        res = next(iter(gapp._RESOLUTIONS))
        tw, th = ltx_dimensions(*gapp._RESOLUTIONS[res])
        payload, wd, _ = self._call(self._png(3000, 1000), resolution=res)
        with Image.open(wd / "scene_02_first_frame.png") as im:
            self.assertEqual(im.size, (tw, th))

    def test_garbage_is_a_400_not_a_stacktrace(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._call(b"not an image at all")
        self.assertEqual(ctx.exception.status_code, 400)


class SettingsTests(unittest.TestCase):
    def test_toggle_is_a_per_style_field_defaulting_off(self):
        import app as gapp
        self.assertIn("h3_first_frames", gapp.STYLE_FIELD_TO_FLAT)
        self.assertFalse(gapp.DEFAULT_CFG["default_h3_first_frames"])
        cfg = {"styles": [{"name": "S", "h3_first_frames": "yes"}], "default_style": "S"}
        self.assertTrue(gapp.style_settings(gapp._ensure_styles(cfg), "S")["h3_first_frames"])


if __name__ == "__main__":
    unittest.main()
