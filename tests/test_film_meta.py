"""Films-tab library metadata: POST /api/films/meta renames the display title
(never the work folder), stars and archives films. Flags persist in
job_config.json (job.json is rewritten wholesale by the renderer), GET /api/jobs
carries title/starred/archived per finished film, and the auto-publish sweep
skips archived films."""
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


CFG = {
    "styles": [{"name": "Kids", "channel": "UC1"}],
    "youtube_channels": [{"id": "UC1", "name": "Kids Channel"}],
}


class FilmMetaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-film-meta-")
        self.addCleanup(self._tmp.cleanup)
        self.output_dir = Path(self._tmp.name) / "videos"
        self.output_dir.mkdir()
        for p in (
            mock.patch.object(app, "OUTPUT_DIR", self.output_dir),
            mock.patch.object(app, "load_config", return_value=dict(CFG)),
            mock.patch.object(backend.pq, "item_by_work_dir", return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _film(self, name: str) -> Path:
        wd = self.output_dir / name
        wd.mkdir()
        (wd / "script.json").write_text("{}")
        (wd / "combined.mp4").write_bytes(b"c")
        (wd / "job.json").write_text("{}")
        return wd

    def _listed(self, wd: Path) -> dict:
        out = backend.list_jobs()
        return next(f for f in out["finished"] if f["work_dir"] == str(wd))

    def test_star_and_archive_persist_in_job_config(self):
        wd = self._film("kids-film")
        backend.set_film_meta(backend.FilmMetaBody(work_dir=str(wd), starred=True))
        backend.set_film_meta(backend.FilmMetaBody(work_dir=str(wd), archived=True))
        jc = json.loads((wd / "job_config.json").read_text())
        self.assertTrue(jc["starred"])
        self.assertTrue(jc["archived"])
        film = self._listed(wd)
        self.assertTrue(film["starred"])
        self.assertTrue(film["archived"])
        # And back off again.
        backend.set_film_meta(backend.FilmMetaBody(
            work_dir=str(wd), starred=False, archived=False))
        film = self._listed(wd)
        self.assertFalse(film["starred"])
        self.assertFalse(film["archived"])

    def test_title_defaults_to_folder_label(self):
        wd = self._film("kids-film-20260101-120000")
        film = self._listed(wd)
        self.assertEqual(film["title"], film["label"])

    def test_rename_updates_title_everywhere(self):
        wd = self._film("kids-film")
        backend.set_film_meta(backend.FilmMetaBody(
            work_dir=str(wd), title="A Better Name"))
        jc = json.loads((wd / "job_config.json").read_text())
        self.assertEqual(jc["video_title"], "A Better Name")
        film = self._listed(wd)
        self.assertEqual(film["title"], "A Better Name")
        self.assertNotEqual(film["label"], "A Better Name")  # folder untouched
        # The durable record took it too — publish prefill reads this back.
        self.assertEqual(backend._video_title_for(wd), "A Better Name")
        self.assertTrue((wd / "combined.mp4").exists())
        self.assertEqual(wd.name, "kids-film")

    def test_rename_rejects_empty_title(self):
        wd = self._film("kids-film")
        with self.assertRaises(HTTPException):
            backend.set_film_meta(backend.FilmMetaBody(work_dir=str(wd), title="   "))

    def test_meta_rejects_paths_outside_output(self):
        with self.assertRaises(HTTPException):
            backend.set_film_meta(backend.FilmMetaBody(work_dir="/etc", starred=True))
        with self.assertRaises(HTTPException):
            backend.set_film_meta(backend.FilmMetaBody(
                work_dir=str(self.output_dir), starred=True))

    def test_publish_sweep_skips_archived(self):
        wd = self._film("kids-film")
        (wd / "job.json").write_text(json.dumps({"status": "done"}))
        targets = ({"enabled": True}, {"enabled": False})
        with mock.patch.object(app, "_list_recent_jobs",
                               return_value=[("Kids Film", str(wd))]), \
             mock.patch.object(backend, "_publish_targets_for_job",
                               return_value=targets), \
             mock.patch.object(backend.pq, "add_item", return_value=True) as add:
            backend.set_film_meta(backend.FilmMetaBody(work_dir=str(wd), archived=True))
            self.assertEqual(backend._enqueue_finished_for_publish(recent_only=False), 0)
            add.assert_not_called()
            backend.set_film_meta(backend.FilmMetaBody(work_dir=str(wd), archived=False))
            self.assertEqual(backend._enqueue_finished_for_publish(recent_only=False), 1)
            add.assert_called_once()


if __name__ == "__main__":
    unittest.main()
