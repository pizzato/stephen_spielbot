"""Manages a pool of ComfyUI worker URLs for distributed video generation."""

import logging
import threading
import time
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
            "Check your cluster.conf and that workers are running."
        )
    return ok


class WorkerPool:
    """Per-URL semaphore pool. Each worker handles one job at a time."""

    def __init__(self, urls: list[str]):
        if not urls:
            raise ValueError("WorkerPool needs at least one URL")
        self._lock = threading.Lock()
        self._urls = list(urls)
        self._sems: dict[str, threading.Semaphore] = {u: threading.Semaphore(1) for u in self._urls}

    @property
    def urls(self) -> list[str]:
        with self._lock:
            return list(self._urls)

    def acquire(self) -> str:
        """Block until any worker is free, return its URL."""
        while True:
            with self._lock:
                urls = list(self._urls)
            if not urls:
                raise RuntimeError("No healthy workers remaining in pool")
            for url in urls:
                sem = self._sems.get(url)
                if sem and sem.acquire(blocking=False):
                    logger.debug("WorkerPool: acquired %s", url)
                    return url
            time.sleep(0.5)

    def release(self, url: str) -> None:
        sem = self._sems.get(url)
        if sem:
            logger.debug("WorkerPool: released %s", url)
            sem.release()
        # else: worker was already removed via mark_failed — no-op

    def mark_failed(self, url: str) -> None:
        """Permanently remove a worker from the pool after a failure."""
        with self._lock:
            if url in self._sems:
                logger.warning("WorkerPool: %s failed, removing from pool", url)
                self._urls = [u for u in self._urls if u != url]
                del self._sems[url]

    def has_healthy(self) -> bool:
        with self._lock:
            return bool(self._urls)
