#!/usr/bin/env python3
"""Render `channels.yaml` into the two places the channel list is shown.

`channels.yaml` is the single source of truth — contributors add their channel
there in a pull request and touch nothing else. This script fans it out to:

  * webapp/frontend/src/channels.json — bundled into the About screen at build
  * README.md — the list between the CHANNELS:START/END markers

Run it with `make channels`. The `Sync channels list` GitHub Action runs it on
every push to main that changes channels.yaml and opens a follow-up pull request
with the result, so the generated files never drift from the source.

Output is deterministic (source order, stable key order) so re-running with no
source change produces no diff.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "channels.yaml"
JSON_OUT = ROOT / "webapp" / "frontend" / "src" / "channels.json"
README = ROOT / "README.md"

START = "<!-- CHANNELS:START -->"
END = "<!-- CHANNELS:END -->"

# Only these reach the frontend, in this order, so the JSON diff stays readable.
FIELDS = ("name", "platform", "url", "handle", "note")
PLATFORMS = {"youtube": "YouTube", "x": "X", "other": ""}


def load(source: Path = SOURCE) -> list[dict]:
    """Parse and validate channels.yaml, returning normalised entries.

    Raises ValueError on anything malformed so a bad pull request fails CI with
    a message naming the offending entry rather than shipping a broken list.
    """
    data = yaml.safe_load(source.read_text()) or {}
    raw = data.get("channels") or []
    if not isinstance(raw, list):
        raise ValueError("channels.yaml: 'channels' must be a list")

    out = []
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"channels.yaml: entry {i} must be a mapping")
        clean = {k: str(entry[k]).strip() for k in FIELDS if entry.get(k) is not None}
        for required in ("name", "url"):
            if not clean.get(required):
                raise ValueError(f"channels.yaml: entry {i} is missing '{required}'")
        if not clean["url"].startswith(("http://", "https://")):
            raise ValueError(f"channels.yaml: entry {i} url must start with http(s)://")
        platform = clean.get("platform", "other").lower()
        if platform not in PLATFORMS:
            raise ValueError(
                f"channels.yaml: entry {i} platform '{platform}' must be one of "
                f"{', '.join(PLATFORMS)}"
            )
        clean["platform"] = platform
        out.append(clean)
    return out


def render_markdown(channels: list[dict]) -> str:
    """The README list body (between the markers)."""
    if not channels:
        return "_No channels listed yet — add yours!_"
    lines = []
    for c in channels:
        # "Name (@handle) — YouTube · note", dropping whichever parts are absent.
        label = c["name"]
        if c.get("handle"):
            label += f" ({c['handle']})"
        tail = " · ".join(x for x in (PLATFORMS[c["platform"]], c.get("note", "")) if x)
        lines.append(f"- [{label}]({c['url']})" + (f" — {tail}" if tail else ""))
    return "\n".join(lines)


def write_json(channels: list[dict], path: Path = JSON_OUT) -> bool:
    """Write the frontend bundle. Returns True if the file changed."""
    body = json.dumps(channels, indent=2, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text() == body:
        return False
    path.write_text(body)
    return True


def write_readme(channels: list[dict], path: Path = README) -> bool:
    """Replace the marked block in the README. Returns True if it changed."""
    text = path.read_text()
    if START not in text or END not in text:
        raise ValueError(f"{path.name}: missing {START} / {END} markers")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    updated = f"{head}{START}\n{render_markdown(channels)}\n{END}{tail}"
    if updated == text:
        return False
    path.write_text(updated)
    return True


def main() -> int:
    try:
        channels = load()
    except (ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    changed = [
        name
        for name, did in (("channels.json", write_json(channels)), ("README.md", write_readme(channels)))
        if did
    ]
    n = len(channels)
    print(f"{n} channel{'s' if n != 1 else ''} · " + (f"updated {', '.join(changed)}" if changed else "already up to date"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
