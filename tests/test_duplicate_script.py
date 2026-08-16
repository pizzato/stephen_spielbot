"""POST /api/scripts/duplicate copies a script into a fresh work dir so it can be
rendered again without overwriting the original film.

A job id is derived from the folder path, so the copy must land in a NEW folder
(new job, new sibling final video) carrying the script + cached first-frames +
description/cover across, and leave the source byte-for-byte intact. These tests
isolate that file/dir handling; durable-store registration is the shared
_register_script_into helper, already exercised through /scripts/load.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import webapp.backend.main as backend  # noqa: E402


SCENES = [
    {"id": 1, "title": "One", "image_prompt": "a", "video_prompt": "av", "narration": "n1"},
    {"id": 2, "title": "Two", "image_prompt": "b", "video_prompt": "bv", "narration": "n2"},
]


class DuplicateScriptTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="spielbot-videos-"))
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.out).start()
        # Stub the durable-store + style resolution so the test stays hermetic and
        # asserts only the copy logic under test.
        self.meta = mock.patch.object(
            backend, "_script_source_meta",
            return_value=("My Film", "noir", "jazz", "Cinematic", {
                "video_title": "My Film", "topic": "about the film",
                "n_scenes": 2, "style_name": "Cinematic",
            })).start()
        self.register = mock.patch.object(
            backend, "_register_script_into",
            side_effect=lambda wd, scenes, **kw: {"work_dir": str(wd), "scenes": scenes, **kw}).start()
        self.addCleanup(mock.patch.stopall)

        # A finished source film: script + cached frames + description + cover,
        # plus render outputs that must NOT be touched.
        self.src = self.out / "my-film-20260101-100000"
        self.src.mkdir(parents=True)
        (self.src / "script.json").write_text(json.dumps(SCENES))
        (self.src / "scene_01_preview.png").write_bytes(b"img1")
        (self.src / "scene_02_first_frame.png").write_bytes(b"img2")
        (self.src / "description.txt").write_text("the description")
        (self.src / "cover.png").write_bytes(b"cover")
        (self.src / "combined.mp4").write_bytes(b"x" * 20000)
        self.final = self.out / "my-film-20260101-100000.mp4"  # sibling final video
        self.final.write_bytes(b"x" * 20000)

    def _new_dir(self, result):
        wd = Path(result["work_dir"])
        self.assertTrue(wd.exists())
        self.assertNotEqual(wd, self.src)
        return wd

    def test_copies_script_and_assets_into_a_new_folder(self):
        result = backend.duplicate_script(backend.DuplicateScriptBody(work_dir=str(self.src)))
        wd = self._new_dir(result)
        self.assertEqual(json.loads((wd / "script.json").read_text()), SCENES)
        self.assertEqual((wd / "scene_01_preview.png").read_bytes(), b"img1")
        self.assertEqual((wd / "scene_02_first_frame.png").read_bytes(), b"img2")
        self.assertEqual((wd / "description.txt").read_text(), "the description")
        self.assertEqual((wd / "cover.png").read_bytes(), b"cover")
        # Registration ran against the NEW dir, with the source's metadata.
        self.register.assert_called_once()
        self.assertEqual(self.register.call_args.args[0], wd)
        self.assertEqual(self.register.call_args.kwargs["video_title"], "My Film")
        self.assertEqual(self.register.call_args.kwargs["style_name"], "Cinematic")
        self.assertEqual(self.register.call_args.kwargs["music_desc"], "jazz")

    def test_music_video_duplicate_keeps_its_song(self):
        # A song film IS its song: without these the copy renders a brand-new
        # lyric-less track over scenes still timed to the original one.
        (self.src / "song.json").write_text(json.dumps({"lyrics": "[Verse]\nla la"}))
        (self.src / "background_music.wav").write_bytes(b"track")
        (self.src / "music_history.json").write_text(json.dumps({"selected": 2}))
        (self.src / "music_history").mkdir()
        (self.src / "music_history" / "background_music_v1.wav").write_bytes(b"v1")

        result = backend.duplicate_script(backend.DuplicateScriptBody(work_dir=str(self.src)))
        wd = self._new_dir(result)
        self.assertEqual(json.loads((wd / "song.json").read_text())["lyrics"],
                         "[Verse]\nla la")
        self.assertEqual((wd / "background_music.wav").read_bytes(), b"track")
        self.assertEqual(json.loads((wd / "music_history.json").read_text())["selected"], 2)
        self.assertEqual((wd / "music_history" / "background_music_v1.wav").read_bytes(), b"v1")
        # The copy is still recognised as a music video by the film editor.
        self.assertTrue(backend._is_music_video(wd))

    def test_duplicate_without_a_song_copies_nothing_extra(self):
        result = backend.duplicate_script(backend.DuplicateScriptBody(work_dir=str(self.src)))
        wd = self._new_dir(result)
        self.assertFalse((wd / "song.json").exists())
        self.assertFalse((wd / "background_music.wav").exists())

    def test_original_film_is_left_intact(self):
        backend.duplicate_script(backend.DuplicateScriptBody(work_dir=str(self.src)))
        self.assertEqual(json.loads((self.src / "script.json").read_text()), SCENES)
        self.assertTrue((self.src / "combined.mp4").exists())
        self.assertTrue(self.final.exists())  # the published video must survive

    def test_title_override_drives_folder_slug_and_registration(self):
        result = backend.duplicate_script(
            backend.DuplicateScriptBody(work_dir=str(self.src), title="Fresh Take"))
        wd = self._new_dir(result)
        self.assertIn("fresh-take", wd.name)
        self.assertEqual(self.register.call_args.kwargs["video_title"], "Fresh Take")

    def test_rejects_path_outside_output_dir(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.duplicate_script(backend.DuplicateScriptBody(work_dir="/etc"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.register.assert_not_called()

    def test_missing_script_is_404(self):
        empty = self.out / "empty-20260101"
        empty.mkdir()
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.duplicate_script(backend.DuplicateScriptBody(work_dir=str(empty)))
        self.assertEqual(ctx.exception.status_code, 404)


class DuplicateScriptRoundTripTests(unittest.TestCase):
    """End-to-end through the real durable store (nothing stubbed): duplicate,
    then load the copy, and confirm it is an independent job with its copied
    first-frames wired up as scene previews."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="spielbot-videos-"))
        os.environ["SPIELBOT_ORCHESTRATOR_DB"] = str(
            Path(tempfile.mkdtemp(prefix="spielbot-db-")) / "orch.sqlite3")
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.out).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(lambda: os.environ.pop("SPIELBOT_ORCHESTRATOR_DB", None))

        self.src = self.out / "round-trip-20260101-090000"
        self.src.mkdir(parents=True)
        (self.src / "script.json").write_text(json.dumps(SCENES))
        (self.src / "scene_01_preview.png").write_bytes(b"img1")
        (self.src / "scene_02_first_frame.png").write_bytes(b"img2")

    def test_duplicate_is_an_independent_job_with_previews(self):
        src_payload = backend.load_script(str(self.src))
        dup = backend.duplicate_script(backend.DuplicateScriptBody(work_dir=str(self.src)))
        # Independent: a different folder means a different job id.
        self.assertNotEqual(dup["work_dir"], src_payload["work_dir"])
        self.assertNotEqual(dup["job_id"], src_payload["job_id"])
        # Same scene content; the copied images are wired up as previews.
        self.assertEqual([s["narration"] for s in dup["scenes"]],
                         [s["narration"] for s in SCENES])
        self.assertTrue(all(s["has_preview"] for s in dup["scenes"]))
        # The copy's id is stable: re-loading its own folder returns the same job.
        self.assertEqual(backend.load_script(dup["work_dir"])["job_id"], dup["job_id"])


if __name__ == "__main__":
    unittest.main()
