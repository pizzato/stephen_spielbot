"""Dynamic UI-worker reservation (issue #98).

Covers the three pieces that replace the dedicated "ui worker":
  * pipeline.ui_activity   — the cross-process activity signal
  * WorkerPool reserve_check — holding one worker idle for the UI
  * timing.next_worker_free_seconds — the "available in ~X" estimate
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

from pipeline import ui_activity
from pipeline.timing import next_worker_free_seconds
from pipeline.worker_pool import WorkerPool


def _try_acquire(pool: WorkerPool, timeout: float = 0.4):
    """acquire() in a thread; return the URL or None if it blocked past timeout."""
    box: list = []
    t = threading.Thread(target=lambda: box.append(pool.acquire()), daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else None


class UiActivitySignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="spielbot-ui-activity-")) / "ui_activity.json"
        self._orig = ui_activity.ACTIVITY_FILE
        ui_activity.ACTIVITY_FILE = self.tmp

    def tearDown(self):
        ui_activity.ACTIVITY_FILE = self._orig

    def test_unwritten_is_idle(self):
        self.assertEqual(ui_activity.last_activity(), 0.0)
        self.assertFalse(ui_activity.is_active(300))

    def test_mark_then_active_then_lapses(self):
        ui_activity.mark_active(now=1000.0)
        self.assertEqual(ui_activity.last_activity(), 1000.0)
        # within the window
        self.assertTrue(ui_activity.is_active(300, now=1100.0))
        self.assertEqual(ui_activity.idle_seconds(now=1100.0), 100.0)
        self.assertEqual(ui_activity.seconds_until_idle(300, now=1100.0), 200.0)
        # past the window
        self.assertFalse(ui_activity.is_active(300, now=1500.0))
        self.assertEqual(ui_activity.seconds_until_idle(300, now=1500.0), 0.0)

    def test_mark_writes_timestamp_file(self):
        ui_activity.mark_active(now=42.0)
        self.assertEqual(json.loads(self.tmp.read_text())["last_activity"], 42.0)


class WorkerPoolReservationTests(unittest.TestCase):
    """The poller is stopped in each test so reservation is driven only by the
    synchronous acquire()/release() paths — deterministic, no 2s races."""

    def _pool(self, urls, active_flag):
        pool = WorkerPool(urls, reserve_check=lambda: active_flag[0])
        pool.shutdown()  # stop the background poller for determinism
        return pool

    def test_idle_ui_leaves_all_workers_available(self):
        active = [False]
        pool = self._pool(["a", "b"], active)
        self.assertIsNotNone(_try_acquire(pool))
        self.assertIsNotNone(_try_acquire(pool))
        self.assertIsNone(pool.reserved_url)

    def test_active_ui_holds_one_worker(self):
        active = [True]
        pool = self._pool(["a", "b"], active)
        first = _try_acquire(pool)
        self.assertIsNotNone(first)
        # One worker is now held for the UI; a second render acquire must block.
        self.assertIsNotNone(pool.reserved_url)
        self.assertNotEqual(pool.reserved_url, first)
        self.assertIsNone(_try_acquire(pool))

    def test_never_reserves_the_last_worker(self):
        active = [True]
        pool = self._pool(["only"], active)
        self.assertEqual(_try_acquire(pool), "only")
        self.assertIsNone(pool.reserved_url)

    def test_pending_reservation_claims_next_release(self):
        active = [False]
        pool = self._pool(["a", "b"], active)
        u1 = _try_acquire(pool)
        u2 = _try_acquire(pool)
        self.assertEqual({u1, u2}, {"a", "b"})  # both busy, none reserved
        self.assertIsNone(pool.reserved_url)

        active[0] = True  # UI wakes mid-render, every worker busy
        pool.release(u1)
        # The freed worker is held for the UI, not handed back to the render.
        self.assertEqual(pool.reserved_url, u1)
        pool.release(u2)
        # Only the non-reserved worker is acquirable now.
        self.assertEqual(_try_acquire(pool), u2)
        self.assertIsNone(_try_acquire(pool))

    def test_reservation_returns_worker_when_ui_goes_idle(self):
        active = [True]
        pool = self._pool(["a", "b"], active)
        held = _try_acquire(pool)
        self.assertIsNotNone(pool.reserved_url)
        # UI goes idle — the next acquire reconciles and frees the reserved one.
        active[0] = False
        pool.release(held)
        self.assertIsNotNone(_try_acquire(pool))
        self.assertIsNotNone(_try_acquire(pool))
        self.assertIsNone(pool.reserved_url)


def _wait_until(pred, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class ReservationIntegrationTests(unittest.TestCase):
    """The seam resume_generation actually wires: a WorkerPool whose reserve_check
    reads the shared activity file, driven by the background poller (not stopped)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="spielbot-ui-int-")) / "ui_activity.json"
        self._orig = ui_activity.ACTIVITY_FILE
        ui_activity.ACTIVITY_FILE = self.tmp

    def tearDown(self):
        ui_activity.ACTIVITY_FILE = self._orig

    def test_poller_reserves_on_activity_and_releases_on_idle(self):
        pool = WorkerPool(["a", "b"], reserve_check=lambda: ui_activity.is_active(300))
        try:
            self.assertIsNone(pool.reserved_url)  # no activity yet
            ui_activity.mark_active()             # UI used → poller reserves one
            self.assertTrue(_wait_until(lambda: pool.reserved_url is not None))
            ui_activity.mark_active(now=time.time() - 10_000)  # lapse the window
            self.assertTrue(_wait_until(lambda: pool.reserved_url is None))
        finally:
            pool.shutdown()


class NextWorkerFreeEtaTests(unittest.TestCase):
    def _video_task(self, status, started_offset, now):
        return {
            "kind": "scene.video.generate",
            "status": status,
            "started_at": (now + started_offset) if started_offset is not None else None,
            "payload_json": json.dumps({"vid_width": 1920, "vid_height": 1080}),
        }

    def test_returns_soonest_running_worker(self):
        now = 10_000.0
        tasks = [
            self._video_task("running", -200.0, now),  # 240 - 200 = ~40 left
            self._video_task("running", -100.0, now),  # 240 - 100 = ~140 left
        ]
        eta = next_worker_free_seconds(tasks, {}, now=now)
        self.assertEqual(eta, 40)

    def test_ignores_non_running_and_non_comfy(self):
        now = 10_000.0
        tasks = [
            self._video_task("succeeded", -10.0, now),
            {"kind": "scene.narration.generate", "status": "running",
             "started_at": now - 5.0, "payload_json": "{}"},
        ]
        self.assertIsNone(next_worker_free_seconds(tasks, {}, now=now))

    def test_none_when_nothing_running(self):
        self.assertIsNone(next_worker_free_seconds([], {}, now=1.0))


if __name__ == "__main__":
    unittest.main()
