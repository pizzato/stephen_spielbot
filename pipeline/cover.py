"""YouTube cover image generation utilities (shared between app.py and resume_generation.py)."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("video_gen")

COVER_WIDTH  = 1280
COVER_HEIGHT = 720


def build_cover_prompt(title: str, style: str = "") -> str:
    """Build a FLUX prompt for a YouTube documentary cover image."""
    style_clean = style.strip().rstrip(".")
    suffix = "cinematic key art, compelling composition, high contrast, dramatic lighting"
    if style_clean:
        return (
            f"{style_clean}. YouTube video thumbnail cover art for a documentary "
            f"titled '{title}', {suffix}"
        )
    return (
        f"YouTube video thumbnail cover art for a documentary titled '{title}', {suffix}"
    )


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
