"""Deterministic cover-title typography.

Instead of asking the diffusion model to draw the title (it misspells words,
and every regeneration is a fresh dice roll), the model generates a TEXT-FREE
background and this module composites the title on top with real fonts. This
is the ONLY way covers are made — the look (font, position, colours, accent
words, card) is a per-style setting (``cover_typography``), so text is
pixel-perfect on every cover and regenerating only rerolls the artwork.

Accented words ("some words in a different colour and size") are ONLY the ones
wrapped in asterisks in the cover phrase: ``The *Secret* War``. The style's
accent rule (first/last/longest word) is written into the title-derived default
phrase as that markup, so a film's phrase is the single source of truth —
strip the asterisks and nothing is accented. A newline in the phrase forces a
line break.
"""
from __future__ import annotations

import re
from pathlib import Path

# Bundled display fonts (assets/fonts/<family>/*.ttf, each with its licence
# file). Thumbnail-grade faces the system font dirs usually lack; styles store
# the font NAME for these (checkout-relative), and a full path for system fonts.
BUNDLED_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# The display faces above are Latin-only: a Chinese or Japanese title drawn in
# one of them comes out as a row of empty .notdef boxes. This bundled face is
# the fallback for phrases they cannot draw (see resolve_font_for_text).
CJK_FALLBACK_FONT = "Noto Sans SC Black"

POSITIONS = ("top", "middle", "bottom")
ALIGNS = ("left", "center", "right")
CASES = ("keep", "upper", "title")
ACCENT_RULES = ("none", "first_word", "last_word", "longest_word")
# Retired rules → their nearest survivor (old configs keep working).
_ACCENT_ALIASES = {"last_line": "last_word"}

DEFAULT_COVER_TYPOGRAPHY = {
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
    font = src.get("font", d["font"])
    d["font"] = str(font).strip() if isinstance(font, (str, Path)) else d["font"]
    if src.get("accent") in _ACCENT_ALIASES:
        src = {**src, "accent": _ACCENT_ALIASES[src["accent"]]}
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


# ── tokens ────────────────────────────────────────────────────────────────────
# Chinese and Japanese are written without spaces, so str.split() sees a whole
# title as ONE word: unwrappable (one small line instead of two big ones) and
# nothing word-shaped for the accent markup to wrap. Runs of those scripts are
# tokenised per CHARACTER instead and drawn with no space between them, which
# lets the fitter break them across lines like an English phrase.

_CJK_RANGES = (
    (0x3040, 0x30FF),    # hiragana + katakana
    (0x3400, 0x4DBF),    # CJK ideographs, extension A
    (0x4E00, 0x9FFF),    # CJK unified ideographs
    (0xF900, 0xFAFF),    # CJK compatibility ideographs
    (0xFF66, 0xFF9F),    # halfwidth katakana
    (0x20000, 0x2A6DF),  # CJK ideographs, extension B
)
# Never allowed to start a line: CJK closing punctuation, and the small kana
# and marks that belong to the character in front of them.
_NO_BREAK_BEFORE = set("、。，．！？：；’”）〕］｝〉》」』】·ー"
                       "ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ"
                       "!?,.:;)]}…")


def is_cjk(ch: str) -> bool:
    """True for characters of scripts written without spaces (Han, kana)."""
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _tokenize(chars, flags: list[bool]) -> tuple[list[str], list[bool], set[int]]:
    """``(tokens, space-before flags, accented token indices)`` for parallel
    sequences of characters and their accent flags."""
    tokens: list[str] = []
    spaced: list[bool] = []
    accents: set[int] = set()
    prev_space = True
    for ch, accented in zip(chars, flags):
        if ch.isspace():
            prev_space = True
            continue
        join = bool(tokens) and not prev_space and (
            ch in _NO_BREAK_BEFORE or not (is_cjk(ch) or is_cjk(tokens[-1][-1])))
        if join:
            tokens[-1] += ch
        else:
            tokens.append(ch)
            spaced.append(prev_space)
        if accented:
            accents.add(len(tokens) - 1)
        prev_space = False
    return tokens, spaced, accents


def tokenize_phrase(clean: str) -> tuple[list[str], list[bool]]:
    """Drawable tokens of a clean phrase, plus whether a space precedes each:
    one token per word for spaced scripts, one per character inside a CJK run."""
    return _tokenize(clean, [False] * len(clean))[:2]


# ── phrase markup ─────────────────────────────────────────────────────────────
# The cover phrase is plain text plus two bits of markup: words wrapped in
# asterisks ("The *Secret* War") get the accent colour/size — and ONLY those
# words do, the style's accent rule is written into the title-derived default
# phrase as asterisks rather than applied at draw time — and a newline forces
# a line break where the fitter would otherwise choose its own.


def split_phrase_markup(phrase: str) -> tuple[str, set[int], set[int]]:
    """Return ``(clean_phrase, accented_token_indices, line_start_indices)``.

    Indices refer to ``tokenize_phrase(clean_phrase)`` — one per word, or one
    per character inside a CJK run. A token is accented when any of its
    characters sat inside an asterisk span. Asterisks never survive into the
    clean phrase (an unpaired one just accents the rest of the phrase). The
    clean phrase keeps one newline per forced break; ``line_start_indices``
    are the tokens that open the second line onwards (empty = let the fitter
    wrap freely).
    """
    lines: list[tuple[list[str], list[bool]]] = [([], [])]
    in_span = False
    for ch in (phrase or "").replace("\r", ""):
        if ch == "*":
            in_span = not in_span
            continue
        if ch == "\n":
            lines.append(([], []))
            continue
        lines[-1][0].append(ch)
        lines[-1][1].append(in_span)
    clean_lines: list[str] = []
    accents: set[int] = set()
    starts: set[int] = set()
    offset = 0
    for chars, flags in lines:
        tokens, _, acc = _tokenize(chars, flags)
        if not tokens:
            continue  # blank lines collapse
        if clean_lines:
            starts.add(offset)
        accents |= {offset + i for i in acc}
        offset += len(tokens)
        clean_lines.append(" ".join("".join(chars).split()))
    return "\n".join(clean_lines), accents, starts


def strip_phrase_markup(phrase: str) -> str:
    """The cover phrase as a single plain line — accent markup and forced
    breaks removed (for prompts/plain overlays)."""
    return " ".join(split_phrase_markup(phrase)[0].split())


def mark_accent(clean: str, rule: str) -> str:
    """Write an accent *rule* into a plain phrase as ``*markup*``:
    ``mark_accent("Hello world", "last_word") == "Hello *world*"``. Used once,
    when the title-derived default phrase is built; the renderer itself only
    honours the asterisks. For CJK runs the "words" are characters, so the
    rule marks one character."""
    tokens, spaced = tokenize_phrase(clean)
    if not tokens or rule not in ACCENT_RULES or rule == "none":
        return clean
    if rule == "first_word":
        idx = 0
    elif rule == "longest_word":
        idx = max(range(len(tokens)), key=lambda i: len(tokens[i]))
    else:  # last_word
        idx = len(tokens) - 1
    out = ""
    for i, tok in enumerate(tokens):
        if i and spaced[i]:
            out += " "
        out += f"*{tok}*" if i == idx else tok
    return out


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


# A permanent Unicode noncharacter: no font maps it, so it always draws the
# face's .notdef box — the yardstick for "this face has no glyph for that char".
_NOTDEF_PROBE = "￿"

_COVERING_CACHE: dict[frozenset, str] = {}


def _glyph(font, ch: str) -> tuple:
    mask = font.getmask(ch)
    return mask.size, bytes(mask)


def _missing_chars(font, text: str) -> set[str]:
    """The characters *font* would draw as .notdef tofu boxes."""
    try:
        notdef = _glyph(font, _NOTDEF_PROBE)
    except Exception:
        return set()
    missing = set()
    for ch in set(text):
        if ch.isspace():
            continue
        try:
            if _glyph(font, ch) == notdef:
                missing.add(ch)
        except Exception:
            missing.add(ch)
    return missing


def _covering_font(text: str) -> str:
    """A font file with glyphs for every character of *text* ("" if none has).

    The bundled CJK face is tried first so Chinese and Japanese covers look the
    same on every host; installed fonts (which cover the scripts the bundle
    does not, e.g. Korean or Arabic) come after, heavy weights first because
    they read better at thumbnail size. Cached — the scan loads every font.
    """
    key = frozenset(text)
    if key in _COVERING_CACHE:
        return _COVERING_CACHE[key]
    from PIL import ImageFont

    from pipeline.cover import available_fonts

    candidates = [resolve_font_path(CJK_FALLBACK_FONT)]
    candidates += [f["path"] for f in sorted(
        available_fonts(), key=lambda f: not re.search(r"black|heavy|bold", f["name"], re.I))]
    best = ""
    for cand in candidates:
        if not cand:
            continue
        try:
            face = ImageFont.truetype(cand, _PROBE)
        except Exception:
            continue
        if not _missing_chars(face, text):
            best = cand
            break
    _COVERING_CACHE[key] = best
    return best


def resolve_font_for_text(font: str, text: str) -> str:
    """The font file used to draw *text*: the style's font whenever it has the
    glyphs, else one that does. Without this a Chinese title set in a Latin
    display face prints as a row of empty boxes."""
    from pipeline.cover import _load_font

    path = resolve_font_path(font)
    if not _missing_chars(_load_font(_PROBE, path), text):
        return path
    return _covering_font(text) or path


# ── layout + render ───────────────────────────────────────────────────────────


def _apply_case(word: str, mode: str) -> str:
    if mode == "upper":
        return word.upper()
    if mode == "title":
        return word[:1].upper() + word[1:]  # keep existing caps ("AI" stays "AI")
    return word


def _partitions(count: int, lines: int):
    """All ways to split ``count`` words into ``lines`` contiguous non-empty runs."""
    if lines == 1:
        yield [count]
        return
    for first in range(1, count - lines + 2):
        for rest in _partitions(count - first, lines - 1):
            yield [first] + rest


def _line_lists(sizes: list[int], spaced: list[bool]) -> list[list[int]]:
    """Token-index lines for the best 1..3-line layout.

    Every viable line count is measured (character-width proxy at this stage);
    the layout that yields the largest fitted font wins in ``_fit`` — here we
    just produce, per line count, the split minimising the widest line.
    """
    out = []
    for n in range(1, min(_MAX_LINES, len(sizes)) + 1):
        best, best_w = None, None
        for split in _partitions(len(sizes), n):
            rows, i = [], 0
            for c in split:
                rows.append(list(range(i, i + c)))
                i += c
            widest = max(sum(sizes[j] for j in row) + sum(spaced[j] for j in row[1:])
                         for row in rows)
            if best_w is None or widest < best_w:
                best, best_w = rows, widest
        out.append(best)
    return out


def _forced_rows(count: int, starts: set[int]) -> list[list[int]]:
    """Token-index lines cut at the phrase's explicit line breaks."""
    rows: list[list[int]] = [[]]
    for i in range(count):
        if i in starts and rows[-1]:
            rows.append([])
        rows[-1].append(i)
    return rows


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

    clean, accents, forced_starts = split_phrase_markup(phrase)
    tokens, spaced = tokenize_phrase(clean)
    words = [_apply_case(w, cfg["case"]) for w in tokens]
    meta = {"font_size": 0, "lines": [], "accents": [], "block": None}
    if not words:
        save(img)
        return meta

    accents = {i for i in accents if i < len(words)}

    font_file = resolve_font_for_text(cfg["font"], "".join(words))
    from pipeline.cover import _load_font

    fonts: dict[int, object] = {}

    def font_at(size: int):
        f = fonts.get(size)
        if f is None:
            f = fonts[size] = _load_font(max(1, int(size)), font_file)
        return f

    probe = font_at(_PROBE)
    draw_probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def measure(line_rows: list[list[int]]):
        """(max width, total height) at probe size, honouring accent scale."""
        space = draw_probe.textlength(" ", font=probe)
        widest, height = 0.0, 0.0
        for li, row in enumerate(line_rows):
            w_line, asc_line, desc_line = 0.0, 0.0, 0.0
            for k, wi in enumerate(row):
                scale = cfg["accent_scale"] if wi in accents else 1.0
                f = font_at(round(_PROBE * scale))
                w_line += draw_probe.textlength(words[wi], font=f)
                if k and spaced[wi]:
                    w_line += space
                asc, desc = f.getmetrics()
                asc_line, desc_line = max(asc_line, asc), max(desc_line, desc)
            widest = max(widest, w_line)
            height += asc_line + desc_line
        height += _PROBE * 0.10 * (len(line_rows) - 1)  # inter-line gap
        return widest, height

    # Pick the line count whose balanced split fits the LARGEST font — unless
    # the phrase carries explicit line breaks, which fix the rows outright.
    max_w_px = W * cfg["width_pct"] / 100.0
    max_h_px = H * _MAX_BLOCK_FRAC
    if forced_starts:
        candidates = [_forced_rows(len(words), forced_starts)]
    else:
        # CJK characters are full-width, so they count double in the width proxy.
        char_sizes = [sum(2 if is_cjk(c) else 1 for c in w) for w in words]
        candidates = _line_lists(char_sizes, spaced)
    best_rows, best_size = [[i for i in range(len(words))]], 0.0
    for rows in candidates:
        w, h = measure(rows)
        size = _PROBE * min(max_w_px / w, max_h_px / h)
        if size > best_size:
            best_rows, best_size = rows, size
    rows = best_rows

    base = int(best_size * cfg["scale"] * 0.98)  # 2% slack for metric nonlinearity
    base = max(10, min(base, int(H * 0.5)))

    def word_scale(wi: int) -> float:
        return cfg["accent_scale"] if wi in accents else 1.0

    # Real-metric pass at the chosen size; shrink once if a line still overflows.
    draw = ImageDraw.Draw(img)
    for _ in range(2):
        space_w = draw.textlength(" ", font=font_at(base))
        line_dims = []  # (width, ascent, descent) per line
        for li, row in enumerate(rows):
            w_line, asc_line, desc_line = 0.0, 0, 0
            for k, wi in enumerate(row):
                f = font_at(round(base * word_scale(wi)))
                w_line += draw.textlength(words[wi], font=f)
                if k and spaced[wi]:
                    w_line += space_w
                asc, desc = f.getmetrics()
                asc_line, desc_line = max(asc_line, asc), max(desc_line, desc)
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
        for k, wi in enumerate(row):
            f = font_at(round(base * word_scale(wi)))
            accented = wi in accents
            if k and spaced[wi]:
                x += space_w
            placed.append((words[wi], x, baseline, f, accented))
            x += draw.textlength(words[wi], font=f)
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
        "accents": sorted(accents),
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
    Returns the cover path, or None when no text-free background exists
    (legacy covers with baked-in text must be regenerated once before
    re-texting works).
    """
    typo = norm_cover_typography(typo)
    wd = Path(work_dir)
    base = wd / COVER_BASE_NAME
    if not base.exists() or base.stat().st_size < 1000:
        return None
    from pipeline.cover import cover_phrase_for

    phrase = cover_phrase_for(wd, title, typo["accent"])  # raw: keeps markup
    out = wd / "cover.png"
    render_cover_typography(base, out, phrase, typo)
    return out
