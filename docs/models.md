# Models

`make install` (and `make download-models` on its own) downloads everything
automatically — roughly **90 GB** for the defaults (the gated LTX 2.5 set needs an HF token — see below). Models live on each worker's host at
`~/github/ComfyUI/models` and are mounted into the containers, so they survive image
rebuilds.

!!! danger "`~/github/ComfyUI/models` is the live model store"
    Never delete it as part of a cleanup. `make uninstall --purge-models` only removes a
    ComfyUI directory the installer itself created, and asks first when interactive.

## What gets downloaded

| Model | Size | Used for |
|---|---|---|
| [LTX 2.5](https://huggingface.co/Lightricks/LTX-2.5) | ~40 GB | Scene video generation (default engine; gated repo — needs an HF token) |
| [LTX 2.3](https://huggingface.co/Lightricks/LTX-2.3) | ~28 GB | Keyframed establishing shots + Remix spatial upscalers |
| [FLUX.2 Klein 4B](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b) | ~16 GB | Scene first-frame images and the "Edit image" inpaint |
| [ACE-Step 1.5](https://github.com/ace-step/ACE-Step) | ~5 GB | Background music (default engine) |
| [FlashVSR v1.1](https://huggingface.co/JunhaoZhuang/FlashVSR-v1.1) | ~7 GB | Default finishing / Edit-film upscaler (video super-resolution) |
| Chatterbox Multilingual | ~3.5 GB | Optional multilingual narration (pre-warmed into each TTS worker's HF cache) |
| LibriVox character voices | small | 10 public-domain voices auto-cast onto script characters (`make download-voices`) |

The **OpenF5-TTS-Base** narration weights are fetched by the TTS container on first use.

## Manual download

If you'd rather fetch them yourself:

=== "LTX 2.3 (~28 GB, keyframed shots + upscalers)"

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

=== "LTX 2.5 (~40 GB, default scene engine)"

    Accept the license at [huggingface.co/Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5)
    first — the repo is click-through gated, so every download needs `--token`.

    ```bash
    cd ~/github/ComfyUI
    huggingface-cli download Lightricks/LTX-2.5 diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False --token "$HF_TOKEN"
    huggingface-cli download Lightricks/LTX-2.5 text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False --token "$HF_TOKEN"
    huggingface-cli download Lightricks/LTX-2.5 vae/ltx-2.5-video-vae-bf16.safetensors --local-dir models/vae --local-dir-use-symlinks False --token "$HF_TOKEN"
    huggingface-cli download Lightricks/LTX-2.5 vae/ltx-2.5-audio-vae-bf16.safetensors --local-dir models/vae --local-dir-use-symlinks False --token "$HF_TOKEN"
    huggingface-cli download Lightricks/LTX-2.5 latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors --local-dir models/latent_upscale_models --local-dir-use-symlinks False --token "$HF_TOKEN"
    ```

=== "FlashVSR v1.1 (~7 GB, default upscaler)"

    The ComfyUI node loads the whole folder by name, so every file goes in
    `models/FlashVSR-v1.1/`:

    ```bash
    for f in LQ_proj_in.ckpt TCDecoder.ckpt Wan2.1_VAE.pth diffusion_pytorch_model_streaming_dmd.safetensors config.json model_index.json; do
      huggingface-cli download JunhaoZhuang/FlashVSR-v1.1 "$f" --revision 27561b186ded3402d7c975f4fd722e2885b6135f --local-dir models/FlashVSR-v1.1 --local-dir-use-symlinks False
    done
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

=== "MiniMax Music 3 (~14 GB, opt-in)"

    ```bash
    huggingface-cli download Comfy-Org/MiniMax-Music-3 diffusion_models/minimax_music3_dit_fp16.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
    huggingface-cli download Comfy-Org/MiniMax-Music-3 text_encoders/minimax_music3_text_encoder_pruned_int8_convrot.safetensors --local-dir models/text_encoders --local-dir-use-symlinks False
    huggingface-cli download Comfy-Org/MiniMax-Music-3 vae/minimax_music3_dav.safetensors --local-dir models/vae --local-dir-use-symlinks False
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
field. These engines ship:

| Engine | Character | License |
|---|---|---|
| **LTX 2.5 22B** (default) | Fast two-pass render, native audio, honors the per-style video negative prompt; Gemma 4 encoder for strong prompt adherence, 24 fps. Measured ~26% faster than the LTX 2.3 engine it replaced | LTX-2.x Community License |
| **MiniMax H3 33B** (opt-in) | Much slower single-pass render, higher fidelity, native **stereo** audio; no negative-prompt path | MiniMax H3 Community License |
| **MiniMax H3 33B Turbo** (opt-in, early preview) | H3 with a distilled few-step LoRA (4 steps instead of 15) on the full non-pruned transformer — measured ~1.9× faster per scene | MiniMax H3 Community License (LoRA itself Apache-2.0) |
| **MiniMax H3 33B Ref2VA** (opt-in) | Not a scene I2V engine: takes character portraits and voice clips instead of a first frame and generates picture + spoken dialogue together. Only [performance films](performance_films.md) use it | MiniMax H3 Community License |
| **MiniMax H3 33B Ref2VA Turbo** (opt-in) | The same, with the distilled few-step LoRA — measured ~2.3× faster (10.2 min vs 22.9 min for a 10 s scene at 704×1280 on a GB10) | MiniMax H3 Community License (LoRA itself Apache-2.0) |
| **MiniMax H3 33B Ref2VA Turbo (LightX2V)** (opt-in) | The same again, on LightX2V's 4-step LoRA distilled **on Ref2VA** rather than on ordinary scenes — measured 2× faster than the default w4a8 at equal spoken accuracy, without the over-sharpened distillation look. Softer than the base engine | MiniMax H3 Community License (LoRA itself Apache-2.0) |

LTX 2.5 notes:

- Its ~40 GB stack (int8+convrot distilled transformer, int8 Gemma 4 12B text
  encoder, video + audio VAEs, 2.5 latent spatial upscaler) is part of the bulk
  install, and can also be fetched per worker from **Settings → Infrastructure
  → Video models**. [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5)
  is a click-through repo: accept its license on Hugging Face and configure an
  HF token before downloading.
- It replaced LTX 2.3 as the scene engine outright (same license terms, ~26%
  faster at production size); configs still naming `ltx23` fall back to it
  automatically. The 2.3 checkpoint stays installed for the keyframed
  establishing shots and the film editor's upscalers.
- Native support ships with **ComfyUI itself (≥ v0.32.0)** — rebuild the worker
  containers (`docker/comfyui/` pins `COMFYUI_REF=v0.33.0`) if the engine shows
  "not installed" with the weights already downloaded. Older workers refuse the
  render rather than failing mid-graph.
- Clips render at **24 fps** (2.3 ran at 25) through the same two-pass
  half-resolution → 2× latent upscale graph, so the Render quality first/second
  pass knobs and the per-style video negative prompt apply as before. The
  distilled transformer replaces 2.3's distill LoRA, so the LoRA-strength knob
  has no effect on this engine.
- Same license family as 2.3: free commercial use under US$10M annual revenue,
  no territory restrictions.

MiniMax H3 notes:

- **Download** its ~40 GB stack (pruned INT8 transformer, NVFP4 Qwen3-VL 32B text
  encoder, video + audio VAEs) from **Settings → Infrastructure → Video models** —
  it is *not* part of the bulk install.
- Its nodes ship with **ComfyUI itself (≥ v0.30.0)** — rebuild the worker
  containers (`docker/comfyui/` pins `COMFYUI_REF=v0.33.0`) if the engine shows
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

### Chained scenes

H3 renders at most ~15 s in one pass, and that is what caps a scene. Turning on
**Settings → Styles → Chained scenes** renders each scene as two clips joined by
[H3 Motion Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context):
the second continues the first's motion and audio instead of cutting, so a scene
can run to about 29 s.

The script is planned to match, which is the point of the toggle — the same
runtime becomes **fewer, longer scenes**, each carrying roughly twice the
content. A four-minute narrated film goes from ~20 scenes of ~30 words to ~10
scenes of ~58 words; total narration is unchanged.

It covers both kinds of scene, on different gates:

- **Narrated scenes** chain when the style's *video engine* is MiniMax. LTX
  narrated scenes ignore the toggle — LTX continues clips natively.
- **Acted scenes** (dialogue, rendered through Ref2VA) chain on the toggle
  alone — reference engines are always MiniMax, so even an LTX-narrated style
  chains its dialogue. The script budget doubles (~6 lines / ~45 spoken words
  per dialogue scene instead of 3 / 22), exchanges that used to split into two
  scenes stay one continuous take, and a scene that still fits one clip renders
  single-clip rather than paying the join overhead for nothing.
- **Silent scenes** chain too where the style
  [performs them](performance_films.md#silent-scenes-performed). A silent beat
  has no lines to divide, so what splits is its *window*: the writer is asked
  for ~19 s instead of ~10, and the scene's timed beats are dealt out to the
  clip whose window they fall in (re-based to start at zero) rather than all
  landing in the first — otherwise the second clip has nothing to do but hold
  the frame. A film's scene count is planned at the longer take, so the runtime
  you ask for is still the runtime you get.

Measured on a GB10 worker:

- The join reads as movement, not an edit — 6.17 RMSE against a 6.53 p90 for
  ordinary adjacent frames, where an unrelated cut measures 21.68.
- It costs about **22 % more render time per delivered second**. Each clip after
  the first pins frames to carry motion across; they arrive at its head and are
  trimmed before concatenation, so it samples more than it delivers. The planner
  already accounts for this (`cadence.CHAIN_JOIN_SECS`).
- Audio is the weaker half. One chained dialogue take came back with a 1.2 s run
  of digital silence shortly after the join, longer than any pause in the
  unchained clip. Review the sound on chained films before publishing.

Workers need the nodes baked in — build with `H3_MOTION_CONTEXT_REF` set (see
`docker/README.md`). Without them ComfyUI rejects the chained graph outright
rather than quietly rendering an unchained clip.

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
  1.23×**; turbo the same clip, 230.4 s → 195.5 s, 1.18×. Two unchanged workers
  rendering the same job landed within 2.4 % of each other, so the gap is the
  kernel, not the machine. The catch is that the output is a *different take* —
  7.8 % mean pixel RMSE at 15 steps, 15.2 % at turbo's 4 — so a mixed fleet
  renders mixed video and it should go to every worker or none. Enable it per
  worker with `COMFYUI_EXTRA_ARGS`; see
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

# performance films — the DEFAULT w4a8 (4-bit, needs ComfyUI >= 0.31.0):
huggingface-cli download Kijai/MiniMax-H3-experimental minimax_h3_ref2va_pruned_w4a8_mixed.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False

# performance films — minimax-h3-ref (pruned) and minimax-h3-ref-turbo (full):
huggingface-cli download Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False
huggingface-cli download Comfy-Org/MiniMax-H3 diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors --local-dir models/diffusion_models --local-dir-use-symlinks False

# performance films — minimax-h3-ref-turbo-lx2v (same full DiT, Ref2VA-trained LoRA):
huggingface-cli download lightx2v/Minimax-h3-Turbo minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors --local-dir models/loras --local-dir-use-symlinks False
```

!!! note "That last LoRA needs its keys renamed"
    LightX2V ship generic-ComfyUI key names (`diffusion_model.blocks.N.…`) and
    the H3 turbo node maps bare ones (`blocks.N.…`). Loaded as downloaded it
    matches nothing and the scene renders at 4 steps with **no LoRA applied** —
    no error, just a bad take. Installing from Settings → Infrastructure does
    the rename for you; by hand, run `h3_ref2v_lx2v_fixup` from
    `scripts/download_models.sh`, which rewrites the header and drops the
    original.

## Music engines (per style)

Each style picks the model that writes its background bed under **Settings →
Styles → Narrator & audio → Music model** (shown only when music is on); child
styles inherit the choice like every other style field. Download either engine
per worker from **Settings → Infrastructure → Music models**.

| Engine | Character | License |
|---|---|---|
| **ACE-Step 1.5 Turbo** (default) | ~5 GB, 8 sampling steps, seconds per film. Instrumental beds from a tag list, any length | Apache-2.0 |
| **MiniMax Music 3** (opt-in) | ~14 GB, an 8B autoregressive pass plus 30 DiT steps. Song-shaped and higher fidelity, capped at 6 minutes. Measured on a GB10 worker: **83 s for a 30 s bed** | MiniMax-Music3 Community License |

MiniMax Music 3 notes:

- Its nodes ship with **ComfyUI itself (≥ v0.33.0)** — rebuild the worker
  containers (`docker/comfyui/` pins `COMFYUI_REF=v0.33.0`) if the engine shows
  "not installed" with the weights already downloaded. Renders are refused
  up-front on an older worker rather than failing mid-graph.
- The film's **music description** becomes the model's *caption*. It reads best
  as a structured brief — genre and tempo, then mood, then instrumentation — and
  should say *instrumental* explicitly, since the model is trained to write songs
  with vocals. A background bed is generated with empty lyrics; a **Music-video
  film** is the exception — its `song.json` lyrics are passed through and the
  model SINGS them (see
  [singing films](performance_films.md#singing-films-the-music-video-format)).
- It stops at **6 minutes** (the model is trained to ~5). A longer film gets a
  shorter bed, and the final mix loops it rather than leaving the tail silent —
  the same safety net covers a song the model chooses to end early.
- The autoregressive stage runs at ~15.7 audio frames/s on a GB10 (25 frames =
  1 s of music), so cost scales with the bed's length: a 30 s bed measured 83 s
  end to end, and a two-minute film's bed lands around four minutes. ACE-Step
  does the same job in seconds — this is the trade for the fidelity.
- **License restrictions**: requires a visible "MiniMax-Music3" credit on any
  commercial product using it, and machine-generated disclosure; separate
  authorization above US$20M yearly revenue. There is no territory restriction
  (unlike MiniMax H3). The picker shows the same note.

## Gated weights

Some Hugging Face repos require accepting a license. Set a **Hugging Face token** in
**Settings → Infrastructure → Image models** and the workers use it to auto-download the
gated engine weights.

## Upscaler modes

Upscale targets run to **4K** (3840×2160 landscape, 2160×3840 portrait, 2160×2160
square) plus QHD — larger than any render resolution, since these are finishing sizes
the engines cannot generate at directly. A film can still be *aimed* at QHD/4K from the
Create screen or a style's render quality: it renders at FHD and the pipeline runs the
finishing upscale automatically (the style's **Finishing upscaler** picks the mode).
Measured on a GB10: the H3 latent upscaler reached 4096×4096 from a 1024×1024 source
in ~13 min for a 6 s clip.

The Edit film screen's final-video upscale has five modes; **FlashVSR** is the default:

| Mode | What it does |
|---|---|
| **FlashVSR** | [FlashVSR](https://github.com/OpenImagingLab/FlashVSR) v1.1 — one-step diffusion *video* super-resolution (Wan2.1-based, Apache-2.0), run in its `tiny` streaming mode through [ComfyUI-FlashVSR_Ultra_Fast](https://github.com/lihaoyun6/ComfyUI-FlashVSR_Ultra_Fast). A true pixel upscaler, not a latent resize: it recovers texture (stone, foliage, signage) without the latent modes' artefacts, keeps colour, and returns exactly the source frame count. Runs at **2×** unless the target needs more than 2.5× the source, then **4×**; the remainder is a plain resample. On a GB10 a 5 s 1280×704 scene takes ~4 min at 2×; big outputs (above ~1 G output pixel-frames — long clips, or 4×) go through the node's tiling and take several times longer, and an untiled run that still exhausts the worker is retried tiled |
| **Fast** | Plain ffmpeg scale |
| **LTX latent** | Simple model path: `LTXVLatentUpsampler` + `ltx-2.3-spatial-upscaler-x2-1.1` |
| **LTX IC-LoRA** | Generative [Pixel Spatial Upscaler](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler) (2×/4× IC-LoRA via ComfyUI-LTXVideo) |
| **H3 latent** | Encodes with MiniMax H3's video VAE, resizes the 24-channel latent with the [H3 latent upscaler](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler), decodes (any factor up to 4×). ~2 min per 5 s scene on a GB10. H3's VAE pads clips to its 17k+5 latent grid, so the decode is trimmed back to the source length — otherwise every scene would run long and desync the captions |

Each upscale is kept as a selectable final-video version, so you can switch back to the
original at any time.

!!! warning "H3 latent upscaler — keep the `.pth` off your workers"

    The community node lists **every** file in `models/latent_upscale_models/` in its
    model dropdown, and loads anything that isn't a `.safetensors` through
    `torch.load(weights_only=False)` — which executes arbitrary code from the file.
    Spielbot only ever names the `bf16.safetensors` checkpoint, so the pipeline never
    takes that path, and `scripts/download_models.sh` installs only that file, pinned
    to a specific revision. If you download the repo's `fp32.pth` by hand, do not put
    it in that folder — selecting it in ComfyUI's own UI would run whatever is pickled
    inside it.

## Licensing

The defaults — FLUX.2 Klein, LTX-Video, ACE-Step, and the OpenF5 narration model — are
commercial-friendly on purpose. The original F5-TTS narration weights are offered only as
an opt-in **non-commercial** preview. MiniMax H3 is opt-in with its own
[community license](#video-engines-per-style) — territory-restricted and
attribution-bearing; review it before switching a publishing style over. MiniMax
Music 3 is opt-in under a [separate community license](#music-engines-per-style)
— no territory restriction, but it wants a visible "MiniMax-Music3" credit.

Read [model licensing](tts_licensing.md) and
[THIRD_PARTY_NOTICES.md](https://github.com/pizzato/stephen_spielbot/blob/main/THIRD_PARTY_NOTICES.md)
before monetizing anything you make.
