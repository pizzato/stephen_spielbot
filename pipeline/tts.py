"""Kokoro TTS client for narration generation."""

import json
import urllib.request
import urllib.error
from pathlib import Path

TTS_URL = "http://127.0.0.1:18790"
DEFAULT_VOICE = "KokHeart00001"


def generate_narration(text: str, output_path: Path, voice_id: str = DEFAULT_VOICE) -> Path:
    """Generate narration audio from text using Kokoro TTS and save to output_path."""
    payload = json.dumps({
        "text": text,
        "model_id": "kokoro",
        "voice_settings": {"stability": 0.5},
    }).encode()

    req = urllib.request.Request(
        f"{TTS_URL}/v1/text-to-speech/{voice_id}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            audio_bytes = resp.read()
    except urllib.error.URLError as e:
        raise RuntimeError(f"TTS request failed: {e}")

    output_path.write_bytes(audio_bytes)
    return output_path
