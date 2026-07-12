"""Load and render prompt templates from prompts.yaml.

Single source of truth for every LLM and image-generation prompt. Edit
prompts.yaml to change wording; the cached loader picks up changes on next
process start (no code edits required).
"""
from __future__ import annotations

import logging
from pathlib import Path
from string import Template
from typing import Any

logger = logging.getLogger("video_gen")

_PROMPTS_PATH = Path(__file__).resolve().parent.parent / "prompts.yaml"
_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to load prompts.yaml. Install with: pip install pyyaml"
        ) from exc
    if not _PROMPTS_PATH.exists():
        raise RuntimeError(f"prompts.yaml not found at {_PROMPTS_PATH}")
    with _PROMPTS_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"prompts.yaml must be a mapping at the top level, got {type(data).__name__}")
    _cache = data
    return data


def reload() -> None:
    """Drop the in-memory cache so the next call re-reads prompts.yaml from disk."""
    global _cache
    _cache = None


def _entry(name: str) -> dict[str, Any]:
    data = _load()
    if name not in data:
        raise KeyError(f"prompt {name!r} not defined in prompts.yaml")
    entry = data[name]
    if not isinstance(entry, dict):
        raise RuntimeError(f"prompt {name!r} must be a mapping (got {type(entry).__name__})")
    return entry


def _render(text: str, **vars: Any) -> str:
    """Substitute ${name} placeholders. Unknown placeholders are left intact."""
    return Template(text).safe_substitute(**{k: ("" if v is None else str(v)) for k, v in vars.items()})


def system(name: str, **vars: Any) -> str:
    """Return the rendered `system` text for the named prompt with ${...} substitutions."""
    return _render(_entry(name).get("system", ""), **vars).rstrip("\n")


def user(name: str, **vars: Any) -> str:
    """Return the rendered `user` text for the named prompt with ${...} substitutions."""
    return _render(_entry(name).get("user", ""), **vars).rstrip("\n")


def value(name: str) -> str:
    """Return the `value` field for static prompts (negatives, etc.)."""
    v = _entry(name).get("value", "")
    return v.strip() if isinstance(v, str) else ""


def render(name: str, **vars: Any) -> dict[str, str]:
    """Return both system + user rendered, as a {'system': ..., 'user': ...} dict."""
    return {"system": system(name), "user": user(name, **vars)}
