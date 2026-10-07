#!/usr/bin/env bash
# Shared by Docker builds, controller installation, and worker repair.
# Usage: install_soulx.sh DEST PYTHON [--system-site-packages]
set -euo pipefail
DEST="${1:?pass the SoulX destination}"
PY="${2:?pass a Python 3.10-3.12 interpreter}"
MODE="${3:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REF=81aeb3ae772c70093c3de74dc23c92d983801ae4

"$PY" -c 'import sys; assert (3, 10) <= sys.version_info[:2] <= (3, 12), "SoulX requires Python 3.10-3.12"'
mkdir -p "$DEST"
if [[ ! -d "$DEST/.git" ]]; then
    git -C "$DEST" init -q
    git -C "$DEST" remote add origin https://github.com/Soul-AILab/SoulX-Singer.git
fi
if [[ "$(git -C "$DEST" rev-parse HEAD 2>/dev/null || true)" != "$REF" ]]; then
    # Preserve any deliberate edits to this external checkout.
    git -C "$DEST" diff --quiet
    git -C "$DEST" diff --cached --quiet
    git -C "$DEST" fetch --depth 1 origin "$REF"
    git -C "$DEST" checkout --detach "$REF"
fi
if [[ ! -x "$DEST/.venv/bin/pip" ]]; then
    if [[ "$MODE" == "--system-site-packages" ]]; then
        "$PY" -m venv --system-site-packages "$DEST/.venv"
    else
        "$PY" -m venv "$DEST/.venv"
    fi
fi
VENV_PY="$DEST/.venv/bin/python"
if [[ "$MODE" == "--system-site-packages" ]]; then
    # Constraints prevent dependency resolution from replacing the worker's
    # validated CUDA torch/torchaudio, even inside this separate environment.
    "$PY" - "$DEST/.torch-constraints.txt" <<'PY'
from importlib.metadata import version
from pathlib import Path
import sys
Path(sys.argv[1]).write_text("\n".join(f"{name}==={version(name)}" for name in ("torch", "torchaudio")) + "\n")
PY
else
    "$VENV_PY" -m pip install torch torchaudio
    : > "$DEST/.torch-constraints.txt"
fi
"$VENV_PY" -m pip install -r "$HERE/requirements-soulx.txt" -c "$DEST/.torch-constraints.txt"
"$VENV_PY" "$HERE/prefetch_soulx.py" "$DEST"
"$VENV_PY" -c 'import torch, torchaudio, transformers; assert transformers.__version__ == "4.41.2"; print("SoulX ready; torch", torch.__version__)'
echo "SoulX-Singer SVC installed at $DEST (models prefetched)"
