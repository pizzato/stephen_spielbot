# Stephen Spielbot — web UI

A React (Vite) + FastAPI app, styled with the shipped design system (sidebar +
bento layout). This is the **only** interface — the former Gradio UI has been
removed. `app.py` is retained as a helper module the backend imports. The app
runs on `localhost:8001`.

```
webapp/
  backend/      FastAPI service that reuses pipeline/* and app.py helpers
  frontend/     Vite + React SPA using the design system theme
```

## How it fits together

The backend imports the `app` module to reuse its helpers (config I/O, work-dir
bookkeeping, job launching, progress polling) plus the `pipeline` package
directly. The frontend talks to it over `/api/*` (JSON), and renders/uploads
media through `/api/file`.

The wired end-to-end flow:

**Create** (generate script) → **Script** (per-scene edit, regenerate preview)
→ **Approve & generate** (launches the real resumable worker subprocess) →
**Render** (live progress + durable task/worker tables) → **Edit film**
(`#/edit/<name>`, also reachable as `#/remix/<name>`: per-scene re-renders,
inpainting, upscales, audio remix on the finished film).

The sidebar screens: **Home**, **Create**, **AI ideas**, **Queue**, **Script**,
**Render**, **Activity** (live fleet + per-job ETAs), **Films**, **Publishing**
(publish queue + scheduler), **Community** (comments + engagement replies),
**Channel Analytics** (stats + predictive model), and **Settings**. The deep
prompt templates (`prompts.yaml`) are editable at `#/prompts`, opened from
Settings → Infrastructure. YouTube channel OAuth runs from Settings → Channels.

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
