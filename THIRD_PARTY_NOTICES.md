# Third-Party Notices

Stephen Spielbot's own code is Apache-2.0 (see [`LICENSE`](LICENSE)). It does
**not** redistribute any model weights — every model is downloaded by you at
install time from its original host (Hugging Face). Each model carries its own
license, which **you** accept when you download and use it. The notes below are
a convenience summary, **not** legal advice — always check the linked model card.

> **Commercial use matters here.** This tool publishes generated videos to
> potentially monetized YouTube/X channels. A model whose license forbids
> commercial use makes the *output* unsafe to monetize. The defaults below are
> chosen to be commercial-friendly, with the one exception called out in bold.

## Image models (per-style engine — see `pipeline/engines.py`)

| Model | Hugging Face repo | License | Commercial? |
|---|---|---|---|
| FLUX.2 Klein 4B (**default** generate + edit) | `Comfy-Org/vae-text-encorder-for-flux-klein-4b` | Apache-2.0 | ✅ Yes |
| FLUX.1 schnell (legacy, opt-in) | `Comfy-Org/flux1-schnell`, `comfyanonymous/flux_text_encoders`, `black-forest-labs/FLUX.1-schnell` (VAE) | Apache-2.0 | ✅ Yes |

> The non-commercial FLUX engines (FLUX.1 Fill, FLUX.2 dev) have been removed
> from this project precisely to keep generated imagery commercially usable.

## Video model

| Model | Hugging Face repo | License | Commercial? |
|---|---|---|---|
| LTX-Video 2.3 (checkpoint, distilled LoRA, spatial upscaler) | `Lightricks/LTX-2.3-fp8`, `Lightricks/LTX-2.3` | Lightricks LTX-Video license — **review the model card** | Permitted under Lightricks' terms (revenue thresholds may apply) |
| LTX-2.3 IC-LoRA Pixel Spatial Upscaler (2×/4×) | `Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler` | Lightricks LTX-2 community license — **review the model card** | Permitted under Lightricks' terms |
| Gemma 3 text encoder (required by the LTX graph) | `Comfy-Org/ltx-2` (`gemma_3_12B_it_fp4_mixed`) | Google **Gemma Terms of Use** + Prohibited Use Policy (not an OSI license) | Allowed under Gemma terms; you must comply with the Prohibited Use Policy |

## Audio models

| Model | Hugging Face repo | License | Commercial? |
|---|---|---|---|
| ACE-Step 1.5 (music) + Qwen text encoders | `Comfy-Org/ace_step_1.5_ComfyUI_files` | Apache-2.0 | ✅ Yes |
| **OpenF5-TTS-Base** (narration — **default**) | `mrfakename/OpenF5-TTS-Base` | Apache-2.0 | ✅ Yes |
| F5-TTS Base original (narration — opt-in) | `SWivid/F5-TTS` (`F5TTS_v1_Base`) | CC-BY-NC-4.0 | ❌ No (non-commercial) |

> Narration uses a **per-style voice-model picker** (`pipeline/tts_engines.py`):
> the default `openf5` is Apache-2.0 and commercial-safe; the original SWivid
> `f5-original` weights are CC-BY-NC and offered only as an opt-in "preview"
> (flagged non-commercial in Settings) for A/B quality checks — **don't** select
> it for monetized output. See [`NOTICE.md`](NOTICE.md) for the TTS-weights
> rationale. The bundled reference clip (`assets/default_narrator.mp3`) provenance
> is still pending a public-domain replacement (tracked TODO).

## Runtime tools

- **ComfyUI** — GPL-3.0 (run as a separate service; not linked into this code).
- **FFmpeg** — used as an external binary; your build is typically LGPL/GPL.
  Stephen Spielbot calls it as a subprocess and does not bundle it.

## Bundled assets

- `assets/StephenSpielbot.png` — original artwork for this project.
- `assets/default_narrator.mp3` — reference voice clip, provenance under review
  (see the narration note above).
