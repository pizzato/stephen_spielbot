#!/usr/bin/env python3
"""Download the bundled character voice library (run by `make install`).

Ten public-domain voice references carved from LibriVox recordings (archive.org),
each ~18 s — enough for F5-TTS zero-shot cloning — with speaker metadata
(gender, age, accent) so story characters can be auto-assigned a fitting voice
(see app._auto_assign_character_voices). Public-domain audio, so cloning and
commercial use are unrestricted; sources are recorded per voice for transparency.

Idempotent: existing clips are kept, missing ones fetched (only ~1.5 MB of each
source chapter is downloaded — the clip is carved from the first minutes).
Registration into config.yaml preserves any user edits (matched by name).

Usage: .venv/bin/python scripts/download_voices.py [--config ~/.config/video-generator/config.yaml]
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import yaml

# name, gender, age, accent, notes, archive.org item, file, carve offset (s)
# All items are solo LibriVox recordings — the whole file is that one reader.
VOICE_PACK = [
    ("Meredith",  "female", "adult",   "American",   "warm, even",
     "pride_and_prejudice_librivox", "prideandprejudice_04-05_austen_64kb.mp3", 75),
    ("Sterling",  "male",   "adult",   "American",   "clear, bright",
     "moby_dick_librivox", "mobydick_001_002_melville_64kb.mp3", 75),
    ("Josephine", "female", "adult",   "American",   "calm, literary",
     "jane_eyre_ver03_0809_librivox", "janeeyre_02_bronte_64kb.mp3", 75),
    ("Walter",    "male",   "mature",  "American",   "deep, gravelly",
     "studyinscarlet_bn_librivox", "studyinscarlet_02_doyle_64kb.mp3", 75),
    ("Beatrice",  "female", "mature",  "British",    "precise, refined",
     "chimes_rg_librivox", "chimes_2_dickens_64kb.mp3", 75),
    ("Nigel",     "male",   "adult",   "British",    "measured, classic",
     "adventuressherlockholmes_v4_1501_librivox", "adventuresofsherlockholmes_02_doyle_64kb.mp3", 75),
    ("Declan",    "male",   "adult",   "Irish",      "lilting, friendly",
     "woodlanders_th_librivox", "thewoodlanders_02_thomashardy_64kb.mp3", 75),
    ("Dorothea",  "female", "elderly", "British",    "characterful, theatrical",
     "curiosity_shop_mn_librivox", "oldcuriosityshop_01_dickens_64kb.mp3", 75),
    ("Bruce",     "male",   "adult",   "Australian", "dry, conversational",
     "literary_taste_0903_librivox", "literarytaste_02_bennett_64kb.mp3", 75),
    ("Kara",      "female", "young",   "American",   "light, youthful",
     "secret_garden_librivox", "secretgarden_02_burnett_64kb.mp3", 75),
]

CLIP_SECONDS = 18
# 64 kbps CBR mp3 ≈ 8 KB/s; offset+clip ≈ 95 s ≈ 760 KB — fetch a safe margin.
FETCH_BYTES = 1_600_000
LICENSE = "Public Domain (LibriVox)"


def _ffmpeg() -> str:
    import shutil
    for c in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if c and Path(c).exists():
            return str(c)
    return "ffmpeg"


def fetch_clip(item: str, filename: str, offset: int, out_wav: Path) -> bool:
    url = f"https://archive.org/download/{item}/{filename}"
    try:
        req = urllib.request.Request(url, headers={"Range": f"bytes=0-{FETCH_BYTES}",
                                                   "User-Agent": "stephen-spielbot-voices/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            head = resp.read(FETCH_BYTES)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ download failed: {url} — {e}")
        return False
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(head)
        tmp_path = Path(tmp.name)
    try:
        r = subprocess.run(
            [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
             "-ss", str(offset), "-t", str(CLIP_SECONDS), "-i", str(tmp_path),
             "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(out_wav)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size < 100_000:
            print(f"  ✗ carve failed for {filename}: {r.stderr[-200:]}")
            out_wav.unlink(missing_ok=True)
            return False
        return True
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path.home() / ".config" / "video-generator" / "config.yaml"))
    args = ap.parse_args()
    cfg_path = Path(args.config)
    lib_dir = cfg_path.parent / "voices" / "library"
    lib_dir.mkdir(parents=True, exist_ok=True)

    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    voices = cfg.get("voices") or []
    by_name = {v.get("name"): v for v in voices if isinstance(v, dict)}

    added = kept = failed = 0
    for name, gender, age, accent, tone, item, filename, offset in VOICE_PACK:
        wav = lib_dir / f"{name.lower()}.wav"
        if not wav.exists():
            print(f"[voices] fetching {name} ({gender}, {age}, {accent}) …")
            if not fetch_clip(item, filename, offset, wav):
                failed += 1
                continue
        meta = {
            "name": name, "path": str(wav),
            "gender": gender, "age": age, "accent": accent, "tone": tone,
            "library": True, "license": LICENSE,
            "source": f"https://archive.org/details/{item}",
        }
        if name in by_name:
            # refresh path/metadata but keep the entry (user may have re-recorded it)
            by_name[name].update({k: v for k, v in meta.items() if k != "name"})
            kept += 1
        else:
            voices.append(meta)
            added += 1

    cfg["voices"] = voices
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    print(f"[voices] library ready: {added} added, {kept} updated, {failed} failed "
          f"({len(VOICE_PACK)} total) → {lib_dir}")
    return 1 if failed == len(VOICE_PACK) else 0


if __name__ == "__main__":
    raise SystemExit(main())
