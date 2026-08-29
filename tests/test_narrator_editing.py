import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app
import webapp.backend.main as backend
from pipeline.orchestrator import DurableStore, job_id_from_work_dir


class NarratorEditCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-narrators-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.config_file = tmp / "config" / "config.yaml"
        self.config_file.parent.mkdir(parents=True)
        self.voices_dir = self.config_file.parent / "voices"
        self.voices_dir.mkdir()
        self.output_dir = tmp / "videos"
        self.output_dir.mkdir()
        for target, attr, value in [
            (app, "CONFIG_FILE", self.config_file),
            (app, "VOICES_DIR", self.voices_dir),
            (app, "OUTPUT_DIR", self.output_dir),
        ]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        db = mock.patch.dict(os.environ, {"SPIELBOT_ORCHESTRATOR_DB": str(tmp / "orchestrator.sqlite3")})
        db.start()
        self.addCleanup(db.stop)

    def _work_dir(self, name: str = "film") -> Path:
        wd = self.output_dir / name
        wd.mkdir()
        return wd

    def _register_scene(self, wd: Path, scene_id: int = 1) -> str:
        job_id = job_id_from_work_dir(wd)
        store = DurableStore.default()
        try:
            store.create_or_update_job(job_id, wd, "Film", config={"video_title": "Film"}, metadata={})
            store.upsert_scene(
                job_id,
                scene_id,
                title=f"Scene {scene_id}",
                image_prompt="image",
                video_prompt="video",
                narration="hello",
            )
        finally:
            store.close()
        return job_id


class SceneNarratorTests(NarratorEditCase):
    def test_update_scene_persists_voice_metadata_and_script_snapshot(self):
        wd = self._work_dir()
        job_id = self._register_scene(wd)

        backend.update_scene(
            job_id,
            1,
            backend.SceneUpdate(
                title="Scene 1",
                image_prompt="image",
                video_prompt="video",
                narration="hello",
                voice="Narrator B",
            ),
        )

        store = DurableStore.default()
        try:
            row = store.get_scene(job_id, 1)
        finally:
            store.close()
        self.assertEqual(row["metadata"]["voice"], "Narrator B")
        script = json.loads((wd / "script.json").read_text())
        self.assertEqual(script[0]["metadata"]["voice"], "Narrator B")

        loaded = backend.film_scenes(work_dir=str(wd))
        self.assertEqual(loaded["scenes"][0]["voice"], "Narrator B")
        self.assertEqual(loaded["scenes"][0]["effective_voice"], "Narrator B")


class RemixNarratorTests(NarratorEditCase):
    def test_remix_narrator_updates_film_and_every_scene(self):
        wd = self._work_dir()
        self._register_scene(wd, 1)
        self._register_scene(wd, 2)
        voice_path = self.voices_dir / "narrator-b.wav"
        voice_path.write_bytes(b"voice")
        (wd / "job_config.json").write_text(json.dumps({
            "default_voice": "Old",
            "voice_ref": "",
            "voice_vol": 100,
            "music_vol": 18,
            "ambient_vol": 0,
        }))
        (wd / "background_music.wav").write_bytes(b"music")
        for sid in (1, 2):
            (wd / f"scene_{sid:02d}_video.mp4").write_bytes(b"x" * 20000)
            (wd / f"scene_{sid:02d}_final.mp4").write_bytes(b"old" * 7000)

        def fake_tts(_text, out_path, **kwargs):
            Path(out_path).write_bytes(b"RIFF" + b"x" * 2000)

        def fake_mux(_video, _audio, out_path, **_kwargs):
            Path(out_path).write_bytes(b"muxed" * 5000)

        def fake_concat(_scenes, out_path, **kw):
            Path(out_path).write_bytes(b"combined" * 5000)

        def fake_mix(_combined, _music, out_path, **_kwargs):
            Path(out_path).write_bytes(b"final" * 5000)

        cfg = {
            "voices": [{"name": "Narrator B", "path": str(voice_path)}],
            "tts_workers": [],
        }
        with mock.patch.object(app, "load_config", return_value=cfg), \
                mock.patch("pipeline.tts_worker.generate_narration", side_effect=fake_tts) as tts, \
                mock.patch("pipeline.assembler.mux_video_audio", side_effect=fake_mux), \
                mock.patch("pipeline.assembler.concatenate_scenes", side_effect=fake_concat), \
                mock.patch("pipeline.assembler.mix_background_music", side_effect=fake_mix):
            backend._run_remix_narrator("narrator-test", wd, "Narrator B")

        self.assertEqual(backend._film_tasks["narrator-test"]["status"], "done")
        jc = json.loads((wd / "job_config.json").read_text())
        self.assertEqual(jc["default_voice"], "Narrator B")
        self.assertEqual(jc["voice_ref"], str(voice_path))
        self.assertEqual(tts.call_count, 2)
        for call in tts.call_args_list:
            self.assertEqual(call.kwargs["reference_wav"], voice_path)

        job_id = job_id_from_work_dir(wd)
        store = DurableStore.default()
        try:
            rows = store.scene_rows(job_id)
        finally:
            store.close()
        self.assertEqual([r["metadata"]["voice"] for r in rows], ["Narrator B", "Narrator B"])


if __name__ == "__main__":
    unittest.main()
