#!/usr/bin/env python3
"""Remote TTS runner — reads JSON from stdin, writes WAV bytes to stdout.

Invoked via SSH by tts_worker.py using the f5tts Python interpreter:
  ssh <host> ~/f5tts-env/bin/python .../tts_runner.py

Stdin JSON: {"text": "...", "ref_audio_b64": "<base64>" | null}
Stdout: raw WAV bytes on success
Stderr: error message on failure
"""

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# We are already running inside the f5tts Python environment when invoked via SSH
F5TTS_PYTHON = sys.executable
DEFAULT_REF   = Path(__file__).parent.parent / "assets" / "default_narrator.wav"


def main() -> None:
    data     = json.loads(sys.stdin.buffer.read())
    text     = data["text"]
    ref_b64  = data.get("ref_audio_b64")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.wav"

        if ref_b64:
            ref = Path(tmp) / "ref.wav"
            ref.write_bytes(base64.b64decode(ref_b64))
        else:
            ref = DEFAULT_REF

        result = subprocess.run(
            [
                F5TTS_PYTHON, "-m", "f5_tts.infer.infer_cli",
                "--model",       "F5TTS_v1_Base",
                "--ref_audio",   str(ref),
                "--ref_text",    "",
                "--gen_text",    text,
                "--output_file", str(out),
                "--speed",       "1.0",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            sys.exit(1)

        sys.stdout.buffer.write(out.read_bytes())


if __name__ == "__main__":
    main()
