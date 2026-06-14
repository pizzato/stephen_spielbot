#!/usr/bin/env bash
# Start the containerized ComfyUI + F5-TTS workers on every host and the web app.
# Worker hosts come from config.yaml (comfy_workers). Containers are managed over
# SSH via docker compose (deployed by `make install`).
# Usage: bash scripts/start.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="/tmp/stephen_spielbot.pid"
APP_LOG="$HOME/.local/share/video-generator/logs/app.log"
VENV="$REPO_ROOT/.venv"
PYTHON="${VENV}/bin/python"

# shellcheck source=scripts/_config.sh
source "$REPO_ROOT/scripts/_config.sh"

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: virtual environment not found at $VENV — run 'make install' first"
    exit 1
fi

# ── 1. Worker containers (ComfyUI + F5-TTS) on every host ─────────────────────

for host in $(remote_hosts); do
    bash "$REPO_ROOT/scripts/worker.sh" start "$host"
    bash "$REPO_ROOT/scripts/worker.sh" status "$host"
done

# ── 2. Web app ────────────────────────────────────────────────────────────────

echo "=== Starting web app ==="

WEB_PORT="${WEB_PORT:-8001}"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "  [app] already running (PID $(cat "$PID_FILE"))"
else
    mkdir -p "$(dirname "$APP_LOG")"
    cd "$REPO_ROOT"
    if [[ ! -f "$REPO_ROOT/webapp/frontend/dist/index.html" ]]; then
        echo "  [app] WARNING: webapp/frontend/dist not found — run 'make web-build' to build the UI"
    fi
    # --timeout-keep-alive 30: keep idle HTTP/1.1 connections open longer than the
    # UI's poll cadence (progress 2.5s, badges 5s) so the server doesn't close a
    # socket just as the next poll reuses it (surfaces as a browser "NetworkError").
    nohup "$PYTHON" -m uvicorn webapp.backend.main:app --host 127.0.0.1 --port "$WEB_PORT" \
        --timeout-keep-alive 30 >/tmp/stephen_spielbot.out 2>&1 &
    echo $! > "$PID_FILE"
    echo "  [app] started (PID $!, log: $APP_LOG)"
fi

# ── 3. UI worker agent(s) (cover-image regeneration) ──────────────────────────
# Controller-side daemons that lease cover tasks and render them against the
# container ComfyUI endpoints in config.yaml (ui_workers).

echo "=== Starting UI worker(s) ==="
bash "$REPO_ROOT/scripts/ui_worker.sh" start

echo ""
echo "Stephen Spielbot is running at http://localhost:${WEB_PORT}"
echo "App log: $APP_LOG"
echo "Stop with: make stop"
