# Containerized workers

Run the GPU workers (ComfyUI + F5-TTS) as containers so a new machine joins the
render fleet with one command instead of the SSH + Miniconda + rsync bootstrap
(issue #12).

Each worker machine runs the same two-service stack, sharing that machine's
GPU(s). The controller (the web app, on your Mac) is unchanged — it just points
at each machine over HTTP.

| Service | Port | What it is |
|---|---|---|
| `comfyui` | 8188 | Vanilla ComfyUI + PyTorch (LTX 2.3 / ACE-Step / FLUX — all native nodes) |
| `tts` | 8189 | F5-TTS behind a small HTTP server (`pipeline/tts_server.py`) |

Models (~33 GB) are **not** baked into the image — they live on the host and are
mounted in, so images stay small and rebuild fast.

## Prerequisites (per worker machine)

- Docker Engine + Docker Compose v2
- NVIDIA driver + the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  (so containers can use the GPU). Verify with:
  ```bash
  docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi
  ```

## Deploy a worker machine

```bash
# On the worker machine:
git clone https://github.com/pizzato/stephen_spielbot
cd stephen_spielbot/docker
cp .env.example .env
#   edit .env → set MODELS_DIR (and, if needed, BASE_IMAGE / TORCH_INDEX_URL)

# One-time: download the ~33 GB of models into MODELS_DIR
bash ../scripts/worker_container.sh fetch-models
#   (or copy/rsync an existing ComfyUI models/ folder into MODELS_DIR)

# Build + start both workers
bash ../scripts/worker_container.sh up
bash ../scripts/worker_container.sh status
```

`make worker-up` / `worker-down` / `worker-status` / `worker-logs` / `worker-build`
from the repo root do the same thing.

## Point the controller at it

On the controller, add the machine to `~/.config/video-generator/config.yaml`
(or the Settings screen). The key change from the SSH setup: **TTS workers are
now `http://` URLs**, which routes narration over HTTP instead of SSH.

```yaml
comfy_workers:
  - http://s1:8188
  - http://s2:8188
ui_workers:               # cover-image regen reuses ComfyUI endpoints
  - http://s1:8188
tts_workers:
  - http://s1:8189        # http:// → containerized F5-TTS over HTTP
  - http://s2:8189
```

Bare hostnames in `tts_workers` (e.g. `s1`) still use the legacy SSH path, so
containerized and SSH-installed TTS hosts can coexist during a migration.

## Configuration knobs (`docker/.env`)

| Var | Default | Notes |
|---|---|---|
| `MODELS_DIR` | — (required) | Host path to the ComfyUI `models/` dir, mounted into the ComfyUI container |
| `COMFYUI_REF` | `master` | Pin ComfyUI to a tag/branch/commit for reproducible workers |
| `BASE_IMAGE` | `nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04` | Multi-arch (amd64 + arm64/sbsa); builds on DGX Spark |
| `TORCH_INDEX_URL` | `…/whl/cu128` | Match your GPU's CUDA — Blackwell/DGX Spark: cu128 or cu130; older: cu124 |
| `COMFYUI_PORT` / `TTS_PORT` | `8188` / `8189` | Host ports; match them in the controller config |

### GPU / arch notes

The default base image is multi-arch, so the same compose file builds on x86 and
on DGX Spark (Blackwell, arm64). The one thing that varies by GPU is the PyTorch
build: set `TORCH_INDEX_URL` to the wheel index for your CUDA version. If a CUDA
base-image tag is missing for your architecture, pick another from
[hub.docker.com/r/nvidia/cuda](https://hub.docker.com/r/nvidia/cuda/tags).

## Build once, run everywhere (optional)

To avoid rebuilding on every machine, build once and push to a registry, then
pull on each worker:

```bash
cd docker
docker compose build
docker compose push        # after setting `image:` to your registry path
# on each worker: docker compose pull && docker compose up -d
```

## Relationship to the SSH installer

The SSH-based installer (`scripts/install_comfyui_worker.sh`,
`scripts/install_f5tts_worker.sh`) still works and is untouched — containers are
an alternative deployment path, not a replacement. A fleet can mix both while you
migrate.
