"""A/B experiments on the H3 acted-scene prompt.

``build_h3_prompt`` (pipeline/performance.py) writes its own six-block format
— [REFERENCE USE] / [IDENTITY LOCKS] / … / [NEGATIVES]. MiniMax's own
prompt-writing guide (MiniMax-AI/MiniMax-H3,
skills/h3-prompt-writing/references/ref-en.txt) documents the format H3's
rewriter emits and the model was trained against: for Ref2VA a fixed
six-SECTION schema (subject_definitions / summary / retention_analysis /
detailed_description / overall_soundscape / non_diegetic_music), ``<Subject N>``
labels in instructional prose, ``<d>[lang] …</d>`` dialogue markup, and
positive field statements instead of prose prohibitions.

This module builds prompt VARIANTS that each isolate one of those ideas so a
take rendered from each can be compared against the current prompt:

  baseline          build_h3_prompt untouched (control)
  positive-audio    baseline, prose audio prohibitions -> positive statements
  dialogue-markup   baseline, dialogue lines in native <d>[English] markup
  schema            native six-section Ref2VA skeleton, same content
  schema-labels     schema + <Subject N> labels replacing names in prose
  schema-reinforce  schema + appearance repeated across sections (the guide:
                    repetition is reinforcement, not redundancy)
  native-full       schema + labels + markup + positive, the whole stack

Every variant is assembled from the SAME structured scene meta, and the
schema variants reuse the baseline's own section bodies (parsed back out of
build_h3_prompt's output) so content parity holds: two variants differ only by
the idea under test.  Nothing here is wired into the render pipeline — the
production prompt is untouched until an experiment wins.

Scope: dialogue/acted scenes. Song-film metas (``singing``) are out of scope
and rejected loudly rather than half-translated.
"""
from __future__ import annotations

import re

from pipeline import performance as _perf

# ---------------------------------------------------------------------------
# Baseline parsing — build_h3_prompt emits "\n\n"-joined sections whose first
# line is the [HEADER]; section bodies never contain a blank line.
# ---------------------------------------------------------------------------


def split_sections(prompt: str) -> list[tuple[str, str]]:
    """[(header, body)] in emitted order; header "" for a headerless chunk."""
    out = []
    for chunk in prompt.split("\n\n"):
        head, _, body = chunk.partition("\n")
        if re.fullmatch(r"\[[A-Z ]+\]", head):
            out.append((head[1:-1], body))
        else:
            out.append(("", chunk))
    return out


def section(prompt: str, name: str) -> str:
    return next((b for h, b in split_sections(prompt) if h == name), "")


def _replace_section(prompt: str, name: str, body: str) -> str:
    chunks = []
    for head, old in split_sections(prompt):
        text = old if head == "" else f"[{head}]\n{old}"
        if head == name:
            text = f"[{head}]\n{body}"
        chunks.append(text)
    return "\n\n".join(chunks)


def _reject_singing(meta: dict) -> None:
    if meta.get("singing"):
        raise ValueError("h3_prompt_experiments covers dialogue scenes only — "
                         "song-film prompts carry sung-window logic these "
                         "variants do not translate.")


# ---------------------------------------------------------------------------
# Experiment: dialogue markup  (<d>[English] … </d>)
# ---------------------------------------------------------------------------

def speaker_ids(lines: list[dict]) -> dict[str, str]:
    """Stable ``(S1)``-style speaker IDs in order of first line (official
    base guide: subjects who speak use stable IDs such as (S1) and (S2))."""
    sids: dict[str, str] = {}
    for line in lines:
        if line["speaker"] not in sids:
            sids[line["speaker"]] = f"(S{len(sids) + 1})"
    return sids


def _markup_dialogue_lines(lines: list[dict], sids: dict | None = None) -> list[str]:
    """The [DIALOGUE] lines in H3's native markup.

    The ``<d>`` tag owns verbatim-ness (so "says exactly" relaxes to "says");
    the lips-close sentence is a separately proven instruction and stays.
    With *sids*, each speaker carries their stable ``(S1)`` ID — the official
    format's per-line voice binding.
    """
    out = []
    for line in lines:
        sid = f" {sids[line['speaker']]}" if sids else ""
        out.append(
            f"{line['speaker']}{sid} says, {line['delivery']}: "
            f"<d>[English] {line['text']}</d> "
            f"{line['speaker']}'s lips close and all mouth movement stops the "
            f"instant the line ends.")
    return out


def apply_dialogue_markup(prompt: str, meta: dict) -> str:
    lines = _perf.norm_lines(meta.get("lines"))
    if not lines or not section(prompt, "DIALOGUE"):
        return prompt
    return _replace_section(prompt, "DIALOGUE",
                            "\n".join(_markup_dialogue_lines(lines)))


_TAIL_SILENCE = ("After the final scripted line, no one speaks again for the "
                 "rest of the clip: both mouths stay completely closed and "
                 "still through to the last frame while the action simply "
                 "continues.")


def apply_tail_silence(prompt: str, meta: dict) -> str:
    """An explicit end-state for the take's tail.

    The clip renders longer than the words need, and H3 pads the unclaimed
    tail with invented speech (the gibberish the render gate normally mutes).
    Negatives don't hold there; this states the positive end-state instead.
    """
    if not _perf.norm_lines(meta.get("lines")) or not section(prompt, "DIALOGUE"):
        return prompt
    if _TAIL_SILENCE.split(":")[0] in prompt:   # production carries it now
        return prompt
    body = section(prompt, "DIALOGUE") + "\n" + _TAIL_SILENCE
    return _replace_section(prompt, "DIALOGUE", body)


# ---------------------------------------------------------------------------
# Experiment: positive audio fields instead of prose prohibitions
# ---------------------------------------------------------------------------

# The exact music clauses build_h3_prompt writes into [NEGATIVES] / sound.
_MUSIC_NEG_RE = re.compile(
    r",?\s*no (?:instrumental )?music(?: beyond the clip's own soundtrack"
    r"| of any kind)?")
_SPEECH_NEG = "No speech and no voices at all"


def _positive_soundscape(meta: dict) -> str:
    """The [PRODUCTION SOUND] body as a positive complete-soundtrack statement."""
    ss = _perf._unterminated(meta.get("soundscape")) or "quiet room tone throughout"
    lines = _perf.norm_lines(meta.get("lines"))
    track = _perf._clean(meta.get("track_usage"))
    if track:
        return (f"Native stereo ambience: {ss}. The complete soundtrack is "
                f"this ambience, the provided music track"
                f"{' and the spoken dialogue' if lines else ''} — every sound "
                f"present is one of those.")
    if lines:
        return (f"Native stereo ambience: {ss}. The complete soundtrack is "
                f"this ambience and the cast's spoken dialogue — every sound "
                f"present is one of those two.")
    return (f"Native stereo ambience: {ss}. The complete soundtrack is this "
            f"ambience alone — every sound present belongs to the scene itself.")


def apply_positive_audio(prompt: str, meta: dict) -> str:
    """Audio prohibitions -> positive end-state statements.

    Community testing (and MiniMax's field placement) says prose audio
    negatives are intermittently dropped while the desired state in the right
    place holds. Visual negatives (subtitles, watermark, extra people) and the
    paired-positive lips-close lock are untouched — the documented exception
    is a negative next to a positive statement, which those already are.
    """
    out = prompt
    if section(out, "PRODUCTION SOUND"):
        out = _replace_section(out, "PRODUCTION SOUND", _positive_soundscape(meta))
    neg = section(out, "NEGATIVES")
    if neg:
        neg = _MUSIC_NEG_RE.sub("", neg)
        neg = neg.replace(_SPEECH_NEG, "The only voices are the cast's scripted lines")
        out = _replace_section(out, "NEGATIVES", _perf._clean(neg))
    return out


# ---------------------------------------------------------------------------
# Reference model for the schema variants
# ---------------------------------------------------------------------------

def _pic_name(pic) -> str:
    return pic if isinstance(pic, str) else _perf._clean(pic.get("name"))


def _pic_kind(pic) -> str:
    return "character" if isinstance(pic, str) else (pic.get("kind") or "character")


def reference_model(pics: list, audio_names: list[str]) -> dict:
    """<Picture/Audio i> physical slots -> <Subject k> content units.

    Characters and locations become Subjects (the guide's reusable content
    units); a wardrobe picture folds into its owner's definition; ``frame`` and
    ``continuity`` pictures stay picture-level (a concrete target frame / a
    planning anchor is exactly what <Picture N> means in the native schema).
    """
    subjects, frames, anchors, generics = [], [], [], []
    wardrobe = {}
    for i, pic in enumerate(pics or [], start=1):
        kind, name = _pic_kind(pic), _pic_name(pic)
        hint = "" if isinstance(pic, str) else _perf._clean(pic.get("hint"))
        if kind == "wardrobe":
            wardrobe[(pic.get("character") or "").strip().lower()] = i
        elif kind == "character":
            subjects.append({"name": name, "kind": "character", "pic": i,
                             "hint": hint})
        elif kind == "location":
            subjects.append({"name": name or "the location", "kind": "location",
                             "pic": i})
        elif kind == "frame":
            frames.append(i)
        elif kind == "continuity":
            anchors.append(i)
        else:  # image / video reference assets
            desc = _perf._clean(pic.get("description")) or name or "this reference"
            usage = _perf._clean(pic.get("usage"))
            generics.append({"name": name or desc, "desc": desc, "usage": usage,
                             "pic": i})
    for k, sub in enumerate(subjects, start=1):
        sub["label"] = f"<Subject {k}>"
        if sub["kind"] == "character":
            sub["wardrobe_pic"] = wardrobe.get(sub["name"].strip().lower())
    audios = [{"label": f"<Audio {i}>", "name": n}
              for i, n in enumerate(audio_names or [], start=1)]
    return {"subjects": subjects, "frames": frames, "anchors": anchors,
            "generics": generics, "audios": audios}


def _subject_definitions(model: dict) -> list[str]:
    out = []
    for sub in model["subjects"]:
        if sub["kind"] == "character":
            line = (f"{sub['label']} is {sub['name']}, the person shown in "
                    f"<Picture {sub['pic']}>")
            if sub.get("hint"):
                line += f" — {sub['hint']}"
            line += (f"; {sub['name']}'s face, hair and build come from "
                     f"<Picture {sub['pic']}>")
            if sub.get("wardrobe_pic"):
                line += (f", and the exact garments, colours and details of "
                         f"{sub['name']}'s clothes come from "
                         f"<Picture {sub['wardrobe_pic']}> (ignore that "
                         f"picture's background)")
            out.append(line + ".")
        else:
            out.append(f"{sub['label']} is the location shown in "
                       f"<Picture {sub['pic']}> — its space, layout, "
                       f"furnishings and lighting; it contains no people.")
    for gen in model["generics"]:
        line = f"<Picture {gen['pic']}> shows {gen['desc']}"
        if gen["usage"]:
            line += f" — {gen['usage'].rstrip('.')}"
        out.append(line + ".")
    for i in model["frames"]:
        out.append(f"<Picture {i}> is the take's opening frame — the space, "
                   f"light, framing and where everyone stands at 0.00 seconds.")
    for i in model["anchors"]:
        out.append(f"<Picture {i}> shows the same room, furniture, lighting "
                   f"and time of day already filmed in this scene; this shot "
                   f"is another angle of that exact space.")
    for aud in model["audios"]:
        out.append(f"{aud['label']} is {aud['name']}'s voice.")
    return out


def _retention_analysis(model: dict) -> list[str]:
    out = []
    for sub in model["subjects"]:
        if sub["kind"] == "character":
            out.append(f"{sub['label']} ({sub['name']}, appears in [Shot 1]): "
                       f"fully_preserved - {sub['name']}'s face, hair, build "
                       f"and wardrobe stay exactly as defined; never merged "
                       f"with or swapped for anyone else.")
        else:
            out.append(f"{sub['label']} (appears in [Shot 1]): fully_preserved "
                       f"- the architecture, layout, furnishings and lighting "
                       f"are retained; no redecoration and no new place.")
    for gen in model["generics"]:
        out.append(f"<Picture {gen['pic']}>: attribute_transfer - only what it "
                   f"is assigned to define transfers; it adds no people and "
                   f"controls nothing else.")
    for i in model["frames"]:
        out.append(f"<Picture {i}>: fully_preserved - the opening composition; "
                   f"the take begins looking like this frame, then the action "
                   f"plays on.")
    for i in model["anchors"]:
        out.append(f"<Picture {i}>: partially_preserved - the room, furniture, "
                   f"lighting and time of day carry over; the framing and "
                   f"action are this shot's own.")
    for aud in model["audios"]:
        out.append(f"{aud['label']}: reference - {aud['name']}'s voice timbre "
                   f"only; {aud['name']} must speak in exactly this voice.")
    return out


def _reinforce_recaps(model: dict) -> list[str]:
    """First-appearance appearance recaps — the deliberate repetition the
    guide calls reinforcement."""
    out = []
    for sub in model["subjects"]:
        if sub["kind"] != "character":
            continue
        looks = sub.get("hint") or f"exactly the person in <Picture {sub['pic']}>"
        out.append(f"{sub['name']} — {looks} — is on screen with the same "
                   f"face, hair, build and wardrobe as the reference, "
                   f"unchanged for the whole take.")
    return out


# ---------------------------------------------------------------------------
# Experiment: <Subject N> labels in instructional prose
# ---------------------------------------------------------------------------

# Spans a label substitution must never touch: literal dialogue.
_PROTECTED_RE = re.compile(r'(<d>.*?</d>|"[^"\n]*")')


def substitute_labels(text: str, model: dict) -> str:
    """Names -> <Subject k> in instructional prose, never inside dialogue.

    The guide: the label is the stable routing identity across the structured
    prompt; a spoken name is a different thing — a character may genuinely say
    one aloud — so quoted/<d> spans are preserved verbatim.
    """
    mapping = sorted(((s["name"], s["label"]) for s in model["subjects"]
                      if s["kind"] == "character" and s["name"]),
                     key=lambda kv: -len(kv[0]))
    parts = _PROTECTED_RE.split(text)
    for idx in range(0, len(parts), 2):        # even indexes = outside spans
        chunk = parts[idx]
        for name, label in mapping:
            chunk = re.sub(rf"(?<![A-Za-z0-9_>]){re.escape(name)}(?![A-Za-z0-9_<])",
                           label, chunk, flags=re.IGNORECASE)
        parts[idx] = chunk
    return "".join(parts)


# ---------------------------------------------------------------------------
# The native six-section schema
# ---------------------------------------------------------------------------

def _sentence(text: str) -> str:
    text = _perf._clean(text)
    if text and text[-1] not in ".!?…":
        text += "."
    return text


def build_schema_prompt(meta: dict, *, style_note: str = "",
                        picture_names: list | None = None,
                        audio_names: list[str] | None = None,
                        labels: bool = False, markup: bool = False,
                        reinforce: bool = False, positive: bool = False,
                        sid: bool = False, tail: bool = False) -> str:
    """The scene in H3's native Ref2VA schema, content-parallel to baseline.

    Geography, camera and the preservation contract are lifted from
    build_h3_prompt's own output (parsed back out), so a schema variant and the
    baseline say the same things — only the container, and any flagged
    experiment on top, differ. Note one inherent overlap: the schema has a
    ``non_diegetic_music`` field, so the music statement moves there even
    without ``positive`` — that much of the positive-fields idea rides along
    with the structure itself.
    """
    _reject_singing(meta)
    base = _perf.build_h3_prompt(meta, style_note=style_note,
                                 picture_names=picture_names,
                                 audio_names=audio_names)
    model = reference_model(picture_names or [], audio_names or [])
    lines = _perf.norm_lines(meta.get("lines"))
    sids = speaker_ids(lines) if sid else {}
    seconds = _perf.render_seconds(meta)
    beats = _perf.norm_beats(meta.get("beats"), seconds)
    cast = [s["name"] for s in model["subjects"] if s["kind"] == "character"]

    # summary — task type prefix + one factual paragraph, no new information.
    setting = _perf._clean(meta.get("setting"))
    place = re.split(r"[.:]", setting)[0].strip() if setting else ""
    place = (place[0].lower() + place[1:]) if place else "the referenced location"
    who = " and ".join(cast) or "the cast"
    talk = (f"{len(lines)} line{'s' if len(lines) != 1 else ''} of scripted "
            f"dialogue" if lines else "no dialogue")
    summary = (f"[reference-guided video generation] In {place}, {who} play a "
               f"{seconds:.0f}-second take with {talk}; identities, wardrobe, "
               f"voices and the location follow the references exactly.")

    # detailed_description — playback order: style, opening state, direction,
    # geography, (reinforcement), timed beats, dialogue, camera, contract.
    body: list[str] = []
    if _perf._clean(style_note):
        body.append(_sentence(f"The style: {_perf._clean(style_note)}"))
    if setting:
        body.append(_sentence(setting))
    direction = _perf._clean(meta.get("direction"))
    if direction:
        body.append(_sentence(direction))
    geo = section(base, "SCREEN GEOGRAPHY")
    if geo:
        body.extend(geo.split("\n"))
    if reinforce:
        body.extend(_reinforce_recaps(model))
    for beat in beats:
        body.append(f"From {beat['t0']:g}s to {beat['t1']:g}s, "
                    f"{_sentence(beat['action'])}")
    if lines:
        if markup:
            dlg = _markup_dialogue_lines(lines, sids or None)
        else:
            dlg = section(base, "DIALOGUE").split("\n")
            if sids:
                dlg = [next((line.replace(f"{n} says", f"{n} {t} says", 1)
                             for n, t in sids.items()
                             if line.startswith(f"{n} says")), line)
                       for line in dlg]
        body.extend(dlg)
        if tail:
            body.append(_TAIL_SILENCE)
    camera = _perf._unterminated(meta.get("camera")) or \
        "locked off at chest height, slight handheld drift, no push, no zoom"
    body.append(f"The camera is {camera}.")
    contract = section(base, "NEGATIVES")
    if positive:
        contract = _perf._clean(
            _MUSIC_NEG_RE.sub("", contract).replace(
                _SPEECH_NEG, "The only voices are the cast's scripted lines"))
    body.append(contract)

    soundscape = (_positive_soundscape(meta) if positive
                  else section(base, "PRODUCTION SOUND"))
    track = _perf._clean(meta.get("track_usage"))
    music = (f"N/A — the clip's own soundtrack is the provided music track. "
             f"{track}" if track else "N/A")

    defs = _subject_definitions(model)
    if sids:
        # The official base guide's stable speaker IDs: bind each voice to its
        # subject at definition time, then repeat the ID on every line.
        for i, d in enumerate(defs):
            for name, sid_tag in sids.items():
                d = d.replace(f"is {name},", f"is {name} {sid_tag},")
                d = d.replace(f"is {name}'s voice.",
                              f"is the voice of {name} {sid_tag}; {name} "
                              f"speaks only in this voice and no other voice "
                              f"speaks {name}'s lines.")
            defs[i] = d
    sections = [
        "subject_definitions:\n" + "\n".join(defs),
        "summary:\n" + summary,
        "retention_analysis:\n" + "\n".join(_retention_analysis(model)),
        "detailed_description:\n[Shot 1] " + " ".join(x for x in body if x),
        "overall_soundscape:\n" + soundscape,
        "non_diegetic_music:\n" + music,
    ]
    if labels:
        # Only instructional prose carries labels; subject_definitions and
        # retention_analysis keep the human names that bind label to name.
        sections[1] = substitute_labels(sections[1], model)
        sections[3] = substitute_labels(sections[3], model)
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# The comparison matrix
# ---------------------------------------------------------------------------

VARIANTS = ("baseline", "positive-audio", "dialogue-markup", "schema",
            "schema-labels", "schema-reinforce", "native-full",
            # Round 2 — built from round 1's viewing notes: schema variants
            # misbound speakers (no stable IDs), and every take babbled into
            # its unclaimed tail.
            "baseline-tail", "schema-sid", "native-v2")


def build_variants(meta: dict, *, style_note: str = "",
                   picture_names: list | None = None,
                   audio_names: list[str] | None = None) -> dict[str, str]:
    """Every experiment's prompt for one scene, keyed by variant name."""
    _reject_singing(meta)
    kw = dict(style_note=style_note, picture_names=picture_names,
              audio_names=audio_names)
    base = _perf.build_h3_prompt(meta, **kw)
    return {
        "baseline": base,
        "positive-audio": apply_positive_audio(base, meta),
        "dialogue-markup": apply_dialogue_markup(base, meta),
        "schema": build_schema_prompt(meta, **kw),
        "schema-labels": build_schema_prompt(meta, labels=True, **kw),
        "schema-reinforce": build_schema_prompt(meta, reinforce=True, **kw),
        "native-full": build_schema_prompt(meta, labels=True, markup=True,
                                           reinforce=True, positive=True, **kw),
        "baseline-tail": apply_tail_silence(base, meta),
        "schema-sid": build_schema_prompt(meta, sid=True, **kw),
        "native-v2": build_schema_prompt(meta, labels=True, markup=True,
                                         reinforce=True, positive=True,
                                         sid=True, tail=True, **kw),
    }


# ---------------------------------------------------------------------------
# Advisory lint — the prompt-check idea, smallest useful slice.
# ---------------------------------------------------------------------------

_QUALITY_RE = re.compile(
    r"\b(cinematic|masterpiece|stunning|breathtaking|beautiful|4k|8k|hyper[- ]?"
    r"realistic|photorealistic|high quality|award[- ]winning|epic)\b", re.I)
# "no voice swaps" is an identity negative, not an audio-layer prohibition.
_AUDIO_NEG_RE = re.compile(
    r"\bno (?:music|speech|voices?(?! swaps)|singing|sound|audio)\b", re.I)


def description_word_count(prompt: str) -> int:
    """Words in detailed_description (schema) or the whole prompt (baseline)."""
    m = re.search(r"detailed_description:\n([\s\S]*?)\n\noverall_soundscape:",
                  prompt)
    text = m.group(1) if m else prompt
    return len(text.split())


def lint_prompt(prompt: str, meta: dict) -> list[str]:
    """Advisory findings, one line each — never blocking."""
    out = []
    hits = sorted({m.group(0).lower() for m in _QUALITY_RE.finditer(prompt)})
    if hits:
        out.append(f"quality tokens (describe behaviour, not your reaction): "
                   f"{', '.join(hits)}")
    negs = len(_AUDIO_NEG_RE.findall(prompt))
    if negs:
        out.append(f"{negs} prose audio prohibition(s) — intermittently "
                   f"ignored; prefer the positive statement in the right field")
    words = description_word_count(prompt)
    if "detailed_description:" in prompt and not 350 <= words <= 500:
        out.append(f"detailed_description is {words} words; MiniMax's normal "
                   f"Ref2VA generation target is 350–500")
    lines = _perf.norm_lines(meta.get("lines"))
    if lines:
        spoken = sum(len(l["text"].split()) for l in lines)
        secs = _perf.render_seconds(meta)
        if spoken / secs > 2.8:
            out.append(f"dialogue is {spoken} words in {secs:.1f}s "
                       f"(~{spoken / secs:.1f} w/s; comfortable speech is ~2.5)")
    return out


# ---------------------------------------------------------------------------
# A complex demo scene — everything at once: two speakers, wardrobe, location,
# continuity, timed beats, direction, refusals. The scene the render pipeline
# would normally split into solo shots is used WHOLE here on purpose: whether
# a labelled two-hander holds identities is exactly experiment #2's question.
# ---------------------------------------------------------------------------

def demo_scene() -> dict:
    """(meta, style_note, picture_names, audio_names) for the A/B harness."""
    meta = {
        "mode": "dialogue",
        "cast": ["MARA", "ELLIS"],
        "setting": ("A cramped harbour-master's office at night: charts pinned "
                    "over peeling paint, a brass lamp throwing warm light "
                    "across a cluttered desk, rain streaking the black window "
                    "behind the two of them"),
        "camera": ("a slow push from a locked-off two-shot at chest height in "
                   "to a loose medium on the desk, slight handheld drift"),
        "soundscape": ("heavy rain on the tin roof, a buoy bell far off, the "
                       "lamp's faint electrical hum"),
        "refusals": "No radio chatter and no thunder.",
        "direction": ("Play it tired rather than angry — the argument is "
                      "already lost and both of them know it."),
        "seconds": 14,
        "beats": [
            {"t0": 0, "t1": 4, "action": "MARA slides the folded manifest "
             "across the desk without letting go of it"},
            {"t0": 4, "t1": 9, "action": "ELLIS reads it, jaw tightening, and "
             "sets his glasses down on the chart"},
            {"t0": 9, "t1": 14, "action": "MARA finally releases the paper and "
             "steps back from the desk into the lamplight"},
        ],
        "lines": [
            {"speaker": "MARA", "delivery": "flat, worn out",
             "text": "Ellis, look at the third column. Just look at it."},
            {"speaker": "ELLIS", "delivery": "quiet, refusing to rise",
             "text": "I signed what the company sent me. Same as every month."},
            {"speaker": "MARA", "delivery": "leaning in, almost a whisper",
             "text": "Then the company sank that boat."},
        ],
    }
    pictures = [
        {"slot": 1, "name": "MARA", "kind": "character",
         "hint": "a weathered woman in her fifties, grey-streaked dark hair "
                 "pinned back, deep lines around sharp pale eyes"},
        {"slot": 2, "name": "ELLIS", "kind": "character",
         "hint": "a heavyset man in his sixties, white stubble, wire-rim "
                 "glasses, a faded navy watch cap"},
        {"slot": 3, "name": "Harbour office", "kind": "location"},
        {"slot": 4, "name": "Mara's oilskin coat", "kind": "wardrobe",
         "character": "MARA"},
        {"slot": 5, "name": "Continuity", "kind": "continuity"},
    ]
    audio_names = ["MARA", "ELLIS"]
    style_note = ("grainy 1970s 16mm neo-noir: warm tungsten interiors, deep "
                  "shadow, muted sea-green and amber palette")
    return {"meta": meta, "style_note": style_note,
            "picture_names": pictures, "audio_names": audio_names}
