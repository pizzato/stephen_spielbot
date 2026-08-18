import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline.comfyui as comfyui


class ComfyLtxUpscaleTests(unittest.TestCase):
    def test_ic_lora_name_for_scale(self):
        self.assertEqual(
            comfyui._ic_lora_name_for_scale(960, 540, 1920, 1080),
            comfyui._IC_LORA_X2,
        )
        self.assertEqual(
            comfyui._ic_lora_name_for_scale(480, 270, 1920, 1080),
            comfyui._IC_LORA_X4,
        )

    def test_ic_lora_pixel_grid_matches_downscale_factor(self):
        # Latent (W/32, H/32) must be divisible by factor → pixel multiple 32*factor.
        self.assertEqual(comfyui._ic_lora_pixel_grid(comfyui._IC_LORA_X2), 64)
        self.assertEqual(comfyui._ic_lora_pixel_grid(comfyui._IC_LORA_X4), 128)
        # 1080 on 4× grid must not land on 1088 (latent h=34, 34%4≠0).
        self.assertEqual(comfyui._snap_ltx_dim(1080, 128, prefer="up"), 1152)
        self.assertEqual(1152 // 32 % 4, 0)

    def test_upscale_video_ltx_queues_ic_lora_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            queued = {}

            def fake_queue(workflow, client_id, comfy_url):
                queued["workflow"] = workflow
                queued["client_id"] = client_id
                queued["comfy_url"] = comfy_url
                return "prompt-1"

            with mock.patch.object(comfyui, "_stage_video_for_load", return_value="staged.mp4") as stage, \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion") as wait, \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "spielbot-ic-lora-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out) as download, \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out) as normalize, \
                 mock.patch("pipeline.assembler._get_video_dimensions", return_value=(960, 540)), \
                 mock.patch("pipeline.assembler._get_duration", return_value=2.0):
                result = comfyui.upscale_video_ltx(
                    src,
                    out,
                    1920,
                    1080,
                    fps=24,
                    timeout_seconds=456,
                    comfy_url="http://worker:8188",
                )

            self.assertEqual(result, out)
            stage.assert_called_once_with(src, comfy_url="http://worker:8188")
            wait.assert_called_once_with(
                "prompt-1",
                queued["client_id"],
                timeout=456,
                comfy_url="http://worker:8188",
            )
            download.assert_called_once()
            # Final output is the user-requested size; working latent grid may differ.
            normalize.assert_called_once_with(out, 1920, 1080)
            workflow = queued["workflow"]
            self.assertEqual(workflow["3"]["class_type"], "LTXICLoRALoaderModelOnly")
            self.assertEqual(
                workflow["3"]["inputs"]["lora_name"],
                comfyui._IC_LORA_X2,
            )
            self.assertEqual(workflow["8"]["class_type"], "VHS_LoadVideoFFmpeg")
            self.assertEqual(workflow["8"]["inputs"]["video"], "staged.mp4")
            self.assertEqual(workflow["10"]["class_type"], "LTXAddVideoICLoRAGuide")
            self.assertEqual(workflow["10"]["inputs"]["latent_downscale_factor"], ["3", 1])
            # 2× → pixel grid 64; 1080 → 1088, latent 34 divisible by 2.
            self.assertEqual(workflow["9"]["inputs"]["width"], 1920)
            self.assertEqual(workflow["9"]["inputs"]["height"], 1088)
            self.assertEqual(workflow["18"]["class_type"], "VHS_VideoCombine")
            self.assertEqual(workflow["18"]["inputs"]["frame_rate"], 24)

    def test_upscale_video_ltx_4x_uses_128_pixel_grid(self):
        """Regression: 1920x1088 latent 60x34 is not divisible by factor 4."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            queued = {}

            def fake_queue(workflow, client_id, comfy_url):
                queued["workflow"] = workflow
                return "prompt-1"

            with mock.patch.object(comfyui, "_stage_video_for_load", return_value="staged.mp4"), \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion"), \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "spielbot-ic-lora-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out), \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out) as normalize, \
                 mock.patch("pipeline.assembler._get_video_dimensions", return_value=(480, 270)), \
                 mock.patch("pipeline.assembler._get_duration", return_value=1.0):
                comfyui.upscale_video_ltx(src, out, 1920, 1080, fps=25, comfy_url="http://w:8188")

            wf = queued["workflow"]
            self.assertEqual(wf["3"]["inputs"]["lora_name"], comfyui._IC_LORA_X4)
            self.assertEqual(wf["9"]["inputs"]["width"], 1920)
            self.assertEqual(wf["9"]["inputs"]["height"], 1152)  # 1080 → 1152 (128 grid)
            # Latent 60x36 is divisible by 4.
            self.assertEqual(1920 // 32 % 4, 0)
            self.assertEqual(1152 // 32 % 4, 0)
            normalize.assert_called_once_with(out, 1920, 1080)

    def test_upscale_video_ltx_retries_dropped_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")

            with mock.patch.object(comfyui, "_stage_video_for_load", return_value="staged.mp4"), \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=["prompt-1", "prompt-2"]) as queue, \
                 mock.patch.object(comfyui, "_wait_for_completion", side_effect=[
                     comfyui.DroppedJobError("Job prompt-1 vanished from queue"),
                     None,
                 ]) as wait, \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "spielbot-ic-lora-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out), \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out), \
                 mock.patch("pipeline.assembler._get_video_dimensions", return_value=(480, 270)), \
                 mock.patch("pipeline.assembler._get_duration", return_value=1.0):
                comfyui.upscale_video_ltx(src, out, 1920, 1080, fps=25, comfy_url="http://w:8188")

            self.assertEqual(queue.call_count, 2)
            self.assertEqual(wait.call_count, 2)
            # 4× scale → x4 LoRA
            wf = queue.call_args_list[0][0][0]
            self.assertEqual(wf["3"]["inputs"]["lora_name"], comfyui._IC_LORA_X4)

    def test_latent_fallback_workflow_still_packaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            queued = {}

            def fake_queue(workflow, client_id, comfy_url):
                queued["workflow"] = workflow
                return "prompt-1"

            with mock.patch.object(comfyui, "_stage_video_for_load", return_value="staged.mp4"), \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion"), \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "spielbot-ltx-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out), \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out):
                comfyui.upscale_video_ltx_latent(src, out, 1920, 1080, fps=24)

            workflow = queued["workflow"]
            self.assertEqual(workflow["4"]["class_type"], "LTXVLatentUpsampler")
            self.assertEqual(workflow["3"]["inputs"]["model_name"],
                             "ltx-2.3-spatial-upscaler-x2-1.1.safetensors")

    def test_h3_latent_workflow_scale_and_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            queued = {}

            def fake_queue(workflow, client_id, comfy_url):
                queued["workflow"] = workflow
                return "prompt-1"

            with mock.patch.object(comfyui, "_stage_video_for_load", return_value="staged.mp4"), \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion"), \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "spielbot-h3-latent-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out), \
                 mock.patch("pipeline.assembler._get_duration", return_value=5.0), \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out):
                comfyui.upscale_video_h3_latent(
                    src, out, 1408, 2560, fps=24,
                    source_width=704, source_height=1280,
                )

            workflow = queued["workflow"]
            self.assertEqual(workflow["4"]["class_type"], "MinimaxH3LatentUpscalerNode3D")
            self.assertEqual(workflow["4"]["inputs"]["model_name"], comfyui._H3_LATENT_UPSCALER)
            # 704x1280 -> 1408x2560 is an exact doubling.
            self.assertEqual(workflow["4"]["inputs"]["scale"], 2.0)
            # Encodes and decodes through H3's own video VAE.
            self.assertEqual(workflow["1"]["inputs"]["vae_name"],
                             "minimax_h3_video_vae_fp16.safetensors")
            self.assertEqual(workflow["3"]["class_type"], "VAEEncode")

    def test_h3_latent_scale_clamped_to_node_ceiling(self):
        """The node tops out at 4x; a bigger jump must not send an invalid scale."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            queued = {}

            def fake_queue(workflow, client_id, comfy_url):
                queued["workflow"] = workflow
                return "prompt-1"

            with mock.patch.object(comfyui, "_stage_video_for_load", return_value="staged.mp4"), \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion"), \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "spielbot-h3-latent-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out), \
                 mock.patch("pipeline.assembler._get_duration", return_value=5.0), \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out):
                comfyui.upscale_video_h3_latent(
                    src, out, 3840, 2160, fps=24,
                    source_width=480, source_height=270,
                )

            self.assertEqual(queued["workflow"]["4"]["inputs"]["scale"], 4.0)

    def test_h3_latent_conforms_vae_length_drift(self):
        """H3's chunked VAE returns a different length; conform it either way.

        Long desyncs the film from its captions; short silently drops the tail
        of the scene, narration included.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            out.write_bytes(b"video")

            # source 5.00s in, padded 5.17s back out
            durations = iter([5.0, 5.1666])

            with mock.patch.object(comfyui, "_stage_video_for_load", return_value="staged.mp4"), \
                 mock.patch.object(comfyui, "_queue_prompt", return_value="prompt-1"), \
                 mock.patch.object(comfyui, "_wait_for_completion"), \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "spielbot-h3-latent-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out), \
                 mock.patch("pipeline.assembler._get_duration", side_effect=lambda *_a, **_k: next(durations)), \
                 mock.patch("pipeline.assembler.conform_video_to_source",
                            side_effect=lambda v, s, o, d: (Path(o).write_bytes(b"fixed"), o)[1]) as trim, \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out):
                comfyui.upscale_video_h3_latent(
                    src, out, 1408, 1408, fps=24.0,
                    source_width=704, source_height=704,
                )

            trim.assert_called_once()
            # conformed to the source length, not the padded one
            self.assertEqual(trim.call_args[0][3], 5.0)
            # audio comes from the source clip, not the round-tripped copy
            self.assertEqual(trim.call_args[0][1], src)

    def test_h3_latent_conforms_when_decode_comes_back_short(self):
        """Measured: a 6.250s/150-frame clip decoded back as 5.875s/141."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            out.write_bytes(b"video")
            durations = iter([6.25, 5.875])

            with mock.patch.object(comfyui, "_stage_video_for_load", return_value="staged.mp4"), \
                 mock.patch.object(comfyui, "_queue_prompt", return_value="prompt-1"), \
                 mock.patch.object(comfyui, "_wait_for_completion"), \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "spielbot-h3-latent-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out), \
                 mock.patch("pipeline.assembler._get_duration", side_effect=lambda *_a, **_k: next(durations)), \
                 mock.patch("pipeline.assembler.conform_video_to_source",
                            side_effect=lambda v, s, o, d: (Path(o).write_bytes(b"fixed"), o)[1]) as conform, \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out):
                comfyui.upscale_video_h3_latent(
                    src, out, 2048, 2048, fps=24.0,
                    source_width=1024, source_height=1024,
                )

            conform.assert_called_once()
            self.assertEqual(conform.call_args[0][3], 6.25)


if __name__ == "__main__":
    unittest.main()
