"""EchoMimic-V3 talking-head worker client (mirrors pipeline/tts_worker.py).

Turns a reference portrait + a speech WAV into a lip-synced talking-head MP4 by
POSTing to a containerized EchoMimic-V3 worker (pipeline/echomimic_server.py).
Used to render dialogue/performance scenes; the narration path never calls this.

Workers are containers reached over HTTP, so ``echomimic_workers`` entries must be
http:// URLs; a bare hostname is rejected (same rule as the TTS worker, issue #12).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# Per-clip ceiling. A talking clip's cost scales with resolution × frames — a
# ~13s line on a 720p-portrait frame legitimately takes ~70 min on a GB10, so
# the old 30-min default killed real renders mid-flight.
_TIMEOUT = int(os.environ.get("ECHOMIMIC_TIMEOUT", "7200"))  # seconds per clip
_FPS = 25


def frames_for_audio(duration_seconds: float, max_frames: int = 1500) -> int:
    """Frame count (at 25 fps) to cover ``duration_seconds`` of audio, clamped.

    EchoMimic caps the clip at min(audio, video_length); we pass a frame count that
    covers the line, bounded so a stray long clip can't run away (1500 ≈ 60 s)."""
    n = int(round(max(0.1, duration_seconds) * _FPS)) + 1
    return max(9, min(n, max_frames))


def animate(
    image_path: Path,
    audio_path: Path,
    output_path: Path,
    host: str,
    prompt: str = "A person is speaking to the camera, natural facial expression.",
    steps: int = 8,
    size: int = 768,
    video_length: int = 81,
    width: int = 0,
    height: int = 0,
) -> Path:
    """Generate a lip-synced talking clip and write it to ``output_path``.

    ``host`` must be an http(s):// EchoMimic worker URL (issue #12). ``video_length``
    is a frame count at 25 fps — size it to the line's audio via ``frames_for_audio``.
    ``width``/``height`` set the output dimensions (e.g. the scene frame's aspect);
    when omitted the clip is ``size``×``size`` square.
    """
    if not host.startswith(("http://", "https://")):
        raise RuntimeError(
            f"EchoMimic worker must be an http:// container URL (e.g. http://host:8190); "
            f"got bare host {host!r}. Set echomimic_workers to http:// URLs."
        )
    payload = json.dumps({
        "image_b64": base64.b64encode(Path(image_path).read_bytes()).decode(),
        "audio_b64": base64.b64encode(Path(audio_path).read_bytes()).decode(),
        "prompt": prompt,
        "steps": int(steps),
        "size": int(size),
        "width": int(width),
        "height": int(height),
        "video_length": int(video_length),
    }).encode()

    req = urllib.request.Request(
        host.rstrip("/") + "/animate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            Path(output_path).write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"EchoMimic failed on {host} ({exc.code}):\n{detail[:1000]}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"EchoMimic worker unreachable at {host}: {exc.reason}")
    return Path(output_path)


def worker_alive(host: str, timeout: int = 3) -> bool:
    """True if an EchoMimic worker is reachable (for the Settings health display)."""
    if host in ("localhost", "127.0.0.1"):
        return True
    if host.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(host.rstrip("/") + "/health", timeout=timeout):
                return True
        except Exception:
            return False
    return False
