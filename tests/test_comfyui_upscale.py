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

            with mock.patch.object(comfyui, "_stage_video_for_load", return_value="staged.mp4") as stage, \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion") as wait, \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "spielbot-ltx-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out) as download, \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out) as normalize:
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
            normalize.assert_called_once_with(out, 1920, 1080)
            workflow = queued["workflow"]
            self.assertEqual(workflow["2"]["class_type"], "VHS_LoadVideoFFmpeg")
            self.assertEqual(workflow["2"]["inputs"]["video"], "staged.mp4")
            self.assertNotIn("vae", workflow["2"]["inputs"])
            self.assertNotIn("format", workflow["2"]["inputs"])
            self.assertEqual(workflow["8"]["class_type"], "VAEEncode")
            self.assertEqual(workflow["8"]["inputs"]["pixels"], ["2", 0])
            self.assertEqual(workflow["4"]["class_type"], "LTXVLatentUpsampler")
            self.assertEqual(workflow["4"]["inputs"]["samples"], ["8", 0])
            self.assertEqual(workflow["5"]["class_type"], "VAEDecode")
            self.assertEqual(workflow["5"]["inputs"]["samples"], ["4", 0])
            self.assertEqual(workflow["7"]["class_type"], "VHS_VideoCombine")
            self.assertEqual(workflow["7"]["inputs"]["images"], ["5", 0])
            self.assertNotIn("vae", workflow["7"]["inputs"])
            self.assertEqual(workflow["7"]["inputs"]["frame_rate"], 24)

    def test_stage_video_for_remote_worker_uses_rsync_and_docker_cp(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mp4"
            src.write_bytes(b"video")
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(comfyui.uuid, "uuid4", side_effect=[
                mock.Mock(hex="abcdef1234567890"),
                mock.Mock(hex="feedface12345678"),
            ]), \
                 mock.patch.object(comfyui.subprocess, "run", side_effect=fake_run):
                name = comfyui._stage_video_for_load(src, comfy_url="http://worker-a:8188")

            self.assertEqual(name, "spielbot-upscale-abcdef123456.mp4")
            self.assertIn(["ssh", "--", "worker-a", "mkdir", "-p", "/tmp/spielbot-comfy-input-feedface"], calls)
            self.assertIn(["rsync", "-az", str(src), "worker-a:/tmp/spielbot-comfy-input-feedface/spielbot-upscale-abcdef123456.mp4"], calls)
            self.assertIn([
                "ssh", "--", "worker-a", "docker", "cp",
                "/tmp/spielbot-comfy-input-feedface/spielbot-upscale-abcdef123456.mp4",
                "spielbot-worker-comfyui-1:/opt/ComfyUI/input/spielbot-upscale-abcdef123456.mp4",
            ], calls)


if __name__ == "__main__":
    unittest.main()
