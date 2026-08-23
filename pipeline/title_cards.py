"""Opening title and end-credit cards for a finished film.

A card is a still — a solid colour or the user's own image — with text drawn
in the film's display fonts, held for a few seconds with a fade in and out.
Any number of cards stack in order at the **start** (prepended, before the
film) or the **end** (appended), so an opening can be several cards in a row
— a title, then a dedication, then a chapter line. The film itself is
untouched (a post-production step on the rendered film, like the
first-frame cover burn).

Prepending changes the timeline, so two things track what was stamped:

* ``title_cards_applied.json`` beside the final records the head/tail
  lengths and the stamped file's duration. A final whose duration matches is
  "titled" — applying again strips the old cards first (never doubling up),
  "remove" trims exactly the stamped lengths, and a rebuilt final (remix,
  new narrator, reassemble — all start from combined.mp4, never titled) is
  recognised as clean. Duration survives an upscale; file size does not.
* Soft caption tracks are shifted by the opening card's length (see
  ``pipeline.captions.build_srt(offset=...)``). Burned captions are drawn
  before the cards go on, so they need no shift.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

APPLIED_NAME = "title_cards_applied.json"

SECONDS_MIN, SECONDS_MAX, SECONDS_DEFAULT = 1.0, 20.0, 4.0
FADE_MIN, FADE_MAX, FADE_DEFAULT = 0.0, 3.0, 0.6
BACKGROUNDS = ("color", "image")
PLACEMENTS = ("start", "end")
MAX_CARDS = 12

DEFAULT_CARD = {
    "id": "",                # stable per-card key (also names its uploaded still)
    "placement": "start",    # start (before the film) | end (after it)
    "text": "",
    "background": "color",   # color | image (the card's uploaded still)
    "color": "#000000",      # solid background
    "seconds": SECONDS_DEFAULT,
    # The look is per card — a title card and a credits card rarely share one.
    "font": "",              # bundled font name / font file; "" = the style's cover font
    "text_color": "#FFFFFF",
    "scale": 1.0,            # size multiplier on the auto-fitted text
    "fade": FADE_DEFAULT,    # fade in/out, seconds
}

DEFAULT_TITLE_CARDS = {
    "cards": [],             # ordered; start cards play in list order, so do end cards
}

# Tolerance when matching a final's duration against the stamped record.
_DURATION_TOLERANCE = 0.25


def _hex_color(value, fallback: str) -> str:
    s = str(value or "").strip()
    if len(s) == 7 and s[0] == "#":
        try:
            int(s[1:], 16)
            return s.upper()
        except ValueError:
            pass
    return fallback


def _clamp(value, lo: float, hi: float, fallback: float) -> float:
    try:
        return round(min(hi, max(lo, float(value))), 2)
    except (TypeError, ValueError):
        return fallback


def norm_card_id(value) -> str:
    """A card id is a short [a-z0-9_-] token: it names the card's still on
    disk, so nothing path-like gets through."""
    return re.sub(r"[^a-z0-9_-]", "", str(value or "").lower())[:32]


def norm_card(value, index: int = 0, shared: dict | None = None) -> dict:
    """One card, fully typed. *shared* is the pre-per-card-look shape's
    top-level font/text_color/scale/fade — films saved by that build keep
    their look when a card carries none of its own."""
    raw = value if isinstance(value, dict) else {}
    shared = shared or {}

    def look(key):
        return raw[key] if key in raw else shared.get(key)

    return {
        "id": norm_card_id(raw.get("id")) or f"card{index + 1}",
        "placement": raw.get("placement") if raw.get("placement") in PLACEMENTS else "start",
        "text": str(raw.get("text") or "").strip(),
        "background": (raw.get("background") if raw.get("background") in BACKGROUNDS
                       else "color"),
        "color": _hex_color(raw.get("color"), DEFAULT_CARD["color"]),
        "seconds": _clamp(raw.get("seconds"), SECONDS_MIN, SECONDS_MAX, SECONDS_DEFAULT),
        "font": str(look("font") or "").strip(),
        "text_color": _hex_color(look("text_color"), DEFAULT_CARD["text_color"]),
        "scale": _clamp(look("scale"), 0.4, 2.5, 1.0),
        "fade": _clamp(look("fade"), FADE_MIN, FADE_MAX, FADE_DEFAULT),
    }


def norm_title_cards(value) -> dict:
    """Coerce a stored/posted title-cards dict to the full, typed shape.
    Card ids are made unique (a duplicate gets a numbered suffix)."""
    raw = value if isinstance(value, dict) else {}
    cards, seen = [], set()
    raw_cards = raw.get("cards") if isinstance(raw.get("cards"), list) else []
    for i, c in enumerate(raw_cards[:MAX_CARDS]):
        card = norm_card(c, i, shared=raw)
        base, n = card["id"], 2
        while card["id"] in seen:
            card["id"] = f"{base}_{n}"
            n += 1
        seen.add(card["id"])
        cards.append(card)
    return {"cards": cards}


def card_image_path(work_dir: Path | str, card_id: str) -> Path:
    """The still uploaded for one card."""
    return Path(work_dir) / f"title_card_{norm_card_id(card_id) or 'card'}.png"


# ── rendering ─────────────────────────────────────────────────────────────────


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def render_card(out_path: Path | str, width: int, height: int, text: str, *,
                background: str = "color", color: str = "#000000",
                image_path: Path | str | None = None, font: str = "",
                text_color: str = "#FFFFFF", scale: float = 1.0) -> dict:
    """Draw one card to *out_path* (PNG, width×height).

    Lines are the text's own newlines; the block is auto-sized so the longest
    line spans at most ~72% of the width and the block at most ~60% of the
    height, then multiplied by *scale*. An image background is cover-cropped
    to the frame. Returns ``{"font_size", "lines"}`` for tests."""
    from PIL import Image, ImageDraw, ImageFilter, ImageOps

    from pipeline.cover import _load_font
    from pipeline.cover_typography import resolve_font_for_text

    if background == "image" and image_path and Path(image_path).exists():
        img = ImageOps.fit(Image.open(image_path).convert("RGB"), (width, height)).convert("RGBA")
    else:
        img = Image.new("RGBA", (width, height), (*_rgb(color), 255))

    lines = [ln.rstrip() for ln in str(text or "").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        img.convert("RGB").save(str(out_path), "PNG")
        return {"font_size": 0, "lines": []}

    font_path = resolve_font_for_text(font, "".join(lines))
    probe = 100
    pf = _load_font(probe, font_path)
    widest = max((pf.getlength(ln) for ln in lines if ln.strip()), default=1.0)
    line_gap = 1.25
    size_w = probe * (width * 0.72) / max(1.0, widest)
    size_h = probe * (height * 0.60) / (len(lines) * line_gap)
    size = max(12, int(min(size_w, size_h) * max(0.1, scale)))
    fnt = _load_font(size, font_path)

    step = int(size * line_gap)
    block_h = step * len(lines)
    y = (height - block_h) // 2
    fg = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    for ln in lines:
        if ln.strip():
            w = fnt.getlength(ln)
            d.text(((width - w) / 2, y), ln, font=fnt, fill=(*_rgb(text_color), 255))
        y += step

    if background == "image":
        # A soft shadow keeps the text legible over any picture.
        shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        alpha = fg.split()[3].filter(ImageFilter.GaussianBlur(radius=max(2, size * 0.06)))
        shadow.putalpha(alpha.point(lambda a: min(255, int(a * 1.4))))
        img = Image.alpha_composite(img, shadow)
    img = Image.alpha_composite(img, fg)
    img.convert("RGB").save(str(out_path), "PNG")
    return {"font_size": size, "lines": lines}


def card_clip(image_path: Path | str, out_path: Path | str, seconds: float, *,
              width: int, height: int, fps: float, fade: float,
              sample_rate: int = 48000) -> Path:
    """A held still with fade in/out + a silent stereo track, encoded to
    match the film so the concat needs no surprises."""
    from pipeline.assembler import _FFMPEG, _run

    seconds = float(seconds)
    fade = max(0.0, min(float(fade), seconds / 2))
    vf = [f"scale={width}:{height}", "setsar=1", f"fps={fps:g}", "format=yuv420p"]
    if fade > 0:
        vf.append(f"fade=t=in:st=0:d={fade:.2f}")
        vf.append(f"fade=t=out:st={seconds - fade:.3f}:d={fade:.2f}")
    _run([
        _FFMPEG, "-y",
        "-loop", "1", "-framerate", f"{fps:g}", "-i", str(image_path),
        "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo",
        "-t", f"{seconds:.3f}",
        "-vf", ",".join(vf),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        "-movflags", "+faststart",
        str(out_path),
    ], timeout=600)
    return Path(out_path)


def _concat(parts: list[Path], out_path: Path, *, width: int, height: int, fps: float) -> Path:
    """Re-encode join (the concat filter), normalising size/fps/audio so a
    card and an upscaled or stream-copied film always line up."""
    from pipeline.assembler import _FFMPEG, _FILM_AR, _has_audio_stream, _run, _get_duration

    inputs: list[str] = []
    for p in parts:
        inputs += ["-i", str(p)]
    n = len(parts)
    filters = []
    next_input = n
    for i, p in enumerate(parts):
        filters.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps:g},format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
        if _has_audio_stream(p):
            a_src = i
        else:
            inputs += ["-f", "lavfi", "-t", f"{_get_duration(p):.3f}",
                       "-i", f"anullsrc=r={_FILM_AR}:cl=stereo"]
            a_src = next_input
            next_input += 1
        filters.append(
            f"[{a_src}:a]aresample={_FILM_AR},"
            f"aformat=sample_rates={_FILM_AR}:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]")
    filters.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vout]")
    filters.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[aout]")
    _run([
        _FFMPEG, "-y", *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ], timeout=3600)
    return out_path


# ── applied-state record ──────────────────────────────────────────────────────


def _applied_path(work_dir: Path | str) -> Path:
    return Path(work_dir) / APPLIED_NAME


def applied_title_cards(work_dir: Path | str, final_path: Path | str) -> dict | None:
    """``{"head", "tail", "duration"}`` when *final_path* is the cut the
    cards were stamped on (its duration matches the record), else None."""
    from pipeline.assembler import _get_duration

    path = _applied_path(work_dir)
    final_path = Path(final_path)
    if not path.exists() or not final_path.exists():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        head, tail, dur = float(rec["head"]), float(rec["tail"]), float(rec["duration"])
    except Exception:
        return None
    if head <= 0 and tail <= 0:
        return None
    try:
        if abs(_get_duration(final_path) - dur) > _DURATION_TOLERANCE:
            return None
    except Exception:
        return None
    return {"head": head, "tail": tail, "duration": dur}


def head_seconds(work_dir: Path | str, final_path: Path | str) -> float:
    """Length of the opening card on the published cut (0 when none) — the
    shift every soft caption track needs."""
    rec = applied_title_cards(work_dir, final_path)
    return float(rec["head"]) if rec else 0.0


def strip_title_cards(final_path: Path | str, work_dir: Path | str) -> bool:
    """Trim the stamped cards off *final_path* in place. Returns whether
    anything was trimmed (False when the cut carries no cards)."""
    from pipeline.assembler import _FFMPEG, _get_duration, _run

    final_path = Path(final_path)
    rec = applied_title_cards(work_dir, final_path)
    if not rec:
        return False
    body = _get_duration(final_path) - rec["head"] - rec["tail"]
    staged = final_path.with_name(f"{final_path.stem}.untitled.tmp{final_path.suffix}")
    logger.info("[ffmpeg] strip_title_cards: %s (head %.2fs, tail %.2fs)",
                final_path.name, rec["head"], rec["tail"])
    try:
        _run([
            _FFMPEG, "-y",
            "-i", str(final_path),
            "-ss", f"{rec['head']:.3f}", "-t", f"{body:.3f}",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(staged),
        ], timeout=3600)
        staged.replace(final_path)
    finally:
        staged.unlink(missing_ok=True)
    _applied_path(work_dir).unlink(missing_ok=True)
    return True


def apply_title_cards(final_path: Path | str, work_dir: Path | str, cfg: dict, *,
                      title: str = "", default_font: str = "") -> dict:
    """Stamp the configured cards onto *final_path* in place: every "start"
    card in list order before the film, every "end" card in list order after.

    A cut that already carries cards is stripped first so the new cards
    replace rather than stack. Returns the applied record
    ``{"head", "tail", "duration"}`` (the total seconds stacked at each end)."""
    from pipeline.assembler import (_FILM_AR, _get_duration, _get_video_dimensions,
                                    _video_frame_rates)

    final_path, work_dir = Path(final_path), Path(work_dir)
    cfg = norm_title_cards(cfg)
    if not (final_path.exists() and final_path.stat().st_size > 0):
        raise FileNotFoundError("Final video not found; render the film first.")
    if not cfg["cards"]:
        raise ValueError("Add a card first.")

    strip_title_cards(final_path, work_dir)
    width, height = _get_video_dimensions(final_path)
    fps = _video_frame_rates(final_path)[0]

    with tempfile.TemporaryDirectory(prefix="titlecards-") as td:
        td = Path(td)
        head_parts: list[Path] = []
        tail_parts: list[Path] = []
        head = tail = 0.0
        for card in cfg["cards"]:
            text = card["text"] or (title if card["placement"] == "start" else "")
            png = td / f"{card['id']}.png"
            render_card(png, width, height, text,
                        background=card["background"], color=card["color"],
                        image_path=card_image_path(work_dir, card["id"]),
                        font=card["font"] or default_font,
                        text_color=card["text_color"], scale=card["scale"])
            clip = card_clip(png, td / f"{card['id']}.mp4", card["seconds"],
                             width=width, height=height, fps=fps, fade=card["fade"],
                             sample_rate=_FILM_AR)
            if card["placement"] == "start":
                head_parts.append(clip)
                head += card["seconds"]
            else:
                tail_parts.append(clip)
                tail += card["seconds"]
        staged = final_path.with_name(f"{final_path.stem}.titled.tmp{final_path.suffix}")
        logger.info("[ffmpeg] apply_title_cards: %s (%d start card(s) %.2fs, %d end card(s) %.2fs)",
                    final_path.name, len(head_parts), head, len(tail_parts), tail)
        try:
            _concat([*head_parts, final_path, *tail_parts], staged,
                    width=width, height=height, fps=fps)
            staged.replace(final_path)
        finally:
            staged.unlink(missing_ok=True)

    rec = {"head": round(head, 3), "tail": round(tail, 3), "duration": _get_duration(final_path)}
    _applied_path(work_dir).write_text(json.dumps(rec), encoding="utf-8")
    return rec
