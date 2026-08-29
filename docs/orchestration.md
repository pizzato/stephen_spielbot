# Durable Orchestration

A SQLite-backed durable layer gives renders a persisted source of truth for planning,
progress, ETAs, recovery, and cancel/retry — instead of relying only on process memory,
ComfyUI queue state, and files.

## The store

`pipeline/orchestrator.py`, class `DurableStore`. Database at
`~/.local/share/video-generator/orchestrator.sqlite3` (override with
`SPIELBOT_ORCHESTRATOR_DB`). WAL mode, foreign keys, 30 s busy timeout, thread-safe.

Tables: `jobs`, `tasks`, `task_dependencies`, `workers`, `artifacts`, `scenes`,
`task_events`, and `timing_stats` (learned per-kind durations that feed the ETA model).

Task states: `queued`, `leased`, `running`, `succeeded`, `failed_retryable`,
`failed_terminal`, `cancelled`, `lost`. Lease mechanics:

- `acquire_next_task` — expires stale leases, then atomically leases the
  highest-priority ready task whose dependencies have all succeeded, respecting
  `max_attempts`. Scheduling filters on the coarse `worker_kind` column
  (`comfy` / `tts` / `local` / `ui`).
- `expire_leases` / `recover_incomplete_tasks` — overdue or interrupted leased/running
  tasks become `lost` (ready for retry).
- `start_task` / `heartbeat_task` / `complete_task` / `fail_task` (retryable vs terminal
  by attempt count); the `TaskRun` context manager wraps the cycle.
- Worker registry: `register_worker`, `heartbeat_worker`, `quarantine_worker`
  (failure count + quarantined-until), `set_worker_status`.

## The generation plan

`ensure_generation_plan` builds a job's task DAG: a pre-completed `story.ready` root;
per scene image → narration → video → mux (an acted scene — dialogue, a singing take, or
a silent scene the style performs — instead gets a single `scene.performance.generate`
task); a `music.generate` unless music is off or every scene is dialogue (all-dialogue
clips carry their own audio out of the forward pass, so there is no score to write); and
a `video.finalize`. The music payload carries the film's music description, a song film's
lyrics, and the per-style music engine — though a song already rendered at script time
completes the task as skipped, with the track recorded as its artifact.
Every task is stamped with a `resource_class` (e.g. `comfy:image`, `comfy:video`, `tts`)
from config. Scenes are upserted as durable records; script-time previews let the plan
skip already-satisfied image tasks via artifacts.

## How renders actually execute

Two execution models coexist; **the monolithic path drives normal renders**:

- `/api/jobs/generate` registers the durable plan, then launches
  `resume_generation.py <work_dir>` as a subprocess (`_launch_generation_job` in
  `app.py`). That process renders everything itself using its own `WorkerPool`
  (parallel ComfyUI/TTS acquire/release, threaded dialogue rendering), while updating
  durable state through `TaskRun`/`complete_task`. Scenes marked
  `continues_previous` are grouped into **chains** (`pipeline/continuity.py`): a chain
  renders its scenes strictly in order — an acted chain holding one worker so each
  take can continue the motion context the previous one left there — while unrelated
  chains still fan out across the fleet in parallel, longest chains scheduled first. It re-runs `ensure_generation_plan`
  and `recover_incomplete_tasks` on start, so an interrupted render resumes without
  losing completed work. The durable store is its **progress/recovery ledger, not its
  scheduler** — it never calls `acquire_next_task`.
- `worker_agent.py` is the lease-based executor (acquire → execute → record artifact,
  with heartbeats). In production it runs only as the **cover agent**
  (`scripts/ui_worker.sh`, kind `ui`, task `ui.cover.generate`) so cover regeneration
  doesn't wait behind renders. Agents for `comfy`/`tts`/`local` kinds exist and can be
  run manually (`make worker-agent KIND=... ENDPOINT=...`) but nothing launches them
  automatically.

## API and UI surfaces

- `/api/progress` — job, task list, status counts, live workers, learned ETA.
- `/api/jobs/pause|resume|retry|cancel|delete` — retry requeues **all** failed/lost
  tasks of the job (`retry_failed_tasks`); cancel SIGTERMs the render process and marks
  the job cancelled.
- `/api/workers/status` — live read-only probes (`up`/`busy` per worker: ComfyUI queue,
  TTS `/health`). Nothing is persisted.
- `/api/workers/control` — start/stop/restart a host's worker containers over SSH
  (`scripts/worker.sh`).
- UI: **Progress** screen (task list with attempts and errors, status counts, per-kind
  ETA table, workers card, Pause / Retry failed / Resume / Cancel / Delete) and
  **Settings → Infra** container power controls.

## Not implemented

- **Per-scene / per-task retry** — retry is job-wide only.
- **Durable worker offline/online toggle** — `set_worker_status` exists in the store but
  has no endpoint or UI; the only worker control is container power over SSH.
- **Persisted worker health metrics** — no GPU/VRAM/disk/queue details are stored;
  `workers.metadata_json` is available but unpopulated.
- **Resource-class scheduling** — `resource_class` is stamped on every task but routing
  still keys off the coarse `worker_kind` only.
