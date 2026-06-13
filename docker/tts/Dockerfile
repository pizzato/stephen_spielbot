# syntax=docker/dockerfile:1
#
# F5-TTS narration worker (issue #12). Runs pipeline/tts_server.py — a small
# HTTP service wrapping the same F5-TTS CLI the SSH runner used. The controller
# reaches it over HTTP when a tts_workers entry is an http:// URL.
#
# The F5TTS_v1_Base weights (~1.3 GB) are fetched from HuggingFace into
# HF_HOME on first request; mount that as a volume so it persists across
# container recreation (see docker/docker-compose.yml).

ARG BASE_IMAGE=nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04
FROM ${BASE_IMAGE}

# PyTorch wheel index matching the base CUDA (see comfyui/Dockerfile for notes).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ffmpeg libsndfile1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --upgrade pip

# torch first (pinned to the GPU's CUDA), then f5-tts + the HTTP server deps.
# Installing torch up front keeps f5-tts from pulling a mismatched build.
RUN if [ -n "${TORCH_INDEX_URL}" ]; then \
        pip install torch torchaudio --index-url "${TORCH_INDEX_URL}"; \
    fi \
    && pip install f5-tts fastapi "uvicorn[standard]"

WORKDIR /app
# pipeline must be importable as a package for `uvicorn pipeline.tts_server:app`.
COPY pipeline/__init__.py /app/pipeline/__init__.py
COPY pipeline/tts_server.py /app/pipeline/tts_server.py
COPY assets/default_narrator.wav /app/assets/default_narrator.wav

EXPOSE 8189

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
    CMD curl -sf http://localhost:8189/health || exit 1

CMD ["uvicorn", "pipeline.tts_server:app", "--host", "0.0.0.0", "--port", "8189"]
