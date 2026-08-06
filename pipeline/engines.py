"""Engine registries: selectable model bundles for image and video generation.

An *engine* is a bundle of {ComfyUI workflow + text encoder + model files + steps
+ license}. The user-facing choice is an engine **key**, not a raw filename,
because FLUX.1 and FLUX.2 use different ComfyUI graphs and encoders. Per-style
settings carry ``image_engine`` (scene generation) and ``edit_engine`` (the
"Edit image" inpaint); both fall back to ``flux1-schnell``.

``VIDEO_ENGINES`` is the same idea for the scene I2V model (per-style
``video_engine``): ``ltx23`` is the incumbent LTX 2.3 path, ``minimax-h3`` is
opt-in. Unlike the image engines, video engines are not Apache-only — each
entry carries its license terms and the Settings UI surfaces them.

Only commercial-usable (Apache-2.0) engines are bundled:
- ``flux2-klein`` — the default; fast 4-step FLUX.2 (Apache-2.0, commercial OK).
- ``flux1-schnell`` — the legacy validated path (text→image + masked img2img),
  opt-in and downloaded on demand.
"""
from __future__ import annotations

# Encoders + VAE shared by every FLUX.1 engine — downloaded once, reused.
_FLUX1_T5 = ("comfyanonymous/flux_text_encoders", "t5xxl_fp8_e4m3fn.safetensors", "models/clip")
_FLUX1_CLIP_L = ("comfyanonymous/flux_text_encoders", "clip_l.safetensors", "models/clip")
_FLUX1_VAE = ("black-forest-labs/FLUX.1-schnell", "ae.safetensors", "models/vae")


def _model(repo: str, remote: str, subdir: str, *, gated: bool = False) -> dict:
    return {"repo": repo, "remote": remote, "dir": subdir,
            "file": remote.rsplit("/", 1)[-1], "gated": gated}


ENGINES: dict[str, dict] = {
    "flux1-schnell": {
        "key": "flux1-schnell",
        "label": "FLUX.1 schnell",
        "sub": "Fast · 4 steps · commercial OK",
        "family": "flux1",
        "can_generate": True,
        "can_edit": True,
        "edit_mode": "noise_mask",
        "t2i_workflow": "flux_t2i.json",
        "edit_workflow": "flux_inpaint.json",
        "steps": 4,
        "edit_denoise": 0.85,
        "guidance": None,
        "commercial_ok": True,
        "license": "Apache-2.0",
        # Canonical filenames; flux1-schnell's may be overridden by the legacy flat
        # config keys (flux_model/flux_clip_t5/flux_clip_l/flux_vae) in resolve().
        "model_file": "flux1-schnell-fp8.safetensors",
        "clip_t5": "t5xxl_fp8_e4m3fn.safetensors",
        "clip_l": "clip_l.safetensors",
        "vae": "ae.safetensors",
        "probe": ("UNETLoader", "unet_name", "flux1-schnell-fp8.safetensors"),
        "models": [
            _model("Comfy-Org/flux1-schnell", "flux1-schnell-fp8.safetensors", "models/unet"),
            _model(*_FLUX1_T5), _model(*_FLUX1_CLIP_L), _model(*_FLUX1_VAE),
        ],
    },
    "flux2-klein": {
        "key": "flux2-klein",
        "label": "FLUX.2 Klein 4B",
        "sub": "Fast · 4 steps · commercial OK · needs validation",
        "family": "flux2",
        "can_generate": True,
        "can_edit": True,
        "edit_mode": "flux2",
        "t2i_workflow": "flux2_t2i.json",
        # Reference-image conditioned t2i (FLUX.2 ReferenceLatent) — used when a
        # scene features a character that has a reference image. See comfyui
        # generate_with_engine(reference_images=...).
        "t2i_ref_workflow": "flux2_t2i_ref.json",
        "edit_workflow": "flux2_edit.json",
        "steps": 4,
        "edit_denoise": 1.0,
        "guidance": 4.0,
        "commercial_ok": True,
        "license": "Apache-2.0",
        "model_file": "flux-2-klein-4b.safetensors",
        "clip_t5": "qwen_3_4b.safetensors",   # Klein uses a Qwen-3 encoder (NOT the Mistral one dev uses)
        "clip_l": None,
        "vae": "flux2-vae.safetensors",
        "probe": ("UNETLoader", "unet_name", "flux-2-klein-4b.safetensors"),
        # Klein 4B (Apache-2.0, commercial). The ComfyUI-ready model + its Qwen-3
        # encoder + VAE all come from the Comfy-Org klein repo (note their typo
        # "encorder"); verified against the ComfyUI Klein blueprint.
        "models": [
            _model("Comfy-Org/vae-text-encorder-for-flux-klein-4b",
                   "split_files/diffusion_models/flux-2-klein-4b.safetensors", "models/diffusion_models"),
            _model("Comfy-Org/vae-text-encorder-for-flux-klein-4b",
                   "split_files/text_encoders/qwen_3_4b.safetensors", "models/text_encoders"),
            _model("Comfy-Org/vae-text-encorder-for-flux-klein-4b",
                   "split_files/vae/flux2-vae.safetensors", "models/vae"),
        ],
    },
}

# FLUX.2 Klein is the default engine (fast, commercial Apache-2.0, much better
# than schnell) — used for new styles and as the resolve()/normalize fallback.
# FLUX.1 schnell is opt-in: chosen per style and downloaded on demand.
# (Covers used to be pinned to it because the model had to render the title
# text in-image and Klein garbled it; cover typography draws the title with
# real fonts on a text-free background, so covers now use the style's engine.)
DEFAULT_ENGINE = "flux2-klein"


def get(key: str) -> dict | None:
    return ENGINES.get((key or "").strip())


def resolve(cfg: dict, key: str) -> dict:
    """Return the engine dict for *key* (falling back to the default), with
    flux1-schnell's filenames overridden by the legacy flat config keys so existing
    setups keep working."""
    eng = dict(get(key) or ENGINES[DEFAULT_ENGINE])
    if eng["key"] == "flux1-schnell":
        eng = {**eng,
               "model_file": cfg.get("flux_model") or eng["model_file"],
               "clip_t5": cfg.get("flux_clip_t5") or eng["clip_t5"],
               "clip_l": cfg.get("flux_clip_l") or eng["clip_l"],
               "vae": cfg.get("flux_vae") or eng["vae"],
               "steps": int(cfg.get("flux_steps") or eng["steps"])}
    return eng


def public_list(commercial_only: bool = False) -> list[dict]:
    """Compact engine descriptors for the Settings UI (no download internals)."""
    out = []
    for e in ENGINES.values():
        if commercial_only and not e["commercial_ok"]:
            continue
        out.append({
            "key": e["key"], "label": e["label"], "sub": e["sub"],
            "can_generate": e["can_generate"], "can_edit": e["can_edit"],
            "commercial_ok": e["commercial_ok"], "license": e["license"],
        })
    return out


# ── Video engines (per-style ``video_engine``) ───────────────────────────────
# ltx23 has no ``models`` list: its weights are part of the bulk worker install
# (scripts/download_models.sh). minimax-h3 downloads on demand like the opt-in
# image engines, and additionally needs the worker's ComfyUI to register its
# nodes (``requires_node`` — ComfyUI ≥ v0.30.0).
VIDEO_ENGINES: dict[str, dict] = {
    "ltx23": {
        "key": "ltx23",
        "label": "LTX 2.3 22B",
        "sub": "Fast · native audio · negative prompt",
        "family": "ltx",
        "commercial_ok": True,
        "license": "LTX-2 Community License",
        "probe": ("CheckpointLoaderSimple", "ckpt_name", "ltx-2.3-22b-dev-fp8.safetensors"),
    },
    "minimax-h3": {
        "key": "minimax-h3",
        "label": "MiniMax H3 33B",
        "sub": "Slow · higher fidelity · native stereo audio · restricted license",
        "family": "minimax",
        "commercial_ok": True,
        "license": "MiniMax H3 Community License",
        "license_note": ("Not licensed for use in the USA, EU, UK or South Korea. "
                         "Requires machine-generated disclosure and “MiniMax H3” "
                         "attribution; separate authorization above US$20M yearly revenue."),
        "workflow": "h3_i2v.json",
        # 15 steps ≈ 25% faster than the ComfyUI template's 20 with little
        # visible loss (the reference recipe is 50 — 20 was already speed-tuned).
        "steps": 15,
        # Native EasyCache (ComfyUI ≥ v0.30.0): skips DiT steps whose latent
        # barely changes. 0.2 is the node default; higher = faster/looser.
        "easycache_threshold": 0.2,
        # H3 scene renders can far outlive the default 1 h durable-task lease on
        # GB10-class workers; renderers extend the lease to this before starting.
        "lease_seconds": 14400,
        # ComfyUI-blessed consumer stack (~40 GB): pruned INT8 unet + NVFP4 AWQ
        # Qwen3-VL 32B encoder + the two VAEs, exactly as in the official
        # video_minimax_h3_i2v template. Swap filenames here to change variant.
        "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "video_vae": "minimax_h3_video_vae_fp16.safetensors",
        "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
        "requires_node": "MiniMaxH3ImageToVideo",
        "probe": ("UNETLoader", "unet_name", "minimax_h3_fl2va_pruned_int8_convrot.safetensors"),
        "models": [
            _model("Comfy-Org/MiniMax-H3",
                   "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                   "models/diffusion_models"),
            _model("Comfy-Org/MiniMax-H3",
                   "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                   "models/text_encoders"),
            _model("Comfy-Org/MiniMax-H3",
                   "vae/minimax_h3_video_vae_fp16.safetensors", "models/vae"),
            _model("Comfy-Org/MiniMax-H3",
                   "vae/minimax_h3_audio_vae_fp32.safetensors", "models/vae"),
        ],
    },
    "minimax-h3-turbo": {
        "key": "minimax-h3-turbo",
        "label": "MiniMax H3 33B Turbo",
        "sub": "Faster H3 · distilled few-step LoRA · early preview · restricted license",
        "family": "minimax",
        "commercial_ok": True,
        "license": "MiniMax H3 Community License",
        "license_note": ("Not licensed for use in the USA, EU, UK or South Korea. "
                         "Requires machine-generated disclosure and “MiniMax H3” "
                         "attribution; separate authorization above US$20M yearly revenue."),
        "workflow": "h3_turbo_i2v.json",
        # Measured on s1 (704×1280, 294 frames): 4 steps = 12.1 min vs the base
        # engine's 23 min, with no visible motion smear; 8 steps = 22.6 min
        # (parity — the non-pruned unet eats the step savings). Raise toward
        # 6–8 only if content shows softness.
        "steps": 4,
        "lease_seconds": 14400,
        # The turbo LoRA only fits the NON-pruned DiT (the pruned variants use a
        # different time-conditioning layer), so this engine swaps the 19 GB
        # pruned unet for the full 31 GB int8 one; encoder and VAEs are shared
        # with minimax-h3.
        "unet": "minimax_h3_fl2va_int8_convrot.safetensors",
        "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "video_vae": "minimax_h3_video_vae_fp16.safetensors",
        "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
        "lora": "minimax_h3_turbo_4step_ckpt500.safetensors",
        # Video and audio ride different flow schedules; the custom sampler
        # steps each on its own clock (a stock sampler breaks the audio at few
        # steps). Shipped by docker/comfyui (ComfyUI-MiniMax-H3-Turbo).
        "requires_node": "MiniMaxH3TurboSampler",
        "probe": ("UNETLoader", "unet_name", "minimax_h3_fl2va_int8_convrot.safetensors"),
        "models": [
            _model("Comfy-Org/MiniMax-H3",
                   "diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors",
                   "models/diffusion_models"),
            _model("Comfy-Org/MiniMax-H3",
                   "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                   "models/text_encoders"),
            _model("Comfy-Org/MiniMax-H3",
                   "vae/minimax_h3_video_vae_fp16.safetensors", "models/vae"),
            _model("Comfy-Org/MiniMax-H3",
                   "vae/minimax_h3_audio_vae_fp32.safetensors", "models/vae"),
            _model("larryvrh/MiniMax-H3-Turbo-Lora",
                   "minimax_h3_turbo_4step_ckpt500.safetensors", "models/loras"),
        ],
    },
}

DEFAULT_VIDEO_ENGINE = "ltx23"


def get_video(key: str) -> dict | None:
    return VIDEO_ENGINES.get((key or "").strip())


def resolve_video(cfg: dict, key: str) -> dict:
    """Return the video-engine dict for *key*, falling back to the default.
    *cfg* is unused today but kept for symmetry with :func:`resolve`."""
    return dict(get_video(key) or VIDEO_ENGINES[DEFAULT_VIDEO_ENGINE])


def public_list_video() -> list[dict]:
    """Compact video-engine descriptors for the Settings UI."""
    return [{
        "key": e["key"], "label": e["label"], "sub": e["sub"],
        "commercial_ok": e["commercial_ok"], "license": e["license"],
        "license_note": e.get("license_note"),
        "downloadable": bool(e.get("models")),
    } for e in VIDEO_ENGINES.values()]
