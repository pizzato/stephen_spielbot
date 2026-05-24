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


def build_cover_prompt(title: str, style: str = "") -> str:
    """Build a FLUX prompt for a YouTube documentary cover image."""
    style_note = style.strip().rstrip(".")
    style_line = f"Video visual style: {style_note}. " if style_note else ""
    prompt = (
        f"Create a high-impact YouTube documentary thumbnail in 16:9 landscape format. "
        f"{style_line}"
        f"Thumbnail style: cinematic historical montage, dramatic lighting, ultra-detailed, bold contrast, "
        f"rich colors, professional YouTube thumbnail design, epic documentary poster look, "
        f"sharp focus, high resolution. "
        f"Composition: large readable title text dominates the centre, with supporting "
        f"historical/subject imagery arranged around it in a dramatic collage. Use depth, "
        f"smoke, light rays, clouds, sparks, maps, symbols, or atmosphere where appropriate. "
        f"The image should look exciting, educational, and clickable at small YouTube size. "
        f'Text: include the exact title: "{title}". '
        f"The title must be spelled correctly, large, clean, bold, and easy to read. "
        f"Use thick block lettering with strong shadow or outline. Do not add any extra words, "
        f"fake letters, random symbols, or misspelled text. "
        f"Visual content: show the key eras, people, objects, places, and technologies related "
        f"to the topic. Make the image feel like a complete visual summary of the story. "
        f"Layout: avoid clutter, keep the subject clear, make the title readable first, then "
        f"the background details. Leave safe margins around the edges. No watermark, no logo, "
        f"no UI elements. "
        f"Avoid: {_COVER_NEGATIVE}."
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
