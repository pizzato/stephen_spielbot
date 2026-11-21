"""Realign a finished song's lyric fields without changing its existing cut."""
import json
from unittest import mock

import webapp.backend.main as backend
from fastapi import HTTPException
from pipeline import captions, lyric_align, performance, song_timing
from pipeline.orchestrator import DurableStore, job_id_from_work_dir
from test_styles import TempConfigCase


class SongRealignTests(TempConfigCase):
    WORDS = [("amber", 1.0, 1.8), ("morning", 2.0, 2.8),
             ("silver", 6.0, 6.8), ("sunset", 7.0, 7.8)]

    def setUp(self):
        super().setUp()
        self.wd = self.output_dir / "song-film"
        self.wd.mkdir()
        self.job_id = job_id_from_work_dir(self.wd)
        self.track = self.wd / "background_music.wav"
        self.track.write_bytes(b"original song")
        self.stem = self.wd / "background_music_vocals.wav"
        self.stem.write_bytes(b"separated vocals")
        (self.wd / "song.json").write_text(json.dumps({
            "lyrics": "[Verse]\namber morning\nsilver sunset", "title": "Song"}))
        (self.wd / "combined.mp4").write_bytes(b"finished film")
        (self.wd / "job_config.json").write_text('{"tts_language": "en"}')
        rows = []
        for sid in range(1, 5):
            meta = {"mode": "silent", "cast": ["Ada"], "duration": 5.0,
                    "setting": "A rooftop", "camera": "Still", "no_first_frame": True,
                    "beats": [{"t0": 0.0, "t1": 5.0, "action": "Ada turns away"}]}
            if sid < 4:
                meta.update(singing=True, song_window=[(sid - 1) * 5.0, sid * 5.0],
                            sings="stale line", line_times=[[0.0, 4.0]],
                            vocal_ranges=[[0.0, 4.0]])
            if sid == 1:
                meta["performs"] = False
            if sid == 2:
                meta["prompt_override"] = "Keep this pinned prompt"
            rows.append({"id": sid, "title": f"Shot {sid}", "image_prompt": "image",
                         "video_prompt": "Old staging", "narration": "",
                         "preview_path": str(self.wd / f"scene_{sid:02d}_preview.png"),
                         "metadata": meta})
            (self.wd / f"scene_{sid:02d}_final.mp4").write_bytes(f"take {sid}".encode())
            (self.wd / f"scene_{sid:02d}_preview.png").write_bytes(f"frame {sid}".encode())
        store = DurableStore.default()
        try:
            store.create_or_update_job(self.job_id, self.wd, "Song")
            store.upsert_scenes(self.job_id, rows)
        finally:
            store.close()
        backend.gapp._persist_script_snapshot(self.wd, self._rows())
        self.before = self._rows()
        self.media = {p: p.read_bytes() for p in self.wd.iterdir()
                      if p.suffix in (".wav", ".mp4", ".png") or p.name == "song.json"}
        for patch in (
            mock.patch.dict(backend._film_tasks, {}, clear=True),
            mock.patch.dict(backend._film_task_meta, {}, clear=True),
            mock.patch.object(backend, "_film_cancelled_tids", set()),
            mock.patch.object(backend, "_film_edit_busy", return_value=False),
            mock.patch.object(backend, "_record_film_task_activity"),
            mock.patch.object(backend, "_scene_style_note", return_value="Drawn"),
            mock.patch("pipeline.assembler._get_duration", return_value=20.0),
            mock.patch.object(song_timing, "vocal_stem", return_value=self.stem),
            mock.patch.object(song_timing, "measure_regions", return_value=[(0.5, 3.2), (5.5, 8.2)]),
            mock.patch.object(song_timing, "vocal_regions", return_value=[(0.5, 3.2), (5.5, 8.2)]),
            mock.patch.object(captions, "_scene_duration", return_value=5.0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(lyric_align, "word_times", return_value=self.WORDS)
        self.words = patch.start()
        self.addCleanup(patch.stop)

        def refresh_captions(wd):
            captions.build_srt(wd)
            return []

        patch = mock.patch.object(backend, "_caption_tracks_for_download", side_effect=refresh_captions)
        self.caption_refresh = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(backend, "_remux_with_current_music")
        self.remux = patch.start()
        self.addCleanup(patch.stop)

    def _rows(self):
        store = DurableStore.default()
        try:
            return store.scene_rows(self.job_id)
        finally:
            store.close()

    def _replace(self, rows):
        store = DurableStore.default()
        try:
            store.replace_scenes(self.job_id, rows)
        finally:
            store.close()
        backend.gapp._persist_script_snapshot(self.wd, self._rows())

    def _run(self):
        tid = "song_realign_test"
        backend._film_tasks[tid] = {"status": "running"}
        backend._film_task_meta[tid] = {"work_dir": str(self.wd), "scene_id": 0,
                                       "component": "song_realign"}
        backend._realign_song_task(tid, self.wd)
        return backend._film_tasks[tid]

    def test_realign_updates_lyrics_without_changing_the_cut_or_manual_staging(self):
        result = self._run()
        self.assertEqual(result["status"], "done", result)
        after = self._rows()
        self.assertEqual(after[0]["metadata"]["sings"], "amber morning")
        self.assertEqual(after[1]["metadata"]["sings"], "silver sunset")
        self.assertEqual(after[0]["metadata"]["line_times"], [[1.0, 2.8]])
        self.assertEqual(after[1]["metadata"]["line_times"], [[1.0, 2.8]])
        self.assertEqual(after[0]["metadata"]["vocal_ranges"], [[0.5, 3.2]])
        self.assertFalse(after[2]["metadata"].get("sings"))
        self.assertFalse(after[2]["metadata"].get("line_times"))
        self.assertEqual(after[2]["metadata"]["vocal_ranges"], [])
        changed_fields = {"sings", "line_times", "vocal_ranges"}
        for old, new in zip(self.before, after):
            self.assertEqual({k: v for k, v in old["metadata"].items() if k not in changed_fields},
                             {k: v for k, v in new["metadata"].items() if k not in changed_fields})
            for key in ("id", "title", "image_prompt", "narration", "preview_path"):
                self.assertEqual(old[key], new[key])
        self.assertEqual(after[3], self.before[3])
        self.assertEqual(after[1]["video_prompt"], self.before[1]["video_prompt"])
        self.assertNotEqual(after[0]["video_prompt"], self.before[0]["video_prompt"])
        self.assertEqual(json.loads((self.wd / "script.json").read_text()), after)
        self.assertEqual({p: p.read_bytes() for p in self.media}, self.media)
        self.assertIn("amber morning", (self.wd / "captions.srt").read_text())
        self.assertNotIn("stale line", (self.wd / "captions.srt").read_text())
        self.remux.assert_not_called()
        self.caption_refresh.assert_called_once_with(self.wd)
        backups = list(self.wd.glob("lyric_alignment_backup_*.json"))
        self.assertEqual(len(backups), 1)
        self.assertIn("stale line", backups[0].read_text())

    def test_weak_alignment_keeps_existing_scene_and_caption_files(self):
        self.words.return_value = [("unrelated", 1.0, 2.0)]
        script = (self.wd / "script.json").read_bytes()
        result = self._run()
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(self._rows(), self.before)
        self.assertEqual((self.wd / "script.json").read_bytes(), script)
        self.caption_refresh.assert_not_called()

    def test_prose_only_action_survives_rebuilding_the_prompt(self):
        rows = self._rows()
        rows[0]["metadata"]["beats"] = []
        rows[0]["video_prompt"] = "Ada opens the balcony door and steps into the rain."
        self._replace(rows)
        expected = performance.acted_meta(rows[0])["beats"]
        result = self._run()
        self.assertEqual(result["status"], "done", result)
        saved = self._rows()[0]
        self.assertEqual(performance.acted_meta(saved)["beats"], expected)
        self.assertIn("opens the balcony door", saved["video_prompt"])

    def test_caption_failure_reports_saved_lyrics_and_a_retry_message(self):
        self.caption_refresh.side_effect = RuntimeError("caption write failed")
        result = self._run()
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(self._rows()[0]["metadata"]["sings"], "amber morning")
        self.assertIn("Caption refresh failed", result["message"])
        self.assertIn("retry", result["message"])

    def test_wordless_vocals_keep_their_window_without_inventing_lyrics(self):
        with mock.patch.object(lyric_align, "voiced_regions", return_value=[
                (0.5, 3.2), (5.5, 8.2), (11.0, 13.0)]):
            result = self._run()
        self.assertEqual(result["status"], "done", result)
        meta = self._rows()[2]["metadata"]
        self.assertFalse(meta["sings"])
        self.assertEqual(meta["line_times"], [])
        self.assertEqual(meta["vocal_ranges"], [[1.0, 3.0]])

    def test_script_only_films_keep_every_scene_when_imported_into_the_store(self):
        store = DurableStore.default()
        try:
            store.replace_scenes(self.job_id, [])
        finally:
            store.close()
        self.assertEqual(self._rows(), [])
        result = self._run()
        self.assertEqual(result["status"], "done", result)
        after = self._rows()
        self.assertEqual([r["id"] for r in after], [1, 2, 3, 4])
        self.assertEqual(after[3], self.before[3])
        self.assertEqual(after[0]["metadata"]["sings"], "amber morning")
        self.assertEqual(json.loads((self.wd / "script.json").read_text()), after)

    def test_editing_a_scene_during_alignment_prevents_partial_overwrite(self):
        def transcribe(*args, **kwargs):
            rows = self._rows()
            rows[1]["metadata"]["sings"] = "a newer manual correction"
            # Leave script.json unchanged: the store's compare-and-write must
            # catch this edit even before its snapshot has reached disk.
            store = DurableStore.default()
            try:
                store.upsert_scenes(self.job_id, rows)
            finally:
                store.close()
            return self.WORDS

        self.words.side_effect = transcribe
        result = self._run()
        self.assertEqual(result["status"], "error", result)
        rows = self._rows()
        self.assertEqual(rows[0], self.before[0])
        self.assertEqual(rows[1]["metadata"]["sings"], "a newer manual correction")
        self.caption_refresh.assert_not_called()

    def test_a_scene_edit_while_reading_inputs_cannot_become_the_expected_old_state(self):
        def duration(*args, **kwargs):
            rows = self._rows()
            rows[1]["metadata"]["sings"] = "edited while probing the track"
            store = DurableStore.default()
            try:
                store.upsert_scenes(self.job_id, rows)
            finally:
                store.close()
            return 20.0

        with mock.patch("pipeline.assembler._get_duration", side_effect=duration):
            result = self._run()
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(self._rows()[1]["metadata"]["sings"], "edited while probing the track")
        self.words.assert_not_called()
        self.caption_refresh.assert_not_called()

    def test_replacing_the_track_during_alignment_prevents_writing_old_timings(self):
        def transcribe(*args, **kwargs):
            self.track.write_bytes(b"a different, longer song track")
            return self.WORDS

        self.words.side_effect = transcribe
        result = self._run()
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(self._rows(), self.before)
        self.caption_refresh.assert_not_called()

    def test_a_new_render_during_alignment_prevents_saving(self):
        def transcribe(*args, **kwargs):
            backend._film_tasks["new-render"] = {"status": "running"}
            backend._film_task_meta["new-render"] = {
                "work_dir": str(self.wd), "scene_id": 1, "component": "video"}
            return self.WORDS

        self.words.side_effect = transcribe
        result = self._run()
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(self._rows(), self.before)
        self.caption_refresh.assert_not_called()

    def test_endpoint_starts_a_tracked_background_alignment(self):
        with mock.patch.object(backend.threading, "Thread") as thread:
            result = backend.film_realign_song(backend.SongRealignBody(work_dir=str(self.wd)))
        tid = result["task_id"]
        self.assertEqual(backend._film_tasks[tid]["status"], "running")
        self.assertEqual(backend._film_task_meta[tid]["component"], "song_realign")
        thread.return_value.start.assert_called_once()
        self.words.assert_not_called()

    def test_endpoint_rejects_an_outside_path_or_missing_song(self):
        with self.assertRaises(HTTPException) as ctx:
            backend.film_realign_song(backend.SongRealignBody(work_dir=str(self.output_dir.parent)))
        self.assertEqual(ctx.exception.status_code, 400)
        (self.wd / "song.json").unlink()
        with self.assertRaises(HTTPException) as ctx:
            backend.film_realign_song(backend.SongRealignBody(work_dir=str(self.wd)))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_endpoint_rejects_invalid_song_windows_before_starting_alignment(self):
        for window in (None, [0, 0], [-1, 4], [0, float("nan")], [0, 21]):
            with self.subTest(window=window):
                rows = self._rows()
                rows[0]["metadata"]["song_window"] = window
                self._replace(rows)
                with self.assertRaises(HTTPException) as ctx:
                    backend.film_realign_song(backend.SongRealignBody(work_dir=str(self.wd)))
                self.assertEqual(ctx.exception.status_code, 400)
        self.words.assert_not_called()

    def test_endpoint_refuses_alignment_during_another_film_edit(self):
        with mock.patch.object(backend, "_film_edit_busy", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                backend.film_realign_song(backend.SongRealignBody(work_dir=str(self.wd)))
        self.assertEqual(ctx.exception.status_code, 409)
