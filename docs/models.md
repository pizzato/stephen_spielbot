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
field. Three engines ship:

| Engine | Character | License |
|---|---|---|
| **LTX 2.3 22B** (default) | Fast two-pass render, native audio, honors the per-style video negative prompt | LTX-2 Community License |
| **MiniMax H3 33B** (opt-in) | Much slower single-pass render, higher fidelity, native **stereo** audio; no negative-prompt path | MiniMax H3 Community License |
| **MiniMax H3 33B Turbo** (opt-in, early preview) | H3 with a distilled few-step LoRA (4 steps instead of 15) on the full non-pruned transformer — measured ~1.9× faster per scene | MiniMax H3 Community License (LoRA itself Apache-2.0) |
| **MiniMax H3 33B Ref2VA** (opt-in) | Not a scene I2V engine: takes character portraits and voice clips instead of a first frame and generates picture + spoken dialogue together. Only [performance films](performance_films.md) use it | MiniMax H3 Community License |
| **MiniMax H3 33B Ref2VA Turbo** (opt-in) | The same, with the distilled few-step LoRA — measured ~2.3× faster (10.2 min vs 22.9 min for a 10 s scene at 704×1280 on a GB10) | MiniMax H3 Community License (LoRA itself Apache-2.0) |

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

Turbo variant notes:

- The [Turbo LoRA](https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora) only
  fits the **non-pruned** transformer, so the turbo engine downloads the full
  **31 GB** int8 DiT instead of the 19 GB pruned one (encoder and VAEs are
  shared with the base H3 engine). Each step runs the bigger model, so the
  speedup only materializes at low step counts. Measured on a GB10 worker
  (704×1280, 12 s scene) against the base engine's 23 min: **4 steps = 12.1 min
  (~1.9×, the default)**, 8 steps = 22.6 min (parity, but non-pruned fidelity).
- It needs the **ComfyUI-MiniMax-H3-Turbo** custom nodes (the LoRA is sampled on
  dual video/audio flow schedules) — they are baked into the worker image, so
  rebuild the containers if the engine shows "not installed".
- **Steps are tunable per style** (Settings → Styles → Sampling steps, shown for
  the MiniMax engines; 0 = engine default). Each step costs ~2.5 min per scene
  on a GB10, so 4 → 12 min and 8 → 23 min; raise it only if content shows
  softness.
- The current checkpoint is an **early, under-trained preview**. Newer
  checkpoints are expected upstream; swap the `lora` filename in
  `pipeline/engines.py` to pick one up.

Speed knobs that sit outside the engine picker:

- The base H3 workflow already runs **EasyCache** (`reuse_threshold` 0.2), which
  skips DiT steps whose latent barely moved. It only pays off when there are
  steps to skip, so it is wired into the base engine's 15-step path and *not*
  into turbo's 4-step one — at 4 steps there is nothing left to reuse. Community
  cache nodes (TeaCache, FirstBlockCache) are alternative implementations of the
  same trick, not additions: they must not be stacked with EasyCache.
- **SageAttention** is compiled into the worker image but off by default. It is a
  quantised attention kernel, so it stacks with caching and helps at *any* step
  count — including turbo. Measured on GB10 workers (704×1280, 124 frames,
  15 steps, EasyCache 0.2, same seed both sides): **421.8 s → 343.0 s warm,
  1.23×**; 440.3 s → 367.9 s cold, 1.20×. Two unchanged workers rendering the
  same job landed within 2.4 % of each other, so the gap is the kernel, not the
  machine. Enable it per worker with `COMFYUI_EXTRA_ARGS`; see
  [SageAttention](https://github.com/pizzato/stephen_spielbot/blob/main/docker/README.md#sageattention-opt-in)
  in the worker README for the flag, the quality caveat, and the rollback.

Ref2VA notes ([performance films](performance_films.md)):

- The Ref2VA checkpoints are siblings of the I2V ones at the same sizes and
  quantisation, sharing the encoder and both VAEs — so adding them costs one
  21 GB download (or 34 GB for the turbo variant), not a second full stack.
- Turbo needs the **non-pruned** checkpoint for the same reason the I2V turbo
  does: the pruned files drop `time_embedder` entirely and bake
  `adaln_proj.linear.weight` to F16, and `adaln_proj` is exactly what the LoRA
  adapts.
- The graph needs `MiniMaxH3ReferenceToVideo`, present since ComfyUI v0.30.0
  (the pinned worker ref), so no image rebuild is required for the base engine.

Manual download:

```bash
cd ~/github/ComfyUI
huggingface-cli download Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/MiniMax-H3 text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/MiniMax-H3 vae/minimax_h3_video_vae_fp16.safetensors --local-dir models/vae --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/MiniMax-H3 vae/minimax_h3_audio_vae_fp32.safetensors --local-dir models/vae --local-dir-use-symlinks False

# minimax-h3-turbo extras (full non-pruned DiT + distillation LoRA):
huggingface-cli download Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
huggingface-cli download larryvrh/MiniMax-H3-Turbo-Lora minimax_h3_turbo_4step_ckpt500.safetensors --local-dir models/loras --local-dir-use-symlinks False

# performance films — minimax-h3-ref (pruned) and minimax-h3-ref-turbo (full):
huggingface-cli download Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
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
