"""YouTube cover image generation utilities (shared between app.py and resume_generation.py)."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("video_gen")

COVER_WIDTH  = 1280
COVER_HEIGHT = 720


_COVER_NEGATIVE = (
    "blurry, low resolution, bad text, misspelled words, extra letters, unreadable title, "
    "random text, watermark, logo, messy layout, too much clutter, distorted faces, "
    "deformed hands, boring flat lighting, plain background"
)


def shorten_title_for_cover(title: str, max_chars: int = 40) -> str:
    """Return a thumbnail-friendly title: drop subtitle after ':' or '—', then cap length."""
    for sep in (":", "—", " - "):
        if sep in title:
            title = title.split(sep, 1)[0].strip()
            break
    if len(title) <= max_chars:
        return title
    # truncate at last word boundary within max_chars
    truncated = title[:max_chars].rsplit(" ", 1)[0].rstrip(",;")
    return truncated


_STYLE_KEYWORDS = (
    "cinematic", "film grain", "depth of field", "color grade", "photorealistic",
    "documentary texture", "lighting quality", "16mm", "35mm", "8mm", "film stock",
    "lens", "anamorphic", "desaturated", "saturated", "tones", "color palette",
)


def _strip_style_prefix(image_prompt: str) -> str:
    """Drop the leading style sentence (shared across scenes) from an image_prompt.

    Image prompts typically open with the visual-style boilerplate (e.g.
    "Cinematic 16mm film grain, deep blacks...") which is identical for every
    scene in a video. The actual scene-specific content (subject, setting,
    composition) follows after the first sentence-ending period.
    """
    text = (image_prompt or "").strip()
    if not text:
        return ""
    # If the first sentence reads like a style declaration, drop it.
    parts = text.split(".", 1)
    if len(parts) == 2:
        first = parts[0].lower()
        if any(kw in first for kw in _STYLE_KEYWORDS):
            return parts[1].strip()
    return text


def _extract_scene_aspects(scenes) -> str:
    """Pull key visual elements from a few representative scenes for the cover prompt.

    Picks first, middle, and last scenes (the narrative arc), strips the shared
    style boilerplate from each image_prompt, and keeps the subject/setting text
    so the cover composition reflects the actual video content.
    """
    if not scenes:
        return ""
    items = list(scenes)
    n = len(items)
    if n >= 3:
        indices = [0, n // 2, n - 1]
    else:
        indices = list(range(n))
    snippets: list[str] = []
    seen: set[str] = set()
    for i in indices:
        s = items[i]
        ip_raw = (s.get("image_prompt") if isinstance(s, dict) else getattr(s, "image_prompt", "")) or ""
        ip = _strip_style_prefix(ip_raw)
        if len(ip) < 20:
            # Fall back to scene title if the prompt is empty/too short.
            ip = (s.get("title") if isinstance(s, dict) else getattr(s, "title", "")) or ""
            ip = ip.strip()
            if not ip:
                continue
        # Trim to ~220 chars at a word boundary to keep the cover prompt concise.
        snippet = ip[:220]
        if len(ip) > 220:
            snippet = snippet.rsplit(" ", 1)[0]
        key = snippet[:60].lower()
        if key in seen:
            continue
        seen.add(key)
        snippets.append(snippet)
    return " | ".join(snippets[:3])


def build_cover_prompt(title: str, style: str = "", scenes=None) -> str:
    """Build a FLUX prompt for a YouTube thumbnail derived from the video's scenes.

    scenes: optional iterable of Scene objects or dicts with `image_prompt`. When
            provided, the thumbnail SUBJECT MATTER is taken directly from these
            scenes — no topic-biasing words like "historical" appear in the prompt,
            so a music video gets musicians, a tech video gets tech, etc.
    """
    style_note = style.strip().rstrip(".")
    aspects = _extract_scene_aspects(scenes)

    # When we have real scene content, lead with it (FLUX weights early tokens
    # heavily). When we don't, fall back to a generic title-led brief.
    if aspects:
        subject_line = (
            f"Subject matter (MUST be the dominant content of the image — combine these "
            f"specific elements into one cohesive composition): {aspects}. "
        )
        topic_brief = ""
    else:
        subject_line = ""
        topic_brief = (
            f'Subject matter: imagery directly representing the topic "{title}". '
        )

    style_line = f"Visual style: {style_note}. " if style_note else ""

    prompt = (
        f"{subject_line}"
        f"{topic_brief}"
        f"Render this as a YouTube thumbnail in 16:9 landscape format. "
        f"{style_line}"
        f"Thumbnail look: dramatic cinematic lighting, ultra-detailed, bold contrast, "
        f"rich colors, sharp focus, high resolution, professional YouTube thumbnail design. "
        f"Composition: the subject matter above is the main image, filling most of the frame. "
        f"Large readable title text overlays the lower or central area without obscuring "
        f"the key subjects. Use dramatic lighting, atmosphere, and depth to make it eye-catching. "
        f'Title text: spell exactly "{title}", large, bold, clean block lettering with strong '
        f"shadow or outline. No extra words, no fake letters, no misspellings. "
        f"Layout: keep the subjects from the video clearly visible, leave safe margins, "
        f"no watermark, no logo, no UI elements. "
        f"Avoid: {_COVER_NEGATIVE}, historical war imagery unless explicitly described above, "
        f"random soldiers, random period costumes, generic stock imagery unrelated to the subject matter."
    )
    return prompt


def overlay_title_on_image(base_path: Path, output_path: Path, title: str) -> None:
    """Overlay video title text on a cover image using PIL."""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap

    img = Image.open(base_path).convert("RGBA")
    width, height = img.size

    # Dark gradient overlay at the bottom half
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    for y in range(height // 2, height):
        alpha = int(200 * (y - height // 2) / (height // 2))
        draw_ov.rectangle([0, y, width, y + 1], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)
    font_size = max(52, width // 18)
    font = None
    for font_path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            font = ImageFont.truetype(font_path, font_size)
            break
        except (OSError, IOError):
            pass
    if font is None:
        font = ImageFont.load_default()

    max_chars = max(10, int(width / (font_size * 0.55)))
    lines = textwrap.wrap(title, width=max_chars)
    line_height = font_size + 12
    total_h = len(lines) * line_height
    y = height - total_h - 48

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        x = (width - tw) // 2
        # Shadow
        draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0, 200))
        # White text
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height

    img.convert("RGB").save(str(output_path), "PNG")
