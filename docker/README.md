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
  docker run --rm --gpus all nvidia/cuda:13.0.1-base-ubuntu24.04 nvidia-smi
  ```

## Deploy from the controller (default — `make install`)

`make install` deploys containers to every host in `comfy_workers`, over SSH:

```bash
make install WORKERS="s1 s2 s3"               # seeds config + container deploy
DEPLOY=ssh make install WORKERS="s1 s2 s3"    # legacy Miniconda/venv install instead
```

Per host it: preflights Docker + the NVIDIA toolkit; rsyncs the build context
(no GitHub access needed on the worker — the repo is private); writes
`docker/.env` with `MODELS_DIR=~/github/ComfyUI/models` (the host's existing
models — **not** re-downloaded); **stops the native ComfyUI** so the container
can take `:8188` + the GPU; `docker compose up -d --build`; waits for health.
Afterwards it rewrites `tts_workers` to the `http://host:8189` URLs.

Re-deploy or add one host later (from the controller):

```bash
bash scripts/install_worker_container.sh s1
```

> The container **mounts** the host's models; it does not download them. On a
> fresh worker with no models yet, populate `~/github/ComfyUI/models` first
> (`bash scripts/download_models.sh ~/github/ComfyUI` on the host, or rsync from
> a worker that has them).

## Or deploy on the worker itself

```bash
# On the worker machine:
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
| `BASE_IMAGE` | `nvidia/cuda:13.0.1-runtime-ubuntu24.04` | Default targets DGX Spark (GB10, CUDA 13). Multi-arch (amd64 + arm64/sbsa) |
| `TORCH_INDEX_URL` | `…/whl/cu130` | Match your GPU's CUDA — DGX Spark/GB10: cu130 (default); older GPUs: cu124/cu128 |
| `COMFYUI_PORT` / `TTS_PORT` | `8188` / `8189` | Host ports; match them in the controller config |

### GPU / arch notes

The defaults are verified on a DGX Spark (GB10 Blackwell, arm64, CUDA 13.0,
driver 580) — the image builds and torch sees the GPU as `torch 2.11.0+cu130`.
The base image is multi-arch, so the same compose file also builds on x86. The
one thing that varies by GPU is the PyTorch build: set `TORCH_INDEX_URL` to the
wheel index for your CUDA version (older GPUs: `cu124`/`cu128`, with a matching
`nvidia/cuda:12.x-runtime-ubuntu24.04` base). If a CUDA base-image tag is missing
for your architecture, pick another from
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
migrate (some hosts SSH-installed, some containerized).

> **Migrating a host: stop the native worker first.** The container ComfyUI and
> the SSH-installed ComfyUI both want the GPU and the same models. Don't run both
> on one machine — a containerized worker plus an already-loaded native render
> can exhaust GPU memory and get a render OOM-killed. Before `worker-up` on a
> host, stop its native worker: `make stop W=<host>` (ComfyUI) and ensure no
> native F5-TTS is mid-job. The container then has the same GPU footprint the
> native install had.
