#!/usr/bin/env bash
# Stop the Gradio app and ComfyUI on all workers.
# Usage: bash scripts/stop.sh [cluster.conf]
set -euo pipefail

CONF="${1:-cluster.conf}"
PID_FILE="/tmp/stephen_spielbot.pid"

# ── Helpers ────────────────────────────────────────────────────────────────────

remote_hosts() {
    [ -f "$CONF" ] || return 0
    grep -v '^\s*#' "$CONF" | grep -v '^\s*$'
}

# Find PIDs of whatever process is listening on port 8188.
# Works regardless of how ComfyUI was invoked (with or without --port 8188).
comfyui_pids() {
    # lsof: BSD/macOS and most Linux
    lsof -ti TCP:8188 -s TCP:LISTEN 2>/dev/null \
    || ss -tlnp 'sport = :8188' 2>/dev/null \
       | grep -oP 'pid=\K[0-9]+' \
    || true
}

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

# ── 2. Local ComfyUI ──────────────────────────────────────────────────────────

echo "=== Stopping ComfyUI (local) ==="

if systemctl --user is-active comfyui-worker.service &>/dev/null; then
    systemctl --user stop comfyui-worker.service
    echo "  [comfyui] stopped (systemd)"
else
    PIDS=$(comfyui_pids)
    if [[ -n "$PIDS" ]]; then
        kill $PIDS
        echo "  [comfyui] stopped (PID $PIDS)"
    else
        echo "  [comfyui] not running"
    fi
fi

# ── 3. Remote ComfyUI workers ─────────────────────────────────────────────────

for host in $(remote_hosts); do
    echo "=== Stopping ComfyUI ($host) ==="
    ssh "$host" bash <<'REMOTE'
        if systemctl --user is-active comfyui-worker.service &>/dev/null; then
            systemctl --user stop comfyui-worker.service
            echo "  [comfyui] stopped (systemd)"
        else
            # Find by port rather than command-line pattern — works however ComfyUI was started
            PIDS=$(lsof -ti TCP:8188 -s TCP:LISTEN 2>/dev/null \
                || ss -tlnp 'sport = :8188' 2>/dev/null | grep -oP 'pid=\K[0-9]+' \
                || true)
            if [[ -n "$PIDS" ]]; then
                kill $PIDS
                echo "  [comfyui] stopped (PID $PIDS)"
            else
                echo "  [comfyui] not running"
            fi
        fi
REMOTE
done

echo ""
echo "All services stopped."
