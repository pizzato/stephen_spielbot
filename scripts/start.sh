#!/usr/bin/env bash
# Start Mosquitto, ComfyUI on all workers, worker agents, and the Gradio app.
# Usage: bash scripts/start.sh [cluster.conf]
set -euo pipefail

CONF="${1:-cluster.conf}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="/tmp/stephen_spielbot.pid"
APP_LOG="$HOME/.local/share/video-generator/logs/app.log"
VENV="$REPO_ROOT/.venv"
PYTHON="${VENV}/bin/python"

MOSQUITTO="${MOSQUITTO:-$(command -v mosquitto 2>/dev/null || echo /opt/homebrew/sbin/mosquitto)}"
MQTT_BROKER_HOST="${MQTT_BROKER_HOST:-$(ipconfig getifaddr en0 2>/dev/null || hostname)}"

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

# ── 0. Mosquitto MQTT broker ──────────────────────────────────────────────────

echo "=== Starting Mosquitto broker ==="
if pgrep -x mosquitto &>/dev/null; then
    echo "  [mosquitto] already running"
elif [[ -x "$MOSQUITTO" ]]; then
    MOSQ_CONF="/opt/homebrew/etc/mosquitto/mosquitto.conf"
    mkdir -p "$(dirname "$MOSQ_CONF")"
    if [[ ! -f "$MOSQ_CONF" ]]; then
        printf 'listener 1883 0.0.0.0\nallow_anonymous true\n' > "$MOSQ_CONF"
    fi
    nohup "$MOSQUITTO" -c "$MOSQ_CONF" >> "$HOME/.local/share/video-generator/logs/mosquitto.log" 2>&1 &
    sleep 1
    echo "  [mosquitto] started (log: ~/.local/share/video-generator/logs/mosquitto.log)"
else
    echo "  [mosquitto] WARNING: mosquitto not found — install with: brew install mosquitto"
fi

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

# ── 3. Worker agents (MQTT job processors) ────────────────────────────────────

echo "=== Starting worker agents ==="
echo "  [broker] workers will connect to $MQTT_BROKER_HOST:1883"

# Local worker agent (if local ComfyUI is running)
LOCAL_AGENT_PID_FILE="/tmp/worker_agent_local.pid"
if curl -sf http://localhost:8188/system_stats &>/dev/null; then
    if [[ -f "$LOCAL_AGENT_PID_FILE" ]] && kill -0 "$(cat "$LOCAL_AGENT_PID_FILE")" 2>/dev/null; then
        echo "  [agent:local] already running (PID $(cat "$LOCAL_AGENT_PID_FILE"))"
    else
        mkdir -p "$(dirname "$APP_LOG")"
        WORKER_ID="${WORKER_ID:-$(hostname -s)}"
        nohup "$PYTHON" "$REPO_ROOT/pipeline/worker_agent.py" \
            --worker-id "$WORKER_ID" \
            --broker "$MQTT_BROKER_HOST" \
            --comfy-url "http://localhost:8188" \
            >> "$HOME/.local/share/video-generator/logs/worker_local.log" 2>&1 &
        echo $! > "$LOCAL_AGENT_PID_FILE"
        echo "  [agent:local] started (PID $!, log: worker_local.log)"
    fi
else
    echo "  [agent:local] skipped (no local ComfyUI)"
fi

# Remote worker agents
for host in $(remote_hosts); do
    REMOTE_LOG="$HOME/.local/share/video-generator/logs/worker_${host}.log"
    echo "  [agent:$host] starting..."
    ssh "$host" bash <<REMOTE
set -euo pipefail
AGENT_PID_FILE="/tmp/worker_agent_${host}.pid"
if [[ -f "\$AGENT_PID_FILE" ]] && kill -0 "\$(cat "\$AGENT_PID_FILE")" 2>/dev/null; then
    echo "  [agent:$host] already running (PID \$(cat "\$AGENT_PID_FILE"))"
    exit 0
fi
VENV="\$HOME/github/comfyui-env"
PYTHON="\$VENV/bin/python"
# paho-mqtt must be installed in the comfyui venv on each worker
if ! "\$PYTHON" -c "import paho.mqtt.client" 2>/dev/null; then
    "\$PYTHON" -m pip install --quiet "paho-mqtt>=2.0"
fi
mkdir -p "\$HOME/.local/share/video-generator/logs"
nohup "\$PYTHON" "\$HOME/github/video-generator/pipeline/worker_agent.py" \\
    --worker-id "$host" \\
    --broker "$MQTT_BROKER_HOST" \\
    --comfy-url "http://localhost:8188" \\
    >> "\$HOME/.local/share/video-generator/logs/worker_agent.log" 2>&1 &
echo \$! > "\$AGENT_PID_FILE"
echo "  [agent:$host] started (PID \$!)"
REMOTE
    echo "  [agent:$host] done"
done

# ── 4. Gradio app ─────────────────────────────────────────────────────────────

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
