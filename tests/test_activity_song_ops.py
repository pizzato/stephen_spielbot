"""The Activity screen must show the song studio's slow work.

Rendering a song and re-voicing it are minutes on a GPU, but they ran in bare
``_script_tasks`` threads — nothing reached ``/api/activity``, so clicking
"Sing it as <voice>" looked like nothing had happened (and an automated music
video spent that time under a bare "Automation tick"). These tests pin the
live rows, the film-editor task's own label, and the failure status.
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Before importing the backend: it resolves the config/state paths from HOME at
# import time, and this module sorts first in the suite.
os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend  # noqa: E402


def _song_dir(tmp: Path) -> Path:
    wd = tmp / "a_song_film"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "song.json").write_text(json.dumps(
        {"title": "Neon Rain", "caption": "dream pop", "lyrics": "[verse]\nla",
         "voice": "Ada", "seconds": 60}))
    (wd / "background_music.wav").write_bytes(b"sung")
    return wd


def _live_names() -> list:
    return [e["name"] for e in backend.get_activity()["live"]]


def _history_by_name(name: str) -> dict:
    return next((e for e in backend.get_activity()["history"] if e["name"] == name), {})


class ActivityTestCase(unittest.TestCase):
    def setUp(self):
        # The tracker is module state — start every test from a clean slate.
        backend._current_ops.clear()
        backend._activity_log.clear()
        backend._activity_log_loaded = True
        for p in (mock.patch.object(backend, "_persist_activity_log_locked", lambda: None),
                  # Only the song ops are under test — keep any durable render
                  # this machine may have on file out of the live list.
                  mock.patch.object(backend, "_live_render_activity_items", return_value=[])):
            p.start()
            self.addCleanup(p.stop)


class SongConvertActivityTests(ActivityTestCase):
    """Re-voicing (seed-vc) — the click that showed nothing."""

    def _convert(self, wd: Path, seen: list, fail: bool = False):
        def convert_song(source, ref, staged, **kw):
            seen.extend(_live_names())
            if fail:
                raise RuntimeError("no worker took it")
            Path(staged).write_bytes(b"revoiced")

        with mock.patch.object(backend.gapp, "voice_path_for",
                               return_value=str(wd / "ref.wav")), \
             mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch("pipeline.svc.convert_song", side_effect=convert_song), \
             mock.patch("pipeline.svc.candidate_workers", return_value=[]), \
             mock.patch("pipeline.assembler._get_duration", return_value=61.0):
            (wd / "ref.wav").write_bytes(b"voice")
            return backend._do_song_convert(wd, "Ada")

    def test_revoicing_is_live_while_it_runs_and_lands_in_history(self):
        wd = _song_dir(Path(self.enterContext(tempfile.TemporaryDirectory())))
        seen: list = []
        self._convert(wd, seen)

        self.assertIn("Re-voicing the song as Ada", seen)   # visible mid-flight
        ev = _history_by_name("Re-voicing the song as Ada")
        self.assertEqual(ev.get("status"), "done")
        self.assertEqual(ev.get("work_dir"), str(wd))       # groups under the film
        self.assertEqual(_live_names(), [])                 # cleared when done

    def test_a_failed_revoicing_is_not_logged_as_done(self):
        wd = _song_dir(Path(self.enterContext(tempfile.TemporaryDirectory())))
        with self.assertRaises(RuntimeError):
            self._convert(wd, [], fail=True)
        self.assertEqual(_history_by_name("Re-voicing the song as Ada").get("status"), "error")

    def test_the_film_editor_path_does_not_report_the_same_work_twice(self):
        # _run_song_revoice already records the film task; the inner conversion
        # opts out so Activity shows one row, not two.
        wd = _song_dir(Path(self.enterContext(tempfile.TemporaryDirectory())))
        seen: list = []
        with mock.patch.object(backend.gapp, "voice_path_for",
                               return_value=str(wd / "ref.wav")), \
             mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch("pipeline.svc.convert_song",
                        side_effect=lambda s, r, staged, **k: (seen.extend(_live_names()),
                                                               Path(staged).write_bytes(b"x"))), \
             mock.patch("pipeline.svc.candidate_workers", return_value=[]), \
             mock.patch("pipeline.assembler._get_duration", return_value=61.0):
            (wd / "ref.wav").write_bytes(b"voice")
            backend._do_song_convert(wd, "Ada", track_op=False)
        self.assertEqual(seen, [])


class SongGenerateActivityTests(ActivityTestCase):
    """Rendering the track itself — equally invisible before."""

    def test_singing_the_song_is_live_while_the_worker_renders(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        wd = _song_dir(tmp)
        seen: list = []

        def generate_music(title, secs, staged, caption=None, **kw):
            seen.extend(_live_names())
            Path(staged).write_bytes(b"track")

        pool = mock.Mock()
        pool.acquire.return_value = "http://w1:8188"
        with mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend.gapp, "_preview_worker_urls",
                               return_value=["http://w1:8188"]), \
             mock.patch("pipeline.worker_pool.WorkerPool", return_value=pool), \
             mock.patch("pipeline.comfyui.generate_music", side_effect=generate_music), \
             mock.patch("pipeline.assembler._get_duration", return_value=60.0):
            backend._do_song_generate(wd)

        self.assertIn("Singing the song", seen)
        self.assertEqual(_history_by_name("Singing the song").get("status"), "done")


class SongRevoiceFilmTaskTests(unittest.TestCase):
    """The film-editor re-voicing is its own kind of job, not a music regen."""

    def test_it_is_labelled_a_re_voicing(self):
        tid = "song_revoice_1700000000"
        meta = {"work_dir": "", "scene_id": 0, "component": "song_revoice",
                "started_at": 1700000000.0}
        with mock.patch.dict(backend._film_task_meta, {tid: meta}, clear=False):
            op = backend._film_task_activity_op(
                tid, {"status": "running", "step": "revoice"})
        self.assertEqual(op["name"], "Re-voicing the song")
        self.assertEqual(op["detail"], "singing it in the new voice")

    def test_its_duration_does_not_train_the_music_regen_eta(self):
        self.assertEqual(backend._film_op_for("song_revoice"), "song_revoice")
        self.assertNotEqual(backend._film_op_for("song_revoice"),
                            backend._film_op_for("music"))


if __name__ == "__main__":
    unittest.main()
