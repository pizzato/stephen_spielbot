#!/usr/bin/env bash
# Manage one host's containerized worker stack (ComfyUI + F5-TTS) from the
# controller, over SSH. The stack is deployed to ~/spielbot-worker/docker on the
# host by `make install` / install_worker_container.sh.
#
# Usage: bash scripts/worker.sh <start|stop|restart|status|logs> <hostname>
#   bash scripts/worker.sh stop    s2
#   bash scripts/worker.sh start   s2
#   bash scripts/worker.sh restart s2
#   bash scripts/worker.sh status  s2
#   bash scripts/worker.sh logs    s2
set -euo pipefail

ACTION="${1:-}"
HOST="${2:-}"

if [[ -z "$ACTION" || -z "$HOST" ]]; then
    echo "Usage: $0 <start|stop|restart|status|logs> <hostname>"
    exit 1
fi

COMFYUI_PORT="${COMFYUI_PORT:-8188}"
TTS_PORT="${TTS_PORT:-8189}"
REMOTE_DIR="spielbot-worker/docker"

# Run a `docker compose` command on the host's deployed stack, over SSH.
_compose() {
    ssh "$HOST" "[ -d \$HOME/$REMOTE_DIR ] || { echo 'ERROR: no container stack at ~/$REMOTE_DIR on $HOST — run: make install'; exit 1; }; cd \$HOME/$REMOTE_DIR && docker compose $*"
}

_health() {
    local name="$1" url="$2"
    if curl -sf -m 5 "$url" >/dev/null 2>&1; then
        echo "    ✓ $name UP    ${url%/*}"
    else
        echo "    ✗ $name DOWN  ${url%/*}"
    fi
}

case "$ACTION" in
    start)
        echo "=== Starting containers ($HOST) ==="
        _compose up -d
        ;;
    stop)
        echo "=== Stopping containers ($HOST) ==="
        _compose stop
        ;;
    restart)
        echo "=== Restarting containers ($HOST) ==="
        _compose restart
        ;;
    status)
        echo "  $HOST:"
        _compose ps 2>/dev/null || true
        _health "ComfyUI" "http://${HOST}:${COMFYUI_PORT}/system_stats"
        _health "F5-TTS " "http://${HOST}:${TTS_PORT}/health"
        # GPU device per container — surfaces a silent CPU fallback (a host
        # daemon-reload can revoke the GPU from a running container; see README).
        for svc in comfyui tts; do
            dev=$(ssh "$HOST" "docker exec spielbot-worker-${svc}-1 nvidia-smi -L 2>/dev/null | grep -m1 '^GPU'" 2>/dev/null || true)
            if [ -n "$dev" ]; then
                printf "    ✓ %-7s GPU  %s\n" "$svc" "$dev"
            else
                printf "    ✗ %-7s CPU  (no GPU — run: bash %s restart %s)\n" "$svc" "$0" "$HOST"
            fi
        done
        ;;
    logs)
        _compose logs --tail 100 -f
        ;;
    *)
        echo "Unknown action '$ACTION'. Use: start | stop | restart | status | logs"
        exit 1
        ;;
esac
