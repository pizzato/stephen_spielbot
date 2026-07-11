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
            # 1080 snaps to 1088 (LTX 32-pixel grid).
            normalize.assert_called_once_with(out, 1920, 1088)
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
            self.assertEqual(workflow["9"]["inputs"]["width"], 1920)
            self.assertEqual(workflow["9"]["inputs"]["height"], 1088)
            self.assertEqual(workflow["18"]["class_type"], "VHS_VideoCombine")
            self.assertEqual(workflow["18"]["inputs"]["frame_rate"], 24)

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


if __name__ == "__main__":
    unittest.main()
