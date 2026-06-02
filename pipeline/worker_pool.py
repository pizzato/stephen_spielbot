"""Manages a pool of ComfyUI worker URLs for distributed video generation."""

import logging
from collections import deque
import threading
import urllib.request
import urllib.error

logger = logging.getLogger("video_gen")


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
    """Per-URL semaphore pool. Each worker handles one job at a time."""

    def __init__(self, urls: list[str]):
        if not urls:
            raise ValueError("WorkerPool needs at least one URL")
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._waiters: deque[object] = deque()
        self._urls = list(urls)
        self._sems: dict[str, threading.Semaphore] = {u: threading.Semaphore(1) for u in self._urls}

    @property
    def urls(self) -> list[str]:
        with self._lock:
            return list(self._urls)

    def acquire(self) -> str:
        """Block until any worker is free, return its URL.

        Keep callers FIFO so retries cannot be starved by later scene threads.
        """
        token = object()
        with self._cond:
            self._waiters.append(token)
            try:
                while True:
                    if not self._urls:
                        raise RuntimeError("No healthy workers remaining in pool")

                    is_turn = self._waiters and self._waiters[0] is token
                    if is_turn:
                        for url in list(self._urls):
                            sem = self._sems.get(url)
                            if sem and sem.acquire(blocking=False):
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
            sem = self._sems.get(url)
            if sem:
                logger.debug("WorkerPool: released %s", url)
                sem.release()
                self._cond.notify_all()
        # else: worker was already removed via mark_failed — no-op

    def mark_failed(self, url: str) -> None:
        """Permanently remove a worker from the pool after a failure."""
        with self._cond:
            if url in self._sems:
                logger.warning("WorkerPool: %s failed, removing from pool", url)
                self._urls = [u for u in self._urls if u != url]
                del self._sems[url]
                self._cond.notify_all()

    def has_healthy(self) -> bool:
        with self._lock:
            return bool(self._urls)
