"""Manages a pool of ComfyUI worker URLs for distributed video generation."""

import errno
import fcntl
import logging
import os
import re
from collections import deque
from pathlib import Path
import threading
import urllib.request
import urllib.error
from typing import Callable, Optional

logger = logging.getLogger("video_gen")

# Cross-process worker leases. Every WorkerPool instance — the render
# subprocess's pool, the backend's shared edit pool, and any ad-hoc pool a
# request builds — flocks one file per worker URL before using that worker, so
# two schedulers can never double-book the same GPU. flock is released by the
# OS when a process dies, so a crashed render never leaves a stale lease.
# Same state dir as ui_activity so the render subprocess and backend agree.
LEASE_DIR = Path.home() / ".local" / "share" / "video-generator" / "worker_leases"


def _lease_path(url: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", url).strip("_") or "worker"
    return LEASE_DIR / f"{slug}.lock"


def check_alive(url: str, timeout: int = 5) -> bool:
    """Return True if the ComfyUI instance at url is responding."""
    try:
        with urllib.request.urlopen(f"{url}/system_stats", timeout=timeout):
            return True
    except Exception:
        return False


def alive_workers(urls: list[str], timeout: int = 5) -> list[str]:
    """Return only the URLs that are currently reachable."""
    ok = [u for u in urls if check_alive(u, timeout=timeout)]
    if not ok:
        raise RuntimeError(
            f"No ComfyUI workers reachable. Tried: {urls}\n"
            "Check comfy_workers in config.yaml (Settings screen) and that workers are running."
        )
    return ok


def queue_depth(url: str, timeout: int = 5) -> int:
    """Return running+pending job count for a ComfyUI worker, or -1 if unreachable."""
    try:
        with urllib.request.urlopen(f"{url}/queue", timeout=timeout) as resp:
            import json
            data = json.loads(resp.read())
        return len(data.get("queue_running", [])) + len(data.get("queue_pending", []))
    except Exception:
        return -1


def idle_workers(urls: list[str], timeout: int = 5) -> list[str]:
    """Return reachable workers ordered least-busy-first, preferring idle ones.

    A worker busy with a long video render reports a non-empty queue; this lets
    light tasks (scene previews, covers) avoid queueing behind it when other
    workers are free. Falls back to all reachable workers if every one is busy.
    """
    reachable = [(u, queue_depth(u, timeout=timeout)) for u in urls]
    reachable = [(u, d) for u, d in reachable if d >= 0]
    if not reachable:
        raise RuntimeError(
            f"No ComfyUI workers reachable. Tried: {urls}\n"
            "Check comfy_workers in config.yaml (Settings screen) and that workers are running."
        )
    idle = [u for u, d in reachable if d == 0]
    if idle:
        return idle
    # All busy — return ordered by ascending load so the least-loaded is picked first.
    return [u for u, _ in sorted(reachable, key=lambda x: x[1])]


class WorkerPool:
    """Per-URL semaphore pool. Each worker handles one job at a time.

    The one-job-at-a-time rule holds ACROSS pools and processes, not just within
    this instance: acquire() also takes a flock lease on the worker's file under
    LEASE_DIR, and skips workers whose lease another pool (e.g. the render
    subprocess vs the backend, or two concurrent upscale batches) already holds.

    Optionally reserves one worker for the web UI (issue #98): while
    ``reserve_check()`` returns True (the UI is being actively used), one worker
    is held back — left idle — so cover/preview jobs the web backend submits land
    on a free GPU instead of queueing behind this render. The held worker rejoins
    the render pool as soon as ``reserve_check()`` goes False (UI idle). The last
    worker is never reserved, so a single-worker render still makes progress.

    The reservation crosses the process boundary purely through ``reserve_check``
    (the render subprocess passes a closure that reads the shared UI-activity
    file); the pool itself just leaves the worker idle, and the backend routes UI
    work to whichever worker is idle.
    """

    def __init__(self, urls: list[str], reserve_check: Optional[Callable[[], bool]] = None):
        if not urls:
            raise ValueError("WorkerPool needs at least one URL")
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._waiters: deque[object] = deque()
        self._urls = list(urls)
        self._sems: dict[str, threading.Semaphore] = {u: threading.Semaphore(1) for u in self._urls}
        self._lease_fds: dict[str, int] = {}
        self._reserve_check = reserve_check
        self._reserved_url: Optional[str] = None   # held idle for the UI
        self._closed = False
        self._poller: Optional[threading.Thread] = None
        if reserve_check is not None:
            # A daemon poller keeps the reservation current even when no render
            # thread is in acquire()/release(): it grabs a free worker once the
            # UI becomes active, and returns the held one once the UI goes idle.
            self._poller = threading.Thread(target=self._reserve_loop, name="ui-reserve", daemon=True)
            self._poller.start()

    @property
    def urls(self) -> list[str]:
        with self._lock:
            return list(self._urls)

    @property
    def reserved_url(self) -> Optional[str]:
        """The worker currently held idle for the UI, or None."""
        with self._lock:
            return self._reserved_url

    def _try_lease(self, url: str) -> bool:
        """Take the cross-process lease for *url* without blocking. Caller holds
        the pool lock and this worker's semaphore. False means another pool or
        process is using the worker right now."""
        if url in self._lease_fds:
            return True
        try:
            LEASE_DIR.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(_lease_path(url)), os.O_CREAT | os.O_RDWR, 0o644)
        except Exception:
            # A state-dir problem must never brick rendering: run unleased.
            logger.warning("WorkerPool: cannot open lease file for %s; skipping lease", url)
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return False
            logger.warning("WorkerPool: lease flock failed for %s (%s); skipping lease", url, e)
            return True
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except Exception:
            pass
        self._lease_fds[url] = fd
        return True

    def _drop_lease(self, url: str) -> None:
        fd = self._lease_fds.pop(url, None)
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass

    def _want_reserve(self) -> bool:
        """True if a worker should be held for the UI right now. Caller holds the
        lock. Never reserve the last worker — the render must keep at least one."""
        if self._reserve_check is None or len(self._urls) < 2:
            return False
        try:
            return bool(self._reserve_check())
        except Exception:
            return False

    def _refresh_reservation(self) -> None:
        """Reconcile the held-for-UI worker with reserve_check(). Caller holds lock."""
        if self._want_reserve():
            if self._reserved_url is None:
                # Hold an idle worker for the UI. If all are busy, a later
                # release() claims the first one freed.
                for url in self._urls:
                    sem = self._sems.get(url)
                    if sem and sem.acquire(blocking=False):
                        self._reserved_url = url
                        logger.debug("WorkerPool: reserved %s for UI", url)
                        break
        elif self._reserved_url is not None:
            # UI idle — return the held worker to the render pool.
            sem = self._sems.get(self._reserved_url)
            if sem:
                sem.release()
            logger.debug("WorkerPool: released UI reservation on %s", self._reserved_url)
            self._reserved_url = None
            self._cond.notify_all()

    def _reserve_loop(self) -> None:
        with self._cond:
            while not self._closed:
                self._refresh_reservation()
                self._cond.wait(timeout=2.0)

    def acquire(self, only: Optional[str] = None) -> str:
        """Block until any worker is free, return its URL.

        Keep callers FIFO so retries cannot be starved by later scene threads.

        *only* waits for ONE named worker instead of taking the first free one —
        for work that can run nowhere else, like continuing an H3 take from the
        motion context saved on that worker's disk.
        """
        if only is not None and only not in self._sems:
            raise RuntimeError(f"Worker {only} is not in the pool")
        token = object()
        with self._cond:
            self._waiters.append(token)
            try:
                while True:
                    if not self._urls:
                        raise RuntimeError("No healthy workers remaining in pool")
                    if only is not None and only not in self._sems:
                        raise RuntimeError(f"Worker {only} left the pool")
                    self._refresh_reservation()
                    if only is not None and self._reserved_url == only:
                        # The one worker that will do is the one held idle for the
                        # UI. Hand it over — the next refresh reserves another —
                        # rather than wait for a reservation that outlasts us.
                        sem = self._sems.get(only)
                        if sem:
                            sem.release()
                        self._reserved_url = None

                    is_turn = self._waiters and self._waiters[0] is token
                    if is_turn:
                        for url in ([only] if only is not None else list(self._urls)):
                            if url == self._reserved_url:
                                continue  # held idle for the UI
                            sem = self._sems.get(url)
                            if sem and sem.acquire(blocking=False):
                                if not self._try_lease(url):
                                    # Busy in another pool or process — put the
                                    # semaphore back and try the next worker.
                                    sem.release()
                                    continue
                                self._waiters.popleft()
                                self._cond.notify_all()
                                logger.debug("WorkerPool: acquired %s", url)
                                return url

                    self._cond.wait(timeout=0.5)
            except Exception:
                try:
                    self._waiters.remove(token)
                    self._cond.notify_all()
                except ValueError:
                    pass
                raise

    def release(self, url: str) -> None:
        with self._cond:
            # The lease goes first: a reserved-for-UI worker keeps its semaphore
            # at 0 in THIS pool but must be leasable by the backend's pools.
            self._drop_lease(url)
            # UI is active and nothing is held yet — keep this freed worker idle
            # for the UI instead of returning it to the render pool. (It keeps the
            # semaphore at 0; _refresh_reservation releases it once the UI idles.)
            if self._reserved_url is None and url in self._sems and self._want_reserve():
                self._reserved_url = url
                logger.debug("WorkerPool: reserved freed %s for UI", url)
                self._cond.notify_all()
                return
            sem = self._sems.get(url)
            if sem:
                logger.debug("WorkerPool: released %s", url)
                sem.release()
                self._cond.notify_all()
        # else: worker was already removed via mark_failed — no-op

    def mark_failed(self, url: str) -> None:
        """Permanently remove a worker from the pool after a failure."""
        with self._cond:
            self._drop_lease(url)
            if url in self._sems:
                logger.warning("WorkerPool: %s failed, removing from pool", url)
                self._urls = [u for u in self._urls if u != url]
                del self._sems[url]
                if self._reserved_url == url:
                    self._reserved_url = None
                self._cond.notify_all()

    def has_healthy(self) -> bool:
        with self._lock:
            return bool(self._urls)

    def shutdown(self) -> None:
        """Stop the reservation poller. For clean teardown in tests; the render
        subprocess just exits and lets the daemon thread die."""
        with self._cond:
            self._closed = True
            for url in list(self._lease_fds):
                self._drop_lease(url)
            self._cond.notify_all()
