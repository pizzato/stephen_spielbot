"""TTS — runs F5-TTS locally or on a containerized HTTP worker (issue #12)."""

import base64
import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("video_gen")

DEFAULT_REF = Path(__file__).parent.parent / "assets" / "default_narrator.wav"


def _find_local_python() -> str:
    """Find the conda f5tts env python, checking common install locations."""
    if "F5TTS_PYTHON" in os.environ:
        return os.environ["F5TTS_PYTHON"]
    for base in [
        Path.home() / "opt" / "anaconda3",
        Path.home() / "opt" / "miniconda3",
        Path.home() / "opt" / "miniforge3",
        Path.home() / "anaconda3",
        Path.home() / "miniconda3",
        Path.home() / "miniforge3",
        Path("/opt/conda"),
    ]:
        candidate = base / "envs" / "f5tts" / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    return str(Path.home() / "miniconda3" / "envs" / "f5tts" / "bin" / "python")


_LOCAL_PYTHON = _find_local_python()


_TTS_TIMEOUT = int(os.environ.get("TTS_TIMEOUT", "300"))  # seconds per narration


def _resolve_ffmpeg() -> str:
    """Locate ffmpeg (used for the robotic voice effect)."""
    found = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(candidate).exists():
            return candidate
    return "ffmpeg"


# How strongly to robotize narration, 0.0 (natural) .. 1.0 (harsh metallic monotone).
# It's the fraction of the phase-zeroed signal blended over the natural voice; lower
# values keep more natural prosody so the robot reads as subtle. Tune to taste.
_ROBOT_AMOUNT = 0.35


def _robotize_wav(path: Path, amount: float | None = None) -> None:
    """Apply a subtle 'robot voice' effect to a narration WAV, in place.

    Blends a phase-zeroed copy of the spectrum back over the natural voice at
    _ROBOT_AMOUNT, all within one ffmpeg afftfilt pass. Zeroing the phase is the
    classic robotization (it flattens prosody into a synthetic monotone); mixing
    it only partially keeps the voice clearly synthetic and not mistaken for a
    human (issue #52) without the harsh metallic buzz of full phase removal. Per
    bin the output is ((1-a)*re, (1-a)*im + a*|X|), the natural<->full-robot
    blend, which at a=1 reduces exactly to the original effect. Spectral, so it
    preserves duration: downstream muxing aligns audio to video by length, which
    must not change.
    """
    a = max(0.0, min(1.0, _ROBOT_AMOUNT if amount is None else amount))
    dry, wet = round(1.0 - a, 3), round(a, 3)
    af = (
        f"afftfilt=real='{dry}*re':imag='{dry}*im+{wet}*hypot(re,im)':"
        "win_size=512:overlap=0.75"
    )
    tmp = path.with_suffix(path.suffix + ".robot.wav")
    try:
        subprocess.run(
            [
                _resolve_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(path),
                "-af", af,
                "-c:a", "pcm_s16le", str(tmp),
            ],
            capture_output=True, text=True, timeout=_TTS_TIMEOUT, check=True,
        )
        tmp.replace(path)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Robotic voice effect timed out")
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Robotic voice effect failed:\n{e.stderr}")


def _f5_local(text: str, ref: Path, output_path: Path, speed: float = 1.0,
              tts_engine: str = "openf5") -> None:
    from pipeline import tts_engines  # selectable narration model (per style)
    try:
        result = subprocess.run(
            [
                _LOCAL_PYTHON, "-m", "f5_tts.infer.infer_cli",
                *tts_engines.cli_args(tts_engine),
                "--ref_audio",   str(ref),
                "--ref_text",    "",
                "--gen_text",    text,
                "--output_file", str(output_path),
                "--speed",       str(speed),
            ],
            capture_output=True,
            text=True,
            timeout=_TTS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"F5-TTS timed out after {_TTS_TIMEOUT}s (local)")
    if result.returncode != 0:
        raise RuntimeError(f"F5-TTS failed:\n{result.stderr}")


def _f5_http(text: str, ref: Path, output_path: Path, url: str, speed: float = 1.0,
             tts_engine: str = "openf5") -> None:
    """POST narration to a containerized F5-TTS HTTP worker (pipeline/tts_server.py).

    The TTS worker runs as a container with no SSH access. The request hits
    <url>/tts and the WAV bytes come back in the response body. A default
    reference is sent as null so the server uses its own bundled narrator.
    tts_engine selects which narration model the worker loads (per style).
    """
    payload = json.dumps({
        "text": text,
        "ref_audio_b64": base64.b64encode(ref.read_bytes()).decode() if ref != DEFAULT_REF else None,
        "speed": speed,
        "engine": tts_engine,
    }).encode()

    req = urllib.request.Request(
        url.rstrip("/") + "/tts",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TTS_TIMEOUT) as resp:
            output_path.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"Remote F5-TTS failed on {url} ({exc.code}):\n{detail}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Remote F5-TTS unreachable at {url}: {exc.reason}")


def worker_alive(host: str, timeout: int = 3) -> bool:
    """Return True if a TTS worker is reachable, for the Settings health display.

    Mirrors generate_narration's transport routing: an http(s):// worker is probed
    at its /health endpoint (pipeline/tts_server.py); localhost runs F5-TTS in
    process, so it has no endpoint to probe and is reported available; a bare
    hostname is a config error (rejected at render time) and reported down.
    """
    if host in ("localhost", "127.0.0.1"):
        return True
    if host.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(host.rstrip("/") + "/health", timeout=timeout):
                return True
        except Exception:
            return False
    return False


def generate_narration(
    text: str,
    output_path: Path,
    reference_wav: Path | None = None,
    host: str = "localhost",
    robotic: bool = False,
    robotic_amount: float | None = None,
    speed: float | None = None,
    tts_engine: str = "openf5",
) -> Path:
    """Generate narration audio, running F5-TTS on host.

    host selects the transport:
      * "localhost" / "127.0.0.1" — run F5-TTS locally
      * "http://…" / "https://…"  — POST to a containerized F5-TTS worker (issue #12)

    Workers are containers reached over HTTP, so tts_workers must be http:// URLs;
    a bare hostname is rejected.

    When robotic is set, post-process the result into a robotic monotone so the
    voice is not mistaken for a human (issue #52). The effect runs locally on
    the produced WAV, so remote TTS hosts need no extra tooling.

    speed is F5-TTS's speaking pace (1.0 natural, lower slower); clamped to a
    range the model handles gracefully.

    tts_engine selects the narration model (see pipeline/tts_engines.py); it is
    carried per style and threaded through to the F5-TTS CLI / worker.
    """
    ref = reference_wav or DEFAULT_REF
    if not ref.exists():
        raise RuntimeError(f"TTS reference audio not found: {ref}")

    engine = tts_engine or "openf5"
    speed = max(0.3, min(2.0, float(speed))) if speed else 1.0
    logger.info("TTS on %s [%s]%s: %r", host, engine, " (robotic)" if robotic else "", text[:60])
    if host in ("localhost", "127.0.0.1"):
        _f5_local(text, ref, output_path, speed, engine)
    elif host.startswith(("http://", "https://")):
        _f5_http(text, ref, output_path, host, speed, engine)
    else:
        raise RuntimeError(
            f"TTS worker must be an http:// container URL (e.g. http://host:8189); "
            f"got bare host {host!r}. Set tts_workers to http:// URLs (issue #12)."
        )
    if robotic:
        _robotize_wav(output_path, robotic_amount)
    return output_path
