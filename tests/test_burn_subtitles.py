"""Burned-in subtitles (open captions): burn the script's SRT track into the
final video's picture. A per-style toggle stamped into job_config.json at
start, honoured by the render's final assembly, and re-applied by every flow
that rebuilds the final from combined.mp4 (remix, narrator/music change,
reassemble, localized cut — which burns its own language). Covers the ffmpeg
command shape, the staged in-place swap, the style plumbing (_ensure_styles
coercion + flat mirror + inheritance) and the rebuild re-apply gating."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app  # noqa: E402
import webapp.backend.main as backend  # noqa: E402
import pipeline.assembler as assembler  # noqa: E402
import pipeline.captions as captions  # noqa: E402
from pipeline import final_video_history  # noqa: E402

# Work dirs must live under OUTPUT_DIR (endpoints reject paths outside it).
_OUT = Path(tempfile.mkdtemp(prefix="spielbot-test-out-"))


class NormTests(unittest.TestCase):
    def test_truthy_strings_and_bools(self):
        for yes in (True, 1, "1", "true", "Yes", " ON "):
            self.assertIs(app._norm_burn_subtitles(yes), True, yes)

    def test_everything_else_is_off(self):
        for no in (False, 0, None, "", "false", "no", "off", "bogus"):
            self.assertIs(app._norm_burn_subtitles(no), False, no)


class BurnCommandTests(unittest.TestCase):
    def test_renders_the_track_and_copies_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "final.mp4"
            srt = Path(tmp) / "captions.srt"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"video")
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")

            with mock.patch.object(assembler, "_run") as run:
                result = assembler.burn_subtitles(src, srt, out)

            self.assertEqual(result, out)
            cmd = run.call_args.args[0]
            vf = cmd[cmd.index("-vf") + 1]
            # The track is copied to a special-character-free temp path first —
            # the subtitles filter reads its filename through two levels of
            # filtergraph escaping, and film names carry apostrophes/colons.
            self.assertTrue(vf.startswith("subtitles="), vf)
            track = vf.split(":", 1)[0]
            self.assertTrue(track.endswith("/captions.srt"), vf)
            self.assertNotIn(str(srt), vf)
            # The style's look rides along as an ASS force_style override.
            self.assertIn(":force_style='", vf)
            self.assertIn("-c:a", cmd)
            self.assertIn("copy", cmd)

    def test_temp_copy_exists_when_ffmpeg_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "final.mp4"
            srt = Path(tmp) / "captions.srt"
            src.write_bytes(b"video")
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")

            def check(cmd, timeout=None):
                vf = cmd[cmd.index("-vf") + 1]
                self.assertTrue(Path(vf.split(":", 1)[0].removeprefix("subtitles=")).exists())

            with mock.patch.object(assembler, "_run", side_effect=check):
                assembler.burn_subtitles(src, srt, Path(tmp) / "out.mp4")


class BurnInPlaceTests(unittest.TestCase):
    def test_swaps_the_staged_result_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "final.mp4"
            video.write_bytes(b"original")
            srt = Path(tmp) / "captions.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")

            def fake_burn(src, track, out, style=None):
                self.assertEqual(track, srt)
                out.write_bytes(b"captioned")
                return out

            with mock.patch("pipeline.assembler.burn_subtitles", side_effect=fake_burn):
                result = captions.burn_srt_into_video(video, srt)

            self.assertEqual(result, video)
            self.assertEqual(video.read_bytes(), b"captioned")
            self.assertFalse(list(Path(tmp).glob("*.tmp*")), "staging file cleaned up")

    def test_requires_a_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "final.mp4"
            video.write_bytes(b"v")
            with self.assertRaises(FileNotFoundError):
                captions.burn_srt_into_video(video, Path(tmp) / "captions.srt")
            (Path(tmp) / "captions.srt").write_text("")  # empty counts as missing
            with self.assertRaises(FileNotFoundError):
                captions.burn_srt_into_video(video, Path(tmp) / "captions.srt")


class StylePlumbingTests(unittest.TestCase):
    def test_ensure_styles_coerces_and_mirrors(self):
        cfg = {"styles": [{"name": "Shorts", "burn_subtitles": "yes"}]}
        app._ensure_styles(cfg)
        self.assertIs(cfg["styles"][0]["burn_subtitles"], True)
        # Flat key mirrors the default style, like every STYLE_FIELD_TO_FLAT entry.
        self.assertIs(cfg["default_burn_subtitles"], True)
        self.assertIs(app.style_settings(cfg, "Shorts")["burn_subtitles"], True)

    def test_defaults_off(self):
        cfg = {"styles": [{"name": "Shorts"}]}
        app._ensure_styles(cfg)
        self.assertIs(app.style_settings(cfg, "Shorts")["burn_subtitles"], False)

    def test_sparse_child_inherits_parent_toggle(self):
        cfg = {
            "styles": [
                {"name": "Base", "burn_subtitles": True},
                {"name": "Kid", "parent": "Base"},
            ],
            "default_style": "Base",
        }
        app._ensure_styles(cfg)
        self.assertNotIn("burn_subtitles", cfg["styles"][1])
        self.assertIs(app.style_settings(cfg, "Kid")["burn_subtitles"], True)


class RebuildReapplyTests(unittest.TestCase):
    """Flows that rebuild the final from combined.mp4 re-burn a STANDING track
    (job_config burn_subtitles) — films whose style never opted in stay clean."""

    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        self.final = _OUT / f"{self.wd.name}.mp4"
        self.final.write_bytes(b"v" * 20_000)

    def test_standing_toggle_is_reapplied(self):
        (self.wd / "job_config.json").write_text('{"burn_subtitles": true}')
        srt = self.wd / "captions.srt"
        with mock.patch("pipeline.captions.build_srt", return_value=srt) as build, \
             mock.patch("pipeline.captions.burn_srt_into_video") as burn:
            backend._maybe_burn_subtitles(self.wd, self.final)
        build.assert_called_once_with(self.wd, lang=None, timing_lang=None, style=mock.ANY)
        burn.assert_called_once_with(self.final, srt, style=mock.ANY)

    def test_localized_rebuild_burns_its_language(self):
        (self.wd / "job_config.json").write_text('{"burn_subtitles": true}')
        with mock.patch("pipeline.captions.build_srt", return_value=None) as build, \
             mock.patch("pipeline.captions.burn_srt_into_video") as burn:
            backend._maybe_burn_subtitles(self.wd, self.final, lang="pt")
        build.assert_called_once_with(self.wd, lang="pt", timing_lang="pt", style=mock.ANY)
        # Nothing to caption (all-acted film, or no translation): no burn.
        burn.assert_not_called()

    def test_no_standing_toggle_means_no_burn(self):
        (self.wd / "job_config.json").write_text('{"burn_subtitles": false}')
        with mock.patch("pipeline.captions.build_srt") as build:
            backend._maybe_burn_subtitles(self.wd, self.final)
            (self.wd / "job_config.json").write_text("{}")  # and with no key at all
            backend._maybe_burn_subtitles(self.wd, self.final)
        build.assert_not_called()

    def test_burn_failure_never_breaks_the_rebuild(self):
        (self.wd / "job_config.json").write_text('{"burn_subtitles": true}')
        with mock.patch("pipeline.captions.build_srt",
                        side_effect=RuntimeError("ffprobe exploded")):
            backend._maybe_burn_subtitles(self.wd, self.final)  # must not raise


class RemixSubtitlesEndpointTests(unittest.TestCase):
    """POST /api/remix/subtitles: burn captions into a film after the fact —
    or remove a burn — by persisting the flag and rebuilding the final."""

    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        self.final = _OUT / f"{self.wd.name}.mp4"
        self.final.write_bytes(b"v" * 20_000)

    def _jc(self):
        return backend._film_job_config(self.wd)

    def test_burn_persists_flag_and_rebuilds(self):
        with mock.patch("pipeline.captions.build_srt",
                        return_value=self.wd / "captions.srt"), \
             mock.patch.object(backend, "_reassemble_film_core",
                               return_value=3) as core:
            result = backend.remix_subtitles(
                backend.RemixSubtitlesBody(work_dir=str(self.wd), burn=True))
        self.assertIs(self._jc()["burn_subtitles"], True)
        core.assert_called_once_with(self.wd, "Burning subtitles")
        self.assertIs(result["burn_subtitles"], True)
        self.assertIn(self.final.name, result["final_url"])

    def test_remove_persists_flag_and_rebuilds_clean(self):
        (self.wd / "job_config.json").write_text('{"burn_subtitles": true}')
        with mock.patch("pipeline.captions.build_srt") as build, \
             mock.patch.object(backend, "_reassemble_film_core",
                               return_value=3) as core:
            result = backend.remix_subtitles(
                backend.RemixSubtitlesBody(work_dir=str(self.wd), burn=False))
        # Removal never needs a track — the rebuild simply skips the burn.
        build.assert_not_called()
        self.assertIs(self._jc()["burn_subtitles"], False)
        core.assert_called_once_with(self.wd, "Removing subtitles")
        self.assertIs(result["burn_subtitles"], False)

    def test_nothing_to_caption_is_400_and_leaves_config_alone(self):
        from fastapi import HTTPException
        with mock.patch("pipeline.captions.build_srt", return_value=None), \
             mock.patch.object(backend, "_reassemble_film_core") as core:
            with self.assertRaises(HTTPException) as ctx:
                backend.remix_subtitles(
                    backend.RemixSubtitlesBody(work_dir=str(self.wd), burn=True))
        self.assertEqual(ctx.exception.status_code, 400)
        core.assert_not_called()
        self.assertNotIn("burn_subtitles", self._jc())

    def test_unrenderable_film_is_400(self):
        from fastapi import HTTPException
        with mock.patch("pipeline.captions.build_srt",
                        return_value=self.wd / "captions.srt"), \
             mock.patch.object(backend, "_reassemble_film_core",
                               side_effect=ValueError("No rendered scenes found.")):
            with self.assertRaises(HTTPException) as ctx:
                backend.remix_subtitles(
                    backend.RemixSubtitlesBody(work_dir=str(self.wd), burn=True))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_picked_upscale_is_burnt_in_place_not_rebuilt(self):
        """A derived cut (upscale / localized re-voicing) is the work the
        versions list keeps — burning must draw onto it, not throw it away by
        rebuilding the plain concat from the scene parts."""
        final_video_history.record(self.wd, self.final, label="Original")
        final_video_history.record(self.wd, self.final,
                                   label="FlashVSR 2560x1440", kind="upscale")
        srt = self.wd / "captions.srt"
        with mock.patch("pipeline.captions.build_srt", return_value=srt) as build, \
             mock.patch("pipeline.captions.burn_srt_into_video") as burn, \
             mock.patch.object(backend, "_title_cards_head_seconds", return_value=6.5), \
             mock.patch.object(backend, "_reassemble_film_core") as core:
            result = backend.remix_subtitles(
                backend.RemixSubtitlesBody(work_dir=str(self.wd), burn=True))
        core.assert_not_called()
        burn.assert_called_once_with(self.final, srt, style=mock.ANY)
        # Cues shift past the opening title card: the rebuild path burns
        # before the cards go on, an in-place burn cannot.
        self.assertEqual(build.call_args.kwargs["offset"], 6.5)
        self.assertIs(self._jc()["burn_subtitles"], True)
        self.assertIs(result["burn_subtitles"], True)
        hist = final_video_history.history(self.wd)
        newest = hist["versions"][-1]
        self.assertEqual(newest["label"], "Subtitles burned")
        self.assertEqual(hist["selected"], newest["id"])

    def test_burning_onto_an_already_burnt_cut_is_refused(self):
        from fastapi import HTTPException
        (self.wd / "job_config.json").write_text('{"burn_subtitles": true}')
        final_video_history.record(self.wd, self.final, label="Original")
        final_video_history.record(self.wd, self.final,
                                   label="FlashVSR 2560x1440", kind="upscale")
        with mock.patch("pipeline.captions.burn_srt_into_video") as burn:
            with self.assertRaises(HTTPException) as ctx:
                backend.remix_subtitles(
                    backend.RemixSubtitlesBody(work_dir=str(self.wd), burn=True))
        self.assertEqual(ctx.exception.status_code, 400)
        burn.assert_not_called()

    def test_removing_a_burn_from_a_picked_cut_says_what_it_replaced(self):
        (self.wd / "job_config.json").write_text('{"burn_subtitles": true}')
        final_video_history.record(self.wd, self.final, label="Original")
        final_video_history.record(self.wd, self.final,
                                   label="FlashVSR 2560x1440", kind="upscale")
        with mock.patch.object(backend, "_reassemble_film_core", return_value=3) as core:
            result = backend.remix_subtitles(
                backend.RemixSubtitlesBody(work_dir=str(self.wd), burn=False))
        core.assert_called_once_with(self.wd, "Removing subtitles")
        self.assertIn("FlashVSR 2560x1440", result["message"])

    def test_plain_cut_still_rebuilds(self):
        """No kept versions (almost every film): unchanged behaviour."""
        srt = self.wd / "captions.srt"
        with mock.patch("pipeline.captions.build_srt", return_value=srt), \
             mock.patch("pipeline.captions.burn_srt_into_video") as burn, \
             mock.patch.object(backend, "_reassemble_film_core", return_value=3) as core:
            backend.remix_subtitles(
                backend.RemixSubtitlesBody(work_dir=str(self.wd), burn=True))
        core.assert_called_once_with(self.wd, "Burning subtitles")
        burn.assert_not_called()

    def test_outside_output_dir_is_rejected(self):
        from fastapi import HTTPException
        outside = Path(tempfile.mkdtemp(prefix="spielbot-outside-"))
        with self.assertRaises(HTTPException) as ctx:
            backend.remix_subtitles(
                backend.RemixSubtitlesBody(work_dir=str(outside), burn=True))
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()


class CaptionDownloadTests(unittest.TestCase):
    """/api/film/captions.srt hands the publishing caption track (with timings)
    to the browser as a named .srt download, per language."""

    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))

    def test_serves_the_track_as_a_named_download(self):
        srt = self.wd / "captions.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
        with mock.patch("pipeline.captions.build_srt", return_value=srt) as build, \
             mock.patch.object(backend, "_video_title_for", return_value="Deep Sea: Giants!"):
            resp = backend.film_captions_srt(work_dir=str(self.wd), lang="")
        build.assert_called_once_with(self.wd, lang=None, timing_lang=None, offset=0.0, style=mock.ANY)
        self.assertEqual(resp.path, str(srt))
        self.assertIn('filename="Deep_Sea_Giants_en.srt"', resp.headers["content-disposition"])

    def test_localized_language_is_worded_in_that_language(self):
        srt = self.wd / "captions_pt.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nOi\n")
        with mock.patch("pipeline.captions.build_srt", return_value=srt) as build:
            backend.film_captions_srt(work_dir=str(self.wd), lang="pt")
        build.assert_called_once_with(self.wd, lang="pt", timing_lang=None, offset=0.0, style=mock.ANY)

    def test_nothing_to_caption_is_a_404(self):
        with mock.patch("pipeline.captions.build_srt", return_value=None):
            with self.assertRaises(backend.HTTPException) as cm:
                backend.film_captions_srt(work_dir=str(self.wd), lang="")
        self.assertEqual(cm.exception.status_code, 404)

    def test_rejects_paths_outside_the_output_folder(self):
        with self.assertRaises(backend.HTTPException):
            backend.film_captions_srt(work_dir="/etc", lang="")
