import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app


class FinalPathNeverAnotherFilmsTests(unittest.TestCase):
    """``_final_path_for_work_dir`` guesses (progress.json size match, recency)
    must never return the final of a different film: writers rebuild the final
    at the returned path, so a wrong guess overwrites another film."""

    def _make(self, output_dir: Path):
        other = output_dir / "pendulum-20260730-163542"
        other.mkdir()
        other_final = output_dir / f"{other.name}.mp4"
        other_final.write_bytes(b"x" * 24_600_000)  # 23.5 MB
        # A hand-copied duplicate still being rendered: its progress.json
        # quotes the source film's size, but its own final does not exist yet.
        dup = output_dir / "song-resync-20260820-104503"
        dup.mkdir()
        (dup / "progress.json").write_text(json.dumps(
            {"pct": 100, "msg": "✅ Done — song-20260819-221526.mp4 (25.1 MB)", "ts": 1}))
        return other, other_final, dup

    def test_size_guess_skips_another_films_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            other, other_final, dup = self._make(output_dir)
            with mock.patch.object(app, "OUTPUT_DIR", output_dir):
                got = app._final_path_for_work_dir(dup)
            self.assertNotEqual(got, other_final)
            self.assertEqual(got, output_dir / f"{dup.name}.mp4")

    def test_recency_guess_skips_another_films_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            other, other_final, dup = self._make(output_dir)
            (dup / "progress.json").write_text(json.dumps({"pct": 50, "msg": "Rendering", "ts": 1}))
            with mock.patch.object(app, "OUTPUT_DIR", output_dir):
                got = app._final_path_for_work_dir(dup)
            self.assertEqual(got, output_dir / f"{dup.name}.mp4")

    def test_orphan_final_still_found_by_size(self):
        """A legacy film whose final is not named after its folder (no work dir
        of that name exists) is still found — that is what the guess is for."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            wd = output_dir / "old-film-20260101-000000"
            wd.mkdir()
            (wd / "progress.json").write_text(json.dumps(
                {"pct": 100, "msg": "✅ Done — old_film_final.mp4 (25.1 MB)", "ts": 1}))
            orphan = output_dir / "old_film_final.mp4"
            orphan.write_bytes(b"x" * 26_300_000)
            with mock.patch.object(app, "OUTPUT_DIR", output_dir):
                got = app._final_path_for_work_dir(wd)
            self.assertEqual(got, orphan)


if __name__ == "__main__":
    unittest.main()
