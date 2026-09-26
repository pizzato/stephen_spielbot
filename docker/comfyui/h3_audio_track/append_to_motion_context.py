"""Audio-driven H3 — pin a full audio TRACK across the whole clip.

Appended to ComfyUI-H3-Motion-Context/nodes.py inside the worker container.
The track is encoded with the H3 audio VAE and handed to stock as a keyframe
whose window starts at frame 0 and runs forward for the clip's length, so the
model reads it as THIS clip's sound and denoises the picture against it.
ComfyUI 0.34+ places that window itself; this node does not patch PackedLayout.
The clip's delivered audio should come out as a close reconstruction of the
input track; the input WAV can also be muxed over the output for bit-exact sound.
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
        _ensure_layout_ok()
        video = _video_from_latent(latent)
        frame_count = _pixel_frames(int(video.shape[2]))
        seconds = frame_count / float(FPS)
        z, rt = _encode_tail_audio(audio_vae, audio, seconds)
        # Stock starts a keyframe's audio window at
        # FRAME_RESCALE * resolved_frame_index and runs forward for the
        # latent's length. Index 0 lays [0, rt] on the clip's own timeline.
        audio_kf = {
            "resolved_frame_index": 0,
            "audio_latent": z,
        }
        out = []
        for emb, extra in conditioning:
            d = extra.copy()
            prior = list(d.get("minimax_keyframes") or [])
            d["minimax_keyframes"] = prior + [audio_kf]
            out.append([emb, d])
        _LOG.info("h3_audio_track: pinned %d audio steps (%.2fs) across a "
                  "%d frame (%.2fs) clip", rt, rt / AUDIO_HZ, frame_count,
                  seconds)
        return (out,)


NODE_CLASS_MAPPINGS["MiniMaxH3AudioTrack"] = MiniMaxH3AudioTrack
NODE_DISPLAY_NAME_MAPPINGS["MiniMaxH3AudioTrack"] = "H3 Audio Track (spike)"
