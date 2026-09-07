"""Edit-screen scene QA marks (issue #383): good / needs_work persist on scene
metadata and survive reload via the film scenes API; unmarked counts as
needs-work for the filter."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app
import webapp.backend.main as backend
from pipeline.orchestrator import DurableStore, job_id_from_work_dir


class SceneQaStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-scene-qa-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.config_file = tmp / "config" / "config.yaml"
        self.config_file.parent.mkdir(parents=True)
        self.voices_dir = self.config_file.parent / "voices"
        self.voices_dir.mkdir()
        self.output_dir = tmp / "videos"
        self.output_dir.mkdir()
        for target, attr, value in [
            (app, "CONFIG_FILE", self.config_file),
            (app, "VOICES_DIR", self.voices_dir),
            (app, "OUTPUT_DIR", self.output_dir),
        ]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        db = mock.patch.dict(os.environ, {"SPIELBOT_ORCHESTRATOR_DB": str(tmp / "orchestrator.sqlite3")})
        db.start()
        self.addCleanup(db.stop)

    def _work_dir(self, name: str = "film") -> Path:
        wd = self.output_dir / name
        wd.mkdir()
        return wd

    def _register_scenes(self, wd: Path, n: int = 2) -> str:
        job_id = job_id_from_work_dir(wd)
        store = DurableStore.default()
        try:
            store.create_or_update_job(
                job_id, wd, "Film", config={"video_title": "Film"}, metadata={})
            for sid in range(1, n + 1):
                store.upsert_scene(
                    job_id,
                    sid,
                    title=f"Scene {sid}",
                    image_prompt="image",
                    video_prompt="video",
                    narration=f"line {sid}",
                )
        finally:
            store.close()
        return job_id

    def test_update_scene_persists_qa_status_and_film_scenes_exposes_it(self):
        wd = self._work_dir()
        job_id = self._register_scenes(wd, n=2)

        backend.update_scene(
            job_id,
            1,
            backend.SceneUpdate(
                title="Scene 1",
                image_prompt="image",
                video_prompt="video",
                narration="line 1",
                qa_status="good",
            ),
        )
        backend.update_scene(
            job_id,
            2,
            backend.SceneUpdate(
                title="Scene 2",
                image_prompt="image",
                video_prompt="video",
                narration="line 2",
                qa_status="needs_work",
            ),
        )

        store = DurableStore.default()
        try:
            self.assertEqual(store.get_scene(job_id, 1)["metadata"].get("qa_status"), "good")
            self.assertEqual(store.get_scene(job_id, 2)["metadata"].get("qa_status"), "needs_work")
        finally:
            store.close()

        snap = json.loads((wd / "script.json").read_text())
        by_id = {int(r["id"]): r for r in snap}
        self.assertEqual(by_id[1]["metadata"].get("qa_status"), "good")
        self.assertEqual(by_id[2]["metadata"].get("qa_status"), "needs_work")

        listed = backend.film_scenes(str(wd))["scenes"]
        by_list = {int(s["id"]): s for s in listed}
        self.assertEqual(by_list[1]["qa_status"], "good")
        self.assertEqual(by_list[2]["qa_status"], "needs_work")

    def test_unmarked_and_cleared_qa_status_read_as_empty(self):
        wd = self._work_dir("cleared")
        job_id = self._register_scenes(wd, n=1)

        listed = backend.film_scenes(str(wd))["scenes"]
        self.assertEqual(listed[0]["qa_status"], "")

        backend.update_scene(
            job_id,
            1,
            backend.SceneUpdate(
                title="Scene 1",
                image_prompt="image",
                video_prompt="video",
                narration="line 1",
                qa_status="good",
            ),
        )
        backend.update_scene(
            job_id,
            1,
            backend.SceneUpdate(
                title="Scene 1",
                image_prompt="image",
                video_prompt="video",
                narration="line 1",
                qa_status="",
            ),
        )
        store = DurableStore.default()
        try:
            self.assertNotIn("qa_status", store.get_scene(job_id, 1).get("metadata") or {})
        finally:
            store.close()
        self.assertEqual(backend.film_scenes(str(wd))["scenes"][0]["qa_status"], "")

    def test_omitted_qa_status_leaves_existing_mark(self):
        wd = self._work_dir("omit")
        job_id = self._register_scenes(wd, n=1)
        backend.update_scene(
            job_id,
            1,
            backend.SceneUpdate(
                title="Scene 1",
                image_prompt="image",
                video_prompt="video",
                narration="line 1",
                qa_status="good",
            ),
        )
        backend.update_scene(
            job_id,
            1,
            backend.SceneUpdate(
                title="Scene 1",
                image_prompt="image",
                video_prompt="video",
                narration="line 1 changed",
            ),
        )
        store = DurableStore.default()
        try:
            row = store.get_scene(job_id, 1)
            self.assertEqual(row["metadata"].get("qa_status"), "good")
            self.assertEqual(row["narration"], "line 1 changed")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
