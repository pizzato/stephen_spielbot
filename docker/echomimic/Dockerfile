# syntax=docker/dockerfile:1
#
# EchoMimic-V3 talking-head worker. Runs pipeline/echomimic_server.py, which wraps
# antgroup/echomimic_v3's infer_flash.py (reference portrait + speech WAV ->
# lip-synced talking-head MP4). The controller reaches it over HTTP when an
# echomimic_workers entry is an http:// URL. Renders dialogue/performance scenes;
# the narration path never touches it.
#
# Weights (~27 GB: Wan2.1-Fun-1.3B base + wav2vec + the EchoMimic flash transformer)
# are fetched from HuggingFace into /weights on first use and persisted via a volume
# (see docker-compose.yml), so they survive container recreation and don't bloat the
# image. Validated on GB10/DGX-Spark (arm64, CUDA 13) — see docs/dialogue_performance_plan.md.

ARG BASE_IMAGE=nvidia/cuda:13.0.1-runtime-ubuntu24.04
FROM ${BASE_IMAGE}

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130
ARG ECHOMIMIC_REF=main

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface \
    ECHOMIMIC_REPO=/app/echomimic_v3 \
    ECHOMIMIC_WEIGHTS=/weights \
    ECHOMIMIC_STUBS=/app/stubs

# python3-dev + build-essential: mmgp/optimum-quanto and audio libs build small
# CUDA/C extensions at import; ffmpeg + libsndfile1 for audio/video I/O; git to
# clone the model repo.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-pip build-essential \
        ffmpeg libsndfile1 git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# torch first (matched to the GPU's CUDA), then the HTTP-server deps. Floating
# versions, kept working on GB10/CUDA-13 (mirrors the comfyui/tts Dockerfiles).
ARG TORCH_SPEC="torch torchvision torchaudio"
RUN if [ -n "${TORCH_INDEX_URL}" ]; then \
        pip install ${TORCH_SPEC} --index-url "${TORCH_INDEX_URL}"; \
    fi \
    && pip install fastapi "uvicorn[standard]" "huggingface_hub[cli]"

WORKDIR /app
RUN git clone --depth 1 --branch ${ECHOMIMIC_REF} https://github.com/antgroup/echomimic_v3 /app/echomimic_v3

# EchoMimic deps minus the arm64-hostile / unused ones:
#  - tensorflow / retina-face: only for face-mask autodetection, which the flash
#    image+audio path doesn't use; tensorflow has no arm64/py3.12 wheel.
#  - decord: only for video-file input (no arm64 wheel) — stubbed below.
# pyloudnorm is imported by infer_flash.py but missing from requirements.txt.
RUN grep -vE '^(tensorflow|retina-face|decord)' /app/echomimic_v3/requirements.txt > /tmp/req.txt \
    && pip install -r /tmp/req.txt pyloudnorm

# decord stub — infer_flash.py imports it but never reads a video on the image path.
RUN mkdir -p /app/stubs/decord && printf '%s\n' \
    'class VideoReader:' \
    '    def __init__(self, *a, **k): raise RuntimeError("decord stub: video-file input unsupported")' \
    '    def __len__(self): return 0' \
    'def cpu(*a, **k): return None' \
    'def gpu(*a, **k): return None' \
    'def bridge(*a, **k): return None' \
    > /app/stubs/decord/__init__.py

# pipeline must be importable as a package for `uvicorn pipeline.echomimic_server:app`.
COPY pipeline/__init__.py /app/pipeline/__init__.py
COPY pipeline/echomimic_server.py /app/pipeline/echomimic_server.py

EXPOSE 8190

# Server up AND GPU still reachable (probed fresh via nvidia-smi) — same GPU-loss
# self-heal contract as the comfyui/tts workers (autoheal restarts if unhealthy).
HEALTHCHECK --interval=30s --timeout=15s --start-period=120s --retries=5 \
    CMD curl -sf http://localhost:8190/health >/dev/null 2>&1 \
        && nvidia-smi -L 2>/dev/null | grep -q '^GPU' || exit 1

CMD ["uvicorn", "pipeline.echomimic_server:app", "--host", "0.0.0.0", "--port", "8190"]
