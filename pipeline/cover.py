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


# Per-film override for the phrase printed on the cover image and burned into
# the first frame. Written from the edit/publish screens; absent or blank, the
# phrase stays derived from the title (deterministic — the LLM cover-phrase
# feature was deliberately reverted, see #86).
COVER_PHRASE_FILE = "cover_phrase.txt"
COVER_PHRASE_MAX_CHARS = 80


def cover_phrase_for(work_dir: Path | str, title: str = "") -> str:
    """The short text shown on the cover image and the first-frame burn:
    the film's saved override (cover_phrase.txt), else shortened from *title*."""
    try:
        text = (Path(work_dir) / COVER_PHRASE_FILE).read_text(encoding="utf-8").strip()
        if text:
            return text[:COVER_PHRASE_MAX_CHARS]
    except Exception:
        pass
    return shorten_title_for_cover(title)


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


COVER_TEXT_POSITIONS = ("top", "center", "bottom")
COVER_TEXT_EMPHASIS_RULES = ("none", "caps", "last_word", "last_line")

COVER_TEXT_POSITION_DEFAULT = "bottom"
COVER_TEXT_CARD_OPACITY_DEFAULT = 55
COVER_TEXT_EMPHASIS_SCALE_DEFAULT = 1.25


def norm_cover_text_position(value) -> str:
    """Coerce a cover-text position to "top" | "center" | "bottom"."""
    v = str(value or "").strip().lower()
    return v if v in COVER_TEXT_POSITIONS else COVER_TEXT_POSITION_DEFAULT


def norm_cover_text_size(value) -> int:
    """Coerce the cover-text size (% of image width) to an int in 4..30."""
    return norm_first_frame_text_size(value)


def _norm_hex_color(value, default: str) -> str:
    s = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", s):
        s = "#" + "".join(c * 2 for c in s[1:])
    if re.fullmatch(r"#[0-9a-fA-F]{6}", s):
        return s.upper()
    return default


def norm_cover_text_color(value) -> str:
    """Coerce a cover-text colour to "#RRGGBB" (white when invalid/unset)."""
    return _norm_hex_color(value, "#FFFFFF")


def norm_cover_card_color(value) -> str:
    """Coerce a cover-card colour to "#RRGGBB" (black when invalid/unset)."""
    return _norm_hex_color(value, "#000000")


def norm_cover_emphasis_color(value) -> str:
    """Coerce a cover-emphasis colour: "" (inherit the base text colour) is
    valid and left as-is; anything else must be a proper hex colour."""
    s = str(value or "").strip()
    if not s:
        return ""
    return _norm_hex_color(s, "")


def norm_cover_card_opacity(value) -> int:
    """Coerce the cover-card opacity (%) to an int in 0..100."""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return COVER_TEXT_CARD_OPACITY_DEFAULT


def norm_cover_emphasis_rule(value) -> str:
    """Coerce a cover emphasis rule to one of COVER_TEXT_EMPHASIS_RULES."""
    v = str(value or "").strip().lower()
    return v if v in COVER_TEXT_EMPHASIS_RULES else "none"


def norm_cover_emphasis_scale(value) -> float:
    """Coerce the cover emphasis size multiplier to a float in 1.0..2.0."""
    try:
        return round(min(2.0, max(1.0, float(value))), 2)
    except (TypeError, ValueError):
        return COVER_TEXT_EMPHASIS_SCALE_DEFAULT


def _word_is_emphasized_by_caps(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    return len(letters) >= 2 and word.upper() == word


def _wrap_words_uniform(draw, words: list[str], font, max_width: int) -> list[list[str]]:
    """Greedy word-wrap *words* at a single *font*, for a first pass used only
    to find which words land on the last line (the "last_line" emphasis rule)."""
    space_w = draw.textlength(" ", font=font)
    lines: list[list[str]] = [[]]
    line_w = 0
    for word in words:
        w = draw.textlength(word, font=font)
        extra = (space_w if line_w else 0) + w
        if line_w and line_w + extra > max_width:
            lines.append([word])
            line_w = w
        else:
            lines[-1].append(word)
            line_w += extra
    return [ln for ln in lines if ln]


def render_cover_typography(
    base_path: Path,
    output_path: Path,
    text: str,
    *,
    font_path: str = "",
    position: str = "bottom",
    size_pct=None,
    color: str = "",
    card: bool = True,
    card_color: str = "",
    card_opacity=None,
    emphasis_rule: str = "none",
    emphasis_color: str = "",
    emphasis_scale=None,
) -> None:
    """Composite *text* onto an EXISTING (already-generated, text-free) cover
    background with crisp PIL vector text — the diffusion model never renders
    characters, so spelling is always correct.

    position anchors the whole text block top/center/bottom (~6% margin).
    When card=True, a semi-transparent rounded plate is drawn behind the text
    block first for legibility; otherwise a stroke outline (as used elsewhere
    in this module) is relied on instead. emphasis_rule picks which words
    render larger/differently-coloured, matched against the phrase text
    itself so no separate per-word authoring UI is needed: "caps" emphasizes
    any word already ALL CAPS in the phrase, "last_word"/"last_line" are
    positional, "none" renders uniformly.
    """
    from PIL import Image, ImageDraw

    img = Image.open(base_path).convert("RGBA")
    width, height = img.size

    words = (text or "").split()
    if not words:
        img.convert("RGB").save(str(output_path), "PNG")
        return

    font_size = max(16, width * norm_cover_text_size(size_pct) // 100)
    base_font = _load_font(font_size, font_path)
    scale = norm_cover_emphasis_scale(emphasis_scale)
    emph_font = _load_font(int(font_size * scale), font_path) if scale != 1.0 else base_font

    rule = norm_cover_emphasis_rule(emphasis_rule)
    scratch = ImageDraw.Draw(img)
    max_width = int(width * 0.88)
    if rule == "last_line":
        # First-pass uniform wrap just to find which trailing words end up on
        # the last line; tracked by index since words may repeat.
        wrapped = _wrap_words_uniform(scratch, words, base_font, max_width)
        last_line_set = set(range(len(words) - len(wrapped[-1]), len(words)))
    else:
        last_line_set = set()

    def is_emphasized(idx: int, word: str) -> bool:
        if rule == "caps":
            return _word_is_emphasized_by_caps(word)
        if rule == "last_word":
            return idx == len(words) - 1
        if rule == "last_line":
            return idx in last_line_set
        return False

    base_color = norm_cover_text_color(color)
    fill = tuple(int(base_color[i:i + 2], 16) for i in (1, 3, 5))
    emph_hex = norm_cover_emphasis_color(emphasis_color) or base_color
    emph_fill = tuple(int(emph_hex[i:i + 2], 16) for i in (1, 3, 5))
    stroke_fill = (255, 255, 255) if sum(fill) < 300 else (0, 0, 0)
    emph_stroke_fill = (255, 255, 255) if sum(emph_fill) < 300 else (0, 0, 0)

    # Second pass: wrap using each word's ACTUAL font (base or emphasis) so
    # mixed sizes still fit the line width.
    runs = [(w, emph_font if is_emphasized(i, w) else base_font,
             emph_fill if is_emphasized(i, w) else fill,
             emph_stroke_fill if is_emphasized(i, w) else stroke_fill)
            for i, w in enumerate(words)]
    lines: list[list[tuple]] = [[]]
    line_w = 0
    for run in runs:
        word, font, _fill, _stroke = run
        w = scratch.textlength(word, font=font)
        space_w = scratch.textlength(" ", font=font)
        extra = (space_w if line_w else 0) + w
        if line_w and line_w + extra > max_width:
            lines.append([run])
            line_w = w
        else:
            lines[-1].append(run)
            line_w += extra
    lines = [ln for ln in lines if ln]

    stroke_w = max(2, font_size // 16)
    line_metrics = []
    for ln in lines:
        line_height = max(scratch.textbbox((0, 0), w, font=f, stroke_width=stroke_w)[3]
                          for w, f, _c, _s in ln)
        line_w_total = 0
        for i, (word, font, _c, _s) in enumerate(ln):
            if i:
                line_w_total += scratch.textlength(" ", font=font)
            line_w_total += scratch.textlength(word, font=font)
        line_metrics.append((line_height, line_w_total))
    line_gap = int(font_size * 0.25)
    total_h = sum(h for h, _w in line_metrics) + line_gap * (len(lines) - 1)
    block_w = max((w for _h, w in line_metrics), default=0)

    margin = int(height * 0.06)
    pos = norm_cover_text_position(position)
    if pos == "top":
        top = margin
    elif pos == "center":
        top = (height - total_h) // 2
    else:
        top = height - total_h - margin

    if card:
        pad = int(font_size * 0.6)
        radius = int(font_size * 0.35)
        card_hex = norm_cover_card_color(card_color)
        card_rgb = tuple(int(card_hex[i:i + 2], 16) for i in (1, 3, 5))
        alpha = int(255 * norm_cover_card_opacity(card_opacity) / 100)
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw_ov = ImageDraw.Draw(overlay)
        draw_ov.rounded_rectangle(
            [(width - block_w) / 2 - pad, top - pad, (width + block_w) / 2 + pad, top + total_h + pad],
            radius=radius, fill=card_rgb + (alpha,))
        img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)
    y = top
    for ln, (line_height, line_w_total) in zip(lines, line_metrics):
        x = (width - line_w_total) / 2
        for word, font, word_fill, word_stroke in ln:
            draw.text((x, y), word, font=font, fill=word_fill + (255,),
                      stroke_width=stroke_w, stroke_fill=word_stroke + (255,))
            x += scratch.textlength(word, font=font) + scratch.textlength(" ", font=font)
        y += line_height + line_gap

    img.convert("RGB").save(str(output_path), "PNG")


def _preview_placeholder_background(width: int, height: int):
    """A generic dark gradient background for previewing cover typography
    independent of any real generated cover art (used by the Styles-tab
    typography editor, which has no film to preview against)."""
    from PIL import Image

    img = Image.new("RGB", (width, height))
    px = img.load()
    top = (28, 32, 46)
    bottom = (12, 14, 22)
    for y in range(height):
        t = y / max(1, height - 1)
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(width):
            px[x, y] = row
    return img


def render_cover_text_overlay(
    width: int,
    height: int,
    output_path: Path,
    text: str,
    *,
    font_path: str = "",
    size_pct=None,
    color: str = "",
) -> None:
    """Draw the cover phrase in large type across the top of a TRANSPARENT
    canvas, ready to composite over the video.

    Used by the "text" first-frame cover mode: the shortened title is drawn
    top-center in big letters (with a dark scrim from the top edge) so the
    opening reads as a thumbnail in the Shorts feed. The canvas is transparent
    so ffmpeg lays it over the moving video — the title card costs no motion,
    however long it is held. Font, size (% of frame width) and colour come from
    the style's cover-text settings; defaults reproduce the original look (bold
    system font, 11%, white).
    """
    from PIL import Image, ImageDraw
    import textwrap

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))

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
    # block, so the text stays readable over any frame underneath.
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

    img.save(str(output_path), "PNG")


FIRST_FRAME_COVER_MODES = ("none", "image", "text")

# How long the burned cover is held at the head of the film. YouTube Shorts
# ignore uploaded thumbnails and pick their own frame; a single stamped frame
# (0.04s at 25fps) reads as a flash and is discarded by frame samplers, so the
# cover is held ~1s — long enough to be its own shot, short enough to keep the
# hook. Capped at 3s; the floor is one frame (the pre-1s behaviour).
FIRST_FRAME_COVER_SECONDS_DEFAULT = 1.0
FIRST_FRAME_COVER_SECONDS_MIN = 0.04
FIRST_FRAME_COVER_SECONDS_MAX = 3.0


def norm_first_frame_cover_seconds(value) -> float:
    """Coerce the cover hold to 0.04..3.0 seconds (1.0 when unset/invalid)."""
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return FIRST_FRAME_COVER_SECONDS_DEFAULT
    return round(min(FIRST_FRAME_COVER_SECONDS_MAX,
                     max(FIRST_FRAME_COVER_SECONDS_MIN, secs)), 2)


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
    seconds=None,
    text_font: str = "",
    text_size=None,
    text_color: str = "",
) -> Path:
    """Burn a cover into the head of the final video, in place.

    YouTube Shorts ignore uploaded thumbnails and pick their own frame from the
    video, so the cover is stamped onto the opening frames themselves — held
    *seconds* long (default 1s) so YouTube's frame picker sees a shot rather
    than a one-frame flash. mode "image" covers the video with the job's cover
    image; mode "text" lays the shortened title in large type OVER the running
    video, so the title card costs no motion. Frames are overlaid, never
    prepended, so duration, audio, and caption timing all stay valid. Working
    PNGs land in *work_dir* (defaults to the video's folder).
    """
    from pipeline.assembler import _FILM_FPS, _get_video_dimensions, replace_first_frame

    mode = (mode or "").strip().lower()
    if mode not in ("image", "text"):
        raise ValueError(f"Unknown first-frame cover mode: {mode!r}")
    wd = Path(work_dir) if work_dir else video_path.parent
    frames = max(1, round(norm_first_frame_cover_seconds(seconds) * _FILM_FPS))
    if mode == "image":
        if not (cover_path and cover_path.exists() and cover_path.stat().st_size > 1000):
            raise FileNotFoundError("No cover image for this film — generate the cover first.")
        frame_src = cover_path
    else:
        text = cover_phrase_for(wd, (title or "").strip() or video_path.stem)
        width, height = _get_video_dimensions(video_path)
        frame_src = wd / "first_frame_text.png"
        render_cover_text_overlay(width, height, frame_src, text,
                                  font_path=text_font, size_pct=text_size, color=text_color)

    staged = video_path.with_name(f"{video_path.stem}.firstframe.tmp{video_path.suffix}")
    try:
        replace_first_frame(video_path, frame_src, staged, frames=frames)
        staged.replace(video_path)
    finally:
        staged.unlink(missing_ok=True)
    return video_path
