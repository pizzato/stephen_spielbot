import importlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline.assembler as assembler


def _fake_clock(video_path, _source_path, output_path, _duration):
    """Stand-in for _restore_source_clock: hand the clip through untouched."""
    output_path.write_bytes(video_path.read_bytes())
    return output_path


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

    def test_temporal_chunks_keep_their_audio(self):
        """Every packaged upscale workflow feeds VHS's audio into VideoCombine,
        and VHS fails the prompt outright when the input has no audio track."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "chunk.mp4"
            src.write_bytes(b"video")
            seen = {}

            def fake_run(cmd, **kw):
                seen["cmd"] = cmd

            with mock.patch.object(assembler, "_run", side_effect=fake_run):
                assembler._extract_temporal_chunk(src, out, 0.0, 4.0)

            cmd = seen["cmd"]
            self.assertNotIn("-an", cmd, "chunks must not be stripped of audio")
            self.assertIn("0:a?", cmd)

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
                 mock.patch.object(assembler, "_constant_rate_source", side_effect=lambda inp, _out: (inp, 24.0)), \
                 mock.patch.object(assembler, "_verify_upscale_not_blank", side_effect=fake_verify), \
                 mock.patch.object(assembler, "_restore_source_clock", side_effect=_fake_clock), \
                 mock.patch.object(assembler, "_chunked_comfy_temporal_upscale", side_effect=fake_chunked), \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", side_effect=fake_single), \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                assembler.temporal_ai_upscale_video(
                    src, out, 2160, 2160, comfy_url="http://w:8188", chunk_seconds=12.0,
                )

            # whole clip first, then recovery chunks at min(configured, duration/2)
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
                 mock.patch.object(assembler, "_constant_rate_source", side_effect=lambda inp, _out: (inp, 24.0)), \
                 mock.patch.object(assembler, "_verify_upscale_not_blank",
                                   side_effect=RuntimeError("came back blank")), \
                 mock.patch.object(assembler, "_restore_source_clock", side_effect=_fake_clock), \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", return_value=out), \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                with self.assertRaises(RuntimeError):
                    assembler.temporal_ai_upscale_video(
                        src, out, 2160, 2160, comfy_url="http://w:8188", chunk_seconds=2.0,
                    )

    def test_long_clip_is_never_split_up_front(self):
        """Chunking is recovery only: a clip well past the chunk length still goes
        through whole, because joining separate pieces breaks continuity."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            out.write_bytes(b"upscaled")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(1024, 1024)), \
                 mock.patch.object(assembler, "_get_duration", return_value=40.0), \
                 mock.patch.object(assembler, "_constant_rate_source", side_effect=lambda inp, _out: (inp, 24.0)), \
                 mock.patch.object(assembler, "_verify_upscale_not_blank"), \
                 mock.patch.object(assembler, "_restore_source_clock", side_effect=_fake_clock), \
                 mock.patch.object(assembler, "_chunked_comfy_temporal_upscale",
                                   return_value=out) as chunked, \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", return_value=out) as single, \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                assembler.temporal_ai_upscale_video(
                    src, out, 2160, 2160, comfy_url="http://w:8188", chunk_seconds=4.0,
                )

            # 40s clip, 4s recovery chunk — old behaviour would have split it into ten
            chunked.assert_not_called()
            single.assert_called_once()

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
                 mock.patch.object(assembler, "_constant_rate_source", side_effect=lambda inp, _out: (inp, 25.0)), \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", return_value=out) as comfy_upscale, \
                 mock.patch.object(assembler, "_restore_source_clock", side_effect=_fake_clock), \
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
                 mock.patch.object(assembler, "_constant_rate_source", side_effect=lambda inp, _out: (inp, 25.0)), \
                 mock.patch("pipeline.comfyui.upscale_video_ltx_latent", return_value=out) as latent, \
                 mock.patch.object(assembler, "_restore_source_clock", side_effect=_fake_clock), \
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
                 mock.patch.object(assembler, "_constant_rate_source", side_effect=lambda inp, _out: (inp, 25.0)), \
                 mock.patch.object(assembler, "_chunked_comfy_temporal_upscale",
                                   return_value=out) as chunked, \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", return_value=out) as whole, \
                 mock.patch.object(assembler, "_restore_source_clock", side_effect=_fake_clock), \
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

    def test_long_scene_still_goes_through_whole(self):
        """A 20s scene is what came back solid black on a GB10, but splitting it
        up front breaks continuity at the seams — it is tried whole and only
        split if that actually fails."""
        chunked, whole = self._route_upscale(20.2)
        self.assertFalse(chunked)
        self.assertTrue(whole)

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
                 mock.patch.object(assembler, "_constant_rate_source", side_effect=lambda inp, _out: (inp, 25.0)), \
                 mock.patch.object(assembler, "_extract_temporal_chunk", side_effect=fake_extract) as extract, \
                 mock.patch.object(assembler, "_xfade_video_chunks", side_effect=fake_xfade) as xfade, \
                 mock.patch.object(assembler, "_concat_video_chunks") as concat, \
                 mock.patch.object(assembler, "_mux_source_audio", side_effect=fake_mux) as mux, \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", side_effect=lambda _src, dst, *_args, **_kw: dst) as comfy_upscale, \
                 mock.patch.object(assembler, "_verify_upscale_not_blank",
                                   side_effect=[RuntimeError("came back blank"), None]), \
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
            # one whole-clip attempt (which blanked) plus the three recovery chunks
            self.assertEqual(comfy_upscale.call_count, 4)
            # Non-final chunks extend by the overlap so xfade can blend the same
            # source-time region; final chunk is the remainder only.
            self.assertEqual(extract_calls[0], (0.0, 4.5))
            self.assertEqual(extract_calls[1], (4.0, 4.5))
            self.assertEqual(extract_calls[2], (8.0, 1.0))
            xfade.assert_called_once()
            concat.assert_not_called()
            # once after the (blank) whole attempt, once over the chunked join
            self.assertEqual(mux.call_count, 2)

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
                 mock.patch.object(assembler, "_constant_rate_source", side_effect=lambda inp, _out: (inp, 25.0)), \
                 mock.patch.object(assembler, "_extract_temporal_chunk") as extract, \
                 mock.patch.object(assembler, "_concat_video_chunks", side_effect=fake_concat) as concat, \
                 mock.patch.object(assembler, "_xfade_video_chunks") as xfade, \
                 mock.patch.object(assembler, "_mux_source_audio", side_effect=fake_mux), \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", side_effect=lambda _src, dst, *_args, **_kw: dst), \
                 mock.patch.object(assembler, "_verify_upscale_not_blank",
                                   side_effect=[RuntimeError("came back blank"), None]), \
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


class TimelineSafetyTests(unittest.TestCase):
    """Frame timing through the joins and the AI upscale.

    A stream-copy join that shifted PTS and DTS by different amounts broke the
    B-frame order of every chained H3 scene, the next mux dropped a quarter of
    the frames, and an upscale written at the damaged clip's average rate ran
    every scene a third long before a mixed-timebase stream copy crushed whole
    scenes into a few milliseconds."""

    def test_stream_copy_join_moves_pts_and_dts_alike(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, out = Path(tmp) / "a.mp4", Path(tmp) / "b.mp4", Path(tmp) / "out.mp4"
            a.write_bytes(b"a")
            b.write_bytes(b"b")
            seen = {}
            with mock.patch.object(assembler, "_run", side_effect=lambda cmd, **kw: seen.setdefault("cmd", cmd)):
                assembler._concat_video_chunks([a, b], out)
            cmd = seen["cmd"]
            self.assertIn("-bsf:v", cmd)
            self.assertEqual(cmd[cmd.index("-bsf:v") + 1], "setts=pts=PTS-STARTPTS:dts=DTS-STARTPTS")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_stream_copy_join_keeps_every_frame_in_step(self):
        """Two B-frame clips joined, then re-muxed the way a scene is: every
        frame must survive, evenly spaced, starting at zero."""
        with tempfile.TemporaryDirectory() as tmp:
            clips = []
            for i in range(2):
                clip = Path(tmp) / f"clip{i}.mp4"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    # Real B-frames (x264 otherwise chooses none for a test
                    # pattern), so the first DTS sits two frames before the
                    # first PTS exactly as in a rendered clip.
                    "-c:v", "libx264", "-x264-params", "bframes=3:b-adapt=0",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(clip),
                ], check=True)
                clips.append(clip)
            joined = Path(tmp) / "joined.mp4"
            assembler._concat_video_chunks(clips, joined)

            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "packet=pts", "-of", "csv=p=0", str(joined),
            ], capture_output=True, text=True, check=True)
            pts = sorted(int(x) for x in probe.stdout.split() if x.strip())
            self.assertEqual(len(pts), 48)
            self.assertEqual(pts[0], 0)
            steps = {b - a for a, b in zip(pts, pts[1:])}
            self.assertEqual(len(steps), 1, f"uneven frame spacing: {sorted(steps)[:5]}")

            remux = subprocess.run([
                "ffmpeg", "-v", "info", "-y", "-i", str(joined),
                "-c:v", "libx264", "-an", str(Path(tmp) / "remux.mp4"),
            ], capture_output=True, text=True, check=True)
            self.assertNotIn("drop=", remux.stderr.replace("drop=0", ""))

    def test_constant_rate_clip_is_handed_over_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "src.mp4", Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            with mock.patch.object(assembler, "_video_frame_rates", return_value=(24.0, 24.0)), \
                 mock.patch.object(assembler, "_run") as run:
                self.assertEqual(assembler._constant_rate_source(src, out), (src, 24.0))
            run.assert_not_called()

    def test_uneven_clip_is_re_timed_at_its_nominal_rate(self):
        """A scene that lost frames reads as 18 fps on average while its frames
        sit on a 24 fps grid; the upscaler must be told 24, or it stretches."""
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "src.mp4", Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            seen = {}
            with mock.patch.object(assembler, "_video_frame_rates", return_value=(24.0, 18.0)), \
                 mock.patch.object(assembler, "_run", side_effect=lambda cmd, **kw: seen.setdefault("cmd", cmd)):
                cfr, rate = assembler._constant_rate_source(src, out)
            self.assertEqual(rate, 24.0)
            self.assertEqual(cfr, Path(tmp) / "out.cfr.mp4")
            self.assertIn("fps=24.0", seen["cmd"])

    def test_restore_source_clock_conforms_only_when_the_length_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            video, src, out = Path(tmp) / "v.mp4", Path(tmp) / "s.mp4", Path(tmp) / "o.mp4"
            for p in (video, src):
                p.write_bytes(b"x")
            with mock.patch.object(assembler, "_mux_source_audio",
                                   side_effect=lambda v, s, o, target_duration=None: o.write_bytes(b"muxed") or o), \
                 mock.patch.object(assembler, "_get_video_stream_duration", return_value=3.0), \
                 mock.patch.object(assembler, "conform_video_to_source") as conform:
                assembler._restore_source_clock(video, src, out, 3.0)
            conform.assert_not_called()

            def fake_conform(v, s, o, duration):
                o.write_bytes(b"conformed")
                return o

            with mock.patch.object(assembler, "_mux_source_audio",
                                   side_effect=lambda v, s, o, target_duration=None: o.write_bytes(b"muxed") or o), \
                 mock.patch.object(assembler, "_get_video_stream_duration", return_value=3.4), \
                 mock.patch.object(assembler, "conform_video_to_source", side_effect=fake_conform) as conform:
                assembler._restore_source_clock(video, src, out, 3.0)
            conform.assert_called_once()
            self.assertEqual(conform.call_args.args[3], 3.0)
            self.assertEqual(out.read_bytes(), b"conformed")

    def test_restore_source_clock_reads_the_video_stream_not_the_container(self):
        """Muxing full-length audio onto a frames-short picture makes the
        container's duration read on-target; the picture must still be
        conformed or every scene ends a beat early and the film drifts."""
        with tempfile.TemporaryDirectory() as tmp:
            video, src, out = Path(tmp) / "v.mp4", Path(tmp) / "s.mp4", Path(tmp) / "o.mp4"
            for p in (video, src):
                p.write_bytes(b"x")

            def fake_conform(v, s, o, duration):
                o.write_bytes(b"conformed")
                return o

            with mock.patch.object(assembler, "_mux_source_audio",
                                   side_effect=lambda v, s, o, target_duration=None: o.write_bytes(b"muxed") or o), \
                 mock.patch.object(assembler, "_get_duration", return_value=5.083), \
                 mock.patch.object(assembler, "_get_video_stream_duration", return_value=4.917), \
                 mock.patch.object(assembler, "conform_video_to_source", side_effect=fake_conform) as conform:
                assembler._restore_source_clock(video, src, out, 5.083)
            conform.assert_called_once()
            self.assertEqual(out.read_bytes(), b"conformed")

    def test_video_stream_duration_falls_back_to_the_container(self):
        """A container with no per-stream duration (e.g. mkv) still measures."""
        probe = mock.Mock(stdout="N/A\n")
        with mock.patch.object(assembler.subprocess, "run", return_value=probe), \
             mock.patch.object(assembler, "_get_duration", return_value=4.2) as fallback:
            self.assertEqual(assembler._get_video_stream_duration(Path("x.mkv")), 4.2)
        fallback.assert_called_once()

    def test_whole_clip_result_goes_back_on_the_source_clock(self):
        """Only the chunked path used to do this; the whole clip came back with
        ComfyUI's frame count and padded audio and was joined as it was."""
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "src.mp4", Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            out.write_bytes(b"upscaled")
            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(512, 288)), \
                 mock.patch.object(assembler, "_get_duration", return_value=3.0), \
                 mock.patch.object(assembler, "_constant_rate_source", side_effect=lambda inp, _out: (inp, 24.0)), \
                 mock.patch.object(assembler, "_verify_upscale_not_blank"), \
                 mock.patch.object(assembler, "_restore_source_clock", side_effect=_fake_clock) as clock, \
                 mock.patch("pipeline.comfyui.upscale_video_ltx", return_value=out), \
                 mock.patch.dict("os.environ", {"TEMPORAL_VIDEO_UPSCALER_CMD": ""}, clear=False):
                assembler.temporal_ai_upscale_video(src, out, 1920, 1080, comfy_url="http://w:8188")
            clock.assert_called_once()
            video, source, staged, duration = clock.call_args.args
            self.assertEqual((video, source, duration), (out, src, 3.0))
            self.assertEqual(staged, Path(tmp) / "out.clock.mp4")
            self.assertFalse(staged.exists())


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
