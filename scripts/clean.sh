#!/usr/bin/env bash
# Reset parts of the local install to a clean slate. Every mode prints exactly
# what it will delete and asks for confirmation first.
#
# Usage: bash scripts/clean.sh [--yes] <queue|workers|settings|all> [...]
#
#   queue      Clear the render queue, the publish queue and the orchestrator
#              DB. Rendered videos and their work dirs are NOT touched.
#   workers    Remove the worker container stacks (delegates to uninstall.sh).
#   settings   Delete config.yaml and the YouTube/X credentials, so the next
#              start re-seeds a fresh config. Voices, characters and the
#              engagement model are kept.
#   all        All of the above.
#   --yes      Skip the confirmation prompt.
#
# Deletions here are irreversible — take a backup from the Settings screen first
# if you might want any of it back.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# shellcheck source=scripts/_config.sh
source "$REPO_ROOT/scripts/_config.sh"

CONFIG_DIR="$HOME/.config/video-generator"
STATE_DIR="$HOME/.local/share/video-generator"

YES=false
MODES=()
for arg in "$@"; do
    case "$arg" in
        --yes)                     YES=true ;;
        queue|workers|settings)    MODES+=("$arg") ;;
        all)                       MODES+=(queue workers settings) ;;
        *) echo "Unknown option: $arg"; echo "Usage: $0 [--yes] <queue|workers|settings|all>"; exit 1 ;;
    esac
done

if [[ ${#MODES[@]} -eq 0 ]]; then
    echo "Usage: $0 [--yes] <queue|workers|settings|all>"
    echo ""
    echo "  queue     Clear the render + publish queues (keeps rendered videos)"
    echo "  workers   Remove the worker Docker stacks (containers, volumes, images)"
    echo "  settings  Delete config.yaml + the YouTube/X credentials"
    echo "  all       All of the above"
    exit 1
fi

_wants() { [[ " ${MODES[*]} " == *" $1 "* ]]; }

# Paths each mode deletes. Globs are expanded later, when they are listed and
# again when they are removed.
QUEUE_PATHS=(
    "$CONFIG_DIR/youtube_queue.json"
    "$CONFIG_DIR/publish_queue.json"
    "$STATE_DIR/orchestrator.sqlite3"
    "$STATE_DIR/orchestrator.sqlite3-wal"
    "$STATE_DIR/orchestrator.sqlite3-shm"
    "$STATE_DIR/rerender_journal.json"
)
SETTINGS_PATHS=(
    "$CONFIG_DIR/config.yaml"
    "$CONFIG_DIR/client_secrets.json"
    "$CONFIG_DIR"/youtube_token_*.json
    "$CONFIG_DIR"/x_token_*.json
    "$CONFIG_DIR/c2pa"
)

# Print the paths from a list that actually exist, one indented line each.
_list_existing() {
    local p found=false
    for p in "$@"; do
        if [[ -e "$p" ]]; then
            echo "      ${p/#$HOME/~}"
            found=true
        fi
    done
    $found || echo "      (nothing to delete)"
}

_is_app_running() {
    [[ -f /tmp/stephen_spielbot.pid ]] && kill -0 "$(cat /tmp/stephen_spielbot.pid)" 2>/dev/null && return 0
    pgrep -f "uvicorn webapp.backend.main" >/dev/null 2>&1
}

# ── Warn + confirm ────────────────────────────────────────────────────────────
echo ""
echo "!!  WARNING — this deletes data and cannot be undone.  !!"
echo ""
echo "This will:"

if _wants queue; then
    echo "  • DELETE the render queue, publish queue and render-progress DB:"
    _list_existing "${QUEUE_PATHS[@]}"
    echo "    (rendered videos and their work dirs are kept)"
fi
if _wants workers; then
    HOSTS="$(remote_hosts || true)"
    echo "  • REMOVE the worker container stacks (containers, volumes, locally-built"
    if [[ -n "$HOSTS" ]]; then
        echo "    images, ~/spielbot-worker) on: $(echo $HOSTS | tr '\n' ' ')"
    else
        echo "    images, ~/spielbot-worker) — but no workers are configured"
    fi
    echo "    (downloaded models are kept — purge: bash scripts/uninstall.sh --purge-models)"
fi
if _wants settings; then
    echo "  • DELETE your settings and API credentials:"
    _list_existing "${SETTINGS_PATHS[@]}"
    echo "    You will have to re-enter your API keys and reconnect YouTube/X."
    echo "    (voices, characters, image history and the engagement model are kept)"
fi

echo ""
if _wants queue && _is_app_running; then
    echo "  NOTE: the web app is running and will rewrite this state as it exits."
    echo "        Stop it first with 'make stop' for a clean result."
    echo ""
fi
echo "  Tip: Settings → Backup exports all of this first."
echo ""

if ! $YES; then
    if [[ -t 0 ]]; then
        read -rp "Type 'yes' to continue: " REPLY
        [[ "$REPLY" == "yes" ]] || { echo "Aborted."; exit 1; }
    else
        echo "Refusing to clean non-interactively without --yes."
        exit 1
    fi
fi

# ── Execute ───────────────────────────────────────────────────────────────────
_remove() {
    local p
    for p in "$@"; do
        [[ -e "$p" ]] || continue
        rm -rf "$p"
        echo "  removed ${p/#$HOME/~}"
    done
}

if _wants queue; then
    echo ""
    echo "=== Clearing the queues ==="
    _remove "${QUEUE_PATHS[@]}"
fi

if _wants workers; then
    echo ""
    bash "$REPO_ROOT/scripts/uninstall.sh" --workers-only --yes
fi

if _wants settings; then
    echo ""
    echo "=== Deleting settings + credentials ==="
    _remove "${SETTINGS_PATHS[@]}"
fi

echo ""
echo "Clean complete."
_wants settings && echo "  Next 'make start' seeds a fresh config.yaml."
_wants workers  && echo "  Re-deploy the workers with: make install"
exit 0
