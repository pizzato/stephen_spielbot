"""A re-render that fails while the user is on another screen must not vanish.

/films/tasks used to return running tasks only, so on reload the edit page saw
nothing for a failed re-render and the scene silently kept its old frame/clip.
It now also returns recently-failed tasks (bounded by _FILM_ERROR_TTL_S), tagged
status="error", so the scene can surface the failure and invite a retry. Starting
a fresh re-render clears the scene's prior terminal records so the badge doesn't
linger and the in-memory stores don't accumulate."""
import time
import unittest

import webapp.backend.main as backend
from test_delete_film_cancels_tasks import FilmTaskCase


class FilmTasksSurfaceErrorsTests(FilmTaskCase):
    def _error_task(self, wd, tid, sid, finished_at, msg="boom", component="video"):
        backend._film_tasks[tid] = {"status": "error", "error": msg, "finished_at": finished_at}
        backend._film_task_meta[tid] = {"work_dir": str(wd), "scene_id": sid, "component": component}

    def test_running_task_returned_with_status(self):
        wd = self.make_work_dir("f1")
        self.register_task(wd, "rerender_01_video_1", sid=1, component="video")
        out = backend.film_tasks_for_work_dir(str(wd))["tasks"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "running")
        self.assertEqual(out[0]["scene_id"], 1)

    def test_recent_error_surfaced(self):
        wd = self.make_work_dir("f1")
        self._error_task(wd, "rerender_02_video_1", sid=2, finished_at=time.time(), msg="LTX blew up")
        out = backend.film_tasks_for_work_dir(str(wd))["tasks"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "error")
        self.assertEqual(out[0]["scene_id"], 2)
        self.assertEqual(out[0]["error"], "LTX blew up")

    def test_stale_error_not_surfaced(self):
        wd = self.make_work_dir("f1")
        self._error_task(wd, "rerender_02_video_1", sid=2,
                         finished_at=time.time() - backend._FILM_ERROR_TTL_S - 10)
        self.assertEqual(backend.film_tasks_for_work_dir(str(wd))["tasks"], [])

    def test_cancelled_not_surfaced(self):
        wd = self.make_work_dir("f1")
        backend._film_tasks["t"] = {"status": "cancelled"}
        backend._film_task_meta["t"] = {"work_dir": str(wd), "scene_id": 1, "component": "video"}
        self.assertEqual(backend.film_tasks_for_work_dir(str(wd))["tasks"], [])

    def test_other_work_dir_ignored(self):
        wd = self.make_work_dir("f1")
        other = self.make_work_dir("f2")
        self._error_task(other, "t", sid=1, finished_at=time.time())
        self.assertEqual(backend.film_tasks_for_work_dir(str(wd))["tasks"], [])

    def test_clear_finished_drops_terminal_keeps_running_and_other_scenes(self):
        wd = self.make_work_dir("f1")
        self._error_task(wd, "err_scene1", sid=1, finished_at=time.time())
        self.register_task(wd, "run_scene1", sid=1, component="image")   # running, same scene
        self._error_task(wd, "err_scene2", sid=2, finished_at=time.time())

        backend._clear_finished_film_tasks(str(wd), 1)

        # Scene 1's failed record is gone (a fresh re-render supersedes it)...
        self.assertNotIn("err_scene1", backend._film_tasks)
        self.assertNotIn("err_scene1", backend._film_task_meta)
        # ...but a running task on the same scene and another scene's error stay.
        self.assertIn("run_scene1", backend._film_tasks)
        self.assertIn("err_scene2", backend._film_tasks)


if __name__ == "__main__":
    unittest.main()
