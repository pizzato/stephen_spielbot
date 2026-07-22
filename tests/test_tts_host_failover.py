"""Single-scene narration must not die just because the first TTS worker is down.

The single-scene paths (voice test, narration re-render, dialogue re-render) used
to default to tts_workers[0]. When s1 was offline while s2/s3 were healthy, the
"Narration" button failed with a "Remote F5-TTS unreachable" error instead of
routing to a live worker. _first_live_tts_host probes the fleet and picks the
first reachable worker.
"""
import unittest
from unittest import mock

from webapp.backend.main import _first_live_tts_host


class FirstLiveTtsHostTests(unittest.TestCase):
    def test_skips_down_worker_for_next_live_one(self):
        cfg = {"tts_workers": ["http://s1:8189", "http://s2:8189", "http://s3:8189"]}
        alive = lambda h, timeout=3: h != "http://s1:8189"  # s1 down
        with mock.patch("pipeline.tts_worker.worker_alive", side_effect=alive):
            self.assertEqual(_first_live_tts_host(cfg), "http://s2:8189")

    def test_returns_first_when_all_up(self):
        cfg = {"tts_workers": ["http://s1:8189", "http://s2:8189"]}
        with mock.patch("pipeline.tts_worker.worker_alive", return_value=True):
            self.assertEqual(_first_live_tts_host(cfg), "http://s1:8189")

    def test_falls_back_to_first_configured_when_all_down(self):
        # No worker answers — keep naming a real endpoint so the error is useful.
        cfg = {"tts_workers": ["http://s1:8189", "http://s2:8189"]}
        with mock.patch("pipeline.tts_worker.worker_alive", return_value=False):
            self.assertEqual(_first_live_tts_host(cfg), "http://s1:8189")

    def test_localhost_when_none_configured(self):
        self.assertEqual(_first_live_tts_host({}), "localhost")
        self.assertEqual(_first_live_tts_host({"tts_workers": ["", "  "]}), "localhost")


if __name__ == "__main__":
    unittest.main()
