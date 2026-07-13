"""LTX clips carry natural diegetic sound, not invented music.

LTX 2.3 generates each clip's audio from the same text prompt, so every video
render appends a natural-sound directive and adds music terms to the negative.
"""
import unittest

from pipeline.comfyui import _AUDIO_NEGATIVE, _AUDIO_POSITIVE, _steer_audio_natural


class SteerAudioTests(unittest.TestCase):
    def test_appends_natural_sound_and_music_negative(self):
        pos, neg = _steer_audio_natural("A cowboy walks into a saloon", "blurry")
        self.assertIn("A cowboy walks into a saloon", pos)
        self.assertIn(_AUDIO_POSITIVE, pos)
        self.assertIn("blurry", neg)
        self.assertIn("music", neg)
        self.assertIn("soundtrack", neg)

    def test_handles_empty_prompts(self):
        pos, neg = _steer_audio_natural("", "")
        self.assertEqual(pos, f"{_AUDIO_POSITIVE}.")
        self.assertEqual(neg, _AUDIO_NEGATIVE)

    def test_no_double_period_or_comma(self):
        pos, neg = _steer_audio_natural("scene ends.", "artifacts,")
        self.assertNotIn("..", pos)
        self.assertNotIn(",,", neg)


if __name__ == "__main__":
    unittest.main()
