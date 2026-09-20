#!/usr/bin/env python3
"""Score a rendered clip's camera drift, and the edges a fix must not blunt.

Written to settle A/B questions about the H3 turbo engines objectively. The
distilled 4-step LoRAs carry their own motion prior: on locked-off scenes they
swing the frame sideways in the opening seconds however the scene's `camera`
line is written, which no amount of reading the prompt back will show you.

Drift is measured by phase correlation between consecutive frames — the
translation that best aligns frame N-1 onto frame N — cumulatively summed, so
the number reported is where the frame content has travelled to relative to
where the clip started, as a percentage of frame width. Content moving LEFT
(negative) means the camera swung RIGHT. Frames are reduced to 192px-wide
greyscale first: the measurement wants gross translation, not detail.

`edge` is the mean absolute Laplacian over evenly spaced frames — the
over-sharpening the ckpt500 distill was rejected for. A drift fix that also
drops this has traded one artefact for another, which is the whole reason it
is printed next to the drift.

  scripts/video_drift.py clip.mp4 [more.mp4 ...]
  scripts/video_drift.py --seconds 4 --expect "the line it should say" clip.mp4

`--expect` adds the spoken-accuracy score from the shot gate (needs
faster-whisper; skipped with a note when it is not installed), so a take can be
checked for saying its line and holding its frame in one pass.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

WIDTH = 192          # analysis width; gross translation needs nothing finer
EDGE_SAMPLES = 5


def probe(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return int(s["width"]), int(s["height"]), float(num) / float(den)


def grey_frames(path: Path, count: int, w: int, h: int) -> np.ndarray:
    """First *count* frames as a greyscale array, scaled to w x h."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={w}:{h}",
         "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True).stdout
    buf = np.frombuffer(out, dtype=np.uint8)
    n = len(buf) // (w * h)
    return buf[:n * w * h].reshape(n, h, w).astype(np.float32)


def translation(a: np.ndarray, b: np.ndarray) -> float:
    """Horizontal shift in pixels that aligns *a* onto *b* (phase correlation).

    Hann-windowed and mean-removed so the frame edges and overall brightness
    do not dominate the correlation peak.
    """
    win = np.hanning(a.shape[0])[:, None] * np.hanning(a.shape[1])[None, :]
    fa = np.fft.fft2((a - a.mean()) * win)
    fb = np.fft.fft2((b - b.mean()) * win)
    r = fa.conj() * fb
    r /= np.abs(r) + 1e-8
    peak = np.fft.ifft2(r).real
    _, dx = np.unravel_index(np.argmax(peak), peak.shape)
    if dx > a.shape[1] // 2:
        dx -= a.shape[1]
    return float(dx)


def edge_energy(path: Path, seconds: float) -> float | None:
    """Mean |Laplacian| over evenly spaced frames — higher = harder edges."""
    vals = []
    for i in range(EDGE_SAMPLES):
        t = seconds * (i + 0.5) / EDGE_SAMPLES
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True).stdout
        if not raw:
            continue
        w, h, _ = probe(path)
        a = np.frombuffer(raw, dtype=np.uint8)[:w * h].reshape(h, w).astype(float)
        lap = (-4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1]
               + a[1:-1, :-2] + a[1:-1, 2:])
        vals.append(float(np.abs(lap).mean()))
    return round(sum(vals) / len(vals), 2) if vals else None


def score(path: Path, seconds: float) -> dict:
    vw, vh, fps = probe(path)
    h = int(round(WIDTH * vh / vw / 2)) * 2
    frames = grey_frames(path, max(6, int(round(seconds * fps)) + 1), WIDTH, h)
    if len(frames) < 4:
        raise RuntimeError(f"{path}: too few frames to measure")
    dx = np.array([translation(frames[i - 1], frames[i])
                   for i in range(1, len(frames))])
    cum = np.cumsum(dx)
    duration = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout or 0)
    return {
        "clip": path.name,
        "size": f"{vw}x{vh}",
        "fps": round(fps, 2),
        "seconds": round(duration, 2),
        # The excursion: how far the frame travelled from where it started.
        "peak_left_pct": round(float(100 * cum.min() / WIDTH), 1),
        "peak_right_pct": round(float(100 * cum.max() / WIDTH), 1),
        "end_pct": round(float(100 * cum[-1] / WIDTH), 1),
        "edge": edge_energy(path, min(seconds, duration or seconds)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clips", nargs="+", type=Path)
    ap.add_argument("--seconds", type=float, default=2.0,
                    help="window measured from the clip start (default 2)")
    ap.add_argument("--expect", default="",
                    help="line the take should say — adds the shot gate's score")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    gate = None
    if args.expect:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from pipeline import shot_gate
        if shot_gate.available():
            gate = shot_gate
        else:
            print("# faster-whisper not installed — speech score skipped",
                  file=sys.stderr)

    rows = []
    for clip in args.clips:
        row = score(clip, args.seconds)
        if gate is not None:
            row["speech"] = round(gate.verify(clip, args.expect)[0], 3)
        rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    head = f"{'clip':<28} {'size':>9} {'secs':>6} {'peak←':>7} {'peak→':>7} {'end':>7} {'edge':>6}"
    if gate is not None:
        head += f" {'speech':>7}"
    print(head)
    for r in rows:
        line = (f"{r['clip'][:28]:<28} {r['size']:>9} {r['seconds']:>6.2f} "
                f"{r['peak_left_pct']:>+7.1f} {r['peak_right_pct']:>+7.1f} "
                f"{r['end_pct']:>+7.1f} {r['edge'] or 0:>6.2f}")
        if gate is not None:
            line += f" {r['speech']:>7.3f}"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
