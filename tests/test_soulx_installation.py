"""The prefetched SVC installation must be complete and usable offline."""
import builtins
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("svc_exit", [0, 17])
def test_make_install_includes_controller_revoicing_and_propagates_failure(tmp_path, svc_exit):
    # Run the public entry point on an existing install, with downloads and
    # package managers stubbed so this cannot change the real controller.
    root = Path(__file__).parents[1]
    (tmp_path / "scripts").mkdir()
    shutil.copy2(root / "Makefile", tmp_path / "Makefile")
    shutil.copy2(root / "scripts/install.sh", tmp_path / "scripts/install.sh")
    (tmp_path / "config.yaml").write_text("comfy_workers: []\n")
    (tmp_path / "scripts/_config.sh").write_text(
        'CONFIG_YAML="$REPO_ROOT/config.yaml"\nremote_hosts() { :; }\n')
    (tmp_path / "scripts/install_svc.sh").write_text(
        f'touch "$(dirname "$0")/../svc-ran"\nexit {svc_exit}\n')
    for name in (".venv/bin/python", ".venv/bin/pip", "bin/npm"):
        command = tmp_path / name
        command.parent.mkdir(parents=True, exist_ok=True)
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o755)
    (tmp_path / "webapp/frontend").mkdir(parents=True)

    result = subprocess.run(
        ["make", "install"], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
             "INSTALL_SERVICE": "0"},
    )

    assert (tmp_path / "svc-ran").exists(), result.stdout + result.stderr
    assert (result.returncode == 0) == (svc_exit == 0), result.stdout + result.stderr
    assert ("Setting up web UI" in result.stdout) == (svc_exit == 0)


def test_dependency_pin_matches_the_installers_validated_transformers_version():
    root = Path(__file__).parents[1] / "docker/comfyui/singing"
    script = (root / "install_soulx.sh").read_text()
    version = re.search(r'transformers\.__version__ == "([^"]+)"', script).group(1)
    assert f"transformers=={version}" in (root / "requirements-soulx.txt").read_text().splitlines()


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
        get_token=lambda: None, hf_hub_download=download, snapshot_download=snapshot,
    ))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        WhisperFeatureExtractor=feature, WhisperModel=model,
    ))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "previous-cache"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "previous-cache/hub"))
    monkeypatch.setenv("HF_TOKEN_PATH", "")
    monkeypatch.delenv("HF_TOKEN_PATH")
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "0")
    monkeypatch.delenv("SOULX_DOWNLOAD_CACHE", raising=False)
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
            assert os.environ["HF_TOKEN_PATH"] == str(tmp_path / "previous-cache/token")
            assert os.environ["HF_HUB_DISABLE_XET"] == "1"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    installer.prefetch(tmp_path)


def test_prefetch_forwards_token_without_logging_it(monkeypatch, tmp_path, installer, capsys):
    calls, _, _ = setup_downloads(monkeypatch, tmp_path, installer)
    monkeypatch.setattr(sys.modules["huggingface_hub"], "get_token", lambda: "test-secret")
    monkeypatch.setenv("HF_TOKEN_PATH", "/run/secrets/hf_token")

    installer.prefetch(tmp_path)

    assert all(call["token"] == "test-secret" for call in calls)
    assert os.environ["HF_TOKEN_PATH"] == "/run/secrets/hf_token"
    output = capsys.readouterr().out
    assert "authentication: token available" in output
    assert "test-secret" not in output


def test_build_cache_survives_and_models_are_copied_for_offline_use(monkeypatch, tmp_path, installer):
    root = tmp_path / "install"
    calls, feature, model = setup_downloads(monkeypatch, root, installer)
    download_root = tmp_path / "build-cache"
    monkeypatch.setenv("SOULX_DOWNLOAD_CACHE", str(download_root))

    installer.prefetch(root)

    assert all(call["cache_dir"] == download_root / "hf-cache/hub" for call in calls)
    assert (root / "pretrained_models/SoulX-Singer/model-svc.pt").read_bytes() == b"checkpoint"
    assert (root / "pretrained_models/SoulX-Singer-Preprocess/rmvpe/rmvpe.pt").read_bytes() == b"checkpoint"
    whisper = root / "hf-cache/hub/models--openai--whisper-base"
    for name in installer.WHISPER_FILES:
        installed = whisper / "snapshots" / installer.WHISPER_REVISION / name
        assert installed.read_bytes() == b"model"
        assert not installed.is_symlink()
    assert (download_root / "pretrained_models/SoulX-Singer/model-svc.pt").exists()
    for verifier in (feature, model):
        verifier.from_pretrained.assert_called_once_with(
            "openai/whisper-base", cache_dir=root / "hf-cache/hub", local_files_only=True,
        )


@pytest.mark.parametrize("token_source", ["HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_TOKEN_PATH", "HF_HOME", "XDG_CACHE_HOME"])
def test_controller_token_resolution(tmp_path, token_source):
    root = Path(__file__).parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HF_", "HUGGING_FACE_", "XDG_CACHE_HOME"))}
    token_file = tmp_path / "huggingface/token"
    token_file.parent.mkdir()
    token_file.write_text("saved-secret\n")
    env.update({
        "HF_TOKEN": "env-secret", "HUGGING_FACE_HUB_TOKEN": "legacy-secret",
        "HF_TOKEN_PATH": str(token_file), "HF_HOME": str(token_file.parent),
        "XDG_CACHE_HOME": str(tmp_path),
    })
    order = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_TOKEN_PATH", "HF_HOME", "XDG_CACHE_HOME")
    for key in order[:order.index(token_source)]:
        env.pop(key)

    result = subprocess.run(
        ["bash", "-c", 'source "$1/scripts/_hf_token.sh"; hf_token', "_", str(root)],
        env=env, capture_output=True, text=True, check=True,
    )

    expected = {"HF_TOKEN": "env-secret", "HUGGING_FACE_HUB_TOKEN": "legacy-secret"}.get(token_source, "saved-secret")
    assert result.stdout.strip() == expected


def test_controller_without_credentials_can_build_anonymously(tmp_path):
    root = Path(__file__).parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HF_", "HUGGING_FACE_"))}
    env["HF_TOKEN_PATH"] = str(tmp_path / "absent-token")
    result = subprocess.run(
        ["bash", "-c", 'set -euo pipefail; source "$1/scripts/_hf_token.sh"; hf_token', "_", str(root)],
        env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize("exit_code", [0, 19])
def test_build_token_is_temporary_and_never_in_command_arguments(tmp_path, exit_code):
    root = Path(__file__).parents[1]
    source = (root / "scripts/install_worker_container.sh").read_text()
    # Run the real build invocation with only the Docker process replaced.
    invocation = source.split('hf_token | _sh "set -e\n', 1)[1].split('\n\n# ── 6.', 1)[0]
    invocation = 'hf_token | _sh "set -e\n' + invocation
    stack = tmp_path / "docker"
    stack.mkdir()
    env = {**os.environ, "HF_TOKEN": "test-secret", "RESULT_DIR": str(tmp_path), "TEST_EXIT": str(exit_code)}
    result = subprocess.run(
        ["bash", "-s", "--", str(root), str(tmp_path)], env=env,
        input='''set -euo pipefail
source "$1/scripts/_hf_token.sh"
REMOTE_BUILD_DIR="$2"
_sh() { bash -c "$*"; }
docker() {
    test "$(cat "$HF_TOKEN_FILE")" = test-secret
    test "$(python3 -c 'import os, stat; print(stat.S_IMODE(os.stat(os.environ["HF_TOKEN_FILE"]).st_mode))')" = 384
    printf '%s' "$HF_TOKEN_FILE" > "$RESULT_DIR/token-path"
    printf '%s\\n' "$@" > "$RESULT_DIR/arguments"
    return "$TEST_EXIT"
}
export -f docker
''' + invocation.replace('cd ~/$REMOTE_BUILD_DIR/docker', 'cd $REMOTE_BUILD_DIR/docker'),
        capture_output=True, text=True,
    )

    assert result.returncode == exit_code, result.stdout + result.stderr
    assert not Path((tmp_path / "token-path").read_text()).exists()
    assert "test-secret" not in (tmp_path / "arguments").read_text()
    assert "test-secret" not in result.stdout + result.stderr


def test_worker_installer_reads_token_before_starting_subprocesses(tmp_path):
    root = Path(__file__).parents[1]
    interpreter = tmp_path / "python"
    interpreter.write_text(
        f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
        f"Path({str(tmp_path / 'received')!r}).write_text(os.environ.get('HF_TOKEN', ''))\n"
        "sys.exit(17)\n"
    )
    interpreter.chmod(0o755)
    result = subprocess.run(
        ["bash", str(root / "docker/comfyui/singing/install_soulx.sh"), str(tmp_path / "install"),
         str(interpreter), "--system-site-packages", "--token-stdin"],
        input="test-secret\n", capture_output=True, text=True,
    )

    assert result.returncode == 17
    assert (tmp_path / "received").read_text() == "test-secret"
    assert "test-secret" not in result.stdout + result.stderr
