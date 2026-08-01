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
registry lives in [`pipeline/tts_engines.py`](../pipeline/tts_engines.py) and the
OpenF5 source in [`pipeline/openf5.py`](../pipeline/openf5.py); the `OPENF5_REPO`
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
  [`pipeline/chatterbox.py`](../pipeline/chatterbox.py). The `CHATTERBOX_REPO`
  environment variable can point at a mirror or pinned fork but should remain an
  MIT-licensed repository.

## Scope

This note covers only the TTS narration weights. The reference voice clip used for
voice cloning is a separate provenance question and is **not** addressed here.
