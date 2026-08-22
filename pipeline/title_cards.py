"""Opening title and end-credit cards for a finished film.

A card is a still — a solid colour or the user's own image — with text drawn
in the film's display fonts, held for a few seconds with a fade in and out.
The opening card is prepended to the published final and the credits card is
appended; the film itself is untouched (a post-production step on the
rendered film, like the first-frame cover burn).

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
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

APPLIED_NAME = "title_cards_applied.json"
OPENING_IMAGE = "title_opening.png"
CREDITS_IMAGE = "title_credits.png"

SECONDS_MIN, SECONDS_MAX, SECONDS_DEFAULT = 1.0, 20.0, 4.0
FADE_MIN, FADE_MAX, FADE_DEFAULT = 0.0, 3.0, 0.6
BACKGROUNDS = ("color", "image")

# The two cards differ only in their defaults.
_DEFAULT_CARD = {
    "enabled": False,
    "text": "",
    "background": "color",   # color | image (the uploaded still)
    "color": "#000000",      # solid background
    "seconds": SECONDS_DEFAULT,
}

DEFAULT_TITLE_CARDS = {
    "opening": dict(_DEFAULT_CARD),
    "credits": dict(_DEFAULT_CARD, seconds=6.0),
    "font": "",              # bundled font name / font file; "" = the style's cover font
    "text_color": "#FFFFFF",
    "fade": FADE_DEFAULT,    # fade in/out on each card, seconds
    "scale": 1.0,            # size multiplier on the auto-fitted text
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


def _norm_card(value, default: dict) -> dict:
    raw = value if isinstance(value, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", default["enabled"])),
        "text": str(raw.get("text") or "").strip(),
        "background": (raw.get("background") if raw.get("background") in BACKGROUNDS
                       else default["background"]),
        "color": _hex_color(raw.get("color"), default["color"]),
        "seconds": _clamp(raw.get("seconds"), SECONDS_MIN, SECONDS_MAX, default["seconds"]),
    }


def norm_title_cards(value) -> dict:
    """Coerce a stored/posted title-cards dict to the full, typed shape."""
    raw = value if isinstance(value, dict) else {}
    return {
        "opening": _norm_card(raw.get("opening"), DEFAULT_TITLE_CARDS["opening"]),
        "credits": _norm_card(raw.get("credits"), DEFAULT_TITLE_CARDS["credits"]),
        "font": str(raw.get("font") or "").strip(),
        "text_color": _hex_color(raw.get("text_color"), DEFAULT_TITLE_CARDS["text_color"]),
        "fade": _clamp(raw.get("fade"), FADE_MIN, FADE_MAX, FADE_DEFAULT),
        "scale": _clamp(raw.get("scale"), 0.4, 2.5, 1.0),
    }


def card_image_path(work_dir: Path | str, which: str) -> Path:
    return Path(work_dir) / (OPENING_IMAGE if which == "opening" else CREDITS_IMAGE)


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
    """Stamp the configured cards onto *final_path* in place.

    A cut that already carries cards is stripped first so the new cards
    replace rather than stack. Returns the applied record
    ``{"head", "tail", "duration"}`` (head/tail 0 when a card is off)."""
    from pipeline.assembler import (_FILM_AR, _get_duration, _get_video_dimensions,
                                    _video_frame_rates)

    final_path, work_dir = Path(final_path), Path(work_dir)
    cfg = norm_title_cards(cfg)
    if not (final_path.exists() and final_path.stat().st_size > 0):
        raise FileNotFoundError("Final video not found; render the film first.")
    if not (cfg["opening"]["enabled"] or cfg["credits"]["enabled"]):
        raise ValueError("Switch on the opening title or the end credits first.")

    strip_title_cards(final_path, work_dir)
    width, height = _get_video_dimensions(final_path)
    fps = _video_frame_rates(final_path)[0]
    font = cfg["font"] or default_font

    with tempfile.TemporaryDirectory(prefix="titlecards-") as td:
        td = Path(td)
        parts: list[Path] = []
        head = tail = 0.0
        for which in ("opening", "credits"):
            card = cfg[which]
            if not card["enabled"]:
                if which == "opening":
                    parts.append(final_path)
                continue
            text = card["text"] or (title if which == "opening" else "")
            png = td / f"{which}.png"
            render_card(png, width, height, text,
                        background=card["background"], color=card["color"],
                        image_path=card_image_path(work_dir, which), font=font,
                        text_color=cfg["text_color"], scale=cfg["scale"])
            clip = card_clip(png, td / f"{which}.mp4", card["seconds"],
                             width=width, height=height, fps=fps, fade=cfg["fade"],
                             sample_rate=_FILM_AR)
            parts.append(clip)
            if which == "opening":
                head = card["seconds"]
                parts.append(final_path)
            else:
                tail = card["seconds"]
        staged = final_path.with_name(f"{final_path.stem}.titled.tmp{final_path.suffix}")
        logger.info("[ffmpeg] apply_title_cards: %s (head %.2fs, tail %.2fs)",
                    final_path.name, head, tail)
        try:
            _concat(parts, staged, width=width, height=height, fps=fps)
            staged.replace(final_path)
        finally:
            staged.unlink(missing_ok=True)

    rec = {"head": head, "tail": tail, "duration": _get_duration(final_path)}
    _applied_path(work_dir).write_text(json.dumps(rec), encoding="utf-8")
    return rec
