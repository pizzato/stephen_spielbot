"""Durable job/task orchestration for Stephen Spielbot.

This module is intentionally dependency-free.  It provides a small SQLite
controller database that can survive process restarts, track task attempts,
expire stale leases, and keep artifacts attached to the task that produced
them.  Generation state lives out of process memory in a durable source of
truth that survives restarts.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


APP_STATE_DIR = Path.home() / ".local" / "share" / "video-generator"
DEFAULT_DB_PATH = APP_STATE_DIR / "orchestrator.sqlite3"

TASK_QUEUED = "queued"
TASK_LEASED = "leased"
TASK_RUNNING = "running"
TASK_SUCCEEDED = "succeeded"
TASK_FAILED_RETRYABLE = "failed_retryable"
TASK_FAILED_TERMINAL = "failed_terminal"
TASK_CANCELLED = "cancelled"
TASK_LOST = "lost"

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_ERROR = "error"

READY_FOR_LEASE = (TASK_QUEUED, TASK_FAILED_RETRYABLE, TASK_LOST)


@dataclass(frozen=True)
class TaskRecord:
    id: str
    job_id: str
    kind: str
    name: str
    status: str
    worker_kind: str
    priority: int
    attempt: int
    max_attempts: int
    payload: dict[str, Any]
    result: dict[str, Any]
    lease_owner: str | None
    lease_expires_at: float | None
    error: str | None


def now_ts() -> float:
    return time.time()


def json_dumps(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    data = json.loads(value)
    return data if isinstance(data, dict) else {}


def job_id_from_work_dir(work_dir: Path | str) -> str:
    """Return a stable compact job id for a work directory."""
    resolved = str(Path(work_dir).expanduser().resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]
    return f"job_{digest}"


def task_id(job_id: str, *parts: object) -> str:
    safe = [str(p).replace(" ", "_").replace("/", "_") for p in parts]
    return ":".join([job_id, *safe])


def worker_id(kind: str, endpoint: str) -> str:
    digest = hashlib.sha1(f"{kind}:{endpoint}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}_{digest}"


def _scene_value(scene: Any, key: str, default: Any = None) -> Any:
    if isinstance(scene, dict):
        return scene.get(key, default)
    return getattr(scene, key, default)


# Task kinds whose run durations we learn, to predict render ETA.  Maps the
# orchestrator task ``kind`` to a short, stable label used as the timing key.
TIMING_KIND_LABELS = {
    "scene.image.generate": "image",
    "scene.video.generate": "video",
    "scene.narration.generate": "narration",
    "music.generate": "music",
    "scene.video.mux": "mux",
    "video.finalize": "finalize",
}

# Labels whose duration depends on output resolution (the rest are flat).
TIMING_RES_SENSITIVE = {"image", "video", "finalize"}


def timing_signature(kind: str, payload: dict[str, Any] | None) -> str | None:
    """Stable key grouping "similar setup" tasks for duration learning.

    Resolution-sensitive kinds (image/video/finalize) are keyed by output
    dimensions, so an FHD scene and an SD scene are learned separately — that
    distinction is what drives render time.  Returns ``None`` for kinds we
    don't predict (e.g. the synthetic ``story.ready`` marker).
    """
    label = TIMING_KIND_LABELS.get(kind)
    if label is None:
        return None
    if label in TIMING_RES_SENSITIVE:
        payload = payload or {}
        w, h = payload.get("vid_width"), payload.get("vid_height")
        res = f"{int(w)}x{int(h)}" if w and h else "unknown"
        return f"{label}|{res}"
    return label


def _file_checksum(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class DurableStore:
    """SQLite-backed controller for jobs, task leases, workers, and artifacts."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self.init_schema()

    @classmethod
    def default(cls) -> "DurableStore":
        return cls(default_db_path())

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    work_dir TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    progress_pct REAL NOT NULL DEFAULT 0,
                    progress_message TEXT NOT NULL DEFAULT '',
                    config_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    final_path TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_kind TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    started_at REAL,
                    finished_at REAL,
                    last_heartbeat_at REAL,
                    progress_pct REAL NOT NULL DEFAULT 0,
                    progress_message TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_dependencies (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    depends_on_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    PRIMARY KEY (task_id, depends_on_id)
                );

                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    capacity INTEGER NOT NULL DEFAULT 1,
                    active_task_id TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    quarantined_until REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_heartbeat_at REAL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    checksum TEXT,
                    size_bytes INTEGER,
                    duration_seconds REAL,
                    width INTEGER,
                    height INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scenes (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    scene_id INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    image_prompt TEXT NOT NULL DEFAULT '',
                    video_prompt TEXT NOT NULL DEFAULT '',
                    narration TEXT NOT NULL DEFAULT '',
                    preview_path TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (job_id, scene_id)
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    task_id TEXT,
                    worker_id TEXT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                -- Learned per-task-kind durations, used to predict render ETA.
                -- One row per "signature" (e.g. "video|1920x1080"); avg_seconds
                -- is a running mean that adapts to the current cluster.
                CREATE TABLE IF NOT EXISTS timing_stats (
                    signature TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    avg_seconds REAL NOT NULL DEFAULT 0,
                    last_seconds REAL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_job_status
                    ON tasks(job_id, status);
                CREATE INDEX IF NOT EXISTS idx_tasks_ready
                    ON tasks(worker_kind, status, priority, created_at);
                CREATE INDEX IF NOT EXISTS idx_deps_depends
                    ON task_dependencies(depends_on_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_job
                    ON artifacts(job_id, kind);
                CREATE INDEX IF NOT EXISTS idx_scenes_job
                    ON scenes(job_id, scene_id);
                """
            )

    def create_or_update_job(
        self,
        job_id: str,
        work_dir: Path | str,
        title: str,
        config: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = JOB_PENDING,
    ) -> None:
        ts = now_ts()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    id, work_dir, title, status, config_json, metadata_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    work_dir=excluded.work_dir,
                    title=excluded.title,
                    config_json=excluded.config_json,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    job_id,
                    str(Path(work_dir).expanduser().resolve()),
                    title or "",
                    status,
                    json_dumps(config),
                    json_dumps(metadata),
                    ts,
                    ts,
                ),
            )

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress_pct: float | None = None,
        progress_message: str | None = None,
        final_path: Path | str | None = None,
        error: str | None = None,
    ) -> None:
        assignments: list[str] = ["updated_at=?"]
        values: list[Any] = [now_ts()]
        if status is not None:
            assignments.append("status=?")
            values.append(status)
            if status == JOB_RUNNING:
                assignments.append("started_at=COALESCE(started_at, ?)")
                values.append(now_ts())
            if status in (JOB_DONE, JOB_ERROR):
                assignments.append("finished_at=?")
                values.append(now_ts())
        if progress_pct is not None:
            assignments.append("progress_pct=?")
            values.append(float(progress_pct))
        if progress_message is not None:
            assignments.append("progress_message=?")
            values.append(progress_message)
        if final_path is not None:
            assignments.append("final_path=?")
            values.append(str(final_path))
        if error is not None:
            assignments.append("error=?")
            values.append(error)
        values.append(job_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id=?",
                values,
            )

    def create_task(
        self,
        task_id_value: str,
        job_id: str,
        kind: str,
        name: str,
        *,
        worker_kind: str,
        payload: dict[str, Any] | None = None,
        dependencies: Iterable[str] = (),
        priority: int = 100,
        max_attempts: int = 3,
        status: str = TASK_QUEUED,
    ) -> None:
        ts = now_ts()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tasks (
                    id, job_id, kind, name, status, worker_kind, priority,
                    payload_json, max_attempts, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    name=excluded.name,
                    worker_kind=excluded.worker_kind,
                    priority=excluded.priority,
                    payload_json=excluded.payload_json,
                    max_attempts=excluded.max_attempts,
                    updated_at=excluded.updated_at
                """,
                (
                    task_id_value,
                    job_id,
                    kind,
                    name,
                    status,
                    worker_kind,
                    int(priority),
                    json_dumps(payload),
                    int(max_attempts),
                    ts,
                    ts,
                ),
            )
            for dep in dependencies:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_id)
                    VALUES (?, ?)
                    """,
                    (task_id_value, dep),
                )

    def update_task_payload(self, task_id_value: str, updates: dict[str, Any]) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM tasks WHERE id=?",
                (task_id_value,),
            ).fetchone()
            payload = json_loads(row["payload_json"]) if row else {}
            payload.update(updates)
            self._conn.execute(
                "UPDATE tasks SET payload_json=?, updated_at=? WHERE id=?",
                (json_dumps(payload), now_ts(), task_id_value),
            )

    def ensure_generation_plan(
        self,
        job_id: str,
        work_dir: Path | str,
        title: str,
        scenes: Iterable[Any],
        config: dict[str, Any],
    ) -> None:
        scene_items = list(scenes)
        metadata = {"scene_count": len(scene_items)}
        self.create_or_update_job(job_id, work_dir, title, config, metadata)
        self.upsert_scenes(job_id, scene_items)

        root = task_id(job_id, "story")
        self.create_task(
            root,
            job_id,
            "story.ready",
            "Story/script ready",
            worker_kind="local",
            payload={"work_dir": str(work_dir), "title": title},
            priority=0,
            max_attempts=1,
            status=TASK_SUCCEEDED,
        )
        self.complete_task(root, result={"already_completed": True})

        common_scene_payload = {
            "vid_width": config.get("vid_width"),
            "vid_height": config.get("vid_height"),
            "max_clip_secs": config.get("max_clip_secs"),
            "lora_strength": config.get("lora_strength"),
            "first_pass_cfg": config.get("first_pass_cfg"),
            "first_pass_steps": config.get("first_pass_steps"),
            "second_pass_cfg": config.get("second_pass_cfg"),
            "second_pass_steps": config.get("second_pass_steps"),
            "flux_model": config.get("flux_model"),
            "flux_clip_t5": config.get("flux_clip_t5"),
            "flux_clip_l": config.get("flux_clip_l"),
            "flux_vae": config.get("flux_vae"),
            "flux_steps": config.get("flux_steps"),
            "voice_ref": config.get("voice_ref"),
            "voice_robotic": config.get("voice_robotic"),
            "voice_robotic_amount": config.get("voice_robotic_amount"),
            "voice_speed": config.get("voice_speed"),
            "tts_engine": config.get("tts_engine"),
        }
        resource_classes = config.get("resource_classes", {}) or {}

        narration_task_ids: list[str] = []
        mux_task_ids: list[str] = []
        last_scene_id = int(_scene_value(scene_items[-1], "id")) if scene_items else None
        for scene in scene_items:
            sid = int(_scene_value(scene, "id"))
            scene_payload = {
                "work_dir": str(work_dir),
                "scene_id": sid,
                "title": _scene_value(scene, "title", f"Scene {sid}"),
                "image_prompt": _scene_value(scene, "image_prompt", ""),
                "video_prompt": _scene_value(scene, "video_prompt", ""),
                "negative_prompt": _scene_value(scene, "negative_prompt", ""),
                "narration": _scene_value(scene, "narration", ""),
            }
            scene_payload.update({k: v for k, v in common_scene_payload.items() if v is not None})
            image_task = task_id(job_id, "scene", sid, "image")
            narration_task = task_id(job_id, "scene", sid, "narration")
            video_task = task_id(job_id, "scene", sid, "video")
            mux_task = task_id(job_id, "scene", sid, "mux")
            narration_task_ids.append(narration_task)
            mux_task_ids.append(mux_task)

            self.create_task(
                image_task,
                job_id,
                "scene.image.generate",
                f"Scene {sid} image",
                worker_kind="comfy",
                payload={**scene_payload, "resource_class": resource_classes.get("image", "comfy:image")},
                dependencies=[root],
                priority=10 + sid,
                max_attempts=3,
            )
            stored_scene = self.get_scene(job_id, sid) or {}
            preview_path = stored_scene.get("preview_path", "")
            if preview_path:
                self.skip_task_if_artifact_exists(
                    image_task,
                    preview_path,
                    artifact_kind="image",
                    result={"source": "script_preview"},
                )
            self.create_task(
                narration_task,
                job_id,
                "scene.narration.generate",
                f"Scene {sid} narration",
                worker_kind="tts",
                payload={**scene_payload, "resource_class": resource_classes.get("narration", "tts")},
                dependencies=[root],
                priority=20 + sid,
                max_attempts=3,
            )
            self.create_task(
                video_task,
                job_id,
                "scene.video.generate",
                f"Scene {sid} video",
                worker_kind="comfy",
                payload={**scene_payload, "resource_class": resource_classes.get("video", "comfy:video")},
                dependencies=[image_task, narration_task],
                priority=40 + sid,
                max_attempts=3,
            )
            self.create_task(
                mux_task,
                job_id,
                "scene.video.mux",
                f"Scene {sid} mux",
                worker_kind="local",
                payload={**scene_payload, "resource_class": resource_classes.get("mux", "local"), "is_last_scene": sid == last_scene_id},
                dependencies=[video_task, narration_task],
                priority=70 + sid,
                max_attempts=2,
            )

        music_task = task_id(job_id, "music")
        self.create_task(
            music_task,
            job_id,
            "music.generate",
            "Background music",
            worker_kind="comfy",
            payload={
                "work_dir": str(work_dir),
                "title": title,
                "music_desc": config.get("music_desc", ""),
                "output_path": str(Path(work_dir) / "background_music.wav"),
                "resource_class": resource_classes.get("music", "comfy:music"),
            },
            dependencies=narration_task_ids or [root],
            priority=30,
            max_attempts=3,
        )

        final_task = task_id(job_id, "final")
        self.create_task(
            final_task,
            job_id,
            "video.finalize",
            "Final assembly",
            worker_kind="local",
            payload={
                "work_dir": str(work_dir),
                "title": title,
                "scene_count": len(scene_items),
                "final_path": str(Path(work_dir).with_suffix(".mp4")),
                "music_vol": config.get("music_vol"),
                "voice_vol": config.get("voice_vol"),
                "ambient_vol": config.get("ambient_vol"),
                "vid_width": config.get("vid_width"),
                "vid_height": config.get("vid_height"),
                "resource_class": resource_classes.get("finalize", "local"),
            },
            dependencies=[music_task, *mux_task_ids],
            priority=200,
            max_attempts=2,
        )

    def upsert_scene(
        self,
        job_id: str,
        scene_id: int,
        *,
        title: str = "",
        image_prompt: str = "",
        video_prompt: str = "",
        narration: str = "",
        preview_path: Path | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ts = now_ts()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO scenes (
                    job_id, scene_id, title, image_prompt, video_prompt,
                    narration, preview_path, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, scene_id) DO UPDATE SET
                    title=excluded.title,
                    image_prompt=excluded.image_prompt,
                    video_prompt=excluded.video_prompt,
                    narration=excluded.narration,
                    preview_path=CASE
                        WHEN excluded.preview_path != '' THEN excluded.preview_path
                        ELSE scenes.preview_path
                    END,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    job_id,
                    int(scene_id),
                    title or "",
                    image_prompt or "",
                    video_prompt or "",
                    narration or "",
                    str(preview_path or ""),
                    json_dumps(metadata),
                    ts,
                    ts,
                ),
            )

    def upsert_scenes(self, job_id: str, scenes: Iterable[Any]) -> None:
        for scene in scenes:
            self.upsert_scene(
                job_id,
                int(_scene_value(scene, "id")),
                title=_scene_value(scene, "title", ""),
                image_prompt=_scene_value(scene, "image_prompt", ""),
                video_prompt=_scene_value(scene, "video_prompt", ""),
                narration=_scene_value(scene, "narration", ""),
                preview_path=_scene_value(scene, "preview_path", "")
                or _scene_value(scene, "preview", ""),
                metadata=_scene_value(scene, "metadata", {}),
            )

    def update_scene_preview(self, job_id: str, scene_id: int, preview_path: Path | str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE scenes
                SET preview_path=?, updated_at=?
                WHERE job_id=? AND scene_id=?
                """,
                (str(preview_path), now_ts(), job_id, int(scene_id)),
            )

    def scene_count(self, job_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM scenes WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def get_scene(self, job_id: str, scene_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM scenes
                WHERE job_id=? AND scene_id=?
                """,
                (job_id, int(scene_id)),
            ).fetchone()
        return self._row_to_scene(row) if row else None

    def scene_rows(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM scenes
                WHERE job_id=?
                ORDER BY scene_id ASC
                """,
                (job_id,),
            ).fetchall()
        return [self._row_to_scene(row) for row in rows]

    def recover_incomplete_tasks(self, job_id: str) -> None:
        """Release stale in-process state after a controller/generator restart."""
        ts = now_ts()
        with self._lock:
            self._conn.execute(
                """
                UPDATE tasks
                SET status=?, lease_owner=NULL, lease_expires_at=NULL,
                    error=COALESCE(error, 'recovered after process restart'),
                    updated_at=?
                WHERE job_id=? AND status IN (?, ?)
                """,
                (TASK_LOST, ts, job_id, TASK_LEASED, TASK_RUNNING),
            )

    def retry_failed_tasks(self, job_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE tasks
                SET status=?, lease_owner=NULL, lease_expires_at=NULL,
                    error=NULL, progress_message='', updated_at=?
                WHERE job_id=?
                  AND status IN (?, ?, ?)
                  AND attempt < max_attempts
                """,
                (
                    TASK_QUEUED,
                    now_ts(),
                    job_id,
                    TASK_FAILED_RETRYABLE,
                    TASK_FAILED_TERMINAL,
                    TASK_LOST,
                ),
            )
            return int(cur.rowcount or 0)

    def cancel_job(self, job_id: str, reason: str = "cancelled by user") -> int:
        ts = now_ts()
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs
                SET status=?, error=?, finished_at=?, updated_at=?
                WHERE id=?
                """,
                (JOB_ERROR, reason, ts, ts, job_id),
            )
            cur = self._conn.execute(
                """
                UPDATE tasks
                SET status=?, lease_owner=NULL, lease_expires_at=NULL,
                    error=?, updated_at=?
                WHERE job_id=?
                  AND status NOT IN (?, ?)
                """,
                (TASK_CANCELLED, reason, ts, job_id, TASK_SUCCEEDED, TASK_CANCELLED),
            )
            return int(cur.rowcount or 0)

    def set_worker_status(self, worker_id_value: str, status: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE workers
                SET status=?, quarantined_until=NULL, updated_at=?
                WHERE id=?
                """,
                (status, now_ts(), worker_id_value),
            )
            return bool(cur.rowcount)

    def expire_leases(self, *, before: float | None = None) -> int:
        cutoff = before if before is not None else now_ts()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE tasks
                SET status=?, lease_owner=NULL, lease_expires_at=NULL,
                    error='lease expired before task completed',
                    updated_at=?
                WHERE status IN (?, ?)
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (TASK_LOST, now_ts(), TASK_LEASED, TASK_RUNNING, cutoff),
            )
            return int(cur.rowcount or 0)

    def register_worker(
        self,
        worker_id_value: str,
        kind: str,
        endpoint: str,
        *,
        status: str = "online",
        capacity: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ts = now_ts()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO workers (
                    id, kind, endpoint, status, capacity, metadata_json,
                    created_at, updated_at, last_heartbeat_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    endpoint=excluded.endpoint,
                    status=excluded.status,
                    capacity=excluded.capacity,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at,
                    last_heartbeat_at=excluded.last_heartbeat_at
                """,
                (
                    worker_id_value,
                    kind,
                    endpoint,
                    status,
                    int(capacity),
                    json_dumps(metadata),
                    ts,
                    ts,
                    ts,
                ),
            )

    def heartbeat_worker(
        self,
        worker_id_value: str,
        *,
        active_task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        updates = ["updated_at=?", "last_heartbeat_at=?", "active_task_id=?"]
        values: list[Any] = [now_ts(), now_ts(), active_task_id]
        if metadata is not None:
            updates.append("metadata_json=?")
            values.append(json_dumps(metadata))
        values.append(worker_id_value)
        with self._lock:
            self._conn.execute(
                f"UPDATE workers SET {', '.join(updates)} WHERE id=?",
                values,
            )

    def quarantine_worker(self, worker_id_value: str, seconds: int, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE workers
                SET status='quarantined',
                    quarantined_until=?,
                    failure_count=failure_count + 1,
                    updated_at=?
                WHERE id=?
                """,
                (now_ts() + seconds, now_ts(), worker_id_value),
            )
        self.record_event("", None, worker_id_value, "warning", reason)

    def acquire_next_task(
        self,
        worker_id_value: str,
        worker_kind: str,
        *,
        lease_seconds: int = 900,
    ) -> TaskRecord | None:
        self.expire_leases()
        ts = now_ts()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.*
                FROM tasks t
                WHERE t.worker_kind = ?
                  AND t.status IN (?, ?, ?)
                  AND t.attempt < t.max_attempts
                  AND NOT EXISTS (
                    SELECT 1
                    FROM task_dependencies d
                    JOIN tasks dep ON dep.id = d.depends_on_id
                    WHERE d.task_id = t.id
                      AND dep.status != ?
                  )
                ORDER BY t.priority ASC, t.created_at ASC
                LIMIT 1
                """,
                (worker_kind, *READY_FOR_LEASE, TASK_SUCCEEDED),
            ).fetchall()
            if not rows:
                return None
            row = rows[0]
            self._conn.execute(
                """
                UPDATE tasks
                SET status=?, lease_owner=?, lease_expires_at=?,
                    attempt=attempt + 1, updated_at=?
                WHERE id=? AND status IN (?, ?, ?)
                """,
                (
                    TASK_LEASED,
                    worker_id_value,
                    ts + lease_seconds,
                    ts,
                    row["id"],
                    *READY_FOR_LEASE,
                ),
            )
            leased = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?",
                (row["id"],),
            ).fetchone()
        return self._row_to_task(leased) if leased else None

    def start_task(
        self,
        task_id_value: str,
        *,
        worker_id_value: str | None = None,
        lease_seconds: int = 900,
        message: str = "",
    ) -> None:
        ts = now_ts()
        with self._lock:
            self._conn.execute(
                """
                UPDATE tasks
                SET status=?, lease_owner=COALESCE(?, lease_owner),
                    lease_expires_at=?, started_at=COALESCE(started_at, ?),
                    last_heartbeat_at=?, progress_message=?,
                    attempt=CASE
                        WHEN status IN ('queued', 'failed_retryable', 'lost')
                        THEN attempt + 1
                        ELSE attempt
                    END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    TASK_RUNNING,
                    worker_id_value,
                    ts + lease_seconds,
                    ts,
                    ts,
                    message,
                    ts,
                    task_id_value,
                ),
            )

    def heartbeat_task(
        self,
        task_id_value: str,
        *,
        lease_seconds: int = 900,
        progress_pct: float | None = None,
        message: str | None = None,
    ) -> None:
        ts = now_ts()
        updates = ["last_heartbeat_at=?", "lease_expires_at=?", "updated_at=?"]
        values: list[Any] = [ts, ts + lease_seconds, ts]
        if progress_pct is not None:
            updates.append("progress_pct=?")
            values.append(float(progress_pct))
        if message is not None:
            updates.append("progress_message=?")
            values.append(message)
        values.append(task_id_value)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id=?",
                values,
            )

    def complete_task(
        self,
        task_id_value: str,
        *,
        result: dict[str, Any] | None = None,
        message: str = "",
    ) -> None:
        ts = now_ts()
        with self._lock:
            prior = self._conn.execute(
                "SELECT kind, payload_json, started_at, attempt FROM tasks WHERE id=?",
                (task_id_value,),
            ).fetchone()
            self._conn.execute(
                """
                UPDATE tasks
                SET status=?, result_json=?, lease_owner=NULL,
                    lease_expires_at=NULL, finished_at=?, progress_pct=100,
                    progress_message=?, error=NULL, updated_at=?
                WHERE id=?
                """,
                (
                    TASK_SUCCEEDED,
                    json_dumps(result),
                    ts,
                    message,
                    ts,
                    task_id_value,
                ),
            )
            if prior is not None:
                self._learn_timing_from_completion(prior, ts)

    # ── timing learning (render-ETA prediction) ──────────────────────────────

    # Ignore samples outside this range: sub-second completions are skips/no-ops
    # and multi-hour ones are almost certainly clock skew or a hung lease.
    _TIMING_MIN_SECONDS = 0.75
    _TIMING_MAX_SECONDS = 6 * 3600.0

    def _learn_timing_from_completion(self, row: sqlite3.Row, finished_at: float) -> None:
        """Record a duration sample from a just-completed task row.

        Only clean, first-attempt successes with a real ``started_at`` feed the
        model, so skipped tasks (no start) and retry-spanning durations don't
        pollute the averages.  Never raises — timing is best-effort bookkeeping.
        """
        try:
            started_at = row["started_at"]
            if started_at is None or int(row["attempt"] or 0) > 1:
                return
            elapsed = float(finished_at) - float(started_at)
            if not (self._TIMING_MIN_SECONDS <= elapsed <= self._TIMING_MAX_SECONDS):
                return
            sig = timing_signature(row["kind"], json_loads(row["payload_json"]))
            if sig is None:
                return
            self._record_timing_sample(sig, row["kind"], elapsed, finished_at)
        except Exception:
            pass

    def _record_timing_sample(
        self, signature: str, kind: str, seconds: float, ts: float
    ) -> None:
        # Caller must hold ``self._lock``.  Running mean that behaves as a true
        # average for the first few samples then settles into a light EMA so it
        # tracks the current cluster's hardware rather than ancient runs.
        existing = self._conn.execute(
            "SELECT sample_count, avg_seconds FROM timing_stats WHERE signature=?",
            (signature,),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO timing_stats
                    (signature, kind, sample_count, avg_seconds, last_seconds, updated_at)
                VALUES (?, ?, 1, ?, ?, ?)
                """,
                (signature, kind, seconds, seconds, ts),
            )
            return
        count = int(existing["sample_count"])
        alpha = max(1.0 / (count + 1), 0.25)
        new_avg = (1.0 - alpha) * float(existing["avg_seconds"]) + alpha * seconds
        self._conn.execute(
            """
            UPDATE timing_stats
            SET sample_count=?, avg_seconds=?, last_seconds=?, updated_at=?
            WHERE signature=?
            """,
            (count + 1, new_avg, seconds, ts, signature),
        )

    def record_timing_sample(
        self, kind: str, payload: dict[str, Any], seconds: float
    ) -> None:
        """Public hook to feed a duration sample (used by tests/external paths)."""
        sig = timing_signature(kind, payload)
        if sig is None or seconds <= 0:
            return
        with self._lock:
            self._record_timing_sample(sig, kind, float(seconds), now_ts())

    def timing_table(self) -> dict[str, dict[str, Any]]:
        """Learned durations: {signature: {kind, avg_seconds, sample_count}}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT signature, kind, sample_count, avg_seconds FROM timing_stats",
            ).fetchall()
        return {
            r["signature"]: {
                "kind": r["kind"],
                "avg_seconds": float(r["avg_seconds"]),
                "sample_count": int(r["sample_count"]),
            }
            for r in rows
        }

    def fail_task(
        self,
        task_id_value: str,
        error: Exception | str,
        *,
        retryable: bool = True,
        message: str = "",
    ) -> str:
        err = str(error)
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt, max_attempts FROM tasks WHERE id=?",
                (task_id_value,),
            ).fetchone()
            if not row:
                return TASK_FAILED_TERMINAL
            can_retry = retryable and int(row["attempt"]) < int(row["max_attempts"])
            status = TASK_FAILED_RETRYABLE if can_retry else TASK_FAILED_TERMINAL
            self._conn.execute(
                """
                UPDATE tasks
                SET status=?, lease_owner=NULL, lease_expires_at=NULL,
                    error=?, progress_message=?, updated_at=?
                WHERE id=?
                """,
                (status, err, message or err[:240], now_ts(), task_id_value),
            )
            return status

    def skip_task_if_artifact_exists(
        self,
        task_id_value: str,
        path: Path | str,
        *,
        artifact_kind: str,
        min_size: int = 1,
        result: dict[str, Any] | None = None,
    ) -> bool:
        p = Path(path)
        if p.exists() and p.stat().st_size >= min_size:
            self.complete_task(task_id_value, result={**(result or {}), "path": str(p), "skipped": True})
            row = self.get_task(task_id_value)
            if row:
                self.record_artifact(row.job_id, task_id_value, artifact_kind, p)
            return True
        return False

    def record_artifact(
        self,
        job_id: str,
        task_id_value: str | None,
        kind: str,
        path: Path | str,
        *,
        duration_seconds: float | None = None,
        width: int | None = None,
        height: int | None = None,
        metadata: dict[str, Any] | None = None,
        checksum: bool = False,
    ) -> None:
        p = Path(path)
        size = p.stat().st_size if p.exists() else None
        digest = _file_checksum(p) if checksum and p.exists() else None
        artifact_id = hashlib.sha1(f"{job_id}:{task_id_value}:{kind}:{p}".encode("utf-8")).hexdigest()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO artifacts (
                    id, job_id, task_id, kind, path, checksum, size_bytes,
                    duration_seconds, width, height, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    checksum=excluded.checksum,
                    size_bytes=excluded.size_bytes,
                    duration_seconds=excluded.duration_seconds,
                    width=excluded.width,
                    height=excluded.height,
                    metadata_json=excluded.metadata_json,
                    created_at=excluded.created_at
                """,
                (
                    artifact_id,
                    job_id,
                    task_id_value,
                    kind,
                    str(p),
                    digest,
                    size,
                    duration_seconds,
                    width,
                    height,
                    json_dumps(metadata),
                    now_ts(),
                ),
            )

    def record_event(
        self,
        job_id: str,
        task_id_value: str | None,
        worker_id_value: str | None,
        level: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO task_events (
                    job_id, task_id, worker_id, level, message, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    task_id_value,
                    worker_id_value,
                    level,
                    message,
                    json_dumps(metadata),
                    now_ts(),
                ),
            )

    def get_task(self, task_id_value: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?",
                (task_id_value,),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def get_job_by_work_dir(self, work_dir: Path | str) -> sqlite3.Row | None:
        resolved = str(Path(work_dir).expanduser().resolve())
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM jobs WHERE work_dir=?",
                (resolved,),
            ).fetchone()

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()

    def recent_jobs(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            )

    def task_rows(self, job_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE job_id=?
                    ORDER BY priority ASC, created_at ASC
                    """,
                    (job_id,),
                ).fetchall()
            )

    def worker_rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM workers ORDER BY kind, endpoint",
                ).fetchall()
            )

    def job_summary(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            counts = self._conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM tasks
                WHERE job_id=?
                GROUP BY status
                """,
                (job_id,),
            ).fetchall()
        return {
            "job": dict(job) if job else None,
            "counts": {row["status"]: row["n"] for row in counts},
        }

    def _row_to_scene(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["scene_id"]),
            "title": row["title"],
            "image_prompt": row["image_prompt"],
            "video_prompt": row["video_prompt"],
            "narration": row["narration"],
            "preview_path": row["preview_path"],
            "metadata": json_loads(row["metadata_json"]),
        }

    def _row_to_task(self, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            job_id=row["job_id"],
            kind=row["kind"],
            name=row["name"],
            status=row["status"],
            worker_kind=row["worker_kind"],
            priority=int(row["priority"]),
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            payload=json_loads(row["payload_json"]),
            result=json_loads(row["result_json"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            error=row["error"],
        )


class TaskRun:
    """Context manager for updating durable task state around existing code."""

    def __init__(
        self,
        store: DurableStore,
        task_id_value: str,
        *,
        worker_id_value: str | None = None,
        lease_seconds: int = 900,
        retryable: bool = True,
        start_message: str = "",
    ):
        self.store = store
        self.task_id = task_id_value
        self.worker_id = worker_id_value
        self.lease_seconds = lease_seconds
        self.retryable = retryable
        self.start_message = start_message

    def __enter__(self) -> "TaskRun":
        self.store.start_task(
            self.task_id,
            worker_id_value=self.worker_id,
            lease_seconds=self.lease_seconds,
            message=self.start_message,
        )
        return self

    def heartbeat(self, progress_pct: float | None = None, message: str | None = None) -> None:
        self.store.heartbeat_task(
            self.task_id,
            lease_seconds=self.lease_seconds,
            progress_pct=progress_pct,
            message=message,
        )

    def complete(self, result: dict[str, Any] | None = None, message: str = "") -> None:
        self.store.complete_task(self.task_id, result=result, message=message)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.store.fail_task(self.task_id, exc, retryable=self.retryable)
        return False


def default_db_path() -> Path:
    env = os.environ.get("SPIELBOT_ORCHESTRATOR_DB")
    return Path(env).expanduser() if env else DEFAULT_DB_PATH
