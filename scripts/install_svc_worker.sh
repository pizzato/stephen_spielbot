#!/usr/bin/env bash
# Install seed-vc INSIDE a worker's ComfyUI container, so the "Sing this as
# [voice]" diffusion runs on CUDA instead of the controller's Apple GPU
# (~12x real time on the Mac; near real time on a GB10).
#
# The venv reuses the container's own CUDA torch (--system-site-packages);
# only seed-vc's pure-python deps are added. Container-local — lost on an
# image rebuild — but docker/comfyui/Dockerfile carries the same install for
# rebuilt images. Point the controller at it with `svc_worker: <host>` in
# ~/.config/video-generator/config.yaml (and `svc_diffusion_steps` to taste:
# 25 fast, 30 default, 50 polish).
#
# Usage: bash scripts/install_svc_worker.sh s2
set -euo pipefail
HOST="${1:?usage: install_svc_worker.sh <host>}"
CONTAINER="${SVC_CONTAINER:-spielbot-worker-comfyui-1}"

ssh "$HOST" docker exec -i -u root "$CONTAINER" bash -s <<'INSIDE'
set -euo pipefail
if [ ! -d /opt/seed-vc ]; then
    git clone --depth 1 https://github.com/Plachtaa/seed-vc.git /opt/seed-vc
fi
cd /opt/seed-vc
# Check for PIP, not python: a failed ensurepip leaves a venv with the python
# symlink in place and nothing else.
if [ ! -x .venv/bin/pip ]; then
    rm -rf .venv
    # The runtime image ships without ensurepip (Debian splits it out).
    python3 -m venv --system-site-packages .venv 2>/dev/null || {
        rm -rf .venv
        apt-get update -qq
        apt-get install -y -qq python3-venv
        python3 -m venv --system-site-packages .venv
    }
fi
.venv/bin/pip install -q --upgrade pip
# The container's torch is the CUDA build we want — filter seed-vc's own
# torch pins (and their index) out of the requirement set.
grep -vE "^torch|^--extra-index-url|^torchvision|^torchaudio" requirements.txt \
    > /tmp/svc-reqs.txt
.venv/bin/pip install -q -r /tmp/svc-reqs.txt
.venv/bin/pip install -q torchcodec
.venv/bin/python - <<'PYEOF'
import torch
assert torch.cuda.is_available(), "no CUDA visible in the container"
print("seed-vc worker ready on", torch.cuda.get_device_name(0))
PYEOF
INSIDE
echo "done: seed-vc installed in $CONTAINER on $HOST"
echo "(model weights download from Hugging Face on the first conversion)"
