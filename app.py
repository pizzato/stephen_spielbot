#!/usr/bin/env python3
"""Core helpers for the AI video generator.

Formerly the Gradio app; the Gradio UI has been removed and the React +
FastAPI web app (``webapp/``) is the only interface. This module is now a
helper library that ``webapp/backend/main.py`` imports for config I/O,
work-dir bookkeeping, job launching, progress polling, and automation.
"""

import concurrent.futures
import fnmatch
import io
import threading
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
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
import pipeline.x as xt
from pipeline.comfyui import (
    generate_with_engine,
    ltx_dimensions,
)
from pipeline.orchestrator import DurableStore
from pipeline.worker_pool import WorkerPool, idle_workers
from pipeline import ui_activity
from pipeline import image_history
from pipeline import engines
from pipeline.cover_typography import DEFAULT_COVER_TYPOGRAPHY
from pipeline import tts_engines

MAX_SCENES    = 200
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
_DEFAULT_ORIENTATION = "Portrait"
_DEFAULT_PIXELS = "fhd"
_DEFAULT_RESOLUTION = compose_resolution(_DEFAULT_ORIENTATION, _DEFAULT_PIXELS)

# Per-style size presets: Small/Medium/Large buckets, each pairing a target
# length in MINUTES with a resolution. The AI-ideas screen offers these as a
# one-tap size that fits the style. `scenes` is the legacy field the minutes
# were migrated from (kept so old readers and hand-edited configs keep
# working); minutes are authoritative — the scene count is derived from the
# narrator's cadence at generation time (pipeline/cadence.py).
_SIZE_BUCKETS = ("small", "medium", "large")
_DEFAULT_SIZE_PRESETS = {
    "small":  {"minutes": 1.0, "scenes": 6,  "resolution": compose_resolution("Portrait", _DEFAULT_PIXELS)},
    "medium": {"minutes": 2.0, "scenes": 12, "resolution": compose_resolution("Landscape", _DEFAULT_PIXELS)},
    "large":  {"minutes": 3.0, "scenes": 20, "resolution": compose_resolution("Landscape", _DEFAULT_PIXELS)},
}


DEFAULT_CFG = {
    # Score the film at all. Off means no music is generated and the final cut
    # IS the concatenation (acted scenes already carry their own sound).
    "music_enabled": True,
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
    # Grok (xAI) — OpenAI-compatible Chat Completions at api.x.ai
    "grok_api_key": "",
    "grok_model": "grok-4.5",
    "grok_api_url": "https://api.x.ai/v1/chat/completions",
    # OpenAI ChatGPT — Chat Completions at api.openai.com
    "openai_api_key": "",
    "openai_model": "gpt-4o",
    "openai_api_url": "https://api.openai.com/v1/chat/completions",
    # FLUX image generation for scene previews
    "flux_model":    "flux1-schnell-fp8.safetensors",
    "flux_clip_t5":  "t5xxl_fp8_e4m3fn.safetensors",
    "flux_clip_l":   "clip_l.safetensors",
    "flux_vae":      "ae.safetensors",
    "flux_steps":    4,
    # Image engine per style (see pipeline/engines.py): which model bundle does
    # scene generation vs the "Edit image" inpaint. Flat keys mirror the DEFAULT
    # style like every other STYLE_FIELD_TO_FLAT entry. Default = FLUX.2 Klein.
    "default_image_engine": "flux2-klein",
    "default_edit_engine":  "flux2-klein",
    # Video engine per style (see pipeline/engines.py VIDEO_ENGINES): which I2V
    # model animates each scene. Default = LTX 2.5.
    "default_video_engine": "ltx25",
    # Ref2VA model for acted scenes: portraits
    # and dialogue instead of a first frame. Narrated films never use it.
    "default_reference_engine": "minimax-h3-ref-w4a8",
    # Music engine per style (see pipeline/engines.py MUSIC_ENGINES): which model
    # writes the background bed. Default = ACE-Step 1.5.
    "default_music_engine": "ace-step",
    # Sampling steps for single-pass video engines (MiniMax H3 / H3 Turbo);
    # 0 = the engine's own default. LTX ignores it (two-pass knobs instead).
    "default_video_steps":  0,
    # Chained H3 scenes: render a scene as two clips joined by H3 Motion
    # Context instead of one, so it can run ~29 s instead of the model's ~15 s
    # ceiling. The planner then writes FEWER, LONGER scenes for the same
    # runtime. MiniMax engines only — LTX chains natively and ignores this.
    "default_h3_chain_scenes": False,
    # Act the SILENT scenes too: a silent beat renders as an H3 Ref2VA take from
    # its cast's portraits (no first frame, no I2V) instead of being animated
    # from a still, so it matches the acted scenes it cuts against. Only scenes
    # the writer gave a cast; the rest stay on the I2V path.
    "default_h3_silent_scenes": False,
    # TTS engine per style (see pipeline/tts_engines.py): which narration model
    # synthesises this style's voice. Default = OpenF5-TTS-Base (Apache-2.0).
    "default_tts_engine":   "openf5",
    # Narration language per style (ISO 639-1) — used by multilingual TTS
    # engines (chatterbox); the F5 engines ignore it (issue #176).
    "default_tts_language": "en",
    # Silence (seconds) spliced between narration sentences to enforce cadence
    # (see pipeline/tts_text.py); 0 keeps the model's own pacing. Per style.
    "default_tts_sentence_pause": 0.0,
    # One-time migration guard: when False, _ensure_styles flips styles still on
    # the old default (flux1-schnell) to the new default, then sets this True so a
    # later deliberate flux1-schnell choice is preserved.
    "engines_default_v2": False,
    # Hugging Face token (with the gated FLUX licenses accepted) used to
    # auto-download engine model weights onto the workers. Set in Settings.
    "hf_token": "",
    "voices": [],
    # Worker lists — edited from the Settings screen, stored in config.yaml.
    # comfy_workers: ComfyUI URLs (image/video/music). One job at a time each.
    # tts_workers:   hostnames for F5-TTS narration.
    "comfy_workers": [],
    "tts_workers":   [],
    # UI worker reservation (issue #98): while the web UI is actively used, the
    # render holds one comfy_worker idle for cover/preview jobs; it rejoins the
    # render pool once the UI has been idle this many seconds.
    "ui_idle_timeout_seconds": 300,
    # Optional advanced override for the packaged Remix temporal AI upscaler.
    # Blank uses LTX-2.3 IC-LoRA Pixel Spatial Upscaler (ComfyUI workflow).
    "temporal_video_upscaler_cmd": "",
    "temporal_video_upscaler_timeout": 7200,
    # Generation defaults
    "default_voice": "",
    # Robotic-monotone post-processing of narration (issue #52): 0.0 is off
    # (natural voice), up to 1.0 (harsh metallic). Level 0 replaced the old
    # on/off toggle (default_voice_robotic), folded in by _ensure_styles.
    "default_voice_robotic_amount": 0.0,
    "default_voice_speed": 1.0,       # F5-TTS speaking pace: 1.0 natural, lower slower, higher faster
    # Target narration cadence in words/minute (pipeline/cadence.py). 0 = the
    # voice's natural measured pace; >0 derives the TTS speed multiplier as
    # target ÷ natural. This replaces the voice-speed slider in the UI —
    # voice_speed stays honored for configs/jobs that predate it.
    "default_voice_cadence_wpm": 0,
    "default_n_scenes": 6,
    # Target video length in MINUTES — the primary length control. The script
    # word budget is minutes × cadence, divided into 10–15 s scenes
    # (pipeline/cadence.py). 0 = derive the length from the legacy n_scenes.
    "default_video_minutes": 0.0,
    # How many scenes that length is divided into. 0 = as many as the scene
    # contract implies (~12 s each narrated, ~10 s acted); >0 pins the count,
    # so fewer scenes are longer ones and more are shorter — held inside what
    # the video engine can render in one take (cadence.engine_scene_ceiling).
    "default_video_scenes": 0,
    # Burn the cover into the head of the final video when a render finishes
    # ("none" | "image" | "text") — YouTube Shorts ignore uploaded thumbnails
    # and pick their own frame. Held for _seconds (1s by default; a single
    # frame reads as a flash and gets discarded). Mirrors the default style.
    "default_first_frame_cover": "none",
    "default_first_frame_cover_seconds": 1.0,
    # Look of the "text" first-frame cover: font file ("" = bold system font),
    # size as % of the video width, and text colour. Mirror the default style.
    # Cover typography (pipeline/cover_typography.py): every cover background
    # is generated TEXT-FREE (with the style's own image engine) and the title
    # is composited on top with real fonts — position, colours, accent words,
    # card are all part of this dict. Mirrors the default style.
    "default_cover_typography": dict(DEFAULT_COVER_TYPOGRAPHY),
    # When True, automation never invents AI ideas in this style while topping up
    # an empty queue (the AI-ideas auto-pick rotation skips it). Opt-out only —
    # the manual AI ideas screen still offers the style. Mirrors the default style.
    "default_auto_pick_exclude": False,
    # Mirror of the DEFAULT style's Small/Medium/Large size presets (see
    # _DEFAULT_SIZE_PRESETS). Per-style values live on each style; this flat key
    # tracks the default style like every other STYLE_FIELD_TO_FLAT mirror.
    "default_size_presets": _DEFAULT_SIZE_PRESETS,
    "default_visual_style": "",
    "default_video_style": "",        # motion/cinematography guidance for each scene's video_prompt (camera + subject movement)
    # Per-style LTX video negative prompt (mirror of the DEFAULT style). Blank
    # means "use the built-in quality default" — resolved at render time.
    "default_video_negative_prompt": "",
    "script_extra_instructions": "",
    # Per-style "avoid" instruction for the script LLM (things to keep OUT of the
    # generated script). Blank = no extra avoid directive.
    "script_avoid": "",
    "title_style": "",                # how generated video titles should be phrased (issue #82)
    # YouTube integration
    "youtube_client_secrets": "~/.config/video-generator/client_secrets.json",
    # Connected channels (issue #22): [{id, name, channel_id}] where `id` is the
    # local key the token file is stored under ("default" = the legacy
    # youtube_token.json login, otherwise the YouTube channel ID). Managed from
    # Settings → YouTube; each style picks one via its `channel` field.
    "youtube_channels": [],
    "youtube_channel": "",   # flat mirror of the DEFAULT style's channel key
    # Per-style playlist the style's uploads are added to. "" = none; a playlist
    # id (PL…) = that playlist; "__auto__" = find-or-create one named after the
    # style on its channel. Flat key mirrors the DEFAULT style (see _ensure_styles).
    "youtube_playlist_id": "",
    # Per-style "Made for Kids" self-declaration sent on upload. Flat key
    # mirrors the DEFAULT style (see _ensure_styles).
    "made_for_kids": False,
    "youtube_auto_fetch_evaluate": False,      # fetch+evaluate on startup and after each post
    "youtube_auto_approve_comments": False,    # auto-approve requests with confidence ≥ threshold
    "youtube_auto_start_job": False,           # auto-start the next queue item with a ready script; loops until the queue is empty
    "youtube_auto_write_scripts": False,       # write (but don't render) scripts for pending queue items, parking them unapproved for review/edit
    "youtube_auto_approve_script": False,      # let auto-start also WRITE missing scripts and render them without review
    "youtube_auto_ai_ideas": False,            # queue an AI idea when the queue runs empty (needs auto_approve_script)
    "youtube_auto_post": False,               # auto-publish when video generation completes
    # Run the script critic (QC: consistency, repetition, engagement — may
    # rewrite/delete/add/reorder scenes) on every automation-written script,
    # after generation and BEFORE it can queue/render.
    "youtube_auto_critic": False,
    # 0 = keep passing until the critic proposes nothing (≤5); 1-5 = fixed count.
    "youtube_auto_critic_passes": 0,
    "youtube_fully_automated": False,          # derived mirror of the auto_* steps above (true iff all on); no behaviour of its own
    # Queue page sort — persisted so it survives page reloads, and authoritative:
    # automation/"Start next render" consume pending items in this order.
    # One of: queue | newest | oldest | interest | views | fastest.
    "queue_sort_order": "queue",
    "youtube_post_privacy": "private",
    "youtube_post_category": "22",
    "description_suffix": "",
    # Open-source attribution — appended to every published video for the style.
    # A footer line on the YouTube description, extra X hashtags, and extra
    # YouTube keyword tags. Editable per style in Settings; defaults credit the
    # Stephen Spielbot repo. Clearing a field turns that credit off.
    "default_attribution_description": "Generated with Stephen Spielbot → https://github.com/pizzato/stephen_spielbot",
    "default_attribution_hashtags": "stephenspielbot",
    "default_attribution_youtube_tags": "stephenspielbot",
    # Content Credentials (C2PA) — sign every published video with signed
    # provenance declaring it AI-generated by Stephen Spielbot. Global (a tool
    # property, not per-style). Best-effort: needs c2patool installed; a local
    # self-signed cert is auto-generated when no cert/key path is set. See
    # pipeline/c2pa.py.
    "c2pa_enabled": True,
    "c2pa_cert_path": "",
    "c2pa_key_path": "",
    # Publishing scheduler — decouples publishing from rendering. When on,
    # finished videos flow into a separate publish queue (publish_queue.json)
    # and are released on each channel/account's own cadence (publish_per_day,
    # spaced evenly) instead of posting the moment a render finishes. Requires
    # youtube_auto_post and/or x_auto_post.
    "publish_schedule_enabled": False,
    # Comment-driven requests skip the cadence and post immediately even when
    # scheduling is on, so requesters get a prompt reply.
    "publish_schedule_skip_comment_requests": True,
    # Approval gate — when on, a finished video is HELD in the publish queue and
    # never released (scheduled or immediate) until the user approves it in the
    # Films tab. Comment-requested videos and an explicit "Publish now" bypass it.
    "publish_require_approval": False,
    # Automation override for the approval gate: when on, finished videos are
    # published on their normal cadence (scheduled or immediate auto-post) even
    # though they're not approved — their `approved` flag stays False. Lets you
    # keep the approval workflow but switch to hands-off publishing. No effect
    # unless publish_require_approval is on.
    "publish_auto_publish_unapproved": False,
    # X (Twitter) integration (issue #107) — mirrors the YouTube block above.
    "x_client_id": "",
    "x_client_secret": "",       # blank → public PKCE client; set → confidential
    # Connected X accounts: [{id, name, account_id, premium, ...}] where `id` is
    # the local key the token file is stored under ("default" = the legacy
    # x_token.json login, otherwise the X user id). Managed from Settings → X;
    # each style picks one via its `x_account` field.
    "x_accounts": [],
    "x_account": "",             # flat mirror of the DEFAULT style's X account key
    "x_auto_fetch_evaluate": False,   # fetch+evaluate X mentions on startup / after a post
    "x_auto_approve_comments": False, # auto-approve request mentions ≥ threshold
    "x_auto_post": False,             # auto-publish to X when video generation completes
    "x_fully_automated": False,       # derived mirror of the x_auto_* steps (no behaviour of its own)
    "x_post_default_text": "",        # appended to the tweet text on post (like description_suffix)
    # Engagement prediction (issue #50).
    "engagement_prediction_days": 3,     # horizon: target = sum of views over the first N calendar days
    "engagement_embed_model": "BAAI/bge-small-en-v1.5",  # fastembed text-embedding model
    "engagement_min_samples": 15,        # below this, the model is flagged "insufficient"
    "engagement_data_lag_days": 3,       # exclude videos newer than this (no full prediction window yet)
    "engagement_short_max_seconds": 180, # videos this long (s) or shorter count as a Short
    # Recurring characters — one library, each entry scoped by its "style"
    # field: "" = the GLOBAL pool every style inherits automatically, else the
    # name of the style that owns it (visible to that style and every style
    # under it in the hierarchy). Each entry is {id, name, aliases[],
    # description, ref_image, ref_strength, enabled, style} — see
    # _norm_characters; resolution in _style_characters. Normalized (and
    # migrated from the old per-style opt-in lists) by _ensure_characters.
    "characters": [],
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
    "video_style":          "default_video_style",
    # Negative prompt applied to every LTX video render in this style (blank →
    # the built-in quality default, see pipeline/llm.py NEGATIVE_PROMPT)
    "video_negative_prompt": "default_video_negative_prompt",
    "extra_instructions":   "script_extra_instructions",
    # "Avoid" instruction fed to the script LLM for every video in this style
    "script_avoid":         "script_avoid",
    "title_style":          "title_style",
    "description_suffix":   "description_suffix",
    # Open-source attribution appended to every published video (see DEFAULT_CFG)
    "attribution_description":     "default_attribution_description",
    "attribution_hashtags":        "default_attribution_hashtags",
    "attribution_youtube_tags":    "default_attribution_youtube_tags",
    "voice":                "default_voice",
    "voice_robotic_amount": "default_voice_robotic_amount",
    "voice_speed":          "default_voice_speed",
    # Target narration cadence (words/minute; 0 = the voice's natural pace)
    "voice_cadence_wpm":    "default_voice_cadence_wpm",
    "n_scenes":             "default_n_scenes",
    # Target video length in minutes (0 = derive from legacy n_scenes)
    "video_minutes":        "default_video_minutes",
    # Scenes that length is divided into (0 = derive from the length)
    "video_scenes":         "default_video_scenes",
    # Burn the cover into the head of the final video after each render
    # ("none" | "image" | "text") — Shorts pick their own frame, not the
    # uploaded thumbnail — and how long that cover is held (seconds)
    "first_frame_cover":    "default_first_frame_cover",
    "first_frame_cover_seconds": "default_first_frame_cover_seconds",
    # Cover typography: text-free background + composited real-font title
    "cover_typography":     "default_cover_typography",
    # Automation — exclude this style from auto-picked queue top-ups (opt-out)
    "auto_pick_exclude":    "default_auto_pick_exclude",
    # Publishing (issue #22) — which connected YouTube channel this style posts to
    "channel":              "youtube_channel",
    # Playlist this style's uploads are added to ("" / PL… id / "__auto__")
    "youtube_playlist_id":  "youtube_playlist_id",
    # Self-declared "Made for Kids" status sent on upload
    "made_for_kids":        "made_for_kids",
    # Publishing (issue #107) — which connected X account this style posts to
    "x_account":            "x_account",
    # Image engine selection (generation vs edit) — see pipeline/engines.py
    "image_engine":         "default_image_engine",
    "edit_engine":          "default_edit_engine",
    # Video engine selection (scene I2V) — see pipeline/engines.py VIDEO_ENGINES
    "video_engine":         "default_video_engine",
    # Optional steps override for the MiniMax engines (0 = engine default)
    "video_steps":          "default_video_steps",
    # Chain two H3 clips per scene so scenes can run past the model's ceiling
    "h3_chain_scenes":      "default_h3_chain_scenes",
    # Act silent scenes on H3 Ref2VA instead of animating them from a still
    "h3_silent_scenes":     "default_h3_silent_scenes",
    # Ref2VA engine for performance films — see pipeline/engines.py
    "reference_engine":     "default_reference_engine",
    # TTS narration model selection — see pipeline/tts_engines.py
    "tts_engine":           "default_tts_engine",
    # Narration language (multilingual TTS engines only)
    "tts_language":         "default_tts_language",
    # Silence spliced between narration sentences (seconds; 0 = model pacing)
    "tts_sentence_pause":   "default_tts_sentence_pause",
    # Render quality
    "resolution":           "resolution",
    # Small/Medium/Large size presets (scenes + resolution per bucket)
    "size_presets":         "default_size_presets",
    "lora_strength":        "lora_strength",
    "first_pass_cfg":       "first_pass_cfg",
    "first_pass_steps":     "first_pass_steps",
    "second_pass_cfg":      "second_pass_cfg",
    "second_pass_steps":    "second_pass_steps",
    # Narrator & audio mix
    "music_enabled":        "music_enabled",
    # Music engine selection (background bed) — see pipeline/engines.py MUSIC_ENGINES
    "music_engine":         "default_music_engine",
    "music_vol":            "music_vol",
    "voice_vol":            "voice_vol",
    "ambient_vol":          "ambient_vol",
}

# Free-text style fields where a child override may embed the literal marker
# "{parent}": it is replaced with the parent's RESOLVED text for that field
# (empty for a root), so a child can extend the parent's instructions — before,
# after, or around its own — instead of replacing them. Composition recurses:
# a grandchild's {parent} receives the child's already-composed text. All other
# fields (names, ids, numbers, booleans, dicts) are plain overrides.
# Mirrored in webapp/frontend/src/styleUtils.js — keep the two lists in sync.
STYLE_TEXT_FIELDS = {
    "description", "visual_style", "video_style", "video_negative_prompt",
    "title_style", "extra_instructions", "script_avoid", "description_suffix",
    "attribution_description", "attribution_hashtags", "attribution_youtube_tags",
}

PARENT_MARKER = "{parent}"


def _expand_parent_marker(value, parent_value, field: str):
    """Compose a text-field override with its parent's effective value: every
    "{parent}" in *value* becomes *parent_value*. Non-text fields, non-string
    values and marker-less text pass through untouched."""
    if field not in STYLE_TEXT_FIELDS or not isinstance(value, str) or PARENT_MARKER not in value:
        return value
    return value.replace(PARENT_MARKER, str(parent_value or "")).strip()


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


def _norm_size_presets(value) -> dict:
    """Normalize a style's Small/Medium/Large size presets into a complete,
    valid {bucket: {minutes, scenes, resolution}} dict.

    Minutes are the authoritative length; a preset that predates them (scenes
    only) gets its minutes derived from the scene count's legacy ~9 s length.
    Missing buckets, non-numeric/out-of-range values and unrecognized
    resolution names fall back to the built-in defaults, so callers can rely on
    every bucket being present and its resolution being a known name. Always
    returns a fresh dict — never the shared default object."""
    from pipeline import cadence
    value = value if isinstance(value, dict) else {}
    out = {}
    for bucket in _SIZE_BUCKETS:
        default = _DEFAULT_SIZE_PRESETS[bucket]
        raw = value.get(bucket)
        raw = raw if isinstance(raw, dict) else {}
        try:
            scenes = int(raw.get("scenes", default["scenes"]))
        except (TypeError, ValueError):
            scenes = default["scenes"]
        scenes = max(1, min(MAX_SCENES, scenes))
        try:
            minutes = float(raw.get("minutes", 0) or 0)
        except (TypeError, ValueError):
            minutes = 0.0
        if minutes <= 0:
            # Pre-minutes preset: carry over the length its scenes produced —
            # unless the bucket is untouched, where the new default applies.
            minutes = (default["minutes"] if "scenes" not in raw
                       else cadence.minutes_for_scenes(scenes))
        minutes = round(max(0.25, min(cadence.MAX_MINUTES, minutes)), 2)
        resolution = raw.get("resolution")
        if resolution not in _RESOLUTIONS:
            resolution = default["resolution"]
        out[bucket] = {"minutes": minutes, "scenes": scenes, "resolution": resolution}
    return out


def _norm_characters(value) -> list[dict]:
    """Normalize a style's recurring-character list into complete, valid entries.

    Drops non-dicts and entries with a blank name; gives each a stable id;
    coerces aliases to a list of non-empty strings and the scalar fields to their
    types; clamps ref_strength to 0.0–1.0. Always returns fresh dicts (never the
    shared default object), so callers can mutate freely. An empty/invalid input
    yields an empty list, which means "no characters" — identical to today."""
    rows = value if isinstance(value, list) else []
    out, seen = [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        # Ids become filenames (<id>.png under the characters dir), so anything
        # path-like is stripped rather than trusted.
        cid = re.sub(r"[^A-Za-z0-9_-]", "", str(raw.get("id") or "").strip())
        cid = cid or f"char_{uuid.uuid4().hex[:8]}"
        while cid in seen:
            cid = f"char_{uuid.uuid4().hex[:8]}"
        seen.add(cid)
        aliases = raw.get("aliases")
        aliases = aliases if isinstance(aliases, list) else []
        aliases = [str(a).strip() for a in aliases if str(a).strip()]
        try:
            strength = float(raw.get("ref_strength", 1.0))
        except (TypeError, ValueError):
            strength = 1.0
        strength = max(0.0, min(1.0, strength))
        out.append({
            "id": cid,
            "name": name,
            "aliases": aliases,
            "description": str(raw.get("description") or "").strip(),
            "ref_image": str(raw.get("ref_image") or "").strip(),
            "ref_strength": strength,
            "enabled": bool(raw.get("enabled", True)),
            # Owning style: "" ⟹ the global pool (every style inherits it);
            # else visible to that style and its descendants. A scope naming a
            # style that no longer exists is KEPT (dormant until the style is
            # restored or the user re-homes it) — same philosophy as a
            # dangling style parent.
            "style": str(raw.get("style") or "").strip(),
            # Named voice (from the voices store) this character speaks with in
            # dialogue scenes; "" ⟹ fall back to the style's narrator voice.
            "voice": str(raw.get("voice") or "").strip(),
            # Voice-casting hints (from the LLM identify or the user) driving
            # the library-voice auto-pick; "" ⟹ unconstrained.
            "gender": str(raw.get("gender") or "").strip().lower(),
            "age": str(raw.get("age") or "").strip().lower(),
        })
    return out


_FEMALE_CUES = ("woman", "female", "she ", "her ", "girl", "lady", "queen", "mrs", "miss", "mother", "sister")
_MALE_CUES = ("man", "male", "he ", "his ", "boy", "gentleman", "king", "mr ", "mr.", "father", "brother", "sir ")
_AGE_NEIGHBORS = {"young": "adult", "adult": "mature", "mature": "adult", "elderly": "mature"}


def _guess_gender(char: dict) -> str:
    """The character's stated gender, else a cue-word guess from the description."""
    g = str(char.get("gender") or "").strip().lower()
    if g in ("male", "female"):
        return g
    text = f" {char.get('description', '')} ".lower()
    f = sum(text.count(c) for c in _FEMALE_CUES)
    m = sum(text.count(c) for c in _MALE_CUES)
    return "female" if f > m else ("male" if m > f else "")


def _auto_assign_character_voices(chars: list[dict], cfg: dict,
                                  exclude: str | None = None) -> list[dict]:
    """Give each voiceless character a fitting voice from the library.

    Library voices carry gender/age/accent metadata (scripts/download_voices.py).
    Match gender first (hard requirement when known), then prefer the exact age
    bracket, then a neighbouring one; spread picks across the cast so two
    characters don't share a voice until the pool runs dry. The style narrator's
    voice is excluded — a character sounding exactly like the narrator was the
    original bug. Characters with a voice already set are untouched."""
    pool = [v for v in (cfg.get("voices") or [])
            if isinstance(v, dict) and v.get("gender") and v.get("name")
            and v.get("name") != (exclude or "")]
    if not pool:
        return chars
    used = {c.get("voice") for c in chars if c.get("voice")}

    def pick(char: dict) -> str:
        gender = _guess_gender(char)
        age = str(char.get("age") or "").strip().lower()

        def score(v: dict, allow_used: bool) -> float:
            if not allow_used and v["name"] in used:
                return -1
            s = 0.0
            if gender and v.get("gender") == gender:
                s += 4
            elif gender and v.get("gender") != gender:
                s -= 4  # wrong-gender voice is worse than no match data
            if age:
                if v.get("age") == age:
                    s += 2
                elif v.get("age") == _AGE_NEIGHBORS.get(age):
                    s += 1
            return s
        for allow_used in (False, True):
            ranked = sorted(pool, key=lambda v: (-score(v, allow_used), v["name"]))
            if ranked and score(ranked[0], allow_used) >= 0:
                return ranked[0]["name"]
        return ""

    for c in chars:
        if not c.get("voice"):
            name = pick(c)
            if name:
                c["voice"] = name
                used.add(name)
    return chars


def _ensure_characters(cfg: dict) -> dict:
    """Normalize the character library in place, migrating older layouts on
    first run.

    v2 (characters_migrated_v2): characters used to live on each style
    (cfg["styles"][i]["characters"]); they became one shared library
    (cfg["characters"]) that styles opted into by id (character_ids).

    v3 (characters_scoped_v3): the opt-in lists are gone — each character now
    carries a single "style" scope instead. "" = the global pool (every style
    inherits it automatically); a style name = owned by that style and visible
    to it and its descendants. The migration derives the scope from the old
    opt-ins: a character listed in exactly ONE style's own character_ids
    becomes that style's; one listed by no style but named exactly like a
    style (the narrator personas) goes to that style; anything else — shared
    across styles, or only ever reached through auto_accept_characters —
    stays global. The obsolete opt-in fields are removed from every style and
    from the flat mirrors. Runs BEFORE _ensure_styles."""
    styles = [s for s in (cfg.get("styles") or []) if isinstance(s, dict)]
    if not cfg.get("characters_migrated_v2"):
        library, seen = [], set()
        for s in styles:
            ids = []
            for c in _norm_characters(s.get("characters")):
                if c["id"] not in seen:
                    seen.add(c["id"])
                    library.append(c)
                ids.append(c["id"])
            # Preserve any character_ids already present (idempotent re-runs)
            # ahead of the migrated ones, deduped. A sparse child style (has a
            # parent) with nothing to migrate is left alone — stamping [] would
            # freeze an empty roster over its inherited one.
            existing = [str(x) for x in (s.get("character_ids") or []) if str(x)]
            if existing or ids or not s.get("parent"):
                s["character_ids"] = list(dict.fromkeys([*existing, *ids]))
        if library:
            cfg["characters"] = library + _norm_characters(cfg.get("characters"))
        cfg.pop("default_characters", None)  # obsolete flat mirror
        cfg["characters_migrated_v2"] = True
    cfg["characters"] = _norm_characters(cfg.get("characters"))
    if not cfg.get("characters_scoped_v3"):
        owners: dict[str, list[str]] = {}
        for s in styles:
            sname = str(s.get("name") or "").strip()
            ids = s.get("character_ids")
            for cid in (ids if isinstance(ids, list) else []):
                cid = str(cid or "").strip()
                if cid and sname and sname not in owners.setdefault(cid, []):
                    owners[cid].append(sname)
        style_names = {str(s.get("name") or "").strip() for s in styles} - {""}
        for c in cfg["characters"]:
            if c.get("style"):  # already scoped by hand — keep it
                continue
            own = owners.get(c["id"], [])
            if len(own) == 1:
                c["style"] = own[0]
            elif not own and c["name"] in style_names:
                c["style"] = c["name"]
        for s in styles:
            s.pop("character_ids", None)
            s.pop("auto_accept_characters", None)
        cfg.pop("default_character_ids", None)
        cfg.pop("default_auto_accept_characters", None)
        cfg["characters_scoped_v3"] = True
    return cfg


def _norm_engine(value, slot: str) -> str:
    """Coerce an engine key to a valid one for *slot* ('generate' or 'edit'),
    falling back to the default engine when unknown or not capable of that slot."""
    eng = engines.get(value)
    ok = eng and (eng["can_generate"] if slot == "generate" else eng["can_edit"])
    return value if ok else engines.DEFAULT_ENGINE


def _norm_video_engine(value) -> str:
    """Coerce a video engine key to a known one (see pipeline/engines.py
    VIDEO_ENGINES), falling back to the default (LTX 2.5) — this also migrates
    configs still carrying the removed ``ltx23`` key."""
    return value if engines.get_video(value) else engines.DEFAULT_VIDEO_ENGINE


def _norm_music_engine(value) -> str:
    """Coerce a music engine key to a known one (see pipeline/engines.py
    MUSIC_ENGINES), falling back to the default (ACE-Step 1.5)."""
    return value if engines.get_music(value) else engines.DEFAULT_MUSIC_ENGINE


def _norm_video_steps(value) -> int:
    """Clamp the per-style video steps override to 0..50 (0 = engine default)."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(50, v))


def _norm_tts_engine(value) -> str:
    """Coerce a TTS engine key to a known one (see pipeline/tts_engines.py)."""
    return tts_engines.norm(value)


def _norm_tts_language(value) -> str:
    """Coerce a narration language to a supported code, falling back to English."""
    from pipeline.chatterbox import norm_language
    return norm_language(value if isinstance(value, str) else "")


def _norm_tts_sentence_pause(value) -> float:
    """Clamp the per-style sentence gap to a sane 0..5 s (0 = model pacing)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(5.0, v))


def _norm_voice_cadence_wpm(value) -> float:
    """Clamp the target cadence to a speakable 60..300 words/minute
    (0 = the voice's natural pace)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v <= 0 else max(60.0, min(300.0, v))


def _norm_video_minutes(value) -> float:
    """Clamp the target video length to 0.25..40 minutes (0 = derive the
    length from the legacy n_scenes value)."""
    from pipeline import cadence
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v <= 0 else round(max(0.25, min(cadence.MAX_MINUTES, v)), 2)


def _norm_video_scenes(value) -> int:
    """Clamp the scene count to 0..MAX_SCENES (0 = derive it from the length)."""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_SCENES, v))


def _norm_reference_engine(value) -> str:
    """Coerce a Ref2VA engine key (performance films) to a known one."""
    return engines.resolve_reference({}, value)["key"]


def _norm_h3_chain_scenes(value) -> bool:
    """Coerce the chained-scenes toggle to a plain bool (YAML/JSON/form)."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _norm_h3_silent_scenes(value) -> bool:
    """Coerce the acted-silent-scenes toggle to a plain bool (YAML/JSON/form)."""
    return _norm_h3_chain_scenes(value)


def _norm_first_frame_cover(value) -> str:
    """Coerce a first-frame cover mode to "none" | "image" | "text"."""
    from pipeline.cover import norm_first_frame_cover
    return norm_first_frame_cover(value)


def _norm_first_frame_cover_seconds(value) -> float:
    """Coerce how long the burned cover is held to 0.04..3.0 seconds."""
    from pipeline.cover import norm_first_frame_cover_seconds
    return norm_first_frame_cover_seconds(value)


def _norm_cover_typography(value) -> dict:
    """Coerce a style's cover_typography dict (see pipeline/cover_typography.py)."""
    from pipeline.cover_typography import norm_cover_typography
    return norm_cover_typography(value)


def _ensure_styles(cfg: dict, fresh: bool = False) -> dict:
    """Normalize the style list in place: migrate a pre-styles config, drop
    malformed entries, fill missing fields, dedupe names, validate
    default_style, and mirror the default style onto the flat keys.

    A style with a ``parent`` (style hierarchy) is kept SPARSE: only the fields
    it explicitly overrides are stored, so everything else keeps flowing from
    the parent chain when the parent is later edited. Root styles (no parent)
    stay dense exactly as before. Self-parents are dropped and parent cycles
    are severed (the severed style is densified so it behaves like a root);
    a parent name that doesn't exist is kept as-is — the style resolves like a
    root until the parent reappears (e.g. after a config restore)."""
    styles = [s for s in (cfg.get("styles") or [])
              if isinstance(s, dict) and str(s.get("name") or "").strip()]
    if not styles:
        styles = [_style_from_flat(cfg, BLANK_STYLE_NAME if fresh else LEGACY_STYLE_NAME)]

    # One-time fold of the removed "Robotic voice" toggle into the level (level
    # 0 now IS off): a style whose toggle resolved off gets an explicit 0 so its
    # voice stays natural; one that resolved on keeps its effective level.
    # Presence-triggered, so it runs once and is a no-op on folded configs.
    if "default_voice_robotic" in cfg or any("voice_robotic" in s for s in styles):
        by_raw = {str(s.get("name") or "").strip(): s for s in reversed(styles)}

        def _legacy_eff(style: dict, key: str, flat: str, fallback):
            cur, hops = style, 0
            while cur is not None and hops <= len(styles):   # hop cap severs cycles
                if key in cur:
                    return cur[key]
                cur, hops = by_raw.get(str(cur.get("parent") or "").strip()), hops + 1
            return cfg.get(flat, fallback)

        folded = []
        for s in styles:
            on = bool(_legacy_eff(s, "voice_robotic", "default_voice_robotic", False))
            amount = _legacy_eff(s, "voice_robotic_amount", "default_voice_robotic_amount", 0.35)
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                amount = 0.35
            folded.append(amount if on else 0.0)
        for s, amount in zip(styles, folded):
            # A sparse child that carried neither key keeps inheriting — its
            # parent's folded level is exactly its old effective behavior.
            if "voice_robotic" in s or "voice_robotic_amount" in s or not str(s.get("parent") or "").strip():
                s["voice_robotic_amount"] = amount
            s.pop("voice_robotic", None)
        cfg.pop("default_voice_robotic", None)

    # NO_STYLE is pre-seeded as taken so no real style can claim the sentinel.
    normalized, seen, missing = [], {NO_STYLE}, []
    for s in styles:
        base = str(s.get("name")).strip()
        name, n = base, 2
        while name in seen:
            name, n = f"{base} {n}", n + 1
        seen.add(name)
        parent = str(s.get("parent") or "").strip()
        if parent == name or parent == NO_STYLE:
            parent = ""
        row = {"name": name}
        if parent:
            row["parent"] = parent
        absent = set()
        if parent:
            # Sparse child: keep only explicit overrides (description included).
            # Absent fields resolve through the parent chain in style_settings.
            if "description" in s:
                row["description"] = str(s.get("description") or "")
            for field in STYLE_FIELD_TO_FLAT:
                if field in s:
                    row[field] = s[field]
        else:
            row["description"] = str(s.get("description") or "")
            for field, flat in STYLE_FIELD_TO_FLAT.items():
                if field in s:
                    row[field] = s[field]
                else:
                    row[field] = DEFAULT_CFG.get(flat)
                    absent.add(field)
        normalized.append(row)
        missing.append(absent)

    # Sever parent cycles (only reachable by hand-editing the YAML — the UI
    # excludes descendants from the parent picker). The style whose pointer
    # closes the loop loses it and is densified so it behaves like a root.
    by_name = {r["name"]: r for r in normalized}
    for row in normalized:
        chain_seen, cur = {row["name"]}, row
        while True:
            nxt = by_name.get(str(cur.get("parent") or ""))
            if nxt is None:
                break
            if nxt["name"] in chain_seen:
                cur.pop("parent", None)
                cur.setdefault("description", "")
                for field, flat in STYLE_FIELD_TO_FLAT.items():
                    cur.setdefault(field, DEFAULT_CFG.get(flat))
                break
            chain_seen.add(nxt["name"])
            cur = nxt

    cfg["styles"] = normalized
    if cfg.get("default_style") not in {s["name"] for s in normalized}:
        cfg["default_style"] = normalized[0]["name"]
    default_idx = next(i for i, s in enumerate(normalized) if s["name"] == cfg["default_style"])
    # A field that became per-style AFTER this config migrated (e.g.
    # description_suffix) has no value on any style yet. The flat key still
    # holds its real value and, by the mirror invariant, the flat keys ARE the
    # default style's settings — so the default style inherits it. Other
    # styles keep the built-in blank: leaking e.g. the Spielbot suffix into
    # every style was exactly the bug being fixed. (Root default only: on a
    # child, an absent field means "inherit from parent", not "adopt flat".)
    if not normalized[default_idx].get("parent"):
        for field in missing[default_idx]:
            flat = STYLE_FIELD_TO_FLAT[field]
            if flat in cfg:
                normalized[default_idx][field] = cfg[flat]
    # size_presets is a nested dict, not a scalar: coerce each style's copy into
    # a complete, valid structure (and give every row its own object) before the
    # mirror below snapshots the default style's onto the flat key. On sparse
    # children only fields they actually override are coerced — writing the
    # coerced value back for an absent field would freeze it as an override.
    def _coerce(row: dict, field: str, fn) -> None:
        if field in row or not row.get("parent"):
            row[field] = fn(row.get(field))

    for row in normalized:
        _coerce(row, "size_presets", _norm_size_presets)
        _coerce(row, "image_engine", lambda v: _norm_engine(v, "generate"))
        _coerce(row, "edit_engine", lambda v: _norm_engine(v, "edit"))
        _coerce(row, "video_engine", _norm_video_engine)
        _coerce(row, "video_steps", _norm_video_steps)
        _coerce(row, "h3_chain_scenes", _norm_h3_chain_scenes)
        _coerce(row, "h3_silent_scenes", _norm_h3_silent_scenes)
        _coerce(row, "reference_engine", _norm_reference_engine)
        _coerce(row, "music_engine", _norm_music_engine)
        _coerce(row, "tts_engine", _norm_tts_engine)
        _coerce(row, "tts_language", _norm_tts_language)
        _coerce(row, "tts_sentence_pause", _norm_tts_sentence_pause)
        _coerce(row, "voice_cadence_wpm", _norm_voice_cadence_wpm)
        _coerce(row, "video_minutes", _norm_video_minutes)
        _coerce(row, "video_scenes", _norm_video_scenes)
        _coerce(row, "first_frame_cover", _norm_first_frame_cover)
        _coerce(row, "first_frame_cover_seconds", _norm_first_frame_cover_seconds)
        _coerce(row, "cover_typography", _norm_cover_typography)
    # One-time flip of the old default engine (flux1-schnell) to the new default
    # (FLUX.2 Klein) so existing styles adopt it; runs once, then a deliberate
    # later flux1-schnell choice is preserved. (A child without its own
    # image_engine override is skipped by the None get — its parent's flip flows
    # through inheritance.)
    if not cfg.get("engines_default_v2"):
        for row in normalized:
            if row.get("image_engine") == "flux1-schnell":
                row["image_engine"] = engines.DEFAULT_ENGINE
            if row.get("edit_engine") == "flux1-schnell":
                row["edit_engine"] = engines.DEFAULT_ENGINE
        cfg["engines_default_v2"] = True
    default = normalized[default_idx]
    if default.get("parent"):
        # The flat keys must mirror the default style's EFFECTIVE values —
        # resume_generation.py and pipeline/llm.py read them from raw YAML with
        # no style resolution of their own.
        eff = style_settings(cfg, default["name"])
        for field, flat in STYLE_FIELD_TO_FLAT.items():
            cfg[flat] = eff[field]
    else:
        for field, flat in STYLE_FIELD_TO_FLAT.items():
            cfg[flat] = default[field]
    return cfg


def _norm_per_day(v) -> float | int:
    """Normalize a 'videos per day' cadence value: 0 (off/no limit) or a
    positive number, kept as an int when whole so the YAML stays clean."""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return 0
    if f <= 0:
        return 0
    return int(f) if f == int(f) else f


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
            # Community-comment engagement (issue #84): per-channel persona/guidance
            # for replying to non-request comments (empty = engagement off), and
            # whether approved drafts post immediately or wait for review.
            "engagement_prompt": str(c.get("engagement_prompt") or ""),
            "auto_respond": bool(c.get("auto_respond")),
            # Default YouTube category id for this channel's uploads (empty =
            # fall back to the global youtube_post_category).
            "video_category": str(c.get("video_category") or ""),
            # Spoken/metadata language for this channel's uploads (BCP-47), and
            # whether to attach a script-based caption track. Default: English,
            # captions on (absent key → on, so existing channels keep captions).
            "language": str(c.get("language") or "").strip() or "en",
            "upload_captions": bool(c.get("upload_captions", True)),
            # Publishing scheduler cadence (decoupled publish queue): how many
            # videos per day this channel releases, spaced evenly (2/day → one
            # every 12h). 0 = no throttle. Only consulted when
            # publish_schedule_enabled is on.
            "publish_per_day": _norm_per_day(c.get("publish_per_day")),
        })
    if not channels and yt._token_path().exists():
        # Pre-#22 setups have one token at the legacy path — surface it as the
        # "default" channel so styles can reference it without re-connecting.
        channels = [{"id": yt.DEFAULT_CHANNEL_KEY, "name": "", "channel_id": "",
                     "engagement_prompt": "", "auto_respond": False,
                     "video_category": "", "language": "en", "upload_captions": True,
                     "publish_per_day": 0}]
        seen = {yt.DEFAULT_CHANNEL_KEY}
    cfg["youtube_channels"] = channels
    for s in (cfg.get("styles") or []):
        if isinstance(s, dict) and s.get("channel") and s["channel"] not in seen:
            s["channel"] = ""
    if cfg.get("youtube_channel") and cfg["youtube_channel"] not in seen:
        cfg["youtube_channel"] = ""
    return cfg


def _style_channel_explicit(cfg: dict, style_name: str = "") -> str:
    """Channel KEY a style is EXPLICITLY assigned to (and still connected), or ''.

    Unlike :func:`channel_for_style` this does NOT fall back to the first channel.
    Idea dedup/grouping uses it so a style with no channel of its own isn't tied
    to an unrelated channel's library — the first-channel fallback is only for
    picking a real publish destination, never for deciding what's 'already made'."""
    keys = [c["id"] for c in (cfg.get("youtube_channels") or [])
            if isinstance(c, dict) and c.get("id")]
    ch = str(style_settings(cfg, style_name).get("channel") or "")
    return ch if ch in keys else ""


def _dedup_scope(cfg: dict, style_name: str = "") -> str:
    """Idea-dedup bucket for a style: its explicit channel, or a private per-style
    bucket when it has none — so two channel-less styles never dedup against each
    other, and none inherits the first channel's library via the publish fallback."""
    return _style_channel_explicit(cfg, style_name) or f"\x00style:{style_name}"


def channel_for_style(cfg: dict, style_name: str = "") -> str:
    """Channel KEY a style publishes to: the style's own channel if connected,
    else the first connected channel, else '' (the legacy single-channel token).

    The first-channel fallback is a *publish target* only; idea dedup uses
    :func:`_style_channel_explicit` so a channel-less style stays a clean slate."""
    keys = [c["id"] for c in (cfg.get("youtube_channels") or [])
            if isinstance(c, dict) and c.get("id")]
    return _style_channel_explicit(cfg, style_name) or (keys[0] if keys else "")


def _ensure_x_accounts(cfg: dict) -> dict:
    """Normalize the connected X accounts list (issue #107) in place — the X
    mirror of _ensure_channels. Drops malformed entries, dedupes keys, seeds the
    reserved "default" entry when a pre-multi-account token file exists, and
    clears style x_account references that point at an account that's no longer
    connected. Runs BEFORE _ensure_styles so the flat-key mirror is clean."""
    accounts, seen = [], set()
    for a in (cfg.get("x_accounts") or []):
        key = str(a.get("id") or "").strip() if isinstance(a, dict) else ""
        if not key or key in seen:
            continue
        seen.add(key)
        accounts.append({
            "id": key,
            "name": str(a.get("name") or ""),
            "account_id": str(a.get("account_id") or ""),
            "premium": bool(a.get("premium")),
            # Community-engagement config (mirrors YouTube channels): per-account
            # persona/guidance for replying to mentions, and whether approved
            # drafts post immediately or wait for review.
            "engagement_prompt": str(a.get("engagement_prompt") or ""),
            "auto_respond": bool(a.get("auto_respond")),
            "language": str(a.get("language") or "").strip() or "en",
            # Publishing scheduler cadence (mirror of YouTube channels): videos
            # per day, spaced evenly. 0 = no throttle.
            "publish_per_day": _norm_per_day(a.get("publish_per_day")),
        })
    if not accounts and xt._token_path().exists():
        accounts = [{"id": xt.DEFAULT_ACCOUNT_KEY, "name": "", "account_id": "",
                     "premium": False, "engagement_prompt": "", "auto_respond": False,
                     "language": "en", "publish_per_day": 0}]
        seen = {xt.DEFAULT_ACCOUNT_KEY}
    cfg["x_accounts"] = accounts
    for s in (cfg.get("styles") or []):
        if isinstance(s, dict) and s.get("x_account") and s["x_account"] not in seen:
            s["x_account"] = ""
    if cfg.get("x_account") and cfg["x_account"] not in seen:
        cfg["x_account"] = ""
    return cfg


def x_account_for_style(cfg: dict, style_name: str = "") -> str:
    """Account KEY a style publishes to on X, or '' for none. Unlike YouTube,
    X is opt-in per style (issue #107): a style only posts to X when it has
    explicitly picked a connected account — a blank choice means don't post."""
    keys = [a["id"] for a in (cfg.get("x_accounts") or [])
            if isinstance(a, dict) and a.get("id")]
    acc = str(style_settings(cfg, style_name).get("x_account") or "")
    return acc if acc in keys else ""


def playlist_for_style(cfg: dict, style_name: str = "") -> str:
    """Raw playlist choice for a style: "" (none), a playlist id, or the
    "__auto__" sentinel (find-or-create one named after the style). The sentinel
    is resolved to a real id at upload time, since that needs a YouTube API call."""
    return str(style_settings(cfg, style_name).get("youtube_playlist_id") or "")


def made_for_kids_for_style(cfg: dict, style_name: str = "") -> bool:
    """Whether a style's uploads should be self-declared "Made for Kids"."""
    return bool(style_settings(cfg, style_name).get("made_for_kids"))


def _style_lineage(styles: list[dict], target: dict) -> list[dict]:
    """Ancestry chain ``[root, …, target]`` for a style, following ``parent``
    names (style hierarchy: a child style stores only its overrides and
    inherits the rest from its parent chain).

    Tolerates a dangling parent (the walk just stops — the style then resolves
    against the flat keys like a root) and cycles (the walk never revisits a
    style). _ensure_styles severs stored cycles; the guard here keeps reads
    safe on non-normalized dicts too."""
    by_name = {}
    for s in styles:
        if isinstance(s, dict) and str(s.get("name") or ""):
            by_name.setdefault(str(s.get("name")), s)
    chain, seen = [], set()
    cur = target
    while isinstance(cur, dict):
        nm = str(cur.get("name") or "")
        if nm in seen:
            break
        seen.add(nm)
        chain.append(cur)
        pname = str(cur.get("parent") or "").strip()
        cur = by_name.get(pname) if pname else None
    chain.reverse()
    return chain


def style_settings(cfg: dict, name: str = "") -> dict:
    """Resolved settings for the named style profile.

    Falls back to the default style when *name* is empty or unknown, and to
    the flat keys / built-in defaults for any missing field — so this is safe
    to call with non-normalized dicts too.

    A style with a ``parent`` inherits every field it doesn't set itself from
    its ancestry chain (nearest ancestor wins), so variant styles can override
    just a narrator or language while tracking the parent for everything else.

    ``name == NO_STYLE`` is the experiment mode: the content-shaped fields
    (visual style, extra instructions, voice, robotic level, speed) come back blank so
    nothing is imposed on the video, while render quality and the audio mix
    keep the default style's values."""
    out = {field: cfg.get(flat, DEFAULT_CFG.get(flat))
           for field, flat in STYLE_FIELD_TO_FLAT.items()}
    out["description"] = ""
    requested = (name or "").strip()
    styles = [s for s in (cfg.get("styles") or []) if isinstance(s, dict)]
    target = None
    if requested != NO_STYLE:
        target = next((s for s in styles if s.get("name") == requested), None)
    if target is None:
        target = next((s for s in styles if s.get("name") == cfg.get("default_style")),
                      styles[0] if styles else None)
    if target:
        # Root-first ancestry walk: each level's explicit fields override its
        # parent's, so a sparse child inherits everything it doesn't set. A
        # text field containing "{parent}" composes with (rather than replaces)
        # the value accumulated so far — i.e. the parent's effective text. At
        # the chain root there is no reachable parent, so the marker expands
        # to empty instead of leaking the flat-key seed.
        for i, s in enumerate(_style_lineage(styles, target)):
            for k in STYLE_FIELD_TO_FLAT:
                if k in s:
                    out[k] = _expand_parent_marker(s[k], out.get(k) if i else "", k)
            if "description" in s:
                out["description"] = _expand_parent_marker(
                    str(s.get("description") or ""),
                    out.get("description") if i else "", "description")
    if requested == NO_STYLE:
        out.update(visual_style="", video_style="", video_negative_prompt="",
                   extra_instructions="", script_avoid="", description_suffix="",
                   attribution_description="", attribution_hashtags="",
                   attribution_youtube_tags="",
                   title_style="", voice="", voice_robotic_amount=0.0, voice_speed=1.0,
                   voice_cadence_wpm=0)
        out["name"] = NO_STYLE
        out["description"] = ""
        return out
    out["name"] = (target or {}).get("name", "")
    return out


def style_video_minutes(ss: dict) -> float:
    """Effective target length (minutes) for a resolved style-settings dict —
    the explicit ``video_minutes`` when set, else the length the style's
    legacy ``n_scenes`` used to produce."""
    from pipeline import cadence
    try:
        m = float(ss.get("video_minutes") or 0)
    except (TypeError, ValueError):
        m = 0.0
    if m > 0:
        return m
    try:
        n = int(ss.get("n_scenes") or 0)
    except (TypeError, ValueError):
        n = 0
    return cadence.minutes_for_scenes(n or int(DEFAULT_CFG["default_n_scenes"]))


def style_video_scenes(ss: dict) -> int:
    """How many scenes this style divides its length into — 0 when it leaves
    that to the scene contract (``video_scenes``, the per-style default for
    the Create screen's scene count)."""
    return _norm_video_scenes(ss.get("video_scenes"))


def style_script_plan(ss: dict, minutes: float | None = None,
                      n_scenes: int | None = None) -> dict:
    """Cadence plan (word budget + scene caps) for a resolved style-settings
    dict. Explicit *minutes* wins, else the style's own length; an explicit
    *n_scenes* (else the style's ``video_scenes``) divides that length into
    that many scenes, so fewer scenes are longer ones. A count with no length
    at all is the legacy redraft path: the count sets the length."""
    from pipeline import cadence
    wpm, measured = cadence.effective_wpm(ss)
    try:
        pause = float(ss.get("tts_sentence_pause") or 0)
    except (TypeError, ValueError):
        pause = 0.0
    # Chained scenes widen the per-scene window, so the same runtime is planned
    # as fewer scenes each carrying proportionally more narration. Only the
    # MiniMax engines chain; LTX has its own continuation and ignores the flag.
    chained = bool(ss.get("h3_chain_scenes")) and \
        str(ss.get("video_engine") or "").startswith("minimax-h3")
    count = int(n_scenes or 0) or style_video_scenes(ss)
    ceiling = cadence.engine_scene_ceiling(ss.get("video_engine"), chained)
    if n_scenes and int(n_scenes) > 0 and not (minutes and float(minutes) > 0):
        plan = cadence.plan_for_scenes(int(n_scenes), wpm, pause, chained)
    else:
        plan = cadence.plan_script(
            float(minutes) if minutes and float(minutes) > 0 else style_video_minutes(ss),
            wpm, pause, chained, n_scenes=count, scene_ceiling=ceiling)
    plan["n_scenes"] = min(plan["n_scenes"], MAX_SCENES)
    plan["wpm_measured"] = measured
    plan["voice"] = ss.get("voice") or ""
    # Acted scenes always render through a MiniMax reference engine (there is
    # no LTX Ref2VA), so their chaining follows the toggle alone — an LTX
    # narrated style still chains its dialogue scenes. The acted split in
    # story.py reads this key.
    plan["chained_acted"] = bool(ss.get("h3_chain_scenes"))
    return plan

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
                if ts <= cutoff:
                    continue
                # An item whose render already errored/finished is stale queue
                # bookkeeping, not a running job — counting it would block
                # automation from starting the next item for up to 24 h.
                wd = q.get("work_dir")
                if wd:
                    try:
                        job_meta = json.loads((Path(wd) / "job.json").read_text())
                        if job_meta.get("status") in ("error", "cancelled", "paused", "done"):
                            continue
                    except Exception:
                        pass
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


# ── Config helpers ───────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load the single YAML config. Saved values are authoritative — worker
    lists (comfy_workers/tts_workers) live here and are edited from the Settings
    screen."""
    cfg = DEFAULT_CFG.copy()
    data = {}
    if CONFIG_FILE.exists():
        data = yaml.safe_load(CONFIG_FILE.read_text()) or {}
        if isinstance(data, dict):
            cfg.update(data)
    # youtube_auto_ai_ideas was split out of youtube_auto_approve_script (which
    # used to imply it): seed it from the old toggle for configs saved before the
    # split, so existing fully-automated setups keep inventing ideas.
    if isinstance(data, dict) and "youtube_auto_ai_ideas" not in data:
        cfg["youtube_auto_ai_ideas"] = bool(cfg.get("youtube_auto_approve_script"))
    # A config that predates styles but already carries customized content
    # settings becomes the "Stephen Spielbot" style; anything else (no file, or
    # an install-seeded file with only worker lists) starts with a blank
    # "Default" style.
    fresh = not any(flat in data for flat in STYLE_FIELD_TO_FLAT.values())
    _ensure_channels(cfg)
    _ensure_x_accounts(cfg)
    _ensure_characters(cfg)
    return _ensure_styles(cfg, fresh=fresh)


def save_config(cfg: dict) -> None:
    _ensure_channels(cfg)
    _ensure_x_accounts(cfg)
    _ensure_characters(cfg)
    _ensure_styles(cfg)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    # The config holds API keys in cleartext — keep it out of other users' reach.
    os.chmod(CONFIG_FILE, 0o600)


# Config keys whose VALUE is a credential — never returned to the browser, and
# preserved (not overwritten with blank) when the client saves them empty.
# youtube_client_secrets is a file PATH, not a secret, so it is deliberately
# excluded here.
_SECRET_VALUE_KEYS = ("claude_api_key", "grok_api_key", "openai_api_key", "hf_token", "x_client_secret")


def public_config(cfg: dict) -> dict:
    """A copy of *cfg* safe to return to the browser: every credential value is
    blanked and a ``<key>_set`` boolean is added so the UI can show a
    'saved — leave blank to keep' placeholder without ever exposing the secret."""
    safe = dict(cfg)
    for key in _SECRET_VALUE_KEYS:
        val = safe.get(key)
        safe[f"{key}_set"] = bool(isinstance(val, str) and val.strip())
        safe[key] = ""
    # Attach each catalogue character's look-version history for the Settings UI.
    # Copies (never the persisted dicts), and _norm_characters drops it on save.
    root = _global_char_hist_root()
    safe["characters"] = [
        {**c, "history": image_history.char_history(root, c.get("id", ""))}
        for c in (cfg.get("characters") or [])
    ]
    return safe


def merge_config_update(current: dict, update: dict) -> dict:
    """Merge a client config *update* onto *current* for persistence: drop the
    UI-only ``<key>_set`` flags, and keep an existing credential when the client
    sends it blank (the 'leave blank to keep' contract of :func:`public_config`)."""
    merged = dict(current)
    for key, val in update.items():
        if key.endswith("_set"):
            continue
        if key in _SECRET_VALUE_KEYS and not (isinstance(val, str) and val.strip()):
            continue
        merged[key] = val
    return merged


# ── Settings backup / restore (issue #106) ──────────────────────────────────
# Everything the app persists lives under the config dir. A backup is a zip of
# that dir (minus regenerable scratch files); "operational" state is the subset
# the app re-accumulates on its own (queue, fetched comments/analytics, AI
# ideas, the trained engagement model) — the rest is settings + credentials you
# can't regenerate (config.yaml, YouTube client secrets + tokens, voices).

BACKUP_APP_ID = "stephen-spielbot"

# Operational state, identified by basename / top-level subdir under the config
# dir. Anything NOT listed here is treated as a setting/credential.
_OPERATIONAL_FILE_NAMES = {
    "youtube_queue.json",
    "youtube_comments.json",
    "youtube_analytics.json",
    "youtube_suggestions.json",
    "youtube_dismissed_suggestions.json",
    "youtube_daily_uploads.json",
    "last_session.json",
    "ui_seen.json",
}
_OPERATIONAL_TOP_DIRS = {"engagement"}

# Regenerable scratch that should never enter a backup: editor/migration
# backups and the voice-audition cache (top-level voice_test*.wav; the real
# reference clips live under voices/ and are kept).
_BACKUP_BAK_GLOBS = ("*.bak", "*.bak-*", "*.bck", "*.bck.*", "*.migrated-bak",
                     "*.orig", "*.tmp", "*~")


def _is_backup_junk(rel: str) -> bool:
    p = Path(rel)
    if len(p.parts) == 1 and fnmatch.fnmatch(p.name, "voice_test*"):
        return True
    return any(fnmatch.fnmatch(p.name, g) for g in _BACKUP_BAK_GLOBS)


def _is_operational(rel: str) -> bool:
    p = Path(rel)
    return p.parts[0] in _OPERATIONAL_TOP_DIRS or p.name in _OPERATIONAL_FILE_NAMES


def _backup_files(scope: str) -> list[tuple[str, Path]]:
    """(arcname, path) pairs to put in a backup of the given scope, sorted."""
    cfg_dir = CONFIG_FILE.parent
    if not cfg_dir.exists():
        return []
    items = []
    for p in cfg_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(cfg_dir).as_posix()
        if _is_backup_junk(rel):
            continue
        if scope == "operational" and not _is_operational(rel):
            continue
        items.append((rel, p))
    return sorted(items)


def build_settings_backup(scope: str = "full") -> tuple[bytes, str]:
    """Zip the config dir (full) or just its operational state, with a manifest.

    Returns (zip_bytes, suggested_filename)."""
    if scope not in ("full", "operational"):
        raise ValueError("scope must be 'full' or 'operational'")
    items = _backup_files(scope)
    manifest = {
        "app": BACKUP_APP_ID,
        "kind": "settings-backup",
        "scope": scope,
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "files": [rel for rel, _ in items],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup_manifest.json", json.dumps(manifest, indent=2))
        for rel, path in items:
            zf.write(path, rel)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return buf.getvalue(), f"{BACKUP_APP_ID}-{scope}-{stamp}.zip"


def restore_settings_backup(data: bytes) -> dict:
    """Extract a backup zip over the config dir (overlay; existing files win
    only where the backup is silent). Validates the manifest and refuses unsafe
    paths. Returns {scope, restored:[arcname,...]}."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That file is not a valid backup (.zip).")
    with zf:
        if "backup_manifest.json" not in zf.namelist():
            raise ValueError("Missing backup manifest — is this a Stephen Spielbot backup?")
        try:
            manifest = json.loads(zf.read("backup_manifest.json"))
        except Exception:
            raise ValueError("The backup manifest is unreadable.")
        if manifest.get("app") != BACKUP_APP_ID:
            raise ValueError("This backup was not produced by Stephen Spielbot.")

        cfg_dir = CONFIG_FILE.parent
        cfg_dir.mkdir(parents=True, exist_ok=True)
        root = cfg_dir.resolve()
        restored = []
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or name == "backup_manifest.json":
                continue
            # Guard against zip-slip / absolute paths escaping the config dir.
            if os.path.isabs(name) or ".." in Path(name).parts:
                raise ValueError(f"Unsafe path in backup: {name}")
            target = (cfg_dir / name).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"Unsafe path in backup: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            restored.append(Path(name).as_posix())
    return {"scope": manifest.get("scope", "full"), "restored": sorted(restored)}






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


_VOICE_GENDERS = ("", "male", "female")
_VOICE_AGES = ("", "young", "adult", "mature", "elderly")


def _voice_meta_fields(gender=None, age=None, accent=None, tone=None) -> dict:
    """Validated casting-metadata updates for a voice; None ⟹ leave untouched.

    gender/age drive app._auto_assign_character_voices — a voice with no gender
    is never auto-cast onto a character (it stays manual-pick only)."""
    out = {}
    if gender is not None:
        g = str(gender).strip().lower()
        if g not in _VOICE_GENDERS:
            raise ValueError(f"gender must be one of {_VOICE_GENDERS[1:]} (or empty).")
        out["gender"] = g
    if age is not None:
        a = str(age).strip().lower()
        if a not in _VOICE_AGES:
            raise ValueError(f"age must be one of {_VOICE_AGES[1:]} (or empty).")
        out["age"] = a
    if accent is not None:
        out["accent"] = str(accent).strip()
    if tone is not None:
        out["tone"] = str(tone).strip()
    return out


def add_voice(name: str, audio: bytes, ext: str = ".wav",
              gender=None, age=None, accent=None, tone=None) -> dict:
    """Save a new reference clip and register the voice. Returns the new config."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Voice name is required.")
    meta = _voice_meta_fields(gender, age, accent, tone)
    cfg = load_config()
    voices = cfg.get("voices", []) or []
    if name == F5TTS_DEFAULT_OPTION or any(v["name"] == name for v in voices):
        raise ValueError(f"A voice named “{name}” already exists.")
    path = _new_voice_path(name, ext)
    path.write_bytes(audio)
    voices.append({"name": name, "path": str(path), **meta})
    cfg["voices"] = voices
    save_config(cfg)
    return cfg


def update_voice(name: str, new_name: str | None = None,
                 audio: bytes | None = None, ext: str = ".wav",
                 gender=None, age=None, accent=None, tone=None) -> dict:
    """Rename a voice, replace its clip, and/or set its casting metadata
    (gender/age/accent/tone — what the character auto-cast matches on).
    Returns the new config."""
    meta = _voice_meta_fields(gender, age, accent, tone)
    cfg = load_config()
    voices = cfg.get("voices", []) or []
    voice = next((v for v in voices if v["name"] == name), None)
    if voice is None:
        raise ValueError(f"No voice named “{name}”.")
    voice.update(meta)
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


def _terminate_job_process(work_dir: Path) -> bool:
    """SIGTERM the generation subprocess recorded in work_dir's job.json.

    Returns True when a running process was signalled."""
    try:
        meta = _read_json(_job_meta_path(work_dir))
    except Exception:
        meta = {}
    pid = meta.get("pid")
    if pid and _process_running(pid):
        try:
            os.kill(int(pid), 15)  # SIGTERM
            return True
        except OSError as exc:
            logger.warning("Could not signal pid %d: %s", pid, exc)
    return False


def on_cancel_active_job(active_job_dir: str):
    job = _active_job_row(active_job_dir)
    if not job:
        return "No durable job available."
    work_dir = Path(job["work_dir"])
    killed = _terminate_job_process(work_dir)
    store = DurableStore.default()
    try:
        count = store.cancel_job(job["id"])
    finally:
        store.close()
    if work_dir.exists():
        # Without this, the dir still reads status "running": _is_job_running
        # would count it as live for 24 h and _reconcile_queue would flip its
        # queue item to "failed", auto-retrying a render cancelled on purpose.
        _write_job_meta(work_dir, status="cancelled", pid=None)
    return f"Cancelled {count} pending/running task(s)." + (
        " Render process terminated." if killed else ""
    )


def on_pause_active_job(active_job_dir: str) -> str:
    """Kill the generation subprocess and re-queue in-progress tasks so the job can be resumed later."""
    work_dir = _preferred_work_dir(active_job_dir)
    if not work_dir:
        return "No active job found."
    killed = _terminate_job_process(work_dir)
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






# ── TTS wrapper ──────────────────────────────────────────────────────────────



# ── Script generation ────────────────────────────────────────────────────────



# ── Scene image generation — one active scene only ───────────────────────────

_IMG_GEN_OUT_COUNT = 3


def _preview_worker_urls() -> list[str]:
    cfg = load_config()
    all_workers = cfg.get("comfy_workers", [])
    # Generating a preview means the UI is in use — keep a worker reserved for it
    # (issue #98) so the render leaves one idle for this and the next preview.
    ui_activity.mark_active()
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


def _character_sheet(characters: list[dict]) -> str:
    """Format a style's enabled, described characters into a prompt block for the
    script LLM, or "" when there are none. Passed to generate_script so scenes
    reference recurring characters BY NAME in their visual prompts —
    _inject_characters appends the canonical appearance (and reference image)
    to any prompt that names them, so a paraphrased description without the
    name breaks that match."""
    rows = [c for c in (characters or [])
            if c.get("enabled", True) and c.get("name") and c.get("description")]
    if not rows:
        return ""
    lines = "\n".join(f"- {c['name']}: {c['description']}" for c in rows)
    return (
        "RECURRING CHARACTERS — when a scene features one of these, refer to them "
        "BY NAME in that scene's image/video prompts. Write the NAME ONLY — never "
        "restate or paraphrase their appearance (the canonical appearance below is "
        "appended automatically to every prompt that names them). Only describe "
        "what DIFFERS from their canonical look in that scene, such as a change "
        "of clothes or an injury:\n"
        f"{lines}\n"
        "Only mention a character when the narration involves them; leave other "
        "scenes unaffected."
    )


# A scene rarely centres on more than a couple of named characters; cap the
# reference images per scene to bound VRAM on the workers (drops are logged).
_MAX_SCENE_REFERENCES = 2


def _characters_dir() -> Path:
    """Directory holding character reference images (sibling of the config YAML).
    Read from CONFIG_FILE at call time so tests that patch it are respected."""
    return CONFIG_FILE.parent / "characters"


def _character_image_path(filename: str) -> Path | None:
    """Absolute path of a stored character reference image, or None if the name
    is blank. The filename is basename-only (guards against path traversal)."""
    name = Path(str(filename or "")).name
    return _characters_dir() / name if name else None


def _global_char_hist_root() -> Path:
    """Root handed to image_history's char_* helpers for GLOBAL catalogue looks.
    Chosen so the canonical path (<root>/characters/<id>.png) is exactly
    _characters_dir()/<id>.png; versions land in <root>/image_history/."""
    return _characters_dir().parent


def _character_mentions(text: str, character: dict) -> bool:
    """True if the character's name or any alias appears in *text* as a whole
    word (case-insensitive)."""
    tokens = [character.get("name", "")] + list(character.get("aliases") or [])
    for tok in tokens:
        tok = (tok or "").strip()
        if tok and re.search(rf"\b{re.escape(tok)}\b", text, re.IGNORECASE):
            return True
    return False


def _character_name_blob(character: dict) -> str:
    """Name + aliases joined for cross-matching (e.g. 'Julius Caesar' ↔ 'Caesar')."""
    parts = [character.get("name", "")] + list(character.get("aliases") or [])
    return " ".join(str(p).strip() for p in parts if str(p or "").strip())


def _characters_refer_to_same(a: dict, b: dict) -> bool:
    """True when two character records share a name/alias (exact token or whole-word
    mention either way). Used to detect that an LLM-identified cast member is
    already the same person as a catalogue entry."""
    a_tokens = {
        t.strip().lower()
        for t in [a.get("name", "")] + list(a.get("aliases") or [])
        if (t or "").strip()
    }
    b_tokens = {
        t.strip().lower()
        for t in [b.get("name", "")] + list(b.get("aliases") or [])
        if (t or "").strip()
    }
    if a_tokens & b_tokens:
        return True
    a_blob, b_blob = _character_name_blob(a), _character_name_blob(b)
    if not a_blob or not b_blob:
        return False
    return _character_mentions(a_blob, b) or _character_mentions(b_blob, a)


def _style_characters(cfg: dict, style_name: str = "") -> list[dict]:
    """The library characters visible to a style, in library order.

    Every style automatically inherits the GLOBAL pool (characters with no
    "style" scope). A character scoped to a style is visible to that style and
    every style below it in the hierarchy — children inherit the parent's cast.
    An empty/unknown name resolves like style_settings (the default style).

    The "(none)" experiment style imposes no STYLE cast, but it still sees the
    global pool: those characters belong to the library, not to a style, so
    asking for one by name in experiment mode must reuse its look and voice
    rather than inventing a duplicate."""
    requested = (style_name or "").strip()
    if requested == NO_STYLE:
        return [c for c in (cfg.get("characters") or [])
                if isinstance(c, dict) and not c.get("style")]
    styles = [s for s in (cfg.get("styles") or []) if isinstance(s, dict)]
    target = next((s for s in styles if s.get("name") == requested), None)
    if target is None:
        target = next((s for s in styles if s.get("name") == cfg.get("default_style")),
                      styles[0] if styles else None)
    visible = {str(s.get("name") or "") for s in _style_lineage(styles, target)} if target else set()
    return [c for c in (cfg.get("characters") or [])
            if isinstance(c, dict) and (not c.get("style") or c.get("style") in visible)]


def _requested_characters(cfg: dict, style_name: str, *texts: str | None) -> list[dict]:
    """The style's catalogue characters the brief actually ASKED for — those whose
    name or alias appears in *texts* (the topic/description, the video title, the
    style's extra instructions).

    Handing the script LLM the whole catalogue made it cast library characters in
    every story, worst of all in acted films where the cast doubles as the
    speaker roster. A catalogue character now reaches the prompts only when the
    brief names them; otherwise the story invents its own people. Nothing is lost
    downstream: _characters_for_scene matches by name, so a character the story
    does name still gets their canonical look and voice."""
    blob = "\n".join(t for t in texts if (t or "").strip())
    if not blob.strip():
        return []
    return [c for c in _style_characters(cfg, style_name) if _character_mentions(blob, c)]


def _filter_identified_against_style(identified, cfg: dict, style_name: str = "") -> list[dict]:
    """Drop LLM-identified characters that already exist in the style's catalogue.

    Per-script characters shadow catalogue entries of the same name in
    :func:`_job_characters`, so re-creating "Julius Caesar" as a script character
    would override the global look/description. Only true *new* cast members
    become per-script characters; style-accessible globals keep winning.

    Matching is by name/alias (case-insensitive exact or whole-word either way).
    Only enabled catalogue characters visible to the style (global pool or
    scoped to its lineage) count as accessible.
    """
    style_chars = [
        c for c in _style_characters(cfg, style_name)
        if c.get("enabled", True) and (c.get("name") or "").strip()
    ]
    if not style_chars:
        return list(identified or [])
    out: list[dict] = []
    for idc in identified or []:
        if not isinstance(idc, dict) or not (idc.get("name") or "").strip():
            continue
        match = next((gc for gc in style_chars if _characters_refer_to_same(idc, gc)), None)
        if match is not None:
            logger.info(
                "Skipping per-script character %r — already in style %r catalogue as %r",
                idc.get("name"), style_name or "(default)", match.get("name"),
            )
            continue
        out.append(idc)
    return out


# ── Per-script characters ────────────────────────────────────────────────────
# A script can carry its OWN characters — identified by the LLM at generation
# time (see llm.generate_script) and living entirely inside the work dir, NOT
# the shared cfg["characters"] catalogue. They ride into that job's renders via
# _job_characters and can be promoted into the catalogue on demand. Catalogue
# characters already opted into the style are NOT re-created here (see
# _filter_identified_against_style) so they keep their global look.

def _script_characters_dir(work_dir: Path) -> Path:
    """Directory holding a script's own character reference images."""
    return Path(work_dir) / "characters"


def _script_characters_path(work_dir: Path) -> Path:
    """The script's own character list (sidecar of script.json)."""
    return Path(work_dir) / "characters.json"


def _read_script_characters(work_dir: Path) -> list[dict]:
    """The script's own characters (normalized), or [] when none are saved."""
    try:
        data = json.loads(_script_characters_path(work_dir).read_text())
    except (OSError, ValueError):
        return []
    return _norm_characters(data)


def _write_script_characters(work_dir: Path, characters) -> list[dict]:
    """Normalize and persist the script's own characters; returns the saved list."""
    norm = _norm_characters(characters)
    path = _script_characters_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(norm, indent=2))
    return norm


def _script_character_image_path(work_dir: Path, filename: str) -> Path | None:
    """Absolute path of a script character's reference image, or None if the name
    is blank. Basename-only (guards against path traversal), resolved inside the
    work dir rather than the global characters directory."""
    name = Path(str(filename or "")).name
    return _script_characters_dir(work_dir) / name if name else None


# ── Per-script visuals: locations and wardrobe ───────────────────────────────
# A performance film drifts between scenes because nothing but words pins the
# place or the clothes. A "visual" is a reference image with a job: a LOCATION
# the scene happens in, or WARDROBE a character wears. They ride the same
# <Picture N> slots as the cast (H3 takes nine), after it.
# location = the space; wardrobe = someone's clothes; image = any other
# reference the model should match (a prop, a vehicle, a logo…); video = the
# same, ingested from a clip — its extracted frame is what feeds the slot.
VISUAL_KINDS = ("location", "wardrobe", "image", "video")


def _assets_dir() -> Path:
    d = CONFIG_FILE.parent / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _asset_image_path(filename: str) -> Path | None:
    name = Path(str(filename or "")).name
    return _assets_dir() / name if name else None


def _norm_assets(raw) -> list[dict]:
    """Catalogue assets: the reusable half of the visual library.

    Characters are the other kind and keep their own list — they carry voices,
    aliases and casting rules these do not. Scoping matches theirs exactly: no
    style = the global pool, a style = that style and its children."""
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        kind = str(item.get("kind") or "location").strip().lower()
        out.append({
            "id": str(item.get("id") or f"ast_{uuid.uuid4().hex[:8]}"),
            "name": name,
            "kind": kind if kind in VISUAL_KINDS else "location",
            "description": str(item.get("description") or "").strip(),
            "character": str(item.get("character") or "").strip(),
            "ref_image": str(item.get("ref_image") or "").strip(),
            "style": str(item.get("style") or "").strip(),
            "enabled": bool(item.get("enabled", True)),
        })
    return out


def style_assets(cfg: dict, style_name: str = "") -> list[dict]:
    """Catalogue assets visible to a style — same rule as _style_characters."""
    requested = (style_name or "").strip()
    assets = _norm_assets(cfg.get("assets"))
    if requested == NO_STYLE:
        return [a for a in assets if not a.get("style")]
    styles = [s for s in (cfg.get("styles") or []) if isinstance(s, dict)]
    target = next((s for s in styles if s.get("name") == requested), None)
    if target is None:
        target = next((s for s in styles if s.get("name") == cfg.get("default_style")),
                      styles[0] if styles else None)
    visible = {str(s.get("name") or "") for s in _style_lineage(styles, target)} if target else set()
    return [a for a in assets if not a.get("style") or a.get("style") in visible]


def save_assets(assets) -> dict:
    cfg = load_config()
    cfg["assets"] = _norm_assets(assets)
    save_config(cfg)
    return load_config()


def generate_asset_image(asset_id: str, style_name: str = "", extra_prompt: str = "") -> dict:
    """Paint a catalogue asset's reference image (same framing rules as a
    per-script one: the space or the garments, never people)."""
    cfg = load_config()
    assets = _norm_assets(cfg.get("assets"))
    asset = next((a for a in assets if a.get("id") == asset_id), None)
    if asset is None:
        raise ValueError(f"Unknown asset {asset_id!r}.")
    if asset["kind"] == "wardrobe":
        subject, framing = (f"{asset['name']}: {asset.get('description') or 'an outfit'}",
                            "the garments alone laid flat on a plain neutral background, "
                            "product photography, empty clothes with nobody wearing them, "
                            "no person, no model, no mannequin, no body parts")
    else:
        subject, framing = (f"{asset['name']}: {asset.get('description') or 'a location'}",
                            "wide establishing shot of the empty space, no people, no text")
    prompt = ", ".join(x for x in (subject, framing, (extra_prompt or "").strip()) if x)
    combined = _compose_visual_style("", cfg, style_name or asset.get("style") or "")
    full = f"{combined}. {prompt}" if combined else prompt
    engine = engines.resolve(cfg, style_settings(cfg, style_name or asset.get("style") or "")
                             .get("image_engine"))
    out = _assets_dir() / f"{asset['id']}.png"
    urls = _preview_worker_urls()
    if not urls:
        raise RuntimeError("No cluster workers reachable to generate this image.")
    pool = WorkerPool(urls)
    url = pool.acquire()
    try:
        generate_with_engine(engine, full, out, width=1024, height=1024, comfy_url=url)
    finally:
        pool.release(url)
    asset["ref_image"] = out.name
    return save_assets(assets)


def set_asset_image(asset_id: str, raw: bytes) -> dict:
    """Store an uploaded photo as a catalogue asset's reference image."""
    from PIL import Image
    cfg = load_config()
    assets = _norm_assets(cfg.get("assets"))
    asset = next((a for a in assets if a.get("id") == asset_id), None)
    if asset is None:
        raise ValueError(f"Unknown asset {asset_id!r}.")
    out = _assets_dir() / f"{asset['id']}.png"
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.convert("RGB").save(out, "PNG")
    except Exception as e:
        raise ValueError(f"Could not read that image: {e}")
    asset["ref_image"] = out.name
    return save_assets(assets)


def _script_visuals_path(work_dir: Path) -> Path:
    return Path(work_dir) / "visuals.json"


def _script_visuals_dir(work_dir: Path) -> Path:
    return Path(work_dir) / "visuals"


def _norm_visuals(raw) -> list[dict]:
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        kind = str(item.get("kind") or "location").strip().lower()
        scenes = []
        for sid in item.get("scenes") or []:
            try:
                scenes.append(int(sid))
            except (TypeError, ValueError):
                continue
        out.append({
            "id": str(item.get("id") or f"vis_{uuid.uuid4().hex[:8]}"),
            "name": name,
            "kind": kind if kind in VISUAL_KINDS else "location",
            "description": str(item.get("description") or "").strip(),
            # Wardrobe belongs to someone; a location belongs to the scene.
            "character": str(item.get("character") or "").strip(),
            "ref_image": str(item.get("ref_image") or "").strip(),
            # Empty = every scene. The common case (one studio for the whole
            # film) then needs no per-scene bookkeeping at all.
            "scenes": scenes,
            "enabled": bool(item.get("enabled", True)),
        })
    return out


def read_script_visuals(work_dir) -> list[dict]:
    try:
        return _norm_visuals(json.loads(_script_visuals_path(Path(work_dir)).read_text()))
    except (OSError, ValueError):
        return []


def write_script_visuals(work_dir, visuals) -> list[dict]:
    norm = _norm_visuals(visuals)
    path = _script_visuals_path(Path(work_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(norm, indent=2))
    return norm


def add_script_visual(work_dir, name: str = "", kind: str = "location",
                      description: str = "", character: str = "") -> list[dict]:
    """Append a visual; blank name gets a placeholder so the row survives
    normalization and shows as an editable card (same rule as characters)."""
    visuals = read_script_visuals(work_dir)
    placeholder = {"location": "New location", "wardrobe": "New outfit",
                   "image": "New reference", "video": "New video reference"}
    visuals.append({"name": name.strip() or placeholder.get(kind, "New reference"),
                    "kind": kind, "description": description, "character": character})
    return write_script_visuals(work_dir, visuals)


def update_script_visual(work_dir, visual_id: str, **fields) -> list[dict]:
    visuals = read_script_visuals(work_dir)
    vis = next((v for v in visuals if v.get("id") == visual_id), None)
    if vis is None:
        raise ValueError(f"Unknown visual {visual_id!r} for this script.")
    for key in ("name", "kind", "description", "character", "scenes", "enabled"):
        if key in fields and fields[key] is not None:
            vis[key] = fields[key]
    return write_script_visuals(work_dir, visuals)


def delete_script_visual(work_dir, visual_id: str) -> list[dict]:
    visuals = read_script_visuals(work_dir)
    vis = next((v for v in visuals if v.get("id") == visual_id), None)
    if vis and vis.get("ref_image"):
        img = _script_visual_image_path(work_dir, vis["ref_image"])
        if img and img.exists():
            img.unlink(missing_ok=True)
    return write_script_visuals(work_dir, [v for v in visuals if v.get("id") != visual_id])


_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".m4v")


def set_script_visual_media(work_dir, visual_id: str, raw: bytes,
                            filename: str = "") -> list[dict]:
    """Store uploaded bytes — image OR video — as a visual's reference.

    A video is kept beside the visual and a representative frame (1s in, where
    fades have usually resolved) is extracted as the reference image, since the
    picture slots feed the model stills."""
    if Path(str(filename or "")).suffix.lower() not in _VIDEO_EXTS:
        return set_script_visual_image(work_dir, visual_id, raw)
    import subprocess

    from pipeline.assembler import _resolve_media_tool
    work_dir = Path(work_dir)
    visuals = read_script_visuals(work_dir)
    vis = next((v for v in visuals if v.get("id") == visual_id), None)
    if vis is None:
        raise ValueError(f"Unknown visual {visual_id!r} for this script.")
    d = _script_visuals_dir(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    src = d / f"{visual_id}{Path(filename).suffix.lower()}"
    src.write_bytes(raw)
    out = d / f"{visual_id}.png"
    ffmpeg = _resolve_media_tool("ffmpeg")
    proc = subprocess.run([ffmpeg, "-y", "-ss", "1", "-i", str(src),
                          "-frames:v", "1", str(out)],
                         capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        # A clip shorter than a second: take its first frame instead.
        proc = subprocess.run([ffmpeg, "-y", "-i", str(src),
                              "-frames:v", "1", str(out)],
                             capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        src.unlink(missing_ok=True)
        raise ValueError("Could not read that video (ffmpeg failed to extract a frame).")
    vis["ref_image"] = out.name
    vis["source_video"] = src.name
    return write_script_visuals(work_dir, visuals)


def fetch_visual_from_url(work_dir, visual_id: str, url: str) -> list[dict]:
    """Pull a visual's reference from a URL: a direct image or video file, or a
    web page — where the page's og:image / og:video is what gets fetched."""
    import re as _re
    import urllib.request

    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Enter an http(s) URL.")

    def _get(u: str, cap: int) -> tuple[bytes, str]:
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (Spielbot)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            data = resp.read(cap + 1)
        if len(data) > cap:
            raise ValueError(f"That file is over the {cap // (1024*1024)} MB limit.")
        return data, ctype

    data, ctype = _get(url, 200 * 1024 * 1024)
    if ctype.startswith("text/html"):
        html = data[:512 * 1024].decode("utf-8", "replace")
        m = (_re.search(r'property=["\']og:video["\'][^>]*content=["\']([^"\']+)', html)
             or _re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:video["\']', html)
             or _re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)', html)
             or _re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', html))
        if not m:
            raise ValueError("No image or video found on that page (no og:image/og:video).")
        url = m.group(1)
        data, ctype = _get(url, 200 * 1024 * 1024)

    name = Path(url.split("?")[0]).name or "reference"
    if ctype.startswith("video/") and Path(name).suffix.lower() not in _VIDEO_EXTS:
        name += ".mp4"
    return set_script_visual_media(work_dir, visual_id, data, filename=name)


def _script_visual_image_path(work_dir, filename: str) -> Path | None:
    name = Path(str(filename or "")).name
    return _script_visuals_dir(Path(work_dir)) / name if name else None


def generate_script_visual_image(work_dir, visual_id: str, style_name: str,
                                 extra_prompt: str = "") -> list[dict]:
    """Render a visual's reference image in the film's own look.

    A location is painted as an empty establishing shot — no people — so it
    conditions the SPACE rather than smuggling in a second cast. Wardrobe is
    painted as the garments themselves for the same reason.
    """
    work_dir = Path(work_dir)
    cfg = load_config()
    visuals = read_script_visuals(work_dir)
    vis = next((v for v in visuals if v.get("id") == visual_id), None)
    if vis is None:
        raise ValueError(f"Unknown visual {visual_id!r} for this script.")

    if vis["kind"] == "wardrobe":
        subject = f"{vis['name']}: {vis.get('description') or 'an outfit'}"
        framing = ("the garments alone laid flat on a plain neutral background, "
                   "product photography, empty clothes with nobody wearing them, "
                   "no person, no model, no mannequin, no body parts")
    else:
        subject = f"{vis['name']}: {vis.get('description') or 'a location'}"
        framing = ("wide establishing shot of the empty space, no people, "
                   "no text, natural lighting")
    parts = [p for p in (subject, framing, (extra_prompt or "").strip()) if p]
    prompt = ", ".join(parts)
    combined = _compose_visual_style("", cfg, style_name)
    full = f"{combined}. {prompt}" if combined else prompt

    engine = engines.resolve(cfg, style_settings(cfg, style_name).get("image_engine"))
    d = _script_visuals_dir(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{vis['id']}.png"
    urls = _preview_worker_urls()
    if not urls:
        raise RuntimeError("No cluster workers reachable to generate this image.")
    pool = WorkerPool(urls)
    url = pool.acquire()
    try:
        generate_with_engine(engine, full, out, width=1024, height=1024, comfy_url=url)
    finally:
        pool.release(url)
    vis["ref_image"] = out.name
    return write_script_visuals(work_dir, visuals)


def set_script_visual_image(work_dir, visual_id: str, raw: bytes) -> list[dict]:
    """Store uploaded bytes as a visual's reference image.

    A real photograph of the actual room or garment beats anything painted —
    generating is the fallback when you do not have one, not the only way in."""
    from PIL import Image
    work_dir = Path(work_dir)
    visuals = read_script_visuals(work_dir)
    vis = next((v for v in visuals if v.get("id") == visual_id), None)
    if vis is None:
        raise ValueError(f"Unknown visual {visual_id!r} for this script.")
    d = _script_visuals_dir(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{visual_id}.png"
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.convert("RGB").save(out, "PNG")
    except Exception as e:
        raise ValueError(f"Could not read that image: {e}")
    vis["ref_image"] = out.name
    return write_script_visuals(work_dir, visuals)


def scene_visuals(work_dir, scene_id: int, cast: list | None = None,
                  cfg: dict | None = None, style_name: str = "") -> list[dict]:
    """The visuals that apply to one scene, in slot order: locations first
    (the space), then wardrobe. A visual with no scene list applies to every
    scene; wardrobe only applies when its character is in the scene.

    The film's own visuals come first and shadow a catalogue asset of the same
    name — same precedence as characters, so a film can override the shared
    studio without editing the catalogue."""
    names = {str(n).strip().lower() for n in (cast or [])}
    own = read_script_visuals(work_dir)
    taken = {v["name"].strip().lower() for v in own}
    catalogue = [
        {**a, "scenes": []}          # catalogue assets are not scene-scoped
        for a in style_assets(cfg if cfg is not None else load_config(), style_name)
        if a["name"].strip().lower() not in taken
    ]
    pool = own + catalogue
    out = []
    for kind in ("location", "image", "video", "wardrobe"):
        for v in pool:
            if not v.get("enabled", True) or v["kind"] != kind:
                continue
            if v["scenes"] and int(scene_id) not in v["scenes"]:
                continue
            if kind == "wardrobe" and v.get("character") and \
                    v["character"].strip().lower() not in names:
                continue
            img = (_script_visual_image_path(work_dir, v.get("ref_image"))
                   if v in own else _asset_image_path(v.get("ref_image")))
            if img and img.exists():
                out.append({**v, "_ref_path": str(img)})
    return out


def _job_characters(cfg: dict, style_name: str, work_dir: Path | None = None) -> list[dict]:
    """The characters a single job can use: the catalogue characters visible
    to the style (global pool + its lineage) plus the script's own
    (per-script) characters. Each entry carries
    a resolved absolute reference-image path under '_ref_path', so callers need
    not know where a character's image is stored. A per-script character shadows
    a catalogue character of the same name (the editor's edits win). Returns fresh
    shallow copies so '_ref_path' never leaks back into persisted config."""
    out: list[dict] = []
    for c in _style_characters(cfg, style_name):
        c = dict(c)
        c["_ref_path"] = _character_image_path(c.get("ref_image"))
        out.append(c)
    if work_dir is not None:
        for c in _read_script_characters(work_dir):
            c = dict(c)
            c["_ref_path"] = _script_character_image_path(work_dir, c.get("ref_image"))
            name = (c.get("name") or "").strip().lower()
            out = [m for m in out if (m.get("name") or "").strip().lower() != name]
            out.append(c)
    return out


def _characters_for_scene(scene_text: str, cfg: dict, style_name: str,
                          work_dir: Path | None = None) -> list[dict]:
    """Enabled characters (with a description) whose name/alias appears in the
    scene text. The single source of truth for both the text injection below and
    Phase 2's reference-image matching. Includes the script's own characters when
    a work_dir is given."""
    chars = _job_characters(cfg, style_name, work_dir)
    return [c for c in chars
            if c.get("enabled", True) and c.get("description")
            and _character_mentions(scene_text, c)]


def _inject_characters(base_prompt: str, scene: dict, cfg: dict, style_name: str,
                       work_dir: Path | None = None) -> str:
    """Append each matched character's canonical appearance to the image prompt so
    the same subject looks consistent across scenes, even if the LLM paraphrased.

    A character matches when its name/alias appears in the scene's image prompt
    (NOT the narration — narration routinely names people who are talked about
    but not on screen). Its description is only appended when not already
    present, so re-generating a scene never stacks duplicate clauses. No match →
    unchanged."""
    scene_text = f"{base_prompt} {scene.get('image_prompt') or ''}"
    clauses = []
    for c in _characters_for_scene(scene_text, cfg, style_name, work_dir):
        desc = c["description"]
        if desc.lower() not in base_prompt.lower():
            clauses.append(f"{c['name']}: {desc}")
    if not clauses:
        return base_prompt
    tail = " ".join(f"{c}." for c in clauses)
    sep = " " if base_prompt.rstrip().endswith((".", "!", "?")) else ". "
    return f"{base_prompt.rstrip()}{sep}{tail}"


def _scene_reference_characters(base_prompt: str, scene: dict, cfg: dict, style_name: str,
                                work_dir: Path | None = None) -> list[tuple[dict, Path]]:
    """(character, image_path) pairs for the characters featured in this scene,
    capped at _MAX_SCENE_REFERENCES. A character contributes its image when it's
    enabled, has a stored ref_image, and its name/alias appears in the scene's
    image prompt — not the narration, which names off-screen people (Phase 2 —
    FLUX.2 reference conditioning). Includes the script's own characters when a
    work_dir is given. Empty list when nothing matches."""
    chars = _job_characters(cfg, style_name, work_dir)
    scene_text = f"{base_prompt} {scene.get('image_prompt') or ''}"
    pairs = []
    for c in chars:
        if not c.get("enabled", True) or not c.get("ref_image"):
            continue
        if not _character_mentions(scene_text, c):
            continue
        p = c.get("_ref_path") or _character_image_path(c.get("ref_image"))
        if p and p.exists():
            pairs.append((c, p))
    if len(pairs) > _MAX_SCENE_REFERENCES:
        logger.info("Scene matched %d character reference images; using first %d.",
                    len(pairs), _MAX_SCENE_REFERENCES)
        pairs = pairs[:_MAX_SCENE_REFERENCES]
    return pairs


def _scene_reference_images(base_prompt: str, scene: dict, cfg: dict, style_name: str,
                            work_dir: Path | None = None) -> list[Path]:
    """Just the image paths of :func:`_scene_reference_characters`."""
    return [p for _c, p in _scene_reference_characters(base_prompt, scene, cfg,
                                                       style_name, work_dir)]


# Binds a character NAME to its attached reference image. Validated on FLUX.2
# Klein (same seed, same reference): without a named binding the text prior —
# famous names especially — routinely overrides the ReferenceLatent and the
# look drifts; with it the render follows the uploaded look. A generic,
# nameless version of this note ("any character depicted…") does NOT work.
_REF_MATCH_NOTE = ("{name} appears EXACTLY as the character in their provided reference "
                   "image — identical face, head, materials and colours; only the pose "
                   "and setting differ.")


def _characters_prompt_and_refs(base_prompt: str, scene: dict, cfg: dict, style_name: str,
                                work_dir: Path | None = None,
                                engine: dict | None = None) -> tuple[str, list[Path]]:
    """Character-consistent prompt + reference images for one scene image render.

    Injects each featured character's canonical appearance (_inject_characters),
    gathers their reference images, and — only when the engine actually
    conditions on references (it has a t2i_ref_workflow, i.e. FLUX.2) — appends
    a per-character _REF_MATCH_NOTE tying the name to its attached image.
    Engines without reference support get the plain injected prompt."""
    prompt = _inject_characters(base_prompt, scene, cfg, style_name, work_dir)
    pairs = _scene_reference_characters(prompt, scene, cfg, style_name, work_dir)
    refs = [p for _c, p in pairs]
    if pairs and engine is not None and engine.get("t2i_ref_workflow"):
        notes = " ".join(_REF_MATCH_NOTE.format(name=c.get("name", "").strip())
                         for c, _p in pairs if (c.get("name") or "").strip())
        if notes:
            sep = " " if prompt.rstrip().endswith((".", "!", "?")) else ". "
            prompt = f"{prompt.rstrip()}{sep}{notes}"
    return prompt, refs


def build_cover_generation(work_dir, cfg: dict, style_name: str, scenes=None,
                           instruction: str = "", extra_style: str = "",
                           text_position: str = "",
                           engine: dict | None = None) -> tuple[str, list]:
    """Cover-background prompt + character reference images for one film.

    The cover must read as a frame from the SAME production as the scene
    stills: the composed per-style visual style leads the prompt
    (_compose_visual_style — the same text the script's image prompts open
    with), and every character named in the subject hint gets the same
    canonical-appearance clause, portrait reference image, and exact-match
    note the scene renders use (_characters_prompt_and_refs). Returns
    ``(prompt, reference_image_paths)`` — pass the refs to
    generate_with_engine (engines without reference conditioning ignore them).
    """
    from pipeline.cover import build_cover_prompt

    style_text = _compose_visual_style(extra_style, cfg, style_name)
    prompt = build_cover_prompt(style_text, scenes=scenes, instruction=instruction,
                                text_position=text_position)
    wd = Path(work_dir) if work_dir else None
    return _characters_prompt_and_refs(prompt, {}, cfg, style_name, wd, engine=engine)


def _find_character(cfg: dict, char_id: str) -> dict:
    """Return the character with the given id from the global library, raising
    ValueError if unknown. The character must already be persisted (ids are
    assigned on save by _norm_characters), so image ops require a saved
    character."""
    char = next((c for c in (cfg.get("characters") or []) if c.get("id") == char_id), None)
    if char is None:
        raise ValueError(f"Unknown character {char_id!r}. Save it first.")
    return char


def set_character_image(char_id: str, raw: bytes) -> dict:
    """Store uploaded image bytes as the character's PNG reference and persist.
    Returns the reloaded config."""
    from PIL import Image
    cfg = load_config()
    char = _find_character(cfg, char_id)
    d = _characters_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{char_id}.png"
    # Keep any current look so uploading a new one doesn't silently discard it.
    image_history.char_seed_if_empty(_global_char_hist_root(), char_id, out)
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.convert("RGB").save(out, "PNG")
    except Exception as e:
        raise ValueError(f"Could not read that image: {e}")
    char["ref_image"] = out.name
    save_config(cfg)
    image_history.char_record(_global_char_hist_root(), char_id, out)
    return load_config()


def select_character_image(char_id: str, version_id: int) -> dict:
    """Make a previously-kept look version the character's current reference image
    (the one FLUX.2 anchors to) and persist. Returns the reloaded config."""
    cfg = load_config()
    char = _find_character(cfg, char_id)
    image_history.char_select(_global_char_hist_root(), char_id, version_id)
    char["ref_image"] = f"{char_id}.png"
    save_config(cfg)
    return load_config()


def delete_character_image_version(char_id: str, version_id: int) -> dict:
    """Delete a kept look version (not the one in use). Returns the reloaded config."""
    cfg = load_config()
    _find_character(cfg, char_id)
    image_history.char_delete(_global_char_hist_root(), char_id, version_id)
    return load_config()


def clear_character_image(char_id: str) -> dict:
    """Delete a character's reference image and clear the field. Returns config."""
    cfg = load_config()
    char = _find_character(cfg, char_id)
    p = _character_image_path(char.get("ref_image"))
    if p and p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    char["ref_image"] = ""
    save_config(cfg)
    return load_config()


def generate_character_portrait(char_id: str, extra_prompt: str = "") -> dict:
    """Generate a portrait from the character's description and lock it in as the
    reference image (re-callable to re-roll). The portrait is anchored to the
    OWNING style's image engine + visual look; a global-pool character falls
    back to the default style's. Needs a worker. Returns the reloaded config."""
    cfg = load_config()
    char = _find_character(cfg, char_id)
    style_name = char.get("style") or cfg.get("default_style") or ""
    parts = [p for p in (char.get("name"), char.get("description"), (extra_prompt or "").strip()) if p]
    prompt = ", ".join(parts) or char.get("name") or "character portrait"
    combined = _compose_visual_style("", cfg, style_name)
    full = f"{combined}. {prompt}" if combined else prompt
    engine = engines.resolve(cfg, style_settings(cfg, style_name).get("image_engine"))
    worker_urls = _preview_worker_urls()
    if not worker_urls:
        raise RuntimeError("No cluster workers reachable to generate a portrait.")
    pool = WorkerPool(worker_urls)
    d = _characters_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{char_id}.png"
    # Keep the current look so re-rolling doesn't silently discard it.
    image_history.char_seed_if_empty(_global_char_hist_root(), char_id, out)
    url = pool.acquire()
    try:
        generate_with_engine(engine, full, out, width=1024, height=1024, comfy_url=url)
    finally:
        pool.release(url)
    char["ref_image"] = out.name
    save_config(cfg)
    image_history.char_record(_global_char_hist_root(), char_id, out)
    return load_config()


def _generate_script_portrait(work_dir: Path, cfg: dict, style_name: str, char: dict,
                              extra_prompt: str = "",
                              worker_pool: "WorkerPool | None" = None) -> Path:
    """Render a look portrait for one per-script character into the work dir and
    set its ref_image (basename). Mutates *char* in place; the caller persists
    characters.json. The style's image engine + visual look anchor the portrait,
    mirroring the global generate_character_portrait."""
    parts = [p for p in (char.get("name"), char.get("description"), (extra_prompt or "").strip()) if p]
    prompt = ", ".join(parts) or char.get("name") or "character portrait"
    combined = _compose_visual_style("", cfg, style_name)
    full = f"{combined}. {prompt}" if combined else prompt
    engine = engines.resolve(cfg, style_settings(cfg, style_name).get("image_engine"))
    d = _script_characters_dir(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{char['id']}.png"
    # Keep any current look so re-rolling doesn't silently discard it.
    image_history.char_seed_if_empty(work_dir, char["id"], out)
    own_pool = worker_pool is None
    if own_pool:
        urls = _preview_worker_urls()
        if not urls:
            raise RuntimeError("No cluster workers reachable to generate a portrait.")
        worker_pool = WorkerPool(urls)
    url = worker_pool.acquire()
    try:
        generate_with_engine(engine, full, out, width=1024, height=1024, comfy_url=url)
    finally:
        worker_pool.release(url)
    char["ref_image"] = out.name
    image_history.char_record(work_dir, char["id"], out)
    return out


def generate_script_character_portrait(work_dir, char_id: str, style_name: str,
                                       extra_prompt: str = "") -> list[dict]:
    """Regenerate one per-script character's look (editor 'Generate look'). Needs
    a worker. Persists characters.json and returns the saved list."""
    work_dir = Path(work_dir)
    cfg = load_config()
    chars = _read_script_characters(work_dir)
    char = next((c for c in chars if c.get("id") == char_id), None)
    if char is None:
        raise ValueError(f"Unknown character {char_id!r} for this script.")
    _generate_script_portrait(work_dir, cfg, style_name, char, extra_prompt)
    return _write_script_characters(work_dir, chars)


def generate_all_script_portraits(work_dir, style_name: str) -> int:
    """Best-effort: render a look for every per-script character that lacks one.
    Runs in a background thread right after script creation, so a missing worker
    or a single failure just leaves that character imageless (the editor offers a
    manual 'Generate look'). Persists after each success. Returns the count made."""
    work_dir = Path(work_dir)
    cfg = load_config()
    chars = _read_script_characters(work_dir)
    todo = [c for c in chars if c.get("description") and not c.get("ref_image")]
    if not todo:
        return 0
    urls = _preview_worker_urls()
    if not urls:
        logger.info("No workers reachable — skipping script portraits for %s", work_dir)
        return 0
    pool = WorkerPool(urls)
    made = 0
    for char in todo:
        try:
            _generate_script_portrait(work_dir, cfg, style_name, char, worker_pool=pool)
            _write_script_characters(work_dir, chars)
            made += 1
        except Exception as exc:
            logger.warning("Script portrait failed for %r: %s", char.get("name"), exc)
    return made


def promote_script_character(work_dir, char_id: str, style_name: str = "") -> dict:
    """Copy a per-script character into the catalogue (with its look image),
    scoped to the job's style — so that style and its children reuse it in
    future videos. Promoting without a style lands it in the global pool. The
    per-script copy stays put — this is a one-way "save to catalogue".
    Returns the reloaded config."""
    work_dir = Path(work_dir)
    chars = _read_script_characters(work_dir)
    src = next((c for c in chars if c.get("id") == char_id), None)
    if src is None:
        raise ValueError(f"Unknown character {char_id!r} for this script.")
    cfg = load_config()
    library = cfg.setdefault("characters", [])
    new_id = f"char_{uuid.uuid4().hex[:8]}"
    entry = {
        "id": new_id,
        "name": src.get("name", ""),
        "aliases": list(src.get("aliases") or []),
        "description": src.get("description", ""),
        "ref_image": "",
        "ref_strength": src.get("ref_strength", 1.0),
        "enabled": True,
        # The "(none)" experiment style owns nothing — such promotes go global.
        "style": "" if (style_name or "").strip() == NO_STYLE else (style_name or "").strip(),
    }
    # Copy the look image into the global characters dir under the new id.
    sp = _script_character_image_path(work_dir, src.get("ref_image"))
    if sp and sp.exists():
        d = _characters_dir()
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"{new_id}.png"
        try:
            shutil.copy2(sp, dest)
            entry["ref_image"] = dest.name
        except OSError as exc:
            logger.warning("Could not copy portrait while promoting %r: %s", entry["name"], exc)
    library.append(entry)
    save_config(cfg)
    return load_config()


def _find_script_character(chars: list[dict], char_id: str) -> dict:
    """The per-script character with *char_id*, raising ValueError if unknown."""
    char = next((c for c in chars if c.get("id") == char_id), None)
    if char is None:
        raise ValueError(f"Unknown character {char_id!r} for this script.")
    return char


def add_script_character(work_dir, name: str = "", aliases=None, description: str = "") -> list[dict]:
    """Append a manually-created character to the script and return the saved list.

    A blank name is replaced with a placeholder so the new row survives
    normalization (_norm_characters drops nameless entries) and shows up as an
    editable card — the editor persists each row on the server immediately, so a
    dropped blank would make "Add character" appear to do nothing."""
    work_dir = Path(work_dir)
    chars = _read_script_characters(work_dir)
    chars.append({"name": name.strip() or "New character",
                  "aliases": list(aliases or []), "description": description})
    return _write_script_characters(work_dir, chars)


def update_script_character(work_dir, char_id: str, *, name=None, aliases=None,
                            description=None, voice=None) -> list[dict]:
    """Patch a per-script character's editable fields; returns the saved list."""
    work_dir = Path(work_dir)
    chars = _read_script_characters(work_dir)
    char = _find_script_character(chars, char_id)
    if name is not None:
        char["name"] = name
    if aliases is not None:
        char["aliases"] = aliases
    if description is not None:
        char["description"] = description
    if voice is not None:
        char["voice"] = str(voice).strip()
    return _write_script_characters(work_dir, chars)


def delete_script_character(work_dir, char_id: str) -> list[dict]:
    """Remove a per-script character (and its look image); returns the saved list."""
    work_dir = Path(work_dir)
    chars = _read_script_characters(work_dir)
    char = next((c for c in chars if c.get("id") == char_id), None)
    p = _script_character_image_path(work_dir, char.get("ref_image")) if char else None
    if p and p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    return _write_script_characters(work_dir, [c for c in chars if c.get("id") != char_id])


def set_script_character_image(work_dir, char_id: str, raw: bytes) -> list[dict]:
    """Store uploaded bytes as a per-script character's look image; returns list."""
    from PIL import Image
    work_dir = Path(work_dir)
    chars = _read_script_characters(work_dir)
    char = _find_script_character(chars, char_id)
    d = _script_characters_dir(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{char_id}.png"
    # Keep any current look so uploading a new one doesn't silently discard it.
    image_history.char_seed_if_empty(work_dir, char_id, out)
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.convert("RGB").save(out, "PNG")
    except Exception as e:
        raise ValueError(f"Could not read that image: {e}")
    char["ref_image"] = out.name
    image_history.char_record(work_dir, char_id, out)
    return _write_script_characters(work_dir, chars)


def select_script_character_image(work_dir, char_id: str, version_id: int) -> list[dict]:
    """Make a kept look version the character's current image (the one woven into
    scene generation) and persist. Returns the saved list."""
    work_dir = Path(work_dir)
    chars = _read_script_characters(work_dir)
    char = _find_script_character(chars, char_id)
    image_history.char_select(work_dir, char_id, version_id)
    char["ref_image"] = f"{char_id}.png"
    return _write_script_characters(work_dir, chars)


def delete_script_character_image_version(work_dir, char_id: str, version_id: int) -> list[dict]:
    """Delete a kept look version (not the one in use). Returns the saved list."""
    work_dir = Path(work_dir)
    chars = _read_script_characters(work_dir)
    _find_script_character(chars, char_id)
    image_history.char_delete(work_dir, char_id, version_id)
    return chars


def clear_script_character_image(work_dir, char_id: str) -> list[dict]:
    """Delete a per-script character's look image and clear the field."""
    work_dir = Path(work_dir)
    chars = _read_script_characters(work_dir)
    char = _find_script_character(chars, char_id)
    p = _script_character_image_path(work_dir, char.get("ref_image"))
    if p and p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    char["ref_image"] = ""
    return _write_script_characters(work_dir, chars)


def _apply_prompt_instruction(prompt: str, instruction: str) -> str:
    """Append a one-off "tell it how" steering instruction to a render prompt.

    Kept transient — it is NOT persisted to the scene's image_prompt, so it only
    steers this single render (e.g. "make it all robots"). Empty → unchanged."""
    instruction = (instruction or "").strip()[:500]
    if not instruction:
        return prompt
    if not (prompt or "").strip():
        return instruction
    return f"{prompt}. {instruction}"


def _image_matches_resolution(path: Path, width: int, height: int) -> bool:
    """True only if *path* is a readable image of exactly width×height pixels."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size == (width, height)
    except Exception:
        return False


def _scene_establishing_frame(work_dir: Path, sid: int, row: dict,
                              width: int, height: int) -> Path | None:
    """The scene's wide establishing frame (the FLUX first frame) at width×height,
    or None. Anchors dialogue close-ups and is the establishing shot's still."""
    cands = []
    pp = (row or {}).get("preview_path") or ""
    if pp:
        cands.append(Path(pp))
    cands += [work_dir / f"scene_{sid:02d}_preview.png",
              work_dir / f"scene_{sid:02d}_first_frame.png"]
    for p in cands:
        if p.exists() and _image_matches_resolution(p, width, height):
            return p
    return None


def _performance_refs(cfg: dict, work_dir: Path, style_name: str = ""):
    """Build (portrait_for, voice_for) for performance scenes (Ref2VA).

    A performance scene conditions on the CHARACTERS, not on a first frame:
    each cast member's portrait becomes a <Picture N> reference and each
    speaker's cast voice becomes an <Audio N> one. Both resolvers take a
    character name and return a path or None — a missing portrait just drops
    that reference (the model then invents the look), and a missing voice lets
    it invent the voice.

    Lives here (not resume_generation) so the backend's per-scene re-render can
    share it — importing resume_generation would clobber its logging config."""
    chars = _job_characters(cfg, style_name or cfg.get("style_name") or "", work_dir)
    voices = {v["name"]: v["path"] for v in (cfg.get("voices") or []) if v.get("name")}

    def _find(name: str):
        key = (name or "").strip().lower()
        if not key:
            return None
        for c in chars:
            names = [c.get("name", ""), *(c.get("aliases") or [])]
            if any(key == str(n).strip().lower() for n in names if str(n).strip()):
                return c
        return None

    def portrait_for(name: str) -> Path | None:
        c = _find(name)
        path = Path(c["_ref_path"]) if c and c.get("_ref_path") else None
        if path and path.exists():
            return path
        logger.info("  performance: no portrait for %r — H3 will invent the look", name)
        return None

    def voice_for(name: str) -> Path | None:
        c = _find(name)
        if c and c.get("voice") and c["voice"] in voices:
            path = Path(voices[c["voice"]])
            if path.exists():
                logger.info("  performance: %s speaks with voice %r", name, c["voice"])
                return path
        logger.info("  performance: no cast voice for %r — H3 will invent the voice", name)
        return None

    def voice_name_for(name: str) -> str:
        c = _find(name)
        return (c or {}).get("voice") or ""

    def looks_like(name: str) -> str:
        """A few words of the character's appearance, for the reference-role line.

        Not a full description — the portrait carries the look. This is the
        HOOK that lets the model bind "<Picture 2>" to the right name: with
        bare names and two same-kind references it swaps them, putting one
        character's face on the other's part."""
        c = _find(name) or {}
        words = re.split(r"[,.]", (c.get("description") or "").strip())
        return (words[0].strip() if words else "")[:80]

    return portrait_for, voice_for, voice_name_for, looks_like


def resolve_performance_references(meta: dict, cfg: dict, work_dir: Path,
                                   style_name: str = "", scene_id: int = 0) -> dict:
    """Which references a performance scene will actually be rendered with.

    Returns ``{"pictures": [...], "audios": [...]}`` where each entry carries its
    one-based ``slot`` — the ``<Picture N>`` / ``<Audio N>`` number cited in the
    prompt — the character it belongs to, and the file on disk. Only references
    that resolved are listed, so the numbering always matches the slots wired
    into the graph.

    The renderer AND the editor's performance view both call this, so what the
    screen shows is what the model receives."""
    from pipeline import performance as _perf

    portrait_for, voice_for, voice_name_for, looks_like = _performance_refs(
        cfg, work_dir, style_name)
    pictures, audios = [], []
    for name in (meta.get("cast") or []):
        if not name:
            continue
        path = portrait_for(name)
        if path is not None:
            pictures.append({"slot": len(pictures) + 1, "name": name,
                             "kind": "character", "hint": looks_like(name),
                             "path": str(path)})
    # The scene's first-frame image, when one was made: it rides as the take's
    # opening-composition reference. Ref2VA has no literal first-frame input,
    # but an image reference demonstrably anchors the space and framing (the
    # shot-splitter's continuity frame proved it).
    frame = None
    for cand in (Path(work_dir) / f"scene_{int(scene_id):02d}_preview.png",
                 Path(work_dir) / f"scene_{int(scene_id):02d}_first_frame.png"):
        if scene_id and cand.exists() and cand.stat().st_size > 0:
            frame = cand
            break
    if frame is not None:
        pictures.append({"slot": len(pictures) + 1, "name": "First frame",
                         "kind": "frame", "path": str(frame)})
    # Locations and wardrobe take the slots after the cast: identity first, then
    # the space, then the clothes — so trimming to H3's nine-image cap drops the
    # least identity-critical references rather than a face. A first frame IS
    # the place, photographed — sending the location asset alongside it wastes
    # a slot and dilutes binding (measured: at 3 picture refs everything held;
    # at 4+ the weakest dropped), so the frame supersedes it.
    for vis in scene_visuals(work_dir, scene_id, meta.get("cast"), cfg, style_name):
        if frame is not None and vis["kind"] == "location":
            continue
        pictures.append({"slot": len(pictures) + 1, "name": vis["name"],
                         "kind": vis["kind"], "character": vis.get("character", ""),
                         "id": vis["id"], "path": vis["_ref_path"]})
    speakers = _perf.speakers_in(_perf.norm_lines(meta.get("lines")))
    for name in speakers[:_perf.MAX_SPEAKERS_PER_SCENE]:
        path = voice_for(name)
        if path is not None:
            audios.append({"slot": len(audios) + 1, "name": name,
                           "voice": voice_name_for(name), "path": str(path)})
    return {"pictures": pictures, "audios": audios}


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
    instruction: str = "",
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
    # Preserve the image we're about to overwrite so the user can return to it.
    image_history.seed_if_empty(work_dir, sid, out)
    # Which model bundle generates this style's scenes (defaults to flux1-schnell).
    engine = engines.resolve(cfg, style_settings(cfg, style_name).get("image_engine"))
    img_width, img_height = _RESOLUTIONS.get(
        resolution or style_settings(cfg, style_name).get("resolution") or _DEFAULT_RESOLUTION,
        (1024, 576),
    )
    # Match the render: snap the preview to LTX's renderable grid so the cached
    # preview is reused as the first frame instead of regenerated at a new size.
    img_width, img_height = ltx_dimensions(img_width, img_height)
    combined_style = _compose_visual_style(style, cfg, style_name)
    md = scene.get("metadata") or {}
    acted_prompt = ""
    if not (image_prompt or scene.get("image_prompt")):
        from pipeline import performance as _perf
        if _perf.is_performance_mode(md.get("mode")):
            cast = [str(n) for n in (md.get("cast") or []) if str(n).strip()]
            who = (f" {' and '.join(cast)} are in the scene, both fully visible."
                   if len(cast) > 1 else f" {cast[0]} is in the scene." if cast else "")
            setting = str(md.get("setting") or "").strip()
            if setting:
                acted_prompt = f"{setting}.{who} The very first moment of the scene, nobody speaking yet."
    base_prompt = image_prompt or scene.get("image_prompt") or acted_prompt or title
    # Re-inject any recurring character's canonical appearance so the same named
    # subject looks consistent across scenes even when the LLM paraphrased it,
    # and anchor featured characters to their reference image (FLUX.2 only).
    # work_dir folds in the script's own (per-script) characters, not just the
    # global catalogue ones the style opted into.
    base_prompt, reference_images = _characters_prompt_and_refs(
        base_prompt, scene, cfg, style_name, work_dir, engine=engine)
    prompt = f"{combined_style}. {base_prompt}" if combined_style else base_prompt
    # One-off user steering from the Re-generate popover (not persisted).
    prompt = _apply_prompt_instruction(prompt, instruction)

    url = worker_pool.acquire()
    try:
        generate_with_engine(
            engine,
            prompt,
            out,
            width=img_width,
            height=img_height,
            reference_images=reference_images,
            comfy_url=url,
        )
        store = DurableStore.default()
        try:
            store.update_scene_preview(job_id, sid, out)
        finally:
            store.close()
        image_history.record(work_dir, sid, out)
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
            # story.json without script.json = a story-first draft awaiting
            # scene division — listed so it can be reopened and divided later.
            if (d / "script.json").exists() or (d / "story.json").exists():
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
    dedup check runs against the channel the new video would actually join. A
    style with no channel of its own returns nothing — idea dedup must NOT
    inherit an unrelated channel's back-catalog via the publish fallback."""
    channel = _style_channel_explicit(cfg, style_name)
    if not channel:
        return []
    secrets = cfg.get("youtube_client_secrets", "")
    titles: list[str] = []
    try:
        titles = yt.cached_channel_video_titles(
            secrets, max_results=500, channel=channel)
    except Exception as exc:
        logger.warning("cached_channel_video_titles error: %s", exc)
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






def _auto_pick_styles(cfg: dict) -> list[str]:
    """Style names eligible for auto-picked queue top-ups, in config order.

    A style opts out via ``auto_pick_exclude`` (Settings → style), so automation
    only invents ideas for the styles left in the rotation. Manual idea
    generation ignores this flag — it governs the automatic top-up only.
    Resolved via style_settings so a child style inherits its parent's opt-out."""
    return [s["name"] for s in (cfg.get("styles") or [])
            if isinstance(s, dict) and str(s.get("name") or "").strip()
            and not style_settings(cfg, s["name"]).get("auto_pick_exclude")]


def _auto_pick_style_of(idea: dict, default_name: str) -> str:
    """Which style an idea belongs to — legacy ideas (no stamp) are the default's."""
    return str(idea.get("style_name") or "") or default_name


def _last_auto_picked_style() -> str:
    """Style of the most recently auto-picked queue item (by created_at), or ''.
    Lets the next pick rotate onto a different style so top-ups mix styles."""
    try:
        picks = [q for q in yt.load_queue()
                 if q.get("source") == "suggestion" and q.get("gen_style_name")]
    except Exception:
        return ""
    if not picks:
        return ""
    return str(max(picks, key=lambda q: q.get("created_at", 0)).get("gen_style_name") or "")


def _generate_mixed_suggestions(cfg: dict, style_names: list[str],
                                discarded: list[str] | None = None) -> list[dict]:
    """Generate a fresh batch of ideas for each style and interleave them, so the
    saved pool alternates styles (A#1, B#1, … then A#2, B#2, …). ``discarded``
    topics are passed to the LLM so automation never re-suggests a thrown-away
    idea."""
    batches = []
    # Titles generated so far this run, keyed by channel, so sibling styles on
    # the same channel dedup against each other's fresh batch.
    fresh_by_channel: dict[str, list[str]] = {}
    for name in style_names:
        ss = style_settings(cfg, name)
        ch = _dedup_scope(cfg, name)
        existing_titles = _channel_video_titles(cfg, style_name=name) + fresh_by_channel.get(ch, [])
        try:
            new_data = generate_video_suggestions(existing_titles, cfg, style=ss,
                                                  discarded_titles=discarded)
        except Exception as exc:
            logger.warning("_generate_mixed_suggestions: generation failed for %r: %s", name, exc)
            new_data = []
        fresh_by_channel.setdefault(ch, []).extend(
            str(s.get("title") or "") for s in new_data if s.get("title"))
        batches.append([
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
        ])
    merged = []
    for i in range(max((len(b) for b in batches), default=0)):
        for b in batches:
            if i < len(b):
                merged.append(b[i])
    return merged


def _rotate_pick(unused: list[dict], eligible: list[str], last_style: str,
                 default_name: str) -> dict | None:
    """Pick the next idea so consecutive auto-picks rotate through the eligible
    styles. Starts at the style after the last-picked one and walks the rotation
    until it finds a style with an unused idea; falls back to the first unused."""
    if not unused:
        return None
    by_style: dict[str, list[dict]] = {}
    for s in unused:
        by_style.setdefault(_auto_pick_style_of(s, default_name), []).append(s)
    start = (eligible.index(last_style) + 1) % len(eligible) if last_style in eligible else 0
    for k in range(len(eligible)):
        name = eligible[(start + k) % len(eligible)]
        if by_style.get(name):
            return by_style[name][0]
    return unused[0]


def _auto_pick_suggestion(cfg: dict, discarded: list[str] | None = None) -> dict | None:
    """Pick an unused suggestion and add it to the queue, rotating through the
    eligible styles so successive top-ups mix styles instead of always using the
    default one (issue #117).

    Generates a fresh mixed batch across the eligible styles when none is
    waiting (steering the LLM away from ``discarded`` topics). Returns the new
    pending queue item dict, or None on failure. Called only when there are no
    pending user requests and auto-start is enabled.
    """
    eligible = _auto_pick_styles(cfg)
    if not eligible:
        logger.info("_auto_pick_suggestion: every style is excluded from auto-pick — nothing to do")
        return None
    default_name = cfg.get("default_style", "")

    suggestions = yt.load_suggestions()
    unused = [s for s in suggestions
              if not s.get("used") and _auto_pick_style_of(s, default_name) in eligible]

    if not unused:
        # No eligible idea waiting — invent a fresh mixed batch across the styles
        # the user left in the rotation.
        logger.info("No unused suggestions for eligible styles — generating a mixed batch")
        merged = _generate_mixed_suggestions(cfg, eligible, discarded=discarded)
        if not merged:
            logger.warning("_auto_pick_suggestion: LLM suggestion generation failed")
            return None
        # Keep any other still-unused ideas (e.g. from the AI ideas screen); drop
        # used ones so the pool doesn't grow without bound.
        kept = [s for s in suggestions if not s.get("used")]
        yt.save_suggestions(kept + merged)
        unused = list(merged)  # eligible by construction

    suggestion = _rotate_pick(unused, eligible, _last_auto_picked_style(), default_name)
    if not suggestion:
        return None
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
        # 0 → the render falls back to the item's style n_scenes (the short
        # default), so auto-suggested videos match the rest instead of forcing 20.
        "suggested_scene_count": 0,
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






