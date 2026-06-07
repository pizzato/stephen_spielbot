"""Voice-cloned TTS via Chatterbox (Resemble AI).

Runs inference in a dedicated conda env (chatterbox) to avoid PyTorch
version conflicts with the ComfyUI environment.

Usage (standalone):
    python -m pipeline.tts_xtts --ref my_voice.wav --text "Hello" --out speech.wav

Usage (from pipeline):
    generate_narration_cloned(text, reference_wav, output_path)
"""

import os
import subprocess
from pathlib import Path

# Override with CHATTERBOX_PYTHON env var, or fall back to ~/miniconda3/envs/chatterbox/bin/python
CHATTERBOX_PYTHON = os.environ.get(
    "CHATTERBOX_PYTHON",
    str(Path.home() / "miniconda3" / "envs" / "chatterbox" / "bin" / "python"),
)
GENERATE_SCRIPT = Path(__file__).parent.parent / "voice_cloner" / "generate.py"


def generate_narration_cloned(
    text: str,
    reference_wav: Path,
    output_path: Path,
) -> Path:
    """Generate speech matching the voice in reference_wav using Chatterbox."""
    reference_wav = Path(reference_wav)
    if not reference_wav.exists():
        raise RuntimeError(f"Reference audio not found: {reference_wav}")

    result = subprocess.run(
        [
            CHATTERBOX_PYTHON,
            str(GENERATE_SCRIPT),
            "--text", text,
            "--ref", str(reference_wav),
            "--out", str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Chatterbox TTS failed:\n{result.stderr}")

    return output_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Clone voice and synthesise speech.")
    parser.add_argument("--ref", required=True, type=Path, help="Reference WAV (10-30s of your voice)")
    parser.add_argument("--text", required=True, help="Text to synthesise")
    parser.add_argument("--out", required=True, type=Path, help="Output WAV path")
    args = parser.parse_args()

    generate_narration_cloned(args.text, args.ref, args.out)
    print(f"Saved: {args.out}")
