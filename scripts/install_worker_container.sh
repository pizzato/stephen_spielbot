#!/usr/bin/env bash
# Deploy the containerized worker stack (ComfyUI + F5-TTS) to a remote host over
# SSH (issue #12). The controller rsyncs the build context to the host and drives
# `docker compose up -d --build` there — the container edition of
# install_comfyui_worker.sh + install_f5tts_worker.sh.
#
# The host needs Docker + the NVIDIA Container Toolkit already installed; this
# script checks for them but does NOT install Docker. Models are NOT copied —
# the ComfyUI container mounts the host's existing ~/github/ComfyUI/models.
#
# Usage: bash scripts/install_worker_container.sh <host>
# Env overrides (mostly for testing):
#   COMFYUI_PORT (8188)  TTS_PORT (8189)  STOP_NATIVE (true)  MODELS_DIR (remote ~/github/ComfyUI/models)
set -euo pipefail

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
    echo "Usage: $0 <host>"
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMFYUI_PORT="${COMFYUI_PORT:-8188}"
TTS_PORT="${TTS_PORT:-8189}"
STOP_NATIVE="${STOP_NATIVE:-true}"
REMOTE_BUILD_DIR="spielbot-worker"   # under the remote $HOME

echo "=== Deploying containerized workers to $TARGET ==="

# ── 1. Preflight: Docker + Compose v2 + NVIDIA runtime ────────────────────────
ssh "$TARGET" bash <<'REMOTE'
set -euo pipefail
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found on $(hostname). Install Docker + the NVIDIA Container Toolkit first."
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: 'docker compose' (v2) not available on $(hostname)."
    exit 1
fi
if ! docker info 2>/dev/null | grep -qi nvidia; then
    echo "WARNING: NVIDIA runtime not detected in 'docker info' on $(hostname) — the GPU may be unavailable to containers."
fi
echo "[preflight] $(docker --version) — OK"
REMOTE

# Resolve the remote home so MODELS_DIR in .env is an absolute path (compose does
# not expand $HOME inside .env values).
REMOTE_HOME="$(ssh "$TARGET" 'echo $HOME')"
REMOTE_MODELS="${MODELS_DIR:-$REMOTE_HOME/github/ComfyUI/models}"

# ── 2. Rsync the build context (controller → host) ────────────────────────────
echo "[deploy] syncing build context to $TARGET:~/$REMOTE_BUILD_DIR ..."
ssh "$TARGET" "mkdir -p ~/$REMOTE_BUILD_DIR"
rsync -az --delete "$REPO_ROOT/docker"   "$TARGET:~/$REMOTE_BUILD_DIR/"
rsync -az          "$REPO_ROOT/pipeline" "$TARGET:~/$REMOTE_BUILD_DIR/"
rsync -az          "$REPO_ROOT/assets"   "$TARGET:~/$REMOTE_BUILD_DIR/"
rsync -az          "$REPO_ROOT/.dockerignore" "$TARGET:~/$REMOTE_BUILD_DIR/.dockerignore"

# ── 3. Write docker/.env on the host (mount existing models, GB10 CUDA defaults)
ssh "$TARGET" "cat > ~/$REMOTE_BUILD_DIR/docker/.env" <<ENV
MODELS_DIR=${REMOTE_MODELS}
COMFYUI_REF=master
BASE_IMAGE=nvidia/cuda:13.0.1-runtime-ubuntu24.04
TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130
COMFYUI_PORT=${COMFYUI_PORT}
TTS_PORT=${TTS_PORT}
ENV
echo "[deploy] models mounted from $TARGET:$REMOTE_MODELS"

# Non-fatal: the build works without models, but renders need them. The
# container mounts the host's existing models (it does not download them).
if ! ssh "$TARGET" "[ -f '$REMOTE_MODELS/checkpoints/ltx-2.3-22b-dev-fp8.safetensors' ]" 2>/dev/null; then
    echo "[deploy] WARNING: LTX models not found at $TARGET:$REMOTE_MODELS"
    echo "         Populate them before rendering — e.g. run 'bash scripts/download_models.sh"
    echo "         ~/github/ComfyUI' on $TARGET, or rsync them from a worker that has them."
fi

# ── 4. Stop the native ComfyUI so the container can claim :$COMFYUI_PORT + GPU ─
if [[ "$STOP_NATIVE" == "true" ]]; then
    echo "[deploy] stopping native ComfyUI on $TARGET (frees :8188 + GPU) ..."
    bash "$REPO_ROOT/scripts/worker.sh" stop "$TARGET" || true
fi

# ── 5. Build + start the containers on the host ───────────────────────────────
echo "[deploy] building + starting containers on $TARGET (first build downloads torch — a few minutes) ..."
ssh "$TARGET" "cd ~/$REMOTE_BUILD_DIR/docker && docker compose up -d --build"

# ── 6. Wait for health ────────────────────────────────────────────────────────
echo -n "[deploy] waiting for ComfyUI (:$COMFYUI_PORT) and F5-TTS (:$TTS_PORT) on $TARGET"
for i in $(seq 1 40); do
    if ssh "$TARGET" "curl -sf http://localhost:$COMFYUI_PORT/system_stats >/dev/null 2>&1 && curl -sf http://localhost:$TTS_PORT/health >/dev/null 2>&1"; then
        echo " ✓"
        break
    fi
    echo -n "."
    sleep 5
done

echo ""
echo "✅ Containerized worker ready on $TARGET"
echo "   ComfyUI: http://${TARGET}:${COMFYUI_PORT}    F5-TTS: http://${TARGET}:${TTS_PORT}"
