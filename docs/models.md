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

The legacy FLUX.1 schnell engine is optional (covers use the style's own image
engine — their titles are drawn with real fonts, not by the model):

```bash
make download-flux             # locally
INSTALL_FLUX1=1 bash scripts/download_models.sh
make download-flux-cluster     # to the first node, then rsync to all workers
```

## Video engines (per style)

Each style picks the model that animates its scenes under **Settings → Styles →
Video model**; child styles inherit the parent's choice like every other style
field. Two engines ship:

| Engine | Character | License |
|---|---|---|
| **LTX 2.3 22B** (default) | Fast two-pass render, native audio, honors the per-style video negative prompt | LTX-2 Community License |
| **MiniMax H3 33B** (opt-in) | Much slower single-pass render, higher fidelity, native **stereo** audio; no negative-prompt path | MiniMax H3 Community License |

MiniMax H3 notes:

- **Download** its ~40 GB stack (pruned INT8 transformer, NVFP4 Qwen3-VL 32B text
  encoder, video + audio VAEs) from **Settings → Infrastructure → Video models** —
  it is *not* part of the bulk install.
- Its nodes ship with **ComfyUI itself (≥ v0.30.0)** — rebuild the worker
  containers (`docker/comfyui/` pins `COMFYUI_REF=v0.30.0`) if the engine shows
  "not installed" with the weights already downloaded.
- Generation is capped at ~1 MP (768×1344-class); larger style resolutions render
  at the cap and are reframed back to the plan size. Clips run 4–15 s at 24 fps.
- **License restrictions**: not licensed for use in the USA, EU, UK or South
  Korea; requires machine-generated disclosure and "MiniMax H3" attribution, and
  separate authorization above US$20M yearly revenue. The picker shows the same
  note.

Manual download:

```bash
cd ~/github/ComfyUI
huggingface-cli download Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/MiniMax-H3 text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/MiniMax-H3 vae/minimax_h3_video_vae_fp16.safetensors --local-dir models/vae --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/MiniMax-H3 vae/minimax_h3_audio_vae_fp32.safetensors --local-dir models/vae --local-dir-use-symlinks False
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
an opt-in **non-commercial** preview. MiniMax H3 is opt-in with its own
[community license](#video-engines-per-style) — territory-restricted and
attribution-bearing; review it before switching a publishing style over.

Read [model licensing](tts_licensing.md) and
[THIRD_PARTY_NOTICES.md](https://github.com/pizzato/stephen_spielbot/blob/main/THIRD_PARTY_NOTICES.md)
before monetizing anything you make.
