# Browsing performance

The 22 October 2026 review found that repeated settings parsing dominated the Films
request. SQLite was a smaller cost; much of the browsing data lives in JSON files and
video folders rather than in the database.

## Measurements

The local sample contained 57 finished films, 161 saved scripts, a 140 KB settings file,
and 63.7 MiB of PNG covers (about 1.1 MiB each). The durable database contained 1,994 jobs
and 30,273 tasks, including older jobs no longer represented by local video folders.

| Check | Before | After |
|---|---:|---:|
| Full `/api/jobs` handler, same local metadata, without profiler | 4.450 s | 0.164 s |
| Settings loads per full jobs request | 43 | 1 |
| Publish queue reads per full jobs request | 35 | 1 |
| Initial JavaScript bundle, minified | 764.66 KB | 229.13 KB |
| Initial JavaScript bundle, gzip estimate | 205.34 KB | 69.94 KB |
| Recent 10 jobs SQL query, median of 30 runs on a database copy | 16.751 ms | 0.272 ms |
| Tasks for the job with most tasks, median of 30 runs on the copy | 0.691 ms | 0.567 ms |

These are local measurements, not deployed end-to-end browser timings. The jobs timings
call the handler directly, without network transfer, image download or React rendering.
Imports and durable-store fallbacks used a scratch state directory; the comparison reads
the same real film metadata and settings without changing them. The database benchmark
used a disposable SQLite backup; the original database was opened read-only.

A separate profile took 12.792 s, including profiler overhead: 12.635 s was inside
`load_config`, almost entirely YAML parsing. Do not compare that profiled time with the
unprofiled results above.

## Implemented changes

- **Settings parsing is cached by file version.** Each read checks inode, nanosecond
  modification/change timestamps and size. Saves, external edits and atomic replacements
  invalidate the entry on the next read. Callers receive a deep copy of the parsed YAML,
  so an unsaved edit cannot modify the cache. Normalisation still runs on each read;
  failed parses are not cached. There is no time-to-live delay for saved changes.
- **Film listings reuse shared data.** Destination names use the request's settings and
  resolved styles. Approval lookup uses one publish-queue snapshot. Each distinct work
  directory's config is read once per request, including when it appears in both the
  finished and script lists. Legacy title/style fallbacks still use SQLite.
- **Each screen asks for its own data.** `/api/jobs?view=films` omits scripts,
  `view=scripts` skips film/publishing metadata, and `view=recent` builds only five
  non-archived film cards for Home. The default `view=all` preserves the existing response.
  Every view retains the `finished`, `scripts` and `resumable` keys; unused sections are
  empty. This is not a cache of mutable film or publishing state.
- **Films mounts 24 cards per page.** Counts and filters still use the complete loaded
  list. Page and filter choices persist in the URL. Covers use browser lazy loading and
  asynchronous decoding. The endpoint's existing 1,000-finished-film guard remains;
  this is browser pagination, not database pagination.
- **Screen code loads on navigation.** Home and the shared shell load first. Settings,
  the editors and other screens have separate hashed JavaScript bundles. Opening a
  screen for the first time still downloads its bundle; subsequent visits reuse it.
- **Two SQLite indexes match existing queries.** `jobs(updated_at DESC)` serves recent
  jobs; `tasks(job_id, priority, created_at)` serves ordered task rows. Both remove the
  temporary sort shown by `EXPLAIN QUERY PLAN`. Existing stores gain the indexes through
  the normal idempotent schema setup. WAL and the existing job/status indexes were
  already enabled; they were not missing performance switches.

## Remaining opportunities, in priority order

These are findings from the broader source review, not additional changes in this patch.
Measure the affected endpoints after deploying the changes above before expanding scope.

| Priority | Area and evidence | Next improvement |
|---|---|---|
| High | **Cover transfer.** Cards still use full-size PNGs through `/api/file`; paging limits how many load, but each remains large. | Serve bounded-size JPEG/WebP thumbnails, cached by source file version. Keep originals for editing/export. Measure image bytes and first visible row separately from JSON latency. |
| High | **Global polling.** `App.jsx` polls badges and UI-worker state every five seconds. Home and Activity add their own polls; interval callbacks can overlap slow requests and continue in hidden tabs. Badge work includes directory scans and auth checks. | Pause browsing polls while hidden, allow one request in flight per poll, and share in-flight requests. Split rapidly changing progress from slower inventory/auth counts. Invalidate seen/approval counts immediately after mutations. |
| High | **Publishing GET requests.** Queue/history endpoints call `_reconcile_publish_queue`, which takes the queue write lock and visits every entry. The schedule also scores active entries. | Maintain a reconciled background snapshot and reuse scores until their inputs change. Preserve immediate approval/upload/delete visibility and the queue's existing write-lock protection. |
| Medium | **Library catalogue.** Filesystem helpers repeatedly enumerate/sort the output directory; filters remain client-side and finished-film metadata stops at 1,000 entries. | Introduce an indexed library summary with reconciliation for externally changed files, then server-side filtering, counts and pagination. Include file-only drafts and legacy films, not just durable job rows. |
| Medium | **Film editor.** `film_scenes` assembles files for every scene, and `_final_upscale_info` probes the final video. | Cache derived media dimensions/durations by file version, and request detailed scene media only when needed. A render/replacement must invalidate the result. |
| Medium | **Settings/worker health.** `workers_status` probes worker URLs sequentially, with a three-second timeout per endpoint. | Bound and parallelise health probes; serve a short-lived snapshot with a freshness indicator. Test with unreachable workers, not just healthy ones. |
| Medium | **SQLite connection setup.** Every `DurableStore` constructor runs schema setup, even for read-heavy requests. | Measure connection/schema overhead under concurrent browsing before introducing a connection lifecycle or migration-on-startup change. Do not share one connection without preserving its threading/locking guarantees. |

Analytics already serves persisted per-channel snapshots unless refreshed or cold;
engagement models already retain process-local state. Those caches should be preserved.
A new external cache service or database migration is not justified by this profile.

## Verification and rollout

`tests/test_browsing_performance.py` checks constant shared-store reads across a larger
fixture, fresh approval changes, legacy database fallbacks, isolated mutable settings,
file replacement/deletion/recreation, partial views, and index creation on an existing
database. Existing film-filter, metadata, approval, settings and orchestrator tests cover
the surrounding behaviour. Build the frontend and verify pagination, combined filters,
back navigation, and direct editor links before merging.

Build/deploy the frontend and backend together after PR review. The first normal database
open creates the indexes; no manual production SQL is needed. Track request durations
and image transfer sizes separately: a fast metadata response does not establish that
the browser has finished downloading and decoding covers.
