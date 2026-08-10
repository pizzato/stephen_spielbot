import tempfile
import unittest
from pathlib import Path

from pipeline.orchestrator import (
    TASK_LOST,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    DurableStore,
    TaskRun,
    job_id_from_work_dir,
    now_ts,
    task_id,
    worker_id,
)


class DurableStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = DurableStore(self.root / "orchestrator.sqlite3")
        self.work_dir = self.root / "job"
        self.work_dir.mkdir()
        self.job_id = job_id_from_work_dir(self.work_dir)
        self.scenes = [
            {
                "id": 1,
                "title": "Scene 1",
                "image_prompt": "A still image",
                "video_prompt": "A slow camera move",
                "narration": "Narration text.",
            },
            {
                "id": 2,
                "title": "Scene 2",
                "image_prompt": "Another still image",
                "video_prompt": "Another slow camera move",
                "narration": "More narration.",
            },
        ]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_generation_plan_creates_dag(self):
        self.store.ensure_generation_plan(
            self.job_id,
            self.work_dir,
            "Demo",
            self.scenes,
            {
                "vid_width": 1024,
                "vid_height": 576,
                "max_clip_secs": 12,
                "lora_strength": 0.5,
                "voice_ref": "/tmp/voice.wav",
                "resource_classes": {
                    "image": "comfy:image",
                    "music": "comfy:music",
                    "video": "comfy:video",
                    "narration": "tts",
                    "finalize": "local",
                },
            },
        )

        rows = self.store.task_rows(self.job_id)
        self.assertEqual(len(rows), 11)
        statuses = {row["id"]: row["status"] for row in rows}
        self.assertEqual(statuses[task_id(self.job_id, "story")], TASK_SUCCEEDED)

        counts = self.store.job_summary(self.job_id)["counts"]
        self.assertEqual(counts[TASK_SUCCEEDED], 1)
        self.assertEqual(counts["queued"], 10)

        video = self.store.get_task(task_id(self.job_id, "scene", 1, "video"))
        self.assertEqual(video.payload["vid_width"], 1024)
        self.assertEqual(video.payload["vid_height"], 576)
        self.assertEqual(video.payload["max_clip_secs"], 12)
        self.assertEqual(video.payload["resource_class"], "comfy:video")
        narration = self.store.get_task(task_id(self.job_id, "scene", 1, "narration"))
        self.assertEqual(narration.payload["voice_ref"], "/tmp/voice.wav")
        self.assertEqual(narration.payload["resource_class"], "tts")
        music = self.store.get_task(task_id(self.job_id, "music"))
        self.assertEqual(music.payload["resource_class"], "comfy:music")

    def test_scenes_are_persisted_independently_of_ui_state(self):
        self.store.create_or_update_job(self.job_id, self.work_dir, "Demo", {}, {"scene_count": 2})
        self.store.upsert_scenes(self.job_id, self.scenes)

        first = self.store.get_scene(self.job_id, 1)
        second = self.store.get_scene(self.job_id, 2)
        self.assertEqual(first["title"], "Scene 1")
        self.assertEqual(second["image_prompt"], "Another still image")

        self.store.upsert_scene(
            self.job_id,
            1,
            title="Edited Scene",
            image_prompt="Edited image",
            video_prompt="Edited video",
            narration="Edited narration",
            preview_path=self.work_dir / "scene_01_preview.png",
        )
        edited = self.store.get_scene(self.job_id, 1)
        self.assertEqual(edited["title"], "Edited Scene")
        self.assertEqual(edited["preview_path"], str(self.work_dir / "scene_01_preview.png"))

        rows = self.store.scene_rows(self.job_id)
        self.assertEqual([row["id"] for row in rows], [1, 2])

    def test_generation_plan_preserves_script_previews_and_completes_image_tasks(self):
        self.store.create_or_update_job(self.job_id, self.work_dir, "Demo", {}, {"scene_count": 1})
        preview = self.work_dir / "scene_01_preview.png"
        preview.write_bytes(b"preview")
        self.store.upsert_scene(
            self.job_id,
            1,
            title="Scene 1",
            image_prompt="A still image",
            video_prompt="A slow camera move",
            narration="Narration text.",
            preview_path=preview,
        )

        self.store.ensure_generation_plan(self.job_id, self.work_dir, "Demo", self.scenes[:1], {})

        scene = self.store.get_scene(self.job_id, 1)
        image_task = self.store.get_task(task_id(self.job_id, "scene", 1, "image"))
        self.assertEqual(scene["preview_path"], str(preview))
        self.assertEqual(image_task.status, TASK_SUCCEEDED)
        self.assertEqual(image_task.result["path"], str(preview))

    def test_leases_expire_and_can_be_reacquired(self):
        self.store.ensure_generation_plan(self.job_id, self.work_dir, "Demo", self.scenes[:1], {})
        wid = worker_id("comfy", "http://s1:8188")
        self.store.register_worker(wid, "comfy", "http://s1:8188")

        first = self.store.acquire_next_task(wid, "comfy", lease_seconds=1)
        self.assertIsNotNone(first)
        self.assertEqual(first.attempt, 1)

        expired = self.store.expire_leases(before=now_ts() + 5)
        self.assertEqual(expired, 1)
        self.assertEqual(self.store.get_task(first.id).status, TASK_LOST)

        second = self.store.acquire_next_task(wid, "comfy", lease_seconds=1)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.attempt, 2)

    def test_dependencies_gate_downstream_tasks(self):
        self.store.ensure_generation_plan(self.job_id, self.work_dir, "Demo", self.scenes[:1], {})
        comfy = worker_id("comfy", "http://s1:8188")
        tts = worker_id("tts", "s1")
        self.store.register_worker(comfy, "comfy", "http://s1:8188")
        self.store.register_worker(tts, "tts", "s1")

        image = self.store.acquire_next_task(comfy, "comfy")
        narration = self.store.acquire_next_task(tts, "tts")
        self.assertEqual(image.kind, "scene.image.generate")
        self.assertEqual(narration.kind, "scene.narration.generate")
        self.store.complete_task(image.id, result={"path": "image.png"})

        still_blocked = self.store.acquire_next_task(comfy, "comfy")
        self.assertIsNone(still_blocked)

        self.store.complete_task(narration.id, result={"path": "narration.wav", "duration": 8.0})
        next_comfy = self.store.acquire_next_task(comfy, "comfy")
        self.assertEqual(next_comfy.kind, "music.generate")

    def test_task_run_marks_running_and_failure_retryable(self):
        self.store.ensure_generation_plan(self.job_id, self.work_dir, "Demo", self.scenes[:1], {})
        narration_task = task_id(self.job_id, "scene", 1, "narration")

        with self.assertRaises(RuntimeError):
            with TaskRun(self.store, narration_task, worker_id_value="tts_local"):
                self.assertEqual(self.store.get_task(narration_task).status, TASK_RUNNING)
                raise RuntimeError("temporary tts failure")

        row = self.store.get_task(narration_task)
        self.assertEqual(row.status, "failed_retryable")
        self.assertIn("temporary tts failure", row.error)


if __name__ == "__main__":
    unittest.main()


class PerformancePlanTests(unittest.TestCase):
    """Performance films plan one Ref2VA task per scene — no image/narration/
    mux quartet and no music (each clip carries its own audio)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = DurableStore(self.root / "orchestrator.sqlite3")
        self.work_dir = self.root / "job"
        self.work_dir.mkdir()
        self.job_id = job_id_from_work_dir(self.work_dir)
        self.scenes = [
            {"id": 1, "title": "Scene 1", "image_prompt": "", "video_prompt": "<Picture 1> is X",
             "narration": "Spoken words.",
             "metadata": {"mode": "performance", "cast": ["X"], "seconds": 10,
                          "lines": [{"speaker": "X", "text": "Spoken words."}]}},
            {"id": 2, "title": "Scene 2", "image_prompt": "", "video_prompt": "<Picture 1> is X",
             "narration": "More words.",
             "metadata": {"mode": "performance", "cast": ["X"], "seconds": 10,
                          "lines": [{"speaker": "X", "text": "More words."}]}},
        ]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _deps(self, task):
        rows = self.store._conn.execute(
            "SELECT depends_on_id FROM task_dependencies WHERE task_id = ?", (task,)).fetchall()
        return sorted(r["depends_on_id"] for r in rows)

    def _plan(self, scenes=None):
        self.store.ensure_generation_plan(
            self.job_id, self.work_dir, "Demo", scenes or self.scenes,
            {"vid_width": 704, "vid_height": 1280,
             "resource_classes": {"image": "comfy:image", "music": "comfy:music",
                                  "video": "comfy:video", "narration": "tts",
                                  "finalize": "local"}})
        return {row["id"]: row for row in self.store.task_rows(self.job_id)}

    def test_one_task_per_scene_and_no_music(self):
        rows = self._plan()
        kinds = sorted(r["kind"] for r in rows.values())
        self.assertEqual(kinds, ["scene.performance.generate", "scene.performance.generate",
                                 "story.ready", "video.finalize"])
        self.assertIsNone(self.store.get_task(task_id(self.job_id, "music")))
        for sid in (1, 2):
            self.assertIsNone(self.store.get_task(task_id(self.job_id, "scene", sid, "image")))
            self.assertIsNone(self.store.get_task(task_id(self.job_id, "scene", sid, "narration")))
            self.assertIsNone(self.store.get_task(task_id(self.job_id, "scene", sid, "mux")))

    def test_finalize_waits_on_every_scene(self):
        self._plan()
        self.assertEqual(self._deps(task_id(self.job_id, "final")),
                         sorted(task_id(self.job_id, "scene", sid, "performance")
                                for sid in (1, 2)))

    def test_performance_task_payload(self):
        self._plan()
        task = self.store.get_task(task_id(self.job_id, "scene", 1, "performance"))
        self.assertEqual(task.payload["resource_class"], "comfy:video")
        self.assertEqual(task.payload["vid_width"], 704)
        self.assertIn("<Picture 1>", task.payload["video_prompt"])

    def test_narrated_films_are_unchanged(self):
        # A film with no performance scenes must still plan music + the quartet.
        rows = self._plan(scenes=[{"id": 1, "title": "S", "image_prompt": "img",
                                   "video_prompt": "vid", "narration": "words"}])
        self.assertIn(task_id(self.job_id, "music"), rows)
        for part in ("image", "narration", "video", "mux"):
            self.assertIn(task_id(self.job_id, "scene", 1, part), rows)
        self.assertIn(task_id(self.job_id, "music"),
                      self._deps(task_id(self.job_id, "final")))

    def test_mixed_film_keeps_music(self):
        # Only an all-performance film drops the score; a mixed script is a
        # narrated film that happens to contain a performance scene.
        mixed = [self.scenes[0],
                 {"id": 2, "title": "S2", "image_prompt": "img", "video_prompt": "vid",
                  "narration": "words"}]
        rows = self._plan(scenes=mixed)
        self.assertIn(task_id(self.job_id, "music"), rows)
        self.assertIn(task_id(self.job_id, "scene", 1, "performance"), rows)
        self.assertIn(task_id(self.job_id, "scene", 2, "video"), rows)
