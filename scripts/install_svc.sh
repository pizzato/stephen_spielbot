#!/usr/bin/env bash
# Install both singing-voice conversion engines on the CONTROLLER, for
# the song panel's "Sing this as [voice]" step: the generated song's vocals
# are re-voiced as a library voice from its ~10 s reference clip — melody,
# timing and words kept, timbre swapped. No training.
#
# SoulX-Singer SVC is the default; Seed-VC remains available by style. Each
# engine has its own environment. Demucs and lyric alignment stay in Seed's.
# See THIRD_PARTY_NOTICES.md for the Apache-2.0 / GPL-3.0 licenses.
#
# Usage: bash scripts/install_svc.sh
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

DEST="${HOME}/.local/share/video-generator/seed-vc"
PY="${SVC_PYTHON:-}"
if [[ -z "$PY" ]]; then
    for candidate in /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.10 python3.11 python3.12 python3.10; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PY="$(command -v "$candidate")"
            break
        fi
    done
fi

if [ ! -x "$PY" ]; then
    echo "ERROR: install Python 3.11 (brew install python@3.11 on macOS)" >&2
    echo "or set SVC_PYTHON to a 3.10-3.12 interpreter (Seed-VC's deps pin" >&2
    echo "scipy versions that predate 3.13)." >&2
    exit 1
fi
"$PY" -c 'import sys; assert (3, 10) <= sys.version_info[:2] <= (3, 12), "SVC_PYTHON must be Python 3.10-3.12"'

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
# demucs (MIT): vocal-stem separation — the converter runs on the VOCAL stem
# only, so the instruments come through untouched.
"$DEST/.venv/bin/pip" install -q demucs
# faster-whisper (MIT): word-timestamp transcription of the vocal stem, used
# by the music-video divide to align the lyric sheet to the sung track
# (song_align_lyrics). Weights download on the first alignment.
"$DEST/.venv/bin/pip" install -q faster-whisper
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

# Prefetch separation too, so normal revoicing has its Demucs model ready.
"$DEST/.venv/bin/python" -c 'from demucs.pretrained import get_model; get_model("htdemucs")'
bash "$REPO_ROOT/docker/comfyui/singing/install_soulx.sh" \
    "${HOME}/.local/share/video-generator/soulx-singer" "$PY"
