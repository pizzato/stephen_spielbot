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

logger = logging.getLogger("video_gen")

# ── Local vLLM defaults (overridden by config: local_llm_url / local_llm_model) ─
_LOCAL_LLM_URL_DEFAULT   = "http://localhost:8000/v1/chat/completions"
_LOCAL_LLM_MODEL_DEFAULT = "openai/gpt-oss-120b"

NEGATIVE_PROMPT = (
    "low quality, worst quality, blurry, static, frozen frame, no motion, jittery, "
    "flickering, strobing, text, watermark, logo, subtitles, distorted, pixelated, "
    "overexposed, underexposed, grainy, washed out colors, inconsistent lighting, "
    "camera shake, jump cut, unnatural motion, morphing faces, extra limbs, "
    "3D CGI look, cartoon, anime"
)


# ── Config loader (avoids circular import with app.py) ────────────────────────
_CONFIG_FILE = Path.home() / ".config" / "video-generator" / "config.json"

def _load_cfg() -> dict:
    try:
        return json.loads(_CONFIG_FILE.read_text())
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

_CLAUDE_SYSTEM = """You are a video script writer specialising in YouTube documentary content. Given a topic title, generate a structured video script optimised for viewer retention.

Output ONLY a JSON object with exactly three keys:

"style": A 15-30 word visual style sentence applied consistently across ALL scenes. This will be PREPENDED to every image prompt, so write it as a complete descriptive phrase that sets the visual tone. Include: camera style, color grade, lighting quality, film stock or rendering style. Example: "Cinematic 35mm film, warm desaturated golden tones, shallow depth of field, photorealistic documentary style"

"music": A 20-40 word description of the background music for the ENTIRE video. Reflect the emotional arc — tense moments need suspenseful music, triumphant moments need bold brass. Include: mood adjectives, tempo, key instruments, genre.

"scenes": Array of scene objects, each with:
  - "id": integer starting from 1
  - "title": 5-10 word scene title
  - "image_prompt": 60-100 word highly detailed STATIC image description for FLUX image generation. Describe this as a frozen, perfectly composed photograph. Include: exact subjects and their positions, environment and setting details, lighting quality and direction, color palette, textures and materials, atmospheric depth. Do NOT include any motion, camera movement, or action verbs. Written as flowing sentences.
  - "video_prompt": 30-50 word description of HOW the scene moves for the LTX video model. Include: what subjects/objects move and how, explicit camera movement (slow dolly forward, aerial descent, gentle pan left, static wide angle, etc.), pacing and rhythm. Do NOT include style descriptors (those are prepended automatically). Match the action described in the narration.
  - "narration": spoken narration — exactly 2 sentences, approximately 9 seconds when read aloud at a calm documentary pace (roughly 18-22 words total). Clear, engaging, educational. Written as part of a coherent flowing story.

STRUCTURE RULES — critical for YouTube retention:
- Scene 1 (introduction): Open with a compelling hook that makes the viewer feel they are about to discover something remarkable. Tease the most surprising or emotionally resonant moment from later in the video — a question left unanswered, a tension not yet resolved, or a revelation hinted at but withheld. The goal is to create an irresistible reason to keep watching.
- Middle scenes: Build the narrative progressively, each scene raising the stakes or deepening understanding. Every narration should end with an implicit pull toward the next scene.
- Final scene (conclusion): Land with a satisfying payoff — a moment of insight, wonder, or emotional resolution that makes the viewer feel their time was well spent. Leave them with something worth remembering or sharing.

Write the narrations as a single continuous narrative. The arc must feel purposeful: curiosity ignited → journey deepened → reward delivered.

Output only the raw JSON object. No markdown, no code fences, no explanation."""

_CLAUDE_CONTINUATION_SYSTEM = """You are a video script writer specialising in YouTube documentary content, continuing a multi-part script in progress.

Output ONLY a JSON array of scene objects. Each object has exactly these keys:
  - "id": integer scene number (as specified in the request)
  - "title": 5-10 word scene title
  - "image_prompt": 60-100 word highly detailed STATIC image description for FLUX. Frozen, perfectly composed photograph. Include: subjects and positions, environment, lighting, colour palette, textures, atmosphere. NO motion, camera movement, or action verbs.
  - "video_prompt": 30-50 word motion description for LTX. Subject actions, explicit camera movement (slow dolly forward, aerial descent, gentle pan left, etc.), pacing. No style descriptors.
  - "narration": exactly 2 sentences, ~9 seconds when read aloud at a calm documentary pace (~18-22 words). Must flow naturally from the previous scenes provided as context.

Output only the raw JSON array. No markdown, no code fences, no explanation."""

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
                 max_tokens: int, label: str) -> str:
    response = client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Claude hit the token limit ({max_tokens}) for {label}. "
            "Try fewer scenes or a shorter topic."
        )
    return response.content[0].text.strip()


def _claude_generate(title: str, n_scenes: int, style_hint: str | None,
                     api_key: str, model: str) -> tuple[list[Scene], str, str]:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

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
    user_msg = (
        f'Generate a {n_scenes}-scene video script for the topic: "{title}"\n'
        f"Output exactly {first_batch} scenes, numbered 1 to {first_batch}.{style_note}{conclusion_note}"
    )
    max_tokens = first_batch * 350 + 500
    raw = _claude_call(client, model, _CLAUDE_SYSTEM, user_msg, max_tokens, f"scenes 1–{first_batch}")
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
        cont_msg = (
            f'Continue the {n_scenes}-scene video script for: "{title}"\n'
            f"Generate scenes {batch_start} to {batch_end} "
            f"(scene IDs {batch_start}–{batch_end}).\n\n"
            f"Previous scenes for narrative continuity:\n{ctx_str}{conclusion_note}"
        )
        max_tokens = (batch_end - batch_start + 1) * 350 + 300
        raw = _claude_call(client, model, _CLAUDE_CONTINUATION_SYSTEM, cont_msg,
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

    return scenes[:n_scenes], music_desc, style


# ══════════════════════════════════════════════════════════════════════════════
# Local vLLM backend (plain-text, two-stage)
# ══════════════════════════════════════════════════════════════════════════════

_STORY_SYSTEM = """\
You are a video script writer specialising in YouTube documentary content. Write a complete narrated story for an AI-generated documentary video, optimised for viewer retention.

Output ONLY plain text using this exact format — no JSON, no markdown, no extra commentary:

STYLE: [15-30 word visual style phrase that will be prepended to every visual prompt. Camera style, color grade, lighting quality, film stock. Example: "Cinematic 35mm film, warm desaturated golden tones, shallow depth of field, slow motion, photorealistic documentary style"]
MUSIC: [20-40 word music description: mood, tempo, key instruments, genre, emotional arc]
TITLE_1: [5-10 word scene title]
NARRATION_1: [exactly 2 sentences, approximately 9 seconds when read aloud at a calm documentary pace, roughly 18-22 words total]
TITLE_2: [5-10 word scene title]
NARRATION_2: [exactly 2 sentences, approximately 9 seconds when read aloud at a calm documentary pace, roughly 18-22 words total]
...continue for all N scenes

Rules:
- SCENE 1 must open with a compelling hook: tease the most surprising or emotionally resonant moment from later in the video — a question unanswered, a tension unresolved, or a revelation hinted at but withheld. Make the listener feel they cannot afford to stop watching.
- MIDDLE SCENES build the narrative progressively, deepening understanding and raising stakes. Each narration should carry an implicit pull toward the next scene.
- FINAL SCENE must deliver a satisfying payoff — insight, wonder, or emotional resolution that rewards the viewer for staying. Leave them with something memorable or worth sharing.
- Write narrations as a single continuous narrative: curiosity ignited → journey deepened → reward delivered.
- Keep narrations short: exactly 2 sentences, ~9 seconds when spoken calmly.
- Every value must fit on a single line (no line breaks within a value).
- No blank lines between entries.
- No text outside the key: value lines."""

_VISUAL_SYSTEM = """\
You are generating two descriptions for an AI video pipeline. The pipeline has two stages:
1. FLUX image generator — needs a highly detailed static image description (no motion).
2. LTX I2V video model — needs a motion/camera description that starts from the FLUX image.

The visual style (camera style, color grade, film stock) will be automatically prepended — do NOT include it in either field.

Output ONLY plain text in this exact two-line format (each on a single line, no line breaks within a value):

IMAGE: [60-100 word highly detailed static image description for FLUX. Describe this as a frozen, perfectly composed photograph. Include exact subjects and positions, environment details, lighting quality and direction, color palette, textures, atmospheric depth. NO motion words, NO camera movement words, NO action verbs.]
VIDEO: [30-50 word motion description for LTX I2V. What moves and how (camera pushes forward, mist drifts, leaves rustle), explicit camera movement (slow dolly, aerial descent, gentle pan, static wide angle), pacing. Match the narration's action. No style descriptors.]"""


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
    m = re.search(rf"^{re.escape(key)}:\s*(.+)", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _local_generate_story(title: str, n_scenes: int, style_hint: str | None,
                          url: str, model: str) -> dict:
    style_note = (
        f"\nIMPORTANT: Use exactly this text for the STYLE line: {style_hint}"
        if style_hint and style_hint.strip()
        else ""
    )
    user_msg = (
        f'Write a {n_scenes}-scene narrated story for the topic: "{title}"\n'
        f"Output exactly {n_scenes} scenes using the TITLE_N / NARRATION_N pattern.{style_note}"
    )
    raw = _local_llm(
        [
            {"role": "system", "content": _STORY_SYSTEM},
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


def _local_generate_visual(title: str, style: str,
                            scene_id: int, scene_title: str, narration: str,
                            url: str, model: str) -> tuple[str, str]:
    user_msg = (
        f'Video topic: "{title}"\n'
        f"Visual style: {style}\n\n"
        f"Scene: {scene_title}\n"
        f"Narration: {narration}\n\n"
        "Generate the IMAGE and VIDEO lines for this scene."
    )
    raw = _local_llm(
        [
            {"role": "system", "content": _VISUAL_SYSTEM},
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
                    style_hint: str | None) -> tuple[list[Scene], str, str]:
    cfg   = _load_cfg()
    url   = cfg.get("local_llm_url",   _LOCAL_LLM_URL_DEFAULT)
    model = cfg.get("local_llm_model", _LOCAL_LLM_MODEL_DEFAULT)

    if not _check_local_available(url):
        raise RuntimeError(
            f"Local LLM at {url} is not reachable.\n"
            "Set the URL in Config → LLM Backend → Local LLM URL."
        )

    story      = _local_generate_story(title, n_scenes, style_hint, url, model)
    style      = (style_hint.strip() if style_hint and style_hint.strip()
                  else story.get("style", ""))
    music_desc = story.get("music", "cinematic orchestral background music, atmospheric, instrumental")
    outlines   = story["scenes"]

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
    return scenes, music_desc, style


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def generate_script(
    title: str,
    n_scenes: int,
    style_hint: str | None = None,
) -> tuple[list[Scene], str, str]:
    """Return (scenes, music_description, style).

    Backend is chosen from config: llm_backend = "claude" | "local".
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
        return _claude_generate(title, n_scenes, style_hint, api_key, model)

    logger.info("Using local vLLM backend")
    return _local_generate(title, n_scenes, style_hint)
