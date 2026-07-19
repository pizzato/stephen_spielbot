"""Film-editor re-render of dialogue/silent scenes (the edit screen's Video button).

The per-scene re-render endpoints were built for narration scenes only:
_run_video_rerender demanded scene_NN_narration.wav and muxed it over an LTX
clip, so a dialogue scene added post-render (issue #193 add-scene flow) died
with "Narration file missing" — its audio is per-line wavs voiced by the
dialogue path, which lived only in the full render (resume_generation).

Now: mode "dialogue" + component "video" dispatches to _run_dialogue_rerender
(per-line TTS + EchoMimic talking heads + LTX silent shots via
pipeline.dialogue_render); component "narration" is rejected for non-narration
scenes; and a silent scene synthesizes its silent "narration" track on the fly
instead of erroring."""
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

import app
import webapp.backend.main as backend
from pipeline.orchestrator import DurableStore, job_id_from_work_dir
from test_styles import TempConfigCase, _style


class _FakePool:
    def acquire(self):
        return "http://comfy:8188"

    def release(self, url):
        pass


def _png(path: Path, width: int, height: int) -> None:
    from PIL import Image
    Image.new("RGB", (width, height), (10, 20, 30)).save(path, "PNG")


class _SceneCase(TempConfigCase):
    """A film work dir + durable-store scene row to re-render."""

    def _seed(self, sid: int, metadata: dict | None, **row_kw):
        self.write_config({
            "styles": [_style("Hero")], "default_style": "Hero",
            "characters_migrated_v2": True,
            "echomimic_workers": ["http://echo:8000"],
            "tts_workers": ["http://tts:8000"],
        })
        wd = self.output_dir / "vid-20260101-000000"
        wd.mkdir(parents=True, exist_ok=True)
        job_id = job_id_from_work_dir(wd)
        store = DurableStore.default()
        try:
            store.create_or_update_job(job_id, wd, "Test film")
            store.upsert_scene(job_id, sid, title=f"Scene {sid}",
                               metadata=metadata, **row_kw)
            row = next(r for r in store.scene_rows(job_id) if int(r["id"]) == sid)
        finally:
            store.close()
        return wd, row


class RerenderDispatchTests(_SceneCase):
    def _call(self, wd, sid, component):
        calls = []

        def fake_logged(target, tid, wd_, sid_, component_, jc, row, instruction=""):
            calls.append(target)

        with mock.patch.object(backend, "_run_rerender_logged", fake_logged):
            r = backend.rerender_film_scene(
                sid, backend.RerenderSceneBody(work_dir=str(wd), component=component))
            self.assertTrue(r["ok"])
            for _ in range(200):  # the worker runs on a daemon thread
                if calls:
                    break
                time.sleep(0.01)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_dialogue_scene_video_uses_dialogue_worker(self):
        wd, _ = self._seed(9, {"mode": "dialogue",
                               "lines": [{"speaker": "Stephen", "text": "hi"}]})
        self.assertIs(self._call(wd, 9, "video"), backend._run_dialogue_rerender)

    def test_narration_scene_video_uses_classic_worker(self):
        wd, _ = self._seed(1, None, narration="Once upon a time.")
        self.assertIs(self._call(wd, 1, "video"), backend._run_video_rerender)

    def test_dialogue_scene_rejects_narration_component(self):
        wd, _ = self._seed(9, {"mode": "dialogue",
                               "lines": [{"speaker": "Stephen", "text": "hi"}]})
        with self.assertRaises(HTTPException) as ctx:
            backend.rerender_film_scene(
                9, backend.RerenderSceneBody(work_dir=str(wd), component="narration"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Video", ctx.exception.detail)

    def test_silent_scene_rejects_narration_component(self):
        wd, _ = self._seed(3, {"mode": "silent", "duration": 4.0})
        with self.assertRaises(HTTPException) as ctx:
            backend.rerender_film_scene(
                3, backend.RerenderSceneBody(work_dir=str(wd), component="narration"))
        self.assertEqual(ctx.exception.status_code, 400)


class DialogueRerenderWorkerTests(_SceneCase):
    def test_renders_scene_via_dialogue_path(self):
        wd, row = self._seed(9, {"mode": "dialogue",
                                 "lines": [{"speaker": "Stephen", "text": "hi"}]})
        jc = {"resolution": "Landscape HD (1024×576)", "style_name": "Hero",
              "tts_engine": "openf5"}
        captured = {}

        def fake_render(scene, work_dir, **kw):
            captured["scene"] = scene
            captured.update(kw)
            final = Path(work_dir) / f"scene_{scene.id:02d}_final.mp4"
            final.write_bytes(b"x" * 20_000)
            return final

        with mock.patch.object(backend, "_shared_edit_render_pool", _FakePool), \
             mock.patch.object(app, "generate_dialogue_shot_stills", lambda *a, **k: 0), \
             mock.patch("pipeline.dialogue_render.render_dialogue_scene", fake_render), \
             mock.patch("pipeline.assembler.fit_video_canvas", lambda p, w, h: p):
            backend._run_dialogue_rerender("tid-dlg", wd, 9, jc, row)

        self.assertEqual(backend._film_tasks["tid-dlg"]["status"], "done")
        self.assertTrue((wd / "scene_09_final.mp4").exists())
        self.assertEqual(captured["echomimic_host"], "http://echo:8000")
        self.assertEqual(captured["tts_host"], "http://tts:8000")
        self.assertEqual(captured["tts_engine"], "openf5")
        self.assertEqual(captured["canvas"], (1024, 576))
        self.assertEqual(captured["scene"].mode, "dialogue")
        self.assertEqual(captured["scene"].lines[0]["speaker"], "Stephen")
        # The new final is kept as a take the user can flip back to.
        from pipeline import video_history
        self.assertEqual(len(video_history.history(wd, 9)["versions"]), 1)

    def test_missing_echomimic_workers_is_a_clear_error(self):
        wd, row = self._seed(9, {"mode": "dialogue",
                                 "lines": [{"speaker": "Stephen", "text": "hi"}]})
        self.write_config({
            "styles": [_style("Hero")], "default_style": "Hero",
            "characters_migrated_v2": True,
        })
        backend._run_dialogue_rerender("tid-noecho", wd, 9, {}, row)
        task = backend._film_tasks["tid-noecho"]
        self.assertEqual(task["status"], "error")
        self.assertIn("echomimic_workers", task["error"])

    def test_no_shots_is_a_clear_error(self):
        wd, row = self._seed(9, {"mode": "dialogue", "lines": []})
        backend._run_dialogue_rerender("tid-noshots", wd, 9, {}, row)
        task = backend._film_tasks["tid-noshots"]
        self.assertEqual(task["status"], "error")
        self.assertIn("no shots", task["error"])


class SilentSceneVideoRerenderTests(_SceneCase):
    def test_silent_scene_synthesizes_its_silent_track(self):
        """No scene_NN_narration.wav + mode silent → a silent wav of the scene's
        duration is written and the render proceeds (no 'Narration file missing')."""
        wd, row = self._seed(3, {"mode": "silent", "duration": 2.0})
        jc = {"resolution": "Landscape HD (1024×576)", "style_name": "Hero"}
        # A preview at the render resolution so the worker reuses it as the
        # first frame instead of calling the image engine.
        _png(wd / "scene_03_preview.png", 1024, 576)

        def fake_gen_video(scene, work_dir, nar_dur, *a, **kw):
            clip = Path(work_dir) / f"scene_{scene.id:02d}_clip_01.mp4"
            clip.write_bytes(b"v" * 20_000)
            return clip, None

        def fake_mux(video, audio, out):
            Path(out).write_bytes(b"m" * 20_000)
            return Path(out)

        # The endpoint seeds the running task entry before spawning the worker.
        backend._film_tasks["tid-silent"] = {"status": "running", "step": "video"}
        with mock.patch.object(backend, "_shared_edit_render_pool", _FakePool), \
             mock.patch("pipeline.scene_video.generate_scene_video", fake_gen_video), \
             mock.patch("pipeline.assembler.mux_video_audio", fake_mux):
            backend._run_video_rerender("tid-silent", wd, 3, jc, row)

        task = backend._film_tasks["tid-silent"]
        self.assertEqual(task.get("status"), "done", task)
        wav = wd / "scene_03_narration.wav"
        self.assertTrue(wav.exists())
        from pipeline.assembler import _get_duration
        self.assertAlmostEqual(_get_duration(wav), 2.0, delta=0.1)
        self.assertTrue((wd / "scene_03_final.mp4").exists())

    def test_narration_scene_without_wav_still_errors(self):
        wd, row = self._seed(1, None, narration="Hello there.")
        jc = {"resolution": "Landscape HD (1024×576)", "style_name": "Hero"}
        _png(wd / "scene_01_preview.png", 1024, 576)

        with mock.patch.object(backend, "_shared_edit_render_pool", _FakePool):
            backend._run_video_rerender("tid-nar", wd, 1, jc, row)

        task = backend._film_tasks["tid-nar"]
        self.assertEqual(task.get("status"), "error", task)
        self.assertIn("Narration file missing", task["error"])


if __name__ == "__main__":
    unittest.main()
