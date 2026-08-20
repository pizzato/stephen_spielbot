"""Story-first script generation — the one way a script is written.

Instead of generating scenes directly in batches (pipeline/llm.py), this mode
writes the whole story as prose first, judges it, and only then divides it
into scenes — so long scripts stay coherent:

    outline (1 call) → chapter prose (1 call per ~10 scenes)
    → critique judge (1 call) → rewrite only flagged chapters
    → divide into scenes (1 JSON call per chapter)

Every step is a one-shot ``_chat_complete`` call, so the mode works on all
four backends (claude/grok/openai/local) through a single code path. The
classic batched generators in pipeline/llm.py are untouched.

Dialogue/mixed formats ride the same path: the prose story is drafted with no
scene or length constraints, and the divide step stages it as acted scenes
(see the *dialogue_note* on ``divide_story``).
"""
from __future__ import annotations

import logging
import math
import re
import time
from pathlib import Path

from pipeline import prompts as _prompts
from pipeline.llm import (
    Scene,
    _chat_complete,
    _detect_recurring_characters,
    _fill_empty_narrations,
    _load_cfg,
    _merge_character_note,
    _norm_identified_characters,
    _parse_claude_response,
    _scene_len_vars,
    condense_long_narrations,
    enforce_scene_word_caps,
    narration_language_name,
    regen_split_scene_visuals,
)

logger = logging.getLogger("video_gen")

_SCENES_PER_CHAPTER = 10   # one prose chapter per ~10 video scenes
_SILENT_SECONDS = 5.0      # a silent beat with no authored length
_WORDS_PER_SCENE = 35      # legacy prose budget when no cadence plan is given


def _words_per_scene(scene_plan: dict | None) -> int:
    """Per-scene prose budget: the cadence plan's word target (10–15 s of
    narration at the narrator's pace), or the legacy constant without one."""
    if scene_plan and scene_plan.get("scene_words_target"):
        return int(scene_plan["scene_words_target"])
    return _WORDS_PER_SCENE


# ── Prompt-note builders (same wording as the classic generator) ──────────────

def _style_note(style_hint: str | None) -> str:
    return (f'\nIMPORTANT: Use exactly this text for the "style" field: "{style_hint}"'
            if style_hint and style_hint.strip() else "")


def _video_style_note(video_style_hint: str | None) -> str:
    return (f'\nMOTION DIRECTION — apply to EVERY scene\'s "video_prompt": {video_style_hint.strip()}'
            if video_style_hint and video_style_hint.strip() else "")


def _avoid_note(avoid_hint: str | None) -> str:
    return (f"\nAVOID — do NOT mention, depict, or reference any of the following anywhere "
            f"in the script (narration, titles, or prompts): {avoid_hint.strip()}"
            if avoid_hint and avoid_hint.strip() else "")


def _character_note(character_sheet: str | None) -> str:
    return f"\n{character_sheet.strip()}" if character_sheet and character_sheet.strip() else ""


def _language_note(language: str | None) -> str:
    lang_name = narration_language_name(language)
    return (f'\nNARRATION LANGUAGE — write every scene\'s "narration" in {lang_name}. '
            f'Everything else — scene titles, "image_prompt", "video_prompt", and character '
            f"names/descriptions — stays in English (it feeds English-only image/video models)."
            if lang_name else "")


def _title_line(title: str, video_title: str | None) -> str:
    if video_title and video_title.strip():
        return f'YouTube Video Title: "{video_title}"\nTopic/Description: "{title}"\n'
    return f'Topic: "{title}"\n'


def _call_fn(cfg: dict):
    """Adapt _chat_complete to the call_fn(system, user, max_tokens, label,
    retries) signature that _fill_empty_narrations/_detect_recurring_characters
    expect."""
    def call(system, user_msg, max_tokens, label, retries=3):
        return _chat_complete(cfg, system, user_msg, max_tokens, label, retries=retries)
    return call


# ── Chapter scene budgets ─────────────────────────────────────────────────────

def _pro_rate_budgets(counts: list, n_scenes: int) -> list[int]:
    """Scale the LLM's per-chapter scene counts so they sum to exactly n_scenes
    (each chapter keeps at least 1 scene). Never trust LLM arithmetic."""
    clean = []
    for c in counts:
        try:
            clean.append(max(1, int(c)))
        except (TypeError, ValueError):
            clean.append(1)
    clean = clean[:n_scenes] or [n_scenes]  # never more chapters than scenes
    total = sum(clean)
    scaled = [max(1, round(c * n_scenes / total)) for c in clean]
    while sum(scaled) > n_scenes:
        scaled[scaled.index(max(scaled))] -= 1
    while sum(scaled) < n_scenes:
        scaled[scaled.index(min(scaled))] += 1
    return scaled


def _tail_words(text: str, n_words: int = 120) -> str:
    words = (text or "").split()
    return " ".join(words[-n_words:])


def _head_words(text: str, n_words: int = 60) -> str:
    words = (text or "").split()
    return " ".join(words[:n_words])


# ── Story generation: outline → chapters → critique → revise ─────────────────

def generate_story(title: str, n_scenes: int,
                   style_hint: str | None = None,
                   video_title: str | None = None,
                   character_sheet: str | None = None,
                   avoid_hint: str | None = None,
                   scene_plan: dict | None = None,
                   dialogue_note: str | None = None) -> dict:
    """Draft, judge, and revise the full prose story. Returns the story dict
    persisted as story.json (status "draft"); ``divide_story`` turns it into
    scenes. *scene_plan* (pipeline/cadence.py) sets the prose word budget per
    scene from the narrator's cadence; without one the legacy 35-word budget
    applies."""
    cfg = _load_cfg()
    call = _call_fn(cfg)
    # NOTE: a future web-research/fact-check step would run here, feeding
    # verified facts into the outline prompt.
    n_chapters = max(1, math.ceil(n_scenes / _SCENES_PER_CHAPTER))
    style_note = _style_note(style_hint)
    avoid_note = _avoid_note(avoid_hint)
    character_note = _character_note(character_sheet)
    # An acted film needs people to speak: the outline must come back with
    # recurring characters, not the empty list an abstract topic would give.
    dialogue_str = f"\n{dialogue_note.strip()}" if dialogue_note and dialogue_note.strip() else ""

    # ── Outline: arc plan + style/music/characters ────────────────────────────
    raw = call(
        _prompts.system("story_outline", n_chapters=n_chapters, n_scenes=n_scenes),
        _prompts.user("story_outline", title_line=_title_line(title, video_title),
                      n_scenes=n_scenes, n_chapters=n_chapters,
                      style_note=style_note, avoid_note=avoid_note,
                      character_note=character_note, dialogue_note=dialogue_str),
        # The floor covers the fixed keys (style + music + two full character
        # objects) regardless of chapter count — a one-chapter film's outline
        # is not a small response.
        1500 + 150 * n_chapters, "story outline",
    )
    outer = _parse_claude_response(raw, "story outline")
    raw_chapters = [c for c in (outer.get("chapters") or []) if isinstance(c, dict)]
    if not raw_chapters:
        raise RuntimeError("Story outline returned no chapters")
    budgets = _pro_rate_budgets([c.get("scenes") for c in raw_chapters], n_scenes)
    outline = [{"chapter": i + 1,
                "title": str(c.get("title") or f"Chapter {i + 1}").strip(),
                "summary": str(c.get("summary") or "").strip(),
                "scenes": budgets[i]}
               for i, c in enumerate(raw_chapters[:len(budgets)])]
    style = style_hint.strip() if style_hint and style_hint.strip() else str(outer.get("style") or "")
    music = str(outer.get("music") or "cinematic orchestral background music, atmospheric, instrumental")
    identified = _norm_identified_characters(outer.get("characters"))
    character_note = _merge_character_note(character_note, identified)

    # ── Chapter prose, in order, each continuing from the previous tail ───────
    title_context = f'"{video_title}"' if video_title and video_title.strip() else f'"{title}"'
    outline_str = "\n".join(
        f'Chapter {o["chapter"]} — "{o["title"]}" ({o["scenes"]} scenes): {o["summary"]}'
        for o in outline
    )
    chapters: list[dict] = []
    words_per_scene = _words_per_scene(scene_plan)
    for o in outline:
        target_words = o["scenes"] * words_per_scene
        prev_tail = (
            f'The previous chapter ends with: "…{_tail_words(chapters[-1]["text"])}"\n'
            if chapters else ""
        )
        text = call(
            _prompts.system("story_chapter", target_words=target_words),
            _prompts.user("story_chapter", title_context=title_context,
                          outline_str=outline_str, chapter_title=o["title"],
                          chapter_summary=o["summary"], target_words=target_words,
                          prev_tail=prev_tail, avoid_note=avoid_note,
                          character_note=character_note),
            o["scenes"] * (words_per_scene * 2 + 30) + 300, f'story chapter {o["chapter"]}',
        ).strip()
        if not text:
            raise RuntimeError(f'Story chapter {o["chapter"]} came back empty')
        chapters.append({**o, "text": text})

    # ── Critique + targeted revision (best-effort: never blocks the story) ────
    critique = _critique_and_revise(call, title_context, chapters, avoid_note, character_note)

    now = time.time()
    return {
        "version": 1,
        "topic": title,
        "video_title": (video_title or "").strip(),
        "n_scenes": n_scenes,
        "scene_plan": scene_plan,
        "outline": outline,
        "chapters": chapters,
        "critique": critique,
        "style": style,
        "music": music,
        "characters": identified,
        "status": "draft",
        "created_at": now,
        "updated_at": now,
    }


def _critique_and_revise(call, title_context: str, chapters: list[dict],
                         avoid_note: str, character_note: str) -> dict:
    """One judge pass over the whole draft; rewrite only flagged chapters
    (mutating *chapters* in place). Any failure degrades to verdict "skipped"
    rather than blocking generation (e.g. a local model whose context can't
    hold the full story)."""
    chapters_str = "\n\n".join(
        f'Chapter {c["chapter"]} — "{c["title"]}":\n{c["text"]}' for c in chapters
    )
    try:
        raw = call(
            _prompts.system("story_critique"),
            _prompts.user("story_critique", n_chapters=len(chapters), chapters_str=chapters_str),
            1000, "story critique", retries=2,
        )
        data = _parse_claude_response(raw, "story critique")
    except Exception as exc:  # noqa: BLE001 — best-effort judge
        logger.warning("Story critique failed (%s) — keeping the draft as-is", exc)
        return {"verdict": "skipped", "notes": [], "chapters": []}

    verdict = "revise" if data.get("verdict") == "revise" else "pass"
    notes = [str(x) for x in (data.get("notes") or []) if str(x).strip()][:5]
    by_id = {c["chapter"]: c for c in chapters}
    flagged = []
    for row in (data.get("chapters") or []):
        if not isinstance(row, dict):
            continue
        try:
            num = int(row.get("chapter"))
        except (TypeError, ValueError):
            continue
        issues = str(row.get("issues") or "").strip()
        if num in by_id and issues:
            flagged.append({"chapter": num, "issues": issues})
    if verdict == "pass" or not flagged:
        return {"verdict": verdict, "notes": notes, "chapters": flagged}

    logger.info("Story critique flagged %d/%d chapters — revising", len(flagged), len(chapters))
    for row in flagged:
        c = by_id[row["chapter"]]
        idx = chapters.index(c)
        prev_tail = (f'The previous chapter ends with: "…{_tail_words(chapters[idx - 1]["text"])}"\n'
                     if idx > 0 else "")
        next_head = (f'The next chapter begins with: "{_head_words(chapters[idx + 1]["text"])}…"\n'
                     if idx + 1 < len(chapters) else "")
        try:
            text = call(
                _prompts.system("story_revise_chapter"),
                _prompts.user("story_revise_chapter", chapter_title=c["title"],
                              title_context=title_context, issues=row["issues"],
                              notes="; ".join(notes) or "none", prev_tail=prev_tail,
                              next_head=next_head, chapter_text=c["text"]),
                c["scenes"] * 80 + 300, f'story revise chapter {c["chapter"]}', retries=2,
            ).strip()
            if text:
                c["text"] = text
        except Exception as exc:  # noqa: BLE001 — keep the original chapter
            logger.warning("Chapter %d revision failed (%s) — keeping the draft", row["chapter"], exc)
    return {"verdict": verdict, "notes": notes, "chapters": flagged}


# ── Story redraft: retell an existing story at a new scene count ─────────────

def redraft_story(story: dict, n_scenes: int,
                  character_sheet: str | None = None,
                  avoid_hint: str | None = None,
                  scene_plan: dict | None = None) -> dict:
    """Re-plan and rewrite an existing prose story for a new scene count —
    expanding (more depth around the same beats) or contracting (keep the
    strongest beats). Returns an updated story dict (status back to "draft");
    style/music/characters are kept from the original."""
    cfg = _load_cfg()
    call = _call_fn(cfg)
    n_scenes = int(n_scenes)
    scene_plan = scene_plan or story.get("scene_plan")
    old_chapters = [c for c in (story.get("chapters") or [])
                    if isinstance(c, dict) and str(c.get("text") or "").strip()]
    if not old_chapters or n_scenes < 1:
        raise RuntimeError("Story has no chapters to redraft")
    title = str(story.get("topic") or "")
    video_title = (story.get("video_title") or "").strip() or None
    old_n_scenes = int(story.get("n_scenes") or 0) or sum(
        int(c.get("scenes") or 1) for c in old_chapters)
    n_chapters = max(1, math.ceil(n_scenes / _SCENES_PER_CHAPTER))
    avoid_note = _avoid_note(avoid_hint)
    identified = _norm_identified_characters(story.get("characters"))
    character_note = _merge_character_note(_character_note(character_sheet), identified)
    story_str = "\n\n".join(
        f'Chapter {c["chapter"]} — "{c.get("title") or ""}":\n{c["text"]}'
        for c in old_chapters
    )

    # ── New outline: retell the same story across the new chapter budget ──────
    raw = call(
        _prompts.system("story_redraft_outline", n_chapters=n_chapters, n_scenes=n_scenes),
        _prompts.user("story_redraft_outline", title_line=_title_line(title, video_title),
                      n_scenes=n_scenes, n_chapters=n_chapters, old_n_scenes=old_n_scenes,
                      avoid_note=avoid_note, character_note=character_note,
                      story_str=story_str),
        1500 + 150 * n_chapters, "story redraft outline",
    )
    outer = _parse_claude_response(raw, "story redraft outline")
    raw_chapters = [c for c in (outer.get("chapters") or []) if isinstance(c, dict)]
    if not raw_chapters:
        raise RuntimeError("Story redraft outline returned no chapters")
    budgets = _pro_rate_budgets([c.get("scenes") for c in raw_chapters], n_scenes)
    outline = [{"chapter": i + 1,
                "title": str(c.get("title") or f"Chapter {i + 1}").strip(),
                "summary": str(c.get("summary") or "").strip(),
                "scenes": budgets[i]}
               for i, c in enumerate(raw_chapters[:len(budgets)])]

    # ── Rewrite each chapter at its new length, from the source story ─────────
    title_context = f'"{video_title}"' if video_title else f'"{title}"'
    outline_str = "\n".join(
        f'Chapter {o["chapter"]} — "{o["title"]}" ({o["scenes"]} scenes): {o["summary"]}'
        for o in outline
    )
    chapters: list[dict] = []
    words_per_scene = _words_per_scene(scene_plan)
    for o in outline:
        target_words = o["scenes"] * words_per_scene
        prev_tail = (
            f'The previous chapter ends with: "…{_tail_words(chapters[-1]["text"])}"\n'
            if chapters else ""
        )
        text = call(
            _prompts.system("story_redraft_chapter", target_words=target_words),
            _prompts.user("story_redraft_chapter", title_context=title_context,
                          outline_str=outline_str, chapter_title=o["title"],
                          chapter_summary=o["summary"], target_words=target_words,
                          prev_tail=prev_tail, avoid_note=avoid_note,
                          character_note=character_note, story_str=story_str),
            o["scenes"] * (words_per_scene * 2 + 30) + 300, f'story redraft chapter {o["chapter"]}',
        ).strip()
        if not text:
            raise RuntimeError(f'Story redraft chapter {o["chapter"]} came back empty')
        chapters.append({**o, "text": text})

    critique = _critique_and_revise(call, title_context, chapters, avoid_note, character_note)
    return {**story, "n_scenes": n_scenes, "scene_plan": scene_plan, "outline": outline,
            "chapters": chapters, "critique": critique, "status": "draft",
            "updated_at": time.time()}


# ── Scene division ────────────────────────────────────────────────────────────

def divide_story(story: dict, n_scenes: int | None = None,
                 style_hint: str | None = None,
                 video_title: str | None = None,
                 video_style_hint: str | None = None,
                 character_sheet: str | None = None,
                 avoid_hint: str | None = None,
                 language: str | None = None,
                 scene_plan: dict | None = None,
                 dialogue_note: str | None = None) -> tuple[list[Scene], str, str, list[dict]]:
    """Divide an approved story into scenes. Same return contract as
    ``pipeline.llm.generate_script``: (scenes, music_description, style,
    characters) — everything downstream of script generation is unchanged.
    *scene_plan* (default: the one stored on the story) sets each scene's
    10–15 s word caps in the divide prompt and drives the split-at-natural-
    pause backstop for over-long narrations.

    *dialogue_note* asks for ACTED scenes: the story stays the source, but its
    beats are staged as characters speaking on camera instead of (or as well
    as) narrated. Those scenes come back mode "dialogue" with their lines
    already assembled into an H3 prompt."""
    cfg = _load_cfg()
    call = _call_fn(cfg)
    scene_plan = scene_plan or story.get("scene_plan")
    title = str(story.get("topic") or "")
    video_title = video_title if video_title is not None else (story.get("video_title") or None)
    n = int(n_scenes or story.get("n_scenes") or 0)
    chapters = [c for c in (story.get("chapters") or [])
                if isinstance(c, dict) and str(c.get("text") or "").strip()]
    if not chapters or n < 1:
        raise RuntimeError("Story has no chapters to divide")
    budgets = _pro_rate_budgets([c.get("scenes") for c in chapters], n)

    identified = _norm_identified_characters(story.get("characters"))
    video_style_note = _video_style_note(video_style_hint)
    avoid_note = _avoid_note(avoid_hint)
    character_note = _merge_character_note(_character_note(character_sheet), identified)
    language_note = _language_note(language)
    topic_ref = f'"{video_title}"' if video_title and video_title.strip() else f'"{title}"'

    scenes: list[Scene] = []
    batch_start = 1
    for chapter, budget in zip(chapters, budgets):
        batch_end = batch_start + budget - 1
        conclusion_note = (
            f"\nIMPORTANT: Scene {batch_end} is the FINAL scene — end the video with a "
            f"clear, simple conclusion: wrap up the story in plain language, resolve the "
            f"hook, and make it unmistakable that the story is complete. No cliffhangers, "
            f"no new questions, no cryptic closing lines."
            if batch_end == n else ""
        )
        ctx_str = "\n".join(
            f'  Scene {s.id}: "{s.title}" — {s.narration}' for s in scenes[-2:]
        ) or "  (none — this chapter opens the video)"
        scenes.extend(_divide_chunk(
            call, title, chapter.get("text", ""), batch_start, batch_end,
            n_scenes=n, topic_ref=topic_ref, ctx_str=ctx_str,
            video_style_note=video_style_note, avoid_note=avoid_note,
            character_note=character_note, language_note=language_note,
            conclusion_note=conclusion_note, scene_plan=scene_plan,
            dialogue_note=dialogue_note, style_hint=style_hint,
        ))
        batch_start = batch_end + 1

    final_scenes = _split_overloaded_acted(
        scenes[:n], chained=bool((scene_plan or {}).get("chained_acted")))
    # Narrated scenes only: a silent scene is meant to be empty, and an acted
    # scene's "narration" is what its characters say (filled at assembly).
    narrated = [s for s in final_scenes if s.mode in ("narration", "", None) and not s.lines]
    _fill_empty_narrations(call, narrated, title, video_title, language=language,
                           scene_plan=scene_plan)
    # Absolute last-resort safety net: no narrated Scene leaves empty.
    for s in narrated:
        if not (s.narration or "").strip():
            s.narration = f"{s.title or f'Scene {s.id}'}."
            logger.warning("Scene %d still empty after divide fill — used title", s.id)
    # 10–15 s scene contract, in order: condense over-cap narrations down to
    # the cap (scene count and video length stay as planned), split whatever
    # still overflows, then give split pieces their own visuals — all BEFORE
    # character detection, since splitting renumbers scene ids.
    condense_long_narrations(call, final_scenes, scene_plan, language=language)
    final_scenes = enforce_scene_word_caps(final_scenes, scene_plan)
    style = style_hint.strip() if style_hint and style_hint.strip() else str(story.get("style") or "")
    regen_split_scene_visuals(call, final_scenes, title, style,
                              video_style_hint=video_style_hint,
                              character_sheet=character_note)
    identified = _detect_recurring_characters(call, final_scenes, identified,
                                              style_hint=style)
    music = str(story.get("music") or "cinematic orchestral background music, atmospheric, instrumental")
    return final_scenes, music, style, identified


def _split_overloaded_acted(scenes: list[Scene], chained: bool = False) -> list[Scene]:
    """Split any acted scene whose dialogue cannot fit one clip, and renumber.

    The video model truncates past ~15 s, so a scene the LLM overfilled is cut
    at a speaker turn into consecutive scenes instead — more short scenes beat
    one that stops mid-sentence. *chained* (h3_chain_scenes) doubles the room
    before a split kicks in: the renderer shoots those scenes as two joined
    clips, so dialogue that used to become two scenes stays one take."""
    from pipeline import performance as _perf

    out: list[Scene] = []
    for scene in scenes:
        if not (_perf.is_performance(scene) and scene.lines):
            out.append(scene)
            continue
        raw = {**_perf.scene_meta(scene), "title": scene.title, "lines": scene.lines}
        pieces = _perf.split_overloaded(raw, chained=chained)
        if len(pieces) == 1:
            out.append(scene)
            continue
        logger.info("Scene %d: dialogue needs more than one clip — split into %d",
                    scene.id, len(pieces))
        for piece in pieces:
            out.append(_perf.scene_from_raw(len(out) + 1, piece,
                                            style_note=scene.metadata_extra.get("style_note", "")))
    for i, scene in enumerate(out, 1):
        scene.id = i
    return out


def _divide_chunk(call, title: str, text: str, start: int, end: int, *,
                  n_scenes: int, topic_ref: str, ctx_str: str,
                  video_style_note: str, avoid_note: str, character_note: str,
                  language_note: str, conclusion_note: str,
                  scene_plan: dict | None = None,
                  dialogue_note: str | None = None,
                  style_hint: str | None = None) -> list[Scene]:
    """One story→scenes JSON call for scenes start..end. On a parse failure the
    chunk is halved and retried (the _translate_batch defense); a single scene
    that still fails becomes a stub the narration-fill pass completes. Always
    returns exactly end-start+1 scenes with positional ids."""
    count = end - start + 1
    try:
        raw = call(
            _prompts.system("story_divide", **_scene_len_vars(scene_plan)),
            _prompts.user("story_divide", n_scenes=n_scenes, topic_ref=topic_ref,
                          topic_full=title,
                          batch_start=start, batch_end=end, chapter_text=text,
                          ctx_str=ctx_str, video_style_note=video_style_note,
                          avoid_note=avoid_note, character_note=character_note,
                          language_note=language_note, conclusion_note=conclusion_note,
                          dialogue_note=("\n" + dialogue_note.strip()
                                         if dialogue_note and dialogue_note.strip() else "")),
            count * 600 + 400, f"divide scenes {start}–{end}", retries=2,
        )
        items = _parse_claude_response(raw, f"divide scenes {start}–{end}")
        if isinstance(items, dict):
            items = items.get("scenes", [])
        if not isinstance(items, list):
            raise RuntimeError(f"divide scenes {start}–{end}: expected a JSON array")
    except Exception as exc:  # noqa: BLE001 — halve and retry, then stub
        if count > 1:
            mid = (start + end) // 2
            first, second = _split_text(text)
            logger.warning("Divide %d–%d failed (%s) — splitting the chapter and retrying",
                           start, end, exc)
            kw = dict(n_scenes=n_scenes, topic_ref=topic_ref, ctx_str=ctx_str,
                      video_style_note=video_style_note, avoid_note=avoid_note,
                      character_note=character_note, language_note=language_note,
                      scene_plan=scene_plan, dialogue_note=dialogue_note,
                      style_hint=style_hint)
            return (_divide_chunk(call, title, first, start, mid, conclusion_note="", **kw)
                    + _divide_chunk(call, title, second, mid + 1, end,
                                    conclusion_note=conclusion_note, **kw))
        logger.warning("Divide scene %d failed (%s) — stubbing for the fill pass", start, exc)
        items = []
    # Positional ids, truncate over-delivery, stub under-delivery.
    out = []
    for i in range(count):
        item = items[i] if i < len(items) and isinstance(items[i], dict) else {}
        out.append(_scene_from_item(start + i, item, title, style_hint))
    return out


def _scene_from_item(scene_id: int, item: dict, title: str,
                     style_hint: str | None) -> Scene:
    """One divide-prompt object → a Scene, in whichever mode it came back as.

    An acted scene is assembled here rather than left as loose fields, so the
    editor shows the same H3 prompt the renderer will send (pipeline/
    performance.py owns that assembly; this is its only other caller)."""
    from pipeline import performance as _perf

    mode = str(item.get("mode") or "narration").strip().lower()
    if _perf.is_performance_mode(mode) and item.get("lines"):
        return _perf.scene_from_raw(scene_id, item, style_note=style_hint or "")
    if mode == "silent":
        # cast/setting/camera/soundscape are kept when the writer supplied them:
        # a style that acts its silent scenes (h3_silent_scenes) performs this
        # beat from those characters' portraits, and the fields are what the H3
        # prompt is assembled from. Absent, the scene renders as it always did.
        extra = {k: item[k] for k in ("setting", "camera", "soundscape", "beats")
                 if item.get(k)}
        cast = item.get("cast")
        if cast:
            # One name comes back as a bare string often enough to matter — and
            # list("Ada") is three "characters" nobody has a portrait for.
            extra["cast"] = [cast] if isinstance(cast, str) else list(cast)
        return Scene(
            id=scene_id,
            title=item.get("title", f"Scene {scene_id}"),
            image_prompt=item.get("image_prompt", title),
            video_prompt=item.get("video_prompt", item.get("image_prompt", title)),
            narration="",
            mode="silent",
            duration=float(item.get("seconds") or 0) or _SILENT_SECONDS,
            metadata_extra={**extra, "mode": "silent"},
        )
    return Scene(
        id=scene_id,
        title=item.get("title", f"Scene {scene_id}"),
        image_prompt=item.get("image_prompt", title),
        video_prompt=item.get("video_prompt", item.get("image_prompt", title)),
        narration=item.get("narration", ""),
    )


# ── Song films (music videos) ────────────────────────────────────────────────

def _song_seconds(target_seconds: float) -> int:
    return max(15, int(target_seconds or 0) or 180)


def _song_word_budget(seconds: int) -> int:
    """~1 sung word per second: singing is SLOW — intros, held notes and the
    music breathing eat most of the clock. The old 1.7 w/s with a 40-word floor
    overfilled every short song (a 15 s track was asked to carry 40 words and
    came out either rushed or cut off mid-line)."""
    return max(10, int(seconds * 1.2))


def write_song(story: dict | None, target_seconds: float,
               language: str | None = None, *,
               topic: str = "", video_title: str = "",
               source_text: str = "", music_hint: str = "",
               instruction: str = "") -> dict:
    """The film's SONG, written from the approved story draft.

    A song film ("song" format) is a music video: the music engine sings the
    whole soundtrack while the cast performs it on camera. This is where the
    soundtrack is authored — returns ``{"caption": ..., "lyrics": ...}``:
    tagged lyrics ([Verse]/[Chorus]/…) both music engines take verbatim, and a
    structured caption (genre, tempo, mood, arrangement) that replaces the
    instrumental ``music`` description. The caption deliberately leaves the
    vocalist out — the render appends a description of the cast singer's
    library voice (gender/age/tone), so the sung voice fits the character
    the film shows singing.

    The lyric budget comes from *target_seconds*: sung delivery runs well
    under two words a second, and over-length lyrics are what make the model
    rush or cut the song off mid-phrase.

    *instruction* is the Song tab's "tell it how" steering on a re-write
    ("simpler words", "make the chorus land harder"): it outranks the prompt's
    own guidance. Empty leaves the prompt byte-identical.
    """
    cfg = _load_cfg()
    call = _call_fn(cfg)
    if story is not None:
        # Song written from an approved story (the divide-time fallback).
        source_text = "\n\n".join(
            f'Chapter {c.get("chapter")} — "{c.get("title", "")}":\n{c.get("text", "")}'
            for c in (story.get("chapters") or []) if isinstance(c, dict))
        topic = str(story.get("topic") or "")
        video_title = str(story.get("video_title") or "")
        music_hint = str(story.get("music") or "")
    # Song-FIRST (the interactive Music-video flow): only the brief exists,
    # so the song is written straight from it and the story follows the
    # lyrics afterwards.
    seconds = _song_seconds(target_seconds)
    word_budget = _song_word_budget(seconds)
    lang_name = narration_language_name(language)
    language_note = (f"\nSONG LANGUAGE — write the lyrics in {lang_name}; the "
                     f"section tags and the caption stay in English."
                     if lang_name else "")
    user = _prompts.user("song_write",
                         title_line=_title_line(topic, video_title or None),
                         story_text=source_text or topic,
                         duration_seconds=seconds,
                         word_budget=word_budget,
                         music_hint=music_hint,
                         language_note=language_note)
    if (instruction or "").strip():
        user += ("\n\nAdditional instruction from the user — follow it, overriding "
                 f"the guidance above where they conflict: {instruction.strip()[:500]}")
    raw = call(
        _prompts.system("song_write"), user,
        word_budget * 3 + 500, "song write", retries=2,
    )
    data = _parse_claude_response(raw, "song write")
    lyrics = str(data.get("lyrics") or "").strip()
    if not lyrics:
        raise RuntimeError("Song writing returned no lyrics")
    caption = str(data.get("caption") or music_hint or "").strip()
    return {"caption": caption, "lyrics": lyrics}


def critique_song(song: dict, target_seconds: float, *,
                  topic: str = "", video_title: str = "",
                  source_text: str = "") -> str:
    """Judge a written song and return a REWRITE INSTRUCTION for it — the empty
    string when it is good enough to render as it stands.

    Automation's song QC (``youtube_auto_song_critic_passes``): a song is
    expensive to change once its track has been rendered on a worker, so the
    lyrics are judged first — length against the clock above all, then
    singability, hook, subject and structure. The instruction feeds straight
    back into :func:`write_song` as its *instruction*, so the songwriter prompt
    stays the one place lyrics are written.

    Best-effort like every other judge here: any failure returns "" and the
    song is kept as drafted rather than blocking the film."""
    cfg = _load_cfg()
    call = _call_fn(cfg)
    seconds = _song_seconds(target_seconds)
    try:
        raw = call(
            _prompts.system("song_critique"),
            _prompts.user("song_critique",
                          title_line=_title_line(topic, video_title or None),
                          story_text=source_text or topic,
                          duration_seconds=seconds,
                          word_budget=_song_word_budget(seconds),
                          caption=str(song.get("caption") or ""),
                          lyrics=str(song.get("lyrics") or "")),
            600, "song critique", retries=2,
        )
        data = _parse_claude_response(raw, "song critique")
    except Exception as exc:  # noqa: BLE001 — best-effort judge
        logger.warning("Song critique failed (%s) — keeping the song as-is", exc)
        return ""
    if data.get("verdict") != "revise":
        return ""
    return str(data.get("issues") or "").strip()


def lyric_lines(lyrics: str) -> list[str]:
    """The sung lines of a tagged lyric sheet, in order — section tags
    ([Verse], [Chorus], …) and blank lines dropped."""
    out = []
    for line in (lyrics or "").splitlines():
        line = line.strip()
        if line and not (line.startswith("[") and line.endswith("]")):
            out.append(line)
    return out


def assign_song_slices(scenes: list[Scene], lyrics: str,
                       total_seconds: float | None = None,
                       track: Path | str | None = None,
                       align_lyrics: bool = False,
                       language: str = "") -> list[Scene]:
    """Give each singing scene its WINDOW of the song and the words sung in it.

    The song plays once across the whole film, so each singing scene covers the
    stretch of it that its screen time occupies: ``song_window`` is [start, end]
    seconds into the track. Both ride the scene metadata: the window is what the
    audio-conditioned H3 render pins in (that slice of the track goes in, a take
    that performs it comes out), and ``sings`` is what the prompt directs the
    cast to mouth. Non-singing scenes are left alone but still advance the clock
    — the song keeps playing under them.

    When the generated *track* is on disk its singing is MEASURED
    (pipeline.song_timing) and the lyrics are placed on the vocal timeline, so
    a scene is told only the words its own slice actually contains and carries
    ``vocal_ranges`` — when inside the clip a voice is heard, relative to the
    clip's own start. Without that, an instrumental intro drags every line out
    of position: a 7.5 s intro had scene 1 mouthing a verse to silence while
    the whole film ran a scene ahead of its own song. Detection failing falls
    back to the old proportional split rather than guessing.

    *align_lyrics* (the song_align_lyrics option) upgrades the line times from
    even pacing to MEASUREMENT: the lyric sheet is whisper-aligned against the
    track's vocal stem (pipeline.lyric_align, *language* hints the model), and
    any alignment failure keeps the paced estimate."""
    from pipeline import song_timing as _song_timing
    from pipeline.performance import (MAX_SCENE_SECONDS, MIN_SCENE_SECONDS,
                                      SCENE_SECONDS)
    singing = [s for s in scenes
               if (getattr(s, "metadata_extra", None) or {}).get("singing")]
    if not singing or not (lyrics or "").strip():
        return scenes
    def secs(s):
        try:
            return float(getattr(s, "duration", 0) or 0) or SCENE_SECONDS
        except (TypeError, ValueError):
            return SCENE_SECONDS
    lines = lyric_lines(lyrics)
    regions = _song_timing.measure_regions(track) if track else []
    spans: list[tuple[float, float]] = []
    # What the prompt calls singing. The level split alone counts separation
    # bleed over an intro or an outro as a voice, and the scene it lands on is
    # told to mime words the sheet has none of; the transcription below throws
    # those regions out. Without it (no whisper install) the split stands.
    sung = regions
    if regions and align_lyrics:
        # The option (song_align_lyrics): whisper-align the KNOWN lyric sheet
        # against the cached vocal stem so each line's time is measured, not
        # paced. Any failure keeps the estimate below — never worse than off.
        from pipeline import lyric_align
        stem = _song_timing.vocal_stem(Path(track))
        if stem is not None:
            words = lyric_align.word_times(stem, language)
            sung = lyric_align.voiced_regions(regions, words)
            spans = lyric_align.align_lines(stem, lines, language=language,
                                            regions=regions, words=words) or []
    if not spans:
        spans = _song_timing.line_times(regions, len(lines)) if regions else []

    def stamp(extra: dict, t0: float, t1: float, lo: int, hi: int) -> None:
        """What is sung in [t0, t1) — measured off the track when we can,
        and the proportional guess (*lo*:*hi*) when we cannot."""
        if spans:
            extra["sings"] = "\n".join(
                _song_timing.lines_in_window(lines, spans, t0, t1)).strip()
            extra["vocal_ranges"] = _song_timing.window_vocals(sung, t0, t1)
        else:
            extra["sings"] = "\n".join(lines[lo:hi]).strip()

    all_singing = len(singing) == len(scenes)
    if all_singing and total_seconds:
        # The whole film sings (the Music-video contract): the track divides
        # into n windows meeting exactly — so the concatenated takes run
        # precisely the track's length and the overlaid song never drifts
        # against the pictures. With the lyrics on the vocal timeline the
        # seams SNAP to the gaps between lines (song_timing.snap_cuts), so no
        # take starts or ends mid-sentence; unmeasured, the split stays even.
        # The takes' minimum length is respected by the clip-count planner
        # upstream and by the snap bounds alike.
        total = float(total_seconds)
        per = total / len(scenes)
        cuts = (_song_timing.snap_cuts(len(scenes), total, spans, regions,
                                       min_secs=MIN_SCENE_SECONDS,
                                       max_secs=MAX_SCENE_SECONDS)
                or [_song_timing.frame_snap(i * per) for i in range(len(scenes))]
                + [_song_timing.frame_snap(total, down=True)])
        for i, s in enumerate(scenes):
            t0, t1 = cuts[i], cuts[i + 1]
            extra = dict(getattr(s, "metadata_extra", None) or {})
            extra["song_window"] = [t0, t1]
            stamp(extra, t0, t1,
                  int(round(len(lines) * i / len(scenes))),
                  int(round(len(lines) * (i + 1) / len(scenes))))
            s.metadata_extra = extra
            s.duration = round(t1 - t0, 2)
        return scenes
    # Mixed film: the song plays across singing and non-singing scenes alike,
    # so windows follow each scene's authored screen time, scaled onto the
    # track. Singing scenes are resized to their windows (floored at the
    # acted minimum) so the film tracks the song as closely as its mix allows.
    film_len = sum(secs(s) for s in scenes)
    scale = (float(total_seconds) / film_len
             if total_seconds and film_len > 0 else 1.0)
    clock = 0.0
    for s in scenes:
        start, end = clock, clock + secs(s)
        clock = end
        extra = dict(getattr(s, "metadata_extra", None) or {})
        if not extra.get("singing"):
            continue
        lo = int(round(len(lines) * (start / film_len))) if film_len else 0
        hi = int(round(len(lines) * (end / film_len))) if film_len else 0
        extra["song_window"] = [_song_timing.frame_snap(start * scale),
                                _song_timing.frame_snap(end * scale)]
        stamp(extra, extra["song_window"][0], extra["song_window"][1], lo, hi)
        s.metadata_extra = extra
        s.duration = max(MIN_SCENE_SECONDS,
                         round(extra["song_window"][1] - extra["song_window"][0], 1))
    return scenes


def mark_singing(scenes: list[Scene]) -> list[Scene]:
    """Stamp a song film's performance flag onto its silent scenes, in place.

    The flag rides each scene's metadata sidecar (never a style toggle), so it
    survives script.json → editor → every re-render, and renders_acted routes
    the scene onto H3 with the singing prompt whatever the style's
    ``h3_silent_scenes`` says. Narrated/dialogue scenes are left alone — a song
    film's stray spoken beat still behaves as authored."""
    for s in scenes:
        if str(getattr(s, "mode", "") or "").strip().lower() == "silent":
            extra = dict(getattr(s, "metadata_extra", None) or {})
            extra["singing"] = True
            s.metadata_extra = extra
    return scenes


# ── Script critic (post-generation QC over the assembled scene list) ─────────

def near_duplicate_pairs(scenes: list[dict], threshold: float = 0.8,
                         cap: int = 5) -> list[tuple[int, int, float]]:
    """Mechanical near-duplicate detection over scene narrations: (id_a, id_b,
    similarity) for every pair whose text similarity ≥ threshold, strongest
    first. A deterministic backstop for the critic — the LLM judge can miss an
    obvious duplicate (notably one involving the protected final scene), so
    detected pairs are fed into its prompt explicitly."""
    from difflib import SequenceMatcher
    rows = [(int(s["id"]), " ".join(str(s.get("narration") or "").lower().split()))
            for s in scenes]
    pairs = []
    for i in range(len(rows)):
        if not rows[i][1]:
            continue
        for j in range(i + 1, len(rows)):
            if not rows[j][1]:
                continue
            ratio = SequenceMatcher(None, rows[i][1], rows[j][1]).ratio()
            if ratio >= threshold:
                pairs.append((rows[i][0], rows[j][0], round(ratio, 2)))
    return sorted(pairs, key=lambda p: -p[2])[:cap]


def critique_scenes(scenes: list[dict], title: str,
                    video_title: str | None = None,
                    avoid_hint: str | None = None,
                    pass_num: int = 1,
                    direction: str = "",
                    dup_note: str = "",
                    scene_plan: dict | None = None) -> dict:
    """One critic pass over an assembled script (classic or story-first): judge
    consistency, repetition, engagement, and instruction adherence (the
    guardrail — narration AND visual prompts must obey the commissioned
    direction), and propose edits. Returns validated ops — {"changed", "notes",
    "rewrites", "deletes", "inserts", "order"} — which the caller applies; run
    repeatedly until "changed" is false to converge. *scenes* rows need
    id/title/narration (+ image_prompt/video_prompt for the guardrail check).
    *pass_num* tells the critic it is re-reviewing its own output, biasing
    later passes toward convergence instead of endless polishing."""
    cfg = _load_cfg()
    n = len(scenes)
    scene_list = "\n".join(
        f'Scene {s["id"]}: "{s.get("title") or ""}" — {s.get("narration") or ""}\n'
        f'  IMAGE: {s.get("image_prompt") or ""}\n'
        f'  VIDEO: {s.get("video_prompt") or ""}'
        for s in scenes
    )
    topic_ref = f'"{video_title}"' if video_title and video_title.strip() else f'"{title}"'
    pass_note = (
        f"\nThis is critic pass {pass_num} on this script — earlier passes already applied "
        "their edits. Only flag defects that GENUINELY remain; if the script is now "
        'acceptable, return {"changed": false}.'
        if pass_num > 1 else ""
    )
    direction_note = (
        f"\nTOPIC/DIRECTION (the script must follow this): {direction.strip()}"
        if direction and direction.strip() else ""
    )
    raw = _chat_complete(
        cfg,
        _prompts.system("script_critic", **_scene_len_vars(scene_plan)),
        _prompts.user("script_critic", topic_ref=topic_ref, n_scenes=n,
                      scene_list=scene_list, avoid_note=_avoid_note(avoid_hint),
                      pass_note=pass_note, direction_note=direction_note,
                      dup_note=dup_note),
        min(n * 150 + 800, 16000), "script critic", retries=2,
    )
    data = _parse_claude_response(raw, "script critic")

    ops = {"changed": bool(data.get("changed")),
           "notes": [str(x) for x in (data.get("notes") or []) if str(x).strip()][:5],
           "rewrites": [], "deletes": [], "inserts": [], "order": None}
    for ins in (data.get("inserts") or []):
        if not isinstance(ins, dict):
            continue
        try:
            after = int(ins.get("after"))
        except (TypeError, ValueError):
            continue
        narration = str(ins.get("narration") or "").strip()
        if after < 0 or not narration:
            continue
        ops["inserts"].append({
            "after": after,
            "title": str(ins.get("title") or "").strip() or "New scene",
            "narration": narration,
            "image_prompt": str(ins.get("image_prompt") or "").strip(),
            "video_prompt": str(ins.get("video_prompt") or "").strip(),
        })
    for r in (data.get("rewrites") or []):
        if not isinstance(r, dict):
            continue
        try:
            sid = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        # Per-field rewrite: any of narration/title/image_prompt/video_prompt;
        # rows carrying none of them are dropped.
        row = {"id": sid}
        for field in ("narration", "title", "image_prompt", "video_prompt"):
            value = str(r.get(field) or "").strip()
            if value:
                row[field] = value
        if len(row) > 1:
            ops["rewrites"].append(row)
    for d in (data.get("deletes") or []):
        try:
            ops["deletes"].append(int(d))
        except (TypeError, ValueError):
            continue
    order = data.get("order")
    if isinstance(order, list) and order:
        try:
            ops["order"] = [int(i) for i in order]
        except (TypeError, ValueError):
            ops["order"] = None
    if not (ops["rewrites"] or ops["deletes"] or ops["inserts"] or ops["order"]):
        ops["changed"] = False
    return ops


def _split_text(text: str) -> tuple[str, str]:
    """Split prose roughly in half at a sentence boundary (for halve-and-retry)."""
    sentences = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    if len(sentences) < 2:
        mid = max(1, len(text) // 2)
        return text[:mid], text[mid:]
    total = sum(len(s) for s in sentences)
    acc, first = 0, []
    for s in sentences:
        if acc >= total / 2 and first:
            break
        first.append(s)
        acc += len(s)
    return " ".join(first), " ".join(sentences[len(first):])
