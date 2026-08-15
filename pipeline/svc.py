"""Zero-shot singing-voice conversion — "Sing this as [voice]".

seed-vc re-voices a SUNG track as any library voice from its ~10 s reference
clip: melody, timing and words are kept, only the timbre changes. It is the
one true voice-clone in the singing pipeline — the music engines can only be
*described* a vocalist, never given one.

The conversion runs on the VOCAL STEM, not the whole mix: demucs separates
vocals from instruments first, seed-vc converts just the voice, and the
converted vocals are remixed over the untouched backing. Converting a full
mix re-voices the instruments too — measured on a real track, that turns the
arrangement into vocal-ish noise. When demucs is missing the whole-mix path
still runs (acceptable for a-cappella-leaning tracks) with a warning.

Runs on the controller (Apple Silicon MPS; a 15 s song converts in a few
minutes) from the install `scripts/install_svc.sh` lays down at
``~/.local/share/video-generator/seed-vc``. Model weights (seed-vc and
demucs) download from Hugging Face on the first conversion.

seed-vc is GPL-3.0 (see THIRD_PARTY_NOTICES.md) — it is invoked as a separate
process, never imported. demucs (MIT) lives in the same venv.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("video_gen")

SVC_DIR = Path.home() / ".local" / "share" / "video-generator" / "seed-vc"


def available() -> bool:
    """Is seed-vc installed on this controller?"""
    return ((SVC_DIR / "inference.py").exists()
            and (SVC_DIR / ".venv" / "bin" / "python").exists())


def convert_song(source: Path, voice_ref: Path, output: Path,
                 diffusion_steps: int = 50, timeout: int = 3600) -> Path:
    """Re-voice *source* (a sung track) with the timbre of *voice_ref*.

    Separates the vocal stem, converts only it, and remixes it (level-matched
    to the original vocals) over the untouched instruments. Writes the result
    to *output* and returns it.
    """
    if not available():
        raise RuntimeError(
            "seed-vc is not installed — run scripts/install_svc.sh on the "
            "controller first.")
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        stems = _separate_stems(source, work)
        if stems is None:
            logger.warning("[svc] demucs unavailable — converting the WHOLE "
                           "mix (instruments will smear; fine only for "
                           "a-cappella-leaning tracks)")
            _convert(source, voice_ref, output, diffusion_steps, timeout)
            _normalize_loudness(output)
            return output
        vocals, backing = stems
        converted = work / "converted_vocals.wav"
        _convert(vocals, voice_ref, converted, diffusion_steps, timeout)
        # The converted stem comes back much quieter than the original
        # (~18 dB measured) — match it to the level the original vocals sat
        # at in the mix, then lay it back over the untouched backing.
        _match_gain(converted, to=_mean_volume(vocals))
        _remix(converted, backing, output)
    logger.info("[svc] re-voiced %s with %s → %s (vocal stem)", source.name,
                voice_ref.name, output.name)
    return output


def _convert(source: Path, voice_ref: Path, output: Path,
             diffusion_steps: int, timeout: int) -> None:
    """One seed-vc inference call. Raises with the converter's own stderr
    tail on failure — "CUDA out of memory" and "weights still downloading"
    need different fixes and both look like "conversion failed" otherwise."""
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        proc = subprocess.run(
            [str(SVC_DIR / ".venv" / "bin" / "python"), "inference.py",
             "--source", str(source),
             "--target", str(voice_ref),
             "--output", str(out_dir),
             "--diffusion-steps", str(diffusion_steps),
             "--length-adjust", "1.0",
             "--inference-cfg-rate", "0.7",
             # f0 conditioning is what makes it a SINGING conversion — without
             # it the melody flattens toward speech.
             "--f0-condition", "True"],
            cwd=SVC_DIR, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
            raise RuntimeError("seed-vc failed: " + " | ".join(tail))
        wavs = sorted(out_dir.glob("*.wav"))
        if not wavs:
            raise RuntimeError("seed-vc produced no output file")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wavs[0], output)


def _separate_stems(source: Path, work: Path) -> tuple[Path, Path] | None:
    """demucs two-stem split → (vocals, backing), or None when unavailable."""
    demucs = SVC_DIR / ".venv" / "bin" / "demucs"
    if not demucs.exists():
        return None
    proc = subprocess.run(
        [str(demucs), "--two-stems", "vocals", "-n", "htdemucs",
         "-o", str(work / "stems"), str(source)],
        capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-4:]
        raise RuntimeError("demucs failed: " + " | ".join(tail))
    hits = list((work / "stems").glob("htdemucs/*/vocals.wav"))
    if not hits:
        raise RuntimeError("demucs produced no vocal stem")
    vocals = hits[0]
    backing = vocals.with_name("no_vocals.wav")
    if not backing.exists():
        raise RuntimeError("demucs produced no backing stem")
    return vocals, backing


def _ffmpeg() -> str:
    from pipeline.assembler import _resolve_media_tool
    return _resolve_media_tool("ffmpeg")


def _mean_volume(path: Path) -> float:
    """A track's mean level in dB (ffmpeg volumedetect)."""
    proc = subprocess.run(
        [_ffmpeg(), "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr or "")
    if not m:
        raise RuntimeError(f"could not measure loudness of {path.name}")
    return float(m.group(1))


def _match_gain(path: Path, to: float) -> None:
    """Gain *path* so its mean level matches *to* dB (in place)."""
    gain = to - _mean_volume(path)
    if abs(gain) < 0.5:
        return
    tmp = path.with_suffix(".gain.wav")
    subprocess.run(
        [_ffmpeg(), "-y", "-v", "error", "-i", str(path),
         "-af", f"volume={gain:.1f}dB", str(tmp)],
        check=True, capture_output=True)
    tmp.replace(path)


def _remix(vocals: Path, backing: Path, output: Path) -> None:
    """Converted vocals over the untouched backing, at their own levels."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".remix.wav")
    subprocess.run(
        [_ffmpeg(), "-y", "-v", "error",
         "-i", str(vocals), "-i", str(backing),
         "-filter_complex",
         "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[a]",
         "-map", "[a]", "-ar", "44100", str(tmp)],
        check=True, capture_output=True)
    tmp.replace(output)


def _normalize_loudness(path: Path) -> None:
    """Bring a whole-mix conversion to streaming loudness (-16 LUFS).

    Only the no-demucs fallback needs this — the stem path level-matches the
    converted vocals against the originals instead."""
    tmp = path.with_suffix(".norm.wav")
    subprocess.run(
        [_ffmpeg(), "-y", "-v", "error", "-i", str(path),
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100",
         str(tmp)],
        check=True, capture_output=True)
    tmp.replace(path)
