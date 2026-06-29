# syntax=docker/dockerfile:1
#
# ComfyUI render worker (issue #12). Vanilla ComfyUI + PyTorch — every node our
# workflows use (LTX 2.3, ACE-Step, FLUX) is native, so no custom nodes are
# installed. The ~33 GB of models are NOT baked in; they are mounted at runtime
# (see docker/docker-compose.yml). Listens on 0.0.0.0:8188, same as the
# SSH-installed worker.

# CUDA base image. The default targets DGX Spark (GB10 Blackwell, arm64, CUDA 13
# — verified). Override BASE_IMAGE / TORCH_INDEX_URL for other GPUs + arch:
#   older NVIDIA GPUs: BASE_IMAGE=nvidia/cuda:12.8.0-runtime-ubuntu24.04
#                      TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124
# The image is multi-arch (amd64 + arm64/sbsa).
ARG BASE_IMAGE=nvidia/cuda:13.0.1-runtime-ubuntu24.04
FROM ${BASE_IMAGE}

# Pin ComfyUI for reproducible workers: a tag, branch, or commit SHA.
ARG COMFYUI_REF=master
# PyTorch wheel index matching the base CUDA. Leave empty if the base image
# already ships torch (e.g. an NGC PyTorch base).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130

# PIP_BREAK_SYSTEM_PACKAGES: Ubuntu 24.04 marks the system env "externally
# managed" (PEP 668); we install into it on purpose inside this container.
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git python3 python3-pip ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone https://github.com/comfyanonymous/ComfyUI.git ComfyUI
WORKDIR /opt/ComfyUI
RUN git checkout "${COMFYUI_REF}"

# torch first (ComfyUI's requirements.txt does not pin it), then ComfyUI deps.
# Versions float by default; pin for reproducible builds, e.g.:
#   --build-arg TORCH_SPEC="torch==<ver> torchvision==<ver> torchaudio==<ver>"
ARG TORCH_SPEC="torch torchvision torchaudio"
RUN if [ -n "${TORCH_INDEX_URL}" ]; then \
        pip install ${TORCH_SPEC} --index-url "${TORCH_INDEX_URL}"; \
    fi \
    && pip install -r requirements.txt

EXPOSE 8188

# Models are mounted at /opt/ComfyUI/models by compose (not baked — ~33 GB).
# The healthcheck verifies BOTH that the server is up AND the GPU is still
# reachable. nvidia-smi is probed fresh each time (not ComfyUI's startup-cached
# device), so a GPU revoked mid-run by a host daemon-reload ("NVML: Unknown
# Error", silent CPU fallback) flips the container "unhealthy" — which autoheal
# then restarts to reclaim the GPU (see docker/docker-compose.yml).
HEALTHCHECK --interval=30s --timeout=15s --start-period=120s --retries=5 \
    CMD curl -sf http://localhost:8188/system_stats >/dev/null 2>&1 \
        && nvidia-smi -L 2>/dev/null | grep -q '^GPU' || exit 1

CMD ["python3", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
