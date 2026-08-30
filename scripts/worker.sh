#!/usr/bin/env bash
# Manage one host's containerized worker stack (ComfyUI + F5-TTS) from the
# controller — over SSH for remote hosts, with plain local commands for
# "localhost" (single-machine setup). The stack is deployed to
# ~/spielbot-worker/docker on the host by `make install` /
# install_worker_container.sh.
#
# Usage: bash scripts/worker.sh <start|stop|restart|status|logs> <hostname>
#   bash scripts/worker.sh stop    s2
#   bash scripts/worker.sh start   s2
#   bash scripts/worker.sh restart s2
#   bash scripts/worker.sh status  s2
#   bash scripts/worker.sh logs    s2
#
# 'start' re-deploys the host first if its image no longer matches the repo's
# build context, then verifies the ComfyUI container registers every node the
# workflows need — see scripts/_worker_build.sh and check_worker_nodes.sh.
set -euo pipefail

ACTION="${1:-}"
HOST="${2:-}"

if [[ -z "$ACTION" || -z "$HOST" ]]; then
    echo "Usage: $0 <start|stop|restart|status|logs> <hostname>"
    exit 1
fi

# Reject a hostname that ssh would parse as an option (e.g. -oProxyCommand=…) —
# a command-injection guard for hosts that originate from saved config.
if [[ "$HOST" == -* ]]; then
    echo "ERROR: invalid hostname '$HOST'"
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMFYUI_PORT="${COMFYUI_PORT:-8188}"
TTS_PORT="${TTS_PORT:-8189}"
REMOTE_DIR="spielbot-worker/docker"

# shellcheck source=scripts/_worker_build.sh
source "$REPO_ROOT/scripts/_worker_build.sh"

LOCAL=false
if [[ "$HOST" == "localhost" || "$HOST" == "127.0.0.1" ]]; then
    LOCAL=true
fi

# Run a command on the host: locally for localhost, over SSH otherwise.
_run() {
    if $LOCAL; then bash -c "$*"; else ssh -- "$HOST" "$*"; fi
}

# Run a `docker compose` command on the host's deployed stack.
_compose() {
    _run "[ -d \$HOME/$REMOTE_DIR ] || { echo 'ERROR: no container stack at ~/$REMOTE_DIR on $HOST — run: make install'; exit 1; }; cd \$HOME/$REMOTE_DIR && docker compose $*"
}

# Block until ComfyUI answers, so the node check below reads a loaded server
# rather than one still starting. Best-effort: give up quietly after ~2 min and
# let the check report what it sees.
_wait_comfy() {
    for _ in $(seq 1 24); do
        curl -sf -m 5 "http://${HOST}:${COMFYUI_PORT}/system_stats" >/dev/null 2>&1 && return 0
        sleep 5
    done
    return 1
}

# Verify the custom nodes the workflows need are actually registered. The stamp
# check above cannot see a pack that clones fine but fails to import (upstream
# dependency drift), so this reads the running server.
_check_nodes() {
    bash "$REPO_ROOT/scripts/check_worker_nodes.sh" "$HOST" "$COMFYUI_PORT" && return 0
    echo "    → rebuild this worker:  bash scripts/install_worker_container.sh $HOST"
    return 1
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
        # A worker keeps the image it was installed with. A custom node added to
        # the Dockerfile since then never reaches it, and every render needing
        # that node fails with "the node 'X' is not installed on this worker".
        # Compare the deployed build stamp against the repo and re-deploy first
        # when they differ (rsync + docker compose build), so a start always
        # leaves this host running the repo's image.
        WANT_STAMP="$(build_stamp "$REPO_ROOT")"
        HAVE_STAMP="$(_run "cat \$HOME/$WORKER_STAMP 2>/dev/null" 2>/dev/null | tr -d '[:space:]' || true)"
        if [[ -n "$WANT_STAMP" && "$WANT_STAMP" != "$HAVE_STAMP" ]]; then
            echo "  worker image is out of date with the repo — rebuilding $HOST"
            echo "  (first build after a Dockerfile change takes a few minutes)"
            bash "$REPO_ROOT/scripts/install_worker_container.sh" "$HOST"
        else
            # --force-recreate: containers hold the NVIDIA device nodes they were
            # CREATED with. If the driver/modules were (re)loaded since (boot race,
            # driver upgrade, manual modprobe), /dev/nvidia-uvm's dynamic major has
            # changed and CUDA fails ("unknown error") while nvidia-smi still works.
            # Recreating on every start self-heals that; volumes persist, and the
            # cost is a few seconds.
            _compose up -d --force-recreate
            _wait_comfy || true
            _check_nodes || true
        fi
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
        _health "ComfyUI  " "http://${HOST}:${COMFYUI_PORT}/system_stats"
        _health "F5-TTS   " "http://${HOST}:${TTS_PORT}/health"
        # GPU device per container — surfaces a silent CPU fallback (a host
        # daemon-reload can revoke the GPU from a running container; see README).
        for svc in comfyui tts; do
            dev=$(_run "docker exec spielbot-worker-${svc}-1 nvidia-smi -L 2>/dev/null | grep -m1 '^GPU'" 2>/dev/null || true)
            if [ -n "$dev" ]; then
                printf "    ✓ %-7s GPU  %s\n" "$svc" "$dev"
            else
                printf "    ✗ %-7s CPU  (no GPU — run: bash %s restart %s)\n" "$svc" "$0" "$HOST"
            fi
        done
        bash "$REPO_ROOT/scripts/check_worker_nodes.sh" "$HOST" "$COMFYUI_PORT" || true
        ;;
    logs)
        _compose logs --tail 100 -f
        ;;
    *)
        echo "Unknown action '$ACTION'. Use: start | stop | restart | status | logs"
        exit 1
        ;;
esac
