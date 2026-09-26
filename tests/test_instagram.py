"""Instagram account isolation, upload protocol, and safe failure handling.

All Meta requests are mocked; these tests never publish real posts.
"""
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from pipeline import instagram as ig


def response(data, status=200):
    result = mock.Mock(status_code=status)
    result.json.return_value = data
    return result


class InstagramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.object(ig, "CONFIG_DIR", self.root / "config")
        patch.start()
        self.addCleanup(patch.stop)
        self.video = self.root / "film.mp4"
        self.video.write_bytes(b"test video")

    def token(self, account="123", token="private-page-token"):
        ig._write_json(ig._token_path(account), {"id": account, "name": "filmstudio", "access_token": token})

    def test_connect_verifies_identity_and_publishing_access_without_exposing_token(self):
        responses = [response({"id": "10", "name": "Studio Page", "instagram_business_account": {
            "id": "123", "username": "filmstudio"}}), response({"data": []})]
        with mock.patch.object(ig.requests, "request", side_effect=responses) as request:
            account = ig.connect_account("private-page-token")
        self.assertEqual(account["id"], "123")
        self.assertEqual(ig.list_accounts(), [account])
        self.assertNotIn("private-page-token", json.dumps(account))
        self.assertEqual(stat.S_IMODE(ig._token_path("123").stat().st_mode), 0o600)
        self.assertTrue(request.call_args_list[1].args[1].endswith("/123/content_publishing_limit"))

    def test_connect_rejects_unlinked_page_and_missing_permission(self):
        for replies in (
            [response({"id": "10", "name": "No Instagram"})],
            [response({"instagram_business_account": {"id": "123", "username": "studio"}}),
             response({"error": {"code": 10, "message": "private-page-token"}}, 403)],
        ):
            with mock.patch.object(ig.requests, "request", side_effect=replies), self.assertRaises(ig.InstagramError):
                ig.connect_account("private-page-token")
            self.assertEqual(ig.list_accounts(), [])

    def test_accounts_have_separate_credentials_and_disconnect_is_local(self):
        self.token("123", "one")
        self.token("456", "two")
        ig.disconnect_account("123")
        self.assertEqual(ig._load_token("456"), "two")
        self.assertEqual([a["id"] for a in ig.list_accounts()], ["456"])
        for unsafe in ("../123", "", "123/456", "default"):
            with self.assertRaises(ig.InstagramError):
                ig.disconnect_account(unsafe)

    def upload_responses(self):
        return [response({"id": "999", "uri": "https://rupload.facebook.com/ig-api-upload/999"}),
                response({"success": True}), response({"status_code": "IN_PROGRESS"}),
                response({"status_code": "FINISHED"}), response({"id": "777"}),
                response({"permalink": "https://www.instagram.com/reel/abc/"})]

    def test_stream_upload_waits_for_processing_before_publishing_once(self):
        self.token()
        events = []
        uploaded_bytes = []
        replies = iter(self.upload_responses())

        def send(method, url, **kw):
            self.assertNotIn("access_token", url)
            self.assertFalse(kw["allow_redirects"])
            if "rupload.facebook.com" in url:
                uploaded_bytes.append(kw["data"].read())
                self.assertEqual(kw["headers"]["file_size"], "10")
                self.assertEqual(kw["headers"]["Authorization"], "OAuth private-page-token")
            if url.endswith("/media_publish"):
                self.assertEqual(events[-1]["status"], "publishing")
                self.assertEqual(kw["data"], {"creation_id": "999"})
            if url.endswith("/777"):
                self.assertEqual(events[-1], {"status": "done", "media_id": "777"})
            return next(replies)

        with mock.patch.object(ig.requests, "request", side_effect=send) as request, \
             mock.patch.object(ig, "validate_video"), mock.patch.object(ig.time, "sleep"):
            result = ig.publish_reel(self.video, "Caption #film", "123", False, lambda **e: events.append(e))
        self.assertEqual(result, {"media_id": "777", "url": "https://www.instagram.com/reel/abc/"})
        self.assertEqual(uploaded_bytes, [b"test video"])
        self.assertEqual(request.call_args_list[0].kwargs["data"], {
            "media_type": "REELS", "upload_type": "resumable", "caption": "Caption #film", "share_to_feed": "false"})
        self.assertEqual(sum(c.args[1].endswith("/media_publish") for c in request.call_args_list), 1)

    def test_processing_failure_or_timeout_never_publishes(self):
        self.token()
        for status in ("ERROR", "EXPIRED", "IN_PROGRESS"):
            replies = self.upload_responses()[:2] + [response({"status_code": status})]
            with mock.patch.object(ig.requests, "request", side_effect=replies) as request, \
                 mock.patch.object(ig, "validate_video"), mock.patch.object(ig, "POLL_ATTEMPTS", 1), \
                 mock.patch.object(ig.time, "sleep"), self.assertRaises(ig.InstagramError):
                ig.publish_reel(self.video, "", "123", True, mock.Mock())
            self.assertFalse(any(c.args[1].endswith("/media_publish") for c in request.call_args_list))

    def test_untrusted_upload_url_never_receives_token(self):
        self.token()
        for url in ("https://example.com/upload", "http://rupload.facebook.com/upload",
                    "https://rupload.facebook.com.evil.test/upload", "https://user@rupload.facebook.com/upload"):
            with mock.patch.object(ig.requests, "request", return_value=response({"id": "999", "uri": url})) as request, \
                 mock.patch.object(ig, "validate_video"), self.assertRaises(ig.InstagramError):
                ig.publish_reel(self.video, "", "123", True, mock.Mock())
            self.assertEqual(request.call_count, 1)

    def test_lost_publish_response_is_uncertain_and_not_retried(self):
        self.token()
        replies = self.upload_responses()[:2] + [response({"status_code": "FINISHED"}), requests.Timeout("private-page-token")]
        with mock.patch.object(ig.requests, "request", side_effect=replies) as request, \
             mock.patch.object(ig, "validate_video"), self.assertRaises(ig.PublishUncertain) as error:
            ig.publish_reel(self.video, "", "123", True, mock.Mock())
        self.assertNotIn("private-page-token", str(error.exception))
        self.assertEqual(request.call_count, 4)

    def test_permalink_failure_still_reports_success(self):
        self.token()
        replies = self.upload_responses()[:2] + [response({"status_code": "FINISHED"}), response({"id": "777"}),
                                                response({"error": {"code": 190}}, 400)]
        with mock.patch.object(ig.requests, "request", side_effect=replies), mock.patch.object(ig, "validate_video"):
            result = ig.publish_reel(self.video, "", "123", True, mock.Mock())
        self.assertEqual(result, {"media_id": "777", "url": ""})

    def test_errors_never_echo_api_messages_or_http_exceptions(self):
        for reply in (response({"error": {"code": 190, "message": "SECRET"}}, 400),
                      response({"error": {"code": 100, "message": "SECRET"}}, 400), requests.ConnectionError("SECRET")):
            with mock.patch.object(ig.requests, "request", side_effect=[reply]), self.assertRaises(ig.InstagramError) as error:
                ig._graph("GET", "me", "SECRET")
            self.assertNotIn("SECRET", str(error.exception))

    def test_rejects_long_caption_before_network(self):
        with mock.patch.object(ig.requests, "request") as request, self.assertRaises(ig.InstagramError):
            ig.publish_reel(self.video, "a" * 2201, "123", True, mock.Mock())
        request.assert_not_called()

    def test_video_requirements(self):
        info = {"format": {"duration": "30"}, "streams": [
            {"codec_type": "video", "codec_name": "h264", "avg_frame_rate": "30/1", "width": 1080},
            {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
        ]}
        with mock.patch("pipeline.assembler._resolve_media_tool", return_value="ffprobe"), \
             mock.patch.object(ig.subprocess, "run", return_value=mock.Mock(stdout=json.dumps(info))):
            ig.validate_video(self.video)
        for duration in ("2", "901", "nan"):
            info["format"]["duration"] = duration
            with mock.patch("pipeline.assembler._resolve_media_tool", return_value="ffprobe"), \
                 mock.patch.object(ig.subprocess, "run", return_value=mock.Mock(stdout=json.dumps(info))), self.assertRaises(ig.InstagramError):
                ig.validate_video(self.video)
