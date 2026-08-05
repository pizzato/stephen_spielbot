"""Text-free cover thumbnails: the diffusion model generates a background with
NO title text (unreliable to spell correctly), then pipeline/cover.py composites
crisp PIL vector text on top (render_cover_typography). Covers the normalizers,
the compositing itself (position/card/emphasis), the prompt no longer asking
for in-image text, the style plumbing (_ensure_styles coercion + flat mirror),
engine unpinning (the cover now uses the style's own image_engine instead of a
hard-coded flux1-schnell), and the worker's generate-then-composite flow."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app  # noqa: E402
import webapp.backend.main as backend  # noqa: E402
import worker_agent  # noqa: E402
import pipeline.cover as cover  # noqa: E402
from pipeline import engines  # noqa: E402

_OUT = Path(tempfile.mkdtemp(prefix="spielbot-test-out-"))


class NormalizerTests(unittest.TestCase):
    def test_position_defaults_to_bottom(self):
        for v in ("top", "center", "bottom"):
            self.assertEqual(cover.norm_cover_text_position(v), v)
        for bad in ("", None, "sideways"):
            self.assertEqual(cover.norm_cover_text_position(bad), "bottom")
        self.assertEqual(cover.norm_cover_text_position("TOP"), "top")  # case-insensitive

    def test_text_size_clamps_like_first_frame(self):
        self.assertEqual(cover.norm_cover_text_size(18), 18)
        self.assertEqual(cover.norm_cover_text_size(1), 4)
        self.assertEqual(cover.norm_cover_text_size(90), 30)
        self.assertEqual(cover.norm_cover_text_size("bad"), 11)

    def test_text_color_and_card_color_have_independent_defaults(self):
        self.assertEqual(cover.norm_cover_text_color("#abc"), "#AABBCC")
        self.assertEqual(cover.norm_cover_text_color("bogus"), "#FFFFFF")
        self.assertEqual(cover.norm_cover_card_color("#abc"), "#AABBCC")
        self.assertEqual(cover.norm_cover_card_color("bogus"), "#000000")

    def test_emphasis_color_blank_is_valid_and_kept(self):
        self.assertEqual(cover.norm_cover_emphasis_color(""), "")
        self.assertEqual(cover.norm_cover_emphasis_color(None), "")
        self.assertEqual(cover.norm_cover_emphasis_color("#ff0000"), "#FF0000")
        self.assertEqual(cover.norm_cover_emphasis_color("bogus"), "")

    def test_card_opacity_clamps_0_100(self):
        self.assertEqual(cover.norm_cover_card_opacity(55), 55)
        self.assertEqual(cover.norm_cover_card_opacity(-5), 0)
        self.assertEqual(cover.norm_cover_card_opacity(200), 100)
        self.assertEqual(cover.norm_cover_card_opacity("bad"), 55)

    def test_emphasis_rule_falls_back_to_none(self):
        for v in ("none", "caps", "last_word", "last_line"):
            self.assertEqual(cover.norm_cover_emphasis_rule(v), v)
        self.assertEqual(cover.norm_cover_emphasis_rule("bogus"), "none")

    def test_emphasis_scale_clamps_1_to_2(self):
        self.assertEqual(cover.norm_cover_emphasis_scale(1.25), 1.25)
        self.assertEqual(cover.norm_cover_emphasis_scale(0.5), 1.0)
        self.assertEqual(cover.norm_cover_emphasis_scale(5), 2.0)
        self.assertEqual(cover.norm_cover_emphasis_scale("bad"), 1.25)


class RenderCoverTypographyTests(unittest.TestCase):
    def _base(self, tmp, size=(1280, 720)):
        from PIL import Image
        base = Path(tmp) / "base.png"
        Image.new("RGB", size, (20, 20, 20)).save(base)
        return base

    def test_output_exists_and_is_opaque(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            out = Path(tmp) / "out.png"
            cover.render_cover_typography(base, out, "A Title Here")
            img = Image.open(out)
            self.assertEqual(img.size, (1280, 720))
            self.assertEqual(img.mode, "RGB")

    def test_position_places_text_near_the_requested_edge(self):
        from PIL import Image

        def brightness(img, y0, y1):
            band = img.crop((0, y0, img.width, y1)).convert("L")
            return sum(v * n for v, n in enumerate(band.histogram()))

        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            top_out, bottom_out = Path(tmp) / "top.png", Path(tmp) / "bottom.png"
            cover.render_cover_typography(base, top_out, "Bright Words", position="top", card=False)
            cover.render_cover_typography(base, bottom_out, "Bright Words", position="bottom", card=False)
            top_img, bottom_img = Image.open(top_out), Image.open(bottom_out)
            # The "top" render should be brighter (more white text pixels) in the
            # top third than the "bottom" render, and vice versa for the bottom third.
            self.assertGreater(brightness(top_img, 0, 240), brightness(bottom_img, 0, 240))
            self.assertGreater(brightness(bottom_img, 480, 720), brightness(top_img, 480, 720))

    def test_card_draws_a_visible_plate_behind_the_text(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp, size=(800, 450))
            no_card, with_card = Path(tmp) / "no_card.png", Path(tmp) / "with_card.png"
            cover.render_cover_typography(base, no_card, "Title", position="center",
                                          card=False, color="#FFFFFF")
            cover.render_cover_typography(base, with_card, "Title", position="center",
                                          card=True, card_color="#FF0000", card_opacity=100,
                                          color="#FFFFFF")
            # A corner of the card block (away from the glyphs themselves) should
            # now read as the card colour, not the plain dark background.
            no_card_px = Image.open(no_card).convert("RGB").getpixel((400, 190))
            with_card_px = Image.open(with_card).convert("RGB").getpixel((400, 190))
            self.assertEqual(no_card_px, (20, 20, 20))
            self.assertGreater(with_card_px[0], 150)   # red-ish plate
            self.assertLess(with_card_px[1], 80)

    def test_caps_emphasis_widens_the_emphasized_word(self):
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp, size=(1280, 720))
            uniform, emphasized = Path(tmp) / "uniform.png", Path(tmp) / "emphasized.png"
            cover.render_cover_typography(base, uniform, "the secret war", card=False)
            cover.render_cover_typography(base, emphasized, "the SECRET war",
                                          card=False, emphasis_rule="caps")
            # Measuring "SECRET" at the emphasis font must be wider than at the
            # base font — the actual mechanism that makes emphasized words look
            # bigger on the image.
            draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
            base_font = cover._load_font(cover.norm_cover_text_size(None) * 1280 // 100)
            emph_font = cover._load_font(int(cover.norm_cover_text_size(None) * 1280 // 100
                                              * cover.norm_cover_emphasis_scale(None)))
            self.assertGreater(draw.textlength("SECRET", font=emph_font),
                               draw.textlength("SECRET", font=base_font))

    def test_blank_text_leaves_the_background_untouched(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            base = self._base(tmp)
            out = Path(tmp) / "out.png"
            cover.render_cover_typography(base, out, "   ")
            self.assertEqual(Image.open(out).convert("RGB").getpixel((640, 360)), (20, 20, 20))


class BuildCoverPromptTests(unittest.TestCase):
    def test_never_asks_the_model_to_render_the_title(self):
        p = cover.build_cover_prompt("The Secret War", style="noir", scenes=None)
        self.assertNotIn("include the exact title", p)
        self.assertNotIn('"The Secret War"', p)
        self.assertIn("Topic: The Secret War.", p)
        self.assertIn("WITHOUT any text", p)


class StylePlumbingTests(unittest.TestCase):
    def test_new_fields_are_coerced_and_mirrored(self):
        cfg = {"styles": [{"name": "Docu",
                           "cover_text_position": "sideways",
                           "cover_text_size": 99,
                           "cover_text_color": "#abc",
                           "cover_text_card_color": "#def",
                           "cover_text_card_opacity": 500,
                           "cover_text_emphasis": "bogus",
                           "cover_text_emphasis_color": "not-a-color",
                           "cover_text_emphasis_scale": 9}]}
        app._ensure_styles(cfg)
        row = cfg["styles"][0]
        self.assertEqual(row["cover_text_position"], "bottom")       # coerced
        self.assertEqual(row["cover_text_size"], 30)                 # clamped
        self.assertEqual(row["cover_text_color"], "#AABBCC")
        self.assertEqual(row["cover_text_card_color"], "#DDEEFF")
        self.assertEqual(row["cover_text_card_opacity"], 100)
        self.assertEqual(row["cover_text_emphasis"], "none")
        self.assertEqual(row["cover_text_emphasis_color"], "")
        self.assertEqual(row["cover_text_emphasis_scale"], 2.0)
        # Flat keys mirror the default style, like every STYLE_FIELD_TO_FLAT entry.
        self.assertEqual(cfg["default_cover_text_position"], "bottom")
        ss = app.style_settings(cfg, "Docu")
        self.assertEqual(ss["cover_text_size"], 30)

    def test_sparse_child_inherits_from_parent(self):
        cfg = {
            "styles": [
                {"name": "Base", "cover_text_position": "top", "cover_text_card": False},
                {"name": "Kid", "parent": "Base"},
            ],
            "default_style": "Base",
        }
        app._ensure_styles(cfg)
        self.assertNotIn("cover_text_position", cfg["styles"][1])
        self.assertEqual(app.style_settings(cfg, "Kid")["cover_text_position"], "top")
        self.assertEqual(app.style_settings(cfg, "Kid")["cover_text_card"], False)

    def test_card_defaults_true_without_explicit_coercion(self):
        # cover_text_card is a plain bool (like auto_pick_exclude) — no _coerce
        # entry, it flows purely through the flat-key default.
        cfg = {"styles": [{"name": "Docu"}]}
        app._ensure_styles(cfg)
        self.assertEqual(app.style_settings(cfg, "Docu")["cover_text_card"], True)


class EngineUnpinningTests(unittest.TestCase):
    """The cover background used to be hard-pinned to flux1-schnell (the only
    engine that could render in-image text passably); now that no text is
    requested from the model, the cover uses the style's own image_engine."""

    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        (self.wd / "job_config.json").write_text('{"style_name": "Docu"}')

    def _cfg(self):
        cfg = {"styles": [{"name": "Docu", "image_engine": "flux2-klein"}],
               "default_style": "Docu"}
        app._ensure_styles(cfg)
        return cfg

    def test_engine_resolves_from_the_films_style_not_a_hard_pin(self):
        captured = {}

        class FakeStore:
            def create_or_update_job(self, *a, **kw): pass
            def create_task(self, *a, **kw): captured["payload"] = kw["payload"]
            def close(self): pass

        with mock.patch.object(backend.gapp, "load_config", return_value=self._cfg()), \
             mock.patch.object(backend, "_video_title_for", return_value="A Film"), \
             mock.patch.object(backend, "_film_dimensions", return_value=(1280, 720)), \
             mock.patch.object(backend.DurableStore, "default", return_value=FakeStore()), \
             mock.patch.object(backend.threading, "Thread"):
            backend.yt_cover(backend.CoverBody(work_dir=str(self.wd), title="A Film"))

        self.assertEqual(captured["payload"]["engine"]["key"], "flux2-klein")
        self.assertIn("cover_text", captured["payload"])
        self.assertEqual(captured["payload"]["cover_text"]["position"], "bottom")

    def test_default_engine_is_no_longer_pinned_to_schnell(self):
        self.assertFalse(hasattr(engines, "COVER_ENGINE"))


class ExecuteUiCoverTests(unittest.TestCase):
    """worker_agent._execute_ui_cover generates a text-free background into
    cover_base.png, then composites the title into cover.png — the diffusion
    model never sees the title text."""

    def test_generates_background_then_composites_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            generated = []

            def fake_generate(engine, prompt, out_path, **kw):
                generated.append((prompt, out_path))
                from PIL import Image
                Image.new("RGB", (kw["width"], kw["height"]), (10, 10, 10)).save(out_path)

            composited = []

            def fake_render(base_path, out_path, text, **kw):
                composited.append((base_path, out_path, text, kw))
                from PIL import Image
                Image.new("RGB", Image.open(base_path).size, (200, 200, 200)).save(out_path)

            recorded = []
            task = mock.Mock()
            task.payload = {
                "work_dir": str(wd), "title": "My Film", "vid_width": 1280, "vid_height": 720,
                "engine": {"key": "flux2-klein"}, "cover_text": {"position": "top"},
            }
            store = mock.Mock()
            store.scene_rows.return_value = []

            with mock.patch.object(worker_agent, "generate_with_engine", side_effect=fake_generate), \
                 mock.patch.object(worker_agent, "render_cover_typography", side_effect=fake_render), \
                 mock.patch.object(worker_agent.image_history, "cover_seed_if_empty"), \
                 mock.patch.object(worker_agent.image_history, "cover_record",
                                   side_effect=lambda *a: recorded.append(a)):
                worker_agent._execute_ui_cover(store, task, "http://localhost:8188")

            self.assertEqual(len(generated), 1, "background generated exactly once")
            self.assertEqual(generated[0][1], wd / "cover_base.png")
            self.assertEqual(len(composited), 1, "title composited exactly once")
            base_path, out_path, text, kw = composited[0]
            self.assertEqual(base_path, wd / "cover_base.png")
            self.assertEqual(out_path, wd / "cover.png")
            self.assertEqual(kw, {"position": "top"})
            self.assertEqual(recorded, [(wd, wd / "cover.png")])
            self.assertTrue((wd / "cover.png").exists())


if __name__ == "__main__":
    unittest.main()
