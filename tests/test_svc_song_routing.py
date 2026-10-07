"""Song studio, finished-film and automatic paths share the film's converter style."""
import json
from unittest import mock

from fastapi import HTTPException

import app
import webapp.backend.main as backend
from test_singing_mode import _SongFilmCase
from test_styles import _style


class SongConverterRoutingTests(_SongFilmCase):
    def _styles(self, rows, default="Default"):
        cfg = app.load_config()
        cfg.update(styles=rows, default_style=default)
        app.save_config(cfg)

    def _convert(self):
        def convert(source, ref, output, **kwargs):
            output.write_bytes(b"new-singer")
            return output
        with mock.patch("pipeline.svc.convert_song", side_effect=convert) as call:
            backend._do_song_convert(self.wd, "Nora")
        return call

    def test_missing_choice_uses_soulx_and_records_model_in_version_label(self):
        call = self._convert()
        self.assertEqual(call.call_args.kwargs["engine"], "soulx-svc")
        self.assertIsNone(call.call_args.kwargs["diffusion_steps"])
        version = backend.music_history.history(self.wd)["versions"][-1]
        self.assertIn("SoulX-SVC", version["desc"])

    def test_song_style_wins_over_default_and_uses_original_after_switch(self):
        self._styles([_style("Default", svc_engine="soulx-svc"),
                      _style("Hero", svc_engine="seed-vc")])
        first = self._convert()
        self.assertEqual(first.call_args.kwargs["engine"], "seed-vc")
        cfg = app.load_config()
        next(s for s in cfg["styles"] if s["name"] == "Hero")["svc_engine"] = "soulx-svc"
        app.save_config(cfg)
        second = self._convert()
        self.assertEqual(second.call_args.kwargs["engine"], "soulx-svc")
        self.assertEqual(second.call_args.args[0].read_bytes(), b"as-generated")

    def test_restyled_film_inherits_current_style_instead_of_stale_song_style(self):
        self._styles([_style("Default", svc_engine="soulx-svc"),
                      _style("Parent", svc_engine="seed-vc"),
                      {"name": "Child", "parent": "Parent"}, _style("Hero")])
        (self.wd / "job_config.json").write_text(json.dumps({"style_name": "Child"}))
        call = self._convert()
        self.assertEqual(call.call_args.kwargs["engine"], "seed-vc")

    def test_availability_and_labels_check_selected_engine_in_both_screens(self):
        self._styles([_style("Default"), _style("Hero", svc_engine="seed-vc")])
        with mock.patch("pipeline.svc.available", side_effect=lambda engine: engine == "seed-vc"):
            for data in (backend.get_job_song(self.job_id), backend._remix_song_info(self.wd)):
                self.assertEqual(data["svc_engine"], "seed-vc")
                self.assertEqual(data["svc_engine_label"], "Seed-VC")
                self.assertTrue(data["svc_available"])

    def test_both_entry_points_report_the_selected_missing_install(self):
        self._styles([_style("Default"), _style("Hero", svc_engine="seed-vc")])
        with mock.patch("pipeline.svc.available", return_value=False) as available:
            for endpoint, body in (
                (backend.song_convert, backend.SongConvertBody),
                (backend.remix_song_voice, backend.SongRevoiceBody),
            ):
                with self.assertRaises(HTTPException) as error:
                    endpoint(body(work_dir=str(self.wd), voice="Nora"))
                self.assertEqual(error.exception.status_code, 503)
                self.assertIn("Seed-VC", error.exception.detail)
                available.assert_called_with("seed-vc")

    def test_explicit_global_step_override_is_retained(self):
        cfg = app.load_config()
        cfg["svc_diffusion_steps"] = 45
        app.save_config(cfg)
        self.assertEqual(self._convert().call_args.kwargs["diffusion_steps"], 45)

    def test_concurrent_revoice_cannot_delete_staging_or_replace_active_mix(self):
        import threading

        converting, finish_conversion = threading.Event(), threading.Event()
        remixing, finish_remix = threading.Event(), threading.Event()
        staged = self.wd / "background_music.staging.wav"

        def convert(source, ref, output, **kwargs):
            output.write_bytes(b"active-conversion")
            converting.set()
            if not finish_conversion.wait(5):
                raise RuntimeError("test conversion timed out")

        def remix(wd):
            remixing.set()
            if not finish_remix.wait(5):
                raise RuntimeError("test remix timed out")
            return wd / "combined.mp4"

        with mock.patch("pipeline.svc.convert_song", side_effect=convert), \
             mock.patch.object(backend, "_remux_with_current_music", side_effect=remix), \
             mock.patch.object(backend, "_record_film_task_activity"):
            worker = threading.Thread(target=backend._run_song_revoice,
                                      args=("active_conversion", self.wd, "Nora"))
            worker.start()
            try:
                self.assertTrue(converting.wait(3))
                backend._run_song_revoice("duplicate_conversion", self.wd, "Nora")
                self.assertEqual(backend._film_tasks["duplicate_conversion"]["status"], "error")
                self.assertIn("already running", backend._film_tasks["duplicate_conversion"]["error"])
                self.assertEqual(staged.read_bytes(), b"active-conversion")
                finish_conversion.set()
                self.assertTrue(remixing.wait(3))
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    backend._do_song_convert(self.wd, "Nora")
            finally:
                finish_conversion.set()
                finish_remix.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(backend._film_tasks["active_conversion"]["status"], "done")
            self.assertFalse(staged.exists())
        # The lock is released after the whole operation; another take can run.
        self._convert()

    def test_same_second_requests_have_distinct_task_ids(self):
        with mock.patch("pipeline.svc.available", return_value=True), \
             mock.patch.object(backend.threading, "Thread"), \
             mock.patch.object(backend.time, "time", return_value=12345):
            body = backend.SongRevoiceBody(work_dir=str(self.wd), voice="Nora")
            first = backend.remix_song_voice(body)
            second = backend.remix_song_voice(body)
        self.assertNotEqual(first["task_id"], second["task_id"])
