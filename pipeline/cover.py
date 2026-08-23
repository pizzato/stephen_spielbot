"""YouTube cover image generation utilities (shared between app.py and resume_generation.py)."""
from __future__ import annotations

import logging
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
# feature was deliberately reverted, see #86). The text may carry *accent*
# markup and explicit line breaks — see pipeline.cover_typography.
COVER_PHRASE_FILE = "cover_phrase.txt"
COVER_PHRASE_MAX_CHARS = 80


def default_cover_phrase(title: str, accent: str = "last_word") -> str:
    """The title-derived cover phrase WITH the style's accent rule written in
    as ``*markup*`` ("Hello *world*" for ``last_word``). The renderer only
    ever accents marked words, so the rule lives in the phrase text itself —
    editing it (or removing the asterisks) is the whole override mechanism."""
    from pipeline.cover_typography import mark_accent

    return mark_accent(shorten_title_for_cover(title), accent)


def cover_phrase_for(work_dir: Path | str, title: str = "",
                     accent: str = "last_word") -> str:
    """The short text shown on the cover image and the first-frame burn:
    the film's saved override (cover_phrase.txt), else the default derived
    from *title* with the style's *accent* rule marked up."""
    try:
        text = (Path(work_dir) / COVER_PHRASE_FILE).read_text(encoding="utf-8").strip()
        if text:
            return text[:COVER_PHRASE_MAX_CHARS]
    except Exception:
        pass
    return default_cover_phrase(title, accent)


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
        meta = (s.get("metadata") if isinstance(s, dict) else getattr(s, "metadata", {})) or {}
        ip_raw = (s.get("image_prompt") if isinstance(s, dict) else getattr(s, "image_prompt", "")) or ""
        ip = _strip_style_prefix(ip_raw)
        if str(meta.get("mode") or "") in ("dialogue", "performance"):
            # An acted scene's subject is WHO is on screen and WHERE — its cast
            # and setting. (Naming the cast is also what lets the cover pick up
            # their reference portraits.) Its image_prompt is empty by design,
            # and its title often paraphrases the film title, which the model
            # would happily paint into the "text-free" background.
            cast = ", ".join(str(c) for c in (meta.get("cast") or []) if str(c).strip())
            setting = str(meta.get("setting") or "").strip()
            ip = f"{cast} — {setting}" if cast and setting else (cast or setting)
            if not ip:
                continue
        elif len(ip) < 20:
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


def build_cover_prompt(style: str = "", scenes=None, instruction: str = "",
                       text_position: str = "") -> str:
    """Build the prompt for a TEXT-FREE cover background.

    The title is never part of the prompt — it is composited afterwards with
    real fonts (pipeline/cover_typography.py), so the model never gets a
    chance to misspell it.

    scenes: optional iterable of Scene objects or dicts with `image_prompt`. When
            provided, a short subject hint is appended so the cover reflects the
            actual video content (not random topic-biased imagery).
    instruction: optional one-off user steering from the Re-generate popover
            (e.g. "make it all robots"). It LEADS the prompt: trailing it after
            the long boilerplate left it under-weighted and sitting right next
            to the "Avoid:" list, where the model could read it as one more
            thing to leave out — so a steer simply never landed.
    text_position: where the title will land ("top"/"middle"/"bottom") — asks
            the model for calmer space there.
    """
    instruction = (instruction or "").strip()[:500]
    style_note = style.strip().rstrip(".")
    style_line = f"Video visual style: {style_note}. " if style_note else ""
    aspects = _extract_scene_aspects(scenes)
    if not aspects:
        subject_hint = ""
    elif instruction:
        # A steer outranks the film's own imagery: the scene elements drop from
        # content the cover must show to material it may draw on. Without this
        # the concrete scene text ("a woman on a rainy street") simply beats a
        # conflicting direction ("make it all robots").
        subject_hint = ("Visual elements from the video, to draw on only where they fit "
                        f"that direction: {aspects}. ")
    else:
        subject_hint = f"Key visual elements from the video: {aspects}. "
    pos = text_position if text_position in ("top", "middle", "bottom") else "bottom"
    space_hint = ("the vertical middle band" if pos == "middle"
                  else f"the {pos} third") + " of the frame"
    prompt = _prompts.user(
        "cover_image_notext",
        style_line=style_line,
        subject_hint=subject_hint,
        space_hint=space_hint,
        negative=_prompts.value("cover_negative_notext"),
    )
    if instruction:
        prompt = (f"{instruction.rstrip('.')}. That direction outranks everything "
                  f"below — where they conflict, follow it. {prompt}")
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


FIRST_FRAME_COVER_MODES = ("none", "image")

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
    """Coerce a first-frame cover mode to "none" | "image".

    Legacy "text" (the big-title overlay mode) burns the cover image instead —
    with cover typography the cover IS the title, so the two modes collapsed."""
    return "image" if value in ("image", "text") else "none"


def burn_cover_into_first_frame(
    video_path: Path,
    *,
    cover_path: Path,
    seconds=None,
) -> Path:
    """Burn the cover image into the head of the final video, in place.

    YouTube Shorts ignore uploaded thumbnails and pick their own frame from the
    video, so the cover is stamped onto the opening frames themselves — held
    *seconds* long (default 1s) so YouTube's frame picker sees a shot rather
    than a one-frame flash. Frames are overlaid, never prepended, so duration,
    audio, and caption timing all stay valid.
    """
    from pipeline.assembler import _FILM_FPS, replace_first_frame

    if not (cover_path and cover_path.exists() and cover_path.stat().st_size > 1000):
        raise FileNotFoundError("No cover image for this film — generate the cover first.")
    frames = max(1, round(norm_first_frame_cover_seconds(seconds) * _FILM_FPS))
    staged = video_path.with_name(f"{video_path.stem}.firstframe.tmp{video_path.suffix}")
    try:
        replace_first_frame(video_path, cover_path, staged, frames=frames)
        staged.replace(video_path)
    finally:
        staged.unlink(missing_ok=True)
    return video_path
