#!/usr/bin/env bash
# Download all models required by Stephen Spielbot into a ComfyUI installation.
# Usage: bash scripts/download_models.sh [/path/to/ComfyUI]
#
# Models downloaded (~33 GB total):
#   LTX 2.3  — checkpoint, LoRA, spatial upscaler, text encoder  (~28 GB)
#   ACE-Step 1.5 — diffusion model, VAE, two CLIP text encoders   (~5 GB)
set -euo pipefail

COMFY_DIR="${1:-$HOME/github/ComfyUI}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_ROOT/.venv"

if [[ ! -d "$COMFY_DIR" ]]; then
    echo "ERROR: ComfyUI directory not found: $COMFY_DIR"
    echo "Usage: $0 [/path/to/ComfyUI]"
    exit 1
fi

# Ensure huggingface-cli is available (prefer venv, fall back to system pip)
if ! command -v huggingface-cli &>/dev/null && ! [[ -x "$VENV/bin/huggingface-cli" ]]; then
    echo "[hf] huggingface-cli not found — installing huggingface_hub..."
    if [[ -x "$VENV/bin/pip" ]]; then
        "$VENV/bin/pip" install --quiet huggingface_hub
    else
        python3 -m pip install --quiet huggingface_hub
    fi
fi

# Use venv's huggingface-cli if available
if [[ -x "$VENV/bin/huggingface-cli" ]]; then
    export PATH="$VENV/bin:$PATH"
fi

echo "=== Downloading models to $COMFY_DIR ==="
echo ""

# ── Helper: download one file, skip if already present ────────────────────────
download() {
    local repo="$1" remote_path="$2" local_dir="$3"
    local filename="${remote_path##*/}"
    local dest="$COMFY_DIR/$local_dir/$filename"

    if [[ -f "$dest" ]]; then
        local size
        size=$(du -sh "$dest" | cut -f1)
        echo "  [skip] $filename ($size already present)"
        return
    fi

    echo "  [download] $filename  ← $repo/$remote_path"
    mkdir -p "$COMFY_DIR/$local_dir"
    huggingface-cli download "$repo" "$remote_path" \
        --local-dir "$COMFY_DIR/$local_dir" \
        --local-dir-use-symlinks False \
        --quiet
    local size
    size=$(du -sh "$dest" | cut -f1)
    echo "  [done] $filename ($size)"
}

# ── LTX 2.3 ───────────────────────────────────────────────────────────────────
echo "--- LTX 2.3 video generation models ---"

download \
    "Lightricks/LTX-2.3-fp8" \
    "ltx-2.3-22b-dev-fp8.safetensors" \
    "models/checkpoints"

download \
    "Lightricks/LTX-2.3" \
    "ltx-2.3-22b-distilled-lora-384.safetensors" \
    "models/loras"

download \
    "Lightricks/LTX-2.3" \
    "ltx-2.3-spatial-upscaler-x2-1.1.safetensors" \
    "models/latent_upscale_models"

download \
    "Comfy-Org/ltx-2" \
    "split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors" \
    "models/text_encoders"

# ── ACE-Step 1.5 (music generation) ──────────────────────────────────────────
echo ""
echo "--- ACE-Step 1.5 music generation models ---"

download \
    "Comfy-Org/ace_step_1.5_ComfyUI_files" \
    "split_files/diffusion_models/acestep_v1.5_turbo.safetensors" \
    "models/diffusion_models"

download \
    "Comfy-Org/ace_step_1.5_ComfyUI_files" \
    "split_files/vae/ace_1.5_vae.safetensors" \
    "models/vae"

download \
    "Comfy-Org/ace_step_1.5_ComfyUI_files" \
    "split_files/text_encoders/qwen_0.6b_ace15.safetensors" \
    "models/text_encoders"

download \
    "Comfy-Org/ace_step_1.5_ComfyUI_files" \
    "split_files/text_encoders/qwen_4b_ace15.safetensors" \
    "models/text_encoders"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "All models present in $COMFY_DIR:"
for dir in models/checkpoints models/loras models/latent_upscale_models \
           models/text_encoders models/diffusion_models models/vae; do
    [[ -d "$COMFY_DIR/$dir" ]] || continue
    while IFS= read -r f; do
        size=$(du -sh "$f" | cut -f1)
        echo "  $size  ${f#$COMFY_DIR/}"
    done < <(find "$COMFY_DIR/$dir" -name "*.safetensors" | sort)
done

echo ""
echo "✅ Model download complete."
