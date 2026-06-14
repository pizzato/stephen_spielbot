#!/usr/bin/env bash
# Show health of the web app and every containerized worker.
# Worker hosts come from config.yaml (comfy_workers). Workers run as Docker
# containers (ComfyUI :8188 + F5-TTS :8189), managed over SSH.
# Usage: bash scripts/status.sh

PID_FILE="/tmp/stephen_spielbot.pid"
ALL_OK=true
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# shellcheck source=scripts/_config.sh
source "$REPO_ROOT/scripts/_config.sh"

check_health() {
    local label="$1" url="$2"
    if curl -sf -m 5 "$url" &>/dev/null; then
        echo "    ✓ ${label} — UP    ${url%/*}"
    else
        echo "    ✗ ${label} — DOWN  ${url%/*}"
        ALL_OK=false
    fi
}

# ── Web app ────────────────────────────────────────────────────────────────────

LABEL="com.stephen-spielbot.server"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"
SERVICE="${DOMAIN}/${LABEL}"

echo "=== Stephen Spielbot status ==="
echo ""
echo "Web app:"
if [[ -f "$PLIST_DST" ]]; then
    if launchctl print "$SERVICE" &>/dev/null 2>&1; then
        PID=$(launchctl print "$SERVICE" 2>/dev/null | awk '/pid = /{print $NF}')
        [[ -z "$PID" ]] && PID=$(pgrep -f "uvicorn webapp.backend.main" 2>/dev/null | head -1 || echo "starting")
        echo "  ✓ Running  (launchd, pid=${PID})  http://localhost:8001"
    else
        echo "  ✗ Not running  (launchd service installed but stopped — run: make start)"
        ALL_OK=false
    fi
elif [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "  ✓ Running  (PID $(cat "$PID_FILE"))  http://localhost:8001"
elif pgrep -f "uvicorn webapp.backend.main" &>/dev/null; then
    echo "  ✓ Running  (PID $(pgrep -f 'uvicorn webapp.backend.main'))  http://localhost:8001"
else
    echo "  ✗ Not running"
    ALL_OK=false
fi

echo ""
echo "UI worker(s):"
if pgrep -f "worker_agent.py --kind ui" &>/dev/null; then
    echo "  ✓ Running  (PID $(pgrep -f 'worker_agent.py --kind ui' | tr '\n' ' '))"
else
    echo "  ✗ Not running (cover regeneration will queue without a ui worker)"
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

# ── Worker containers ──────────────────────────────────────────────────────────

echo ""
echo "Worker containers:"
for host in $(remote_hosts); do
    echo "  ${host}:"
    ssh -o ConnectTimeout=5 "$host" \
        'cd ~/spielbot-worker/docker 2>/dev/null && docker compose ps --format "    {{.Service}}  {{.Status}}"' \
        2>/dev/null || echo "    (no container stack — run: make install)"
    check_health "ComfyUI ${host}" "http://${host}:8188/system_stats"
    check_health "F5-TTS  ${host}" "http://${host}:8189/health"
done

# ── Summary ────────────────────────────────────────────────────────────────────

echo ""
if $ALL_OK; then
    echo "All services are UP."
else
    echo "Some services are DOWN. Run 'make start' to restart them."
    exit 1
fi
