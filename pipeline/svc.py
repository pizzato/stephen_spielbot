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

The diffusion runs on whichever GPU worker is free — every worker's ComfyUI
container carries seed-vc, so a re-voicing is picked up like any other UI
job (idle worker first). Stem separation, level-matching and the remix stay
on the controller, which needs the install `scripts/install_svc.sh` lays
down at ``~/.local/share/video-generator/seed-vc``; that install is also the
fallback when no worker can take it (Apple Silicon MPS, ~12x real time).
Model weights (seed-vc and demucs) download from Hugging Face on the first
conversion — on the controller and on each worker.

seed-vc is GPL-3.0 (see THIRD_PARTY_NOTICES.md) — it is invoked as a separate
process, never imported. demucs (MIT) lives in the same venv.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("video_gen")

SVC_DIR = Path.home() / ".local" / "share" / "video-generator" / "seed-vc"


def available() -> bool:
    """Is seed-vc installed on this controller?"""
    return ((SVC_DIR / "inference.py").exists()
            and (SVC_DIR / ".venv" / "bin" / "python").exists())


def _host_of(entry: str) -> str:
    """Worker host for a config entry: http://HOST:PORT → HOST, HOST:PORT → HOST.

    Empty for anything ssh would read as an option (a leading '-') — worker
    entries come from saved config and end up in an ssh argv."""
    e = (entry or "").strip()
    if "://" in e:
        host = urllib.parse.urlparse(e).hostname or ""
    else:
        host = e.split("/")[0].split(":")[0]
    return "" if host.startswith("-") else host


def candidate_workers(cfg: dict) -> list[str]:
    """Worker hosts that can run the diffusion, best first.

    Any worker will do — every ComfyUI container carries seed-vc — so the
    pick is just "who is free": idle workers first, then least-busy, the
    same ordering the UI uses to route cover jobs. Each is tried in turn and
    the controller is the last resort, so a host that is down, or whose
    container predates the seed-vc image, only costs the next one a moment.

    ``svc_worker`` in the config pins one host instead of the whole fleet.
    """
    pinned = _host_of(str(cfg.get("svc_worker") or ""))
    if pinned:
        return [pinned]
    urls = [u for u in (cfg.get("comfy_workers") or []) if u]
    try:
        from pipeline.worker_pool import idle_workers
        urls = idle_workers(urls, timeout=2)
    except Exception:
        pass  # nothing reachable (or no workers at all) — order as configured
    hosts: list[str] = []
    for url in urls:
        host = _host_of(url)
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def convert_song(source: Path, voice_ref: Path, output: Path,
                 diffusion_steps: int = 30, timeout: int = 3600,
                 workers: Sequence[str] = ()) -> Path:
    """Re-voice *source* (a sung track) with the timbre of *voice_ref*.

    Separates the vocal stem, converts only it, and remixes it (level-matched
    to the original vocals) over the untouched instruments. Writes the result
    to *output* and returns it.

    *diffusion_steps* trades speed for polish: seed-vc's own default is 25;
    30 is this pipeline's default, 50 the high-quality setting (config key
    ``svc_diffusion_steps``). *workers* are candidate GPU worker hosts (see
    ``candidate_workers``), tried in order — a CUDA GB10 converts near real
    time where the controller's Apple GPU runs at ~12x real time. Separation
    and remixing stay local whoever converts (they are the cheap part), and
    when no worker takes it the controller does rather than the song failing.
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
            _convert_anywhere(source, voice_ref, output, diffusion_steps,
                              timeout, workers)
            _normalize_loudness(output)
            return output
        vocals, backing = stems
        converted = work / "converted_vocals.wav"
        _convert_anywhere(vocals, voice_ref, converted, diffusion_steps,
                          timeout, workers)
        # The converted stem comes back much quieter than the original
        # (~18 dB measured) — match it to the level the original vocals sat
        # at in the mix, then lay it back over the untouched backing.
        _match_gain(converted, to=_mean_volume(vocals))
        _remix(converted, backing, output)
    logger.info("[svc] re-voiced %s with %s → %s (vocal stem)", source.name,
                voice_ref.name, output.name)
    return output


def separate_vocals(source: Path, output: Path) -> Path | None:
    """Just the vocal stem of *source*, written to *output*.

    The same demucs pass a re-voicing opens with, exposed on its own so vocal
    TIMING can be measured on the stem — on a separated stem silence is real
    silence, where the full mix cannot tell a loud instrument from a voice.
    Runs on the controller like every separation here (it is the cheap part).
    Returns None when demucs is not installed (scripts/install_svc.sh lays it
    down beside seed-vc)."""
    with tempfile.TemporaryDirectory() as td:
        stems = _separate_stems(source, Path(td))
        if stems is None:
            return None
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stems[0], output)
    return output


def _convert_anywhere(source: Path, voice_ref: Path, output: Path,
                      diffusion_steps: int, timeout: int,
                      workers: Sequence[str]) -> None:
    """The diffusion pass, on the first worker that will take it — falling
    back to the controller when none does, because a slow conversion beats a
    failed one."""
    for worker in workers:
        host = (worker or "").strip()
        if not host:
            continue
        try:
            _convert_remote(host, source, voice_ref, output,
                            diffusion_steps, timeout)
            return
        except Exception as e:
            logger.warning("[svc] conversion on %s failed (%s) — trying the "
                           "next worker", host, e)
    if workers:
        logger.warning("[svc] no worker took the conversion — running on the "
                       "controller (slow)")
    _convert(source, voice_ref, output, diffusion_steps, timeout)


# Where docker/comfyui/Dockerfile (and scripts/install_svc_worker.sh, for
# containers built before it) lays seed-vc down INSIDE the worker's ComfyUI
# container — it reuses the container's CUDA torch via a system-site-packages
# venv.
_REMOTE_DIR = "/opt/seed-vc"
_REMOTE_CONTAINER = "spielbot-worker-comfyui-1"
# A worker that is down should cost the next one a moment, not a TCP timeout.
_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


def _is_local(host: str) -> bool:
    """Is this host the controller itself? (single-machine setup — same
    hostnames worker.sh manages without SSH)."""
    return host in ("localhost", "127.0.0.1", "::1")


def _convert_remote(host: str, source: Path, voice_ref: Path, output: Path,
                    diffusion_steps: int, timeout: int) -> None:
    """Run the seed-vc diffusion inside *host*'s worker container (CUDA).

    Plain scp + docker cp + docker exec — the same transport worker.sh uses,
    local commands and all when the worker is this machine. Files travel by
    copy: the audio is seconds of wav, the diffusion is the minutes, so the
    copies are noise."""
    def _sh(args, step, tmo=120):
        proc = subprocess.run(args, capture_output=True, text=True, timeout=tmo)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            raise RuntimeError(f"{step}: " + " | ".join(tail))
        return proc

    def _on_host(cmd: str) -> list[str]:
        if _is_local(host):
            return ["bash", "-c", cmd]
        return ["ssh", *_SSH_OPTS, "--", host, cmd]

    def _push(local: Path, remote: str) -> list[str]:
        if _is_local(host):
            return ["cp", str(local), remote]
        return ["scp", "-q", *_SSH_OPTS, str(local), f"{host}:{remote}"]

    def _pull(remote: str, local: Path) -> list[str]:
        if _is_local(host):
            return ["cp", remote, str(local)]
        return ["scp", "-q", *_SSH_OPTS, f"{host}:{remote}", str(local)]

    job = f"svc_{os.getpid()}_{source.stem}"
    _sh(_push(source, f"/tmp/{job}_src.wav"), "copy source")
    _sh(_push(voice_ref, f"/tmp/{job}_ref.wav"), "copy voice ref")
    _sh(_on_host(
        f"docker cp /tmp/{job}_src.wav {_REMOTE_CONTAINER}:/tmp/{job}_src.wav && "
        f"docker cp /tmp/{job}_ref.wav {_REMOTE_CONTAINER}:/tmp/{job}_ref.wav"),
        "stage into container")
    _sh(_on_host(
        f"docker exec {_REMOTE_CONTAINER} {_REMOTE_DIR}/.venv/bin/python "
        f"{_REMOTE_DIR}/inference.py "
        f"--source /tmp/{job}_src.wav --target /tmp/{job}_ref.wav "
        f"--output /tmp/{job}_out --diffusion-steps {diffusion_steps} "
        f"--length-adjust 1.0 --inference-cfg-rate 0.7 --f0-condition True"),
        "remote inference", tmo=timeout)
    _sh(_on_host(
        f"docker exec {_REMOTE_CONTAINER} sh -c "
        f"'cp /tmp/{job}_out/*.wav /tmp/{job}_done.wav' && "
        f"docker cp {_REMOTE_CONTAINER}:/tmp/{job}_done.wav /tmp/{job}_done.wav"),
        "collect output")
    output.parent.mkdir(parents=True, exist_ok=True)
    _sh(_pull(f"/tmp/{job}_done.wav", output), "copy result")
    subprocess.run(_on_host(
        f"rm -f /tmp/{job}_src.wav /tmp/{job}_ref.wav /tmp/{job}_done.wav; "
        f"docker exec {_REMOTE_CONTAINER} rm -rf /tmp/{job}_src.wav "
        f"/tmp/{job}_ref.wav /tmp/{job}_out /tmp/{job}_done.wav"),
        capture_output=True, timeout=60)
    logger.info("[svc] diffusion on %s done (%d steps)", host, diffusion_steps)


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
