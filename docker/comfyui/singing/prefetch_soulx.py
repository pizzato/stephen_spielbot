"""Fetch only the pinned SVC, RMVPE, and Whisper assets into the SoulX install."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

SVC_REVISION = "40493ad90286056c7a9095035164434a79daa8c9"
PREPROCESS_REVISION = "83dc50289d22a81b1e9998f5b9e111aef7c1fdcd"
WHISPER_REVISION = "e37978b90ca9030d5170a5c07aadb050351a65bb"
WHISPER_FILES = ("config.json", "preprocessor_config.json", "model.safetensors")


def prefetch(root: Path) -> None:
    cache = root / "hf-cache" / "hub"
    download_root = Path(os.environ.get("SOULX_DOWNLOAD_CACHE", str(root)))
    download_cache = download_root / "hf-cache" / "hub"
    # Both libraries capture their default cache directories during import.
    # Keep CLI credentials available when selecting SoulX's isolated cache.
    default_hf_home = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "huggingface"
    token_home = Path(os.environ.get("HF_HOME", str(default_hf_home)))
    os.environ.setdefault("HF_TOKEN_PATH", str(token_home / "token"))
    os.environ["HF_HOME"] = str(root / "hf-cache")
    os.environ["HF_HUB_CACHE"] = str(cache)
    # Xet can leave range requests struggling for minutes on worker networks.
    # The Hub's HTTP downloader resumes partial files and uses the same token.
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    from huggingface_hub import get_token, hf_hub_download, snapshot_download

    token = get_token()
    auth = "token available" if token else "anonymous"
    print(f"SoulX downloads: resumable HTTP; Hugging Face authentication: {auth}", flush=True)

    assets = (
        ("Soul-AILab/SoulX-Singer", SVC_REVISION, "model-svc.pt"),
        ("Soul-AILab/SoulX-Singer-Preprocess", PREPROCESS_REVISION, "rmvpe/rmvpe.pt"),
    )
    manifest = {repo: revision for repo, revision, _ in assets}
    manifest["openai/whisper-base"] = WHISPER_REVISION
    for repo, revision, filename in assets:
        print(f"Prefetching {repo}/{filename} at {revision}", flush=True)
        checkpoint = Path(hf_hub_download(
            repo_id=repo,
            revision=revision,
            filename=filename,
            local_dir=download_root / "pretrained_models" / repo.split("/")[1],
            cache_dir=download_cache,
            token=token,
        ))
        if download_root != root:
            target = root / "pretrained_models" / repo.split("/")[1] / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(checkpoint, target)
        print(f"Ready: {repo}/{filename} ({checkpoint.stat().st_size:,} bytes)", flush=True)
    print(f"Prefetching openai/whisper-base at {WHISPER_REVISION}", flush=True)
    snapshot = Path(snapshot_download(
        repo_id="openai/whisper-base",
        revision=WHISPER_REVISION,
        allow_patterns=list(WHISPER_FILES),
        cache_dir=download_cache,
        token=token,
    ))
    for name in WHISPER_FILES:
        if not (snapshot / name).is_file():
            raise RuntimeError(f"Whisper prefetch incomplete: {name}")
    if download_root != root:
        target = cache / "models--openai--whisper-base" / "snapshots" / WHISPER_REVISION
        target.mkdir(parents=True, exist_ok=True)
        for name in WHISPER_FILES:
            shutil.copy2(snapshot / name, target / name)
    # Upstream calls from_pretrained by repository name. Pin its local main
    # reference to our verified snapshot so offline inference is reproducible.
    ref = cache / "models--openai--whisper-base" / "refs" / "main"
    ref.parent.mkdir(parents=True, exist_ok=True)
    temporary = ref.with_suffix(".tmp")
    temporary.write_text(WHISPER_REVISION)
    temporary.replace(ref)
    from transformers import WhisperFeatureExtractor, WhisperModel

    WhisperFeatureExtractor.from_pretrained("openai/whisper-base", cache_dir=cache, local_files_only=True)
    WhisperModel.from_pretrained("openai/whisper-base", cache_dir=cache, local_files_only=True)
    (root / "spielbot-models.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    prefetch(parser.parse_args().root.resolve())
