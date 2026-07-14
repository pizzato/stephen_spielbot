#!/usr/bin/env python3
"""Make a ~N-second speech WAV for the InfiniteTalk spike, using our existing
OpenF5 TTS worker (the container already listening on :8189).

Same voice the app uses, so the lip-sync test reflects real output. Synthesises
sentence by sentence and concatenates with ffmpeg until the target length is
reached (one long F5 call can be unstable), then reports the final duration.

Throwaway helper — stdlib + ffmpeg only, no repo imports.

  python3 make_test_audio.py --seconds 60 --out test_60s.wav
  python3 make_test_audio.py --url http://localhost:8189/tts --seconds 90 --out test_90s.wav
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# A neutral, on-camera monologue. Enough sentences to comfortably exceed 90 s;
# we stop as soon as the target is hit.
SENTENCES = [
    "Hello, and thanks for stopping by.",
    "I want to walk you through something I have been thinking about for a while.",
    "It started as a small idea, but the more I looked into it, the bigger it became.",
    "The interesting part is not the technology itself, but what it lets ordinary people do.",
    "A few years ago, this would have taken a whole team and a serious budget.",
    "Today, one person with a laptop can put together something that genuinely looks professional.",
    "Of course, there are still rough edges, and I will be honest about those too.",
    "The voice can wander, the timing is not always perfect, and long takes drift.",
    "But the direction is clear, and it is moving faster than most people expect.",
    "So stick with me, and let us see how far we can actually push this.",
    "By the end, I think you will be surprised at what holds up and what does not.",
    "Let us get into it.",
]


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _synth(url: str, text: str, out: Path, speed: float, engine: str,
           ref_b64: str | None = None) -> None:
    payload = json.dumps(
        {"text": text, "ref_audio_b64": ref_b64, "speed": speed, "engine": engine}
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        out.write_bytes(resp.read())


def _concat(parts: list[Path], out: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write(f"file '{p.resolve()}'\n")
        listfile = Path(f.name)
    try:
        # re-encode on concat so mismatched wav params don't fail
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(listfile),
             "-ar", "24000", "-ac", "1", str(out)],
            check=True,
        )
    finally:
        listfile.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8189/tts",
                    help="OpenF5 TTS worker endpoint (default: local container)")
    ap.add_argument("--seconds", type=float, default=60.0, help="target length")
    ap.add_argument("--out", default="test_60s.wav")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--engine", default="openf5")
    ap.add_argument("--ref", default=None,
                    help="reference voice clip (wav/mp3/m4a) to clone; omit for default narrator")
    args = ap.parse_args()

    import base64
    ref_b64 = None
    if args.ref:
        ref_b64 = base64.b64encode(Path(args.ref).read_bytes()).decode()

    out = Path(args.out)
    tmpdir = Path(tempfile.mkdtemp(prefix="infinitetalk_audio_"))
    parts: list[Path] = []
    total = 0.0

    print(f"synthesising via {args.url} until >= {args.seconds:.0f}s ...")
    i = 0
    while total < args.seconds:
        text = SENTENCES[i % len(SENTENCES)]
        part = tmpdir / f"part_{i:03d}.wav"
        try:
            _synth(args.url, text, part, args.speed, args.engine, ref_b64)
        except Exception as e:  # noqa: BLE001
            print(f"\nTTS request failed: {e}", file=sys.stderr)
            print("Is the OpenF5 worker up on this host? Try --url http://<host>:8189/tts, "
                  "or pass your own wav to run_spike.sh with --audio.", file=sys.stderr)
            return 1
        d = _duration(part)
        if d <= 0:
            print("got a 0-length clip back; aborting", file=sys.stderr)
            return 1
        parts.append(part)
        total += d
        i += 1
        print(f"  +{d:5.1f}s  (total {total:5.1f}s)  <- {text[:50]}")

    _concat(parts, out)
    final = _duration(out)
    print(f"\nwrote {out}  ({final:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
