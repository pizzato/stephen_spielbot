#!/usr/bin/env bash
# Install seed-vc (zero-shot singing-voice conversion) on the CONTROLLER, for
# the song panel's "Sing this as [voice]" step: the generated song's vocals
# are re-voiced as a library voice from its ~10 s reference clip — melody,
# timing and words kept, timbre swapped. No training.
#
# Runs locally (Apple Silicon: torch on MPS; a 15 s song converts in a few
# minutes). NOTE: seed-vc is GPL-3.0 — fine self-hosted; see
# THIRD_PARTY_NOTICES.md before redistributing anything built from it.
#
# Usage: bash scripts/install_svc.sh
set -euo pipefail

DEST="${HOME}/.local/share/video-generator/seed-vc"
PY="${SVC_PYTHON:-/opt/homebrew/bin/python3.10}"

if [ ! -x "$PY" ]; then
    echo "ERROR: $PY not found — install python 3.10 (brew install python@3.10)" >&2
    echo "or set SVC_PYTHON to a 3.10/3.11 interpreter (seed-vc's deps pin" >&2
    echo "scipy versions that predate 3.13)." >&2
    exit 1
fi

if [ ! -d "$DEST/.git" ]; then
    git clone --depth 1 https://github.com/Plachtaa/seed-vc.git "$DEST"
fi
if [ ! -x "$DEST/.venv/bin/python" ]; then
    "$PY" -m venv "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install -q --upgrade pip
# torch first (arm64 wheels), then the repo's mac requirements minus its
# nightly-CPU torch pins — the stable wheels are fine and cache better.
"$DEST/.venv/bin/pip" install -q torch torchaudio torchcodec
grep -v -E "^torch|^--extra-index-url|^torchvision|^torchaudio" \
    "$DEST/requirements-mac.txt" > "$DEST/.reqs.txt"
"$DEST/.venv/bin/pip" install -q -r "$DEST/.reqs.txt"

# MPS can't hold float64 tensors, and the f0 extractor returns float64 arrays —
# cast at the boundary. Idempotent (replace is a no-op once applied).
"$DEST/.venv/bin/python" - <<'PYEOF'
from pathlib import Path
import os
p = Path(os.path.expanduser("~/.local/share/video-generator/seed-vc/inference.py"))
t = p.read_text()
for name in ("F0_ori", "F0_alt"):
    t = t.replace(
        f"{name} = torch.from_numpy({name}).to(device)[None]",
        f"{name} = torch.from_numpy({name}.astype('float32')).to(device)[None]")
p.write_text(t)
print("MPS float32 patch applied")
PYEOF

echo "seed-vc installed at $DEST"
echo "(model weights download from Hugging Face on the first conversion)"
