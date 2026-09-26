"""Manual publishing is durable, per-account, and independent of the scheduler."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

import webapp.backend.main as backend
from pipeline import instagram as ig


class InstagramAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wd = self.root / "film"
        self.wd.mkdir()
        self.video = self.wd / "final.mp4"
        self.video.write_bytes(b"video")
        for patch in (
            mock.patch.object(backend.gapp, "OUTPUT_DIR", self.root),
            mock.patch.object(backend.gapp, "_final_path_for_work_dir", return_value=self.video),
            mock.patch.object(ig, "list_accounts", return_value=[{"id": "123", "name": "studio"}, {"id": "456", "name": "second"}]),
            mock.patch.object(backend, "_track_op", return_value=mock.MagicMock()),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def body(self, **kwargs):
        return backend.InstagramPostBody(**{"work_dir": str(self.wd), "account": "123", "caption": "Hello", **kwargs})

    def test_success_persists_link_and_library_destination_without_touching_queue(self):
        def publish(_path, _caption, _account, _feed, progress):
            progress(status="publishing", container_id="9")
            return {"media_id": "7", "url": "https://www.instagram.com/reel/abc/"}

        def thread(*, target, args, **kwargs):
            return mock.Mock(start=lambda: target(*args))

        with mock.patch.object(backend.threading, "Thread", side_effect=thread), \
             mock.patch.object(ig, "publish_reel", side_effect=publish), \
             mock.patch.object(backend, "_drop_from_publish_queue") as drop:
            backend.instagram_post(self.body())
        drop.assert_not_called()
        state = backend.instagram_post_status(str(self.wd), "123")
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["media_id"], "7")
        destinations = backend._film_publish_status(self.wd, {}, {})["destinations"]
        self.assertEqual(destinations, [{"platform": "instagram", "name": "studio", "url": state["url"]}])
        with self.assertRaises(HTTPException) as error:
            backend.instagram_post(self.body())
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(backend.instagram_post_status(str(self.wd), "456")["status"], "idle")

    def test_concurrent_duplicate_is_blocked(self):
        lock = backend._instagram_lock(self.wd, "123")
        try:
            with self.assertRaises(HTTPException) as error:
                backend.instagram_post(self.body())
            self.assertEqual(error.exception.status_code, 409)
            second = backend._instagram_lock(self.wd, "456")
            second.close()
        finally:
            lock.close()

    def test_lost_response_and_restart_keep_publish_uncertain(self):
        for state in ({"status": "publishing"}, {"status": "uncertain"}):
            ig.write_state(self.wd, "123", state)
            self.assertEqual(backend.instagram_post_status(str(self.wd), "123")["status"], "uncertain")
            with mock.patch.object(backend.threading, "Thread") as thread, self.assertRaises(HTTPException):
                backend.instagram_post(self.body())
            thread.assert_not_called()

    def test_worker_error_is_safe_and_releases_lock(self):
        for exc, expected in ((ig.PublishUncertain("Check the account"), "uncertain"),
                              (ig.InstagramError("Expired token"), "error"), (RuntimeError("SECRET"), "error")):
            state = {"status": "uploading"}
            lock = backend._instagram_lock(self.wd, "123")
            with mock.patch.object(ig, "publish_reel", side_effect=exc):
                backend._run_instagram_post(self.wd, self.video, self.body(), state, lock)
            stored = ig.read_state(self.wd, "123")
            self.assertEqual(stored["status"], expected)
            self.assertNotIn("SECRET", str(stored))
            self.assertTrue(lock.closed)

    def test_interrupted_upload_is_retryable_but_live_worker_is_not(self):
        ig.write_state(self.wd, "123", {"status": "processing"})
        lock = backend._instagram_lock(self.wd, "123")
        try:
            self.assertEqual(backend.instagram_post_status(str(self.wd), "123")["status"], "processing")
        finally:
            lock.close()
        self.assertEqual(backend.instagram_post_status(str(self.wd), "123")["status"], "error")

    def test_path_account_and_caption_validation(self):
        for body in (self.body(work_dir=str(self.root.parent)), self.body(account="999"), self.body(caption="x" * 2201)):
            with mock.patch.object(backend.threading, "Thread") as thread, self.assertRaises(HTTPException):
                backend.instagram_post(body)
            thread.assert_not_called()
        with mock.patch.object(backend.gapp, "_final_path_for_work_dir", return_value=Path("/etc/passwd")), self.assertRaises(HTTPException):
            backend.instagram_post(self.body())

    def test_corrupt_history_fails_closed(self):
        for text in ('{broken', '{}', '{"status": "unexpected"}'):
            ig.state_path(self.wd, "123").write_text(text)
            with mock.patch.object(backend.threading, "Thread") as thread, self.assertRaises(HTTPException):
                backend.instagram_post(self.body())
            thread.assert_not_called()
