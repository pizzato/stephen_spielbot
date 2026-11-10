#!/usr/bin/env bash
# Print credentials only into a pipe to the worker, never into command arguments.
hf_token() {
    local token="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"
    if [[ -n "$token" ]]; then
        printf '%s' "$token"
        return
    fi
    local token_home="${HF_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/huggingface}"
    local token_path="${HF_TOKEN_PATH:-$token_home/token}"
    if [[ -r "$token_path" ]]; then
        cat -- "$token_path"
    fi
}
