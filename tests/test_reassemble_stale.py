"""Stale-final auto-reassembly sweep (_stale_final_films / _film_edit_busy).

Editing a rendered film refreshes scene_XX_final.mp4 parts (or the display
order) but leaves the published final untouched until "Reassemble film" is
clicked. The automation loop's sweep finds finished films whose parts are
newer than the final — once quiet — and reassembles them. These tests cover
the selection predicate: mtime comparison (parts and order file), the quiet
window, job status, and the busy guards (running edit task, in-flight
publish/upload).
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app
import webapp.backend.main as backend


class StaleFinalCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-stale-final-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.output_dir = tmp / "videos"
        self.output_dir.mkdir()
        p = mock.patch.object(app, "OUTPUT_DIR", self.output_dir)
        p.start()
        self.addCleanup(p.stop)
        for reg in (backend._film_tasks, backend._film_task_meta, backend._upload_tasks):
            reg.clear()
            self.addCleanup(reg.clear)
        # No publish-queue entries unless a test says otherwise.
        pq = mock.patch.object(backend.pq, "item_by_work_dir", return_value=None)
        pq.start()
        self.addCleanup(pq.stop)

    def make_film(self, name: str, *, status: str = "done",
                  part_age_s: float = 400.0, final_age_s: float = 1000.0) -> Path:
        """A finished film: work dir with one scene part + the published final.
        Ages are seconds-before-now for the respective mtimes."""
        wd = self.output_dir / name
        wd.mkdir()
        now = time.time()
        (wd / "combined.mp4").write_bytes(b"c")  # _list_recent_jobs marker
        (wd / "job.json").write_text(json.dumps({"status": status}))
        part = wd / "scene_01_final.mp4"
        part.write_bytes(b"p")
        os.utime(part, (now - part_age_s, now - part_age_s))
        final = self.output_dir / f"{name}.mp4"
        final.write_bytes(b"f")
        os.utime(final, (now - final_age_s, now - final_age_s))
        return wd

    def test_part_newer_than_final_and_quiet_is_stale(self):
        wd = self.make_film("edited")
        self.assertEqual(backend._stale_final_films(), [wd])

    def test_final_newer_than_parts_is_consistent(self):
        self.make_film("fresh", part_age_s=1000.0, final_age_s=400.0)
        self.assertEqual(backend._stale_final_films(), [])

    def test_recent_part_change_waits_for_quiet(self):
        self.make_film("busy-editing", part_age_s=10.0)
        self.assertEqual(backend._stale_final_films(), [])

    def test_reorder_alone_marks_stale(self):
        wd = self.make_film("reordered", part_age_s=2000.0, final_age_s=1000.0)
        order = wd / "scene_edit_order.json"
        order.write_text("[1]")
        ts = time.time() - 400
        os.utime(order, (ts, ts))
        self.assertEqual(backend._stale_final_films(), [wd])

    def test_unfinished_job_skipped(self):
        self.make_film("still-rendering", status="running")
        self.assertEqual(backend._stale_final_films(), [])

    def test_missing_final_skipped(self):
        wd = self.make_film("never-assembled")
        (self.output_dir / f"{wd.name}.mp4").unlink()
        self.assertEqual(backend._stale_final_films(), [])

    def test_running_edit_task_blocks_sweep(self):
        wd = self.make_film("mid-rerender")
        backend._film_tasks["t1"] = {"status": "running", "step": "video"}
        backend._film_task_meta["t1"] = {"work_dir": str(wd), "scene_id": 1,
                                         "component": "video"}
        self.assertEqual(backend._stale_final_films(), [])
        backend._film_tasks["t1"] = {"status": "done"}
        self.assertEqual(backend._stale_final_films(), [wd])

    def test_uploading_blocks_sweep(self):
        wd = self.make_film("mid-upload")
        backend._upload_tasks["u1"] = {"status": "uploading", "work_dir": str(wd)}
        self.assertEqual(backend._stale_final_films(), [])

    def test_publishing_queue_state_blocks_sweep(self):
        wd = self.make_film("mid-publish")
        entry = {"youtube": {"status": "publishing"}, "x": {"status": "pending"}}
        with mock.patch.object(backend.pq, "item_by_work_dir", return_value=entry):
            self.assertEqual(backend._stale_final_films(), [])

    def test_sweep_reassembles_each_stale_film(self):
        wd = self.make_film("stale-one")
        with mock.patch.object(backend, "_reassemble_film_core", return_value=1) as core:
            backend._reassemble_stale_finals()
        core.assert_called_once_with(wd, op_name="Auto-reassembling film")


if __name__ == "__main__":
    unittest.main()
