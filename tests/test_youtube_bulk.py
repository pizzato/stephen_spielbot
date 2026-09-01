"""Bulk video actions (/api/youtube/videos/bulk): per-video results, cache
patching (privacy updated, deleted rows dropped), and input validation.
No network — the per-video YouTube calls are mocked."""
import os
import tempfile
import unittest
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

from fastapi import HTTPException

import webapp.backend.main as backend


def _body(**kw):
    return backend.BulkVideosBody(**{"channel": "ch1", "video_ids": ["v1", "v2"], **kw})


class BulkVideosTests(unittest.TestCase):
    def test_privacy_patches_cache_and_reports_per_video(self):
        snapshot = {"channel": {"name": "ch"}, "videos": [
            {"video_id": "v1", "privacy": "public"},
            {"video_id": "v2", "privacy": "public"},
            {"video_id": "v3", "privacy": "public"},
        ]}
        cache = {"ch1": snapshot}
        saved = {}
        results = {"v1": {"success": True, "error": ""},
                   "v2": {"success": False, "error": "boom"}}
        with mock.patch.object(backend.yt, "set_video_privacy",
                               side_effect=lambda s, vid, p, channel: results[vid]), \
             mock.patch.object(backend.yt, "load_analytics_cache", return_value=cache), \
             mock.patch.object(backend.yt, "save_analytics_cache", side_effect=saved.update):
            out = backend.yt_videos_bulk(_body(action="privacy", privacy="private"))
        self.assertEqual(out["succeeded"], 1)
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["results"][1]["error"], "boom")
        # only the succeeded video's cached privacy changed
        privacies = {v["video_id"]: v["privacy"] for v in snapshot["videos"]}
        self.assertEqual(privacies, {"v1": "private", "v2": "public", "v3": "public"})
        self.assertIn("ch1", saved)

    def test_delete_drops_rows_from_cache(self):
        snapshot = {"channel": {"name": "ch"}, "videos": [
            {"video_id": "v1"}, {"video_id": "v2"}, {"video_id": "v3"},
        ]}
        with mock.patch.object(backend.yt, "delete_video",
                               return_value={"success": True, "error": ""}), \
             mock.patch.object(backend.yt, "load_analytics_cache", return_value={"ch1": snapshot}), \
             mock.patch.object(backend.yt, "save_analytics_cache"):
            out = backend.yt_videos_bulk(_body(action="delete"))
        self.assertEqual(out["succeeded"], 2)
        self.assertEqual([v["video_id"] for v in snapshot["videos"]], ["v3"])

    def test_playlist_does_not_touch_cache(self):
        with mock.patch.object(backend.yt, "add_video_to_playlist",
                               return_value={"success": True, "error": ""}), \
             mock.patch.object(backend.yt, "load_analytics_cache") as lc:
            out = backend.yt_videos_bulk(_body(action="playlist", playlist_id="pl1"))
        self.assertEqual(out["succeeded"], 2)
        lc.assert_not_called()

    def test_rejects_bad_input(self):
        for body in (_body(action="privacy", video_ids=[]),          # no videos
                     _body(action="privacy", privacy="secret"),      # bad privacy
                     _body(action="playlist"),                       # no playlist
                     _body(action="explode")):                       # unknown action
            with self.assertRaises(HTTPException):
                backend.yt_videos_bulk(body)


if __name__ == "__main__":
    unittest.main()
