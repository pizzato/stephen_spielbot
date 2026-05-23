#!/usr/bin/env bash
# Manage a single remote ComfyUI worker.
# Usage: bash scripts/worker.sh <start|stop|restart|status> <hostname>
#
#   bash scripts/worker.sh stop    s2
#   bash scripts/worker.sh start   s2
#   bash scripts/worker.sh restart s2
#   bash scripts/worker.sh status  s2
set -euo pipefail

ACTION="${1:-}"
HOST="${2:-}"

if [[ -z "$ACTION" || -z "$HOST" ]]; then
    echo "Usage: $0 <start|stop|restart|status> <hostname>"
    exit 1
fi

COMFYUI_PORT="${COMFYUI_PORT:-8188}"

# ── Helpers ────────────────────────────────────────────────────────────────────

_stop() {
    echo "=== Stopping ComfyUI ($HOST) ==="
    ssh "$HOST" bash <<'REMOTE'
        if systemctl --user is-active comfyui-worker.service &>/dev/null; then
            systemctl --user stop comfyui-worker.service
            echo "  [comfyui] stopped (systemd)"
        else
            # Kill by port — works regardless of how ComfyUI was invoked
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
}

_start() {
    echo "=== Starting ComfyUI ($HOST) ==="
    ssh "$HOST" bash <<'REMOTE'
        if systemctl --user is-active comfyui-worker.service &>/dev/null; then
            echo "  [comfyui] already running"
        elif systemctl --user list-unit-files comfyui-worker.service 2>/dev/null | grep -q comfyui; then
            systemctl --user start comfyui-worker.service
            echo "  [comfyui] started via systemd"
        elif [[ -x "$HOME/github/ComfyUI/start_worker.sh" ]]; then
            bash "$HOME/github/ComfyUI/start_worker.sh"
        else
            echo "  [comfyui] WARNING: no start method found on $(hostname)"
            exit 1
        fi
REMOTE

    # Wait for ComfyUI to respond
    local url="http://${HOST}:${COMFYUI_PORT}"
    echo -n "  Waiting for ComfyUI on $HOST"
    for i in $(seq 1 40); do
        if curl -sf "${url}/system_stats" &>/dev/null; then
            echo " ✓"
            echo "  ComfyUI is up at $url"
            return 0
        fi
        echo -n "."
        sleep 3
    done
    echo " TIMEOUT"
    echo "  WARNING: ComfyUI did not respond within 2 minutes — it may still be loading."
    return 1
}

_status() {
    local url="http://${HOST}:${COMFYUI_PORT}"
    echo -n "  $HOST: "
    if curl -sf "${url}/system_stats" &>/dev/null; then
        local nodes
        nodes=$(curl -sf "${url}/object_info" 2>/dev/null \
            | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d))' 2>/dev/null || echo "?")
        echo "✓ ComfyUI UP  ($nodes nodes)  $url"
    else
        echo "✗ ComfyUI DOWN  $url"
        # Try to show systemd status if reachable
        ssh "$HOST" bash <<'REMOTE' 2>/dev/null || true
            if systemctl --user is-active comfyui-worker.service &>/dev/null; then
                echo "  systemd service: active"
            elif systemctl --user is-failed comfyui-worker.service &>/dev/null; then
                echo "  systemd service: failed"
                journalctl --user -u comfyui-worker.service -n 5 --no-pager 2>/dev/null || true
            else
                echo "  systemd service: inactive / not installed"
            fi
REMOTE
        return 1
    fi
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

case "$ACTION" in
    stop)    _stop ;;
    start)   _start ;;
    restart) _stop && _start ;;
    status)  _status ;;
    *)
        echo "Unknown action '$ACTION'. Use: start | stop | restart | status"
        exit 1
        ;;
esac
