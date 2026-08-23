"""Per-style look of burned-in subtitles (open captions).

The burn itself is ffmpeg's ``subtitles`` filter (libass). Left alone it draws
every film the same way — Arial, white, bottom-centre. A style's
``subtitle_style`` dict (font, size, colours, outline, box, position) is turned
into an ASS ``force_style`` override here, the same way ``cover_typography``
decides how cover titles look: fonts come from the bundled set in
``assets/fonts`` or the machine's font folders, picked in Settings → Styles.

libass finds fonts through fontconfig by family NAME, not by file path, so the
chosen file is copied beside the SRT and handed over with ``fontsdir`` — which
makes bundled and system fonts work identically, on any machine.
"""
from __future__ import annotations

import re
from pathlib import Path

from pipeline.cover_typography import _clamp, _hex_color, resolve_font_path

POSITIONS = ("top", "middle", "bottom")
ALIGNS = ("left", "center", "right")

# ffmpeg converts SRT to ASS with PlayResY=288 and FontSize=16 — that default
# is what "scale 1.0" means, so an untouched style burns exactly as before.
_BASE_FONT_SIZE = 16

DEFAULT_SUBTITLE_STYLE = {
    "font": "",                 # "" = libass default; bundled name or system font path
    "scale": 1.0,               # size multiplier on the default subtitle size
    "bold": False,
    "color": "#FFFFFF",
    "position": "bottom",       # top | middle | bottom
    "align": "center",          # left | center | right
    "outline": 1.0,             # stroke width (0 = none)
    "outline_color": "#000000",
    "shadow": False,            # drop shadow under the text
    "card": False,              # opaque box behind each line
    "card_color": "#000000",
    "card_opacity": 0.55,
    "margin": 4,                # distance from the edge, % of the picture height
    # Timing (pipeline/captions.py): a cue shorter than min_seconds is merged
    # with the next one on its scene into a two-line cue, or held longer;
    # 0 = every line exactly as paced. delay shifts every cue later (seconds;
    # negative = earlier) to nudge a track that reads early or late.
    "min_seconds": 2.5,
    "delay": 0.0,
}

# ASS numpad alignment: rows bottom/middle/top × columns left/centre/right.
_ALIGNMENT = {
    ("bottom", "left"): 1, ("bottom", "center"): 2, ("bottom", "right"): 3,
    ("middle", "left"): 4, ("middle", "center"): 5, ("middle", "right"): 6,
    ("top", "left"): 7, ("top", "center"): 8, ("top", "right"): 9,
}


def norm_subtitle_style(value) -> dict:
    """Coerce a style's ``subtitle_style`` to a full, valid settings dict."""
    src = value if isinstance(value, dict) else {}
    d = dict(DEFAULT_SUBTITLE_STYLE)
    font = src.get("font", d["font"])
    d["font"] = str(font).strip() if isinstance(font, (str, Path)) else d["font"]
    for key, allowed in (("position", POSITIONS), ("align", ALIGNS)):
        v = str(src.get(key, d[key]) or "").strip().lower()
        d[key] = v if v in allowed else DEFAULT_SUBTITLE_STYLE[key]
    d["scale"] = round(_clamp(src.get("scale", d["scale"]), 0.5, 2.5, d["scale"]), 2)
    d["outline"] = round(_clamp(src.get("outline", d["outline"]), 0, 4, d["outline"]), 1)
    d["card_opacity"] = round(_clamp(src.get("card_opacity", d["card_opacity"]), 0.05, 1.0, d["card_opacity"]), 2)
    d["margin"] = int(_clamp(src.get("margin", d["margin"]), 0, 40, d["margin"]))
    d["min_seconds"] = round(_clamp(src.get("min_seconds", d["min_seconds"]), 0, 10, d["min_seconds"]), 2)
    d["delay"] = round(_clamp(src.get("delay", d["delay"]), -5, 5, d["delay"]), 2)
    d["color"] = _hex_color(src.get("color"), d["color"])
    d["outline_color"] = _hex_color(src.get("outline_color"), d["outline_color"])
    d["card_color"] = _hex_color(src.get("card_color"), d["card_color"])
    d["bold"] = bool(src.get("bold", d["bold"]))
    d["shadow"] = bool(src.get("shadow", d["shadow"]))
    d["card"] = bool(src.get("card", d["card"]))
    return d


def _ass_color(hex_color: str, alpha: float = 0.0) -> str:
    """``#RRGGBB`` + opacity (0..1 transparent fraction) → ASS ``&HAABBGGRR``."""
    r, g, b = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    a = int(round(255 * min(1.0, max(0.0, alpha))))
    return f"&H{a:02X}{b}{g}{r}".upper()


# Plain words only in the ASS style string: a stray comma, colon or quote in a
# font family name would split the override. Family names are drawn from real
# font files so this is belt-and-braces.
_SAFE_FONT_NAME = re.compile(r"[^\w .\-]")


def font_family_name(font_path: str) -> str:
    """The family name libass matches ``FontName`` against, read from the file."""
    try:
        from PIL import ImageFont
        name = ImageFont.truetype(font_path, 24).getname()[0]
    except Exception:
        name = Path(font_path).stem
    return _SAFE_FONT_NAME.sub("", name).strip()


def ass_force_style(style: dict, font_name: str = "") -> str:
    """The ``force_style`` override string for a normalised style dict.
    *font_name* is the resolved family name (blank keeps libass's default)."""
    s = norm_subtitle_style(style)
    parts = []
    if font_name:
        parts.append(f"FontName={font_name}")
    parts += [
        f"FontSize={max(1, round(_BASE_FONT_SIZE * s['scale']))}",
        f"Bold={-1 if s['bold'] else 0}",
        f"PrimaryColour={_ass_color(s['color'])}",
        f"OutlineColour={_ass_color(s['outline_color'])}",
        f"Outline={s['outline']:g}",
        f"Shadow={1 if s['shadow'] else 0}",
        f"Alignment={_ALIGNMENT[(s['position'], s['align'])]}",
    ]
    # Margins are in PlayResY units (288 tall): % of the picture height.
    mv = round(288 * s["margin"] / 100)
    parts.append(f"MarginV={mv}")
    parts.append(f"MarginL={mv}")
    parts.append(f"MarginR={mv}")
    if s["card"]:
        # BorderStyle 4 (libass): an opaque box in BackColour behind each
        # line, keeping the text outline — 3 would recolour the box with the
        # outline colour and lose the stroke.
        parts.append("BorderStyle=4")
        parts.append(f"BackColour={_ass_color(s['card_color'], 1.0 - s['card_opacity'])}")
    return ",".join(parts)


def _escape_filter_path(path: str) -> str:
    """Escape a path for use inside a filter option value (':' and '\\')."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def subtitles_filter(srt_path: Path, style: dict | None, fonts_dir: Path) -> str:
    """The ``-vf`` expression burning *srt_path* in *style*.

    *fonts_dir* is a scratch directory the chosen font file gets copied into
    (then passed as libass's ``fontsdir``), so the family name resolves even
    for fonts fontconfig has never seen. Paths must be special-character free
    (callers stage the SRT in a temp dir for exactly that reason).
    """
    import shutil

    s = norm_subtitle_style(style)
    font_name = ""
    font_path = resolve_font_path(s["font"]) if s["font"] else ""
    if font_path:
        fonts_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(font_path, fonts_dir / Path(font_path).name)
        font_name = font_family_name(font_path)
    opts = [f"subtitles={srt_path}"]
    if font_name:
        opts.append(f"fontsdir={_escape_filter_path(str(fonts_dir))}")
    force = ass_force_style(s, font_name)
    opts.append(f"force_style='{force}'")
    return ":".join(opts)
