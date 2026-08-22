"""Per-style look of burned-in subtitles (pipeline/subtitle_style.py): the
settings dict is coerced like cover_typography, turned into an ASS force_style
override, and the chosen font file is handed to libass through fontsdir."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app  # noqa: E402
from pipeline import subtitle_style as ss  # noqa: E402


class NormTests(unittest.TestCase):
    def test_defaults_fill_in(self):
        self.assertEqual(ss.norm_subtitle_style(None), ss.DEFAULT_SUBTITLE_STYLE)
        self.assertEqual(ss.norm_subtitle_style("junk"), ss.DEFAULT_SUBTITLE_STYLE)

    def test_values_are_clamped_and_validated(self):
        d = ss.norm_subtitle_style({
            "scale": 9, "outline": -1, "card_opacity": 2, "margin": "99",
            "position": "Top", "align": "sideways", "color": "#abc",
            "outline_color": "red", "bold": 1, "font": "  Anton ",
        })
        self.assertEqual(d["scale"], 2.5)
        self.assertEqual(d["outline"], 0)
        self.assertEqual(d["card_opacity"], 1.0)
        self.assertEqual(d["margin"], 40)
        self.assertEqual(d["position"], "top")
        self.assertEqual(d["align"], "center")
        self.assertEqual(d["color"], "#AABBCC")
        self.assertEqual(d["outline_color"], "#000000")
        self.assertIs(d["bold"], True)
        self.assertEqual(d["font"], "Anton")


class ForceStyleTests(unittest.TestCase):
    def test_default_look_matches_ffmpeg_defaults(self):
        fs = ss.ass_force_style({})
        self.assertNotIn("FontName", fs)
        self.assertIn("FontSize=16", fs)
        self.assertIn("PrimaryColour=&H00FFFFFF", fs)
        self.assertIn("Alignment=2", fs)
        self.assertNotIn("BorderStyle", fs)

    def test_colours_positions_and_card(self):
        fs = ss.ass_force_style({
            "color": "#FFD400", "position": "top", "align": "left", "scale": 1.5,
            "bold": True, "card": True, "card_color": "#102040", "card_opacity": 0.5,
        }, "Bangers")
        self.assertIn("FontName=Bangers", fs)
        self.assertIn("FontSize=24", fs)
        self.assertIn("Bold=-1", fs)
        self.assertIn("PrimaryColour=&H0000D4FF", fs)  # ASS is &HAABBGGRR
        self.assertIn("Alignment=7", fs)
        self.assertIn("BorderStyle=4", fs)
        self.assertIn("BackColour=&H80402010", fs)


class FilterTests(unittest.TestCase):
    def test_bundled_font_is_copied_into_fontsdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            srt = Path(tmp) / "captions.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
            fonts = Path(tmp) / "fonts"
            vf = ss.subtitles_filter(srt, {"font": "Anton"}, fonts)
            self.assertTrue(vf.startswith(f"subtitles={srt}:fontsdir="), vf)
            self.assertIn("FontName=Anton", vf)
            self.assertTrue(list(fonts.glob("*.ttf")), "font file staged for libass")

    def test_unknown_font_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            srt = Path(tmp) / "captions.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
            vf = ss.subtitles_filter(srt, {"font": "No Such Face"}, Path(tmp) / "fonts")
            self.assertNotIn("fontsdir", vf)
            self.assertNotIn("FontName", vf)


class StylePlumbingTests(unittest.TestCase):
    def test_style_dict_is_coerced_and_mirrored(self):
        cfg = {"styles": [{"name": "Shorts", "subtitle_style": {"scale": "1.5", "color": "#fff"}}]}
        app._ensure_styles(cfg)
        st = cfg["styles"][0]["subtitle_style"]
        self.assertEqual(st["scale"], 1.5)
        self.assertEqual(st["color"], "#FFFFFF")
        self.assertEqual(st["position"], "bottom")
        self.assertEqual(cfg["default_subtitle_style"]["scale"], 1.5)

    def test_child_inherits_parent_look(self):
        cfg = {"styles": [
            {"name": "Base", "subtitle_style": {"font": "Anton", "card": True}},
            {"name": "Kid", "parent": "Base"},
        ]}
        app._ensure_styles(cfg)
        self.assertNotIn("subtitle_style", cfg["styles"][1])
        eff = app.style_settings(cfg, "Kid")["subtitle_style"]
        self.assertEqual(eff["font"], "Anton")
        self.assertIs(eff["card"], True)


if __name__ == "__main__":
    unittest.main()
