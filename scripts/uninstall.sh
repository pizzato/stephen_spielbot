#!/usr/bin/env bash
# Uninstall Stephen Spielbot: stop everything, remove the system service and the
# worker container stacks. Data-preserving by default — config, models, and
# rendered videos survive unless explicitly purged.
#
# Usage: bash scripts/uninstall.sh [--yes] [--purge-data] [--purge-models]
#
#   (default)       Stop the web app + cover agent, remove the macOS launchd
#                   service, and on every worker host: remove the container
#                   stack (containers, volumes, locally-built images) and the
#                   ~/spielbot-worker build dir. Keeps config, state, models.
#   --purge-data    Also delete ~/.config/video-generator and
#                   ~/.local/share/video-generator on this machine (config +
#                   API tokens, orchestrator DB, voice library, logs).
#   --purge-models  Also delete ~/github/ComfyUI on every worker (the ~50+ GB
#                   model downloads) — but ONLY where Spielbot created that
#                   directory (install.sh leaves a .spielbot_created marker).
#                   A pre-existing ComfyUI install is never deleted (interactive
#                   runs ask; non-interactive runs skip it).
#   --yes           Skip the confirmation prompt.
#
# Rendered videos (~/videos) are NEVER touched. The repo itself (including
# .venv and the built frontend) is not deleted — remove the folder afterwards.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# shellcheck source=scripts/_config.sh
source "$REPO_ROOT/scripts/_config.sh"

YES=false
PURGE_DATA=false
PURGE_MODELS=false
for arg in "$@"; do
    case "$arg" in
        --yes)          YES=true ;;
        --purge-data)   PURGE_DATA=true ;;
        --purge-models) PURGE_MODELS=true ;;
        *) echo "Unknown option: $arg"; echo "Usage: $0 [--yes] [--purge-data] [--purge-models]"; exit 1 ;;
    esac
done

# Run a command on a worker host: locally for localhost, over SSH otherwise.
_on_host() {
    local host="$1"; shift
    if is_local_host "$host"; then bash -c "$*"; else ssh -- "$host" "$*"; fi
}

HOSTS="$(remote_hosts || true)"

echo "This will:"
echo "  • stop the web app and cover agent on this machine"
if [[ "$(uname)" == "Darwin" ]]; then
    echo "  • remove the launchd service (if installed)"
fi
if [[ -n "$HOSTS" ]]; then
    echo "  • remove the worker container stack (containers, volumes, images,"
    echo "    ~/spielbot-worker) on: $(echo $HOSTS | tr '\n' ' ')"
fi
$PURGE_DATA   && echo "  • DELETE config + state: ~/.config/video-generator, ~/.local/share/video-generator"
$PURGE_MODELS && echo "  • DELETE the downloaded models (~/github/ComfyUI) on workers where Spielbot created it"
echo "It will NOT touch rendered videos (~/videos) or this repo folder."
echo ""
if ! $YES; then
    if [[ -t 0 ]]; then
        read -rp "Continue? [y/N] " REPLY
        [[ "$REPLY" =~ ^[Yy] ]] || { echo "Aborted."; exit 1; }
    else
        echo "Refusing to uninstall non-interactively without --yes."
        exit 1
    fi
fi

# ── 1. Stop the app + cover agent ─────────────────────────────────────────────
bash "$REPO_ROOT/scripts/stop_server.sh" || true

# ── 2. Remove the macOS launchd service ───────────────────────────────────────
if [[ "$(uname)" == "Darwin" ]]; then
    bash "$REPO_ROOT/scripts/launchd.sh" uninstall || true
fi

# ── 3. Remove the worker container stacks ─────────────────────────────────────
for host in $HOSTS; do
    echo ""
    echo "=== Removing worker stack on $host ==="
    # --rmi all (not local): our images are tagged (spielbot-*:latest), which
    # --rmi local skips. Then belt-and-braces cleanup by compose-project label
    # and image tag, so leftovers go even when the compose dir is already gone.
    _on_host "$host" "cd ~/spielbot-worker/docker 2>/dev/null && docker compose down -v --rmi all" \
        || echo "  (no compose stack dir on $host — cleaning up directly)"
    _on_host "$host" "docker ps -aq --filter label=com.docker.compose.project=spielbot-worker | xargs -r docker rm -f" 2>/dev/null || true
    _on_host "$host" "docker volume ls -q --filter label=com.docker.compose.project=spielbot-worker | xargs -r docker volume rm -f" 2>/dev/null || true
    _on_host "$host" "docker rmi spielbot-comfyui:latest spielbot-tts:latest spielbot-echomimic:latest" 2>/dev/null || true
    _on_host "$host" "rm -rf ~/spielbot-worker" || true
    if $PURGE_MODELS; then
        # ~/github/ComfyUI is deleted ONLY if Spielbot created it (install.sh
        # drops a marker) — a pre-existing ComfyUI install must survive.
        if _on_host "$host" "[ -f ~/github/ComfyUI/.spielbot_created ]" 2>/dev/null; then
            echo "  deleting ~/github/ComfyUI on $host (created by Spielbot) ..."
            _on_host "$host" "rm -rf ~/github/ComfyUI" || true
        elif ! _on_host "$host" "[ -d ~/github/ComfyUI ]" 2>/dev/null; then
            echo "  no ~/github/ComfyUI on $host — nothing to delete"
        elif [[ -t 0 ]]; then
            read -rp "  ~/github/ComfyUI on $host has no Spielbot marker (pre-existing install?). Delete it anyway? [y/N] " R
            if [[ "$R" =~ ^[Yy] ]]; then
                _on_host "$host" "rm -rf ~/github/ComfyUI" || true
            else
                echo "  keeping ~/github/ComfyUI on $host"
            fi
        else
            echo "  keeping ~/github/ComfyUI on $host — no Spielbot marker (pre-existing ComfyUI?); delete manually if intended."
        fi
    fi
done

# ── 4. Purge controller config + state (opt-in) ───────────────────────────────
if $PURGE_DATA; then
    echo ""
    echo "=== Deleting config + state ==="
    rm -rf "$HOME/.config/video-generator" "$HOME/.local/share/video-generator"
    echo "  removed ~/.config/video-generator and ~/.local/share/video-generator"
fi

echo ""
echo "Uninstall complete."
echo "  Kept: rendered videos (~/videos) and this repo folder (delete it to finish)."
$PURGE_DATA || echo "  Kept: config + state (remove with: bash scripts/uninstall.sh --purge-data)."
$PURGE_MODELS || [[ -z "$HOSTS" ]] || echo "  Kept: worker models (remove with: bash scripts/uninstall.sh --purge-models)."
