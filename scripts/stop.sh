#!/usr/bin/env bash
# Stop the web app and ComfyUI on all workers.
# Worker hosts come from config.yaml (comfy_workers).
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

# ── 2. UI workers ─────────────────────────────────────────────────────────────

echo "=== Stopping UI worker(s) ==="
bash "$REPO_ROOT/scripts/ui_worker.sh" stop

# ── 3. UI ComfyUI worker hosts ────────────────────────────────────────────────
# Hosts in ui_workers that are NOT already render workers — stopped via SSH
# (same interface as render workers, works for localhost too).

_COMFY_HOSTS=" $(remote_hosts | tr '\n' ' ') "
for host in $(ui_hosts); do
    [[ "$_COMFY_HOSTS" == *" $host "* ]] && continue
    echo "=== Stopping ComfyUI (ui: $host) ==="
    bash "$REPO_ROOT/scripts/worker.sh" stop "$host"
done

# ── 4. Remote ComfyUI workers ─────────────────────────────────────────────────

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
