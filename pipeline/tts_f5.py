"""F5-TTS narration generation — supports both default voice and voice cloning.

Uses the f5tts conda environment to avoid dependency conflicts.

Usage (standalone):
    python -m pipeline.tts_f5 --text "Hello world." --out speech.wav
    python -m pipeline.tts_f5 --text "Hello world." --ref my_voice.wav --out speech.wav
"""

import os
import subprocess
from pathlib import Path

# Override with F5TTS_PYTHON env var, or fall back to ~/miniconda3/envs/f5tts/bin/python
F5TTS_PYTHON = os.environ.get(
    "F5TTS_PYTHON",
    str(Path.home() / "miniconda3" / "envs" / "f5tts" / "bin" / "python"),
)
DEFAULT_REF   = Path(__file__).parent.parent / "assets" / "default_narrator.mp3"


def generate_narration_f5(
    text: str,
    output_path: Path,
    reference_wav: Path | None = None,
) -> Path:
    """Synthesise speech using F5-TTS.

    Uses reference_wav for voice cloning, or DEFAULT_REF when none is provided.
    """
    ref = Path(reference_wav) if reference_wav else DEFAULT_REF
    if not ref.exists():
        raise RuntimeError(
            f"F5-TTS reference audio not found: {ref}\n"
            "Add a voice in the Config tab or regenerate the default narrator."
        )

    from pipeline.openf5 import f5_model_args  # Apache-2.0 OpenF5-TTS-Base weights

    result = subprocess.run(
        [
            F5TTS_PYTHON, "-m", "f5_tts.infer.infer_cli",
            *f5_model_args(),
            "--ref_audio",  str(ref),
            "--ref_text",   "",        # leave empty — model auto-transcribes
            "--gen_text",   text,
            "--output_file", str(output_path),
            "--speed",      "1.0",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"F5-TTS failed:\n{result.stderr}")

    return output_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="F5-TTS narration synthesis.")
    parser.add_argument("--text", required=True, help="Text to synthesise")
    parser.add_argument("--ref",  type=Path, default=None, help="Reference WAV for voice cloning")
    parser.add_argument("--out",  required=True, type=Path, help="Output WAV path")
    args = parser.parse_args()

    generate_narration_f5(args.text, args.out, reference_wav=args.ref)
    print(f"Saved: {args.out}")
