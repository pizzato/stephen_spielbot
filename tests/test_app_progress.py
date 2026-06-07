import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app


class AppProgressTests(unittest.TestCase):
    """Progress polling moved from the removed Gradio ``_poll_job_outputs``
    callback to the FastAPI ``/api/progress`` endpoint (webapp/backend/main.py),
    which is a thin wrapper over these ``app`` helpers:
    ``_preferred_work_dir`` → ``_status_for_work_dir`` → ``_final_path_for_work_dir``.
    """

    def test_progress_falls_back_to_latest_job(self):
        """With no tracked work dir, /api/progress reports the most recent job."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            work_dir = output_dir / "history-test-20260521-010203"
            work_dir.mkdir()
            (work_dir / "progress.json").write_text(
                json.dumps({"pct": 100, "msg": "Done", "ts": 1})
            )
            (work_dir / "combined.mp4").write_bytes(b"x" * 20_000)
            (output_dir / f"{work_dir.name}.mp4").write_bytes(b"x" * 20_000)

            with mock.patch.object(app, "OUTPUT_DIR", output_dir):
                picked = app._preferred_work_dir("")
                pct, msg = app._status_for_work_dir(picked)
                final_path = app._final_path_for_work_dir(picked)

            self.assertEqual(picked, work_dir)
            self.assertEqual(pct, 100.0)
            self.assertIn("Done", msg)
            self.assertTrue(final_path.exists())

    def test_progress_honors_explicit_active_job(self):
        """An explicitly tracked work dir is honored even when a newer job exists.

        The Gradio-era behaviour of auto-jumping to the newest job was removed:
        the React client passes the work dir it is tracking and the server keeps
        it. Only an empty/missing tracked dir falls back to the latest job.
        """
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            old_dir = output_dir / "old-job-20260521-010203"
            new_dir = output_dir / "new-job-20260521-020304"
            old_dir.mkdir()
            new_dir.mkdir()
            (old_dir / "progress.json").write_text(
                json.dumps({"pct": 100, "msg": "Old done", "ts": 1})
            )
            (new_dir / "progress.json").write_text(
                json.dumps({"pct": 5, "msg": "New running", "ts": 2})
            )
            os.utime(old_dir / "progress.json", (1, 1))
            os.utime(new_dir / "progress.json", (10, 10))

            with mock.patch.object(app, "OUTPUT_DIR", output_dir):
                self.assertEqual(app._latest_work_dir(), new_dir)
                # Explicitly tracked dir wins, even though new_dir is newer.
                self.assertEqual(app._preferred_work_dir(str(old_dir)), old_dir)
                # Empty or missing tracked dir falls back to the latest job.
                self.assertEqual(app._preferred_work_dir(""), new_dir)
                self.assertEqual(
                    app._preferred_work_dir(str(output_dir / "gone")), new_dir
                )


if __name__ == "__main__":
    unittest.main()
