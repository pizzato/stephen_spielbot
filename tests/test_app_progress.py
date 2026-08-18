import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app


class AppProgressTests(unittest.TestCase):
    """Progress polling moved from the removed Gradio ``_poll_job_outputs``
    callback to the FastAPI ``/api/progress`` endpoint (webapp/backend/main.py),
    which is a thin wrapper over these ``app`` helpers:
    ``_preferred_work_dir`` → ``_status_for_work_dir`` → ``_final_path_for_work_dir``.
    """

    def test_progress_falls_back_to_latest_job(self):
        """With no tracked work dir, /api/progress reports the most recent job."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            work_dir = output_dir / "history-test-20260521-010203"
            work_dir.mkdir()
            (work_dir / "progress.json").write_text(
                json.dumps({"pct": 100, "msg": "Done", "ts": 1})
            )
            (work_dir / "combined.mp4").write_bytes(b"x" * 20_000)
            (output_dir / f"{work_dir.name}.mp4").write_bytes(b"x" * 20_000)

            with mock.patch.object(app, "OUTPUT_DIR", output_dir):
                picked = app._preferred_work_dir("")
                pct, msg = app._status_for_work_dir(picked)
                final_path = app._final_path_for_work_dir(picked)

            self.assertEqual(picked, work_dir)
            self.assertEqual(pct, 100.0)
            self.assertIn("Done", msg)
            self.assertTrue(final_path.exists())

    def test_progress_honors_explicit_active_job(self):
        """An explicitly tracked work dir is honored even when a newer job exists.

        The Gradio-era behaviour of auto-jumping to the newest job was removed:
        the React client passes the work dir it is tracking and the server keeps
        it. Only an empty/missing tracked dir falls back to the latest job.
        """
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            old_dir = output_dir / "old-job-20260521-010203"
            new_dir = output_dir / "new-job-20260521-020304"
            old_dir.mkdir()
            new_dir.mkdir()
            (old_dir / "progress.json").write_text(
                json.dumps({"pct": 100, "msg": "Old done", "ts": 1})
            )
            (new_dir / "progress.json").write_text(
                json.dumps({"pct": 5, "msg": "New running", "ts": 2})
            )
            os.utime(old_dir / "progress.json", (1, 1))
            os.utime(new_dir / "progress.json", (10, 10))

            with mock.patch.object(app, "OUTPUT_DIR", output_dir):
                self.assertEqual(app._latest_work_dir(), new_dir)
                # Explicitly tracked dir wins, even though new_dir is newer.
                self.assertEqual(app._preferred_work_dir(str(old_dir)), old_dir)
                # Empty or missing tracked dir falls back to the latest job.
                self.assertEqual(app._preferred_work_dir(""), new_dir)
                self.assertEqual(
                    app._preferred_work_dir(str(output_dir / "gone")), new_dir
                )

    def test_running_render_wins_over_newer_files(self):
        """Creating a second film touches its script/job files constantly, which
        made every 'current render' surface flip to it while the first film was
        still rendering. The job whose process is actually alive must win."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            rendering = output_dir / "rendering-20260818-000001"
            creating = output_dir / "creating-20260818-000002"
            rendering.mkdir()
            creating.mkdir()
            (rendering / "job.json").write_text(json.dumps(
                # Our own pid answers os.kill(pid, 0) — a live render process.
                {"status": "running", "pid": os.getpid(), "created_at": 1}
            ))
            (rendering / "progress.json").write_text(
                json.dumps({"pct": 40, "msg": "Scene 4", "ts": 1})
            )
            (creating / "script.json").write_text("{}")
            (creating / "progress.json").write_text(
                json.dumps({"pct": 0, "msg": "Waiting to start...", "ts": 2})
            )
            for p in rendering.iterdir():
                os.utime(p, (1, 1))
            for p in creating.iterdir():
                os.utime(p, (10, 10))

            with mock.patch.object(app, "OUTPUT_DIR", output_dir):
                # The mtime heuristic alone would pick the film being created…
                self.assertEqual(app._latest_work_dir(), creating)
                # …but the live render process wins.
                self.assertEqual(app._running_work_dirs(), [rendering])
                self.assertEqual(app._preferred_work_dir(""), rendering)

    def test_dead_pid_does_not_count_as_running(self):
        """A job.json left at status "running" by a crashed process must not
        hijack the current-render pick — recency applies as before."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            stale = output_dir / "crashed-20260818-000001"
            fresh = output_dir / "fresh-20260818-000002"
            stale.mkdir()
            fresh.mkdir()
            (stale / "job.json").write_text(json.dumps(
                {"status": "running", "pid": 2 ** 22 + 1234, "created_at": 1}
            ))
            (fresh / "progress.json").write_text(
                json.dumps({"pct": 0, "msg": "Waiting to start...", "ts": 2})
            )
            os.utime(stale / "job.json", (1, 1))
            os.utime(fresh / "progress.json", (10, 10))

            with mock.patch.object(app, "OUTPUT_DIR", output_dir):
                self.assertEqual(app._running_work_dirs(), [])
                self.assertEqual(app._preferred_work_dir(""), fresh)


class WorkerActivityTests(unittest.TestCase):
    """``/api/progress`` builds its worker list from the configured render fleet
    and each in-flight task's ``lease_owner`` (= ``worker_id(kind, endpoint)``),
    so the render page shows what every ComfyUI/TTS worker is actually rendering
    rather than a flat "online" badge. The cover agent's ``kind="ui"`` lease never
    maps onto a render worker (issue #98 removed the dedicated ui_workers pool).
    """

    cfg = {
        "comfy_workers": ["http://s1:8188", "http://s3:8188"],
        "tts_workers": ["s1", "s3"],
    }

    def _activity(self, tasks, reserved=0):
        from webapp.backend.main import _worker_activity

        return {(w["kind"], w["endpoint"]): w for w in _worker_activity(self.cfg, tasks, reserved)}

    def test_lists_exactly_the_configured_fleet_idle(self):
        out = self._activity([])
        self.assertEqual(
            set(out),
            {("comfy", "http://s1:8188"), ("comfy", "http://s3:8188"),
             ("tts", "s1"), ("tts", "s3")},
        )
        self.assertTrue(all(w["state"] == "idle" and w["job"] == "" for w in out.values()))

    def test_running_task_shows_its_job_on_the_leased_worker(self):
        from pipeline.orchestrator import worker_id

        tasks = [{"status": "running", "name": "Scene 3 video",
                  "lease_owner": worker_id("comfy", "http://s1:8188")}]
        out = self._activity(tasks)
        self.assertEqual(out[("comfy", "http://s1:8188")]["state"], "working")
        self.assertEqual(out[("comfy", "http://s1:8188")]["job"], "Scene 3 video")
        self.assertEqual(out[("comfy", "http://s3:8188")]["state"], "idle")

    def test_tts_task_maps_to_its_tts_endpoint(self):
        from pipeline.orchestrator import worker_id

        tasks = [{"status": "running", "name": "Scene 1 narration",
                  "lease_owner": worker_id("tts", "s3")}]
        out = self._activity(tasks)
        self.assertEqual(out[("tts", "s3")]["job"], "Scene 1 narration")
        self.assertEqual(out[("tts", "s1")]["state"], "idle")

    def test_one_idle_comfy_worker_is_flagged_reserved_during_a_render(self):
        from pipeline.orchestrator import worker_id

        tasks = [{"status": "running", "name": "Scene 3 video",
                  "lease_owner": worker_id("comfy", "http://s1:8188")}]
        out = self._activity(tasks, reserved=1)
        self.assertEqual(out[("comfy", "http://s1:8188")]["state"], "working")
        self.assertEqual(out[("comfy", "http://s3:8188")]["state"], "reserved")

    def test_no_reservation_label_when_nothing_is_rendering(self):
        # reserved=1 but no comfy task running — don't mislabel an all-idle fleet.
        out = self._activity([], reserved=1)
        self.assertTrue(all(w["state"] != "reserved" for w in out.values()))

    def test_ui_kind_lease_owner_never_maps_to_a_render_worker(self):
        from pipeline.orchestrator import worker_id

        # The cover agent leases as kind="ui" on a render endpoint; its job must
        # not surface on that comfy worker.
        tasks = [{"status": "running", "name": "cover",
                  "lease_owner": worker_id("ui", "http://s1:8188")}]
        out = self._activity(tasks)
        self.assertEqual(out[("comfy", "http://s1:8188")]["state"], "idle")


if __name__ == "__main__":
    unittest.main()
