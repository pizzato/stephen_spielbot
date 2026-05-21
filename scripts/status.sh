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

# ── Durable orchestration ─────────────────────────────────────────────────────

echo ""
echo "Durable orchestration:"
DB="${SPIELBOT_ORCHESTRATOR_DB:-$HOME/.local/share/video-generator/orchestrator.sqlite3}"
if [[ -f "$DB" ]]; then
    python3 -c '
import sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
jobs = con.execute("SELECT title,status,progress_pct,updated_at FROM jobs ORDER BY updated_at DESC LIMIT 3").fetchall()
workers = con.execute("SELECT kind,endpoint,status,last_heartbeat_at FROM workers ORDER BY kind,endpoint").fetchall()
print(f"  ✓ DB: {db}")
if jobs:
    for row in jobs:
        title = row["title"] or "(untitled)"
        status = row["status"]
        progress = row["progress_pct"]
        print(f"    job {status:>7} {progress:5.1f}%  {title[:60]}")
else:
    print("    no jobs recorded yet")
if workers:
    print("    workers:")
    for row in workers:
        kind = row["kind"]
        status = row["status"]
        endpoint = row["endpoint"]
        print(f"      {kind} {status} {endpoint}")
' "$DB"
else
    echo "  - No orchestration DB yet ($DB)"
fi

# ── ComfyUI workers ────────────────────────────────────────────────────────────

echo ""
echo "ComfyUI workers:"
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
