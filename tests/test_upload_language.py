"""Upload language resolution: the finished video's own narration language
(per-style tts_language stamped into job_config.json at render) labels the
YouTube upload and X caption track, with the channel preference as fallback
for jobs predating the stamp.
"""
import json
import tempfile
import unittest
from pathlib import Path

import webapp.backend.main as backend


class VideoLanguageForWorkDirTests(unittest.TestCase):
    def _wd(self, job_cfg=None):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-lang-"))
        if job_cfg is not None:
            (wd / "job_config.json").write_text(json.dumps(job_cfg))
        return wd

    def test_job_language_wins_over_channel(self):
        wd = self._wd({"tts_language": "pt"})
        self.assertEqual(backend._video_language_for_work_dir(wd, "en"), "pt")

    def test_missing_job_config_falls_back(self):
        wd = self._wd(None)
        self.assertEqual(backend._video_language_for_work_dir(wd, "en"), "en")

    def test_job_without_stamp_falls_back(self):
        wd = self._wd({"resolution": "Portrait 720p"})
        self.assertEqual(backend._video_language_for_work_dir(wd, "de"), "de")

    def test_blank_stamp_falls_back(self):
        wd = self._wd({"tts_language": ""})
        self.assertEqual(backend._video_language_for_work_dir(wd, "en"), "en")


class XSubtitleDisplayNameTests(unittest.TestCase):
    def test_chatterbox_languages_get_real_names(self):
        # "sw" is outside x.py's own label map but named in chatterbox.LANGUAGES.
        from pipeline.chatterbox import LANGUAGES
        self.assertEqual(LANGUAGES.get("sw"), "Swahili")
        import pipeline.x as x
        self.assertNotIn("sw", x._LANGUAGE_DISPLAY_NAMES)


if __name__ == "__main__":
    unittest.main()
