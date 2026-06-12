"""Deleting (or cancelling) a job must kill its render subprocess.

POST /api/jobs/delete used to call on_cancel_active_job, which only cancels
pending/running tasks in the DurableStore — it never signalled the
resume_generation.py subprocess, and no-oped entirely when the job had no
durable row. The work dir was then rmtree'd while the render kept running as
an orphan, burning ComfyUI worker time on prompts whose output was discarded.
Covers the repairs — _terminate_job_process (shared with pause) SIGTERMing the
pid from job.json, delete_job killing by work_dir before deleting files, and
on_cancel_active_job killing the process and stamping job.json "cancelled" so
the dir stops counting as a running job.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app
import webapp.backend.main as backend
from pipeline.orchestrator import DurableStore


class RenderKillCase(unittest.TestCase):
    """Point app.CONFIG_FILE (and friends) at per-test temp locations."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-delete-kill-")
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

    def make_work_dir(self, name: str, job_status: str, pid: int | None = None) -> Path:
        wd = self.output_dir / name
        wd.mkdir()
        (wd / "job_config.json").write_text(json.dumps({"title": name}))
        meta = {"work_dir": str(wd), "status": job_status, "updated_at": time.time()}
        if pid is not None:
            meta["pid"] = pid
        (wd / "job.json").write_text(json.dumps(meta))
        return wd

    def spawn_render_stub(self) -> subprocess.Popen:
        """A live process standing in for resume_generation.py."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

        def _reap():
            if proc.poll() is None:
                proc.kill()
            proc.wait()

        self.addCleanup(_reap)
        return proc

    def assert_sigtermed(self, proc: subprocess.Popen):
        self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)

    def durable_row(self, job_id: str, wd: Path, status: str = "running") -> None:
        store = DurableStore.default()
        try:
            store.create_or_update_job(job_id, wd, wd.name, status=status)
        finally:
            store.close()


class TerminateJobProcessTests(RenderKillCase):
    """The shared kill helper extracted from on_pause_active_job."""

    def test_sigterms_the_pid_from_job_json(self):
        proc = self.spawn_render_stub()
        wd = self.make_work_dir("live", "running", pid=proc.pid)
        self.assertTrue(app._terminate_job_process(wd))
        self.assert_sigtermed(proc)

    def test_dead_pid_is_a_noop(self):
        wd = self.make_work_dir("dead", "running", pid=2 ** 30)
        self.assertFalse(app._terminate_job_process(wd))

    def test_missing_job_json_is_a_noop(self):
        wd = self.output_dir / "bare"
        wd.mkdir()
        self.assertFalse(app._terminate_job_process(wd))


class DeleteJobTests(RenderKillCase):
    """delete_job must kill the render before removing its files — even when
    the job has no durable row, where on_cancel_active_job no-ops."""

    def test_delete_kills_render_without_durable_row(self):
        proc = self.spawn_render_stub()
        wd = self.make_work_dir("doomed", "running", pid=proc.pid)
        with mock.patch.object(backend.yt, "load_queue", return_value=[]):
            out = backend.delete_job(backend.JobActionBody(work_dir=str(wd)))
        self.assertTrue(out["ok"])
        self.assert_sigtermed(proc)
        self.assertFalse(wd.exists())

    def test_rejected_path_is_not_killed(self):
        # Validation must run before any signal: a path outside the videos
        # directory must neither SIGTERM its job.json pid nor be deleted.
        proc = self.spawn_render_stub()
        outside = Path(self._tmp.name) / "elsewhere"
        outside.mkdir()
        (outside / "job.json").write_text(json.dumps({"status": "running", "pid": proc.pid}))
        with self.assertRaises(backend.HTTPException):
            backend.delete_job(backend.JobActionBody(work_dir=str(outside)))
        self.assertIsNone(proc.poll(), "render outside the videos dir must be left alone")
        self.assertTrue(outside.exists())


class CancelJobTests(RenderKillCase):
    """on_cancel_active_job (the /api/jobs/cancel and /api/queue/abandon path)
    must stop the render, not just cancel its DurableStore tasks."""

    def test_cancel_kills_process_and_marks_job_cancelled(self):
        proc = self.spawn_render_stub()
        wd = self.make_work_dir("cancelme", "running", pid=proc.pid)
        self.durable_row("job-1", wd)
        msg = app.on_cancel_active_job(str(wd))
        self.assert_sigtermed(proc)
        self.assertIn("terminated", msg)
        meta = json.loads((wd / "job.json").read_text())
        self.assertEqual(meta["status"], "cancelled")
        self.assertIsNone(meta["pid"])

    def test_cancelled_job_stops_counting_as_running(self):
        # Killed without the "cancelled" stamp, the dir would read "running"
        # and block automation (_is_job_running) for up to 24 h.
        wd = self.make_work_dir("stale", "running", pid=2 ** 30)
        self.durable_row("job-2", wd)
        app.on_cancel_active_job(str(wd))
        with mock.patch.object(app.yt, "load_queue", return_value=[]):
            self.assertFalse(app._is_job_running())

    def test_cancel_without_durable_row_does_not_kill(self):
        # Documented gap: cancel acts only on durable jobs, which is why
        # delete_job kills by work_dir itself before delegating here.
        proc = self.spawn_render_stub()
        wd = self.make_work_dir("rowless", "running", pid=proc.pid)
        msg = app.on_cancel_active_job(str(wd))
        self.assertIn("No durable job", msg)
        self.assertIsNone(proc.poll())


class PauseJobTests(RenderKillCase):
    """on_pause_active_job kept its behaviour after the helper extraction."""

    def test_pause_still_kills_and_marks_paused(self):
        proc = self.spawn_render_stub()
        wd = self.make_work_dir("pauseme", "running", pid=proc.pid)
        msg = app.on_pause_active_job(str(wd))
        self.assert_sigtermed(proc)
        self.assertIn("process terminated", msg)
        meta = json.loads((wd / "job.json").read_text())
        self.assertEqual(meta["status"], "paused")
        self.assertIsNone(meta["pid"])


if __name__ == "__main__":
    unittest.main()
