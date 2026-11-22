#!/usr/bin/env bash
# Print credentials only into a pipe to the worker, never into command arguments.
hf_token() {
    local token="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"
    if [[ -n "$token" ]]; then
        printf '%s' "$token"
        return
    fi
    # Settings and make install share one config. Environment overrides win;
    # otherwise prefer the saved application token to HF CLI credentials.
    local config_path="${CONFIG_YAML:-$HOME/.config/video-generator/config.yaml}"
    if [[ -r "$config_path" ]]; then
        local token_repo token_python
        token_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
        token_python="$token_repo/.venv/bin/python"
        [[ -x "$token_python" ]] || token_python="python3"
        token="$("$token_python" - "$config_path" <<'PY'
import sys
try:
    import yaml
except ImportError:
    sys.exit(0)
try:
    with open(sys.argv[1]) as source:
        config = yaml.safe_load(source) or {}
    token = config.get("hf_token", "") if isinstance(config, dict) else ""
    if isinstance(token, str):
        print(token.strip(), end="")
except (OSError, ValueError, yaml.YAMLError):
    pass
PY
        )"
        if [[ -n "$token" ]]; then
            printf '%s' "$token"
            return
        fi
    fi
    local token_home="${HF_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/huggingface}"
    local token_path="${HF_TOKEN_PATH:-$token_home/token}"
    if [[ -r "$token_path" ]]; then
        cat -- "$token_path"
    fi
}
