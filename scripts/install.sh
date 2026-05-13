#!/usr/bin/env bash
# Install Stephen Spielbot dependencies locally and on all cluster workers.
# Usage: bash scripts/install.sh [cluster.conf]
set -euo pipefail

CONF="${1:-cluster.conf}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── Helpers ────────────────────────────────────────────────────────────────────

remote_hosts() {
    [ -f "$CONF" ] || return 0
    grep -v '^\s*#' "$CONF" | grep -v '^\s*$'
}

banner() { echo ""; echo "=== $* ==="; }

# ── 1. Local Python dependencies ──────────────────────────────────────────────

banner "Installing local Python dependencies"
python3 -m pip install --quiet -r "$REPO_ROOT/requirements.txt"
echo "[pip] requirements installed"

# ── 2. Local F5-TTS environment ───────────────────────────────────────────────

banner "Checking local F5-TTS environment"
F5_ENV="${F5TTS_PYTHON:-$HOME/miniconda3/envs/f5tts/bin/python}"

if [[ -x "$F5_ENV" ]] && "$F5_ENV" -c "import f5_tts" 2>/dev/null; then
    echo "[f5tts] already installed at $F5_ENV"
else
    echo "[f5tts] installing into ~/miniconda3/envs/f5tts ..."
    CONDA="$HOME/miniconda3/bin/conda"
    if [[ ! -x "$CONDA" ]]; then
        echo "[f5tts] Miniconda not found at $HOME/miniconda3 — install it first:"
        echo "  https://docs.conda.io/en/latest/miniconda.html"
        exit 1
    fi
    "$CONDA" create -n f5tts python=3.10 -y
    "$HOME/miniconda3/envs/f5tts/bin/pip" install --quiet f5-tts
    echo "[f5tts] installed"
fi

# ── 3. Download LTX 2.3 + ACE-Step models ────────────────────────────────────

banner "Downloading models"
bash "$REPO_ROOT/scripts/download_models.sh"

# ── 4. Remote workers ──────────────────────────────────────────────────────────

HOSTS=$(remote_hosts)
if [[ -z "$HOSTS" ]]; then
    echo ""
    echo "No remote workers defined in $CONF — single-machine setup complete."
    exit 0
fi

for host in $HOSTS; do
    banner "Installing worker: $host"
    bash "$REPO_ROOT/scripts/install_comfyui_worker.sh" "$host"
    bash "$REPO_ROOT/scripts/install_f5tts_worker.sh"   "$host"
done

echo ""
echo "Installation complete. Run 'make start' to launch the cluster."
