"""Suno settings persist; film rendering never spends Suno account credits."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
from pipeline.orchestrator import DurableStore, job_id_from_work_dir, task_id


class SunoStyleTests(unittest.TestCase):
    def test_model_and_voice_inherit_without_copying_into_child(self):
        cfg = app._ensure_styles({
            "styles": [{"name": "Parent", "music_engine": "suno",
                        "suno_model": " v6-mini ", "suno_voice_id": " voice-123 "},
                       {"name": "Child", "parent": "Parent"}],
            "default_style": "Child",
        })
        settings = app.style_settings(cfg, "Child")
        self.assertEqual(settings["music_engine"], "suno")
        self.assertEqual(settings["suno_model"], "v6-mini")
        self.assertEqual(settings["suno_voice_id"], "voice-123")
        self.assertEqual(cfg["default_suno_model"], "v6-mini")
        self.assertEqual(cfg["default_suno_voice_id"], "voice-123")
        self.assertNotIn("suno_model", cfg["styles"][1])
        self.assertNotIn("suno_voice_id", cfg["styles"][1])

    def test_existing_configs_use_account_default_model_and_voice(self):
        cfg = app._ensure_styles({"styles": [{"name": "Old"}], "default_style": "Old"})
        self.assertEqual(app.style_settings(cfg)["suno_model"], "")
        self.assertEqual(app.style_settings(cfg)["suno_voice_id"], "")


class SunoRoutingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work_dir = Path(tmp.name)
        self.store = DurableStore(self.work_dir / "orchestrator.sqlite3")
        self.addCleanup(self.store.close)
        self.job_id = job_id_from_work_dir(self.work_dir)
        self.scenes = [{"id": 1, "title": "Opening", "image_prompt": "Mountain",
                        "video_prompt": "Clouds move", "narration": "A new day."}]
        self.config = {"music_engine": "suno", "suno_model": "v6-mini",
                       "suno_voice_id": "voice-123", "tts_workers": ["tts-host"]}

    def test_working_file_cleanup_preserves_suno_requests_and_history_metadata(self):
        from pipeline import music_history, working_files

        folder = self.work_dir / "suno_requests" / "operation"
        folder.mkdir(parents=True)
        request = folder / "request.json"
        request.write_text(json.dumps({"clip_ids": ["clip-1", "clip-2"]}))
        audio = folder / "take_1.wav"
        audio.write_bytes(b"audio")
        music_history.record(self.work_dir, audio, provider="suno", remote_id="clip-1",
                             model="v6-mini", singing_voice_id="voice-123")
        before = (self.work_dir / "music_history.json").read_bytes()
        final = self.work_dir / "final.mp4"
        final.write_bytes(b"final")
        (self.work_dir / "scene_01_final.mp4").write_bytes(b"working video")
        removed = working_files.clean(self.work_dir, final)
        self.assertEqual(removed["files"], 1)
        self.assertTrue(request.is_file())
        self.assertTrue(audio.is_file())
        self.assertEqual((self.work_dir / "music_history.json").read_bytes(), before)
        self.assertEqual(music_history.history(self.work_dir)["versions"][0]["remote_id"], "clip-1")

    def test_render_plan_always_uses_minimax_for_background_music(self):
        for selected in ("suno", "ace-step", "minimax-music3"):
            with self.subTest(selected=selected):
                self.store.ensure_generation_plan(
                    self.job_id, self.work_dir, "Dawn", self.scenes,
                    {**self.config, "music_engine": selected})
                music = self.store.get_task(task_id(self.job_id, "music"))
                self.assertEqual(music.worker_kind, "comfy")
                self.assertEqual(music.payload["music_engine"], "minimax-music3")
                self.assertEqual(music.payload["resource_class"], "comfy:music")

    def _render_music(self, engine="suno", existing=False):
        import resume_generation as renderer
        from pipeline import suno

        wd = self.work_dir / f"{engine}-{existing}"
        wd.mkdir()
        (wd / "script.json").write_text(json.dumps(self.scenes))
        (wd / "scene_01_narration.wav").write_bytes(b"x" * 2000)
        if existing:
            (wd / "background_music.wav").write_bytes(b"approved-suno-track" * 1000)
        pool = mock.Mock()
        pool.acquire.return_value = "http://gpu:8188"

        class MusicFinished(Exception):
            pass

        def progress(path, percent, message):
            if message.startswith("Music ready."):
                raise MusicFinished()

        with mock.patch.object(renderer, "load_job_config", return_value={
                 **self.config, "music_engine": engine}), \
             mock.patch.object(renderer.DurableStore, "default", return_value=self.store), \
             mock.patch.object(renderer, "alive_workers", return_value=["http://gpu:8188"]), \
             mock.patch.object(renderer, "WorkerPool", return_value=pool), \
             mock.patch.object(renderer, "write_progress", side_effect=progress), \
             mock.patch.object(renderer, "_get_duration", return_value=8.0), \
             mock.patch.object(renderer, "generate_music") as comfy_generate, \
             mock.patch.object(suno, "call_tool") as cloud:
            with self.assertRaises(MusicFinished):
                renderer.main(wd)
        cloud.assert_not_called()
        return comfy_generate, pool, wd

    def test_rendering_always_makes_minimax_background_music(self):
        for engine in ("suno", "ace-step", "minimax-music3"):
            with self.subTest(engine=engine):
                generate, pool, _ = self._render_music(engine=engine)
                generate.assert_called_once()
                self.assertEqual(generate.call_args.kwargs["music_engine"], "minimax-music3")
                pool.acquire.assert_called_once()

    def test_rendering_reuses_the_approved_suno_track(self):
        generate, pool, wd = self._render_music(existing=True)
        generate.assert_not_called()
        pool.acquire.assert_not_called()
        self.assertEqual((wd / "background_music.wav").read_bytes(), b"approved-suno-track" * 1000)
