"""A worker that stops answering must not hold a render open to the timeout.

A Spark losing power mid-render left /queue unanswerable ("unknown"). The wait
loop treated that as no evidence either way and skipped the rest of its health
check, so neither stuck-detector ran and the render sat for 88 minutes until the
engine's give-up deadline. HTTP silence alone still proves nothing — ComfyUI
stops serving while it loads a big checkpoint — so the discriminator is SSH.
"""
import unittest
from unittest import mock

import websocket

from pipeline import comfyui


class _Clock:
    """Deterministic stand-in for the module's time source."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now


class _FakeWS:
    """WebSocket that only ever times out, advancing the clock as it does."""

    def __init__(self, clock, step):
        self.clock = clock
        self.step = step

    def connect(self, _url):
        pass

    def settimeout(self, _secs):
        pass

    def recv(self):
        self.clock.now += self.step
        raise websocket.WebSocketTimeoutException()

    def close(self):
        pass


class UnreachableWorkerTests(unittest.TestCase):
    def _run(self, queue_status, gpu_idle, timeout=7200):
        """Drive _wait_for_completion against a fixed worker state.

        Returns (elapsed_fake_seconds, reached_poll_fallback, raised_or_None).
        """
        clock = _Clock()
        started = clock.now
        # One WS timeout per loop pass, long enough to trigger a queue check
        # each time round.
        ws = _FakeWS(clock, comfyui._QUEUE_CHECK_INTERVAL + 1)
        polled = []
        with mock.patch.object(comfyui, "time", clock), \
             mock.patch.object(comfyui.websocket, "WebSocket", lambda: ws), \
             mock.patch.object(comfyui, "_check_queue", return_value=queue_status), \
             mock.patch.object(comfyui, "_check_gpu_idle", return_value=gpu_idle), \
             mock.patch.object(comfyui, "_check_history", return_value="absent"), \
             mock.patch.object(comfyui, "_cleanup_prompt"), \
             mock.patch.object(comfyui, "_interrupt_running_prompt"), \
             mock.patch.object(comfyui, "_poll_completion",
                               side_effect=lambda *a, **k: polled.append(True)):
            try:
                comfyui._wait_for_completion("pid-1234", "cid", timeout=timeout,
                                             comfy_url="http://s2:8188")
            except Exception as exc:          # returned, so callers can assert on it
                return clock.now - started, bool(polled), exc
        return clock.now - started, bool(polled), None

    def test_dead_worker_gives_up_instead_of_waiting_out_the_timeout(self):
        # Neither HTTP nor SSH answers: the machine is gone.
        elapsed, polled, exc = self._run("unknown", None)
        self.assertIsInstance(exc, comfyui.StuckJobError)
        self.assertIn("neither HTTP nor SSH", str(exc))
        self.assertIn("will try another", str(exc))
        self.assertFalse(polled, "should not fall through to /history polling")
        # The whole point: minutes, not the engine's multi-hour give-up bound.
        self.assertLess(elapsed, 1200, f"took {elapsed:.0f}s of fake time to notice")

    def test_loading_worker_is_left_alone(self):
        # HTTP silent because ComfyUI is loading a checkpoint, but the box is up
        # and its GPU is busy — that job is healthy and must not be killed.
        _elapsed, polled, exc = self._run("unknown", False, timeout=900)
        self.assertIsNone(exc, "a busy GPU means the job is alive")
        self.assertTrue(polled, "healthy job should run to the deadline, not raise")

    def test_live_worker_with_flaky_ssh_is_left_alone(self):
        # HTTP answers "running", so an SSH failure is just SSH being flaky.
        _elapsed, polled, exc = self._run("running", None, timeout=900)
        self.assertIsNone(exc, "a worker answering HTTP must not be dropped")
        self.assertTrue(polled)


if __name__ == "__main__":
    unittest.main()
