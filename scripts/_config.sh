#!/usr/bin/env bash
# Shared helpers: read worker lists from the single YAML config.
#
# config.yaml is the only configuration file (no cluster.conf / ui_workers.conf).
# These helpers print one item per line for use in `for host in $(...)` loops.

CONFIG_YAML="${CONFIG_YAML:-$HOME/.config/video-generator/config.yaml}"

# Resolve the project venv python (PyYAML lives there), falling back to python3.
_cfg_python() {
    local repo_root
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    if [[ -x "$repo_root/.venv/bin/python" ]]; then
        echo "$repo_root/.venv/bin/python"
    else
        echo "python3"
    fi
}

# Print a config list key, one item per line (empty if missing/unreadable).
_cfg_list() {
    local key="$1"
    "$(_cfg_python)" - "$CONFIG_YAML" "$key" <<'PY'
import sys, yaml
path, key = sys.argv[1], sys.argv[2]
try:
    data = yaml.safe_load(open(path).read()) or {}
except Exception:
    sys.exit(0)
for item in (data.get(key) or []):
    print(item)
PY
}

# ComfyUI/render hosts for worker bootstrap — bare hostnames derived from
# comfy_workers URLs (http://HOST:PORT -> HOST). May include "localhost" for a
# single-machine setup (managed without SSH — see is_local_host).
remote_hosts() {
    _cfg_list comfy_workers | sed -E 's#^https?://##; s#[:/].*$##' | awk 'NF' | sort -u
}

# True when a worker host is this machine — its containers are managed with
# plain local commands instead of SSH.
is_local_host() {
    [[ "$1" == "localhost" || "$1" == "127.0.0.1" ]]
}

# Fallback ComfyUI endpoint for the controller-side cover agent. The dedicated
# UI worker was removed (issue #98): covers now route per-task to a render worker
# the backend keeps idle while the UI is in use, so this is only the agent's
# default endpoint. First comfy worker, or localhost for a single-machine setup.
cover_endpoint() {
    local first
    first="$(_cfg_list comfy_workers | awk 'NF' | head -n1)"
    echo "${first:-http://localhost:8188}"
}
