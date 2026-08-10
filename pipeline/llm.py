"""Script generation — supports Claude, Grok (xAI), OpenAI, and local vLLM.

Backend is selected via config:
  llm_backend: "claude" | "grok" | "openai" | "local"   (default: "local")
  claude_api_key / claude_model               (when backend is "claude")
  grok_api_key / grok_model                   (when backend is "grok"; key also
                                              from env XAI_API_KEY)
  openai_api_key / openai_model               (when backend is "openai"; key also
                                              from env OPENAI_API_KEY)
  local_llm_url / local_llm_model             (when backend is "local")

Claude / Grok / OpenAI: JSON batch protocol, reliable structured output.
Local:                  two-stage plain-text (story + per-scene visuals), works
                        around the reasoning-model token constraints.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("video_gen")

# ── Local vLLM defaults (overridden by config: local_llm_url / local_llm_model) ─
from pipeline import prompts as _prompts

_LOCAL_LLM_URL_DEFAULT   = "http://localhost:8000/v1/chat/completions"
_LOCAL_LLM_MODEL_DEFAULT = "openai/gpt-oss-120b"

# xAI Grok — OpenAI-compatible Chat Completions.
_GROK_CHAT_URL_DEFAULT = "https://api.x.ai/v1/chat/completions"
_GROK_MODEL_DEFAULT = "grok-4.5"

# OpenAI ChatGPT — Chat Completions API.
_OPENAI_CHAT_URL_DEFAULT = "https://api.openai.com/v1/chat/completions"
_OPENAI_MODEL_DEFAULT = "gpt-4o"

NEGATIVE_PROMPT = _prompts.value("video_negative")


# ── Config loader (avoids circular import with app.py) ────────────────────────
_CONFIG_FILE = Path.home() / ".config" / "video-generator" / "config.yaml"

def _load_cfg() -> dict:
    try:
        return yaml.safe_load(_CONFIG_FILE.read_text()) or {}
    except Exception:
        return {}


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class Scene:
    id: int
    title: str
    image_prompt: str   # FLUX: highly detailed static description, no motion words
    video_prompt: str   # LTX I2V: motion, camera movement, action, pacing
    narration: str
    negative_prompt: str = NEGATIVE_PROMPT
    # ── Dialogue/performance (additive; absent/"narration" ⟹ today's behavior) ──
    # mode: "narration" (default) | "silent" | "dialogue". A scene with narration
    # text and no explicit mode is narration — existing scripts are unchanged.
    mode: str = "narration"
    # dialogue lines: ordered [{"speaker": <character name|"Narrator">, "text": str}].
    # The whole scene renders as one acted H3 generation in the cast's voices.
    lines: list = field(default_factory=list)
    # explicit length (seconds) for silent scenes that have no audio to time from.
    duration: float = 0.0
    # Any other metadata the scene sidecar carried (e.g. per-scene voice) —
    # preserved through the `metadata` property below.
    metadata_extra: dict = field(default_factory=dict)

    @property
    def metadata(self) -> dict:
        """The scene's metadata sidecar, dialogue fields included.

        The orchestrator mirrors scenes into its store via getattr(scene,
        "metadata") — without this property every render start overwrote the
        stored metadata with {} and authored dialogue silently reverted to
        narration."""
        md = {k: v for k, v in self.metadata_extra.items()
              if k not in ("mode", "lines", "duration")}
        if self.mode not in ("narration", "", None):
            md["mode"] = self.mode
        if self.lines:
            md["lines"] = self.lines
        if self.duration:
            md["duration"] = self.duration
        return md


# ══════════════════════════════════════════════════════════════════════════════
# Claude backend
# ══════════════════════════════════════════════════════════════════════════════

_CLAUDE_BATCH_SIZE = 10  # max scenes per API call


# ── Scene length (cadence) plumbing ──────────────────────────────────────────

def _scene_len_vars(scene_plan: dict | None) -> dict:
    """Prompt placeholders (scene word caps) from a cadence plan — falling
    back to the DEFAULT_WPM plan so every prompt always renders concrete
    numbers (safe_substitute would otherwise leave literal ${…} behind)."""
    from pipeline import cadence
    return cadence.prompt_vars(scene_plan or cadence.default_plan())


def _condensable(s: "Scene") -> bool:
    """Scenes whose narration length we may rewrite/split: plain narration
    with no dialogue lines and no authored spoken-text override."""
    return (s.mode in ("narration", "", None) and not s.lines
            and not (s.metadata_extra or {}).get("tts_text")
            and bool((s.narration or "").strip()))


def condense_long_narrations(call_fn, scenes: list["Scene"], scene_plan: dict | None,
                             language: str | None = None) -> None:
    """Rewrite any over-cap narration DOWN to the plan's word cap, in place.

    The first line of defense for the 10–15 s scene contract: an over-long
    narration is condensed by the LLM to fit its scene — the scene count (and
    with it the promised video length) stays exactly as planned, and no
    visuals need duplicating. Only when a condensed narration STILL exceeds
    the cap does the splitting backstop fire. A failed/over-cap rewrite keeps
    whichever text is shorter; never raises.
    """
    from pipeline import cadence
    if not scene_plan:
        return
    max_w = int(scene_plan.get("scene_words_max") or 0)
    if max_w <= 0:
        return
    lang_name = narration_language_name(language)
    lang_note = f"\nKeep the narration in {lang_name}." if lang_name else ""
    for s in scenes:
        if not _condensable(s) or cadence.word_count(s.narration) <= max_w:
            continue
        try:
            rewritten = call_fn(
                _prompts.system("condense_narration"),
                _prompts.user("condense_narration", narration=s.narration,
                              lang_note=lang_note, **_scene_len_vars(scene_plan)),
                max_w * 3 + 80, f"condense narration scene {s.id}", retries=2,
            ).strip().strip('"').strip()
        except Exception as exc:  # noqa: BLE001 — the splitter still backstops
            logger.warning("Condense failed for scene %d (%s) — keeping original", s.id, exc)
            continue
        if rewritten and cadence.word_count(rewritten) < cadence.word_count(s.narration):
            logger.info("Scene %d condensed %d → %d words (cap %d)", s.id,
                        cadence.word_count(s.narration), cadence.word_count(rewritten), max_w)
            s.narration = rewritten


def enforce_scene_word_caps(scenes: list["Scene"], scene_plan: dict | None) -> list["Scene"]:
    """Deterministic backstop for the 10–15 s scene contract.

    Any narration-mode scene whose spoken words exceed the plan's per-scene
    cap is split at sentence ends — or at a natural pause (comma, dash) inside
    an over-long sentence — into consecutive scenes, then ids are renumbered
    1..N. Continuation pieces carry the source scene's visuals as a stand-in
    and are marked (``_split_clone``) so callers can regenerate DISTINCT
    image/video prompts for them (see regen_split_scene_visuals). With the
    condense pass running first this should be rare. Dialogue/silent scenes
    (their timing is per-line) and scenes with authored spoken-text overrides
    pass through untouched. No-op without a plan.
    """
    from pipeline import cadence
    if not scene_plan:
        return scenes
    max_w = int(scene_plan.get("scene_words_max") or 0)
    if max_w <= 0:
        return scenes
    min_w = int(scene_plan.get("scene_words_min") or 0)
    out: list[Scene] = []
    for s in scenes:
        pieces = cadence.split_narration(s.narration, max_w, min_w) if _condensable(s) else []
        if len(pieces) <= 1:
            out.append(s)
            continue
        logger.info("Scene %d narration over the %d-word cap — split into %d scenes",
                    s.id, max_w, len(pieces))
        for j, piece in enumerate(pieces):
            clone = Scene(
                id=s.id, title=s.title,
                image_prompt=s.image_prompt, video_prompt=s.video_prompt,
                narration=piece, negative_prompt=s.negative_prompt,
                mode=s.mode, lines=[], duration=s.duration,
                metadata_extra=dict(s.metadata_extra) if j == 0 else {},
            )
            if j > 0:
                # Transient marker (not a dataclass field → never serialized):
                # this piece needs its own visuals, not a copy of the source's.
                clone._split_clone = True
            out.append(clone)
    for i, s in enumerate(out, 1):
        s.id = i
    return out


def regen_split_scene_visuals(call_fn, scenes: list["Scene"], title: str, style: str,
                              video_style_hint: str | None = None,
                              character_sheet: str | None = None) -> None:
    """Write DISTINCT image/video prompts for scenes created by the splitting
    backstop, in place — every scene of a split sentence should show its own
    moment, not a duplicate of the source scene's visuals. Reuses the
    script_local_visual prompt (plain IMAGE:/VIDEO: lines, backend-agnostic).
    Best-effort: a failed call keeps the inherited stand-in visuals.
    """
    video_style_note = (
        f"\nMOTION DIRECTION for the VIDEO line: {video_style_hint.strip()}"
        if video_style_hint and video_style_hint.strip() else ""
    )
    character_note = (
        f"\n{character_sheet.strip()}"
        if character_sheet and character_sheet.strip() else ""
    )
    for s in scenes:
        if not getattr(s, "_split_clone", False):
            continue
        try:
            raw = call_fn(
                _prompts.system("script_local_visual"),
                _prompts.user("script_local_visual", title=title, style=style,
                              scene_title=s.title, narration=s.narration,
                              video_style_note=video_style_note,
                              character_note=character_note),
                400, f"visuals for split scene {s.id}", retries=2,
            )
            image_prompt = _get_field(raw, "IMAGE")
            video_prompt = _get_field(raw, "VIDEO")
            if image_prompt:
                s.image_prompt = image_prompt
                s.video_prompt = video_prompt or image_prompt
                logger.info("Split scene %d got its own visuals", s.id)
        except Exception as exc:  # noqa: BLE001 — inherited visuals remain
            logger.warning("Visuals for split scene %d failed (%s) — keeping the "
                           "source scene's", s.id, exc)

# The batch-1 identify names at most one or two CENTRAL figures (it only sees the
# first ~10 scenes). The post-assembly recurring-cast pass (over the whole script)
# may surface supporting characters too, so it keeps a larger merged set.
_MAX_MAIN_CHARACTERS = 2
_MAX_RECURRING_CHARACTERS = 8


def _norm_identified_characters(raw_list, cap: int = _MAX_MAIN_CHARACTERS) -> list[dict]:
    """Coerce the LLM's identified characters into {name, aliases, description}.

    Drops entries without a name, coerces aliases to a clean string list (accepts
    a comma-separated string too), and caps the list at *cap* (the batch-1 main
    subjects use the default; the recurring-cast pass passes a larger cap). An
    empty/invalid input yields [] — meaning "this video has no recurring
    character", which leaves the rest of the pipeline unchanged."""
    out: list[dict] = []
    for raw in (raw_list if isinstance(raw_list, list) else []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        aliases = raw.get("aliases")
        if isinstance(aliases, str):
            aliases = aliases.split(",")
        aliases = [str(a).strip() for a in (aliases or []) if str(a).strip()]
        gender = str(raw.get("gender") or "").strip().lower()
        age = str(raw.get("age") or "").strip().lower()
        out.append({
            "name": name,
            "aliases": aliases,
            "description": str(raw.get("description") or "").strip(),
            # voice-casting hints (used to auto-pick a library voice; free-form
            # values are tolerated downstream, unknown ⟹ no constraint)
            "gender": gender if gender in ("male", "female") else "",
            "age": age if age in ("young", "adult", "mature", "elderly") else "",
        })
        if len(out) >= cap:
            break
    return out


def _identified_sheet(chars: list[dict]) -> str:
    """A RECURRING CHARACTERS block for characters just established for this video,
    appended to the later batches / visual prompts. The render pipeline injects
    each named character's canonical appearance and reference image into any
    image prompt that contains their name (app._inject_characters), so visual
    prompts must use the NAME — a paraphrased appearance without the name breaks
    that match and the character drifts. "" when none have a description."""
    rows = [c for c in chars if c.get("name") and c.get("description")]
    if not rows:
        return ""
    lines = "\n".join(f"- {c['name']}: {c['description']}" for c in rows)
    return (
        "RECURRING CHARACTERS — when a scene features one of these characters, refer "
        "to them BY NAME in the narration AND in that scene's image/video prompts. "
        "In the visual prompts write the NAME ONLY — never restate or paraphrase "
        "their appearance (the canonical appearance below is appended automatically "
        "to every prompt that names them; an unnamed description breaks consistency). "
        "Only describe what DIFFERS from their canonical look in that scene, such as "
        "a change of clothes or an injury:\n"
        f"{lines}"
    )


def _merge_character_note(character_note: str, identified: list[dict]) -> str:
    """Fold freshly-identified main characters into the character note that rides
    along in continuation/visual prompts, preserving any opted-in catalogue sheet."""
    extra = _identified_sheet(identified)
    if not extra:
        return character_note
    return f"{character_note}\n{extra}" if character_note else f"\n{extra}"


def _character_keys(c: dict) -> set[str]:
    """Lower-cased name + aliases of a character, for dedup across the two passes."""
    toks = [c.get("name", ""), *(c.get("aliases") or [])]
    return {t.strip().lower() for t in toks if t and str(t).strip()}


def _merge_recurring(identified: list[dict], found: list[dict]) -> list[dict]:
    """Merge the recurring-cast pass into the batch-1 list. Batch-1 entries win
    (their descriptions were folded into generation), and any detected duplicate —
    matched by name OR alias — is dropped. Capped at _MAX_RECURRING_CHARACTERS."""
    seen: set[str] = set()
    for c in identified:
        seen |= _character_keys(c)
    out = list(identified)
    for c in found:
        keys = _character_keys(c)
        if keys & seen:
            continue
        seen |= keys
        out.append(c)
        if len(out) >= _MAX_RECURRING_CHARACTERS:
            break
    return out[:_MAX_RECURRING_CHARACTERS]


def _detect_recurring_characters(call_fn, scenes: list[Scene],
                                 identified: list[dict],
                                 style_hint: str | None = None) -> list[dict]:
    """Second-pass recurring-cast detection over the FULL assembled script.

    The batch-1 identify only sees the first ~10 scenes and only names the 1-2
    CENTRAL subjects, so recurring supporting characters — and anyone introduced
    later — are missed (e.g. a general who appears in scenes 3, 7 and 12 of a
    documentary that isn't strictly "about" him). This pass shows the model every
    scene's narration and asks for each named figure appearing in 2+ scenes, then
    merges the result into the batch-1 list (which wins on conflicts).

    Characters this pass ADDS were discovered after every scene was already
    written, so their visual prompts may describe them without the name — which
    render-time injection (app._inject_characters) can't match. A follow-up
    retag pass rewrites those prompts to use the name (see
    _retag_unnamed_character_scenes).

    *call_fn(system, user_msg, max_tokens, label, retries=...)* → str (the active
    backend's caller). Best-effort: any failure leaves the batch-1 list untouched
    so a hiccup here never fails script generation."""
    lines = [f"Scene {s.id}: {(s.narration or '').strip()}"
             for s in scenes if (s.narration or "").strip()]
    if len(lines) < 2:  # nothing can recur across <2 narrated scenes
        return identified
    # Descriptions must fit the video's world (a robot style means robot
    # characters), so hand the pass the effective visual style when known.
    style_note = (f"VISUAL STYLE (all characters must belong to this world): "
                  f"{style_hint.strip()}\n\n"
                  if style_hint and style_hint.strip() else "")
    try:
        raw = call_fn(
            _prompts.system("recurring_characters"),
            _prompts.user("recurring_characters", style_note=style_note,
                          scene_list="\n".join(lines)),
            _MAX_RECURRING_CHARACTERS * 120 + 400,
            "recurring characters",
            retries=2,
        )
        data = _parse_claude_response(raw, "recurring characters")
    except Exception as exc:  # noqa: BLE001 — best-effort, keep batch-1 result
        logger.warning("Recurring-character pass failed (%s) — keeping %d identified",
                       exc, len(identified))
        return identified
    rows = data if isinstance(data, list) else data.get("characters", [])
    # Keep only characters the model placed in 2+ distinct scenes.
    multi = [r for r in (rows if isinstance(rows, list) else [])
             if isinstance(r, dict)
             and len({str(x) for x in r.get("scenes", []) if str(x).strip()}) >= 2]
    found = _norm_identified_characters(multi, cap=_MAX_RECURRING_CHARACTERS)
    merged = _merge_recurring(identified, found)
    logger.info("Recurring-character pass: %d identified + %d detected → %d total",
                len(identified), len(found), len(merged))
    # _merge_recurring appends survivors after the batch-1 entries, so the tail
    # is exactly the NEW characters — the ones whose scenes were written before
    # they existed and may depict them namelessly.
    new_chars = merged[len(identified):]
    if new_chars:
        scene_map = {(r.get("name") or "").strip().lower(): _row_scene_ids(r)
                     for r in multi}
        _retag_unnamed_character_scenes(call_fn, scenes, new_chars, scene_map)
    return merged


def _row_scene_ids(row: dict) -> set[int]:
    """Scene ids from a recurring-pass row's "scenes" list, tolerating strings
    like "Scene 3"."""
    ids: set[int] = set()
    for x in row.get("scenes", []) or []:
        m = re.search(r"\d+", str(x))
        if m:
            ids.add(int(m.group()))
    return ids


def _character_named_in(text: str, char: dict) -> bool:
    """True when the character's name or an alias appears in *text*."""
    low = (text or "").lower()
    return any(t.strip() and t.strip().lower() in low
               for t in [char.get("name") or "", *(char.get("aliases") or [])])


def _retag_unnamed_character_scenes(call_fn, scenes: list[Scene],
                                    new_chars: list[dict],
                                    scene_map: dict[str, set[int]]) -> None:
    """Rewrite visual prompts that depict a newly-detected recurring character
    without naming them, so render-time injection/reference matching fires.

    Only the scenes the recurring pass placed each NEW character in are
    candidates, and only when the character's name/alias is absent from the
    scene's image prompt — a scene that already names them is left alone, and
    the model is instructed never to add a character to a scene that doesn't
    depict them. Mutates the Scene objects in place. Best-effort: any failure
    leaves every prompt as written."""
    by_id = {s.id: s for s in scenes}
    targets: dict[int, list[dict]] = {}
    for c in new_chars:
        if not (c.get("name") and c.get("description")):
            continue
        for sid in scene_map.get((c["name"] or "").strip().lower(), set()):
            s = by_id.get(sid)
            if s is None or _character_named_in(s.image_prompt, c):
                continue
            targets.setdefault(sid, []).append(c)
    if not targets:
        return
    chars = {id(c): c for cl in targets.values() for c in cl}.values()
    character_list = "\n".join(f"- {c['name']}: {c['description']}" for c in chars)
    scene_list = "\n".join(
        f"Scene {sid}:\nIMAGE: {by_id[sid].image_prompt}\nVIDEO: {by_id[sid].video_prompt}"
        for sid in sorted(targets)
    )
    try:
        raw = call_fn(
            _prompts.system("retag_character_scenes"),
            _prompts.user("retag_character_scenes",
                          character_list=character_list, scene_list=scene_list),
            len(targets) * 400 + 300,
            "character retag",
            retries=2,
        )
        data = _parse_claude_response(raw, "character retag")
    except Exception as exc:  # noqa: BLE001 — best-effort, keep prompts as written
        logger.warning("Character retag pass failed (%s) — prompts left as written", exc)
        return
    rows = data.get("rewrites") if isinstance(data, dict) else None
    renamed = 0
    for r in (rows if isinstance(rows, list) else []):
        if not isinstance(r, dict):
            continue
        try:
            sid = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        s = by_id.get(sid)
        if s is None or sid not in targets:
            continue
        img = str(r.get("image_prompt") or "").strip()
        vid = str(r.get("video_prompt") or "").strip()
        # Accept a rewrite only if it actually names one of the scene's characters.
        if img and any(_character_named_in(img, c) for c in targets[sid]):
            s.image_prompt = img
            if vid:
                s.video_prompt = vid
            renamed += 1
    logger.info("Character retag: renamed %d of %d candidate scenes",
                renamed, len(targets))


def _parse_claude_response(content: str, label: str):
    """Strip fences, remove trailing commas, parse JSON. Raises RuntimeError on failure."""
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    content = re.sub(r",\s*([}\]])", r"\1", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude returned invalid JSON ({label}): {e}\nContent: {content[:400]}")


def _claude_call(client, model: str, system: str, user_msg: str,
                 max_tokens: int, label: str, retries: int = 6) -> str:
    """Call the Claude API using streaming to avoid long-idle-connection timeouts.

    Large responses (many scenes) can take minutes to generate.  Non-streaming
    requests leave the HTTP connection idle while Claude thinks, which causes
    some network appliances / NAT routers to drop the connection after ~3 min.
    Streaming sends token deltas as they arrive, keeping the connection alive.
    """
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            chunks: list[str] = []
            stop_reason: str | None = None
            # Use the streaming context-manager so tokens arrive incrementally.
            with client.messages.stream(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user_msg}],
            ) as stream:
                for text in stream.text_stream:
                    chunks.append(text)
                final_msg = stream.get_final_message()
                stop_reason = final_msg.stop_reason
            text = "".join(chunks).strip()
            if stop_reason == "max_tokens":
                raise RuntimeError(
                    f"Claude hit the token limit ({max_tokens}) for {label}. "
                    "Try fewer scenes or a shorter topic."
                )
            return text
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                # Exponential backoff: 10s, 20s, 40s, 60s, 60s
                delay = min(10 * (2 ** (attempt - 1)), 60)
                logger.warning("Claude API call failed (attempt %d/%d): %s — retrying in %ds",
                               attempt, retries, exc, delay)
                _time.sleep(delay)
    raise last_exc


def _openai_compatible_call(url: str, api_key: str, model: str, system: str,
                            user_msg: str, max_tokens: int, label: str,
                            retries: int = 6, timeout: int = 300) -> str:
    """Chat Completions call (OpenAI, Grok/xAI, or other OpenAI-compatible hosts).

    Used by the OpenAI and Grok backends. Retries with exponential backoff.
    """
    import time as _time
    messages = []
    if (system or "").strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_msg})
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max_tokens,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            choice = data["choices"][0]
            finish = choice.get("finish_reason")
            content = (choice.get("message") or {}).get("content") or ""
            if finish in ("length", "max_tokens"):
                raise RuntimeError(
                    f"LLM hit the token limit ({max_tokens}) for {label}. "
                    "Try fewer scenes or a shorter topic."
                )
            if not content.strip():
                raise RuntimeError(f"Empty LLM content for {label} (finish_reason={finish})")
            return content.strip()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                delay = min(10 * (2 ** (attempt - 1)), 60)
                logger.warning("OpenAI-compatible call failed (attempt %d/%d, %s): %s — retrying in %ds",
                               attempt, retries, label, exc, delay)
                _time.sleep(delay)
    raise last_exc


def _claude_api_key(cfg: dict) -> str:
    return (cfg.get("claude_api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _grok_api_key(cfg: dict) -> str:
    return (cfg.get("grok_api_key") or "").strip() or os.environ.get("XAI_API_KEY", "").strip()


def _openai_api_key(cfg: dict) -> str:
    return (cfg.get("openai_api_key") or "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()


def llm_backend_ready(cfg: dict) -> bool:
    """True when the configured LLM backend has what it needs to make a call.

    Local is assumed ready (the call fails later if the server is down). Cloud
    backends need an API key in config or the matching environment variable.
    """
    backend = cfg.get("llm_backend", "local")
    if backend == "claude":
        return bool(_claude_api_key(cfg))
    if backend == "grok":
        return bool(_grok_api_key(cfg))
    if backend == "openai":
        return bool(_openai_api_key(cfg))
    return True


def _chat_complete(cfg: dict, system: str, user_msg: str, max_tokens: int,
                   label: str, retries: int = 3) -> str:
    """One-shot chat completion against the configured LLM backend.

    Shared by suggestions, descriptions, tags, director briefs, community
    replies, and field regen. Script generation uses the same backends but
    its own batching protocol.
    """
    backend = cfg.get("llm_backend", "local")
    if backend == "claude":
        api_key = _claude_api_key(cfg)
        if not api_key:
            raise RuntimeError("No Claude API key configured (Settings → LLM backend).")
        import anthropic
        import httpx
        timeout = httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=30.0)
        client = anthropic.Anthropic(
            api_key=api_key, http_client=httpx.Client(http2=False, timeout=timeout)
        )
        return _claude_call(
            client, cfg.get("claude_model", "claude-sonnet-4-6"),
            system, user_msg, max_tokens, label, retries=retries,
        )
    if backend == "grok":
        api_key = _grok_api_key(cfg)
        if not api_key:
            raise RuntimeError("No Grok API key configured (Settings → LLM backend).")
        return _openai_compatible_call(
            cfg.get("grok_api_url") or _GROK_CHAT_URL_DEFAULT,
            api_key,
            cfg.get("grok_model") or _GROK_MODEL_DEFAULT,
            system, user_msg, max_tokens, label, retries=retries,
        )
    if backend == "openai":
        api_key = _openai_api_key(cfg)
        if not api_key:
            raise RuntimeError("No OpenAI API key configured (Settings → LLM backend).")
        return _openai_compatible_call(
            cfg.get("openai_api_url") or _OPENAI_CHAT_URL_DEFAULT,
            api_key,
            cfg.get("openai_model") or _OPENAI_MODEL_DEFAULT,
            system, user_msg, max_tokens, label, retries=retries,
        )
    url = cfg.get("local_llm_url", _LOCAL_LLM_URL_DEFAULT)
    model = cfg.get("local_llm_model", _LOCAL_LLM_MODEL_DEFAULT)
    return _local_llm(
        [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
        max_tokens=max_tokens, url=url, model=model, retries=max(1, retries - 1),
    )


def narration_language_name(code: str | None) -> str | None:
    """Human name for a narration-language code ("pt" → "Portuguese"), or None
    for English/unknown — English is the baseline, so no prompt note is needed."""
    code = (code or "").strip().lower()
    if not code or code == "en":
        return None
    from pipeline.chatterbox import LANGUAGES
    return LANGUAGES.get(code)


def _fill_empty_narrations(call_fn, scenes: list[Scene],
                           title: str, video_title: str | None,
                           language: str | None = None,
                           scene_plan: dict | None = None) -> None:
    """For any scene with empty narration, make a targeted LLM call to fill it.

    *call_fn(system, user_msg, max_tokens, label, retries=...)* → str.
    Mutates the scenes list in place.
    """
    topic = video_title or title
    lang_name = narration_language_name(language)
    empty = [s for s in scenes if not (s.narration or "").strip()]
    if not empty:
        return
    logger.warning("Scenes with empty narration after generation: %s — filling", [s.id for s in empty])
    for scene in empty:
        prev_narr = next((s.narration for s in scenes if s.id == scene.id - 1 and s.narration), "")
        next_narr = next((s.narration for s in scenes if s.id == scene.id + 1 and s.narration), "")
        ctx_parts = [f'Topic: "{topic}"', f'Scene {scene.id} title: "{scene.title}"']
        if lang_name:
            ctx_parts.append(f"Write the narration in {lang_name}.")
        if prev_narr:
            ctx_parts.append(f'Previous scene ends with: "{prev_narr}"')
        if next_narr:
            ctx_parts.append(f'Next scene begins with: "{next_narr}"')
        try:
            narration = call_fn(
                _prompts.system("script_claude_fill_narration"),
                _prompts.user("script_claude_fill_narration", ctx="\n".join(ctx_parts),
                              **_scene_len_vars(scene_plan)),
                160,
                f"fill narration scene {scene.id}",
                retries=2,
            ).strip()
            if narration:
                scene.narration = narration
                logger.info("Filled narration for scene %d: %r", scene.id, narration[:60])
        except Exception as exc:
            logger.warning("Could not fill narration for scene %d: %s", scene.id, exc)


def _norm_scene_lines(item: dict) -> list[dict]:
    """Validated shot sequence for a dialogue scene.

    A shot is either SPEAKING — {speaker, text, shot?} performed on camera
    — or SILENT — {silent: true, shot?, video_prompt?, duration} rendered as a
    motion clip (people move, no speech). Empty rows are dropped."""
    raw = item.get("lines")
    if not isinstance(raw, list):
        return []
    out = []
    for ln in raw:
        if not isinstance(ln, dict):
            continue
        text = str(ln.get("text") or "").strip()
        shot = str(ln.get("shot") or "").strip()
        if ln.get("silent") or (not text and (ln.get("duration") or ln.get("video_prompt"))):
            row = {"silent": True, "duration": _coerce_float(ln.get("duration"), 3.0)}
            if shot:
                row["shot"] = shot
            vp = str(ln.get("video_prompt") or "").strip()
            if vp:
                row["video_prompt"] = vp
            out.append(row)
            continue
        if not text:
            continue
        row = {"speaker": str(ln.get("speaker") or "Narrator").strip() or "Narrator", "text": text}
        if shot:
            row["shot"] = shot  # close framing of the speaker — rendered as this line's still
        out.append(row)
    return out


def _coerce_float(v, default: float) -> float:
    try:
        f = float(v)
        return f if f > 0 else default
    except (TypeError, ValueError):
        return default


def _scene_mode_lines(item: dict) -> tuple[str, list[dict]]:
    """(mode, lines) for a generated scene — 'narration' unless the LLM marked it."""
    mode = str(item.get("mode") or "narration").strip().lower() or "narration"
    lines = _norm_scene_lines(item) if mode == "dialogue" else []
    if mode == "dialogue" and not lines:
        mode = "narration"  # dialogue with no usable lines degrades to narration
    if mode not in ("narration", "silent", "dialogue"):
        mode = "narration"
    return mode, lines


def _json_script_generate(title: str, n_scenes: int, style_hint: str | None,
                          call_fn,
                          video_title: str | None = None,
                          video_style_hint: str | None = None,
                          character_sheet: str | None = None,
                          avoid_hint: str | None = None,
                          dialogue_note: str | None = None,
                          language: str | None = None,
                          scene_plan: dict | None = None) -> tuple[list[Scene], str, str, list[dict]]:
    """JSON batch script generation shared by Claude and Grok.

    *call_fn(system, user_msg, max_tokens, label, retries=...)* → str.
    """
    # ── Batch 1: style + music + first BATCH_SIZE scenes ──────────────────────
    first_batch = min(_CLAUDE_BATCH_SIZE, n_scenes)
    style_note = (
        f'\nIMPORTANT: Use exactly this text for the "style" field: "{style_hint}"'
        if style_hint and style_hint.strip() else ""
    )
    video_style_note = (
        f'\nMOTION DIRECTION — apply to EVERY scene\'s "video_prompt": {video_style_hint.strip()}'
        if video_style_hint and video_style_hint.strip() else ""
    )
    avoid_note = (
        f"\nAVOID — do NOT mention, depict, or reference any of the following anywhere "
        f"in the script (narration, titles, or prompts): {avoid_hint.strip()}"
        if avoid_hint and avoid_hint.strip() else ""
    )
    character_note = (
        f"\n{character_sheet.strip()}"
        if character_sheet and character_sheet.strip() else ""
    )
    dialogue_note_str = (
        f"\n{dialogue_note.strip()}"
        if dialogue_note and dialogue_note.strip() else ""
    )
    lang_name = narration_language_name(language)
    language_note = (
        f'\nNARRATION LANGUAGE — write every scene\'s "narration" (and every dialogue '
        f'line\'s "text") in {lang_name}. Everything else — "style", "music", scene '
        f'titles, "image_prompt", "video_prompt", and character names/descriptions — '
        f"stays in English (it feeds English-only image/video models)."
        if lang_name else ""
    )
    # The system prompt pins the scene schema, so dialogue fields must be added
    # THERE — a user-message note alone gets ignored ("each with exactly these
    # keys" wins). Empty when dialogue is off → the narration prompt is unchanged.
    dialogue_schema = (
        '\n      - "mode": "narration" | "dialogue" | "silent" — how the scene plays. '
        'Use "dialogue" when characters speak on screen, "silent" for a pure visual beat with no voice-over.'
        '\n      - "lines": ONLY for "dialogue" scenes — ordered array of shots. A SPEAKING shot is '
        '{"speaker": <character name>, "text": <1-2 short spoken sentences>, '
        '"shot": <20-40 word STATIC framing showing ONLY the speaker in this scene\'s setting — '
        'a single person, a medium shot (roughly waist-up, some of the setting around them), '
        'their face clearly visible and looking toward the camera (NOT an extreme close-up); '
        'NO other characters in the frame; include setting details around them>}. '
        'A SILENT shot (optional, use occasionally for a beat where characters MOVE but do not '
        'speak — a reaction, a draw, an entrance) is '
        '{"silent": true, "shot": <static framing of the moment>, '
        '"video_prompt": <the motion — what moves and how, camera included>, "duration": <seconds, 2-5>}. '
        'A dialogue scene is a sequence of these shots — the camera cuts between them. '
        'Dialogue and silent scenes MUST have "narration": "". '
        'For dialogue scenes the "image_prompt" is the establishing wide shot of the setting.'
        if dialogue_note_str else ""
    )
    is_last_batch = (first_batch == n_scenes)
    conclusion_note = (
        f"\nIMPORTANT: Scene {n_scenes} is the FINAL scene — end the video with a clear, "
        f"simple conclusion: wrap up the story in plain language, resolve the hook, and "
        f"make it unmistakable that the story is complete. No cliffhangers, no new "
        f"questions, no cryptic closing lines."
        if is_last_batch else ""
    )
    title_line = f'Topic: "{title}"\n'
    if video_title and video_title.strip():
        title_line = f'YouTube Video Title: "{video_title}"\nTopic/Description: "{title}"\n'
    user_msg = _prompts.user(
        "script_claude_initial",
        title_line=title_line,
        n_scenes=n_scenes,
        first_batch=first_batch,
        style_note=style_note,
        video_style_note=video_style_note,
        avoid_note=avoid_note,
        character_note=character_note,
        dialogue_note=dialogue_note_str,
        language_note=language_note,
        conclusion_note=conclusion_note,
    )
    max_tokens = first_batch * 600 + 600  # per-scene headroom + overhead
    raw = call_fn(_prompts.system("script_claude_initial", dialogue_schema=dialogue_schema,
                                  **_scene_len_vars(scene_plan)),
                  user_msg, max_tokens, f"scenes 1–{first_batch}")
    outer = _parse_claude_response(raw, f"scenes 1–{first_batch}")

    style      = style_hint.strip() if style_hint and style_hint.strip() else outer.get("style", "")
    music_desc = outer.get("music", "cinematic orchestral background music, atmospheric, instrumental")
    scenes_data = outer.get("scenes", [])
    if not scenes_data:
        raise RuntimeError("Claude returned empty scene list")

    # Main characters the model established for this video (0-2). Fold them into
    # the note carried to later batches so scenes past the first stay consistent.
    identified = _norm_identified_characters(outer.get("characters"))
    character_note = _merge_character_note(character_note, identified)

    scenes = []
    for i, item in enumerate(scenes_data[:first_batch]):
        mode, lines = _scene_mode_lines(item)
        scenes.append(Scene(
            id=item.get("id", i + 1),
            title=item.get("title", f"Scene {i + 1}"),
            image_prompt=item.get("image_prompt", title),
            video_prompt=item.get("video_prompt", item.get("image_prompt", title)),
            narration=item.get("narration", ""),
            mode=mode, lines=lines,
        ))

    # ── Continuation batches ──────────────────────────────────────────────────
    batch_start = first_batch + 1
    topic_ref = f'"{video_title}"' if video_title and video_title.strip() else f'"{title}"'
    while batch_start <= n_scenes:
        batch_end   = min(batch_start + _CLAUDE_BATCH_SIZE - 1, n_scenes)
        is_last     = batch_end == n_scenes
        # Provide last 3 scenes as continuity context
        ctx = [
            {"id": s.id, "title": s.title, "narration": s.narration}
            for s in scenes[-3:]
        ]
        ctx_str = "\n".join(
            f'  Scene {s["id"]}: "{s["title"]}" — {s["narration"]}' for s in ctx
        )
        conclusion_note = (
            f"\nIMPORTANT: Scene {batch_end} is the FINAL scene — end the video with a "
            f"clear, simple conclusion: wrap up the story in plain language, resolve the "
            f"hook, and make it unmistakable that the story is complete. No cliffhangers, "
            f"no new questions, no cryptic closing lines."
            if is_last else ""
        )
        cont_msg = _prompts.user(
            "script_claude_continuation",
            n_scenes=n_scenes,
            topic_ref=topic_ref,
            topic_full=title,
            batch_start=batch_start,
            batch_end=batch_end,
            ctx_str=ctx_str,
            video_style_note=video_style_note,
            avoid_note=avoid_note,
            character_note=character_note,
            dialogue_note=dialogue_note_str,
            language_note=language_note,
            conclusion_note=conclusion_note,
        )
        max_tokens = (batch_end - batch_start + 1) * 450 + 300
        raw = call_fn(_prompts.system("script_claude_continuation", dialogue_schema=dialogue_schema,
                                      **_scene_len_vars(scene_plan)), cont_msg,
                      max_tokens, f"scenes {batch_start}–{batch_end}")
        items = _parse_claude_response(raw, f"scenes {batch_start}–{batch_end}")
        if not isinstance(items, list):
            items = items.get("scenes", [])
        for i, item in enumerate(items):
            mode, lines = _scene_mode_lines(item)
            scenes.append(Scene(
                id=batch_start + i,
                title=item.get("title", f"Scene {batch_start + i}"),
                image_prompt=item.get("image_prompt", title),
                video_prompt=item.get("video_prompt", item.get("image_prompt", title)),
                narration=item.get("narration", ""),
                mode=mode, lines=lines,
            ))
        batch_start = batch_end + 1

    final_scenes = scenes[:n_scenes]
    _fill_empty_narrations(call_fn, final_scenes, title, video_title, language=language,
                           scene_plan=scene_plan)
    # Absolute last-resort safety net: no Scene leaves with empty narration.
    for s in final_scenes:
        if not (s.narration or "").strip():
            s.narration = f"{s.title or f'Scene {s.id}'}."
            logger.warning("Scene %d still empty after cloud fill — used title", s.id)
    # 10–15 s scene contract, in order: condense over-cap narrations down to
    # the cap (scene count and video length stay as planned), split whatever
    # still overflows, then give the split pieces their own visuals. All
    # BEFORE character detection — splitting renumbers scene ids, which would
    # break the detector's scene references.
    condense_long_narrations(call_fn, final_scenes, scene_plan, language=language)
    final_scenes = enforce_scene_word_caps(final_scenes, scene_plan)
    regen_split_scene_visuals(call_fn, final_scenes, title, style,
                              video_style_hint=video_style_hint,
                              character_sheet=character_note)
    # Second pass over the whole script: catch recurring supporting characters the
    # first-batch identify (scenes 1–10, 1-2 central subjects) missed.
    identified = _detect_recurring_characters(call_fn, final_scenes, identified,
                                              style_hint=style)
    return final_scenes, music_desc, style, identified


def _claude_generate(title: str, n_scenes: int, style_hint: str | None,
                     api_key: str, model: str,
                     video_title: str | None = None,
                     video_style_hint: str | None = None,
                     character_sheet: str | None = None,
                     avoid_hint: str | None = None,
                     dialogue_note: str | None = None,
                     language: str | None = None,
                     scene_plan: dict | None = None) -> tuple[list[Scene], str, str, list[dict]]:
    import anthropic
    import httpx
    # Force HTTP/1.1 — HTTP/2 multiplexed connections get RST_STREAM / GOAWAY
    # from Anthropic's servers after ~3 minutes on large prompts, causing
    # "Server disconnected without sending a response" errors.
    timeout = httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=30.0)
    http_client = httpx.Client(http2=False, timeout=timeout)
    client = anthropic.Anthropic(api_key=api_key, http_client=http_client)

    def call_fn(system, user_msg, max_tokens, label, retries=6):
        return _claude_call(client, model, system, user_msg, max_tokens, label, retries=retries)

    return _json_script_generate(
        title, n_scenes, style_hint, call_fn,
        video_title=video_title, video_style_hint=video_style_hint,
        character_sheet=character_sheet, avoid_hint=avoid_hint, dialogue_note=dialogue_note,
        language=language, scene_plan=scene_plan,
    )


def _grok_generate(title: str, n_scenes: int, style_hint: str | None,
                   api_key: str, model: str,
                   video_title: str | None = None,
                   video_style_hint: str | None = None,
                   character_sheet: str | None = None,
                   avoid_hint: str | None = None,
                   dialogue_note: str | None = None,
                   language: str | None = None,
                   api_url: str | None = None,
                   scene_plan: dict | None = None) -> tuple[list[Scene], str, str, list[dict]]:
    """Grok (xAI) uses the same JSON batch protocol as Claude."""
    url = api_url or _GROK_CHAT_URL_DEFAULT

    def call_fn(system, user_msg, max_tokens, label, retries=6):
        return _openai_compatible_call(
            url, api_key, model, system, user_msg, max_tokens, label, retries=retries,
        )

    return _json_script_generate(
        title, n_scenes, style_hint, call_fn,
        video_title=video_title, video_style_hint=video_style_hint,
        character_sheet=character_sheet, avoid_hint=avoid_hint, dialogue_note=dialogue_note,
        language=language, scene_plan=scene_plan,
    )


def _openai_generate(title: str, n_scenes: int, style_hint: str | None,
                     api_key: str, model: str,
                     video_title: str | None = None,
                     video_style_hint: str | None = None,
                     character_sheet: str | None = None,
                     avoid_hint: str | None = None,
                     dialogue_note: str | None = None,
                     language: str | None = None,
                     api_url: str | None = None,
                     scene_plan: dict | None = None) -> tuple[list[Scene], str, str, list[dict]]:
    """OpenAI ChatGPT uses the same JSON batch protocol as Claude/Grok."""
    url = api_url or _OPENAI_CHAT_URL_DEFAULT

    def call_fn(system, user_msg, max_tokens, label, retries=6):
        return _openai_compatible_call(
            url, api_key, model, system, user_msg, max_tokens, label, retries=retries,
        )

    return _json_script_generate(
        title, n_scenes, style_hint, call_fn,
        video_title=video_title, video_style_hint=video_style_hint,
        character_sheet=character_sheet, avoid_hint=avoid_hint, dialogue_note=dialogue_note,
        language=language, scene_plan=scene_plan,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Local vLLM backend (plain-text, two-stage)
# ══════════════════════════════════════════════════════════════════════════════


def _check_local_available(url: str) -> bool:
    models_url = url.split("/chat/completions")[0].rstrip("/v1") + "/v1/models"
    try:
        req = urllib.request.Request(models_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def _local_llm(messages: list[dict], max_tokens: int,
               url: str, model: str, retries: int = 2) -> str:
    # Reasoning models (Qwen3, DeepSeek-R1 distills, …) spend tokens "thinking"
    # before the answer — either inline <think>…</think> in content, or in a
    # separate reasoning_content field (llama.cpp). A small max_tokens can be
    # eaten entirely by the thinking, so grow the budget on retry when that
    # happens instead of repeating an identical doomed request.
    tokens = max_tokens

    last_err: Exception | None = None
    for attempt in range(1 + retries):
        payload = json.dumps({
            "model":       model,
            "messages":    messages,
            "temperature": 0.7,
            "max_tokens":  tokens,
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                detail = e.read(500).decode("utf-8", "replace")
            except Exception:
                detail = ""
            last_err = RuntimeError(f"LLM request failed: {e}" + (f" — {detail}" if detail else ""))
            logger.warning("Local LLM attempt %d/%d failed: %s %s",
                           attempt + 1, 1 + retries, e, detail)
            continue
        except urllib.error.URLError as e:
            last_err = RuntimeError(f"LLM request failed: {e}")
            logger.warning("Local LLM attempt %d/%d failed: %s", attempt + 1, 1 + retries, e)
            continue

        choice  = data["choices"][0]
        message = choice.get("message", {})
        finish  = choice.get("finish_reason")
        content = message.get("content") or ""
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        if "<think>" in content:
            # Unterminated thinking — the token limit cut it off mid-thought.
            content = content.split("<think>")[0]
        if content.strip():
            logger.debug("local LLM finish_reason=%s", finish)
            return content

        thinking = bool(message.get("reasoning_content"))
        if thinking or finish in ("length", "max_tokens"):
            tokens = min(tokens * 4, 8192)
        last_err = RuntimeError(
            f"Local LLM empty content (finish_reason={finish}"
            + (", reasoning used the whole token budget" if thinking else "")
            + ")"
        )
        logger.warning("Local LLM attempt %d/%d empty: %s%s (next max_tokens=%d)",
                       attempt + 1, 1 + retries, finish,
                       " — reasoning used the whole token budget" if thinking else "", tokens)

    raise last_err  # type: ignore[misc]


def _get_field(text: str, key: str) -> str:
    """Get value for KEY: from `KEY: value` lines.

    Handles two formats the LLM occasionally produces:
      1. Same line:  `KEY: value`
      2. Next line:  `KEY:\n  value`  (with optional blank lines between)
    """
    # Same-line format
    m = re.search(rf"^{re.escape(key)}:\s*(.+)", text, re.MULTILINE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    # Next-line format (key followed by blank/whitespace, value on a later line)
    m = re.search(rf"^{re.escape(key)}:\s*\n+\s*([^\n][^\n]*)", text, re.MULTILINE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return ""


def _local_generate_story(title: str, n_scenes: int, style_hint: str | None,
                          url: str, model: str,
                          video_title: str | None = None,
                          character_sheet: str | None = None,
                          avoid_hint: str | None = None,
                          language: str | None = None,
                          scene_plan: dict | None = None) -> dict:
    style_note = (
        f"\nIMPORTANT: Use exactly this text for the STYLE line: {style_hint}"
        if style_hint and style_hint.strip()
        else ""
    )
    avoid_note = (
        f"\nAVOID — do NOT mention, depict, or reference any of the following anywhere "
        f"in the script: {avoid_hint.strip()}"
        if avoid_hint and avoid_hint.strip() else ""
    )
    character_note = (
        f"\n{character_sheet.strip()}"
        if character_sheet and character_sheet.strip() else ""
    )
    title_context = (
        f'YouTube Video Title: "{video_title}"\nTopic/Description: "{title}"'
        if video_title and video_title.strip()
        else f'the topic: "{title}"'
    )
    lang_name = narration_language_name(language)
    language_note = (
        f"\nNARRATION LANGUAGE — write every NARRATION_N value in {lang_name}. "
        f"Keep the STYLE, MUSIC, TITLE_N and CHARACTER_* lines in English "
        f"(they feed English-only image/video models)."
        if lang_name else ""
    )
    user_msg = _prompts.user(
        "script_local_story",
        n_scenes=n_scenes,
        title_context=title_context,
        style_note=style_note,
        avoid_note=avoid_note,
        character_note=character_note,
        language_note=language_note,
    )
    raw = _local_llm(
        [
            {"role": "system", "content": _prompts.system("script_local_story",
                                                          **_scene_len_vars(scene_plan))},
            {"role": "user",   "content": user_msg},
        ],
        max_tokens=4096 + n_scenes * 200,
        url=url, model=model,
    )
    logger.debug("Story raw (%d chars):\n%s", len(raw), raw[:800])
    style  = _get_field(raw, "STYLE")
    music  = _get_field(raw, "MUSIC")
    characters = []
    for i in range(1, _MAX_MAIN_CHARACTERS + 1):
        cname = _get_field(raw, f"CHARACTER_{i}_NAME")
        if not cname:
            continue
        calias = _get_field(raw, f"CHARACTER_{i}_ALIASES")
        aliases = [a.strip() for a in calias.split(",") if a.strip()] if calias else []
        characters.append({
            "name": cname,
            "aliases": aliases,
            "description": _get_field(raw, f"CHARACTER_{i}_DESC"),
        })
    scenes = []
    for i in range(1, n_scenes + 1):
        title_val = _get_field(raw, f"TITLE_{i}")
        narr_val  = _get_field(raw, f"NARRATION_{i}")
        if title_val or narr_val:
            scenes.append({"id": i, "title": title_val, "narration": narr_val})
    result = {"style": style, "music": music, "scenes": scenes, "characters": characters}
    if not scenes:
        raise RuntimeError(f"Story call returned no scenes.\nRaw response:\n{raw[:600]}")
    return result


def _fill_empty_outlines_local(outlines: list[dict], title: str, video_title: str | None,
                                 url: str, model: str,
                                 language: str | None = None,
                                 scene_plan: dict | None = None) -> None:
    """Fill any outline dicts whose narration is empty via a targeted local-LLM call.

    Operates BEFORE visual generation so that downstream image/video prompts have
    proper narration context. Mutates the outlines list in place.
    """
    topic = video_title or title
    lang_name = narration_language_name(language)
    empty = [o for o in outlines if not (o.get("narration") or "").strip()]
    if not empty:
        return
    logger.warning("Outlines with empty narration after story pass: %s — filling",
                   [o.get("id") for o in empty])
    for o in empty:
        sid = o.get("id", 0)
        prev_narr = next((x.get("narration", "") for x in outlines
                          if x.get("id") == sid - 1 and (x.get("narration") or "").strip()), "")
        next_narr = next((x.get("narration", "") for x in outlines
                          if x.get("id") == sid + 1 and (x.get("narration") or "").strip()), "")
        scene_title = o.get("title") or f"Scene {sid}"
        ctx_parts = [f'Topic: "{topic}"', f'Scene {sid} title: "{scene_title}"']
        if lang_name:
            ctx_parts.append(f"Write the narration in {lang_name}.")
        if sid == 1:
            ctx_parts.append(
                "This is SCENE 1 — open with a compelling hook that teases the most "
                "surprising or emotionally resonant moment from later in the video."
            )
        if prev_narr:
            ctx_parts.append(f'Previous scene narration: "{prev_narr}"')
        if next_narr:
            ctx_parts.append(f'Next scene narration: "{next_narr}"')
        try:
            narration = _local_llm(
                [
                    {"role": "system",
                     "content": _prompts.system("script_local_fill_narration")},
                    {"role": "user",
                     "content": _prompts.user("script_local_fill_narration", ctx="\n".join(ctx_parts),
                                              **_scene_len_vars(scene_plan))},
                ],
                max_tokens=220,
                url=url,
                model=model,
                retries=2,
            ).strip()
            # Strip any stray label/quote the LLM might prepend
            for prefix in ("NARRATION:", "Narration:", "NARRATOR:", f"NARRATION_{sid}:"):
                if narration.upper().startswith(prefix.upper()):
                    narration = narration[len(prefix):].strip()
                    break
            narration = narration.strip().strip('"').strip("'").strip()
            if narration:
                o["narration"] = narration
                logger.info("Filled narration for scene %d: %r", sid, narration[:80])
            else:
                # Last-resort: use the scene title as narration so TTS isn't blank
                o["narration"] = f"{scene_title}."
                logger.warning("Local LLM returned empty fill for scene %d — using title fallback", sid)
        except Exception as exc:
            logger.warning("Could not fill narration for scene %d: %s — using title fallback", sid, exc)
            o["narration"] = f"{scene_title}."


def _local_generate_visual(title: str, style: str,
                            scene_id: int, scene_title: str, narration: str,
                            url: str, model: str,
                            video_style_hint: str | None = None,
                            character_sheet: str | None = None) -> tuple[str, str]:
    video_style_note = (
        f"\nMOTION DIRECTION for the VIDEO line: {video_style_hint.strip()}"
        if video_style_hint and video_style_hint.strip() else ""
    )
    character_note = (
        f"\n{character_sheet.strip()}"
        if character_sheet and character_sheet.strip() else ""
    )
    user_msg = _prompts.user(
        "script_local_visual",
        title=title,
        style=style,
        scene_title=scene_title,
        narration=narration,
        video_style_note=video_style_note,
        character_note=character_note,
    )
    raw = _local_llm(
        [
            {"role": "system", "content": _prompts.system("script_local_visual")},
            {"role": "user",   "content": user_msg},
        ],
        max_tokens=2048,
        url=url, model=model,
    )
    image_prompt = _get_field(raw, "IMAGE")
    video_prompt = _get_field(raw, "VIDEO")
    # Graceful fallback if model didn't split correctly
    if not image_prompt:
        image_prompt = raw.strip().lstrip("IMAGE:").strip()
    if not video_prompt:
        video_prompt = image_prompt
    return image_prompt, video_prompt


def _local_generate(title: str, n_scenes: int,
                    style_hint: str | None,
                    video_title: str | None = None,
                    video_style_hint: str | None = None,
                    character_sheet: str | None = None,
                    avoid_hint: str | None = None,
                    language: str | None = None,
                    scene_plan: dict | None = None) -> tuple[list[Scene], str, str, list[dict]]:
    cfg   = _load_cfg()
    url   = cfg.get("local_llm_url",   _LOCAL_LLM_URL_DEFAULT)
    model = cfg.get("local_llm_model", _LOCAL_LLM_MODEL_DEFAULT)

    if not _check_local_available(url):
        raise RuntimeError(
            f"Local LLM at {url} is not reachable.\n"
            "Set the URL in Config → LLM Backend → Local LLM URL."
        )

    story      = _local_generate_story(title, n_scenes, style_hint, url, model,
                                       video_title=video_title, character_sheet=character_sheet,
                                       avoid_hint=avoid_hint, language=language,
                                       scene_plan=scene_plan)
    style      = (style_hint.strip() if style_hint and style_hint.strip()
                  else story.get("style", ""))
    music_desc = story.get("music", "cinematic orchestral background music, atmospheric, instrumental")
    outlines   = story["scenes"]

    # Main characters the story stage established (0-2). Fold them into the sheet
    # the per-scene visual stage sees so each scene's IMAGE prompt names them and
    # describes their appearance consistently.
    identified   = _norm_identified_characters(story.get("characters"))
    visual_sheet = _merge_character_note(character_sheet or "", identified).strip() or None

    # Critical: fill any empty narrations BEFORE visual generation so the image/video
    # prompts get proper context. Scene 1 is particularly prone to being left blank.
    _fill_empty_outlines_local(outlines, title, video_title, url, model, language=language,
                               scene_plan=scene_plan)

    # 10–15 s scene contract: condense over-cap narrations first (scene count
    # and video length stay as planned), then split whatever still overflows —
    # BEFORE the visual stage, so each split scene gets its own image/video
    # prompts (not a copy of its source's).
    if scene_plan and int(scene_plan.get("scene_words_max") or 0) > 0:
        from pipeline import cadence

        def _cond_call(system, user_msg, max_tokens, label, retries=2):  # noqa: ARG001
            return _local_llm([{"role": "system", "content": system},
                               {"role": "user", "content": user_msg}],
                              max_tokens=max_tokens, url=url, model=model, retries=retries)
        temp = [Scene(id=o["id"], title=o.get("title", ""), image_prompt="",
                      video_prompt="", narration=o.get("narration", ""))
                for o in outlines]
        condense_long_narrations(_cond_call, temp, scene_plan, language=language)
        for o, t in zip(outlines, temp):
            o["narration"] = t.narration
        max_w = int(scene_plan["scene_words_max"])
        min_w = int(scene_plan.get("scene_words_min") or 0)
        expanded: list[dict] = []
        for o in sorted(outlines, key=lambda x: x["id"]):
            pieces = cadence.split_narration(o.get("narration", ""), max_w, min_w)
            if len(pieces) <= 1:
                expanded.append(o)
                continue
            logger.info("Scene %d narration over the %d-word cap — split into %d scenes",
                        o["id"], max_w, len(pieces))
            expanded.extend({**o, "narration": p} for p in pieces)
        for i, o in enumerate(expanded, 1):
            o["id"] = i
        outlines = expanded

    logger.info("Story: %d scenes, style=%r", len(outlines), style)

    def _fetch(outline: dict) -> tuple[int, str, str]:
        img_p, vid_p = _local_generate_visual(
            title, style,
            outline["id"],
            outline.get("title", f"Scene {outline['id']}"),
            outline.get("narration", ""),
            url=url, model=model,
            video_style_hint=video_style_hint,
            character_sheet=visual_sheet,
        )
        return outline["id"], img_p, vid_p

    img_prompts: dict[int, str] = {}
    vid_prompts: dict[int, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for sid, img_p, vid_p in pool.map(_fetch, outlines):
            img_prompts[sid] = img_p
            vid_prompts[sid] = vid_p

    scenes = [
        Scene(
            id=o["id"],
            title=o.get("title", f"Scene {o['id']}"),
            image_prompt=img_prompts.get(o["id"], title),
            video_prompt=vid_prompts.get(o["id"], title),
            narration=o.get("narration", ""),
        )
        for o in sorted(outlines, key=lambda x: x["id"])
    ]
    if not scenes:
        raise RuntimeError("No scenes assembled")

    # Absolute last-resort safety net: no Scene leaves this function with empty narration.
    for s in scenes:
        if not (s.narration or "").strip():
            s.narration = f"{s.title or f'Scene {s.id}'}."
            logger.warning("Scene %d still had empty narration at assembly — used title", s.id)

    # Second pass over the whole script: catch recurring supporting characters the
    # story stage (0-2 central subjects) missed. Adapts _local_llm to the shared
    # call_fn(system, user_msg, max_tokens, label, retries) signature.
    def _rc_call(system, user_msg, max_tokens, label, retries=2):  # noqa: ARG001
        return _local_llm(
            [{"role": "system", "content": system},
             {"role": "user", "content": user_msg}],
            max_tokens=max_tokens, url=url, model=model, retries=retries,
        )
    identified = _detect_recurring_characters(_rc_call, scenes, identified,
                                              style_hint=style)

    return scenes, music_desc, style, identified


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def generate_script(
    title: str,
    n_scenes: int,
    style_hint: str | None = None,
    video_title: str | None = None,
    video_style_hint: str | None = None,
    character_sheet: str | None = None,
    avoid_hint: str | None = None,
    dialogue_note: str | None = None,
    language: str | None = None,
    scene_plan: dict | None = None,
) -> tuple[list[Scene], str, str, list[dict]]:
    """Return (scenes, music_description, style, characters).

    scene_plan is a cadence plan (pipeline/cadence.py — see app.style_script_plan):
    it sets each scene's narration word target and hard cap (10–15 s at the
    narrator's cadence) in the prompts, and over-long narrations are split at
    natural pauses afterwards. None keeps the DEFAULT_WPM word caps in the
    prompts but skips the splitting backstop (legacy callers).

    Backend is chosen from config: llm_backend = "claude" | "grok" | "openai" | "local".
    video_title is the short YouTube title; title is the full topic/description.
    video_style_hint is per-style motion/cinematography guidance steering each
    scene's video_prompt (camera + subject movement).
    character_sheet is a pre-formatted block describing recurring characters and
    their fixed appearance, injected into every batch/scene so named characters
    look consistent (see app._character_sheet).
    avoid_hint is the per-style "avoid" instruction — topics/words/tropes to keep
    OUT of the generated script; woven into every batch (see style.script_avoid).
    language is the style's narration-language code (see chatterbox.LANGUAGES):
    narration and dialogue lines are written in that language, while prompts,
    titles, style and music descriptions stay in English. None/"en" → all English.
    characters is the list of 0-2 main characters the model identified for THIS
    video, each {name, aliases, description}; [] when the topic has no recurring
    character. These are per-script (not the global catalogue) — the caller
    persists and can later promote them (see app._read/_write_script_characters).
    """
    cfg     = _load_cfg()
    backend = cfg.get("llm_backend", "local")

    if backend == "claude":
        api_key = _claude_api_key(cfg)
        if not api_key:
            raise RuntimeError(
                'Claude API key not set. Add it in Settings under "LLM backend".'
            )
        model = cfg.get("claude_model", "claude-sonnet-4-6")
        logger.info("Using Claude backend: model=%s", model)
        return _claude_generate(title, n_scenes, style_hint, api_key, model,
                                video_title=video_title, video_style_hint=video_style_hint,
                                character_sheet=character_sheet, avoid_hint=avoid_hint,
                                dialogue_note=dialogue_note, language=language,
                                scene_plan=scene_plan)

    if backend == "grok":
        api_key = _grok_api_key(cfg)
        if not api_key:
            raise RuntimeError(
                'Grok API key not set. Add it in Settings under "LLM backend" '
                "(or set the XAI_API_KEY environment variable)."
            )
        model = cfg.get("grok_model") or _GROK_MODEL_DEFAULT
        logger.info("Using Grok backend: model=%s", model)
        return _grok_generate(title, n_scenes, style_hint, api_key, model,
                              video_title=video_title, video_style_hint=video_style_hint,
                              character_sheet=character_sheet, avoid_hint=avoid_hint,
                              dialogue_note=dialogue_note, language=language,
                              api_url=cfg.get("grok_api_url") or _GROK_CHAT_URL_DEFAULT,
                              scene_plan=scene_plan)

    if backend == "openai":
        api_key = _openai_api_key(cfg)
        if not api_key:
            raise RuntimeError(
                'OpenAI API key not set. Add it in Settings under "LLM backend" '
                "(or set the OPENAI_API_KEY environment variable)."
            )
        model = cfg.get("openai_model") or _OPENAI_MODEL_DEFAULT
        logger.info("Using OpenAI backend: model=%s", model)
        return _openai_generate(title, n_scenes, style_hint, api_key, model,
                                video_title=video_title, video_style_hint=video_style_hint,
                                character_sheet=character_sheet, avoid_hint=avoid_hint,
                                dialogue_note=dialogue_note, language=language,
                                api_url=cfg.get("openai_api_url") or _OPENAI_CHAT_URL_DEFAULT,
                                scene_plan=scene_plan)

    if dialogue_note:
        raise RuntimeError(
            "Dialogue/mixed script generation needs the Claude, Grok, or OpenAI backend "
            "(the local vLLM backend produces narration only). Switch the LLM "
            "backend in Settings, or use the Narration format."
        )
    logger.info("Using local vLLM backend")
    return _local_generate(title, n_scenes, style_hint, video_title=video_title,
                           video_style_hint=video_style_hint, character_sheet=character_sheet,
                           avoid_hint=avoid_hint, language=language, scene_plan=scene_plan)


# ── YouTube video prompt generation (director's brief) ───────────────────────

def generate_video_prompt(title: str, comment: str) -> str:  # noqa: ARG001
    """Generate a directorial style brief (how to make it, not what it covers)."""
    cfg = _load_cfg()
    # Note: comment is intentionally ignored — the brief must be topic-agnostic.
    sys_msg = _prompts.system("director_brief")
    user_msg = _prompts.user("director_brief", title=title or "Untitled")
    try:
        return _chat_complete(cfg, sys_msg, user_msg, max_tokens=200, label="video_prompt").strip()
    except Exception as exc:
        logger.warning("generate_video_prompt failed: %s", exc)
        return ""  # empty string — caller should show Create tab without a pre-filled prompt


# ── Community comment reply (issue #84) ───────────────────────────────────────

_COMMUNITY_SAFE_DEFAULT = {"should_reply": False, "reply": "", "reason": ""}


def _parse_community_reply(text: str) -> dict:
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            result = json.loads(m.group())
            return {
                "should_reply": bool(result.get("should_reply", False)),
                "reply": str(result.get("reply", "")).strip(),
                "reason": str(result.get("reason", "")).strip(),
            }
    except Exception:
        pass
    return {**_COMMUNITY_SAFE_DEFAULT, "reason": "Could not parse LLM response"}


def generate_community_reply(comment_text: str, commenter: str, thread_text: str,
                             guidance: str, cfg: dict) -> dict:
    """Decide whether to reply to a non-request community comment and, if so, draft
    a reply in the channel's voice. Returns {should_reply, reply, reason}; safe
    default on any failure. Backend is chosen from cfg (claude | grok | local)."""
    sys_msg = _prompts.system("community_reply")
    user_msg = _prompts.user(
        "community_reply",
        guidance=(guidance or "").strip() or "(no specific guidance — use a friendly, on-brand tone)",
        commenter=(commenter or "viewer")[:100],
        comment=(comment_text or "").strip()[:2000],
        thread=(thread_text or "").strip() or "(no replies yet)",
    )
    try:
        text = _chat_complete(cfg, sys_msg, user_msg, max_tokens=400, label="community_reply")
        return _parse_community_reply(text)
    except Exception as exc:
        logger.warning("generate_community_reply failed: %s", exc)
        return {**_COMMUNITY_SAFE_DEFAULT, "reason": f"Engagement error: {str(exc)[:120]}"}


# ── Narration translation (localize an already-rendered film) ────────────────

def _parse_translations(text: str, scene_ids: set[int]) -> dict[int, str]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise RuntimeError("Translation response did not contain a JSON object.")
    raw = json.loads(m.group())
    if not isinstance(raw, dict):
        raise RuntimeError("Translation response JSON was not an object.")
    out: dict[int, str] = {}
    for key, val in raw.items():
        try:
            sid = int(key)
        except (TypeError, ValueError):
            continue
        if sid in scene_ids and isinstance(val, str) and val.strip():
            out[sid] = val.strip()
    return out


# Scenes per translation call. Large scripts are translated in chunks so no
# single response ever nears a backend's output-token ceiling (mirrors script
# generation's batching protocol).
_TRANSLATE_BATCH = 12


def _translate_batch(rows: dict[int, str], target_lang_name: str, title: str,
                     cfg: dict) -> dict[int, str]:
    """Translate one batch of {scene_id: narration} rows; see translate_narrations."""
    sys_msg = _prompts.system("translate_narrations")
    user_msg = _prompts.user(
        "translate_narrations",
        target_lang=target_lang_name,
        title=(title or "").strip() or "(untitled)",
        scenes_json=json.dumps({str(k): v for k, v in rows.items()}, ensure_ascii=False),
    )
    # Translations run about the length of the source (some languages ~1.3×).
    # Budget ~2× the source's estimated tokens (chars/3 is conservative for
    # accented text) plus per-scene JSON overhead, so the model never truncates
    # mid-object.
    src_tokens = sum(len(t) for t in rows.values()) // 3
    max_tokens = 800 + 2 * src_tokens + 30 * len(rows)
    try:
        text = _chat_complete(cfg, sys_msg, user_msg, max_tokens=max_tokens,
                              label="translate_narrations")
    except RuntimeError as exc:
        # Defense-in-depth: if a backend still reports its token limit (e.g. a
        # local model with a small context), halve the batch and retry.
        if "token limit" in str(exc) and len(rows) > 1:
            ids = list(rows)
            mid = len(ids) // 2
            first = _translate_batch({k: rows[k] for k in ids[:mid]}, target_lang_name, title, cfg)
            second = _translate_batch({k: rows[k] for k in ids[mid:]}, target_lang_name, title, cfg)
            return {**first, **second}
        raise
    translations = _parse_translations(text, set(rows.keys()))
    missing = set(rows.keys()) - set(translations.keys())
    if missing:
        raise RuntimeError(f"Translation response was missing scenes: {sorted(missing)}")
    return translations


def translate_narrations(
    scenes: list[dict], target_lang_name: str, title: str = "", cfg: dict | None = None,
) -> dict[int, str]:
    """Translate every scene's narration text into *target_lang_name*, batching
    _TRANSLATE_BATCH scenes per LLM call so large scripts stay well under any
    backend's output-token limit. *scenes* are row dicts with an "id" (or
    "scene_id") and "narration". Returns {scene_id: translated_text} — scenes
    with empty/whitespace-only narration are omitted, the caller skips TTS for
    those. Raises on failure (malformed response, LLM/network error) rather
    than silently returning untranslated text, since a film mislabeled as
    translated but still in the original language is a worse failure than a
    visible error."""
    rows = {}
    for row in scenes:
        sid = int(row.get("id") if row.get("id") is not None else row.get("scene_id") or 0)
        text = (row.get("narration") or "").strip()
        if sid and text:
            rows[sid] = text
    if not rows:
        return {}

    cfg = cfg or _load_cfg()
    ids = list(rows)
    out: dict[int, str] = {}
    for start in range(0, len(ids), _TRANSLATE_BATCH):
        chunk = {sid: rows[sid] for sid in ids[start:start + _TRANSLATE_BATCH]}
        out.update(_translate_batch(chunk, target_lang_name, title, cfg))
    return out


def translate_metadata(title: str, description: str, target_lang_name: str,
                       cfg: dict | None = None, cover_phrase: str = "") -> dict[str, str]:
    """Translate a film's publish metadata (YouTube title + description, plus
    the short cover phrase drawn on the thumbnail) into *target_lang_name*.
    Returns {"title": ..., "description": ..., "cover_phrase": ...}. Raises on
    failure — the caller decides whether localized metadata is best-effort."""
    cfg = cfg or _load_cfg()
    sys_msg = _prompts.system("translate_metadata")
    user_msg = _prompts.user(
        "translate_metadata",
        target_lang=target_lang_name,
        title=(title or "").strip(),
        description=(description or "").strip(),
        cover_phrase=(cover_phrase or "").strip(),
    )
    # Same sizing rationale as narration: output ≈ input, budget 2× + overhead.
    src_tokens = (len(title) + len(description) + len(cover_phrase)) // 3
    text = _chat_complete(cfg, sys_msg, user_msg,
                          max_tokens=600 + 2 * src_tokens, label="translate_metadata")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise RuntimeError("Metadata translation did not contain a JSON object.")
    raw = json.loads(m.group())
    out_title = str(raw.get("title") or "").strip()
    if not out_title:
        raise RuntimeError("Metadata translation was missing the title.")
    from pipeline.cover import COVER_PHRASE_MAX_CHARS
    return {"title": out_title[:100],
            "description": str(raw.get("description") or "").strip(),
            "cover_phrase": str(raw.get("cover_phrase") or "").strip()[:COVER_PHRASE_MAX_CHARS]}


# ── Video topic suggestions ───────────────────────────────────────────────────

def _parse_suggestions(text: str) -> list[dict]:
    try:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            if isinstance(data, list):
                result = []
                for item in data[:5]:
                    if isinstance(item, dict) and item.get("title"):
                        result.append({
                            "title": str(item["title"]),
                            "reason": str(item.get("reason", "")),
                            "interestingness": float(item.get("interestingness", 0.7)),
                        })
                return result
    except Exception:
        pass
    return []


def style_suggestion_context(style: dict | None) -> str:
    """Render a style profile (issue #66) into prompt lines steering idea
    generation — including how titles should be worded (issue #82). Empty when
    there's nothing distinctive to say."""
    if not style:
        return ""
    lines = []
    name = (style.get("name") or "").strip()
    description = (style.get("description") or "").strip()
    visual = (style.get("visual_style") or "").strip()
    title_style = (style.get("title_style") or "").strip()
    if name and name != "(none)":
        lines.append(f'The new videos will be produced in the channel\'s "{name}" style.')
    if description:
        lines.append(f"That style is described as: {description}")
    if visual:
        lines.append(f"Its visuals look like: {visual}")
    if lines:
        lines.append("Every suggested topic must suit videos made in that style.")
    # Title styling is independent of topic suitability — apply it even when the
    # style has no descriptive context, so a title-style-only profile still steers.
    if title_style:
        lines.append(f"Word every video title to follow this style: {title_style}")
    if not lines:
        return ""
    return "\n" + "\n".join(lines) + "\n"


def _norm_title(t: str) -> str:
    """Casefolded, whitespace-collapsed title key for verbatim de-duplication."""
    return " ".join((t or "").strip().lower().split())


def _drop_repeats(suggestions: list[dict], previous_titles: list[str] | None) -> list[dict]:
    """Drop any suggestion whose title verbatim-matches an already-made/queued
    title. A hard guarantee on top of the prompt instruction that idea generation
    never re-proposes an existing video (the model occasionally repeats one)."""
    made = {_norm_title(t) for t in (previous_titles or [])}
    return [s for s in suggestions if _norm_title(s.get("title")) not in made]


def generate_video_suggestions(previous_titles: list[str], cfg: dict | None = None,
                               style: dict | None = None,
                               discarded_titles: list[str] | None = None) -> list[dict]:
    """Generate 5 video topic suggestions that suit the channel's style.

    ``style`` (optional) is a style profile dict (name/description/visual_style)
    whose character steers the ideas — a children-story style should yield
    children-story topics, NOT topics inferred from past videos. ``previous_titles``
    and ``discarded_titles`` are shown to the model only as 'do not repeat' lists,
    and verbatim repeats are also filtered out programmatically. Returns a list of
    dicts with keys: title, reason, interestingness.
    """
    if cfg is None:
        cfg = _load_cfg()
    titles_list = (
        "\n".join(f'- "{t}"' for t in previous_titles)
        if previous_titles
        else "(no previous videos yet — suggest a varied starting set)"
    )
    discarded_list = (
        "\n".join(f'- "{t}"' for t in discarded_titles)
        if discarded_titles
        else "(none — nothing has been discarded yet)"
    )
    sys_msg = _prompts.system("video_suggestions")
    user_msg = _prompts.user("video_suggestions", titles_list=titles_list,
                             discarded_list=discarded_list,
                             style_context=style_suggestion_context(style))
    try:
        text = _chat_complete(cfg, sys_msg, user_msg, max_tokens=2048, label="video_suggestions")
        return _drop_repeats(_parse_suggestions(text), previous_titles)
    except Exception as exc:
        logger.warning("generate_video_suggestions failed: %s", exc)
        return []


# ── YouTube description generation ───────────────────────────────────────────

def generate_youtube_description(
    title: str,
    scenes: list[dict],
    style: str = "",
    music_desc: str = "",
) -> str:
    """Generate a YouTube video description from scene narrations via LLM."""
    cfg = _load_cfg()
    narrations = "\n".join(
        f"{i+1}. {s.get('narration', '').strip()}"
        for i, s in enumerate(scenes)
        if s.get("narration", "").strip()
    )
    if not narrations:
        return f"A documentary about {title}. Generated by Stephen Spielbot."

    sys_msg = _prompts.system("youtube_description")
    user_msg = _prompts.user(
        "youtube_description",
        title=title or "Untitled",
        narrations=narrations[:3000],
    )
    try:
        return _chat_complete(cfg, sys_msg, user_msg, max_tokens=160, label="youtube_description").strip()
    except Exception as exc:
        logger.warning("generate_youtube_description failed: %s", exc)
        return f"A documentary about {title}. Generated by Stephen Spielbot."


def _parse_tag_list(text: str) -> list[str]:
    """Split an LLM tag reply (comma- or newline-separated) into clean keyword
    strings: strips leading hashes/quotes and list bullets, drops empties and
    over-long items, de-dupes case-insensitively, caps at 15."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,\n]", text or ""):
        t = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", raw).strip().strip("#\"'").strip()
        if not t or len(t) > 60:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= 15:
            break
    return out


def generate_youtube_tags(
    title: str,
    scenes: list[dict],
    style: str = "",
    music_desc: str = "",
) -> list[str]:
    """Generate YouTube keyword tags from scene narrations via LLM.

    Returns an ordered list of topic keyword phrases (the caller adds any
    style/narrator tags). Best-effort: returns [] on any failure."""
    cfg = _load_cfg()
    narrations = "\n".join(
        f"{i+1}. {s.get('narration', '').strip()}"
        for i, s in enumerate(scenes)
        if s.get("narration", "").strip()
    )
    if not narrations:
        return []

    sys_msg = _prompts.system("youtube_tags")
    user_msg = _prompts.user(
        "youtube_tags",
        title=title or "Untitled",
        narrations=narrations[:3000],
    )
    try:
        text = _chat_complete(cfg, sys_msg, user_msg, max_tokens=120, label="youtube_tags")
    except Exception as exc:
        logger.warning("generate_youtube_tags failed: %s", exc)
        return []
    return _parse_tag_list(text)
