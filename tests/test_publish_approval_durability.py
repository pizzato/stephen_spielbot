"""An approval, once given, survives everything that rewrites the publish queue.

Approved films came back unapproved because ``approved`` lived only on the
publish-queue entry, and that file was a lock-free, non-atomic read-modify-write
shared by the automation tick and the request handlers. Two ways it lost the
flag: a reconcile pass saved back a snapshot taken before the approval landed,
and a reader catching the file mid-write got a parse error that ``load_queue``
reported as an empty queue — which then got saved, wiping every entry so the
next scan re-added the films as fresh, unapproved ones.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app
import webapp.backend.main as backend
from pipeline import publish_queue as pq


class QueueStoreTests(unittest.TestCase):
    """The store itself: atomic writes, an honest load, a usable lock."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-pq-store-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "publish_queue.json"
        p = mock.patch.object(pq, "PUBLISH_QUEUE_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def test_missing_file_is_an_empty_queue(self):
        self.assertEqual(pq.load_queue(), [])

    def test_damaged_file_raises_instead_of_reading_as_empty(self):
        # The wipe's first domino: a half-written file must not look like "no
        # entries", or the next save writes that emptiness back.
        self.path.write_text('[{"id": "abc", "work_')
        with self.assertRaises(json.JSONDecodeError):
            pq.load_queue()

    def test_concurrent_approval_survives_a_reconcile_pass(self):
        for i in range(5):
            pq.add_item(f"/videos/film-{i}", title=f"Film {i}")
        ids = [e["id"] for e in pq.load_queue()]
        stop = threading.Event()

        def reconciler():
            # What _reconcile_publish_queue does: load, work, save the whole file.
            while not stop.is_set():
                with pq.locked():
                    snapshot = pq.load_queue()
                    time.sleep(0.001)  # stands in for reading each film's job.json
                    pq.save_queue(snapshot)

        threads = [threading.Thread(target=reconciler) for _ in range(3)]
        for t in threads:
            t.start()
        try:
            for item_id in ids:
                pq.update_item(item_id, approved=True)
                time.sleep(0.002)
        finally:
            stop.set()
            for t in threads:
                t.join()

        final = pq.load_queue()
        self.assertEqual(len(final), 5, "reconcile passes dropped entries")
        self.assertTrue(all(e.get("approved") for e in final),
                        "an approval was overwritten by a stale snapshot")

    def test_a_reader_never_sees_a_half_written_file(self):
        pq.save_queue([{"id": "seed", "work_dir": "/videos/seed", "title": "x" * 500}])
        errors: list[Exception] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    pq.load_queue()
                except Exception as exc:  # a torn read would land here
                    errors.append(exc)

        t = threading.Thread(target=reader)
        t.start()
        try:
            for i in range(200):
                pq.save_queue([{"id": f"e{n}", "work_dir": f"/videos/{n}",
                                "title": "x" * 500} for n in range(i % 20 + 1)])
        finally:
            stop.set()
            t.join()
        self.assertEqual(errors, [], "a reader saw a partially written queue")


class ApprovalOutlivesItsEntryTests(unittest.TestCase):
    """The film carries the decision, so a rebuilt entry can't silently lose it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-pq-approval-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.output_dir = tmp / "videos"
        self.output_dir.mkdir()
        for target, attr, value in [
            (app, "OUTPUT_DIR", self.output_dir),
            (pq, "PUBLISH_QUEUE_PATH", tmp / "publish_queue.json"),
            (pq, "PUBLISH_CLOCK_PATH", tmp / "publish_clock.json"),
        ]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

        self.work_dir = self.output_dir / "why-does-a-thing-20260921-101500"
        self.work_dir.mkdir()
        (self.work_dir / "job.json").write_text(
            json.dumps({"status": "done", "updated_at": time.time()}))
        (self.work_dir / "job_config.json").write_text(
            json.dumps({"video_title": "Why Does A Thing?"}))

        targets = ({"enabled": True, "channel": "UC-test", "status": "pending",
                    "video_id": None, "url": None, "released_at": None,
                    "published_at": None, "error": None},
                   {"enabled": False, "account": "", "status": "skipped",
                    "tweet_id": None, "url": None, "released_at": None,
                    "published_at": None, "error": None})
        p = mock.patch.object(backend, "_publish_targets_for_job", return_value=targets)
        p.start()
        self.addCleanup(p.stop)

    def _approve(self):
        backend.publish_approve(
            backend.PublishApproveBody(work_dir=str(self.work_dir), approved=True))

    def test_approval_is_recorded_on_the_film(self):
        self._approve()
        jc = json.loads((self.work_dir / "job_config.json").read_text())
        self.assertTrue(jc.get("publish_approved"))

    def test_a_rebuilt_entry_comes_back_approved(self):
        self._approve()
        # The entry vanishes — a wipe, a delete, or a hand-edited store.
        pq.save_queue([])
        with mock.patch.object(app, "_list_recent_jobs",
                               return_value=[("job", str(self.work_dir))]):
            backend._enqueue_finished_for_publish(recent_only=False)
        entry = pq.item_by_work_dir(str(self.work_dir))
        self.assertIsNotNone(entry, "the film was not re-enqueued")
        self.assertTrue(entry.get("approved"),
                        "a re-enqueued film came back awaiting approval")

    def test_un_approving_clears_the_record(self):
        self._approve()
        backend.publish_approve(
            backend.PublishApproveBody(work_dir=str(self.work_dir), approved=False))
        jc = json.loads((self.work_dir / "job_config.json").read_text())
        self.assertFalse(jc.get("publish_approved"))
        pq.save_queue([])
        with mock.patch.object(app, "_list_recent_jobs",
                               return_value=[("job", str(self.work_dir))]):
            backend._enqueue_finished_for_publish(recent_only=False)
        self.assertFalse((pq.item_by_work_dir(str(self.work_dir)) or {}).get("approved"))

    def test_films_list_reports_approval_without_a_queue_entry(self):
        self._approve()
        pq.save_queue([])
        state = backend._film_publish_status(
            self.work_dir, {"status": "done"},
            {"publish_require_approval": True})
        self.assertTrue(state.get("approved"))
        self.assertFalse(state.get("awaiting_approval"))


if __name__ == "__main__":
    unittest.main()
