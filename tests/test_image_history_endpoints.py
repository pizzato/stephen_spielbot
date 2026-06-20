"""Endpoint-level tests for image-version history. The ComfyUI image call is mocked
to write a dummy PNG, so these exercise the real seed/record/select wiring in
``_generate_active_scene_preview`` and the select endpoints — not model output."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend


class _FakeStore:
    """Stand-in for DurableStore: serves one scene row, swallows writes."""
    def __init__(self, scene):
        self._scene = scene

    def get_scene(self, job_id, sid):
        return self._scene

    def get_job(self, job_id):
        return None

    def update_scene_preview(self, job_id, sid, path):
        pass

    def close(self):
        pass


class ImageHistoryEndpointTests(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-wd-"))
        self.sid = 1
        self.preview = self.wd / "scene_01_preview.png"

    def _regen(self, image_bytes):
        """Run the real regen endpoint with the image generator mocked to write bytes."""
        def fake_generate(prompt, out, **kw):
            Path(out).write_bytes(image_bytes)

        store = _FakeStore({"image_prompt": "p", "preview_path": str(self.preview)})
        with mock.patch.object(backend.gapp, "_job_work_dir", return_value=self.wd), \
             mock.patch.object(backend.gapp.DurableStore, "default", return_value=store), \
             mock.patch.object(backend.gapp, "_preview_worker_urls", return_value=["http://w"]), \
             mock.patch.object(backend.gapp, "WorkerPool") as WP, \
             mock.patch.object(backend.gapp, "generate_scene_image", side_effect=fake_generate):
            WP.return_value.acquire.return_value = "http://w"
            return backend.regen_scene_preview("job1", self.sid)

    def test_regen_twice_keeps_both_versions_newest_selected(self):
        r1 = self._regen(b"first")
        self.assertEqual(len(r1["history"]["versions"]), 1)

        r2 = self._regen(b"second")
        versions = r2["history"]["versions"]
        self.assertEqual(len(versions), 2)                             # both kept
        self.assertEqual(r2["history"]["selected"], versions[-1]["id"])  # newest selected
        self.assertEqual(self.preview.read_bytes(), b"second")          # canonical = newest

    def test_select_switches_back_to_older_version(self):
        self._regen(b"first")
        r2 = self._regen(b"second")
        older = r2["history"]["versions"][0]["id"]

        with mock.patch.object(backend.gapp, "_job_work_dir", return_value=self.wd), \
             mock.patch.object(backend.DurableStore, "default", return_value=_FakeStore({})):
            sel = backend.select_scene_preview(
                "job1", self.sid, backend.PreviewSelectBody(version_id=older))

        self.assertEqual(sel["history"]["selected"], older)
        self.assertEqual(Path(sel["preview_path"]).read_bytes(), b"first")  # canonical reverted
        self.assertEqual(self.preview.read_bytes(), b"first")

    def test_select_unknown_version_is_404(self):
        self._regen(b"first")
        with mock.patch.object(backend.gapp, "_job_work_dir", return_value=self.wd), \
             mock.patch.object(backend.DurableStore, "default", return_value=_FakeStore({})):
            with self.assertRaises(backend.HTTPException) as ctx:
                backend.select_scene_preview(
                    "job1", self.sid, backend.PreviewSelectBody(version_id=999))
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
