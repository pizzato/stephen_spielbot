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


class WorkerConfigFilterTests(unittest.TestCase):
    """``/api/progress`` lists workers from the durable store's global registry,
    which is append-only and keyed by (kind, endpoint). ``_worker_in_config``
    filters it down to the live config so the render page reflects reality: it
    drops endpoints no longer configured, and the cover agent (``kind="ui"``),
    which is not a render worker — issue #98 removed the dedicated ui_workers
    pool, so a ui registration sits on a render endpoint but never counts as one.
    """

    cfg = {
        "comfy_workers": ["http://s1:8188", "http://s3:8188"],
        "tts_workers": ["s1", "s3"],
    }

    def test_cover_agent_registration_is_hidden_but_its_endpoint_is_kept(self):
        from webapp.backend.main import _worker_in_config

        # The cover agent registers kind="ui" at a render endpoint (comfy[0]); the
        # ui registration is hidden, but that endpoint as a comfy worker is kept.
        self.assertFalse(
            _worker_in_config({"kind": "ui", "endpoint": "http://s1:8188"}, self.cfg)
        )
        self.assertTrue(
            _worker_in_config({"kind": "comfy", "endpoint": "http://s1:8188"}, self.cfg)
        )

    def test_unconfigured_endpoint_is_hidden(self):
        from webapp.backend.main import _worker_in_config

        # s2 is no longer in comfy_workers — its stale registration is dropped.
        self.assertFalse(
            _worker_in_config({"kind": "comfy", "endpoint": "http://s2:8188"}, self.cfg)
        )

    def test_configured_workers_are_kept(self):
        from webapp.backend.main import _worker_in_config

        for kind, endpoint in [
            ("comfy", "http://s1:8188"),
            ("comfy", "http://s3:8188"),
            ("tts", "s3"),
        ]:
            self.assertTrue(
                _worker_in_config({"kind": kind, "endpoint": endpoint}, self.cfg),
                f"{kind} {endpoint} should be kept",
            )

    def test_internal_local_workers_always_kept(self):
        """Internal workers (e.g. the assembler) are never in config — keep them."""
        from webapp.backend.main import _worker_in_config

        self.assertTrue(
            _worker_in_config({"kind": "local", "endpoint": "assembler"}, self.cfg)
        )


if __name__ == "__main__":
    unittest.main()
