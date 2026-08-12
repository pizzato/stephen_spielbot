"""Trim-the-tail endpoint for a rendered film scene.

ffmpeg is mocked (it just writes shorter bytes), so these cover the wiring the
endpoint owns: validation of the requested end point, the atomic staging swap,
and the trimmed cut landing in video history with the untrimmed take kept.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app
import pipeline.assembler as assembler
import webapp.backend.main as backend
from fastapi import HTTPException


class SceneTrimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-trim-")
        self.addCleanup(self._tmp.cleanup)
        self.output_dir = Path(self._tmp.name) / "videos"
        self.output_dir.mkdir()
        p = mock.patch.object(app, "OUTPUT_DIR", self.output_dir)
        p.start()
        self.addCleanup(p.stop)

        self.wd = self.output_dir / "film"
        self.wd.mkdir()
        self.final = self.wd / "scene_01_final.mp4"
        self.final.write_bytes(b"untrimmed")

    def _body(self, end):
        return backend.FilmTrimBody(work_dir=str(self.wd), end_seconds=end)

    def _trim(self, end, duration=10.0, trim=None):
        def fake_trim(src, out, dur):
            Path(out).write_bytes(b"trimmed")
            return out

        with mock.patch.object(assembler, "_get_duration", return_value=duration), \
             mock.patch.object(assembler, "trim_video", side_effect=trim or fake_trim):
            return backend.trim_film_scene(1, self._body(end))

    def test_trim_rewrites_final_and_keeps_untrimmed_take(self):
        r = self._trim(6.0)

        self.assertEqual(self.final.read_bytes(), b"trimmed")
        versions = r["video_history"]["versions"]
        self.assertEqual(len(versions), 2)                              # untrimmed + trimmed
        self.assertEqual(r["video_history"]["selected"], versions[-1]["id"])
        self.assertEqual(Path(versions[0]["path"]).read_bytes(), b"untrimmed")
        self.assertEqual(Path(versions[-1]["path"]).read_bytes(), b"trimmed")
        self.assertFalse((self.wd / "scene_01_final.staging.mp4").exists())

    def test_trim_passes_requested_end_to_ffmpeg(self):
        seen = []

        def record(src, out, dur):
            seen.append((Path(src).name, dur))
            Path(out).write_bytes(b"trimmed")

        self._trim(6.5, trim=record)
        self.assertEqual(seen, [("scene_01_final.mp4", 6.5)])

    def test_full_length_is_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            self._trim(10.0)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(self.final.read_bytes(), b"untrimmed")

    def test_sliver_is_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            self._trim(0.2)
        self.assertEqual(cm.exception.status_code, 400)

    def test_missing_video_is_rejected(self):
        self.final.unlink()
        with self.assertRaises(HTTPException) as cm:
            self._trim(6.0)
        self.assertEqual(cm.exception.status_code, 400)

    def test_failed_trim_leaves_the_clip_alone(self):
        def boom(src, out, dur):
            Path(out).write_bytes(b"half")
            raise RuntimeError("ffmpeg failed")

        with self.assertRaises(HTTPException) as cm:
            self._trim(6.0, trim=boom)
        self.assertEqual(cm.exception.status_code, 503)
        self.assertEqual(self.final.read_bytes(), b"untrimmed")
        self.assertFalse((self.wd / "scene_01_final.staging.mp4").exists())

    def test_path_outside_output_dir_is_rejected(self):
        outside = Path(self._tmp.name) / "elsewhere"
        outside.mkdir()
        with self.assertRaises(HTTPException) as cm:
            backend.trim_film_scene(1, backend.FilmTrimBody(work_dir=str(outside), end_seconds=5.0))
        self.assertEqual(cm.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
