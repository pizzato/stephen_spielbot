"""Edit-screen scene re-renders must share ONE WorkerPool.

Before this, _run_image_rerender / _run_video_rerender each built their own
WorkerPool per request. The pool's per-worker semaphore then gated nothing
across requests: clicking regenerate on many scenes submitted them all to
ComfyUI at once, piling extra jobs onto the busy workers. Anything left pending
behind a running job for >_PENDING_TIMEOUT (180s) is deleted by ComfyUI's
worker-stuck safety valve, so every re-render past the worker count failed
silently. Both now go through backend._shared_edit_render_pool(), which returns
one process-wide pool so acquire() is a real FIFO queue."""
import unittest
from unittest import mock

import webapp.backend.main as backend
from test_styles import TempConfigCase


class SharedEditRenderPoolTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        # Reset the module-level singleton so each test starts clean.
        backend._edit_render_pool = None
        backend._edit_render_pool_key = ()
        self.addCleanup(setattr, backend, "_edit_render_pool", None)
        self.addCleanup(setattr, backend, "_edit_render_pool_key", ())
        # mark_active() just touches a file; keep it out of the way.
        p = mock.patch.object(backend.ui_activity, "mark_active")
        p.start()
        self.addCleanup(p.stop)

    def _reachable(self, urls):
        """Patch alive_workers so the 'reachable' set is exactly *urls*."""
        return mock.patch("pipeline.worker_pool.alive_workers", return_value=list(urls))

    def test_returns_same_pool_instance_across_calls(self):
        self.write_config({"comfy_workers": ["http://w1:8188", "http://w2:8188"]})
        with self._reachable(["http://w1:8188", "http://w2:8188"]):
            a = backend._shared_edit_render_pool()
            b = backend._shared_edit_render_pool()
        # Same object → the per-worker semaphores are shared, so concurrent
        # re-renders queue in acquire() instead of all hitting ComfyUI at once.
        self.assertIsNotNone(a)
        self.assertIs(a, b)
        self.assertEqual(sorted(a.urls), ["http://w1:8188", "http://w2:8188"])

    def test_rebuilds_when_reachable_set_changes(self):
        self.write_config({"comfy_workers": ["http://w1:8188", "http://w2:8188"]})
        with self._reachable(["http://w1:8188", "http://w2:8188"]):
            a = backend._shared_edit_render_pool()
        # A worker drops off → the pool is rebuilt over the new set.
        with self._reachable(["http://w1:8188"]):
            b = backend._shared_edit_render_pool()
        self.assertIsNot(a, b)
        self.assertEqual(b.urls, ["http://w1:8188"])

    def test_none_when_no_workers_reachable(self):
        self.write_config({"comfy_workers": ["http://w1:8188"]})

        def _raise(_urls, **_kw):
            raise RuntimeError("No ComfyUI workers reachable.")

        with mock.patch("pipeline.worker_pool.alive_workers", side_effect=_raise):
            self.assertIsNone(backend._shared_edit_render_pool())

    def test_serializes_when_more_renders_than_workers(self):
        """With one worker, a second acquire must block until the first releases —
        this is the queueing that was missing when each request had its own pool."""
        self.write_config({"comfy_workers": ["http://w1:8188"]})
        with self._reachable(["http://w1:8188"]):
            pool = backend._shared_edit_render_pool()
        url = pool.acquire()
        try:
            import threading
            got = threading.Event()

            def _second():
                u = pool.acquire()
                try:
                    got.set()
                finally:
                    pool.release(u)

            t = threading.Thread(target=_second, daemon=True)
            t.start()
            # The one worker is held → the second acquire cannot complete yet.
            self.assertFalse(got.wait(timeout=1.0))
        finally:
            pool.release(url)
        # Once released, the queued acquire proceeds.
        self.assertTrue(got.wait(timeout=2.0))


if __name__ == "__main__":
    unittest.main()
