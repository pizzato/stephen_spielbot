"""Performance-film script generation (per-style script_mode = "performance").

A performance film is a different kind of video from the narrated ones: the
characters act and SPEAK, and MiniMax H3 Ref2VA writes picture and voice in a
single pass from character portraits. There is no first frame, no TTS step and
no background music — so the script this module produces is a different shape
from the narrated one:

    scene = cast + timed beats + quoted dialogue + soundscape + refusals

``build_h3_prompt`` turns that structure into the six-block prompt H3 responds
to (reference roles → style → timed beats → camera → audio → refusals). The
assembly is deterministic rather than LLM prose, so the reference-role line and
the "no subtitles" refusals are present on every single scene — H3 burns
subtitles in without them.

The narrated generators (pipeline/llm.py, pipeline/story.py) are untouched;
nothing here runs unless a style opts in.
"""
from __future__ import annotations

import json
import logging
import re

from pipeline import prompts as _prompts
from pipeline.llm import Scene, _chat_complete, _load_cfg, _parse_claude_response

logger = logging.getLogger("video_gen")

# H3 caps a clip at 15 s, and cost grows superlinearly with frames: 10 s renders
# in ~10 min (turbo) while 15 s clips are disproportionately slower and pad with
# a frozen tail. Scenes are written to this budget.
SCENE_SECONDS = 10.0
MIN_SCENE_SECONDS = 5.0
MAX_SCENE_SECONDS = 14.0

# One audio reference per speaker, and H3 accepts at most 3 (and 9 images).
MAX_SPEAKERS_PER_SCENE = 3

_REFUSALS = ("Do not add subtitles, do not add captions, do not add any on-screen text, "
             "no watermark, no extra characters, no scene changes, no music.")


def _clamp_seconds(value) -> float:
    try:
        secs = float(value or 0) or SCENE_SECONDS
    except (TypeError, ValueError):
        secs = SCENE_SECONDS
    return max(MIN_SCENE_SECONDS, min(MAX_SCENE_SECONDS, secs))


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def norm_lines(raw) -> list[dict]:
    """Normalize the LLM's dialogue lines to [{speaker, delivery, text}]."""
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        text = _clean(item.get("text"))
        speaker = _clean(item.get("speaker"))
        if not text or not speaker:
            continue
        out.append({"speaker": speaker, "delivery": _clean(item.get("delivery")) or "even, natural",
                    "text": text.strip('"“”')})
    return out


def norm_beats(raw, seconds: float) -> list[dict]:
    """Normalize timed beats to [{t0, t1, action}], clipped to the clip length."""
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        action = _clean(item.get("action"))
        if not action:
            continue
        try:
            t0 = max(0.0, float(item.get("t0") or 0))
            t1 = min(seconds, float(item.get("t1") or seconds))
        except (TypeError, ValueError):
            t0, t1 = 0.0, seconds
        if t1 <= t0:
            t1 = min(seconds, t0 + 2.0)
        out.append({"t0": round(t0, 1), "t1": round(t1, 1), "action": action})
    return out


def speakers_in(lines: list[dict]) -> list[str]:
    """Distinct speaker names, in first-spoken order (the <Audio N> order)."""
    seen: list[str] = []
    for line in lines:
        name = line.get("speaker", "")
        if name and name not in seen:
            seen.append(name)
    return seen


def build_h3_prompt(scene_meta: dict, *, style_note: str = "",
                    picture_names: list[str] | None = None,
                    audio_names: list[str] | None = None) -> str:
    """Assemble the six-block H3 prompt for one performance scene.

    *picture_names* are the characters whose portraits are wired to
    ``<Picture 1..N>`` and *audio_names* the speakers wired to ``<Audio 1..N>``,
    both in reference order — the render passes the same order to ComfyUI, so
    the tags in the prompt and the slots in the graph always agree.
    """
    seconds = _clamp_seconds(scene_meta.get("seconds"))
    lines = norm_lines(scene_meta.get("lines"))
    beats = norm_beats(scene_meta.get("beats"), seconds)
    blocks: list[str] = []

    # 1 — reference roles. Every reference gets an explicit job or H3 blends them.
    roles = [f"<Picture {i + 1}> is {name}" for i, name in enumerate(picture_names or [])]
    roles += [f"<Audio {i + 1}> is {name}'s voice — {name} must speak in exactly that voice"
              for i, name in enumerate(audio_names or [])]
    if roles:
        blocks.append(". ".join(roles) +
                      ". Keep every face, wardrobe and body exactly as in the references.")

    # 2 — style contract.
    setting = _clean(scene_meta.get("setting"))
    look = " ".join(x for x in (setting, _clean(style_note)) if x)
    if look:
        blocks.append(f"Style: {look}")

    # 3 — timed beats, with the quoted dialogue in place.
    for beat in beats:
        blocks.append(f"[{beat['t0']:g}s-{beat['t1']:g}s] {beat['action']}")
    for line in lines:
        # Delivery in front, line verbatim in quotes, then the lips-close
        # instruction — without it the mouth keeps moving after the line ends.
        blocks.append(
            f"{line['speaker']} says exactly, {line['delivery']}: \"{line['text']}\" "
            f"{line['speaker']}'s lips close and all mouth movement stops the instant "
            f"the line ends.")

    # 4 — camera.
    blocks.append(f"Camera: {_clean(scene_meta.get('camera')) or 'locked off at chest height, slight handheld drift, no push, no zoom'}.")

    # 5 — audio as its own track (never music: performance films carry no score).
    soundscape = _clean(scene_meta.get("soundscape")) or "quiet room tone throughout"
    blocks.append(f"Audio: {soundscape}, no music of any kind.")

    # 6 — refusals. Plain sentences: H3 is CFG-free, there is no negative field.
    extra = _clean(scene_meta.get("refusals"))
    blocks.append(f"{_REFUSALS} {extra}".strip())

    return "\n".join(blocks)


def spoken_text(scene_meta: dict) -> str:
    """Everything said in the scene, for captions and the description."""
    return " ".join(line["text"] for line in norm_lines(scene_meta.get("lines")))


def _call_fn(cfg: dict):
    def call(system, user_msg, max_tokens, label, retries=3):
        return _chat_complete(cfg, system, user_msg, max_tokens, label, retries=retries)
    return call


def generate_performance_script(
    title: str,
    n_scenes: int,
    *,
    style_hint: str | None = None,
    video_title: str | None = None,
    character_sheet: str | None = None,
    avoid_hint: str | None = None,
    language: str | None = None,
    cfg: dict | None = None,
) -> tuple[list[Scene], str, list[dict]]:
    """Write a performance film: N scenes of acted, spoken drama.

    Returns ``(scenes, style, characters)`` — the same shape the narrated
    generators return minus the music description, since performance films have
    no score. Every scene comes back with ``mode = "performance"`` and its
    structured performance fields in ``metadata_extra``.
    """
    cfg = cfg or _load_cfg()
    call = _call_fn(cfg)
    n_scenes = max(1, int(n_scenes or 1))

    notes = []
    if style_hint and style_hint.strip():
        notes.append(f'Use exactly this text for the "style" field: "{style_hint.strip()}"')
    if character_sheet:
        notes.append(f"Existing cast you should reuse where they fit:\n{character_sheet}")
    if avoid_hint:
        notes.append(f"Avoid: {avoid_hint}")
    if language:
        notes.append(f"Write every spoken line in {language}. "
                     "Keep setting/camera/soundscape descriptions in English.")

    raw = call(
        _prompts.system("performance_script"),
        _prompts.user("performance_script",
                      title=(video_title or title),
                      topic=title,
                      n_scenes=n_scenes,
                      seconds=int(SCENE_SECONDS),
                      max_speakers=MAX_SPEAKERS_PER_SCENE,
                      notes=("\n\n".join(notes) if notes else "")),
        max_tokens=1200 + 700 * n_scenes,
        label="performance script",
    )
    data = _parse_claude_response(raw, "performance script")
    if not isinstance(data, dict) or not data.get("scenes"):
        raise RuntimeError("Performance script generation returned no scenes")

    style = _clean(data.get("style")) or (style_hint or "")
    characters = _norm_characters(data.get("characters"))
    scenes: list[Scene] = []
    for i, raw_scene in enumerate(data["scenes"][:n_scenes], start=1):
        scenes.append(_to_scene(i, raw_scene, style_note=style))
    logger.info("Performance script: %d scenes, %d characters", len(scenes), len(characters))
    return scenes, style, characters


def _norm_characters(raw) -> list[dict]:
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        if not name:
            continue
        out.append({
            "name": name,
            "description": _clean(item.get("description")),
            "gender": _clean(item.get("gender")).lower(),
            "age": _clean(item.get("age")).lower(),
        })
    return out


def _to_scene(scene_id: int, raw: dict, *, style_note: str = "") -> Scene:
    """One LLM scene object → a performance Scene.

    The assembled H3 prompt lives in ``video_prompt`` so the script editor shows
    and edits the exact text the model receives; the structured fields stay in
    metadata so a re-assembly (or a re-cast voice) can rebuild it.
    """
    seconds = _clamp_seconds(raw.get("seconds"))
    lines = norm_lines(raw.get("lines"))
    beats = norm_beats(raw.get("beats"), seconds)
    cast = [c for c in (speakers_in(lines) + [_clean(x) for x in (raw.get("cast") or [])])
            if c]
    # De-dup, first mention wins (this is the <Picture N> order).
    ordered_cast: list[str] = []
    for name in cast:
        if name not in ordered_cast:
            ordered_cast.append(name)

    # "lines" rides Scene.lines (the dataclass field the metadata property
    # re-emits), so it is deliberately absent from metadata_extra — putting it
    # in both would let an edit to one silently lose to the other.
    meta = {
        "mode": "performance",
        "cast": ordered_cast,
        "lines": lines,
        "beats": beats,
        "seconds": seconds,
        "setting": _clean(raw.get("setting")),
        "camera": _clean(raw.get("camera")),
        "soundscape": _clean(raw.get("soundscape")),
        "refusals": _clean(raw.get("refusals")),
    }
    # Reference wiring is resolved at render time (portraits/voices that actually
    # exist), so the stored prompt names the cast in scene order as a preview;
    # the renderer rebuilds it with the real slots before queueing.
    prompt = build_h3_prompt(meta, style_note=style_note, picture_names=ordered_cast,
                             audio_names=speakers_in(lines)[:MAX_SPEAKERS_PER_SCENE])
    return Scene(
        id=scene_id,
        title=_clean(raw.get("title")) or f"Scene {scene_id}",
        # No image engine runs for a performance scene.
        image_prompt="",
        video_prompt=prompt,
        narration=spoken_text(meta),
        mode="performance",
        lines=lines,
        duration=seconds,
        metadata_extra={k: v for k, v in meta.items() if k != "lines"},
    )


def scene_meta(scene) -> dict:
    """The performance metadata of a scene, from either a Scene or a stored row."""
    if isinstance(scene, dict):
        return dict(scene.get("metadata") or {})
    return dict(getattr(scene, "metadata", {}) or {})


def is_performance(scene) -> bool:
    return scene_meta(scene).get("mode") == "performance" or \
        getattr(scene, "mode", "") == "performance"


def parse_scene_rows(rows) -> list[dict]:
    """Stored scene rows → performance metadata dicts (json-decoded)."""
    out = []
    for row in rows:
        meta = row.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        out.append(meta or {})
    return out
