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

_find_conda() {
    for p in \
        "$HOME/miniconda3/bin/conda" \
        "$HOME/miniforge3/bin/conda" \
        "$HOME/anaconda3/bin/conda" \
        "$HOME/opt/miniconda3/bin/conda" \
        "$HOME/opt/miniforge3/bin/conda" \
        "$HOME/opt/anaconda3/bin/conda" \
        "/opt/conda/bin/conda" \
        "/usr/local/bin/conda"; do
        [[ -x "$p" ]] && echo "$p" && return 0
    done
    # Fall back to PATH (works when conda shell function is active)
    local c
    c="$(command -v conda 2>/dev/null || true)"
    [[ -x "$c" ]] && echo "$c" && return 0
    return 1
}

# ── 1. Local Python venv + dependencies ───────────────────────────────────────

banner "Setting up local Python environment"
VENV="$REPO_ROOT/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
    echo "[venv] creating $VENV ..."
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet -r "$REPO_ROOT/requirements.txt"
echo "[venv] requirements installed at $VENV"

# ── 2. Local F5-TTS environment ───────────────────────────────────────────────

banner "Checking local F5-TTS environment"
F5_ENV="${F5TTS_PYTHON:-}"

# If not overridden, search for the conda-managed f5tts env
if [[ -z "$F5_ENV" ]]; then
    CONDA="$(_find_conda || true)"
    if [[ -n "$CONDA" ]]; then
        CONDA_BASE="$(dirname "$(dirname "$CONDA")")"
        F5_ENV="$CONDA_BASE/envs/f5tts/bin/python"
    fi
fi

if [[ -n "$F5_ENV" ]] && [[ -x "$F5_ENV" ]] && "$F5_ENV" -c "import f5_tts" 2>/dev/null; then
    echo "[f5tts] already installed at $F5_ENV"
else
    CONDA="${CONDA:-$(_find_conda || true)}"
    if [[ -z "$CONDA" ]]; then
        echo "[f5tts] WARNING: conda not found — skipping F5-TTS install."
        echo "  Install Miniconda/Miniforge first to enable local TTS:"
        echo "  https://github.com/conda-forge/miniforge"
        echo "  Then re-run 'make install'."
    else
        CONDA_BASE="$(dirname "$(dirname "$CONDA")")"
        echo "[f5tts] installing into conda env f5tts (conda: $CONDA) ..."
        "$CONDA" create -n f5tts python=3.10 -y
        # Install cmake + llvmlite via conda to avoid compilation issues
        "$CONDA" install -n f5tts -c conda-forge cmake llvmlite numba -y --quiet
        "$CONDA_BASE/envs/f5tts/bin/pip" install --quiet f5-tts
        echo "[f5tts] installed"
    fi
fi

# ── 3. Download LTX 2.3 + ACE-Step models ────────────────────────────────────

banner "Downloading models"
COMFY_DIR="${COMFY_DIR:-$HOME/github/ComfyUI}"
if [[ ! -d "$COMFY_DIR" ]]; then
    echo "[models] WARNING: ComfyUI not found at $COMFY_DIR — skipping model download."
    echo "  Install ComfyUI first, then run:  bash scripts/download_models.sh"
else
    bash "$REPO_ROOT/scripts/download_models.sh" "$COMFY_DIR"
fi

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
