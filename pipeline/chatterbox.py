"""Chatterbox Multilingual TTS — the multilingual narration engine (issue #176).

Resemble AI's Chatterbox Multilingual synthesises expressive speech in 23
languages with zero-shot voice cloning from a reference clip, so the existing
voice library carries straight over. Code (``chatterbox-tts``) and weights
(``ResembleAI/chatterbox``) are both MIT — commercial use OK (see docs/tts_licensing.md).
Every generated clip carries Resemble's Perth neural watermark, which we keep:
the output is meant to be identifiable as synthetic.

Unlike the F5-TTS engines this is a different inference backend, not a
checkpoint swap: the worker runs this module as a CLI
(``python -m pipeline.chatterbox``) instead of ``f5_tts.infer.infer_cli``.
The heavyweight imports live inside ``main()`` so the controller can import
the constants without torch installed.
"""

from __future__ import annotations

import os

# Override for a mirror or a pinned fork via the environment if ever needed.
CHATTERBOX_REPO = os.environ.get("CHATTERBOX_REPO", "ResembleAI/chatterbox")

# What ChatterboxMultilingualTTS.from_pretrained() downloads (its
# snapshot_download allow_patterns) — kept in sync so is_cached/prewarm in
# pipeline/tts_engines.py reflect what inference will actually fetch.
CHATTERBOX_FILES = [
    "ve.pt",
    "t3_mtl23ls_v2.safetensors",
    "s3gen.pt",
    "grapheme_mtl_merged_expanded_v1.json",
    "conds.pt",
    "Cangjie5_TC.json",
]

# The model's 23 supported languages (chatterbox.mtl_tts.SUPPORTED_LANGUAGES).
# Mirrored here so the Settings UI can list them without importing torch.
LANGUAGES: dict[str, str] = {
    "ar": "Arabic",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "fi": "Finnish",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "ms": "Malay",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ru": "Russian",
    "sv": "Swedish",
    "sw": "Swahili",
    "tr": "Turkish",
    "zh": "Chinese",
}


def norm_language(code: str | None) -> str:
    """Coerce a language code to a supported one, falling back to English."""
    code = (code or "").strip().lower()
    return code if code in LANGUAGES else "en"


def main(argv: list[str] | None = None) -> None:
    """Synthesise one narration WAV: the worker-side CLI entry point.

    Runs inside the TTS worker env (chatterbox-tts installed). Model load is
    per-call, like the F5-TTS CLI the other engines use — slower per request,
    but the GPU is shared with ComfyUI so nothing stays resident between calls.
    """
    import argparse
    import shutil
    import subprocess

    parser = argparse.ArgumentParser(description="Chatterbox Multilingual TTS")
    parser.add_argument("--text", required=True)
    parser.add_argument("--language", default="en")
    parser.add_argument("--ref", required=True, help="reference voice clip to clone")
    parser.add_argument("--out", required=True, help="output WAV path")
    parser.add_argument("--speed", type=float, default=1.0)
    args = parser.parse_args(argv)

    import torch
    import torchaudio
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = ChatterboxMultilingualTTS.from_pretrained(device=device)
    wav = model.generate(
        args.text,
        language_id=norm_language(args.language),
        audio_prompt_path=args.ref,
    )
    torchaudio.save(args.out, wav, model.sr)

    # Chatterbox has no native pacing control (F5's --speed); approximate it
    # with an ffmpeg atempo pass. This changes the WAV's duration, which is
    # fine: scene timing is measured from the finished narration file.
    speed = max(0.5, min(2.0, args.speed or 1.0))
    if abs(speed - 1.0) >= 0.01:
        ffmpeg = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"
        tmp = args.out + ".speed.wav"
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-i", args.out, "-af", f"atempo={speed}", "-c:a", "pcm_s16le", tmp],
            check=True,
        )
        os.replace(tmp, args.out)


if __name__ == "__main__":
    main()
