#!/usr/bin/env bash
# Verify a worker's ComfyUI registers every node class the workflows need
# (docker/comfyui/required_nodes.txt), by reading its /object_info.
#
# Run after a worker is healthy: the HTTP health check passes on an image that
# is missing custom nodes, so without this a stale worker looks fine until a
# render fails with "the node 'X' is not installed on this worker".
#
# Usage: bash scripts/check_worker_nodes.sh <host> [port]
# Exit:  0 = all present   1 = one or more missing   2 = worker unreachable
set -uo pipefail

HOST="${1:-}"
PORT="${2:-8188}"
if [[ -z "$HOST" ]]; then
    echo "Usage: $0 <host> [port]"
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="python3"

"$PYTHON" - "$HOST" "$PORT" "$REPO_ROOT/docker/comfyui/required_nodes.txt" <<'PY'
import json
import sys
import urllib.request

host, port, list_path = sys.argv[1], sys.argv[2], sys.argv[3]

required = []  # (node_class, pack)
with open(list_path) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        node, _, pack = line.partition("#")
        required.append((node.strip(), pack.strip()))

try:
    with urllib.request.urlopen(f"http://{host}:{port}/object_info", timeout=60) as r:
        available = set(json.loads(r.read()))
except Exception as e:
    print(f"    ? nodes    could not read {host}:{port}/object_info ({e})")
    sys.exit(2)

missing = [(node, pack) for node, pack in required if node not in available]
if not missing:
    print(f"    ✓ nodes    all {len(required)} required nodes present")
    sys.exit(0)

print(f"    ✗ nodes    {len(missing)} of {len(required)} MISSING on {host}:")
for node, pack in missing:
    print(f"                 {node}" + (f"  ({pack})" if pack else ""))
sys.exit(1)
PY
