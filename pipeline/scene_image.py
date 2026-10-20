"""Known character provenance for scene images, not recognition of their pixels.

Generated frames record the characters requested through their references.
Older ComfyUI PNGs can recover the same information from attached portrait
filenames. Unknown uploaded images make no identity claim.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, PngImagePlugin

_CAST_KEY = "spielbot_scene_cast"


def record_cast(image: Path, characters: list[dict]) -> None:
    """Record requested identities, preserving pixels and existing PNG text.

    An empty list is meaningful: it prevents legacy graph metadata from adding
    a character that the current generation did not request.
    """
    image = Path(image)
    identities = [
        {"id": str(c.get("id") or "").strip(), "name": str(c["name"]).strip()}
        for c in characters if isinstance(c, dict) and str(c.get("name") or "").strip()
    ]
    try:
        with Image.open(image) as im:
            if im.format != "PNG":
                return
            pixels = im.copy()
            metadata = PngImagePlugin.PngInfo()
            for key, value in im.text.items():
                if key != _CAST_KEY:
                    metadata.add_text(key, value)
            settings = {k: im.info[k] for k in
                        ("dpi", "transparency", "icc_profile", "exif") if k in im.info}
    except (OSError, ValueError, SyntaxError):
        return
    metadata.add_text(_CAST_KEY, json.dumps(identities))
    pixels.save(image, format="PNG", pnginfo=metadata, **settings)


def read_cast(image: Path, characters: list[dict]) -> list[str]:
    """Resolve recorded references to current known names, including old PNGs.

    This establishes who was requested, not whether the model drew them. A
    reference in the style prose alone is never evidence of an identity.
    """
    try:
        with Image.open(image) as im:
            if im.format != "PNG":
                return []
            info = dict(im.info)
    except (OSError, ValueError, SyntaxError):
        return []
    known = [c for c in characters
             if isinstance(c, dict) and str(c.get("name") or "").strip()]
    matches = []
    try:
        if _CAST_KEY in info:
            entries = json.loads(info[_CAST_KEY])
            if not isinstance(entries, list):
                return []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                cid = str(entry.get("id") or "").strip()
                name = str(entry.get("name") or "").strip().casefold()
                matches.extend(
                    c for c in known
                    if (str(c.get("id") or "").strip() == cid if cid else
                        str(c["name"]).strip().casefold() == name))
        else:
            graph = json.loads(info.get("prompt") or "{}")
            if not isinstance(graph, dict):
                return []
            refs = {
                Path(node["inputs"]["image"]).name
                for node in graph.values()
                if isinstance(node, dict) and node.get("class_type") == "LoadImage"
                and isinstance(node.get("inputs"), dict)
                and isinstance(node["inputs"].get("image"), str)
            }
            matches = [
                c for c in known
                if any(value and Path(str(value)).name in refs
                       for value in (c.get("ref_image"), c.get("_ref_path")))
            ]
    except (TypeError, ValueError):
        return []
    return list(dict.fromkeys(str(c["name"]).strip() for c in matches))
