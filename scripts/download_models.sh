#!/usr/bin/env bash
# Download all models required by Stephen Spielbot into a ComfyUI installation.
# Usage: bash scripts/download_models.sh [/path/to/ComfyUI]
#
# Models downloaded by default (~90+ GB):
#   LTX 2.5  — distilled transformer, Gemma 4 encoder, VAEs, latent upscaler
#              (the DEFAULT scene video engine; gated repo — needs HF_TOKEN with
#              the license accepted at huggingface.co/Lightricks/LTX-2.5)
#   LTX 2.3  — checkpoint, distilled LoRA, latent spatial upscaler, text encoder
#              (still required: keyframed establishing shots + Remix upscale)
#   LTX IC-LoRA Pixel Spatial Upscaler (2× + 4×) for Remix AI temporal upscale
#   ACE-Step 1.5 — diffusion model, VAE, two CLIP text encoders
#   FLUX.2 Klein 4B — diffusion model, Qwen-3 encoder, VAE
# Opt-in (env flags): INSTALL_FLUX1=1 adds the legacy FLUX.1-schnell engine.
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
    # 4th arg pins a git revision. Reputable org repos track main; use this for
    # sources where a silent weight swap is a real risk (see the H3 latent
    # upscaler below).
    local repo="$1" remote_path="$2" local_dir="$3" revision="${4:-}"
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
    [[ -n "$revision" ]] && extra_args+=(--revision "$revision")
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

# ── LightX2V Ref2VA turbo LoRA: rename its keys for the H3 turbo node ─────────
# LightX2V ship generic-ComfyUI keys (diffusion_model.blocks.N.…) while
# ComfyUI-MiniMax-H3-Turbo's loader builds its key map from bare module names
# (blocks.N.…). Left unstripped the node matches NOTHING and the render quietly
# produces 4 steps with no LoRA at all, so the converted file is the only one
# kept. Renaming keys leaves the tensor payload untouched — this rewrites the
# safetensors header and streams the data block through, so no torch needed.
h3_ref2v_lx2v_fixup() {
    local raw="$COMFY_DIR/models/loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
    local out="$COMFY_DIR/models/loras/minimax_h3_ref2v_turbo_4step_v0.1_h3node.safetensors"

    [[ -f "$raw" ]] || return 0
    if [[ -f "$out" ]]; then
        echo "  [skip] $(basename "$out") (already converted)"
        rm -f "$raw"
        return 0
    fi

    echo "  [convert] $(basename "$raw") → $(basename "$out")"
    if ! python3 - "$raw" "$out.part" <<'PY'
import json, shutil, sys

PREFIX = "diffusion_model."
src, dst = sys.argv[1], sys.argv[2]
with open(src, "rb") as f:
    header = json.loads(f.read(int.from_bytes(f.read(8), "little")))
    meta = dict(header.pop("__metadata__", {}))
    if not header or not all(k.startswith(PREFIX) for k in header):
        sys.exit("unexpected key layout — refusing to convert")
    out = {k[len(PREFIX):]: v for k, v in header.items()}
    meta["converted_from"] = "lightx2v/Minimax-h3-Turbo minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16"
    meta["conversion"] = "stripped 'diffusion_model.' key prefix for ComfyUI-MiniMax-H3-Turbo"
    out["__metadata__"] = meta
    blob = json.dumps(out).encode()
    blob += b" " * (-len(blob) % 8)          # keep the data block 8-byte aligned
    with open(dst, "wb") as g:
        g.write(len(blob).to_bytes(8, "little"))
        g.write(blob)
        shutil.copyfileobj(f, g, 1024 * 1024)
PY
    then
        echo "  [warn] LoRA key conversion failed — minimax-h3-ref-turbo-lx2v will not work"
        rm -f "$out.part"
        return 0
    fi
    mv "$out.part" "$out"
    rm -f "$raw"
    echo "  [done] $(basename "$out") ($(du -sh "$out" | cut -f1))"
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
    h3_ref2v_lx2v_fixup   # no-op unless that LoRA is one of the files above
    echo "✅ Engine model download complete."
    exit 0
fi

# ── LTX 2.5 (default scene video engine) ─────────────────────────────────────
# Gated repo: accept the license at huggingface.co/Lightricks/LTX-2.5 and run
# with HF_TOKEN set. Skip with:  SKIP_LTX=1 bash scripts/download_models.sh
if [[ "${SKIP_LTX:-0}" == "1" ]]; then
    echo "--- LTX 2.5 models skipped (SKIP_LTX=1) ---"
else
echo "--- LTX 2.5 video generation models (~40 GB, gated) ---"
if [[ -z "$HF_TOKEN" ]]; then
    echo "  [warn] HF_TOKEN not set — the Lightricks/LTX-2.5 repo is click-through"
    echo "         gated, so these downloads will likely fail. Accept the license at"
    echo "         https://huggingface.co/Lightricks/LTX-2.5 and re-run with HF_TOKEN=<token>."
fi

download \
    "Lightricks/LTX-2.5" \
    "diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors" \
    "models/diffusion_models" \
    || echo "  [warn] LTX 2.5 transformer skipped — scene renders need it (see HF_TOKEN note above)"

download \
    "Lightricks/LTX-2.5" \
    "text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors" \
    "models/text_encoders" \
    || echo "  [warn] LTX 2.5 text encoder skipped"

download \
    "Lightricks/LTX-2.5" \
    "vae/ltx-2.5-video-vae-bf16.safetensors" \
    "models/vae" \
    || echo "  [warn] LTX 2.5 video VAE skipped"

download \
    "Lightricks/LTX-2.5" \
    "vae/ltx-2.5-audio-vae-bf16.safetensors" \
    "models/vae" \
    || echo "  [warn] LTX 2.5 audio VAE skipped"

download \
    "Lightricks/LTX-2.5" \
    "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors" \
    "models/latent_upscale_models" \
    || echo "  [warn] LTX 2.5 latent upscaler skipped"

# ── LTX 2.3 ───────────────────────────────────────────────────────────────────
# No longer a scene engine, but still required by the keyframed establishing
# shots (dialogue push-ins) and the Remix IC-LoRA pixel upscale.
echo ""
echo "--- LTX 2.3 models (keyframed shots + Remix upscale) ---"

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

# Generative pixel spatial upscalers (IC-LoRA) — Remix "AI temporal" mode.
# https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler
download \
    "Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler" \
    "ltx-2.3-22b-ic-lora-pixel-spatial-upscaler-x2-0.9.safetensors" \
    "models/loras"

download \
    "Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler" \
    "ltx-2.3-22b-ic-lora-pixel-spatial-upscaler-x4-0.9.safetensors" \
    "models/loras"

# MiniMax H3 latent upscaler — Remix "H3 latent" mode. Community Apache-2.0
# model over H3's 24-channel latents. bf16 only: the repo's .pth loads through
# torch.load(weights_only=False), which we won't run.
# https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler
# PINNED to a revision, unlike the org-published models above: this is a
# single-author repo first published 2026-08-17, so tracking a mutable main
# would let the weights be swapped under us on any future rebuild.
# Verified contents of this revision (s3, 2026-08-18): pure safetensors, 322
# BF16 tensors, all names matching the node's declared architecture, no
# __metadata__ blob. sha256:
#   4f57821f5837f32f7142b67d815606dbd7550f194e5c769f7d6c3f83b146a5e6
download \
    "LBH-123-AI/Minimax_h3_latent_Upscaler" \
    "minimax_h3_latent_upscaler_3d_bf16.safetensors" \
    "models/latent_upscale_models" \
    "97b4a93d3ab57957d80244b141348a322d77c80a" \
    || echo "  [warn] MiniMax H3 latent upscaler skipped"

# FlashVSR v1.1 — the default "flashvsr" finishing/Remix upscaler (one-step
# diffusion video super-resolution on Wan2.1, Apache-2.0). The ComfyUI node
# loads the whole folder by name, so all four files go to models/FlashVSR-v1.1/.
# PINNED to a revision: single-author repo. The .ckpt/.pth files are pickles;
# torch >= 2.6 loads them with weights_only=True, which the image satisfies.
# https://huggingface.co/JunhaoZhuang/FlashVSR-v1.1
for f in LQ_proj_in.ckpt TCDecoder.ckpt Wan2.1_VAE.pth \
         diffusion_pytorch_model_streaming_dmd.safetensors config.json model_index.json; do
    download \
        "JunhaoZhuang/FlashVSR-v1.1" \
        "$f" \
        "models/FlashVSR-v1.1" \
        "27561b186ded3402d7c975f4fd722e2885b6135f" \
        || echo "  [warn] FlashVSR $f skipped"
done

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

# ── FLUX.2 Klein 4B (DEFAULT image + edit engine) ─────────────────────────────
# Fast (4-step), commercial (Apache-2.0) — the default for scenes + cover.
# ~16 GB. Skip with:  SKIP_FLUX2=1 bash scripts/download_models.sh
if [[ "${SKIP_FLUX2:-0}" == "1" ]]; then
    echo ""
    echo "--- FLUX.2 Klein models skipped (SKIP_FLUX2=1) ---"
else
    echo ""
    echo "--- FLUX.2 Klein 4B image models (~16 GB) ---"

    download \
        "Comfy-Org/vae-text-encorder-for-flux-klein-4b" \
        "split_files/diffusion_models/flux-2-klein-4b.safetensors" \
        "models/diffusion_models"

    download \
        "Comfy-Org/vae-text-encorder-for-flux-klein-4b" \
        "split_files/text_encoders/qwen_3_4b.safetensors" \
        "models/text_encoders"

    download \
        "Comfy-Org/vae-text-encorder-for-flux-klein-4b" \
        "split_files/vae/flux2-vae.safetensors" \
        "models/vae"
fi

# ── FLUX.1 schnell (legacy image engine) — OPT-IN ─────────────────────────────
# The default is now FLUX.2 Klein. Download schnell only if you'll select it per
# style:
#   INSTALL_FLUX1=1 bash scripts/download_models.sh
if [[ "${INSTALL_FLUX1:-0}" == "1" ]]; then
    echo ""
    echo "--- FLUX.1-schnell scene preview models (~13 GB) ---"

    download \
        "Comfy-Org/flux1-schnell" \
        "flux1-schnell-fp8.safetensors" \
        "models/unet"

    # BF16 (non-quantised) version — required to run ComfyUI on MPS (Apple
    # Silicon), which cannot load the fp8 model. Requires accepting the BFL
    # license at huggingface.co/black-forest-labs/FLUX.1-schnell
    download \
        "black-forest-labs/FLUX.1-schnell" \
        "flux1-schnell.safetensors" \
        "models/unet" || echo "  [warn] flux1-schnell.safetensors skipped — accept the license at huggingface.co/black-forest-labs/FLUX.1-schnell then re-run with HF_TOKEN=<token>"

    # Shared FLUX.1 text encoders
    download \
        "comfyanonymous/flux_text_encoders" \
        "t5xxl_fp8_e4m3fn.safetensors" \
        "models/clip"

    download \
        "comfyanonymous/flux_text_encoders" \
        "clip_l.safetensors" \
        "models/clip"

    # FLUX.1 VAE — from the official, ungated Apache-2.0 schnell repo
    # (matches pipeline/engines.py; no unofficial mirror).
    download \
        "black-forest-labs/FLUX.1-schnell" \
        "ae.safetensors" \
        "models/vae"
else
    echo ""
    echo "--- FLUX.1-schnell skipped (opt-in: INSTALL_FLUX1=1) ---"
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
