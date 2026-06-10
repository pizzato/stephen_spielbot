#!/usr/bin/env python3
"""Core helpers for the AI video generator.

Formerly the Gradio app; the Gradio UI has been removed and the React +
FastAPI web app (``webapp/``) is the only interface. This module is now a
helper library that ``webapp/backend/main.py`` imports for config I/O,
work-dir bookkeeping, job launching, progress polling, and automation.
"""

import concurrent.futures
import threading
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import yaml


LOG_DIR = Path.home() / ".local" / "share" / "video-generator" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_fmt)

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_log_fmt)

# Keep third-party libraries quiet; only our own logger runs at DEBUG.
logging.basicConfig(level=logging.WARNING, handlers=[_file_handler, _stream_handler], force=True)
logger = logging.getLogger("video_gen")
logger.setLevel(logging.DEBUG)
logger.info("Logging to %s", LOG_FILE)

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.llm import generate_video_prompt, generate_video_suggestions
import pipeline.youtube as yt
from pipeline.comfyui import (
    generate_scene_image,
    ltx_dimensions,
)
from pipeline.orchestrator import DurableStore
from pipeline.worker_pool import WorkerPool, idle_workers

MAX_SCENES    = 100
MAX_CLIP_SECS = 0.0  # 0 means request one clip for the full scene duration.
OUTPUT_DIR   = Path.home() / "videos"
OUTPUT_DIR.mkdir(exist_ok=True)
CONFIG_FILE  = Path.home() / ".config" / "video-generator" / "config.yaml"
VOICES_DIR   = CONFIG_FILE.parent / "voices"
SESSION_FILE = CONFIG_FILE.parent / "last_session.json"
REPO_ROOT    = Path(__file__).parent
RESUME_SCRIPT = REPO_ROOT / "resume_generation.py"

# Resolution is chosen as (orientation × pixel tier).  Each tier defines a
# "long" and "short" edge; orientation decides which axis each maps to (square
# uses the long edge for both).  The composed name strings below are the
# canonical keys stored in config.yaml / job_config.json, so they must stay
# byte-for-byte stable — old saved configs resolve through _RESOLUTIONS.get(name).
_ORIENTATIONS = ["Landscape", "Portrait", "Square"]

# Ordered low→high.  ``label`` is the in-name tag ("" = the base tier, which has
# no tag).  (long_edge, short_edge) are the 16:9 dimensions; square uses
# (long_edge, long_edge).
_PIXEL_TIERS = [
    {"key": "fast", "label": "Fast", "long": 512,  "short": 288},
    {"key": "base", "label": "",     "long": 832,  "short": 480},
    {"key": "hd",   "label": "HD",   "long": 1024, "short": 576},
    {"key": "720p", "label": "720p", "long": 1280, "short": 720},
    {"key": "fhd",  "label": "FHD",  "long": 1920, "short": 1080},
]


def _resolution_dims(orientation: str, tier: dict) -> tuple[int, int]:
    """(width, height) for an orientation × pixel tier."""
    long_e, short_e = tier["long"], tier["short"]
    if orientation == "Portrait":
        return (short_e, long_e)
    if orientation == "Square":
        # Square uses a single edge per tier (the historical 1:1 sizes).
        side = {"fast": 288, "base": 512, "hd": 576, "720p": 720, "fhd": 1080}[tier["key"]]
        return (side, side)
    return (long_e, short_e)  # Landscape


def compose_resolution(orientation: str, tier_key: str) -> str:
    """Compose the canonical resolution name string from orientation + tier key.

    Returns "" if either selector is unknown.
    """
    tier = next((t for t in _PIXEL_TIERS if t["key"] == tier_key), None)
    if orientation not in _ORIENTATIONS or tier is None:
        return ""
    w, h = _resolution_dims(orientation, tier)
    tag = f" {tier['label']}" if tier["label"] else ""
    return f"{orientation}{tag} ({w}×{h})"


def _build_resolutions() -> dict:
    """Build the canonical {name: (w, h)} map from orientations × pixel tiers."""
    out = {}
    for orientation in _ORIENTATIONS:
        for tier in _PIXEL_TIERS:
            out[compose_resolution(orientation, tier["key"])] = _resolution_dims(orientation, tier)
    return out


_RESOLUTIONS = _build_resolutions()
_DEFAULT_ORIENTATION = "Landscape"
_DEFAULT_PIXELS = "fhd"
_DEFAULT_RESOLUTION = compose_resolution(_DEFAULT_ORIENTATION, _DEFAULT_PIXELS)


DEFAULT_CFG = {
    "music_vol": 18,
    "voice_vol": 100,
    "ambient_vol": 0,
    "resolution": _DEFAULT_RESOLUTION,
    "max_clip_secs": 0,
    "lora_strength": 0.5,
    # First-pass (distilled LoRA) settings — set steps=8 + cfg=1.0 for distilled mode;
    # set lora_strength=0 + steps=20-30 + cfg=3-5 for pure dev model mode.
    "first_pass_cfg": 1.0,
    "first_pass_steps": 8,
    # Second-pass (refinement) quality: higher CFG and more steps = better detail
    "second_pass_cfg": 3.0,
    "second_pass_steps": 6,
    "llm_backend": "local",
    "local_llm_url":   "http://localhost:8000/v1/chat/completions",
    "local_llm_model": "openai/gpt-oss-120b",
    "claude_api_key": "",
    "claude_model": "claude-sonnet-4-6",
    # FLUX image generation for scene previews
    "flux_model":    "flux1-schnell-fp8.safetensors",
    "flux_clip_t5":  "t5xxl_fp8_e4m3fn.safetensors",
    "flux_clip_l":   "clip_l.safetensors",
    "flux_vae":      "ae.safetensors",
    "flux_steps":    4,
    "voices": [],
    # Worker lists — edited from the Settings screen, stored in config.yaml.
    # comfy_workers: ComfyUI URLs (image/video/music). One job at a time each.
    # tts_workers:   hostnames for F5-TTS narration.
    # ui_workers:    ComfyUI URLs the lightweight "ui" worker renders covers on.
    "comfy_workers": [],
    "tts_workers":   [],
    "ui_workers":    [],
    # Generation defaults
    "default_voice": "",
    "default_voice_robotic": False,   # post-process narration into a robotic monotone (issue #52)
    "default_voice_robotic_amount": 0.35,  # how strong the robotic effect is: 0.0 (natural) .. 1.0 (harsh metallic)
    "default_n_scenes": 20,
    "default_visual_style": "",
    "script_extra_instructions": "",
    # YouTube integration
    "youtube_client_secrets": "~/.config/video-generator/client_secrets.json",
    # Connected channels (issue #22): [{id, name, channel_id}] where `id` is the
    # local key the token file is stored under ("default" = the legacy
    # youtube_token.json login, otherwise the YouTube channel ID). Managed from
    # Settings → YouTube; each style picks one via its `channel` field.
    "youtube_channels": [],
    "youtube_channel": "",   # flat mirror of the DEFAULT style's channel key
    "youtube_auto_fetch_evaluate": False,      # fetch+evaluate on startup and after each post
    "youtube_auto_approve_comments": False,    # auto-approve requests with confidence ≥ threshold
    "youtube_auto_start_job": False,           # auto-prepare best pending request: generate its script, park as "Script ready"
    "youtube_auto_approve_script": False,      # render auto-prepared scripts without review (only acts when auto_start_job is on)
    "youtube_auto_post": False,               # auto-publish when video generation completes
    "youtube_fully_automated": False,          # derived mirror of the auto_* steps above (true iff all on); no behaviour of its own
    "youtube_post_privacy": "private",
    "youtube_post_category": "22",
    "description_suffix": "",
    # Engagement prediction (issue #50) — config-file-only advanced knobs.
    "engagement_embed_model": "BAAI/bge-small-en-v1.5",  # fastembed text-embedding model
    "engagement_min_samples": 15,        # below this, the model is flagged "insufficient"
    "engagement_data_lag_days": 3,       # exclude videos newer than this (no full 3-day window yet)
    "engagement_short_max_seconds": 180, # videos this long (s) or shorter count as a Short
    # Style profiles (issue #66) — named bundles of the script/content, render
    # quality and audio-mix settings above. load/save normalize this list and
    # mirror the default style back onto the flat keys (see _ensure_styles).
    "styles": [],
    "default_style": "",
}

# Per-style field → the legacy flat config key it replaces. The flat keys stay
# in the config as a mirror of the DEFAULT style, so job_config.json and every
# old fallback path keep working; the styles list is the source of truth.
STYLE_FIELD_TO_FLAT = {
    # Script & content
    "visual_style":         "default_visual_style",
    "extra_instructions":   "script_extra_instructions",
    "description_suffix":   "description_suffix",
    "voice":                "default_voice",
    "voice_robotic":        "default_voice_robotic",
    "voice_robotic_amount": "default_voice_robotic_amount",
    "n_scenes":             "default_n_scenes",
    # Publishing (issue #22) — which connected YouTube channel this style posts to
    "channel":              "youtube_channel",
    # Render quality
    "resolution":           "resolution",
    "lora_strength":        "lora_strength",
    "first_pass_cfg":       "first_pass_cfg",
    "first_pass_steps":     "first_pass_steps",
    "second_pass_cfg":      "second_pass_cfg",
    "second_pass_steps":    "second_pass_steps",
    # Narrator & audio mix
    "music_vol":            "music_vol",
    "voice_vol":            "voice_vol",
    "ambient_vol":          "ambient_vol",
}

# A pre-styles config that has been customized gets its settings preserved
# under this name; a fresh install starts with a blank "Default" style.
LEGACY_STYLE_NAME = "Stephen Spielbot"
BLANK_STYLE_NAME = "Default"

# Reserved style_name meaning "no style profile" (the Create screen's
# experiment mode): nothing content-shaped is imposed — no visual style, no
# extra script instructions, no voice — while render quality and the audio mix
# still come from the default style (they have no per-video controls).
# _ensure_styles keeps real styles from taking this name.
NO_STYLE = "(none)"


def _style_from_flat(cfg: dict, name: str) -> dict:
    """Build a style profile from the flat config keys (migration helper)."""
    style = {"name": name, "description": ""}
    for field, flat in STYLE_FIELD_TO_FLAT.items():
        style[field] = cfg.get(flat, DEFAULT_CFG.get(flat))
    return style


def _ensure_styles(cfg: dict, fresh: bool = False) -> dict:
    """Normalize the style list in place: migrate a pre-styles config, drop
    malformed entries, fill missing fields, dedupe names, validate
    default_style, and mirror the default style onto the flat keys."""
    styles = [s for s in (cfg.get("styles") or [])
              if isinstance(s, dict) and str(s.get("name") or "").strip()]
    if not styles:
        styles = [_style_from_flat(cfg, BLANK_STYLE_NAME if fresh else LEGACY_STYLE_NAME)]

    # NO_STYLE is pre-seeded as taken so no real style can claim the sentinel.
    normalized, seen, missing = [], {NO_STYLE}, []
    for s in styles:
        base = str(s.get("name")).strip()
        name, n = base, 2
        while name in seen:
            name, n = f"{base} {n}", n + 1
        seen.add(name)
        row = {"name": name, "description": str(s.get("description") or "")}
        absent = set()
        for field, flat in STYLE_FIELD_TO_FLAT.items():
            if field in s:
                row[field] = s[field]
            else:
                row[field] = DEFAULT_CFG.get(flat)
                absent.add(field)
        normalized.append(row)
        missing.append(absent)

    cfg["styles"] = normalized
    if cfg.get("default_style") not in {s["name"] for s in normalized}:
        cfg["default_style"] = normalized[0]["name"]
    default_idx = next(i for i, s in enumerate(normalized) if s["name"] == cfg["default_style"])
    # A field that became per-style AFTER this config migrated (e.g.
    # description_suffix) has no value on any style yet. The flat key still
    # holds its real value and, by the mirror invariant, the flat keys ARE the
    # default style's settings — so the default style inherits it. Other
    # styles keep the built-in blank: leaking e.g. the Spielbot suffix into
    # every style was exactly the bug being fixed.
    for field in missing[default_idx]:
        flat = STYLE_FIELD_TO_FLAT[field]
        if flat in cfg:
            normalized[default_idx][field] = cfg[flat]
    default = normalized[default_idx]
    for field, flat in STYLE_FIELD_TO_FLAT.items():
        cfg[flat] = default[field]
    return cfg


def _ensure_channels(cfg: dict) -> dict:
    """Normalize the connected-channels list (issue #22) in place.

    Drops malformed entries, dedupes keys, seeds the reserved "default" entry
    when a pre-multi-channel token file exists so the original login shows up
    as a channel, and clears style channel references that point at a channel
    that is no longer connected. Runs BEFORE _ensure_styles so the styles'
    flat-key mirror sees the cleaned values.
    """
    channels, seen = [], set()
    for c in (cfg.get("youtube_channels") or []):
        key = str(c.get("id") or "").strip() if isinstance(c, dict) else ""
        if not key or key in seen:
            continue
        seen.add(key)
        channels.append({
            "id": key,
            "name": str(c.get("name") or ""),
            "channel_id": str(c.get("channel_id") or ""),
        })
    if not channels and yt._token_path().exists():
        # Pre-#22 setups have one token at the legacy path — surface it as the
        # "default" channel so styles can reference it without re-connecting.
        channels = [{"id": yt.DEFAULT_CHANNEL_KEY, "name": "", "channel_id": ""}]
        seen = {yt.DEFAULT_CHANNEL_KEY}
    cfg["youtube_channels"] = channels
    for s in (cfg.get("styles") or []):
        if isinstance(s, dict) and s.get("channel") and s["channel"] not in seen:
            s["channel"] = ""
    if cfg.get("youtube_channel") and cfg["youtube_channel"] not in seen:
        cfg["youtube_channel"] = ""
    return cfg


def channel_for_style(cfg: dict, style_name: str = "") -> str:
    """Channel KEY a style publishes to: the style's own channel if connected,
    else the first connected channel, else '' (the legacy single-channel
    token)."""
    keys = [c["id"] for c in (cfg.get("youtube_channels") or [])
            if isinstance(c, dict) and c.get("id")]
    ch = str(style_settings(cfg, style_name).get("channel") or "")
    if ch and ch in keys:
        return ch
    return keys[0] if keys else ""


def style_settings(cfg: dict, name: str = "") -> dict:
    """Resolved settings for the named style profile.

    Falls back to the default style when *name* is empty or unknown, and to
    the flat keys / built-in defaults for any missing field — so this is safe
    to call with non-normalized dicts too.

    ``name == NO_STYLE`` is the experiment mode: the content-shaped fields
    (visual style, extra instructions, voice, robotic) come back blank so
    nothing is imposed on the video, while render quality and the audio mix
    keep the default style's values."""
    out = {field: cfg.get(flat, DEFAULT_CFG.get(flat))
           for field, flat in STYLE_FIELD_TO_FLAT.items()}
    requested = (name or "").strip()
    styles = [s for s in (cfg.get("styles") or []) if isinstance(s, dict)]
    target = None
    if requested != NO_STYLE:
        target = next((s for s in styles if s.get("name") == requested), None)
    if target is None:
        target = next((s for s in styles if s.get("name") == cfg.get("default_style")),
                      styles[0] if styles else None)
    if target:
        out.update({k: target[k] for k in STYLE_FIELD_TO_FLAT if k in target})
    if requested == NO_STYLE:
        out.update(visual_style="", extra_instructions="", description_suffix="",
                   voice="", voice_robotic=False)
        out["name"] = NO_STYLE
        out["description"] = ""
        return out
    out["name"] = (target or {}).get("name", "")
    out["description"] = (target or {}).get("description", "")
    return out

F5TTS_DEFAULT_OPTION = "Default (F5-TTS)"

# Thread pool for long blocking operations — keeps SSE alive via heartbeat yields
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# Set of work-dir paths that have already fired the auto-post trigger this
# process lifetime.  Guards against two rapid timer ticks both claiming the
# trigger before the first one's _write_job_meta() flush reaches disk.
_auto_post_triggered: set[str] = set()
_auto_post_lock = threading.Lock()

# Flag that prevents two simultaneous yt_auto_start_trigger.change chains
# (e.g. one from demo.load startup fetch, one from the user pressing the
# button at the same time) from both launching full pipelines concurrently.
_auto_start_in_progress = threading.Event()


def _is_job_running() -> bool:
    """Return True if any video generation job is currently in progress."""
    # Check the queue first: a "creating" item means script_generate() is running
    # but job_config.json may not exist yet — the filesystem check below would miss it.
    try:
        cutoff = time.time() - 86400
        for q in yt.load_queue():
            if q.get("status") == "creating":
                ts = q.get("updated_at") or q.get("created_at") or time.time()
                if ts > cutoff:
                    return True
    except Exception:
        pass
    try:
        cutoff = time.time() - 86400  # ignore stale jobs older than 24 h
        for d in OUTPUT_DIR.iterdir():
            if not d.is_dir():
                continue
            cfg_file = d / "job_config.json"
            combined = d / "combined.mp4"
            if cfg_file.exists() and not combined.exists():
                if cfg_file.stat().st_mtime > cutoff:
                    # Skip jobs that have already errored or been cancelled —
                    # they have no combined.mp4 but are not "running".
                    job_file = d / "job.json"
                    if job_file.exists():
                        try:
                            job_data = json.loads(job_file.read_text())
                            if job_data.get("status") in ("error", "cancelled", "paused"):
                                continue
                        except Exception:
                            pass
                    return True
    except Exception:
        pass
    return False


def _best_pending_queue_item() -> dict | None:
    """Return the first pending queue item (respecting queue order)."""
    try:
        queue = yt.load_queue()
        first = next((q for q in queue if q.get("status") == "pending"), None)
        return {**first} if first else None  # fresh copy
    except Exception:
        return None


# ── Config helpers ───────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load the single YAML config. Saved values are authoritative — worker
    lists (comfy_workers/tts_workers/ui_workers) live here and are edited from
    the Settings screen."""
    cfg = DEFAULT_CFG.copy()
    data = {}
    if CONFIG_FILE.exists():
        data = yaml.safe_load(CONFIG_FILE.read_text()) or {}
        if isinstance(data, dict):
            cfg.update(data)
    # A config that predates styles but already carries customized content
    # settings becomes the "Stephen Spielbot" style; anything else (no file, or
    # an install-seeded file with only worker lists) starts with a blank
    # "Default" style.
    fresh = not any(flat in data for flat in STYLE_FIELD_TO_FLAT.values())
    _ensure_channels(cfg)
    return _ensure_styles(cfg, fresh=fresh)


def save_config(cfg: dict) -> None:
    _ensure_channels(cfg)
    _ensure_styles(cfg)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))






def _job_meta_path(work_dir: Path) -> Path:
    return work_dir / "job.json"


def _final_path_for_work_dir(work_dir: Path) -> Path:
    meta_path = _job_meta_path(work_dir)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            final_path = Path(meta.get("final_path", ""))
            if final_path.exists() and final_path.stat().st_size > 10_000:
                return final_path
        except Exception:
            pass

    canonical = OUTPUT_DIR / f"{work_dir.name}.mp4"
    if canonical.exists() and canonical.stat().st_size > 10_000:
        return canonical

    status_path = work_dir / "progress.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text())
            msg = str(status.get("msg", ""))
            match = re.search(r"Done\s+[—-]\s+(.+?\.mp4)\s+\(([\d.]+)\s+MB\)", msg)
            if match:
                size_mb = float(match.group(2))
                for candidate in OUTPUT_DIR.glob("*.mp4"):
                    try:
                        actual_mb = candidate.stat().st_size / 1024 / 1024
                        if abs(actual_mb - size_mb) < max(2.0, size_mb * 0.02):
                            return candidate
                    except OSError:
                        continue
        except Exception:
            pass

    try:
        marker_mtime = max(
            (p.stat().st_mtime for p in (meta_path, status_path) if p.exists()),
            default=0.0,
        )
        # Only use the recency heuristic when we have a real anchor time.
        # If marker_mtime == 0 it means neither job.json nor progress.json
        # exists yet (job just started), so marker_mtime - 600 == -600 and
        # every .mp4 in OUTPUT_DIR would match — returning the wrong video.
        if marker_mtime > 0:
            recent = [
                p for p in OUTPUT_DIR.glob("*.mp4")
                if p.stat().st_size > 10_000 and p.stat().st_mtime >= marker_mtime - 600
            ]
            if recent:
                return max(recent, key=lambda p: p.stat().st_mtime)
    except Exception:
        pass

    return canonical


def _latest_work_dir() -> Path | None:
    candidates: dict[Path, float] = {}
    for pattern in ("*/progress.json", "*/script.json", "*/job.json"):
        for path in OUTPUT_DIR.glob(pattern):
            if path.parent.exists():
                candidates[path.parent] = max(candidates.get(path.parent, 0.0), path.stat().st_mtime)
    if not candidates:
        return None
    return max(candidates, key=lambda p: candidates[p])




def _preferred_work_dir(active_job_dir: str = "") -> Path | None:
    if active_job_dir:
        active = Path(active_job_dir)
        if active.exists():
            return active
    return _latest_work_dir()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _process_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_job_meta(work_dir: Path, **updates) -> dict:
    meta_path = _job_meta_path(work_dir)
    try:
        meta = _read_json(meta_path)
    except Exception:
        meta = {"work_dir": str(work_dir), "created_at": time.time()}
    meta.update(updates)
    if "error" in updates and updates["error"] is None:
        meta.pop("error", None)
    meta["updated_at"] = time.time()
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta


def _launch_generation_job(work_dir: Path) -> dict:
    """Start the resumable generator in its own process."""
    meta_path = _job_meta_path(work_dir)
    if meta_path.exists():
        try:
            meta = _read_json(meta_path)
            if _process_running(meta.get("pid")):
                return meta
        except Exception:
            pass

    log_path = work_dir / "job.log"
    log_f = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(RESUME_SCRIPT), str(work_dir)],
        cwd=str(REPO_ROOT),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    logger.info("Launched generation job pid=%s work_dir=%s", proc.pid, work_dir)
    return _write_job_meta(
        work_dir,
        pid=proc.pid,
        status="running",
        error=None,
        log_path=str(log_path),
        command=[sys.executable, str(RESUME_SCRIPT), str(work_dir)],
    )


def _job_config_snapshot(cfg: dict) -> dict:
    """Persist only non-secret runtime settings needed by the background worker."""
    job_cfg = cfg.copy()
    # The job stores its RESOLVED style values under the flat keys (plus
    # style_name); carrying the whole style list would just go stale.
    job_cfg.pop("styles", None)
    job_cfg.pop("default_style", None)
    for key in list(job_cfg):
        lowered = key.lower()
        if "api_key" in lowered or "token" in lowered or "secret" in lowered:
            job_cfg.pop(key, None)
    return job_cfg


def _status_for_work_dir(work_dir: Path) -> tuple[float, str]:
    status_file = work_dir / "progress.json"
    pct = 0.0
    msg = "Waiting to start..."
    if status_file.exists():
        try:
            data = _read_json(status_file)
            pct = float(data.get("pct", 0))
            msg = str(data.get("msg", "..."))
        except Exception:
            pass

    final_path = _final_path_for_work_dir(work_dir)
    combined_check = work_dir / "combined.mp4"
    if final_path.exists() and final_path.stat().st_size > 10_000 and combined_check.exists():
        return 100.0, f"Done - {final_path.name} ({final_path.stat().st_size / 1024 / 1024:.1f} MB)"

    try:
        meta = _read_json(_job_meta_path(work_dir))
        if meta.get("status") == "error":
            return pct, f"Generation failed: {str(meta.get('error', 'unknown error')).splitlines()[0][:300]}"
        pid = meta.get("pid")
        if pid and not _process_running(pid) and pct < 100:
            return pct, f"Generation process exited before completion. Check {work_dir / 'job.log'}"
    except Exception:
        pass

    return pct, msg




def get_voice_choices() -> list[str]:
    return [F5TTS_DEFAULT_OPTION] + [v["name"] for v in load_config().get("voices", [])]


def voice_path_for(name: str) -> str | None:
    if not name or name == F5TTS_DEFAULT_OPTION:
        return None
    for v in load_config().get("voices", []):
        if v["name"] == name:
            return v["path"]
    return None


def _under_voices_dir(p: Path) -> bool:
    """True if *p* lives inside VOICES_DIR (so we own it and may delete it)."""
    try:
        p.resolve().relative_to(VOICES_DIR.resolve())
        return True
    except (ValueError, OSError):
        return False


def _new_voice_path(name: str, ext: str) -> Path:
    """A non-colliding path under VOICES_DIR for a voice's reference clip."""
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    stem = slugify(name) or "voice"
    candidate = VOICES_DIR / f"{stem}{ext}"
    n = 2
    while candidate.exists():
        candidate = VOICES_DIR / f"{stem}-{n}{ext}"
        n += 1
    return candidate


def add_voice(name: str, audio: bytes, ext: str = ".wav") -> dict:
    """Save a new reference clip and register the voice. Returns the new config."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Voice name is required.")
    cfg = load_config()
    voices = cfg.get("voices", []) or []
    if name == F5TTS_DEFAULT_OPTION or any(v["name"] == name for v in voices):
        raise ValueError(f"A voice named “{name}” already exists.")
    path = _new_voice_path(name, ext)
    path.write_bytes(audio)
    voices.append({"name": name, "path": str(path)})
    cfg["voices"] = voices
    save_config(cfg)
    return cfg


def update_voice(name: str, new_name: str | None = None,
                 audio: bytes | None = None, ext: str = ".wav") -> dict:
    """Rename a voice and/or replace its reference clip. Returns the new config."""
    cfg = load_config()
    voices = cfg.get("voices", []) or []
    voice = next((v for v in voices if v["name"] == name), None)
    if voice is None:
        raise ValueError(f"No voice named “{name}”.")
    if new_name is not None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("Voice name is required.")
        if new_name != name:
            if new_name == F5TTS_DEFAULT_OPTION or any(v["name"] == new_name for v in voices):
                raise ValueError(f"A voice named “{new_name}” already exists.")
            voice["name"] = new_name
            if cfg.get("default_voice") == name:
                cfg["default_voice"] = new_name
            for s in cfg.get("styles") or []:
                if isinstance(s, dict) and s.get("voice") == name:
                    s["voice"] = new_name
    if audio is not None:
        old_path = Path(voice["path"])
        new_path = _new_voice_path(voice["name"], ext)
        new_path.write_bytes(audio)
        voice["path"] = str(new_path)
        if old_path != new_path and _under_voices_dir(old_path) and old_path.exists():
            old_path.unlink()
    save_config(cfg)
    return cfg


def delete_voice(name: str) -> dict:
    """Remove a voice and delete its reference clip. Returns the new config."""
    cfg = load_config()
    voices = cfg.get("voices", []) or []
    voice = next((v for v in voices if v["name"] == name), None)
    if voice is None:
        raise ValueError(f"No voice named “{name}”.")
    cfg["voices"] = [v for v in voices if v["name"] != name]
    if cfg.get("default_voice") == name:
        cfg["default_voice"] = ""
    for s in cfg.get("styles") or []:
        if isinstance(s, dict) and s.get("voice") == name:
            s["voice"] = ""
    save_config(cfg)
    path = Path(voice["path"])
    if _under_voices_dir(path) and path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    return cfg




# ── UI helpers ───────────────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-")
















def _active_job_row(active_job_dir: str = ""):
    store = DurableStore.default()
    try:
        if active_job_dir:
            # A specific job was named: act on exactly that one, never a fallback.
            # (A script/partial with no durable row must NOT resolve to the
            # currently-running render — deleting it would cancel that render.)
            job = store.get_job_by_work_dir(active_job_dir)
        else:
            recent = store.recent_jobs(limit=1)
            job = recent[0] if recent else None
        return dict(job) if job else None
    finally:
        store.close()


def on_resume_active_job(active_job_dir: str):
    job = _active_job_row(active_job_dir)
    if not job:
        return "No durable job available to resume.", None, active_job_dir
    work_dir = Path(job["work_dir"])
    if not work_dir.exists():
        return f"Work directory missing: {work_dir}", None, active_job_dir
    store = DurableStore.default()
    try:
        store.recover_incomplete_tasks(job["id"])
        store.update_job(job["id"], status="running", progress_message="resume requested")
    finally:
        store.close()
    _launch_generation_job(work_dir)
    return f"Resume launched for {work_dir.name}", None, str(work_dir)


def on_retry_failed_tasks(active_job_dir: str):
    job = _active_job_row(active_job_dir)
    if not job:
        return "No durable job available."
    store = DurableStore.default()
    try:
        count = store.retry_failed_tasks(job["id"])
    finally:
        store.close()
    return f"Requeued {count} failed/lost task(s)."


def on_cancel_active_job(active_job_dir: str):
    job = _active_job_row(active_job_dir)
    if not job:
        return "No durable job available."
    store = DurableStore.default()
    try:
        count = store.cancel_job(job["id"])
    finally:
        store.close()
    return f"Cancelled {count} pending/running task(s)."


def on_pause_active_job(active_job_dir: str) -> str:
    """Kill the generation subprocess and re-queue in-progress tasks so the job can be resumed later."""
    work_dir = _preferred_work_dir(active_job_dir)
    if not work_dir:
        return "No active job found."
    try:
        meta = _read_json(_job_meta_path(work_dir))
    except Exception:
        meta = {}
    pid = meta.get("pid")
    killed = False
    if pid and _process_running(pid):
        try:
            os.kill(int(pid), 15)  # SIGTERM
            killed = True
        except OSError as exc:
            logger.warning("Could not signal pid %d: %s", pid, exc)
    job = _active_job_row(active_job_dir)
    if job:
        store = DurableStore.default()
        try:
            store.recover_incomplete_tasks(job["id"])
        finally:
            store.close()
    _write_job_meta(work_dir, status="paused", pid=None)
    return f"Paused {work_dir.name}" + (" — process terminated" if killed else " — process was not running")


def _list_resumable_jobs() -> list[tuple[str, str]]:
    """Jobs that were started (have job.json) but not yet completed (no combined.mp4)."""
    results = []
    try:
        dirs = sorted(
            (d for d in OUTPUT_DIR.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for d in dirs:
            if (d / "job.json").exists() and not (d / "combined.mp4").exists():
                try:
                    meta = _read_json(d / "job.json")
                    if meta.get("status") == "cancelled":
                        continue
                except Exception:
                    pass
                results.append((_job_folder_label(d), str(d)))
    except Exception:
        pass
    return results






def _script_work_dir(title: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work_dir = OUTPUT_DIR / f"{slugify(title)}-{stamp}"
    suffix = 1
    while work_dir.exists():
        suffix += 1
        work_dir = OUTPUT_DIR / f"{slugify(title)}-{stamp}-{suffix}"
    work_dir.mkdir(parents=True, exist_ok=False)
    return work_dir


def _persist_script_snapshot(work_dir: Path, scenes: list[dict[str, str]]) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "script.json").write_text(json.dumps(scenes, indent=2))


def _job_work_dir(job_id: str) -> Path | None:
    if not job_id:
        return None
    store = DurableStore.default()
    try:
        row = store.get_job(job_id)
        return Path(row["work_dir"]) if row else None
    finally:
        store.close()






def _save_active_scene(
    job_id: str,
    scene_id: int,
    title: str,
    image_prompt: str,
    video_prompt: str,
    narration: str,
) -> None:
    if not job_id:
        return
    store = DurableStore.default()
    try:
        sid = int(scene_id or 1)
        current = store.get_scene(job_id, sid) or {}
        store.upsert_scene(
            job_id,
            sid,
            title=title,
            image_prompt=image_prompt,
            video_prompt=video_prompt,
            narration=narration,
            preview_path=current.get("preview_path", ""),
            metadata=current.get("metadata", {}),
        )
        rows = store.scene_rows(job_id)
    finally:
        store.close()
    work_dir = _job_work_dir(job_id)
    if work_dir:
        _persist_script_snapshot(work_dir, rows)






# ── YouTube cover image ──────────────────────────────────────────────────────
# _overlay_title_on_image and _cover_prompt are imported from pipeline.cover




# ── TTS wrapper ──────────────────────────────────────────────────────────────



# ── Script generation ────────────────────────────────────────────────────────



# ── Scene image generation — one active scene only ───────────────────────────

_IMG_GEN_OUT_COUNT = 3


def _preview_worker_urls() -> list[str]:
    cfg = load_config()
    all_workers = cfg.get("comfy_workers", [])
    try:
        # Prefer idle workers so previews don't queue behind a video render on a
        # busy worker; falls back to all reachable workers if every one is busy.
        return idle_workers(all_workers)
    except Exception as exc:
        logger.warning("Scene image generation worker probe failed: %s", exc)
        return []


def _compose_visual_style(style: str, cfg: dict, style_name: str = "") -> str:
    """Combine a per-job visual style with the job's style profile, de-duplicated.

    The Create/Script UI pre-fills the per-job style from the profile's
    ``visual_style``, so the two are usually identical. Joining them blindly
    repeated the style in the FLUX prompt (e.g. "Cinematic …. Cinematic ….
    <scene>"), which over-stylized the images. Keep genuinely distinct styles;
    drop case-insensitive repeats. ``style_name`` picks the profile; empty
    falls back to the default style (the pre-#66 behaviour).
    """
    parts: list[str] = []
    for raw in (style, style_settings(cfg, style_name).get("visual_style", "")):
        p = (raw or "").strip().rstrip(".")
        if p and p.lower() not in [q.lower() for q in parts]:
            parts.append(p)
    return ". ".join(parts)


def _generate_active_scene_preview(
    job_id: str,
    scene_id: int,
    resolution: str,
    style: str,
    title: str,
    image_prompt: str,
    *,
    force: bool = False,
    worker_pool: WorkerPool | None = None,
) -> Path:
    work_dir = _job_work_dir(job_id)
    if work_dir is None:
        raise RuntimeError("No script work directory is available.")
    sid = int(scene_id)
    store = DurableStore.default()
    try:
        scene = store.get_scene(job_id, sid) or {}
        job_row = store.get_job(job_id)
    finally:
        store.close()
    existing = scene.get("preview_path") or ""
    if existing and Path(existing).exists() and not force:
        return Path(existing)

    # Resolve the job's style profile (stamped at script time) so previews use
    # that profile's visual style and resolution, not the global default's.
    style_name = ""
    if job_row is not None:
        try:
            style_name = json.loads(dict(job_row).get("config_json") or "{}").get("style_name", "")
        except Exception:
            style_name = ""

    cfg = load_config()
    if worker_pool is None:
        worker_urls = _preview_worker_urls()
        if worker_urls:
            worker_pool = WorkerPool(worker_urls)
    if worker_pool is None:
        raise RuntimeError("No cluster workers reachable for scene preview generation.")

    out = work_dir / f"scene_{sid:02d}_preview.png"
    flux_model = cfg.get("flux_model", "flux1-schnell-fp8.safetensors")
    flux_clip_t5 = cfg.get("flux_clip_t5", "t5xxl_fp8_e4m3fn.safetensors")
    flux_clip_l = cfg.get("flux_clip_l", "clip_l.safetensors")
    flux_vae = cfg.get("flux_vae", "ae.safetensors")
    flux_steps = int(cfg.get("flux_steps", 4))
    img_width, img_height = _RESOLUTIONS.get(
        resolution or style_settings(cfg, style_name).get("resolution") or _DEFAULT_RESOLUTION,
        (1024, 576),
    )
    # Match the render: snap the preview to LTX's renderable grid so the cached
    # preview is reused as the first frame instead of regenerated at a new size.
    img_width, img_height = ltx_dimensions(img_width, img_height)
    combined_style = _compose_visual_style(style, cfg, style_name)
    base_prompt = image_prompt or scene.get("image_prompt") or title
    prompt = f"{combined_style}. {base_prompt}" if combined_style else base_prompt

    url = worker_pool.acquire()
    try:
        generate_scene_image(
            prompt,
            out,
            width=img_width,
            height=img_height,
            steps=flux_steps,
            flux_model=flux_model,
            clip_t5=flux_clip_t5,
            clip_l=flux_clip_l,
            flux_vae=flux_vae,
            comfy_url=url,
        )
        store = DurableStore.default()
        try:
            store.update_scene_preview(job_id, sid, out)
        finally:
            store.close()
        return out
    finally:
        worker_pool.release(url)






# ── Video generation — generator, yields progressive UI updates ──────────────

# gen_outputs count: progress, music, final, combined_state, music_state,
# ambient_state, tabs, active_job
_GEN_OUT_COUNT = 8






# ── Session restore ──────────────────────────────────────────────────────────

def _job_folder_label(d: Path) -> str:
    """Human-readable label for a job folder: title without the trailing timestamp."""
    # Folder names look like: my-great-video-20260528-082307
    # Strip the trailing -YYYYMMDD-HHMMSS suffix so only the title part remains.
    name = re.sub(r"-\d{8}-\d{6}(-\d+)?$", "", d.name)
    return name.replace("-", " ").title()


def _list_recent_jobs(max_results: int = 10) -> list[tuple[str, str]]:
    """Return list of (label, work_dir_str) for completed jobs, newest first."""
    results = []
    try:
        dirs = sorted(
            (d for d in OUTPUT_DIR.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for d in dirs:
            combined = d / "combined.mp4"
            if combined.exists():
                results.append((_job_folder_label(d), str(d)))
            if len(results) >= max_results:
                break
    except Exception:
        pass
    return results


def _list_script_jobs() -> list[tuple[str, str]]:
    """Return list of (label, work_dir_str) for all jobs with a saved script, newest first."""
    results = []
    try:
        dirs = sorted(
            (d for d in OUTPUT_DIR.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for d in dirs:
            if (d / "script.json").exists():
                results.append((_job_folder_label(d), str(d)))
    except Exception:
        pass
    return results






# Keep for backward compat with _remix_select_all






# ── Remix ────────────────────────────────────────────────────────────────────

def on_remix(
    combined_path_str: str,
    music_path_str: str,
    ambient_path_str: str,
    voice_vol: float,
    music_vol: float,
    ambient_vol: float,
):
    """Re-mux the final video with new voice/music/ambient volumes.

    Volumes are percentages (100 = unchanged), matching the UI sliders and
    job_config; they're divided by 100 into the fractional gains that
    mix_background_music expects. Returns ``(final_video_path, message)`` — the
    path is ``""`` on failure and ``message`` explains the outcome.
    """
    combined_path = Path(combined_path_str)
    if not combined_path.exists():
        return "", "combined.mp4 not found — nothing to remix."
    work_dir = combined_path.parent
    # Overwrite the canonical published final (what Publish/EditFilm read) so the
    # remix IS what gets published. Writing to work_dir/{name}.mp4 left the
    # published video — OUTPUT_DIR/{name}.mp4 — stale. See issue #14.
    final_video = _final_path_for_work_dir(work_dir)
    music_path = Path(music_path_str) if music_path_str else None
    ambient_path = Path(ambient_path_str) if ambient_path_str else None
    try:
        from pipeline.assembler import mix_background_music
        mix_background_music(
            video_path=combined_path,
            music_path=music_path,
            output_path=final_video,
            volume=music_vol / 100.0,
            voice_volume=voice_vol / 100.0,
            ambient_path=ambient_path,
            ambient_volume=ambient_vol / 100.0,
        )
        # Persist the new volumes to job_config so a later re-render keeps them.
        try:
            cfg_path = work_dir / "job_config.json"
            jc = json.loads(cfg_path.read_text())
            jc.update(voice_vol=voice_vol, music_vol=music_vol, ambient_vol=ambient_vol)
            cfg_path.write_text(json.dumps(jc, indent=2))
        except Exception:
            logger.warning("Could not persist remix volumes to job_config", exc_info=True)
        return str(final_video), "Re-mixed the audio and re-muxed the film."
    except Exception as e:
        logger.exception("Remix failed")
        return "", f"Remix failed: {str(e).splitlines()[0][:200]}"


# ── LTX Upscale ──────────────────────────────────────────────────────────────





# ── Config management ────────────────────────────────────────────────────────











# ── YouTube tab handlers ─────────────────────────────────────────────────────















































# ── Post tab handlers ─────────────────────────────────────────────────────────













def _load_scenes_for_work_dir(work_dir: Path) -> list[dict]:
    """Return scene dicts for a work_dir.

    Tries DurableStore first; falls back to reading script.json from disk so
    older jobs (predating the DB) or jobs after a DB wipe still get usable
    image_prompts for cover regeneration.
    """
    # DurableStore path
    try:
        store = DurableStore.default()
        try:
            job = store.get_job_by_work_dir(str(work_dir))
            if job:
                rows = store.scene_rows(job["id"]) or []
                if rows:
                    return rows
        finally:
            store.close()
    except Exception as exc:
        logger.debug("DurableStore scene lookup failed for %s: %s", work_dir, exc)

    # script.json fallback
    script_path = work_dir / "script.json"
    if script_path.exists():
        try:
            data = json.loads(script_path.read_text())
            if isinstance(data, list):
                return data
        except Exception as exc:
            logger.debug("script.json read failed for %s: %s", work_dir, exc)
    return []




















# ── UI ────────────────────────────────────────────────────────────────────────



# ── Video Suggestions ─────────────────────────────────────────────────────────



def _channel_video_titles(cfg: dict, style_name: str = "") -> list[str]:
    """Collect known video titles: YouTube API first, posted queue items as fallback.

    Titles come from the channel the style publishes to (issue #22), so the
    dedup check runs against the channel the new video would actually join."""
    secrets = cfg.get("youtube_client_secrets", "")
    titles: list[str] = []
    try:
        titles = yt.fetch_channel_video_titles(
            secrets, max_results=500, channel=channel_for_style(cfg, style_name))
    except Exception as exc:
        logger.warning("fetch_channel_video_titles error: %s", exc)
    # Supplement with posted queue items in case the API call failed or is partial
    try:
        queue = yt.load_queue()
        for q in queue:
            t = q.get("final_title", "")
            if q.get("status") == "posted" and t and t not in titles:
                titles.append(t)
    except Exception:
        pass
    return titles






def _auto_pick_suggestion(cfg: dict) -> dict | None:
    """Pick the first unused suggestion (generating new ones if needed) and add it to the queue.

    Returns the new pending queue item dict, or None on failure.
    Called only when there are no pending user requests and auto-start is enabled.
    """
    suggestions = yt.load_suggestions()
    unused = [s for s in suggestions if not s.get("used")]

    if not unused:
        # Ask the LLM for a fresh batch of 5, steered by the default style —
        # automation invents ideas for the channel's default persona.
        logger.info("No unused suggestions — generating a new batch")
        ss = style_settings(cfg)
        existing_titles = _channel_video_titles(cfg)
        new_data = generate_video_suggestions(existing_titles, cfg, style=ss)
        if not new_data:
            logger.warning("_auto_pick_suggestion: LLM suggestion generation failed")
            return None
        suggestions = [
            {
                "id": str(uuid.uuid4())[:8],
                "title": s["title"],
                "reason": s["reason"],
                "interestingness": s["interestingness"],
                "style_name": ss["name"],
                "created_at": time.time(),
                "used": False,
            }
            for s in new_data
        ]
        yt.save_suggestions(suggestions)
        unused = suggestions

    suggestion = unused[0]
    suggestion["used"] = True
    # Persist the 'used' flag so this idea is closed and never re-picked. Match
    # by id, fall back to title, and never re-mark an already-used row — a
    # missing/duplicate id must not close the wrong idea and leave this one open
    # (which made automation pick the same idea over and over).
    sid = str(suggestion.get("id") or "")
    stitle = (suggestion.get("title") or "").strip().lower()
    all_suggestions = yt.load_suggestions()
    for s in all_suggestions:
        if s.get("used"):
            continue
        if (sid and str(s.get("id") or "") == sid) or ((s.get("title") or "").strip().lower() == stitle):
            s["used"] = True
            break
    yt.save_suggestions(all_suggestions)

    # Add to queue
    fake_comment = {
        "comment_id": "",
        "video_id": "",
        "commenter": "AI Suggestion",
        "text": suggestion.get("reason", ""),
        "suggested_scene_count": 20,
        "interestingness": suggestion.get("interestingness", 0.7),
    }
    queue_item = yt.add_to_queue(fake_comment, suggestion["title"], source="suggestion")
    if not queue_item:
        logger.warning("_auto_pick_suggestion: add_to_queue returned empty for %r", suggestion["title"])
        return None

    # The idea's style profile rides onto the queue item so the render uses it
    # (legacy un-stamped ideas resolve to the default style downstream).
    style_name = str(suggestion.get("style_name") or "")
    if style_name:
        yt.update_queue_item(queue_item["id"], gen_style_name=style_name)
        queue_item["gen_style_name"] = style_name

    # Generate directorial brief synchronously (we're already in a background context)
    try:
        prompt = generate_video_prompt(suggestion["title"], suggestion.get("reason", ""))
        if prompt:
            yt.update_queue_item(queue_item["id"], video_prompt=prompt)
            queue_item["video_prompt"] = prompt
    except Exception as exc:
        logger.warning("_auto_pick_suggestion: prompt generation failed: %s", exc)

    logger.info("Auto-picked suggestion: %r (id=%s)", suggestion["title"], queue_item.get("id"))
    return queue_item








