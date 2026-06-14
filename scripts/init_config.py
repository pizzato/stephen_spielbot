#!/usr/bin/env python3
"""Seed ~/.config/video-generator/config.yaml on first install.

Never clobbers an existing config. Worker hostnames come from argv (space- or
comma-separated); comfy/tts worker lists are derived from them. With no hosts,
writes a single-machine (localhost) config. Everything else is filled in at
runtime from app.DEFAULT_CFG, so only the worker lists are seeded here.
"""
import sys
from pathlib import Path

import yaml

CONFIG = Path.home() / ".config" / "video-generator" / "config.yaml"


def main(argv: list[str]) -> int:
    if CONFIG.exists():
        print(f"[config] {CONFIG} already exists — leaving it untouched")
        return 0

    # Accept "s1 s2", "s1,s2", or repeated args.
    hosts: list[str] = []
    for token in argv:
        hosts.extend(h.strip() for h in token.replace(",", " ").split())
    hosts = [h for h in hosts if h]

    comfy = [f"http://{h}:8188" for h in hosts]
    tts = list(hosts)

    cfg = {"comfy_workers": comfy, "tts_workers": tts}
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))

    print(f"[config] wrote {CONFIG}")
    print(f"  comfy_workers: {comfy or '(none — single machine, local ComfyUI)'}")
    print(f"  tts_workers:   {tts or '(none)'}")
    print("  Edit these any time in the Settings screen, or in the file above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
