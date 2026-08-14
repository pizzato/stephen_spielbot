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
    write_silence_wav as _write_silence_wav,
)
from pipeline import cadence as _cadence
from pipeline import scene_context as _scene_context
from pipeline.tts_text import spoken_source
from pipeline.tts_worker import generate_narration, resolve_robotic_amount
from pipeline.orchestrator import (
    DurableStore, TaskRun,
    JOB_DONE, JOB_ERROR, JOB_RUNNING,
    job_id_from_work_dir, task_id, worker_id,
)
from pipeline.scene_video import generate_scene_video as _generate_scene_video
from pipeline.worker_pool import WorkerPool, alive_workers
from pipeline import ui_activity
# Resolution name → (w, h) map. Import the canonical table from app rather than
# keeping a copy here — a stale local copy silently dropped the 720p tier and
# rendered every 720p job at the 1920×1080 fallback (wrong size and orientation).
from app import _RESOLUTIONS, _DEFAULT_RESOLUTION
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

# _write_silence_wav lives in pipeline.assembler so the web backend's
# per-scene re-render shares it (imported above).


def chain_scenes_flag(cfg: dict, style_name: str = "") -> bool:
    """The style's ``h3_chain_scenes``, for the ACTED path.

    Flat key first (stamped into job_config at render start), styles lookup for
    older job dirs — the flat mirror alone only ever carries the DEFAULT style,
    so reading it without the fallback silently ignores a per-style toggle.
    Reference engines are always MiniMax, so unlike a narrated scene there is no
    engine-family carve-out: the toggle alone decides.
    """
    flag = cfg.get("h3_chain_scenes")
    if flag is None:
        for s in cfg.get("styles") or []:
            if isinstance(s, dict) and s.get("name") == (cfg.get("style_name") or style_name or ""):
                flag = s.get("h3_chain_scenes")
                break
    return bool(flag)


def ensure_opening_frame(scene, work_dir: Path, cfg: dict, *, comfy_url: str,
                         vid_width: int, vid_height: int) -> Path | None:
    """The image a SILENT acted scene opens on, generated if it isn't there yet.

    Ref2VA has no literal first-frame input, but the scene's own image rides as
    a reference that defines the opening composition (picture_role's "frame"
    role: "begin the take looking like this picture"). For a silent beat that
    is what carries the shot — the image prompt still composes the scene, and
    for a beat with nobody in it the frame is the only reference the take has.

    A dialogue scene is untouched: its pictures ARE the portraits, and the
    Create screen's preview (when one exists) already rides along.

    Returns the frame, or None when there is nothing to make one from.
    """
    if not _performance.is_silent(scene):
        return None
    # Any existing image will do — unlike the I2V path this is a REFERENCE, not
    # frame zero, so an off-resolution preview from the Create screen is still
    # the composition the take should open on (and resolve_performance_references
    # picks the preview first either way — regenerating here would burn a FLUX
    # render nothing then uses).
    for ext in ("_preview.png", "_first_frame.png"):
        existing = work_dir / f"scene_{scene.id:02d}{ext}"
        if existing.exists() and existing.stat().st_size > 0:
            return existing
    prompt = str(getattr(scene, "image_prompt", "") or "").strip()
    if not prompt:
        logger.info("Scene %d: silent scene has no image prompt — the take opens "
                    "on its references alone", scene.id)
        return None
    engine = _engines.resolve(cfg, cfg.get("image_engine"))
    out = work_dir / f"scene_{scene.id:02d}_first_frame.png"
    logger.info("Scene %d: opening frame for the silent take (%s) on %s",
                scene.id, engine.get("key"), comfy_url)
    generate_with_engine(engine, prompt, out, width=vid_width, height=vid_height,
                         comfy_url=comfy_url)
    return out


def render_performance_scene(scene: Scene, work_dir: Path, cfg: dict, *,
                             comfy_url: str, vid_width: int, vid_height: int,
                             style_name: str = "", direction: str = "") -> Path:
    """Render ONE performance scene: character portraits + dialogue → a clip that
    already contains its own speech. Returns the finished scene_NN_final.mp4.

    Shared with the backend's per-scene re-render, so it takes everything it
    needs as arguments and touches no module state. *direction* is the editor's
    note for THIS take ("Shoot again" with an instruction) — it reaches the
    model as the prompt's [DIRECTION] block and is not persisted to the scene.
    """
    # Fills in cast/length/setting for a dialogue scene authored in a MIXED
    # film, where only the lines and the classic prompts exist. Chaining is
    # read here too: it decides how long a SILENT scene is allowed to be, and
    # clamping before the renderer sees it would cut the scene short.
    scene_meta = _performance.acted_meta(scene, chained=chain_scenes_flag(cfg, style_name))
    if direction.strip():
        scene_meta["direction"] = direction.strip()
    ensure_opening_frame(scene, work_dir, cfg, comfy_url=comfy_url,
                         vid_width=vid_width, vid_height=vid_height)
    # One scene = ONE generation, whole conversation in a single continuous
    # clip (the user's call: shot/reverse-shot splitting kept identities safe
    # but broke scenes apart). The splitter remains available per config
    # (performance_shot_split) for content where identity outranks flow;
    # in one-clip mode the identity locks and the gate carry the swap risk.
    if bool(cfg.get("performance_shot_split", False)):
        shots = _performance.shots_for(
            scene_meta, establishing=bool(cfg.get("performance_establishing", True)))
    else:
        shots = [dict(scene_meta)]
    if len(shots) > 1:
        final = _render_performance_shots(
            scene, shots, scene_meta, work_dir, cfg, comfy_url=comfy_url,
            vid_width=vid_width, vid_height=vid_height, style_name=style_name)
    else:
        final = _render_performance_clip(
            scene, shots[0], work_dir, cfg, work_dir / f"scene_{scene.id:02d}_final.mp4",
            comfy_url=comfy_url, vid_width=vid_width, vid_height=vid_height,
            style_name=style_name)
    # Bind the continuation point to the clip that is actually in the cut.
    _scene_context.stamp_final(work_dir, scene.id, final)
    return final


# What every continuation is told before the editor's own note: it is not a new
# scene, and re-establishing one is exactly the failure the pinned frames exist
# to prevent.
_CONTINUATION_DIRECTION = (
    "This is a continuation of a take already in progress — the camera has not "
    "cut and nothing restarts. Carry straight on from the moment on screen, in "
    "the same room, the same framing and the same light, with everyone exactly "
    "where they already are. Do not re-establish the scene and do not "
    "re-introduce anyone.")


def continue_performance_scene(scene, work_dir: Path, cfg: dict, *, comfy_url: str,
                               vid_width: int, vid_height: int, style_name: str,
                               ctx: dict, lines: list, seconds: float,
                               direction: str = "") -> Path:
    """Shoot MORE of an acted scene that is already in the cut.

    One Ref2VA clip continuing the motion context *ctx* points at, written to
    scene_NN_continue.mp4 for the caller to join onto the scene's final. The
    scene's own portraits and voices are wired in again, so the people who come
    out of the join are the people who went into it; *lines* is what they say
    next (empty for a held beat) and *direction* the editor's note.
    """
    from pipeline.comfyui import generate_video_h3_ref_continue
    from app import resolve_performance_references

    meta = {
        **_performance.acted_meta(scene),
        "lines": _performance.norm_lines(lines),
        # The action beat and the framing belong to the frames already pinned in
        # front of this clip; a fresh beat would fight them.
        "beats": [],
        "seconds": seconds,
        "direction": " ".join(x for x in (_CONTINUATION_DIRECTION, direction.strip()) if x),
    }
    # A hand-edited prompt is a prompt for the ORIGINAL take, lines and all —
    # honouring it here would have the scene play its opening again.
    meta.pop("prompt_override", None)

    refs = resolve_performance_references(meta, cfg, work_dir, style_name, scene_id=scene.id)
    ref_images = [Path(p["path"]) for p in refs["pictures"]]
    ref_audios = [Path(a["path"]) for a in refs["audios"]]
    if not ref_images:
        raise RuntimeError(
            f"Scene {scene.id}: no character portrait resolved — a continuation "
            "needs the same references the take was shot with.")

    # The engine the take was shot on, not whatever the style points at today:
    # a continuation sampled differently from the clip it joins is a visible
    # change of look mid-scene.
    engine = _engines.resolve_reference(cfg, ctx.get("engine") or cfg.get("reference_engine"))
    prompt = _performance.build_h3_prompt(
        meta, style_note=cfg.get("style", ""), picture_names=refs["pictures"],
        audio_names=[a["name"] for a in refs["audios"]])

    out = work_dir / f"scene_{scene.id:02d}_continue.mp4"
    logger.info("Scene %d: continuing the take (+%.1fs, %d line(s)) from %s on %s",
                scene.id, seconds, len(meta["lines"]), ctx.get("latent"), comfy_url)
    generate_video_h3_ref_continue(
        engine, prompt, ref_images, out,
        context_latent=str(ctx.get("latent") or ""),
        context_token=str(ctx.get("token") or ""),
        clip_index=int(ctx.get("next_index") or 2),
        ref_audios=ref_audios, width=vid_width, height=vid_height,
        duration_seconds=seconds, comfy_url=comfy_url)
    # H3 renders under its own pixel cap; the continuation has to match the frame
    # of the clip it is joined to.
    ensure_video_resolution(out, vid_width, vid_height)
    return out


def unify_mixed_engine(video_engine: dict, cfg: dict, *, has_acted: bool,
                       has_classic: bool) -> dict:
    """The video engine a MIXED film's narrated scenes render on.

    H3 acted takes cut against LTX narrated clips read as two different
    productions — colour, grain and motion all shift shot to shot. When a film
    mixes the two kinds, the narrated scenes render on H3 I2V so the whole
    film is one look. A style already on a MiniMax engine keeps its own pick
    (e.g. turbo); an unmixed film is untouched.
    """
    if has_acted and has_classic and video_engine.get("family") != "minimax":
        unified = _engines.resolve_video(cfg, "minimax-h3")
        logger.info("Mixed film: narrated scenes render on %s to match the acted takes",
                    unified.get("label"))
        return unified
    return video_engine


def render_acted_scene(scene, work_dir: Path, cfg: dict, *, store, durable_job_id: str,
                       worker_pool: WorkerPool, vid_width: int, vid_height: int) -> Path:
    """Render one acted (dialogue) scene, retrying on a fresh worker.

    A worker that cannot run the engine at all — model not downloaded there,
    ComfyUI too old for w4a8 — fails instantly and would fail again, so it is
    dropped from the pool rather than burning the scene's retries.
    """
    final = work_dir / f"scene_{scene.id:02d}_final.mp4"
    if final.exists() and final.stat().st_size > 10_000:
        logger.info("Scene %d: already rendered — skipping", scene.id)
        return final

    t_id = task_id(durable_job_id, "scene", scene.id, "performance")
    lease_secs = int(_engines.resolve_reference(
        cfg, cfg.get("reference_engine")).get("lease_seconds") or 14400)
    last_err: Exception | None = None
    for attempt in range(1, _MAX_SCENE_ATTEMPTS + 1):
        if not worker_pool.has_healthy():
            raise RuntimeError(f"All workers failed — last error: {last_err}")
        url = worker_pool.acquire()
        try:
            store.register_worker(worker_id("comfy", url), "comfy", url)
            with TaskRun(store, t_id, worker_id_value=worker_id("comfy", url),
                         lease_seconds=lease_secs, retryable=True,
                         start_message=f"acted scene {scene.id}") as run:
                out = render_performance_scene(
                    scene, work_dir, cfg, comfy_url=url,
                    vid_width=vid_width, vid_height=vid_height,
                    style_name=cfg.get("style_name") or "")
                store.record_artifact(durable_job_id, t_id, "scene_final", out,
                                      duration_seconds=_get_duration(out))
                run.complete({"path": str(out)}, "acted scene rendered")
            return out
        except Exception as e:
            last_err = e
            worker_fault = (isinstance(e, StuckJobError)
                            or any(k in str(e) for k in _WORKER_ERR_KEYWORDS)
                            or "not in list" in str(e))
            if worker_fault:
                logger.warning("Worker %s cannot render scene %d (%d/%d): %s — removing",
                               url, scene.id, attempt, _MAX_SCENE_ATTEMPTS, str(e)[:200])
                worker_pool.mark_failed(url)
            else:
                logger.warning("Scene %d failed on %s (%d/%d): %s — retrying",
                               scene.id, url, attempt, _MAX_SCENE_ATTEMPTS, str(e)[:200])
                if attempt < _MAX_SCENE_ATTEMPTS:
                    time.sleep(5)
        finally:
            worker_pool.release(url)
    raise RuntimeError(f"Scene {scene.id} failed after {_MAX_SCENE_ATTEMPTS} attempts: {last_err}")


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
    from pipeline.comfyui import (context_latent_name, generate_video_h3_ref,
                                  generate_video_h3_ref_chained)
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
            f"Scene {scene.id}: no reference image resolved for cast "
            f"{meta.get('cast')} — Ref2VA needs at least one (generate the "
            "character look images first; a silent scene can open on its own "
            "first frame instead, but it needs an image prompt to make one)")

    engine = _engines.resolve_reference(cfg, cfg.get("reference_engine"))
    # Chained acted scenes (h3_chain_scenes): a scene longer than one clip is
    # shot as two Ref2VA clips joined by H3 Motion Context instead of being
    # split. Reference engines are always MiniMax, so the toggle alone decides
    # — but a scene that already fits one clip renders single-clip either way
    # rather than paying the join's ~22% overhead for nothing.
    chained = chain_scenes_flag(cfg, style_name)
    if chained and _performance.content_seconds(
            meta, chained=True) > _performance.acted_limits(False)[1]:
        # Dialogue divides at speaker turns; a SILENT scene has no lines to
        # divide, so its clip window (and the beats inside it) is what splits.
        sub_metas = (_performance.split_silent_for_chain(meta)
                     if not _performance.norm_lines(meta.get("lines"))
                     else _performance.split_lines_for_chain(meta))
        chained = len(sub_metas) > 1
    else:
        chained, sub_metas = False, [meta]
    prompts = [
        _performance.build_h3_prompt(
            m, style_note=cfg.get("style", ""),
            picture_names=picture_names, audio_names=audio_names)
        for m in sub_metas
    ]
    logger.info("Scene %d: performance render (%s%s) — %d portraits, %d voices → %s",
                scene.id, engine["key"],
                f", chained x{len(sub_metas)}" if chained else "",
                len(ref_images), len(ref_audios), clip.name)
    # Where this take can be picked up again (the film editor's Continue). Each
    # attempt saves under its own prefix — a gate retake is a different take, and
    # continuing the discarded one would splice in a moment nobody watched.
    ctx_prefix = _scene_context.token_prefix(work_dir, scene.id)
    attempts = {"n": 0}
    last_ctx: dict = {}
    base_seconds = sum(_performance.render_seconds(m) for m in sub_metas)

    def _generate(out: Path, stretch: float = 1.0) -> Path:
        """Render the clip. *stretch* multiplies its length — the gate uses it
        to buy a truncated take the time its own delivery turned out to need,
        spread across a chained scene's clips in the proportion they were
        sized in. Each clip still stops at the model's own per-clip ceiling."""
        attempts["n"] += 1
        token = f"{ctx_prefix}_t{attempts['n']}"
        durations = [min(_performance.H3_CEILING_SECONDS,
                         _performance.render_seconds(m) * stretch)
                     for m in sub_metas]
        if chained:
            generate_video_h3_ref_chained(
                engine, prompts, ref_images, out,
                ref_audios=ref_audios,
                width=vid_width, height=vid_height,
                durations=durations,
                context_token=token,
                comfy_url=comfy_url,
            )
        else:
            generate_video_h3_ref(
                engine, prompts[0], ref_images, out,
                ref_audios=ref_audios,
                width=vid_width, height=vid_height,
                duration_seconds=durations[0],
                context_token=token,
                comfy_url=comfy_url,
            )
        index = len(prompts) if chained else 1
        last_ctx.update(token=token, latent=context_latent_name(token, index),
                        clip_index=index)
        return out

    _generate(clip)
    kept_ctx = dict(last_ctx)

    # The gate: a shot that doesn't say its line is a miss, and misses get
    # retaken instead of shipped — on a fresh seed for wrong words, on a longer
    # clip for a line that ran out of time. That is what turns a stochastic
    # model into a consistent one. Soft dependency: without
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
                kept_ctx = dict(last_ctx)
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

        def _miss(score: float, said: str) -> bool:
            """A take fails the gate for saying the WRONG words or for running
            out of clip mid-line. Truncation needs its own test: similarity
            rewards a matching head, so a take that nails two thirds of the
            line and stops dead still scores above the threshold and used to
            ship exactly as it was."""
            return (score < shot_gate.DEFAULT_THRESHOLD
                    or shot_gate.truncated(said, expected))

        attempt = 0
        stretch = 1.0
        while _miss(best, transcript) and attempt < retakes:
            attempt += 1
            # A take that ran out of clip will say the same words again on a
            # fresh seed — the clip is the problem, not the roll. Buy it the
            # length its own pace turned out to need. H3's delivery rate varies
            # more than 2:1 across lines, so the scene's word-count estimate
            # cannot see this coming; the failed take can.
            if shot_gate.truncated(transcript, expected):
                needed = shot_gate.seconds_for_full_line(
                    transcript, expected, _get_duration(clip))
                if needed > 0 and base_seconds > 0:
                    stretch = max(stretch, needed / base_seconds)
                    logger.warning("[gate] scene %d %s: truncated — retaking at "
                                   "%.1fs (%.0f%% longer)", scene.id, clip.name,
                                   base_seconds * stretch, (stretch - 1) * 100)
            logger.warning("[gate] scene %d %s: said %r (score %.2f) — retake %d/%d",
                           scene.id, clip.name, transcript[:80], best, attempt, retakes)
            candidate = clip.with_suffix(".retake.mp4")
            _generate(candidate, stretch)
            cand_score, cand_tr = shot_gate.verify(candidate, expected)
            if cand_score > best:
                candidate.replace(clip)
                kept_ctx = dict(last_ctx)
                best, transcript = cand_score, cand_tr
            else:
                candidate.unlink(missing_ok=True)
        logger.info("[gate] scene %d %s: score %.2f%s", scene.id, clip.name, best,
                    " (best of retakes)" if _miss(best, transcript) else "")

    # The kept take's continuation point, on the worker that shot it. Written
    # after the gate so it belongs to the clip that survived, not to a reject.
    _scene_context.save(
        work_dir, scene.id, latent=kept_ctx.get("latent", ""),
        token=kept_ctx.get("token", ""), next_index=int(kept_ctx.get("clip_index", 1)) + 1,
        comfy_url=comfy_url, engine=engine.get("key", ""),
        width=vid_width, height=vid_height)

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
    # Chained scenes (h3_chain_scenes): render each scene as two H3 clips joined
    # by Motion Context so it can run past the model's ceiling. Same fallback
    # as the engines above — newer job dirs stamp the resolved key, older ones
    # need the styles lookup (the flat key only mirrors the DEFAULT style, so
    # reading cfg flat alone silently ignores any per-style toggle).
    chain_flag = cfg.get("h3_chain_scenes")
    if chain_flag is None:
        for s in cfg.get("styles") or []:
            if isinstance(s, dict) and s.get("name") == (cfg.get("style_name") or ""):
                chain_flag = s.get("h3_chain_scenes")
                break
    # Narrated scenes: MiniMax only — LTX continues natively. Must agree with
    # app.scene_plan_for_settings, which planned FEWER, LONGER scenes off the
    # same flag.
    chain_scenes = bool(chain_flag) and video_engine.get("family") == "minimax"
    # Silent scenes acted on H3 Ref2VA (h3_silent_scenes) instead of animated
    # from a first frame. Same three-step fallback as the flags above; resolved
    # ONCE here and stamped flat into plan_cfg, so the task planner and the
    # render below route every scene the same way.
    silent_flag = cfg.get("h3_silent_scenes")
    if silent_flag is None:
        for s in cfg.get("styles") or []:
            if isinstance(s, dict) and s.get("name") == (cfg.get("style_name") or ""):
                silent_flag = s.get("h3_silent_scenes")
                break
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
        "h3_silent_scenes": bool(silent_flag),
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

    # ── Dialogue scenes (acted, via H3 Ref2VA) ───────────────────────────────
    # Rendered up front to scene_NN_final.mp4; the narration/video/mux phases
    # then skip them (they operate on classic_scenes). A narration-only script
    # has dialogue_scenes == [] and classic_scenes == scenes, so everything
    # below runs exactly as before — which is what makes MIXED films work: each
    # scene takes the path its mode asks for. Silent scenes join them when the
    # style acts those too (h3_silent_scenes) — the same predicate the task
    # planner used, off the same stamped flag.
    dialogue_scenes = [s for s in scenes if _performance.renders_acted(s, plan_cfg)]
    acted_ids = {s.id for s in dialogue_scenes}
    classic_scenes = [s for s in scenes if s.id not in acted_ids]
    dialogue_durs: dict[int, float] = {}
    # One production, one look: a mixed film's narrated scenes join the acted
    # takes on H3 rather than cutting between two different video models.
    video_engine = unify_mixed_engine(video_engine, cfg,
                                      has_acted=bool(dialogue_scenes),
                                      has_classic=bool(classic_scenes))

    # Progress bands. Acted scenes dominate a dialogue film's wall-clock, so the
    # dialogue phase gets a share of the bar proportional to its weight;
    # narration-only jobs keep the exact historical bands (invariant).
    if dialogue_scenes:
        units_dlg, units_classic = 8 * len(dialogue_scenes), 3 * len(classic_scenes)
        dlg_end = min(85.0, max(12.0, 2 + 88.0 * units_dlg / max(1, units_dlg + units_classic)))
        tts_band = (dlg_end, dlg_end + 0.18 * (92.0 - dlg_end))
        video_band = (tts_band[1], 92.0)
    else:
        dlg_end = 0.0
        tts_band = (0.0, 20.0)
        video_band = (35.0, 90.0)
    n_classic = max(1, len(classic_scenes))

    if dialogue_scenes:
        done_dlg = [0]
        _dlg_lock = threading.Lock()

        def _render_dialogue(scene: Scene) -> tuple[int, float]:
            with _dlg_lock:
                pct = 2 + (dlg_end - 2) * (done_dlg[0] / max(1, len(dialogue_scenes)))
                write_progress(status_file, pct,
                               f"Acted scene {scene.id} of {len(scenes)} "
                               f"({done_dlg[0]} done)")
            out = render_acted_scene(
                scene, work_dir, plan_cfg, store=store, durable_job_id=durable_job_id,
                worker_pool=worker_pool, vid_width=vid_width, vid_height=vid_height)
            with _dlg_lock:
                done_dlg[0] += 1
            return scene.id, _get_duration(out)

        n_parallel = max(1, min(len(worker_pool.urls), len(dialogue_scenes)))
        write_progress(status_file, 1,
                       f"Rendering {len(dialogue_scenes)} acted scene(s) across "
                       f"{n_parallel} worker(s)…")
        dlg_pool = concurrent.futures.ThreadPoolExecutor(max_workers=n_parallel)
        try:
            futs = {dlg_pool.submit(_render_dialogue, s): s for s in dialogue_scenes}
            for fut in concurrent.futures.as_completed(futs):
                sid, dur = fut.result()
                dialogue_durs[sid] = dur
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
    # Optional per style/film. Music is a FINAL-MIX ingredient only — it is
    # never baked into a scene — so switching it off simply skips generation
    # and the mix passes the concatenated audio straight through.
    music_dur  = max(total_dur * 1.05, 30.0)
    music_path = work_dir / "background_music.wav"
    title = title or (scenes[0].title.split(":")[0] if scenes else "Australia")
    music_on = bool(cfg.get("music_enabled", True))

    if not music_on:
        logger.info("Music is off for this film — skipping generation")
        write_progress(status_file, tts_band[1], "Music off — generating video…")
        music_task = task_id(durable_job_id, "music")
        try:
            store.complete_task(music_task, result={"skipped": "music disabled"},
                                message="music off")
        except Exception:
            logger.debug("No music task to complete (planned without one)")
    elif music_path.exists() and music_path.stat().st_size > 10_000:
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
        # Same three-step fallback as the image/video engines above: the stamped
        # job_config key, then the style row, then the flat default mirror.
        music_key = cfg.get("music_engine")
        if not music_key:
            for s in cfg.get("styles") or []:
                if isinstance(s, dict) and s.get("name") == (cfg.get("style_name") or ""):
                    music_key = s.get("music_engine")
                    break
        music_engine = _engines.resolve_music(music_key or cfg.get("default_music_engine"))
        music_task = task_id(durable_job_id, "music")
        store.update_task_payload(
            music_task,
            {
                "duration_seconds": music_dur,
                "output_path": str(music_path),
                "music_desc": cfg.get("music_desc") or "",
                "music_engine": music_engine["key"],
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
                    lease_seconds=music_engine["timeout"] + 300,
                    start_message=f"music on {music_url}",
                ) as run:
                    generate_music(title, music_dur, music_path, cfg.get("music_desc") or None,
                                   comfy_url=music_url, music_engine=music_engine["key"])
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
                        chained=chain_scenes,
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
        # Acted scenes — dialogue, and silent ones when the style shoots those
        # on H3 too — were rendered in the pre-pass and their artifacts are
        # tracked against the performance task. They have NO mux task, so
        # recording a scene_final artifact against one fails the FK and takes
        # the whole assembly down. Same predicate the planner used, so the two
        # can't disagree. Just collect the finished clip.
        if s.id in acted_ids:
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

        if music_on and music_path.exists():
            write_progress(status_file, 95,
                           f"Mixing audio (voice {voice_vol*100:.0f}%, music {music_vol*100:.0f}%)…")
            mix_background_music(
                combined, music_path, final_path,
                volume=music_vol, voice_volume=voice_vol,
                ambient_path=ambient_path, ambient_volume=ambient_vol,
            )
        else:
            # No score: the clips already carry their own audio, so the final
            # IS the concatenation. (Acted scenes have voices in-picture.)
            write_progress(status_file, 95, "No music — finishing the cut…")
            shutil.copy2(combined, final_path)
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
