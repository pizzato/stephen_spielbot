#!/usr/bin/env python3
"""Chatterbox TTS inference — runs inside the chatterbox conda env.

Called as a subprocess by pipeline/tts_xtts.py.
"""

import argparse
import sys
from pathlib import Path

import torch
import soundfile as sf


def _patch_perth() -> None:
    """PerthImplicitWatermarker is None on aarch64 — fall back to the no-op dummy."""
    import perth
    if perth.PerthImplicitWatermarker is None:
        perth.PerthImplicitWatermarker = perth.DummyWatermarker


def main() -> None:
    _patch_perth()
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    parser.add_argument("--ref", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--exaggeration", type=float, default=0.4,
                        help="Voice expressiveness 0-1 (default 0.4)")
    parser.add_argument("--cfg-weight", type=float, default=0.5,
                        help="Guidance strength (default 0.5)")
    args = parser.parse_args()

    from chatterbox.tts import ChatterboxTTS

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = ChatterboxTTS.from_pretrained(device=device)

    wav = model.generate(
        args.text,
        audio_prompt_path=str(args.ref),
        exaggeration=args.exaggeration,
        cfg_weight=args.cfg_weight,
    )
    sf.write(str(args.out), wav.squeeze().cpu().numpy(), model.sr)


if __name__ == "__main__":
    main()
