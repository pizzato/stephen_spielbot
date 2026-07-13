"""Dialogue rendering is visible to the progress/ETA system.

Locks: the generation plan creates one durable task per dialogue line (instead
of a narration/video/mux quartet that never runs and poisons the ETA), the
timing model knows the "dialogue line" kind and the echomimic worker pool, and
completed line tasks feed the learned table like any other kind.
"""
import tempfile
import unittest
from pathlib import Path

from pipeline.llm import Scene
from pipeline.orchestrator import (
    DurableStore, TIMING_KIND_LABELS, timing_signature,
)
from pipeline.timing import estimate_eta


def _scenes():
    return [
        Scene(id=1, title="talk", image_prompt="i", video_prompt="v", narration="",
              mode="dialogue", lines=[{"speaker": "A", "text": "hi"},
                                      {"speaker": "B", "text": "yo"}]),
        Scene(id=2, title="story", image_prompt="i", video_prompt="v", narration="n"),
    ]


class DialoguePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DurableStore(Path(self.tmp.name) / "orchestrator.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_plan_creates_line_tasks_for_dialogue_scenes(self):
        self.store.ensure_generation_plan(
            "job_x", self.tmp.name, "T", _scenes(),
            {"vid_width": 512, "vid_height": 256})
        rows = self.store.task_rows("job_x")
        kinds = {r["id"]: r["kind"] for r in rows}
        # dialogue scene 1 → two line tasks, and NO narration/video/mux tasks
        line_ids = [i for i, k in kinds.items() if k == "scene.dialogue.line"]
        self.assertEqual(len(line_ids), 2)
        self.assertFalse(any(i.startswith("job_x:scene:1:") and k != "scene.dialogue.line"
                             for i, k in kinds.items()))
        # classic scene 2 keeps the standard quartet
        self.assertIn("job_x:scene:2:narration", kinds)
        self.assertIn("job_x:scene:2:mux", kinds)

    def test_dialogue_scene_has_no_mux_task_so_artifact_must_not_target_it(self):
        # The mux loop must skip dialogue scenes: they have no mux task, and
        # recording a scene_final artifact against a non-existent task id fails
        # the FK (the crash this guards). Reproduce the FK to lock the reason.
        import sqlite3
        self.store.ensure_generation_plan(
            "job_fk", self.tmp.name, "T", _scenes(), {"vid_width": 512, "vid_height": 256})
        mux_id = "job_fk:scene:1:mux"   # dialogue scene 1 → never planned
        self.assertNotIn(mux_id, {r["id"] for r in self.store.task_rows("job_fk")})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_artifact("job_fk", mux_id, "scene_final",
                                       Path(self.tmp.name) / "x.mp4")

    def test_completed_line_feeds_timing_table(self):
        self.store.ensure_generation_plan(
            "job_y", self.tmp.name, "T", _scenes(),
            {"vid_width": 512, "vid_height": 256})
        line_id = next(r["id"] for r in self.store.task_rows("job_y")
                       if r["kind"] == "scene.dialogue.line")
        self.store.start_task(line_id)
        self.store.complete_task(line_id, result={})
        sig = timing_signature("scene.dialogue.line", {"vid_width": 512, "vid_height": 256})
        self.assertEqual(sig, "dialogue line|512x256")
        # the label is registered, so the sample lands in the learned table
        self.assertIn("scene.dialogue.line", TIMING_KIND_LABELS)


class DialogueEtaTests(unittest.TestCase):
    def test_estimate_eta_counts_echomimic_pool(self):
        tasks = [
            {"kind": "scene.dialogue.line", "status": "queued",
             "payload_json": '{"vid_width": 512, "vid_height": 256}'},
            {"kind": "scene.dialogue.line", "status": "queued",
             "payload_json": '{"vid_width": 512, "vid_height": 256}'},
            {"kind": "video.finalize", "status": "queued", "payload_json": "{}"},
        ]
        eta = estimate_eta(tasks, {}, {"comfy_workers": ["a"], "tts_workers": ["b"],
                                       "echomimic_workers": ["c", "d"]})
        self.assertIsNotNone(eta)
        self.assertEqual(eta["workers"]["echomimic"], 2)
        # two seeded lines render serially (~480s each, res-scaled) + finalize
        self.assertGreater(eta["eta_seconds"], 100)
        self.assertTrue(any(r["label"].startswith("dialogue line") for r in eta["table"]))


if __name__ == "__main__":
    unittest.main()
