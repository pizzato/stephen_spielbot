"""Keep pi-H3's sampled latent and its transient DWD pixels in the same run."""

from comfy_extras.nodes_custom_sampler import Noise_RandomNoise


class SpielbotDWDNoise:
    """Native seeded noise, with sampling refreshed for every submitted graph.

    piFlow only retains pixels for its latest four samples. A cached sampler
    result can otherwise outlive those pixels and make DWD Decode fail.
    Loader and text/image conditioning caches remain reusable.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"noise_seed": ("INT", {
            "default": 0, "min": 0, "max": 0xffffffffffffffff,
        })}}

    RETURN_TYPES = ("NOISE",)
    FUNCTION = "get_noise"
    CATEGORY = "Spielbot/pi-H3"

    @classmethod
    def IS_CHANGED(cls, noise_seed):
        return float("nan")

    def get_noise(self, noise_seed):
        return (Noise_RandomNoise(noise_seed),)


NODE_CLASS_MAPPINGS = {"SpielbotDWDNoise": SpielbotDWDNoise}
