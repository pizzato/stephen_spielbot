"""Noise mask for 1-d (audio) latents — the "same song, just longer" node.

Stock SetLatentNoiseMask force-reshapes every mask to 4-dim, which
comfy.utils.reshape_mask cannot interpolate onto an ACE-Step 1.5 [B, 64, T]
audio latent (its dims==1 path wants a 3-dim mask). This node builds the
3-dim mask directly: the first *keep_seconds* of the encoded audio survive
sampling untouched, everything after is regenerated from noise — a repaint
extend, the audio analogue of outpainting. *fade_seconds* ramps the boundary
so the seam doesn't click.

Proven 2026-08-19 on s1: a 20s head extended to 32s keeps the head bit-exact
(waveform corr 0.985) while the tail comes back as new music that lands an
ending. Deployed via docker/comfyui/Dockerfile into custom_nodes/.
"""
import torch


class AudioLatentExtendMask:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "samples": ("LATENT",),
            "keep_seconds": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1000.0, "step": 0.1}),
            "fade_seconds": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 10.0, "step": 0.1}),
        }}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "apply"
    CATEGORY = "latent/audio"

    def apply(self, samples, keep_seconds, fade_seconds):
        s = samples.copy()
        frames = samples["samples"].shape[-1]
        fps = 48000.0 / 1920.0  # ACE-Step 1.5 latent frames per second
        keep = max(0, min(frames, int(round(keep_seconds * fps))))
        mask = torch.ones((1, 1, frames), dtype=torch.float32)
        mask[..., :keep] = 0.0
        fade = int(round(fade_seconds * fps))
        for i in range(fade):
            idx = keep + i
            if idx < frames:
                mask[..., idx] = (i + 1) / (fade + 1)
        s["noise_mask"] = mask
        return (s,)


NODE_CLASS_MAPPINGS = {"AudioLatentExtendMask": AudioLatentExtendMask}
NODE_DISPLAY_NAME_MAPPINGS = {"AudioLatentExtendMask": "Audio Latent Extend Mask"}
