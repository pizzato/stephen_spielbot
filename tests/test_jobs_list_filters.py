"""GET /api/jobs carries style_name + channel per finished film and script,
so the Films and Scripts lists can filter by them (URL-persisted filters)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app
import webapp.backend.main as backend


CFG = {
    "styles": [{"name": "Kids", "channel": "UC1"}, {"name": "Docs"}],
    "youtube_channels": [{"id": "UC1", "name": "Kids Channel"}],
}


class JobsListFilterFields(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-jobs-filters-")
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

    def _dir(self, name: str, *, finished: bool, style: str = "") -> Path:
        wd = self.output_dir / name
        wd.mkdir()
        (wd / "script.json").write_text("{}")
        if finished:
            (wd / "combined.mp4").write_bytes(b"c")
            (wd / "job.json").write_text("{}")
        if style:
            (wd / "job_config.json").write_text(json.dumps({"style_name": style}))
        return wd

    def test_finished_and_scripts_carry_style_and_channel(self):
        self._dir("kids-film", finished=True, style="Kids")
        self._dir("draft-script", finished=False, style="Kids")
        out = backend.list_jobs()
        film = next(f for f in out["finished"] if "kids-film" in f["work_dir"])
        self.assertEqual(film["style_name"], "Kids")
        self.assertEqual(film["channel"], "Kids Channel")
        script = next(s for s in out["scripts"] if "draft-script" in s["work_dir"])
        self.assertEqual(script["style_name"], "Kids")
        self.assertEqual(script["channel"], "Kids Channel")

    def test_styleless_job_gets_empty_style_and_fallback_channel(self):
        # No style stamp anywhere → style_name ''; the publish-target channel
        # still resolves via the first-connected-channel fallback.
        self._dir("bare-film", finished=True)
        out = backend.list_jobs()
        film = next(f for f in out["finished"] if "bare-film" in f["work_dir"])
        self.assertEqual(film["style_name"], "")
        self.assertEqual(film["channel"], "Kids Channel")

    def test_no_channels_connected_leaves_channel_empty(self):
        cfg = {"styles": [{"name": "Docs"}], "youtube_channels": []}
        self._dir("solo-film", finished=True, style="Docs")
        with mock.patch.object(app, "load_config", return_value=cfg):
            out = backend.list_jobs()
        film = next(f for f in out["finished"] if "solo-film" in f["work_dir"])
        self.assertEqual(film["channel"], "")


if __name__ == "__main__":
    unittest.main()
