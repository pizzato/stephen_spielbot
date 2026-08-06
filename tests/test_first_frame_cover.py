"""First-frame cover (YouTube Shorts thumbnails): burn the cover image into the
head of the finished film. Shorts ignore uploaded thumbnails and pick their own
frame, so the cover is stamped onto the opening frames and held ~1s (a single
frame is a flash the picker discards). With cover typography the cover IS the
title, so the old "text" overlay mode collapsed into the image burn. Covers the
pipeline helpers (mode/duration coercion, ffmpeg command shape, staged in-place
swap), the style plumbing (_ensure_styles coercion + flat mirror +
style_settings resolution), and the edit-screen endpoint (validation + history
versioning)."""
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
    def test_image_passes_through_and_legacy_text_becomes_image(self):
        self.assertEqual(cover.norm_first_frame_cover("image"), "image")
        # Pre-typography configs could pick a big-title overlay; that mode is
        # gone — the cover (which now carries the title) is burned instead.
        self.assertEqual(cover.norm_first_frame_cover("text"), "image")

    def test_everything_else_coerces_to_none(self):
        for bad in ("", None, "bogus", "IMAGE", 3, True):
            self.assertEqual(cover.norm_first_frame_cover(bad), "none")

    def test_cover_seconds_clamps_to_one_frame_through_three_seconds(self):
        self.assertEqual(cover.norm_first_frame_cover_seconds(1.5), 1.5)
        self.assertEqual(cover.norm_first_frame_cover_seconds("0.5"), 0.5)
        self.assertEqual(cover.norm_first_frame_cover_seconds(0), 0.04)    # one frame
        self.assertEqual(cover.norm_first_frame_cover_seconds(-2), 0.04)
        self.assertEqual(cover.norm_first_frame_cover_seconds(30), 3.0)
        for bad in (None, "", "a while"):
            self.assertEqual(cover.norm_first_frame_cover_seconds(bad), 1.0)


class AvailableFontsTests(unittest.TestCase):
    def test_scans_dirs_skips_hidden_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "MyFont.ttf").write_bytes(b"not a real font")
            (Path(tmp) / ".Hidden.ttf").write_bytes(b"x")
            (Path(tmp) / "notes.txt").write_bytes(b"x")
            with mock.patch.object(cover, "_FONT_DIRS", (tmp,)), \
                 mock.patch.object(cover, "_FONTS_CACHE", None):
                fonts = cover.available_fonts()
                # Unparseable file falls back to the filename stem.
                self.assertEqual(fonts, [{"path": str(Path(tmp) / "MyFont.ttf"),
                                          "name": "MyFont"}])
                # Cached: a second call doesn't rescan the (now empty) dir.
                (Path(tmp) / "MyFont.ttf").unlink()
                self.assertEqual(cover.available_fonts(), fonts)
                self.assertEqual(cover.available_fonts(refresh=True), [])


class ReplaceFirstFrameCommandTests(unittest.TestCase):
    def _graph(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "final.mp4"
            frame = Path(tmp) / "cover.png"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            frame.write_bytes(b"png")

            with mock.patch.object(assembler, "_get_video_dimensions", return_value=(720, 1280)), \
                 mock.patch.object(assembler, "_run") as run:
                result = assembler.replace_first_frame(src, frame, out, **kwargs)

            self.assertEqual(result, out)
            cmd = run.call_args.args[0]
            return cmd, cmd[cmd.index("-filter_complex") + 1]

    def test_overlays_the_opening_frames_and_copies_audio(self):
        cmd, graph = self._graph(frames=25)
        # The cover is scaled to the video frame and held over the opening run
        # of frames — overlaying (not prepending) keeps duration and caption
        # timing valid.
        self.assertIn("scale=720:1280:force_original_aspect_ratio=increase", graph)
        self.assertIn("overlay=enable='lt(n,25)'", graph)
        # RGBA survives the scale so a PNG with transparency composites over
        # the video instead of blanking it.
        self.assertIn("[1:v]format=rgba", graph)
        self.assertIn("-c:a", cmd)
        self.assertIn("copy", cmd)
        # Audio is mapped through even when absent (0:a? is optional).
        self.assertIn("0:a?", cmd)

    def test_defaults_to_a_single_frame(self):
        _, graph = self._graph()
        self.assertIn("overlay=enable='lt(n,1)'", graph)


class CoverPhraseTests(unittest.TestCase):
    def test_derived_from_title_when_no_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cover.cover_phrase_for(Path(tmp), "The Silent City: A Story"),
                             "The Silent City")

    def test_override_file_wins_and_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cover_phrase.txt").write_text("SILENT CITY!")
            self.assertEqual(cover.cover_phrase_for(Path(tmp), "The Silent City: A Story"),
                             "SILENT CITY!")
            (Path(tmp) / "cover_phrase.txt").write_text("x" * 200)
            self.assertEqual(len(cover.cover_phrase_for(Path(tmp), "t")), 80)

    def test_blank_override_falls_back_to_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cover_phrase.txt").write_text("   \n")
            self.assertEqual(cover.cover_phrase_for(Path(tmp), "Plain Title"), "Plain Title")


class CoverPhraseEndpointTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(backend, "_video_title_for",
                              return_value="The Silent City: A Story")
        q.start()
        self.addCleanup(q.stop)
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))

    def test_save_and_clear_round_trip(self):
        r = backend.save_cover_phrase(
            backend.CoverPhraseBody(work_dir=str(self.wd), phrase="  SILENT\nCITY! "))
        self.assertEqual(r["cover_phrase"], "SILENT CITY!")       # whitespace collapsed
        self.assertEqual(r["cover_phrase_default"], "The Silent City")
        self.assertEqual((self.wd / "cover_phrase.txt").read_text(), "SILENT CITY!")

        r = backend.save_cover_phrase(
            backend.CoverPhraseBody(work_dir=str(self.wd), phrase=""))
        self.assertEqual(r["cover_phrase"], "The Silent City")    # back to derived
        self.assertFalse((self.wd / "cover_phrase.txt").exists())

    def test_saving_the_derived_phrase_clears_the_override(self):
        (self.wd / "cover_phrase.txt").write_text("OLD")
        backend.save_cover_phrase(
            backend.CoverPhraseBody(work_dir=str(self.wd), phrase="The Silent City"))
        self.assertFalse((self.wd / "cover_phrase.txt").exists())

    def test_rejects_paths_outside_output_dir(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.save_cover_phrase(
                backend.CoverPhraseBody(work_dir="/tmp/elsewhere", phrase="x"))
        self.assertEqual(ctx.exception.status_code, 400)


class BurnCoverTests(unittest.TestCase):
    def test_requires_a_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "final.mp4"
            video.write_bytes(b"v")
            with self.assertRaises(FileNotFoundError):
                cover.burn_cover_into_first_frame(
                    video, cover_path=Path(tmp) / "cover.png")

    def test_swaps_the_staged_result_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "final.mp4"
            video.write_bytes(b"original")
            cover_png = Path(tmp) / "cover.png"
            cover_png.write_bytes(b"p" * 2000)

            held = []

            def fake_replace(src, frame, out, frames=1):
                self.assertEqual(frame, cover_png)
                held.append(frames)
                out.write_bytes(b"stamped")
                return out

            with mock.patch("pipeline.assembler.replace_first_frame", side_effect=fake_replace):
                result = cover.burn_cover_into_first_frame(video, cover_path=cover_png)

            self.assertEqual(result, video)
            self.assertEqual(video.read_bytes(), b"stamped")
            # Default hold is 1s — 25 frames at the film's fixed 25fps.
            self.assertEqual(held, [25])
            self.assertFalse(list(Path(tmp).glob("*.tmp*")), "staging file cleaned up")

    def test_hold_seconds_become_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "final.mp4"
            cover_png = Path(tmp) / "cover.png"
            cover_png.write_bytes(b"p" * 2000)

            held = []

            def fake_replace(src, frame, out, frames=1):
                held.append(frames)
                out.write_bytes(b"stamped")
                return out

            for secs, frames in ((0.6, 15), (2, 50), (0, 1), (99, 75), ("x", 25)):
                video.write_bytes(b"original")
                with mock.patch("pipeline.assembler.replace_first_frame",
                                side_effect=fake_replace):
                    cover.burn_cover_into_first_frame(
                        video, cover_path=cover_png, seconds=secs)
            self.assertEqual(held, [15, 50, 1, 75, 25])


class StylePlumbingTests(unittest.TestCase):
    def test_ensure_styles_keeps_and_mirrors_the_mode(self):
        cfg = {"styles": [{"name": "Shorts", "first_frame_cover": "image"}]}
        app._ensure_styles(cfg)
        self.assertEqual(cfg["styles"][0]["first_frame_cover"], "image")
        # Flat key mirrors the default style, like every STYLE_FIELD_TO_FLAT entry.
        self.assertEqual(cfg["default_first_frame_cover"], "image")
        self.assertEqual(app.style_settings(cfg, "Shorts")["first_frame_cover"], "image")

    def test_legacy_text_mode_migrates_to_image(self):
        cfg = {"styles": [{"name": "Shorts", "first_frame_cover": "text"}]}
        app._ensure_styles(cfg)
        self.assertEqual(cfg["styles"][0]["first_frame_cover"], "image")
        self.assertEqual(cfg["default_first_frame_cover"], "image")

    def test_ensure_styles_coerces_bad_values(self):
        cfg = {"styles": [{"name": "Shorts", "first_frame_cover": "sideways"}]}
        app._ensure_styles(cfg)
        self.assertEqual(cfg["styles"][0]["first_frame_cover"], "none")

    def test_hold_seconds_are_coerced_and_mirrored(self):
        cfg = {"styles": [{"name": "Shorts", "first_frame_cover_seconds": 99}]}
        app._ensure_styles(cfg)
        self.assertEqual(cfg["styles"][0]["first_frame_cover_seconds"], 3.0)   # clamped
        self.assertEqual(cfg["default_first_frame_cover_seconds"], 3.0)
        # A style that never set it gets the 1s default, not the old one frame.
        cfg = {"styles": [{"name": "Shorts"}]}
        app._ensure_styles(cfg)
        self.assertEqual(app.style_settings(cfg, "Shorts")["first_frame_cover_seconds"], 1.0)

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

    def test_requires_cover(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.remix_first_frame_cover(
                backend.FirstFrameCoverBody(work_dir=str(self.wd)))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("cover", ctx.exception.detail.lower())

    def test_requires_final_video(self):
        (self.wd / "cover.png").write_bytes(b"p" * 2000)
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.remix_first_frame_cover(
                backend.FirstFrameCoverBody(work_dir=str(self.wd)))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_spawns_the_burn_task(self):
        (self.wd / "cover.png").write_bytes(b"p" * 2000)
        final = _OUT / f"{self.wd.name}.mp4"
        final.write_bytes(b"v" * 20_000)
        with mock.patch.object(backend.threading, "Thread") as thread:
            r = backend.remix_first_frame_cover(
                backend.FirstFrameCoverBody(work_dir=str(self.wd)))
        self.assertTrue(r["ok"])
        self.assertIn("task_id", r)
        self.assertIs(thread.call_args.kwargs["target"], backend._run_first_frame_cover)
        self.assertEqual(backend._film_tasks[r["task_id"]]["step"], "first_frame_cover")

    def test_legacy_text_mode_body_is_accepted(self):
        # Old clients still send mode="text"; the burn is the cover image now.
        (self.wd / "cover.png").write_bytes(b"p" * 2000)
        final = _OUT / f"{self.wd.name}.mp4"
        final.write_bytes(b"v" * 20_000)
        with mock.patch.object(backend.threading, "Thread"):
            r = backend.remix_first_frame_cover(
                backend.FirstFrameCoverBody(work_dir=str(self.wd), mode="text"))
        self.assertTrue(r["ok"])

    def test_runner_records_history_versions(self):
        final = _OUT / f"{self.wd.name}.mp4"
        final.write_bytes(b"v" * 20_000)
        tid = "first_frame_cover_test"
        backend._film_tasks[tid] = {"status": "running", "step": "first_frame_cover"}
        backend._film_task_meta[tid] = {
            "work_dir": str(self.wd), "scene_id": 0,
            "component": "first_frame_cover", "started_at": 0.0,
        }

        def fake_burn(path, **kwargs):
            Path(path).write_bytes(b"stamped" * 3000)
            return path

        with mock.patch("pipeline.cover.burn_cover_into_first_frame", side_effect=fake_burn):
            backend._run_first_frame_cover(tid, self.wd)

        task = backend._film_tasks[tid]
        self.assertEqual(task["status"], "done", task.get("error"))
        hist = final_video_history.history(self.wd)
        labels = [v["label"] for v in hist["versions"]]
        self.assertEqual(labels, ["Original", "Cover on first frame"])
        self.assertEqual(hist["selected"], hist["versions"][-1]["id"])
        self.assertEqual(final.read_bytes(), b"stamped" * 3000)


class BurnSecondsOverrideTests(unittest.TestCase):
    """The edit/publish screens can hold the cover longer or shorter than the
    style says, for this one burn."""

    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        (self.wd / "cover.png").write_bytes(b"p" * 2000)
        self.final = _OUT / f"{self.wd.name}.mp4"
        self.final.write_bytes(b"v" * 20_000)

    def _burn(self, seconds):
        captured = {}

        def fake_burn(path, **kw):
            captured.update(kw)
            Path(path).write_bytes(b"stamped" * 3000)
            return path

        tid = f"ff_{seconds}"
        backend._film_tasks[tid] = {"status": "running", "step": "first_frame_cover"}
        backend._film_task_meta[tid] = {
            "work_dir": str(self.wd), "scene_id": 0,
            "component": "first_frame_cover", "started_at": 0.0,
        }
        with mock.patch("pipeline.cover.burn_cover_into_first_frame", side_effect=fake_burn), \
             mock.patch.object(backend.gapp, "load_config", return_value={}):
            backend._run_first_frame_cover(tid, self.wd, seconds)
        self.assertEqual(backend._film_tasks[tid]["status"], "done",
                         backend._film_tasks[tid].get("error"))
        return captured

    def test_explicit_seconds_win_and_are_clamped(self):
        self.assertEqual(self._burn(2.5)["seconds"], 2.5)
        self.assertEqual(self._burn(99)["seconds"], 3.0)

    def test_none_falls_back_to_the_style(self):
        self.assertEqual(self._burn(None)["seconds"], 1.0)

    def test_endpoint_passes_the_hold_through(self):
        with mock.patch.object(backend.threading, "Thread") as thread:
            backend.remix_first_frame_cover(backend.FirstFrameCoverBody(
                work_dir=str(self.wd), seconds=1.5))
        self.assertEqual(thread.call_args.kwargs["args"][2], 1.5)

    def test_hold_is_optional(self):
        with mock.patch.object(backend.threading, "Thread") as thread:
            backend.remix_first_frame_cover(
                backend.FirstFrameCoverBody(work_dir=str(self.wd)))
        self.assertIsNone(thread.call_args.kwargs["args"][2])


class BurnOptsResolutionTests(unittest.TestCase):
    """Manual burns and rebuild re-applies resolve the hold LIVE from the
    film's style, so a Settings tweak applies to the very next burn."""

    def test_resolves_live_style_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "job_config.json").write_text('{"style_name": "Shorts"}')
            cfg = {"styles": [{"name": "Shorts", "first_frame_cover_seconds": 1.5}]}
            app._ensure_styles(cfg)
            with mock.patch.object(backend.gapp, "load_config", return_value=cfg):
                opts = backend._first_frame_burn_opts(wd)
            self.assertEqual(opts, {"seconds": 1.5})

    def test_defaults_when_style_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(backend.gapp, "load_config", return_value={}):
                opts = backend._first_frame_burn_opts(Path(tmp))
            self.assertEqual(opts, {"seconds": 1.0})


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
        with mock.patch("pipeline.cover.burn_cover_into_first_frame") as burn:
            backend._maybe_burn_first_frame_cover(self.wd, self.final)
        burn.assert_called_once()
        self.assertEqual(burn.call_args.args, (self.final,))
        self.assertEqual(burn.call_args.kwargs["cover_path"], self.wd / "cover.png")

    def test_legacy_standing_text_mode_burns_the_cover_image(self):
        (self.wd / "job_config.json").write_text('{"first_frame_cover": "text"}')
        with mock.patch("pipeline.cover.burn_cover_into_first_frame") as burn:
            backend._maybe_burn_first_frame_cover(self.wd, self.final)
        burn.assert_called_once()
        self.assertEqual(burn.call_args.kwargs["cover_path"], self.wd / "cover.png")

    def test_no_standing_mode_means_no_burn(self):
        (self.wd / "job_config.json").write_text('{"first_frame_cover": "none"}')
        with mock.patch("pipeline.cover.burn_cover_into_first_frame") as burn:
            backend._maybe_burn_first_frame_cover(self.wd, self.final)
            (self.wd / "job_config.json").write_text("{}")  # and with no key at all
            backend._maybe_burn_first_frame_cover(self.wd, self.final)
        burn.assert_not_called()

    def test_burn_failure_never_breaks_the_rebuild(self):
        (self.wd / "job_config.json").write_text('{"first_frame_cover": "image"}')
        with mock.patch("pipeline.cover.burn_cover_into_first_frame",
                        side_effect=RuntimeError("ffmpeg exploded")):
            backend._maybe_burn_first_frame_cover(self.wd, self.final)  # must not raise


if __name__ == "__main__":
    unittest.main()
