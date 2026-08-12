#!/usr/bin/env bash
# Deploy the containerized worker stack (ComfyUI + F5-TTS) to a worker host
# (issue #12). Remote hosts get the build context rsynced over SSH and
# `docker compose up -d --build` driven there; "localhost" is deployed with
# plain local commands (single-machine setup, no SSH needed). Called per host
# by `make install`.
#
# The host needs Docker + the NVIDIA Container Toolkit already installed; this
# script checks for them but does NOT install Docker. Models are NOT copied —
# the ComfyUI container mounts the host's existing ~/github/ComfyUI/models.
#
# Usage: bash scripts/install_worker_container.sh <host>
# Env:
#   MODEL_SOURCE   host to rsync models from if <host> has none (set by install.sh)
#   GPU_MODE       "cdi" = modern CDI device injection (fixes cuInit 999 on new
#                  drivers where the legacy --gpus path is broken); default is
#                  the legacy path. Sticky: a host once deployed with cdi keeps
#                  it on later runs unless GPU_MODE=legacy is passed.
#   COMFYUI_PORT (8188)  TTS_PORT (8189)  STOP_NATIVE (true)  MODELS_DIR (remote ~/github/ComfyUI/models)
#   (the port/STOP_NATIVE overrides are mainly for non-disruptive testing)
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
GPU_MODE="${GPU_MODE:-}"             # "" = keep host's previous mode (default legacy)
REMOTE_BUILD_DIR="spielbot-worker"   # under the target $HOME

LOCAL=false
if [[ "$TARGET" == "localhost" || "$TARGET" == "127.0.0.1" ]]; then
    LOCAL=true
fi

# Run a command on the target: locally for localhost, over SSH otherwise.
# stdin (heredocs) flows through either way.
_sh() {
    if $LOCAL; then bash -c "$*"; else ssh -- "$TARGET" "$*"; fi
}

echo "=== Deploying containerized workers to $TARGET ==="

# ── 1. Preflight: Docker + Compose v2 + NVIDIA runtime ────────────────────────
_sh bash <<'REMOTE'
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
# Smoke-test that a container actually starts, so host-level problems (e.g. a
# kernel that can't create veth pairs after an unrebooted kernel upgrade) fail
# here with guidance instead of minutes into the image build.
if ! out=$(docker run --rm hello-world 2>&1); then
    echo "ERROR: cannot start a test container on $(hostname):"
    echo "$out" | tail -5 | sed 's/^/    /'
    case "$out" in
        *veth*|*"operation not supported"*)
            echo "  The kernel refused to create container network interfaces — usually a kernel"
            echo "  upgrade pending a reboot (the running kernel's 'veth' module is gone)."
            echo "  Try: sudo modprobe veth — and if that fails, reboot, then re-run." ;;
        *)
            echo "  Fix Docker on this host (a plain 'docker run --rm hello-world' must work), then re-run." ;;
    esac
    exit 1
fi
# CUDA needs /dev/nvidia-uvm; nvidia-smi does not (it auto-creates only
# /dev/nvidiactl + /dev/nvidiaN). On a host that has never run CUDA (fresh
# driver install, no reboot) the uvm node is often missing — containers then
# see the GPU in nvidia-smi but torch fails with "Found no NVIDIA driver".
# nvidia-modprobe is setuid root, so try creating it ourselves first.
if [[ -e /dev/nvidiactl && ! -e /dev/nvidia-uvm ]]; then
    echo "[preflight] /dev/nvidia-uvm missing — trying to load the nvidia-uvm module ..."
    nvidia-modprobe -u -c=0 2>/dev/null || sudo -n modprobe nvidia-uvm 2>/dev/null || true
    if [[ -e /dev/nvidia-uvm ]]; then
        echo "[preflight] nvidia-uvm loaded"
    else
        echo "ERROR: /dev/nvidia-uvm is missing on $(hostname) and could not be created."
        echo "  nvidia-smi works without it, but CUDA inside the containers will fail"
        echo "  ('Found no NVIDIA driver on your system'). Fix on this host:"
        echo "      sudo modprobe nvidia-uvm        # or reboot if that fails"
        echo "      echo nvidia-uvm | sudo tee /etc/modules-load.d/nvidia-uvm.conf   # survive reboots"
        echo "  then re-run."
        exit 1
    fi
fi
echo "[preflight] $(docker --version) — container start OK"
REMOTE

# Resolve the target home so MODELS_DIR in .env is an absolute path (compose does
# not expand $HOME inside .env values).
if $LOCAL; then
    REMOTE_HOME="$HOME"
else
    REMOTE_HOME="$(ssh "$TARGET" 'echo $HOME')"
fi
REMOTE_MODELS="${MODELS_DIR:-$REMOTE_HOME/github/ComfyUI/models}"

# GPU injection mode. Sticky per host: read the previous .env (before the rsync
# below replaces the build context) so a host once deployed with CDI stays on
# CDI across re-deploys unless GPU_MODE=legacy is passed explicitly.
if [[ -z "$GPU_MODE" ]] && _sh "grep -qs 'docker-compose.cdi.yml' ~/$REMOTE_BUILD_DIR/docker/.env" 2>/dev/null; then
    GPU_MODE=cdi
    echo "[deploy] keeping CDI GPU mode from the previous deploy (override with GPU_MODE=legacy)"
fi
if [[ "$GPU_MODE" == "cdi" ]]; then
    if ! _sh "grep -qs 'nvidia.com/gpu' /etc/cdi/*.yaml /etc/cdi/*.json /var/run/cdi/*.yaml /var/run/cdi/*.json" 2>/dev/null; then
        echo "ERROR: GPU_MODE=cdi but no NVIDIA CDI spec found on $TARGET."
        echo "  Generate it there first:  sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"
        echo "  (and regenerate after driver upgrades), then re-run."
        exit 1
    fi
    # The spec embeds versioned driver library paths (libcuda.so.<version>), so
    # a driver upgrade silently stales it and containers fail at start. Compare
    # against the running driver and refuse to deploy against a stale spec.
    SPEC_VER="$(_sh "grep -rhoE 'libcuda\.so\.[0-9]+\.[0-9.]+' /etc/cdi/*.yaml /etc/cdi/*.json /var/run/cdi/*.yaml /var/run/cdi/*.json 2>/dev/null | head -1" 2>/dev/null | sed 's/^libcuda\.so\.//')"
    DRV_VER="$(_sh "nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1" 2>/dev/null | tr -d '[:space:]')"
    if [[ -n "$SPEC_VER" && -n "$DRV_VER" && "$SPEC_VER" != "$DRV_VER" ]]; then
        echo "ERROR: the CDI spec on $TARGET is stale — it references driver $SPEC_VER but $DRV_VER is running."
        echo "  Regenerate it:  sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"
        echo "  Tip: newer NVIDIA Container Toolkits ship an auto-refresh unit — enable it once with:"
        echo "       sudo systemctl enable --now nvidia-cdi-refresh.path   (if present)"
        echo "  then re-run."
        exit 1
    fi
    echo "[deploy] using CDI GPU injection (nvidia.com/gpu=all)"
fi

# ── 2. Rsync the build context (controller → host) ────────────────────────────
if $LOCAL; then
    BUILD_DEST="$REMOTE_HOME/$REMOTE_BUILD_DIR"
else
    BUILD_DEST="$TARGET:~/$REMOTE_BUILD_DIR"
fi
echo "[deploy] syncing build context to $BUILD_DEST ..."
_sh "mkdir -p ~/$REMOTE_BUILD_DIR"
rsync -az --delete "$REPO_ROOT/docker"   "$BUILD_DEST/"
rsync -az          "$REPO_ROOT/pipeline" "$BUILD_DEST/"
rsync -az          "$REPO_ROOT/assets"   "$BUILD_DEST/"
rsync -az          "$REPO_ROOT/.dockerignore" "$BUILD_DEST/.dockerignore"

# ── 3. Write docker/.env on the host (mount existing models, GB10 CUDA defaults)
_sh "cat > ~/$REMOTE_BUILD_DIR/docker/.env" <<ENV
MODELS_DIR=${REMOTE_MODELS}
COMFYUI_INPUT_DIR=${REMOTE_HOME}/github/ComfyUI/input
# Pinned ComfyUI release: v0.32.0 = first with native LTX 2.5 support
# (v0.30.0 added the MiniMax H3 nodes, v0.31.0 the w4a8 loaders).
COMFYUI_REF=v0.32.0
BASE_IMAGE=nvidia/cuda:13.0.1-runtime-ubuntu24.04
TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130
COMFYUI_PORT=${COMFYUI_PORT}
TTS_PORT=${TTS_PORT}
# DGX Spark / GB10: use cuBLASLt so FLUX.2's bf16 GEMMs don't hit the failing
# legacy cuBLAS path (CUBLAS_STATUS_INTERNAL_ERROR). Harmless on other GPUs.
TORCH_BLAS_PREFER_CUBLASLT=1
ENV
if [[ "$GPU_MODE" == "cdi" ]]; then
    _sh "cat >> ~/$REMOTE_BUILD_DIR/docker/.env" <<'ENV'
# CDI GPU injection (see docker-compose.cdi.yml) — every plain `docker compose`
# run in this directory picks this up automatically.
COMPOSE_FILE=docker-compose.yml:docker-compose.cdi.yml
ENV
fi
echo "[deploy] models mounted from $TARGET:$REMOTE_MODELS"

# The container mounts the host's existing models (it does not download them).
# If they're missing and a MODEL_SOURCE host is given, rsync them over (host↔host
# via SSH agent forwarding); otherwise warn — the build works, but renders need them.
if _sh "[[ -f '$REMOTE_MODELS/diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors' && -f '$REMOTE_MODELS/checkpoints/ltx-2.3-22b-dev-fp8.safetensors' && -f '$REMOTE_MODELS/latent_upscale_models/ltx-2.3-spatial-upscaler-x2-1.1.safetensors' ]]" 2>/dev/null; then
    echo "[deploy] models present on $TARGET"
elif [[ -n "${MODEL_SOURCE:-}" && "$MODEL_SOURCE" != "$TARGET" ]]; then
    echo "[deploy] models missing on $TARGET — rsyncing from $MODEL_SOURCE (this can take a while) ..."
    for d in checkpoints diffusion_models loras latent_upscale_models text_encoders upscale_models vae unet clip; do
        if $LOCAL; then
            mkdir -p "$REMOTE_MODELS/$d" && rsync -az --ignore-existing "${MODEL_SOURCE}:$REMOTE_MODELS/$d/" "$REMOTE_MODELS/$d/" 2>/dev/null \
                || echo "[deploy]   warning: rsync of $d may be incomplete"
        else
            ssh -A "$TARGET" "mkdir -p '$REMOTE_MODELS/$d' && rsync -az --ignore-existing '${MODEL_SOURCE}:$REMOTE_MODELS/$d/' '$REMOTE_MODELS/$d/'" 2>/dev/null \
                || echo "[deploy]   warning: rsync of $d may be incomplete"
        fi
    done
else
    echo "[deploy] WARNING: LTX models not found at $TARGET:$REMOTE_MODELS and no MODEL_SOURCE to sync from."
    echo "         Populate them before rendering: 'bash scripts/download_models.sh ~/github/ComfyUI' on $TARGET."
fi

# ── 4. Stop any pre-existing native ComfyUI so the container can claim :8188 ───
# One-time cutover from the old SSH/conda install — kills the native process on
# :8188 (systemd or nohup), but never a docker-proxy (a container already there).
if [[ "$STOP_NATIVE" == "true" ]]; then
    echo "[deploy] stopping any native ComfyUI on $TARGET (frees :8188 + GPU) ..."
    _sh bash <<'REMOTE' || true
        if systemctl --user is-active comfyui-worker.service &>/dev/null; then
            systemctl --user stop comfyui-worker.service && echo "  native ComfyUI stopped (systemd)"
        fi
        for pid in $(lsof -ti TCP:8188 -s TCP:LISTEN 2>/dev/null || true); do
            if ps -p "$pid" -o comm= 2>/dev/null | grep -qiv docker; then
                kill "$pid" 2>/dev/null && echo "  native ComfyUI stopped (PID $pid)"
            fi
        done
REMOTE
fi

# ── 5. Build + start the containers on the host ───────────────────────────────
# --force-recreate: always fresh containers, so they pick up the host's CURRENT
# NVIDIA device nodes (see the note in worker.sh start).
echo "[deploy] building + starting containers on $TARGET (first build downloads torch — a few minutes) ..."
_sh "cd ~/$REMOTE_BUILD_DIR/docker && docker compose up -d --build --force-recreate"

# ── 6. Wait for health ────────────────────────────────────────────────────────
echo -n "[deploy] waiting for ComfyUI (:$COMFYUI_PORT) and F5-TTS (:$TTS_PORT) on $TARGET"
for i in $(seq 1 40); do
    if _sh "curl -sf http://localhost:$COMFYUI_PORT/system_stats >/dev/null 2>&1 && curl -sf http://localhost:$TTS_PORT/health >/dev/null 2>&1"; then
        echo " ✓"
        break
    fi
    echo -n "."
    sleep 5
done

# ── 7. Verify the containers actually got a WORKING GPU ───────────────────────
# The HTTP health endpoints above return 200 even on CPU, so check the GPU
# directly — in two stages, because they fail differently:
#   • nvidia-smi failing ("NVML: Unknown Error") = the NVIDIA Container Toolkit
#     isn't granting the GPU at all. See docker/README.md ("GPU lost at runtime").
#   • nvidia-smi OK but CUDA init failing = NVML works without /dev/nvidia-uvm
#     but CUDA does not (torch reports "Found no NVIDIA driver"), or the host
#     driver is too old for the container's CUDA 13 stack.
# Either way the render/narration would run on CPU (very slow) or crash-loop.
CUDA_TEST='import ctypes,sys; sys.exit(ctypes.CDLL("libcuda.so.1").cuInit(0))'
echo ""
echo "[deploy] verifying GPU access inside the containers on $TARGET ..."
for svc in comfyui tts; do
    if ! _sh "docker exec spielbot-worker-${svc}-1 nvidia-smi -L 2>/dev/null | grep -q '^GPU'"; then
        echo "  ✗ WARNING: $svc has NO GPU access — it will run on CPU (slow)."
        echo "    Check the NVIDIA Container Toolkit on $TARGET, then: bash scripts/worker.sh restart $TARGET"
    elif ! _sh "docker exec spielbot-worker-${svc}-1 python3 -c '$CUDA_TEST' >/dev/null 2>&1"; then
        echo "  ✗ WARNING: $svc sees the GPU (nvidia-smi) but CUDA cannot initialize."
        echo "    Common causes on $TARGET:"
        echo "      • /dev/nvidia-uvm missing (sudo modprobe nvidia-uvm)"
        echo "      • the NVIDIA driver/modules were (re)loaded after the containers were"
        echo "        created — the containers hold stale device nodes; RECREATE them:"
        echo "        cd ~/spielbot-worker/docker && docker compose down && docker compose up -d"
        echo "      • host driver too old for CUDA 13 (nvidia-smi header must show >= 13.0)"
    else
        echo "  ✓ $svc on GPU (CUDA OK)"
    fi
done

echo ""
echo "✅ Containerized worker ready on $TARGET"
echo "   ComfyUI: http://${TARGET}:${COMFYUI_PORT}    F5-TTS: http://${TARGET}:${TTS_PORT}"
