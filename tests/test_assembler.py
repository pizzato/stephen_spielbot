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

    def test_blank_result_retries_at_half_the_chunk(self):
        """A blank clip means the worker OOMed; split it rather than fail the film."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            out.write_bytes(b"upscaled")
            calls = []

            def fake_chunked(*a, **kw):
                calls.append(("chunked", kw.get("chunk_seconds")))
                return out

            def fake_single(*a, **kw):
                calls.append(("single", None))
                return out

            # blank the first time (the 7.25s single-shot), fine on the retry
            verdicts = iter([RuntimeError("came back blank"), None])

            def fake_verify(_src, _res):
                v = next(verdicts)
                if v:
                    raise v

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(1024, 1024)), \
                 mock.patch.object(assembler, "_get_duration", return_value=7.25), \
                 mock.patch.object(assembler, "_get_video_fps", return_value=24.0), \
                 mock.patch.object(assembler, "_verify_upscale_not_blank", side_effect=fake_verify), \
                 mock.patch.object(assembler, "_chunked_comfy_temporal_upscale", side_effect=fake_chunked), \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", side_effect=fake_single), \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                assembler.temporal_ai_upscale_video(
                    src, out, 2160, 2160, comfy_url="http://w:8188", chunk_seconds=12.0,
                )

            # first attempt single-shot (7.25s < 12s), retry chunked at half the clip
            self.assertEqual(calls[0][0], "single")
            self.assertEqual(calls[1][0], "chunked")
            self.assertAlmostEqual(calls[1][1], 3.625, places=3)

    def test_blank_result_gives_up_once_chunks_hit_the_floor(self):
        """Below the floor the retry cannot help, so the failure must surface."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            out.write_bytes(b"upscaled")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(1024, 1024)), \
                 mock.patch.object(assembler, "_get_duration", return_value=2.0), \
                 mock.patch.object(assembler, "_get_video_fps", return_value=24.0), \
                 mock.patch.object(assembler, "_verify_upscale_not_blank",
                                   side_effect=RuntimeError("came back blank")), \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", return_value=out), \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                with self.assertRaises(RuntimeError):
                    assembler.temporal_ai_upscale_video(
                        src, out, 2160, 2160, comfy_url="http://w:8188", chunk_seconds=2.0,
                    )

    def test_configured_chunk_seconds_beats_the_env_default(self):
        """The Settings value must force a split the 12s default would not make."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            out.write_bytes(b"upscaled")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(1024, 1024)), \
                 mock.patch.object(assembler, "_get_duration", return_value=7.25), \
                 mock.patch.object(assembler, "_get_video_fps", return_value=24.0), \
                 mock.patch.object(assembler, "_verify_upscale_not_blank"), \
                 mock.patch.object(assembler, "_chunked_comfy_temporal_upscale",
                                   return_value=out) as chunked, \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", return_value=out), \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                assembler.temporal_ai_upscale_video(
                    src, out, 2160, 2160, comfy_url="http://w:8188", chunk_seconds=4.0,
                )

            chunked.assert_called_once()
            self.assertEqual(chunked.call_args.kwargs["chunk_seconds"], 4.0)

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
            out.write_bytes(b"upscaled")  # the mocked upscaler's output

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
            out.write_bytes(b"upscaled")  # the mocked upscaler's output

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

    def _route_upscale(self, duration: float):
        """Run one upscale at *duration* and report whether it was chunked."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            out.write_bytes(b"upscaled")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(512, 288)), \
                 mock.patch.object(assembler, "_get_duration", return_value=duration), \
                 mock.patch.object(assembler, "_get_video_fps", return_value=25.0), \
                 mock.patch.object(assembler, "_chunked_comfy_temporal_upscale",
                                   return_value=out) as chunked, \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", return_value=out) as whole, \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                assembler.temporal_ai_upscale_video(
                    src, out, 1920, 1080,
                    timeout_seconds=123,
                    comfy_url="http://worker:8188",
                )
            return chunked.called, whole.called

    def test_typical_scene_upscales_in_one_job(self):
        """A ~11s scene is the length the fleet is known to handle whole."""
        chunked, whole = self._route_upscale(11.0)
        self.assertFalse(chunked)
        self.assertTrue(whole)

    def test_long_scene_is_split_before_the_worker_can_blank_it(self):
        """A 20s scene is what came back solid black on a GB10 — it must split."""
        chunked, whole = self._route_upscale(20.2)
        self.assertTrue(chunked)
        self.assertFalse(whole)

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


class BlankUpscaleGuardTests(unittest.TestCase):
    """ComfyUI reports success and returns a solid black clip when a worker runs
    out of memory, so the upscale has to be checked against its own source."""

    def _verify(self, source_luma, result_luma):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "src.mp4", Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            out.write_bytes(b"upscaled")
            profiles = {str(src): source_luma, str(out): result_luma}
            with mock.patch.object(assembler, "_luma_profile",
                                   side_effect=lambda p, **_kw: profiles[str(p)]), \
                 mock.patch.object(assembler, "_get_duration", return_value=20.0):
                assembler._verify_upscale_not_blank(src, out)

    def test_blank_result_is_rejected(self):
        with self.assertRaises(RuntimeError) as caught:
            self._verify([95.0] * 6, [16.0] * 6)
        self.assertIn("came back blank", str(caught.exception))

    def test_one_black_chunk_among_good_ones_is_rejected(self):
        """Averaging would hide this: half the clip black still reads as 50%."""
        with self.assertRaises(RuntimeError):
            self._verify([95.0] * 6, [93.0, 91.0, 94.0, 0.0, 0.0, 0.0])

    def test_faithful_result_is_accepted(self):
        self._verify([68.6, 70.1, 69.4, 68.0, 71.2, 70.4],
                     [70.4, 71.0, 70.9, 69.1, 72.0, 71.3])

    def test_genuinely_dark_scene_is_not_mistaken_for_a_failure(self):
        self._verify([2.0] * 6, [1.4] * 6)

    def test_unmeasurable_clip_does_not_fail_the_upscale(self):
        """A probe hiccup must not throw away an hour of GPU time."""
        self._verify([], [])

    def test_missing_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "src.mp4", Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            with self.assertRaises(RuntimeError):
                assembler._verify_upscale_not_blank(src, out)


if __name__ == "__main__":
    unittest.main()
