# Environment variables

Nearly everything is configured in [`config.yaml`](configuration.md). These variables
cover the rest — mostly paths, timeouts, and fallbacks for keys you'd rather not store in
the config file.

## Credentials

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | _(unset)_ | Fallback Claude API key when `claude_api_key` isn't set in config |
| `XAI_API_KEY` | _(unset)_ | Fallback Grok/xAI API key when `grok_api_key` isn't set in config |
| `OPENAI_API_KEY` | _(unset)_ | Fallback OpenAI API key when `openai_api_key` isn't set in config |

## Media tools

| Variable | Default | Description |
|---|---|---|
| `FFMPEG_PATH` | `$(which ffmpeg)` | Path to the ffmpeg binary — set it when ffmpeg isn't on `PATH` |
| `FFMPEG_TIMEOUT` | `600` | Per-call ffmpeg timeout, in seconds |

!!! note "launchd and PATH"
    When the app runs as a macOS LaunchAgent it inherits a minimal `PATH` that does
    **not** include `/opt/homebrew/bin`. Media tools are resolved explicitly for that
    reason; if you hit a "not found" error only under the service, set `FFMPEG_PATH`.

## Speech

| Variable | Default | Description |
|---|---|---|
| `F5TTS_PYTHON` | _(auto-detected `f5tts` conda env)_ | Python interpreter for **local** TTS (F5 and Chatterbox). Workers use the container instead. Probes anaconda3/miniconda3/miniforge3 and `/opt/conda` when unset |
| `TTS_TIMEOUT` | `300` | Per-narration TTS timeout, in seconds |
| `OPENF5_REPO` | `mrfakename/OpenF5-TTS-Base` | Hugging Face repo for the narration weights — mirror or pin it, but keep it Apache/CC-BY licensed |
| `CHATTERBOX_REPO` | `ResembleAI/chatterbox` | Hugging Face repo for the Chatterbox weights — keep it MIT licensed |

See [model licensing](tts_licensing.md) before pointing these at other weights.

## Upscaling

| Variable | Default | Description |
|---|---|---|
| `TEMPORAL_VIDEO_UPSCALER_CMD` | _(unset)_ | Optional external command template for the film editor's temporal AI upscaler. Blank uses the packaged LTX-2.3 IC-LoRA workflow |
| `TEMPORAL_VIDEO_UPSCALER_TIMEOUT` | `7200` | Timeout for the temporal AI upscaler, in seconds |

## Storage

| Variable | Default | Description |
|---|---|---|
| `SPIELBOT_ORCHESTRATOR_DB` | `~/.local/share/video-generator/orchestrator.sqlite3` | Override path for the [durable orchestrator database](orchestration.md) |
| `CONFIG_YAML` | `~/.config/video-generator/config.yaml` | Override the config path (respected by the `scripts/` helpers) |

## Install-time variables

These are read by `make install` and the worker deploy scripts rather than by the app.

| Variable | Description |
|---|---|
| `WORKERS` | Space-separated worker hostnames, e.g. `make install WORKERS="s1 s2 s3"` |
| `GPU_MODE` | `legacy` (default) or `cdi` — how containers get the GPU. See [Cluster & workers](cluster.md#gpu-injection-mode) |
| `INSTALL_SERVICE` | `1` installs the macOS LaunchAgent without prompting |
| `INSTALL_FLUX1` | `1` also downloads the legacy FLUX.1 schnell models |
| `SVC_PYTHON` | Python interpreter for the controller-side seed-vc install (`scripts/install_svc.sh`) — [song re-voicing](performance_films.md#singing-films-the-music-video-format) |
| `SVC_CONTAINER` | Target ComfyUI container name for `make svc-install` (`scripts/install_svc_worker.sh`) |
