"""Cross-pool / cross-process worker leases.

Every comfy-bound path builds (or shares) a WorkerPool, but before the lease
layer each pool believed all workers were free: two upscale batches plus a
render could each submit one job per worker, stacking 3+ jobs on every GPU.
acquire() now flocks a per-worker lease file shared by every pool in every
process, so one worker runs one job no matter who dispatched it.
"""

import fcntl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pipeline import worker_pool
from pipeline.worker_pool import WorkerPool, _lease_path


def _try_acquire(pool: WorkerPool, timeout: float = 0.4, only=None):
    """acquire() in a thread; return the URL or None if it blocked past timeout."""
    box: list = []
    t = threading.Thread(target=lambda: box.append(pool.acquire(only=only)), daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else None


class LeaseIsolationMixin(unittest.TestCase):
    def setUp(self):
        self._orig_dir = worker_pool.LEASE_DIR
        worker_pool.LEASE_DIR = Path(tempfile.mkdtemp(prefix="spielbot-leases-"))

    def tearDown(self):
        worker_pool.LEASE_DIR = self._orig_dir


class CrossPoolLeaseTests(LeaseIsolationMixin):
    def test_two_pools_cannot_double_book_a_worker(self):
        a = WorkerPool(["w1"])
        b = WorkerPool(["w1"])
        url = a.acquire()
        self.assertEqual(url, "w1")
        box: list = []
        waiter = threading.Thread(target=lambda: box.append(b.acquire()), daemon=True)
        waiter.start()
        waiter.join(0.4)
        self.assertEqual(box, [], "second pool must wait, not double-book")
        a.release(url)
        waiter.join(2.0)
        self.assertEqual(box, ["w1"], "released worker goes to the waiting pool")

    def test_second_pool_takes_the_other_worker(self):
        a = WorkerPool(["w1", "w2"])
        b = WorkerPool(["w1", "w2"])
        first = a.acquire()
        second = _try_acquire(b, timeout=2.0)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        # Both taken now — a third pool waits.
        c = WorkerPool(["w1", "w2"])
        self.assertIsNone(_try_acquire(c))

    def test_mark_failed_frees_the_lease(self):
        a = WorkerPool(["w1", "w2"])
        b = WorkerPool(["w1"])
        url = a.acquire(only="w1")
        a.mark_failed(url)
        self.assertEqual(_try_acquire(b, timeout=2.0), "w1")

    def test_reserved_worker_stays_leasable_by_other_pools(self):
        # The render pool holds a worker idle for the UI; the backend's pools
        # must still be able to lease it — that's what the reservation is FOR.
        render = WorkerPool(["w1", "w2"], reserve_check=lambda: True)
        deadline = time.time() + 3.0
        while render.reserved_url is None and time.time() < deadline:
            time.sleep(0.05)
        reserved = render.reserved_url
        self.assertIsNotNone(reserved)
        ui = WorkerPool([reserved])
        self.assertEqual(_try_acquire(ui, timeout=2.0), reserved)
        render.shutdown()

    def test_shutdown_drops_all_leases(self):
        a = WorkerPool(["w1", "w2"])
        a.acquire()
        a.acquire()
        a.shutdown()
        b = WorkerPool(["w1", "w2"])
        self.assertIsNotNone(_try_acquire(b, timeout=2.0))

    def test_unwritable_lease_dir_fails_open(self):
        # A state-dir problem must never brick rendering.
        blocker = Path(tempfile.mkdtemp(prefix="spielbot-leases-")) / "not-a-dir"
        blocker.write_text("")
        worker_pool.LEASE_DIR = blocker / "leases"
        a = WorkerPool(["w1"])
        self.assertEqual(a.acquire(), "w1")
        a.release("w1")


class CrossProcessLeaseTests(LeaseIsolationMixin):
    def test_worker_leased_by_another_process_is_skipped(self):
        lease = _lease_path("w1")
        lease.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", (
                "import fcntl,sys,time\n"
                f"fd=open({str(lease)!r},'w')\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "print('held', flush=True)\n"
                "time.sleep(30)\n"
            )],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            pool = WorkerPool(["w1", "w2"])
            self.assertEqual(_try_acquire(pool, timeout=2.0), "w2")
            box: list = []
            waiter = threading.Thread(target=lambda: box.append(pool.acquire()), daemon=True)
            waiter.start()
            waiter.join(0.4)
            self.assertEqual(box, [], "w1 is leased elsewhere — must wait")
        finally:
            holder.kill()
            holder.wait()
        # The OS released the dead process's flock — w1 is usable again.
        waiter.join(3.0)
        self.assertEqual(box, ["w1"])

    def test_lease_file_is_flocked_while_held(self):
        pool = WorkerPool(["w1"])
        pool.acquire()
        with open(_lease_path("w1"), "w") as probe:
            with self.assertRaises(OSError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
