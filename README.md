<p align="center">
  <img src="assets/StephenSpielbot.png" alt="Stephen Spielbot" width="220">
</p>

# Stephen Spielbot

An AI video generator that turns a topic into a fully produced short film — complete with cinematic visuals, narration, and background music.

<p align="center">
  <a href="https://www.youtube.com/watch?v=2h5T0mkW1gc">
    <img src="https://img.youtube.com/vi/2h5T0mkW1gc/maxresdefault.jpg" alt="Watch: how Stephen Spielbot works" width="640"><br>
    ▶️ Watch how it works
  </a>
</p>

## What it does

1. **Script** — an LLM (local vLLM, Claude, Grok, or OpenAI) writes a multi-scene script with visual prompts, narration, and a mood-matched music description
2. **Images** — FLUX.2 Klein (the default per-style image engine) generates each scene's first-frame still, with optional recurring [characters](docs/characters.md) kept consistent via reference images
3. **Video** — [LTX 2.3](https://huggingface.co/Lightricks/LTX-2.3) animates each scene from its still via ComfyUI (local or distributed workers)
4. **Narration** — [F5-TTS](https://github.com/SWivid/F5-TTS) synthesises speech with voice cloning from a reference WAV. The default weights are the Apache-2.0 [OpenF5-TTS-Base](https://huggingface.co/mrfakename/OpenF5-TTS-Base) so narration is licensed for commercial use — see [docs/tts_licensing.md](docs/tts_licensing.md). A per-style voice-model picker adds [Chatterbox Multilingual](https://github.com/resemble-ai/chatterbox) (23 languages, with a per-style narration language that also drives the script's language)
5. **Dialogue** — scenes can instead be talking-head [dialogue or silent scenes](docs/dialogue_scenes.md): characters speak their lines in their own cloned voices, lip-synced by EchoMimic-V3
6. **Music** — [ACE-Step](https://github.com/ace-step/ACE-Step) generates background music from the LLM's mood description
7. **Assembly** — FFmpeg mixes everything into a single video with synced audio

Around the pipeline, the web app also handles the full channel workflow: a render
queue with automation, AI-suggested video ideas, per-scene editing with image
inpainting and version history, publishing to **YouTube** (multi-channel, with
playlists, captions, and tags) and **X**, a publish scheduler with per-channel
cadence, comment fetching / AI replies / community engagement, a predictive
engagement model, and C2PA "AI-generated" content credentials on published
videos.

## Requirements

**Controller** (runs the web app):
- Python 3.11+
- Node.js 20+ (builds the React frontend — `make install` skips the UI without it)
- FFmpeg (final assembly)
- A local vLLM server **or** an API key (Claude, Grok, or OpenAI) for script generation
- Passwordless SSH to each worker (`ssh-copy-id`)
- Optional: [c2patool](https://github.com/contentauth/c2pa-rs) + OpenSSL, for C2PA
  Content Credentials on published videos (signing is skipped when absent)

**Workers** (GPU machines):
- Docker + the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- ComfyUI (LTX 2.3), F5-TTS/Chatterbox, and EchoMimic (talking-head dialogue)
  run as containers — `make install` builds and deploys them; the worker needs
  no Python/conda of its own

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

> The interface is a React + FastAPI web app (`webapp/`) served from a single
> uvicorn process on port 8001. `make install` builds the frontend; after later
> frontend changes run `make web-build`, or use `make web-dev` for hot reload.

```bash
make stop      # stop everything
make status    # check health of the app and all workers
```

To remove an installation, `make uninstall` stops everything and removes the
service + worker container stacks (containers, volumes, and the built
`spielbot-*` images), keeping config, models, and rendered videos.
`bash scripts/uninstall.sh --purge-data --purge-models` also removes those —
`--purge-models` deletes `~/github/ComfyUI` only where the installer created it
(a pre-existing ComfyUI install is never deleted; interactive runs ask). Videos
in `~/videos` are never touched; delete the repo folder to finish.

## Security

The web app has **no authentication** and is meant to run bound to `localhost`
as a single-user tool — anyone who can reach port 8001 has full control. Don't
expose it to untrusted networks. `make tailscale` shares it with your tailnet
(no app auth, tailnet-only — never public). See [`SECURITY.md`](SECURITY.md).

## Cluster setup

Workers are configured in the single config file (see below) — there is no
separate `cluster.conf`. List your render workers under `comfy_workers`;
`make install` deploys the containers over SSH and derives `tts_workers` and
`echomimic_workers` from them automatically:

```yaml
# ~/.config/video-generator/config.yaml
comfy_workers:           # you set these
  - http://s1:8188
  - http://s2:8188
tts_workers:             # set by make install → containerized F5-TTS/Chatterbox
  - http://s1:8189
  - http://s2:8189
echomimic_workers:       # set by make install → talking-head (dialogue scenes)
  - http://s1:8190
  - http://s2:8190
```

Cover/preview regeneration has no dedicated worker: while the web UI is in use
the backend keeps one render worker idle for it, returning it to the render pool
after the UI has been idle for `ui_idle_timeout_seconds` (default 5 min, set in
**Settings**).

Workers must be reachable via SSH without a password (`ssh-copy-id`) and have
Docker + the NVIDIA Container Toolkit installed. The exception is `localhost`
(the single-machine setup): it is managed with plain local commands — no SSH
needed — but still needs Docker + the NVIDIA Container Toolkit.

### Containerized workers

`make install` deploys each render worker (ComfyUI + F5-TTS/Chatterbox +
EchoMimic) as **Docker containers**, driven over SSH from the controller (or locally for
`localhost`): it rsyncs the build context, mounts the host's existing models,
runs `docker compose up -d --build`, and points the config at the container
endpoints. See [`docker/README.md`](docker/README.md).

```bash
make install WORKERS="s1 s2 s3"
```

Deploy or re-deploy a single host from the controller:

```bash
bash scripts/install_worker_container.sh s1
```

**GPU injection mode**: by default containers get the GPU via the toolkit's
legacy path (`--gpus`-style device reservations). On very new drivers that path
can break — the symptom is CUDA failing with "unknown error" (cuInit → 999)
inside containers while `nvidia-smi` works, and
`docker run --device nvidia.com/gpu=all …` working fine. For such hosts deploy
with **CDI** injection instead:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml   # on the worker; redo after driver upgrades
GPU_MODE=cdi bash scripts/install_worker_container.sh s1     # or: make install GPU_MODE=cdi
```

The mode is sticky per host (kept on re-deploys; pass `GPU_MODE=legacy` to
revert) and is wired through `docker/.env`'s `COMPOSE_FILE`, so every plain
`docker compose` command on that host uses it automatically.

The CDI spec embeds versioned driver library paths, so a **driver upgrade
stales it** — the deploy preflight detects the mismatch and tells you to
regenerate. To keep it fresh automatically, enable the toolkit's refresh unit
where available: `sudo systemctl enable --now nvidia-cdi-refresh.path`. Note
that `nvidia-ctk cdi generate` also loads the NVIDIA kernel modules as a side
effect — if a *reboot* (not an upgrade) "broke CDI", the actual fix is loading
the modules before Docker at boot (see the `modules-load.d` note above), not
regenerating the spec.

Manage the workers from the controller — all of them, or one with `W=<host>`:

```bash
make status                 # web app + every worker container's health
make restart W=s2           # restart just s2's containers
make stop                   # stop all worker containers + the web app
make logs W=s2              # tail s2's container logs
```

`make start/stop/restart/status` drive `docker compose` on each host over SSH.
Containers carry `restart: unless-stopped`, so they also survive a host reboot.
`make start` recreates the containers (not just starts them) so they pick up
the host's current NVIDIA device nodes — this self-heals the "nvidia-smi works
but CUDA fails" state left behind when the driver or its modules were
(re)loaded after the containers were created. To prevent that state at boot,
load the modules before Docker: `printf 'nvidia\nnvidia-uvm\n' | sudo tee
/etc/modules-load.d/nvidia.conf`.

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
See [`docs/orchestration.md`](docs/orchestration.md) for how the durable layer
and the render process fit together.

## Configuration

All settings live in the single YAML file `~/.config/video-generator/config.yaml`
and can be edited live in the **Settings** screen (which also shows a read-only
cluster status panel). Worker lists are part of this file:

| Setting | Description |
|---|---|
| ComfyUI Workers | One URL per line — scenes are distributed across workers in parallel |
| TTS Workers | F5-TTS/Chatterbox endpoints for parallel narration (one container per worker on port 8189, derived from your render workers by `make install`) |
| EchoMimic Workers | Talking-head endpoints for dialogue scenes (one container per worker on port 8190, derived by `make install`) |
| UI worker idle timeout | Minutes the UI must be idle before its reserved render worker rejoins the pool (default 5) |
| LLM Backend | `local` (vLLM), `claude` (Anthropic), `grok` (xAI), or `openai` (ChatGPT) |
| Local LLM URL | OpenAI-compatible endpoint, e.g. `http://localhost:8000/v1/chat/completions` |
| Grok API key / model | xAI key (or `XAI_API_KEY`) and model name, e.g. `grok-4.5` |
| OpenAI API key / model | OpenAI key (or `OPENAI_API_KEY`) and model name, e.g. `gpt-4o` |
| Voice model / language | Per-style TTS engine — `openf5` (default), `chatterbox-multilingual` (23 languages + narration language), or the non-commercial `f5-original` preview |
| Resolution | Portrait FHD (1080×1920) default; landscape / portrait / square presets from 512×288 up to 1920×1080 |

The table above is a highlight reel — the Settings screen is the intended
editor for everything else. The full key set, with defaults and inline
documentation, is `DEFAULT_CFG` in [`app.py`](app.py); missing keys always fall
back to those defaults, so a minimal `config.yaml` is fine.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `F5TTS_PYTHON` | _(auto-detected `f5tts` conda env)_ | Python interpreter for *local* TTS (F5 and Chatterbox; workers use the container). Probes anaconda3/miniconda3/miniforge3 and `/opt/conda` when unset |
| `ANTHROPIC_API_KEY` | _(unset)_ | Fallback Claude API key when `claude_api_key` isn't set in config |
| `XAI_API_KEY` | _(unset)_ | Fallback Grok/xAI API key when `grok_api_key` isn't set in config |
| `OPENAI_API_KEY` | _(unset)_ | Fallback OpenAI API key when `openai_api_key` isn't set in config |
| `FFMPEG_PATH` | `$(which ffmpeg)` | Path to the ffmpeg binary (set when it isn't on `PATH`) |
| `FFMPEG_TIMEOUT` | `600` | Per-call ffmpeg timeout, seconds |
| `TEMPORAL_VIDEO_UPSCALER_CMD` | _(unset)_ | Optional external command template for the Remix temporal AI upscaler |
| `TEMPORAL_VIDEO_UPSCALER_TIMEOUT` | `7200` | Timeout for the temporal AI upscaler, in seconds |
| `TTS_TIMEOUT` | `300` | Per-narration TTS timeout, seconds |
| `ECHOMIMIC_TIMEOUT` | `7200` | Per-clip talking-head (EchoMimic) timeout, seconds |
| `OPENF5_REPO` | `mrfakename/OpenF5-TTS-Base` | Hugging Face repo for the narration weights (mirror/pin; keep it Apache/CC-BY licensed) |
| `CHATTERBOX_REPO` | `ResembleAI/chatterbox` | Hugging Face repo for the Chatterbox weights (mirror/pin; keep it MIT licensed) |
| `SPIELBOT_ORCHESTRATOR_DB` | `~/.local/share/video-generator/orchestrator.sqlite3` | Override path for the durable orchestrator database |

Final-film upscale on the Edit film screen has three modes:

| Mode | What it does |
|------|----------------|
| **Fast** | Plain ffmpeg scale |
| **LTX latent** | Simple model path: `LTXVLatentUpsampler` + `ltx-2.3-spatial-upscaler-x2-1.1` |
| **LTX IC-LoRA** | Generative [Pixel Spatial Upscaler](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler) (2×/4× IC-LoRA via ComfyUI-LTXVideo) |

Models are downloaded by `make install`; each upscale is kept as a selectable
final-video version so you can switch back to the original.

## Models

`make install` (and `make download-models`) downloads everything automatically. To download manually (~49 GB total):

**LTX 2.3** (~28 GB):
```bash
cd ~/github/ComfyUI
huggingface-cli download Lightricks/LTX-2.3-fp8 ltx-2.3-22b-dev-fp8.safetensors --local-dir models/checkpoints --local-dir-use-symlinks False
huggingface-cli download Lightricks/LTX-2.3 ltx-2.3-22b-distilled-lora-384.safetensors --local-dir models/loras --local-dir-use-symlinks False
huggingface-cli download Lightricks/LTX-2.3 ltx-2.3-spatial-upscaler-x2-1.1.safetensors --local-dir models/latent_upscale_models --local-dir-use-symlinks False
# IC-LoRA Pixel Spatial Upscaler (Remix AI temporal) — 2× and 4×
huggingface-cli download Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler ltx-2.3-22b-ic-lora-pixel-spatial-upscaler-x2-0.9.safetensors --local-dir models/loras --local-dir-use-symlinks False
huggingface-cli download Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler ltx-2.3-22b-ic-lora-pixel-spatial-upscaler-x4-0.9.safetensors --local-dir models/loras --local-dir-use-symlinks False
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

The legacy FLUX.1 schnell engine (also used for cover images) is optional
(`INSTALL_FLUX1=1 bash scripts/download_models.sh`, or `make download-flux`).

Two more model sets are fetched automatically outside this script:
**Chatterbox Multilingual** TTS weights (~3.5 GB — `make install` pre-warms them
into each TTS worker's Hugging Face cache) and the **EchoMimic-V3** talking-head
weights (~27 GB: Wan2.1-Fun-1.3B + chinese-wav2vec2 + EchoMimicV3, fetched into
the `echomimic` container's volume on first use). `make install` also downloads
a 10-voice public-domain LibriVox **character voice library**
(`make download-voices`) used to auto-cast dialogue characters.

## Channels using this tool

<!-- CHANNELS:START -->
- [Stephen Spielbot (@StephenSpielbot)](https://www.youtube.com/@StephenSpielbot) — YouTube · The original
- [A Brief History of Botkind (@BHOBk)](https://www.youtube.com/@BHOBk) — YouTube
- [A Brief History of Botkind (@aBHOBk)](https://x.com/aBHOBk) — X
<!-- CHANNELS:END -->

Making films with Stephen Spielbot? Add your channel to
[`channels.yaml`](channels.yaml) and open a pull request — that file is the only
thing you need to edit. On merge, a GitHub Action regenerates this list and the
app's **About** screen. Run `make channels` if you want to preview the result
locally.

## More docs

- [`docs/characters.md`](docs/characters.md) — recurring characters: consistent looks, reference images, voices
- [`docs/dialogue_scenes.md`](docs/dialogue_scenes.md) — dialogue, silent, and narration scene modes; the EchoMimic worker
- [`docs/orchestration.md`](docs/orchestration.md) — the durable SQLite task layer and how renders execute
- [`docs/youtube_setup.md`](docs/youtube_setup.md) — Google Cloud / OAuth setup for YouTube publishing
- [`docs/x_setup.md`](docs/x_setup.md) — X (Twitter) developer app setup for posting
- [`docker/README.md`](docker/README.md) — the containerized worker stack in detail
- [`webapp/README.md`](webapp/README.md) — web UI architecture and development workflow
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, tests, and the CI gate

## License

Stephen Spielbot's code is licensed under [Apache-2.0](LICENSE).

The AI **models** it downloads each carry their own licenses — see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and
[`docs/tts_licensing.md`](docs/tts_licensing.md).
The defaults (FLUX.2 Klein, LTX-Video, ACE-Step, and the OpenF5 narration model)
are commercial-friendly; the original F5-TTS narration weights are offered only
as an opt-in **non-commercial** preview. Review the notices before monetizing.

> "Stephen Spielbot" is a playful name and is not affiliated with, endorsed by,
> or connected to Steven Spielberg or any of his companies.
