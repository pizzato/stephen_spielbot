"""Tests for the issue #88 regenerate endpoints: comment reply drafting, Create
brief improvement, and YouTube title regeneration. The LLM call is mocked so the
tests assert prompt wiring and response shaping, not model output."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend

# Work dirs must live under OUTPUT_DIR (endpoints reject paths outside it).
_OUT = Path(tempfile.mkdtemp(prefix="spielbot-test-out-"))


class DraftReplyTests(unittest.TestCase):
    def test_draft_reply_uses_channel_guidance_and_returns_reply(self):
        comment = {"comment_id": "c1", "channel": "chanA", "text": "Love this!",
                   "commenter": "Sam", "replies": []}
        cfg = {"youtube_channels": [{"id": "chanA", "engagement_prompt": "Be playful."}]}
        with mock.patch.object(backend.yt, "load_comments_cache", return_value=[comment]), \
             mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend, "_llm_complete", return_value='"Thanks Sam!"') as llm:
            result = backend.youtube_draft_reply(backend.CommentActionBody(comment_id="c1"))

        # The surrounding quotes from the model are stripped.
        self.assertEqual(result, {"reply": "Thanks Sam!"})
        user_prompt = llm.call_args.args[1]
        self.assertIn("Be playful.", user_prompt)   # channel voice fed in
        self.assertIn("Love this!", user_prompt)    # the comment text fed in

    def test_draft_reply_missing_comment_is_404(self):
        with mock.patch.object(backend.yt, "load_comments_cache", return_value=[]):
            with self.assertRaises(backend.HTTPException) as ctx:
                backend.youtube_draft_reply(backend.CommentActionBody(comment_id="nope"))
        self.assertEqual(ctx.exception.status_code, 404)


class CreateImproveTests(unittest.TestCase):
    def test_improve_title_returns_value_with_current_text_in_prompt(self):
        with mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend, "_llm_complete", return_value="A Sharper Title") as llm:
            result = backend.create_improve(
                backend.BriefImproveBody(field="title", title="old title", direction="angle"))
        self.assertEqual(result, {"value": "A Sharper Title"})
        self.assertIn("old title", llm.call_args.args[1])

    def test_improve_direction_returns_value(self):
        with mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend, "_llm_complete", return_value="Sharper direction."):
            result = backend.create_improve(
                backend.BriefImproveBody(field="direction", title="t", direction="vague"))
        self.assertEqual(result, {"value": "Sharper direction."})

    def test_improve_rejects_unknown_field(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.create_improve(backend.BriefImproveBody(field="bogus"))
        self.assertEqual(ctx.exception.status_code, 400)


class PostTitleTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)

    def test_post_title_caps_at_100_chars(self):
        wd = tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT)
        with mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend.gapp, "_load_scenes_for_work_dir", return_value=[]), \
             mock.patch.object(backend.gapp, "style_settings", return_value={"title_style": ""}), \
             mock.patch.object(backend, "_work_dir_style_name", return_value=""), \
             mock.patch.object(backend, "_video_title_for", return_value="current"), \
             mock.patch.object(backend, "_llm_complete", return_value="x" * 250):
            result = backend.yt_post_title(backend.DescribeBody(work_dir=wd, title="current"))
        self.assertLessEqual(len(result["title"]), 100)

    def test_post_title_missing_film_is_404(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.yt_post_title(backend.DescribeBody(work_dir=str(_OUT / "no-such-film-dir"), title="t"))
        self.assertEqual(ctx.exception.status_code, 404)


class FilmUpscaleTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        backend._film_tasks.clear()
        backend._film_task_meta.clear()
        backend._film_cancelled_tids.clear()
        backend._activity_log.clear()
        backend._current_ops.clear()

    def test_final_video_upscale_replaces_final_and_records_version(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        final = wd.with_suffix(".mp4")
        final.write_bytes(b"low-res-final")

        def fake_upscale(src, out, width, height):
            self.assertEqual(src, final)
            self.assertEqual((width, height), (1920, 1080))
            out.write_bytes(b"upscaled-final")
            return out

        with mock.patch.object(backend.gapp, "_RESOLUTIONS", {"Landscape FHD (1920×1080)": (1920, 1080)}), \
             mock.patch("pipeline.assembler._get_video_dimensions", return_value=(512, 288)), \
             mock.patch("pipeline.assembler.upscale_video", side_effect=fake_upscale):
            backend._film_tasks["tid"] = {"status": "running", "step": "final_upscale"}
            backend._run_final_video_upscale("tid", wd, "Landscape FHD (1920×1080)", "fast")

        self.assertEqual(final.read_bytes(), b"upscaled-final")
        self.assertEqual(backend._film_tasks["tid"]["status"], "done")
        history = backend.final_video_history.history(wd)
        self.assertEqual(len(history["versions"]), 2)
        self.assertEqual(history["selected"], 2)

    def test_final_video_upscale_temporal_mode_uses_ai_upscaler(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        final = wd.with_suffix(".mp4")
        final.write_bytes(b"low-res-final")
        scene = wd / "scene_01_final.mp4"
        scene.write_bytes(b"scene-one" * 1500)

        def fake_temporal(src, out, width, height, command_template=None, timeout_seconds=None, comfy_url=None, engine="ic_lora", chunk_seconds=None):
            self.assertEqual(src, scene)
            self.assertEqual((width, height), (1920, 1080))
            self.assertEqual(command_template, "temporal-cli -i {input} -o {output}")
            self.assertEqual(timeout_seconds, 1234)
            self.assertIsNone(comfy_url)
            self.assertEqual(engine, "ic_lora")
            out.write_bytes(b"temporal-upscaled-scene")
            return out

        def fake_concat(paths, out, fade=0.3, **kw):
            self.assertEqual([p.name for p in paths], ["scene_01_final.upscaled.mp4"])
            out.write_bytes(b"temporal-upscaled-final")
            return out

        with mock.patch.object(backend.gapp, "_RESOLUTIONS", {"Landscape FHD (1920×1080)": (1920, 1080)}), \
             mock.patch.object(backend.gapp, "load_config", return_value={
                 "temporal_video_upscaler_cmd": "temporal-cli -i {input} -o {output}",
                 "temporal_video_upscaler_timeout": 1234,
             }), \
             mock.patch("pipeline.assembler._get_video_dimensions", return_value=(512, 288)), \
             mock.patch("pipeline.assembler.temporal_ai_upscale_video", side_effect=fake_temporal) as temporal, \
             mock.patch("pipeline.assembler.concatenate_scenes", side_effect=fake_concat), \
             mock.patch("pipeline.assembler.upscale_video") as fast:
            backend._film_tasks["tid"] = {"status": "running", "step": "final_upscale"}
            backend._run_final_video_upscale("tid", wd, "Landscape FHD (1920×1080)", "temporal_ai")

        self.assertEqual(final.read_bytes(), b"temporal-upscaled-final")
        self.assertEqual(backend._film_tasks["tid"]["status"], "done")
        temporal.assert_called_once()
        self.assertEqual(temporal.call_args.kwargs.get("engine"), "ic_lora")
        fast.assert_not_called()

    def test_final_video_upscale_ltx_latent_mode(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        final = wd.with_suffix(".mp4")
        final.write_bytes(b"low-res-final")
        scene = wd / "scene_01_final.mp4"
        scene.write_bytes(b"scene-one" * 1500)

        def fake_temporal(src, out, width, height, command_template=None, timeout_seconds=None, comfy_url=None, engine="ic_lora", chunk_seconds=None):
            self.assertEqual(engine, "ltx_latent_2x")
            self.assertEqual(src, scene)
            # A 2x mode finishes at twice the film, not at the FHD target that
            # was passed in — the upsampler cannot reach an arbitrary size.
            self.assertEqual((width, height), (1024, 576))
            out.write_bytes(b"latent-upscaled-scene")
            return out

        def fake_concat(paths, out, fade=0.3, **kw):
            out.write_bytes(b"latent-upscaled-final")
            return out

        with mock.patch.object(backend.gapp, "_RESOLUTIONS", {"Landscape FHD (1920×1080)": (1920, 1080)}), \
             mock.patch.object(backend.gapp, "load_config", return_value={
                 "comfy_workers": ["http://w1:8188"],
                 "temporal_video_upscaler_timeout": 60,
             }), \
             mock.patch("pipeline.worker_pool.alive_workers", return_value=["http://w1:8188"]), \
             mock.patch("pipeline.assembler._get_video_dimensions", return_value=(512, 288)), \
             mock.patch("pipeline.assembler.temporal_ai_upscale_video", side_effect=fake_temporal) as temporal, \
             mock.patch("pipeline.assembler.concatenate_scenes", side_effect=fake_concat), \
             mock.patch("pipeline.assembler.upscale_video") as fast:
            backend._film_tasks["tid"] = {"status": "running", "step": "final_upscale"}
            backend._run_final_video_upscale("tid", wd, "Landscape FHD (1920×1080)", "ltx_latent")

        self.assertEqual(final.read_bytes(), b"latent-upscaled-final")
        self.assertEqual(backend._film_tasks["tid"]["status"], "done")
        temporal.assert_called_once()
        fast.assert_not_called()

    def test_packaged_temporal_final_upscale_runs_by_scene(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        final = wd.with_suffix(".mp4")
        final.write_bytes(b"low-res-final")
        scene_1 = wd / "scene_01_final.mp4"
        scene_2 = wd / "scene_02_final.mp4"
        scene_1.write_bytes(b"scene-one" * 1500)
        scene_2.write_bytes(b"scene-two" * 1500)
        (wd / "background_music.wav").write_bytes(b"music")

        calls = []
        mixed = []

        def fake_temporal(src, out, width, height, command_template=None, timeout_seconds=None, comfy_url=None, engine="ic_lora", chunk_seconds=None):
            self.assertNotEqual(src, final)
            self.assertEqual((width, height), (1920, 1080))
            self.assertIsNone(command_template)
            self.assertEqual(timeout_seconds, 1234)
            self.assertEqual(engine, "ic_lora")
            calls.append((src, comfy_url))
            time.sleep(0.05)
            out.write_bytes(f"upscaled:{src.name}".encode())
            return out

        def fake_concat(paths, out, fade=0.3, **kw):
            self.assertEqual([p.name for p in paths], ["scene_01_final.upscaled.mp4", "scene_02_final.upscaled.mp4"])
            # the film's own join, with its dip-to-black between scenes (the
            # default fade, as the render uses) — never the stream copy
            self.assertEqual(fade, 0.3)
            out.write_bytes(b"combined-scenes")
            return out

        def fake_mix(video_path, music_path, output_path, volume=0.0, voice_volume=1.0, ambient_path=None, ambient_volume=0.0):
            mixed.append((video_path.read_bytes(), music_path, volume, voice_volume, ambient_path, ambient_volume))
            output_path.write_bytes(b"scene-temporal-final")
            return output_path

        with mock.patch.object(backend.gapp, "_RESOLUTIONS", {"Landscape FHD (1920×1080)": (1920, 1080)}), \
             mock.patch.object(backend.gapp, "load_config", return_value={
                 "comfy_workers": ["http://w1:8188", "http://w2:8188"],
                 "temporal_video_upscaler_timeout": 1234,
                 "music_vol": 5,
                 "voice_vol": 200,
                 "ambient_vol": 1,
             }), \
             mock.patch("pipeline.worker_pool.alive_workers", return_value=["http://w1:8188", "http://w2:8188"]), \
             mock.patch("pipeline.assembler._get_video_dimensions", return_value=(512, 288)), \
             mock.patch("pipeline.assembler.temporal_ai_upscale_video", side_effect=fake_temporal), \
             mock.patch("pipeline.assembler.concatenate_scenes", side_effect=fake_concat), \
             mock.patch("pipeline.assembler.mix_background_music", side_effect=fake_mix):
            backend._film_tasks["tid"] = {"status": "running", "step": "final_upscale"}
            backend._run_final_video_upscale("tid", wd, "Landscape FHD (1920×1080)", "temporal_ai")

        self.assertEqual(final.read_bytes(), b"scene-temporal-final")
        self.assertEqual(backend._film_tasks["tid"]["status"], "done")
        self.assertEqual({src for src, _ in calls}, {scene_1, scene_2})
        self.assertEqual({url for _, url in calls}, {"http://w1:8188", "http://w2:8188"})
        self.assertEqual(mixed[0], (b"combined-scenes", wd / "background_music.wav", 0.05, 2.0, None, 0.01))
        history = backend.final_video_history.history(wd)
        self.assertEqual(len(history["versions"]), 2)
        self.assertEqual(history["selected"], 2)
        # Hours of GPU work stay on disk for a rebuild; only the joined copy goes.
        kept = wd / "final_upscale_scenes" / "ic_lora-1920x1080"
        self.assertEqual(sorted(p.name for p in kept.iterdir()),
                         ["scene_01_final.upscaled.mp4", "scene_02_final.upscaled.mp4"])

    def test_cached_scene_upscale_is_reused_only_when_fresher_than_the_scene(self):
        """A scene re-shot to the same length must not pass off its old upscale."""
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        wd.with_suffix(".mp4").write_bytes(b"low-res-final")
        scene = wd / "scene_01_final.mp4"
        scene.write_bytes(b"scene-one" * 1500)
        cached = wd / "final_upscale_scenes" / "ic_lora-1920x1080" / "scene_01_final.upscaled.mp4"
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"cached-upscale" * 1000)

        def run(cached_is_fresher: bool) -> list:
            stamp = scene.stat().st_mtime + (60 if cached_is_fresher else -60)
            os.utime(cached, (stamp, stamp))
            calls = []

            def fake_temporal(src, out, *_a, **_kw):
                calls.append(src)
                out.write_bytes(b"fresh-upscale" * 1000)
                return out

            with mock.patch.object(backend.gapp, "_RESOLUTIONS", {"Landscape FHD (1920×1080)": (1920, 1080)}), \
                 mock.patch.object(backend.gapp, "load_config", return_value={"comfy_workers": ["http://w1:8188"]}), \
                 mock.patch("pipeline.worker_pool.alive_workers", return_value=["http://w1:8188"]), \
                 mock.patch("pipeline.assembler._get_video_dimensions",
                            side_effect=lambda p: (1920, 1080) if "upscaled" in p.name else (512, 288)), \
                 mock.patch("pipeline.assembler._get_duration", return_value=3.0), \
                 mock.patch("pipeline.assembler._verify_upscale_not_blank"), \
                 mock.patch("pipeline.assembler.temporal_ai_upscale_video", side_effect=fake_temporal), \
                 mock.patch("pipeline.assembler.concatenate_scenes",
                            side_effect=lambda paths, out, fade=0.3, **kw: out.write_bytes(b"joined") or out):
                backend._film_tasks["tid"] = {"status": "running", "step": "final_upscale"}
                backend._run_final_video_upscale("tid", wd, "Landscape FHD (1920×1080)", "ic_lora")
            self.assertEqual(backend._film_tasks["tid"]["status"], "done")
            return calls

        self.assertEqual(run(cached_is_fresher=True), [])
        self.assertEqual(run(cached_is_fresher=False), [scene])

    def test_factor_upscale_sizes_off_rendered_film_not_selected_upscale(self):
        """Re-running FlashVSR 2× while its own 2× output is the selected final
        must target rendered×2 (1408), not selected×2 (2816) — compounding would
        stretch the scene finals to the selected size and miss the scene cache."""
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        final = wd.with_suffix(".mp4")
        # The rendered 704x704 cut is the base version in the history...
        final.write_bytes(b"rendered-704" * 1000)
        backend.final_video_history.record(wd, final, label="Original")
        # ...and the published final is a selected FlashVSR 2x (1408x1408) pass.
        final.write_bytes(b"flashvsr-1408" * 1000)
        backend.final_video_history.record(
            wd, final, label="FlashVSR 2× 1408x1408", kind="upscale")
        scene = wd / "scene_01_final.mp4"
        scene.write_bytes(b"scene-one" * 1500)

        def fake_dims(p):
            return (1408, 1408) if b"1408" in Path(p).read_bytes() else (704, 704)

        calls = []

        def fake_temporal(src, out, width, height, **kw):
            calls.append((src, width, height))
            out.write_bytes(b"re-upscaled-scene" * 1000)
            return out

        with mock.patch.object(backend.gapp, "load_config", return_value={"comfy_workers": ["http://w1:8188"]}), \
             mock.patch("pipeline.worker_pool.alive_workers", return_value=["http://w1:8188"]), \
             mock.patch("pipeline.assembler._get_video_dimensions", side_effect=fake_dims), \
             mock.patch("pipeline.assembler.temporal_ai_upscale_video", side_effect=fake_temporal), \
             mock.patch("pipeline.assembler.concatenate_scenes",
                        side_effect=lambda paths, out, fade=0.3, **kw: out.write_bytes(b"joined-1408") or out):
            backend._film_tasks["tid"] = {"status": "running", "step": "final_upscale"}
            backend._run_final_video_upscale("tid", wd, "", "flashvsr_2x")

        self.assertEqual(backend._film_tasks["tid"]["status"], "done")
        # Sized off the rendered film (704×2), fed the raw scene — no conform pass.
        self.assertEqual(calls, [(scene, 1408, 1408)])
        # Cached under the rendered-size key a later re-run can find again.
        self.assertTrue((wd / "final_upscale_scenes" / "flashvsr_2x-1408x1408").is_dir())
        history = backend.final_video_history.history(wd)
        self.assertEqual(history["versions"][-1]["label"], "FlashVSR 2× 1408x1408")

    def test_remix_upscale_endpoint_accepts_target_and_mode(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        wd.with_suffix(".mp4").write_bytes(b"low-res-final")

        started = []

        def fake_thread(target, args, daemon):
            started.append(args)
            return mock.Mock(start=lambda: None)

        with mock.patch.object(backend.gapp, "_RESOLUTIONS", {"Landscape FHD (1920×1080)": (1920, 1080)}), \
             mock.patch.object(backend.threading, "Thread", side_effect=fake_thread):
            result = backend.remix_upscale_video(
                backend.RemixUpscaleBody(
                    work_dir=str(wd),
                    target_resolution="Landscape FHD (1920×1080)",
                    upscale_mode="temporal_ai",
                ),
            )

        self.assertTrue(result["task_id"].startswith("final_upscale_"))
        self.assertEqual(started[0][1], wd)
        self.assertEqual(started[0][2], "Landscape FHD (1920×1080)")
        # temporal_ai is normalized to ic_lora before the worker thread starts.
        self.assertEqual(started[0][3], "ic_lora")

    def test_remix_upscale_endpoint_accepts_ltx_latent_mode(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        wd.with_suffix(".mp4").write_bytes(b"low-res-final")
        started = []

        def fake_thread(target, args, daemon):
            started.append(args)
            return mock.Mock(start=lambda: None)

        with mock.patch.object(backend.gapp, "_RESOLUTIONS", {"Landscape FHD (1920×1080)": (1920, 1080)}), \
             mock.patch.object(backend.threading, "Thread", side_effect=fake_thread):
            backend.remix_upscale_video(
                backend.RemixUpscaleBody(
                    work_dir=str(wd),
                    target_resolution="Landscape FHD (1920×1080)",
                    upscale_mode="ltx_latent",
                ),
            )
        # The pre-factor spelling resolves to the 2x mode — the only factor the
        # LTX latent upsampler has ever produced.
        self.assertEqual(started[0][3], "ltx_latent_2x")

    def test_remix_upscale_endpoint_accepts_factor_mode_without_resolution(self):
        """A factor mode sizes itself off the film, so no target is sent."""
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        wd.with_suffix(".mp4").write_bytes(b"low-res-final")
        started = []

        def fake_thread(target, args, daemon):
            started.append(args)
            return mock.Mock(start=lambda: None)

        with mock.patch.object(backend.threading, "Thread", side_effect=fake_thread):
            backend.remix_upscale_video(
                backend.RemixUpscaleBody(
                    work_dir=str(wd),
                    target_resolution="",
                    upscale_mode="flashvsr_2x",
                ),
            )
        self.assertEqual(started[0][2], "")
        self.assertEqual(started[0][3], "flashvsr_2x")

    def test_remix_upscale_endpoint_accepts_canonical_ic_lora_mode(self):
        """UI sends upscale_mode=ic_lora; normalizer must accept the canonical key."""
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        wd.with_suffix(".mp4").write_bytes(b"low-res-final")
        started = []

        def fake_thread(target, args, daemon):
            started.append(args)
            return mock.Mock(start=lambda: None)

        with mock.patch.object(backend.gapp, "_RESOLUTIONS", {"Landscape FHD (1920×1080)": (1920, 1080)}), \
             mock.patch.object(backend.threading, "Thread", side_effect=fake_thread):
            backend.remix_upscale_video(
                backend.RemixUpscaleBody(
                    work_dir=str(wd),
                    target_resolution="Landscape FHD (1920×1080)",
                    upscale_mode="ic_lora",
                ),
            )
        self.assertEqual(started[0][3], "ic_lora")

    def test_activity_surfaces_running_final_upscale(self):
        backend._film_tasks["final_upscale_123"] = {"status": "running", "step": "final_upscale"}
        backend._film_task_meta["final_upscale_123"] = {
            "work_dir": str(_OUT / "film-a"),
            "scene_id": 0,
            "component": "final_upscale",
            "started_at": 123.0,
        }

        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=[]):
            activity = backend.get_activity()

        self.assertEqual(activity["current_op"]["name"], "Upscaling final video")
        self.assertEqual(activity["active_ops"][0]["name"], "Upscaling final video")
        self.assertIn("upscaling final video", activity["active_ops"][0]["detail"])

    def test_activity_marks_waiting_rerender_as_queued(self):
        """A re-render blocked in _acquire_render_worker (queued=True) surfaces as
        status "queued" with no ETA countdown, not as a green "running" row."""
        backend._film_tasks["rerender_7_1"] = {"status": "running", "step": "video", "queued": True}
        backend._film_task_meta["rerender_7_1"] = {
            "work_dir": str(_OUT / "film-a"),
            "scene_id": 7,
            "component": "video",
            "started_at": 123.0,
        }

        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=[]):
            activity = backend.get_activity()

        op = activity["active_ops"][0]
        self.assertEqual(op["name"], "Re-rendering scene 7")
        self.assertEqual(op["status"], "queued")
        self.assertIn("waiting for a free worker", op["detail"])
        self.assertIsNone(op["eta_text"])
        # Queued work is still live: its group must sort with the live ones.
        group = next(g for g in activity["groups"] if any(i["id"] == op["id"] for i in g["items"]))
        self.assertEqual(group["live_count"], 1)

    def test_acquire_render_worker_flags_queue_wait(self):
        """The queued flag is set while blocked in pool.acquire() and cleared
        once a worker is handed out."""
        backend._film_tasks["tid"] = {"status": "running", "step": "video"}
        seen = {}

        class FakePool:
            def acquire(self):
                seen["queued_during_acquire"] = backend._film_tasks["tid"].get("queued")
                return "http://worker:8188"

        url = backend._acquire_render_worker(FakePool(), "tid")
        self.assertEqual(url, "http://worker:8188")
        self.assertTrue(seen["queued_during_acquire"])
        self.assertNotIn("queued", backend._film_tasks["tid"])

    def test_acquire_op_worker_marks_tracked_op_queued(self):
        """A tracked op (image/cover edit) reads "queued" while it waits for a
        worker, and flips to "running" once it is on a GPU."""
        seen = {}

        class FakePool:
            def acquire(self):
                seen["row"] = dict(next(iter(backend._current_ops.values())))
                return "http://worker:8188"

        with backend._track_op("Editing image", "scene 3") as op_id:
            url = backend._acquire_op_worker(FakePool(), op_id)
            self.assertEqual(backend._current_ops[op_id]["status"], "running")
            self.assertEqual(backend._current_ops[op_id]["detail"], "scene 3")

        self.assertEqual(url, "http://worker:8188")
        self.assertEqual(seen["row"]["status"], "queued")
        self.assertEqual(seen["row"]["detail"], "waiting for a free worker · scene 3")

    def test_activity_refreshes_tracked_op_elapsed(self):
        """A _track_op row is built once at op start, so its stored elapsed_s is
        ~0 forever — /api/activity re-derives it, which is what the queue card's
        "queued 4m 18s" reads."""
        op_id = "op-elapsed"
        backend._current_ops[op_id] = backend._make_activity_event(
            name="Editing cover", detail="waiting for a free worker",
            started_at=time.time() - 90, status="queued",
            event_id=op_id, category="film",
        )
        try:
            with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
                 mock.patch.object(backend.yt, "load_queue", return_value=[]):
                activity = backend.get_activity()
        finally:
            backend._current_ops.pop(op_id, None)

        row = next(o for o in activity["live"] if o["id"] == op_id)
        self.assertGreaterEqual(row["elapsed_s"], 89)

    def test_track_op_keeps_concurrent_operations_visible(self):
        with backend._track_op("First task", "a"):
            with backend._track_op("Second task", "b"):
                with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
                     mock.patch.object(backend.yt, "load_queue", return_value=[]):
                    activity = backend.get_activity()

        names = {op["name"] for op in activity["active_ops"]}
        self.assertEqual(names, {"First task", "Second task"})

    def test_remix_video_select_restores_chosen_master(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        final = wd.with_suffix(".mp4")
        final.write_bytes(b"original")
        backend.final_video_history.record(wd, final, "Original")
        final.write_bytes(b"upscaled")
        h = backend.final_video_history.record(wd, final, "Upscaled")

        result = backend.select_remix_video(
            backend.RemixVideoSelectBody(work_dir=str(wd), version_id=h["versions"][0]["id"])
        )

        self.assertTrue(result["ok"])
        self.assertEqual(final.read_bytes(), b"original")
        self.assertEqual(result["video_history"]["selected"], h["versions"][0]["id"])


class InstructionSteeringTests(unittest.TestCase):
    """Guided re-generation: the optional free-text 'tell it how' instruction is
    threaded into the LLM/render prompt, and an empty instruction leaves the
    prompt byte-identical to the un-guided one."""

    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)

    def _store(self):
        store = mock.Mock()
        store.get_job.return_value = None
        store.scene_rows.return_value = []
        return store

    def test_instruction_note_empty_is_blank(self):
        self.assertEqual(backend._instruction_note(""), "")
        self.assertEqual(backend._instruction_note("   "), "")

    def test_instruction_note_includes_text_and_truncates(self):
        note = backend._instruction_note("make it all robots")
        self.assertIn("make it all robots", note)
        long = backend._instruction_note("x" * 900)
        self.assertLessEqual(long.count("x"), 500)

    def test_apply_prompt_instruction(self):
        self.assertEqual(backend.gapp._apply_prompt_instruction("a castle", ""), "a castle")
        self.assertEqual(backend.gapp._apply_prompt_instruction("a castle", "   "), "a castle")
        self.assertEqual(backend.gapp._apply_prompt_instruction("a castle", "all robots"), "a castle. all robots")
        self.assertEqual(backend.gapp._apply_prompt_instruction("", "all robots"), "all robots")

    def test_regenerate_field_threads_instruction(self):
        with mock.patch.object(backend.DurableStore, "default", return_value=self._store()), \
             mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend.gapp, "_save_active_scene"), \
             mock.patch.object(backend, "_llm_complete", return_value="Shorter narration.") as llm:
            result = backend.regenerate_field(
                "job1", 1, field="narration",
                body=backend.FieldRegenBody(narration="Long draft.", instruction="make it shorter"))
        self.assertEqual(result["value"], "Shorter narration.")
        self.assertIn("make it shorter", llm.call_args.args[1])

    def test_regenerate_field_empty_instruction_leaves_prompt_unchanged(self):
        prompts = []

        def cap(system, user, cfg, **kw):
            prompts.append(user)
            return "x"

        with mock.patch.object(backend.DurableStore, "default", return_value=self._store()), \
             mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend.gapp, "_save_active_scene"), \
             mock.patch.object(backend, "_llm_complete", side_effect=cap):
            backend.regenerate_field("job1", 1, field="narration",
                                     body=backend.FieldRegenBody(narration="Draft."))
            backend.regenerate_field("job1", 1, field="narration",
                                     body=backend.FieldRegenBody(narration="Draft.", instruction=""))
        self.assertEqual(prompts[0], prompts[1])
        self.assertNotIn("Additional instruction", prompts[0])

    def test_draft_reply_threads_instruction(self):
        comment = {"comment_id": "c1", "channel": "", "text": "Hi", "commenter": "Sam", "replies": []}
        with mock.patch.object(backend.yt, "load_comments_cache", return_value=[comment]), \
             mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend, "_llm_complete", return_value="reply") as llm:
            backend.youtube_draft_reply(backend.CommentActionBody(comment_id="c1", instruction="be funnier"))
        self.assertIn("be funnier", llm.call_args.args[1])

    def test_improve_threads_instruction(self):
        with mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend, "_llm_complete", return_value="t") as llm:
            backend.create_improve(backend.BriefImproveBody(field="title", title="old", instruction="make it shorter"))
        self.assertIn("make it shorter", llm.call_args.args[1])

    def test_post_title_threads_instruction(self):
        wd = tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT)
        with mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend.gapp, "_load_scenes_for_work_dir", return_value=[]), \
             mock.patch.object(backend.gapp, "style_settings", return_value={"title_style": ""}), \
             mock.patch.object(backend, "_work_dir_style_name", return_value=""), \
             mock.patch.object(backend, "_video_title_for", return_value="current"), \
             mock.patch.object(backend, "_llm_complete", return_value="New title") as llm:
            backend.yt_post_title(backend.DescribeBody(work_dir=wd, title="current", instruction="add a number"))
        self.assertIn("add a number", llm.call_args.args[1])

    def test_build_cover_prompt_leads_with_instruction(self):
        """The steer must OPEN the prompt, not trail it.

        Appended last it sat immediately after the "Avoid:" negative list, where
        the model reads it as one more thing to leave out — the reason typing
        into "tell it how" changed nothing."""
        from pipeline.cover import build_cover_prompt
        base = build_cover_prompt("cinematic")
        steered = build_cover_prompt("cinematic", instruction="make it all robots")
        self.assertNotIn("make it all robots", base)
        self.assertTrue(steered.startswith("make it all robots."))
        self.assertLess(steered.index("make it all robots"), steered.index("Avoid:"))
        # An un-steered cover is unchanged.
        self.assertEqual(base, build_cover_prompt("cinematic", instruction="   "))

    def test_build_cover_prompt_subordinates_scene_hint_to_instruction(self):
        """With a steer, the film's own imagery drops to optional reference —
        otherwise the concrete scene text beats a conflicting direction."""
        from pipeline.cover import build_cover_prompt
        scenes = [{"image_prompt": "A woman walking through a rainy street at night"}]
        base = build_cover_prompt("cinematic", scenes=scenes)
        steered = build_cover_prompt("cinematic", scenes=scenes,
                                     instruction="make it all robots")
        self.assertIn("Key visual elements from the video:", base)
        self.assertNotIn("Key visual elements from the video:", steered)
        self.assertIn("only where they fit that direction", steered)
        self.assertIn("A woman walking through a rainy street", steered)

    def test_rerender_film_scene_threads_instruction(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        started = []

        def fake_thread(target, args, daemon):
            started.append(args)
            return mock.Mock(start=lambda: None)

        store = mock.Mock()
        store.scene_rows.return_value = [{"id": 1, "image_prompt": "p", "video_prompt": "v"}]
        with mock.patch.object(backend.DurableStore, "default", return_value=store), \
             mock.patch.object(backend, "job_id_from_work_dir", return_value="job1"), \
             mock.patch.object(backend, "_film_job_config", return_value={}), \
             mock.patch.object(backend.image_history, "capture_current"), \
             mock.patch.object(backend, "_RERENDER_JOURNAL_PATH", wd / "rerender_journal.json"), \
             mock.patch.object(backend.threading, "Thread", side_effect=fake_thread):
            result = backend.rerender_film_scene(
                1, backend.RerenderSceneBody(work_dir=str(wd), component="image",
                                             instruction="make it all robots"))
        self.assertTrue(result["ok"])
        # args = (target, tid, wd, sid, component, jc, row, instruction)
        self.assertEqual(started[0][-1], "make it all robots")


class RerenderJournalTests(unittest.TestCase):
    """Scene re-renders survive a backend restart like a full render does:
    dispatch journals the intent to disk, reaching a terminal state clears it,
    and startup requeues whatever a dead process left behind."""

    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        journal_dir = Path(tempfile.mkdtemp(prefix="spielbot-journal-", dir=_OUT))
        j = mock.patch.object(backend, "_RERENDER_JOURNAL_PATH",
                              journal_dir / "rerender_journal.json")
        j.start()
        self.addCleanup(j.stop)
        backend._film_tasks.clear()
        backend._film_task_meta.clear()
        backend._film_cancelled_tids.clear()

    def test_dispatch_journals_intent_and_terminal_state_clears_it(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        store = mock.Mock()
        store.scene_rows.return_value = [{"id": 2, "image_prompt": "p", "video_prompt": "v"}]

        with mock.patch.object(backend.DurableStore, "default", return_value=store), \
             mock.patch.object(backend, "job_id_from_work_dir", return_value="job1"), \
             mock.patch.object(backend, "_film_job_config", return_value={}), \
             mock.patch.object(backend.video_history, "capture_current"), \
             mock.patch.object(backend.threading, "Thread",
                               side_effect=lambda **kw: mock.Mock(start=lambda: None)):
            tid = backend._start_scene_rerender(wd, 2, "video", "more dragons")

        entries = backend._load_rerender_journal()
        self.assertEqual([e["task_id"] for e in entries], [tid])
        self.assertEqual(entries[0]["scene_id"], 2)
        self.assertEqual(entries[0]["instruction"], "more dragons")

        # The wrapper clears the entry once the worker reaches a terminal state
        # (done here; error/cancelled hit the same finally).
        with mock.patch.object(backend, "_append_activity_locked"), \
             mock.patch.object(backend.film_timing, "record"):
            backend._run_rerender_logged(lambda *a, **k: None, tid, wd, 2, "video", {}, {})
        self.assertEqual(backend._load_rerender_journal(), [])

    def test_startup_requeues_interrupted_rerenders_and_drops_vanished_films(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        backend._journal_rerender_add("rerender_02_video_1", wd, 2, "video", "more dragons")
        backend._journal_rerender_add("rerender_01_video_1", _OUT / "no-such-film", 1, "video", "")

        started = []
        with mock.patch.object(backend, "_start_scene_rerender",
                               side_effect=lambda *a: started.append(a) or "tid-new"):
            backend._resume_interrupted_rerenders()

        self.assertEqual(started, [(wd, 2, "video", "more dragons")])
        # Old entries are consumed; the live requeue re-journals under its new
        # task id via the (mocked-out) dispatch.
        self.assertEqual(backend._load_rerender_journal(), [])


if __name__ == "__main__":
    unittest.main()
