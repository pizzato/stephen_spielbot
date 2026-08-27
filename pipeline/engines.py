"""Engine registries: selectable model bundles for image and video generation.

An *engine* is a bundle of {ComfyUI workflow + text encoder + model files + steps
+ license}. The user-facing choice is an engine **key**, not a raw filename,
because FLUX.1 and FLUX.2 use different ComfyUI graphs and encoders. Per-style
settings carry ``image_engine`` (scene generation) and ``edit_engine`` (the
"Edit image" inpaint); both fall back to ``flux1-schnell``.

``VIDEO_ENGINES`` is the same idea for the scene I2V model (per-style
``video_engine``): ``ltx25`` is the default LTX path, ``minimax-h3`` is
opt-in. ``MUSIC_ENGINES`` is the same again for the background bed (per-style
``music_engine``): ``ace-step`` is the default, ``minimax-music3`` is opt-in.
Unlike the image engines, video and music engines are not Apache-only — each
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
# ltx25 is the default scene engine (LTX 2.3 was replaced outright — same
# license terms, ~26% faster at production size; the 2.3 checkpoint itself is
# still bulk-installed for the keyframed establishing shots and the Remix
# IC-LoRA pixel upscale, which render outside this registry). ltx25 is part of
# the bulk install and downloadable per worker from Settings; the H3 engines
# download on demand only. Engines need the worker's ComfyUI to register their
# nodes (``requires_node`` — ComfyUI ≥ v0.30.0 for H3, ≥ v0.32.0 for LTX 2.5).
VIDEO_ENGINES: dict[str, dict] = {
    "ltx25": {
        "key": "ltx25",
        "label": "LTX 2.5 22B",
        "sub": "Fast · native audio · negative prompt · 24 fps",
        "family": "ltx",
        "commercial_ok": True,
        "license": "LTX-2.x Community License",
        "workflow": "ltx25_i2v.json",
        # LTX 2.5 runs at 24 fps (2.3 runs at 25) and its latent wants frame
        # counts of the form 8k+1 — comfyui.generate_video_continuation reads
        # both off the engine.
        "fps": 24,
        "frame_multiple": 8,
        # ComfyUI-blessed consumer stack (~40 GB): int8+convrot transformer and
        # Gemma 4 12B encoder exactly as in the official docs.comfy.org LTX-2.5
        # workflows, plus the diffusion video VAE ("best quality" decoder), the
        # audio VAE and the 2.5 latent spatial upscaler for the second pass.
        "unet": "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
        "clip": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
        "video_vae": "ltx-2.5-video-vae-bf16.safetensors",
        "audio_vae": "ltx-2.5-audio-vae-bf16.safetensors",
        "upscaler": "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
        # The graph itself uses only native loaders/samplers, but LTX 2.5
        # support (gemma4 'ltxv' CLIP + the 2.5 transformer) landed in ComfyUI
        # v0.32.0 — probe one of the nodes that release introduced so the
        # Settings availability check catches too-old workers, and hard-gate
        # renders on the same floor.
        "requires_node": "LTXVDualCFGGuider",
        "min_comfyui": (0, 32, 0),
        "probe": ("UNETLoader", "unet_name",
                  "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors"),
        # Lightricks/LTX-2.5 is a click-through repo: accept the license on
        # huggingface.co/Lightricks/LTX-2.5 and set an HF token before the
        # Settings download.
        "models": [
            _model("Lightricks/LTX-2.5",
                   "diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
                   "models/diffusion_models", gated=True),
            _model("Lightricks/LTX-2.5",
                   "text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
                   "models/text_encoders", gated=True),
            _model("Lightricks/LTX-2.5",
                   "vae/ltx-2.5-video-vae-bf16.safetensors", "models/vae", gated=True),
            _model("Lightricks/LTX-2.5",
                   "vae/ltx-2.5-audio-vae-bf16.safetensors", "models/vae", gated=True),
            _model("Lightricks/LTX-2.5",
                   "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
                   "models/latent_upscale_models", gated=True),
        ],
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
    # ── Reference-to-video (Ref2VA): performance films ───────────────────────
    # Not scene I2V engines — these take character portraits (and voice clips)
    # instead of a first frame, and are only reachable from a performance-mode
    # scene (the Dialogue/Mixed format). "reference" marks them so the Settings
    # video-engine picker keeps offering only the I2V engines.
    "minimax-h3-ref": {
        "key": "minimax-h3-ref",
        "label": "MiniMax H3 33B Ref2VA",
        "sub": "Character portraits → video + spoken dialogue · restricted license",
        "family": "minimax",
        "reference": True,
        "commercial_ok": True,
        "license": "MiniMax H3 Community License",
        "license_note": ("Not licensed for use in the USA, EU, UK or South Korea. "
                         "Requires machine-generated disclosure and “MiniMax H3” "
                         "attribution; separate authorization above US$20M yearly revenue."),
        "workflow": "h3_ref2v.json",
        "steps": 15,
        "easycache_threshold": 0.2,
        "lease_seconds": 14400,
        # Same stack as minimax-h3 with the ref2va sibling checkpoint (identical
        # size and quantisation); encoder and both VAEs are already downloaded
        # for the I2V engine. Measured on s1/s2: ~22-24 min per 10 s scene at
        # 704×1280 (warm ≈ cold), so prefer the turbo entry below.
        "unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "video_vae": "minimax_h3_video_vae_fp16.safetensors",
        "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
        "requires_node": "MiniMaxH3ReferenceToVideo",
        "probe": ("UNETLoader", "unet_name",
                  "minimax_h3_ref2va_pruned_int8_convrot.safetensors"),
        "models": [
            _model("Comfy-Org/MiniMax-H3",
                   "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
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
    "minimax-h3-ref-turbo": {
        "key": "minimax-h3-ref-turbo",
        "label": "MiniMax H3 33B Ref2VA Turbo",
        "sub": "Faster Ref2VA · distilled few-step LoRA · restricted license",
        "family": "minimax",
        "reference": True,
        "commercial_ok": True,
        "license": "MiniMax H3 Community License",
        "license_note": ("Not licensed for use in the USA, EU, UK or South Korea. "
                         "Requires machine-generated disclosure and “MiniMax H3” "
                         "attribution; separate authorization above US$20M yearly revenue."),
        "workflow": "h3_ref2v_turbo.json",
        # Measured on s3 (704×1280, 243 frames): 10.2 min including a cold 34 GB
        # load, vs 22.9 min for the base ref engine — quality at least equal.
        "steps": 4,
        "lease_seconds": 14400,
        # As with the I2V turbo, the LoRA only fits the NON-pruned DiT: the
        # pruned checkpoints drop time_embedder.* and bake adaln_proj to F16,
        # and adaln_proj is exactly what the LoRA adapts.
        "unet": "minimax_h3_ref2va_int8_convrot.safetensors",
        "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "video_vae": "minimax_h3_video_vae_fp16.safetensors",
        "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
        "lora": "minimax_h3_turbo_4step_ckpt500.safetensors",
        "requires_node": "MiniMaxH3TurboSampler",
        "probe": ("UNETLoader", "unet_name", "minimax_h3_ref2va_int8_convrot.safetensors"),
        "models": [
            _model("Comfy-Org/MiniMax-H3",
                   "diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors",
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
    "minimax-h3-ref-turbo-lx2v": {
        "key": "minimax-h3-ref-turbo-lx2v",
        "label": "MiniMax H3 33B Ref2VA Turbo (LightX2V)",
        "sub": "Faster Ref2VA · Ref2VA-trained 4-step LoRA · softer than base · restricted license",
        "family": "minimax",
        "reference": True,
        "commercial_ok": True,
        "license": "MiniMax H3 Community License",
        "license_note": ("Not licensed for use in the USA, EU, UK or South Korea. "
                         "Requires machine-generated disclosure and “MiniMax H3” "
                         "attribution; separate authorization above US$20M yearly revenue."),
        # Same graph and base as minimax-h3-ref-turbo; the LoRA is the difference.
        # ckpt500 is an FL2VA/T2VA distill pointed at Ref2VA, which is where its
        # over-sharpened look comes from; LightX2V publish one trained ON Ref2VA.
        # Measured on s1, one acted 14 s scene (704×1280, 345 frames, same seed,
        # two speakers, two portraits + two voice refs) against the other two
        # reference engines:
        #   w4a8 (default)  40.2 min · speech 0.938 · edge energy 3.40
        #   ref-turbo       19.7 min · speech 0.969 · edge energy 5.16  ← the crunch
        #   this engine     19.6 min · speech 0.969 · edge energy 2.76
        # So: half the wall clock of w4a8 at equal-or-better spoken accuracy, and
        # the distillation crunch is gone. It errs SOFT rather than sharp, and
        # drifted further from the reference wardrobe than w4a8 did — one scene at
        # one seed, which is why this is opt-in and w4a8 stays the default.
        "workflow": "h3_ref2v_turbo.json",
        "steps": 4,
        "lease_seconds": 14400,
        # Non-pruned DiT as with ref-turbo. This LoRA does not adapt adaln_proj at
        # all (so the pruned bases would not break it), but it was distilled on the
        # full checkpoint and that is what the measurement above used.
        "unet": "minimax_h3_ref2va_int8_convrot.safetensors",
        "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "video_vae": "minimax_h3_video_vae_fp16.safetensors",
        "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
        # DERIVED from the downloaded file, not downloaded under this name:
        # LightX2V ship generic-ComfyUI keys (diffusion_model.blocks.N…) while
        # MiniMaxH3TurboLoRA builds its key map from bare module names, so the
        # prefix is stripped at install time (scripts/download_models.sh). Loaded
        # unstripped the node matches nothing and silently renders 4 steps with no
        # LoRA at all, so the installer writes this name only after converting.
        "lora": "minimax_h3_ref2v_turbo_4step_v0.1_h3node.safetensors",
        "lora_source": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
        "requires_node": "MiniMaxH3TurboSampler",
        "probe": ("UNETLoader", "unet_name", "minimax_h3_ref2va_int8_convrot.safetensors"),
        "models": [
            _model("Comfy-Org/MiniMax-H3",
                   "diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors",
                   "models/diffusion_models"),
            _model("Comfy-Org/MiniMax-H3",
                   "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                   "models/text_encoders"),
            _model("Comfy-Org/MiniMax-H3",
                   "vae/minimax_h3_video_vae_fp16.safetensors", "models/vae"),
            _model("Comfy-Org/MiniMax-H3",
                   "vae/minimax_h3_audio_vae_fp32.safetensors", "models/vae"),
            _model("lightx2v/Minimax-h3-Turbo",
                   "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
                   "models/loras"),
        ],
    },
    "minimax-h3-ref-w4a8": {
        "key": "minimax-h3-ref-w4a8",
        "label": "MiniMax H3 33B Ref2VA W4A8",
        "sub": "Experimental 4-bit weights · full step count, no distillation · restricted license",
        "family": "minimax",
        "reference": True,
        "commercial_ok": True,
        "license": "MiniMax H3 Community License",
        "license_note": ("Not licensed for use in the USA, EU, UK or South Korea. "
                         "Requires machine-generated disclosure and “MiniMax H3” "
                         "attribution; separate authorization above US$20M yearly revenue."),
        # Same graph as the base ref engine: multi-step sampling with EasyCache,
        # NOT the turbo LoRA path (w4a8 is a pruned checkpoint and the distill
        # LoRA only fits the non-pruned DiT). The point of this engine is base
        # fidelity at a lower memory cost — speed without the distillation look.
        "workflow": "h3_ref2v.json",
        "steps": 15,
        "easycache_threshold": 0.2,
        "lease_seconds": 14400,
        "unet": "minimax_h3_ref2va_pruned_w4a8_mixed.safetensors",
        "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "video_vae": "minimax_h3_video_vae_fp16.safetensors",
        "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
        "requires_node": "MiniMaxH3ReferenceToVideo",
        # 4-bit weights need loader support landed in ComfyUI 0.31.0. On an
        # older worker the render SUCCEEDS and returns black frames, so this is
        # a hard gate rather than a warning.
        "min_comfyui": (0, 31, 0),
        "probe": ("UNETLoader", "unet_name",
                  "minimax_h3_ref2va_pruned_w4a8_mixed.safetensors"),
        "models": [
            _model("Kijai/MiniMax-H3-experimental",
                   "minimax_h3_ref2va_pruned_w4a8_mixed.safetensors",
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
}

DEFAULT_VIDEO_ENGINE = "ltx25"
# Acted scenes render through a Ref2VA engine
# instead of the style's I2V engine; this is the default when none is stamped.
# w4a8 over turbo, measured on one shot at one seed: same wall clock (~6.6 min
# vs ~6), but 15 real steps instead of a 4-step distillation — sharpness 4.2 vs
# turbo's 5.5 (base scores 4.4), which is the over-sharpened look going away.
# Needs ComfyUI >= 0.31.0 fleet-wide; older workers are refused, not silently
# given black frames (see comfyui.check_engine_supported).
DEFAULT_REFERENCE_ENGINE = "minimax-h3-ref-w4a8"


def get_video(key: str) -> dict | None:
    return VIDEO_ENGINES.get((key or "").strip())


def resolve_video(cfg: dict, key: str) -> dict:
    """Return the video-engine dict for *key*, falling back to the default.

    *cfg* (a style/job settings dict) may carry ``video_steps`` — a per-style
    steps override for single-pass engines (the MiniMax family; entries with a
    ``steps`` field). 0/absent keeps the engine default; LTX has no ``steps``
    and ignores it."""
    eng = dict(get_video(key) or VIDEO_ENGINES[DEFAULT_VIDEO_ENGINE])
    try:
        steps = int(cfg.get("video_steps") or 0) if isinstance(cfg, dict) else 0
    except (TypeError, ValueError):
        steps = 0
    if steps > 0 and eng.get("steps"):
        eng["steps"] = steps
    return eng


def resolve_reference(cfg: dict, key: str) -> dict:
    """Return the Ref2VA engine dict for *key* (performance films), falling back
    to DEFAULT_REFERENCE_ENGINE. Only ``reference`` engines are eligible — an
    I2V key here would be a mis-stamped job, not a usable graph."""
    eng = get_video(key)
    if not eng or not eng.get("reference"):
        eng = VIDEO_ENGINES[DEFAULT_REFERENCE_ENGINE]
    eng = dict(eng)
    try:
        steps = int(cfg.get("video_steps") or 0) if isinstance(cfg, dict) else 0
    except (TypeError, ValueError):
        steps = 0
    if steps > 0 and eng.get("steps"):
        eng["steps"] = steps
    return eng


def _public_video(e: dict) -> dict:
    return {
        "key": e["key"], "label": e["label"], "sub": e["sub"],
        "commercial_ok": e["commercial_ok"], "license": e["license"],
        "license_note": e.get("license_note"),
        "downloadable": bool(e.get("models")),
    }


def public_list_video() -> list[dict]:
    """Compact scene-I2V engine descriptors for the Settings UI. Ref2VA engines
    are excluded — they take portraits, not a first frame, and are picked by the
    performance-film setting instead."""
    return [_public_video(e) for e in VIDEO_ENGINES.values() if not e.get("reference")]


def public_list_reference() -> list[dict]:
    """Compact Ref2VA engine descriptors (performance films)."""
    return [_public_video(e) for e in VIDEO_ENGINES.values() if e.get("reference")]


# ── Music engines (per-style ``music_engine``) ───────────────────────────────
# The background-music bed, generated once per film and mixed under the
# narration. ACE-Step 1.5 Turbo is the default; MiniMax Music 3 is opt-in and
# needs newer ComfyUI nodes. Both graphs take the same three placeholders
# (TAGS/DURATION/SEED), so comfyui.generate_music only swaps the workflow file.
MUSIC_ENGINES: dict[str, dict] = {
    "ace-step": {
        "key": "ace-step",
        "label": "ACE-Step 1.5 Turbo",
        "sub": "Fast · 8 steps · instrumental beds · commercial OK",
        "workflow": "ace_music.json",
        # Repaint-extend: keep an existing take's audio verbatim and generate
        # only the added tail ("re-generate longer" without losing the song).
        # Needs the AudioLatentExtendMask custom node on the worker — the
        # caller probes for it and falls back to a fresh generation.
        "extend_workflow": "ace_music_extend.json",
        "extend_node": "AudioLatentExtendMask",
        "commercial_ok": True,
        "license": "Apache-2.0",
        # EmptyAceStep15LatentAudio accepts up to 1000 s — far past any film.
        "max_seconds": 1000.0,
        "timeout": 600,
        "probe": ("UNETLoader", "unet_name", "acestep_v1.5_turbo.safetensors"),
        "models": [
            _model("Comfy-Org/ace_step_1.5_ComfyUI_files",
                   "split_files/diffusion_models/acestep_v1.5_turbo.safetensors",
                   "models/diffusion_models"),
            _model("Comfy-Org/ace_step_1.5_ComfyUI_files",
                   "split_files/vae/ace_1.5_vae.safetensors", "models/vae"),
            _model("Comfy-Org/ace_step_1.5_ComfyUI_files",
                   "split_files/text_encoders/qwen_0.6b_ace15.safetensors", "models/text_encoders"),
            _model("Comfy-Org/ace_step_1.5_ComfyUI_files",
                   "split_files/text_encoders/qwen_4b_ace15.safetensors", "models/text_encoders"),
        ],
    },
    "minimax-music3": {
        "key": "minimax-music3",
        "label": "MiniMax Music 3",
        "sub": "Slow · 30 steps · song-shaped · restricted license",
        "workflow": "minimax_music.json",
        "commercial_ok": True,
        "license": "MiniMax-Music3 Community License",
        "license_note": ("Requires a visible “MiniMax-Music3” credit on any commercial "
                         "product using it, and machine-generated disclosure; separate "
                         "authorization above US$20M yearly revenue."),
        # The AR stage caps at MAX_AUDIO_FRAMES/AUDIO_FRAMES_PER_SECOND = 9000/25.
        # The model is trained to ~5 min, so anything past that is a hard stop —
        # generate_music clamps and the mix loops the bed over a longer film.
        "max_seconds": 360.0,
        # An 8B autoregressive pass plus 30 DiT steps: minutes, not seconds.
        "timeout": 1800,
        # Nodes landed in ComfyUI v0.33.0 — older workers have no graph at all
        # (a clean "node not found", not black frames, but gate it anyway so the
        # Settings availability chip can say "worker needs a rebuild").
        "requires_node": "MiniMaxMusic3TextEncode",
        "min_comfyui": (0, 33, 0),
        "too_old_note": "its nodes are not registered there",
        "probe": ("UNETLoader", "unet_name", "minimax_music3_dit_fp16.safetensors"),
        "models": [
            _model("Comfy-Org/MiniMax-Music-3",
                   "diffusion_models/minimax_music3_dit_fp16.safetensors",
                   "models/diffusion_models"),
            _model("Comfy-Org/MiniMax-Music-3",
                   "text_encoders/minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
                   "models/text_encoders"),
            _model("Comfy-Org/MiniMax-Music-3",
                   "vae/minimax_music3_dav.safetensors", "models/vae"),
        ],
    },
}

DEFAULT_MUSIC_ENGINE = "ace-step"


def get_music(key: str) -> dict | None:
    return MUSIC_ENGINES.get((key or "").strip())


def resolve_music(key: str) -> dict:
    """Return the music-engine dict for *key*, falling back to the default."""
    return dict(get_music(key) or MUSIC_ENGINES[DEFAULT_MUSIC_ENGINE])


def public_list_music() -> list[dict]:
    """Compact music-engine descriptors for the Settings UI."""
    return [_public_video(e) for e in MUSIC_ENGINES.values()]
