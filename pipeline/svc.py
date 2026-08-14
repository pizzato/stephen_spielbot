"""Zero-shot singing-voice conversion — "Sing this as [voice]".

seed-vc re-voices a SUNG track as any library voice from its ~10 s reference
clip: melody, timing and words are kept, only the timbre changes. It is the
one true voice-clone in the singing pipeline — the music engines can only be
*described* a vocalist, never given one.

Runs on the controller (Apple Silicon MPS; a 15 s song converts in a few
minutes) from the install `scripts/install_svc.sh` lays down at
``~/.local/share/video-generator/seed-vc``. Model weights download from
Hugging Face on the first conversion.

Best on vocal-forward tracks: the converter re-voices the WHOLE mix, so dense
instrumentation smears — captions that keep the voice up front clone best.
(A vocal-stem separation pass is the known upgrade path.)

seed-vc is GPL-3.0 (see THIRD_PARTY_NOTICES.md) — it is invoked as a separate
process, never imported.
"""
from __future__ import annotations

import logging
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

    Writes the converted wav to *output* and returns it. Raises with the
    converter's own stderr tail on failure — the caller surfaces it verbatim,
    because "CUDA out of memory" and "weights still downloading" need
    different fixes and both look like "conversion failed" otherwise.
    """
    if not available():
        raise RuntimeError(
            "seed-vc is not installed — run scripts/install_svc.sh on the "
            "controller first.")
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
    logger.info("[svc] re-voiced %s with %s → %s", source.name, voice_ref.name,
                output.name)
    return output
