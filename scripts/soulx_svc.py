#!/usr/bin/env python3
"""SoulX-SVC subprocess entry point; run only with its isolated environment.

Uses the tested SVC checkpoint, source audio and RMVPE pitch. No SVS lyric
alignment, transcription or pitch shifting is involved. Heavy imports stay out
of the application process.
"""
from __future__ import annotations

import argparse
import gc
import importlib.util
import math
import os
from pathlib import Path
import sys
import tempfile


def reference_clip(audio, sample_rate: int):
    """Select up to ten seconds with the most active speech, as in the A/B test."""
    import numpy as np

    length = 10 * sample_rate
    if len(audio) <= length:
        return audio
    frame = max(1, round(sample_rate * 0.05))
    frames = audio[:len(audio) // frame * frame].reshape(-1, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    active = rms > max(0.008, float(rms.max()) * 0.1)
    scores = np.convolve(active.astype(float), np.ones(length // frame), mode="valid")
    start = int(scores.argmax()) * frame
    return audio[start:start + length]


def bounded_segments(overlaps, segments, duration: float):
    """Keep upstream vocal boundaries, splitting any overlong inference window.

    Whisper accepts at most 30 seconds. Upstream can merge a short span with a
    30-second sustained note, or fall back to the entire file when F0 is absent.
    Smaller overlapping windows avoid truncating its content conditioning.
    """
    result = []
    pairs = list(zip(overlaps, segments)) or [((0.0, duration), (0.0, duration))]
    for (context_start, context_end), (start, end) in pairs:
        end = min(end, duration)
        if context_end - context_start < 30:
            result.append((context_start, min(context_end, duration), start, end))
            continue
        while start < end:
            stop = min(start + 28, end)
            result.append((max(context_start, start - 0.5),
                           min(context_end, duration, stop + 0.5), start, stop))
            start = stop
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--diffusion-steps", type=int, default=32)
    args = parser.parse_args()
    root = args.model_dir.resolve()
    os.environ["HF_HOME"] = str(root / "hf-cache")
    os.environ["HF_HUB_CACHE"] = str(root / "hf-cache/hub")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    sys.path.insert(0, str(root))

    import numpy as np
    import soundfile as sf
    import torch
    from scipy.signal import resample_poly

    from cli.inference_svc import build_model
    from soulxsinger.models.soulxsinger_svc import SoulXSingerSVC
    from soulxsinger.utils.file_utils import load_config

    if not 1 <= args.diffusion_steps <= 200:
        raise ValueError("Diffusion steps must be between 1 and 200.")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    fp16 = device == "cuda"
    torch.manual_seed(123)
    np.random.seed(123)
    config = load_config(root / "soulxsinger/config/soulxsinger.yaml")
    rate, hop = config.audio.sample_rate, config.audio.hop_size

    def load(path):
        audio, original_rate = sf.read(path, dtype="float32", always_2d=True)
        audio = audio.mean(axis=1)
        if not len(audio) or not np.isfinite(audio).all():
            raise ValueError(f"Empty or invalid audio: {path.name}")
        if original_rate != rate:
            divisor = math.gcd(original_rate, rate)
            audio = resample_poly(audio, rate // divisor, original_rate // divisor)
        return np.ascontiguousarray(audio, dtype=np.float32)

    source = load(args.source)
    prompt = reference_clip(load(args.target), rate)
    if len(prompt) < rate or float(np.max(np.abs(prompt))) < 1e-5:
        raise ValueError("Voice reference needs at least one second of audible speech.")
    if len(source) < hop:
        raise ValueError("Source vocal is too short for conversion.")

    # Import RMVPE directly: preprocess.tools imports unrelated SVS/ASR models.
    spec = importlib.util.spec_from_file_location(
        "soulx_svc_f0", root / "preprocess/tools/f0_extraction.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    extractor = module.F0Extractor(
        str(root / "pretrained_models/SoulX-Singer-Preprocess/rmvpe/rmvpe.pt"),
        device=device, max_duration=max(300, math.ceil(len(source) / rate) + 1))
    with tempfile.TemporaryDirectory() as td:
        temporary = Path(td)
        for name, audio in (("source", source), ("prompt", prompt)):
            sf.write(temporary / f"{name}.wav", audio, rate, subtype="FLOAT")
        source_f0 = extractor.process(str(temporary / "source.wav")).astype("float32")
        prompt_f0 = extractor.process(str(temporary / "prompt.wav")).astype("float32")
    del extractor
    gc.collect()
    if fp16:
        torch.cuda.empty_cache()

    model = build_model(str(root / "pretrained_models/SoulX-Singer/model-svc.pt"),
                        config, device=device, use_fp16=fp16)
    prompt_tensor = torch.from_numpy(prompt).unsqueeze(0).to(device)
    prompt_pitch = torch.from_numpy(prompt_f0).unsqueeze(0).to(device)

    def infer(audio, pitch):
        with torch.inference_mode():
            generated, shift = model.infer(
                pt_wav=prompt_tensor,
                gt_wav=torch.from_numpy(audio).unsqueeze(0).to(device),
                pt_f0=prompt_pitch,
                gt_f0=torch.from_numpy(pitch).unsqueeze(0).to(device),
                auto_shift=False, pitch_shift=0, n_steps=args.diffusion_steps,
                cfg=1.0, use_fp16=fp16)
        result = generated.reshape(-1).float().cpu().numpy()
        if shift != 0 or len(result) != len(audio) or not np.isfinite(result).all():
            raise RuntimeError("SoulX-SVC produced invalid audio duration, pitch shift or samples.")
        return result

    overlaps, segments = [], []
    if len(source) >= 30 * rate:
        overlaps, segments = SoulXSingerSVC.build_vocal_segments(
            source_f0, f0_rate=rate // hop, uv_frames_th=10,
            min_duration_sec=15.0, max_duration_sec=30.0)
    if len(source) < 30 * rate or (overlaps and all(b - a <= 30 for a, b in overlaps)):
        # The same inference path and settings as the listening comparison.
        generated = infer(source, source_f0)
    else:
        print("Using bounded windows for sustained or unpitched vocals.", flush=True)
        generated = np.zeros_like(source)
        for left, right, start, end in bounded_segments(overlaps, segments, len(source) / rate):
            lo, hi = round(left * rate), round(right * rate)
            first, last = round(start * rate), round(end * rate)
            pitch = source_f0[round(left * rate / hop):round(right * rate / hop)]
            chunk = infer(source[lo:hi], pitch)
            generated[first:last] = chunk[first - lo:last - lo]
    args.output.mkdir(parents=True, exist_ok=True)
    # Float WAV retains headroom until the controller level-matches and remixes.
    sf.write(args.output / "generated.wav", generated, rate, subtype="FLOAT")
    print(f"SoulX-SVC: {len(generated) / rate:.3f}s, {device}, "
          f"{args.diffusion_steps} steps, original pitch preserved", flush=True)


if __name__ == "__main__":
    main()
