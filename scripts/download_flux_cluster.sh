#!/usr/bin/env bash
# Download FLUX.1-schnell models to the first cluster node, then rsync to all others.
# Usage: bash scripts/download_flux_cluster.sh [cluster.conf]
set -euo pipefail

CONF="${1:-cluster.conf}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

remote_hosts() {
    [ -f "$CONF" ] || return 0
    grep -v '^\s*#' "$CONF" | grep -v '^\s*$'
}

HOSTS="$(remote_hosts)"
if [[ -z "$HOSTS" ]]; then
    echo "ERROR: No remote hosts found in $CONF"
    exit 1
fi

FIRST_HOST="$(echo "$HOSTS" | head -1)"
ALL_HOSTS=($HOSTS)

FLUX_DIRS=("models/unet" "models/clip")

echo "=== Downloading FLUX.1-schnell models to $FIRST_HOST ==="
ssh "$FIRST_HOST" "mkdir -p \$HOME/github/video-generator/scripts"
rsync -q "$REPO_ROOT/scripts/download_models.sh" \
    "${FIRST_HOST}:~/github/video-generator/scripts/download_models.sh"
ssh "$FIRST_HOST" \
    "bash \$HOME/github/video-generator/scripts/download_models.sh \$HOME/github/ComfyUI"

echo ""
echo "=== Rsyncing FLUX models from $FIRST_HOST to remaining workers ==="
for host in "${ALL_HOSTS[@]}"; do
    [[ "$host" == "$FIRST_HOST" ]] && continue
    echo "--- $host ---"
    for dir in "${FLUX_DIRS[@]}"; do
        echo "  [rsync] $dir → $host"
        ssh -A "$host" "mkdir -p \$HOME/github/ComfyUI/$dir && \
            rsync -avz --ignore-existing \
                ${FIRST_HOST}:\$HOME/github/ComfyUI/$dir/ \
                \$HOME/github/ComfyUI/$dir/ 2>&1 | tail -3" \
        || echo "  [rsync] Warning: some files in $dir may have failed"
    done
    # Also sync the vae (ae.safetensors is in models/vae, shared with other models)
    echo "  [rsync] models/vae (ae.safetensors) → $host"
    ssh -A "$host" "mkdir -p \$HOME/github/ComfyUI/models/vae && \
        rsync -avz --ignore-existing \
            ${FIRST_HOST}:\$HOME/github/ComfyUI/models/vae/ \
            \$HOME/github/ComfyUI/models/vae/ 2>&1 | tail -3" \
    || echo "  [rsync] Warning: vae sync may have failed"
done

echo ""
echo "✅ FLUX models distributed. Run 'make restart' if ComfyUI is already running."
