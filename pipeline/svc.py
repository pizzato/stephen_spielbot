"""Style-selected singing-voice conversion: SoulX-SVC (default) or Seed-VC.

Demucs separates vocals on the controller; the selected model converts them on
an available leased worker, falling back to the same model on the controller.
Level-matched vocals are mixed over the original backing. The original take is
preserved by the calling song-history workflow.

``make install`` prepares isolated runtimes on the controller and workers.
SoulX-SVC uses pinned SVC, RMVPE and Whisper weights, without lyric alignment.
SoulX-SVC is Apache-2.0; Seed-VC is GPL-3.0. Both run as subprocesses, never as
imports in the application process. See THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import logging
import json
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("video_gen")

SVC_DIR = Path.home() / ".local" / "share" / "video-generator" / "seed-vc"
SOULX_DIR = SVC_DIR.with_name("soulx-singer")
DEFAULT_ENGINE = "soulx-svc"
ENGINE_LABELS = {"soulx-svc": "SoulX-SVC", "seed-vc": "Seed-VC"}
SOULX_RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "soulx_svc.py"


def norm_engine(value) -> str:
    """Normalize the style's converter, defaulting older styles to SoulX-SVC."""
    key = str(value or "").strip().lower()
    return key if key in ENGINE_LABELS else DEFAULT_ENGINE


def available(engine: str = DEFAULT_ENGINE) -> bool:
    """Is the selected converter installed on this controller?"""
    if norm_engine(engine) == "soulx-svc":
        if not all(path.exists() for path in (
            SOULX_DIR / ".venv/bin/python",
            SOULX_DIR / "soulxsinger/models/soulxsinger_svc.py",
            SOULX_DIR / "pretrained_models/SoulX-Singer/model-svc.pt",
            SOULX_DIR / "pretrained_models/SoulX-Singer-Preprocess/rmvpe/rmvpe.pt",
            SOULX_RUNNER,
        )):
            return False
        try:
            manifest = json.loads((SOULX_DIR / "spielbot-models.json").read_text())
            revision = manifest["openai/whisper-base"]
            cache = SOULX_DIR / "hf-cache/hub/models--openai--whisper-base"
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                return False
            return ((cache / "refs/main").read_text().strip() == revision
                    and all((cache / "snapshots" / revision / name).is_file()
                            for name in ("config.json", "preprocessor_config.json",
                                         "model.safetensors")))
        except (OSError, ValueError, KeyError, TypeError):
            return False
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
    """Workers that can run the diffusion, best first — as their comfy URLs,
    so the fleet-wide worker lease can key on them (the conversion itself
    docker-execs on the URL's host).

    Any worker will do — every ComfyUI container carries both converters — so the
    pick is just "who is free": idle workers first, then least-busy, the
    same ordering the UI uses to route cover jobs. Each is tried in turn and
    the controller is the last resort, so a host that is down, or whose
    container predates the selected converter, only costs the next one a moment.

    ``svc_worker`` in the config pins one host instead of the whole fleet
    (returned as its comfy URL when the fleet lists it, else the bare host).
    """
    urls = [u for u in (cfg.get("comfy_workers") or []) if u]
    pinned = _host_of(str(cfg.get("svc_worker") or ""))
    if pinned:
        for url in urls:
            if _host_of(url) == pinned:
                return [url]
        return [pinned]
    try:
        from pipeline.worker_pool import idle_workers
        urls = idle_workers(urls, timeout=2)
    except Exception:
        pass  # nothing reachable (or no workers at all) — order as configured
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        host = _host_of(url)
        if host and host not in seen:
            seen.add(host)
            out.append(url)
    return out


def convert_song(source: Path, voice_ref: Path, output: Path,
                 diffusion_steps: int | None = None, timeout: int = 3600,
                 workers: Sequence[str] = (), *, engine: str = DEFAULT_ENGINE) -> Path:
    """Re-voice *source* (a sung track) with the timbre of *voice_ref*.

    Separates the vocal stem, converts only it, and remixes it (level-matched
    to the original vocals) over the untouched instruments. Writes the result
    to *output* and returns it.

    Defaults are 32 diffusion steps for SoulX-SVC and 30 for Seed-VC;
    ``svc_diffusion_steps`` can override them. Workers are tried under the
    shared fleet lease, then the selected model runs on the controller.
    SoulX-SVC uses CUDA when available, otherwise CPU (slower).
    """
    engine = norm_engine(engine)
    diffusion_steps = int(diffusion_steps or (32 if engine == "soulx-svc" else 30))
    if not 1 <= diffusion_steps <= 200:
        raise ValueError("Voice conversion diffusion steps must be between 1 and 200.")
    if not available(engine):
        raise RuntimeError(
            f"{ENGINE_LABELS[engine]} is not installed — run make install "
            "(or scripts/install_svc.sh) on the controller first.")
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        stems = _separate_stems(source, work)
        if stems is None:
            if engine == "soulx-svc":
                raise RuntimeError("SoulX-SVC requires Demucs vocal separation — "
                                   "run make install (or scripts/install_svc.sh).")
            logger.warning("[svc] demucs unavailable — converting the WHOLE "
                           "mix (instruments will smear; fine only for "
                           "a-cappella-leaning tracks)")
            _convert_anywhere(source, voice_ref, output, diffusion_steps,
                              timeout, workers, engine=engine)
            _normalize_loudness(output)
            return output
        vocals, backing = stems
        converted = work / "converted_vocals.wav"
        _convert_anywhere(vocals, voice_ref, converted, diffusion_steps,
                          timeout, workers, engine=engine)
        # Match either model's output to the original vocal level before
        # laying it back over the untouched backing.
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
                      workers: Sequence[str], *, engine: str = DEFAULT_ENGINE) -> None:
    """The diffusion pass, on the first free worker that will take it —
    falling back to the controller when none does, because a slow conversion
    beats a failed one.

    Free means holding the fleet-wide worker lease: a worker busy with a
    render or upscale is skipped rather than double-booked, and when every
    worker is leased the controller converts (slow beats waiting out a
    multi-minute GPU job)."""
    from pipeline.worker_pool import try_worker_lease

    for worker in workers:
        entry = (worker or "").strip()
        host = _host_of(entry)
        if not host:
            continue
        release = try_worker_lease(entry)
        if release is None:
            logger.info("[svc] %s is busy (worker lease held) — trying the "
                        "next worker", host)
            continue
        try:
            _convert_remote(host, source, voice_ref, output,
                            diffusion_steps, timeout, engine=engine)
            return
        except Exception as e:
            logger.warning("[svc] conversion on %s failed (%s) — trying the "
                           "next worker", host, e)
        finally:
            release()
    if workers:
        logger.warning("[svc] no worker took the conversion — running on the "
                       "controller (slow)")
    _convert(source, voice_ref, output, diffusion_steps, timeout, engine=engine)


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


def _inference_command(engine: str, root: Path, source: Path, voice_ref: Path,
                       out_dir: Path, steps: int, runner: Path) -> list[str]:
    python = str(root / ".venv/bin/python")
    if engine == "soulx-svc":
        return [python, str(runner), "--model-dir", str(root),
                "--source", str(source), "--target", str(voice_ref),
                "--output", str(out_dir), "--diffusion-steps", str(steps)]
    return [python, str(root / "inference.py"),
            "--source", str(source), "--target", str(voice_ref),
            "--output", str(out_dir), "--diffusion-steps", str(steps),
            "--length-adjust", "1.0", "--inference-cfg-rate", "0.7",
            "--f0-condition", "True"]


def _convert_remote(host: str, source: Path, voice_ref: Path, output: Path,
                    diffusion_steps: int, timeout: int, *,
                    engine: str = DEFAULT_ENGINE) -> None:
    """Run the selected converter in a leased worker, cleaning up on failure too."""
    engine = norm_engine(engine)

    def _sh(args, step, tmo=120):
        proc = subprocess.run(args, capture_output=True, text=True, timeout=tmo)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
            raise RuntimeError(f"{step}: " + " | ".join(tail))
        return proc

    def _on_host(args: list[str]) -> list[str]:
        command = shlex.join(args)
        if _is_local(host):
            return ["bash", "-c", command]
        return ["ssh", *_SSH_OPTS, "--", host, command]

    def _push(local: Path, remote: str) -> list[str]:
        if _is_local(host):
            return ["cp", str(local), remote]
        return ["scp", "-q", *_SSH_OPTS, str(local), f"{host}:{remote}"]

    def _pull(remote: str, local: Path) -> list[str]:
        if _is_local(host):
            return ["cp", remote, str(local)]
        return ["scp", "-q", *_SSH_OPTS, f"{host}:{remote}", str(local)]

    # Do not interpolate source names into a remote shell or collide with a
    # concurrent request in this process.
    job = f"/tmp/svc_{uuid.uuid4().hex}"
    src, ref = Path(job + "_src.wav"), Path(job + "_ref.wav")
    runner, out_dir = Path(job + "_runner.py"), Path(job + "_out")
    done = Path(job + "_done.wav")
    staged = [(source, src), (voice_ref, ref)]
    if engine == "soulx-svc":
        staged.append((SOULX_RUNNER, runner))
    root = Path("/opt/soulx-singer" if engine == "soulx-svc" else _REMOTE_DIR)
    try:
        for local, remote in staged:
            _sh(_push(local, str(remote)), "copy conversion input")
            _sh(_on_host(["docker", "cp", str(remote),
                          f"{_REMOTE_CONTAINER}:{remote}"]), "stage into container")
        command = _inference_command(engine, root, src, ref, out_dir,
                                     diffusion_steps, runner)
        _sh(_on_host(["docker", "exec", _REMOTE_CONTAINER, *command]),
            f"{ENGINE_LABELS[engine]} inference", tmo=timeout)
        # Both CLIs write one WAV. All paths here contain only our UUID.
        _sh(_on_host(["docker", "exec", _REMOTE_CONTAINER, "sh", "-c",
                      f"cp {out_dir}/*.wav {done}"]), "collect output")
        _sh(_on_host(["docker", "cp", f"{_REMOTE_CONTAINER}:{done}", str(done)]),
            "copy out of container")
        output.parent.mkdir(parents=True, exist_ok=True)
        _sh(_pull(str(done), output), "copy result")
    finally:
        paths = [str(src), str(ref), str(runner), str(out_dir), str(done)]
        for command in (["rm", "-f", str(src), str(ref), str(runner), str(done)],
                        ["docker", "exec", _REMOTE_CONTAINER, "rm", "-rf", *paths]):
            try:
                subprocess.run(_on_host(command), capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                logger.warning("[svc] could not clean temporary conversion files on %s", host)
    logger.info("[svc] %s on %s done (%d steps)", ENGINE_LABELS[engine], host,
                diffusion_steps)


def _convert(source: Path, voice_ref: Path, output: Path,
             diffusion_steps: int, timeout: int, *, engine: str = DEFAULT_ENGINE) -> None:
    """Run the selected converter in its isolated controller environment."""
    engine = norm_engine(engine)
    root = SOULX_DIR if engine == "soulx-svc" else SVC_DIR
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        command = _inference_command(engine, root, source.resolve(), voice_ref.resolve(),
                                     out_dir, diffusion_steps, SOULX_RUNNER)
        proc = subprocess.run(command, cwd=root, capture_output=True, text=True,
                              timeout=timeout)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
            raise RuntimeError(f"{ENGINE_LABELS[engine]} failed: " + " | ".join(tail))
        wavs = sorted(out_dir.glob("*.wav"))
        if not wavs:
            raise RuntimeError(f"{ENGINE_LABELS[engine]} produced no output file")
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
         "-af", f"volume={gain:.1f}dB", "-c:a", "pcm_f32le", str(tmp)],
        check=True, capture_output=True)
    tmp.replace(path)


def _remix(vocals: Path, backing: Path, output: Path) -> None:
    """Mix at the original vocal balance, then leave 1 dB of peak headroom.

    Float intermediates prevent a loud cloned vocal from clipping before the
    shared final attenuation. No limiter or independent backing gain is used.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".remix.wav")
    subprocess.run(
        [_ffmpeg(), "-y", "-v", "error",
         "-i", str(vocals), "-i", str(backing),
         "-filter_complex",
         "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[a]",
         "-map", "[a]", "-ar", "44100", "-c:a", "pcm_f32le", str(tmp)],
        check=True, capture_output=True)
    try:
        proc = subprocess.run(
            [_ffmpeg(), "-i", str(tmp), "-af", "astats=reset=0", "-f", "null", "-"],
            check=True, capture_output=True, text=True)
        levels = re.findall(r"Peak level dB:\s*(-?[\d.]+|-inf)", proc.stderr or "")
        if not levels:
            raise RuntimeError("Could not measure converted mix peak level.")
        gain = min(0.0, -1.0 - max(float(level) for level in levels))
        subprocess.run(
            [_ffmpeg(), "-y", "-v", "error", "-i", str(tmp),
             "-af", f"volume={gain:.6f}dB", "-c:a", "pcm_s24le", str(output)],
            check=True, capture_output=True)
    finally:
        tmp.unlink(missing_ok=True)


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
