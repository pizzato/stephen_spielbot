"""MiniMax H3 I2V — frame/dimension math, workflow parameterization, dispatch."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import comfyui, engines


class H3MathTests(unittest.TestCase):
    def test_frame_count_grid(self):
        for secs in (None, 1.0, 4.0, 5.2, 6.0, 8.7, 14.9, 40.0):
            n = comfyui.h3_frame_count(secs)
            self.assertEqual(n % 17, 5, f"{secs}s -> {n} frames")
        # Short requests floor at the model minimum, long ones cap at 15 s.
        self.assertEqual(comfyui.h3_frame_count(1.0), comfyui.h3_frame_count(4.0))
        self.assertEqual(comfyui.h3_frame_count(40.0), comfyui.h3_frame_count(15.0))
        # 6 s at 24 fps rounds UP to the next valid count (144 → 158).
        self.assertEqual(comfyui.h3_frame_count(6.0), 158)

    def test_dimensions_snap_and_cap(self):
        # Under the pixel cap and already on the grid: unchanged.
        self.assertEqual(comfyui.h3_dimensions(832, 480), (832, 480))
        # Over the cap: scaled down, snapped to 32, aspect roughly kept.
        w, h = comfyui.h3_dimensions(1920, 1088)
        self.assertLessEqual(w * h, comfyui.H3_MAX_PIXELS)
        self.assertEqual(w % 32, 0)
        self.assertEqual(h % 32, 0)
        self.assertAlmostEqual(w / h, 1920 / 1088, delta=0.15)


class H3WorkflowTests(unittest.TestCase):
    def _generate(self, engine_key="minimax-h3"):
        eng = engines.resolve_video({}, engine_key)
        captured = {}

        def fake_queue(workflow, client_id, comfy_url=None):
            captured["workflow"] = workflow
            return "pid"

        outputs = [{"filename": "h3.mp4", "type": "output"}]
        with tempfile.TemporaryDirectory() as tmp:
            frame = Path(tmp) / "frame.png"
            frame.write_bytes(b"png")
            out = Path(tmp) / "out.mp4"
            with mock.patch.object(comfyui, "_upload_image", return_value="frame.png"), \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion") as waited, \
                 mock.patch.object(comfyui, "_get_outputs", return_value=outputs), \
                 mock.patch.object(comfyui, "_download_output",
                                   side_effect=lambda item, dest, comfy_url=None: dest):
                comfyui.generate_video_h3(
                    eng, "a cat sails a paper boat", frame, out,
                    width=1280, height=736, seed=7, duration_seconds=6.0,
                )
            captured["wait_kwargs"] = waited.call_args.kwargs
        return eng, captured

    def test_workflow_parameterization(self):
        eng, cap = self._generate()
        wf = cap["workflow"]
        i2v = wf["7"]["inputs"]
        self.assertTrue(i2v["prompt"].startswith("a cat sails a paper boat"))
        self.assertIn("diegetic", i2v["prompt"])  # audio steering applied
        # Quoted {{...}} placeholders must land as real ints, on H3's grids.
        self.assertIsInstance(i2v["width"], int)
        self.assertIsInstance(i2v["length"], int)
        self.assertEqual((i2v["width"], i2v["height"]), (1280, 736))
        self.assertEqual(i2v["length"] % 17, 5)
        # Engine-supplied model files reach the loaders.
        self.assertEqual(wf["1"]["inputs"]["unet_name"], eng["unet"])
        self.assertEqual(wf["2"]["inputs"]["clip_name"], eng["clip"])
        self.assertEqual(wf["3"]["inputs"]["vae_name"], eng["video_vae"])
        self.assertEqual(wf["4"]["inputs"]["vae_name"], eng["audio_vae"])
        self.assertEqual(wf["9"]["inputs"]["steps"], eng["steps"])
        self.assertEqual(wf["10"]["inputs"]["noise_seed"], 7)
        self.assertEqual(wf["16"]["inputs"]["format"], "mp4")
        # EasyCache sits between the loader and both model consumers.
        self.assertEqual(wf["17"]["class_type"], "EasyCache")
        self.assertEqual(wf["17"]["inputs"]["reuse_threshold"], eng["easycache_threshold"])
        self.assertEqual(wf["9"]["inputs"]["model"], ["17", 0])
        self.assertEqual(wf["11"]["inputs"]["model"], ["17", 0])

    def test_h3_uses_long_heartbeat_warmup(self):
        _, cap = self._generate()
        self.assertEqual(cap["wait_kwargs"]["heartbeat_warmup"],
                         comfyui._H3_HEARTBEAT_WARMUP)

    def test_turbo_workflow(self):
        eng, cap = self._generate("minimax-h3-turbo")
        wf = cap["workflow"]
        # The distillation LoRA replaces EasyCache in the model chain: both the
        # scheduler and the guider must see the patched model.
        self.assertEqual(wf["17"]["class_type"], "MiniMaxH3TurboLoRA")
        self.assertEqual(wf["17"]["inputs"]["model"], ["1", 0])
        self.assertEqual(wf["17"]["inputs"]["lora_name"], eng["lora"])
        self.assertEqual(wf["9"]["inputs"]["model"], ["17", 0])
        self.assertEqual(wf["11"]["inputs"]["model"], ["17", 0])
        self.assertNotIn("EasyCache", {n["class_type"] for n in wf.values()})
        # Video and audio ride different flow schedules — the few-step render
        # only works through the custom dual-clock sampler.
        self.assertEqual(wf["8"]["class_type"], "MiniMaxH3TurboSampler")
        self.assertEqual(wf["9"]["inputs"]["steps"], eng["steps"])
        self.assertEqual(wf["1"]["inputs"]["unet_name"], eng["unet"])


class VideoDispatchTests(unittest.TestCase):
    def test_dispatch_by_family(self):
        with mock.patch.object(comfyui, "generate_video_continuation") as ltx, \
             mock.patch.object(comfyui, "generate_video_h3") as h3:
            comfyui.generate_video_with_engine(None, "p", "n", Path("f"), Path("o"))
            ltx.assert_called_once()
            h3.assert_not_called()

            ltx.reset_mock()
            comfyui.generate_video_with_engine(
                engines.resolve_video({}, "ltx23"), "p", "n", Path("f"), Path("o"))
            ltx.assert_called_once()
            h3.assert_not_called()

            ltx.reset_mock()
            comfyui.generate_video_with_engine(
                engines.resolve_video({}, "minimax-h3"), "p", "n", Path("f"), Path("o"))
            h3.assert_called_once()
            ltx.assert_not_called()


if __name__ == "__main__":
    unittest.main()
