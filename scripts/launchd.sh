#!/usr/bin/env bash
# Manage the Stephen Spielbot web server as a macOS LaunchAgent.
# The service keeps uvicorn running at login and auto-restarts it on crash.
# Usage: bash scripts/launchd.sh [install|uninstall|start|stop|restart|status]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.stephen-spielbot.server"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
VENV="$REPO_ROOT/.venv"
PYTHON="${VENV}/bin/python"
LOG_DIR="$HOME/.local/share/video-generator/logs"
LOG_FILE="$LOG_DIR/app.log"
DOMAIN="gui/$(id -u)"
SERVICE="${DOMAIN}/${LABEL}"

_is_loaded() {
    launchctl print "$SERVICE" &>/dev/null
}

case "${1:-}" in
  install)
    if [[ ! -x "$PYTHON" ]]; then
        echo "ERROR: .venv not found at $VENV — run 'make install' first (venv step)"
        exit 1
    fi
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST_DST")"
    cat > "$PLIST_DST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>webapp.backend.main:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8001</string>
        <string>--timeout-keep-alive</string>
        <string>30</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${REPO_ROOT}</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
</dict>
</plist>
PLIST
    # Reload if already registered, otherwise just bootstrap.
    if _is_loaded; then
        launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
        sleep 1
    fi
    launchctl bootstrap "$DOMAIN" "$PLIST_DST"
    echo "[launchd] installed and started: ${LABEL}"
    echo "[launchd] plist:  $PLIST_DST"
    echo "[launchd] logs:   $LOG_FILE"
    echo "[launchd] app:    http://localhost:8001"
    ;;

  uninstall)
    if _is_loaded; then
        launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
        echo "[launchd] stopped and unloaded: ${LABEL}"
    fi
    if [[ -f "$PLIST_DST" ]]; then
        rm -f "$PLIST_DST"
        echo "[launchd] plist removed: $PLIST_DST"
    else
        echo "[launchd] nothing to remove (plist not found)"
    fi
    ;;

  start)
    if [[ ! -f "$PLIST_DST" ]]; then
        echo "ERROR: service not installed — run: bash scripts/launchd.sh install"
        exit 1
    fi
    if _is_loaded; then
        echo "[launchd] already running: ${LABEL}"
    else
        launchctl bootstrap "$DOMAIN" "$PLIST_DST"
        echo "[launchd] started: ${LABEL}"
    fi
    ;;

  stop)
    if _is_loaded; then
        launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
        echo "[launchd] stopped: ${LABEL}"
    else
        echo "[launchd] not running: ${LABEL}"
    fi
    ;;

  restart)
    if [[ ! -f "$PLIST_DST" ]]; then
        echo "ERROR: service not installed — run: bash scripts/launchd.sh install"
        exit 1
    fi
    if _is_loaded; then
        launchctl kickstart -k "$SERVICE"
        echo "[launchd] restarted: ${LABEL}"
    else
        launchctl bootstrap "$DOMAIN" "$PLIST_DST"
        echo "[launchd] started: ${LABEL}"
    fi
    ;;

  status)
    if [[ ! -f "$PLIST_DST" ]]; then
        echo "[launchd] NOT INSTALLED (run: make launchd-install)"
        exit 0
    fi
    if _is_loaded; then
        PID=$(launchctl print "$SERVICE" 2>/dev/null | awk '/pid = /{print $NF}')
        [[ -z "$PID" ]] && PID=$(pgrep -f "uvicorn webapp.backend.main" 2>/dev/null | head -1 || echo "starting")
        echo "[launchd] RUNNING  (label=${LABEL}, pid=${PID})  http://localhost:8001"
    else
        echo "[launchd] STOPPED  (plist exists but service not loaded)"
    fi
    ;;

  *)
    echo "Usage: $0 [install|uninstall|start|stop|restart|status]"
    exit 1
    ;;
esac
