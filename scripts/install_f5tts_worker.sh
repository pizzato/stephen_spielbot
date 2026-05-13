#!/usr/bin/env bash
# Install F5-TTS on a remote worker host.
# Usage: bash scripts/install_f5tts_worker.sh <hostname>
set -euo pipefail

TARGET="${1:-}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -z "$TARGET" ]]; then
    echo "Usage: $0 <hostname>"
    exit 1
fi

echo "=== Installing F5-TTS on $TARGET ==="

# ── 1. Create venv and install f5-tts ─────────────────────────────────────────
ssh "$TARGET" bash <<'REMOTE'
set -euo pipefail

VENV="$HOME/f5tts-env"
PYTHON="$HOME/miniconda3/bin/python3"

# Fall back to system python3 if miniconda isn't present
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="$(which python3)"
fi

if [[ -f "$VENV/bin/activate" ]]; then
    echo "[f5tts] venv already exists at $VENV"
else
    echo "[f5tts] creating venv at $VENV with $($PYTHON --version)..."
    "$PYTHON" -m venv "$VENV"
fi

source "$VENV/bin/activate"

if python -c "import f5_tts" 2>/dev/null; then
    echo "[f5tts] already installed: $(pip show f5-tts 2>/dev/null | grep ^Version || echo 'unknown')"
else
    echo "[f5tts] installing f5-tts..."
    pip install --quiet f5-tts
    echo "[f5tts] installed: $(pip show f5-tts | grep ^Version)"
fi
REMOTE

# ── 2. Sync default narrator reference audio ──────────────────────────────────
if [[ -f "$REPO_ROOT/assets/default_narrator.wav" ]]; then
    echo "[f5tts] syncing default narrator..."
    ssh "$TARGET" "mkdir -p \$HOME/github/video-generator/assets"
    rsync -q "$REPO_ROOT/assets/default_narrator.wav" \
        "${TARGET}:~/github/video-generator/assets/"
    echo "[f5tts] narrator synced"
fi

echo "✅ F5-TTS ready on $TARGET"
