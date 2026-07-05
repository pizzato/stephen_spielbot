<p align="center">
  <img src="assets/StephenSpielbot.png" alt="Stephen Spielbot" width="220">
</p>

# Stephen Spielbot

An AI video generator that turns a topic into a fully produced short film — complete with cinematic visuals, narration, and background music.

## What it does

1. **Script** — an LLM writes a multi-scene script with visual prompts, narration, and a mood-matched music description
2. **Video** — [LTX 2.3](https://huggingface.co/Lightricks/LTX-Video) generates each scene clip via ComfyUI (local or distributed workers)
3. **Narration** — [F5-TTS](https://github.com/SWivid/F5-TTS) synthesises speech; supports voice cloning from a reference WAV. Uses the Apache-2.0 [OpenF5-TTS-Base](https://huggingface.co/mrfakename/OpenF5-TTS-Base) weights so narration is licensed for commercial use — see [NOTICE.md](NOTICE.md)
4. **Music** — [ACE-Step](https://github.com/ace-step/ACE-Step) generates background music from the LLM's mood description
5. **Assembly** — FFmpeg mixes everything into a single video with synced audio

## Durable orchestration

Generation state is mirrored into a SQLite controller database at
`~/.local/share/video-generator/orchestrator.sqlite3`.  Each story, image,
narration, music, scene-video, mux, and final-assembly unit is tracked as a
task with dependencies, attempts, leases, worker ownership, and produced
artifacts.  The web app's **Render** screen shows this durable task graph
alongside the progress bar.

The standard app path still launches `resume_generation.py`, but that process
now writes durable task/artifact state as it runs.  For a fully agent-driven
deployment, run one worker daemon per execution resource:

```bash
make worker-agent KIND=comfy ENDPOINT=http://s1:8188
make worker-agent KIND=tts ENDPOINT=http://s1:8189
make worker-agent KIND=local ENDPOINT=assembler
```

Worker agents lease ready tasks, heartbeat while running, and expired leases are
made available for retry.  This gives recovery a persisted source of truth
instead of relying only on process memory, ComfyUI queue state, and files.
Current implementation and test status are tracked in
[`docs/durable_orchestration_status.md`](docs/durable_orchestration_status.md).

## Requirements

**Controller** (runs the web app):
- Python 3.10+
- FFmpeg (final assembly)
- A local vLLM server **or** a Claude API key for script generation
- Passwordless SSH to each worker (`ssh-copy-id`)

**Workers** (GPU machines):
- Docker + the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- ComfyUI (LTX 2.3) + F5-TTS run as containers — `make install` builds and
  deploys them; the worker needs no Python/conda of its own

## Quick start

```bash
git clone https://github.com/pizzato/stephen_spielbot
cd stephen_spielbot
make install WORKERS="s1 s2 s3"   # deps, models, workers, config.yaml, web UI
make start                        # start ComfyUI on all workers, then launch the app
```

`make install` sets up everything — Python deps, models, the web UI (backend +
React build), and seeds `config.yaml` with your workers. Omit `WORKERS=...` for a
single-machine (localhost) setup, or run `make install` with no args to be
prompted. Then open [http://localhost:8001](http://localhost:8001).

Optional: seed the Remix screen's AI-temporal final-video upscale command during install:

```bash
TEMPORAL_VIDEO_UPSCALER_CMD='your-upscaler -i {input} -o {output} --width {width} --height {height}' make install
```

> The interface is a React + FastAPI web app (`webapp/`) served from a single
> uvicorn process on port 8001. `make install` builds the frontend; after later
> frontend changes run `make web-build`, or use `make web-dev` for hot reload.

```bash
make stop      # stop everything
make status    # check health of the app and all workers
```

## Security

The web app has **no authentication** and is meant to run bound to `localhost`
as a single-user tool — anyone who can reach port 8001 has full control. Don't
expose it to untrusted networks. `make tailscale` shares it with your tailnet
(no app auth, tailnet-only — never public). See [`SECURITY.md`](SECURITY.md).

## Cluster setup

Workers are configured in the single config file (see below) — there is no
separate `cluster.conf`. List your render workers under `comfy_workers`;
`make install` deploys the containers over SSH and derives `tts_workers` from
them automatically:

```yaml
# ~/.config/video-generator/config.yaml
comfy_workers:           # you set these
  - http://s1:8188
  - http://s2:8188
tts_workers:             # set by make install → containerized F5-TTS
  - http://s1:8189
  - http://s2:8189
```

Cover/preview regeneration has no dedicated worker: while the web UI is in use
the backend keeps one render worker idle for it, returning it to the render pool
after the UI has been idle for `ui_idle_timeout_seconds` (default 5 min, set in
**Settings**).

Workers must be reachable via SSH without a password (`ssh-copy-id`) and have
Docker + the NVIDIA Container Toolkit installed.

### Containerized workers

`make install` deploys each render worker (ComfyUI + F5-TTS) as **Docker
containers**, driven over SSH from the controller: it rsyncs the build context,
mounts the host's existing models, runs `docker compose up -d --build`, and
points the config at the container endpoints. See
[`docker/README.md`](docker/README.md).

```bash
make install WORKERS="s1 s2 s3"
```

Deploy or re-deploy a single host from the controller:

```bash
bash scripts/install_worker_container.sh s1
```

Manage the workers from the controller — all of them, or one with `W=<host>`:

```bash
make status                 # web app + every worker container's health
make restart W=s2           # restart just s2's containers
make stop                   # stop all worker containers + the web app
make logs W=s2              # tail s2's container logs
```

`make start/stop/restart/status` drive `docker compose` on each host over SSH.
Containers carry `restart: unless-stopped`, so they also survive a host reboot.

## Configuration

All settings live in the single YAML file `~/.config/video-generator/config.yaml`
and can be edited live in the **Settings** screen (which also shows a read-only
cluster status panel). Worker lists are part of this file:

| Setting | Description |
|---|---|
| ComfyUI Workers | One URL per line — scenes are distributed across workers in parallel |
| TTS Workers | F5-TTS endpoints for parallel narration (one container per worker on port 8189, derived from your render workers by `make install`) |
| UI worker idle timeout | Minutes the UI must be idle before its reserved render worker rejoins the pool (default 5) |
| LLM Backend | `local` (vLLM) or `claude` (Anthropic API) |
| Local LLM URL | OpenAI-compatible endpoint, e.g. `http://localhost:8000/v1/chat/completions` |
| Resolution | 832×480 default; portrait and square presets available |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `F5TTS_PYTHON` | `~/miniconda3/envs/f5tts/bin/python` | Python interpreter for *local* F5-TTS (workers use the container) |
| `CHATTERBOX_PYTHON` | `~/miniconda3/envs/chatterbox/bin/python` | Python interpreter for Chatterbox TTS |
| `ANTHROPIC_API_KEY` | _(unset)_ | Fallback Claude API key when `claude_api_key` isn't set in config |
| `FFMPEG_PATH` | `$(which ffmpeg)` | Path to the ffmpeg binary (set when it isn't on `PATH`) |
| `FFMPEG_TIMEOUT` | `600` | Per-call ffmpeg timeout, seconds |
| `TEMPORAL_VIDEO_UPSCALER_CMD` | unset | Optional install/env fallback for the saved Settings value. Seeds `temporal_video_upscaler_cmd` during `make install`. |
| `TEMPORAL_VIDEO_UPSCALER_TIMEOUT` | `7200` | Optional install/env fallback for the saved Settings timeout, in seconds |
| `TTS_TIMEOUT` | `300` | Per-narration F5-TTS timeout, seconds |
| `SPIELBOT_ORCHESTRATOR_DB` | `~/.local/share/video-generator/orchestrator.sqlite3` | Override path for the durable orchestrator database |

The temporal upscaler command is also editable in Settings → Infrastructure.
Use it from the Remix screen after reviewing the finished film; each upscale is
kept as a selectable final-video version so you can switch back to the original.
It runs on the controller backend, not inside the ComfyUI/TTS worker containers,
so use an absolute command path if the app runs under launchd.

## Models

`make install` (and `make download-models`) downloads everything automatically. To download manually (~49 GB total):

**LTX 2.3** (~28 GB):
```bash
cd ~/github/ComfyUI
huggingface-cli download Lightricks/LTX-2.3-fp8 ltx-2.3-22b-dev-fp8.safetensors --local-dir models/checkpoints --local-dir-use-symlinks False
huggingface-cli download Lightricks/LTX-2.3 ltx-2.3-22b-distilled-lora-384.safetensors --local-dir models/loras --local-dir-use-symlinks False
huggingface-cli download Lightricks/LTX-2.3 ltx-2.3-spatial-upscaler-x2-1.1.safetensors --local-dir models/latent_upscale_models --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/ltx-2 split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False
```

**ACE-Step 1.5** (~5 GB, for music generation):
```bash
huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files split_files/diffusion_models/acestep_v1.5_turbo.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files split_files/vae/ace_1.5_vae.safetensors --local-dir models/vae --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files split_files/text_encoders/qwen_0.6b_ace15.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files split_files/text_encoders/qwen_4b_ace15.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False
```

**FLUX.2 Klein 4B** (~16 GB, the default image generate + edit engine):
```bash
huggingface-cli download Comfy-Org/vae-text-encorder-for-flux-klein-4b split_files/diffusion_models/flux-2-klein-4b.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/vae-text-encorder-for-flux-klein-4b split_files/text_encoders/qwen_3_4b.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/vae-text-encorder-for-flux-klein-4b split_files/vae/flux2-vae.safetensors --local-dir models/vae --local-dir-use-symlinks False
```

The legacy FLUX.1 schnell engine is optional (`INSTALL_FLUX1=1 bash scripts/download_models.sh`).

## License

Stephen Spielbot's code is licensed under [Apache-2.0](LICENSE).

The AI **models** it downloads each carry their own licenses — see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and [`NOTICE.md`](NOTICE.md).
The defaults (FLUX.2 Klein, LTX-Video, ACE-Step, and the OpenF5 narration model)
are commercial-friendly; the original F5-TTS narration weights are offered only
as an opt-in **non-commercial** preview. Review the notices before monetizing.

> "Stephen Spielbot" is a playful name and is not affiliated with, endorsed by,
> or connected to Steven Spielberg or any of his companies.
