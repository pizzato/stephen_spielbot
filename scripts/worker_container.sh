#!/usr/bin/env bash
# Manage the containerized worker stack on THIS machine (issue #12).
# Run this on a worker machine (needs Docker + the NVIDIA Container Toolkit).
#
# Usage: bash scripts/worker_container.sh <action>
#   build         Build the ComfyUI + F5-TTS images
#   up            Build (if needed) and start the workers in the background
#   down          Stop and remove the workers
#   restart       down + up
#   status        Show container + health status
#   logs          Tail logs from both workers (Ctrl-C to stop)
#   fetch-models  Download the ~33 GB of models into MODELS_DIR (from docker/.env)
set -euo pipefail

ACTION="${1:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKER_DIR="$REPO_ROOT/docker"
ENV_FILE="$DOCKER_DIR/.env"

if [[ -z "$ACTION" ]]; then
    sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
fi

_need_docker() {
    if ! command -v docker &>/dev/null; then
        echo "ERROR: docker not found. Install Docker + the NVIDIA Container Toolkit." >&2
        exit 1
    fi
}

_need_env() {
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "ERROR: $ENV_FILE not found." >&2
        echo "  cp $DOCKER_DIR/.env.example $ENV_FILE   # then set MODELS_DIR" >&2
        exit 1
    fi
}

# Read a KEY from docker/.env (no sourcing — avoids surprises).
_env_get() {
    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-
}

_compose() {
    _need_docker
    _need_env
    ( cd "$DOCKER_DIR" && docker compose "$@" )
}

case "$ACTION" in
    build)   _compose build ;;
    up)      _compose up -d --build ;;
    down)    _compose down ;;
    restart) _compose down && _compose up -d --build ;;
    logs)    _compose logs -f ;;
    status)
        _compose ps
        echo ""
        COMFY_PORT="$(_env_get COMFYUI_PORT)"; COMFY_PORT="${COMFY_PORT:-8188}"
        TTS_PORT="$(_env_get TTS_PORT)"; TTS_PORT="${TTS_PORT:-8189}"
        echo -n "  ComfyUI (:$COMFY_PORT): "
        curl -sf "http://localhost:${COMFY_PORT}/system_stats" >/dev/null \
            && echo "✓ UP" || echo "✗ DOWN"
        echo -n "  F5-TTS  (:$TTS_PORT): "
        curl -sf "http://localhost:${TTS_PORT}/health" >/dev/null \
            && echo "✓ UP" || echo "✗ DOWN"
        ;;
    fetch-models)
        _need_env
        MODELS_DIR="$(_env_get MODELS_DIR)"
        if [[ -z "$MODELS_DIR" ]]; then
            echo "ERROR: MODELS_DIR is not set in $ENV_FILE" >&2
            exit 1
        fi
        # download_models.sh expects the ComfyUI root and writes <root>/models/...
        COMFY_ROOT="$(dirname "$MODELS_DIR")"
        mkdir -p "$MODELS_DIR"
        echo "=== Downloading models into $MODELS_DIR (ComfyUI root: $COMFY_ROOT) ==="
        bash "$REPO_ROOT/scripts/download_models.sh" "$COMFY_ROOT"
        ;;
    *)
        echo "Unknown action '$ACTION'. Use: build | up | down | restart | status | logs | fetch-models" >&2
        exit 1
        ;;
esac
