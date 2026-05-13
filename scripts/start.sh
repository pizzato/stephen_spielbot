#!/usr/bin/env bash
# Start ComfyUI on all workers and the Gradio app locally.
# Usage: bash scripts/start.sh [cluster.conf]
set -euo pipefail

CONF="${1:-cluster.conf}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="/tmp/stephen_spielbot.pid"
APP_LOG="$HOME/.local/share/video-generator/logs/app.log"
VENV="$REPO_ROOT/.venv"
PYTHON="${VENV}/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: virtual environment not found at $VENV — run 'make install' first"
    exit 1
fi

# ── Helpers ────────────────────────────────────────────────────────────────────

remote_hosts() {
    [ -f "$CONF" ] || return 0
    grep -v '^\s*#' "$CONF" | grep -v '^\s*$'
}

wait_for_comfyui() {
    local host="$1" url="$2"
    echo -n "  Waiting for ComfyUI on $host"
    for i in $(seq 1 30); do
        if curl -sf "${url}/system_stats" &>/dev/null; then
            echo " ✓"
            return 0
        fi
        echo -n "."
        sleep 3
    done
    echo " TIMEOUT (ComfyUI may still be loading)"
}

# ── 1. Local ComfyUI ──────────────────────────────────────────────────────────

echo "=== Starting ComfyUI (local) ==="

if systemctl --user is-active comfyui-worker.service &>/dev/null; then
    echo "  [comfyui] already running (systemd)"
elif systemctl --user list-unit-files comfyui-worker.service 2>/dev/null | grep -q comfyui; then
    systemctl --user start comfyui-worker.service
    echo "  [comfyui] started via systemd"
elif pgrep -f "python.*main.py.*8188" &>/dev/null; then
    echo "  [comfyui] already running (process)"
else
    COMFY_DIR="$HOME/github/ComfyUI"
    COMFY_ENV="$HOME/github/comfyui-env"
    if [[ ! -d "$COMFY_DIR" ]]; then
        echo "  [comfyui] not installed locally — skipping (using remote workers only)"
    else
        echo "  [comfyui] starting directly..."
        mkdir -p "$(dirname "$COMFY_DIR/comfyui.log")"
        (source "$COMFY_ENV/bin/activate" && cd "$COMFY_DIR" && \
            nohup python main.py --listen 0.0.0.0 --port 8188 \
                >> "$COMFY_DIR/comfyui.log" 2>&1) &
        echo "  [comfyui] started (log: $COMFY_DIR/comfyui.log)"
        wait_for_comfyui "localhost" "http://localhost:8188"
    fi
fi

# ── 2. Remote ComfyUI workers ─────────────────────────────────────────────────

for host in $(remote_hosts); do
    echo "=== Starting ComfyUI ($host) ==="
    ssh "$host" bash <<'REMOTE'
        if systemctl --user is-active comfyui-worker.service &>/dev/null; then
            echo "  [comfyui] already running"
        elif systemctl --user list-unit-files comfyui-worker.service 2>/dev/null | grep -q comfyui; then
            systemctl --user start comfyui-worker.service
            echo "  [comfyui] started via systemd"
        elif [[ -x "$HOME/github/ComfyUI/start_worker.sh" ]]; then
            bash "$HOME/github/ComfyUI/start_worker.sh"
        else
            echo "  [comfyui] WARNING: no start method found on $(hostname)"
        fi
REMOTE
    wait_for_comfyui "$host" "http://${host}:8188"
done

# ── 3. Gradio app ─────────────────────────────────────────────────────────────

echo "=== Starting Gradio app ==="

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "  [app] already running (PID $(cat "$PID_FILE"))"
else
    mkdir -p "$(dirname "$APP_LOG")"
    cd "$REPO_ROOT"
    nohup "$PYTHON" app.py >> "$APP_LOG" 2>&1 &
    echo $! > "$PID_FILE"
    echo "  [app] started (PID $!, log: $APP_LOG)"
fi

echo ""
echo "Stephen Spielbot is running at http://localhost:7860"
echo "App log: $APP_LOG"
echo "Stop with: make stop"
