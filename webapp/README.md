# Stephen Spielbot — modern web UI

A React (Vite) + FastAPI rebuild of the Gradio interface, styled with the
shipped design system (sidebar + bento layout). The original Gradio app
(`app.py`) is **unchanged** and still runs on `localhost:7860`; this is an
additive, parallel front end.

```
webapp/
  backend/      FastAPI service that reuses pipeline/* and app.py helpers
  frontend/     Vite + React SPA using the design system theme
```

## How it fits together

The backend imports the existing `app` module purely to reuse its
Gradio-free helpers (config I/O, work-dir bookkeeping, job launching, progress
polling) plus the `pipeline` package directly. No Gradio UI is started. The
frontend talks to it over `/api/*` (JSON), and renders/uploads media through
`/api/file`.

The wired end-to-end flow:

**Create** (generate script) → **Script** (per-scene edit, regenerate preview)
→ **Approve & generate** (launches the real resumable worker subprocess) →
**Render** (live progress + durable task/worker tables) → **Remix** (re-mux
audio on the finished film). **Home**, **Queue**, **YouTube** (comments + AI
ideas), **Films** and **Settings** read/write the real backend too. YouTube
*publishing* still goes through the classic Post/Config tabs (OAuth flow).

## Run it (development)

Two processes. From the repo root:

```bash
# 1. backend (reuses the project's existing venv + deps)
pip install -r webapp/backend/requirements.txt
uvicorn webapp.backend.main:app --port 8001 --reload

# 2. frontend (separate terminal)
cd webapp/frontend
npm install
npm run dev          # opens http://localhost:5174, proxies /api → :8001
```

## Build for production

```bash
cd webapp/frontend && npm run build      # outputs webapp/frontend/dist
uvicorn webapp.backend.main:app --port 8001
# the backend serves the built SPA at http://localhost:8001 when dist/ exists
```
