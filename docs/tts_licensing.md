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
the default or used for monetized output; the default stays `openf5`. The same
applies to [Raon-OpenTTS-1B](#raon-opentts-1b--krafton-non-commercial) below. The model
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

## Raon-OpenTTS-1B — KRAFTON (non-commercial)

A second opt-in preview engine, `raon-opentts-1b`: a 1.048B-parameter DiT
flow-matching model KRAFTON trained on 510K hours of *public* English speech.

- **Code:** [`krafton-ai/Raon-OpenTTS`](https://github.com/krafton-ai/Raon-OpenTTS) — Apache-2.0.
- **Weights:** [`KRAFTON/Raon-OpenTTS-1B`](https://huggingface.co/KRAFTON/Raon-OpenTTS-1B)
  — **CC-BY-NC-4.0**, so it carries the same restriction as `f5-original`: A/B
  comparison only, never the default and never monetized output. The training
  data being public does not make the released weights commercial-use.
- **Vocoder:** a 16 kHz HiFi-GAN from
  [`speechbrain/tts-hifigan-libritts-16kHz`](https://huggingface.co/speechbrain/tts-hifigan-libritts-16kHz)
  (Apache-2.0), fetched separately — the fork vendors a standalone loader for
  it, so speechbrain itself is not installed.
- **English only**, and it synthesises at **16 kHz** (the other engines run at
  24 kHz), so it is the lower-bandwidth option of the three.

### Why it needs its own virtualenv

The HuggingFace repo declares `library_name: f5-tts` and ships the same
`config.yaml` / checkpoint / `vocab.txt` trio as OpenF5, which makes it look
like an `openf5`-style `--model_cfg/--ckpt_file/--vocab_file` swap. It is not:
its `sbhifigan16k` mel type is unknown to upstream F5-TTS's `load_vocoder`, and
KRAFTON's fork installs a package under the **same `f5_tts` import name** —
installing it beside `f5-tts` would break `openf5` and `f5-original`. So it is
a third *backend*, in its own virtualenv, always run as a separate process:
`pipeline/raon.py` is the single-utterance entry point the fork lacks (its own
`infer_cli` is a batch evaluation harness). The TTS worker image leaves it out
by default; build with `--build-arg INSTALL_RAON=1` to include it, and point
`RAON_PYTHON` elsewhere if the virtualenv lives somewhere else.

The checkpoint is a ~16 GB training checkpoint (EMA state included), so
pre-warm the engine from Settings rather than paying for the download and the
`torch.load` on the first render.

## Singing-voice conversion (seed-vc)

The [Music-video format](performance_films.md#singing-films-the-music-video-format)'s
"Sing this as *voice*" step is the one true voice-clone in the app: **seed-vc**
re-voices a generated song with a library voice's timbre. It is **GPL-3.0** — installed
by `scripts/install_svc.sh` into its own virtualenv and always invoked as a separate
process, never imported, so the app's Apache-2.0 licensing is unaffected. Its helpers
demucs (MIT) and faster-whisper (MIT) ride in the same install. See
[`THIRD_PARTY_NOTICES.md`](https://github.com/pizzato/stephen_spielbot/blob/main/THIRD_PARTY_NOTICES.md)
for the full terms.

## Scope

This note covers only the TTS narration weights and the singing-voice conversion above.
The reference voice clip used for voice cloning is a separate provenance question and is
**not** addressed here.
