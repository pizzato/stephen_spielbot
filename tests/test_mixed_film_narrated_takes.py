"""A mixed film's narrated scenes are shot as silent takes on the acted engine.

render_narrated_take is the one clip step both the renderer (_run_scene) and
the film editor's re-shoot swap in for a mixed film: it sizes a silent take to
the narration, opens it on the scene's first frame, casts whoever the prompts
name, and hands back (clip, ambient) exactly like generate_scene_video so the
narration mux after it is unchanged."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline.llm import Scene


class NarratedTakeTests(unittest.TestCase):
    def _scene(self):
        return Scene(id=3, title="The garden",
                     image_prompt="Amelia leans over a red ladybird on a leaf",
                     video_prompt="Slow dolly toward the ladybird as Amelia leans in",
                     narration="I leaned in close and counted the little black spots.")

    def _run(self, work_dir: Path, narration_dur: float, cfg: dict | None = None, **kw):
        import resume_generation as rg
        captured = {}

        def fake_clip(scene, meta, wd, cfg_, clip, **kwargs):
            captured["meta"] = meta
            captured["kwargs"] = kwargs
            Path(clip).write_bytes(b"take")
            return clip

        def fake_extract(video_path, output_path, duration=None):
            Path(output_path).write_bytes(b"amb")
            return output_path

        chars = [{"name": "Amelia", "description": "a curious girl", "enabled": True}]
        with mock.patch.object(rg, "_render_performance_clip", side_effect=fake_clip), \
             mock.patch.object(rg, "extract_audio", side_effect=fake_extract), \
             mock.patch.object(rg, "_get_duration", return_value=narration_dur + 1.0), \
             mock.patch("app._characters_for_scene", return_value=chars) as chars_for, \
             mock.patch("pipeline.scene_video.generate_with_engine") as paint:
            out = rg.render_narrated_take(
                self._scene(), work_dir, cfg or {}, narration_dur,
                comfy_url="http://w:8188", vid_width=704, vid_height=1280,
                style_name="Amelia", **kw)
        captured["chars_for"] = chars_for
        captured["paint"] = paint
        return out, captured

    def test_take_is_a_silent_scene_sized_to_the_narration_with_the_cast_named_in_the_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "scene_03_preview.png").write_bytes(b"png")
            (clip, ambient), got = self._run(wd, 8.0, scene_first_frame=wd / "scene_03_preview.png")
            self.assertTrue(ambient.exists())
        meta = got["meta"]
        self.assertEqual(meta["mode"], "silent")
        self.assertEqual(meta["lines"], [])
        self.assertEqual(meta["cast"], ["Amelia"])
        self.assertAlmostEqual(meta["seconds"], 9.0)       # narration + the clip buffer
        self.assertIn("Slow dolly", meta["setting"])       # the video prompt directs the take
        # The cast is matched against everything the scene says, not one field.
        text = got["chars_for"].call_args[0][0]
        for piece in ("ladybird on a leaf", "Slow dolly", "counted the little black spots"):
            self.assertIn(piece, text)
        self.assertEqual(clip.name, "scene_03_clip_01.mp4")
        self.assertEqual(ambient.name, "scene_03_ambient.wav")
        got["paint"].assert_not_called()                   # the first frame was already there
        self.assertIsNone(got["kwargs"]["extra_pictures"])
        self.assertEqual(got["kwargs"]["drop_kinds"], ())

    def test_a_missing_first_frame_is_painted_before_the_take(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (clip, _), got = self._run(wd, 8.0, scene_first_frame=None)
        got["paint"].assert_called_once()
        self.assertEqual(Path(got["paint"].call_args[0][2]).name, "scene_03_first_frame.png")

    def test_a_long_narration_is_held_to_the_acted_scene_cap(self):
        # The same cap a silent beat is held to — the mux freezes the closing
        # frame for whatever narration runs past it.
        from pipeline import performance as perf
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "scene_03_preview.png").write_bytes(b"png")
            (_, _), got = self._run(wd, 40.0, scene_first_frame=wd / "scene_03_preview.png")
        self.assertEqual(got["meta"]["seconds"], perf.acted_limits(False)[0])

    def test_a_continued_shot_opens_on_the_handoff_frame_instead_of_its_still(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            handoff = wd / "scene_03_handoff.png"
            handoff.write_bytes(b"png")
            (_, _), got = self._run(wd, 6.0, scene_first_frame=handoff, handoff_frame=handoff)
        got["paint"].assert_not_called()
        self.assertEqual(got["kwargs"]["drop_kinds"], ("frame",))
        self.assertEqual(got["kwargs"]["extra_pictures"][0]["path"], str(handoff))
        self.assertEqual(got["kwargs"]["extra_pictures"][0]["kind"], "frame")

    def test_ambient_extraction_failure_does_not_lose_the_clip(self):
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "scene_03_preview.png").write_bytes(b"png")
            with mock.patch.object(rg, "_render_performance_clip",
                                   side_effect=lambda s, m, w, c, clip, **k: Path(clip).write_bytes(b"t")), \
                 mock.patch.object(rg, "extract_audio", side_effect=RuntimeError("no audio")), \
                 mock.patch.object(rg, "_get_duration", return_value=7.0), \
                 mock.patch("app._characters_for_scene", return_value=[]):
                clip, ambient = rg.render_narrated_take(
                    self._scene(), wd, {}, 6.0, comfy_url="http://w:8188",
                    vid_width=704, vid_height=1280,
                    scene_first_frame=wd / "scene_03_preview.png")
            self.assertTrue(clip.exists())
        self.assertIsNone(ambient)


if __name__ == "__main__":
    unittest.main()
