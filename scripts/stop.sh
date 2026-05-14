#!/usr/bin/env bash
# Stop the Gradio app, worker agents, and ComfyUI on all workers.
# Usage: bash scripts/stop.sh [cluster.conf]
set -euo pipefail

CONF="${1:-cluster.conf}"
PID_FILE="/tmp/stephen_spielbot.pid"

# ── Helpers ────────────────────────────────────────────────────────────────────

remote_hosts() {
    [ -f "$CONF" ] || return 0
    grep -v '^\s*#' "$CONF" | grep -v '^\s*$'
}

# ── 1. Gradio app ─────────────────────────────────────────────────────────────

echo "=== Stopping Gradio app ==="

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
    # Fallback: find by process name
    PIDS=$(pgrep -f "python.*app\.py" 2>/dev/null || true)
    if [[ -n "$PIDS" ]]; then
        kill $PIDS
        echo "  [app] stopped (PID $PIDS)"
    else
        echo "  [app] not running"
    fi
fi

# ── 2. Local worker agent ─────────────────────────────────────────────────────

echo "=== Stopping local worker agent ==="
LOCAL_AGENT_PID="/tmp/worker_agent_local.pid"
if [[ -f "$LOCAL_AGENT_PID" ]]; then
    PID="$(cat "$LOCAL_AGENT_PID")"
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "  [agent:local] stopped (PID $PID)"
    else
        echo "  [agent:local] not running (stale PID)"
    fi
    rm -f "$LOCAL_AGENT_PID"
else
    PIDS=$(pgrep -f "python.*worker_agent" 2>/dev/null || true)
    if [[ -n "$PIDS" ]]; then
        kill $PIDS
        echo "  [agent:local] stopped (PID $PIDS)"
    else
        echo "  [agent:local] not running"
    fi
fi

# ── 3. Remote worker agents ────────────────────────────────────────────────────

for host in $(remote_hosts); do
    echo "=== Stopping worker agent ($host) ==="
    ssh "$host" bash <<REMOTE 2>/dev/null || true
AGENT_PID_FILE="/tmp/worker_agent_${host}.pid"
if [[ -f "\$AGENT_PID_FILE" ]]; then
    PID="\$(cat "\$AGENT_PID_FILE")"
    if kill -0 "\$PID" 2>/dev/null; then
        kill "\$PID"
        echo "  [agent:$host] stopped (PID \$PID)"
    else
        echo "  [agent:$host] not running (stale PID)"
    fi
    rm -f "\$AGENT_PID_FILE"
else
    PIDS=\$(pgrep -f "python.*worker_agent" 2>/dev/null || true)
    if [[ -n "\$PIDS" ]]; then
        kill \$PIDS
        echo "  [agent:$host] stopped"
    else
        echo "  [agent:$host] not running"
    fi
fi
REMOTE
done

# ── 4. Local ComfyUI ──────────────────────────────────────────────────────────

echo "=== Stopping ComfyUI (local) ==="

if systemctl --user is-active comfyui-worker.service &>/dev/null; then
    systemctl --user stop comfyui-worker.service
    echo "  [comfyui] stopped (systemd)"
else
    PIDS=$(pgrep -f "python.*main\.py.*8188" 2>/dev/null || true)
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
            PIDS=$(pgrep -f "python.*main\.py.*8188" 2>/dev/null || true)
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
