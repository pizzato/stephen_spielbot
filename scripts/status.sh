#!/usr/bin/env bash
# Show health of the Gradio app and every ComfyUI worker.
# Usage: bash scripts/status.sh [cluster.conf]

CONF="${1:-cluster.conf}"
PID_FILE="/tmp/stephen_spielbot.pid"
ALL_OK=true

# ── Helpers ────────────────────────────────────────────────────────────────────

remote_hosts() {
    [ -f "$CONF" ] || return 0
    grep -v '^\s*#' "$CONF" | grep -v '^\s*$'
}

check_comfyui() {
    local label="$1" url="$2"
    if curl -sf "${url}/system_stats" &>/dev/null; then
        local nodes
        nodes=$(curl -sf "${url}/object_info" 2>/dev/null | python3 -c \
            'import sys,json; d=json.load(sys.stdin); print(len(d))' 2>/dev/null || echo "?")
        echo "  ✓ ComfyUI ${label} — UP  (${nodes} nodes)  ${url}"
    else
        echo "  ✗ ComfyUI ${label} — DOWN  ${url}"
        ALL_OK=false
    fi
}

# ── Gradio app ─────────────────────────────────────────────────────────────────

echo "=== Stephen Spielbot status ==="
echo ""
echo "Gradio app:"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "  ✓ Running  (PID $(cat "$PID_FILE"))  http://localhost:7860"
elif pgrep -f "python.*app\.py" &>/dev/null; then
    echo "  ✓ Running  (PID $(pgrep -f 'python.*app\.py'))  http://localhost:7860"
else
    echo "  ✗ Not running"
    ALL_OK=false
fi

# ── ComfyUI workers ────────────────────────────────────────────────────────────

echo ""
echo "ComfyUI workers:"
check_comfyui "localhost" "http://localhost:8188"

for host in $(remote_hosts); do
    check_comfyui "$host" "http://${host}:8188"
done

# ── Summary ────────────────────────────────────────────────────────────────────

echo ""
if $ALL_OK; then
    echo "All services are UP."
else
    echo "Some services are DOWN. Run 'make start' to restart them."
    exit 1
fi
