"""The prefetched SVC installation must be complete and usable offline."""
import builtins
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def installer():
    path = Path(__file__).parents[1] / "docker/comfyui/singing/prefetch_soulx.py"
    spec = importlib.util.spec_from_file_location("prefetch_soulx", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_downloads(monkeypatch, tmp_path, installer, missing=None):
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        path = kwargs["local_dir"] / kwargs["filename"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"checkpoint")
        return str(path)

    def snapshot(**kwargs):
        calls.append(kwargs)
        path = kwargs["cache_dir"] / "models--openai--whisper-base" / "snapshots" / kwargs["revision"]
        path.mkdir(parents=True, exist_ok=True)
        for name in kwargs["allow_patterns"]:
            if name != missing:
                (path / name).write_bytes(b"model")
        return str(path)

    feature = Mock()
    model = Mock()
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        hf_hub_download=download, snapshot_download=snapshot,
    ))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        WhisperFeatureExtractor=feature, WhisperModel=model,
    ))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "previous-cache"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "previous-cache/hub"))
    return calls, feature, model


def test_prefetch_pins_only_needed_models_and_verifies_offline(monkeypatch, tmp_path, installer):
    calls, feature, model = setup_downloads(monkeypatch, tmp_path, installer)
    installer.prefetch(tmp_path)

    assert [(x["repo_id"], x.get("filename")) for x in calls] == [
        ("Soul-AILab/SoulX-Singer", "model-svc.pt"),
        ("Soul-AILab/SoulX-Singer-Preprocess", "rmvpe/rmvpe.pt"),
        ("openai/whisper-base", None),
    ]
    assert [x["revision"] for x in calls] == [
        installer.SVC_REVISION, installer.PREPROCESS_REVISION, installer.WHISPER_REVISION,
    ]
    assert all(len(x["revision"]) == 40 for x in calls)
    assert calls[2]["allow_patterns"] == ["config.json", "preprocessor_config.json", "model.safetensors"]
    cache = tmp_path / "hf-cache/hub"
    assert (cache / "models--openai--whisper-base/refs/main").read_text() == installer.WHISPER_REVISION
    for verifier in (feature, model):
        verifier.from_pretrained.assert_called_once_with(
            "openai/whisper-base", cache_dir=cache, local_files_only=True,
        )
    assert json.loads((tmp_path / "spielbot-models.json").read_text())["openai/whisper-base"] == installer.WHISPER_REVISION


def test_incomplete_whisper_snapshot_cannot_mark_install_ready(monkeypatch, tmp_path, installer):
    setup_downloads(monkeypatch, tmp_path, installer, missing="model.safetensors")
    with pytest.raises(RuntimeError, match="Whisper prefetch incomplete"):
        installer.prefetch(tmp_path)
    assert not (tmp_path / "spielbot-models.json").exists()


def test_offline_load_failure_cannot_mark_install_ready(monkeypatch, tmp_path, installer):
    _, _, model = setup_downloads(monkeypatch, tmp_path, installer)
    model.from_pretrained.side_effect = OSError("invalid cached model")
    with pytest.raises(OSError, match="invalid cached model"):
        installer.prefetch(tmp_path)
    assert not (tmp_path / "spielbot-models.json").exists()


def test_cache_is_selected_before_hub_import(monkeypatch, tmp_path, installer):
    setup_downloads(monkeypatch, tmp_path, installer)
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "huggingface_hub":
            assert os.environ["HF_HOME"] == str(tmp_path / "hf-cache")
            assert os.environ["HF_HUB_CACHE"] == str(tmp_path / "hf-cache/hub")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    installer.prefetch(tmp_path)
