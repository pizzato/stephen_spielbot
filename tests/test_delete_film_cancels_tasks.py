"""Deleting a film must cancel its running scene re-render tasks.

POST /api/films/delete used to validate the path and rmtree the work dir
without touching _film_tasks/_film_task_meta. Scene re-renders run as daemon
threads inside the backend process — no pid to SIGTERM, unlike PR #76's
resume_generation.py — so after a delete they kept going: the in-flight
ComfyUI/TTS call ran to completion on the worker (and a thread blocked in
WorkerPool.acquire() would later submit fresh work) for a film nobody would
ever see, then the thread died as status "error" ("No such file or
directory") and logged "Re-render failed" for a deletion the user asked for.
Covers the repairs — _cancel_film_tasks flagging running tasks by work_dir,
worker checkpoints aborting before the next remote step, post-deletion
failures landing as "cancelled" rather than "error", and delete_film /
delete_job calling the helper before removing files.
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app
import webapp.backend.main as backend


class FilmTaskCase(unittest.TestCase):
    """Temp OUTPUT_DIR plus clean film-task registries for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-delete-film-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.config_file = tmp / "config" / "config.yaml"
        self.config_file.parent.mkdir(parents=True)
        self.output_dir = tmp / "videos"
        self.output_dir.mkdir()
        for target, attr, value in [
            (app, "CONFIG_FILE", self.config_file),
            (app, "VOICES_DIR", self.config_file.parent / "voices"),
            (app, "OUTPUT_DIR", self.output_dir),
        ]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        db = mock.patch.dict(os.environ, {"SPIELBOT_ORCHESTRATOR_DB": str(tmp / "orchestrator.sqlite3")})
        db.start()
        self.addCleanup(db.stop)
        for reg in (backend._film_tasks, backend._film_task_meta, backend._film_cancelled_tids):
            reg.clear()
            self.addCleanup(reg.clear)

    def make_work_dir(self, name: str) -> Path:
        wd = self.output_dir / name
        wd.mkdir()
        (wd / "job_config.json").write_text(json.dumps({"title": name}))
        return wd

    def register_task(self, wd: Path, tid: str, status: str = "running",
                      sid: int = 1, component: str = "narration") -> str:
        backend._film_tasks[tid] = {"status": status, "step": component}
        backend._film_task_meta[tid] = {"work_dir": str(wd), "scene_id": sid, "component": component}
        return tid

    def run_narration_worker(self, tid: str, wd: Path, sid: int = 1) -> threading.Thread:
        """The exact thread rerender_film_scene starts for a narration task."""
        return threading.Thread(
            target=backend._run_rerender_logged,
            args=(backend._run_narration_rerender, tid, wd, sid, "narration", {}, {"narration": "hello"}),
            daemon=True,
        )


class CancelFilmTasksTests(FilmTaskCase):
    """The flagging helper shared by delete_film and delete_job."""

    def test_flags_only_running_tasks_for_that_dir(self):
        wd = self.make_work_dir("target")
        other = self.make_work_dir("other")
        running = self.register_task(wd, "rerender_01_narration_1")
        finished = self.register_task(wd, "rerender_02_video_2", status="done", sid=2, component="video")
        elsewhere = self.register_task(other, "rerender_01_image_3", component="image")
        self.assertEqual(backend._cancel_film_tasks(wd), 1)
        self.assertIn(running, backend._film_cancelled_tids)
        self.assertNotIn(finished, backend._film_cancelled_tids)
        self.assertNotIn(elsewhere, backend._film_cancelled_tids)


class DeleteFilmTests(FilmTaskCase):
    """delete_film must stop a film's re-render threads before rmtree."""

    def test_delete_mid_step_lands_cancelled_not_error(self):
        # The thread is "inside" the remote TTS call when the film is deleted;
        # the call completes and writes into the vanished dir. That failure
        # must read as a cancellation, not an error, and must not reach mux.
        wd = self.make_work_dir("doomed")
        tid = self.register_task(wd, "rerender_01_narration_1000")
        in_tts = threading.Event()
        deleted = threading.Event()

        def fake_tts(text, out_path, **kwargs):
            in_tts.set()
            deleted.wait(timeout=10)
            Path(out_path).write_bytes(b"x" * 2000)

        with mock.patch("pipeline.tts_worker.generate_narration", side_effect=fake_tts), \
                mock.patch("pipeline.assembler.mux_video_audio") as mux:
            t = self.run_narration_worker(tid, wd)
            t.start()
            self.assertTrue(in_tts.wait(timeout=10))
            out = backend.delete_film(backend.JobActionBody(work_dir=str(wd)))
            deleted.set()
            t.join(timeout=10)
            self.assertFalse(t.is_alive())

        self.assertTrue(out["ok"])
        self.assertFalse(wd.exists())
        self.assertEqual(backend._film_tasks[tid]["status"], "cancelled")
        mux.assert_not_called()
        # The edit page's resume endpoint must no longer report the task...
        self.assertEqual(backend.film_tasks_for_work_dir(work_dir=str(wd))["tasks"], [])
        # ...and the Activity log records a cancellation, not a failure.
        self.assertEqual(backend._activity_log[0]["name"], "Re-render cancelled — scene 1")

    def test_flagged_task_aborts_before_first_remote_call(self):
        # Delete lands after the task was registered but before its thread
        # ran (rerender_film_scene registers first): no TTS call at all.
        wd = self.make_work_dir("early")
        tid = self.register_task(wd, "rerender_01_narration_2000")
        backend.delete_film(backend.JobActionBody(work_dir=str(wd)))
        with mock.patch("pipeline.tts_worker.generate_narration") as gen, \
                mock.patch("pipeline.assembler.mux_video_audio") as mux:
            t = self.run_narration_worker(tid, wd)
            t.start()
            t.join(timeout=10)
            self.assertFalse(t.is_alive())
        gen.assert_not_called()
        mux.assert_not_called()
        self.assertEqual(backend._film_tasks[tid]["status"], "cancelled")

    def test_rejected_path_cancels_nothing(self):
        # Validation must run before any cancellation: a path outside the
        # videos directory must neither flag its tasks nor be deleted.
        outside = Path(self._tmp.name) / "elsewhere"
        outside.mkdir()
        tid = self.register_task(outside, "rerender_01_narration_3000")
        with self.assertRaises(backend.HTTPException):
            backend.delete_film(backend.JobActionBody(work_dir=str(outside)))
        self.assertNotIn(tid, backend._film_cancelled_tids)
        self.assertTrue(outside.exists())


class WorkerCheckpointTests(FilmTaskCase):
    """The image and video workers honour the flag before touching a GPU."""

    def test_image_worker_aborts_without_comfy_call(self):
        wd = self.make_work_dir("img")
        tid = self.register_task(wd, "rerender_01_image_4000", component="image")
        backend._film_cancelled_tids.add(tid)
        with mock.patch.object(app, "_preview_worker_urls", return_value=["http://stub:8188"]), \
                mock.patch("pipeline.comfyui.generate_scene_image") as gen:
            backend._run_image_rerender(tid, wd, 1, {}, {})
        gen.assert_not_called()
        self.assertEqual(backend._film_tasks[tid]["status"], "cancelled")

    def test_video_worker_aborts_without_comfy_call(self):
        wd = self.make_work_dir("vid")
        tid = self.register_task(wd, "rerender_01_video_5000", component="video")
        backend._film_cancelled_tids.add(tid)
        with mock.patch.object(app, "_preview_worker_urls", return_value=["http://stub:8188"]), \
                mock.patch("pipeline.scene_video.generate_scene_video") as gen:
            backend._run_video_rerender(tid, wd, 1, {}, {})
        gen.assert_not_called()
        self.assertEqual(backend._film_tasks[tid]["status"], "cancelled")


class DeleteJobTests(FilmTaskCase):
    """delete_job removes the same dirs from the Jobs page — it must flag
    film re-render tasks too, not just SIGTERM the render subprocess."""

    def test_delete_job_flags_film_tasks(self):
        wd = self.make_work_dir("jobdir")
        tid = self.register_task(wd, "rerender_01_narration_6000")
        with mock.patch.object(backend.yt, "load_queue", return_value=[]):
            out = backend.delete_job(backend.JobActionBody(work_dir=str(wd)))
        self.assertTrue(out["ok"])
        self.assertIn(tid, backend._film_cancelled_tids)
        self.assertFalse(wd.exists())


if __name__ == "__main__":
    unittest.main()
