"""Image-engine registry: selectable model bundles for generation and editing.

An *engine* is a bundle of {ComfyUI workflow + text encoder + model files + steps
+ license}. The user-facing choice is an engine **key**, not a raw filename,
because FLUX.1 and FLUX.2 use different ComfyUI graphs and encoders. Per-style
settings carry ``image_engine`` (scene generation) and ``edit_engine`` (the
"Edit image" inpaint); both fall back to ``flux1-schnell``.

Validation status:
- ``flux1-schnell``  — the existing, validated path (text→image + masked img2img).
- ``flux1-fill``     — dedicated FLUX.1 Fill inpaint; standard ComfyUI Fill graph.
- ``flux2-dev``      — authored from the public ComfyUI FLUX.2 templates; the
  workflow node graph and download repo ids **need first-run validation on the
  cluster** (sampler/edit wiring and exact HF repo names may need a tweak).
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
    "flux1-fill": {
        "key": "flux1-fill",
        "label": "FLUX.1 Fill",
        "sub": "Dedicated inpaint · best edges · non-commercial",
        "family": "flux1",
        "can_generate": False,
        "can_edit": True,
        "edit_mode": "fill",
        "t2i_workflow": None,
        "edit_workflow": "flux_fill.json",
        "steps": 20,
        "edit_denoise": 1.0,
        "guidance": 30.0,
        "commercial_ok": False,
        "license": "FLUX.1 [dev] Non-Commercial",
        "model_file": "flux1-fill-dev.safetensors",
        "clip_t5": "t5xxl_fp8_e4m3fn.safetensors",
        "clip_l": "clip_l.safetensors",
        "vae": "ae.safetensors",
        "probe": ("UNETLoader", "unet_name", "flux1-fill-dev.safetensors"),
        "models": [
            _model("black-forest-labs/FLUX.1-Fill-dev", "flux1-fill-dev.safetensors",
                   "models/diffusion_models", gated=True),
            _model(*_FLUX1_T5), _model(*_FLUX1_CLIP_L), _model(*_FLUX1_VAE),
        ],
    },
    "flux2-dev": {
        "key": "flux2-dev",
        "label": "FLUX.2 dev",
        "sub": "Higher quality · ~28 steps · non-commercial · needs validation",
        "family": "flux2",
        "can_generate": True,
        "can_edit": True,
        "edit_mode": "flux2",
        "t2i_workflow": "flux2_t2i.json",
        "edit_workflow": "flux2_edit.json",
        "steps": 28,
        "edit_denoise": 1.0,
        "guidance": 4.0,
        "commercial_ok": False,
        "license": "FLUX.2 [dev] Non-Commercial",
        "model_file": "flux2_dev_fp8mixed.safetensors",
        "clip_t5": "mistral_3_small_flux2_bf16.safetensors",   # single Mistral-3 encoder
        "clip_l": None,
        "vae": "flux2-vae.safetensors",
        "probe": ("UNETLoader", "unet_name", "flux2_dev_fp8mixed.safetensors"),
        # NOTE: repo ids below need verification against the live ComfyUI FLUX.2 release.
        "models": [
            _model("Comfy-Org/flux2-dev", "split_files/diffusion_models/flux2_dev_fp8mixed.safetensors",
                   "models/diffusion_models", gated=True),
            _model("Comfy-Org/flux2-dev", "split_files/text_encoders/mistral_3_small_flux2_bf16.safetensors",
                   "models/text_encoders"),
            _model("Comfy-Org/flux2-dev", "split_files/vae/flux2-vae.safetensors", "models/vae"),
        ],
    },
}

DEFAULT_ENGINE = "flux1-schnell"


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
