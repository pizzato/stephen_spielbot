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
