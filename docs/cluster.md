# Cluster & workers

Workers are configured in the [single config file](configuration.md) — there is no
separate `cluster.conf`. You list your render workers under `comfy_workers`;
`make install` deploys the containers over SSH and derives `tts_workers` from them
automatically.

```yaml
# ~/.config/video-generator/config.yaml
comfy_workers:           # you set these
  - http://s1:8188
  - http://s2:8188
tts_workers:             # set by make install → containerized F5-TTS/Chatterbox
  - http://s1:8189
  - http://s2:8189
```

Scenes are distributed across the workers in parallel, so adding a machine shortens
every render.

## Prerequisites per worker

- Docker Engine + Docker Compose v2
- NVIDIA driver + the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- Reachable over SSH **without a password** (`ssh-copy-id`)

The exception is `localhost` — the single-machine setup is managed with plain local
commands and needs no SSH, but still needs Docker and the toolkit.

Verify the GPU is visible to Docker:

```bash
docker run --rm --gpus all nvidia/cuda:13.0.1-base-ubuntu24.04 nvidia-smi
```

## The container stack

Each worker machine runs the same stack, sharing that machine's GPU(s):

| Service | Port | What it is |
|---|---|---|
| `comfyui` | 8188 | ComfyUI + PyTorch (LTX 2.5 / ACE-Step / MiniMax Music 3 / FLUX — all native nodes) |
| `tts` | 8189 | F5-TTS + Chatterbox Multilingual behind a small HTTP server |
| `autoheal` | — | Restarts any container whose GPU-aware healthcheck fails |

ComfyUI models (~49 GB) are **not** baked into the images — they live on the host and are
mounted in, so images stay small and rebuild fast. Chatterbox weights (~3.5 GB) land in
the TTS container's Hugging Face cache, pre-warmed by `make install`.

Full detail: [`docker/README.md`](https://github.com/pizzato/stephen_spielbot/blob/main/docker/README.md).

### Song re-voicing rides along in the ComfyUI container

The [singing films](performance_films.md#singing-films-the-music-video-format)
feature's **"Sing this as [voice]"** step (seed-vc) is the one job that does not go
through ComfyUI's API: the controller copies the vocal stem in and runs the diffusion
with `docker exec` inside `spielbot-worker-comfyui-1`, reusing that container's CUDA
PyTorch. **Any worker can take it** — the backend picks the idle one (the same
least-busy-first ordering covers use), tries the next if that host is down, and converts
on the controller's own GPU only when no worker will. It honours the
[fleet-wide worker lease](#one-job-per-worker-no-matter-who-asks): a worker busy with a
render or upscale is skipped rather than double-booked, and when every worker is leased
the controller converts (slow beats waiting out a multi-minute GPU job).

The image carries seed-vc, so a worker deployed with `make install` is ready. Containers
built before it landed need it added once — they keep running while it installs:

```bash
make svc-install          # every worker; add W=s2 for one host
```

seed-vc and the ~1 GB of weights it downloads live in the `seed-vc` volume, so they
survive the container recreation `make start` does. The volume is seeded from the image
the first time the container is created, which means the order on an older fleet is:
redeploy the stack (`make install`), then `make svc-install` once. A worker without it
is not a failure — the backend just moves to the next worker, or converts on the
controller.

## Deploying

Deploy or re-deploy every worker:

```bash
make install WORKERS="s1 s2 s3"
```

Deploy a single host from the controller:

```bash
bash scripts/install_worker_container.sh s1
```

The installer rsyncs the build context, mounts the host's existing models, runs
`docker compose up -d --build`, and points the config at the container endpoints.

## Managing workers

All of them, or one with `W=<host>`:

```bash
make status                 # web app + every worker container's health
make restart W=s2           # restart just s2's containers
make stop                   # stop all worker containers + the web app
make logs W=s2              # tail s2's container logs
```

These drive `docker compose` on each host over SSH. Containers carry
`restart: unless-stopped`, so they survive a host reboot.

`make start` **recreates** the containers rather than just starting them, so they pick up
the host's current NVIDIA device nodes. This self-heals the "nvidia-smi works but CUDA
fails" state left behind when the driver or its modules were (re)loaded after the
containers were created.

To prevent that state at boot, load the modules before Docker:

```bash
printf 'nvidia\nnvidia-uvm\n' | sudo tee /etc/modules-load.d/nvidia.conf
```

You can also power workers on and off from **Settings → Infrastructure → Container power**.

## GPU injection mode

By default containers get the GPU via the toolkit's legacy path (`--gpus`-style device
reservations). On very new drivers that path can break. The symptom is CUDA failing with
"unknown error" (`cuInit` → 999) *inside* containers while `nvidia-smi` works, and
`docker run --device nvidia.com/gpu=all …` working fine.

For such hosts, deploy with **CDI** injection instead:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml   # on the worker
GPU_MODE=cdi bash scripts/install_worker_container.sh s1     # or: make install GPU_MODE=cdi
```

The mode is sticky per host (kept on re-deploys; pass `GPU_MODE=legacy` to revert) and is
wired through `docker/.env`'s `COMPOSE_FILE`, so every plain `docker compose` command on
that host uses it automatically.

!!! warning "A driver upgrade stales the CDI spec"
    The spec embeds versioned driver library paths. The deploy preflight detects the
    mismatch and tells you to regenerate. To keep it fresh automatically, enable the
    toolkit's refresh unit where available:
    `sudo systemctl enable --now nvidia-cdi-refresh.path`.

    Note that `nvidia-ctk cdi generate` also loads the NVIDIA kernel modules as a side
    effect. If a *reboot* (rather than an upgrade) appears to have "broken CDI", the real
    fix is loading the modules before Docker at boot — see `modules-load.d` above — not
    regenerating the spec.

## One job per worker, no matter who asks

Every ComfyUI-bound task — a film render, a batch upscale, scene re-renders, previews,
inpainting, song generation, cover generation — takes a per-worker **lease** before submitting, shared
across the whole app (backend and the render subprocess alike). A worker runs one job
at a time; everything else waits its turn and shows as *queued · waiting for a free
worker* on the Activity screen. Starting an upscale mid-render (or two upscales at
once) queues the work instead of stacking jobs onto GPUs that are already busy. A
crashed process releases its leases automatically.

## The reserved UI worker

Cover and preview regeneration has no dedicated worker. While the web UI is in use, the
backend holds **one render worker idle** for it, and returns it to the render pool after
the UI has been idle for `ui_idle_timeout_seconds` (default 5 minutes, set in
**Settings → Infrastructure**).

With a single worker this means an interactive session briefly competes with the render;
with three or more it is invisible. Song re-voicing marks the UI active the same way, so
a conversion started mid-render lands on the held-idle worker rather than on the GPU
that is rendering.

## Worker agents (optional)

The standard path launches `resume_generation.py`, which writes
[durable task state](orchestration.md) as it runs. For a fully agent-driven deployment,
run one worker daemon per execution resource instead:

```bash
make worker-agent KIND=comfy ENDPOINT=http://s1:8188
make worker-agent KIND=tts   ENDPOINT=http://s1:8189
make worker-agent KIND=local ENDPOINT=assembler
```

Worker agents lease ready tasks, heartbeat while running, and expired leases become
available for retry.
