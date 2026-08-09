"""Acted scenes: the shape they take, and the H3 prompt they become.

In an acted scene the characters SPEAK, and MiniMax H3 Ref2VA writes picture and
voice in a single pass from their portraits. There is no first frame and no TTS
step, so an acted scene is a different shape from a narrated one:

    scene = cast + timed beats + quoted dialogue + soundscape + refusals

``build_h3_prompt`` turns that structure into the six-block prompt H3 responds
to (reference roles → style → timed beats → camera → audio → refusals). The
assembly is deterministic rather than LLM prose, so the reference-role line and
the "no subtitles" refusals are present on every single scene — H3 burns
subtitles in without them.

The scenes themselves are written by pipeline/story.py, which stages an approved
story as acted scenes when the film's format asks for them (``scene_from_raw``
is where one of its scene objects becomes a Scene). A narrated scene in the same
film is untouched by any of this.
"""
from __future__ import annotations

import json
import logging
import re

from pipeline.llm import Scene

logger = logging.getLogger("video_gen")

# H3 caps a clip at 15 s, and cost grows superlinearly with frames: 10 s renders
# in ~10 min (turbo) while 15 s clips are disproportionately slower and pad with
# a frozen tail. Scenes are written to this budget.
SCENE_SECONDS = 10.0
MIN_SCENE_SECONDS = 5.0
# 12, not the model's 15: scenes at the ceiling get truncated mid-line and
# freeze-pad. The margin is the safety.
MAX_SCENE_SECONDS = 12.0
H3_CEILING_SECONDS = 15.0

# One audio reference per speaker, and H3 accepts at most 3 (and 9 images).
MAX_SPEAKERS_PER_SCENE = 3

# Shot sizing: ~2.5 spoken words per second, plus a beat of air. Oversized
# shots made the model pad the tail with speech nobody scripted.
WORDS_PER_SECOND = 2.5
SHOT_AIR_SECONDS = 1.5

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


def _unterminated(text: str) -> str:
    """Drop a trailing full stop so the assembled block reads as one sentence.

    The LLM ends most fields with a period; the camera/audio blocks append their
    own punctuation, which produced "no push, no zoom.." and "...at second 10.,
    no music of any kind."."""
    return _clean(text).rstrip(".").strip()


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
            t0 = float(item.get("t0") or 0)
            t1 = float(item.get("t1") or seconds)
        except (TypeError, ValueError):
            t0, t1 = 0.0, seconds
        # The LLM does overrun and occasionally inverts a pair. Clamp the START
        # into the clip first — clamping only the end can leave t1 < t0, which
        # reaches the model as a nonsense window like "[18s-14s]".
        t0 = min(max(0.0, t0), max(0.0, seconds - 1.0))
        t1 = min(max(t1, t0 + 1.0), seconds)
        out.append({"t0": round(t0, 1), "t1": round(t1, 1), "action": action})
    return out


def content_seconds(meta: dict) -> float:
    """How long this scene's dialogue actually needs on screen.

    ~2.5 spoken words/second, a beat per speaker turn, and a moment of air.
    The LLM's own "seconds" guess is ignored when there are lines — scenes it
    overloaded got truncated mid-sentence at the model's 15 s ceiling.
    """
    lines = norm_lines(meta.get("lines"))
    if not lines:
        return _clamp_seconds(meta.get("seconds"))
    words = sum(len(l["text"].split()) for l in lines)
    turns = 1 + sum(1 for a, b in zip(lines, lines[1:]) if a["speaker"] != b["speaker"])
    return words / WORDS_PER_SECOND + 1.0 * turns + 1.0


def render_seconds(meta: dict) -> float:
    """The clip length a shot is actually rendered at: never below what its
    words need, never above the model's hard ceiling. A scene that needs more
    than the ceiling will still truncate — the script-side split exists so
    that never happens; this is the last line of defence."""
    if norm_lines(meta.get("lines")):
        # Content wins outright when there are lines: a stored guess used as a
        # floor pads the clip, and padding is where the model invents speech.
        return min(H3_CEILING_SECONDS, max(MIN_SCENE_SECONDS, content_seconds(meta)))
    return min(H3_CEILING_SECONDS, _clamp_seconds(meta.get("seconds")))


def split_overloaded(raw_scene: dict) -> list[dict]:
    """Split a scene whose dialogue cannot fit one clip into consecutive
    scenes: same setting, cast and sound, lines divided at speaker turns.
    More short scenes beat one long truncated one."""
    lines = norm_lines(raw_scene.get("lines"))
    if not lines or content_seconds({"lines": lines}) <= MAX_SCENE_SECONDS:
        return [raw_scene]
    parts: list[list[dict]] = [[]]
    for line in lines:
        candidate = parts[-1] + [line]
        if parts[-1] and content_seconds({"lines": candidate}) > MAX_SCENE_SECONDS:
            parts.append([line])
        else:
            parts[-1] = candidate
    out = []
    for i, chunk in enumerate(parts):
        piece = dict(raw_scene)
        piece["lines"] = chunk
        piece["seconds"] = content_seconds({"lines": chunk})
        if i > 0:
            piece["title"] = f"{_clean(raw_scene.get('title')) or 'Scene'} (cont.)"
            piece["beats"] = []          # the action beat belongs to the opening
        out.append(piece)
    logger.info("Scene %r needs %.1fs of dialogue — split into %d scenes",
                raw_scene.get("title"), content_seconds({"lines": lines}), len(out))
    return out


def speakers_in(lines: list[dict]) -> list[str]:
    """Distinct speaker names, in first-spoken order (the <Audio N> order)."""
    seen: list[str] = []
    for line in lines:
        name = line.get("speaker", "")
        if name and name not in seen:
            seen.append(name)
    return seen


def _picture_label(pic) -> str:
    return pic if isinstance(pic, str) else str(pic.get("name") or "")


def shots_for(meta: dict, establishing: bool = False) -> list[dict]:
    """Split a scene into single-speaker shots.

    H3 does not reliably bind a reference to a NAME: give it two faces and two
    voices and it swaps them — one character speaking in the other's voice, or
    sitting in their seat. Names, appearance hints and explicit refusals all
    failed to hold it. What does hold is removing the ambiguity: a shot with
    ONE face and ONE voice has nothing to swap, and the scene becomes the
    shot/reverse-shot a two-hander is really made of.

    Consecutive lines by the same speaker stay in one shot. A scene with a
    single speaker (or none) returns a single shot — unchanged behaviour.
    """
    lines = norm_lines(meta.get("lines"))
    if len({line["speaker"] for line in lines}) < 2:
        return [dict(meta)]

    runs: list[dict] = []
    for line in lines:
        if runs and runs[-1]["speaker"] == line["speaker"]:
            runs[-1]["lines"].append(line)
        else:
            runs.append({"speaker": line["speaker"], "lines": [line]})

    shots = []
    if establishing:
        # A silent wide of everyone together, so the film doesn't feel like two
        # people who never met. Swap-proof by construction: a swap needs two
        # VOICES, and nobody speaks here.
        shots.append({
            **meta,
            "seconds": MIN_SCENE_SECONDS,
            "lines": [],
            "beats": norm_beats(meta.get("beats"), MIN_SCENE_SECONDS)[:1],
            "cast": speakers_in(lines),
            "establishing": True,
        })
    for run in runs:
        words = sum(len(l["text"].split()) for l in run["lines"])
        # Size the shot to its WORDS (~2.5 spoken words/second plus a beat of
        # air), not to a share of the scene. Oversized shots left the model
        # padding the tail — where it invents speech that was never scripted.
        secs = round(words / WORDS_PER_SECOND + SHOT_AIR_SECONDS, 1)
        shots.append({
            **meta,
            "seconds": _clamp_seconds(secs),
            "lines": run["lines"],
            "speaker": run["speaker"],
            # Only the speaker is on camera: the beats describe the whole scene,
            # so they are replaced by what this one person is doing.
            "beats": [],
            "cast": [run["speaker"]],
            "solo": True,
        })
    return shots


def picture_role(pic, has_wardrobe: frozenset = frozenset()) -> str:
    """One reference's bounded-authority line: what it controls, what to ignore.

    References compete — a portrait's own clothing and background fight the
    wardrobe and location references unless each asset's authority is bounded
    (measured: the weakest references drop first). So every line says what the
    reference defines AND what it must not control.
    """
    if isinstance(pic, str):
        return f"defines {pic}'s appearance"
    name, kind = pic.get("name", ""), pic.get("kind", "character")
    hint = _clean(pic.get("hint"))
    if kind == "location":
        return (f"defines the place only — keep the space, layout, furnishings "
                f"and lighting exactly as shown. It contains no people and adds "
                f"none")
    if kind == "continuity":
        return (f"defines the SAME room, furniture, lighting and time of day "
                f"already filmed in this scene — this shot is another angle of "
                f"that exact space; do not redecorate or move to a new place")
    if kind == "wardrobe":
        owner = (pic.get("character") or "").strip()
        who = f"the clothes {owner} wears" if owner else "the wardrobe"
        return (f"defines {who} only — those exact garments, colours and "
                f"details. Ignore its background")
    tail = f" — {name} is {hint}" if hint else ""
    if name.strip().lower() in has_wardrobe:
        # Their clothes come from a wardrobe reference; the portrait must not
        # compete for them.
        return (f"defines {name}'s face, hair and build only{tail}. Ignore "
                f"this picture's clothing and background")
    return f"defines {name}'s appearance{tail}. Ignore this picture's background"


def _sides(scene_cast: list, speaker: str) -> tuple[str, str]:
    """(speaker's frame position, listener's off-frame side), kept stable by
    cast order across a scene so reverse-shot eyelines actually meet."""
    try:
        idx = [str(n) for n in (scene_cast or [])].index(str(speaker))
    except ValueError:
        idx = 0
    return ("left", "right") if idx == 0 else ("right", "left")


def build_h3_prompt(scene_meta: dict, *, style_note: str = "",
                    picture_names: list | None = None,
                    audio_names: list[str] | None = None) -> str:
    """Assemble the H3 prompt for one performance shot.

    Structured after the six-section architecture the strongest prompting
    guides converge on: reference authority first, then identity locks, the
    scene, dialogue, screen geography, the shot list, camera, sound, and a
    preservation contract — with the tag syntax (<Picture N>/<Audio N>) and
    the phrasings we have verified on this model kept intact.

    A hand-edited prompt (``prompt_override``) wins outright: the check lives
    here, in the one function both the editor and the renderer call, so what is
    shown on screen is exactly what the model receives.
    """
    override = _clean(scene_meta.get("prompt_override"))
    if override:
        return override
    seconds = _clamp_seconds(scene_meta.get("seconds"))
    lines = norm_lines(scene_meta.get("lines"))
    beats = norm_beats(scene_meta.get("beats"), seconds)
    pics = picture_names or []
    people = [p for p in pics
              if (not isinstance(p, dict)) or p.get("kind", "character") == "character"]
    people_names = [_picture_label(p) for p in people]
    wardrobe_owners = frozenset(
        str(p.get("character") or "").strip().lower()
        for p in pics if isinstance(p, dict) and p.get("kind") == "wardrobe")
    sections: list[str] = []

    # [REFERENCE USE] — every asset's authority, bounded.
    refs = [f"<Picture {i + 1}> {picture_role(p, wardrobe_owners)}."
            for i, p in enumerate(pics)]
    refs += [f"<Audio {i + 1}> defines {name}'s voice only — {name} must speak "
             f"in exactly this voice." for i, name in enumerate(audio_names or [])]
    if refs:
        sections.append("[REFERENCE USE]\n" + "\n".join(refs))

    # [IDENTITY LOCKS] — who is on screen, how many, and never merged.
    locks = []
    if people_names:
        count = len(people_names)
        locks.append(f"Exactly {'one person' if count == 1 else f'{count} people'} "
                     f"on screen: {' and '.join(people_names)}. No one else appears.")
    if len(people_names) > 1:
        locks.append(f"{' and '.join(people_names)} are different people — never "
                     f"swap or merge their faces, bodies, clothes or voices.")
    locks.append("Keep every face, wardrobe and body exactly as in the references.")
    sections.append("[IDENTITY LOCKS]\n" + "\n".join(locks))

    # [SCENE]
    setting = _clean(scene_meta.get("setting"))
    look = " ".join(x for x in (setting, _clean(style_note)) if x)
    if look:
        sections.append(f"[SCENE]\n{look}")

    # [DIALOGUE] — each line bound to one speaker and one delivery; the
    # lips-close instruction is load-bearing (without it the mouth keeps
    # moving after the line ends).
    if lines:
        spoken = []
        for line in lines:
            spoken.append(
                f"{line['speaker']} says exactly, {line['delivery']}: \"{line['text']}\" "
                f"{line['speaker']}'s lips close and all mouth movement stops the "
                f"instant the line ends.")
        sections.append("[DIALOGUE]\n" + "\n".join(spoken))

    # [SCREEN GEOGRAPHY] — place people in the frame before any action, with
    # eyelines that stay stable across the scene's shots.
    geo = []
    if scene_meta.get("solo"):
        speaker = scene_meta.get("speaker")
        others = [n for n in (scene_meta.get("scene_cast") or []) if n != speaker]
        _, off_side = _sides(scene_meta.get("scene_cast"), speaker)
        line = (f"{speaker} is centered, framed from the chest up, face fully "
                f"visible to the camera for the entire shot, never turning away "
                f"from it")
        if others:
            line += (f", angled slightly toward {' and '.join(others)} just off "
                     f"the {off_side} edge of frame. Do not show "
                     f"{' or '.join(others)}")
        geo.append(line + ".")
    if (lines and len(people_names) > 1
            and not scene_meta.get("solo") and not scene_meta.get("establishing")):
        # Whole conversation in one clip: fix everyone's side up front so the
        # framing is a real two-shot with meeting eyelines, not a guess.
        placed = [f"{n} on the {'left' if i == 0 else 'right'}"
                  for i, n in enumerate(people_names[:2])]
        geo.append(f"{' and '.join(people_names)} are together in the frame, "
                   f"{', '.join(placed)}, facing each other, both faces clearly "
                   f"visible to the camera. They keep these positions for the "
                   f"whole shot.")
    if scene_meta.get("establishing") and len(people_names) > 1:
        placed = [f"{n} on the {'left' if i == 0 else 'right'}"
                  for i, n in enumerate(people_names[:2])]
        geo.append(f"{' and '.join(people_names)} are together in the frame, "
                   f"{', '.join(placed)}, in a wide shot. Nobody speaks: every "
                   f"mouth stays completely closed for the whole shot.")
    if geo:
        sections.append("[SCREEN GEOGRAPHY]\n" + "\n".join(geo))

    # [SHOT LIST]
    if beats:
        sections.append("[SHOT LIST]\n" + "\n".join(
            f"[{b['t0']:g}s-{b['t1']:g}s] {b['action']}" for b in beats))

    # [CAMERA]
    camera = _unterminated(scene_meta.get("camera")) or \
        "locked off at chest height, slight handheld drift, no push, no zoom"
    sections.append(f"[CAMERA]\n{camera}.")

    # [PRODUCTION SOUND] — diegetic only; performance films carry no score.
    soundscape = _unterminated(scene_meta.get("soundscape")) or "quiet room tone throughout"
    sections.append(f"[PRODUCTION SOUND]\nNative stereo ambience: {soundscape}. "
                    f"Clear dialogue, no music of any kind.")

    # [NEGATIVES] — a preservation contract plus named failure modes. H3 is
    # CFG-free: these are plain sentences, not a negative prompt.
    extra = _clean(scene_meta.get("refusals"))
    sections.append(("[NEGATIVES]\n"
                     "Preserve every reference exactly as assigned; do not modify "
                     "anything else. No extra people, no face drift, no wardrobe "
                     "changes, no voice swaps, no broken eyelines, no unmotivated "
                     "cuts. Do not add subtitles, do not add captions, do not add "
                     "any on-screen text, no watermark, no scene changes, no "
                     f"music. {extra}").strip())

    return "\n\n".join(sections)


def spoken_text(scene_meta: dict) -> str:
    """Everything said in the scene, for captions and the description."""
    return " ".join(line["text"] for line in norm_lines(scene_meta.get("lines")))


def scene_from_raw(scene_id: int, raw: dict, *, style_note: str = "") -> Scene:
    """One LLM scene object → an acted Scene.

    The assembled H3 prompt lives in ``video_prompt`` so the script editor shows
    and edits the exact text the model receives; the structured fields stay in
    metadata so a re-assembly (or a re-cast voice) can rebuild it.
    """
    lines = norm_lines(raw.get("lines"))
    # Content decides the length; the LLM's guess routinely under-buys time
    # and the truncation lands mid-sentence.
    seconds = _clamp_seconds(content_seconds({**raw, "lines": lines})
                             if lines else raw.get("seconds"))
    beats = norm_beats(raw.get("beats"), seconds)
    # The scene's OWN cast order leads, then anyone who speaks but wasn't
    # listed. This is the <Picture N> order, and the screen-geography block
    # places Picture 1 left and Picture 2 right — so when a long exchange is
    # split across consecutive scenes, a per-piece order (whoever speaks first)
    # would swap the two of them from side to side across the cut.
    cast = [c for c in ([_clean(x) for x in (raw.get("cast") or [])] + speakers_in(lines))
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
        "mode": "dialogue",
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
        mode="dialogue",
        lines=lines,
        duration=seconds,
        metadata_extra={**{k: v for k, v in meta.items() if k != "lines"},
                        "style_note": _clean(style_note)},
    )


# "dialogue" is the user-facing name for an acted scene; older scripts wrote
# "performance". Both render through H3 Ref2VA — one path, two spellings.
PERFORMANCE_MODES = ("dialogue", "performance")


def is_performance_mode(mode) -> bool:
    return str(mode or "").strip().lower() in PERFORMANCE_MODES


def acted_meta(scene) -> dict:
    """The metadata an acted scene needs to render, filled in where it's absent.

    A performance script authors cast/beats/seconds/setting directly. A dialogue
    scene written in a MIXED film has only lines and the classic prompts — so
    the cast comes from who speaks, the length from what they say, and the
    setting from the scene's own video/image prompt. Without this a mixed film's
    dialogue scene would render castless (no portraits → hard failure) and
    5 seconds long.
    """
    def field(name, default=""):
        return (scene.get(name, default) if isinstance(scene, dict)
                else getattr(scene, name, default))

    meta = dict(scene_meta(scene))
    lines = norm_lines(meta.get("lines") or field("lines", None))
    meta["lines"] = lines
    meta["mode"] = "dialogue" if is_performance_mode(meta.get("mode")) else meta.get("mode", "")

    cast = [c for c in (list(meta.get("cast") or []) + speakers_in(lines)) if _clean(c)]
    ordered: list[str] = []
    for name in cast:
        if name not in ordered:
            ordered.append(name)
    meta["cast"] = ordered

    if not _clean(meta.get("setting")):
        # An acted scene's video_prompt IS the assembled prompt once it has been
        # saved through the editor, and feeding that back in as the setting
        # would nest the whole thing inside itself.
        video = _clean(field("video_prompt"))
        if video.startswith("[") or "[NEGATIVES]" in video:
            # Already an assembled prompt (a previous save) — feeding it back
            # in as the setting would nest the whole thing inside itself.
            video = ""
        meta["setting"] = video or _clean(field("image_prompt"))
    if not meta.get("seconds"):
        meta["seconds"] = _clamp_seconds(
            content_seconds(meta) if lines else field("duration", 0) or SCENE_SECONDS)
    meta["beats"] = norm_beats(meta.get("beats"), float(meta["seconds"]))
    return meta


def scene_meta(scene) -> dict:
    """The performance metadata of a scene, from either a Scene or a stored row."""
    if isinstance(scene, dict):
        return dict(scene.get("metadata") or {})
    return dict(getattr(scene, "metadata", {}) or {})


def is_performance(scene) -> bool:
    return (is_performance_mode(scene_meta(scene).get("mode"))
            or is_performance_mode(getattr(scene, "mode", "")))


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
