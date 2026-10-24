"""Assemble MiniMax song conditioning without rewriting the chosen vocalist."""
from __future__ import annotations

import re


_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*)?(Global Metadata|Vocal Details|Arrangement)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def sections(caption: str) -> dict[str, str]:
    headings = list(_HEADING.finditer(caption))
    parts: dict[str, str] = {}
    for i, heading in enumerate(headings):
        name = heading.group(1).lower()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(caption)
        body = caption[heading.end():end].strip()
        parts[name] = "\n\n".join(x for x in (parts.get(name), body) if x)
    if headings and caption[:headings[0].start()].strip():
        parts["global metadata"] = (caption[:headings[0].start()].strip()
                                    + "\n\n" + parts.get("global metadata", ""))
    return parts


def is_structured(caption: str) -> bool:
    parts = sections(caption)
    return bool(parts.get("global metadata") and parts.get("arrangement"))


def song_caption(caption: str, vocalist: str, engine: str | None) -> str:
    """Keep ACE tags intact; put the authoritative voice inside MiniMax's caption."""
    if engine != "minimax-music3" or not is_structured(caption):
        return ", ".join(x for x in (caption.strip(), vocalist.strip()) if x)
    parts = sections(caption)
    vocal = vocalist.strip() or parts.get("vocal details", "")
    return (f"Global Metadata:\n{parts['global metadata']}\n\n"
            + (f"Vocal Details:\n{vocal}\n\n" if vocal else "")
            + f"Arrangement:\n{parts['arrangement']}")
