"""Real ffmpeg regression: loud converted vocals must not clip before remixing."""
from pathlib import Path
import shutil
import subprocess
from unittest import mock

import numpy as np
import pytest

from pipeline import svc

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


def _tone(path: Path, amplitude: float, hz: int):
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                    f"aevalsrc={amplitude}*sin(2*PI*{hz}*t):s=44100:d=0.25",
                    "-c:a", "pcm_f32le", str(path)], check=True)


def _samples(path: Path):
    data = subprocess.run([FFMPEG, "-v", "error", "-i", str(path),
                           "-f", "f32le", "-c:a", "pcm_f32le", "-"],
                          capture_output=True, check=True).stdout
    return np.frombuffer(data, dtype="<f4")


def test_gain_and_mix_preserve_loud_vocal_shape_and_full_timeline(tmp_path):
    vocal, backing, output = [tmp_path / name for name in ("v.wav", "b.wav", "out.wav")]
    _tone(vocal, 0.8, 440)
    _tone(backing, 0.4, 880)
    original_backing = backing.read_bytes()
    original_vocal = _samples(vocal)
    with mock.patch.object(svc, "_ffmpeg", return_value=FFMPEG), \
         mock.patch.object(svc, "_mean_volume", return_value=-12):
        svc._match_gain(vocal, to=0)
        boosted = _samples(vocal)
        assert np.max(np.abs(boosted)) > 3  # retained headroom, not clipped to 1
        np.testing.assert_allclose(boosted, original_vocal * 10 ** (12 / 20), atol=1e-6)
        expected = boosted + _samples(backing)
        svc._remix(vocal, backing, output)
    result = _samples(output)
    assert len(result) == 11025
    assert np.isfinite(result).all()
    assert 0.88 < np.max(np.abs(result)) <= 10 ** (-1 / 20) + 1e-5
    gain = np.dot(result, expected) / np.dot(expected, expected)
    np.testing.assert_allclose(result, expected * gain, atol=1e-6)
    assert backing.read_bytes() == original_backing
