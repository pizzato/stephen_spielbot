# syntax=docker/dockerfile:1
#
# F5-TTS narration worker (issue #12). Runs pipeline/tts_server.py — a small
# HTTP service wrapping the same F5-TTS CLI the SSH runner used. The controller
# reaches it over HTTP when a tts_workers entry is an http:// URL.
#
# The OpenF5-TTS-Base weights (Apache-2.0, commercial-safe — see NOTICE.md) are
# fetched from HuggingFace into HF_HOME on first request; mount that as a volume
# so it persists across container recreation (see docker/docker-compose.yml).

ARG BASE_IMAGE=nvidia/cuda:13.0.1-runtime-ubuntu24.04
FROM ${BASE_IMAGE}

# PyTorch wheel index matching the base CUDA (see comfyui/Dockerfile for notes).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130

# PIP_BREAK_SYSTEM_PACKAGES: install into Ubuntu 24.04's "externally managed"
# system env on purpose inside this container (PEP 668).
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface

# python3-dev provides libpython3.12.so.1.0, which torchcodec (pulled in by
# f5-tts for audio decode) dlopen's at import — the plain python3 package ships
# only the statically-linked interpreter, so without this F5-TTS fails at
# inference with "libpython3.12.so.1.0: cannot open shared object file".
# ffmpeg supplies the libav* libs torchcodec links against (FFmpeg 6 on 24.04).
# gcc: torch's Triton JIT compiles kernel launchers at runtime on some GPU
# archs (e.g. sm_120 Blackwell workstation) and needs a C compiler.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-pip gcc ffmpeg libsndfile1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# torch first (matched to the GPU's CUDA), then f5-tts + the HTTP server deps.
# Installing torch up front keeps f5-tts from pulling a mismatched build.
# Versions float by default (kept working on GB10/CUDA-13). For reproducible
# builds, pin them at build time, e.g.:
#   --build-arg TORCH_SPEC="torch==<ver> torchaudio==<ver>" \
#   --build-arg F5TTS_SPEC="f5-tts==<ver>"
ARG TORCH_SPEC="torch torchaudio"
ARG F5TTS_SPEC="f5-tts"
RUN if [ -n "${TORCH_INDEX_URL}" ]; then \
        pip install ${TORCH_SPEC} --index-url "${TORCH_INDEX_URL}"; \
    fi \
    && pip install ${F5TTS_SPEC} fastapi "uvicorn[standard]"

# Chatterbox Multilingual (issue #176) — the multilingual narration engine
# (pipeline/chatterbox.py). Installed --no-deps because the package pins
# torch==2.6.0 (no cu130/arm64 wheel — it would clobber the CUDA torch above)
# plus gradio and other demo-only deps. The runtime deps f5-tts doesn't already
# provide are listed explicitly instead (floating, like everything else here).
ARG CHATTERBOX_SPEC="chatterbox-tts"
RUN pip install --no-deps ${CHATTERBOX_SPEC} \
    && pip install s3tokenizer resemble-perth conformer diffusers omegaconf \
        pykakasi spacy-pkuseg pyloudnorm

WORKDIR /app
# pipeline must be importable as a package for `uvicorn pipeline.tts_server:app`.
COPY pipeline/__init__.py /app/pipeline/__init__.py
COPY pipeline/openf5.py /app/pipeline/openf5.py
COPY pipeline/chatterbox.py /app/pipeline/chatterbox.py
COPY pipeline/tts_engines.py /app/pipeline/tts_engines.py
COPY pipeline/tts_server.py /app/pipeline/tts_server.py
COPY assets/default_narrator.mp3 /app/assets/default_narrator.mp3

EXPOSE 8189

# Verifies the server is up AND the GPU is still reachable (probed fresh via
# nvidia-smi). A GPU revoked mid-run by a host daemon-reload ("NVML: Unknown
# Error") makes F5-TTS silently fall back to CPU; this flips the container
# "unhealthy" so autoheal restarts it to reclaim the GPU (see docker-compose.yml).
HEALTHCHECK --interval=30s --timeout=15s --start-period=60s --retries=5 \
    CMD curl -sf http://localhost:8189/health >/dev/null 2>&1 \
        && nvidia-smi -L 2>/dev/null | grep -q '^GPU' || exit 1

CMD ["uvicorn", "pipeline.tts_server:app", "--host", "0.0.0.0", "--port", "8189"]
