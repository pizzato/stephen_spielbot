import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline.assembler as assembler


class AssemblerToolResolutionTests(unittest.TestCase):
    def test_media_tool_path_can_be_configured_when_path_is_sparse(self):
        with mock.patch.dict("os.environ", {"FFPROBE_PATH": "/custom/bin/ffprobe"}, clear=False):
            reloaded = importlib.reload(assembler)
            self.assertEqual(reloaded._FFPROBE, "/custom/bin/ffprobe")
        importlib.reload(assembler)

    def test_upscale_video_uses_lanczos_scale_and_audio_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(512, 288)), \
                 mock.patch.object(assembler, "_run") as run:
                result = assembler.upscale_video(src, out, 1920, 1080)

            self.assertEqual(result, out)
            cmd = run.call_args.args[0]
            self.assertIn("scale=1920:1080:flags=lanczos:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1", cmd)
            self.assertIn("-c:a", cmd)
            self.assertIn("copy", cmd)

    def test_upscale_video_rejects_non_larger_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            src.write_bytes(b"video")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(1920, 1080)):
                with self.assertRaises(ValueError):
                    assembler.upscale_video(src, Path(tmp) / "out.mp4", 1280, 720)

    def test_temporal_ai_upscale_uses_configured_command_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")

            def fake_run(cmd, **kwargs):
                self.assertEqual(cmd, ["temporal-cli", "-i", str(src), "-o", str(out), "--size", "1920x1080"])
                out.write_bytes(b"upscaled")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(512, 288)), \
                 mock.patch.object(assembler, "_run", side_effect=fake_run):
                result = assembler.temporal_ai_upscale_video(
                    src,
                    out,
                    1920,
                    1080,
                    "temporal-cli -i {input} -o {output} --size {width}x{height}",
                )

            self.assertEqual(result, out)

    def test_temporal_ai_upscale_uses_packaged_comfy_workflow_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(512, 288)), \
                 mock.patch.object(assembler, "_get_video_fps", return_value=25.0), \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", return_value=out) as comfy_upscale, \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                result = assembler.temporal_ai_upscale_video(
                    src,
                    out,
                    1920,
                    1080,
                    timeout_seconds=123,
                    comfy_url="http://worker:8188",
                )

            self.assertEqual(result, out)
            comfy_upscale.assert_called_once_with(
                src,
                out,
                1920,
                1080,
                fps=25.0,
                timeout_seconds=123,
                comfy_url="http://worker:8188",
            )


if __name__ == "__main__":
    unittest.main()
