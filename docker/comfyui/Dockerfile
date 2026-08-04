# syntax=docker/dockerfile:1
#
# ComfyUI render worker (issue #12). Vanilla ComfyUI + PyTorch — every node our
# workflows use (LTX 2.3, ACE-Step, FLUX) is native, so no custom nodes are
# installed. The ~49 GB of models are NOT baked in; they are mounted at runtime
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
# v0.30.0 is the first release with native MiniMax H3 nodes (nodes_minimax_h3).
ARG COMFYUI_REF=v0.30.0
# PyTorch wheel index matching the base CUDA. Leave empty if the base image
# already ships torch (e.g. an NGC PyTorch base).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130

# PIP_BREAK_SYSTEM_PACKAGES: Ubuntu 24.04 marks the system env "externally
# managed" (PEP 668); we install into it on purpose inside this container.
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1

# gcc + python3-dev: torch's Triton JIT compiles kernel launchers at runtime on
# some GPU archs (e.g. sm_120 Blackwell workstation) — without a C compiler,
# renders die with "Failed to find C compiler" at the first encode node.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git python3 python3-dev python3-pip gcc ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone https://github.com/comfyanonymous/ComfyUI.git ComfyUI
WORKDIR /opt/ComfyUI
# Fetch before checkout: the clone layer above is cache-stable, so a REF newer
# than the cached clone (e.g. a fresh release tag) would otherwise not exist yet.
RUN git fetch --tags --force origin && git checkout "${COMFYUI_REF}"

# torch first (ComfyUI's requirements.txt does not pin it), then ComfyUI deps.
# Versions float by default; pin for reproducible builds, e.g.:
#   --build-arg TORCH_SPEC="torch==<ver> torchvision==<ver> torchaudio==<ver>"
ARG TORCH_SPEC="torch torchvision torchaudio"
RUN if [ -n "${TORCH_INDEX_URL}" ]; then \
        pip install ${TORCH_SPEC} --index-url "${TORCH_INDEX_URL}"; \
    fi \
    && pip install -r requirements.txt

# Video Helper Suite: load/save MP4s for Remix upscale without a local CLI.
RUN git clone --depth=1 https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git \
        custom_nodes/ComfyUI-VideoHelperSuite \
    && pip install -r custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt

# Lightricks IC-LoRA nodes (LTXICLoRALoaderModelOnly, LTXAddVideoICLoRAGuide, …)
# for the LTX-2.3 Pixel Spatial Upscaler used by Remix "AI temporal" upscale.
# Newer kornia dropped `pad` from geometry.transform.pyramid; patch it so the
# whole ComfyUI-LTXVideo package (including IC-LoRA) can import.
RUN git clone --depth=1 https://github.com/Lightricks/ComfyUI-LTXVideo.git \
        custom_nodes/ComfyUI-LTXVideo \
    && if [ -f custom_nodes/ComfyUI-LTXVideo/requirements.txt ]; then \
         pip install -r custom_nodes/ComfyUI-LTXVideo/requirements.txt; \
       fi \
    && python3 <<'PY'
from pathlib import Path

p = Path("custom_nodes/ComfyUI-LTXVideo/pyramid_blending.py")
t = p.read_text()
old = """from kornia.geometry.transform.pyramid import (
    PyrUp,
    build_laplacian_pyramid,
    build_pyramid,
    find_next_powerof_two,
    is_powerof_two,
    pad,
)"""
new = """from kornia.geometry.transform.pyramid import (
    PyrUp,
    build_laplacian_pyramid,
    build_pyramid,
    find_next_powerof_two,
    is_powerof_two,
)
try:
    from kornia.geometry.transform.pyramid import pad
except ImportError:
    # kornia API drift — pad left the pyramid module; mirror its call shape.
    def pad(tensor, padding, border_type="reflect"):
        mode = {"reflect": "reflect", "constant": "constant",
                "replicate": "replicate"}.get(border_type, "reflect")
        return torch.nn.functional.pad(tensor, padding, mode=mode)
"""
if old in t:
    p.write_text(t.replace(old, new))
    print("patched pyramid_blending.py for kornia pad")
else:
    print("pyramid_blending.py already compatible or layout changed")

# Soft-fail Laplacian blend so IC-LoRA still loads if pyramid_blending breaks.
init = Path("custom_nodes/ComfyUI-LTXVideo/__init__.py")
it = init.read_text()
hard = "from .pyramid_blending import LTXVLaplacianPyramidBlend"
if hard in it and "except ImportError" not in it:
    it = it.replace(
        hard,
        "try:\n"
        "    from .pyramid_blending import LTXVLaplacianPyramidBlend\n"
        "except ImportError:  # optional; IC-LoRA nodes must still load\n"
        "    LTXVLaplacianPyramidBlend = None",
    )
    it = it.replace(
        '    "LTXVLaplacianPyramidBlend": LTXVLaplacianPyramidBlend,\n',
        '    **({"LTXVLaplacianPyramidBlend": LTXVLaplacianPyramidBlend}\n'
        "       if LTXVLaplacianPyramidBlend is not None else {}),\n",
    )
    init.write_text(it)
    print("softened pyramid_blending import in __init__.py")
PY

EXPOSE 8188

# Models are mounted at /opt/ComfyUI/models by compose (not baked — ~49 GB).
# The healthcheck verifies BOTH that the server is up AND the GPU is still
# reachable. nvidia-smi is probed fresh each time (not ComfyUI's startup-cached
# device), so a GPU revoked mid-run by a host daemon-reload ("NVML: Unknown
# Error", silent CPU fallback) flips the container "unhealthy" — which autoheal
# then restarts to reclaim the GPU (see docker/docker-compose.yml).
HEALTHCHECK --interval=30s --timeout=15s --start-period=120s --retries=5 \
    CMD curl -sf http://localhost:8188/system_stats >/dev/null 2>&1 \
        && nvidia-smi -L 2>/dev/null | grep -q '^GPU' || exit 1

CMD ["python3", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
