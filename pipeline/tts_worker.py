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

DEFAULT_REF = Path(__file__).parent.parent / "assets" / "default_narrator.mp3"


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
              tts_engine: str = "openf5", language: str = "en") -> None:
    from pipeline import tts_engines  # selectable narration model (per style)
    if tts_engines.backend(tts_engine) == "chatterbox":
        # Different inference stack: the multilingual Chatterbox CLI
        # (pipeline/chatterbox.py) instead of the F5-TTS one. Run from the repo
        # root so `-m pipeline.chatterbox` resolves regardless of caller cwd.
        cmd = [
            _LOCAL_PYTHON, "-m", "pipeline.chatterbox",
            "--text",     text,
            "--language", language,
            "--ref",      str(ref),
            "--out",      str(output_path),
            "--speed",    str(speed),
        ]
        cwd = str(Path(__file__).parent.parent)
    else:
        cmd = [
            _LOCAL_PYTHON, "-m", "f5_tts.infer.infer_cli",
            *tts_engines.cli_args(tts_engine),
            "--ref_audio",   str(ref),
            "--ref_text",    "",
            "--gen_text",    text,
            "--output_file", str(output_path),
            "--speed",       str(speed),
        ]
        cwd = None
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TTS_TIMEOUT,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"TTS timed out after {_TTS_TIMEOUT}s (local)")
    if result.returncode != 0:
        raise RuntimeError(f"TTS failed:\n{result.stderr}")


def _f5_http(text: str, ref: Path, output_path: Path, url: str, speed: float = 1.0,
             tts_engine: str = "openf5", language: str = "en") -> None:
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
        "language": language,
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


def _trim_trailing_artifacts(wav_path: Path, rel_loud_db: float = 12.0,
                             window_secs: float = 0.1, pad_secs: float = 0.4,
                             min_gap_secs: float = 1.2,
                             max_babble_secs: float = 3.5) -> None:
    """Cut Chatterbox's post-speech junk off the end of a narration WAV.

    Chatterbox Multilingual often appends junk after the last word: quiet
    babble and clicks, or a babble swell nearly as loud as the speech itself
    after a second-plus of near-silence. Scene videos are stretched to
    narration length, so the junk becomes an audible noise at the end of
    every localized scene.

    Loudness is judged RELATIVE to the take's own speech level (the 90th-
    percentile window RMS): voices land anywhere from -17 dB to -29 dB speech
    RMS, so a fixed absolute threshold reads a quiet voice's speech as
    silence and shreds it. A window within *rel_loud_db* of the speech level
    counts as loud.

    Two junk shapes are cut:
      * a tail with no loud window in it (quiet babble/clicks) — cut
        *pad_secs* after the last loud window, keeping the natural decay;
      * a babble swell separated from the narration by a quiet stretch of
        *min_gap_secs* or more (a lone one-window click doesn't split the
        stretch). Real pauses can run past 2 s, but they resume with several
        seconds of actual narration, while trailing babble is short — so a
        stretch only counts as the junk boundary when less than
        *max_babble_secs* of loud audio remains after it.
    Best-effort — on anything unexpected the file is left untouched.
    """
    import array
    import math
    import wave

    try:
        with wave.open(str(wav_path), "rb") as w:
            if w.getsampwidth() != 2:
                return
            rate = w.getframerate()
            channels = w.getnchannels()
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        samples = array.array("h", frames)
        if not samples or not rate:
            return
        win = max(1, int(rate * window_secs)) * channels
        rms_db = []
        for start in range(0, len(samples), win):
            chunk = samples[start:start + win]
            rms = math.sqrt(sum(x * x for x in chunk) / len(chunk))
            rms_db.append(20 * math.log10(rms / 32768.0) if rms else -120.0)
        speech_db = sorted(rms_db)[int(len(rms_db) * 0.9)]
        loud = [db >= speech_db - rel_loud_db for db in rms_db]
        if not any(loud):
            return  # no speech found at all — don't guess
        # Quiet stretches, merged across single-window clicks.
        stretches = []
        i, n = 0, len(loud)
        while i < n:
            if loud[i]:
                i += 1
                continue
            j = i
            while j < n and not loud[j]:
                j += 1
            if stretches and i - stretches[-1][1] == 1:
                stretches[-1] = (stretches[-1][0], j)
            else:
                stretches.append((i, j))
            i = j
        cut_win = max(k for k in range(n) if loud[k])  # default: quiet-tail cut
        gap_wins = max(1, int(min_gap_secs / window_secs))
        for a, b in stretches:
            if a == 0 or b - a < gap_wins:
                continue
            loud_after = sum(1 for k in range(a, n) if loud[k])
            if loud_after * window_secs < max_babble_secs:
                cut_win = max(k for k in range(a) if loud[k])
                break
        keep = min(len(samples), (cut_win + 1) * win + int(rate * pad_secs) * channels)
        if len(samples) - keep < int(rate * 0.25) * channels:
            return  # tail already tight — nothing worth rewriting
        trimmed = samples[:keep]
        tmp = wav_path.with_suffix(".trim.wav")
        with wave.open(str(tmp), "wb") as w:
            w.setparams(params)
            w.writeframes(trimmed.tobytes())
        tmp.replace(wav_path)
        logger.info("Trimmed %.1fs of trailing TTS artifacts from %s",
                    (len(samples) - keep) / (rate * channels), wav_path.name)
    except Exception as exc:
        logger.warning("Trailing-artifact trim skipped for %s: %s", wav_path.name, exc)


def generate_narration(
    text: str,
    output_path: Path,
    reference_wav: Path | None = None,
    host: str = "localhost",
    robotic: bool = False,
    robotic_amount: float | None = None,
    speed: float | None = None,
    tts_engine: str = "openf5",
    language: str = "en",
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

    language is the narration language (ISO 639-1); only the multilingual
    chatterbox backend uses it — the F5 engines ignore it.
    """
    ref = reference_wav or DEFAULT_REF
    if not ref.exists():
        raise RuntimeError(f"TTS reference audio not found: {ref}")

    engine = tts_engine or "openf5"
    language = language or "en"
    speed = max(0.3, min(2.0, float(speed))) if speed else 1.0
    logger.info("TTS on %s [%s/%s]%s: %r", host, engine, language,
                " (robotic)" if robotic else "", text[:60])
    if host in ("localhost", "127.0.0.1"):
        _f5_local(text, ref, output_path, speed, engine, language)
    elif host.startswith(("http://", "https://")):
        _f5_http(text, ref, output_path, host, speed, engine, language)
    else:
        raise RuntimeError(
            f"TTS worker must be an http:// container URL (e.g. http://host:8189); "
            f"got bare host {host!r}. Set tts_workers to http:// URLs (issue #12)."
        )
    if engine == "chatterbox-multilingual":
        # Chatterbox tends to append quiet babble after the last word; the F5
        # engines don't, so only its output gets the trim.
        _trim_trailing_artifacts(output_path)
    if robotic:
        _robotize_wav(output_path, robotic_amount)
    return output_path
