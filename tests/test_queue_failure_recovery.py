"""Queue recovery when a render fails (the overnight-stuck-queue bug).

A render that died left its queue item "creating" forever: _is_job_running
counted it as a live job for 24 h, automation never started the next item, and
_reconcile_queue (browser-poll only, and blind to job.json status "error")
never failed the item. Covers the three repairs — _is_job_running ignoring
items whose job already errored, _reconcile_queue failing them (even for the
active work dir), and _auto_start_best retrying failed items when nothing
else is startable — plus the LTX frame clamp that caused the failure.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import yaml

import app
import webapp.backend.main as backend
from pipeline import comfyui


class TempConfigCase(unittest.TestCase):
    """Point app.CONFIG_FILE (and friends) at per-test temp locations."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-queue-recovery-")
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
        db = mock.patch.dict(os.environ, {"VIDEO_GEN_DB": str(tmp / "orchestrator.sqlite3")})
        db.start()
        self.addCleanup(db.stop)

    def write_config(self, data: dict) -> None:
        self.config_file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def make_work_dir(self, name: str, job_status: str, error: str = "", pid: int | None = None,
                      meta_age_secs: float = 3600) -> Path:
        wd = self.output_dir / name
        wd.mkdir()
        (wd / "job_config.json").write_text(json.dumps({"title": name}))
        meta = {"work_dir": str(wd), "status": job_status,
                "updated_at": time.time() - meta_age_secs}
        if error:
            meta["error"] = error
        if pid is not None:
            meta["pid"] = pid
        (wd / "job.json").write_text(json.dumps(meta))
        return wd


class IsJobRunningTests(TempConfigCase):
    """A fresh "creating" queue item only counts as a running job while its
    render hasn't already errored/finished."""

    def _creating_item(self, wd: Path | None):
        item = {"id": "q1", "status": "creating", "final_title": "T",
                "created_at": time.time()}
        if wd is not None:
            item["work_dir"] = str(wd)
        return item

    def test_errored_job_does_not_count_as_running(self):
        wd = self.make_work_dir("errored", "error", error="Scene 8 failed")
        with mock.patch.object(app.yt, "load_queue", return_value=[self._creating_item(wd)]):
            self.assertFalse(app._is_job_running())

    def test_live_job_still_counts_as_running(self):
        wd = self.make_work_dir("live", "running", pid=os.getpid())
        with mock.patch.object(app.yt, "load_queue", return_value=[self._creating_item(wd)]):
            self.assertTrue(app._is_job_running())

    def test_creating_without_work_dir_counts_as_running(self):
        # Script generation in flight — no job dir yet, must still block starts.
        with mock.patch.object(app.yt, "load_queue", return_value=[self._creating_item(None)]):
            self.assertTrue(app._is_job_running())


class ReconcileFailedRenderTests(TempConfigCase):
    """_reconcile_queue must fail "creating" items whose render errored — even
    when theirs is the latest (active) work dir, which is the common case."""

    def _reconcile(self, queue):
        saved = []
        with mock.patch.object(backend.yt, "load_queue", return_value=queue), \
             mock.patch.object(backend.yt, "save_queue", side_effect=saved.append):
            out = backend._reconcile_queue()
        return out, saved

    def test_errored_active_job_marks_item_failed(self):
        wd = self.make_work_dir("errored", "error", error="Scene 8 failed after 3 attempts")
        item = {"id": "q1", "status": "creating", "final_title": "T", "work_dir": str(wd)}
        out, saved = self._reconcile([item])
        self.assertEqual(out[0]["status"], "failed")
        self.assertIn("Scene 8 failed", out[0]["error"])
        self.assertTrue(saved, "reconcile must persist the repaired row")

    def test_dead_process_marks_item_failed_after_grace(self):
        wd = self.make_work_dir("dead", "running", pid=2 ** 30, meta_age_secs=3600)
        item = {"id": "q1", "status": "creating", "final_title": "T", "work_dir": str(wd)}
        out, _ = self._reconcile([item])
        self.assertEqual(out[0]["status"], "failed")

    def test_cancelled_job_closes_item_as_cancelled(self):
        # on_cancel_active_job stamps the job "cancelled" (deliberately not
        # "error", so nothing auto-retries it) — the queue row must close too,
        # not sit in "creating" showing a phantom render forever.
        wd = self.make_work_dir("stopped", "cancelled")
        item = {"id": "q1", "status": "creating", "final_title": "T", "work_dir": str(wd)}
        out, saved = self._reconcile([item])
        self.assertEqual(out[0]["status"], "cancelled")
        self.assertNotIn("error", out[0])
        self.assertTrue(saved, "reconcile must persist the repaired row")

    def test_just_launched_job_is_left_alone(self):
        # Dead pid but job.json written seconds ago: the relaunch window.
        wd = self.make_work_dir("fresh", "running", pid=2 ** 30, meta_age_secs=5)
        item = {"id": "q1", "status": "creating", "final_title": "T", "work_dir": str(wd)}
        out, _ = self._reconcile([item])
        self.assertEqual(out[0]["status"], "creating")

    def test_stale_scriptless_claim_fails_fresh_one_survives(self):
        stale = {"id": "q1", "status": "creating", "final_title": "Stale",
                 "updated_at": time.time() - 3600}
        fresh = {"id": "q2", "status": "creating", "final_title": "Fresh",
                 "updated_at": time.time()}
        out, _ = self._reconcile([stale, fresh])
        by_id = {i["id"]: i for i in out}
        self.assertEqual(by_id["q1"]["status"], "failed")
        self.assertEqual(by_id["q2"]["status"], "creating")


class AutoRetryFailedTests(TempConfigCase):
    """_auto_start_best retries failed items when nothing else is startable."""

    def _config(self, approve=True, ai_ideas=False):
        self.write_config({"youtube_auto_start_job": True,
                           "youtube_auto_approve_script": approve,
                           "youtube_auto_ai_ideas": ai_ideas})

    def _failed_item(self, qid, retry_count=0, **extra):
        # approved=True: a failed item already passed through "creating", which
        # stamps approved=True, so under the review gate it stays retry-eligible.
        return {"id": qid, "status": "failed", "final_title": qid,
                "script_ready": True, "approved": True, "work_dir": f"/tmp/{qid}",
                "video_job_id": f"job-{qid}", "retry_count": retry_count, **extra}

    def _run(self, queue):
        started, updates = [], []
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=queue), \
             mock.patch.object(backend.yt, "update_queue_item",
                               side_effect=lambda qid, **kw: updates.append((qid, kw)) or True), \
             mock.patch.object(backend.gapp, "_auto_pick_suggestion") as pick, \
             mock.patch.object(backend, "_start_queue_item",
                               side_effect=lambda it: started.append(it) or {"ok": True}):
            backend._auto_start_best()
        return started, updates, pick

    def test_failed_item_retries_when_queue_is_idle(self):
        self._config()
        started, updates, _ = self._run([self._failed_item("f1")])
        self.assertEqual([i["id"] for i in started], ["f1"])
        self.assertEqual(updates, [("f1", {"retry_count": 1})])

    def test_pending_item_wins_over_failed(self):
        self._config()
        queue = [self._failed_item("f1"),
                 {"id": "p1", "status": "pending", "final_title": "P"}]
        started, updates, _ = self._run(queue)
        self.assertEqual([i["id"] for i in started], ["p1"])
        self.assertEqual(updates, [])

    def test_retry_cap_leaves_item_failed(self):
        self._config()
        started, updates, _ = self._run([self._failed_item("f1", retry_count=3)])
        self.assertEqual(started, [])
        self.assertEqual(updates, [])

    def test_oldest_failure_retries_first(self):
        self._config()
        a = self._failed_item("f-new", updated_at=200)
        b = self._failed_item("f-old", updated_at=100)
        started, _, _ = self._run([a, b])
        self.assertEqual([i["id"] for i in started], ["f-old"])

    def test_retry_beats_ai_idea_fallback(self):
        self._config(ai_ideas=True)
        started, _, pick = self._run([self._failed_item("f1")])
        self.assertEqual([i["id"] for i in started], ["f1"])
        pick.assert_not_called()

    def test_review_mode_only_retries_reviewed_scripts(self):
        self._config(approve=False)
        scriptless = {"id": "f1", "status": "failed", "final_title": "F"}
        # script_ready and linked, but never Approved: the gate still skips it.
        unapproved = dict(self._failed_item("f3"), approved=False)
        started, updates, _ = self._run([scriptless, unapproved])
        self.assertEqual(started, [])
        self.assertEqual(updates, [])
        started, _, _ = self._run([scriptless, self._failed_item("f2")])
        self.assertEqual([i["id"] for i in started], ["f2"])


class FrameClampTests(unittest.TestCase):
    """The failure that started it all: a 42.7 s narration asked LTX for 1092
    frames; LTXVEmptyLatentAudio rejects anything over 1000."""

    def test_long_narration_is_clamped(self):
        self.assertEqual(comfyui._frame_count(126, 43.68), comfyui.LTX_MAX_FRAMES)

    def test_explicit_length_is_clamped(self):
        self.assertEqual(comfyui._frame_count(2000, None), comfyui.LTX_MAX_FRAMES)

    def test_normal_durations_untouched(self):
        self.assertEqual(comfyui._frame_count(126, None), 126)
        self.assertEqual(comfyui._frame_count(126, 5.0), 5 * comfyui.LTX_FPS + 1)


if __name__ == "__main__":
    unittest.main()
