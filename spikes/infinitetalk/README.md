# InfiniteTalk spike (throwaway test)

**Goal:** find out — on *one* real worker — whether InfiniteTalk is viable for our
"characters speak, consistent voice, long scene" use case, before we integrate anything.

This folder is a **disposable experiment**. Nothing here imports from `pipeline/` or
`app.py`, and nothing in the app imports from here. Delete it when you're done:
`rm -rf spikes/infinitetalk`.

It answers four questions with real numbers instead of my estimates:

1. **Does it even run** on the GB10 / DGX-Spark (arm64, CUDA 13) fleet?  ← biggest risk, see below
2. **Peak VRAM** at 480p and 720p.
3. **Max length** before it OOMs or visibly degrades (InfiniteTalk's own docs warn
   "beyond 1 minute, color shifts become more pronounced" — so the ~60 s test is the
   interesting one).
4. **Lip-sync + identity quality** when driven by *our* OpenF5 cloned voice.

What InfiniteTalk is: audio-driven talking video on a **Wan2.1-I2V-14B** base, Apache-2.0,
`--mode streaming` for long/"unlimited" length. You give it **one portrait + one audio wav**
and it lip-syncs the face to the audio. (Repo: https://github.com/MeiGen-AI/InfiniteTalk)

---

## ⚠️ The #1 risk: the arm64 / CUDA-13 install

The official install pins **torch 2.4.1 (cu121) + flash-attn 2.7.4 + xformers 0.0.28**, which
are **x86_64 / CUDA-12 wheels**. On a GB10 (Grace-Blackwell, aarch64, CUDA 13) those will
**not** `pip install` as-is. `setup.sh` detects this and stops with guidance rather than
installing the wrong wheels.

**If you can't get torch + attention to build on the GB10, that is itself the finding** — it
means InfiniteTalk needs containerisation work (an arm64/CUDA-13 PyTorch base image) before
it's fleet-viable, and we'd weigh that against staying on LTX. Two escape hatches if the bare
install fights you:

- **Reuse the ComfyUI container** (recommended fallback): your ComfyUI already runs on the
  GB10, so its torch/attention stack is proven on this arch. Install kijai's
  `ComfyUI-WanVideoWrapper` (it supports InfiniteTalk/MultiTalk) into that container and run an
  InfiniteTalk workflow through the ComfyUI API instead. More setup, but sidesteps the wheel
  problem entirely and matches our eventual integration path. This spike doesn't script that —
  do it only if the CLI route below can't install.
- **fp8 + low-VRAM flags** (`--fp8 --lowvram`) if it installs but OOMs.

---

## Prerequisites (on the worker)

- The InfiniteTalk repo, a Python env, and ~40 GB of weights (Wan2.1-I2V-14B + InfiniteTalk +
  wav2vec) — `setup.sh` fetches all of it.
- `ffmpeg` / `ffprobe` and `nvidia-smi` on PATH (you already have these).
- A **portrait image** of a character (reuse one your Characters tab already generated, or any
  face PNG).
- For the audio: either bring your own speech wav, or let `make_test_audio.py` synthesise a
  ~60 s clip from your **existing OpenF5 TTS worker** (the container already listening on
  `:8189`) — same voice the app uses.

## Run it (3 steps, on one worker)

```bash
# 0. copy this folder to the worker (from the controller)
rsync -a spikes/infinitetalk/ s1:~/infinitetalk_spike/spike/

# then, ON the worker:
cd ~/infinitetalk_spike/spike

# 1. install repo + env + weights (reads the arm64 warning; ~40 GB download)
bash setup.sh

# 2. make a ~60 s test wav from our OpenF5 voice (or skip and pass your own --audio)
python3 make_test_audio.py --seconds 60 --out test_60s.wav
#    (defaults to http://localhost:8189/tts — the local TTS container)

# 3. run the measured generation (portrait + audio -> talking video + a metrics report)
bash run_spike.sh --image /path/to/portrait.png --audio test_60s.wav --size infinitetalk-480
#    add  --lowvram  and/or  --fp8  if it OOMs;  --size infinitetalk-720  for the 720p VRAM number
```

`run_spike.sh` starts the generation, samples GPU memory once a second while it runs, times it,
then `ffprobe`s the output. It prints a summary like:

```
── InfiniteTalk spike result ─────────────────────────────
 requested : streaming, max_frame_num=1000 (~40s), 480p, 40 steps
 input     : audio 61.2s
 OUTPUT    : 61.0s  832x480  25fps   -> /…/infinitetalk_spike_out/infinitetalk_res.mp4
 peak VRAM : 31544 MiB
 wall-clock: 742s   (12m22s)   exit=0
──────────────────────────────────────────────────────────
```

## What to look at (the actual test)

- **Ran at all?** exit=0 and a playable mp4 → the arm64 stack works. That alone is a big yes/no.
- **Peak VRAM** vs the GB10's budget — and whether `--lowvram`/`--fp8` were needed. Record 480p
  *and* 720p.
- **Length:** push `--frames` up (e.g. `--frames 1500` ≈ 60 s, `--frames 3000` ≈ 120 s) and see
  where it OOMs or the wall-clock becomes impractical.
- **Quality (watch the mp4):** is the mouth actually synced to the words? Does the face stay the
  same person start-to-finish? **Does colour/skin shift after ~60 s?** (their known weak point —
  this decides whether "one 2-minute take" is realistic or whether we cap shots at ~45–60 s.)

Jot those numbers back to me and we'll decide InfiniteTalk vs MultiTalk vs staying on LTX with
real data.
