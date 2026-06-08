import json
import tempfile
import unittest
from pathlib import Path

from pipeline.orchestrator import (
    DurableStore,
    job_id_from_work_dir,
    now_ts,
    task_id,
    timing_signature,
)
from pipeline.timing import estimate_eta, humanize_eta


def _task(kind, status="queued", started_at=None, **payload):
    return {
        "kind": kind,
        "status": status,
        "started_at": started_at,
        "payload_json": json.dumps(payload),
    }


class TimingSignatureTests(unittest.TestCase):
    def test_resolution_sensitive_kinds_split_by_dims(self):
        self.assertEqual(
            timing_signature("scene.video.generate", {"vid_width": 1920, "vid_height": 1080}),
            "video|1920x1080",
        )
        self.assertEqual(
            timing_signature("scene.image.generate", {"vid_width": 832, "vid_height": 480}),
            "image|832x480",
        )

    def test_flat_kinds_ignore_resolution(self):
        self.assertEqual(timing_signature("scene.narration.generate", {"vid_width": 1920}), "narration")
        self.assertEqual(timing_signature("scene.video.mux", {}), "mux")
        self.assertEqual(timing_signature("music.generate", {}), "music")

    def test_untracked_kind_returns_none(self):
        self.assertIsNone(timing_signature("story.ready", {"work_dir": "/x"}))

    def test_missing_dims_use_unknown_bucket(self):
        self.assertEqual(timing_signature("scene.video.generate", {}), "video|unknown")


class TimingLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DurableStore(Path(self.tmp.name) / "orchestrator.sqlite3")
        self.work_dir = Path(self.tmp.name) / "job"
        self.work_dir.mkdir()
        self.job_id = job_id_from_work_dir(self.work_dir)
        self.store.ensure_generation_plan(
            self.job_id,
            self.work_dir,
            "Demo",
            [{"id": 1, "title": "S1", "narration": "Hi."}],
            {"vid_width": 1920, "vid_height": 1080},
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _set_started(self, tid, seconds_ago):
        self.store._conn.execute(
            "UPDATE tasks SET started_at=? WHERE id=?",
            (now_ts() - seconds_ago, tid),
        )

    def test_completion_records_duration(self):
        tid = task_id(self.job_id, "scene", 1, "image")
        self.store.start_task(tid)            # attempt -> 1, sets started_at
        self._set_started(tid, 30.0)          # pin a known elapsed
        self.store.complete_task(tid)

        table = self.store.timing_table()
        self.assertIn("image|1920x1080", table)
        entry = table["image|1920x1080"]
        self.assertEqual(entry["sample_count"], 1)
        self.assertAlmostEqual(entry["avg_seconds"], 30.0, delta=2.0)

    def test_skipped_task_without_start_is_not_recorded(self):
        # Completing a task that was never started (e.g. reused-artifact skip)
        # has no started_at, so it must not pollute the table.
        tid = task_id(self.job_id, "scene", 1, "narration")
        self.store.complete_task(tid)
        self.assertNotIn("narration", self.store.timing_table())

    def test_retry_spanning_completion_is_ignored(self):
        tid = task_id(self.job_id, "scene", 1, "image")
        # Simulate a task on its 2nd attempt — elapsed would span the failed run.
        self.store._conn.execute(
            "UPDATE tasks SET attempt=2, started_at=? WHERE id=?",
            (now_ts() - 999.0, tid),
        )
        self.store.complete_task(tid)
        self.assertNotIn("image|1920x1080", self.store.timing_table())

    def test_running_mean_blends_samples(self):
        payload = {"vid_width": 1920, "vid_height": 1080}
        for s in (100.0, 100.0, 100.0):
            self.store.record_timing_sample("scene.video.generate", payload, s)
        entry = self.store.timing_table()["video|1920x1080"]
        self.assertEqual(entry["sample_count"], 3)
        self.assertAlmostEqual(entry["avg_seconds"], 100.0, delta=0.01)


class EstimateEtaTests(unittest.TestCase):
    def setUp(self):
        self.table = {
            "video|1920x1080": {"kind": "video", "avg_seconds": 100.0, "sample_count": 5},
            "narration": {"kind": "narration", "avg_seconds": 10.0, "sample_count": 5},
            "finalize|1920x1080": {"kind": "finalize", "avg_seconds": 30.0, "sample_count": 2},
        }
        self.cfg = {"comfy_workers": ["a", "b"], "tts_workers": ["x"]}  # 2 comfy, 1 tts

    def _job(self):
        dims = {"vid_width": 1920, "vid_height": 1080}
        return (
            [_task("scene.video.generate", **dims) for _ in range(4)]
            + [_task("scene.narration.generate", **dims) for _ in range(2)]
            + [_task("video.finalize", **dims)]
        )

    def test_divides_comfy_work_across_workers(self):
        # comfy: 4*100/2 = 200 ; tts: 2*10/1 = 20 ; finalize tail: 30 -> 230
        eta = estimate_eta(self._job(), self.table, self.cfg, now=1000.0)
        self.assertEqual(eta["eta_seconds"], 230)
        self.assertEqual(eta["total_seconds"], 230)
        self.assertEqual(eta["confidence"], "learned")
        self.assertEqual(eta["workers"], {"comfy": 2, "tts": 1})

    def test_more_workers_is_faster(self):
        fast = estimate_eta(self._job(), self.table, {"comfy_workers": ["a", "b", "c", "d"], "tts_workers": ["x"]}, now=1000.0)
        # comfy: 4*100/4 = 100 ; + finalize 30 -> 130
        self.assertEqual(fast["eta_seconds"], 130)

    def test_remaining_shrinks_as_tasks_finish(self):
        tasks = self._job()
        tasks[0]["status"] = "succeeded"
        tasks[1]["status"] = "succeeded"
        eta = estimate_eta(tasks, self.table, self.cfg, now=1000.0)
        # comfy remaining: 2*100/2 = 100 ; + finalize 30 -> 130, total still 230
        self.assertEqual(eta["eta_seconds"], 130)
        self.assertEqual(eta["total_seconds"], 230)

    def test_running_task_counts_only_remainder(self):
        tasks = [_task("scene.video.generate", status="running", started_at=960.0,
                       vid_width=1920, vid_height=1080)]
        # 100s est, 40s elapsed by now=1000 -> 60s remaining, 1 comfy worker
        eta = estimate_eta(tasks, self.table, {"comfy_workers": ["a"], "tts_workers": ["x"]}, now=1000.0)
        self.assertEqual(eta["eta_seconds"], 60)

    def test_missing_samples_flagged_rough(self):
        eta = estimate_eta(self._job(), {}, self.cfg, now=1000.0)
        self.assertEqual(eta["confidence"], "rough")
        self.assertGreater(eta["eta_seconds"], 0)

    def test_breakdown_table_groups_by_label(self):
        rows = {r["label"]: r for r in estimate_eta(self._job(), self.table, self.cfg, now=1000.0)["table"]}
        self.assertEqual(rows["video @ 1920×1080"]["count"], 4)
        self.assertEqual(rows["video @ 1920×1080"]["seconds"], 100)
        self.assertFalse(rows["video @ 1920×1080"]["estimated"])

    def test_no_trackable_tasks_returns_none(self):
        self.assertIsNone(estimate_eta([_task("story.ready")], self.table, self.cfg))


class HumanizeTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(humanize_eta(10), "under a minute")
        self.assertEqual(humanize_eta(120), "~2 min")
        self.assertEqual(humanize_eta(3600), "~1h")
        self.assertEqual(humanize_eta(3900), "~1h 5m")


if __name__ == "__main__":
    unittest.main()
