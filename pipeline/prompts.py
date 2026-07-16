"""Load and render prompt templates from prompts.yaml.

Single source of truth for every LLM and image-generation prompt. Two layers:

* the packaged ``prompts.yaml`` next to this package is the BASELINE. It ships
  with the app, is tracked in git, and is never written to — so upgrades keep
  improving any prompt the user has not touched.
* ``~/.config/video-generator/prompts.yaml`` holds the user's edits (Settings →
  Infrastructure → prompt editor). It is SPARSE: only fields that differ from
  the baseline are stored, so "revert to the original" is just dropping them.

Reads merge the two per field, with the override winning. The merged result is
cached; call :func:`reload` after writing overrides.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from string import Template
from typing import Any

logger = logging.getLogger("video_gen")

_PROMPTS_PATH = Path(__file__).resolve().parent.parent / "prompts.yaml"
_OVERRIDE_PATH = Path.home() / ".config" / "video-generator" / "prompts.yaml"

# The fields a user may override. `description` documents what the prompt is for
# rather than being sent to any model, so it stays as shipped.
EDITABLE_FIELDS = ("system", "user", "value")

_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}")

_cache: dict[str, Any] | None = None
_defaults_cache: dict[str, Any] | None = None


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to load prompts.yaml. Install with: pip install pyyaml"
        ) from exc
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must be a mapping at the top level, got {type(data).__name__}")
    return data


def defaults() -> dict[str, Any]:
    """The packaged baseline, exactly as shipped. Never reflects user edits."""
    global _defaults_cache
    if _defaults_cache is not None:
        return _defaults_cache
    if not _PROMPTS_PATH.exists():
        raise RuntimeError(f"prompts.yaml not found at {_PROMPTS_PATH}")
    _defaults_cache = _read_yaml(_PROMPTS_PATH)
    return _defaults_cache


def overrides() -> dict[str, Any]:
    """The user's sparse edits. Unreadable or absent → no overrides, because a
    broken override file must not take generation down with it."""
    if not _OVERRIDE_PATH.exists():
        return {}
    try:
        return _read_yaml(_OVERRIDE_PATH)
    except Exception:
        logger.exception("Ignoring unreadable prompt overrides at %s", _OVERRIDE_PATH)
        return {}


def _load() -> dict[str, Any]:
    """Baseline merged with the overrides, per field."""
    global _cache
    if _cache is not None:
        return _cache
    merged = {name: dict(entry) if isinstance(entry, dict) else entry
              for name, entry in defaults().items()}
    for name, entry in overrides().items():
        # A prompt the baseline no longer defines is a leftover from an older
        # version — ignore it rather than resurrecting a dead prompt.
        if name not in merged or not isinstance(entry, dict) or not isinstance(merged[name], dict):
            continue
        for field in EDITABLE_FIELDS:
            if isinstance(entry.get(field), str):
                merged[name][field] = entry[field]
    _cache = merged
    return merged


def reload() -> None:
    """Drop the in-memory cache so the next call re-reads both files from disk."""
    global _cache, _defaults_cache
    _cache = None
    _defaults_cache = None


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


# ── editing (Settings → prompt editor) ───────────────────────────────────────

def placeholders(text: str) -> list[str]:
    """The ${...} placeholder names in *text*, in first-seen order."""
    seen: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(text or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def catalogue() -> list[dict[str, Any]]:
    """Every prompt with its current text, for the editor. Each field carries the
    baseline's placeholder names so the UI can warn when an edit drops one."""
    base, over = defaults(), overrides()
    out = []
    for name, entry in base.items():
        if not isinstance(entry, dict):
            continue
        ov = over.get(name) if isinstance(over.get(name), dict) else {}
        fields = []
        for key in EDITABLE_FIELDS:
            if not isinstance(entry.get(key), str):
                continue
            edited = ov.get(key) if isinstance(ov.get(key), str) else None
            fields.append({
                "key": key,
                "value": entry[key] if edited is None else edited,
                "modified": edited is not None,
                "placeholders": placeholders(entry[key]),
            })
        out.append({
            "name": name,
            "description": (entry.get("description") or "").strip(),
            "modified": any(f["modified"] for f in fields),
            "fields": fields,
        })
    return out


def _write_overrides(data: dict[str, Any]) -> None:
    """Persist the sparse override file, or remove it once nothing is overridden."""
    import yaml

    # Block scalars keep the multi-line prompts readable for anyone who opens the
    # file by hand. Scoped to a local Dumper so other yaml.safe_dump callers
    # (app.py's config writer) keep their formatting.
    class _BlockDumper(yaml.SafeDumper):
        pass

    def _str_presenter(dumper, value):
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _BlockDumper.add_representer(str, _str_presenter)

    if not data:
        _OVERRIDE_PATH.unlink(missing_ok=True)
        reload()
        return
    _OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Prompt overrides — edits made in Settings → Infrastructure → prompt editor.\n"
        "# Only fields that differ from the app's built-in prompts.yaml are stored;\n"
        "# everything else falls back to the built-in text, so upgrades still apply.\n"
        "# Deleting a field here reverts that prompt to the original.\n\n"
    )
    body = yaml.dump(data, Dumper=_BlockDumper, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=10_000)
    _OVERRIDE_PATH.write_text(header + body, encoding="utf-8")
    reload()


def save(name: str, fields: dict[str, str]) -> None:
    """Override *fields* of the named prompt. A field equal to the baseline (or
    absent from *fields*) is dropped, so saving the original text reverts it."""
    base = defaults()
    if name not in base or not isinstance(base[name], dict):
        raise KeyError(f"prompt {name!r} not defined in prompts.yaml")
    data = overrides()
    entry: dict[str, str] = {}
    for key in EDITABLE_FIELDS:
        if not isinstance(base[name].get(key), str) or not isinstance(fields.get(key), str):
            continue
        if fields[key] != base[name][key]:
            entry[key] = fields[key]
    if entry:
        data[name] = entry
    else:
        data.pop(name, None)
    _write_overrides(data)


def reset(name: str | None = None) -> None:
    """Drop the override for *name*, or every override when *name* is None."""
    if name is None:
        _write_overrides({})
        return
    data = overrides()
    data.pop(name, None)
    _write_overrides(data)
