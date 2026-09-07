"""Per-scene review marks in the film editor (issue #383).

Scenes are marked "approved" (signed off) or "todo" (still being worked on),
and an unmarked scene is one still to be reviewed, so the edit of a long film
can be tracked card by card. The marks live in scene_review.json beside the
film — they are notes about the edit, not part of
what renders — and ride back out on GET /api/films/scenes.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

from fastapi import HTTPException

import app
import webapp.backend.main as backend
from pipeline.orchestrator import DurableStore, job_id_from_work_dir


class SceneReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-scene-review-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.output_dir = tmp / "videos"
        self.output_dir.mkdir()
        p = mock.patch.object(app, "OUTPUT_DIR", self.output_dir)
        p.start()
        self.addCleanup(p.stop)
        db = mock.patch.dict(os.environ,
                             {"SPIELBOT_ORCHESTRATOR_DB": str(tmp / "orchestrator.sqlite3")})
        db.start()
        self.addCleanup(db.stop)

        self.wd = self.output_dir / "film"
        self.wd.mkdir()
        job_id = job_id_from_work_dir(self.wd)
        store = DurableStore.default()
        try:
            store.create_or_update_job(job_id, self.wd, "Film",
                                       config={"video_title": "Film"}, metadata={})
            for sid in (1, 2, 3):
                store.upsert_scene(job_id, sid, title=f"Scene {sid}",
                                   narration=f"narration {sid}")
        finally:
            store.close()

    def _mark(self, sid: int, status: str) -> dict:
        return backend.set_film_scene_review(
            sid, backend.FilmSceneReviewBody(work_dir=str(self.wd), status=status))

    def _reviews(self) -> dict:
        return {s["id"]: s["review"] for s in backend.film_scenes(work_dir=str(self.wd))["scenes"]}

    def test_marks_persist_and_ride_back_on_the_scene_list(self):
        self._mark(1, "approved")
        self._mark(3, "todo")

        self.assertEqual(json.loads((self.wd / "scene_review.json").read_text()),
                         {"1": "approved", "3": "todo"})
        # A scene still to be reviewed comes back with an empty mark, not a
        # missing key — the editor reads that as its own category.
        self.assertEqual(self._reviews(), {1: "approved", 2: "", 3: "todo"})

    def test_empty_status_clears_the_mark(self):
        self._mark(2, "approved")
        out = self._mark(2, "")

        self.assertEqual(out["review"], "")
        self.assertEqual(self._reviews()[2], "")
        self.assertEqual(json.loads((self.wd / "scene_review.json").read_text()), {})

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            self._mark(1, "maybe")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_deleting_a_scene_drops_its_mark(self):
        self._mark(1, "approved")
        self._mark(2, "todo")

        backend.delete_film_scene(backend.DeleteFilmSceneBody(work_dir=str(self.wd), scene_id=2))

        self.assertEqual(json.loads((self.wd / "scene_review.json").read_text()), {"1": "approved"})

    def test_unreadable_store_is_ignored(self):
        (self.wd / "scene_review.json").write_text("not json")
        self.assertEqual(self._reviews(), {1: "", 2: "", 3: ""})

    def test_path_outside_the_output_folder_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            backend.set_film_scene_review(
                1, backend.FilmSceneReviewBody(work_dir="/etc", status="approved"))
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
