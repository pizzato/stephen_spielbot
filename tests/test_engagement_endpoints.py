"""Engagement endpoint wiring (issue #50). No network, no model download."""
import os
import tempfile
import unittest
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend


class EngagementEndpointTests(unittest.TestCase):
    def setUp(self):
        backend._engagement_tasks.clear()

    def test_build_spawns_task(self):
        with mock.patch.object(backend.threading, "Thread") as Thread:
            r = backend.engagement_build()
        self.assertTrue(r["ok"])
        self.assertTrue(r["task_id"])
        Thread.assert_called_once()
        self.assertEqual(backend._engagement_tasks[r["task_id"]]["status"], "building")

    def test_build_rejects_when_one_running(self):
        backend._engagement_tasks["x"] = {"status": "building", "phase": "training"}
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.engagement_build()
        self.assertEqual(ctx.exception.status_code, 409)

    def test_build_status_unknown_task(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.engagement_build_status(task_id="nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_run_build_records_done(self):
        with mock.patch.object(backend.eng, "build",
                               return_value={"available": True, "reliability": "ok", "n_samples": 20}), \
             mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/s.json"):
            backend._run_engagement_build("t1")
        self.assertEqual(backend._engagement_tasks["t1"]["status"], "done")
        self.assertTrue(backend._engagement_tasks["t1"]["result"]["available"])

    def test_run_build_records_error_when_unavailable(self):
        with mock.patch.object(backend.eng, "build",
                               return_value={"available": False, "error": "not enough data"}), \
             mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/s.json"):
            backend._run_engagement_build("t2")
        self.assertEqual(backend._engagement_tasks["t2"]["status"], "error")
        self.assertIn("not enough data", backend._engagement_tasks["t2"]["error"])

    def test_predict_forwards_to_engagement(self):
        with mock.patch.object(backend.eng, "predict",
                               return_value={"available": False}) as p:
            out = backend.engagement_predict(backend.EngagementBody(title="x", description="y"))
        self.assertEqual(out, {"available": False})
        p.assert_called_once_with("x", "y")


if __name__ == "__main__":
    unittest.main()
