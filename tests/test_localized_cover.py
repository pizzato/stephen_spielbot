"""Localized cover images: a localized cut publishes the same text-free cover
art re-titled with the translated cover phrase (localize/{lang}/cover.png).
No network, no GPU — rendering is pure PIL onto a synthetic background."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline.cover_typography as ct
from pipeline import final_video_history
import webapp.backend.main as backend


def _film(out: Path, lang_meta: dict | None = None, with_bg: bool = True,
          name: str = "film") -> Path:
    wd = out / name
    wd.mkdir(parents=True, exist_ok=True)
    if with_bg:
        ct.preview_background(320, 180).save(wd / ct.COVER_BASE_NAME, "PNG")
    if lang_meta is not None:
        scripts = wd / "localize_scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "pt.json").write_text(
            json.dumps({"lang": "pt", "scenes": {}, **lang_meta}))
    return wd


class RenderLocalizedCoverTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="spielbot-loccover-"))

    def test_renders_translated_phrase_onto_background(self):
        wd = _film(self.out, {"title": "Título PT", "cover_phrase": "Frase *PT*"})
        cover = backend._render_localized_cover(wd, "pt")
        self.assertEqual(cover, wd / "localize" / "pt" / "cover.png")
        self.assertTrue(cover.exists() and cover.stat().st_size > 1000)
        # cover.png itself is untouched — the localized cover lives per-language.
        self.assertFalse((wd / "cover.png").exists())

    def test_falls_back_to_localized_title_without_phrase(self):
        wd = _film(self.out, {"title": "Título PT: legenda"})
        with mock.patch.object(backend, "render_cover_typography") as rct:
            backend._render_localized_cover(wd, "pt")
        self.assertEqual(rct.call_args.args[2], "Título *PT*")  # shortened title + accent rule

    def test_no_background_or_no_translation_returns_none(self):
        self.assertIsNone(backend._render_localized_cover(
            _film(self.out, {"title": "Título"}, with_bg=False, name="a"), "pt"))
        self.assertIsNone(backend._render_localized_cover(
            _film(self.out, None, name="b"), "pt"))  # localization never ran
        self.assertIsNone(backend._render_localized_cover(
            _film(self.out, {}, name="c"), "pt"))    # no cached metadata yet


class PublishCoverPathTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="spielbot-pubcover-"))

    def _with_history(self, wd: Path, select_lang: str | None):
        final = wd / "final.mp4"
        final.write_bytes(b"x" * 64)
        final_video_history.seed_if_empty(wd, final, "Original", lang="en")
        if select_lang:
            final_video_history.record(wd, final, label="Portuguese",
                                       lang=select_lang, kind="localize")

    def test_localized_cut_gets_localized_cover(self):
        wd = _film(self.out, {"title": "Título PT", "cover_phrase": "Frase PT"})
        self._with_history(wd, "pt")
        self.assertEqual(backend._publish_cover_path(wd),
                         wd / "localize" / "pt" / "cover.png")

    def test_original_cut_keeps_cover_png(self):
        wd = _film(self.out, {"title": "Título PT", "cover_phrase": "Frase PT"})
        self._with_history(wd, None)
        self.assertEqual(backend._publish_cover_path(wd), wd / "cover.png")

    def test_legacy_cover_without_background_falls_back(self):
        wd = _film(self.out, {"title": "Título PT"}, with_bg=False)
        self._with_history(wd, "pt")
        self.assertEqual(backend._publish_cover_path(wd), wd / "cover.png")


class PhraseSaveInvalidationTests(unittest.TestCase):
    def test_saving_a_phrase_drops_cached_translations(self):
        out = Path(tempfile.mkdtemp(prefix="spielbot-phrase-"))
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", out)
        p.start(); self.addCleanup(p.stop)
        wd = _film(out, {"title": "Título PT", "cover_phrase": "Frase Velha"})
        with mock.patch.object(backend, "_video_title_for", return_value="The Film"):
            backend.save_cover_phrase(backend.CoverPhraseBody(
                work_dir=str(wd), phrase="Loud *Words*"))
        stored = json.loads((wd / "localize_scripts" / "pt.json").read_text())
        self.assertNotIn("cover_phrase", stored)     # stale translation dropped
        self.assertEqual(stored["title"], "Título PT")  # the rest is untouched


if __name__ == "__main__":
    unittest.main()
