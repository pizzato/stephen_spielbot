"""Distributed TTS — runs F5-TTS locally or on remote hosts via SSH."""

import base64
import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("video_gen")

# Override with F5TTS_PYTHON env var, or fall back to ~/miniconda3/envs/f5tts/bin/python
_LOCAL_PYTHON  = os.environ.get(
    "F5TTS_PYTHON",
    str(Path.home() / "miniconda3" / "envs" / "f5tts" / "bin" / "python"),
)
# ~/f5tts-env is the install path on remote workers (created by install_comfyui_worker.sh)
_REMOTE_PYTHON = os.environ.get("F5TTS_REMOTE_PYTHON", "~/f5tts-env/bin/python")

_RUNNER = str(Path(__file__).parent / "tts_runner.py")
DEFAULT_REF = Path(__file__).parent.parent / "assets" / "default_narrator.wav"


def _f5_local(text: str, ref: Path, output_path: Path) -> None:
    result = subprocess.run(
        [
            _LOCAL_PYTHON, "-m", "f5_tts.infer.infer_cli",
            "--model",       "F5TTS_v1_Base",
            "--ref_audio",   str(ref),
            "--ref_text",    "",
            "--gen_text",    text,
            "--output_file", str(output_path),
            "--speed",       "1.0",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"F5-TTS failed:\n{result.stderr}")


def _f5_remote(text: str, ref: Path, output_path: Path, host: str) -> None:
    payload = json.dumps({
        "text": text,
        "ref_audio_b64": base64.b64encode(ref.read_bytes()).decode() if ref != DEFAULT_REF else None,
    }).encode()

    # Sync tts_runner.py to remote
    subprocess.run(
        ["rsync", "-q", _RUNNER, f"{host}:{_RUNNER}"],
        check=True,
    )

    proc = subprocess.run(
        ["ssh", host, f"{_REMOTE_PYTHON} {_RUNNER}"],
        input=payload,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Remote F5-TTS failed on {host}:\n{proc.stderr.decode()}")
    output_path.write_bytes(proc.stdout)


def generate_narration(
    text: str,
    output_path: Path,
    reference_wav: Path | None = None,
    host: str = "localhost",
) -> Path:
    """Generate narration audio, running F5-TTS on host (localhost or remote)."""
    ref = reference_wav or DEFAULT_REF
    if not ref.exists():
        raise RuntimeError(f"TTS reference audio not found: {ref}")

    logger.info("TTS on %s: %r", host, text[:60])
    if host in ("localhost", "127.0.0.1"):
        _f5_local(text, ref, output_path)
    else:
        _f5_remote(text, ref, output_path, host)
    return output_path
