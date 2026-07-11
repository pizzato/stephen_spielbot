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

    def test_concatenate_scenes_hard_cut_uses_stream_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene_1 = Path(tmp) / "scene_1.mp4"
            scene_2 = Path(tmp) / "scene_2.mp4"
            out = Path(tmp) / "out.mp4"
            scene_1.write_bytes(b"one")
            scene_2.write_bytes(b"two")

            with mock.patch.object(assembler, "_concat_video_chunks", return_value=out) as concat, \
                 mock.patch.object(assembler, "concatenate_scenes") as fallback:
                result = assembler.concatenate_scenes_hard_cut([scene_1, scene_2], out)

            self.assertEqual(result, out)
            concat.assert_called_once_with([scene_1, scene_2], out)
            fallback.assert_not_called()

    def test_concatenate_scenes_hard_cut_falls_back_without_fades(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene_1 = Path(tmp) / "scene_1.mp4"
            scene_2 = Path(tmp) / "scene_2.mp4"
            out = Path(tmp) / "out.mp4"
            scene_1.write_bytes(b"one")
            scene_2.write_bytes(b"two")
            out.write_bytes(b"partial")

            with mock.patch.object(assembler, "_concat_video_chunks", side_effect=RuntimeError("copy failed")), \
                 mock.patch.object(assembler, "concatenate_scenes", return_value=out) as fallback:
                result = assembler.concatenate_scenes_hard_cut([scene_1, scene_2], out)

            self.assertEqual(result, out)
            fallback.assert_called_once_with([scene_1, scene_2], out, fade=0.0)

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
                 mock.patch.object(assembler, "_get_duration", return_value=3.0), \
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
            comfy_upscale.assert_called_once()
            args, kwargs = comfy_upscale.call_args
            self.assertEqual(args[:4], (src, out, 1920, 1080))
            self.assertEqual(kwargs.get("fps"), 25.0)
            self.assertEqual(kwargs.get("timeout_seconds"), 123)
            self.assertEqual(kwargs.get("comfy_url"), "http://worker:8188")

    def test_ltx_latent_engine_uses_latent_upscaler(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(512, 288)), \
                 mock.patch.object(assembler, "_get_duration", return_value=2.0), \
                 mock.patch.object(assembler, "_get_video_fps", return_value=25.0), \
                 mock.patch("pipeline.comfyui.upscale_video_ltx_latent", return_value=out) as latent, \
                 mock.patch("pipeline.comfyui.upscale_video_ltx") as ic, \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                result = assembler.temporal_ai_upscale_video(
                    src, out, 1920, 1080,
                    timeout_seconds=60,
                    comfy_url="http://worker:8188",
                    engine="ltx_latent",
                )

            self.assertEqual(result, out)
            latent.assert_called_once()
            ic.assert_not_called()

    def test_temporal_ai_upscale_chunks_long_packaged_comfy_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")

            extract_calls = []

            def fake_extract(input_path, output_path, start, duration):
                extract_calls.append((start, duration))
                output_path.write_bytes(b"chunk")
                return output_path

            def fake_xfade(chunks, output_path, body_seconds, overlap_seconds):
                self.assertEqual(len(chunks), 3)
                self.assertEqual(body_seconds, 4.0)
                self.assertEqual(overlap_seconds, 0.5)
                output_path.write_bytes(b"joined")
                return output_path

            def fake_mux(video_path, source_path, output_path, target_duration=None):
                self.assertEqual(target_duration, 9.0)
                output_path.write_bytes(b"muxed")
                return output_path

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(512, 288)), \
                 mock.patch.object(assembler, "_get_duration", return_value=9.0), \
                 mock.patch.object(assembler, "_get_video_fps", return_value=25.0), \
                 mock.patch.object(assembler, "_extract_temporal_chunk", side_effect=fake_extract) as extract, \
                 mock.patch.object(assembler, "_xfade_video_chunks", side_effect=fake_xfade) as xfade, \
                 mock.patch.object(assembler, "_concat_video_chunks") as concat, \
                 mock.patch.object(assembler, "_mux_source_audio", side_effect=fake_mux) as mux, \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", side_effect=lambda _src, dst, *_args, **_kw: dst) as comfy_upscale, \
                 mock.patch.dict("os.environ", {
                     "TEMPORAL_VIDEO_UPSCALER_CMD": "",
                     "TEMPORAL_VIDEO_UPSCALE_CHUNK_SECONDS": "4",
                     "TEMPORAL_VIDEO_UPSCALE_CHUNK_OVERLAP": "0.5",
                 }, clear=False):
                result = assembler.temporal_ai_upscale_video(
                    src,
                    out,
                    1920,
                    1080,
                    timeout_seconds=123,
                    comfy_url="http://worker:8188",
                )

            self.assertEqual(result, out)
            self.assertEqual(extract.call_count, 3)
            self.assertEqual(comfy_upscale.call_count, 3)
            # Non-final chunks extend by the overlap so xfade can blend the same
            # source-time region; final chunk is the remainder only.
            self.assertEqual(extract_calls[0], (0.0, 4.5))
            self.assertEqual(extract_calls[1], (4.0, 4.5))
            self.assertEqual(extract_calls[2], (8.0, 1.0))
            xfade.assert_called_once()
            concat.assert_not_called()
            mux.assert_called_once()

    def test_chunked_upscale_hard_concats_when_overlap_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")

            def fake_concat(chunks, output_path):
                self.assertEqual(len(chunks), 2)
                output_path.write_bytes(b"joined")
                return output_path

            def fake_mux(video_path, source_path, output_path, target_duration=None):
                output_path.write_bytes(b"muxed")
                return output_path

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(512, 288)), \
                 mock.patch.object(assembler, "_get_duration", return_value=7.0), \
                 mock.patch.object(assembler, "_get_video_fps", return_value=25.0), \
                 mock.patch.object(assembler, "_extract_temporal_chunk") as extract, \
                 mock.patch.object(assembler, "_concat_video_chunks", side_effect=fake_concat) as concat, \
                 mock.patch.object(assembler, "_xfade_video_chunks") as xfade, \
                 mock.patch.object(assembler, "_mux_source_audio", side_effect=fake_mux), \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", side_effect=lambda _src, dst, *_args, **_kw: dst), \
                 mock.patch.dict("os.environ", {
                     "TEMPORAL_VIDEO_UPSCALER_CMD": "",
                     "TEMPORAL_VIDEO_UPSCALE_CHUNK_SECONDS": "4",
                     "TEMPORAL_VIDEO_UPSCALE_CHUNK_OVERLAP": "0",
                 }, clear=False):
                assembler.temporal_ai_upscale_video(
                    src, out, 1920, 1080, comfy_url="http://worker:8188",
                )

            self.assertEqual(extract.call_count, 2)
            concat.assert_called_once()
            xfade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
