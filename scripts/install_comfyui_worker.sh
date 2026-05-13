#!/usr/bin/env bash
# Install and configure a ComfyUI worker on a remote DGX Spark.
# Usage: bash scripts/install_comfyui_worker.sh <hostname>
# Example: bash scripts/install_comfyui_worker.sh s1
#
# What it does:
#   1. Installs Miniconda (if absent)
#   2. Clones ComfyUI and creates the comfyui-env venv (via miniconda Python 3.13)
#   3. Rsyncs all LTX + ACE-Step models from s3 (already fully set up)
#   4. Deploys the video-generator repo (workflows + pipeline)
#   5. Starts ComfyUI listening on 0.0.0.0:8188
#   6. Verifies the worker is responding

set -euo pipefail

TARGET="${1:-}"
MODEL_SOURCE="s3"   # machine that already has all models
COMFYUI_PORT=8188

if [[ -z "$TARGET" ]]; then
    echo "Usage: $0 <hostname>"
    echo "  Example: $0 s1"
    exit 1
fi

echo "=== Installing ComfyUI worker on $TARGET ==="

# ── 1. Install Miniconda if not present ──────────────────────────────────────
ssh "$TARGET" bash <<'REMOTE'
set -euo pipefail
if [[ -f "$HOME/miniconda3/bin/python3" ]]; then
    echo "[miniconda] already installed: $($HOME/miniconda3/bin/python3 --version)"
    exit 0
fi
echo "[miniconda] installing Miniconda..."
ARCH=$(uname -m)
URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-${ARCH}.sh"
curl -fsSL "$URL" -o /tmp/miniconda.sh
bash /tmp/miniconda.sh -b -p "$HOME/miniconda3"
rm /tmp/miniconda.sh
echo "[miniconda] installed: $($HOME/miniconda3/bin/python3 --version)"
REMOTE

# ── 2. Clone ComfyUI and create venv ─────────────────────────────────────────
ssh "$TARGET" bash <<'REMOTE'
set -euo pipefail

PYTHON="$HOME/miniconda3/bin/python3"
VENV="$HOME/github/comfyui-env"

mkdir -p "$HOME/github"

if [[ -d "$HOME/github/ComfyUI/.git" ]]; then
    echo "[comfyui] already cloned, pulling latest..."
    git -C "$HOME/github/ComfyUI" pull --ff-only
else
    echo "[comfyui] cloning..."
    git clone https://github.com/comfyanonymous/ComfyUI "$HOME/github/ComfyUI"
fi

if [[ -f "$VENV/bin/activate" ]]; then
    echo "[venv] comfyui-env already exists"
else
    echo "[venv] creating comfyui-env with $($PYTHON --version)..."
    "$PYTHON" -m venv "$VENV"
fi

source "$VENV/bin/activate"

echo "[pip] installing ComfyUI dependencies..."
cd "$HOME/github/ComfyUI"
pip install --quiet -r requirements.txt

if ! python -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
    echo "[pip] installing PyTorch..."
    pip install --quiet torch torchvision torchaudio
fi

echo "[venv] OK — $(python --version), torch $(python -c 'import torch; print(torch.__version__)')"
REMOTE

# ── 3. Rsync models from reference machine (s3) ──────────────────────────────
echo "=== Syncing models from $MODEL_SOURCE to $TARGET (this may take a while for large files) ==="

MODEL_DIRS=(
    "models/checkpoints"
    "models/diffusion_models"
    "models/loras"
    "models/latent_upscale_models"
    "models/text_encoders"
    "models/upscale_models"
    "models/vae"
)

for dir in "${MODEL_DIRS[@]}"; do
    echo "[rsync] $dir..."
    ssh -A "$TARGET" "mkdir -p \$HOME/github/ComfyUI/$dir && \
        rsync -avz --ignore-existing \
            ${MODEL_SOURCE}:\$HOME/github/ComfyUI/$dir/ \
            \$HOME/github/ComfyUI/$dir/ 2>&1 | tail -5" \
    || echo "[rsync] Warning: some files in $dir may have failed"
done

# ── 4. Deploy video-generator workflows ──────────────────────────────────────
echo "=== Deploying video-generator to $TARGET ==="

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Create directory structure
ssh "$TARGET" "mkdir -p \$HOME/github/video-generator/{workflows,pipeline,assets}"

# Sync workflows and pipeline
rsync -avz --delete \
    "$REPO_ROOT/workflows/" \
    "${TARGET}:~/github/video-generator/workflows/"

rsync -avz --delete \
    "$REPO_ROOT/pipeline/" \
    "${TARGET}:~/github/video-generator/pipeline/"

# Sync assets (default narrator wav for F5-TTS)
if [[ -d "$REPO_ROOT/assets" ]]; then
    rsync -avz \
        "$REPO_ROOT/assets/" \
        "${TARGET}:~/github/video-generator/assets/"
fi

# ── 5. Write ComfyUI start script ─────────────────────────────────────────────
ssh "$TARGET" bash <<'REMOTE'
set -euo pipefail
cat > $HOME/github/ComfyUI/start_worker.sh <<'SCRIPT'
#!/usr/bin/env bash
# Start ComfyUI worker, listening on all interfaces
source "$HOME/github/comfyui-env/bin/activate"
cd "$HOME/github/ComfyUI"
nohup python main.py --listen 0.0.0.0 --port 8188 \
    > "$HOME/github/ComfyUI/comfyui.log" 2>&1 &
echo "ComfyUI started (PID $!), log: $HOME/github/ComfyUI/comfyui.log"
SCRIPT
chmod +x $HOME/github/ComfyUI/start_worker.sh

# Write a systemd user service for auto-start
mkdir -p $HOME/.config/systemd/user
cat > $HOME/.config/systemd/user/comfyui-worker.service <<UNIT
[Unit]
Description=ComfyUI Worker
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/github/ComfyUI
ExecStart=/bin/bash -c 'source %h/github/comfyui-env/bin/activate && python %h/github/ComfyUI/main.py --listen 0.0.0.0 --port 8188'
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable comfyui-worker.service 2>/dev/null || true
echo "[service] comfyui-worker.service registered"
REMOTE

# ── 6. Start ComfyUI ─────────────────────────────────────────────────────────
echo "=== Starting ComfyUI on $TARGET ==="
ssh "$TARGET" bash <<'REMOTE'
# Kill any existing instance
pkill -f "python.*main.py.*8188" 2>/dev/null || true
sleep 2
bash "$HOME/github/ComfyUI/start_worker.sh"
REMOTE

# Wait for it to come up
echo -n "Waiting for ComfyUI to respond"
for i in $(seq 1 30); do
    if ssh "$TARGET" "curl -sf http://localhost:8188/system_stats" &>/dev/null; then
        echo " OK"
        break
    fi
    echo -n "."
    sleep 3
done

# ── 7. Verify ────────────────────────────────────────────────────────────────
echo "=== Verifying worker ==="
ssh "$TARGET" bash <<'REMOTE'
set -euo pipefail
STATS=$(curl -sf http://localhost:8188/system_stats)
echo "ComfyUI running: $(echo $STATS | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["system"]["python_version"][:20])')"
NODE_COUNT=$(curl -sf http://localhost:8188/object_info | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d))')
echo "Nodes available: $NODE_COUNT"
LTX_COUNT=$(curl -sf http://localhost:8188/object_info | python3 -c \
    'import sys,json; d=json.load(sys.stdin); print(len([k for k in d if "LTX" in k or "ltx" in k.lower()]))')
echo "LTX nodes: $LTX_COUNT"
REMOTE

echo ""
echo "✅ Worker $TARGET is ready at http://${TARGET}:8188"
echo ""
echo "Add it to the video-generator Config tab:"
echo "  http://${TARGET}:${COMFYUI_PORT}"
