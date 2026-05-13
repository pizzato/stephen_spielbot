<p align="center">
  <img src="assets/StephenSpielbot.png" alt="Stephen Spielbot" width="220">
</p>

# Stephen Spielbot

An AI video generator that turns a topic into a fully produced short film — complete with cinematic visuals, narration, and background music.

## What it does

1. **Script** — an LLM writes a multi-scene script with visual prompts, narration, and a mood-matched music description
2. **Video** — [LTX 2.3](https://huggingface.co/Lightricks/LTX-Video) generates each scene clip via ComfyUI (local or distributed workers)
3. **Narration** — [F5-TTS](https://github.com/SWivid/F5-TTS) synthesises speech; supports voice cloning from a reference WAV
4. **Music** — [ACE-Step](https://github.com/ace-step/ACE-Step) generates background music from the LLM's mood description
5. **Assembly** — FFmpeg mixes everything into a single video with synced audio

## Requirements

- Python 3.10+
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) with LTX 2.3 models
- F5-TTS in a separate Python environment (set `F5TTS_PYTHON`)
- FFmpeg
- A local vLLM server **or** a Claude API key for script generation

## Setup

```bash
git clone https://github.com/pizzato/stephen_spielbot
cd stephen_spielbot
pip install gradio anthropic websocket-client

# Point to your F5-TTS Python interpreter
export F5TTS_PYTHON=~/miniconda3/envs/f5tts/bin/python

# Start ComfyUI (separate terminal)
cd ~/github/ComfyUI && python main.py --listen

# Launch the app
python app.py
```

Open [http://localhost:7860](http://localhost:7860).

## Configuration

All settings live in `~/.config/video-generator/config.json` and can be edited live in the **Config** tab:

| Setting | Description |
|---|---|
| ComfyUI Workers | One URL per line — scenes are distributed across workers in parallel |
| TTS Workers | Hostnames for parallel narration (need F5-TTS at `~/f5tts-env`) |
| LLM Backend | `local` (vLLM) or `claude` (Anthropic API) |
| Local LLM URL | OpenAI-compatible endpoint, e.g. `http://localhost:8000/v1/chat/completions` |
| Resolution | 832×480 default; portrait and square presets available |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `F5TTS_PYTHON` | `~/miniconda3/envs/f5tts/bin/python` | Python interpreter for F5-TTS |
| `CHATTERBOX_PYTHON` | `~/miniconda3/envs/chatterbox/bin/python` | Python interpreter for Chatterbox TTS |
| `F5TTS_REMOTE_PYTHON` | `~/f5tts-env/bin/python` | F5-TTS Python path on remote TTS workers |

## LTX 2.3 models

Download to your ComfyUI `models/` directory:

```bash
huggingface-cli download Lightricks/LTX-2.3-fp8 ltx-2.3-22b-dev-fp8.safetensors --local-dir models/checkpoints
huggingface-cli download Lightricks/LTX-2.3 ltx-2.3-22b-distilled-lora-384.safetensors --local-dir models/loras
huggingface-cli download Lightricks/LTX-2.3 ltx-2.3-spatial-upscaler-x2-1.1.safetensors --local-dir models/upscale_models
huggingface-cli download Comfy-Org/ltx-2 split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors --local-dir models/text_encoders
```
