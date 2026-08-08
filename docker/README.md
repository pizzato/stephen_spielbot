# Containerized workers

Run the GPU workers (ComfyUI + TTS + EchoMimic) as containers so a new machine
joins the render fleet with one command instead of the SSH + Miniconda + rsync
bootstrap (issue #12).

Each worker machine runs the same stack, sharing that machine's GPU(s). The
controller (the web app, on your Mac) is unchanged — it just points at each
machine over HTTP.

| Service | Port | What it is |
|---|---|---|
| `comfyui` | 8188 | Vanilla ComfyUI + PyTorch (LTX 2.3 / ACE-Step / FLUX — all native nodes) |
| `tts` | 8189 | F5-TTS + Chatterbox Multilingual behind a small HTTP server (`pipeline/tts_server.py`) |
| `echomimic` | 8190 | EchoMimic-V3 talking-head server for dialogue scenes (`pipeline/echomimic_server.py`) |
| `autoheal` | — | Restarts any container whose (GPU-aware) healthcheck fails |

ComfyUI models (~49 GB) are **not** baked into the image — they live on the host
and are mounted in, so images stay small and rebuild fast. The EchoMimic weights
(~27 GB) live in a named Docker volume, fetched from Hugging Face on first use;
Chatterbox weights (~3.5 GB) land in the TTS container's HF cache (pre-warmed by
`make install`).

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
(no GitHub access needed on the worker); writes
`docker/.env` with `MODELS_DIR=~/github/ComfyUI/models` (the host's existing
models — **not** re-downloaded); **stops any native ComfyUI** so the container
can take `:8188` + the GPU; `docker compose up -d --build`; waits for health.
Afterwards it rewrites `tts_workers` to the `http://host:8189` URLs and
`echomimic_workers` to the `http://host:8190` URLs.

The Edit film screen's `Upscale video → AI temporal` mode runs Lightricks'
LTX-2.3 IC-LoRA Pixel Spatial Upscaler on the render workers. The ComfyUI image
installs Video Helper Suite and ComfyUI-LTXVideo; `make install` downloads the
IC-LoRA 2×/4× weights (plus the latent spatial upscaler used in scene gen).
Large finished MP4s are staged into the worker's ComfyUI input folder over
SSH/Docker instead of HTTP upload. No manual command template is required.

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
echomimic_workers:        # set by install — talking-head (dialogue scenes)
  - http://s1:8190
  - http://s2:8190
```

Cover/preview regen has no dedicated worker: while the UI is in use the backend
keeps one render worker idle for it (issue #98), so no `ui_workers` list.

A bare hostname in `tts_workers` is rejected — workers are HTTP containers, so
the value must be an `http://host:8189` URL.

## Configuration knobs (`docker/.env`)

| Var | Default | Notes |
|---|---|---|
| `MODELS_DIR` | — (required) | Host path to the ComfyUI `models/` dir, mounted into the ComfyUI container |
| `COMFYUI_INPUT_DIR` | `./input` | Host path mounted into `/opt/ComfyUI/input`; `make install` sets this to `~/github/ComfyUI/input` |
| `COMFYUI_REF` | `master` | Pin ComfyUI to a tag/branch/commit for reproducible workers |
| `BASE_IMAGE` | `nvidia/cuda:13.0.1-runtime-ubuntu24.04` | Default targets DGX Spark (GB10, CUDA 13). Multi-arch (amd64 + arm64/sbsa) |
| `TORCH_INDEX_URL` | `…/whl/cu130` | Match your GPU's CUDA — DGX Spark/GB10: cu130 (default); older GPUs: cu124/cu128 |
| `COMFYUI_PORT` / `TTS_PORT` / `ECHOMIMIC_PORT` | `8188` / `8189` / `8190` | Host ports; match them in the controller config |
| `BUILDER_IMAGE` | `nvidia/cuda:13.0.1-devel-ubuntu24.04` | `-devel` image used only to compile SageAttention (needs `nvcc`); not shipped. Match `BASE_IMAGE`'s CUDA version |
| `SAGEATTENTION_ARCHS` | `12.0;12.1` | Compute capabilities SageAttention is compiled for — `12.1` = GB10 (DGX Spark), `12.0` = sm_120 Blackwell workstation. Set **empty** on non-Blackwell GPUs to skip the build |
| `SAGEATTENTION_REF` | `main` | Pin SageAttention to a tag/branch/commit for reproducible workers |
| `COMFYUI_EXTRA_ARGS` | — (empty) | Extra flags appended to ComfyUI's launch. Set to `--use-sage-attention` to turn SageAttention on |

Temporal AI upscaling is configured by the app and submitted to the worker's
ComfyUI API after the finished film has been reviewed on the Remix screen. The
only exposed knob is the controller-side timeout in Settings.

### GPU / arch notes

The defaults are verified on a DGX Spark (GB10 Blackwell, arm64, CUDA 13.0,
driver 580) — the image builds and torch sees the GPU as `torch 2.11.0+cu130`.
The base image is multi-arch, so the same compose file also builds on x86. The
one thing that varies by GPU is the PyTorch build: set `TORCH_INDEX_URL` to the
wheel index for your CUDA version (older GPUs: `cu124`/`cu128`, with a matching
`nvidia/cuda:12.x-runtime-ubuntu24.04` base). If a CUDA base-image tag is missing
for your architecture, pick another from
[hub.docker.com/r/nvidia/cuda](https://hub.docker.com/r/nvidia/cuda/tags).

### SageAttention (opt-in)

[SageAttention](https://github.com/thu-ml/SageAttention) replaces attention with
quantised INT8/FP8 kernels. It ships no aarch64/CUDA-13 wheels and PyPI's build
does not target sm_121, so the image compiles it from source in a throwaway
`-devel` stage and installs only the resulting wheel — the CUDA toolkit itself
never reaches the worker. Expect the first build to take ~20 extra minutes; it
layer-caches afterwards.

Building it does **not** enable it. Turn it on per worker in `docker/.env`:

```
COMFYUI_EXTRA_ARGS=--use-sage-attention
```

then `bash scripts/worker.sh restart <host>`. The flag is global — it applies to
LTX and FLUX renders too, not just MiniMax H3 — so A/B one worker before rolling
it out, and check output quality as well as speed: SageAttention is an
approximation, and the same seed will not reproduce the un-accelerated render.
To roll back, clear the variable and restart; the kernels stay in the image,
unused.

Measured on GB10 (one H3 scene, 704×1280, 124 frames, 15 steps, EasyCache 0.2,
seed held equal across workers): **1.23× warm** (421.8 s → 343.0 s), 1.20× cold.
Two unchanged workers running the same job differed by 2.4 %, so the gap is the
kernel rather than the box. Output stayed intact — 124 frames, mean luma 137.4
un-accelerated vs 136.3 accelerated. Only H3 was measured; LTX and FLUX go
through the same flag unmeasured.

When benchmarking, **change the seed between rounds**. ComfyUI caches by
workflow hash, so re-submitting a seed a worker has already rendered returns the
cached mp4 in well under a second and looks like an enormous speedup.

Two known failure modes on GB10, both fixed by clearing `COMFYUI_EXTRA_ARGS`:
Triton has no sm_121 support, so if the flag falls through to SageAttention's
Triton backend the render can come out black; and a torch upgrade that changes
the C++ ABI needs an image rebuild, since the wheel is compiled against the
torch installed at build time.

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
