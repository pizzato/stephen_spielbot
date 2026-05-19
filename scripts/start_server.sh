#!/usr/bin/env bash
# Start only the Gradio app (assumes ComfyUI workers are already running).
# Usage: bash scripts/start_server.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="/tmp/stephen_spielbot.pid"
APP_LOG="$HOME/.local/share/video-generator/logs/app.log"
VENV="$REPO_ROOT/.venv"
PYTHON="${VENV}/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: virtual environment not found at $VENV — run 'make install' first"
    exit 1
fi

echo "=== Starting Gradio app ==="

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "  [app] already running (PID $(cat "$PID_FILE"))"
else
    mkdir -p "$(dirname "$APP_LOG")"
    cd "$REPO_ROOT"
    nohup "$PYTHON" app.py >/tmp/stephen_spielbot.out 2>&1 &
    echo $! > "$PID_FILE"
    echo "  [app] started (PID $!, log: $APP_LOG)"
fi

echo ""
echo "Stephen Spielbot is running at http://localhost:7860"
echo "App log: $APP_LOG"
