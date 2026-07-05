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

## Deploy from the controller (`make install`)

`make install` deploys containers to every host in `comfy_workers`, over SSH:

```bash
make install WORKERS="s1 s2 s3"               # seeds config + container deploy
```

Per host it: preflights Docker + the NVIDIA toolkit; rsyncs the build context
(no GitHub access needed on the worker — the repo is private); writes
`docker/.env` with `MODELS_DIR=~/github/ComfyUI/models` (the host's existing
models — **not** re-downloaded); **stops any native ComfyUI** so the container
can take `:8188` + the GPU; `docker compose up -d --build`; waits for health.
Afterwards it rewrites `tts_workers` to the `http://host:8189` URLs.

The Remix screen's `Upscale video → AI temporal` command runs on the controller
backend, not in these worker containers. Set it in Settings → Infrastructure, or
seed it while installing with `TEMPORAL_VIDEO_UPSCALER_CMD=... make install`.
No ComfyUI or TTS Dockerfile change is needed unless you deliberately point that
command at a custom containerized upscaler.

Re-deploy or add one host later (from the controller):

```bash
bash scripts/install_worker_container.sh s1
```

> The container **mounts** the host's models; it does not download them. On a
> fresh worker with no models yet, `make install` downloads them on the first
> worker and rsyncs to the rest; or populate `~/github/ComfyUI/models` yourself
> (`bash scripts/download_models.sh ~/github/ComfyUI` on the host).

## Config the controller writes

`make install` sets these in `~/.config/video-generator/config.yaml` from your
`comfy_workers` (you can also edit them in the Settings screen). All worker
endpoints are container URLs — TTS is reached over HTTP (`http://host:8189`), not
SSH:

```yaml
comfy_workers:            # you set these
  - http://s1:8188
  - http://s2:8188
tts_workers:              # set by install — http:// selects the HTTP transport
  - http://s1:8189
  - http://s2:8189
```

Cover/preview regen has no dedicated worker: while the UI is in use the backend
keeps one render worker idle for it (issue #98), so no `ui_workers` list.

A bare hostname in `tts_workers` is rejected — workers are HTTP containers, so
the value must be an `http://host:8189` URL.

## Configuration knobs (`docker/.env`)

| Var | Default | Notes |
|---|---|---|
| `MODELS_DIR` | — (required) | Host path to the ComfyUI `models/` dir, mounted into the ComfyUI container |
| `COMFYUI_REF` | `master` | Pin ComfyUI to a tag/branch/commit for reproducible workers |
| `BASE_IMAGE` | `nvidia/cuda:13.0.1-runtime-ubuntu24.04` | Default targets DGX Spark (GB10, CUDA 13). Multi-arch (amd64 + arm64/sbsa) |
| `TORCH_INDEX_URL` | `…/whl/cu130` | Match your GPU's CUDA — DGX Spark/GB10: cu130 (default); older GPUs: cu124/cu128 |
| `COMFYUI_PORT` / `TTS_PORT` | `8188` / `8189` | Host ports; match them in the controller config |

Temporal AI upscaling is configured in the controller's `config.yaml`, not
`docker/.env`, because the command is executed by the web backend after the
finished film has been reviewed on the Remix screen.

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

## Managing workers from the controller

Containers are the only worker pathway. `make start/stop/restart/status` manage
them over SSH (via `docker compose` on each host), and `W=<host>` scopes to one:

```bash
make status                 # web app + every worker container's health
make restart W=s2           # restart just s2's containers
make stop                   # stop all worker containers + the web app
```

Containers carry `restart: unless-stopped`, so they also come back on their own
after a host reboot. An `autoheal` sidecar additionally restarts any container
whose healthcheck fails (see GPU troubleshooting below). `make logs W=s2` tails a
host's container logs.

## Troubleshooting: GPU lost at runtime (silent CPU fallback)

A container can **lose access to the GPU while running** — typically after a host
`systemctl daemon-reload` or NVIDIA driver update, which revokes the device
cgroup from already-running containers. The symptom, inside the container:

```bash
$ docker exec spielbot-worker-tts-1 nvidia-smi -L
Failed to initialize NVML: Unknown Error
```

ComfyUI and F5-TTS then **silently fall back to CPU** — renders/narration still
"work" but are an order of magnitude slower (F5-TTS picks cuda→…→cpu with no
error). The host GPU itself is fine (`nvidia-smi` on the host works, and a fresh
`docker run --gpus all … nvidia-smi` sees it); only long-running containers are
affected, and it can hit one container but not another on the same host.

**Detect it:**
```bash
make status W=s1            # now prints GPU/CPU per container
```

**Fix it** (re-runs the NVIDIA prestart hook, reclaiming the GPU):
```bash
make restart W=s1          # or, one container: docker restart spielbot-worker-tts-1
```

**Self-healing — built in:** each container has a GPU-aware healthcheck, and the
`autoheal` service in `docker-compose.yml` restarts any container whose GPU check
fails, so a runtime GPU loss self-corrects within ~1–2 minutes. `make install`
also verifies GPU access on every container at the end of a deploy and warns if
one came up on CPU.

**Permanent prevention (optional, opt-in):** to stop a `daemon-reload` from
revoking the GPU in the first place, switch the toolkit to CDI device injection
on each host (test on one host first):
```bash
sudo nvidia-ctk runtime configure --runtime=docker --cdi.enabled
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml   # re-run after driver updates
sudo systemctl restart docker
```
The built-in self-heal keeps things working even without this.
