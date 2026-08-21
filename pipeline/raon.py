"""Raon-OpenTTS-1B — KRAFTON's 1B English narration engine (opt-in).

A 1.048B-parameter DiT flow-matching model trained on 510K hours of *public*
speech (Raon-OpenTTS-Core). It does zero-shot voice cloning from a reference
clip, so the existing voice library carries straight over.

**Non-commercial.** The weights (``KRAFTON/Raon-OpenTTS-1B``) are CC-BY-NC-4.0,
so this engine is opt-in and flagged in Settings exactly like ``f5-original`` —
it must never become the default or narrate monetized output. The *code* is a
separate matter: the fork is Apache-2.0. See docs/tts_licensing.md.

Why this is its own backend rather than an F5 checkpoint swap
-------------------------------------------------------------
The HuggingFace repo advertises ``library_name: f5-tts`` and ships the familiar
``config.yaml`` / checkpoint / ``vocab.txt`` trio, which makes it look like an
``openf5``-style ``--model_cfg/--ckpt_file/--vocab_file`` swap. It is not:

- It runs at **16 kHz / 80 mel** through a HiFi-GAN vocoder the config names
  ``sbhifigan16k``. Upstream F5-TTS's ``load_vocoder`` only knows ``vocos`` and
  ``bigvgan``, so the stock CLI cannot vocode it at all.
- The vocoder weights come from a *second* repo
  (``speechbrain/tts-hifigan-libritts-16kHz``, just ``generator.ckpt``).
- KRAFTON's fork installs its own package under the **same** ``f5_tts`` import
  name. Installing it beside ``f5-tts`` would clobber the ``openf5`` and
  ``f5-original`` engines, so it lives in its own virtualenv and is always run
  as a separate process (the arrangement seed-vc already uses).
- The fork's own ``infer_cli`` is a batch evaluation harness driving a TSV
  manifest of utterances; there is no single-utterance entry point. This module
  is that entry point, assembling the model the way the fork's trainer and
  ``infer_cli`` do and calling its ``infer_process`` once.

So the worker runs ``python -m pipeline.raon`` on the Raon interpreter, the way
it runs ``python -m pipeline.chatterbox`` for Chatterbox. As with that module,
the heavyweight imports live inside ``main()`` so the controller can read the
constants without torch installed.
"""

from __future__ import annotations

import os
from functools import lru_cache

# Override for a mirror or a pinned fork via the environment if ever needed.
RAON_REPO = os.environ.get("RAON_REPO", "KRAFTON/Raon-OpenTTS-1B")

# The model config, the EMA checkpoint and the custom tokenizer vocab. The
# checkpoint is a ~16 GB training checkpoint (EMA + optimizer state), so both
# the download and the ``torch.load`` that reads it are heavy — pre-warm the
# worker rather than paying for it on the first render.
RAON_FILES = ["config.yaml", "model_520000.pt", "vocab.txt"]

# The 16 kHz HiFi-GAN the config's ``sbhifigan16k`` mel type vocodes through.
# The fork vendors a standalone loader for it, so only the generator weights
# are needed (no speechbrain install).
RAON_VOCODER_REPO = os.environ.get(
    "RAON_VOCODER_REPO", "speechbrain/tts-hifigan-libritts-16kHz"
)
RAON_VOCODER_FILES = ["generator.ckpt"]

# Interpreter for the virtualenv holding KRAFTON's fork. Kept out of the
# f5-tts env on purpose: both projects install a package named ``f5_tts``.
RAON_PYTHON = os.environ.get("RAON_PYTHON", "/opt/raon/bin/python")


# Rebuilding is the only way in, so the error says so rather than surfacing a
# bare "No such file or directory" from the subprocess launch.
NOT_INSTALLED = (
    f"Raon-OpenTTS is not installed on this worker ({RAON_PYTHON} missing). "
    "Rebuild the TTS worker image with --build-arg INSTALL_RAON=1, or set "
    "RAON_PYTHON to an interpreter that has KRAFTON's fork installed."
)


def available() -> bool:
    """True if this host has the Raon virtualenv's interpreter."""
    return os.path.isfile(RAON_PYTHON) and os.access(RAON_PYTHON, os.X_OK)


@lru_cache(maxsize=1)
def ensure_raon_model() -> tuple[str, str, str, str]:
    """Fetch Raon's files and return local ``(cfg, ckpt, vocab, vocoder)`` paths.

    Pulled once into the HuggingFace cache (HF_HOME) and reused on every later
    call, mirroring ``pipeline/openf5.py``'s ``ensure_openf5_model``.
    """
    from huggingface_hub import hf_hub_download

    cfg, ckpt, vocab = (hf_hub_download(RAON_REPO, f) for f in RAON_FILES)
    vocoder = hf_hub_download(RAON_VOCODER_REPO, RAON_VOCODER_FILES[0])
    return cfg, ckpt, vocab, vocoder


def main(argv: list[str] | None = None) -> None:
    """Synthesise one narration WAV: the worker-side CLI entry point.

    Runs inside the Raon virtualenv (KRAFTON's fork installed). Model load is
    per-call, like the F5-TTS CLI the other engines use — slower per request,
    but the GPU is shared with ComfyUI so nothing stays resident between calls.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Raon-OpenTTS-1B narration")
    parser.add_argument("--text", required=True)
    parser.add_argument("--ref", required=True, help="reference voice clip to clone")
    parser.add_argument("--out", required=True, help="output WAV path")
    parser.add_argument("--speed", type=float, default=1.0)
    args = parser.parse_args(argv)

    import hydra
    import soundfile as sf
    import torch
    from ema_pytorch import EMA
    from omegaconf import OmegaConf

    # KRAFTON's fork, which occupies the ``f5_tts`` import name in this venv.
    from f5_tts.infer.utils_infer import (
        infer_process,
        load_vocoder,
        preprocess_ref_audio_text,
    )
    from f5_tts.model import CFM
    from f5_tts.model.utils import get_tokenizer

    cfg_path, ckpt_path, vocab_path, vocoder_path = ensure_raon_model()
    model_cfg = OmegaConf.load(cfg_path)
    mel_cfg = model_cfg.model.mel_spec
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # "custom" tokenizer = read vocab.txt in file order, which is how the fork's
    # trainer built the embedding table. We pass the downloaded vocab explicitly
    # because the config names it by the bare relative path "vocab.txt".
    vocab_char_map, vocab_size = get_tokenizer(vocab_path, "custom")

    model = CFM(
        transformer=hydra.utils.get_class(f"f5_tts.model.{model_cfg.model.backbone}")(
            **model_cfg.model.arch,
            text_num_embeds=vocab_size,
            mel_dim=mel_cfg.n_mel_channels,
        ),
        mel_spec_kwargs=mel_cfg,
        vocab_char_map=vocab_char_map,
    ).to(device)

    # The published checkpoint carries only EMA weights; load them through an
    # EMA wrapper and copy them onto the model, as the fork's infer_cli does.
    ckpt = torch.load(ckpt_path, map_location="cpu")
    if "ema_model_state_dict" not in ckpt:
        raise RuntimeError(f"{ckpt_path} has no 'ema_model_state_dict'")
    ema = EMA(model, include_online_model=False).to(device)
    ema.load_state_dict(ckpt["ema_model_state_dict"])
    for key, param in ema.ema_model.state_dict().items():
        model.state_dict()[key].copy_(param)
    model.eval()

    vocoder = load_vocoder(
        vocoder_name=mel_cfg.mel_spec_type,
        is_local=True,
        local_path=os.path.dirname(vocoder_path),
        device=device,
    )

    # Empty ref_text: the fork transcribes the reference clip itself (Whisper),
    # matching how we call the F5-TTS CLI with --ref_text "".
    ref_audio, ref_text = preprocess_ref_audio_text(args.ref, "")
    audio, sample_rate, _ = infer_process(
        ref_audio,
        ref_text,
        args.text,
        model,
        vocoder,
        mel_spec_type=mel_cfg.mel_spec_type,
        speed=max(0.5, min(2.0, args.speed or 1.0)),
        device=device,
    )
    sf.write(args.out, audio, sample_rate)


if __name__ == "__main__":
    main()
