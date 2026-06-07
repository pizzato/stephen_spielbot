#!/usr/bin/env python3
"""Record a voice reference sample for voice cloning.

Records from the default microphone using ffmpeg.
Aim for 15-30 seconds, speaking naturally and clearly.

Usage:
    python record_voice.py                        # saves to my_voice.wav
    python record_voice.py --out studio_voice.wav
    python record_voice.py --seconds 20
"""

import argparse
import subprocess
import sys
from pathlib import Path

SAMPLE_TEXT = """
In the beginning, the universe was filled with light and heat beyond imagination.
Stars were born from vast clouds of gas and dust, burning brightly across the cosmos.
Over billions of years, these stars lived and died, seeding the galaxy with the
elements that would one day form planets, oceans, and life itself.
The story of the cosmos is also the story of us — written in the stars above.
"""


def record(output_path: Path, seconds: int) -> None:
    print("\nVoice Reference Recorder")
    print("=" * 40)
    print(f"Recording {seconds} seconds to: {output_path}")
    print()
    print("Suggested text to read aloud:")
    print("-" * 40)
    print(SAMPLE_TEXT.strip())
    print("-" * 40)
    print()
    print("Press ENTER when you're ready to start recording…")
    input()

    # Try pulse first, fall back to alsa
    for audio_input in [("pulse", "default"), ("alsa", "default")]:
        fmt, dev = audio_input
        cmd = [
            "ffmpeg", "-y",
            "-f", fmt, "-i", dev,
            "-ar", "22050", "-ac", "1",
            "-t", str(seconds),
            str(output_path),
        ]
        print(f"Recording… ({seconds}s) Speak now!")
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0:
            break
        # Try next input method
    else:
        print("[ERROR] Could not open microphone via pulse or alsa.", file=sys.stderr)
        print("Try manually: arecord -f cd -t wav -d 30 my_voice.wav", file=sys.stderr)
        sys.exit(1)

    print(f"\nDone! Saved to: {output_path}")
    size_kb = output_path.stat().st_size // 1024
    print(f"File size: {size_kb} KB")
    print()
    print("Next step — test voice cloning:")
    print('  source ~/github/comfyui-env/bin/activate')
    print(f'  python -m pipeline.tts_xtts --ref {output_path} --text "Testing my cloned voice." --out test_clone.wav')
    print()
    print("Then use in video generation:")
    print(f'  python generate_video.py "Your Topic" --voice-ref {output_path}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Record voice reference for cloning.")
    parser.add_argument("--out", type=Path, default=Path("my_voice.wav"), help="Output file (default: my_voice.wav)")
    parser.add_argument("--seconds", type=int, default=30, help="Recording length in seconds (default: 30)")
    args = parser.parse_args()

    record(args.out, args.seconds)
