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
| LTX-Video 2.5 (default scene engine: distilled transformer, Gemma-4-based text encoder, video/audio VAEs, latent spatial upscaler) | `Lightricks/LTX-2.5` (click-through gated) | **LTX-2.x Community License Agreement** (not an OSI license); the bundled Gemma 4 encoder also carries Google's **Gemma Terms of Use** | ✅ Free commercial use for entities under **US$10M annual revenue**; above that, a paid Commercial Use Agreement with Lightricks is required |
| Gemma 3 text encoder (required by the LTX graph) | `Comfy-Org/ltx-2` (`gemma_3_12B_it_fp4_mixed`) | Google **Gemma Terms of Use** + Prohibited Use Policy (not an OSI license) | Allowed under Gemma terms; you must comply with the Prohibited Use Policy |
| MiniMax H3 33B (opt-in engine: DiT and Ref2VA sibling checkpoints, Qwen3-VL text encoder, video/audio VAEs) | `Comfy-Org/MiniMax-H3` | **MiniMax H3 Community License** (not an OSI license) | ⚠️ Territory-restricted (not licensed in the USA, EU, UK, South Korea); machine-generated disclosure + "MiniMax H3" attribution required; separate authorization above US$20M yearly revenue |
| faster-whisper + CTranslate2 (performance-shot quality gate, CPU transcription) | `SYSTRAN/faster-whisper` (base.en weights via `Systran/faster-whisper-base.en`) | MIT | ✅ |
| MiniMax H3 w4a8 Ref2VA checkpoint (default performance-film engine; 4-bit weights) | `Kijai/MiniMax-H3-experimental` | Derived from MiniMax H3 — **MiniMax H3 Community License** | ⚠️ Same territory restrictions and attribution as the base H3 weights |
| MiniMax H3 Turbo LoRA (opt-in few-step distillation) | `larryvrh/MiniMax-H3-Turbo-Lora` | Apache-2.0 (the base H3 weights it patches keep the MiniMax H3 Community License) | ✅ LoRA itself yes; output remains bound by the H3 terms above |

## Audio models

| Model | Hugging Face repo | License | Commercial? |
|---|---|---|---|
| ACE-Step 1.5 (music — **default**) + Qwen text encoders | `Comfy-Org/ace_step_1.5_ComfyUI_files` | Apache-2.0 | ✅ Yes |
| MiniMax Music 3 (music — opt-in engine: DiT, pruned text encoder, audio VAE) | `Comfy-Org/MiniMax-Music-3` (repackaged from `MiniMaxAI/MiniMax-Music3`) | **MiniMax-Music3 Community License** (not an OSI license) | ⚠️ Yes, with conditions: a prominently displayed "MiniMax-Music3" credit on any commercial product using it, machine-generated disclosure, and separate authorization above US$20M yearly revenue. No territory restriction |
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

## Runtime tools

- **ComfyUI** — GPL-3.0 (run as a separate service; not linked into this code).
- **FFmpeg** — used as an external binary; your build is typically LGPL/GPL.
  Stephen Spielbot calls it as a subprocess and does not bundle it.
- **seed-vc** (singing-voice conversion — the song panel's "Sing this as
  [voice]" step) — GPL-3.0. Installed on the controller by
  `scripts/install_svc.sh`, and inside each worker's ComfyUI container by the
  image build (or `make svc-install`), where the diffusion actually runs. It is
  invoked as a **separate process** (`pipeline/svc.py`), never imported or
  bundled; its model weights download from Hugging Face on first use. Fine for
  self-hosted use; consider the GPL terms before redistributing an installation
  that includes it.
- **demucs** (vocal-stem separation, so a re-voicing converts the voice and not
  the arrangement, and so a music video's vocal timing is measured on the stem)
  — MIT, its `htdemucs` weights released under the same
  license. Installed into the same seed-vc virtualenv and run as a separate
  process; when it is missing, the conversion falls back to the whole mix.
- **faster-whisper** (word-timestamp transcription of the vocal stem, used to
  align a music video's lyric sheet to the sung track — `song_align_lyrics`) —
  MIT, built on CTranslate2 (MIT); the `Systran/faster-whisper-small` weights
  (MIT, converted from OpenAI Whisper) download from Hugging Face on the first
  alignment. Installed into the same seed-vc virtualenv and run as a separate
  process; when it is missing, the energy-based timing measurement is used.

## Code installed into the worker containers

The Dockerfiles under `docker/` clone or pip-install third-party code when
**you** build the images — none of it is part of this repository:

| Project | Installed by | License |
|---|---|---|
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | `docker/comfyui` | GPL-3.0 |
| [ComfyUI-VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite) | `docker/comfyui` | GPL-3.0 |
| [ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo) | `docker/comfyui` (cloned + patched at build time) | LTX-2 Community License Agreement |
| [ComfyUI-MiniMax-H3-Turbo](https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo) | `docker/comfyui` | Apache-2.0 |
| [ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context) | `docker/comfyui` (pinned; omit with an empty `H3_MOTION_CONTEXT_REF`) | GPL-3.0 |
| [seed-vc](https://github.com/Plachtaa/seed-vc) (song re-voicing; run with `docker exec`, never imported) | `docker/comfyui` (cloned into `/opt/seed-vc`; `make svc-install` adds it to older containers) | GPL-3.0 |
| [F5-TTS](https://github.com/SWivid/F5-TTS) (code only) | `docker/tts` | MIT |
| [chatterbox-tts](https://github.com/resemble-ai/chatterbox) | `docker/tts` | MIT |

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
