#!/usr/bin/env bash
# Start/stop local "ui" worker agents — one per endpoint in config.yaml (ui_workers).
#
# A ui worker leases lightweight interface tasks (cover-image regeneration) from
# the durable orchestrator and renders them against a ComfyUI endpoint, so those
# tasks don't wait behind the heavy render workers.
#
# The worker PROCESS runs on this machine (it reads the local orchestrator DB);
# the ComfyUI endpoints (config.yaml ui_workers) may be local or remote.
#
# Usage:
#   bash scripts/ui_worker.sh start
#   bash scripts/ui_worker.sh stop
#   bash scripts/ui_worker.sh status
set -euo pipefail

ACTION="${1:-start}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
PYTHON="${VENV}/bin/python"
PID_DIR="/tmp/stephen_spielbot_ui"
LOG_DIR="$HOME/.local/share/video-generator/logs"

# shellcheck source=scripts/_config.sh
source "$REPO_ROOT/scripts/_config.sh"

endpoints() { ui_endpoints; }

case "$ACTION" in
  start)
    if [[ ! -x "$PYTHON" ]]; then
        echo "ERROR: virtual environment not found at $VENV — run 'make install' first"
        exit 1
    fi
    mkdir -p "$PID_DIR" "$LOG_DIR"
    n=0
    while IFS= read -r endpoint; do
        n=$((n + 1))
        pid_file="$PID_DIR/ui_${n}.pid"
        log_file="$LOG_DIR/ui_worker_${n}.log"
        if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            echo "  [ui-worker $n] already running (PID $(cat "$pid_file"))  $endpoint"
            continue
        fi
        cd "$REPO_ROOT"
        nohup "$PYTHON" worker_agent.py --kind ui --endpoint "$endpoint" \
            >>"$log_file" 2>&1 &
        echo $! > "$pid_file"
        echo "  [ui-worker $n] started (PID $!)  $endpoint  (log: $log_file)"
    done < <(endpoints)
    if [[ "$n" -eq 0 ]]; then
        echo "  [ui-worker] no ui_workers in config.yaml — cover regeneration will not run"
    fi
    ;;

  stop)
    stopped=0
    if [[ -d "$PID_DIR" ]]; then
        for pid_file in "$PID_DIR"/ui_*.pid; do
            [[ -e "$pid_file" ]] || continue
            PID="$(cat "$pid_file")"
            if kill -0 "$PID" 2>/dev/null; then
                kill "$PID"
                echo "  [ui-worker] stopped (PID $PID)"
                stopped=$((stopped + 1))
            fi
            rm -f "$pid_file"
        done
    fi
    # Fallback: catch any ui workers started outside this script.
    PIDS=$(pgrep -f "worker_agent.py --kind ui" 2>/dev/null || true)
    if [[ -n "$PIDS" ]]; then
        kill $PIDS 2>/dev/null || true
        echo "  [ui-worker] stopped stragglers (PID $PIDS)"
        stopped=$((stopped + 1))
    fi
    [[ "$stopped" -eq 0 ]] && echo "  [ui-worker] not running"
    ;;

  status)
    PIDS=$(pgrep -f "worker_agent.py --kind ui" 2>/dev/null || true)
    if [[ -n "$PIDS" ]]; then
        echo "  ✓ ui worker(s) running (PID $PIDS)"
    else
        echo "  ✗ no ui worker running"
    fi
    ;;

  *)
    echo "Usage: bash scripts/ui_worker.sh start|stop|status"
    exit 1
    ;;
esac
