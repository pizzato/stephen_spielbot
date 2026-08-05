"""Deterministic cover-title typography.

Instead of asking the diffusion model to draw the title (it misspells words,
and every regeneration is a fresh dice roll), the model generates a TEXT-FREE
background and this module composites the title on top with real fonts. The
look — font, position, colours, accent words, card — is a per-style setting
(``cover_typography``), so text is pixel-perfect on every cover and
regenerating only rerolls the artwork.

Accented words ("some words in a different colour and size") come from a rule
set in the style (first/last word, last line, longest word), overridable per
video by wrapping words in asterisks in the cover phrase: ``The *Secret* War``.
"""
from __future__ import annotations

import re
from pathlib import Path

# Bundled display fonts (assets/fonts/<family>/*.ttf, each with its licence
# file). Thumbnail-grade faces the system font dirs usually lack; styles store
# the font NAME for these (checkout-relative), and a full path for system fonts.
BUNDLED_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

POSITIONS = ("top", "middle", "bottom")
ALIGNS = ("left", "center", "right")
CASES = ("keep", "upper", "title")
ACCENT_RULES = ("none", "first_word", "last_word", "last_line", "longest_word")

DEFAULT_COVER_TYPOGRAPHY = {
    "enabled": False,
    "font": "Anton",            # bundled font name, or a system font file path
    "position": "bottom",       # top | middle | bottom
    "align": "center",          # left | center | right
    "case": "upper",            # keep | upper | title
    "width_pct": 82,            # max text-block width, % of image width
    "scale": 1.0,               # size multiplier on the auto-fitted text
    "color": "#FFFFFF",
    "accent": "last_word",      # which words get the accent colour/size
    "accent_color": "#FFD400",
    "accent_scale": 1.15,       # accented words' size relative to the rest
    "outline": True,            # dark stroke + soft shadow for legibility
    "card": False,              # rounded backdrop card behind the text block
    "card_color": "#000000",
    "card_opacity": 0.55,
}

# The text block never exceeds this fraction of the image height (3 lines max).
_MAX_BLOCK_FRAC = 0.42
_MAX_LINES = 3
_PROBE = 100  # measurement size; TrueType metrics scale ~linearly with size


def _hex_color(value, fallback: str) -> str:
    s = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", s):
        s = "#" + "".join(c * 2 for c in s[1:])
    if re.fullmatch(r"#[0-9a-fA-F]{6}", s):
        return s.upper()
    return fallback


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _clamp(value, lo: float, hi: float, fallback: float) -> float:
    try:
        return min(hi, max(lo, float(value)))
    except (TypeError, ValueError):
        return fallback


def norm_cover_typography(value) -> dict:
    """Coerce a style's ``cover_typography`` to a full, valid settings dict."""
    src = value if isinstance(value, dict) else {}
    d = dict(DEFAULT_COVER_TYPOGRAPHY)
    d["enabled"] = bool(src.get("enabled", d["enabled"]))
    font = src.get("font", d["font"])
    d["font"] = str(font).strip() if isinstance(font, (str, Path)) else d["font"]
    for key, allowed in (("position", POSITIONS), ("align", ALIGNS),
                         ("case", CASES), ("accent", ACCENT_RULES)):
        v = str(src.get(key, d[key]) or "").strip().lower()
        d[key] = v if v in allowed else DEFAULT_COVER_TYPOGRAPHY[key]
    d["width_pct"] = int(_clamp(src.get("width_pct", d["width_pct"]), 40, 96, d["width_pct"]))
    d["scale"] = round(_clamp(src.get("scale", d["scale"]), 0.5, 1.6, d["scale"]), 2)
    d["accent_scale"] = round(_clamp(src.get("accent_scale", d["accent_scale"]), 1.0, 1.8, d["accent_scale"]), 2)
    d["card_opacity"] = round(_clamp(src.get("card_opacity", d["card_opacity"]), 0.05, 0.95, d["card_opacity"]), 2)
    d["color"] = _hex_color(src.get("color"), d["color"])
    d["accent_color"] = _hex_color(src.get("accent_color"), d["accent_color"])
    d["card_color"] = _hex_color(src.get("card_color"), d["card_color"])
    d["outline"] = bool(src.get("outline", d["outline"]))
    d["card"] = bool(src.get("card", d["card"]))
    return d


# ── phrase markup ─────────────────────────────────────────────────────────────
# Per-video accent override: words wrapped in asterisks in cover_phrase.txt
# ("The *Secret* War") are accented, overriding the style's accent rule.


def split_phrase_markup(phrase: str) -> tuple[str, set[int]]:
    """Return ``(clean_phrase, accented_word_indices)`` from ``*word*`` markup.

    Indices refer to ``clean_phrase.split()``. A word is accented when any of
    its characters sat inside an asterisk span. Asterisks never survive into
    the clean phrase (an unpaired one just accents the rest of the phrase).
    """
    clean_chars: list[str] = []
    flags: list[bool] = []
    in_span = False
    for ch in phrase or "":
        if ch == "*":
            in_span = not in_span
            continue
        clean_chars.append(ch)
        flags.append(in_span)
    accents: set[int] = set()
    word_i = 0
    in_word = False
    for ch, accented in zip(clean_chars, flags):
        if ch.isspace():
            if in_word:
                word_i += 1
            in_word = False
        else:
            in_word = True
            if accented:
                accents.add(word_i)
    return " ".join("".join(clean_chars).split()), accents


def strip_phrase_markup(phrase: str) -> str:
    """The cover phrase with accent markup removed (for prompts/plain overlays)."""
    return split_phrase_markup(phrase)[0]


# ── fonts ─────────────────────────────────────────────────────────────────────

_BUNDLED_CACHE: list[dict] | None = None


def bundled_fonts() -> list[dict]:
    """Fonts shipped in assets/fonts as ``[{"name", "path"}]``, sorted by name."""
    global _BUNDLED_CACHE
    if _BUNDLED_CACHE is not None:
        return _BUNDLED_CACHE
    from PIL import ImageFont

    fonts: list[dict] = []
    if BUNDLED_FONT_DIR.is_dir():
        for path in sorted(BUNDLED_FONT_DIR.rglob("*")):
            if path.suffix.lower() not in (".ttf", ".otf"):
                continue
            try:
                family, style = ImageFont.truetype(str(path), 24).getname()
                name = family if style in ("", "Regular") else f"{family} {style}"
            except Exception:
                name = path.stem
            fonts.append({"name": name, "path": str(path)})
    fonts.sort(key=lambda f: f["name"].lower())
    _BUNDLED_CACHE = fonts
    return fonts


def resolve_font_path(font: str) -> str:
    """A loadable font file for a style's ``font`` value: an existing path is
    used as-is; otherwise the name is matched against bundled fonts (so styles
    stay portable across checkouts), then the system font scan. "" = no match
    (the renderer falls back to a bold system face)."""
    font = (font or "").strip()
    if not font:
        return ""
    if Path(font).expanduser().is_file():
        return str(Path(font).expanduser())
    want = font.lower()
    for f in bundled_fonts():
        if f["name"].lower() == want or Path(f["path"]).stem.lower() == want:
            return f["path"]
    from pipeline.cover import available_fonts

    for f in available_fonts():
        if f["name"].lower() == want:
            return f["path"]
    return ""


# ── layout + render ───────────────────────────────────────────────────────────


def _apply_case(word: str, mode: str) -> str:
    if mode == "upper":
        return word.upper()
    if mode == "title":
        return word[:1].upper() + word[1:]  # keep existing caps ("AI" stays "AI")
    return word


def _accent_set(words: list[str], rule: str, markup: set[int]) -> tuple[set[int], bool]:
    """Word indices to accent, plus a last-line flag resolved after wrapping."""
    if markup:
        return {i for i in markup if i < len(words)}, False
    if not words or rule == "none":
        return set(), False
    if rule == "first_word":
        return {0}, False
    if rule == "last_word":
        return {len(words) - 1}, False
    if rule == "longest_word":
        return {max(range(len(words)), key=lambda i: len(words[i]))}, False
    return set(), rule == "last_line"


def _partitions(count: int, lines: int):
    """All ways to split ``count`` words into ``lines`` contiguous non-empty runs."""
    if lines == 1:
        yield [count]
        return
    for first in range(1, count - lines + 2):
        for rest in _partitions(count - first, lines - 1):
            yield [first] + rest


def _line_lists(words: list[str], sizes: list[int]) -> list[list[int]]:
    """Word-index lines for the best 1..3-line layout.

    Every viable line count is measured (character-width proxy at this stage);
    the layout that yields the largest fitted font wins in ``_fit`` — here we
    just produce, per line count, the split minimising the widest line.
    """
    out = []
    for n in range(1, min(_MAX_LINES, len(words)) + 1):
        best, best_w = None, None
        for split in _partitions(len(words), n):
            rows, i = [], 0
            for c in split:
                rows.append(list(range(i, i + c)))
                i += c
            widest = max(sum(sizes[j] for j in row) + (len(row) - 1) for row in rows)
            if best_w is None or widest < best_w:
                best, best_w = rows, widest
        out.append(best)
    return out


def render_cover_typography(bg_path: Path | str, out_path: Path | str,
                            phrase: str, cfg: dict) -> dict:
    """Composite *phrase* over the background image and save to *out_path*.

    Pure function of its inputs (no randomness, no clock), so re-running with
    the same background, phrase, and settings reproduces the identical cover.
    Returns layout metadata for tests/telemetry: ``{"font_size", "lines",
    "accents", "block"}``.
    """
    from PIL import Image, ImageDraw, ImageFilter

    cfg = norm_cover_typography(cfg)
    # bg/out are paths in the pipeline; the preview endpoint passes a PIL image
    # in and a BytesIO out (no temp files for a throwaway render).
    img = (bg_path if hasattr(bg_path, "convert") else Image.open(bg_path)).convert("RGBA")
    W, H = img.size

    def save(final) -> None:
        final.convert("RGB").save(
            out_path if hasattr(out_path, "write") else str(out_path), "PNG")

    clean, markup = split_phrase_markup(phrase)
    words = [_apply_case(w, cfg["case"]) for w in clean.split()]
    meta = {"font_size": 0, "lines": [], "accents": [], "block": None}
    if not words:
        save(img)
        return meta

    accents, accent_last_line = _accent_set(words, cfg["accent"], markup)

    font_file = resolve_font_path(cfg["font"])
    from pipeline.cover import _load_font

    fonts: dict[int, object] = {}

    def font_at(size: int):
        f = fonts.get(size)
        if f is None:
            f = fonts[size] = _load_font(max(1, int(size)), font_file)
        return f

    probe = font_at(_PROBE)
    draw_probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def measure(line_rows: list[list[int]], accent_rows: set[int]):
        """(max width, total height) at probe size, honouring accent scale."""
        space = draw_probe.textlength(" ", font=probe)
        widest, height = 0.0, 0.0
        for li, row in enumerate(line_rows):
            w_line, asc_line, desc_line = 0.0, 0.0, 0.0
            for wi in row:
                scale = cfg["accent_scale"] if (wi in accents or li in accent_rows) else 1.0
                f = font_at(round(_PROBE * scale))
                w_line += draw_probe.textlength(words[wi], font=f)
                asc, desc = f.getmetrics()
                asc_line, desc_line = max(asc_line, asc), max(desc_line, desc)
            w_line += space * (len(row) - 1)
            widest = max(widest, w_line)
            height += asc_line + desc_line
        height += _PROBE * 0.10 * (len(line_rows) - 1)  # inter-line gap
        return widest, height

    # Pick the line count whose balanced split fits the LARGEST font.
    max_w_px = W * cfg["width_pct"] / 100.0
    max_h_px = H * _MAX_BLOCK_FRAC
    char_sizes = [len(w) for w in words]
    best_rows, best_size = [[i for i in range(len(words))]], 0.0
    for rows in _line_lists(words, char_sizes):
        acc_rows = {len(rows) - 1} if (accent_last_line and len(rows) > 1) else set()
        w, h = measure(rows, acc_rows)
        size = _PROBE * min(max_w_px / w, max_h_px / h)
        if size > best_size:
            best_rows, best_size = rows, size
    rows = best_rows
    accent_rows = {len(rows) - 1} if accent_last_line and len(rows) > 1 else set()
    if accent_last_line and len(rows) == 1:
        accents = {len(words) - 1}  # single-line phrase: degrade to last word

    base = int(best_size * cfg["scale"] * 0.98)  # 2% slack for metric nonlinearity
    base = max(10, min(base, int(H * 0.5)))

    def word_scale(li: int, wi: int) -> float:
        return cfg["accent_scale"] if (wi in accents or li in accent_rows) else 1.0

    # Real-metric pass at the chosen size; shrink once if a line still overflows.
    draw = ImageDraw.Draw(img)
    for _ in range(2):
        space_w = draw.textlength(" ", font=font_at(base))
        line_dims = []  # (width, ascent, descent) per line
        for li, row in enumerate(rows):
            w_line, asc_line, desc_line = 0.0, 0, 0
            for wi in row:
                f = font_at(round(base * word_scale(li, wi)))
                w_line += draw.textlength(words[wi], font=f)
                asc, desc = f.getmetrics()
                asc_line, desc_line = max(asc_line, asc), max(desc_line, desc)
            w_line += space_w * (len(row) - 1)
            line_dims.append((w_line, asc_line, desc_line))
        widest = max(d[0] for d in line_dims)
        if widest <= max_w_px or base <= 10:
            break
        base = max(10, int(base * max_w_px / widest))

    gap = int(base * 0.10)
    block_w = max(d[0] for d in line_dims)
    block_h = sum(a + de for _, a, de in line_dims) + gap * (len(rows) - 1)

    margin_x = int(W * 0.05)
    margin_y = int(H * 0.06)
    if cfg["align"] == "left":
        block_x = margin_x
    elif cfg["align"] == "right":
        block_x = W - margin_x - block_w
    else:
        block_x = (W - block_w) / 2
    if cfg["position"] == "top":
        block_y = margin_y
    elif cfg["position"] == "middle":
        block_y = (H - block_h) / 2
    else:
        block_y = H - margin_y - block_h

    stroke = max(2, base // 14) if cfg["outline"] else 0
    pad_x, pad_y = base * 0.42, base * 0.30
    block_box = (block_x - pad_x, block_y - pad_y,
                 block_x + block_w + pad_x, block_y + block_h + pad_y)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    if cfg["card"]:
        card = ImageDraw.Draw(overlay)
        card.rounded_rectangle(block_box, radius=int(base * 0.30),
                               fill=_rgb(cfg["card_color"]) + (int(cfg["card_opacity"] * 255),))
    img = Image.alpha_composite(img, overlay)

    # Word positions (shared per shadow/text passes).
    placed: list[tuple[str, float, float, object, bool]] = []  # word, x, baseline, font, accented
    y = block_y
    for li, row in enumerate(rows):
        w_line, asc_line, desc_line = line_dims[li]
        if cfg["align"] == "left":
            x = block_x
        elif cfg["align"] == "right":
            x = block_x + block_w - w_line
        else:
            x = block_x + (block_w - w_line) / 2
        baseline = y + asc_line
        for wi in row:
            f = font_at(round(base * word_scale(li, wi)))
            accented = wi in accents or li in accent_rows
            placed.append((words[wi], x, baseline, f, accented))
            x += draw.textlength(words[wi], font=f) + space_w
        y += asc_line + desc_line + gap

    if cfg["outline"]:
        # Soft drop shadow: blurred dark copy, offset down-right.
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        off = max(2, int(base * 0.045))
        for word, x, bl, f, _acc in placed:
            sd.text((x + off, bl + off), word, font=f, fill=(0, 0, 0, 170),
                    anchor="ls", stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(1, base * 0.05)))
        img = Image.alpha_composite(img, shadow)

    draw = ImageDraw.Draw(img)
    fill_rgb = _rgb(cfg["color"])
    accent_rgb = _rgb(cfg["accent_color"])
    for word, x, bl, f, accented in placed:
        rgb = accent_rgb if accented else fill_rgb
        # Keep the outline readable whatever colour is picked: dark text gets
        # a light stroke, light text a dark one.
        stroke_rgb = ((255, 255, 255) if sum(rgb) < 300 else (10, 10, 10)) if stroke else None
        draw.text((x, bl), word, font=f, fill=rgb + (255,), anchor="ls",
                  stroke_width=stroke, stroke_fill=(stroke_rgb + (255,)) if stroke_rgb else None)

    save(img)
    meta.update({
        "font_size": base,
        "lines": [[words[wi] for wi in row] for row in rows],
        "accents": sorted(accents | {wi for li in accent_rows for wi in rows[li]}),
        "block": tuple(int(v) for v in block_box),
    })
    return meta


def preview_background(width: int, height: int):
    """Deterministic stand-in artwork for the Styles-tab live preview: a dark
    cinematic gradient with a soft light bloom (no GPU, no randomness)."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    grad = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        grad.putpixel((0, y), tuple(int(a + (b - a) * t)
                                    for a, b in zip((40, 48, 68), (9, 11, 17))))
    img = grad.resize((width, height))
    bloom = Image.new("L", (width, height), 0)
    ImageDraw.Draw(bloom).ellipse(
        (width * 0.55, -height * 0.35, width * 1.30, height * 0.45), fill=85)
    bloom = bloom.filter(ImageFilter.GaussianBlur(radius=min(width, height) * 0.18))
    return ImageChops.add(img, Image.merge("RGB", (bloom, bloom, bloom)))


# ── pipeline helpers ──────────────────────────────────────────────────────────

# The text-free background sits beside cover.png. Deliberately NOT the legacy
# "cover_base.png" (old pipeline runs left those behind WITH baked-in text —
# compositing onto one would print the title twice).
COVER_BASE_NAME = "cover_bg.png"


def apply_cover_typography(work_dir: Path | str, typo, title: str = "") -> Path | None:
    """Composite the film's cover phrase onto its saved text-free background.

    ``cover_bg.png`` (written at generation time) + cover_phrase → cover.png.
    Returns the cover path, or None when typography is disabled or no text-free
    background exists (legacy covers with baked-in text must be regenerated
    once before re-texting works).
    """
    typo = norm_cover_typography(typo)
    if not typo["enabled"]:
        return None
    wd = Path(work_dir)
    base = wd / COVER_BASE_NAME
    if not base.exists() or base.stat().st_size < 1000:
        return None
    from pipeline.cover import cover_phrase_for

    phrase = cover_phrase_for(wd, title)  # raw: keeps *accent* markup
    out = wd / "cover.png"
    render_cover_typography(base, out, phrase, typo)
    return out
