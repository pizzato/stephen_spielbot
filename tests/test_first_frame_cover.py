"""First-frame cover (YouTube Shorts thumbnails): burn the cover image — or the
title in large type — into frame 0 of the finished film. Shorts ignore uploaded
thumbnails and show the video's first frame in the feed, so the cover is
stamped onto the frame itself. Covers the pipeline helpers (mode coercion,
ffmpeg command shape, PIL text overlay, staged in-place swap), the style
plumbing (_ensure_styles coercion + flat mirror + style_settings resolution),
and the edit-screen endpoint (validation + history versioning)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app  # noqa: E402
import webapp.backend.main as backend  # noqa: E402
import pipeline.assembler as assembler  # noqa: E402
import pipeline.cover as cover  # noqa: E402
from pipeline import final_video_history  # noqa: E402

# Work dirs must live under OUTPUT_DIR (endpoints reject paths outside it).
_OUT = Path(tempfile.mkdtemp(prefix="spielbot-test-out-"))


class NormModeTests(unittest.TestCase):
    def test_valid_modes_pass_through(self):
        self.assertEqual(cover.norm_first_frame_cover("image"), "image")
        self.assertEqual(cover.norm_first_frame_cover("text"), "text")

    def test_everything_else_coerces_to_none(self):
        for bad in ("", None, "bogus", "IMAGE", 3, True):
            self.assertEqual(cover.norm_first_frame_cover(bad), "none")


class ReplaceFirstFrameCommandTests(unittest.TestCase):
    def test_overlays_only_frame_zero_and_copies_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "final.mp4"
            frame = Path(tmp) / "cover.png"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            frame.write_bytes(b"png")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(720, 1280)), \
                 mock.patch.object(assembler, "_run") as run:
                result = assembler.replace_first_frame(src, frame, out)

            self.assertEqual(result, out)
            cmd = run.call_args.args[0]
            graph = cmd[cmd.index("-filter_complex") + 1]
            # The cover is scaled to the video frame and shown on frame 0 ONLY —
            # replacing (not prepending) keeps duration and caption timing valid.
            self.assertIn("scale=720:1280:force_original_aspect_ratio=increase", graph)
            self.assertIn("overlay=enable='eq(n,0)'", graph)
            self.assertIn("-c:a", cmd)
            self.assertIn("copy", cmd)
            # Audio is mapped through even when absent (0:a? is optional).
            self.assertIn("0:a?", cmd)


class OverlayCoverTextTests(unittest.TestCase):
    def test_draws_large_text_across_the_top(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "frame.png"
            out = Path(tmp) / "texted.png"
            Image.new("RGB", (360, 640), (40, 90, 40)).save(base)

            cover.overlay_cover_text(base, out, "The Silent City")

            img = Image.open(out)
            self.assertEqual(img.size, (360, 640))
            # Top region gained the scrim + white text; bottom stays untouched.
            top = img.crop((0, 0, 360, 200)).convert("L")
            extremes = top.histogram()
            whites = sum(extremes[200:])
            self.assertGreater(whites, 100, "expected bright text pixels near the top")
            bottom_px = img.getpixel((180, 620))
            self.assertEqual(bottom_px, (40, 90, 40))


class BurnCoverTests(unittest.TestCase):
    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            cover.burn_cover_into_first_frame(Path("/tmp/x.mp4"), "sideways")

    def test_image_mode_requires_a_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "final.mp4"
            video.write_bytes(b"v")
            with self.assertRaises(FileNotFoundError):
                cover.burn_cover_into_first_frame(
                    video, "image", cover_path=Path(tmp) / "cover.png")

    def test_image_mode_swaps_the_staged_result_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "final.mp4"
            video.write_bytes(b"original")
            cover_png = Path(tmp) / "cover.png"
            cover_png.write_bytes(b"p" * 2000)

            def fake_replace(src, frame, out):
                self.assertEqual(frame, cover_png)
                out.write_bytes(b"stamped")
                return out

            with mock.patch("pipeline.assembler.replace_first_frame", side_effect=fake_replace):
                result = cover.burn_cover_into_first_frame(
                    video, "image", cover_path=cover_png)

            self.assertEqual(result, video)
            self.assertEqual(video.read_bytes(), b"stamped")
            self.assertFalse(list(Path(tmp).glob("*.tmp*")), "staging file cleaned up")

    def test_text_mode_stamps_the_shortened_title_on_the_first_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp) / "job"
            wd.mkdir()
            video = Path(tmp) / "final.mp4"
            video.write_bytes(b"original")

            texts = []

            def fake_extract(src, out):
                out.write_bytes(b"frame")
                return out

            def fake_overlay(base, out, text):
                texts.append(text)
                out.write_bytes(b"texted")

            def fake_replace(src, frame, out):
                self.assertEqual(frame.name, "first_frame_text.png")
                out.write_bytes(b"stamped")
                return out

            with mock.patch("pipeline.assembler.extract_first_frame", side_effect=fake_extract), \
                 mock.patch("pipeline.cover.overlay_cover_text", side_effect=fake_overlay), \
                 mock.patch("pipeline.assembler.replace_first_frame", side_effect=fake_replace):
                cover.burn_cover_into_first_frame(
                    video, "text",
                    title="The Silent City: A Story of Machines", work_dir=wd)

            # The subtitle after ":" is dropped — same phrase the cover uses.
            self.assertEqual(texts, ["The Silent City"])
            self.assertEqual(video.read_bytes(), b"stamped")
            self.assertTrue((wd / "first_frame_text_base.png").exists())


class StylePlumbingTests(unittest.TestCase):
    def test_ensure_styles_keeps_and_mirrors_the_mode(self):
        cfg = {"styles": [{"name": "Shorts", "first_frame_cover": "text"}]}
        app._ensure_styles(cfg)
        self.assertEqual(cfg["styles"][0]["first_frame_cover"], "text")
        # Flat key mirrors the default style, like every STYLE_FIELD_TO_FLAT entry.
        self.assertEqual(cfg["default_first_frame_cover"], "text")
        self.assertEqual(app.style_settings(cfg, "Shorts")["first_frame_cover"], "text")

    def test_ensure_styles_coerces_bad_values(self):
        cfg = {"styles": [{"name": "Shorts", "first_frame_cover": "sideways"}]}
        app._ensure_styles(cfg)
        self.assertEqual(cfg["styles"][0]["first_frame_cover"], "none")

    def test_sparse_child_inherits_parent_mode(self):
        cfg = {
            "styles": [
                {"name": "Base", "first_frame_cover": "image"},
                {"name": "Kid", "parent": "Base"},
            ],
            "default_style": "Base",
        }
        app._ensure_styles(cfg)
        self.assertNotIn("first_frame_cover", cfg["styles"][1])
        self.assertEqual(app.style_settings(cfg, "Kid")["first_frame_cover"], "image")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))

    def test_rejects_unknown_mode(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.remix_first_frame_cover(
                backend.FirstFrameCoverBody(work_dir=str(self.wd), mode="bogus"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_image_mode_requires_cover(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.remix_first_frame_cover(
                backend.FirstFrameCoverBody(work_dir=str(self.wd), mode="image"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("cover", ctx.exception.detail.lower())

    def test_text_mode_requires_final_video(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.remix_first_frame_cover(
                backend.FirstFrameCoverBody(work_dir=str(self.wd), mode="text"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_spawns_the_burn_task(self):
        final = _OUT / f"{self.wd.name}.mp4"
        final.write_bytes(b"v" * 20_000)
        with mock.patch.object(backend.threading, "Thread") as thread:
            r = backend.remix_first_frame_cover(
                backend.FirstFrameCoverBody(work_dir=str(self.wd), mode="text"))
        self.assertTrue(r["ok"])
        self.assertIn("task_id", r)
        self.assertIs(thread.call_args.kwargs["target"], backend._run_first_frame_cover)
        self.assertEqual(backend._film_tasks[r["task_id"]]["step"], "first_frame_cover")

    def test_runner_records_history_versions(self):
        final = _OUT / f"{self.wd.name}.mp4"
        final.write_bytes(b"v" * 20_000)
        tid = "first_frame_cover_test"
        backend._film_tasks[tid] = {"status": "running", "step": "first_frame_cover"}
        backend._film_task_meta[tid] = {
            "work_dir": str(self.wd), "scene_id": 0,
            "component": "first_frame_cover", "started_at": 0.0,
        }

        def fake_burn(path, mode, **kwargs):
            Path(path).write_bytes(b"stamped" * 3000)
            return path

        with mock.patch("pipeline.cover.burn_cover_into_first_frame", side_effect=fake_burn), \
             mock.patch.object(backend, "_video_title_for", return_value="A Film"):
            backend._run_first_frame_cover(tid, self.wd, "image")

        task = backend._film_tasks[tid]
        self.assertEqual(task["status"], "done", task.get("error"))
        hist = final_video_history.history(self.wd)
        labels = [v["label"] for v in hist["versions"]]
        self.assertEqual(labels, ["Original", "Cover on first frame"])
        self.assertEqual(hist["selected"], hist["versions"][-1]["id"])
        self.assertEqual(final.read_bytes(), b"stamped" * 3000)


class RebuildReapplyTests(unittest.TestCase):
    """Flows that rebuild the final from combined.mp4 re-apply a STANDING burn
    (job_config first_frame_cover) — one-off manual stamps are not re-applied."""

    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        self.final = _OUT / f"{self.wd.name}.mp4"
        self.final.write_bytes(b"v" * 20_000)

    def test_standing_mode_is_reapplied(self):
        (self.wd / "job_config.json").write_text('{"first_frame_cover": "image"}')
        with mock.patch("pipeline.cover.burn_cover_into_first_frame") as burn, \
             mock.patch.object(backend, "_video_title_for", return_value="A Film"):
            backend._maybe_burn_first_frame_cover(self.wd, self.final)
        burn.assert_called_once()
        self.assertEqual(burn.call_args.args, (self.final, "image"))

    def test_no_standing_mode_means_no_burn(self):
        (self.wd / "job_config.json").write_text('{"first_frame_cover": "none"}')
        with mock.patch("pipeline.cover.burn_cover_into_first_frame") as burn:
            backend._maybe_burn_first_frame_cover(self.wd, self.final)
            (self.wd / "job_config.json").write_text("{}")  # and with no key at all
            backend._maybe_burn_first_frame_cover(self.wd, self.final)
        burn.assert_not_called()

    def test_burn_failure_never_breaks_the_rebuild(self):
        (self.wd / "job_config.json").write_text('{"first_frame_cover": "text"}')
        with mock.patch("pipeline.cover.burn_cover_into_first_frame",
                        side_effect=RuntimeError("ffmpeg exploded")), \
             mock.patch.object(backend, "_video_title_for", return_value="A Film"):
            backend._maybe_burn_first_frame_cover(self.wd, self.final)  # must not raise


if __name__ == "__main__":
    unittest.main()
