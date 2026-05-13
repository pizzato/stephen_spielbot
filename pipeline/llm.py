"""Script generation — supports Claude API and local vLLM backends.

Backend is selected via config:
  llm_backend: "claude" | "local"   (default: "local")
  claude_api_key: "sk-ant-..."       (required when backend is "claude")
  claude_model: "claude-sonnet-4-6"  (optional override)

Claude backend: single call, JSON output, reliable.
Local backend:  two-stage plain-text (story + per-scene visuals), works
                around the reasoning-model token constraints.
"""

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
    visual_prompt: str
    narration: str
    negative_prompt: str = NEGATIVE_PROMPT


# ══════════════════════════════════════════════════════════════════════════════
# Claude backend
# ══════════════════════════════════════════════════════════════════════════════

_CLAUDE_SYSTEM = """You are a video script writer. Given a topic title, generate a structured video script.

Output ONLY a JSON object with exactly three keys:

"style": A 15-30 word visual style sentence applied consistently across ALL scenes. This will be PREPENDED to every visual prompt, so write it as a complete descriptive phrase that sets the visual tone. Include: camera style, color grade, lighting quality, film stock or rendering style. Example: "Cinematic 35mm film, warm desaturated golden tones, shallow depth of field, slow motion, photorealistic documentary style"

"music": A 20-40 word description of the background music for the ENTIRE video. Reflect the emotional arc — tense moments need suspenseful music, triumphant moments need bold brass. Include: mood adjectives, tempo, key instruments, genre.

"scenes": Array of scene objects, each with:
  - "id": integer starting from 1
  - "title": 5-10 word scene title
  - "visual_prompt": 50-80 word cinematic description written as flowing sentences (NOT a keyword list). This drives an AI video model that understands natural language. You MUST include ALL five elements:
      1. Subject/setting — what is shown and where
      2. Motion/action — what moves, how it moves (this is the most important element — static descriptions produce static video)
      3. Camera — explicit camera movement (slow dolly forward, aerial tracking shot, static wide angle, gentle pan left, etc.)
      4. Lighting — specific and evocative (golden afternoon light, cool diffused overcast, warm candlelight, neon reflections)
      5. Atmosphere — particles, mist, air quality, sound cues that enhance mood (dust motes in shafts of light, mist drifts through the scene, distant sounds implied by the visuals)
    No people unless essential to the story. No text or logos. Do NOT include style descriptors (those come from the style field). Match the visual to what the narration describes.
  - "narration": spoken narration — exactly 2 sentences, approximately 9 seconds when read aloud at a calm documentary pace (roughly 18-22 words total). Clear, engaging, educational. Written as part of a coherent flowing story.

Write the narrations as a continuous narrative — each scene follows naturally from the last.

Output only the raw JSON object. No markdown, no code fences, no explanation."""


def _claude_generate(title: str, n_scenes: int, style_hint: str | None,
                     api_key: str, model: str) -> tuple[list[Scene], str, str]:
    import anthropic

    style_note = (
        f'\nIMPORTANT: Use exactly this text for the "style" field: "{style_hint}"'
        if style_hint and style_hint.strip()
        else ""
    )
    user_msg = (
        f'Generate a {n_scenes}-scene video script for the topic: "{title}"\n'
        f"Output exactly {n_scenes} scenes.{style_note}"
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=8096,
        system=_CLAUDE_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    content = response.content[0].text.strip()

    # Strip markdown fences if present
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    # Remove trailing commas
    content = re.sub(r",\s*([}\]])", r"\1", content)

    try:
        outer = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude returned invalid JSON: {e}\nContent: {content[:500]}")

    style      = (style_hint.strip() if style_hint and style_hint.strip()
                  else outer.get("style", ""))
    music_desc = outer.get("music", "cinematic orchestral background music, atmospheric, instrumental")
    scenes_data = outer.get("scenes", [])

    if not scenes_data:
        raise RuntimeError("Claude returned empty scene list")

    scenes = [
        Scene(
            id=item.get("id", i + 1),
            title=item.get("title", f"Scene {i + 1}"),
            visual_prompt=item.get("visual_prompt", title),
            narration=item.get("narration", ""),
        )
        for i, item in enumerate(scenes_data[:n_scenes])
    ]
    return scenes, music_desc, style


# ══════════════════════════════════════════════════════════════════════════════
# Local vLLM backend (plain-text, two-stage)
# ══════════════════════════════════════════════════════════════════════════════

_STORY_SYSTEM = """\
You are a video script writer. Write a complete narrated story for an AI-generated documentary video.

Output ONLY plain text using this exact format — no JSON, no markdown, no extra commentary:

STYLE: [15-30 word visual style phrase that will be prepended to every visual prompt. Camera style, color grade, lighting quality, film stock. Example: "Cinematic 35mm film, warm desaturated golden tones, shallow depth of field, slow motion, photorealistic documentary style"]
MUSIC: [20-40 word music description: mood, tempo, key instruments, genre, emotional arc]
TITLE_1: [5-10 word scene title]
NARRATION_1: [exactly 2 sentences, approximately 9 seconds when read aloud at a calm documentary pace, roughly 18-22 words total]
TITLE_2: [5-10 word scene title]
NARRATION_2: [exactly 2 sentences, approximately 9 seconds when read aloud at a calm documentary pace, roughly 18-22 words total]
...continue for all N scenes

Rules:
- Write narrations as a coherent flowing story — each paragraph follows naturally from the last.
- Keep narrations short: exactly 2 sentences, ~9 seconds when spoken calmly.
- Every value must fit on a single line (no line breaks within a value).
- No blank lines between entries.
- No text outside the key: value lines."""

_VISUAL_SYSTEM = """\
You are a cinematographer writing a visual description for an AI video generation model (LTX 2.3).
The model reads full sentences and generates motion based on what you describe. Static or vague descriptions produce static video.
The visual style (camera style, color grade, film stock) will be automatically prepended — do NOT include it.

Output ONLY plain text in this exact single-line format:

VISUAL: [50-80 word description written as flowing sentences. You MUST include: (1) subject and setting, (2) what moves and how — this is the most important element, be explicit about motion (camera pushes forward, mist drifts between columns, leaves rustle), (3) explicit camera movement (slow dolly, aerial descent, static wide angle, gentle pan), (4) specific lighting quality (golden afternoon light, diffused overcast, flickering candlelight), (5) atmosphere or particles (mist drifts, dust motes float, steam rises). No people unless essential. No text or logos. Match the narration's subject.]"""


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
                            url: str, model: str) -> str:
    user_msg = (
        f'Video topic: "{title}"\n'
        f"Visual style: {style}\n\n"
        f"Scene: {scene_title}\n"
        f"Narration: {narration}\n\n"
        "Generate the VISUAL line for this scene."
    )
    raw = _local_llm(
        [
            {"role": "system", "content": _VISUAL_SYSTEM},
            {"role": "user",   "content": user_msg},
        ],
        max_tokens=2048,
        url=url, model=model,
    )
    visual = _get_field(raw, "VISUAL")
    return visual or raw.strip().lstrip("VISUAL:").strip()


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

    def _fetch(outline: dict) -> tuple[int, str]:
        return outline["id"], _local_generate_visual(
            title, style,
            outline["id"],
            outline.get("title", f"Scene {outline['id']}"),
            outline.get("narration", ""),
            url=url, model=model,
        )

    visuals: dict[int, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for sid, visual in pool.map(_fetch, outlines):
            visuals[sid] = visual

    scenes = [
        Scene(
            id=o["id"],
            title=o.get("title", f"Scene {o['id']}"),
            visual_prompt=visuals.get(o["id"], title),
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
