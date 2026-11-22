"""Installer credentials must match Settings and survive remote handoff."""
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def credential_env(tmp_path, config_token="config-secret"):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HF_", "HUGGING_FACE_"))}
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"hf_token": config_token}))
    cli = tmp_path / "cli-token"
    cli.write_text("cli-secret\n")
    env.update(CONFIG_YAML=str(cfg), HF_TOKEN_PATH=str(cli))
    return env


@pytest.mark.parametrize("override,expected", [
    ({}, "config-secret"), ({"HF_TOKEN": "env-secret"}, "env-secret"),
    ({"HUGGING_FACE_HUB_TOKEN": "legacy-secret"}, "legacy-secret"),
])
def test_config_precedes_cli_credentials_but_environment_wins(tmp_path, override, expected):
    result = subprocess.run(
        ["bash", "-c", 'set -euo pipefail; source "$1/scripts/_hf_token.sh"; hf_token', "_", str(ROOT)],
        env={**credential_env(tmp_path), **override}, capture_output=True, text=True, check=True)
    assert result.stdout == expected
    assert result.stderr == ""


@pytest.mark.parametrize("contents", ["hf_token: ''", "hf_token: 12", "hf_token: [invalid", "[]"])
def test_absent_or_invalid_saved_token_falls_back_to_cli(tmp_path, contents):
    env = credential_env(tmp_path)
    Path(env["CONFIG_YAML"]).write_text(contents)
    result = subprocess.run(
        ["bash", "-c", 'set -euo pipefail; source "$1/scripts/_hf_token.sh"; hf_token', "_", str(ROOT)],
        env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "cli-secret"
    assert result.stderr == ""


@pytest.mark.parametrize("saved", [True, False])
def test_existing_config_or_cli_token_skips_interactive_prompt(tmp_path, saved):
    env = credential_env(tmp_path, "config-secret" if saved else "")
    source = (ROOT / "scripts/install.sh").read_text()
    prompt = source[source.index("_ask_hf_token() {"):source.index("# Workers mount models")]
    code = 'source "$1/scripts/_hf_token.sh"; HF_TOKEN="$(hf_token)";\n' + prompt + '\n_ask_hf_token'
    master, slave = pty.openpty()
    try:
        result = subprocess.run(["bash", "-c", code, "_", str(ROOT)], env=env,
                                stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=5, check=True)
    finally:
        os.close(slave)
        os.close(master)
    assert result.stdout == result.stderr == ""


def test_remote_download_uses_stdin_for_token_and_retains_install_flags(tmp_path):
    env = credential_env(tmp_path, "fake-token'\";$(touch should-not-execute)")
    env["RESULT_DIR"] = str(tmp_path)
    source = (ROOT / "scripts/install.sh").read_text()
    invocation = source[source.index("    printf -v _download_env"):source.index("\nfi\n\n# ── 3b.")]
    remote = tmp_path / "remote-download.sh"
    remote.write_text(f'''"{sys.executable}" - <<'PY'
import json, os
from pathlib import Path
Path(os.environ['RESULT_DIR'], 'received.json').write_text(json.dumps({{
 'token': os.environ['HF_TOKEN'], 'flag': os.environ['INSTALL_PIH3']}}))
PY
''')
    invocation = invocation.replace("/tmp/spielbot_download_models.sh", str(remote))
    code = '''set -euo pipefail
source "$1/scripts/_hf_token.sh"
HF_TOKEN="$(hf_token)"
MODEL_SOURCE=test-worker
INSTALL_PIH3=1
ssh() {
    printf '%s\n' "$@" > "$RESULT_DIR/ssh-args"
    bash -c "${@: -1}"
}
''' + invocation
    result = subprocess.run(["bash", "-c", code, "_", str(ROOT)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, check=True)
    received = json.loads((tmp_path / "received.json").read_text())
    assert received == {"token": yaml.safe_load(Path(env["CONFIG_YAML"]).read_text())["hf_token"], "flag": "1"}
    assert received["token"] not in (tmp_path / "ssh-args").read_text()
    assert received["token"] not in result.stdout + result.stderr
    assert not (tmp_path / "should-not-execute").exists()


def test_direct_model_download_reads_config_and_uses_cli_environment(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("download_models.sh", "_hf_token.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    bin_dir = tmp_path / ".venv/bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    hf = bin_dir / "hf"
    hf.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
dest = Path(sys.argv[sys.argv.index('--local-dir') + 1]) / sys.argv[3]
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text('test model')
Path(os.environ['HF_TEST_LOG']).write_text(json.dumps({'args': sys.argv, 'token': os.environ.get('HF_TOKEN')}))
''')
    hf.chmod(0o755)
    models = tmp_path / "comfy"
    models.mkdir()
    env = credential_env(tmp_path)
    log = tmp_path / "hf.json"
    env.update(ENGINE_MODELS="Test/model|weights.safetensors|models/diffusion_models", HF_TEST_LOG=str(log))
    result = subprocess.run(["bash", str(scripts / "download_models.sh"), str(models)],
                            env=env, capture_output=True, text=True, check=True)
    call = json.loads(log.read_text())
    assert call["token"] == "config-secret"
    assert "--token" not in call["args"]
    assert "config-secret" not in result.stdout + result.stderr
    assert (models / "models/diffusion_models/weights.safetensors").exists()
