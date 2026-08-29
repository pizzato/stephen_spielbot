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

class ComfyFlashVsrUpscaleTests(unittest.TestCase):
    def _run(self, w, h, src_w, src_h, seconds=5.0):
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
                     {"filename": "spielbot-flashvsr-upscale_00001.mp4", "subfolder": "", "type": "output"}
                 ]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out), \
                 mock.patch("pipeline.assembler._get_duration", return_value=seconds), \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out):
                comfyui.upscale_video_flashvsr(
                    src, out, w, h, fps=24,
                    source_width=src_w, source_height=src_h,
                )
            return queued["workflow"]

    def test_exact_double_is_2x_untiled(self):
        wf = self._run(2560, 1408, 1280, 704)
        node = wf["3"]
        self.assertEqual(node["class_type"], "FlashVSRNode")
        self.assertEqual(node["inputs"]["model"], comfyui._FLASHVSR_MODEL)
        self.assertEqual(node["inputs"]["mode"], "tiny")
        self.assertEqual(node["inputs"]["scale"], 2)
        self.assertIs(node["inputs"]["tiled_dit"], False)
        self.assertIs(node["inputs"]["tiled_vae"], False)
        # Source audio rides through to the combine.
        self.assertEqual(wf["7"]["inputs"]["audio"], ["2", 2])

    def test_slightly_over_2x_stays_at_2x(self):
        """An H3 1920x1024 render finishing at 4K UHD is 2.11x — 4x would cost
        ten times more; the final normalize covers the remainder."""
        wf = self._run(3840, 2160, 1920, 1024)
        self.assertEqual(wf["3"]["inputs"]["scale"], 2)
        self.assertIs(wf["3"]["inputs"]["tiled_dit"], False)

    def test_big_jump_is_4x_and_tiled(self):
        """5120x2816 out of 1280x704 ran out of memory untiled on a GB10."""
        wf = self._run(5120, 2816, 1280, 704)
        self.assertEqual(wf["3"]["inputs"]["scale"], 4)
        self.assertIs(wf["3"]["inputs"]["tiled_dit"], True)
        self.assertIs(wf["3"]["inputs"]["tiled_vae"], True)

    def test_long_clip_tiles_even_at_2x(self):
        """Memory scales with frames too: 12 s of 1920x1024 at 2x OOMed untiled."""
        wf = self._run(3840, 2160, 1920, 1024, seconds=12.25)
        self.assertEqual(wf["3"]["inputs"]["scale"], 2)
        self.assertIs(wf["3"]["inputs"]["tiled_dit"], True)

    def test_untiled_oom_retries_tiled(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            submitted = []

            def fake_queue(workflow, client_id, comfy_url):
                submitted.append(workflow["3"]["inputs"]["tiled_dit"])
                return f"prompt-{len(submitted)}"

            def fake_wait(prompt_id, client_id, timeout, comfy_url):
                if prompt_id == "prompt-1":
                    raise RuntimeError("ComfyUI execution error at FlashVSRNode: "
                                       "Allocation on device 0 would exceed allowed memory. (out of memory)")

            with mock.patch.object(comfyui, "_stage_video_for_load", return_value="staged.mp4"), \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion", side_effect=fake_wait), \
                 mock.patch.object(comfyui, "_get_outputs", return_value=[
                     {"filename": "x.mp4", "subfolder": "", "type": "output"}]), \
                 mock.patch.object(comfyui, "_download_output", return_value=out), \
                 mock.patch("pipeline.assembler._get_duration", return_value=5.0), \
                 mock.patch.object(comfyui, "_ensure_exact_video_resolution", return_value=out):
                comfyui.upscale_video_flashvsr(
                    src, out, 2560, 1408, fps=24, source_width=1280, source_height=704)
            self.assertEqual(submitted, [False, True])

    def test_dispatcher_routes_flashvsr(self):
        from pipeline import assembler
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp4"
            src.write_bytes(b"video")
            out = Path(tmp) / "out.mp4"
            def fake_clock(video_path, _source, output_path, _duration):
                output_path.write_bytes(video_path.read_bytes())
                return output_path

            def fake_up(inp, outp, *a, **kw):
                outp.write_bytes(b"upscaled")
                return outp

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(1280, 704)), \
                 mock.patch.object(assembler, "_get_duration", return_value=5.0), \
                 mock.patch.object(assembler, "_constant_rate_source", side_effect=lambda inp, _o: (inp, 24.0)), \
                 mock.patch.object(assembler, "_verify_upscale_not_blank"), \
                 mock.patch.object(assembler, "_restore_source_clock", side_effect=fake_clock), \
                 mock.patch("pipeline.comfyui.upscale_video_flashvsr", side_effect=fake_up) as up, \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                result = assembler.temporal_ai_upscale_video(
                    src, out, 2560, 1408, engine="flashvsr", comfy_url="http://w:8188")
            self.assertEqual(result, out)
            self.assertEqual(up.call_count, 1)
            self.assertEqual(up.call_args.kwargs["source_width"], 1280)


class UpscaleResolutionSplitTests(unittest.TestCase):
    """Finishing sizes must be reachable by upscale and by nothing else."""

    def test_render_list_excludes_upscale_only_tiers(self):
        import app
        for name in app._RESOLUTIONS:
            self.assertNotIn("4K", name, f"4K leaked into the render list: {name}")
            self.assertNotIn("QHD", name, f"QHD leaked into the render list: {name}")

    def test_upscale_list_is_a_superset(self):
        import app
        for name in app._RESOLUTIONS:
            self.assertIn(name, app._UPSCALE_RESOLUTIONS)
        extra = set(app._UPSCALE_RESOLUTIONS) - set(app._RESOLUTIONS)
        self.assertEqual(len(extra), 6)  # QHD + 4K across three orientations

    def test_four_k_present_for_every_orientation(self):
        import app
        for want in ("Landscape 4K (3840×2160)",
                     "Portrait 4K (2160×3840)",
                     "Square 4K (2160×2160)"):
            self.assertIn(want, app._UPSCALE_RESOLUTIONS)

    def test_default_render_resolution_still_renderable(self):
        import app
        self.assertIn(app._DEFAULT_RESOLUTION, app._RESOLUTIONS)

    def test_split_render_target_passes_render_names_through(self):
        import app
        for name in app._RESOLUTIONS:
            self.assertEqual(app.split_render_target(name), (name, ""))

    def test_split_render_target_maps_upscale_only_to_top_render_tier(self):
        import app
        self.assertEqual(
            app.split_render_target("Landscape 4K (3840×2160)"),
            ("Landscape FHD (1920×1080)", "Landscape 4K (3840×2160)"))
        self.assertEqual(
            app.split_render_target("Portrait QHD (1440×2560)"),
            ("Portrait FHD (1080×1920)", "Portrait QHD (1440×2560)"))
        self.assertEqual(
            app.split_render_target("Square 4K (2160×2160)"),
            ("Square FHD (1080×1080)", "Square 4K (2160×2160)"))

    def test_split_render_target_render_half_is_always_renderable(self):
        import app
        for name in app._UPSCALE_RESOLUTIONS:
            render_name, finish = app.split_render_target(name)
            self.assertIn(render_name, app._RESOLUTIONS)
            if finish:
                self.assertNotIn(finish, app._RESOLUTIONS)
                self.assertIn(finish, app._UPSCALE_RESOLUTIONS)

    def test_split_render_target_unknown_and_blank_pass_through(self):
        import app
        self.assertEqual(app.split_render_target(""), ("", ""))
        self.assertEqual(app.split_render_target("not a size"), ("not a size", ""))


class FinishUpscaleScenesTests(unittest.TestCase):
    """The pipeline's finishing stage lifts rendered scene clips to a QHD/4K
    target before final assembly (resume_generation._finish_upscale_scenes)."""

    def _work_dir(self, tmp, n=2):
        wd = Path(tmp)
        clips = []
        for i in range(1, n + 1):
            p = wd / f"scene_{i:02d}_final.mp4"
            p.write_bytes(b"x" * 20_000)
            clips.append(p)
        return wd, clips

    class _Pool:
        def __init__(self):
            self.acquired, self.released = 0, 0

        def acquire(self):
            self.acquired += 1
            return "http://worker:8188"

        def release(self, url):
            self.released += 1

    @staticmethod
    def _fake_upscale(inp, out, w, h, **kw):
        Path(out).write_bytes(b"u" * 20_000)
        return Path(out)

    def test_noop_without_finish_resolution(self):
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as tmp:
            wd, clips = self._work_dir(tmp)
            got = rg._finish_upscale_scenes(
                wd, clips, {}, wd / "progress.json", self._Pool(), 1080, 1920)
            self.assertEqual(got, (clips, 1080, 1920))

    def test_fast_mode_upscales_every_scene_to_target_dims(self):
        import resume_generation as rg
        cfg = {"finish_resolution": "Portrait 4K (2160×3840)",
               "finish_upscale_mode": "fast"}
        with tempfile.TemporaryDirectory() as tmp:
            wd, clips = self._work_dir(tmp)
            with mock.patch.object(rg, "upscale_video",
                                   side_effect=self._fake_upscale) as up:
                out, w, h = rg._finish_upscale_scenes(
                    wd, clips, cfg, wd / "progress.json", self._Pool(), 1080, 1920)
            self.assertEqual((w, h), (2160, 3840))
            self.assertEqual(up.call_count, 2)
            self.assertEqual([p.parent.name for p in out],
                             ["finish_upscale_scenes"] * 2)
            for p in out:
                self.assertTrue(p.exists())

    def test_cached_scene_outputs_are_reused_on_resume(self):
        import resume_generation as rg
        cfg = {"finish_resolution": "Portrait 4K (2160×3840)",
               "finish_upscale_mode": "fast"}
        with tempfile.TemporaryDirectory() as tmp:
            wd, clips = self._work_dir(tmp)
            with mock.patch.object(rg, "upscale_video",
                                   side_effect=self._fake_upscale) as up:
                rg._finish_upscale_scenes(
                    wd, clips, cfg, wd / "progress.json", self._Pool(), 1080, 1920)
                rg._finish_upscale_scenes(
                    wd, clips, cfg, wd / "progress.json", self._Pool(), 1080, 1920)
            self.assertEqual(up.call_count, 2)  # second run reused both

    def test_ai_failure_falls_back_to_fast_and_releases_worker(self):
        import resume_generation as rg
        cfg = {"finish_resolution": "Portrait QHD (1440×2560)",
               "finish_upscale_mode": "h3_latent"}
        with tempfile.TemporaryDirectory() as tmp:
            wd, clips = self._work_dir(tmp, n=1)
            pool = self._Pool()
            with mock.patch.object(rg, "temporal_ai_upscale_video",
                                   side_effect=RuntimeError("no node")), \
                 mock.patch.object(rg, "upscale_video",
                                   side_effect=self._fake_upscale) as up:
                out, w, h = rg._finish_upscale_scenes(
                    wd, clips, cfg, wd / "progress.json", pool, 1080, 1920)
            self.assertEqual((w, h), (1440, 2560))
            self.assertEqual(up.call_count, 1)
            self.assertEqual(pool.acquired, 1)
            self.assertEqual(pool.released, 1)
            self.assertTrue(out[0].exists())

class PackagedWorkflowSanityTests(unittest.TestCase):
    """Inputs ComfyUI does not recognise are dropped silently, so a graph can
    look configured while doing nothing. VAEDecode carried tile_size/overlap/
    temporal_size/temporal_overlap for the tiled decode it never performed."""

    def test_vae_decode_nodes_carry_no_phantom_inputs(self):
        import json
        from pipeline.comfyui import WORKFLOWS_DIR
        for path in sorted(Path(WORKFLOWS_DIR).glob("*.json")):
            graph = json.loads(path.read_text())
            for node_id, node in graph.items():
                if node.get("class_type") != "VAEDecode":
                    continue
                extra = set(node.get("inputs", {})) - {"samples", "vae"}
                self.assertFalse(
                    extra,
                    f"{path.name} node {node_id} passes {sorted(extra)} to VAEDecode, "
                    "which has no such inputs — ComfyUI drops them silently",
                )


if __name__ == "__main__":
    unittest.main()


class ComfyRejectionReasonTests(unittest.TestCase):
    """A rejection has to say WHY on its first line: callers keep only that
    line (a film task stores ``str(e).splitlines()[0]``), so a bare status code
    reads on screen as if nothing happened at all."""

    MISSING_NODE = (
        '{"error": {"type": "missing_node_type", "message": "Node \'FlashVSRNode\' not '
        'found. The custom node may not be installed.", "details": "Node ID \'#3\'", '
        '"extra_info": {"node_id": "3", "class_type": "FlashVSRNode", '
        '"node_title": "FlashVSRNode"}}, "node_errors": {}}'
    )

    def test_missing_node_names_the_node_and_the_fix(self):
        reason = comfyui._rejection_reason(self.MISSING_NODE)
        self.assertIn("FlashVSRNode", reason)
        self.assertIn("not installed", reason)
        self.assertNotIn("\n", reason)

    def test_validation_error_keeps_comfy_message(self):
        body = '{"error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation"}}'
        self.assertEqual(
            comfyui._rejection_reason(body), "Prompt outputs failed validation")

    def test_unparseable_body_does_not_raise(self):
        self.assertTrue(comfyui._rejection_reason("<html>502 Bad Gateway</html>"))

    def test_queue_prompt_puts_the_reason_on_the_first_line(self):
        import io
        import urllib.error

        err = urllib.error.HTTPError(
            "http://s2:8188/prompt", 400, "Bad Request", {},
            io.BytesIO(self.MISSING_NODE.encode()))
        with mock.patch.object(comfyui.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                comfyui._queue_prompt({}, "cid", comfy_url="http://s2:8188")
        first_line = str(ctx.exception).splitlines()[0]
        self.assertIn("FlashVSRNode", first_line)
        self.assertIn("http://s2:8188", first_line)
        # The raw body still follows, for the log.
        self.assertIn("missing_node_type", str(ctx.exception))
