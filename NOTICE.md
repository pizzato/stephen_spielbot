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

## What we deliberately do NOT use

The official F5-TTS base weights — **`SWivid/F5-TTS` (`F5TTS_v1_Base`)** — are
licensed **CC-BY-NC-4.0** because they were trained on the Emilia *in-the-wild*
dataset (CC-BY-NC-4.0). The maintainers confirm the non-commercial restriction
survives fine-tuning:

> "CC-BY-NC Emilia trained Base Model cannot be used commercially also after
> finetuning."
> — https://github.com/SWivid/F5-TTS/discussions/997

Those weights are therefore **not** valid for this project's monetized output and
must not be reintroduced as the default. The model source is centralised in
[`pipeline/openf5.py`](pipeline/openf5.py); the `OPENF5_REPO` environment variable
can point at a mirror or pinned fork but should remain an Apache/CC-BY-licensed
repository.

## Scope

This note covers only the TTS narration weights. The reference voice clip used for
voice cloning is a separate provenance question and is **not** addressed here.
