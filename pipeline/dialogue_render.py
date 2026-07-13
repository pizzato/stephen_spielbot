"""Render a dialogue/performance scene into one clip via EchoMimic.

For each line ``{speaker, text}``: synthesize the speaker's voice (OpenF5 TTS),
take the talking still (the scene's own first frame when there is a single
speaker — so the character speaks *in the scene* — else the speaker's portrait,
shot/reverse-shot style), animate it to the audio (EchoMimic talking-head), then
concatenate the per-line clips into the scene's final mp4.

Narration scenes never call this — it is only reached for ``scene.mode == "dialogue"``.
The side-effectful steps (TTS, still, animate, probe, concat) are injected so the
orchestrator can wire in its real workers and tests can drive it with fakes.
"""
from __future__ import annotations

import contextlib
import logging
import shutil
from pathlib import Path

from pipeline import echomimic
from pipeline.assembler import _get_duration, concatenate_scenes_hard_cut, fit_video_canvas
from pipeline.tts_worker import generate_narration

logger = logging.getLogger("video_gen")

NARRATOR = "Narrator"

# EchoMimic (Wan-based) wants /16 dimensions; cap the long side for speed.
_ECHO_MAX_SIDE = 768
_ECHO_MIN_SIDE = 128


def echo_dims_for_still(still: Path, cap: int = _ECHO_MAX_SIDE) -> tuple[int, int]:
    """(width, height) to request from EchoMimic for *still*.

    Keeps the still's aspect (so a landscape scene frame stays landscape and a
    square portrait stays square), scales the long side down to *cap*, and rounds
    to the /16 grid the model needs. Falls back to cap×cap when unreadable."""
    try:
        from PIL import Image
        with Image.open(still) as img:
            w, h = img.size
    except Exception:
        return cap, cap
    scale = min(1.0, cap / max(w, h))
    w, h = int(w * scale), int(h * scale)
    w = max(_ECHO_MIN_SIDE, (w // 16) * 16)
    h = max(_ECHO_MIN_SIDE, (h // 16) * 16)
    return w, h


def render_dialogue_scene(
    scene,
    work_dir: Path,
    *,
    voice_ref_for,          # (speaker:str) -> Path | None   reference wav for the voice
    make_still,             # (scene, speaker:str, idx:int) -> Path   still to animate
    echomimic_host: str,
    tts_host: str = "localhost",
    prompt_for=lambda scene, speaker: "A person speaks to the camera, natural facial expression.",
    tts_engine: str = "openf5",
    canvas: tuple[int, int] | None = None,   # film (width, height); line clips are fitted onto it
    steps: int = 8,
    # (scene, idx, n_lines, speaker) -> context manager wrapping one shot's work —
    # the orchestrator uses it for progress messages + durable task tracking.
    line_cm=None,
    # (scene, shot, still, out_clip) -> Path — render a SILENT shot (motion, no
    # speech) as an LTX i2v clip. None ⟹ silent shots hold on their still.
    silent_video=None,
    # injected for testing / to plug the real workers
    _tts=generate_narration,
    _animate=echomimic.animate,
    _duration=_get_duration,
    _concat=concatenate_scenes_hard_cut,
    _fit=fit_video_canvas,
) -> Path:
    """Render ``scene`` (mode 'dialogue') to ``scene_NN_final.mp4`` and return it.

    Each entry in ``scene.lines`` is a shot: SPEAKING (has ``text`` → talking
    head) or SILENT (``silent`` flag → motion clip, no speech)."""
    def _is_silent(ln):
        return bool((ln or {}).get("silent")) and not str((ln or {}).get("text") or "").strip()

    lines = [ln for ln in (scene.lines or [])
             if _is_silent(ln) or str((ln or {}).get("text") or "").strip()]
    if not lines:
        raise ValueError(f"dialogue scene {scene.id} has no usable shots")

    line_clips: list[Path] = []
    for idx, ln in enumerate(lines):
        silent = _is_silent(ln)
        speaker = "" if silent else (str(ln.get("speaker") or NARRATOR).strip() or NARRATOR)
        clip = work_dir / f"scene_{scene.id:02d}_line_{idx:02d}.mp4"

        cm = line_cm(scene, idx, len(lines), speaker or "silent") if line_cm else contextlib.nullcontext()
        with cm:
            still = make_still(scene, speaker, idx)

            if silent:
                if silent_video is None:
                    raise RuntimeError(
                        f"scene {scene.id} shot {idx} is silent but no silent-video renderer is configured")
                silent_video(scene, ln, still, clip)
                logger.info("  scene %d shot %d (silent, %.1fs) -> %s",
                            scene.id, idx, float(ln.get("duration") or 3.0), clip.name)
            else:
                text = str(ln.get("text") or "").strip()
                wav = work_dir / f"scene_{scene.id:02d}_line_{idx:02d}.wav"
                _tts(text, wav, reference_wav=voice_ref_for(speaker), host=tts_host, tts_engine=tts_engine)
                dur = _duration(wav)
                ew, eh = echo_dims_for_still(still)
                _animate(
                    still, wav, clip, echomimic_host,
                    prompt=prompt_for(scene, speaker),
                    steps=steps, width=ew, height=eh,
                    video_length=echomimic.frames_for_audio(dur),
                )
                logger.info("  scene %d line %d (%s): %.1fs @%dx%d -> %s",
                            scene.id, idx, speaker, dur, ew, eh, clip.name)

            # Fit each shot onto the film canvas (blurred pillarbox when aspects
            # differ) so the in-scene concat and the cross-scene concat both see
            # uniform dimensions.
            if canvas:
                _fit(clip, canvas[0], canvas[1])
            line_clips.append(clip)

    final = work_dir / f"scene_{scene.id:02d}_final.mp4"
    if len(line_clips) == 1:
        shutil.copy2(line_clips[0], final)
    else:
        _concat(line_clips, final)
    return final
