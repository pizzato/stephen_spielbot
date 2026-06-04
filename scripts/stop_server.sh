#!/usr/bin/env bash
# Stop only the web app (leaves ComfyUI workers running).
# Uses launchd when the service is installed; falls back to kill-by-PID.
# Usage: bash scripts/stop_server.sh
set -euo pipefail

PID_FILE="/tmp/stephen_spielbot.pid"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.stephen-spielbot.server"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

echo "=== Stopping web app ==="

if [[ -f "$PLIST_DST" ]]; then
    bash "$REPO_ROOT/scripts/launchd.sh" stop
else
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
fi

echo "=== Stopping UI worker(s) ==="
bash "$REPO_ROOT/scripts/ui_worker.sh" stop
