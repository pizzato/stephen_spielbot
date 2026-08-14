"""Music engines — registry, workflow parameterization, duration ceiling, mix loop."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as gapp
from pipeline import assembler, comfyui, engines


class MusicEngineRegistryTests(unittest.TestCase):
    def test_default_is_ace_step(self):
        self.assertEqual(engines.DEFAULT_MUSIC_ENGINE, "ace-step")
        self.assertEqual(engines.resolve_music(None)["key"], "ace-step")
        self.assertEqual(engines.resolve_music("nonsense")["key"], "ace-step")
        self.assertEqual(engines.resolve_music("minimax-music3")["key"], "minimax-music3")

    def test_every_engine_has_a_workflow_file(self):
        for key, eng in engines.MUSIC_ENGINES.items():
            path = comfyui.WORKFLOWS_DIR / eng["workflow"]
            self.assertTrue(path.exists(), f"{key}: missing {eng['workflow']}")
            # Placeholders are shared, so generate_music can stay engine-agnostic.
            text = path.read_text()
            for placeholder in ("{{TAGS}}", "{{DURATION}}", "{{SEED}}"):
                self.assertIn(placeholder, text, f"{key}: {eng['workflow']} lacks {placeholder}")

    def test_style_field_normalizes_and_mirrors(self):
        self.assertEqual(gapp._norm_music_engine("minimax-music3"), "minimax-music3")
        self.assertEqual(gapp._norm_music_engine("ace-step-2"), "ace-step")
        self.assertEqual(gapp._norm_music_engine(None), "ace-step")
        self.assertEqual(gapp.STYLE_FIELD_TO_FLAT["music_engine"], "default_music_engine")


class MusicWorkflowTests(unittest.TestCase):
    def _generate(self, engine_key, duration=90.0):
        captured = {}

        def fake_queue(workflow, client_id, comfy_url=None):
            captured["workflow"] = workflow
            return "pid"

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "music.wav"
            with mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "check_engine_supported"), \
                 mock.patch.object(comfyui, "_wait_for_completion") as waited, \
                 mock.patch.object(comfyui, "_get_outputs",
                                   return_value=[{"filename": "a.flac", "type": "output"}]), \
                 mock.patch.object(comfyui, "_download_output",
                                   side_effect=lambda item, dest, comfy_url=None: dest):
                comfyui.generate_music("Fjords", duration, out, tags="calm strings",
                                       seed=11, music_engine=engine_key)
            captured["wait_kwargs"] = waited.call_args.kwargs
        return captured

    def test_default_engine_uses_the_ace_graph(self):
        cap = self._generate(None)
        classes = {n["class_type"] for n in cap["workflow"].values()}
        self.assertIn("TextEncodeAceStepAudio1.5", classes)
        self.assertEqual(cap["wait_kwargs"]["timeout"], 600)

    def test_minimax_graph_is_wired_to_the_minimax_nodes(self):
        cap = self._generate("minimax-music3")
        wf = cap["workflow"]
        by_class = {n["class_type"]: n for n in wf.values()}
        self.assertIn("MiniMaxMusic3TextEncode", by_class)
        encode = by_class["MiniMaxMusic3TextEncode"]
        self.assertEqual(encode["inputs"]["caption"], "calm strings")
        self.assertEqual(encode["inputs"]["max_duration"], 90.0)
        self.assertEqual(encode["inputs"]["seed"], 11)
        # The latent takes the length the AR stage actually produced (the text
        # encode's second output), not the requested one — it can end early.
        encode_id = next(k for k, n in wf.items() if n["class_type"] == "MiniMaxMusic3TextEncode")
        self.assertEqual(by_class["EmptyMiniMaxMusic3LatentAudio"]["inputs"]["seconds"],
                         [encode_id, 1])
        # An 8B autoregressive pass needs far longer than ACE's 10 minutes.
        self.assertEqual(cap["wait_kwargs"]["timeout"], 1800)

    def test_duration_is_clamped_to_the_engine_ceiling(self):
        cap = self._generate("minimax-music3", duration=900.0)
        encode = next(n for n in cap["workflow"].values()
                      if n["class_type"] == "MiniMaxMusic3TextEncode")
        self.assertEqual(encode["inputs"]["max_duration"], 360.0)
        # ACE has no practical ceiling, so a long film still gets a full bed.
        cap = self._generate("ace-step", duration=900.0)
        latent = next(n for n in cap["workflow"].values()
                      if n["class_type"] == "EmptyAceStep1.5LatentAudio")
        self.assertEqual(latent["inputs"]["seconds"], 900.0)

    def test_too_old_worker_is_refused_before_queueing(self):
        eng = engines.resolve_music("minimax-music3")
        with mock.patch.object(comfyui, "comfyui_version", return_value=(0, 32, 0)):
            with self.assertRaises(RuntimeError) as ctx:
                comfyui.check_engine_supported(eng, "http://s1:8188")
        self.assertIn("0.33.0", str(ctx.exception))
        # A current worker passes.
        with mock.patch.object(comfyui, "comfyui_version", return_value=(0, 33, 0)):
            comfyui.check_engine_supported(eng, "http://s1:8188")


class MusicMixTests(unittest.TestCase):
    """A bed shorter than the picture is looped rather than left to run dry."""

    def _mix(self, video_dur, music_dur):
        calls = []
        durations = {"v": video_dur, "m": music_dur}

        def fake_duration(path):
            return durations["v"] if str(path).endswith("video.mp4") else durations["m"]

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            music = Path(tmp) / "music.wav"
            out = Path(tmp) / "out.mp4"
            for p in (video, music):
                p.write_bytes(b"x")
            with mock.patch.object(assembler, "_get_duration", side_effect=fake_duration), \
                 mock.patch.object(assembler, "_run", side_effect=lambda cmd: calls.append(cmd)):
                assembler.mix_background_music(video, music, out)
        return calls[0]

    def test_short_bed_loops(self):
        cmd = self._mix(video_dur=400.0, music_dur=360.0)
        self.assertIn("-stream_loop", cmd)
        self.assertEqual(cmd[cmd.index("-stream_loop") + 1], "-1")
        # Only the music input is looped, and it stays input #1.
        self.assertEqual(cmd.index("-stream_loop"), cmd.index("-i") + 2)

    def test_full_length_bed_is_not_looped(self):
        self.assertNotIn("-stream_loop", self._mix(video_dur=120.0, music_dur=126.0))


class MusicEngineStampTests(unittest.TestCase):
    """The chosen engine has to reach the worker that renders the bed."""

    def test_orchestrator_puts_the_engine_in_the_music_payload(self):
        from pipeline import orchestrator
        src = Path(orchestrator.__file__).read_text()
        self.assertIn('"music_engine": config.get("music_engine")', src)

    def test_worker_agent_passes_the_payload_engine_through(self):
        import worker_agent
        src = Path(worker_agent.__file__).read_text()
        self.assertIn('music_engine=p.get("music_engine")', src)


if __name__ == "__main__":
    unittest.main()
