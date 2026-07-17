"""TTS-engine registry: selectable narration models (mirrors pipeline/engines.py).

A TTS *engine* is a narration model the user can pick per style as
``tts_engine`` (falls back to the Apache-2.0 default). All engines do
zero-shot voice cloning from a reference WAV, so the voice library carries
across. Each engine names a ``backend`` — the inference stack that runs it:

- ``f5``          — the F5-TTS CLI; switching engines is a checkpoint swap.
- ``chatterbox``  — Resemble AI's Chatterbox (``python -m pipeline.chatterbox``),
  which adds multilingual narration (issue #176).

Engines:

- ``openf5``      — OpenF5-TTS-Base, Apache-2.0, commercial OK (the default).
- ``f5-original`` — the original SWivid ``F5TTS_v1_Base``; generally higher
  quality but CC-BY-NC (non-commercial), so flagged "preview" in the UI. Useful
  to A/B against ``openf5``. See NOTICE.md for the licensing rationale.
- ``chatterbox-multilingual`` — Chatterbox Multilingual, MIT, 23 languages;
  the per-style ``tts_language`` picks which one it speaks.
"""
from __future__ import annotations

from pipeline.chatterbox import CHATTERBOX_FILES, CHATTERBOX_REPO, LANGUAGES
from pipeline.openf5 import OPENF5_REPO, ensure_openf5_model

TTS_ENGINES: dict[str, dict] = {
    "openf5": {
        "key": "openf5",
        "label": "OpenF5-TTS-Base",
        "sub": "Apache-2.0 · voice cloning · commercial OK",
        "license": "Apache-2.0",
        "commercial_ok": True,
        # HuggingFace download spec (repo + files), for pre-warming workers.
        "repo": OPENF5_REPO,
        "files": ["config.yaml", "model.pt", "vocab.txt"],
    },
    "f5-original": {
        "key": "f5-original",
        "label": "F5-TTS Base (original)",
        "sub": "CC-BY-NC · voice cloning · non-commercial",
        "license": "CC-BY-NC-4.0",
        "commercial_ok": False,
        # The F5-TTS CLI's built-in model name: weights auto-download from
        # SWivid/F5-TTS and the vocab ships with the f5-tts package.
        "model_name": "F5TTS_v1_Base",
        "repo": "SWivid/F5-TTS",
        "files": ["F5TTS_v1_Base/model_1250000.safetensors"],
    },
    "chatterbox-multilingual": {
        "key": "chatterbox-multilingual",
        "label": "Chatterbox Multilingual",
        "sub": "MIT · 23 languages · voice cloning · commercial OK",
        "license": "MIT",
        "commercial_ok": True,
        "backend": "chatterbox",
        # Language code -> name; drives the per-style narration-language picker.
        "languages": LANGUAGES,
        "repo": CHATTERBOX_REPO,
        "files": CHATTERBOX_FILES,
    },
}

# Default for new styles and the normalize/resolve fallback. MUST stay
# commercial-safe — the output narrates monetized channels (see NOTICE.md).
DEFAULT_TTS_ENGINE = "openf5"


def get(key: str) -> dict | None:
    return TTS_ENGINES.get((key or "").strip())


def norm(key: str) -> str:
    """Coerce an engine key to a known one, falling back to the default."""
    return key if get(key) else DEFAULT_TTS_ENGINE


def backend(key: str) -> str:
    """Which inference stack runs *key*: "f5" (the default) or "chatterbox"."""
    e = get(key) or TTS_ENGINES[DEFAULT_TTS_ENGINE]
    return e.get("backend", "f5")


def public_list() -> list[dict]:
    """Compact descriptors for the Settings UI (no download internals)."""
    return [
        {"key": e["key"], "label": e["label"], "sub": e["sub"],
         "license": e["license"], "commercial_ok": e["commercial_ok"],
         "languages": e.get("languages", {})}
        for e in TTS_ENGINES.values()
    ]


def cli_args(key: str) -> list[str]:
    """F5-TTS CLI flags selecting *key*'s weights (f5-backend engines only —
    callers branch on backend() first; chatterbox runs its own CLI).

    Drop-in replacement for the old hardcoded ``["--model", "F5TTS_v1_Base"]``.
    For ``openf5`` this resolves (and caches) the OpenF5 config/checkpoint/vocab
    and passes them explicitly; for CLI-named models it just names the model and
    lets F5-TTS download it.
    """
    e = get(key) or TTS_ENGINES[DEFAULT_TTS_ENGINE]
    if e.get("backend", "f5") != "f5":
        raise ValueError(f"cli_args() is F5-only; engine {e['key']!r} uses backend {e['backend']!r}")
    if e["key"] == "openf5":
        cfg, ckpt, vocab = ensure_openf5_model()
        return ["--model_cfg", cfg, "--ckpt_file", ckpt, "--vocab_file", vocab]
    return ["--model", e["model_name"]]


def ensure(key: str) -> None:
    """Download *key*'s weights into the HuggingFace cache (idempotent).

    Used to pre-warm a worker so the first render doesn't wait on a multi-GB
    fetch. No-op for an unknown key.
    """
    e = get(key)
    if not e:
        return
    if e["key"] == "openf5":
        ensure_openf5_model()
        return
    from huggingface_hub import hf_hub_download
    for f in e["files"]:
        hf_hub_download(e["repo"], f)


def is_cached(key: str) -> bool:
    """True if every file for *key* is already in the local HF cache."""
    e = get(key)
    if not e:
        return False
    from huggingface_hub import try_to_load_from_cache
    for f in e["files"]:
        if not isinstance(try_to_load_from_cache(e["repo"], f), str):
            return False
    return True
