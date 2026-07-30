"""YouTube cover image generation utilities (shared between app.py and resume_generation.py)."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from pipeline import prompts as _prompts

logger = logging.getLogger("video_gen")

COVER_WIDTH  = 1280
COVER_HEIGHT = 720


def cover_dimensions(vid_width: int, vid_height: int) -> tuple[int, int]:
    """Cover dimensions matching the video's aspect ratio.

    Keeps the YouTube-standard 1280×720 for landscape, 720×1280 for portrait,
    and ~960×960 for square videos. The pixel area is held roughly constant
    (≈ a 1280×720 cover) so render cost doesn't balloon across orientations,
    and each side is snapped to a multiple of 16 for the image model.
    """
    if vid_width <= 0 or vid_height <= 0:
        return COVER_WIDTH, COVER_HEIGHT
    import math
    area = COVER_WIDTH * COVER_HEIGHT
    ratio = vid_width / vid_height
    h = math.sqrt(area / ratio)
    w = h * ratio
    snap = lambda v: max(16, int(round(v / 16)) * 16)
    return snap(w), snap(h)


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


def build_cover_prompt(title: str, style: str = "", scenes=None, instruction: str = "") -> str:
    """Build a FLUX prompt for a YouTube documentary cover image.

    scenes: optional iterable of Scene objects or dicts with `image_prompt`. When
            provided, a short subject hint is appended so the cover reflects the
            actual video content (not random topic-biased imagery).
    instruction: optional one-off user steering from the Re-generate popover
            (e.g. "make it all robots"), appended to the prompt.
    """
    style_note = style.strip().rstrip(".")
    style_line = f"Video visual style: {style_note}. " if style_note else ""
    aspects = _extract_scene_aspects(scenes)
    subject_hint = f"Key visual elements from the video: {aspects}. " if aspects else ""
    prompt = _prompts.user(
        "cover_image",
        title=title,
        style_line=style_line,
        subject_hint=subject_hint,
        negative=_prompts.value("cover_negative"),
    )
    instruction = (instruction or "").strip()[:500]
    if instruction:
        prompt = f"{prompt}. {instruction}"
    return prompt


def _load_font(font_size: int, font_path: str = ""):
    """Font at *font_size*: the requested file first (per-style cover-text font),
    then the best available bold-ish system font, then PIL's default."""
    from PIL import ImageFont

    for candidate in (
        font_path,
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, font_size)
        except (OSError, IOError):
            pass
    return ImageFont.load_default()


# Directories scanned for the per-style cover-text font picker. The burn runs
# on the controller host, so its installed fonts are the ones that matter.
_FONT_DIRS = (
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    "~/Library/Fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.local/share/fonts",
    "~/.fonts",
)

_FONTS_CACHE: list[dict] | None = None


def available_fonts(refresh: bool = False) -> list[dict]:
    """Fonts installed on this machine as [{"path", "name"}], sorted by name.

    Names come from the font file itself ("Helvetica Bold"), so weight/style
    variants are picked as separate entries. Hidden files (Apple's ".SFNS…"
    system faces) and emoji fonts are skipped. Cached after the first scan —
    pass refresh=True to rescan."""
    global _FONTS_CACHE
    if _FONTS_CACHE is not None and not refresh:
        return _FONTS_CACHE
    from PIL import ImageFont

    fonts: list[dict] = []
    seen: set[str] = set()
    for base in _FONT_DIRS:
        root = Path(base).expanduser()
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in (".ttf", ".otf", ".ttc") or path.name.startswith("."):
                continue
            resolved = str(path)
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                family, style = ImageFont.truetype(resolved, 24).getname()
                name = family if style in ("", "Regular") else f"{family} {style}"
            except Exception:
                name = path.stem
            # Apple's private faces name their FAMILY with a leading dot
            # (".Aqua Kana" in AquaKana.ttc) — skip those like hidden files.
            if name.startswith(".") or "emoji" in name.lower():
                continue
            fonts.append({"path": resolved, "name": name})
    fonts.sort(key=lambda f: f["name"].lower())
    _FONTS_CACHE = fonts
    return fonts


FIRST_FRAME_TEXT_SIZE_DEFAULT = 11   # % of the video width per line of text
FIRST_FRAME_TEXT_COLOR_DEFAULT = "#FFFFFF"


def norm_first_frame_text_size(value) -> int:
    """Coerce the cover-text size (% of frame width) to an int in 4..30."""
    try:
        return max(4, min(30, int(value)))
    except (TypeError, ValueError):
        return FIRST_FRAME_TEXT_SIZE_DEFAULT


def norm_first_frame_text_color(value) -> str:
    """Coerce a cover-text colour to "#RRGGBB" (white when invalid)."""
    s = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", s):
        s = "#" + "".join(c * 2 for c in s[1:])
    if re.fullmatch(r"#[0-9a-fA-F]{6}", s):
        return s.upper()
    return FIRST_FRAME_TEXT_COLOR_DEFAULT


def overlay_title_on_image(base_path: Path, output_path: Path, title: str) -> None:
    """Overlay video title text on a cover image using PIL."""
    from PIL import Image, ImageDraw
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
    font = _load_font(font_size)

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


def overlay_cover_text(
    base_path: Path,
    output_path: Path,
    text: str,
    *,
    font_path: str = "",
    size_pct=None,
    color: str = "",
) -> None:
    """Draw the cover phrase in large type across the top of a video frame.

    Used by the "text" first-frame cover mode: the shortened title is drawn
    top-center in big letters (with a dark scrim from the top edge) so the
    frame reads as a thumbnail in the Shorts feed. Font, size (% of frame
    width) and colour come from the style's cover-text settings; defaults
    reproduce the original look (bold system font, 11%, white).
    """
    from PIL import Image, ImageDraw
    import textwrap

    img = Image.open(base_path).convert("RGBA")
    width, height = img.size

    font_size = max(16, width * norm_first_frame_text_size(size_pct) // 100)
    font = _load_font(font_size, font_path)
    fill = tuple(int(norm_first_frame_text_color(color)[i:i + 2], 16) for i in (1, 3, 5))
    # Keep the outline readable whatever colour is picked: dark text gets a
    # light stroke (the scrim below is dark), light text keeps the dark one.
    stroke_fill = (255, 255, 255) if sum(fill) < 300 else (0, 0, 0)
    max_chars = max(8, int(width / (font_size * 0.55)))
    lines = textwrap.wrap(text, width=max_chars)
    line_height = int(font_size * 1.18)
    top = int(height * 0.08)
    total_h = len(lines) * line_height

    # Dark gradient scrim fading down from the top edge, sized to the text
    # block, so the text stays readable over any first frame.
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    scrim_h = min(height, top + total_h + int(height * 0.06))
    for y in range(scrim_h):
        alpha = int(190 * (1 - y / scrim_h))
        draw_ov.rectangle([0, y, width, y + 1], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)
    stroke = max(2, font_size // 16)
    y = top
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke)
        tw = bbox[2] - bbox[0]
        x = (width - tw) // 2
        draw.text((x + 4, y + 4), line, font=font, fill=(0, 0, 0, 200),
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 200))
        draw.text((x, y), line, font=font, fill=fill + (255,),
                  stroke_width=stroke, stroke_fill=stroke_fill + (255,))
        y += line_height

    img.convert("RGB").save(str(output_path), "PNG")


FIRST_FRAME_COVER_MODES = ("none", "image", "text")


def norm_first_frame_cover(value) -> str:
    """Coerce a first-frame cover mode to "none" | "image" | "text"."""
    return value if value in ("image", "text") else "none"


def burn_cover_into_first_frame(
    video_path: Path,
    mode: str,
    *,
    cover_path: Path | None = None,
    title: str = "",
    work_dir: Path | None = None,
    text_font: str = "",
    text_size=None,
    text_color: str = "",
) -> Path:
    """Burn a cover into the final video's first frame, in place.

    YouTube Shorts ignore uploaded thumbnails and show the video's first frame
    in the feed, so the cover is stamped onto frame 0 itself. mode "image"
    uses the job's cover image; mode "text" overlays the shortened title in
    large type on the video's own first frame. Exactly one frame is replaced
    (nothing is prepended), so duration, audio, and caption timing all stay
    valid. Working PNGs land in *work_dir* (defaults to the video's folder).
    """
    from pipeline.assembler import extract_first_frame, replace_first_frame

    mode = (mode or "").strip().lower()
    if mode not in ("image", "text"):
        raise ValueError(f"Unknown first-frame cover mode: {mode!r}")
    wd = Path(work_dir) if work_dir else video_path.parent
    if mode == "image":
        if not (cover_path and cover_path.exists() and cover_path.stat().st_size > 1000):
            raise FileNotFoundError("No cover image for this film — generate the cover first.")
        frame_src = cover_path
    else:
        text = shorten_title_for_cover((title or "").strip() or video_path.stem)
        base = wd / "first_frame_text_base.png"
        extract_first_frame(video_path, base)
        frame_src = wd / "first_frame_text.png"
        overlay_cover_text(base, frame_src, text,
                           font_path=text_font, size_pct=text_size, color=text_color)

    staged = video_path.with_name(f"{video_path.stem}.firstframe.tmp{video_path.suffix}")
    try:
        replace_first_frame(video_path, frame_src, staged)
        staged.replace(video_path)
    finally:
        staged.unlink(missing_ok=True)
    return video_path
