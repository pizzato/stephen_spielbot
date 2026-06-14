import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend


def _make_script(title="Old Title", style_name="docu"):
    """Temp work dir registered in the durable store as a script in review."""
    wd = Path(tempfile.mkdtemp(prefix="spielbot-test-cover-"))
    job_id = backend.job_id_from_work_dir(wd)
    store = backend.DurableStore.default()
    try:
        store.create_or_update_job(
            job_id, wd, title,
            config={"video_title": title, "phase": "script_review", "style_name": style_name},
            metadata={"scene_count": 6, "style": "noir"},
        )
    finally:
        store.close()
    return wd


class CoverSaveTests(unittest.TestCase):
    def test_persists_title_and_description(self):
        wd = _make_script()
        backend.yt_post_save(backend.CoverSaveBody(
            work_dir=str(wd), title="  New Title  ", description="Line one\nLine two"))
        # Title is read back from the durable record (trimmed)...
        self.assertEqual(backend._video_title_for(wd), "New Title")
        # ...and the description lands verbatim in description.txt.
        self.assertEqual((wd / "description.txt").read_text(), "Line one\nLine two")

    def test_keeps_other_config_fields(self):
        wd = _make_script(style_name="docu")
        backend.yt_post_save(backend.CoverSaveBody(work_dir=str(wd), title="Renamed"))
        # The merge must not clobber style_name/phase the upsert would overwrite.
        self.assertEqual(backend._work_dir_style_name(wd), "docu")

    def test_syncs_pending_queue_item(self):
        wd = _make_script()
        queue = [{"id": "q1", "status": "pending", "final_title": "Old Title"}]
        with mock.patch.object(backend.yt, "load_queue", return_value=queue), \
             mock.patch.object(backend.yt, "update_queue_item") as upd:
            backend.yt_post_save(backend.CoverSaveBody(
                work_dir=str(wd), title="Synced", queue_item_id="q1"))
        upd.assert_called_once_with("q1", final_title="Synced")

    def test_skips_queue_sync_when_not_pending(self):
        wd = _make_script()
        queue = [{"id": "q1", "status": "rendering", "final_title": "Old Title"}]
        with mock.patch.object(backend.yt, "load_queue", return_value=queue), \
             mock.patch.object(backend.yt, "update_queue_item") as upd:
            backend.yt_post_save(backend.CoverSaveBody(
                work_dir=str(wd), title="Synced", queue_item_id="q1"))
        upd.assert_not_called()

    def test_blank_title_only_saves_description(self):
        wd = _make_script(title="Keep Me")
        backend.yt_post_save(backend.CoverSaveBody(
            work_dir=str(wd), title="   ", description="just a desc"))
        # An empty title must not wipe the stored one.
        self.assertEqual(backend._video_title_for(wd), "Keep Me")
        self.assertEqual((wd / "description.txt").read_text(), "just a desc")

    def test_missing_work_dir_404(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.yt_post_save(backend.CoverSaveBody(
                work_dir="/tmp/does-not-exist-spielbot", title="x"))
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
