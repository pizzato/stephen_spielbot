#!/usr/bin/env bash
# Stop only the web app (leaves ComfyUI workers running).
# Usage: bash scripts/stop_server.sh
set -euo pipefail

PID_FILE="/tmp/stephen_spielbot.pid"

echo "=== Stopping web app ==="

if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "  [app] stopped (PID $PID)"
    else
        echo "  [app] not running (stale PID file)"
    fi
    rm -f "$PID_FILE"
else
    PIDS=$(pgrep -f "uvicorn webapp.backend.main" 2>/dev/null || true)
    if [[ -n "$PIDS" ]]; then
        kill $PIDS
        echo "  [app] stopped (PID $PIDS)"
    else
        echo "  [app] not running"
    fi
fi
