"""Cover typography (pipeline/cover_typography.py): the diffusion model paints a
TEXT-FREE background and the title is composited on top with real fonts — so
cover words can never be misspelled and regenerating rerolls only the artwork.
Covers the settings normalizer, the *accent* phrase markup, accent rules, the
deterministic renderer (layout, colours, bounds), the text-free prompt fork,
the re-text helper (cover_bg.png → cover.png), the cover-history background
snapshots, style plumbing (_ensure_styles coercion + flat mirror), bundled
fonts, and the preview/retext endpoints."""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app  # noqa: E402
import webapp.backend.main as backend  # noqa: E402
from pipeline import cover_typography as ct  # noqa: E402
from pipeline import image_history  # noqa: E402
from pipeline.cover import build_cover_prompt  # noqa: E402


class NormTests(unittest.TestCase):
    def test_defaults_when_missing_or_junk(self):
        for value in (None, "", 42, [], {}):
            d = ct.norm_cover_typography(value)
            self.assertEqual(d["font"], "Anton")
            self.assertEqual(d["position"], "bottom")

    def test_legacy_enabled_key_is_ignored(self):
        # Typography is the only cover mode now; configs saved when it was a
        # toggle may still carry "enabled" — it must not survive the coercion.
        self.assertNotIn("enabled", ct.norm_cover_typography({"enabled": False}))

    def test_clamps_and_choices(self):
        d = ct.norm_cover_typography({
            "position": "sideways", "align": "wide", "case": "loud",
            "accent": "every_word", "width_pct": 300, "scale": 99,
            "accent_scale": 0.1, "card_opacity": 7,
            "color": "#f0a", "accent_color": "junk", "card_color": None,
        })
        self.assertEqual(d["position"], "bottom")
        self.assertEqual(d["align"], "center")
        self.assertEqual(d["case"], "upper")
        self.assertEqual(d["accent"], "last_word")
        self.assertEqual(d["width_pct"], 96)
        self.assertEqual(d["scale"], 1.6)
        self.assertEqual(d["accent_scale"], 1.0)
        self.assertEqual(d["card_opacity"], 0.95)
        self.assertEqual(d["color"], "#FF00AA")       # 3-digit hex expanded
        self.assertEqual(d["accent_color"], "#FFD400")  # junk → default


class MarkupTests(unittest.TestCase):
    def test_no_markup(self):
        self.assertEqual(ct.split_phrase_markup("The Silent City"),
                         ("The Silent City", set()))

    def test_single_and_multi_word_spans(self):
        clean, acc = ct.split_phrase_markup("The *Secret* Life of *Deep Sea* Giants")
        self.assertEqual(clean, "The Secret Life of Deep Sea Giants")
        self.assertEqual(acc, {1, 4, 5})

    def test_unpaired_asterisk_accents_the_rest(self):
        clean, acc = ct.split_phrase_markup("Fall of *Rome Tonight")
        self.assertEqual(clean, "Fall of Rome Tonight")
        self.assertEqual(acc, {2, 3})

    def test_strip_removes_markup_only(self):
        self.assertEqual(ct.strip_phrase_markup("A *B* C"), "A B C")
        self.assertEqual(ct.strip_phrase_markup(""), "")


class AccentRuleTests(unittest.TestCase):
    WORDS = ["Alpha", "Bee", "Gammagamma", "Delta"]

    def test_rules(self):
        self.assertEqual(ct._accent_set(self.WORDS, "none", set()), (set(), False))
        self.assertEqual(ct._accent_set(self.WORDS, "first_word", set()), ({0}, False))
        self.assertEqual(ct._accent_set(self.WORDS, "last_word", set()), ({3}, False))
        self.assertEqual(ct._accent_set(self.WORDS, "longest_word", set()), ({2}, False))
        self.assertEqual(ct._accent_set(self.WORDS, "last_line", set()), (set(), True))

    def test_markup_overrides_rule(self):
        self.assertEqual(ct._accent_set(self.WORDS, "first_word", {3}), ({3}, False))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="spielbot-covertypo-"))
        self.bg = ct.preview_background(320, 180)

    def _render(self, name, phrase, cfg):
        out = self.tmp / name
        meta = ct.render_cover_typography(self.bg, out, phrase, cfg)
        return out, meta

    def test_deterministic(self):
        a, _ = self._render("a.png", "The Silent City", {"accent": "last_word"})
        b, _ = self._render("b.png", "The Silent City", {"accent": "last_word"})
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_layout_meta_and_bounds(self):
        out, meta = self._render("m.png", "The Secret Life of Deep Sea Giants", {})
        self.assertGreater(meta["font_size"], 0)
        self.assertEqual(sum(len(l) for l in meta["lines"]), 7)
        x0, y0, x1, y1 = meta["block"]
        self.assertTrue(0 <= x0 < x1 <= 320)
        self.assertTrue(0 <= y0 < y1 <= 180)
        self.assertTrue(out.exists() and out.stat().st_size > 500)

    def test_accent_colour_lands_on_pixels(self):
        out, meta = self._render("acc.png", "Silent *CITY*",
                                 {"accent_color": "#FF0055", "outline": False})
        self.assertEqual(meta["accents"], [1])
        from PIL import Image
        colors = set(Image.open(out).convert("RGB").getdata())
        self.assertIn((255, 0, 85), colors)

    def test_markup_beats_rule_in_render(self):
        _, meta = self._render("mk.png", "Alpha *Beta* Gamma", {"accent": "first_word"})
        self.assertEqual(meta["accents"], [1])

    def test_last_line_accent_degrades_to_last_word_on_one_line(self):
        _, meta = self._render("ll.png", "Tiny", {"accent": "last_line"})
        self.assertEqual(meta["accents"], [0])

    def test_card_changes_the_render(self):
        a, _ = self._render("card0.png", "The Silent City", {"card": False})
        b, _ = self._render("card1.png", "The Silent City", {"card": True})
        self.assertNotEqual(a.read_bytes(), b.read_bytes())

    def test_empty_phrase_keeps_background(self):
        out, meta = self._render("empty.png", "", {})
        self.assertEqual(meta["font_size"], 0)
        self.assertTrue(out.exists())

    def test_portrait_fits(self):
        bg = ct.preview_background(180, 320)
        out = self.tmp / "portrait.png"
        meta = ct.render_cover_typography(bg, out, "Why Volcanoes Sing at Night",
                                          {"position": "bottom"})
        x0, y0, x1, y1 = meta["block"]
        self.assertTrue(0 <= x0 < x1 <= 180)
        self.assertTrue(0 <= y0 < y1 <= 320)

    def test_render_to_buffer(self):
        buf = io.BytesIO()
        ct.render_cover_typography(self.bg, buf, "Hello World", {})
        self.assertEqual(buf.getvalue()[:8], b"\x89PNG\r\n\x1a\n")


class FontTests(unittest.TestCase):
    def test_bundled_fonts_ship(self):
        names = [f["name"] for f in ct.bundled_fonts()]
        self.assertIn("Anton", names)
        self.assertIn("Luckiest Guy", names)

    def test_resolver_matches_bundled_by_name_and_falls_back(self):
        path = ct.resolve_font_path("Anton")
        self.assertTrue(path and Path(path).is_file())
        self.assertEqual(ct.resolve_font_path("No Such Face 9000"), "")
        self.assertEqual(ct.resolve_font_path(""), "")


class PromptTests(unittest.TestCase):
    def test_prompt_is_text_free_and_position_aware(self):
        p = build_cover_prompt("Gritty 16mm", text_position="top")
        self.assertIn("NO text", p)
        self.assertIn("top third", p)
        self.assertIn("Gritty 16mm", p)
        self.assertIn("bottom third", build_cover_prompt())  # default position

    def test_prompt_defers_to_the_video_style(self):
        # The template must not push covers toward a generic photoreal
        # documentary look — the film's own style has to win.
        p = build_cover_prompt("Cel-shaded animated graphic novel")
        self.assertIn("SAME production", p)
        self.assertNotIn("documentary", p)
        self.assertNotIn("16:9", p)  # portrait covers use the same template


class BuildCoverGenerationTests(unittest.TestCase):
    """The cover gets the same conditioning as scene stills: the composed
    visual style leads, matched characters contribute their canonical
    appearance, and reference-conditioning engines get portraits + notes."""

    def setUp(self):
        self.portrait = Path(tempfile.mkdtemp(prefix="spielbot-char-")) / "amy.png"
        ct.preview_background(64, 64).save(self.portrait, "PNG")
        self.cfg = {"styles": [{"name": "Docs",
                                "visual_style": "Cel-shaded graphic novel still"}],
                    "default_style": "Docs"}
        app._ensure_styles(self.cfg)
        self.char = {"name": "Amelia", "description": "a girl with a red scarf",
                     "enabled": True, "ref_image": "amy.png", "_ref_path": self.portrait}
        self.scenes = [{"image_prompt":
                        "Amelia stands on a cliff overlooking the storm at dawn, wide shot"}]

    def test_style_leads_and_characters_flow_in(self):
        engine = {"key": "flux2-klein", "t2i_ref_workflow": "flux2_t2i_ref.json"}
        with mock.patch.object(app, "_job_characters", return_value=[self.char]):
            prompt, refs = app.build_cover_generation(
                None, self.cfg, "Docs", scenes=self.scenes, engine=engine)
        self.assertIn("Cel-shaded graphic novel still", prompt)
        self.assertLess(prompt.index("Cel-shaded"), prompt.index("Amelia"))
        self.assertIn("a girl with a red scarf", prompt)   # canonical appearance
        self.assertIn("EXACTLY as the character", prompt)  # reference-match note
        self.assertEqual(refs, [self.portrait])

    def test_engines_without_reference_support_get_no_note(self):
        with mock.patch.object(app, "_job_characters", return_value=[self.char]):
            prompt, refs = app.build_cover_generation(
                None, self.cfg, "Docs", scenes=self.scenes, engine={"key": "flux1-schnell"})
        self.assertIn("a girl with a red scarf", prompt)
        self.assertNotIn("EXACTLY as the character", prompt)
        self.assertEqual(refs, [self.portrait])  # caller passes; flux1 ignores

    def test_unmentioned_characters_stay_out(self):
        with mock.patch.object(app, "_job_characters", return_value=[self.char]):
            prompt, refs = app.build_cover_generation(
                None, self.cfg, "Docs",
                scenes=[{"image_prompt": "A lighthouse at night in heavy rain, long exposure"}])
        self.assertNotIn("red scarf", prompt)
        self.assertEqual(refs, [])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-apply-"))

    def test_missing_background_is_a_noop(self):
        self.assertIsNone(ct.apply_cover_typography(self.wd, {}, "T"))
        self.assertFalse((self.wd / "cover.png").exists())

    def test_composites_phrase_over_background(self):
        ct.preview_background(320, 180).save(self.wd / ct.COVER_BASE_NAME, "PNG")
        (self.wd / "cover_phrase.txt").write_text("Silent *City*", encoding="utf-8")
        out = ct.apply_cover_typography(self.wd, {}, "Ignored Title")
        self.assertEqual(out, self.wd / "cover.png")
        self.assertTrue(out.exists() and out.stat().st_size > 500)


class HistoryBackgroundTests(unittest.TestCase):
    """Cover versions carry their text-free background, so selecting an old
    version keeps re-texting correct — and versions without one drop the
    stale background rather than let it mismatch the art."""

    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-hist-"))
        self.cover = self.wd / "cover.png"
        self.bgf = self.wd / "cover_bg.png"

    def _png(self, path, w=32, h=18):
        ct.preview_background(w, h).save(path, "PNG")

    def test_bg_snapshots_follow_versions(self):
        self._png(self.cover); self._png(self.bgf)
        bg1 = self.bgf.read_bytes()
        image_history.cover_record(self.wd, self.cover)          # v1 with bg
        self._png(self.bgf, 34, 20)
        image_history.cover_record(self.wd, self.cover)          # v2 with new bg
        image_history.cover_select(self.wd, 1)
        self.assertEqual(self.bgf.read_bytes(), bg1)             # v1 bg restored
        self.bgf.unlink()
        image_history.cover_record(self.wd, self.cover)          # v3 without bg
        image_history.cover_select(self.wd, 1)
        self.assertTrue(self.bgf.exists())
        image_history.cover_select(self.wd, 3)
        self.assertFalse(self.bgf.exists())                      # stale bg dropped

    def test_delete_removes_bg_snapshot(self):
        self._png(self.cover); self._png(self.bgf)
        image_history.cover_record(self.wd, self.cover)          # v1
        image_history.cover_record(self.wd, self.cover)          # v2 (selected)
        hist_bg = self.wd / "image_history" / "cover_v1_bg.png"
        self.assertTrue(hist_bg.exists())
        image_history.cover_delete(self.wd, 1)
        self.assertFalse(hist_bg.exists())


class StylePlumbingTests(unittest.TestCase):
    def test_ensure_styles_coerces_and_mirrors(self):
        cfg = {"styles": [{"name": "Docs", "cover_typography":
                           {"width_pct": 300, "position": "sideways"}}]}
        app._ensure_styles(cfg)
        row = cfg["styles"][0]["cover_typography"]
        self.assertEqual(row["width_pct"], 96)
        self.assertEqual(row["position"], "bottom")
        self.assertEqual(row["font"], "Anton")  # filled to a complete dict
        # Flat key mirrors the default style, like every STYLE_FIELD_TO_FLAT entry.
        self.assertEqual(cfg["default_cover_typography"]["width_pct"], 96)

    def test_children_inherit_the_dict(self):
        cfg = {"styles": [
            {"name": "Docs", "cover_typography": {"font": "Bangers"}},
            {"name": "Kids", "parent": "Docs"},
        ], "default_style": "Docs"}
        app._ensure_styles(cfg)
        eff = app.style_settings(cfg, "Kids")["cover_typography"]
        self.assertEqual(eff["font"], "Bangers")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="spielbot-out-"))
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", self.out)
        p.start(); self.addCleanup(p.stop)
        self.wd = self.out / "film"
        self.wd.mkdir()

    def test_preview_returns_png_both_orientations(self):
        for orientation in ("landscape", "portrait"):
            r = backend.cover_typography_preview(backend.CoverTypographyPreviewBody(
                cover_typography={"card": True}, text="Hi *There*",
                orientation=orientation))
            self.assertEqual(r.media_type, "image/png")
            self.assertEqual(bytes(r.body)[:8], b"\x89PNG\r\n\x1a\n")

    def test_retext_requires_a_background_then_composites(self):
        with mock.patch.object(backend, "_video_title_for", return_value="The Film"):
            with self.assertRaises(backend.HTTPException) as e:
                backend.cover_retext(backend.CoverRetextBody(work_dir=str(self.wd)))
            self.assertIn("no text-free background", e.exception.detail)
            ct.preview_background(320, 180).save(self.wd / ct.COVER_BASE_NAME, "PNG")
            r = backend.cover_retext(backend.CoverRetextBody(work_dir=str(self.wd)))
        self.assertTrue(r["ok"])
        self.assertTrue((self.wd / "cover.png").exists())
        self.assertIn("cover.png", r["cover_url"])

    def test_phrase_save_retexts_when_background_exists(self):
        ct.preview_background(320, 180).save(self.wd / ct.COVER_BASE_NAME, "PNG")
        with mock.patch.object(backend, "_video_title_for", return_value="The Film"):
            r = backend.save_cover_phrase(backend.CoverPhraseBody(
                work_dir=str(self.wd), phrase="Loud *Words*"))
        self.assertTrue(r["retexted"])
        self.assertTrue((self.wd / "cover.png").exists())
        self.assertIn("cover.png", r["cover_url"])


if __name__ == "__main__":
    unittest.main()
