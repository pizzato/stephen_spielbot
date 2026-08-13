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

    def test_a_typographic_apostrophe_is_the_same_word(self):
        """Scripts are written with ’ and ASR writes ' — the same line."""
        self.assertEqual(shot_gate.score("I'm David and we'll look at Norway",
                                         "I’m David and we’ll look at Norway"), 1.0)
        self.assertEqual(shot_gate.word_count("I’m David"), 2)


class TruncationTests(unittest.TestCase):
    """Telling "ran out of clip" apart from "said the wrong thing"."""

    LINE = ("Mountains. Darkness. Impossible wealth. I'm David Attenbot "
            "and today we'll look at history of Norway")

    def test_a_cut_off_line_is_truncation(self):
        # What the real take actually delivered before its last frame.
        heard = "mountains darkness is it impossible wealth I'm David Attenbott and today"
        self.assertTrue(shot_gate.truncated(heard, self.LINE))
        # ...and the gate's own score does NOT catch it: a matching head keeps
        # the similarity above threshold, which is why it shipped.
        self.assertGreater(shot_gate.score(heard, self.LINE), 0.4)

    def test_wrong_words_are_not_truncation(self):
        self.assertFalse(shot_gate.truncated("something else entirely was said",
                                             "You can burn the trees"))

    def test_a_complete_take_is_not_truncation(self):
        self.assertFalse(shot_gate.truncated(self.LINE, self.LINE))
        self.assertFalse(shot_gate.truncated("", self.LINE))

    def test_invented_tail_speech_is_not_truncation(self):
        self.assertFalse(shot_gate.truncated(
            "ready for our big walk keep your little chovia", "ready for our big walk"))

    def test_the_retake_length_comes_from_the_delivered_pace(self):
        # The real take: 11 of the line's 15 words inside an 8 s clip, so the
        # whole line needs 8 × 15/11 ≈ 10.9 s at that pace, plus a beat of air.
        heard = "mountains darkness is it impossible wealth I'm David Attenbott and today"
        self.assertEqual(shot_gate.word_count(heard), 11)
        secs = shot_gate.seconds_for_full_line(heard, self.LINE, 8.0)
        self.assertAlmostEqual(secs, 8.0 * 15 / 11 + shot_gate.RETAKE_AIR_SECONDS, places=2)
        # Enough for the ~10 s this scene actually needed, and inside the model's
        # 15 s ceiling — a retake that can land the line.
        self.assertGreater(secs, 10.0)

    def test_nothing_to_measure_returns_zero(self):
        self.assertEqual(shot_gate.seconds_for_full_line("", self.LINE, 8.0), 0.0)
        self.assertEqual(shot_gate.seconds_for_full_line(self.LINE, self.LINE, 8.0), 0.0)
        self.assertEqual(shot_gate.seconds_for_full_line("a b", "a b c d", 0.0), 0.0)


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

    def test_a_truncated_take_is_retaken_LONGER(self):
        """The clip, not the seed, is what a truncation is about.

        Measured on a real film: 15 words sized to 8.0 s came out at 1.4 words
        a second and lost the last third. Three re-generations produced three
        8.000 s clips cut in the same place — a fresh roll cannot buy time.

        The take also scored 0.62, ABOVE the threshold, because similarity
        rewards a matching head — so the gate has to notice the truncation
        itself or the shot ships cut.
        """
        import resume_generation as rg
        from pipeline.llm import Scene
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "face.png").write_bytes(b"png")
            asked = []

            def fake_gen(engine, prompt, ref_images, out, ref_audios=None, **kw):
                asked.append(kw["duration_seconds"])
                Path(out).write_bytes(b"take")
                return Path(out)

            def fake_refs(m, cfg, work_dir, style_name="", scene_id=0):
                return {"pictures": [{"slot": 1, "name": "A", "kind": "character",
                                      "path": str(wd / "face.png")}], "audios": []}

            line = ("Mountains. Darkness. Impossible wealth. I'm David Attenbot "
                    "and today we'll look at history of Norway")
            scene = Scene(id=1, title="S", image_prompt="", video_prompt="", narration="",
                          mode="performance", lines=[{"speaker": "A", "text": line}],
                          metadata_extra={"mode": "performance", "cast": ["A"]})
            # The first take says the opening and stops; the retake lands it.
            heard = "Mountains darkness impossible wealth I'm David Attenbot and today"
            self.assertGreater(shot_gate.score(heard, line), shot_gate.DEFAULT_THRESHOLD)
            scores = iter([(shot_gate.score(heard, line), heard), (0.95, line)])
            with mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
                 mock.patch("app.resolve_performance_references", side_effect=fake_refs), \
                 mock.patch.object(shot_gate, "available", return_value=True), \
                 mock.patch.object(shot_gate, "verify", side_effect=lambda *a: next(scores)), \
                 mock.patch.object(rg, "_get_duration", return_value=8.0), \
                 mock.patch.object(rg, "ensure_video_resolution"):
                rg.render_performance_scene(
                    scene, wd, {"performance_verify": True}, comfy_url="http://w:8188",
                    vid_width=704, vid_height=1280)
            self.assertEqual(len(asked), 2)
            self.assertEqual(asked[0], 8.0)            # the words' own estimate
            # 9 of 15 words in 8.0 s ⟹ 8 × 15/9 ≈ 13.3 s, plus a beat of air.
            self.assertAlmostEqual(asked[1], 8.0 * 15 / 9 + shot_gate.RETAKE_AIR_SECONDS,
                                   places=2)

    def test_wrong_words_are_retaken_at_the_SAME_length(self):
        """A take that said the wrong thing has time to spare — only a
        truncation buys more, or every miss would inflate the render."""
        import resume_generation as rg
        from pipeline.llm import Scene
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "face.png").write_bytes(b"png")
            asked = []

            def fake_gen(engine, prompt, ref_images, out, ref_audios=None, **kw):
                asked.append(kw["duration_seconds"])
                Path(out).write_bytes(b"take")
                return Path(out)

            def fake_refs(m, cfg, work_dir, style_name="", scene_id=0):
                return {"pictures": [{"slot": 1, "name": "A", "kind": "character",
                                      "path": str(wd / "face.png")}], "audios": []}

            scene = Scene(id=1, title="S", image_prompt="", video_prompt="", narration="",
                          mode="performance",
                          lines=[{"speaker": "A", "text": "You can burn the trees"}],
                          metadata_extra={"mode": "performance", "cast": ["A"]})
            scores = iter([(0.1, "something else entirely was said"), (0.9, "ok")])
            with mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
                 mock.patch("app.resolve_performance_references", side_effect=fake_refs), \
                 mock.patch.object(shot_gate, "available", return_value=True), \
                 mock.patch.object(shot_gate, "verify", side_effect=lambda *a: next(scores)), \
                 mock.patch.object(rg, "_get_duration", return_value=5.0), \
                 mock.patch.object(rg, "ensure_video_resolution"):
                rg.render_performance_scene(
                    scene, wd, {"performance_verify": True}, comfy_url="http://w:8188",
                    vid_width=704, vid_height=1280)
            self.assertEqual(asked[0], asked[1])

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
