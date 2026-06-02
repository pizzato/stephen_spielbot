#!/usr/bin/env bash
# Install Stephen Spielbot dependencies locally and on all cluster workers.
# Worker hosts come from config.yaml (comfy_workers). On a fresh machine with no
# config yet, this installs single-machine; add comfy_workers and re-run for a cluster.
# Usage: bash scripts/install.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# shellcheck source=scripts/_config.sh
source "$REPO_ROOT/scripts/_config.sh"

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
    # Prefer Python 3.13/3.12/3.11/3.10 — gradio requires 3.10+
    PYTHON3=""
    for candidate in python3.13 python3.12 python3.11 python3.10; do
        if command -v "$candidate" &>/dev/null; then
            PYTHON3="$(command -v "$candidate")"
            break
        fi
    done
    [[ -z "$PYTHON3" ]] && PYTHON3="python3"
    echo "[venv] creating $VENV with $("$PYTHON3" --version) ..."
    "$PYTHON3" -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet -r "$REPO_ROOT/requirements.txt"
echo "[venv] requirements installed at $VENV"

# ── 1b. Seed config.yaml (worker lists) on first install ──────────────────────
# Workers can be passed non-interactively:  make install WORKERS="s1 s2 s3"
# (env var WORKERS). Otherwise, if no config exists yet and we're on a TTY,
# prompt once. Existing config is never overwritten.

banner "Configuration"
if [[ -f "$CONFIG_YAML" ]]; then
    echo "[config] $CONFIG_YAML exists — leaving it untouched"
else
    WORKERS="${WORKERS:-}"
    if [[ -z "$WORKERS" ]] && [[ -t 0 ]]; then
        echo "No config yet. List your render worker hostnames (ComfyUI + F5-TTS),"
        echo "space-separated. Leave blank for a single-machine (localhost) setup."
        read -rp "Workers: " WORKERS
    fi
    "$VENV/bin/python" "$REPO_ROOT/scripts/init_config.py" $WORKERS
fi

# Re-evaluate hosts now that a config may have just been written.
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

# ── 3. Download models ────────────────────────────────────────────────────────
# Strategy:
#   • Local ComfyUI present  → download directly here
#   • Remote workers only    → download once on MODEL_SOURCE, others rsync from it
# Either way, ask for an optional HuggingFace token once to speed things up.

banner "Downloading models"

COMFY_DIR="${COMFY_DIR:-$HOME/github/ComfyUI}"
HF_TOKEN="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"

# Use the first cluster node as the model source (download once there, rsync to others)
FIRST_HOST="$(remote_hosts | head -1)"
MODEL_SOURCE="${MODEL_SOURCE:-${FIRST_HOST:-}}"

# Sentinel files — if all exist, all models (including FLUX) are assumed present
_models_present_on() {
    local host="$1"
    if [[ "$host" == "localhost" ]]; then
        [[ -f "$COMFY_DIR/models/checkpoints/ltx-2.3-22b-dev-fp8.safetensors" ]] && \
        [[ -f "$COMFY_DIR/models/diffusion_models/acestep_v1.5_turbo.safetensors" ]] && \
        [[ -f "$COMFY_DIR/models/unet/flux1-schnell-fp8.safetensors" ]]
    else
        ssh "$host" "[[ -f \$HOME/github/ComfyUI/models/checkpoints/ltx-2.3-22b-dev-fp8.safetensors && \
                        -f \$HOME/github/ComfyUI/models/diffusion_models/acestep_v1.5_turbo.safetensors && \
                        -f \$HOME/github/ComfyUI/models/unet/flux1-schnell-fp8.safetensors ]]" 2>/dev/null
    fi
}

_ask_hf_token() {
    if [[ -z "$HF_TOKEN" ]]; then
        echo ""
        echo "Models (~33 GB total) need to be downloaded."
        echo "A HuggingFace token speeds up downloads (optional — all models are public)."
        read -rp "HuggingFace token (press Enter to skip): " HF_TOKEN
    fi
}

if [[ -d "$COMFY_DIR" ]]; then
    # Local ComfyUI — download here
    if ! _models_present_on localhost; then
        _ask_hf_token
    fi
    HF_TOKEN="$HF_TOKEN" bash "$REPO_ROOT/scripts/download_models.sh" "$COMFY_DIR"

elif [[ -n "$(remote_hosts)" ]]; then
    # No local ComfyUI — download once on the first cluster node; others rsync from it
    if [[ -z "$MODEL_SOURCE" ]]; then
        echo "[models] No comfy_workers in config.yaml — skipping download."
    elif _models_present_on "$MODEL_SOURCE"; then
        echo "[models] All models already present on $MODEL_SOURCE — skipping download"
    else
        _ask_hf_token
        echo "[models] Downloading models to $MODEL_SOURCE (first node; others will rsync from it)..."
        # Sync the download script to the source machine and run it there
        ssh "$MODEL_SOURCE" "mkdir -p \$HOME/github/video-generator/scripts"
        rsync -q "$REPO_ROOT/scripts/download_models.sh" \
            "${MODEL_SOURCE}:~/github/video-generator/scripts/download_models.sh"
        ssh "$MODEL_SOURCE" \
            "HF_TOKEN='${HF_TOKEN}' bash \$HOME/github/video-generator/scripts/download_models.sh \$HOME/github/ComfyUI"
    fi

else
    echo "[models] No local ComfyUI and no remote workers — skipping."
    echo "  Add comfy_workers to config.yaml or install ComfyUI, then run: bash scripts/download_models.sh"
fi

# ── 3b. Web UI (FastAPI backend deps + React frontend build) ──────────────────

banner "Setting up web UI"
"$VENV/bin/pip" install --quiet -r "$REPO_ROOT/webapp/backend/requirements.txt"
echo "[web] backend deps installed"
if command -v npm &>/dev/null; then
    ( cd "$REPO_ROOT/webapp/frontend" && npm install --silent && npm run build )
    echo "[web] frontend built → webapp/frontend/dist"
else
    echo "[web] WARNING: npm not found — skipping frontend build."
    echo "  Install Node.js, then run: make web-build"
fi

# ── 4. Remote workers ──────────────────────────────────────────────────────────

HOSTS=$(remote_hosts)
if [[ -z "$HOSTS" ]]; then
    echo ""
    echo "No remote workers defined in config.yaml — single-machine setup complete."
    echo "Run 'make start' to launch."
    exit 0
fi

for host in $HOSTS; do
    banner "Installing worker: $host"
    bash "$REPO_ROOT/scripts/install_comfyui_worker.sh" "$host" "$MODEL_SOURCE"
    bash "$REPO_ROOT/scripts/install_f5tts_worker.sh"   "$host"
done

# ── 5. UI ComfyUI worker hosts ────────────────────────────────────────────────
# Any host in ui_workers that is NOT already a render worker gets its own
# ComfyUI install (SSH-based, same script). Covers localhost and dedicated
# UI machines alike.

_COMFY_HOSTS=" $(remote_hosts | tr '\n' ' ') "
for host in $(ui_hosts); do
    if [[ "$_COMFY_HOSTS" == *" $host "* ]]; then
        echo "[ui-comfy] $host already installed as a render worker — skipping"
        continue
    fi
    banner "Installing UI ComfyUI worker: $host"
    bash "$REPO_ROOT/scripts/install_comfyui_worker.sh" "$host" "$MODEL_SOURCE"
done

echo ""
echo "Installation complete. Run 'make start' to launch the cluster."
