"""The editor's "Upscaled to …" indication (_final_upscale_info).

The scene clips always stay at the render resolution; only the assembled final
is upscaled. The editor APIs report that final-vs-render relation so the UI can
say the film was upscaled — and that a scene re-render doesn't cost the upscale.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app
import webapp.backend.main as backend


class FinalUpscaleInfoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-upscale-info-")
        self.addCleanup(self._tmp.cleanup)
        self.wd = Path(self._tmp.name) / "videos" / "film"
        self.wd.mkdir(parents=True)
        (self.wd / "job_config.json").write_text(json.dumps(
            {"resolution": "Portrait FHD (1080×1920)",
             "vid_width": 1080, "vid_height": 1920}))
        self.final = self.wd / "final.mp4"

    def _info(self, final_dims=None):
        with mock.patch.object(app, "_final_path_for_work_dir",
                               return_value=self.final):
            if final_dims is None:
                return backend._final_upscale_info(self.wd)
            self.final.write_bytes(b"\x00")
            with mock.patch("pipeline.assembler._get_video_dimensions",
                            return_value=final_dims):
                return backend._final_upscale_info(self.wd)

    def test_no_final_reports_not_upscaled(self):
        info = self._info()
        self.assertFalse(info["upscaled"])
        self.assertEqual(info["label"], "")

    def test_final_at_render_size_reports_not_upscaled(self):
        info = self._info((1080, 1920))
        self.assertFalse(info["upscaled"])
        self.assertEqual((info["final_width"], info["final_height"]), (1080, 1920))

    def test_upscaled_final_names_the_tier(self):
        info = self._info((2160, 3840))
        self.assertTrue(info["upscaled"])
        self.assertEqual(info["label"], "Portrait 4K (2160×3840)")

    def test_hand_sized_upscale_falls_back_to_pixels(self):
        info = self._info((1600, 2844))
        self.assertTrue(info["upscaled"])
        self.assertEqual(info["label"], "1600×2844")


if __name__ == "__main__":
    unittest.main()
