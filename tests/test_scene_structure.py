"""Scene add / remove / reorder in the script editor, and add-scene in the film
editor (issue #193).

Script-editor structural edits renumber scene ids to 1..N — the pre-render
pipeline treats id order as THE order — so these tests pin the whole contract:
DB rows, script.json, scene_NN_* file renames (two-phase, swap-safe), the
image/video history manifest remaps, and the render-active guard. The film
editor keeps its stable-id + scene_edit_order.json convention, so add there
allocates max(id)+1 and only touches the order sidecar.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

from fastapi import HTTPException

import app
import webapp.backend.main as backend
from pipeline import image_history, video_history
from pipeline.orchestrator import DurableStore, job_id_from_work_dir


class SceneStructureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-scenes-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.config_file = tmp / "config" / "config.yaml"
        self.config_file.parent.mkdir(parents=True)
        self.output_dir = tmp / "videos"
        self.output_dir.mkdir()
        for target, attr, value in [
            (app, "CONFIG_FILE", self.config_file),
            (app, "OUTPUT_DIR", self.output_dir),
        ]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        db = mock.patch.dict(os.environ, {"SPIELBOT_ORCHESTRATOR_DB": str(tmp / "orchestrator.sqlite3")})
        db.start()
        self.addCleanup(db.stop)

    def _register_job(self, n_scenes: int, name: str = "film") -> tuple[Path, str]:
        wd = self.output_dir / name
        wd.mkdir()
        job_id = job_id_from_work_dir(wd)
        store = DurableStore.default()
        try:
            store.create_or_update_job(job_id, wd, "Film", config={"video_title": "Film"}, metadata={})
            for sid in range(1, n_scenes + 1):
                preview = wd / f"scene_{sid:02d}_preview.png"
                preview.write_bytes(f"img-{sid}".encode())
                store.upsert_scene(
                    job_id,
                    sid,
                    title=f"Scene {sid}",
                    image_prompt=f"image {sid}",
                    video_prompt=f"video {sid}",
                    narration=f"narration {sid}",
                    preview_path=preview,
                )
        finally:
            store.close()
        return wd, job_id

    def _rows(self, job_id: str) -> list[dict]:
        store = DurableStore.default()
        try:
            return store.scene_rows(job_id)
        finally:
            store.close()


class ReorderScenesTests(SceneStructureCase):
    def test_reorder_renumbers_rows_files_and_script_json(self):
        wd, job_id = self._register_job(3)
        (wd / "scene_02_final.mp4").write_bytes(b"final-2")
        image_history.record(wd, 1, wd / "scene_01_preview.png")
        video_history.record(wd, 2, wd / "scene_02_final.mp4")

        out = backend.reorder_job_scenes(job_id, backend.SceneReorderBody(order=[3, 1, 2]))

        self.assertEqual([s["title"] for s in out["scenes"]], ["Scene 3", "Scene 1", "Scene 2"])
        self.assertEqual([s["id"] for s in out["scenes"]], [1, 2, 3])

        rows = self._rows(job_id)
        self.assertEqual([r["title"] for r in rows], ["Scene 3", "Scene 1", "Scene 2"])
        script = json.loads((wd / "script.json").read_text())
        self.assertEqual([s["narration"] for s in script],
                         ["narration 3", "narration 1", "narration 2"])

        # Files followed their scenes (two-phase rename — the 3-cycle can't clobber).
        self.assertEqual((wd / "scene_01_preview.png").read_bytes(), b"img-3")
        self.assertEqual((wd / "scene_02_preview.png").read_bytes(), b"img-1")
        self.assertEqual((wd / "scene_03_preview.png").read_bytes(), b"img-2")
        self.assertEqual((wd / "scene_03_final.mp4").read_bytes(), b"final-2")
        self.assertFalse((wd / "scene_02_final.mp4").exists())

        # preview_path rows point at the renamed files.
        self.assertEqual(rows[0]["preview_path"], str(wd / "scene_01_preview.png"))

        # History manifests follow: old scene 1's image history is scene 2's now,
        # old scene 2's video takes are scene 3's, and their files are renamed.
        img_hist = image_history.history(wd, 2)
        self.assertEqual(len(img_hist["versions"]), 1)
        self.assertEqual(Path(img_hist["versions"][0]["path"]).name, "scene_02_v1.png")
        self.assertIn("t", img_hist["versions"][0])
        self.assertEqual(image_history.history(wd, 1)["versions"], [])
        vid_hist = video_history.history(wd, 3)
        self.assertEqual(len(vid_hist["versions"]), 1)
        self.assertEqual(Path(vid_hist["versions"][0]["path"]).name, "scene_03_v1.mp4")

    def test_reorder_swap_is_collision_safe(self):
        wd, job_id = self._register_job(2)
        backend.reorder_job_scenes(job_id, backend.SceneReorderBody(order=[2, 1]))
        self.assertEqual((wd / "scene_01_preview.png").read_bytes(), b"img-2")
        self.assertEqual((wd / "scene_02_preview.png").read_bytes(), b"img-1")

    def test_reorder_must_be_a_permutation(self):
        _, job_id = self._register_job(3)
        for bad in ([1, 2], [1, 2, 2], [1, 2, 4]):
            with self.assertRaises(HTTPException) as ctx:
                backend.reorder_job_scenes(job_id, backend.SceneReorderBody(order=bad))
            self.assertEqual(ctx.exception.status_code, 400)

    def test_reorder_retires_film_order_sidecar(self):
        wd, job_id = self._register_job(2)
        (wd / "scene_edit_order.json").write_text("[2, 1]")
        backend.reorder_job_scenes(job_id, backend.SceneReorderBody(order=[2, 1]))
        self.assertFalse((wd / "scene_edit_order.json").exists())

    def test_structural_edit_blocked_while_rendering(self):
        wd, job_id = self._register_job(2)
        (wd / "job_config.json").write_text("{}")
        (wd / "job.json").write_text(json.dumps({"status": "running"}))
        with self.assertRaises(HTTPException) as ctx:
            backend.reorder_job_scenes(job_id, backend.SceneReorderBody(order=[2, 1]))
        self.assertEqual(ctx.exception.status_code, 409)
        # A finished render (combined.mp4 present) is editable again.
        (wd / "combined.mp4").write_bytes(b"x")
        backend.reorder_job_scenes(job_id, backend.SceneReorderBody(order=[2, 1]))


class AddSceneTests(SceneStructureCase):
    def test_add_scene_inserts_blank_after_anchor_and_shifts_files(self):
        wd, job_id = self._register_job(2)
        out = backend.add_job_scene(job_id, backend.SceneAddBody(after_scene_id=1))

        self.assertEqual(out["new_scene_id"], 2)
        self.assertEqual([s["id"] for s in out["scenes"]], [1, 2, 3])
        self.assertEqual([s["title"] for s in out["scenes"]], ["Scene 1", "", "Scene 2"])
        new_scene = out["scenes"][1]
        self.assertEqual(new_scene["narration"], "")
        self.assertFalse(new_scene["has_preview"])

        # Old scene 2's artifacts moved to slot 3; slot 2 starts clean.
        self.assertEqual((wd / "scene_03_preview.png").read_bytes(), b"img-2")
        self.assertFalse((wd / "scene_02_preview.png").exists())
        script = json.loads((wd / "script.json").read_text())
        self.assertEqual([s["id"] for s in script], [1, 2, 3])

    def test_add_scene_appends_without_anchor(self):
        _, job_id = self._register_job(2)
        out = backend.add_job_scene(job_id, backend.SceneAddBody())
        self.assertEqual(out["new_scene_id"], 3)
        self.assertEqual([s["title"] for s in out["scenes"]], ["Scene 1", "Scene 2", ""])


class DeleteSceneTests(SceneStructureCase):
    def test_delete_removes_files_and_renumbers_the_rest(self):
        wd, job_id = self._register_job(3)
        image_history.record(wd, 2, wd / "scene_02_preview.png")

        out = backend.delete_job_scene(job_id, 2)

        self.assertEqual([s["title"] for s in out["scenes"]], ["Scene 1", "Scene 3"])
        self.assertEqual([s["id"] for s in out["scenes"]], [1, 2])
        self.assertEqual((wd / "scene_02_preview.png").read_bytes(), b"img-3")
        self.assertFalse((wd / "scene_03_preview.png").exists())
        # The deleted scene's history entry and version files are gone.
        self.assertEqual(image_history.history(wd, 2)["versions"], [])
        self.assertEqual(list((wd / "image_history").glob("*.png")), [])

    def test_delete_unknown_scene_404s(self):
        _, job_id = self._register_job(2)
        with self.assertRaises(HTTPException) as ctx:
            backend.delete_job_scene(job_id, 9)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_cannot_delete_the_last_scene(self):
        _, job_id = self._register_job(1)
        with self.assertRaises(HTTPException) as ctx:
            backend.delete_job_scene(job_id, 1)
        self.assertEqual(ctx.exception.status_code, 400)


class AddFilmSceneTests(SceneStructureCase):
    def test_add_film_scene_allocates_next_id_and_orders_it(self):
        wd, job_id = self._register_job(2)
        (wd / "scene_edit_order.json").write_text("[2, 1]")

        out = backend.add_film_scene(backend.AddFilmSceneBody(
            work_dir=str(wd), after_scene_id=2))

        self.assertEqual(out["scene_id"], 3)
        self.assertEqual(out["order"], [2, 3, 1])
        rows = self._rows(job_id)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[2]["title"], "")
        script = json.loads((wd / "script.json").read_text())
        self.assertEqual([s["id"] for s in script], [1, 2, 3])
        # Scene ids stay stable — nothing was renamed.
        self.assertEqual((wd / "scene_01_preview.png").read_bytes(), b"img-1")

        # The film editor lists it in display order, last-but-one.
        loaded = backend.film_scenes(work_dir=str(wd))
        self.assertEqual([s["id"] for s in loaded["scenes"]], [2, 3, 1])

    def test_add_film_scene_appends_by_default(self):
        wd, _ = self._register_job(2)
        out = backend.add_film_scene(backend.AddFilmSceneBody(work_dir=str(wd)))
        self.assertEqual(out["order"], [1, 2, 3])

    def test_add_film_scene_heals_store_from_script_json(self):
        wd = self.output_dir / "legacy"
        wd.mkdir()
        (wd / "script.json").write_text(json.dumps([
            {"id": 1, "title": "One", "image_prompt": "i", "video_prompt": "v", "narration": "n"},
            {"id": 2, "title": "Two", "image_prompt": "i", "video_prompt": "v", "narration": "n"},
        ]))
        out = backend.add_film_scene(backend.AddFilmSceneBody(work_dir=str(wd)))
        self.assertEqual(out["scene_id"], 3)
        rows = self._rows(job_id_from_work_dir(wd))
        self.assertEqual([r["title"] for r in rows], ["One", "Two", ""])


if __name__ == "__main__":
    unittest.main()
