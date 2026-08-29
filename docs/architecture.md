# Architecture

A controller process drives GPU workers over HTTP. Nothing is hosted; everything runs on
machines you own.

```
┌─ Controller ───────────────────────────────┐      ┌─ Worker s1 ────────────┐
│                                            │      │  comfyui   :8188       │
│  webapp/frontend  React + Vite SPA         │      │  tts       :8189       │
│         ↓ /api/*                           │ HTTP │  (Docker, shared GPU)  │
│  webapp/backend   FastAPI  :8001  ─────────┼─────▶│                        │
│         ↓                                  │      └────────────────────────┘
│  app.py           config, work dirs, jobs  │      ┌─ Worker s2 ────────────┐
│  pipeline/*       the render stages        │─────▶│  … same stack …        │
│  resume_generation.py  the render process  │      └────────────────────────┘
│         ↓                                  │
│  orchestrator.sqlite3   durable task graph │
│  ~/videos/              work dirs + finals │
└────────────────────────────────────────────┘
```

## The pieces

**`webapp/frontend`** — a React (Vite) single-page app, hash-routed so deep links,
refresh, and back/forward all work with no server-side routing. `nav.js` is the single
source of truth mapping every page to a URL and back. This is the **only** interface; the
former Gradio UI is gone.

**`webapp/backend`** — a FastAPI service that imports `app.py` and the `pipeline` package
directly, rather than reimplementing them. It serves `/api/*` as JSON, streams media
through `/api/file`, and serves the built SPA from `webapp/frontend/dist` when it exists.

**`app.py`** — the shared helper module: config I/O, the canonical resolution table, style
resolution, work-directory bookkeeping, job launching, and progress polling. `DEFAULT_CFG`
at the top is the documented key set.

**`pipeline/`** — one module per concern:

| Module | Responsibility |
|---|---|
| `llm.py` | Script generation across the local vLLM, Claude, Grok, and OpenAI backends |
| `story.py` | Script generation — draft the story, critique it, then divide it into scenes; for a music video it also writes and critiques the song, and the divide places the lyrics on the track's timeline |
| `engines.py` | The engine registries — image (FLUX.2 Klein, FLUX.1 schnell), video (LTX 2.5, the MiniMax H3 family), and music (ACE-Step, MiniMax Music 3) |
| `comfyui.py`, `scene_video.py` | ComfyUI workflow submission, per-scene video rendering, and music generation |
| `tts_engines.py`, `openf5.py`, `chatterbox.py`, `tts_text.py` | Narration: engine choice, weights, and spoken-text handling |
| `performance.py`, `shot_gate.py` | Acted scenes and performance films: the acted script shape, the H3 Ref2VA prompt, and the speech gate |
| `song_timing.py`, `lyric_align.py` | Music videos: measure the sung track's vocal stem, whisper-align the lyric sheet, cut scenes between sung lines |
| `svc.py` | Song re-voicing (seed-vc) on whichever GPU worker is free |
| `assembler.py`, `captions.py`, `cover.py` | Final mux, SRT captions, cover images and first-frame burns |
| `youtube.py`, `x.py`, `publish_queue.py` | Publishing, multi-channel tokens, the cadence scheduler |
| `engagement.py` | Comment fetching, reply drafting, the predictive model |
| `orchestrator.py` | The durable SQLite task graph |
| `worker_pool.py`, `film_timing.py`, `timing.py` | Worker leasing, learned ETAs |
| `*_history.py` | Version history for images, videos, music, and final cuts |
| `c2pa.py` | Content Credentials signing at the publish chokepoints |

**`resume_generation.py`** — the render process itself. It is resumable by design: state
lives in the work directory and the durable database, so a killed render picks up where it
left off rather than starting over.

**`worker_agent.py`** — the optional daemon form. One agent per execution resource leases
ready tasks from the durable graph instead of the controller pushing work.

## The render, end to end

1. **Create** drafts a script (`llm.py` or `story.py`) into a fresh work directory under
   `~/videos/<slug>-<timestamp>/`.
2. **Script** lets you edit every scene, regenerate any field, and preview first frames.
   Nothing has rendered yet.
3. **Approve** puts the item in the queue. Starting it launches `resume_generation.py`.
4. The render fans scenes across the ComfyUI workers and narration across the TTS
   workers — in parallel, one job per worker. Scenes marked
   [continues the previous scene](performance_films.md#continued-shots) are the
   exception: they chain, rendering in order on one worker.
5. `assembler.py` muxes scenes, narration, and music into the final cut.
6. The final lands at `~/videos/<name>.mp4`, alongside the work directory that produced it.

Every stage writes a task row into the [durable graph](orchestration.md) with attempts,
leases, and produced artifacts, which is what the **Render** screen shows.

A [**music video**](performance_films.md#singing-films-the-music-video-format) bends the
order: its song is written and rendered at *script* time, because the divide measures the
finished track and cuts scenes between its sung lines. By the time the render starts,
`background_music.wav` already exists (the music task completes as skipped), every scene
renders as a singing H3 take carrying its own slice of the track, and the final mix is
the song alone — voice and ambient pinned to zero.

## Three state stores

Worth knowing when something looks stuck, because they can disagree:

| Store | Holds |
|---|---|
| The queue JSON | The request list — what's waiting, rendering, ready, posted |
| `job.json` in each work dir | Per-film render state and error stamps |
| `orchestrator.sqlite3` | The durable task graph, leases, and artifacts |

Clearing a phantom render means resetting all three — see
[Troubleshooting](troubleshooting.md#a-render-is-stuck-or-phantom).

## Development

The [contributing guide](contributing.md) covers the dev loop. In short: the controller
and the tests run fine without any GPU worker, `make web-dev` gives hot reload on port
5174, and CI runs pytest on Python 3.11/3.12 plus ruff and the frontend build.
