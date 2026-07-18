"""Localized-scene synthesis fans out across the TTS worker fleet.

No network and no real TTS — _render_scene_narration is mocked; the tests
assert the orchestration shape: every reachable worker used, scenes voiced
concurrently, aggregate fanout progress recorded.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import webapp.backend.main as backend

_HOSTS = ["http://s1:8000", "http://s2:8000", "http://s3:8000"]


def _run(jobs, cfg, alive=True):
    """Run _localize_synthesize_scenes with mocked render; returns call log."""
    log = {"hosts": [], "peak": 0, "active": 0}
    lock = threading.Lock()

    def fake_render(task_id, wd, sid, jc, row, **kwargs):
        with lock:
            log["active"] += 1
            log["peak"] = max(log["peak"], log["active"])
            log["hosts"].append(kwargs["tts_host"])
        time.sleep(0.1)
        with lock:
            log["active"] -= 1

    wd = Path(tempfile.mkdtemp(prefix="spielbot-fanout-"))
    with mock.patch.object(backend, "_render_scene_narration", side_effect=fake_render), \
         mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
         mock.patch.object(backend, "_scene_voice_name", return_value=""), \
         mock.patch.object(backend.film_timing, "estimate", return_value=(5.0, False)), \
         mock.patch.object(backend.film_timing, "record"), \
         mock.patch("pipeline.tts_worker.worker_alive", return_value=alive):
        backend._localize_synthesize_scenes("t-fanout", wd, {}, "es", jobs)
    return log


class LocalizeFanoutTests(unittest.TestCase):
    def test_scenes_spread_across_all_workers_concurrently(self):
        jobs = {i: {"id": i, "narration": f"line {i}"} for i in range(1, 7)}
        log = _run(jobs, {"tts_workers": _HOSTS})
        self.assertEqual(len(log["hosts"]), 6)
        self.assertEqual(set(log["hosts"]), set(_HOSTS))  # every worker pulled work
        self.assertEqual(log["peak"], 3)                  # 3 scenes voiced at once
        self.assertEqual(backend._film_tasks["t-fanout"]["current"], 6)
        self.assertTrue(backend._film_tasks["t-fanout"]["fanout"])

    def test_single_scene_uses_one_worker(self):
        log = _run({1: {"id": 1, "narration": "solo"}}, {"tts_workers": _HOSTS})
        self.assertEqual(len(log["hosts"]), 1)
        self.assertEqual(log["peak"], 1)

    def test_dead_fleet_falls_back_to_first_configured(self):
        jobs = {i: {"id": i, "narration": f"line {i}"} for i in range(1, 4)}
        log = _run(jobs, {"tts_workers": _HOSTS}, alive=False)
        self.assertEqual(set(log["hosts"]), {_HOSTS[0]})  # sequential single host
        self.assertEqual(log["peak"], 1)

    def test_subjobs_cleared_after_run(self):
        jobs = {i: {"id": i, "narration": f"line {i}"} for i in range(1, 4)}
        _run(jobs, {"tts_workers": _HOSTS})
        leftovers = [k for k in backend._film_subjobs if k.startswith("film:t-fanout#")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
