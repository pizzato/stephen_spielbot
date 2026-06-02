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

# ComfyUI/render hosts for SSH bootstrap — bare hostnames derived from
# comfy_workers URLs (http://HOST:PORT -> HOST).
remote_hosts() {
    _cfg_list comfy_workers | sed -E 's#^https?://##; s#[:/].*$##' | awk 'NF' | sort -u
}

# UI worker ComfyUI endpoints (full URLs).
ui_endpoints() {
    _cfg_list ui_workers
}

# UI ComfyUI hosts — bare hostnames derived from ui_workers URLs.
# Used for SSH-based install/start/stop, same interface as remote_hosts().
ui_hosts() {
    _cfg_list ui_workers | sed -E 's#^https?://##; s#[:/].*$##' | awk 'NF' | sort -u
}
