# Models

`make install` (and `make download-models` on its own) downloads everything
automatically — roughly **49 GB** for the defaults. Models live on each worker's host at
`~/github/ComfyUI/models` and are mounted into the containers, so they survive image
rebuilds.

!!! danger "`~/github/ComfyUI/models` is the live model store"
    Never delete it as part of a cleanup. `make uninstall --purge-models` only removes a
    ComfyUI directory the installer itself created, and asks first when interactive.

## What gets downloaded

| Model | Size | Used for |
|---|---|---|
| [LTX 2.3](https://huggingface.co/Lightricks/LTX-2.3) | ~28 GB | Scene video generation + spatial upscalers |
| [FLUX.2 Klein 4B](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b) | ~16 GB | Scene first-frame images and the "Edit image" inpaint |
| [ACE-Step 1.5](https://github.com/ace-step/ACE-Step) | ~5 GB | Background music |
| Chatterbox Multilingual | ~3.5 GB | Optional multilingual narration (pre-warmed into each TTS worker's HF cache) |
| EchoMimic-V3 (+ Wan2.1-Fun-1.3B, chinese-wav2vec2) | ~27 GB | Talking-head dialogue scenes — fetched into the `echomimic` container volume on first use |
| LibriVox character voices | small | 10 public-domain voices auto-cast onto script characters (`make download-voices`) |

The **OpenF5-TTS-Base** narration weights are fetched by the TTS container on first use.

## Manual download

If you'd rather fetch them yourself:

=== "LTX 2.3 (~28 GB)"

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

=== "FLUX.2 Klein 4B (~16 GB)"

    ```bash
    huggingface-cli download Comfy-Org/vae-text-encorder-for-flux-klein-4b split_files/diffusion_models/flux-2-klein-4b.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
    huggingface-cli download Comfy-Org/vae-text-encorder-for-flux-klein-4b split_files/text_encoders/qwen_3_4b.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False
    huggingface-cli download Comfy-Org/vae-text-encorder-for-flux-klein-4b split_files/vae/flux2-vae.safetensors --local-dir models/vae --local-dir-use-symlinks False
    ```

=== "ACE-Step 1.5 (~5 GB)"

    ```bash
    huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files split_files/diffusion_models/acestep_v1.5_turbo.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
    huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files split_files/vae/ace_1.5_vae.safetensors --local-dir models/vae --local-dir-use-symlinks False
    huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files split_files/text_encoders/qwen_0.6b_ace15.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False
    huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files split_files/text_encoders/qwen_4b_ace15.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False
    ```

The legacy FLUX.1 schnell engine (also used for cover images) is optional:

```bash
make download-flux             # locally
INSTALL_FLUX1=1 bash scripts/download_models.sh
make download-flux-cluster     # to the first node, then rsync to all workers
```

## Gated weights

Some Hugging Face repos require accepting a license. Set a **Hugging Face token** in
**Settings → Infrastructure → Image models** and the workers use it to auto-download the
gated engine weights.

## Upscaler modes

The Edit film screen's final-video upscale has three modes:

| Mode | What it does |
|---|---|
| **Fast** | Plain ffmpeg scale |
| **LTX latent** | Simple model path: `LTXVLatentUpsampler` + `ltx-2.3-spatial-upscaler-x2-1.1` |
| **LTX IC-LoRA** | Generative [Pixel Spatial Upscaler](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler) (2×/4× IC-LoRA via ComfyUI-LTXVideo) |

Each upscale is kept as a selectable final-video version, so you can switch back to the
original at any time.

## Licensing

The defaults — FLUX.2 Klein, LTX-Video, ACE-Step, and the OpenF5 narration model — are
commercial-friendly on purpose. The original F5-TTS narration weights are offered only as
an opt-in **non-commercial** preview.

Read [model licensing](tts_licensing.md) and
[THIRD_PARTY_NOTICES.md](https://github.com/pizzato/stephen_spielbot/blob/main/THIRD_PARTY_NOTICES.md)
before monetizing anything you make.
