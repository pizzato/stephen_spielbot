#!/usr/bin/env bash
# Stop the web app and the containerized workers on every host.
# Worker hosts come from config.yaml (comfy_workers). Containers are stopped over
# SSH via docker compose.
# Usage: bash scripts/stop.sh
set -euo pipefail

PID_FILE="/tmp/stephen_spielbot.pid"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# shellcheck source=scripts/_config.sh
source "$REPO_ROOT/scripts/_config.sh"

# ── 1. Web app ────────────────────────────────────────────────────────────────

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

# ── 2. UI worker agent(s) ─────────────────────────────────────────────────────

echo "=== Stopping UI worker(s) ==="
bash "$REPO_ROOT/scripts/ui_worker.sh" stop

# ── 3. Worker containers on every host ────────────────────────────────────────

for host in $(remote_hosts); do
    bash "$REPO_ROOT/scripts/worker.sh" stop "$host"
done

echo ""
echo "All services stopped."
