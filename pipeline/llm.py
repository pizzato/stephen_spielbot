"""Script generation — supports Claude API and local vLLM backends.

Backend is selected via config:
  llm_backend: "claude" | "local"   (default: "local")
  claude_api_key: "sk-ant-..."       (required when backend is "claude")
  claude_model: "claude-sonnet-4-6"  (optional override)

Claude backend: single call, JSON output, reliable.
Local backend:  two-stage plain-text (story + per-scene visuals), works
                around the reasoning-model token constraints.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger("video_gen")

# ── Local vLLM defaults (overridden by config: local_llm_url / local_llm_model) ─
from pipeline import prompts as _prompts

_LOCAL_LLM_URL_DEFAULT   = "http://localhost:8000/v1/chat/completions"
_LOCAL_LLM_MODEL_DEFAULT = "openai/gpt-oss-120b"

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


# ══════════════════════════════════════════════════════════════════════════════
# Claude backend
# ══════════════════════════════════════════════════════════════════════════════

_CLAUDE_BATCH_SIZE = 10  # max scenes per API call


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


def _fill_empty_narrations(client, model: str, scenes: list[Scene],
                           title: str, video_title: str | None) -> None:
    """For any scene with empty narration, make a targeted Claude call to fill it.

    Mutates the scenes list in place. Prioritises scene 1 which is most likely
    to be left empty by the main generation pass.
    """
    topic = video_title or title
    empty = [s for s in scenes if not (s.narration or "").strip()]
    if not empty:
        return
    logger.warning("Scenes with empty narration after generation: %s — filling", [s.id for s in empty])
    for scene in empty:
        prev_narr = next((s.narration for s in scenes if s.id == scene.id - 1 and s.narration), "")
        next_narr = next((s.narration for s in scenes if s.id == scene.id + 1 and s.narration), "")
        ctx_parts = [f'Topic: "{topic}"', f'Scene {scene.id} title: "{scene.title}"']
        if prev_narr:
            ctx_parts.append(f'Previous scene ends with: "{prev_narr}"')
        if next_narr:
            ctx_parts.append(f'Next scene begins with: "{next_narr}"')
        try:
            narration = _claude_call(
                client, model,
                _prompts.system("script_claude_fill_narration"),
                _prompts.user("script_claude_fill_narration", ctx="\n".join(ctx_parts)),
                max_tokens=120,
                label=f"fill narration scene {scene.id}",
                retries=2,
            ).strip()
            if narration:
                scene.narration = narration
                logger.info("Filled narration for scene %d: %r", scene.id, narration[:60])
        except Exception as exc:
            logger.warning("Could not fill narration for scene %d: %s", scene.id, exc)


def _claude_generate(title: str, n_scenes: int, style_hint: str | None,
                     api_key: str, model: str,
                     video_title: str | None = None) -> tuple[list[Scene], str, str]:
    import anthropic
    import httpx
    # Force HTTP/1.1 — HTTP/2 multiplexed connections get RST_STREAM / GOAWAY
    # from Anthropic's servers after ~3 minutes on large prompts, causing
    # "Server disconnected without sending a response" errors.
    timeout = httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=30.0)
    http_client = httpx.Client(http2=False, timeout=timeout)
    client = anthropic.Anthropic(api_key=api_key, http_client=http_client)

    # ── Batch 1: style + music + first BATCH_SIZE scenes ──────────────────────
    first_batch = min(_CLAUDE_BATCH_SIZE, n_scenes)
    style_note = (
        f'\nIMPORTANT: Use exactly this text for the "style" field: "{style_hint}"'
        if style_hint and style_hint.strip() else ""
    )
    is_last_batch = (first_batch == n_scenes)
    conclusion_note = (
        f"\nIMPORTANT: Scene {n_scenes} is the FINAL scene — deliver a satisfying payoff."
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
        conclusion_note=conclusion_note,
    )
    max_tokens = first_batch * 500 + 600  # 500 tokens/scene headroom + overhead
    raw = _claude_call(client, model, _prompts.system("script_claude_initial"), user_msg, max_tokens, f"scenes 1–{first_batch}")
    outer = _parse_claude_response(raw, f"scenes 1–{first_batch}")

    style      = style_hint.strip() if style_hint and style_hint.strip() else outer.get("style", "")
    music_desc = outer.get("music", "cinematic orchestral background music, atmospheric, instrumental")
    scenes_data = outer.get("scenes", [])
    if not scenes_data:
        raise RuntimeError("Claude returned empty scene list")

    scenes = [
        Scene(
            id=item.get("id", i + 1),
            title=item.get("title", f"Scene {i + 1}"),
            image_prompt=item.get("image_prompt", title),
            video_prompt=item.get("video_prompt", item.get("image_prompt", title)),
            narration=item.get("narration", ""),
        )
        for i, item in enumerate(scenes_data[:first_batch])
    ]

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
            f"\nIMPORTANT: Scene {batch_end} is the FINAL scene — deliver a satisfying payoff."
            if is_last else ""
        )
        cont_msg = _prompts.user(
            "script_claude_continuation",
            n_scenes=n_scenes,
            topic_ref=topic_ref,
            batch_start=batch_start,
            batch_end=batch_end,
            ctx_str=ctx_str,
            conclusion_note=conclusion_note,
        )
        max_tokens = (batch_end - batch_start + 1) * 350 + 300
        raw = _claude_call(client, model, _prompts.system("script_claude_continuation"), cont_msg,
                           max_tokens, f"scenes {batch_start}–{batch_end}")
        items = _parse_claude_response(raw, f"scenes {batch_start}–{batch_end}")
        if not isinstance(items, list):
            items = items.get("scenes", [])
        for i, item in enumerate(items):
            scenes.append(Scene(
                id=batch_start + i,
                title=item.get("title", f"Scene {batch_start + i}"),
                image_prompt=item.get("image_prompt", title),
                video_prompt=item.get("video_prompt", item.get("image_prompt", title)),
                narration=item.get("narration", ""),
            ))
        batch_start = batch_end + 1

    final_scenes = scenes[:n_scenes]
    _fill_empty_narrations(client, model, final_scenes, title, video_title)
    # Absolute last-resort safety net: no Scene leaves with empty narration.
    for s in final_scenes:
        if not (s.narration or "").strip():
            s.narration = f"{s.title or f'Scene {s.id}'}."
            logger.warning("Scene %d still empty after Claude fill — used title", s.id)
    return final_scenes, music_desc, style


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
    payload = json.dumps({
        "model":       model,
        "messages":    messages,
        "temperature": 0.7,
        "max_tokens":  max_tokens,
    }).encode()

    last_err: Exception | None = None
    for attempt in range(1 + retries):
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as e:
            last_err = RuntimeError(f"LLM request failed: {e}")
            logger.warning("Local LLM attempt %d/%d failed: %s", attempt + 1, 1 + retries, e)
            continue

        choice  = data["choices"][0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        if content.strip():
            logger.debug("local LLM finish_reason=%s", choice.get("finish_reason"))
            return content

        last_err = RuntimeError(
            f"Local LLM empty content (finish_reason={choice.get('finish_reason')})"
        )
        logger.warning("Local LLM attempt %d/%d empty: %s",
                       attempt + 1, 1 + retries, choice.get("finish_reason"))

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
                          video_title: str | None = None) -> dict:
    style_note = (
        f"\nIMPORTANT: Use exactly this text for the STYLE line: {style_hint}"
        if style_hint and style_hint.strip()
        else ""
    )
    title_context = (
        f'YouTube Video Title: "{video_title}"\nTopic/Description: "{title}"'
        if video_title and video_title.strip()
        else f'the topic: "{title}"'
    )
    user_msg = _prompts.user(
        "script_local_story",
        n_scenes=n_scenes,
        title_context=title_context,
        style_note=style_note,
    )
    raw = _local_llm(
        [
            {"role": "system", "content": _prompts.system("script_local_story")},
            {"role": "user",   "content": user_msg},
        ],
        max_tokens=4096 + n_scenes * 150,
        url=url, model=model,
    )
    logger.debug("Story raw (%d chars):\n%s", len(raw), raw[:800])
    style  = _get_field(raw, "STYLE")
    music  = _get_field(raw, "MUSIC")
    scenes = []
    for i in range(1, n_scenes + 1):
        title_val = _get_field(raw, f"TITLE_{i}")
        narr_val  = _get_field(raw, f"NARRATION_{i}")
        if title_val or narr_val:
            scenes.append({"id": i, "title": title_val, "narration": narr_val})
    result = {"style": style, "music": music, "scenes": scenes}
    if not scenes:
        raise RuntimeError(f"Story call returned no scenes.\nRaw response:\n{raw[:600]}")
    return result


def _fill_empty_outlines_local(outlines: list[dict], title: str, video_title: str | None,
                                 url: str, model: str) -> None:
    """Fill any outline dicts whose narration is empty via a targeted local-LLM call.

    Operates BEFORE visual generation so that downstream image/video prompts have
    proper narration context. Mutates the outlines list in place.
    """
    topic = video_title or title
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
                     "content": _prompts.user("script_local_fill_narration", ctx="\n".join(ctx_parts))},
                ],
                max_tokens=200,
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
                            url: str, model: str) -> tuple[str, str]:
    user_msg = _prompts.user(
        "script_local_visual",
        title=title,
        style=style,
        scene_title=scene_title,
        narration=narration,
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
                    video_title: str | None = None) -> tuple[list[Scene], str, str]:
    cfg   = _load_cfg()
    url   = cfg.get("local_llm_url",   _LOCAL_LLM_URL_DEFAULT)
    model = cfg.get("local_llm_model", _LOCAL_LLM_MODEL_DEFAULT)

    if not _check_local_available(url):
        raise RuntimeError(
            f"Local LLM at {url} is not reachable.\n"
            "Set the URL in Config → LLM Backend → Local LLM URL."
        )

    story      = _local_generate_story(title, n_scenes, style_hint, url, model, video_title=video_title)
    style      = (style_hint.strip() if style_hint and style_hint.strip()
                  else story.get("style", ""))
    music_desc = story.get("music", "cinematic orchestral background music, atmospheric, instrumental")
    outlines   = story["scenes"]

    # Critical: fill any empty narrations BEFORE visual generation so the image/video
    # prompts get proper context. Scene 1 is particularly prone to being left blank.
    _fill_empty_outlines_local(outlines, title, video_title, url, model)

    logger.info("Story: %d scenes, style=%r", len(outlines), style)

    def _fetch(outline: dict) -> tuple[int, str, str]:
        img_p, vid_p = _local_generate_visual(
            title, style,
            outline["id"],
            outline.get("title", f"Scene {outline['id']}"),
            outline.get("narration", ""),
            url=url, model=model,
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

    return scenes, music_desc, style


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def generate_script(
    title: str,
    n_scenes: int,
    style_hint: str | None = None,
    video_title: str | None = None,
) -> tuple[list[Scene], str, str]:
    """Return (scenes, music_description, style).

    Backend is chosen from config: llm_backend = "claude" | "local".
    video_title is the short YouTube title; title is the full topic/description.
    """
    cfg     = _load_cfg()
    backend = cfg.get("llm_backend", "local")

    if backend == "claude":
        api_key = cfg.get("claude_api_key", "")
        if not api_key:
            raise RuntimeError(
                'Claude API key not set. Add it in the Config tab under "LLM Backend".'
            )
        model = cfg.get("claude_model", "claude-sonnet-4-6")
        logger.info("Using Claude backend: model=%s", model)
        return _claude_generate(title, n_scenes, style_hint, api_key, model, video_title=video_title)

    logger.info("Using local vLLM backend")
    return _local_generate(title, n_scenes, style_hint, video_title=video_title)


# ── YouTube video prompt generation (director's brief) ───────────────────────

def generate_video_prompt(title: str, comment: str) -> str:  # noqa: ARG001
    """Generate a directorial style brief (how to make it, not what it covers)."""
    cfg = _load_cfg()
    # Note: comment is intentionally ignored — the brief must be topic-agnostic.
    sys_msg = _prompts.system("director_brief")
    user_msg = _prompts.user("director_brief", title=title or "Untitled")
    backend = cfg.get("llm_backend", "local")
    try:
        if backend == "claude":
            api_key = cfg.get("claude_api_key", "")
            if not api_key:
                raise RuntimeError("No Claude API key configured")
            import anthropic, httpx
            timeout = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=30.0)
            client = anthropic.Anthropic(api_key=api_key, http_client=httpx.Client(http2=False, timeout=timeout))
            text = _claude_call(
                client,
                cfg.get("claude_model", "claude-sonnet-4-6"),
                sys_msg,
                user_msg,
                max_tokens=200,
                label="video_prompt",
            )
            return text.strip()
        url = cfg.get("local_llm_url", _LOCAL_LLM_URL_DEFAULT)
        model = cfg.get("local_llm_model", _LOCAL_LLM_MODEL_DEFAULT)
        return _local_llm(
            [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
            max_tokens=200,
            url=url,
            model=model,
        ).strip()
    except Exception as exc:
        logger.warning("generate_video_prompt failed: %s", exc)
        return ""  # empty string — caller should show Create tab without a pre-filled prompt


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


def generate_video_suggestions(previous_titles: list[str], cfg: dict | None = None) -> list[dict]:
    """Generate 5 video topic suggestions complementary to the channel's existing content.

    Returns a list of dicts with keys: title, reason, interestingness.
    """
    if cfg is None:
        cfg = _load_cfg()
    titles_list = (
        "\n".join(f'- "{t}"' for t in previous_titles)
        if previous_titles
        else "(no previous videos yet — suggest a varied starting set)"
    )
    sys_msg = _prompts.system("video_suggestions")
    user_msg = _prompts.user("video_suggestions", titles_list=titles_list)
    backend = cfg.get("llm_backend", "local")
    try:
        if backend == "claude":
            api_key = cfg.get("claude_api_key", "")
            if not api_key:
                raise RuntimeError("No Claude API key configured")
            import anthropic
            import httpx
            timeout = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=30.0)
            client = anthropic.Anthropic(
                api_key=api_key, http_client=httpx.Client(http2=False, timeout=timeout)
            )
            text = _claude_call(
                client,
                cfg.get("claude_model", "claude-sonnet-4-6"),
                sys_msg,
                user_msg,
                max_tokens=700,
                label="video_suggestions",
            )
            return _parse_suggestions(text)
        # Local backend
        url = cfg.get("local_llm_url", _LOCAL_LLM_URL_DEFAULT)
        model = cfg.get("local_llm_model", _LOCAL_LLM_MODEL_DEFAULT)
        text = _local_llm(
            [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=700,
            url=url,
            model=model,
        )
        return _parse_suggestions(text)
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
    backend = cfg.get("llm_backend", "local")
    try:
        if backend == "claude":
            api_key = cfg.get("claude_api_key", "")
            if not api_key:
                raise RuntimeError("No Claude API key configured")
            import anthropic, httpx
            timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=30.0)
            client = anthropic.Anthropic(api_key=api_key, http_client=httpx.Client(http2=False, timeout=timeout))
            text = _claude_call(
                client,
                cfg.get("claude_model", "claude-sonnet-4-6"),
                sys_msg,
                user_msg,
                max_tokens=160,
                label="youtube_description",
            )
            return text.strip()
        # Local backend
        url = cfg.get("local_llm_url", _LOCAL_LLM_URL_DEFAULT)
        model = cfg.get("local_llm_model", _LOCAL_LLM_MODEL_DEFAULT)
        return _local_llm(
            [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
            max_tokens=160,
            url=url,
            model=model,
        ).strip()
    except Exception as exc:
        logger.warning("generate_youtube_description failed: %s", exc)
        return f"A documentary about {title}. Generated by Stephen Spielbot."
