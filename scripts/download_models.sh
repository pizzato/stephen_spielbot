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
COMFY_ENV="$HOME/github/comfyui-env"

# Accept token from env (set by install.sh or caller)
HF_TOKEN="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"

if [[ ! -d "$COMFY_DIR" ]]; then
    echo "ERROR: ComfyUI directory not found: $COMFY_DIR"
    echo "Usage: $0 [/path/to/ComfyUI]"
    exit 1
fi

# Ensure the HuggingFace CLI is available.
# huggingface_hub >= 1.0 ships the binary as `hf`; older versions used `huggingface-cli`.
# Check: local venv → comfyui-env → system PATH → install into best available pip.
HF_CMD=""
for candidate in "$VENV/bin/hf" "$COMFY_ENV/bin/hf" \
                 "$VENV/bin/huggingface-cli" "$COMFY_ENV/bin/huggingface-cli"; do
    if [[ -x "$candidate" ]]; then
        HF_CMD="$candidate"
        break
    fi
done
if [[ -z "$HF_CMD" ]]; then
    if   command -v hf               &>/dev/null; then HF_CMD="hf"
    elif command -v huggingface-cli  &>/dev/null; then HF_CMD="huggingface-cli"
    fi
fi
if [[ -z "$HF_CMD" ]]; then
    echo "[hf] HuggingFace CLI not found — installing huggingface_hub..."
    if [[ -x "$VENV/bin/pip" ]]; then
        "$VENV/bin/pip" install --quiet "huggingface_hub>=1.0"
        HF_CMD="$VENV/bin/hf"
    elif [[ -x "$COMFY_ENV/bin/pip" ]]; then
        "$COMFY_ENV/bin/pip" install --quiet "huggingface_hub>=1.0"
        HF_CMD="$COMFY_ENV/bin/hf"
    else
        python3 -m pip install --quiet --user "huggingface_hub>=1.0"
        HF_CMD="hf"
    fi
fi

echo "=== Downloading models to $COMFY_DIR ==="
[[ -n "$HF_TOKEN" ]] && echo "    (using HuggingFace token)"
echo ""

# ── Helper: download one file, skip if already present ────────────────────────
# The new `hf` CLI preserves the repo subdirectory structure under --local-dir
# (e.g. split_files/text_encoders/foo.safetensors → local_dir/split_files/…).
# After downloading we move the file to the flat local_dir and clean up the
# leftover subdirectory so the rest of the script finds it at the expected path.
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
    local extra_args=()
    [[ -n "$HF_TOKEN" ]] && extra_args+=(--token "$HF_TOKEN")
    "$HF_CMD" download "$repo" "$remote_path" \
        --local-dir "$COMFY_DIR/$local_dir" \
        --quiet \
        "${extra_args[@]+"${extra_args[@]}"}"

    # Flatten: if hf reproduced the repo subdir structure, move file to dest.
    local actual="$COMFY_DIR/$local_dir/$remote_path"
    if [[ -f "$actual" && "$actual" != "$dest" ]]; then
        mv "$actual" "$dest"
        # Remove the now-empty top-level subdir (e.g. split_files/)
        local top_subdir="${remote_path%%/*}"
        [[ "$top_subdir" != "$remote_path" && -d "$COMFY_DIR/$local_dir/$top_subdir" ]] \
            && rm -rf "$COMFY_DIR/$local_dir/$top_subdir"
    fi

    local size
    size=$(du -sh "$dest" | cut -f1)
    echo "  [done] $filename ($size)"
}

# ── Targeted per-engine download (Settings "Download" button) ─────────────────
# ENGINE_MODELS="repo|remote|dir;repo|remote|dir;…" downloads just those files,
# reusing the resolved hf CLI + download() (skip-if-present + split_files flatten),
# then exits — so the webapp can install one engine's weights without the bulk set.
if [[ -n "${ENGINE_MODELS:-}" ]]; then
    echo "=== Downloading engine models to $COMFY_DIR ==="
    [[ -n "$HF_TOKEN" ]] && echo "    (using HuggingFace token)"
    IFS=';' read -ra _ENGINE_SPECS <<< "$ENGINE_MODELS"
    for _spec in "${_ENGINE_SPECS[@]}"; do
        [[ -z "$_spec" ]] && continue
        IFS='|' read -r _repo _remote _dir <<< "$_spec"
        download "$_repo" "$_remote" "$_dir"
    done
    echo "✅ Engine model download complete."
    exit 0
fi

# ── LTX 2.3 ───────────────────────────────────────────────────────────────────
# Skip with:  SKIP_LTX=1 bash scripts/download_models.sh
if [[ "${SKIP_LTX:-0}" == "1" ]]; then
    echo "--- LTX 2.3 models skipped (SKIP_LTX=1) ---"
else
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

fi  # SKIP_LTX

# ── ACE-Step 1.5 (music generation) ──────────────────────────────────────────
# Skip with:  SKIP_ACE=1 bash scripts/download_models.sh
if [[ "${SKIP_ACE:-0}" == "1" ]]; then
    echo "--- ACE-Step models skipped (SKIP_ACE=1) ---"
else
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

fi  # SKIP_ACE

# ── FLUX.1-schnell (scene preview image generation) — optional ────────────────
# ~13 GB total. Skip with:  SKIP_FLUX=1 bash scripts/download_models.sh
if [[ "${SKIP_FLUX:-0}" == "1" ]]; then
    echo ""
    echo "--- FLUX.1-schnell models skipped (SKIP_FLUX=1) ---"
else
    echo ""
    echo "--- FLUX.1-schnell scene preview models (~13 GB) ---"

    download \
        "Comfy-Org/flux1-schnell" \
        "flux1-schnell-fp8.safetensors" \
        "models/unet"

    # BF16 (non-quantised) version — required to run ComfyUI on MPS (Apple
    # Silicon), which cannot load the fp8 model. Point flux_model at it in config
    # on a Mac worker. Requires accepting the BFL license at
    # huggingface.co/black-forest-labs/FLUX.1-schnell
    download \
        "black-forest-labs/FLUX.1-schnell" \
        "flux1-schnell.safetensors" \
        "models/unet" || echo "  [warn] flux1-schnell.safetensors skipped — accept the license at huggingface.co/black-forest-labs/FLUX.1-schnell then re-run with HF_TOKEN=<token> make install"

    # Shared FLUX text encoders (also used by FLUX dev)
    download \
        "comfyanonymous/flux_text_encoders" \
        "t5xxl_fp8_e4m3fn.safetensors" \
        "models/clip"

    download \
        "comfyanonymous/flux_text_encoders" \
        "clip_l.safetensors" \
        "models/clip"

    # FLUX VAE — BFL repo requires license approval; use public mirror instead
    download \
        "camenduru/FLUX.1-dev" \
        "ae.safetensors" \
        "models/vae"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "All models present in $COMFY_DIR:"
for dir in models/checkpoints models/loras models/latent_upscale_models \
           models/text_encoders models/diffusion_models models/vae \
           models/unet models/clip; do
    [[ -d "$COMFY_DIR/$dir" ]] || continue
    while IFS= read -r f; do
        size=$(du -sh "$f" | cut -f1)
        echo "  $size  ${f#$COMFY_DIR/}"
    done < <(find "$COMFY_DIR/$dir" -name "*.safetensors" | sort)
done

echo ""
echo "✅ Model download complete."
