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
# PyTorch wheel index matching the base CUDA. Leave empty if the base image
# already ships torch (e.g. an NGC PyTorch base).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130
# torch spec shared by the SageAttention builder and the runtime stage — the
# compiled extension links against torch's C++ ABI, so both must resolve to the
# same wheel. Versions float by default; pin for reproducible builds, e.g.:
#   --build-arg TORCH_SPEC="torch==<ver> torchvision==<ver> torchaudio==<ver>"
ARG TORCH_SPEC="torch torchvision torchaudio"

# ── SageAttention builder ────────────────────────────────────────────────────
# SageAttention ships no aarch64/CUDA-13 wheels and PyPI's sdist is not built
# for sm_121 (GB10), so it is compiled here from source. This needs nvcc, which
# the -runtime base image does not carry — hence a -devel builder stage that
# emits a wheel, keeping the ~5 GB CUDA toolkit out of the shipped worker.
ARG BUILDER_IMAGE=nvidia/cuda:13.0.1-devel-ubuntu24.04
FROM ${BUILDER_IMAGE} AS sage-builder
ARG TORCH_INDEX_URL
ARG TORCH_SPEC
# Compute capabilities to emit. 12.1 = GB10 (DGX Spark), 12.0 = sm_120 Blackwell
# workstation cards. Set empty to skip the build entirely (non-Blackwell GPUs,
# where SageAttention's Blackwell kernels do not apply).
ARG SAGEATTENTION_ARCHS="12.0;12.1"
# Pin for reproducible workers: a tag, branch, or commit SHA.
ARG SAGEATTENTION_REF=main
# --no-build-isolation below is load-bearing: SageAttention's setup.py imports
# torch to discover the CUDA extension toolchain, and PEP 517's isolated build
# env has no torch in it. Without the flag the build dies with a bare
# "ModuleNotFoundError: No module named 'torch'" that reads like a CUDA-13
# incompatibility and isn't one.
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN mkdir -p /wheels
RUN if [ -n "${SAGEATTENTION_ARCHS}" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            git python3 python3-dev python3-pip \
        && rm -rf /var/lib/apt/lists/* \
        && if [ -n "${TORCH_INDEX_URL}" ]; then \
               pip install ${TORCH_SPEC} --index-url "${TORCH_INDEX_URL}"; \
           fi \
        && pip install setuptools wheel packaging ninja \
        && git clone https://github.com/thu-ml/SageAttention.git /src/SageAttention \
        && git -C /src/SageAttention checkout "${SAGEATTENTION_REF}" \
        && cd /src/SageAttention \
        && TORCH_CUDA_ARCH_LIST="${SAGEATTENTION_ARCHS}" \
           EXT_PARALLEL=4 NVCC_APPEND_FLAGS="--threads 8" MAX_JOBS=8 \
           pip wheel . --no-deps --no-build-isolation --wheel-dir /wheels; \
    fi

FROM ${BASE_IMAGE}

# Pin ComfyUI for reproducible workers: a tag, branch, or commit SHA.
# v0.30.0 is the first release with native MiniMax H3 nodes (nodes_minimax_h3).
ARG COMFYUI_REF=v0.31.1
ARG TORCH_INDEX_URL
ARG TORCH_SPEC

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
RUN if [ -n "${TORCH_INDEX_URL}" ]; then \
        pip install ${TORCH_SPEC} --index-url "${TORCH_INDEX_URL}"; \
    fi \
    && pip install -r requirements.txt

# SageAttention, compiled for this image's arch list in the builder stage above.
# Installed AFTER torch and ComfyUI's requirements on purpose: `pip install
# sageattention` from PyPI silently shadows a locally built extension, so the
# wheel must land last and nothing may pull the package name again afterwards.
# Enabled per-worker at launch, not here — see COMFYUI_EXTRA_ARGS in
# docker/docker-compose.yml.
COPY --from=sage-builder /wheels /tmp/sage-wheels
RUN if ls /tmp/sage-wheels/*.whl >/dev/null 2>&1; then \
        pip install /tmp/sage-wheels/*.whl \
        && python3 -c "from sageattention import sageattn; print('sageattention OK:', sageattn.__module__)"; \
    else \
        echo "SageAttention not built (SAGEATTENTION_ARCHS empty) — skipping"; \
    fi \
    && rm -rf /tmp/sage-wheels

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

# MiniMax-H3 Turbo nodes (MiniMaxH3TurboLoRA + the dual-schedule few-step
# sampler) for the opt-in minimax-h3-turbo engine. Pure-Python, no extra deps.
# PINNED: this repo adds required node inputs without warning — 55fee86 made
# `low_vram` mandatory on MiniMaxH3TurboLoRA, and an unpinned clone silently
# broke every turbo render on rebuilt workers while old ones kept going
# ("Required input is missing: low_vram"). Bump this deliberately, checking
# workflows/h3_turbo_i2v.json still satisfies the node's inputs.
ARG H3_TURBO_NODES_REF=55fee864dd7b2976b1c4ce3c3d5f7968f181409f
RUN git clone https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo.git \
        custom_nodes/ComfyUI-MiniMax-H3-Turbo \
    && git -C custom_nodes/ComfyUI-MiniMax-H3-Turbo checkout "${H3_TURBO_NODES_REF}"

# H3 Motion Context — EXPERIMENTAL, off by default. Chains H3 clips so clip B
# continues clip A's motion and audio, which is the only way past the model's
# 15 s ceiling (pipeline/comfyui.py: H3_MAX_SECONDS) without a hard cut.
#
# It earns its default-off setting: it monkey-patches ComfyUI's H3 internals
# (comfy.ldm.minimax.model.PackedLayout.__init__ and extra_conds) to lift a
# restriction on interior keyframe anchors. The patch self-tests against the
# installed ComfyUI first and refuses if anything moved, and it stands down if
# another pack already patched the same spot — but a ComfyUI bump can still
# silently drop the feature, so a rebuild wants a chained render to prove it.
# GPL-3.0, unlike everything else in this image; keep that in mind before
# redistributing the built image.
ARG H3_MOTION_CONTEXT_REF=
RUN if [ -n "${H3_MOTION_CONTEXT_REF}" ]; then \
        git clone https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context.git \
            custom_nodes/ComfyUI-H3-Motion-Context \
        && git -C custom_nodes/ComfyUI-H3-Motion-Context checkout "${H3_MOTION_CONTEXT_REF}"; \
    else \
        echo "H3 Motion Context not installed (H3_MOTION_CONTEXT_REF empty)"; \
    fi

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
