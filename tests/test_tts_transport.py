"""TTS transport routing (issue #12 — containerized workers).

generate_narration() picks a transport from the host string:
  localhost / 127.0.0.1   -> local F5-TTS subprocess
  http(s)://host:port     -> HTTP POST to a containerized F5-TTS worker
  anything else           -> SSH to a bare host (legacy distributed path)

The HTTP transport is what lets the F5-TTS worker run as a container. These
tests exercise the routing and the HTTP client without invoking real TTS.
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import tts_worker


def _ref() -> Path:
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    f.write(b"RIFFfake-reference")
    f.close()
    return Path(f.name)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.ref = _ref()
        self.out = Path(tempfile.mkdtemp()) / "out.wav"

    def _run(self, host):
        with mock.patch.object(tts_worker, "_f5_local") as local, \
             mock.patch.object(tts_worker, "_f5_remote") as remote, \
             mock.patch.object(tts_worker, "_f5_http") as http:
            tts_worker.generate_narration("hello", self.out, reference_wav=self.ref, host=host)
        return local, remote, http

    def test_localhost_uses_local(self):
        local, remote, http = self._run("localhost")
        local.assert_called_once()
        remote.assert_not_called()
        http.assert_not_called()

    def test_http_url_uses_http(self):
        local, remote, http = self._run("http://s1:8189")
        http.assert_called_once()
        local.assert_not_called()
        remote.assert_not_called()

    def test_https_url_uses_http(self):
        local, remote, http = self._run("https://s1:8189")
        http.assert_called_once()
        remote.assert_not_called()

    def test_bare_host_uses_ssh(self):
        local, remote, http = self._run("s1")
        remote.assert_called_once()
        http.assert_not_called()
        local.assert_not_called()


class HttpClientTests(unittest.TestCase):
    def test_posts_to_tts_endpoint_and_writes_wav(self):
        ref = _ref()
        out = Path(tempfile.mkdtemp()) / "out.wav"
        captured = {}

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = req.data
            return FakeResp(b"WAVDATA")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            # Trailing slash on the URL must not double up before /tts.
            tts_worker._f5_http("hello", ref, out, "http://s1:8189/", speed=1.0)

        self.assertEqual(captured["url"], "http://s1:8189/tts")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(out.read_bytes(), b"WAVDATA")
        # A non-default reference must be sent inline (base64), not dropped.
        body = json.loads(captured["body"])
        self.assertIsNotNone(body["ref_audio_b64"])
        self.assertEqual(body["speed"], 1.0)

    def test_default_ref_sent_as_null(self):
        out = Path(tempfile.mkdtemp()) / "out.wav"
        captured = {}

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return FakeResp(b"WAVDATA")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            tts_worker._f5_http("hi", tts_worker.DEFAULT_REF, out, "http://s1:8189", speed=1.0)

        body = json.loads(captured["body"])
        self.assertIsNone(body["ref_audio_b64"])


if __name__ == "__main__":
    unittest.main()
