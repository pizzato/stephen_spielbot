"""OpenF5-TTS-Base weights — the commercially-licensed F5-TTS model source.

Stephen Spielbot generates narration for monetized YouTube/X channels, so the
TTS weights must allow commercial use. The official F5-TTS base weights
(SWivid/F5-TTS, ``F5TTS_v1_Base``) are CC-BY-NC-4.0 — trained on Emilia
in-the-wild — and the maintainers confirm they "cannot be used commercially
also after finetuning" (github.com/SWivid/F5-TTS/discussions/997).

We use ``mrfakename/OpenF5-TTS-Base`` instead: Apache-2.0, trained on
Emilia-YODAS (CC-BY-4.0). Its architecture is identical to ``F5TTS_v1_Base``
(DiT, dim 1024 / depth 22 / heads 16, vocos mel, 24 kHz) so it loads through
the same F5-TTS inference code — pass its config / checkpoint / vocab via
``--model_cfg`` / ``--ckpt_file`` / ``--vocab_file`` instead of ``--model``.

See NOTICE.md for the licensing rationale.
"""

from __future__ import annotations

import os
from functools import lru_cache

# Override for a mirror or a pinned fork via the environment if ever needed.
OPENF5_REPO = os.environ.get("OPENF5_REPO", "mrfakename/OpenF5-TTS-Base")


@lru_cache(maxsize=1)
def ensure_openf5_model() -> tuple[str, str, str]:
    """Fetch the OpenF5-TTS-Base files and return local ``(cfg, ckpt, vocab)`` paths.

    The three files are pulled once into the HuggingFace cache (HF_HOME) and
    reused on every later call — mirroring how F5-TTS used to auto-download the
    SWivid weights on first request, but from the Apache-2.0 repo instead.
    """
    from huggingface_hub import hf_hub_download

    cfg = hf_hub_download(OPENF5_REPO, "config.yaml")
    ckpt = hf_hub_download(OPENF5_REPO, "model.pt")
    vocab = hf_hub_download(OPENF5_REPO, "vocab.txt")
    return cfg, ckpt, vocab


def f5_model_args() -> list[str]:
    """F5-TTS CLI flags selecting the OpenF5-TTS-Base weights.

    Drop-in replacement for the old ``["--model", "F5TTS_v1_Base"]``.
    """
    cfg, ckpt, vocab = ensure_openf5_model()
    return ["--model_cfg", cfg, "--ckpt_file", ckpt, "--vocab_file", vocab]
