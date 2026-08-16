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

A SILENT scene can be performed the same way — same portraits, no dialogue —
when the style asks for it (``h3_silent_scenes``): ``renders_acted`` is the one
predicate that decides, and everything from the prompt down treats it as an
acted scene whose cast happens to say nothing.
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


def acted_limits(chained: bool = False) -> tuple[float, float]:
    """(split threshold, render ceiling) in seconds for one acted scene.

    Unchained is the single-clip contract above. Chained (h3_chain_scenes)
    renders a scene as cadence.CHAIN_CLIPS clips joined by H3 Motion Context,
    so both limits stretch by the clip count minus the pinned frames each join
    trims — the same arithmetic cadence.scene_window() applies to narrated
    scenes. Reference engines are always MiniMax, so unlike narrated scenes
    there is no LTX carve-out: the toggle alone decides."""
    if not chained:
        return MAX_SCENE_SECONDS, H3_CEILING_SECONDS
    from pipeline import cadence
    lost = cadence.CHAIN_JOIN_SECS * (cadence.CHAIN_CLIPS - 1)
    return (MAX_SCENE_SECONDS * cadence.CHAIN_CLIPS - lost,
            H3_CEILING_SECONDS * cadence.CHAIN_CLIPS - lost)


def scene_seconds(chained: bool = False) -> float:
    """How long ONE scene is written to run — the planner's unit.

    SCENE_SECONDS for a single clip; chained, the clips minus what each join
    trims, so a film's scene count still adds up to the runtime that was asked
    for (at one scene per take, a chained film planned at the unchained length
    comes out twice as long).
    """
    if not chained:
        return SCENE_SECONDS
    from pipeline import cadence
    return (SCENE_SECONDS * cadence.CHAIN_CLIPS
            - cadence.CHAIN_JOIN_SECS * (cadence.CHAIN_CLIPS - 1))


# One audio reference per speaker, and H3 accepts at most 3 (and 9 images).
MAX_SPEAKERS_PER_SCENE = 3

# Shot sizing: ~2.5 spoken words per second, plus a beat of air. Oversized
# shots made the model pad the tail with speech nobody scripted.
WORDS_PER_SECOND = 2.5
SHOT_AIR_SECONDS = 1.5

_REFUSALS = ("Do not add subtitles, do not add captions, do not add any on-screen text, "
             "no watermark, no extra characters, no scene changes, no music.")


def _clamp_seconds(value, chained: bool = False) -> float:
    """An authored length, held inside what one scene may run.

    *chained* (h3_chain_scenes) widens the cap to the joined-clip window, the
    same way acted_limits does — without it a 20 s silent beat written for a
    chained style would be cut back to 12 and the film would come in short."""
    try:
        secs = float(value or 0) or SCENE_SECONDS
    except (TypeError, ValueError):
        secs = SCENE_SECONDS
    return max(MIN_SCENE_SECONDS, min(acted_limits(chained)[0], secs))


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
    """Normalize timed beats to [{t0, t1, action}], clipped to the clip length.

    The LLM sometimes writes "beats" as one prose sentence instead of a timed
    array — that becomes a single beat covering the whole take, rather than
    being iterated character-by-character into nothing (a stored string used
    to silently drop the action from the prompt AND crash the beat editor)."""
    if isinstance(raw, str):
        raw = [{"action": raw}] if raw.strip() else []
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


def content_seconds(meta: dict, chained: bool = False) -> float:
    """How long this scene's content actually needs on screen.

    ~2.5 spoken words/second, a beat per speaker turn, and a moment of air.
    The LLM's own "seconds" guess is ignored when there are lines — scenes it
    overloaded got truncated mid-sentence at the model's 15 s ceiling. With no
    lines (a silent scene) the authored length IS the content, held inside the
    window *chained* opens up.
    """
    lines = norm_lines(meta.get("lines"))
    if not lines:
        return _clamp_seconds(meta.get("seconds"), chained)
    words = sum(len(l["text"].split()) for l in lines)
    turns = 1 + sum(1 for a, b in zip(lines, lines[1:]) if a["speaker"] != b["speaker"])
    return words / WORDS_PER_SECOND + 1.0 * turns + 1.0


def render_seconds(meta: dict, chained: bool = False) -> float:
    """The clip length a shot is actually rendered at: never below what its
    words need, never above the ceiling. A scene that needs more than the
    ceiling will still truncate — the script-side split exists so that never
    happens; this is the last line of defence. *chained* raises the ceiling to
    the two-clip window (acted_limits)."""
    _, ceiling = acted_limits(chained)
    if norm_lines(meta.get("lines")):
        # Content wins outright when there are lines: a stored guess used as a
        # floor pads the clip, and padding is where the model invents speech.
        return min(ceiling, max(MIN_SCENE_SECONDS, content_seconds(meta)))
    return min(ceiling, _clamp_seconds(meta.get("seconds"), chained))


def split_overloaded(raw_scene: dict, chained: bool = False) -> list[dict]:
    """Split a scene whose dialogue cannot fit one clip into consecutive
    scenes: same setting, cast and sound, lines divided at speaker turns.
    More short scenes beat one long truncated one. *chained* raises the
    threshold to the two-clip window, so dialogue that previously became two
    or three scenes stays one longer take."""
    max_secs, _ = acted_limits(chained)
    lines = norm_lines(raw_scene.get("lines"))
    if not lines or content_seconds({"lines": lines}) <= max_secs:
        return [raw_scene]
    parts: list[list[dict]] = [[]]
    for line in lines:
        candidate = parts[-1] + [line]
        if parts[-1] and content_seconds({"lines": candidate}) > max_secs:
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


def split_lines_for_chain(meta: dict, n_clips: int = 2) -> list[dict]:
    """Divide one chained scene's dialogue into *n_clips* consecutive sub-metas.

    Each clip of a chained render needs a prompt carrying only ITS lines — a
    12 s clip prompted with 24 s of dialogue rushes and truncates, which is the
    exact failure chaining exists to avoid. Lines split at speaker turns where
    possible, balancing spoken seconds; setting, cast, sound and title stay
    shared so the clips read as one take. Returns [meta] unchanged when there
    is nothing to split."""
    lines = norm_lines(meta.get("lines"))
    if len(lines) < 2 or n_clips < 2:
        return [meta]
    total = content_seconds({"lines": lines})
    parts: list[list[dict]] = [[]]
    for line in lines:
        if (len(parts) < n_clips and parts[-1]
                and content_seconds({"lines": parts[-1]}) >= total / n_clips):
            parts.append([])
        parts[-1].append(line)
    out = []
    for i, chunk in enumerate(parts):
        piece = dict(meta)
        piece["lines"] = chunk
        piece["seconds"] = content_seconds({"lines": chunk})
        if i > 0:
            # Continuation clips inherit the space from the pinned frames; a
            # fresh action beat would fight them.
            piece["beats"] = []
        out.append(piece)
    return out


def split_silent_for_chain(meta: dict, n_clips: int = 2) -> list[dict]:
    """Divide one chained SILENT scene into *n_clips* consecutive sub-metas.

    The dialogue splitter has nothing to divide here — a silent scene's content
    is its LENGTH and its timed beats — so the clip window is what is split, and
    each clip takes the beats that fall inside its own window, re-based to start
    at zero. Sending every beat to clip one (what split_lines_for_chain does
    with a continuation) would leave the second clip with nothing to do but hold
    the frame. Setting, cast, camera and sound stay shared: it is one take.
    """
    total = render_seconds(meta, chained=True)
    if n_clips < 2 or total <= 0:
        return [meta]
    per = total / n_clips
    beats = norm_beats(meta.get("beats"), total)
    out = []
    for i in range(n_clips):
        lo, hi = per * i, per * (i + 1)
        mine = [{"t0": round(max(0.0, b["t0"] - lo), 1),
                 "t1": round(min(per, b["t1"] - lo), 1),
                 "action": b["action"]}
                for b in beats if b["t1"] > lo and b["t0"] < hi]
        out.append({**meta, "seconds": per, "beats": mine})
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
        return ("defines the place only — keep the space, layout, furnishings "
                "and lighting exactly as shown. It contains no people and adds "
                "none")
    if kind == "frame":
        return ("defines the scene's OPENING IMAGE — the space, light, framing "
                "and where everyone stands. Begin the take looking like this "
                "picture. Faces come from the portrait references only, voices "
                "from the audio references only")
    if kind == "continuity":
        return ("defines the SAME room, furniture, lighting and time of day "
                "already filmed in this scene — this shot is another angle of "
                "that exact space; do not redecorate or move to a new place")
    if kind in ("image", "video"):
        what = _clean(pic.get("description")) or name or "this reference"
        usage = _clean(pic.get("usage")).rstrip(".")
        if usage:
            # The user said how the reference is to be used ("the characters
            # copy this dance's movements") — their instruction IS the
            # authority line, replacing the default match-it-exactly contract.
            return f"shows {what} — {usage}"
        return (f"defines {what} only — match it exactly where it appears in "
                f"the scene. It adds no people and controls nothing else")
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


def sung_windows(scene_meta: dict) -> list[list[float]] | None:
    """When a voice is heard inside this clip, in the clip's OWN seconds.

    ``None`` means nobody measured it (a script written before the song was
    analysed, or detection that stood down) — the caller keeps the old
    assumption that the whole clip is sung. ``[]`` means measured and
    instrumental: the song is playing, but no one is singing over this shot."""
    raw = scene_meta.get("vocal_ranges")
    if raw is None:
        return None
    out = []
    for item in raw or []:
        try:
            start, end = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if end > start:
            out.append([start, end])
    return out


def _phrase_windows(windows: list[list[float]]) -> str:
    """The sung stretches as prompt text: "1.5s to 4s and 7s to 10s"."""
    return " and ".join(f"{a:g}s to {b:g}s" for a, b in windows)


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
    shown on screen is exactly what the model receives. A note for THIS take
    (``direction``) still applies on top of it — it belongs to the re-shoot, not
    to the scene, so an edited prompt must not swallow it.
    """
    direction = _clean(scene_meta.get("direction"))
    override = _clean(scene_meta.get("prompt_override"))
    if override:
        return f"{override}\n\n[DIRECTION]\n{direction}" if direction else override
    # The length the clip is RENDERED at, so the shot list describes the take
    # the model is actually asked for. Sizing the beats off the stored guess
    # instead left prompts ending at 5 s on an 8 s clip.
    seconds = render_seconds(scene_meta)
    lines = norm_lines(scene_meta.get("lines"))
    beats = norm_beats(scene_meta.get("beats"), seconds)
    pics = picture_names or []
    people = [p for p in pics
              if (not isinstance(p, dict)) or p.get("kind", "character") == "character"]
    people_names = [_picture_label(p) for p in people]
    wardrobe_owners = frozenset(
        str(p.get("character") or "").strip().lower()
        for p in pics if isinstance(p, dict) and p.get("kind") == "wardrobe")
    # A song film's shots. *sung* is when a voice is actually heard inside this
    # clip (measured off the track by assign_song_slices); *performs* is
    # whether anybody mimes it here at all — which needs both somebody on
    # screen to do it and a voice for them to follow.
    singing = bool(scene_meta.get("singing")) and not lines
    sung = sung_windows(scene_meta) if singing else None
    performs = singing and bool(people_names) and sung != []
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

    # [DIRECTION] — a note about THIS take rather than the scene: what the
    # editor asked for when re-shooting it, or how a continuation carries on
    # from the take before it. Placed ahead of the dialogue it steers.
    if direction:
        sections.append(f"[DIRECTION]\n{direction}")

    # [SOUNDTRACK] — a pinned soundtrack artifact whose card says how it is to
    # be used ("the characters dance to this track"): the take is generated
    # against that audio, and this is where the performance is told what to do
    # with it.
    track_usage = _clean(scene_meta.get("track_usage"))
    if track_usage:
        sections.append(f"[SOUNDTRACK]\nThe clip's own soundtrack is a "
                        f"provided music track. {track_usage}")

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
    elif not lines:
        if singing and not people_names:
            # A song film's B-ROLL. The scene was authored with nobody in it
            # ("no people in frame"), so its only reference is the empty place
            # — and asking for a performer anyway made the model invent one:
            # a different stranger in every such scene, and where the
            # reference was a diagram rather than a place, it abandoned the
            # art style entirely to fit a singer in. The song still plays over
            # the shot; nobody mimes it.
            geo.append("This is a scenery shot with NO people in it: nobody "
                       "appears, nobody sings, and no face is shown anywhere "
                       "in the frame. Do not add a person, a singer or a "
                       "performer. The shot holds on the place itself.")
        elif singing and sung == []:
            # Measured and instrumental — an intro, a break or the outro fills
            # this whole clip. The cast is present but nobody is singing yet.
            who = " and ".join(people_names)
            verb = "are" if len(people_names) > 1 else "is"
            geo.append(f"No voice sings anywhere in this shot: {who} "
                       f"{verb} listening and moving with the music — swaying, "
                       f"looking around, breathing — with the mouth closed and "
                       f"completely still the whole time. No miming, no "
                       f"mouthing of words, no singing.")
        elif singing:
            # The cast visibly PERFORMS the film's song. The take's own audio is
            # replaced after the gate by its slice of the real track (and the
            # whole track laid over the film), so what matters here is the
            # singing BODY — an open moving
            # mouth reads as singing under the real vocals, a closed one reads
            # as a still. The mouth is tied to the VOICE rather than ordered to
            # move throughout: this clip has the real track pinned into it, and
            # an absolute "never closed" overrode that audio and had the lead
            # mouthing a verse through a 7.5 s instrumental intro.
            who = " and ".join(people_names)
            verb = "are" if len(people_names) > 1 else "is"
            geo.append(f"{who} {verb} performing a song on camera: visibly "
                       f"singing along with the clip's own soundtrack, mouth "
                       f"opening and moving with the sung words, expressive "
                       f"face, swaying and moving with the beat. The mouth "
                       f"follows the voice on that soundtrack — it moves only "
                       f"while a voice is singing, and closes the instant the "
                       f"singing stops.")
            if sung:
                # Where in THIS clip the voice comes in, so the shot can open
                # on a closed mouth when the music is still playing alone.
                geo.append(f"A voice sings only from {_phrase_windows(sung)} of "
                           f"this shot. Outside that, {who} {verb} not singing: "
                           f"mouth closed and still, moving with the music only.")
            # This scene's own slice of the song (assign_song_slices): when the
            # render pins that stretch of the real track into the take
            # (audio-driven H3), the model hears exactly these words — and on
            # the fallback path without a pinned track, singing the REAL words
            # still puts the right mouth shapes under this stretch of the film.
            sings = _clean(str(scene_meta.get("sings") or "").replace("\n", " / "))
            if sings:
                sections.append(f"[SONG]\n{who} sing{'' if len(people_names) > 1 else 's'} "
                                f"the clip's soundtrack — exactly these words, "
                                f"in time with them: \"{sings}\"")
        else:
            # A shot with no lines is one the model will otherwise babble into —
            # measured on real establishing wides. Say it outright; the
            # render-time gate mutes whatever still gets through.
            geo.append("Nobody speaks in this scene: every mouth stays completely "
                       "closed for the whole shot.")
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
    if performs:
        # Ask for the singing VOICE (mouth movement follows the audio the model
        # writes for itself) but keep instruments out — this audio is replaced
        # by the film's real track, and "no music of any kind" would fight the
        # singing itself. A song-film shot where nobody performs (scenery, or a
        # clip that falls inside an instrumental stretch) wants the ordinary
        # no-voices sound instead: asking for a voice there is what put a
        # singer into an empty street.
        sections.append(f"[PRODUCTION SOUND]\nNative stereo ambience: {soundscape}. "
                        f"A clear live singing voice — unaccompanied: no "
                        f"instruments, no backing track, no other music.")
    else:
        speech = "Clear dialogue" if lines else "No speech and no voices at all"
        # With a pinned track whose card says what to do with it, "no music of
        # any kind" would fight the very audio the take is generated against.
        music = ("the provided soundtrack only, no other music" if track_usage
                 else "no music of any kind")
        sections.append(f"[PRODUCTION SOUND]\nNative stereo ambience: {soundscape}. "
                        f"{speech}, {music}.")

    # [NEGATIVES] — a preservation contract plus named failure modes. H3 is
    # CFG-free: these are plain sentences, not a negative prompt.
    extra = _clean(scene_meta.get("refusals"))
    # A singing scene ASKS for a voice raised in song; the blanket "no music"
    # would fight it, so only the instruments are refused there. Same for a
    # pinned track in use: refuse music BEYOND it, not the track itself.
    no_music = ("no instrumental music" if performs
                else "no music beyond the clip's own soundtrack" if track_usage
                else "no music")
    sections.append(("[NEGATIVES]\n"
                     "Preserve every reference exactly as assigned; do not modify "
                     "anything else. No extra people, no face drift, no wardrobe "
                     "changes, no voice swaps, no broken eyelines, no unmotivated "
                     "cuts. Do not add subtitles, do not add captions, do not add "
                     f"any on-screen text, no watermark, no scene changes, "
                     f"{no_music}. {extra}").strip())

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


def acted_meta(scene, chained: bool = False) -> dict:
    """The metadata an acted scene needs to render, filled in where it's absent.

    A performance script authors cast/beats/seconds/setting directly. A dialogue
    scene written in a MIXED film has only lines and the classic prompts — so
    the cast comes from who speaks, the length from what they say, and the
    setting from the scene's own video/image prompt. Without this a mixed film's
    dialogue scene would render castless (no portraits → hard failure) and
    5 seconds long.

    *chained* (h3_chain_scenes) widens the length a scene may hold to the
    joined-clip window. It only moves a SILENT scene, whose length is authored
    rather than counted from words — but that scene would otherwise be clamped
    back to the single-clip cap before the renderer ever saw how long it was
    written to run.
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
    if lines:
        # The lines decide the length EVERY time, not just when nothing is
        # stored: a value saved before the dialogue was edited goes stale, and
        # a stale one is what the editor shows and the shot list is written
        # against while the renderer sizes the clip from the words.
        meta["seconds"] = render_seconds(meta)
    else:
        # A silent scene's authored length lives in ``duration`` — on the Scene
        # itself, and in the metadata sidecar of a stored row. Re-clamped every
        # time (not only when absent) so a length stored under one chaining
        # setting is held inside the window the scene will actually render in.
        meta["seconds"] = _clamp_seconds(
            meta.get("seconds") or field("duration", 0) or meta.get("duration")
            or SCENE_SECONDS, chained)
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


def is_silent(scene) -> bool:
    return (str(scene_meta(scene).get("mode") or "").strip().lower() == "silent"
            or str(getattr(scene, "mode", "") or "").strip().lower() == "silent")


def is_singing(scene) -> bool:
    """A silent scene of a SONG film: the cast performs the film's song on
    camera (mouths moving, full performance energy) while the real vocals come
    from the generated track laid over the whole film. Stamped into scene
    metadata at divide time (story.mark_singing), so the flag travels with the
    scene through the editor and every re-render."""
    return is_silent(scene) and bool(scene_meta(scene).get("singing"))


def renders_acted(scene, cfg: dict | None = None) -> bool:
    """Does this scene render as ONE H3 Ref2VA take rather than first-frame I2V?

    Always for a dialogue scene with lines. EVERY silent scene when the style
    asks for it (``h3_silent_scenes``) — the toggle alone decides, so a film's
    silent beats are all shot the same way. A cast is not required: a silent
    scene with nobody in it opens on its own first frame instead of portraits
    (see the "frame" reference in picture_role).

    The one predicate the task planner, the renderer and the editor's re-shoot
    all call — they must agree, or a scene is planned on one path and rendered
    on the other (an acted task nobody completes hangs the render).
    """
    if is_performance(scene) and (scene_meta(scene).get("lines")
                                  or getattr(scene, "lines", None)):
        return True
    # A singing scene is a performance by definition — the film exists to show
    # the cast performing its song — so the flag on the SCENE decides, with no
    # style toggle involved.
    if is_singing(scene):
        return True
    return is_silent(scene) and bool((cfg or {}).get("h3_silent_scenes"))


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
