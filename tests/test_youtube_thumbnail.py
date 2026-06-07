import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend


def _make_film(with_cover=True, video_id=""):
    """Create a temp work dir with an optional cover.png and job.json meta."""
    wd = Path(tempfile.mkdtemp(prefix="spielbot-test-film-"))
    if with_cover:
        # Must exceed the 1000-byte threshold the endpoint checks.
        (wd / "cover.png").write_bytes(b"\x89PNG" + b"\0" * 2000)
    if video_id:
        (wd / "job.json").write_text(json.dumps({"youtube_video_id": video_id}))
    return wd


class YouTubeThumbnailTests(unittest.TestCase):
    def test_pushes_cover_to_existing_video(self):
        wd = _make_film(video_id="vid-123")
        with mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/secrets.json"), \
             mock.patch.object(backend.yt, "set_thumbnail", return_value={"success": True, "error": ""}) as set_thumb:
            result = backend.yt_thumbnail(backend.ThumbnailBody(work_dir=str(wd)))

        self.assertEqual(result, {"ok": True})
        set_thumb.assert_called_once()
        # video_id is resolved from job.json; the cover path is passed through.
        _secrets, video_id, thumb_path = set_thumb.call_args.args
        self.assertEqual(video_id, "vid-123")
        self.assertEqual(thumb_path, str(wd / "cover.png"))

    def test_explicit_video_id_overrides_meta(self):
        wd = _make_film(video_id="from-meta")
        with mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/secrets.json"), \
             mock.patch.object(backend.yt, "set_thumbnail", return_value={"success": True, "error": ""}) as set_thumb:
            backend.yt_thumbnail(backend.ThumbnailBody(work_dir=str(wd), video_id="explicit-id"))
        self.assertEqual(set_thumb.call_args.args[1], "explicit-id")

    def test_rejects_when_not_uploaded(self):
        wd = _make_film(video_id="")  # cover present, but no video id anywhere
        with mock.patch.object(backend.yt, "set_thumbnail") as set_thumb:
            with self.assertRaises(backend.HTTPException) as ctx:
                backend.yt_thumbnail(backend.ThumbnailBody(work_dir=str(wd)))
        self.assertEqual(ctx.exception.status_code, 400)
        set_thumb.assert_not_called()

    def test_rejects_when_cover_missing(self):
        wd = _make_film(with_cover=False, video_id="vid-123")
        with mock.patch.object(backend.yt, "set_thumbnail") as set_thumb:
            with self.assertRaises(backend.HTTPException) as ctx:
                backend.yt_thumbnail(backend.ThumbnailBody(work_dir=str(wd)))
        self.assertEqual(ctx.exception.status_code, 400)
        set_thumb.assert_not_called()

    def test_surfaces_youtube_error(self):
        wd = _make_film(video_id="vid-123")
        with mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/secrets.json"), \
             mock.patch.object(backend.yt, "set_thumbnail",
                               return_value={"success": False, "error": "quota exceeded"}):
            with self.assertRaises(backend.HTTPException) as ctx:
                backend.yt_thumbnail(backend.ThumbnailBody(work_dir=str(wd)))
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("quota exceeded", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
