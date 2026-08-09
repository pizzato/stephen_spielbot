#!/usr/bin/env python3
"""Standalone resume script — resumes video generation without Gradio.

Usage:
    .venv/bin/python resume_generation.py <work_dir>
"""
import concurrent.futures
import json
import threading
import logging
import logging.handlers
import shutil
import sys
import time
from pathlib import Path

import yaml

LOG_DIR = Path.home() / ".local" / "share" / "video-generator" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"  # Append to same log as main app

_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_fmt)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_log_fmt)
logging.basicConfig(level=logging.WARNING, handlers=[_file_handler, _stream_handler], force=True)
logger = logging.getLogger("video_gen")
logger.setLevel(logging.DEBUG)

from PIL import Image as _PILImage

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.llm import Scene, NEGATIVE_PROMPT, narration_language_name
from pipeline import engines as _engines
from pipeline import performance as _performance
from pipeline import shot_gate
from pipeline import prompts as _prompts
from pipeline.comfyui import generate_music, generate_with_engine, ltx_dimensions, StuckJobError
from pipeline.assembler import (
    _get_duration, mux_video_audio, FINAL_SCENE_TAIL_SECS,
    concat_audio, concatenate_scenes, extract_last_frame,
    ensure_video_resolution, mix_background_music,
    fit_video_canvas,
    write_silence_wav as _write_silence_wav,
)
from pipeline import cadence as _cadence
from pipeline.tts_text import spoken_source
from pipeline.tts_worker import generate_narration, resolve_robotic_amount
from pipeline.orchestrator import (
    DurableStore, TaskRun,
    JOB_DONE, JOB_ERROR, JOB_RUNNING,
    job_id_from_work_dir, task_id, worker_id, timing_signature,
)
from pipeline.scene_video import generate_scene_video as _generate_scene_video
from pipeline.worker_pool import WorkerPool, alive_workers
from pipeline import ui_activity
# Resolution name → (w, h) map. Import the canonical table from app rather than
# keeping a copy here — a stale local copy silently dropped the 720p tier and
# rendered every 720p job at the 1920×1080 fallback (wrong size and orientation).
from app import _RESOLUTIONS, _DEFAULT_RESOLUTION, _dialogue_resolvers
from app import build_cover_generation as _build_cover_generation
from pipeline.cover import (
    burn_cover_into_first_frame as _burn_first_frame,
    cover_dimensions as _cover_dimensions,
)
from pipeline.cover_typography import (
    COVER_BASE_NAME as _COVER_BG_NAME,
    apply_cover_typography as _apply_cover_typography,
    norm_cover_typography as _norm_cover_typography,
)

CONFIG_FILE = Path.home() / ".config" / "video-generator" / "config.yaml"
OUTPUT_DIR  = Path.home() / "videos"


def _image_matches_resolution(path: Path, width: int, height: int) -> bool:
    """Return True only if *path* is a readable image with exactly width×height pixels."""
    try:
        with _PILImage.open(path) as img:
            return img.size == (width, height)
    except Exception:
        return False

_WORKER_ERR_KEYWORDS = ("timed out", "not reachable", "URLError", "Connection refused",
                        "ConnectionRefused", "RemoteDisconnected")
# Per-scene render attempts, each on a freshly acquired worker.
_MAX_SCENE_ATTEMPTS = 3
_PROGRESS_STORE: DurableStore | None = None
_PROGRESS_JOB_ID: str | None = None


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return yaml.safe_load(CONFIG_FILE.read_text()) or {}
    raise RuntimeError(f"Config not found: {CONFIG_FILE}")


def load_job_config(work_dir: Path) -> dict:
    cfg = load_config()
    job_cfg = work_dir / "job_config.json"
    if job_cfg.exists():
        cfg.update(json.loads(job_cfg.read_text()))
    return cfg


def _heal_empty_scenes(scenes: list[Scene], title: str, cfg: dict, work_dir: Path) -> None:
    """Fill missing narration/image_prompt/video_prompt fields before audio generation.

    A scene with empty narration would produce a 0-byte audio clip and a silent video.
    This recovers from any upstream save bug by calling the LLM to fill what's missing,
    and falls back to scene title text so generation NEVER produces a silent scene.
    """
    # Dialogue scenes carry spoken lines and silent scenes have no voice-over by
    # design — never heal either into narrated scenes. Silent scenes still need
    # an image_prompt (they render visuals).
    def _needs_heal(s: Scene) -> bool:
        mode = getattr(s, "mode", "narration")
        if mode == "dialogue":
            return False
        if mode == "silent":
            return not (s.image_prompt or "").strip()
        return not (s.narration or "").strip() or not (s.image_prompt or "").strip()

    bad = [s for s in scenes if _needs_heal(s)]
    if not bad:
        return
    logger.warning("Self-heal: %d scene(s) with empty fields — filling: %s",
                   len(bad), [(s.id, not s.narration, not s.image_prompt) for s in bad])

    video_title = cfg.get("video_title") or cfg.get("title") or title
    # Build minimal context from neighbouring scenes' narrations so the LLM keeps continuity.
    backend = cfg.get("llm_backend", "claude")
    try:
        if backend == "claude" and cfg.get("claude_api_key"):
            _fill_via_claude(scenes, title, video_title, cfg)
        elif backend in ("grok", "openai"):
            _fill_via_chat(scenes, title, video_title, cfg)
        else:
            _fill_via_local(scenes, title, video_title, cfg)
    except Exception as exc:
        logger.warning("Self-heal LLM fill failed: %s — falling back to title text", exc)

    # Absolute last resort: never leave a scene with empty narration or image_prompt.
    # Only narration-mode scenes get narration filled — dialogue/silent scenes are
    # voiceless by design.
    for s in scenes:
        mode = getattr(s, "mode", "narration")
        if mode == "narration" and not (s.narration or "").strip():
            s.narration = f"{s.title or f'Scene {s.id}'}."
            logger.warning("Self-heal: scene %d narration still empty after LLM — used title", s.id)
        if mode != "dialogue" and not (s.image_prompt or "").strip():
            s.image_prompt = s.title or f"Scene {s.id}: {title}"
            logger.warning("Self-heal: scene %d image_prompt still empty after LLM — used title", s.id)
        if not (s.video_prompt or "").strip():
            s.video_prompt = s.image_prompt

    # Persist the healed script so the next resume doesn't have to redo this work.
    # Keep the dialogue/silent metadata — dropping it here would silently turn
    # those scenes back into narration on the next load.
    try:
        rows = []
        for s in scenes:
            row = {"id": s.id, "title": s.title, "image_prompt": s.image_prompt,
                   "video_prompt": s.video_prompt, "narration": s.narration}
            md = {}
            if getattr(s, "mode", "narration") not in ("narration", "", None):
                md["mode"] = s.mode
            if getattr(s, "lines", None):
                md["lines"] = s.lines
            if getattr(s, "duration", 0):
                md["duration"] = s.duration
            if md:
                row["metadata"] = md
            rows.append(row)
        (work_dir / "script.json").write_text(json.dumps(rows, indent=2))
        logger.info("Self-heal: rewrote script.json with %d filled scenes", len(scenes))
    except Exception as exc:
        logger.warning("Self-heal: could not rewrite script.json: %s", exc)


def _fill_via_claude(scenes: list[Scene], title: str, video_title: str, cfg: dict) -> None:
    """Use Claude to fill empty narration/image_prompt fields per scene."""
    import anthropic, httpx
    api_key = cfg.get("claude_api_key", "")
    if not api_key:
        return
    model = cfg.get("claude_model", "claude-sonnet-4-6")
    lang_name = narration_language_name(cfg.get("tts_language", cfg.get("default_tts_language")))
    timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=30.0)
    client = anthropic.Anthropic(api_key=api_key, http_client=httpx.Client(http2=False, timeout=timeout))
    for s in scenes:
        if (s.narration or "").strip() and (s.image_prompt or "").strip():
            continue
        prev_narr = next((p.narration for p in scenes if p.id == s.id - 1 and p.narration), "")
        next_narr = next((p.narration for p in scenes if p.id == s.id + 1 and p.narration), "")
        ctx = [f'Video topic: "{video_title}"', f'Scene {s.id} title: "{s.title or "(no title)"}"']
        if lang_name: ctx.append(f"Write the narration in {lang_name} (image_prompt stays in English).")
        if prev_narr: ctx.append(f'Previous scene: "{prev_narr}"')
        if next_narr: ctx.append(f'Next scene: "{next_narr}"')
        need = []
        if not (s.narration or "").strip():
            need.append('"narration": exactly 2 sentences, ~18-22 words, calm documentary tone')
        if not (s.image_prompt or "").strip():
            need.append('"image_prompt": 60-100 word static scene description for FLUX, no motion verbs')
        if not need:
            continue
        try:
            with client.messages.stream(
                model=model, max_tokens=400,
                system=_prompts.system("heal_claude"),
                messages=[{"role": "user", "content": _prompts.user(
                    "heal_claude",
                    ctx="\n".join(ctx),
                    needed_keys=", ".join(need),
                )}],
            ) as stream:
                text = "".join(stream.text_stream).strip()
            _apply_heal_json(s, text, label="Claude")
        except Exception as exc:
            logger.warning("Self-heal: Claude fill failed for scene %d: %s", s.id, exc)


def _fill_via_chat(scenes: list[Scene], title: str, video_title: str, cfg: dict) -> None:
    """Fill empty fields via the configured chat backend (Grok / shared path)."""
    from pipeline.llm import _chat_complete
    lang_name = narration_language_name(cfg.get("tts_language", cfg.get("default_tts_language")))
    for s in scenes:
        if (s.narration or "").strip() and (s.image_prompt or "").strip():
            continue
        prev_narr = next((p.narration for p in scenes if p.id == s.id - 1 and p.narration), "")
        next_narr = next((p.narration for p in scenes if p.id == s.id + 1 and p.narration), "")
        ctx = [f'Video topic: "{video_title}"', f'Scene {s.id} title: "{s.title or "(no title)"}"']
        if lang_name: ctx.append(f"Write the narration in {lang_name} (image_prompt stays in English).")
        if prev_narr: ctx.append(f'Previous scene: "{prev_narr}"')
        if next_narr: ctx.append(f'Next scene: "{next_narr}"')
        need = []
        if not (s.narration or "").strip():
            need.append('"narration": exactly 2 sentences, ~18-22 words, calm documentary tone')
        if not (s.image_prompt or "").strip():
            need.append('"image_prompt": 60-100 word static scene description for FLUX, no motion verbs')
        if not need:
            continue
        try:
            text = _chat_complete(
                cfg,
                _prompts.system("heal_claude"),
                _prompts.user("heal_claude", ctx="\n".join(ctx), needed_keys=", ".join(need)),
                max_tokens=400,
                label=f"heal scene {s.id}",
            )
            label = (cfg.get("llm_backend") or "chat").capitalize()
            _apply_heal_json(s, text, label=label)
        except Exception as exc:
            logger.warning("Self-heal: chat fill failed for scene %d: %s", s.id, exc)


def _apply_heal_json(s: Scene, text: str, label: str = "LLM") -> None:
    """Parse a heal_claude JSON blob and apply non-empty fields onto *s*."""
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    data = json.loads(text)
    if not (s.narration or "").strip() and data.get("narration"):
        s.narration = data["narration"].strip()
        logger.info("Self-heal: filled scene %d narration via %s: %r", s.id, label, s.narration[:60])
    if not (s.image_prompt or "").strip() and data.get("image_prompt"):
        s.image_prompt = data["image_prompt"].strip()
        logger.info("Self-heal: filled scene %d image_prompt via %s", s.id, label)


def _fill_via_local(scenes: list[Scene], title: str, video_title: str, cfg: dict) -> None:
    """Use the local LLM to fill empty narration/image_prompt fields per scene."""
    import urllib.request
    url = cfg.get("local_llm_url", "http://localhost:8000/v1/chat/completions")
    model = cfg.get("local_llm_model", "openai/gpt-oss-120b")
    lang_name = narration_language_name(cfg.get("tts_language", cfg.get("default_tts_language")))
    for s in scenes:
        if (s.narration or "").strip() and (s.image_prompt or "").strip():
            continue
        ctx = [f'Video topic: "{video_title}"', f'Scene {s.id} title: "{s.title or "(no title)"}"']
        if lang_name: ctx.append(f"Write the narration in {lang_name}.")
        prev_narr = next((p.narration for p in scenes if p.id == s.id - 1 and p.narration), "")
        next_narr = next((p.narration for p in scenes if p.id == s.id + 1 and p.narration), "")
        if prev_narr: ctx.append(f'Previous scene: "{prev_narr}"')
        if next_narr: ctx.append(f'Next scene: "{next_narr}"')
        if not (s.narration or "").strip():
            payload = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": _prompts.system("heal_local_narration")},
                    {"role": "user", "content": _prompts.user("heal_local_narration", ctx="\n".join(ctx))},
                ],
                "max_tokens": 120, "temperature": 0.7,
            }).encode()
            try:
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                text = data["choices"][0]["message"]["content"].strip()
                if text:
                    s.narration = text
                    logger.info("Self-heal: filled scene %d narration via local LLM: %r", s.id, text[:60])
            except Exception as exc:
                logger.warning("Self-heal: local LLM narration fill failed for scene %d: %s", s.id, exc)
        if not (s.image_prompt or "").strip():
            payload = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": _prompts.system("heal_local_image_prompt")},
                    {"role": "user", "content": _prompts.user("heal_local_image_prompt", ctx="\n".join(ctx[:2]))},
                ],
                "max_tokens": 250, "temperature": 0.7,
            }).encode()
            try:
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                text = data["choices"][0]["message"]["content"].strip()
                if text:
                    s.image_prompt = text
                    logger.info("Self-heal: filled scene %d image_prompt via local LLM", s.id)
            except Exception as exc:
                logger.warning("Self-heal: local LLM image_prompt fill failed for scene %d: %s", s.id, exc)


def write_progress(status_file: Path, pct: float, msg: str) -> None:
    try:
        status_file.write_text(json.dumps({"pct": round(pct, 1), "msg": msg, "ts": time.time()}))
    except Exception:
        pass
    try:
        if _PROGRESS_STORE is not None and _PROGRESS_JOB_ID is not None:
            status = JOB_DONE if pct >= 100 else JOB_RUNNING
            _PROGRESS_STORE.update_job(
                _PROGRESS_JOB_ID,
                status=status,
                progress_pct=pct,
                progress_message=msg,
            )
    except Exception:
        logger.debug("Could not mirror progress to durable store", exc_info=True)
    logger.info("PROGRESS %.0f%% — %s", pct, msg)


_SILENT_DEFAULT_SECS = 5.0

# _write_silence_wav and _dialogue_resolvers moved to pipeline.assembler / app so
# the web backend's per-scene dialogue re-render shares them (imported above).


def render_performance_scene(scene: Scene, work_dir: Path, cfg: dict, *,
                             comfy_url: str, vid_width: int, vid_height: int,
                             style_name: str = "") -> Path:
    """Render ONE performance scene: character portraits + dialogue → a clip that
    already contains its own speech. Returns the finished scene_NN_final.mp4.

    Shared with the backend's per-scene re-render, so it takes everything it
    needs as arguments and touches no module state.
    """
    scene_meta = _performance.scene_meta(scene)
    # A two-hander is rendered as shot/reverse-shot: one face and one voice per
    # clip, because with two of each the model swaps them (see shots_for). It
    # opens on a silent wide of everyone together (swap-proof: no voices) so
    # the scene still reads as people in one place — config-off-able.
    shots = _performance.shots_for(
        scene_meta, establishing=bool(cfg.get("performance_establishing", True)))
    if len(shots) > 1:
        return _render_performance_shots(
            scene, shots, scene_meta, work_dir, cfg, comfy_url=comfy_url,
            vid_width=vid_width, vid_height=vid_height, style_name=style_name)
    return _render_performance_clip(
        scene, shots[0], work_dir, cfg, work_dir / f"scene_{scene.id:02d}_final.mp4",
        comfy_url=comfy_url, vid_width=vid_width, vid_height=vid_height,
        style_name=style_name)


def _render_performance_shots(scene, shots, scene_meta, work_dir, cfg, *, comfy_url,
                              vid_width, vid_height, style_name) -> Path:
    """Render each single-speaker shot, then join them into the scene.

    Every shot is an independent generation, so left alone they each invent
    their own room and the scene appears to teleport between cuts. The first
    shot's own last frame is fed to the rest as a continuity reference: the
    reverse angle is then demonstrably the same space, furniture and light.
    """
    parts = []
    room: Path | None = None
    for idx, shot in enumerate(shots):
        out = work_dir / f"scene_{scene.id:02d}_shot_{idx:02d}.mp4"
        if not (out.exists() and out.stat().st_size > 10_000):
            who = ("establishing wide" if shot.get("establishing")
                   else f"{shot.get('speaker')} speaks")
            logger.info("Scene %d shot %d/%d: %s%s",
                        scene.id, idx + 1, len(shots), who,
                        " (matching the first shot's room)" if room else "")
            extra = ([{"name": "the room already filmed in this scene",
                       "kind": "continuity", "path": str(room)}] if room else [])
            _render_performance_clip(
                scene, {**shot, "scene_cast": _performance.speakers_in(
                    _performance.norm_lines(scene_meta.get("lines")))},
                work_dir, cfg, out, comfy_url=comfy_url, vid_width=vid_width,
                vid_height=vid_height, style_name=style_name, extra_pictures=extra,
                # The room frame IS the location, photographed. Sending the
                # location asset alongside it wastes a slot and dilutes binding —
                # measured: at 3 picture refs everything held (outfit, wharf,
                # face); at 4+ the weakest refs started dropping. The wide is
                # over budget with two casts' wardrobe, and garment detail is
                # invisible at that distance anyway.
                drop_kinds=(("location",) if room else ())
                + (("wardrobe",) if shot.get("establishing") else ()))
        parts.append(out)
        if room is None:
            # Best-effort: without it the later shots simply lose the hint.
            candidate = work_dir / f"scene_{scene.id:02d}_room.png"
            try:
                extract_last_frame(out, candidate)
                room = candidate
            except Exception as exc:
                logger.warning("Scene %d: no continuity frame (%s)", scene.id, exc)
    final = work_dir / f"scene_{scene.id:02d}_final.mp4"
    concatenate_scenes(parts, final)
    return final


def _render_performance_clip(scene, meta, work_dir, cfg, clip: Path, *, comfy_url,
                             vid_width, vid_height, style_name,
                             extra_pictures: list[dict] | None = None,
                             drop_kinds: tuple = ()) -> Path:
    from pipeline.comfyui import generate_video_h3_ref
    from app import resolve_performance_references

    # The SAME resolver the editor's performance view calls, so the slots shown
    # on screen are the slots wired into the graph.
    refs = resolve_performance_references(meta, cfg, work_dir, style_name, scene_id=scene.id)
    if drop_kinds:
        kept = [p for p in refs["pictures"] if p.get("kind") not in drop_kinds]
        refs["pictures"] = [{**p, "slot": i + 1} for i, p in enumerate(kept)]
    for pic in (extra_pictures or []):
        refs["pictures"].append({**pic, "slot": len(refs["pictures"]) + 1})
    # Passed whole: build_h3_prompt reads each reference's kind to give it the
    # right job (keep the face / keep the space / keep the garments).
    picture_names = refs["pictures"]
    ref_images = [Path(p["path"]) for p in refs["pictures"]]
    audio_names = [a["name"] for a in refs["audios"]]
    ref_audios = [Path(a["path"]) for a in refs["audios"]]

    if not ref_images:
        raise RuntimeError(
            f"Scene {scene.id}: no character portrait resolved for cast "
            f"{meta.get('cast')} — Ref2VA needs at least one reference image "
            "(generate the character look images first)")

    prompt = _performance.build_h3_prompt(
        meta, style_note=cfg.get("style", ""),
        picture_names=picture_names, audio_names=audio_names)
    engine = _engines.resolve_reference(cfg, cfg.get("reference_engine"))
    logger.info("Scene %d: performance render (%s) — %d portraits, %d voices → %s",
                scene.id, engine["key"], len(ref_images), len(ref_audios), clip.name)
    def _generate(out: Path) -> Path:
        generate_video_h3_ref(
            engine, prompt, ref_images, out,
            ref_audios=ref_audios,
            width=vid_width, height=vid_height,
            duration_seconds=float(meta.get("seconds") or _performance.SCENE_SECONDS),
            comfy_url=comfy_url,
        )
        return out

    _generate(clip)

    # The gate: a shot that doesn't say its line is a miss, and misses get
    # retaken with a fresh seed instead of shipped — that is what turns a
    # stochastic model into a consistent one. Soft dependency: without
    # faster-whisper the gate stands down. Silent shots have nothing to verify.
    expected = _performance.spoken_text(meta)
    verify_on = shot_gate.available() and bool(cfg.get("performance_verify", True))

    # A shot with NO lines must be silent — and the model does babble into
    # them against instructions ("and seal it in a thween", heard in a real
    # establishing wide). Retake once; if speech survives, strip the audio:
    # a wide with no room tone beats one where a ghost mumbles.
    if not expected and verify_on:
        transcript = shot_gate.transcribe(clip)
        words = shot_gate.word_count(transcript)
        retakes = int(cfg.get("performance_verify_retakes", 1) or 0)
        attempt = 0
        while words > shot_gate.SILENCE_MAX_WORDS and attempt < retakes:
            attempt += 1
            logger.warning("[gate] scene %d %s: silent shot says %r — retake %d/%d",
                           scene.id, clip.name, transcript[:80], attempt, retakes)
            candidate = clip.with_suffix(".retake.mp4")
            _generate(candidate)
            cand_tr = shot_gate.transcribe(candidate)
            if shot_gate.word_count(cand_tr) < words:
                candidate.replace(clip)
                transcript, words = cand_tr, shot_gate.word_count(cand_tr)
            else:
                candidate.unlink(missing_ok=True)
        if words > shot_gate.SILENCE_MAX_WORDS:
            logger.warning("[gate] scene %d %s: still speaking after retakes — muting",
                           scene.id, clip.name)
            silence = clip.with_suffix(".silence.wav")
            _write_silence_wav(silence, _get_duration(clip))
            muted = clip.with_suffix(".muted.mp4")
            mux_video_audio(clip, silence, muted)
            muted.replace(clip)
            silence.unlink(missing_ok=True)

    if (expected and verify_on):
        best, transcript = shot_gate.verify(clip, expected)
        retakes = int(cfg.get("performance_verify_retakes", 1) or 0)
        attempt = 0
        while best < shot_gate.DEFAULT_THRESHOLD and attempt < retakes:
            attempt += 1
            logger.warning("[gate] scene %d %s: said %r (score %.2f) — retake %d/%d",
                           scene.id, clip.name, transcript[:80], best, attempt, retakes)
            candidate = clip.with_suffix(".retake.mp4")
            _generate(candidate)
            cand_score, cand_tr = shot_gate.verify(candidate, expected)
            if cand_score > best:
                candidate.replace(clip)
                best, transcript = cand_score, cand_tr
            else:
                candidate.unlink(missing_ok=True)
        logger.info("[gate] scene %d %s: score %.2f%s", scene.id, clip.name, best,
                    "" if best >= shot_gate.DEFAULT_THRESHOLD else " (best of retakes)")

    # H3 renders under its own pixel cap; bring the clip back to the film's frame.
    ensure_video_resolution(clip, vid_width, vid_height)
    return clip


def generate_cover_image(work_dir: Path, cfg: dict, scenes: list, *, image_engine: dict,
                        worker_pool: WorkerPool, vid_width: int, vid_height: int,
                        title: str) -> Path | None:
    """Paint the film's cover, or return None if it already exists / fails.

    Extracted from the narrated flow so performance films get one too: without
    a cover the film shows up blank in Films and has no thumbnail to publish.
    Non-fatal by design — a finished film with no cover beats a failed render.
    """
    cover_path = work_dir / "cover.png"
    video_title = cfg.get("video_title", "").strip() or title
    style_clean = cfg.get("style", "").strip()
    if cover_path.exists():
        logger.info("Cover image already exists, skipping: %s", cover_path)
        return cover_path

    logger.info("Generating YouTube cover image for %r", video_title)
    _cover_url: str | None = None
    cover_w, cover_h = _cover_dimensions(vid_width, vid_height)
    # Cover typography: paint a TEXT-FREE background, then composite the title
    # with real fonts on top — the model never draws (or misspells) a letter.
    typo = _norm_cover_typography(cfg.get("cover_typography")
                                  or cfg.get("default_cover_typography"))
    cover_prompt, cover_refs = _build_cover_generation(
        work_dir, cfg, cfg.get("style_name") or "", scenes=scenes,
        extra_style=style_clean, text_position=typo["position"],
        engine=image_engine)
    try:
        _cover_url = worker_pool.acquire()
        generate_with_engine(
            image_engine, cover_prompt, work_dir / _COVER_BG_NAME,
            width=cover_w, height=cover_h, comfy_url=_cover_url,
            reference_images=cover_refs or None,
        )
        worker_pool.release(_cover_url)
        _cover_url = None
        _apply_cover_typography(work_dir, typo, video_title)
        logger.info("Cover image saved: %s", cover_path)
        return cover_path
    except Exception as cover_err:
        logger.warning("Cover image generation failed (non-fatal): %s", cover_err)
        if _cover_url is not None:
            try:
                worker_pool.release(_cover_url)
            except Exception:
                pass
        return None


def _run_performance_film(work_dir: Path, scenes: list[Scene], cfg: dict, *,
                          store: DurableStore, durable_job_id: str,
                          worker_pool: WorkerPool, status_file: Path,
                          vid_width: int, vid_height: int,
                          final_path: Path, cover_path: Path,
                          image_engine: dict | None = None) -> Path:
    """Render a performance film end to end.

    Nothing here overlaps the narrated pipeline: no first frames, no TTS, no
    music, no per-scene mux. Each scene is one Ref2VA generation that already
    contains its dialogue, and assembly is a straight concat that keeps the
    clips' own audio.
    """
    scene_finals: dict[int, Path] = {}
    done = [0]
    lock = threading.Lock()
    style_name = cfg.get("style_name") or ""
    # Only used to paint the cover — performance scenes render no stills.
    image_engine = image_engine or _engines.resolve(cfg, cfg.get("image_engine"))

    def _progress() -> float:
        return 2 + 88.0 * (done[0] / max(1, len(scenes)))

    def _render(scene: Scene) -> tuple[int, Path]:
        t_id = task_id(durable_job_id, "scene", scene.id, "performance")
        clip = work_dir / f"scene_{scene.id:02d}_final.mp4"
        if clip.exists() and clip.stat().st_size > 10_000:
            logger.info("Scene %d: performance clip already rendered — skipping", scene.id)
            return scene.id, clip
        lease_secs = int(_engines.resolve_reference(
            cfg, cfg.get("reference_engine")).get("lease_seconds") or 14400)

        last_err: Exception | None = None
        for attempt in range(1, _MAX_SCENE_ATTEMPTS + 1):
            if not worker_pool.has_healthy():
                raise RuntimeError(f"All workers failed — last error: {last_err}")
            comfy_url = worker_pool.acquire()
            store.register_worker(worker_id("comfy", comfy_url), "comfy", comfy_url)
            try:
                with TaskRun(store, t_id, worker_id_value=worker_id("comfy", comfy_url),
                             lease_seconds=lease_secs, retryable=True,
                             start_message=f"performance scene {scene.id}") as run:
                    with lock:
                        write_progress(status_file, _progress(),
                                       f"Performance · scene {scene.id} of {len(scenes)} "
                                       f"({done[0]} done)")
                    out = render_performance_scene(
                        scene, work_dir, cfg, comfy_url=comfy_url,
                        vid_width=vid_width, vid_height=vid_height, style_name=style_name)
                    store.record_artifact(durable_job_id, t_id, "scene_final", out,
                                          duration_seconds=_get_duration(out))
                    run.complete({"path": str(out)}, "performance scene rendered")
                with lock:
                    done[0] += 1
                return scene.id, out
            except Exception as e:
                last_err = e
                # A worker that can't run this engine at all (model not
                # downloaded there) fails instantly and would fail again — drop
                # it from the pool so the scene retries on a worker that can,
                # exactly like an unreachable one.
                is_worker_fault = (
                    isinstance(e, StuckJobError)
                    or any(kw in str(e) for kw in _WORKER_ERR_KEYWORDS)
                    or "not in list" in str(e))
                if is_worker_fault:
                    logger.warning("Worker %s cannot render scene %d (attempt %d/%d): %s "
                                   "— removing from the pool", comfy_url, scene.id,
                                   attempt, _MAX_SCENE_ATTEMPTS, str(e)[:300])
                    worker_pool.mark_failed(comfy_url)
                else:
                    logger.warning("Scene %d failed on %s (attempt %d/%d): %s — retrying",
                                   scene.id, comfy_url, attempt, _MAX_SCENE_ATTEMPTS,
                                   str(e)[:300])
                    if attempt < _MAX_SCENE_ATTEMPTS:
                        time.sleep(5)
            finally:
                worker_pool.release(comfy_url)
        raise RuntimeError(
            f"Scene {scene.id} failed after {_MAX_SCENE_ATTEMPTS} attempts: {last_err}")

    # One scene per available worker; the pool honours the UI reservation.
    workers = max(1, len(worker_pool.urls))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(scenes)))
    try:
        for sid, clip in pool.map(_render, scenes):
            scene_finals[sid] = clip
    finally:
        pool.shutdown(wait=True)

    ordered = [scene_finals[s.id] for s in scenes if s.id in scene_finals]
    if len(ordered) != len(scenes):
        raise RuntimeError(f"Performance render produced {len(ordered)}/{len(scenes)} scenes")

    # A film with no cover shows up blank in Films and has no thumbnail to
    # publish, so performance films paint one too (same engine and typography
    # as a narrated film's).
    write_progress(status_file, 90, "Generating the cover image…")
    generate_cover_image(work_dir, cfg, scenes, image_engine=image_engine,
                         worker_pool=worker_pool, vid_width=vid_width,
                         vid_height=vid_height, title=cfg.get("title") or work_dir.name)

    final_task = task_id(durable_job_id, "final")
    store.update_task_payload(final_task, {
        "scene_count": len(scenes), "final_path": str(final_path),
        "vid_width": vid_width, "vid_height": vid_height,
    })
    write_progress(status_file, 92, "Concatenating scenes…")
    with TaskRun(store, final_task, worker_id_value=worker_id("local", "assembler"),
                 lease_seconds=900, start_message="assembling final video") as final_run:
        # The clips already carry voice and ambience from the same forward pass,
        # so assembly keeps their audio untouched — no music, no mixing.
        #
        # Still write combined.mp4 (the narrated path's pre-mix artifact) and
        # copy it to the final: the backend treats its existence as "this film
        # finished" and the film editor, re-render and publish paths all read
        # it. Concatenating straight to the final left every performance film
        # showing 99% forever.
        combined = Path(work_dir) / "combined.mp4"
        concatenate_scenes(ordered, combined)
        ensure_video_resolution(combined, vid_width, vid_height)
        shutil.copy2(combined, final_path)
        ff_cover = str(cfg.get("first_frame_cover")
                       or cfg.get("default_first_frame_cover") or "none").strip().lower()
        if ff_cover in ("image", "text"):
            write_progress(status_file, 97, "Burning cover into the first frame…")
            try:
                _burn_first_frame(final_path, cover_path=cover_path,
                                  seconds=cfg.get("first_frame_cover_seconds",
                                                  cfg.get("default_first_frame_cover_seconds")))
            except Exception as ff_err:
                logger.warning("First-frame cover failed (non-fatal): %s", ff_err)
        store.record_artifact(durable_job_id, final_task, "final_video", final_path,
                              duration_seconds=_get_duration(final_path))
        final_run.complete({"path": str(final_path)}, "final video ready")
    return final_path


def main(work_dir: Path) -> None:
    global _PROGRESS_STORE, _PROGRESS_JOB_ID
    cfg = load_job_config(work_dir)

    # Load script
    script_data = json.loads((work_dir / "script.json").read_text())
    scenes = [
        Scene(
            id=s["id"],
            title=s["title"],
            image_prompt=s.get("image_prompt") or s.get("visual_prompt", s["title"]),
            video_prompt=s.get("video_prompt") or s.get("visual_prompt", s["title"]),
            narration=s.get("narration", ""),
            # Per-style video negative (blank → built-in default). job_config.json
            # carries the resolved value stamped at render time.
            negative_prompt=(cfg.get("video_negative_prompt") or "").strip() or NEGATIVE_PROMPT,
            # Dialogue/performance fields ride in the scene's metadata sidecar
            # (absent ⟹ narration — existing scripts hydrate unchanged).
            mode=str((s.get("metadata") or {}).get("mode") or "narration"),
            lines=list((s.get("metadata") or {}).get("lines") or []),
            duration=float((s.get("metadata") or {}).get("duration") or 0.0),
            metadata_extra=dict(s.get("metadata") or {}),
        )
        for s in script_data
    ]
    n = len(scenes)
    logger.info("Loaded %d scenes from %s", n, work_dir / "script.json")
    title = cfg.get("title") or (scenes[0].title.split(":")[0] if scenes else work_dir.name)

    # Self-heal: any scene with empty narration would produce a 0-byte audio clip
    # and a silent video. Fill missing fields from the LLM before audio generation.
    _heal_empty_scenes(scenes, title, cfg, work_dir)

    store = DurableStore.default()
    durable_job_id = job_id_from_work_dir(work_dir)
    _PROGRESS_STORE = store
    _PROGRESS_JOB_ID = durable_job_id

    # Config
    music_vol         = cfg.get("music_vol", 18) / 100.0
    voice_vol         = cfg.get("voice_vol", 100) / 100.0
    voice_name        = cfg.get("default_voice", "Thomas")
    voice_robotic_amount = resolve_robotic_amount(cfg)  # 0 = natural; legacy toggle honored
    tts_engine        = cfg.get("tts_engine", cfg.get("default_tts_engine", "openf5"))
    tts_language      = cfg.get("tts_language", cfg.get("default_tts_language", "en"))
    # Speed as target cadence ÷ the voice's measured natural pace (pipeline/
    # cadence.py); legacy jobs without a cadence keep their stored multiplier.
    voice_speed       = _cadence.resolve_voice_speed({
        "voice": voice_name,
        "tts_engine": tts_engine,
        "voice_cadence_wpm": cfg.get("voice_cadence_wpm", cfg.get("default_voice_cadence_wpm", 0)),
        "voice_speed": cfg.get("voice_speed", cfg.get("default_voice_speed", 1.0)),
    })
    # Per-style sentence gap spliced between narration sentences (pipeline/tts_text.py).
    tts_sentence_pause = float(cfg.get("tts_sentence_pause", cfg.get("default_tts_sentence_pause", 0.0)) or 0.0)
    voice_ref_str     = None
    for v in cfg.get("voices", []):
        if v["name"] == voice_name:
            voice_ref_str = v["path"]
            break
    max_clip_secs     = float(cfg.get("max_clip_secs", 12.0))
    lora_strength     = float(cfg.get("lora_strength", 0.5))
    first_pass_cfg    = float(cfg.get("first_pass_cfg", 1.0))
    first_pass_steps  = int(cfg.get("first_pass_steps", 8))
    second_pass_cfg   = float(cfg.get("second_pass_cfg", 1.5))
    second_pass_steps = int(cfg.get("second_pass_steps", 3))
    # The style's image engine drives every first frame and the cover (same as
    # the UI preview/cover paths). job_config stamps the resolved per-style key
    # ("image_engine"); older job dirs fall back to a styles lookup, then the
    # flat mirror of the default style.
    engine_key = cfg.get("image_engine")
    if not engine_key:
        for s in cfg.get("styles") or []:
            if isinstance(s, dict) and s.get("name") == (cfg.get("style_name") or ""):
                engine_key = s.get("image_engine")
                break
    image_engine = _engines.resolve(cfg, engine_key or cfg.get("default_image_engine"))
    # Same three-step fallback for the style's video engine (scene I2V model).
    video_key = cfg.get("video_engine")
    if not video_key:
        for s in cfg.get("styles") or []:
            if isinstance(s, dict) and s.get("name") == (cfg.get("style_name") or ""):
                video_key = s.get("video_engine")
                break
    video_engine = _engines.resolve_video(cfg, video_key or cfg.get("default_video_engine"))
    tts_hosts     = cfg.get("tts_workers", [])
    worker_urls   = alive_workers(cfg.get("comfy_workers", []))

    res_name = cfg.get("resolution", _DEFAULT_RESOLUTION)
    vid_width, vid_height = _RESOLUTIONS.get(res_name, _RESOLUTIONS[_DEFAULT_RESOLUTION])
    # Snap to LTX's renderable grid so the FLUX first frame, the LTX clips and the
    # final video are all the same size (no silent shrink + rescale). See ltx_dimensions.
    vid_width, vid_height = ltx_dimensions(vid_width, vid_height)
    logger.info("Resolution: %s → %dx%d", res_name, vid_width, vid_height)
    logger.info("Workers: %s", worker_urls)
    logger.info("TTS hosts: %s", tts_hosts)

    plan_cfg = {
        **cfg,
        "title": title,
        "resolution": res_name,
        "vid_width": vid_width,
        "vid_height": vid_height,
        "voice_ref": voice_ref_str or "",
        "voice_robotic_amount": voice_robotic_amount,
        "voice_speed": voice_speed,
    }
    store.ensure_generation_plan(durable_job_id, work_dir, title, scenes, plan_cfg)
    store.recover_incomplete_tasks(durable_job_id)
    store.update_job(durable_job_id, status=JOB_RUNNING, progress_pct=0, progress_message="starting")

    # While the web UI is actively used, hold one worker idle for it (issue #98)
    # so cover/preview jobs don't queue behind this render. The idle window is the
    # configured timeout; reads the shared activity file the backend stamps.
    _ui_idle_timeout = float(cfg.get("ui_idle_timeout_seconds", ui_activity.DEFAULT_IDLE_TIMEOUT))
    worker_pool = WorkerPool(
        worker_urls,
        reserve_check=lambda: ui_activity.is_active(_ui_idle_timeout),
    )
    status_file = work_dir / "progress.json"

    # Work dir name → derive slug and stamp for final output path
    dir_name = work_dir.name
    # Find slug and stamp — the format is {slug}-{stamp} where stamp is YYYYMMDD-HHMMSS
    import re
    m = re.match(r"^(.+)-(\d{8}-\d{6})$", dir_name)
    if m:
        slug, stamp = m.group(1), m.group(2)
    else:
        slug, stamp = dir_name, "resumed"

    write_progress(status_file, 0, "Resume: starting…")

    # ── Performance films (script_mode = "performance") ─────────────────────
    # A wholly different pipeline: character portraits + dialogue → Ref2VA clips
    # that already carry their own speech. No first frames, no TTS, no music, no
    # mux. Handled here and returned so none of the narrated flow below runs.
    if scenes and all(_performance.is_performance(s) for s in scenes):
        perf_final = OUTPUT_DIR / f"{slug}-{stamp}.mp4"
        try:
            _run_performance_film(
                work_dir, scenes, plan_cfg, store=store, durable_job_id=durable_job_id,
                worker_pool=worker_pool, status_file=status_file,
                vid_width=vid_width, vid_height=vid_height,
                final_path=perf_final, cover_path=work_dir / "cover.png",
                image_engine=image_engine)
        finally:
            worker_pool.shutdown()
        size_mb = perf_final.stat().st_size / 1024 / 1024
        logger.info("DONE — %s (%.1f MB)", perf_final.name, size_mb)
        write_progress(status_file, 100, f"✅ Done — {perf_final.name} ({size_mb:.1f} MB)")
        store.update_job(durable_job_id, status=JOB_DONE, progress_pct=100,
                         progress_message=f"Done - {perf_final.name}", final_path=perf_final)
        print(f"\n✅ DONE: {perf_final}")
        return

    # ── Dialogue/performance scenes (talking-head via EchoMimic) ─────────────
    # Rendered up front to scene_NN_final.mp4; the narration/video/mux phases then
    # skip them (they operate on classic_scenes / find the final already present).
    # A narration-only script has dialogue_scenes == [] and classic_scenes ==
    # scenes, so everything below runs exactly as before.
    dialogue_scenes = [s for s in scenes if getattr(s, "mode", "narration") == "dialogue" and (s.lines or [])]
    classic_scenes = [s for s in scenes if s not in dialogue_scenes]
    dialogue_durs: dict[int, float] = {}
    total_lines = sum(len(s.lines or []) for s in dialogue_scenes)

    # Progress bands. Dialogue lines dominate a dialogue film's wall-clock, so
    # the dialogue phase gets a share of the bar proportional to its weight;
    # narration-only jobs keep the exact historical bands (invariant).
    if dialogue_scenes:
        units_dlg, units_classic = 5 * total_lines, 3 * len(classic_scenes)
        dlg_end = min(85.0, max(12.0, 2 + 88.0 * units_dlg / max(1, units_dlg + units_classic)))
        tts_band = (dlg_end, dlg_end + 0.18 * (92.0 - dlg_end))
        video_band = (tts_band[1], 92.0)
    else:
        dlg_end = 0.0
        tts_band = (0.0, 20.0)
        video_band = (35.0, 90.0)
    n_classic = max(1, len(classic_scenes))

    if dialogue_scenes:
        from contextlib import contextmanager

        from pipeline.dialogue_render import render_dialogue_scene, NARRATOR
        from pipeline.timing import humanize_eta

        echo_hosts = [h for h in (cfg.get("echomimic_workers") or []) if str(h).startswith(("http://", "https://"))]
        if not echo_hosts:
            raise RuntimeError("Dialogue scenes present but no echomimic_workers are configured")
        if not tts_hosts:
            raise RuntimeError("Dialogue scenes need a TTS worker but none are configured")
        voice_ref_for, make_still, prompt_for = _dialogue_resolvers(
            cfg, work_dir, voice_ref_str, vid_width=vid_width, vid_height=vid_height)
        tts_host = tts_hosts[0]
        timing_table = store.timing_table()
        lines_done = [0]
        _dlg_lock = threading.Lock()

        def _line_pct() -> float:
            return 2 + (dlg_end - 2) * (lines_done[0] / max(1, total_lines))

        def _make_line_cm(host):
            """A per-scene shot wrapper bound to that scene's echomimic host, so
            concurrently-rendering scenes each record the right worker. Shared
            counter + progress writes are locked (scenes run on separate threads)."""
            @contextmanager
            def line_cm(scene, idx, n_lines, speaker):
                silent = (speaker == "silent")
                who = "a silent motion shot" if silent else f"{speaker} speaks"
                t_id = task_id(durable_job_id, "scene", scene.id, f"line-{idx}")
                payload = {"work_dir": str(work_dir), "scene_id": scene.id, "line_index": idx,
                           "speaker": "" if silent else speaker, "silent": silent,
                           "vid_width": vid_width, "vid_height": vid_height,
                           "resource_class": "comfy:video" if silent else "echomimic"}
                # Upsert — jobs planned before per-line tasks existed lack the row.
                store.create_task(t_id, durable_job_id, "scene.dialogue.line",
                                  f"Scene {scene.id} · {who} ({idx + 1}/{n_lines})",
                                  worker_kind="comfy" if silent else "echomimic", payload=payload, max_attempts=2)
                sig = timing_signature("scene.dialogue.line", payload)
                entry = timing_table.get(sig) if sig else None
                est = (f" — ~{humanize_eta(entry['avg_seconds']).lstrip('~')} for this shot"
                       if entry and entry.get("sample_count") else "")
                with _dlg_lock:
                    write_progress(status_file, _line_pct(),
                                   f"Dialogue · scene {scene.id}: {who} "
                                   f"(shot {lines_done[0] + 1}/{total_lines}){est}")
                wid = worker_id("comfy", worker_pool.urls[0]) if silent else worker_id("echomimic", host)
                with TaskRun(store, t_id, worker_id_value=wid, lease_seconds=7200,
                             start_message=f"{who} — shot {idx + 1}/{n_lines}") as run:
                    yield
                    run.complete({"scene_id": scene.id, "line_index": idx}, "shot rendered")
                with _dlg_lock:
                    lines_done[0] += 1
                    write_progress(status_file, _line_pct(),
                                   f"Dialogue · scene {scene.id}: {who} done "
                                   f"({lines_done[0]}/{total_lines} shots)")
            return line_cm

        def silent_video(scene, shot, still, out_clip):
            """Render a silent shot (people move, no speech) as an LTX i2v clip
            from its still, reusing the classic scene-video path."""
            vp = (str((shot or {}).get("video_prompt") or "").strip()
                  or scene.video_prompt or scene.image_prompt or scene.title)
            dur = float((shot or {}).get("duration") or 0) or _SILENT_DEFAULT_SECS
            shot_scene = Scene(id=scene.id, title=scene.title, image_prompt="",
                               video_prompt=vp, narration="", negative_prompt=NEGATIVE_PROMPT)
            url = worker_pool.acquire()
            try:
                raw, _amb = _generate_scene_video(
                    shot_scene, work_dir, dur, vid_width, vid_height,
                    max_clip_secs, lora_strength, first_pass_cfg, first_pass_steps,
                    second_pass_cfg, second_pass_steps, url,
                    scene_first_frame=Path(still), image_engine=image_engine,
                    video_engine=video_engine,
                )
            finally:
                worker_pool.release(url)
            if Path(raw) != Path(out_clip):
                shutil.move(str(raw), str(out_clip))
            return Path(out_clip)

        from app import _scene_establishing_frame
        from pipeline.comfyui import generate_keyframed_clip
        _establish_secs = float(cfg.get("dialogue_establishing_seconds", 2.5) or 0)

        def _first_shot_still(scene):
            """The first talking shot's solo close-up still — the frame the scene's
            first talking-head line then animates. Only a real in-scene shot still
            (not the multi-person scene frame or a portrait) is a good push-in
            target, so anything else returns None."""
            first = None
            for ln in (scene.lines or []):
                ln = ln or {}
                silent = bool(ln.get("silent")) and not str(ln.get("text") or "").strip()
                if silent or str(ln.get("text") or "").strip():
                    first = ln
                    break
            if first is None:
                return None
            speaker = "" if (bool(first.get("silent")) and not str(first.get("text") or "").strip()) \
                else str(first.get("speaker") or NARRATOR).strip()
            try:
                still = make_still(scene, speaker, 0)
            except Exception:
                return None
            return still if still and still.name.endswith("_line_00_shot.png") else None

        def establishing(scene):
            """Open on the scene's wide establishing frame and push the camera in to
            the first speaker's close-up — a REAL LTX first→last keyframe move, so
            the setting is shown first, never lost, and the push lands exactly on the
            still the talking head then animates (seamless arrival)."""
            if _establish_secs <= 0:
                return None
            wide = _scene_establishing_frame(
                work_dir, scene.id, {"preview_path": getattr(scene, "preview_path", "")},
                vid_width, vid_height)
            if wide is None:
                return None
            close = _first_shot_still(scene)
            # No distinct in-scene close-up to push toward (the talking head would
            # animate the wide frame itself) — skip the beat rather than render a
            # pointless hold.
            if close is None or Path(close) == Path(wide):
                return None
            out = work_dir / f"scene_{scene.id:02d}_establish.mp4"
            url = worker_pool.acquire()
            try:
                generate_keyframed_clip(
                    first_frame_path=wide, last_frame_path=close, output_path=out,
                    positive_prompt=(
                        "slow cinematic push-in from the wide establishing shot toward "
                        "the speaker, steady smooth camera move, the setting and people "
                        "stay consistent, subtle natural motion"),
                    negative_prompt="static, jump cut, warp, morph, distortion, flicker, "
                                    + ((cfg.get("video_negative_prompt") or "").strip() or NEGATIVE_PROMPT),
                    width=vid_width, height=vid_height,
                    duration_seconds=_establish_secs,
                    lora_strength=lora_strength, comfy_url=url,
                )
            finally:
                worker_pool.release(url)
            return out

        def _render_one(i: int, s: Scene) -> tuple[int, float]:
            host = echo_hosts[i % len(echo_hosts)]
            final = work_dir / f"scene_{s.id:02d}_final.mp4"
            if not (final.exists() and final.stat().st_size > 10_000):
                render_dialogue_scene(
                    s, work_dir,
                    voice_ref_for=voice_ref_for, make_still=make_still, prompt_for=prompt_for,
                    echomimic_host=host,
                    tts_host=tts_host, tts_engine=tts_engine, tts_language=tts_language,
                    canvas=(vid_width, vid_height),
                    line_cm=_make_line_cm(host), silent_video=silent_video,
                    establishing=establishing,
                )
            else:
                with _dlg_lock:
                    lines_done[0] += len(s.lines or [])  # resumed scene — lines already done
            # EchoMimic output is portrait-shaped; fit it onto the film's canvas
            # (blurred pillarbox) so the cross-scene concat gets uniform dims.
            fit_video_canvas(final, vid_width, vid_height)
            return s.id, _get_duration(final)

        # Render scenes CONCURRENTLY, one per echomimic worker, so the whole
        # fleet is used instead of a single busy worker while the rest idle.
        n_parallel = max(1, min(len(echo_hosts), len(dialogue_scenes)))
        write_progress(status_file, 1,
                       f"Rendering {len(dialogue_scenes)} dialogue scene(s) across "
                       f"{n_parallel} worker(s) — {total_lines} shot(s)…")
        dlg_pool = concurrent.futures.ThreadPoolExecutor(max_workers=n_parallel)
        try:
            futs = {dlg_pool.submit(_render_one, i, s): s for i, s in enumerate(dialogue_scenes)}
            done_scenes = 0
            for fut in concurrent.futures.as_completed(futs):
                sid, dur = fut.result()
                dialogue_durs[sid] = dur
                done_scenes += 1
                with _dlg_lock:
                    write_progress(status_file, _line_pct(),
                                   f"Dialogue scene {sid} assembled ({done_scenes}/{len(dialogue_scenes)})")
        finally:
            dlg_pool.shutdown(wait=True)

    # ── Narrations (0–20%) ───────────────────────────────────────────────────
    narration_paths: dict[int, Path] = {}
    narration_durs:  dict[int, float] = {}

    def _tts_scene(scene: Scene, primary_host: str) -> tuple[int, Path]:
        out = work_dir / f"scene_{scene.id:02d}_narration.wav"
        narration_task = task_id(durable_job_id, "scene", scene.id, "narration")
        if out.exists() and out.stat().st_size > 1000:
            logger.info("Scene %d narration exists (%d KB), skipping TTS",
                        scene.id, out.stat().st_size // 1024)
            dur = _get_duration(out)
            store.complete_task(narration_task, result={"path": str(out), "duration": dur, "skipped": True})
            store.record_artifact(durable_job_id, narration_task, "narration", out, duration_seconds=dur)
            return scene.id, out
        if getattr(scene, "mode", "narration") == "silent":
            # No voice-over by design: a silent track of the scene's duration keeps
            # the duration/mux/concat pipeline unchanged without speaking anything.
            secs = float(getattr(scene, "duration", 0) or 0) or _SILENT_DEFAULT_SECS
            _write_silence_wav(out, secs)
            logger.info("Scene %d is silent — %.1fs silence instead of TTS", scene.id, secs)
            store.complete_task(narration_task, result={"path": str(out), "duration": secs, "silent": True})
            store.record_artifact(durable_job_id, narration_task, "narration", out, duration_seconds=secs)
            return scene.id, out
        hosts_to_try = [primary_host] + [h for h in tts_hosts if h != primary_host]
        last_err: Exception | None = None
        for host in hosts_to_try:
            wid = worker_id("tts", host)
            store.register_worker(wid, "tts", host)
            try:
                ref = Path(voice_ref_str) if voice_ref_str and Path(voice_ref_str).exists() else None
                with TaskRun(
                    store,
                    narration_task,
                    worker_id_value=wid,
                    lease_seconds=600,
                    start_message=f"TTS on {host}",
                ) as run:
                    generate_narration(spoken_source(scene.narration, scene.metadata_extra.get("tts_text")), out, reference_wav=ref, host=host, robotic_amount=voice_robotic_amount, speed=voice_speed, tts_engine=tts_engine, language=tts_language, sentence_pause=tts_sentence_pause, cadence_voice=voice_name)
                    dur = _get_duration(out)
                    store.record_artifact(
                        durable_job_id,
                        narration_task,
                        "narration",
                        out,
                        duration_seconds=dur,
                    )
                    run.complete({"path": str(out), "duration": dur, "host": host}, "narration ready")
                return scene.id, out
            except Exception as e:
                logger.warning("TTS failed on %s for scene %d: %s", host, scene.id, e)
                last_err = e
        raise RuntimeError(f"TTS failed on all hosts for scene {scene.id}: {last_err}")

    write_progress(status_file, tts_band[0], f"Generating {len(classic_scenes)} narrations…")
    if not tts_hosts:
        raise RuntimeError("No TTS workers configured")
    tts_pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(n, len(tts_hosts)))
    tts_pending = {
        tts_pool.submit(_tts_scene, scene, tts_hosts[i % len(tts_hosts)]): scene
        for i, scene in enumerate(classic_scenes)
    }
    tts_done = 0
    try:
        while tts_pending:
            done_futs, _ = concurrent.futures.wait(
                list(tts_pending.keys()), timeout=30,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done_futs:
                scene = tts_pending.pop(fut)
                sid, out = fut.result()
                dur = _get_duration(out)
                narration_paths[sid] = out
                narration_durs[sid]  = dur
                tts_done += 1
                pct = tts_band[0] + (tts_band[1] - tts_band[0]) * tts_done / n_classic
                write_progress(status_file, pct, f"Narration {sid}/{n} done — {dur:.1f}s ({tts_done}/{n})")
    finally:
        tts_pool.shutdown(wait=False)

    total_dur = sum(narration_durs.values()) + sum(dialogue_durs.values())
    logger.info("All narrations done — %.1fs total", total_dur)
    write_progress(status_file, tts_band[1], f"Narrations done — {total_dur:.0f}s, generating video…")

    # ── Background music (20–35%) ────────────────────────────────────────────
    music_dur  = max(total_dur * 1.05, 30.0)
    music_path = work_dir / "background_music.wav"
    title = title or (scenes[0].title.split(":")[0] if scenes else "Australia")

    if music_path.exists() and music_path.stat().st_size > 10_000:
        logger.info("Music already exists (%.1f MB), skipping", music_path.stat().st_size / 1024 / 1024)
        music_task = task_id(durable_job_id, "music")
        store.complete_task(
            music_task,
            result={"path": str(music_path), "duration": _get_duration(music_path), "skipped": True},
        )
        store.record_artifact(
            durable_job_id,
            music_task,
            "music",
            music_path,
            duration_seconds=_get_duration(music_path),
        )
    else:
        write_progress(status_file, tts_band[1], f"Generating background music ({music_dur:.0f}s)…")
        _MAX_MUSIC_ATTEMPTS = 3
        music_task = task_id(durable_job_id, "music")
        store.update_task_payload(
            music_task,
            {
                "duration_seconds": music_dur,
                "output_path": str(music_path),
                "music_desc": cfg.get("music_desc") or "",
            },
        )
        for attempt in range(1, _MAX_MUSIC_ATTEMPTS + 1):
            music_url = worker_pool.acquire()
            music_worker = worker_id("comfy", music_url)
            store.register_worker(music_worker, "comfy", music_url)
            write_progress(
                status_file,
                20,
                f"Generating background music ({music_dur:.0f}s) on {music_url} "
                f"(attempt {attempt}/{_MAX_MUSIC_ATTEMPTS})…",
            )
            try:
                with TaskRun(
                    store,
                    music_task,
                    worker_id_value=music_worker,
                    lease_seconds=900,
                    start_message=f"music on {music_url}",
                ) as run:
                    generate_music(title, music_dur, music_path, cfg.get("music_desc") or None, comfy_url=music_url)
                    actual_music_dur = _get_duration(music_path)
                    store.record_artifact(
                        durable_job_id,
                        music_task,
                        "music",
                        music_path,
                        duration_seconds=actual_music_dur,
                    )
                    run.complete(
                        {"path": str(music_path), "duration": actual_music_dur, "worker": music_url},
                        "music ready",
                    )
                worker_pool.release(music_url)
                break
            except Exception as e:
                logger.warning("Music attempt %d/%d failed on %s: %s", attempt, _MAX_MUSIC_ATTEMPTS, music_url, e)
                first_line = str(e).splitlines()[0][:180]
                if isinstance(e, StuckJobError) or any(kw in str(e) for kw in _WORKER_ERR_KEYWORDS):
                    worker_pool.mark_failed(music_url)
                else:
                    worker_pool.release(music_url)
                if attempt == _MAX_MUSIC_ATTEMPTS:
                    write_progress(status_file, tts_band[1], f"Background music failed on {music_url}: {first_line}")
                    raise
                write_progress(
                    status_file,
                    tts_band[1],
                    f"Background music attempt {attempt}/{_MAX_MUSIC_ATTEMPTS} failed on {music_url}; "
                    f"retrying. {first_line}",
                )

    write_progress(status_file, video_band[0], "Music ready. Generating cover image and scene videos…")

    # ── Cover image (at ~35%, non-blocking, non-fatal) ───────────────────────
    cover_path = work_dir / "cover.png"
    generate_cover_image(work_dir, cfg, scenes, image_engine=image_engine,
                         worker_pool=worker_pool, vid_width=vid_width,
                         vid_height=vid_height, title=title)

    write_progress(status_file, video_band[0], "Music ready. Generating scene videos…")

    # ── Video generation (35–90%) ────────────────────────────────────────────
    scene_raws_map: dict[int, Path] = {}
    scene_ambient_map: dict[int, Path | None] = {}

    def _run_scene(scene: Scene) -> tuple[int, Path, Path | None]:
        existing = work_dir / f"scene_{scene.id:02d}_video.mp4"
        image_task = task_id(durable_job_id, "scene", scene.id, "image")
        video_task = task_id(durable_job_id, "scene", scene.id, "video")
        first_frame_path = work_dir / f"scene_{scene.id:02d}_first_frame.png"

        def _persist_first_frame(frame: Path) -> None:
            """Record the first-frame image the video actually used as this scene's
            preview, so loading the script shows exactly that frame and doesn't
            regenerate it. Keyed by the same job_id load_script reads."""
            try:
                store.update_scene_preview(durable_job_id, scene.id, frame)
            except Exception:
                logger.debug("Could not persist preview for scene %d", scene.id, exc_info=True)

        # The first frame (fast FLUX) and the video (slow LTX) are produced in one
        # _generate_scene_video call, but tracked as two tasks. Complete the image
        # task the instant FLUX lands rather than after the video — otherwise its
        # measured duration ≈ the video's and the ETA learns image≈video.
        image_done = [False]

        def _finish_image(frame: Path) -> None:
            if image_done[0]:
                return
            image_done[0] = True
            store.complete_task(image_task, result={"path": str(frame)})
            store.record_artifact(durable_job_id, image_task, "image", frame)
            _persist_first_frame(frame)

        if existing.exists() and existing.stat().st_size > 10_000:
            logger.info("Scene %d video exists (%d KB), skipping", scene.id, existing.stat().st_size // 1024)
            if first_frame_path.exists():
                store.complete_task(image_task, result={"path": str(first_frame_path), "skipped": True})
                store.record_artifact(durable_job_id, image_task, "image", first_frame_path)
                _persist_first_frame(first_frame_path)
            store.complete_task(video_task, result={"path": str(existing), "skipped": True})
            store.record_artifact(durable_job_id, video_task, "scene_video", existing, duration_seconds=_get_duration(existing))
            return scene.id, existing, None

        single_clip = work_dir / f"scene_{scene.id:02d}_clip_01.mp4"
        clip_02 = work_dir / f"scene_{scene.id:02d}_clip_02.mp4"
        if (single_clip.exists() and single_clip.stat().st_size > 10_000
                and not clip_02.exists()):
            amb = work_dir / f"scene_{scene.id:02d}_ambient.wav"
            logger.info("Scene %d single-clip exists (%d KB), skipping",
                        scene.id, single_clip.stat().st_size // 1024)
            if first_frame_path.exists():
                store.complete_task(image_task, result={"path": str(first_frame_path), "skipped": True})
                store.record_artifact(durable_job_id, image_task, "image", first_frame_path)
                _persist_first_frame(first_frame_path)
            store.complete_task(video_task, result={"path": str(single_clip), "skipped": True})
            store.record_artifact(durable_job_id, video_task, "scene_video", single_clip, duration_seconds=_get_duration(single_clip))
            return scene.id, single_clip, amb if amb.exists() else None

        # Check if a preview image exists for this scene and matches the target resolution.
        # Images generated during Script review may be at a different (landscape) resolution —
        # if they don't match we force regeneration at the correct resolution.
        scene_first_frame: Path | None = None
        for ext in ("_preview.png", "_first_frame.png"):
            p = work_dir / f"scene_{scene.id:02d}{ext}"
            if not p.exists():
                continue
            if _image_matches_resolution(p, vid_width, vid_height):
                scene_first_frame = p
                break
            logger.info(
                "Scene %d: ignoring %s — dimensions don't match target %dx%d, will regenerate",
                scene.id, p.name, vid_width, vid_height,
            )

        last_err: Exception | None = None
        for attempt in range(1, _MAX_SCENE_ATTEMPTS + 1):
            if not worker_pool.has_healthy():
                raise RuntimeError(f"All workers failed — last error: {last_err}")
            url = worker_pool.acquire()
            comfy_worker = worker_id("comfy", url)
            store.register_worker(comfy_worker, "comfy", url)
            try:
                logger.info("Scene %d attempt %d/%d on %s", scene.id, attempt, _MAX_SCENE_ATTEMPTS, url)
                store.update_task_payload(
                    video_task,
                    {
                        "narration_duration": narration_durs[scene.id],
                        "vid_width": vid_width,
                        "vid_height": vid_height,
                        "max_clip_secs": max_clip_secs,
                        "lora_strength": lora_strength,
                        "first_pass_cfg": first_pass_cfg,
                        "first_pass_steps": first_pass_steps,
                        "second_pass_cfg": second_pass_cfg,
                        "second_pass_steps": second_pass_steps,
                    },
                )
                image_done[0] = False
                if scene_first_frame and scene_first_frame.exists():
                    store.complete_task(image_task, result={"path": str(scene_first_frame), "skipped": True})
                    store.record_artifact(durable_job_id, image_task, "image", scene_first_frame)
                    _persist_first_frame(scene_first_frame)
                    image_done[0] = True
                else:
                    store.start_task(
                        image_task,
                        worker_id_value=comfy_worker,
                        lease_seconds=900,
                        message=f"first frame on {url}",
                    )
                with TaskRun(
                    store,
                    video_task,
                    worker_id_value=comfy_worker,
                    # Slow engines (MiniMax H3) declare a longer lease so the
                    # controller doesn't re-lease the scene mid-render.
                    lease_seconds=int(video_engine.get("lease_seconds") or 3600),
                    start_message=f"video on {url}",
                ) as run:
                    sf, sa = _generate_scene_video(
                        scene, work_dir,
                        narration_durs[scene.id],
                        vid_width, vid_height, max_clip_secs,
                        lora_strength, first_pass_cfg, first_pass_steps,
                        second_pass_cfg, second_pass_steps,
                        comfy_url=url,
                        scene_first_frame=scene_first_frame,
                        image_engine=image_engine,
                        on_first_frame=_finish_image,
                        video_engine=video_engine,
                    )
                    store.record_artifact(
                        durable_job_id,
                        video_task,
                        "scene_video",
                        sf,
                        duration_seconds=_get_duration(sf),
                    )
                    if sa:
                        store.record_artifact(
                            durable_job_id,
                            video_task,
                            "ambient",
                            sa,
                            duration_seconds=_get_duration(sa),
                        )
                    run.complete(
                        {"path": str(sf), "ambient_path": str(sa) if sa else "", "worker": url},
                        "scene video ready",
                    )
                return scene.id, sf, sa
            except Exception as e:
                if image_done[0]:
                    pass  # first frame already finished; only the video failed
                elif first_frame_path.exists():
                    _finish_image(first_frame_path)
                else:
                    store.fail_task(image_task, e, retryable=True)
                last_err = e
                is_worker_fault = (
                    isinstance(e, StuckJobError)
                    or any(kw in str(e) for kw in _WORKER_ERR_KEYWORDS)
                )
                if is_worker_fault:
                    logger.warning("Worker %s unhealthy for scene %d (attempt %d/%d): %s — removing",
                                   url, scene.id, attempt, _MAX_SCENE_ATTEMPTS, e)
                    worker_pool.mark_failed(url)
                else:
                    logger.warning("Scene %d failed on %s (attempt %d/%d): %s — retrying",
                                   scene.id, url, attempt, _MAX_SCENE_ATTEMPTS, e)
                    if attempt < _MAX_SCENE_ATTEMPTS:
                        time.sleep(5)
            finally:
                worker_pool.release(url)
        raise RuntimeError(f"Scene {scene.id} failed after {_MAX_SCENE_ATTEMPTS} attempts: {last_err}")

    n_workers = len(worker_pool.urls)
    write_progress(status_file, video_band[0], f"Generating {len(classic_scenes)} scenes across {n_workers} worker(s)…")

    scene_pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(n, max(1, len(worker_pool.urls))))
    pending: dict[concurrent.futures.Future, Scene] = {
        scene_pool.submit(_run_scene, scene): scene for scene in classic_scenes
    }
    completed = 0
    first_error: Exception | None = None
    last_yield = time.time()

    try:
        while pending:
            done, _ = concurrent.futures.wait(
                list(pending.keys()), timeout=30,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done:
                scene = pending.pop(fut)
                try:
                    sid, scene_raw, scene_amb = fut.result()
                    scene_raws_map[sid]    = scene_raw
                    scene_ambient_map[sid] = scene_amb
                    completed += 1
                    pct = video_band[0] + (video_band[1] - video_band[0]) * completed / n_classic
                    write_progress(status_file, pct, f"Scene {sid}/{n} complete ✓  ({completed}/{n} done)")
                    last_yield = time.time()
                except Exception as e:
                    logger.error("Scene %d failed permanently: %s", scene.id, e)
                    if first_error is None:
                        first_error = e
                    write_progress(status_file, video_band[0] + (video_band[1] - video_band[0]) * completed / n_classic,
                                   f"Scene {scene.id} FAILED: {e}")
                    last_yield = time.time()
            now = time.time()
            if pending and first_error is None and (now - last_yield >= 30):
                running = sorted(pending[f].id for f in pending)
                write_progress(status_file, video_band[0] + (video_band[1] - video_band[0]) * completed / n_classic,
                               f"Scenes {running} generating… ({completed}/{n} done)")
                last_yield = now
    finally:
        scene_pool.shutdown(wait=False)

    if first_error is not None:
        raise first_error

    # ── Mux narrations into scene videos ────────────────────────────────────
    scene_finals: list[Path] = []
    for s in scenes:
        scene_final = work_dir / f"scene_{s.id:02d}_final.mp4"
        # Dialogue scenes were rendered in the pre-pass and their artifacts are
        # tracked per line — they have NO mux task, so recording a scene_final
        # artifact against one fails the FK. Just collect the finished clip.
        if getattr(s, "mode", "narration") == "dialogue" and (s.lines or []):
            scene_finals.append(scene_final)
            continue
        raw = scene_raws_map.get(s.id)
        mux_task = task_id(durable_job_id, "scene", s.id, "mux")
        is_last = s.id == scenes[-1].id
        if scene_final.exists() and scene_final.stat().st_size > 10_000:
            logger.info("Scene %d muxed final exists, skipping", s.id)
            store.complete_task(mux_task, result={"path": str(scene_final), "skipped": True})
            store.record_artifact(durable_job_id, mux_task, "scene_final", scene_final, duration_seconds=_get_duration(scene_final))
        else:
            with TaskRun(
                store,
                mux_task,
                worker_id_value=worker_id("local", "assembler"),
                lease_seconds=600,
                start_message="muxing scene",
            ) as run:
                mux_video_audio(raw, narration_paths[s.id], scene_final,
                                extra_tail_secs=FINAL_SCENE_TAIL_SECS if is_last else 0.0)
                store.record_artifact(
                    durable_job_id,
                    mux_task,
                    "scene_final",
                    scene_final,
                    duration_seconds=_get_duration(scene_final),
                )
                run.complete({"path": str(scene_final)}, "scene muxed")
        scene_finals.append(scene_final)

    scene_ambient_wavs = [scene_ambient_map[s.id] for s in scenes if scene_ambient_map.get(s.id)]

    # ── Final assembly (90–100%) ─────────────────────────────────────────────
    combined     = work_dir / "combined.mp4"
    ambient_path: Path | None = work_dir / "ambient.wav"
    final_path   = OUTPUT_DIR / f"{slug}-{stamp}.mp4"

    if scene_ambient_wavs:
        try:
            if len(scene_ambient_wavs) == 1:
                shutil.copy2(scene_ambient_wavs[0], ambient_path)
            else:
                concat_audio(scene_ambient_wavs, ambient_path)
        except Exception:
            logger.warning("Could not assemble ambient.wav")
            ambient_path = None
    else:
        ambient_path = None

    ambient_vol = cfg.get("ambient_vol", 0) / 100.0
    final_task = task_id(durable_job_id, "final")
    store.update_task_payload(
        final_task,
        {
            "scene_count": len(scenes),
            "final_path": str(final_path),
            "music_path": str(music_path),
            "music_vol": music_vol,
            "voice_vol": voice_vol,
            "ambient_vol": ambient_vol,
            "vid_width": vid_width,
            "vid_height": vid_height,
            "ambient_path": str(ambient_path) if ambient_path else "",
        },
    )
    write_progress(status_file, max(90.0, video_band[1]), "Concatenating scenes…")
    with TaskRun(
        store,
        final_task,
        worker_id_value=worker_id("local", "assembler"),
        lease_seconds=900,
        start_message="assembling final video",
    ) as final_run:
        concatenate_scenes(scene_finals, combined)

        write_progress(status_file, 95, f"Mixing audio (voice {voice_vol*100:.0f}%, music {music_vol*100:.0f}%)…")
        mix_background_music(
            combined, music_path, final_path,
            volume=music_vol, voice_volume=voice_vol,
            ambient_path=ambient_path, ambient_volume=ambient_vol,
        )
        ensure_video_resolution(final_path, vid_width, vid_height)
        # Per-style automation: burn the cover into the head of the film —
        # YouTube Shorts ignore uploaded thumbnails and pick their own frame.
        # Frames are overlaid (not prepended) so caption timing stays valid.
        # Non-fatal: a finished film without the stamp beats a failed render.
        ff_cover = str(cfg.get("first_frame_cover")
                       or cfg.get("default_first_frame_cover") or "none").strip().lower()
        if ff_cover in ("image", "text"):  # legacy "text" burns the cover image too
            write_progress(status_file, 97, "Burning cover into the first frame…")
            try:
                _burn_first_frame(
                    final_path,
                    cover_path=cover_path,
                    seconds=cfg.get("first_frame_cover_seconds",
                                    cfg.get("default_first_frame_cover_seconds")),
                )
            except Exception as ff_err:
                logger.warning("First-frame cover failed (non-fatal): %s", ff_err)
        store.record_artifact(
            durable_job_id,
            final_task,
            "final_video",
            final_path,
            duration_seconds=_get_duration(final_path),
        )
        final_run.complete({"path": str(final_path), "combined": str(combined)}, "final video ready")

    size_mb = final_path.stat().st_size / 1024 / 1024
    logger.info("DONE — %s (%.1f MB)", final_path.name, size_mb)
    write_progress(status_file, 100, f"✅ Done — {final_path.name} ({size_mb:.1f} MB)")
    store.update_job(durable_job_id, status=JOB_DONE, progress_pct=100, progress_message=f"Done - {final_path.name}", final_path=final_path)
    try:
        (work_dir / "job.json").write_text(json.dumps({
            "work_dir": str(work_dir),
            "status": "done",
            "final_path": str(final_path),
            "updated_at": time.time(),
        }, indent=2))
    except Exception:
        pass
    print(f"\n✅ DONE: {final_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <work_dir>", file=sys.stderr)
        sys.exit(1)
    work_dir = Path(sys.argv[1]).expanduser().resolve()
    if not work_dir.is_dir():
        print(f"Not a directory: {work_dir}", file=sys.stderr)
        sys.exit(1)
    try:
        main(work_dir)
    except Exception as exc:
        try:
            if _PROGRESS_STORE is not None and _PROGRESS_JOB_ID is not None:
                _PROGRESS_STORE.update_job(
                    _PROGRESS_JOB_ID,
                    status=JOB_ERROR,
                    progress_message=str(exc).splitlines()[0][:300],
                    error=str(exc),
                )
            (work_dir / "job.json").write_text(json.dumps({
                "work_dir": str(work_dir),
                "status": "error",
                "error": str(exc),
                "updated_at": time.time(),
            }, indent=2))
            write_progress(work_dir / "progress.json", 0, f"Error: {str(exc).splitlines()[0][:300]}")
        except Exception:
            pass
        raise
