"""Voice cadence (words per minute): measurement, storage, and length planning.

Videos are commissioned by TARGET LENGTH (minutes), not scene count. To turn a
length into a script we need each narrator's cadence — how many words per
minute that voice actually speaks. This module owns that number:

* **Store** — ``voice_cadence.json`` beside config.yaml maps
  ``"<voice>|<engine>"`` to a measured words-per-minute figure. Until a voice
  is measured, :data:`DEFAULT_WPM` (a calm documentary pace) is used.
* **Learning** — every real narration render is a measurement: the synthesized
  WAV's duration (minus spliced pause silence, divided by the applied speed
  multiplier) against its word count. ``generate_narration`` feeds samples in
  via :func:`record_sample`, so cadences converge on reality with zero extra
  synthesis. :func:`calibrate_voice` measures a voice explicitly by
  synthesizing a fixed calibration passage once.
* **Speed = cadence** — the per-style "voice speed" knob is expressed as a
  target cadence (``voice_cadence_wpm``); :func:`resolve_voice_speed` turns it
  into the multiplier the TTS engines understand (target ÷ natural). Legacy
  ``voice_speed`` multipliers still resolve when no cadence is set.
* **Planning** — :func:`plan_script` converts minutes × cadence into a word
  budget and a scene plan where every scene is 10–15 seconds of narration
  (never more), i.e. roughly one sentence. :func:`split_narration` is the
  deterministic backstop: an over-long narration is split at sentence ends,
  then at natural pauses (commas, dashes) inside an over-long sentence — the
  only places a scene change can land mid-sentence without sounding cut.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import time
import wave
from pathlib import Path

from pipeline import tts_text

logger = logging.getLogger("video_gen")

# Fallback speaking rate until a voice has been measured — a calm documentary
# pace (the old "2 sentences ≈ 18-22 words ≈ 9 s" rule of thumb ≈ 133 wpm was
# on the slow side of real F5 output; measured library voices land 130–170).
DEFAULT_WPM = 150.0

# Scene duration contract: every scene's narration is 10–15 s at the
# narrator's cadence — never more — aiming mid-range so LLM drift stays in
# bounds. One scene ≈ one sentence.
SCENE_MIN_SECS = 10.0
SCENE_MAX_SECS = 15.0
SCENE_TARGET_SECS = 12.0

# Chained scenes (styles with ``h3_chain_scenes`` on). H3 cannot render past
# SCENE_MAX_SECS in one pass, but two clips can be joined by H3 Motion Context
# so the seam reads as motion rather than a cut, letting one scene run about
# twice as long. The join costs the frames it pins to carry motion across:
# those arrive at the head of the second clip and are trimmed before
# concatenation, so a chained scene DELIVERS less than it sampled. Measured on
# GB10 at the node's 22-frame default — 294 sampled, 272 delivered.
CHAIN_CLIPS = 2
CHAIN_JOIN_SECS = 22 / 24.0


def scene_window(chained: bool = False) -> tuple[float, float, float]:
    """(min, target, max) seconds of narration for one scene.

    Unchained is the model's own 10–15 s contract. Chained multiplies it by
    CHAIN_CLIPS and pays CHAIN_JOIN_SECS per join, so callers budget against
    what a scene actually delivers rather than what it sampled."""
    if not chained:
        return SCENE_MIN_SECS, SCENE_TARGET_SECS, SCENE_MAX_SECS
    lost = CHAIN_JOIN_SECS * (CHAIN_CLIPS - 1)
    return (SCENE_MIN_SECS * CHAIN_CLIPS - lost,
            SCENE_TARGET_SECS * CHAIN_CLIPS - lost,
            SCENE_MAX_SECS * CHAIN_CLIPS - lost)

# Mirror of generate_narration's speed clamp.
SPEED_MIN, SPEED_MAX = 0.3, 2.0

# What one scene amounted to BEFORE lengths existed ("2 sentences, ~18-22
# words, ~9 s") — used only to interpret legacy scene counts as minutes.
LEGACY_SCENE_SECS = 9.0

# Longest plannable video: the MAX_SCENES cap (200) at the scene target.
MAX_MINUTES = 200 * SCENE_TARGET_SECS / 60.0


def max_minutes(chained: bool = False) -> float:
    """MAX_MINUTES for the given chaining mode — chained scenes are longer, so
    the same 200-scene cap reaches further."""
    return 200 * scene_window(chained)[1] / 60.0

# Sanity range for measured cadences — samples outside are discarded (a
# mis-measured duration, a transcript mismatch, a silence-heavy take).
_WPM_SANE_MIN, _WPM_SANE_MAX = 60.0, 300.0

# Rolling-average horizon: old samples decay so a re-recorded voice converges
# within ~this many narrations.
_MAX_EFFECTIVE_SAMPLES = 20

_CONFIG_DIR = Path.home() / ".config" / "video-generator"

_DEFAULT_VOICE_KEY = "__default__"

# ~60 words of neutral documentary prose for explicit calibration — plain
# vocabulary, no digits or abbreviations (engines expand those unpredictably).
CALIBRATION_TEXT = (
    "Every great story begins with a question that refuses to let go. "
    "Across distant mountains and quiet valleys, people have searched for "
    "answers hidden in plain sight. Some found them in old letters, others "
    "in the patterns of the stars. What follows is one of those journeys, "
    "told slowly and carefully, so that every detail finds its place."
)

# CJK ideographs and kana have no spaces — count each as a word so cadence
# and budgets stay meaningful for the multilingual TTS languages.
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")


def word_count(text: str) -> int:
    """Spoken-word count of narration text (pause markers excluded)."""
    plain = tts_text.strip_pause_markers(text or "")
    cjk = len(_CJK_RE.findall(plain))
    rest = _CJK_RE.sub(" ", plain)
    return cjk + len([t for t in rest.split() if any(c.isalnum() for c in t)])


def wav_seconds(path: Path) -> float:
    """Duration of a WAV file in seconds (0.0 when unreadable)."""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / float(rate) if rate else 0.0
    except Exception:  # noqa: BLE001 — measurement is always best-effort
        return 0.0


# ── Cadence store ─────────────────────────────────────────────────────────────

def _store_path() -> Path:
    return Path(os.environ.get("VOICE_CADENCE_FILE",
                               str(_CONFIG_DIR / "voice_cadence.json")))


def _key(voice_name: str | None, engine: str | None) -> str:
    name = (voice_name or "").strip()
    # The picker's "Default (F5-TTS)" option IS the bundled default narrator.
    if name.lower() == "default (f5-tts)":
        name = ""
    return f"{name or _DEFAULT_VOICE_KEY}|{(engine or 'openf5').strip()}"


def load_store() -> dict:
    try:
        data = json.loads(_store_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt file → empty store
        return {}


def _save_store(store: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(store, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def natural_wpm(voice_name: str | None, engine: str | None = None) -> tuple[float, bool]:
    """(cadence, measured?) for a voice at speed 1.0 — DEFAULT_WPM until measured."""
    entry = load_store().get(_key(voice_name, engine))
    if entry and entry.get("wpm"):
        try:
            return float(entry["wpm"]), True
        except (TypeError, ValueError):
            pass
    return DEFAULT_WPM, False


def record_sample(voice_name: str | None, engine: str | None, words: int,
                  seconds: float, speed: float = 1.0,
                  silence_secs: float = 0.0) -> None:
    """Fold one real synthesis into the voice's rolling cadence average.

    *seconds* is the produced WAV's length; *silence_secs* is silence spliced
    in by pause markers / sentence gaps (excluded — it isn't speech); *speed*
    is the multiplier the take was synthesized at (normalized back out so the
    stored figure is always the natural, speed-1.0 cadence). Too-short takes
    carry too much noise to be worth recording. Best-effort: never raises.
    """
    try:
        speech = float(seconds) - max(0.0, float(silence_secs))
        speed = float(speed) if speed else 1.0
        if words < 6 or speech < 2.0 or speed <= 0:
            return
        sample = words / speech * 60.0 / speed
        if not (_WPM_SANE_MIN <= sample <= _WPM_SANE_MAX):
            return
        store = load_store()
        key = _key(voice_name, engine)
        entry = store.get(key) if isinstance(store.get(key), dict) else {}
        prev_wpm = float(entry.get("wpm") or 0.0)
        prev_n = int(entry.get("samples") or 0)
        n = min(prev_n, _MAX_EFFECTIVE_SAMPLES - 1) + 1
        wpm = sample if prev_n < 1 else (prev_wpm * (n - 1) + sample) / n
        store[key] = {"wpm": round(wpm, 1), "samples": prev_n + 1,
                      "updated_at": time.time()}
        _save_store(store)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Cadence sample skipped for %r: %s", voice_name, exc)


def set_measured(voice_name: str | None, engine: str | None, wpm: float) -> None:
    """Overwrite a voice's cadence with an explicit calibration measurement."""
    store = load_store()
    store[_key(voice_name, engine)] = {
        # Seeded with weight so the next few render samples refine rather
        # than swamp the calibrated figure.
        "wpm": round(float(wpm), 1), "samples": 5, "updated_at": time.time(),
    }
    _save_store(store)


def calibrate_voice(voice_name: str | None, ref_path: Path | None, host: str,
                    engine: str = "openf5", language: str = "en") -> float:
    """Synthesize the calibration passage once and store the measured cadence.

    Runs on the given TTS *host* (same transports as narration). Returns the
    measured words-per-minute; raises on synthesis failure.
    """
    from pipeline.tts_worker import generate_narration  # lazy: avoid cycle

    words = word_count(CALIBRATION_TEXT)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "calibration.wav"
        generate_narration(CALIBRATION_TEXT, out, reference_wav=ref_path,
                           host=host, speed=1.0, tts_engine=engine,
                           language=language)
        secs = wav_seconds(out)
    if secs < 2.0:
        raise RuntimeError(f"Calibration produced no measurable audio ({secs:.1f}s)")
    wpm = words / secs * 60.0
    if not (_WPM_SANE_MIN <= wpm <= _WPM_SANE_MAX):
        raise RuntimeError(f"Calibration cadence out of range ({wpm:.0f} wpm)")
    set_measured(voice_name, engine, wpm)
    logger.info("Calibrated voice %r [%s]: %.0f wpm (%d words / %.1fs)",
                voice_name or _DEFAULT_VOICE_KEY, engine, wpm, words, secs)
    return wpm


# ── Speed ⇄ cadence resolution ────────────────────────────────────────────────

def _clamp_speed(v: float) -> float:
    return max(SPEED_MIN, min(SPEED_MAX, v))


def resolve_voice_speed(settings: dict) -> float:
    """Effective TTS speed multiplier for a style/job settings mapping.

    ``voice_cadence_wpm`` (target words per minute; 0/unset = the voice's
    natural pace) wins; the legacy ``voice_speed`` multiplier is honored when
    no cadence is set, so old configs and job_config.json keep working.
    """
    try:
        target = float(settings.get("voice_cadence_wpm") or 0)
    except (TypeError, ValueError):
        target = 0.0
    if target > 0:
        nat, _ = natural_wpm(settings.get("voice"), settings.get("tts_engine"))
        return _clamp_speed(target / nat)
    try:
        return _clamp_speed(float(settings.get("voice_speed") or 1.0))
    except (TypeError, ValueError):
        return 1.0


def effective_wpm(settings: dict) -> tuple[float, bool]:
    """(playback cadence, measured?) for a style/job settings mapping.

    The rate narration will actually play at: the target cadence when set,
    else the voice's natural cadence scaled by any legacy speed multiplier.
    """
    try:
        target = float(settings.get("voice_cadence_wpm") or 0)
    except (TypeError, ValueError):
        target = 0.0
    nat, measured = natural_wpm(settings.get("voice"), settings.get("tts_engine"))
    if target > 0:
        return target, measured
    try:
        speed = _clamp_speed(float(settings.get("voice_speed") or 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    return nat * speed, measured


# ── Length planning ───────────────────────────────────────────────────────────

def plan_script(minutes: float, wpm: float,
                sentence_pause: float = 0.0, chained: bool = False) -> dict:
    """Turn a target length into a word budget and per-scene word caps.

    Each scene is one scene_window() of audio; when the style splices
    ``sentence_pause`` silence between sentences (~one gap per scene at 1–2
    sentences a scene), that silence eats into the scene's word room. Values
    are words, so they hold for any cadence.

    With *chained* on, the same runtime is planned as fewer, longer scenes —
    each carrying proportionally more narration.
    """
    secs_min, secs_target, secs_max = scene_window(chained)
    minutes = max(0.25, min(max_minutes(chained), float(minutes or 0)))
    wpm = float(wpm) if wpm and wpm > 0 else DEFAULT_WPM
    gap = max(0.0, min(float(sentence_pause or 0.0), secs_min / 2))
    n_scenes = max(1, round(minutes * 60.0 / secs_target))
    words_target = max(1, round(wpm * (secs_target - gap) / 60.0))
    words_max = max(words_target, math.floor(wpm * (secs_max - gap) / 60.0))
    words_min = max(1, min(words_target, math.ceil(wpm * (secs_min - gap) / 60.0)))
    return {
        "minutes": round(minutes, 2),
        "wpm": round(wpm, 1),
        "sentence_pause": gap,
        "n_scenes": n_scenes,
        "words_total": n_scenes * words_target,
        "scene_words_target": words_target,
        "scene_words_min": words_min,
        "scene_words_max": words_max,
        "scene_secs_min": secs_min,
        "scene_secs_max": secs_max,
        "chained": bool(chained),
    }


def plan_for_scenes(n_scenes: int, wpm: float,
                    sentence_pause: float = 0.0, chained: bool = False) -> dict:
    """A plan pinned to an explicit scene count (redraft/legacy paths)."""
    n = max(1, int(n_scenes))
    _, secs_target, _ = scene_window(chained)
    plan = plan_script(n * secs_target / 60.0, wpm, sentence_pause, chained)
    plan["n_scenes"] = n
    plan["words_total"] = n * plan["scene_words_target"]
    plan["minutes"] = round(n * secs_target / 60.0, 2)
    return plan


def minutes_for_scenes(n_scenes: int) -> float:
    """The length a LEGACY scene count used to produce (~9 s scenes) — for
    migrating n_scenes-era styles, presets, and queue items to minutes."""
    return max(1, int(n_scenes)) * LEGACY_SCENE_SECS / 60.0


def prompt_vars(plan: dict) -> dict:
    """The ${…} placeholders scene-writing prompts need from a plan."""
    return {
        "scene_words_target": plan["scene_words_target"],
        "scene_words_min": plan["scene_words_min"],
        "scene_words_max": plan["scene_words_max"],
        "scene_secs_min": int(plan["scene_secs_min"]),
        "scene_secs_max": int(plan["scene_secs_max"]),
    }


def default_plan(n_scenes: int | None = None) -> dict:
    """A DEFAULT_WPM plan for callers with no style context (tests, tools)."""
    if n_scenes:
        return plan_for_scenes(n_scenes, DEFAULT_WPM)
    return plan_script(1.0, DEFAULT_WPM)


# ── Deterministic narration splitting ─────────────────────────────────────────

# Sentence enders (same family tts_text/captions split on) …
_SENTENCE_RE = re.compile(r"(?<=[.!?…。！？])\s+")
# … and the natural in-sentence pauses where a scene change can land without
# sounding cut: comma, semicolon, colon, dashes (spoken pauses in every
# narration language the engines support).
_CLAUSE_RE = re.compile(r"(?<=[,;:，；：、])\s*|\s+(?=[—–])|(?<=[—–])\s+")


def _split_sentence(sentence: str, max_words: int) -> list[str]:
    """Break one over-long sentence at its natural pauses into ≤max chunks.

    Greedy left-to-right packing of clause fragments. A clause that alone
    exceeds the cap and has no further pause inside is kept whole — a scene
    change anywhere else mid-clause would put an audible cut where the voice
    never pauses.
    """
    clauses = [c for c in _CLAUSE_RE.split(sentence) if c and c.strip()]
    if len(clauses) <= 1:
        return [sentence]
    out: list[str] = []
    cur = ""
    for clause in clauses:
        cand = f"{cur} {clause}".strip() if cur else clause.strip()
        if cur and word_count(cand) > max_words:
            out.append(cur)
            cur = clause.strip()
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def split_narration(text: str, max_words: int, min_words: int = 0) -> list[str]:
    """Split narration into pieces of at most *max_words* spoken words.

    Cuts at sentence boundaries first; a sentence that alone exceeds the cap
    is cut at its natural pauses (see :func:`_split_sentence`). Pieces shorter
    than *min_words* are merged forward when that doesn't burst the cap.
    Returns ``[text]`` unchanged when it already fits.
    """
    text = (text or "").strip()
    if not text or word_count(text) <= max_words:
        return [text] if text else []

    fragments: list[str] = []
    for sentence in _SENTENCE_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if word_count(sentence) > max_words:
            fragments.extend(_split_sentence(sentence, max_words))
        else:
            fragments.append(sentence)

    # Greedy pack fragments into scene-sized pieces.
    pieces: list[str] = []
    cur = ""
    for frag in fragments:
        cand = f"{cur} {frag}".strip() if cur else frag
        if cur and word_count(cand) > max_words:
            pieces.append(cur)
            cur = frag
        else:
            cur = cand
    if cur:
        pieces.append(cur)

    # Merge a runt tail into its neighbour when the cap allows.
    if min_words > 0 and len(pieces) > 1 and word_count(pieces[-1]) < min_words:
        merged = f"{pieces[-2]} {pieces[-1]}".strip()
        if word_count(merged) <= max_words:
            pieces[-2:] = [merged]
    return pieces
