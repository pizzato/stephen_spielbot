"""Acted (dialogue) rendering is visible to the progress/ETA system.

Locks: the generation plan creates ONE durable task per acted scene (instead of
a narration/video/mux quartet that never runs and poisons the ETA), the timing
model knows the "acted scene" kind, and completed scene tasks feed the learned
table like any other kind.
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

    def test_plan_creates_one_task_per_acted_scene(self):
        self.store.ensure_generation_plan(
            "job_x", self.tmp.name, "T", _scenes(),
            {"vid_width": 512, "vid_height": 256})
        rows = self.store.task_rows("job_x")
        kinds = {r["id"]: r["kind"] for r in rows}
        # dialogue scene 1 → ONE acted task, and NO narration/video/mux tasks
        acted = [i for i, k in kinds.items() if k == "scene.performance.generate"]
        self.assertEqual(len(acted), 1)
        self.assertFalse(any(i.startswith("job_x:scene:1:") and k != "scene.performance.generate"
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

    def test_completed_acted_scene_feeds_timing_table(self):
        self.store.ensure_generation_plan(
            "job_y", self.tmp.name, "T", _scenes(),
            {"vid_width": 512, "vid_height": 256})
        acted_id = next(r["id"] for r in self.store.task_rows("job_y")
                        if r["kind"] == "scene.performance.generate")
        self.store.start_task(acted_id)
        self.store.complete_task(acted_id, result={})
        sig = timing_signature("scene.performance.generate",
                               {"vid_width": 512, "vid_height": 256})
        self.assertEqual(sig, "acted scene|512x256")
        # the label is registered, so the sample lands in the learned table
        self.assertIn("scene.performance.generate", TIMING_KIND_LABELS)


class DialogueEtaTests(unittest.TestCase):
    def test_acted_scenes_share_the_comfy_pool(self):
        # Acted scenes render on ComfyUI like any other clip, so they queue
        # behind the image/video work rather than on a pool of their own.
        tasks = [
            {"kind": "scene.performance.generate", "status": "queued",
             "payload_json": '{"vid_width": 512, "vid_height": 256, "scene_id": 1}'},
            {"kind": "scene.performance.generate", "status": "queued",
             "payload_json": '{"vid_width": 512, "vid_height": 256, "scene_id": 2}'},
            {"kind": "video.finalize", "status": "queued", "payload_json": "{}"},
        ]
        one = estimate_eta(tasks, {}, {"comfy_workers": ["a"], "tts_workers": ["b"]})
        two = estimate_eta(tasks, {}, {"comfy_workers": ["a", "c"], "tts_workers": ["b"]})
        self.assertIsNotNone(one)
        self.assertNotIn("echomimic", one["workers"])
        self.assertTrue(any(r["label"].startswith("acted scene") for r in one["table"]))
        # a second render worker roughly halves the acted wall
        self.assertLess(two["eta_seconds"], one["eta_seconds"])


if __name__ == "__main__":
    unittest.main()
