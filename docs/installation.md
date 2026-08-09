# Installation

Stephen Spielbot has two roles. The **controller** runs the web app, holds the config
and the finished videos, and drives everything else. The **workers** are GPU machines
that render images, video, narration, and music.

They can be the same machine. `make install` with no worker list sets up a
single-machine install where the controller is also the (localhost) worker.

## Requirements

=== "Controller"

    - Python 3.11+
    - Node.js 20+ — builds the React frontend (`make install` skips the UI build without it)
    - FFmpeg — final assembly
    - A local vLLM server **or** an API key for Claude, Grok, or OpenAI (script writing)
    - Passwordless SSH to each worker (`ssh-copy-id`) — not needed for a single-machine install
    - Optional: [c2patool](https://github.com/contentauth/c2pa-rs) + OpenSSL, for
      [C2PA Content Credentials](manual/settings.md#content-credentials-c2pa) on published
      videos. Signing is skipped silently when it isn't installed.

=== "Workers (GPU machines)"

    - An NVIDIA GPU with enough VRAM for LTX 2.3 (the models total ~49 GB on disk)
    - Docker Engine + Docker Compose v2
    - The [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)

    ComfyUI (LTX 2.3 / FLUX / ACE-Step) and F5-TTS/Chatterbox run as
    **containers** that `make install` builds and deploys. A worker needs no Python,
    conda, or ComfyUI install of its own.

## Quick start

```bash
git clone https://github.com/pizzato/stephen_spielbot
cd stephen_spielbot
make install WORKERS="s1 s2 s3"   # deps, models, workers, config.yaml, web UI
make start                        # start the workers, then launch the app
```

Then open [http://localhost:8001](http://localhost:8001).

Omit `WORKERS=...` for a single-machine (localhost) setup, or run `make install` with no
arguments and it prompts once. An existing `config.yaml` is never overwritten.

!!! tip "Single machine"
    On one Linux box with a GPU, `make install` (no arguments, blank at the prompt)
    configures `localhost` as the worker. It's managed with plain local commands — no
    SSH — but still needs Docker and the NVIDIA Container Toolkit.

### What `make install` does

1. Creates `.venv` and installs the Python requirements
2. Seeds `~/.config/video-generator/config.yaml` with your worker list (first run only)
3. Downloads the [models](models.md) (~49 GB) and the 10-voice LibriVox character voice library
4. Builds and deploys the [worker containers](cluster.md) over SSH, and points the config at them
5. Installs the web backend deps and builds the React frontend

The macOS login service (a LaunchAgent that keeps the app running) is opt-in — the
installer asks, or set `INSTALL_SERVICE=1 make install` to install it without asking.

## Day-to-day commands

```bash
make start      # start every worker's containers + the web app
make stop       # stop everything
make restart    # stop, then start
make status     # health of the app and every worker container
make logs W=s2  # tail one worker's container logs
```

`start`, `stop`, `restart`, `status`, and `logs` all accept `W=<host>` to target a single
worker. `make restart-server` restarts only the web app and leaves the workers running.

!!! note "Frontend changes need a rebuild"
    The backend serves the *built* SPA. After changing frontend code run `make web-build`
    (or use `make web-dev` for hot reload on port 5174), then `make restart-server`.

## Running the web app

The interface is a React + FastAPI app in `webapp/`, served from a single uvicorn
process on port 8001. `make install` builds it; these targets manage it afterwards:

| Target | What it does |
|---|---|
| `make web` | Build the SPA and serve UI + API on `localhost:8001` |
| `make web-build` | Production build of the frontend into `webapp/frontend/dist` |
| `make web-dev` | FastAPI (autoreload) + Vite dev server with `/api` proxy on `localhost:5174` |
| `make restart-server` | Restart just the web app, serving the last build |

## Remote access

`make tailscale` shares the app with your [Tailscale](https://tailscale.com) devices over
tailnet-only HTTPS, so you can drive it from your phone. Turn it off with
`tailscale serve reset`.

Never expose it publicly — [there is no authentication](security.md).

## Resetting parts of the install

```bash
make clean queue      # clear the render + publish queues (keeps rendered videos)
make clean workers    # remove the worker Docker stacks (containers, volumes, images)
make clean settings   # delete config.yaml + YouTube/X credentials
make clean all        # all of the above
```

Each mode prints what it will delete and asks for confirmation first.

## Uninstalling

```bash
make uninstall
```

Stops everything and removes the service and the worker container stacks (containers,
volumes, and the built `spielbot-*` images), keeping config, models, and rendered videos.

To go further:

```bash
bash scripts/uninstall.sh --purge-data --purge-models
```

`--purge-models` deletes `~/github/ComfyUI` **only where the installer created it** — a
pre-existing ComfyUI install is never deleted, and interactive runs ask first. Videos in
`~/videos` are never touched. Delete the repo folder to finish.

## Next

- [Your first film](first-film.md) — the end-to-end walkthrough
- [Cluster & workers](cluster.md) — multi-worker deployment, GPU injection modes
- [Configuration](configuration.md) — the single config file and the Settings screen
- [Troubleshooting](troubleshooting.md) — when something doesn't come up
