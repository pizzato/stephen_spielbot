"""Opening-frame clothes must not morph into the portrait's costume."""
import unittest

from pipeline import performance


class OpeningFrameWardrobeTests(unittest.TestCase):
    def _prompt(self, *pictures, **meta):
        return performance.build_h3_prompt(
            {"seconds": 10, "cast": ["Ana"], **meta},
            picture_names=list(pictures))

    def test_frame_limits_portraits_to_identity(self):
        prompt = self._prompt(
            {"name": "Ana", "kind": "character", "hint": "a sailor"},
            {"name": "First frame", "kind": "frame"})
        self.assertIn("defines Ana's face, hair and build only", prompt)
        self.assertIn("Ignore this picture's clothing and background", prompt)
        self.assertNotIn("defines Ana's appearance", prompt)
        self.assertIn("outfit visible in the opening image throughout the take", prompt)

    def test_legacy_named_portrait_obeys_the_same_frame_authority(self):
        prompt = self._prompt("Ana", {"name": "First frame", "kind": "frame"})
        self.assertIn("defines Ana's face, hair and build only", prompt)
        self.assertNotIn("defines Ana's appearance", prompt)

    def test_frame_outfit_wins_over_a_conflicting_wardrobe_sheet(self):
        prompt = self._prompt(
            {"name": "Ana", "kind": "character"},
            {"name": "First frame", "kind": "frame"},
            {"name": "Gala coat", "kind": "wardrobe", "character": "Ana"})
        self.assertIn("unseen details", prompt)
        self.assertIn("never replace or change the clothes visible in the opening image", prompt)
        self.assertNotIn("defines the clothes Ana wears only", prompt)
        self.assertNotIn("Keep every face, wardrobe and body exactly as in the references", prompt)

    def test_no_wardrobe_preserves_existing_frame_clothes(self):
        prompt = self._prompt(
            {"name": "Ana", "kind": "character"},
            {"name": "First frame", "kind": "frame"}, no_wardrobe=True)
        self.assertIn("defines Ana's face, hair and build only", prompt)
        self.assertIn("outfit visible in the opening image throughout the take", prompt)

    def test_portrait_still_controls_clothing_without_an_opening_frame(self):
        prompt = self._prompt({"name": "Ana", "kind": "character"}, no_wardrobe=True)
        self.assertIn("defines Ana's appearance", prompt)
        self.assertNotIn("Ignore this picture's clothing", prompt)
        self.assertNotIn("opening image", prompt)

    def test_wardrobe_still_controls_clothing_without_an_opening_frame(self):
        prompt = self._prompt(
            {"name": "Ana", "kind": "character"},
            {"name": "Gala coat", "kind": "wardrobe", "character": "Ana"})
        self.assertIn("defines Ana's face, hair and build only", prompt)
        self.assertIn("defines the clothes Ana wears only", prompt)
        self.assertNotIn("opening image", prompt)

    def test_hand_edited_prompt_keeps_its_authority(self):
        prompt = self._prompt(
            {"name": "Ana", "kind": "character"},
            {"name": "First frame", "kind": "frame"},
            prompt_override="Ana puts on her coat.")
        self.assertEqual(prompt, "Ana puts on her coat.")
