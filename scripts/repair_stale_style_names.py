"""Repair job_config.json style names left over from the style-hierarchy
refactor (PR #218, merged 2026-07-28).

Before that refactor, a child style's full name was the flat compound string
"{parent}-{child}" (e.g. "BHOB-David Attenbot"), stamped into job_config.json
at render time. The refactor split styles into a bare child name plus a
separate "parent" field, but never migrated already-rendered jobs. A job
stamped with the old compound name no longer matches any style in the current
config, so app.style_settings() silently substitutes whatever "default_style"
happens to be at the moment something looks the job up — including at publish
time, which is how a Brief-History-of-Botkind film was uploaded to the wrong
YouTube channel after sitting unpublished across the refactor.

For each style with a "parent" in the current config, this looks for job dirs
whose job_config.json style_name equals the old "{parent}-{child}" compound
and rewrites it to the bare child name. Jobs already using a current style
name, or a compound that doesn't match any current style, are left untouched
(the latter are printed for manual review).

Usage: .venv/bin/python scripts/repair_stale_style_names.py [--dry-run] [WORK_DIR ...]
       (no WORK_DIR given → scans every job under ~/videos)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as gapp  # noqa: E402


def repair(wd: Path, compound_map: dict[str, str], current_names: set[str], dry_run: bool) -> str:
    jc_path = wd / "job_config.json"
    if not jc_path.exists():
        return "no job_config.json"
    jc = json.loads(jc_path.read_text())
    style_name = jc.get("style_name", "")
    if not style_name or style_name in current_names:
        return "ok"
    new_name = compound_map.get(style_name)
    if new_name is None:
        return f"UNRECOGNIZED style_name {style_name!r} — needs manual review"
    if dry_run:
        return f"would rewrite style_name {style_name!r} -> {new_name!r}"
    jc["style_name"] = new_name
    jc_path.write_text(json.dumps(jc, indent=2))
    return f"rewrote style_name {style_name!r} -> {new_name!r}"


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    dirs = [Path(a) for a in argv if a != "--dry-run"]
    if not dirs:
        dirs = [p for p in gapp.OUTPUT_DIR.iterdir()
                if p.is_dir() and (p / "job_config.json").exists()]

    cfg = gapp.load_config()
    styles = [s for s in (cfg.get("styles") or []) if isinstance(s, dict)]
    current_names = {s["name"] for s in styles if s.get("name")}
    compound_map = {f"{s['parent']}-{s['name']}": s["name"]
                    for s in styles if s.get("parent") and s.get("name")}

    for wd in sorted(dirs):
        if not wd.is_dir():
            print(f"{wd}: not a directory")
            continue
        result = repair(wd, compound_map, current_names, dry_run)
        if result != "ok":
            print(f"{wd.name}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
