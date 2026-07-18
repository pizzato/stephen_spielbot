"""Localized publish metadata: translated once, cached in the localization's
script file, reused afterward. No network — the LLM call is mocked."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import webapp.backend.main as backend


def _film_with_localization(lang="pt"):
    wd = Path(tempfile.mkdtemp(prefix="spielbot-meta-"))
    scripts = wd / "localize_scripts"
    scripts.mkdir()
    (scripts / f"{lang}.json").write_text(json.dumps(
        {"lang": lang, "scenes": {"1": "Primeira cena."}}
    ))
    return wd


class LocalizeMetadataTests(unittest.TestCase):
    def test_translates_and_caches_on_first_use(self):
        wd = _film_with_localization()
        translated = {"title": "Título PT", "description": "Descrição PT"}
        with mock.patch("pipeline.llm.translate_metadata", return_value=translated) as tm, \
             mock.patch.object(backend, "_video_title_for", return_value="Original Title"), \
             mock.patch.object(backend, "_cached_description", return_value="Original description."):
            first = backend._localize_metadata(wd, "pt")
            second = backend._localize_metadata(wd, "pt")
        self.assertEqual(first, translated)
        self.assertEqual(second["title"], "Título PT")
        self.assertEqual(tm.call_count, 1)  # second hit served from the cache
        stored = json.loads((wd / "localize_scripts" / "pt.json").read_text())
        self.assertEqual(stored["title"], "Título PT")
        self.assertEqual(stored["scenes"]["1"], "Primeira cena.")  # scenes untouched

    def test_missing_localization_raises(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-meta-"))
        with self.assertRaises(FileNotFoundError):
            backend._localize_metadata(wd, "pt")


if __name__ == "__main__":
    unittest.main()
