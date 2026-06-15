"""X posting backend wiring (issue #107): the background post task routes the
right account + premium flag to pipeline.x.post_video, and surfaces skip /
YouTube-link-fallback outcomes. Mirrors UploadRoutingTests. No network."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app as gapp
import webapp.backend.main as backend


def _film_dir() -> tuple[Path, Path]:
    wd = Path(tempfile.mkdtemp(prefix="spielbot-test-film-"))
    final = wd / "final.mp4"
    final.write_bytes(b"\0" * 20_000)
    return wd, final


class XPostTaskTests(unittest.TestCase):
    def _run(self, post_result, premium=True):
        wd, final = _film_dir()
        with mock.patch.object(backend, "_x_client_creds", return_value=("cid", "sec")), \
             mock.patch.object(backend.xt, "check_auth_status",
                               return_value={"connected": True, "premium": premium}), \
             mock.patch.object(backend.xt, "post_video", return_value=post_result) as pv, \
             mock.patch.object(backend.gapp, "_write_job_meta"), \
             mock.patch.object(backend.yt, "update_queue_item"):
            backend._run_x_post_task(
                "t1", {"text": "Hello", "title": "T", "account": "acc1"}, wd, final)
        return pv

    def test_posts_full_and_passes_account_and_premium(self):
        pv = self._run({"tweet_id": "x1", "url": "https://x.com/u/status/x1",
                        "error": "", "fell_back_to_link": False, "skipped": False, "reason": ""},
                       premium=True)
        self.assertEqual(pv.call_args.kwargs.get("account"), "acc1")
        self.assertTrue(pv.call_args.kwargs.get("premium"))
        self.assertEqual(backend._x_post_tasks["t1"]["status"], "done")

    def test_fallback_to_link_reports_done_with_message(self):
        self._run({"tweet_id": "x2", "url": "https://x.com/u/status/x2", "error": "",
                   "fell_back_to_link": True, "skipped": False, "reason": "too long"},
                  premium=False)
        task = backend._x_post_tasks["t1"]
        self.assertEqual(task["status"], "done")
        self.assertTrue(task["fell_back_to_link"])
        self.assertIn("link", task["message"].lower())

    def test_skip_reports_warning(self):
        self._run({"tweet_id": "", "url": "", "error": "Video too long, no link",
                   "fell_back_to_link": False, "skipped": True, "reason": "Video too long, no link"},
                  premium=False)
        task = backend._x_post_tasks["t1"]
        self.assertEqual(task["status"], "warning")
        self.assertIn("too long", task["message"].lower())

    def test_api_error_reports_error(self):
        self._run({"tweet_id": "", "url": "", "error": "401 Unauthorized",
                   "fell_back_to_link": False, "skipped": False, "reason": ""})
        self.assertEqual(backend._x_post_tasks["t1"]["status"], "error")


class FinalizeNewXAccountTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(gapp.DEFAULT_CFG)
        self.cfg["x_accounts"] = []
        self.saved = None

    def _run(self, account_id, username, status=None):
        def save(cfg):
            self.saved = cfg
        with mock.patch.object(backend.gapp, "load_config", return_value=self.cfg), \
             mock.patch.object(backend.gapp, "save_config", side_effect=save), \
             mock.patch.object(backend, "_x_client_creds", return_value=("cid", "sec")), \
             mock.patch.object(backend.xt, "check_auth_status",
                               return_value=status or {"connected": False, "account_id": ""}):
            return backend._finalize_new_x_account(account_id, username)

    def test_new_account_appends_entry(self):
        key = self._run("99", "nine")
        self.assertEqual(key, "99")
        self.assertEqual(self.saved["x_accounts"], [{"id": "99", "name": "nine", "account_id": "99"}])

    def test_reconnect_known_account_reuses_entry(self):
        self.cfg["x_accounts"] = [{"id": "99", "name": "old", "account_id": "99"}]
        key = self._run("99", "newname")
        self.assertEqual(key, "99")
        self.assertEqual(len(self.saved["x_accounts"]), 1)
        self.assertEqual(self.saved["x_accounts"][0]["name"], "newname")


if __name__ == "__main__":
    unittest.main()
