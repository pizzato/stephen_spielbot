import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline.comfyui as comfyui


class ComfyLtxUpscaleTests(unittest.TestCase):
    def test_upscale_video_ltx_queues_packaged_workflow(self):
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

            with mock.patch.object(comfyui, "_upload_video", return_value="uploaded.mp4") as upload, \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion") as wait, \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "spielbot-ltx-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out) as download:
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
            upload.assert_called_once_with(src, comfy_url="http://worker:8188")
            wait.assert_called_once_with(
                "prompt-1",
                queued["client_id"],
                timeout=456,
                comfy_url="http://worker:8188",
            )
            download.assert_called_once()
            workflow = queued["workflow"]
            self.assertEqual(workflow["2"]["class_type"], "VHS_LoadVideoFFmpeg")
            self.assertEqual(workflow["2"]["inputs"]["video"], "uploaded.mp4")
            self.assertEqual(workflow["4"]["class_type"], "LTXVLatentUpsampler")
            self.assertEqual(workflow["6"]["inputs"]["width"], 1920)
            self.assertEqual(workflow["6"]["inputs"]["height"], 1080)
            self.assertEqual(workflow["7"]["class_type"], "VHS_VideoCombine")
            self.assertEqual(workflow["7"]["inputs"]["frame_rate"], 24)


if __name__ == "__main__":
    unittest.main()
