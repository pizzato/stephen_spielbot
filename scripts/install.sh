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

# F5-TTS runs in the worker containers, so the controller needs no local F5-TTS
# environment (the workers handle narration over HTTP).

# ── 2. Download models ────────────────────────────────────────────────────────
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

# Sentinel files — if all exist, the default model set (LTX + ACE + FLUX.2 Klein)
# is assumed present. FLUX.1 is opt-in, so it is NOT part of the sentinel.
_models_present_on() {
    local host="$1"
    if [[ "$host" == "localhost" ]]; then
        [[ -f "$COMFY_DIR/models/checkpoints/ltx-2.3-22b-dev-fp8.safetensors" ]] && \
        [[ -f "$COMFY_DIR/models/diffusion_models/acestep_v1.5_turbo.safetensors" ]] && \
        [[ -f "$COMFY_DIR/models/diffusion_models/flux-2-klein-4b.safetensors" ]]
    else
        ssh "$host" "[[ -f \$HOME/github/ComfyUI/models/checkpoints/ltx-2.3-22b-dev-fp8.safetensors && \
                        -f \$HOME/github/ComfyUI/models/diffusion_models/acestep_v1.5_turbo.safetensors && \
                        -f \$HOME/github/ComfyUI/models/diffusion_models/flux-2-klein-4b.safetensors ]]" 2>/dev/null
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

# Workers mount models from their own ~/github/ComfyUI/models. Download once on
# the first worker (MODEL_SOURCE); install_worker_container.sh rsyncs them to the
# other workers during deploy.
if [[ -z "$MODEL_SOURCE" ]]; then
    echo "[models] No comfy_workers in config.yaml — skipping model download."
elif _models_present_on "$MODEL_SOURCE"; then
    echo "[models] All models already present on $MODEL_SOURCE — skipping download"
else
    _ask_hf_token
    echo "[models] Downloading models to $MODEL_SOURCE (first worker; others rsync from it during deploy)..."
    rsync -q "$REPO_ROOT/scripts/download_models.sh" "${MODEL_SOURCE}:/tmp/spielbot_download_models.sh"
    ssh "$MODEL_SOURCE" \
        "HF_TOKEN='${HF_TOKEN}' bash /tmp/spielbot_download_models.sh \$HOME/github/ComfyUI"
fi

# ── 3b. Web UI (FastAPI backend deps + React frontend build) ──────────────────

banner "Setting up web UI"
"$VENV/bin/pip" install --quiet -r "$REPO_ROOT/webapp/backend/requirements.txt"
echo "[web] backend deps installed"

# Pre-fetch the engagement-prediction embedding model (~130 MB, BAAI/bge-small-en-v1.5)
# so the first model build works offline without a surprise download. Non-fatal —
# fastembed lazy-downloads it on first use anyway if this is skipped.
echo "[web] pre-fetching engagement embedding model ..."
if "$VENV/bin/python" -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" >/dev/null 2>&1; then
    echo "[web] embedding model ready"
else
    echo "[web] WARNING: could not pre-fetch the embedding model — it will download on first build."
fi
if command -v npm &>/dev/null; then
    if ( cd "$REPO_ROOT/webapp/frontend" && npm install && npm run build ); then
        echo "[web] frontend built → webapp/frontend/dist"
    else
        echo "[web] WARNING: frontend build failed — run 'make web-build' to retry."
        echo "  (Any existing dist/ is still served; build when ready.)"
    fi
else
    echo "[web] WARNING: npm not found — skipping frontend build."
    echo "  Install Node.js, then run: make web-build"
fi

# ── 3c. macOS LaunchAgent (auto-start + auto-restart on login) ───────────────

banner "Installing system service (launchd)"
if [[ "$(uname)" == "Darwin" ]]; then
    bash "$REPO_ROOT/scripts/launchd.sh" install
else
    echo "[launchd] skipped (not macOS)"
fi

# ── 4. Deploy worker containers ───────────────────────────────────────────────
# Each render worker runs ComfyUI + F5-TTS as Docker containers, deployed over
# SSH (issue #12). Needs Docker + the NVIDIA Container Toolkit on each worker.

HOSTS=$(remote_hosts)
if [[ -z "$HOSTS" ]]; then
    echo ""
    echo "No workers defined in config.yaml (comfy_workers) — nothing to deploy."
    echo "Add comfy_workers and re-run, or run 'make start' for just the web app."
    exit 0
fi

for host in $HOSTS; do
    banner "Deploying container worker: $host"
    MODEL_SOURCE="$MODEL_SOURCE" bash "$REPO_ROOT/scripts/install_worker_container.sh" "$host"
done

# Point the config at the containers we just deployed:
#   tts_workers → http://host:8189  (http:// selects the HTTP transport)
# comfy_workers are already http://host:8188 URLs, so they need no change.
# Cover regen reuses a render worker the backend keeps idle while the UI is in
# use (issue #98) — there is no separate ui_workers list anymore.
"$VENV/bin/python" - "$CONFIG_YAML" $HOSTS <<'PY'
import sys, yaml
path, hosts = sys.argv[1], sys.argv[2:]
data = yaml.safe_load(open(path)) or {}
data["tts_workers"] = [f"http://{h}:8189" for h in hosts]
data.pop("ui_workers", None)  # removed concept (issue #98)
# Same dump options as app.save_config, so this matches an in-app settings save.
with open(path, "w") as f:
    yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
print("[config] tts_workers ->", data["tts_workers"])
PY

echo ""
echo "Installation complete. Containers are up (compose 'restart: unless-stopped')."
echo "Run 'make start' to launch the web app (and (re)start every worker)."
