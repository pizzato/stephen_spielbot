import os
import tempfile
import unittest

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app


class ComposeVisualStyleTests(unittest.TestCase):
    """``_compose_visual_style`` combines the per-job visual style with the
    configured ``default_visual_style``. The Create/Script UI pre-fills the
    per-job style FROM the default, so the two are usually identical — joining
    them blindly doubled the style in the FLUX prompt and over-stylized the
    images. The composer must de-duplicate."""

    DEFAULT = "Cinematic cel-shaded animated graphic-novel still"

    def test_identical_style_is_not_doubled(self):
        cfg = {"default_visual_style": self.DEFAULT}
        self.assertEqual(app._compose_visual_style(self.DEFAULT, cfg), self.DEFAULT)

    def test_distinct_styles_are_both_kept_job_first(self):
        cfg = {"default_visual_style": self.DEFAULT}
        self.assertEqual(
            app._compose_visual_style("Noir watercolor", cfg),
            f"Noir watercolor. {self.DEFAULT}",
        )

    def test_empty_job_style_falls_back_to_default(self):
        cfg = {"default_visual_style": self.DEFAULT}
        self.assertEqual(app._compose_visual_style("", cfg), self.DEFAULT)

    def test_dedup_is_case_and_punctuation_insensitive(self):
        cfg = {"default_visual_style": self.DEFAULT}
        # trailing period + different case must still be treated as a repeat
        self.assertEqual(
            app._compose_visual_style(self.DEFAULT.lower() + ".", cfg),
            self.DEFAULT.lower(),
        )

    def test_no_default_uses_job_style_only(self):
        self.assertEqual(app._compose_visual_style("Noir watercolor", {}), "Noir watercolor")

    def test_both_empty_is_empty(self):
        self.assertEqual(app._compose_visual_style("", {"default_visual_style": ""}), "")


if __name__ == "__main__":
    unittest.main()
