# Third-party model licensing — TTS narration weights

Stephen Spielbot produces narration for **monetized** YouTube/X channels, so the
text-to-speech weights must permit commercial use.

## What we use

The narration model is **[OpenF5-TTS-Base](https://huggingface.co/mrfakename/OpenF5-TTS-Base)**
(`mrfakename/OpenF5-TTS-Base`):

- **License:** Apache-2.0 — commercial use permitted.
- **Training data:** Emilia-YODAS (CC-BY-4.0, commercial OK).
- **Architecture:** identical to F5-TTS `F5TTS_v1_Base` (DiT, dim 1024 / depth 22 /
  16 heads, vocos mel, 24 kHz), so it loads through the unmodified F5-TTS
  inference code. We pass its `config.yaml` / `model.pt` / `vocab.txt` via the
  F5-TTS CLI's `--model_cfg` / `--ckpt_file` / `--vocab_file` flags.

The runtime engine is still [F5-TTS](https://github.com/SWivid/F5-TTS) (MIT-licensed
code); only the *weights* are swapped.

## The original (non-commercial) weights — opt-in only

The official F5-TTS base weights — **`SWivid/F5-TTS` (`F5TTS_v1_Base`)** — are
licensed **CC-BY-NC-4.0** because they were trained on the Emilia *in-the-wild*
dataset (CC-BY-NC-4.0). The maintainers confirm the non-commercial restriction
survives fine-tuning:

> "CC-BY-NC Emilia trained Base Model cannot be used commercially also after
> finetuning."
> — https://github.com/SWivid/F5-TTS/discussions/997

They are selectable in Settings as the opt-in `f5-original` engine (flagged
**non-commercial**) for A/B quality comparison only — they **must not** be made
the default or used for monetized output; the default stays `openf5`. The model
registry lives in [`pipeline/tts_engines.py`](https://github.com/pizzato/stephen_spielbot/blob/main/pipeline/tts_engines.py) and the
OpenF5 source in [`pipeline/openf5.py`](https://github.com/pizzato/stephen_spielbot/blob/main/pipeline/openf5.py); the `OPENF5_REPO`
environment variable can point at a mirror or pinned fork but should remain an
Apache/CC-BY-licensed repository.

## Chatterbox Multilingual — the multilingual engine (issue #176)

Multilingual narration (23 languages) uses **Chatterbox Multilingual** by
Resemble AI, selectable in Settings as the `chatterbox-multilingual` engine:

- **Code:** [`chatterbox-tts`](https://github.com/resemble-ai/chatterbox) — MIT.
- **Weights:** [`ResembleAI/chatterbox`](https://huggingface.co/ResembleAI/chatterbox)
  (`t3_mtl23ls_v2` + `s3gen` et al.) — MIT, commercial use permitted.
- **Watermark:** Chatterbox embeds Resemble's **Perth** neural watermark in every
  generated clip. We deliberately keep it enabled — the narration is meant to be
  identifiable as synthetic (it complements the robotic-voice measure of issue #52
  and the C2PA content credentials).
- The per-style narration language lives in `tts_language`; the CLI wrapper is
  [`pipeline/chatterbox.py`](https://github.com/pizzato/stephen_spielbot/blob/main/pipeline/chatterbox.py). The `CHATTERBOX_REPO`
  environment variable can point at a mirror or pinned fork but should remain an
  MIT-licensed repository.

## Singing-voice conversion

The [Music-video format](performance_films.md#singing-films-the-music-video-format)'s
**Sing this as…** action re-voices an existing song using the engine selected under
**Settings → Styles → Narrator & audio → Singing voice conversion**.

- **SoulX-Singer SVC** is the default. Its
  [code license](https://github.com/Soul-AILab/SoulX-Singer/blob/main/LICENSE) is
  **Apache-2.0**, and the [publisher's model card](https://huggingface.co/Soul-AILab/SoulX-Singer#license)
  explicitly applies that license to the weights as well. Commercial use is allowed;
  redistributions must retain the license and applicable notices and identify changes.
  Its RMVPE pitch extractor is Apache-2.0. The downloaded
  [Whisper-base checkpoint](https://huggingface.co/openai/whisper-base) is labelled
  Apache-2.0 on its model card; the original OpenAI Whisper project is MIT.
- **Seed-VC** remains selectable. Its
  [code license](https://github.com/Plachtaa/seed-vc/blob/main/LICENSE) is **GPL-3.0**.
  Commercial use is permitted, but distributing the runtime carries GPL obligations.

`make install` places each engine in its own virtual environment and the app invokes
it as a separate process. Demucs (MIT) and faster-whisper (MIT) remain shared helpers
in the Seed-VC environment. Stephen Spielbot's own code remains Apache-2.0; selecting
SoulX does not remove the licensing obligations of Seed-VC included in an installation.
See [`THIRD_PARTY_NOTICES.md`](https://github.com/pizzato/stephen_spielbot/blob/main/THIRD_PARTY_NOTICES.md)
for component licenses and installation details.

## Scope

This note covers only the TTS narration weights and the singing-voice conversion above.
The reference voice clip used for voice cloning is a separate provenance question and is
**not** addressed here.
