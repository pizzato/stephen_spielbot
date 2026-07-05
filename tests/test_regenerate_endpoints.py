"""Tests for the issue #88 regenerate endpoints: comment reply drafting, Create
brief improvement, and YouTube title regeneration. The LLM call is mocked so the
tests assert prompt wiring and response shaping, not model output."""
import os
import tempfile
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

        def fake_temporal(src, out, width, height, command_template=None, timeout_seconds=None):
            self.assertEqual(src, final)
            self.assertEqual((width, height), (1920, 1080))
            self.assertEqual(command_template, "temporal-cli -i {input} -o {output}")
            self.assertEqual(timeout_seconds, 1234)
            out.write_bytes(b"temporal-upscaled-final")
            return out

        with mock.patch.object(backend.gapp, "_RESOLUTIONS", {"Landscape FHD (1920×1080)": (1920, 1080)}), \
             mock.patch.object(backend.gapp, "load_config", return_value={
                 "temporal_video_upscaler_cmd": "temporal-cli -i {input} -o {output}",
                 "temporal_video_upscaler_timeout": 1234,
             }), \
             mock.patch("pipeline.assembler._get_video_dimensions", return_value=(512, 288)), \
             mock.patch("pipeline.assembler.temporal_ai_upscale_video", side_effect=fake_temporal) as temporal, \
             mock.patch("pipeline.assembler.upscale_video") as fast:
            backend._film_tasks["tid"] = {"status": "running", "step": "final_upscale"}
            backend._run_final_video_upscale("tid", wd, "Landscape FHD (1920×1080)", "temporal_ai")

        self.assertEqual(final.read_bytes(), b"temporal-upscaled-final")
        self.assertEqual(backend._film_tasks["tid"]["status"], "done")
        temporal.assert_called_once()
        fast.assert_not_called()

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
        self.assertEqual(started[0][3], "temporal_ai")

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


if __name__ == "__main__":
    unittest.main()
