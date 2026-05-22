import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app


class AppProgressTests(unittest.TestCase):
    def test_output_polling_falls_back_to_latest_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            work_dir = output_dir / "history-test-20260521-010203"
            work_dir.mkdir()
            (work_dir / "progress.json").write_text(
                json.dumps({"pct": 100, "msg": "Done", "ts": 1})
            )
            (work_dir / "combined.mp4").write_bytes(b"x" * 20_000)
            (work_dir / "background_music.wav").write_bytes(b"x" * 20_000)
            (output_dir / f"{work_dir.name}.mp4").write_bytes(b"x" * 20_000)

            with mock.patch.object(app, "OUTPUT_DIR", output_dir), \
                 mock.patch.object(app, "save_session"):
                result = app._poll_job_outputs("")

            self.assertEqual(len(result), 7)
            self.assertIn("Done", result[0])
            self.assertEqual(result[6], str(work_dir))

    def test_output_polling_prefers_newer_job_over_stale_active_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            old_dir = output_dir / "old-job-20260521-010203"
            new_dir = output_dir / "new-job-20260521-020304"
            old_dir.mkdir()
            new_dir.mkdir()
            (old_dir / "progress.json").write_text(json.dumps({"pct": 100, "msg": "Old done", "ts": 1}))
            (new_dir / "progress.json").write_text(json.dumps({"pct": 5, "msg": "New running", "ts": 2}))
            os.utime(old_dir / "progress.json", (1, 1))
            os.utime(new_dir / "progress.json", (10, 10))

            with mock.patch.object(app, "OUTPUT_DIR", output_dir), \
                 mock.patch.object(app, "save_session"):
                result = app._poll_job_outputs(str(old_dir))

            self.assertIn("New running", result[0])
            self.assertEqual(result[6], str(new_dir))


if __name__ == "__main__":
    unittest.main()
