"""Render a dialogue/performance scene into one clip via EchoMimic.

For each line ``{speaker, text}``: synthesize the speaker's voice (OpenF5 TTS),
take a talking-framed still of the speaker (FLUX, reusing the character's reference
image), animate that still to the audio (EchoMimic talking-head), then concatenate
the per-line clips into the scene's final mp4. Cutting between single-speaker shots
is the shot/reverse-shot form dialogue naturally takes.

Narration scenes never call this — it is only reached for ``scene.mode == "dialogue"``.
The side-effectful steps (TTS, still, animate, probe, concat) are injected so the
orchestrator can wire in its real workers and tests can drive it with fakes.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from pipeline import echomimic
from pipeline.assembler import _get_duration, concatenate_scenes_hard_cut
from pipeline.tts_worker import generate_narration

logger = logging.getLogger("video_gen")

NARRATOR = "Narrator"


def render_dialogue_scene(
    scene,
    work_dir: Path,
    *,
    voice_ref_for,          # (speaker:str) -> Path | None   reference wav for the voice
    make_still,             # (scene, speaker:str, idx:int) -> Path   FLUX still of the speaker
    echomimic_host: str,
    tts_host: str = "localhost",
    prompt_for=lambda scene, speaker: "A person speaks to the camera, natural facial expression.",
    tts_engine: str = "openf5",
    size: int = 768,
    steps: int = 8,
    # injected for testing / to plug the real workers
    _tts=generate_narration,
    _animate=echomimic.animate,
    _duration=_get_duration,
    _concat=concatenate_scenes_hard_cut,
) -> Path:
    """Render ``scene`` (mode 'dialogue') to ``scene_NN_final.mp4`` and return it."""
    lines = [ln for ln in (scene.lines or []) if str((ln or {}).get("text") or "").strip()]
    if not lines:
        raise ValueError(f"dialogue scene {scene.id} has no non-empty lines")

    line_clips: list[Path] = []
    for idx, ln in enumerate(lines):
        speaker = str(ln.get("speaker") or NARRATOR).strip() or NARRATOR
        text = str(ln.get("text") or "").strip()

        wav = work_dir / f"scene_{scene.id:02d}_line_{idx:02d}.wav"
        _tts(text, wav, reference_wav=voice_ref_for(speaker), host=tts_host, tts_engine=tts_engine)
        dur = _duration(wav)

        still = make_still(scene, speaker, idx)

        clip = work_dir / f"scene_{scene.id:02d}_line_{idx:02d}.mp4"
        _animate(
            still, wav, clip, echomimic_host,
            prompt=prompt_for(scene, speaker),
            steps=steps, size=size,
            video_length=echomimic.frames_for_audio(dur),
        )
        logger.info("  scene %d line %d (%s): %.1fs -> %s", scene.id, idx, speaker, dur, clip.name)
        line_clips.append(clip)

    final = work_dir / f"scene_{scene.id:02d}_final.mp4"
    if len(line_clips) == 1:
        shutil.copy2(line_clips[0], final)
    else:
        _concat(line_clips, final)
    return final
