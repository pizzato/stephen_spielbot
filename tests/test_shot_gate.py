"""The quality gate: verify each shot said its line; retake misses."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import shot_gate


class ScoreTests(unittest.TestCase):
    def test_exact_and_near_matches_pass(self):
        self.assertEqual(shot_gate.score("Hello there Joe.", "Hello there, Joe!"), 1.0)
        # ASR quirks ("Stidney" for Sydney) must not fail a good take.
        s = shot_gate.score("Ready for our big walk through Stidney",
                            "Ready for our big walk through Sydney?")
        self.assertGreaterEqual(s, shot_gate.DEFAULT_THRESHOLD)

    def test_wrong_or_missing_speech_fails(self):
        self.assertLess(shot_gate.score("something else entirely was said here",
                                        "You can burn the trees"), 0.3)
        self.assertEqual(shot_gate.score("", "You can burn the trees"), 0.0)

    def test_invented_tail_speech_costs_score(self):
        clean = shot_gate.score("ready for our big walk", "ready for our big walk")
        babble = shot_gate.score("ready for our big walk keep your little chovia",
                                 "ready for our big walk")
        self.assertLess(babble, clean)

    def test_nothing_scripted_never_fails(self):
        self.assertEqual(shot_gate.score("anything at all", ""), 1.0)


class RetakeTests(unittest.TestCase):
    def test_a_miss_is_retaken_and_the_better_take_kept(self):
        import resume_generation as rg
        from pipeline.llm import Scene
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "face.png").write_bytes(b"png")
            renders = []

            def fake_gen(engine, prompt, ref_images, out, ref_audios=None, **kw):
                renders.append(Path(out).name)
                Path(out).write_bytes(f"take{len(renders)}".encode())
                return Path(out)

            def fake_refs(m, cfg, work_dir, style_name="", scene_id=0):
                return {"pictures": [{"slot": 1, "name": "A", "kind": "character",
                                      "path": str(wd / "face.png")}], "audios": []}

            scene = Scene(id=1, title="S", image_prompt="", video_prompt="", narration="",
                          mode="performance", lines=[{"speaker": "A", "text": "hello"}],
                          metadata_extra={"mode": "performance", "cast": ["A"], "seconds": 6})
            # First take scores a miss, the retake a hit.
            scores = iter([(0.2, "wrong words"), (0.9, "hello")])
            with mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
                 mock.patch("app.resolve_performance_references", side_effect=fake_refs), \
                 mock.patch.object(shot_gate, "available", return_value=True), \
                 mock.patch.object(shot_gate, "verify", side_effect=lambda *a: next(scores)), \
                 mock.patch.object(rg, "ensure_video_resolution"):
                out = rg.render_performance_scene(
                    scene, wd, {"performance_verify": True}, comfy_url="http://w:8188",
                    vid_width=704, vid_height=1280)
            self.assertEqual(len(renders), 2)                    # take + one retake
            self.assertEqual(out.read_bytes(), b"take2")         # the better take won
            self.assertFalse(out.with_suffix(".retake.mp4").exists())

    def test_a_worse_retake_is_discarded(self):
        import resume_generation as rg
        from pipeline.llm import Scene
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "face.png").write_bytes(b"png")

            def fake_gen(engine, prompt, ref_images, out, ref_audios=None, **kw):
                Path(out).write_bytes(b"data")
                return Path(out)

            def fake_refs(m, cfg, work_dir, style_name="", scene_id=0):
                return {"pictures": [{"slot": 1, "name": "A", "kind": "character",
                                      "path": str(wd / "face.png")}], "audios": []}

            scene = Scene(id=1, title="S", image_prompt="", video_prompt="", narration="",
                          mode="performance", lines=[{"speaker": "A", "text": "hello"}],
                          metadata_extra={"mode": "performance", "cast": ["A"], "seconds": 6})
            scores = iter([(0.4, "close-ish"), (0.1, "worse")])
            with mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
                 mock.patch("app.resolve_performance_references", side_effect=fake_refs), \
                 mock.patch.object(shot_gate, "available", return_value=True), \
                 mock.patch.object(shot_gate, "verify", side_effect=lambda *a: next(scores)), \
                 mock.patch.object(rg, "ensure_video_resolution"):
                out = rg.render_performance_scene(
                    scene, wd, {"performance_verify": True}, comfy_url="http://w:8188",
                    vid_width=704, vid_height=1280)
            self.assertTrue(out.exists())
            self.assertFalse(out.with_suffix(".retake.mp4").exists())


if __name__ == "__main__":
    unittest.main()


class SilenceGateTests(unittest.TestCase):
    """A shot with no lines must be silent — the model babbles into wides."""

    def _run(self, transcripts, retakes=1):
        import resume_generation as rg
        from pipeline.llm import Scene
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "face.png").write_bytes(b"png")
            renders = []

            def fake_gen(engine, prompt, ref_images, out, ref_audios=None, **kw):
                renders.append(out)
                Path(out).write_bytes(f"take{len(renders)}".encode())
                return Path(out)

            def fake_refs(m, cfg, work_dir, style_name="", scene_id=0):
                return {"pictures": [{"slot": 1, "name": "A", "kind": "character",
                                      "path": str(wd / "face.png")}], "audios": []}

            scene = Scene(id=1, title="S", image_prompt="", video_prompt="", narration="",
                          mode="performance", lines=[],
                          metadata_extra={"mode": "performance", "cast": ["A", "B"],
                                          "seconds": 5, "establishing": True})
            tr = iter(transcripts)
            muted = []
            with mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
                 mock.patch("app.resolve_performance_references", side_effect=fake_refs), \
                 mock.patch.object(shot_gate, "available", return_value=True), \
                 mock.patch.object(shot_gate, "transcribe", side_effect=lambda *a: next(tr)), \
                 mock.patch.object(rg, "_write_silence_wav",
                                   side_effect=lambda p, s: Path(p).write_bytes(b"wav")), \
                 mock.patch.object(rg, "_get_duration", return_value=5.0), \
                 mock.patch.object(rg, "mux_video_audio",
                                   side_effect=lambda v, a, o, **k: (muted.append(1),
                                                                     Path(o).write_bytes(b"mutd"))[-1]), \
                 mock.patch.object(rg, "ensure_video_resolution"):
                out = rg.render_performance_scene(
                    scene, wd, {"performance_verify": True,
                                "performance_verify_retakes": retakes},
                    comfy_url="http://w:8188", vid_width=704, vid_height=1280)
                # Read before the TemporaryDirectory (and the clip) vanish.
                content = Path(out).read_bytes()
            return content, len(renders), bool(muted)

    def test_babbling_wide_is_retaken(self):
        content, renders, was_muted = self._run(["and seal it in a thween during it", ""])
        self.assertEqual(renders, 2)          # take + retake
        self.assertFalse(was_muted)           # the retake came back silent
        self.assertEqual(content, b"take2")

    def test_persistent_babble_gets_muted(self):
        content, renders, was_muted = self._run(
            ["it's the least of five and five", "tenine at five to ten minutes okay"])
        self.assertEqual(renders, 2)
        self.assertTrue(was_muted)            # last resort: strip the audio
        self.assertEqual(content, b"mutd")

    def test_a_genuinely_silent_wide_passes_untouched(self):
        content, renders, was_muted = self._run([""])
        self.assertEqual(renders, 1)
        self.assertFalse(was_muted)
