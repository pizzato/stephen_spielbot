#!/usr/bin/env bash
# InfiniteTalk spike — one-shot setup on a single worker. Throwaway; see README.md.
# Clones the repo, builds an isolated env, downloads weights (~40 GB) into the repo's weights/.
# Deliberately NOT idempotent-clever: it's a spike. Re-run steps by hand if one fails.
set -euo pipefail

SPIKE_ROOT="${SPIKE_ROOT:-$HOME/infinitetalk_spike}"
REPO_DIR="${REPO_DIR:-$SPIKE_ROOT/InfiniteTalk}"
ENV_NAME="${ENV_NAME:-infinitetalk_spike}"
ARCH="$(uname -m)"

echo "== InfiniteTalk spike setup =="
echo "   spike root : $SPIKE_ROOT"
echo "   repo dir   : $REPO_DIR"
echo "   arch       : $ARCH"
echo

mkdir -p "$SPIKE_ROOT"

# --- 1. clone ---------------------------------------------------------------
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone https://github.com/MeiGen-AI/InfiniteTalk "$REPO_DIR"
else
  echo "repo already present, skipping clone"
fi
cd "$REPO_DIR"

# --- 2. python env ----------------------------------------------------------
# Prefer conda (matches upstream); fall back to venv.
if command -v conda >/dev/null 2>&1; then
  eval "$(conda shell.bash hook)"
  conda create -y -n "$ENV_NAME" python=3.10 || echo "env may already exist"
  conda activate "$ENV_NAME"
  PYBIN="$(command -v python)"
else
  echo "conda not found — using python -m venv"
  python3 -m venv "$SPIKE_ROOT/venv"
  # shellcheck disable=SC1091
  source "$SPIKE_ROOT/venv/bin/activate"
  PYBIN="$(command -v python)"
fi
echo "python: $PYBIN"

# --- 3. torch + attention (THE arch-dependent decision point) ---------------
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
  cat <<'WARN'

  ┌────────────────────────────────────────────────────────────────────────┐
  │  aarch64 (GB10 / DGX Spark) detected.                                    │
  │                                                                          │
  │  The upstream pins are x86_64 / CUDA-12 wheels and will NOT install:     │
  │      torch==2.4.1 (cu121)  xformers==0.0.28 (cu121)  flash_attn==2.7.4   │
  │                                                                          │
  │  This script will NOT install those on arm64 (it would just error out    │
  │  or, worse, pull a CPU build). Do ONE of these, then re-run with         │
  │  SKIP_TORCH=1 to continue to requirements + weights:                     │
  │                                                                          │
  │   A) Install an arm64 / CUDA-13 PyTorch that matches this box, e.g. the  │
  │      NVIDIA sbsa build already used by your ComfyUI container, then try  │
  │      the model WITHOUT flash-attn (Wan/InfiniteTalk fall back to torch   │
  │      SDPA attention — slower, but proves viability). flash-attn on       │
  │      arm64+CUDA13 usually needs a from-source build; skip it first.      │
  │                                                                          │
  │   B) If the bare install fights you, STOP and use the ComfyUI-           │
  │      WanVideoWrapper route in README.md instead — your ComfyUI container │
  │      already has a working arm64 torch stack.                            │
  │                                                                          │
  │  Whether A even works is itself a key result of this spike — write it    │
  │  down either way.                                                        │
  └────────────────────────────────────────────────────────────────────────┘

WARN
  if [ "${SKIP_TORCH:-0}" != "1" ]; then
    echo "Exiting before torch install. Set up arm64 torch, then: SKIP_TORCH=1 bash setup.sh"
    exit 2
  fi
  echo "SKIP_TORCH=1 — assuming you installed an arm64/CUDA13 torch yourself. Continuing."
else
  echo "x86_64 — installing upstream-pinned torch/xformers/flash-attn (CUDA 12.1 wheels)"
  pip install torch==2.4.1 torchvision==0.19.1 torchaudio==2.4.1 --index-url https://download.pytorch.org/whl/cu121
  pip install -U xformers==0.0.28 --index-url https://download.pytorch.org/whl/cu121
  pip install flash_attn==2.7.4.post1 || echo "flash_attn build failed — try running the model without it (SDPA)"
fi

# --- 4. repo requirements + audio deps --------------------------------------
pip install -r requirements.txt
if command -v conda >/dev/null 2>&1; then
  conda install -y -c conda-forge librosa ffmpeg || pip install librosa
else
  pip install librosa
fi
pip install "huggingface_hub[cli]"

# --- 5. weights (~40 GB) ----------------------------------------------------
mkdir -p weights
echo "== downloading weights into $REPO_DIR/weights (this is the big one) =="
huggingface-cli download Wan-AI/Wan2.1-I2V-14B-480P     --local-dir ./weights/Wan2.1-I2V-14B-480P
huggingface-cli download TencentGameMate/chinese-wav2vec2-base --local-dir ./weights/chinese-wav2vec2-base
huggingface-cli download MeiGen-AI/InfiniteTalk         --local-dir ./weights/InfiniteTalk

echo
echo "== setup done =="
echo "repo : $REPO_DIR"
echo "next : python3 make_test_audio.py --seconds 60 --out test_60s.wav"
echo "then : bash run_spike.sh --image /path/to/portrait.png --audio test_60s.wav"
