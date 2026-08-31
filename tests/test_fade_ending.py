"""Ending fade (dip to black) on the finished film: the published cut's last
seconds fade to black and silence in one re-encode. The standing length lives
in job_config ("fade_ending") so every rebuild re-applies it, and the applied
record keyed on the cut's exact SIZE is what keeps one file from ever being
faded twice — fading never changes duration, so size stands in for the title
cards' duration match."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend  # noqa: E402
import pipeline.assembler as assembler  # noqa: E402

_OUT = Path(tempfile.mkdtemp(prefix="spielbot-test-out-"))
_HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _synthetic_film(path: Path, seconds: float = 4.0) -> Path:
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ], check=True)
    return path


def _mean_volume_db(path: Path, start: float, length: float) -> float:
    out = subprocess.run([
        "ffmpeg", "-v", "info", "-ss", str(start), "-t", str(length),
        "-i", str(path), "-af", "volumedetect", "-f", "null", "-",
    ], capture_output=True, text=True).stderr
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", out)
    return float(m.group(1)) if m else 0.0


def _frame_brightness(path: Path, at: float) -> float:
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        frame = Path(td) / "f.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(at), "-i", str(path),
                        "-frames:v", "1", str(frame)], check=True)
        with Image.open(frame) as im:
            g = im.convert("L")
            px = list(g.getdata())
    return sum(px) / len(px)


@unittest.skipUnless(_HAVE_FFMPEG, "ffmpeg not installed")
class FadeVideoEndingTests(unittest.TestCase):
    def test_tail_goes_black_and_silent_duration_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            src = _synthetic_film(Path(td) / "film.mp4", seconds=4.0)
            out = Path(td) / "faded.mp4"
            assembler.fade_video_ending(src, out, seconds=1.5)
            self.assertAlmostEqual(assembler._get_duration(out), 4.0, delta=0.25)
            # The opening is untouched, the last frames are black.
            self.assertGreater(_frame_brightness(out, 0.5), 40)
            self.assertLess(_frame_brightness(out, 3.9), 10)
            # The sound dips with the picture: the tail (inside the capped
            # 1 s fade) sits well below the untouched head.
            head_db = _mean_volume_db(out, 0.0, 1.0)
            self.assertGreater(head_db, -25)
            self.assertLess(_mean_volume_db(out, 3.6, 0.4), head_db - 8)

    def test_fade_is_capped_at_a_quarter_of_the_film(self):
        with tempfile.TemporaryDirectory() as td:
            src = _synthetic_film(Path(td) / "film.mp4", seconds=4.0)
            out = Path(td) / "faded.mp4"
            assembler.fade_video_ending(src, out, seconds=30)
            # A 30 s fade on a 4 s film fades only the last second: the frame
            # at 2.5 s (before the capped fade starts) is still bright.
            self.assertGreater(_frame_brightness(out, 2.5), 40)

    def test_video_only_input_is_not_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "silent.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
                "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src),
            ], check=True)
            out = Path(td) / "faded.mp4"
            assembler.fade_video_ending(src, out, seconds=0.5)
            # Well into the fade the frame is far darker than the body.
            self.assertLess(_frame_brightness(out, 1.9), _frame_brightness(out, 1.0) / 2)


class _FakeFade:
    """Stand-in for the ffmpeg re-encode: copies the file with a stamp
    appended, so the output is valid to replace() and its SIZE changes —
    the property the applied record keys on."""

    def __init__(self):
        self.calls = []

    def __call__(self, input_path, output_path, seconds=2.0):
        self.calls.append(float(seconds))
        Path(output_path).write_bytes(
            Path(input_path).read_bytes() + b"faded:%f" % seconds)
        return Path(output_path)


class AppliedRecordTests(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-fade-"))
        self.final = self.wd / "film.mp4"
        self.final.write_bytes(b"a fresh cut")

    def test_record_matches_only_the_exact_file(self):
        self.assertIsNone(backend._applied_ending_fade(self.wd, self.final))
        with mock.patch.object(assembler, "fade_video_ending", _FakeFade()):
            backend._fade_final_in_place(self.wd, self.final, 2.0)
        self.assertEqual(backend._applied_ending_fade(self.wd, self.final), 2.0)
        # A rebuild replaces the file with a different cut — the record no
        # longer matches, so the cut reads as clean.
        self.final.write_bytes(b"a rebuilt, different-sized cut")
        self.assertIsNone(backend._applied_ending_fade(self.wd, self.final))

    def test_maybe_fade_applies_once_and_only_when_configured(self):
        fake = _FakeFade()
        with mock.patch.object(assembler, "fade_video_ending", fake):
            backend._maybe_fade_ending(self.wd, self.final)  # no config
            self.assertEqual(fake.calls, [])
            (self.wd / "job_config.json").write_text(json.dumps({"fade_ending": 3.0}))
            backend._maybe_fade_ending(self.wd, self.final)
            backend._maybe_fade_ending(self.wd, self.final)  # already faded
        self.assertEqual(fake.calls, [3.0])
        self.assertEqual(backend._applied_ending_fade(self.wd, self.final), 3.0)

    def test_maybe_fade_is_best_effort(self):
        (self.wd / "job_config.json").write_text(json.dumps({"fade_ending": 2.0}))
        with mock.patch.object(assembler, "fade_video_ending",
                               side_effect=RuntimeError("boom")):
            backend._maybe_fade_ending(self.wd, self.final)  # must not raise
        self.assertIsNone(backend._applied_ending_fade(self.wd, self.final))

    def test_seconds_are_clamped(self):
        self.assertEqual(backend._norm_fade_ending_seconds(0.1), 0.5)
        self.assertEqual(backend._norm_fade_ending_seconds(99), 8.0)
        self.assertEqual(backend._norm_fade_ending_seconds("junk"), 2.0)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        (self.wd / "job_config.json").write_text("{}")
        self.final = backend.gapp._final_path_for_work_dir(self.wd)
        self.final.write_bytes(b"the published cut")
        self.addCleanup(lambda: self.final.unlink(missing_ok=True))
        self.fake = _FakeFade()
        q = mock.patch.object(assembler, "fade_video_ending", self.fake)
        q.start()
        self.addCleanup(q.stop)

    def _jc(self):
        return json.loads((self.wd / "job_config.json").read_text())

    def test_apply_fades_in_place_and_keeps_the_previous_cut(self):
        r = backend.remix_fade_ending(
            backend.FadeEndingBody(work_dir=str(self.wd), seconds=3.0))
        self.assertEqual(self.fake.calls, [3.0])
        self.assertEqual(self._jc()["fade_ending"], 3.0)
        self.assertTrue(r["fade_ending_applied"])
        labels = [v["label"] for v in r["video_history"]["versions"]]
        self.assertEqual(labels, ["Original", "Faded ending (3 s)"])
        self.assertEqual(backend._applied_ending_fade(self.wd, self.final), 3.0)

    def test_same_length_again_is_a_no_op(self):
        backend.remix_fade_ending(backend.FadeEndingBody(work_dir=str(self.wd), seconds=3.0))
        with mock.patch.object(backend, "_reassemble_film_core") as core:
            r = backend.remix_fade_ending(
                backend.FadeEndingBody(work_dir=str(self.wd), seconds=3.0))
        self.assertEqual(self.fake.calls, [3.0])  # not faded again
        core.assert_not_called()
        self.assertIn("already fades", r["message"])

    def test_new_length_rebuilds_instead_of_stacking(self):
        backend.remix_fade_ending(backend.FadeEndingBody(work_dir=str(self.wd), seconds=3.0))
        with mock.patch.object(backend, "_reassemble_film_core") as core:
            backend.remix_fade_ending(
                backend.FadeEndingBody(work_dir=str(self.wd), seconds=5.0))
        core.assert_called_once()
        self.assertEqual(self.fake.calls, [3.0])  # the faded cut itself was never re-faded
        self.assertEqual(self._jc()["fade_ending"], 5.0)

    def test_remove_clears_the_setting_and_rebuilds_a_faded_cut(self):
        backend.remix_fade_ending(backend.FadeEndingBody(work_dir=str(self.wd), seconds=2.0))
        with mock.patch.object(backend, "_reassemble_film_core") as core:
            r = backend.remix_fade_ending_remove(
                backend.FadeEndingRemoveBody(work_dir=str(self.wd)))
        core.assert_called_once()
        self.assertNotIn("fade_ending", self._jc())
        self.assertFalse(r["fade_ending_applied"])
        self.assertFalse((self.wd / backend._FADE_ENDING_APPLIED_NAME).exists())

    def test_remove_on_a_clean_cut_touches_nothing(self):
        with mock.patch.object(backend, "_reassemble_film_core") as core:
            r = backend.remix_fade_ending_remove(
                backend.FadeEndingRemoveBody(work_dir=str(self.wd)))
        core.assert_not_called()
        self.assertIn("no ending fade", r["message"])

    def test_missing_final_is_a_404(self):
        self.final.unlink()
        with self.assertRaises(backend.HTTPException) as cm:
            backend.remix_fade_ending(backend.FadeEndingBody(work_dir=str(self.wd)))
        self.assertEqual(cm.exception.status_code, 404)


class ReassembleChainTests(unittest.TestCase):
    def test_rebuild_chain_fades_between_the_burns_and_the_title_cards(self):
        # The fade must land on the film's ending, not on the end credits —
        # every rebuild chain calls it after the burns and before the cards.
        src = Path(backend.__file__).read_text()
        sites = re.findall(
            r"_maybe_burn_first_frame_cover\(wd, final_path[^)]*\)\s*\n"
            r"\s*(_maybe_fade_ending)\(wd, final_path\)\s*\n"
            r"\s*_maybe_apply_title_cards\(wd, final_path\)", src)
        self.assertEqual(len(sites), src.count("_maybe_burn_first_frame_cover(wd, final_path"))
        self.assertGreaterEqual(len(sites), 7)


if __name__ == "__main__":
    unittest.main()
