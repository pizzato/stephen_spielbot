"""The Activity feed must track every live full-film job, not a guess.

``_live_render_activity_items`` used to report a single item: whatever
``_preferred_work_dir("")`` picked by file mtime. Creating a second film
(script + song generation write its files constantly) flipped that pick, so
the film actually rendering vanished from Activity and the new film showed as
"Rendering film · running · Waiting to start...". These tests pin the fix:
one "running" row per job whose render process is alive, and an honest
"queued" row for a queue item still creating its script/song.
"""
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Before importing the backend: it resolves the config/state paths from HOME
# at import time (same pattern as test_activity_song_ops).
os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend  # noqa: E402


class LiveRenderItemsTests(unittest.TestCase):
    def _dirs(self, tmp: Path) -> tuple[Path, Path]:
        rendering = tmp / "film-rendering-20260818-000001"
        creating = tmp / "film-creating-20260818-000002"
        rendering.mkdir()
        creating.mkdir()
        (rendering / "job.json").write_text(json.dumps(
            {"status": "running", "pid": os.getpid(), "created_at": 1}
        ))
        (rendering / "progress.json").write_text(
            json.dumps({"pct": 40, "msg": "Acted scene 4 of 11", "ts": 1})
        )
        (creating / "script.json").write_text("{}")
        (creating / "progress.json").write_text(
            json.dumps({"pct": 0, "msg": "Waiting to start...", "ts": 2})
        )
        # The film being created has the newest files — the old failure mode.
        for p in rendering.iterdir():
            os.utime(p, (1, 1))
        for p in creating.iterdir():
            os.utime(p, (10, 10))
        return rendering, creating

    def test_running_render_plus_creating_queue_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rendering, creating = self._dirs(out)
            queue = [{"status": "creating", "work_dir": str(creating),
                      "title": "Film B", "updated_at": time.time()}]
            with (mock.patch.object(backend.gapp, "OUTPUT_DIR", out),
                  mock.patch.object(backend.yt, "load_queue", return_value=queue)):
                items = backend._live_render_activity_items()

        self.assertEqual(
            [(i["name"], i["status"], Path(i["work_dir"]).name) for i in items],
            [("Rendering film", "running", rendering.name),
             ("Render queued", "queued", creating.name)],
        )
        # The banner fields come from items[0] — the real render, with its
        # real progress message, not the newcomer's "Waiting to start...".
        self.assertIn("Acted scene 4 of 11", items[0]["detail"])

    def test_creating_queue_item_matching_the_render_is_not_duplicated(self):
        """The running film's own queue item stays 'creating' during its render
        — it must not get a second, queued row."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rendering, _creating = self._dirs(out)
            queue = [{"status": "creating", "work_dir": str(rendering),
                      "title": "Film A", "updated_at": time.time()}]
            with (mock.patch.object(backend.gapp, "OUTPUT_DIR", out),
                  mock.patch.object(backend.yt, "load_queue", return_value=queue)):
                items = backend._live_render_activity_items()

        self.assertEqual([i["work_dir"] for i in items], [str(rendering)])
        self.assertEqual(items[0]["status"], "running")

    def test_no_running_process_falls_back_to_single_recency_pick(self):
        """Script/song generation with no render process yet: the old
        single-item behaviour (recency pick) still surfaces the live work."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            creating = out / "film-creating-20260818-000002"
            creating.mkdir()
            (creating / "progress.json").write_text(
                json.dumps({"pct": 0, "msg": "Waiting to start...", "ts": 2})
            )
            queue = [{"status": "creating", "work_dir": str(creating),
                      "title": "Film B", "updated_at": time.time()}]
            with (mock.patch.object(backend.gapp, "OUTPUT_DIR", out),
                  mock.patch.object(backend.yt, "load_queue", return_value=queue)):
                items = backend._live_render_activity_items()

        self.assertEqual([i["work_dir"] for i in items], [str(creating)])
        self.assertEqual(items[0]["status"], "running")


if __name__ == "__main__":
    unittest.main()
