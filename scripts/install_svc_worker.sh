#!/usr/bin/env bash
# Install seed-vc INSIDE the workers' ComfyUI containers, so the "Sing this as
# [voice]" diffusion runs on CUDA instead of the controller's Apple GPU
# (~12x real time on the Mac; near real time on a GB10).
#
# The venv reuses each container's own CUDA torch (--system-site-packages);
# only seed-vc's pure-python deps are added. Container-local — lost on an
# image rebuild — but docker/comfyui/Dockerfile carries the same install, so
# this script is only for containers built before that landed. Any worker can
# take a re-voicing, so install it on all of them: with no host given, every
# worker in config.yaml (comfy_workers) gets it.
#
# Usage: bash scripts/install_svc_worker.sh          # whole fleet
#        bash scripts/install_svc_worker.sh s2       # one host
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/_config.sh
source "$REPO_ROOT/scripts/_config.sh"

CONTAINER="${SVC_CONTAINER:-spielbot-worker-comfyui-1}"
HOSTS=("$@")
if [ ${#HOSTS[@]} -eq 0 ]; then
    # shellcheck disable=SC2207
    HOSTS=($(remote_hosts))
    [ ${#HOSTS[@]} -gt 0 ] || {
        echo "ERROR: no comfy_workers in $CONFIG_YAML — pass a host explicitly"
        exit 1
    }
fi

for HOST in "${HOSTS[@]}"; do
    echo "=== $HOST ==="
    if is_local_host "$HOST"; then
        DOCKER=(docker)
    else
        DOCKER=(ssh -- "$HOST" docker)
    fi
    "${DOCKER[@]}" exec -i -u root "$CONTAINER" bash -s <<'INSIDE'
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
print("seed-vc ready on", torch.cuda.get_device_name(0))
PYEOF
INSIDE
    echo "done: seed-vc installed in $CONTAINER on $HOST"
done
echo "(model weights download from Hugging Face on each worker's first conversion)"
