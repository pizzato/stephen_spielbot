"""SPIKE: audio-driven H3 — pin a full audio TRACK across the whole clip.

Appended to ComfyUI-H3-Motion-Context/nodes.py inside the worker container.
Reuses the pack's layout/payload patches and audio encode path: the track is
encoded with the H3 audio VAE and its reference rows are TRANSLATED onto the
clip's own timeline starting at frame 0 (window [0, rt] on the 40 Hz audio
grid), so the model reads it as THIS clip's sound and denoises the picture
against it — singing, rhythm and all. The clip's delivered audio should come
out as (a close reconstruction of) the input track; the input WAV can also be
muxed over the output for bit-exact sound.
"""


class MiniMaxH3AudioTrack:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "conditioning": ("CONDITIONING",),
                "latent": ("LATENT",),
                "audio_vae": ("VAE",),
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("CONDITIONING",)
    FUNCTION = "apply"
    CATEGORY = "conditioning/minimax"
    DESCRIPTION = ("Pin an entire audio track across the clip's own timeline "
                   "so the video is generated to match it (audio-driven "
                   "generation). The track should be at least as long as the "
                   "clip; its last clip-length seconds are used.")

    def apply(self, conditioning, latent, audio_vae, audio):
        _ensure_layout_patch()
        _ensure_payload_patch()
        video = _video_from_latent(latent)
        frame_count = _pixel_frames(int(video.shape[2]))
        seconds = frame_count / float(FPS)
        z, rt = _encode_tail_audio(audio_vae, audio, seconds)
        # Window [0, rt] on the target's audio grid: _fixup_audio places the
        # window END at FRAME_RESCALE * end_frame and its start rt steps
        # earlier, so ending at rt/FRAME_RESCALE starts it exactly at 0 —
        # the track lies along the clip from its first frame.
        end_frame = rt / FRAME_RESCALE
        ref = {
            "kind": "audio",
            "ref_audio_t": rt,
            "audio_latent": z,
            MC_AUDIO_KEY: end_frame,
        }
        out = node_helpers.conditioning_set_values(
            conditioning, {"minimax_refs": [ref]}, append=True)
        _LOG.info("h3_audio_track: pinned %d audio steps (%.2fs) across a "
                  "%d frame (%.2fs) clip", rt, rt / AUDIO_HZ, frame_count,
                  seconds)
        return (out,)


NODE_CLASS_MAPPINGS["MiniMaxH3AudioTrack"] = MiniMaxH3AudioTrack
NODE_DISPLAY_NAME_MAPPINGS["MiniMaxH3AudioTrack"] = "H3 Audio Track (spike)"
