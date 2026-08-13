"""Continuing an acted take: the saved motion context, and what the film editor
does with it.

The promise the feature exists for: a dialogue scene that stops too early can be
carried on from its own last frame — same room, same faces, same voices, no cut —
instead of being re-shot from scratch. That only works if the context latent the
first render leaves on the worker still describes the clip in the cut, so most of
what is worth testing is about NOT continuing the wrong thing.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

from pipeline import comfyui, scene_context  # noqa: E402


def _workflow(name):
    return comfyui._fill_template(comfyui._load_workflow(name), {
        "UNET_NAME": "u", "CLIP_NAME": "c", "VIDEO_VAE": "v", "AUDIO_VAE": "a",
        "POSITIVE_PROMPT": "p", "WIDTH": 704, "HEIGHT": 384, "LENGTH": 124,
        "SEED": 1, "STEPS": 20, "EASYCACHE_THRESHOLD": 0.2, "LORA_NAME": "",
    })


def _by_class(workflow, class_type):
    return [n for n in workflow.values() if n.get("class_type") == class_type]


class ContextNamingTests(unittest.TestCase):
    def test_save_and_load_agree_on_the_filename(self):
        # The chain path computes the name it will LOAD from the index the
        # previous clip SAVED under. If these two ever drift, a chained render
        # silently loses its join.
        self.assertEqual(comfyui.context_latent_name("h3_context/x", 2),
                         "h3_context/x_00002.safetensors")

    def test_prefix_is_per_scene_and_filesystem_safe(self):
        a = scene_context.token_prefix(Path("/out/My Film (2026)"), 3)
        b = scene_context.token_prefix(Path("/out/My Film (2026)"), 4)
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("h3_context/"))
        self.assertNotIn(" ", a)
        self.assertNotIn("(", a)

    def test_prefix_is_stable_so_re_shoots_overwrite_their_own_slot(self):
        self.assertEqual(scene_context.token_prefix(Path("/out/film"), 2),
                         scene_context.token_prefix(Path("/out/film"), 2))


class SaveInjectionTests(unittest.TestCase):
    def test_ref2v_graph_learns_to_save_its_context(self):
        wf = _workflow("h3_ref2v.json")
        self.assertFalse(_by_class(wf, "MiniMaxH3MotionContextSaveLatent"))
        comfyui._add_context_save(wf, "h3_context/t", 1)
        saves = _by_class(wf, "MiniMaxH3MotionContextSaveLatent")
        self.assertEqual(len(saves), 1)
        sampler = comfyui._node_id(wf, "SamplerCustomAdvanced")
        self.assertEqual(saves[0]["inputs"]["latent"], [sampler, 0])
        self.assertEqual(saves[0]["inputs"]["clip_index"], 1)

    def test_chain_graph_keeps_its_own_save_node(self):
        # The chain templates already save through the template's own prefix;
        # injecting a second one would write the slot twice.
        wf = comfyui._fill_template(comfyui._load_workflow("h3_ref2v_chain_a.json"), {
            "UNET_NAME": "u", "CLIP_NAME": "c", "VIDEO_VAE": "v", "AUDIO_VAE": "a",
            "POSITIVE_PROMPT": "p", "WIDTH": 704, "HEIGHT": 384, "LENGTH": 124,
            "SEED": 1, "STEPS": 20, "EASYCACHE_THRESHOLD": 0.2, "LORA_NAME": "",
            "CONTEXT_PREFIX": "h3_context/x", "CLIP_INDEX": 1,
        })
        comfyui._add_context_save(wf, "h3_context/other", 5)
        saves = _by_class(wf, "MiniMaxH3MotionContextSaveLatent")
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0]["inputs"]["filename_prefix"], "h3_context/x")


class MotionContextSurgeryTests(unittest.TestCase):
    def test_continuation_graph_matches_the_chain_template(self):
        built = _workflow("h3_ref2v.json")
        comfyui._add_motion_context(built, "h3_context/x_00001.safetensors")

        ctx = _by_class(built, "MiniMaxH3MotionContext")[0]["inputs"]
        trim = _by_class(built, "MiniMaxH3MotionContextTrim")[0]["inputs"]
        load = _by_class(built, "MiniMaxH3MotionContextLoadLatent")[0]["inputs"]
        ref = comfyui._ref_node_id(built)

        self.assertEqual(load["latent_path"], "h3_context/x_00001.safetensors")
        # Conditioned on the reference node, pinned frames trimmed off the front.
        self.assertEqual(ctx["conditioning"], [ref, 0])
        self.assertEqual(ctx["latent"], [ref, 1])
        self.assertEqual(ctx["context_length"], str(comfyui.H3_CHAIN_CONTEXT_FRAMES))
        self.assertEqual(trim["match_tail"], True)
        self.assertEqual(trim["fps"], comfyui.H3_FPS)

        # The sampler now follows the context, and the video is built from the
        # trimmed frames rather than the raw decode.
        guider = built[comfyui._node_id(built, "BasicGuider")]["inputs"]
        create = built[comfyui._node_id(built, "CreateVideo")]["inputs"]
        ctx_id = next(k for k, v in built.items()
                      if v.get("class_type") == "MiniMaxH3MotionContext")
        trim_id = next(k for k, v in built.items()
                       if v.get("class_type") == "MiniMaxH3MotionContextTrim")
        self.assertEqual(guider["conditioning"], [ctx_id, 0])
        self.assertEqual(create["images"], [trim_id, 0])
        self.assertEqual(create["audio"], [trim_id, 1])

    def test_a_turbo_take_continues_on_the_turbo_sampler(self):
        # Why the graph is patched instead of loading the static chain template:
        # the chain files carry the base sampler only, and a continuation sampled
        # differently from the clip it joins is a change of look mid-scene.
        wf = _workflow("h3_ref2v_turbo.json")
        comfyui._add_motion_context(wf, "h3_context/x_00001.safetensors")
        self.assertTrue(_by_class(wf, "MiniMaxH3TurboSampler"))
        self.assertTrue(_by_class(wf, "MiniMaxH3TurboLoRA"))


class ContinueRenderTests(unittest.TestCase):
    """generate_video_h3_ref_continue, with the worker mocked out."""

    def setUp(self):
        self.engine = {"key": "h3ref", "unet": "u", "clip": "c", "video_vae": "v",
                       "audio_vae": "a", "steps": 20}
        self.tmp = Path(tempfile.mkdtemp(prefix="spielbot-cont-"))
        self.captured = {}

    def _run(self, **kw):
        def fake_submit(workflow, out, **sub):
            self.captured["workflow"] = workflow
            self.captured.update(sub)
            Path(out).write_bytes(b"clip")
            return out
        with mock.patch.object(comfyui, "_upload_image", return_value="p.png"), \
             mock.patch.object(comfyui, "_upload_audio", return_value="v.wav"), \
             mock.patch.object(comfyui, "check_engine_supported"), \
             mock.patch.object(comfyui, "_h3_submit", side_effect=fake_submit):
            return comfyui.generate_video_h3_ref_continue(
                self.engine, "prompt", [self.tmp / "face.png"], self.tmp / "out.mp4",
                context_latent="h3_context/x_00001.safetensors",
                context_token="h3_context/x", clip_index=2,
                ref_audios=[self.tmp / "voice.wav"], **kw)

    def test_samples_longer_than_it_delivers(self):
        from pipeline import cadence
        self._run(duration_seconds=6.0)
        # The pinned frames arrive at the head of the clip and are trimmed, so
        # six delivered seconds have to be paid for with more than six sampled.
        self.assertEqual(self.captured["length"],
                         comfyui.h3_frame_count(6.0 + cadence.CHAIN_JOIN_SECS))
        self.assertGreater(self.captured["length"], comfyui.h3_frame_count(6.0))

    def test_wires_the_scene_refs_and_saves_the_next_context(self):
        self._run(duration_seconds=5.0)
        wf = self.captured["workflow"]
        ref = wf[comfyui._ref_node_id(wf)]["inputs"]
        # Dotted autogrow keys — a flat key renders with no references at all.
        self.assertEqual(ref["ref_images.ref_image_0"][1], 0)
        self.assertIn("ref_audios.ref_audio_0", ref)
        save = _by_class(wf, "MiniMaxH3MotionContextSaveLatent")[0]["inputs"]
        self.assertEqual((save["filename_prefix"], save["clip_index"]), ("h3_context/x", 2))

    def test_refuses_without_a_portrait(self):
        with self.assertRaises(ValueError):
            comfyui.generate_video_h3_ref_continue(
                self.engine, "p", [], self.tmp / "o.mp4",
                context_latent="l", context_token="t", clip_index=2)


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-wd-"))
        self.final = self.wd / "scene_01_final.mp4"
        self.final.write_bytes(b"take one")

    def _save(self):
        scene_context.save(self.wd, 1, latent="h3_context/f_s01_t1_00001.safetensors",
                           token="h3_context/f_s01_t1", next_index=2,
                           comfy_url="http://w1:8188", engine="h3ref")

    def test_unrendered_scene_cannot_be_continued(self):
        self.assertIsNone(scene_context.load(self.wd, 1))
        self.assertFalse(scene_context.continuable(self.wd, 1, self.final))

    def test_saved_and_stamped_take_can_be_continued(self):
        self._save()
        # Not until it is bound to the clip that actually landed in the cut.
        self.assertFalse(scene_context.continuable(self.wd, 1, self.final))
        scene_context.stamp_final(self.wd, 1, self.final)
        self.assertTrue(scene_context.continuable(self.wd, 1, self.final))

    def test_a_different_take_in_the_cut_is_not_continuable(self):
        # Selecting an older take (or trimming this one) leaves the film holding
        # a clip the saved latent does not end on — continuing it would splice
        # one take onto another.
        self._save()
        scene_context.stamp_final(self.wd, 1, self.final)
        self.final.write_bytes(b"a completely different take")
        self.assertFalse(scene_context.continuable(self.wd, 1, self.final))

    def test_save_merges_rather_than_replaces(self):
        self._save()
        scene_context.save(self.wd, 1, next_index=3)
        data = scene_context.load(self.wd, 1)
        self.assertEqual(data["next_index"], 3)
        self.assertEqual(data["comfy_url"], "http://w1:8188")

    def test_a_corrupt_note_is_simply_not_continuable(self):
        scene_context.path(self.wd, 1).write_text("{not json", encoding="utf-8")
        self.assertIsNone(scene_context.load(self.wd, 1))
        self.assertFalse(scene_context.continuable(self.wd, 1, self.final))


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.saved = None

    def scene_rows(self, job_id):
        return self.rows

    def get_scene(self, job_id, sid):
        return next((r for r in self.rows if int(r.get("id") or 0) == int(sid)), None)

    def upsert_scene(self, job_id, sid, **kw):
        self.saved = kw

    def close(self):
        pass


class EndpointGuardTests(unittest.TestCase):
    """What the endpoint refuses. Every one of these would otherwise end as a
    join between two unrelated takes."""

    def setUp(self):
        import webapp.backend.main as backend
        self.backend = backend
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-wd-"))
        self.final = self.wd / "scene_01_final.mp4"
        self.final.write_bytes(b"take one")
        self.row = {"id": 1, "title": "t", "metadata": {"mode": "dialogue", "lines": []}}

    def _call(self, rows=None):
        backend = self.backend
        body = backend.FilmContinueBody(work_dir=str(self.wd), seconds=5)
        store = _FakeStore(self.row if rows is None else rows)
        with mock.patch.object(backend.gapp, "OUTPUT_DIR", self.wd.parent), \
             mock.patch.object(backend.DurableStore, "default", return_value=store), \
             mock.patch.object(backend.threading, "Thread") as T:
            out = backend.continue_film_scene(1, body)
            self.started = T.called
            return out

    def _stamped(self):
        scene_context.save(self.wd, 1, latent="l", token="t", next_index=2,
                           comfy_url="http://w1:8188")
        scene_context.stamp_final(self.wd, 1, self.final)

    def test_take_shot_before_the_feature_existed(self):
        with self.assertRaises(self.backend.HTTPException) as e:
            self._call(rows=[self.row])
        self.assertIn("shoot the scene again", str(e.exception.detail).lower())

    def test_take_replaced_since_the_context_was_saved(self):
        self._stamped()
        self.final.write_bytes(b"another take entirely")
        with self.assertRaises(self.backend.HTTPException) as e:
            self._call(rows=[self.row])
        self.assertIn("not the take", str(e.exception.detail))

    def test_narrated_scene_is_refused(self):
        self._stamped()
        narrated = {"id": 1, "title": "t", "metadata": {"mode": "narration"}}
        with self.assertRaises(self.backend.HTTPException) as e:
            self._call(rows=[narrated])
        self.assertIn("acted", str(e.exception.detail))

    def test_an_acted_take_starts_a_render(self):
        self._stamped()
        out = self._call(rows=[self.row])
        self.assertTrue(out["ok"])
        self.assertTrue(out["task_id"].startswith("continue_01_"))
        self.assertTrue(self.started)


class ContinueWorkerTests(unittest.TestCase):
    """_run_scene_continue: the join, and what it leaves behind."""

    def setUp(self):
        import webapp.backend.main as backend
        self.backend = backend
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-wd-"))
        self.final = self.wd / "scene_01_final.mp4"
        self.final.write_bytes(b"take one")
        scene_context.save(self.wd, 1, latent="h3_context/f_s01_t1_00001.safetensors",
                           token="h3_context/f_s01_t1", next_index=2,
                           comfy_url="http://w1:8188", engine="h3ref")
        scene_context.stamp_final(self.wd, 1, self.final)
        self.row = {"id": 1, "title": "t", "metadata": {
            "mode": "dialogue", "lines": [{"speaker": "Ana", "text": "Hello."}],
            "cast": ["Ana"]}}

    def _run(self, lines=None):
        backend = self.backend
        import pipeline.assembler as assembler
        import resume_generation

        pool = mock.Mock()
        pool.urls = ["http://w1:8188"]
        pool.acquire.return_value = "http://w1:8188"

        def fake_continue(scene, wd, cfg, **kw):
            self.continue_kwargs = kw
            out = Path(wd) / "scene_01_continue.mp4"
            out.write_bytes(b"more")
            return out

        def fake_concat(chunks, out):
            Path(out).write_bytes(b"".join(Path(c).read_bytes() for c in chunks))
            return Path(out)

        self.store = _FakeStore([self.row])
        body = backend.FilmContinueBody(work_dir=str(self.wd), seconds=5,
                                        direction="she looks up", lines=lines or [])
        with mock.patch.object(backend, "_shared_edit_render_pool", return_value=pool), \
             mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend.gapp, "_persist_script_snapshot"), \
             mock.patch.object(backend.DurableStore, "default", return_value=self.store), \
             mock.patch.object(resume_generation, "continue_performance_scene",
                               side_effect=fake_continue), \
             mock.patch.object(assembler, "_concat_video_chunks", side_effect=fake_concat):
            backend._run_scene_continue("tid1", self.wd, 1, {}, self.row, body)
        return backend._film_tasks.get("tid1")

    def test_the_continuation_is_joined_onto_the_take(self):
        task = self._run()
        self.assertEqual(task.get("status"), "done", msg=task)
        self.assertEqual(self.final.read_bytes(), b"take onemore")
        # The scene's own file, not a leftover.
        self.assertFalse((self.wd / "scene_01_continue.mp4").exists())

    def test_the_shorter_take_is_kept_as_a_version(self):
        self._run()
        from pipeline import video_history
        takes = video_history.history(self.wd, 1)
        self.assertGreaterEqual(len(takes["versions"]), 2)

    def test_the_next_continuation_starts_where_this_one_stops(self):
        self._run()
        ctx = scene_context.load(self.wd, 1)
        self.assertEqual(ctx["latent"], "h3_context/f_s01_t1_00002.safetensors")
        self.assertEqual(ctx["next_index"], 3)
        # And it is bound to the longer cut, so Continue stays offered.
        self.assertTrue(scene_context.continuable(self.wd, 1, self.final))

    def test_spoken_lines_join_the_scene_script(self):
        # A scene whose video says more than its text is a scene whose subtitles
        # stop early.
        self._run(lines=[{"speaker": "Ana", "text": "Wait — one more thing."}])
        self.assertIsNotNone(self.store.saved)
        saved_lines = self.store.saved["metadata"]["lines"]
        self.assertEqual([ln["text"] for ln in saved_lines],
                         ["Hello.", "Wait — one more thing."])
        self.assertIn("one more thing", self.store.saved["narration"])

    def test_a_held_beat_leaves_the_script_alone(self):
        self._run(lines=[])
        self.assertIsNone(self.store.saved)

    def test_the_editors_note_reaches_the_render(self):
        self._run()
        self.assertEqual(self.continue_kwargs["direction"], "she looks up")

    def test_a_failed_render_leaves_the_film_untouched(self):
        backend = self.backend
        import resume_generation
        pool = mock.Mock()
        pool.urls = ["http://w1:8188"]
        pool.acquire.return_value = "http://w1:8188"
        body = backend.FilmContinueBody(work_dir=str(self.wd), seconds=5)
        with mock.patch.object(backend, "_shared_edit_render_pool", return_value=pool), \
             mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend.DurableStore, "default", return_value=_FakeStore([self.row])), \
             mock.patch.object(resume_generation, "continue_performance_scene",
                               side_effect=RuntimeError("worker died")):
            backend._run_scene_continue("tid2", self.wd, 1, {}, self.row, body)
        self.assertEqual(backend._film_tasks["tid2"]["status"], "error")
        self.assertEqual(self.final.read_bytes(), b"take one")
        self.assertFalse((self.wd / "scene_01_final.staging.mp4").exists())

    def test_the_worker_that_shot_it_has_to_be_there(self):
        backend = self.backend
        pool = mock.Mock()
        pool.urls = ["http://w9:8188"]        # a different machine
        body = backend.FilmContinueBody(work_dir=str(self.wd), seconds=5)
        with mock.patch.object(backend, "_shared_edit_render_pool", return_value=pool), \
             mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend.DurableStore, "default", return_value=_FakeStore([self.row])):
            backend._run_scene_continue("tid3", self.wd, 1, {}, self.row, body)
        task = backend._film_tasks["tid3"]
        self.assertEqual(task["status"], "error")
        self.assertIn("motion context", task.get("error", ""))
        self.assertEqual(self.final.read_bytes(), b"take one")


class SceneFilesFlagTests(unittest.TestCase):
    def test_the_editor_only_offers_continue_where_it_can_work(self):
        import webapp.backend.main as backend
        wd = Path(tempfile.mkdtemp(prefix="spielbot-wd-"))
        final = wd / "scene_02_final.mp4"
        final.write_bytes(b"x" * 20_000)
        self.assertFalse(backend._film_scene_files(wd, 2)["can_continue"])
        scene_context.save(wd, 2, latent="l", token="t", next_index=2,
                           comfy_url="http://w1:8188")
        scene_context.stamp_final(wd, 2, final)
        self.assertTrue(backend._film_scene_files(wd, 2)["can_continue"])


class DirectionBlockTests(unittest.TestCase):
    def test_the_note_reaches_the_prompt(self):
        from pipeline import performance
        prompt = performance.build_h3_prompt(
            {"lines": [{"speaker": "Ana", "delivery": "flat", "text": "Hi."}],
             "direction": "she finally looks up"},
            picture_names=["Ana"])
        self.assertIn("[DIRECTION]\nshe finally looks up", prompt)

    def test_scenes_without_a_note_are_unchanged(self):
        from pipeline import performance
        self.assertNotIn("[DIRECTION]", performance.build_h3_prompt({"lines": []}))

    def test_a_hand_edited_prompt_does_not_swallow_the_note(self):
        # The override is the prompt for the SCENE; the note is for THIS take.
        from pipeline import performance
        prompt = performance.build_h3_prompt(
            {"prompt_override": "[SCENE]\nA kitchen.", "direction": "slower this time"})
        self.assertTrue(prompt.startswith("[SCENE] A kitchen."))
        self.assertIn("[DIRECTION]\nslower this time", prompt)


class ReshootDirectionTests(unittest.TestCase):
    """The film editor's "Shoot again" note has to reach the model — it used to
    be written to the render config, which nothing read."""

    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-wd-"))

    def _reshoot(self, direction):
        import resume_generation as rg
        from pipeline.llm import Scene

        scene = Scene(id=1, title="t", image_prompt="", video_prompt="",
                      narration="Hello.", mode="dialogue",
                      lines=[{"speaker": "Ana", "delivery": "flat", "text": "Hello."}])
        refs = {"pictures": [{"name": "Ana", "kind": "character",
                              "path": str(self.wd / "ana.png")}],
                "audios": [{"name": "Ana", "path": str(self.wd / "ana.wav")}]}
        seen = {}

        def generate(engine, prompt, images, out, **kw):
            seen["prompt"] = prompt
            Path(out).write_bytes(b"clip")
            return out

        with mock.patch("app.resolve_performance_references", return_value=refs), \
             mock.patch.object(rg._engines, "resolve_reference",
                               return_value={"key": "h3ref", "steps": 20}), \
             mock.patch.object(rg.shot_gate, "available", return_value=False), \
             mock.patch.object(rg, "ensure_video_resolution"), \
             mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=generate):
            rg.render_performance_scene(
                scene, self.wd, {}, comfy_url="http://w1:8188",
                vid_width=832, vid_height=480, style_name="s", direction=direction)
        return seen["prompt"]

    def test_the_note_reaches_the_prompt(self):
        self.assertIn("[DIRECTION]\nmake her angrier", self._reshoot("make her angrier"))

    def test_a_plain_reshoot_has_no_direction_block(self):
        self.assertNotIn("[DIRECTION]", self._reshoot(""))


class RenderSavesContextTests(unittest.TestCase):
    """The renderer's half of the deal: every acted take leaves a continuation
    point, and a gate retake's reject never becomes one."""

    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-wd-"))

    def _render(self, generate):
        import resume_generation as rg
        from pipeline.llm import Scene

        scene = Scene(id=1, title="t", image_prompt="", video_prompt="",
                      narration="Hello.", mode="dialogue",
                      lines=[{"speaker": "Ana", "delivery": "flat", "text": "Hello."}])
        refs = {"pictures": [{"name": "Ana", "kind": "character", "path": str(self.wd / "ana.png")}],
                "audios": [{"name": "Ana", "path": str(self.wd / "ana.wav")}]}
        with mock.patch("app.resolve_performance_references", return_value=refs), \
             mock.patch.object(rg._engines, "resolve_reference",
                               return_value={"key": "h3ref", "steps": 20}), \
             mock.patch.object(rg.shot_gate, "available", return_value=False), \
             mock.patch.object(rg, "ensure_video_resolution"), \
             mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=generate):
            rg._render_performance_clip(
                scene, {"mode": "dialogue", "cast": ["Ana"], "seconds": 6,
                        "lines": [{"speaker": "Ana", "delivery": "flat", "text": "Hello."}]},
                self.wd, {}, self.wd / "scene_01_final.mp4",
                comfy_url="http://w1:8188", vid_width=832, vid_height=480,
                style_name="s")

    def test_a_take_records_where_it_can_be_picked_up(self):
        seen = {}

        def generate(engine, prompt, images, out, **kw):
            seen.update(kw)
            Path(out).write_bytes(b"clip")
            return out

        self._render(generate)
        ctx = scene_context.load(self.wd, 1)
        self.assertEqual(ctx["comfy_url"], "http://w1:8188")
        self.assertEqual(ctx["latent"], comfyui.context_latent_name(seen["context_token"], 1))
        self.assertEqual(ctx["next_index"], 2)
        # Saved under this scene's own prefix, on the worker that shot it.
        self.assertTrue(seen["context_token"].startswith(
            scene_context.token_prefix(self.wd, 1)))


if __name__ == "__main__":
    unittest.main()
