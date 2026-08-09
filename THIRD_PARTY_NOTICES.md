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
| FLUX.2 Klein 4B (**default** generate + edit) | `Comfy-Org/vae-text-encorder-for-flux-klein-4b` (repackaging of `black-forest-labs/FLUX.2-klein-4B`) | Apache-2.0 (per the upstream Black Forest Labs model card — note the **9B** Klein variants are non-commercial; this project uses the 4B) | ✅ Yes |
| FLUX.1 schnell (legacy, opt-in) | `Comfy-Org/flux1-schnell`, `comfyanonymous/flux_text_encoders`, `black-forest-labs/FLUX.1-schnell` (VAE) | Apache-2.0 | ✅ Yes |

> The non-commercial FLUX engines (FLUX.1 Fill, FLUX.2 dev) have been removed
> from this project precisely to keep generated imagery commercially usable.

## Video model

| Model | Hugging Face repo | License | Commercial? |
|---|---|---|---|
| LTX-Video 2.3 (checkpoint, distilled LoRA, spatial upscaler) | `Lightricks/LTX-2.3-fp8`, `Lightricks/LTX-2.3` | **LTX-2 Community License Agreement** (not an OSI license) | ✅ Free commercial use for entities under **US$10M annual revenue** (all affiliates combined); above that, a paid Commercial Use Agreement with Lightricks is required |
| LTX-2.3 IC-LoRA Pixel Spatial Upscaler (2×/4×) | `Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler` | **LTX-2 Community License Agreement** | Same terms as above |
| Gemma 3 text encoder (required by the LTX graph) | `Comfy-Org/ltx-2` (`gemma_3_12B_it_fp4_mixed`) | Google **Gemma Terms of Use** + Prohibited Use Policy (not an OSI license) | Allowed under Gemma terms; you must comply with the Prohibited Use Policy |
| MiniMax H3 33B (opt-in engine: DiT and Ref2VA sibling checkpoints, Qwen3-VL text encoder, video/audio VAEs) | `Comfy-Org/MiniMax-H3` | **MiniMax H3 Community License** (not an OSI license) | ⚠️ Territory-restricted (not licensed in the USA, EU, UK, South Korea); machine-generated disclosure + "MiniMax H3" attribution required; separate authorization above US$20M yearly revenue |
| faster-whisper + CTranslate2 (performance-shot quality gate, CPU transcription) | `SYSTRAN/faster-whisper` (base.en weights via `Systran/faster-whisper-base.en`) | MIT | ✅ |
| MiniMax H3 w4a8 Ref2VA checkpoint (default performance-film engine; 4-bit weights) | `Kijai/MiniMax-H3-experimental` | Derived from MiniMax H3 — **MiniMax H3 Community License** | ⚠️ Same territory restrictions and attribution as the base H3 weights |
| MiniMax H3 Turbo LoRA (opt-in few-step distillation) | `larryvrh/MiniMax-H3-Turbo-Lora` | Apache-2.0 (the base H3 weights it patches keep the MiniMax H3 Community License) | ✅ LoRA itself yes; output remains bound by the H3 terms above |

## Audio models

| Model | Hugging Face repo | License | Commercial? |
|---|---|---|---|
| ACE-Step 1.5 (music) + Qwen text encoders | `Comfy-Org/ace_step_1.5_ComfyUI_files` | Apache-2.0 | ✅ Yes |
| **OpenF5-TTS-Base** (narration — **default**) | `mrfakename/OpenF5-TTS-Base` | Apache-2.0 | ✅ Yes |
| F5-TTS Base original (narration — opt-in) | `SWivid/F5-TTS` (`F5TTS_v1_Base`) | CC-BY-NC-4.0 | ❌ No (non-commercial) |
| Chatterbox Multilingual (narration — 23-language option) | `ResembleAI/chatterbox` | MIT | ✅ Yes (embeds Resemble's Perth watermark — kept on purpose) |

> Narration uses a **per-style voice-model picker** (`pipeline/tts_engines.py`):
> the default `openf5` is Apache-2.0 and commercial-safe, and the multilingual
> `chatterbox-multilingual` engine is MIT (code and weights); the original SWivid
> `f5-original` weights are CC-BY-NC and offered only as an opt-in "preview"
> (flagged non-commercial in Settings) for A/B quality checks — **don't** select
> it for monetized output. See [`docs/tts_licensing.md`](docs/tts_licensing.md)
> for the TTS-weights rationale. The bundled reference clip
> (`assets/default_narrator.mp3`) provenance is still pending a public-domain
> replacement (tracked TODO).

## Talking-head models (dialogue scenes)

The `echomimic` worker container downloads these on first use
(`pipeline/echomimic_server.py`):

| Model | Hugging Face repo | License | Commercial? |
|---|---|---|---|
| EchoMimic-V3 (flash transformer) | `BadToBest/EchoMimicV3` | Apache-2.0 | ✅ Yes |
| Wan2.1-Fun-V1.1-1.3B-InP (base video model) | `alibaba-pai/Wan2.1-Fun-V1.1-1.3B-InP` | Apache-2.0 | ✅ Yes |
| chinese-wav2vec2-base (audio encoder) | `TencentGameMate/chinese-wav2vec2-base` | MIT | ✅ Yes |

## Runtime tools

- **ComfyUI** — GPL-3.0 (run as a separate service; not linked into this code).
- **FFmpeg** — used as an external binary; your build is typically LGPL/GPL.
  Stephen Spielbot calls it as a subprocess and does not bundle it.

## Code installed into the worker containers

The Dockerfiles under `docker/` clone or pip-install third-party code when
**you** build the images — none of it is part of this repository:

| Project | Installed by | License |
|---|---|---|
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | `docker/comfyui` | GPL-3.0 |
| [ComfyUI-VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite) | `docker/comfyui` | GPL-3.0 |
| [ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo) | `docker/comfyui` (cloned + patched at build time) | LTX-2 Community License Agreement |
| [ComfyUI-MiniMax-H3-Turbo](https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo) | `docker/comfyui` | Apache-2.0 |
| [F5-TTS](https://github.com/SWivid/F5-TTS) (code only) | `docker/tts` | MIT |
| [chatterbox-tts](https://github.com/resemble-ai/chatterbox) | `docker/tts` | MIT |
| [echomimic_v3](https://github.com/antgroup/echomimic_v3) | `docker/echomimic` | Apache-2.0 |

> Running the built images locally is unremarkable. But **publishing** a built
> `spielbot-comfyui` image to a registry distributes GPL-3.0 code (and
> Lightricks-licensed code), which triggers those licenses' source and notice
> obligations — keep built images private unless you're prepared to meet them.

## Bundled assets

- `assets/StephenSpielbot.png` — original artwork for this project.
- `assets/default_narrator.mp3` — reference voice clip, provenance under review
  (see the narration note above).

## Bundled character voice library

`make install` (via `scripts/download_voices.py`) downloads ten ~18-second voice
reference clips carved from **LibriVox** audiobook recordings hosted on
archive.org. LibriVox recordings are released into the **public domain** by
their readers, so the clips carry no **copyright** restrictions, commercial use
included. Note that copyright is not the whole picture for voice *cloning*: the
readers are (or were) real people, and several jurisdictions protect a person's
voice and likeness separately from copyright — that dimension is yours to
assess for your use. Each voice's source recording is recorded in `config.yaml`
(`voices[].source`) for transparency.

## Bundled display fonts (cover typography)

`assets/fonts/` ships eleven display typefaces used to draw cover titles
(`pipeline/cover_typography.py`), fetched from the
[google/fonts](https://github.com/google/fonts) repository. Each family's
directory includes its own licence file. All are commercial-friendly.

| Font | Licence |
|---|---|
| Abril Fatface, Alfa Slab One, Anton, Archivo Black, Bangers, Bebas Neue, Lilita One, Passion One, Titan One | SIL Open Font License 1.1 (`OFL.txt` in each dir) |
| Luckiest Guy | Apache-2.0 (`LICENSE.txt` in its dir) |
| Noto Sans SC Black | SIL Open Font License 1.1 (`OFL.txt` in its dir) |

`notosanssc/NotoSansSC-Black.ttf` is the Chinese/Japanese fallback face — the
other ten are Latin-only, so titles in those scripts would otherwise draw as
empty boxes. It is not the upstream file: the variable
`ofl/notosanssc/NotoSansSC[wght].ttf` was instanced at `wght=900` and subset to
the GB 2312 and JIS X 0208 character sets plus kana, Latin and punctuation
(10,780 characters, 3.5 MB instead of 17 MB) with `fontTools`. The OFL permits
this; the licence file is shipped unchanged alongside it.
