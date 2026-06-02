# Durable Orchestration Status

This document tracks the durable orchestration work so future sessions can
continue without rediscovering the implementation state.

## Implemented

- SQLite controller database at `~/.local/share/video-generator/orchestrator.sqlite3`.
- Durable jobs, tasks, dependencies, workers, artifacts, events, and scenes.
- Task leases, retries, lease expiry, worker heartbeat, and worker quarantine.
- Worker agent entry point for `comfy`, `tts`, and `local` task kinds.
- Web app Render screen with durable job, task, and worker status.
- Script tab uses one active scene editor backed by durable scene records.
- Script review stores full scene data in SQLite and `script.json`, not in
  page-sized in-memory UI state.
- Background generation launch reads scenes from durable storage and writes a
  task graph before starting `resume_generation.py`.

## Pending

- UI controls for retry failed task, retry scene, cancel job, mark worker
  offline/online, and restart worker ComfyUI.
- Worker health probes that persist GPU utilization, VRAM, disk, ComfyUI queue
  status, prompt id, workflow hash, seed, and last node details for every
  attempt.
- Full resource-class scheduler enforcement for `comfy:image`, `comfy:music`,
  `comfy:video`, `tts`, and `local`.
- Final Chrome validation. The in-app Browser test path works; Chrome plugin
  attachment has failed locally and should be retried before signoff.
- Long 1080p end-to-end validation.

## Test Matrix

| Scenario | Topic | Settings | Expected result |
|---|---|---|---|
| Unit tests | n/a | SQLite temp DB | Scene persistence, leases, dependency gating, and task payloads pass. |
| Browser UI smoke | n/a | Local app at `127.0.0.1:7870` | Create, Script, and Progress tabs load without console errors. |
| Script scale | History of Writing | 30 scenes | Script tab renders one editor and a lightweight scene summary. |
| Low-quality generation | History of Paper | 2 scenes, fast landscape, short duration | Durable job launches, task graph appears, final artifact is produced. |
| Recovery | History of Clocks | 5 scenes | Interrupted leased work expires or resumes without losing completed artifacts. |
| Final acceptance | History of Navigation | Long 1080p landscape | Final video, audio muxing, durable task completion, and no Script tab memory blowup. |

## Browser Notes

- Use the in-app Browser plugin for interim UI tests against
  `http://127.0.0.1:7870/`.
- Retry Chrome after user/plugin setup. If Chrome still cannot attach, record
  the blocker here and keep Browser results as interim evidence only.

## Latest Local Verification

- Unit tests passed with `python -m unittest tests.test_orchestrator tests.test_script_editor`.
- Browser smoke test on `127.0.0.1:7870` loaded Create, Script, and Progress
  without console errors.
- A 30-scene `History of Writing` script rendered one active scene editor, one
  title field, one image prompt field, one video prompt field, and a compact
  scene summary instead of 30 mounted scene editors.
- A low-quality `History of Paper` two-scene job was launched from the browser
  and completed with all 11 durable tasks succeeded.
