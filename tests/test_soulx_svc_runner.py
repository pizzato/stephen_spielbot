"""Audio preparation remains safe for silence, sustained notes and long references."""
import unittest

import numpy as np

from scripts.soulx_svc import bounded_segments, reference_clip


class SoulXPreparationTests(unittest.TestCase):
    def assert_complete_bounded_coverage(self, windows, samples, rate):
        coverage = np.zeros(samples, dtype=np.uint8)
        for left, right, start, end in windows:
            lo, hi = round(left * rate), round(right * rate)
            first, last = round(start * rate), round(end * rate)
            self.assertTrue(0 <= lo <= first < last <= hi <= samples)
            self.assertLess(hi - lo, 30 * rate)
            coverage[first:last] += 1
        # Each source sample is emitted once: context may overlap, output may not.
        self.assertTrue(np.all(coverage == 1))

    def test_silence_without_upstream_segments_covers_the_whole_recording(self):
        rate, samples = 24000, 65 * 24000 + 321
        windows = bounded_segments([], [], samples / rate)
        self.assertGreater(len(windows), 1)
        self.assert_complete_bounded_coverage(windows, samples, rate)

    def test_sustained_note_with_overlong_upstream_window_keeps_exact_coverage(self):
        rate, samples = 24000, 65 * 24000 + 321
        # Pinned upstream output for 3 seconds unvoiced followed by a held note:
        # the short initial span merges with a 30-second span into 32.9 seconds.
        # F0 ends on its 20 ms grid, just beyond the final audio sample.
        spans = [(0.0, 32.9), (32.9, 62.9), (62.9, 65.02)]
        windows = bounded_segments(spans, spans, samples / rate)
        self.assertGreater(len(windows), len(spans))
        self.assert_complete_bounded_coverage(windows, samples, rate)

    def test_ordinary_upstream_vocal_boundaries_and_context_are_unchanged(self):
        overlaps = [(0.0, 15.72), (12.82, 32.16), (30.16, 49.68), (47.06, 65.12)]
        segments = [(0.0, 15.72), (15.72, 32.16), (32.16, 49.68), (49.68, 65.12)]
        windows = bounded_segments(overlaps, segments, 65.12)
        self.assertEqual(windows, [(*context, *output) for context, output in zip(overlaps, segments)])
        self.assert_complete_bounded_coverage(windows, round(65.12 * 24000), 24000)

    def test_reference_selects_ten_active_seconds_from_a_long_recording(self):
        rate = 24000
        recording = np.zeros(50 * rate, dtype=np.float32)
        active = 0.2 * np.sin(2 * np.pi * 173 * np.arange(10 * rate) / rate)
        recording[22 * rate:32 * rate] = active
        recording[:rate // 20] = 0.9  # A louder isolated transient is a worse reference.
        selected = reference_clip(recording, rate)
        self.assertEqual(len(selected), 10 * rate)
        np.testing.assert_array_equal(selected, recording[22 * rate:32 * rate])

    def test_short_reference_is_retained_without_padding_or_trimming(self):
        rate = 24000
        for seconds in (1.25, 7.3, 10):
            with self.subTest(seconds=seconds):
                audio = np.linspace(-0.2, 0.2, round(seconds * rate), dtype=np.float32)
                np.testing.assert_array_equal(reference_clip(audio, rate), audio)
