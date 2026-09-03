"""YouTube Shorts skip the custom thumbnail. A Short is a square or portrait
film no longer than engagement_short_max_seconds (3 min); Shorts ignore
uploaded thumbnails and a thumbnail on a square Short has broken playback, so
both the Publish screen default and the auto-poster key off _is_shorts_film
instead of portrait-only."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app  # noqa: E402
import webapp.backend.main as backend  # noqa: E402
import pipeline.assembler as assembler  # noqa: E402


class IsShortsFilm(unittest.TestCase):
    def _check(self, dims, seconds, limit=180):
        wd = Path("/tmp/film")
        with mock.patch.object(backend, "_film_dimensions", return_value=dims), \
             mock.patch.object(app, "_final_path_for_work_dir", return_value=wd / "final.mp4"), \
             mock.patch.object(assembler, "_get_duration", return_value=seconds), \
             mock.patch.object(app, "load_config",
                               return_value={"engagement_short_max_seconds": limit}):
            return backend._is_shorts_film(wd)

    def test_square_under_three_minutes_is_a_short(self):
        self.assertTrue(self._check((1080, 1080), 45.0))

    def test_portrait_under_three_minutes_is_a_short(self):
        self.assertTrue(self._check((1080, 1920), 179.9))

    def test_landscape_is_never_a_short(self):
        self.assertFalse(self._check((1920, 1080), 30.0))

    def test_square_over_the_limit_is_long_form(self):
        self.assertFalse(self._check((1080, 1080), 181.0))

    def test_limit_follows_the_config(self):
        self.assertFalse(self._check((1080, 1080), 100.0, limit=60))
        self.assertTrue(self._check((1080, 1080), 100.0, limit=120))

    def test_unprobeable_final_falls_back_to_portrait_rule(self):
        # Duration unknown (0.0 or ffprobe blew up): portrait counts, square doesn't.
        self.assertTrue(self._check((1080, 1920), 0.0))
        self.assertFalse(self._check((1080, 1080), 0.0))
        wd = Path("/tmp/film")
        with mock.patch.object(backend, "_film_dimensions", return_value=(1080, 1920)), \
             mock.patch.object(app, "_final_path_for_work_dir", return_value=wd / "final.mp4"), \
             mock.patch.object(assembler, "_get_duration", side_effect=RuntimeError("no ffprobe")):
            self.assertTrue(backend._is_shorts_film(wd))


class ThumbnailDefaults(unittest.TestCase):
    def test_auto_poster_skips_thumbnail_for_shorts(self):
        """_claim_and_post_youtube passes include_thumbnail=not _is_shorts_film."""
        import inspect
        src = inspect.getsource(backend._claim_and_post_youtube)
        self.assertIn("include_thumbnail=not _is_shorts_film(p)", src)
        self.assertNotIn("_is_portrait_film", src)


if __name__ == "__main__":
    unittest.main()
