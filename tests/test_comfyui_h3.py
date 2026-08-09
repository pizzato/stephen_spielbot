"""MiniMax H3 I2V — frame/dimension math, workflow parameterization, dispatch."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import comfyui, engines


class H3MathTests(unittest.TestCase):
    def test_frame_count_grid(self):
        for secs in (None, 1.0, 4.0, 5.2, 6.0, 8.7, 14.9, 40.0):
            n = comfyui.h3_frame_count(secs)
            self.assertEqual(n % 17, 5, f"{secs}s -> {n} frames")
        # Short requests floor at the model minimum, long ones cap at 15 s.
        self.assertEqual(comfyui.h3_frame_count(1.0), comfyui.h3_frame_count(4.0))
        self.assertEqual(comfyui.h3_frame_count(40.0), comfyui.h3_frame_count(15.0))
        # 6 s at 24 fps rounds UP to the next valid count (144 → 158).
        self.assertEqual(comfyui.h3_frame_count(6.0), 158)

    def test_dimensions_snap_and_cap(self):
        # Under the pixel cap and already on the grid: unchanged.
        self.assertEqual(comfyui.h3_dimensions(832, 480), (832, 480))
        # Over the cap: scaled down, snapped to 32, aspect roughly kept.
        w, h = comfyui.h3_dimensions(1920, 1088)
        self.assertLessEqual(w * h, comfyui.H3_MAX_PIXELS)
        self.assertEqual(w % 32, 0)
        self.assertEqual(h % 32, 0)
        self.assertAlmostEqual(w / h, 1920 / 1088, delta=0.15)


class H3WorkflowTests(unittest.TestCase):
    def _generate(self, engine_key="minimax-h3"):
        eng = engines.resolve_video({}, engine_key)
        captured = {}

        def fake_queue(workflow, client_id, comfy_url=None):
            captured["workflow"] = workflow
            return "pid"

        outputs = [{"filename": "h3.mp4", "type": "output"}]
        with tempfile.TemporaryDirectory() as tmp:
            frame = Path(tmp) / "frame.png"
            frame.write_bytes(b"png")
            out = Path(tmp) / "out.mp4"
            with mock.patch.object(comfyui, "_upload_image", return_value="frame.png"), \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion") as waited, \
                 mock.patch.object(comfyui, "_get_outputs", return_value=outputs), \
                 mock.patch.object(comfyui, "_download_output",
                                   side_effect=lambda item, dest, comfy_url=None: dest):
                comfyui.generate_video_h3(
                    eng, "a cat sails a paper boat", frame, out,
                    width=1280, height=736, seed=7, duration_seconds=6.0,
                )
            captured["wait_kwargs"] = waited.call_args.kwargs
        return eng, captured

    def test_workflow_parameterization(self):
        eng, cap = self._generate()
        wf = cap["workflow"]
        i2v = wf["7"]["inputs"]
        self.assertTrue(i2v["prompt"].startswith("a cat sails a paper boat"))
        self.assertIn("diegetic", i2v["prompt"])  # audio steering applied
        # Quoted {{...}} placeholders must land as real ints, on H3's grids.
        self.assertIsInstance(i2v["width"], int)
        self.assertIsInstance(i2v["length"], int)
        self.assertEqual((i2v["width"], i2v["height"]), (1280, 736))
        self.assertEqual(i2v["length"] % 17, 5)
        # Engine-supplied model files reach the loaders.
        self.assertEqual(wf["1"]["inputs"]["unet_name"], eng["unet"])
        self.assertEqual(wf["2"]["inputs"]["clip_name"], eng["clip"])
        self.assertEqual(wf["3"]["inputs"]["vae_name"], eng["video_vae"])
        self.assertEqual(wf["4"]["inputs"]["vae_name"], eng["audio_vae"])
        self.assertEqual(wf["9"]["inputs"]["steps"], eng["steps"])
        self.assertEqual(wf["10"]["inputs"]["noise_seed"], 7)
        self.assertEqual(wf["16"]["inputs"]["format"], "mp4")
        # EasyCache sits between the loader and both model consumers.
        self.assertEqual(wf["17"]["class_type"], "EasyCache")
        self.assertEqual(wf["17"]["inputs"]["reuse_threshold"], eng["easycache_threshold"])
        self.assertEqual(wf["9"]["inputs"]["model"], ["17", 0])
        self.assertEqual(wf["11"]["inputs"]["model"], ["17", 0])

    def test_h3_uses_long_heartbeat_warmup(self):
        _, cap = self._generate()
        self.assertEqual(cap["wait_kwargs"]["heartbeat_warmup"],
                         comfyui._H3_HEARTBEAT_WARMUP)

    def test_turbo_workflow(self):
        eng, cap = self._generate("minimax-h3-turbo")
        wf = cap["workflow"]
        # The distillation LoRA replaces EasyCache in the model chain: both the
        # scheduler and the guider must see the patched model.
        self.assertEqual(wf["17"]["class_type"], "MiniMaxH3TurboLoRA")
        self.assertEqual(wf["17"]["inputs"]["model"], ["1", 0])
        self.assertEqual(wf["17"]["inputs"]["lora_name"], eng["lora"])
        self.assertEqual(wf["9"]["inputs"]["model"], ["17", 0])
        self.assertEqual(wf["11"]["inputs"]["model"], ["17", 0])
        self.assertNotIn("EasyCache", {n["class_type"] for n in wf.values()})
        # Video and audio ride different flow schedules — the few-step render
        # only works through the custom dual-clock sampler.
        self.assertEqual(wf["8"]["class_type"], "MiniMaxH3TurboSampler")
        self.assertEqual(wf["9"]["inputs"]["steps"], eng["steps"])
        self.assertEqual(wf["1"]["inputs"]["unet_name"], eng["unet"])


class VideoDispatchTests(unittest.TestCase):
    def test_dispatch_by_family(self):
        with mock.patch.object(comfyui, "generate_video_continuation") as ltx, \
             mock.patch.object(comfyui, "generate_video_h3") as h3:
            comfyui.generate_video_with_engine(None, "p", "n", Path("f"), Path("o"))
            ltx.assert_called_once()
            h3.assert_not_called()

            ltx.reset_mock()
            comfyui.generate_video_with_engine(
                engines.resolve_video({}, "ltx23"), "p", "n", Path("f"), Path("o"))
            ltx.assert_called_once()
            h3.assert_not_called()

            ltx.reset_mock()
            comfyui.generate_video_with_engine(
                engines.resolve_video({}, "minimax-h3"), "p", "n", Path("f"), Path("o"))
            h3.assert_called_once()
            ltx.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class H3ReferenceWorkflowTests(unittest.TestCase):
    """Ref2VA: portraits + voice clips instead of a first frame."""

    def _generate(self, engine_key="minimax-h3-ref", n_images=2, n_audios=1, **kwargs):
        eng = engines.resolve_reference({}, engine_key)
        captured = {}

        def fake_queue(workflow, client_id, comfy_url=None):
            captured["workflow"] = workflow
            return "pid"

        outputs = [{"filename": "h3ref.mp4", "type": "output"}]
        with tempfile.TemporaryDirectory() as tmp:
            imgs = []
            for i in range(n_images):
                p = Path(tmp) / f"char{i}.png"
                p.write_bytes(b"png")
                imgs.append(p)
            auds = []
            for i in range(n_audios):
                p = Path(tmp) / f"voice{i}.wav"
                p.write_bytes(b"wav")
                auds.append(p)
            out = Path(tmp) / "out.mp4"
            with mock.patch.object(comfyui, "_upload_image", side_effect=lambda p, comfy_url=None: p.name), \
                 mock.patch.object(comfyui, "_upload_audio", side_effect=lambda p, comfy_url=None: p.name), \
                 mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "_wait_for_completion"), \
                 mock.patch.object(comfyui, "_get_outputs", return_value=outputs), \
                 mock.patch.object(comfyui, "_download_output",
                                   side_effect=lambda item, dest, comfy_url=None: dest):
                comfyui.generate_video_h3_ref(
                    eng, "CHICO says exactly: \"hello\"", imgs, out,
                    ref_audios=auds, width=704, height=1280, seed=7,
                    duration_seconds=10.0, **kwargs)
        return eng, captured["workflow"]

    def _ref_inputs(self, workflow):
        node = next(n for n in workflow.values()
                    if n["class_type"] == "MiniMaxH3ReferenceToVideo")
        return node["inputs"]

    def test_references_use_the_dotted_autogrow_keys(self):
        # A flat "ref_image_0" is silently ignored by ComfyUI (finalize_prefix
        # looks up "<group>.<prefix><n>") — that bug renders with NO references
        # and no error, so pin the exact key shape.
        _, wf = self._generate()
        inputs = self._ref_inputs(wf)
        self.assertIn("ref_images.ref_image_0", inputs)
        self.assertIn("ref_images.ref_image_1", inputs)
        self.assertIn("ref_audios.ref_audio_0", inputs)
        self.assertNotIn("ref_image_0", inputs)
        self.assertNotIn("first_frame", inputs)
        # Each reference points at a real loader node of the right class.
        for key, cls in (("ref_images.ref_image_0", "LoadImage"),
                         ("ref_audios.ref_audio_0", "LoadAudio")):
            node_id = inputs[key][0]
            self.assertEqual(wf[node_id]["class_type"], cls)

    def test_reference_order_is_preserved(self):
        # <Picture 1>/<Picture 2> in the prompt refer to slots 0/1 in order.
        _, wf = self._generate(n_images=3, n_audios=0)
        inputs = self._ref_inputs(wf)
        names = [wf[inputs[f"ref_images.ref_image_{i}"][0]]["inputs"]["image"]
                 for i in range(3)]
        self.assertEqual(names, ["char0.png", "char1.png", "char2.png"])

    def test_reference_caps(self):
        _, wf = self._generate(n_images=12, n_audios=5)
        inputs = self._ref_inputs(wf)
        imgs = [k for k in inputs if k.startswith("ref_images.")]
        auds = [k for k in inputs if k.startswith("ref_audios.")]
        self.assertEqual(len(imgs), comfyui.H3_MAX_REF_IMAGES)
        self.assertEqual(len(auds), comfyui.H3_MAX_REF_AUDIOS)

    def test_loader_ids_never_collide_with_graph_nodes(self):
        _, wf = self._generate(n_images=9, n_audios=3)
        self.assertEqual(len(wf), len(set(wf)))
        inputs = self._ref_inputs(wf)
        targets = [inputs[k][0] for k in inputs if k.startswith(("ref_images.", "ref_audios."))]
        self.assertEqual(len(targets), len(set(targets)))

    def test_turbo_reference_graph(self):
        eng, wf = self._generate(engine_key="minimax-h3-ref-turbo")
        lora = next(n for n in wf.values() if n["class_type"] == "MiniMaxH3TurboLoRA")
        self.assertEqual(lora["inputs"]["lora_name"], eng["lora"])
        self.assertTrue(any(n["class_type"] == "MiniMaxH3TurboSampler" for n in wf.values()))
        unet = next(n for n in wf.values() if n["class_type"] == "UNETLoader")
        self.assertEqual(unet["inputs"]["unet_name"], eng["unet"])

    def test_refuses_without_an_image_reference(self):
        # H3 rejects audio-only conditioning; fail before queueing.
        eng = engines.resolve_reference({}, "minimax-h3-ref")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                comfyui.generate_video_h3_ref(eng, "x", [], Path(tmp) / "o.mp4")


class QueueHealthTests(unittest.TestCase):
    """An unreachable worker must never be read as "the job is gone"."""

    def _check(self, side_effect):
        with mock.patch.object(comfyui.urllib.request, "urlopen", side_effect=side_effect):
            return comfyui._check_queue("pid", "http://w:8188")

    def _resp(self, payload):
        class _Ctx:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                import json as _json
                return _json.dumps(payload).encode()
        return lambda *a, **k: _Ctx()

    def test_running_and_pending(self):
        self.assertEqual(self._check(self._resp({"queue_running": [[0, "pid"]],
                                                 "queue_pending": []})), "running")
        self.assertEqual(self._check(self._resp({"queue_running": [],
                                                 "queue_pending": [[0, "pid"]]})), "pending")

    def test_answered_but_not_listed_is_absent(self):
        self.assertEqual(self._check(self._resp({"queue_running": [[0, "other"]],
                                                 "queue_pending": []})), "absent")

    def test_unreachable_worker_is_unknown_not_absent(self):
        # H3 stops serving HTTP while it loads a 20-34 GB checkpoint. Reading
        # that as "absent" declared healthy jobs dropped and queued a duplicate
        # render behind the one still loading.
        self.assertEqual(self._check(TimeoutError("timed out")), "unknown")
        self.assertEqual(self._check(OSError("connection refused")), "unknown")


class FallbackPollTests(unittest.TestCase):
    """When the websocket drops, the history poll must not wait out a 6-hour
    deadline for a job the worker has forgotten."""

    def _run(self, history_seq, queue_status, seconds=3600):
        calls = {"n": 0}

        class _Ctx:
            def __enter__(self_i): return self_i
            def __exit__(self_i, *a): return False
            def read(self_i):
                import json as _json
                i = min(calls["n"], len(history_seq) - 1)
                calls["n"] += 1
                return _json.dumps(history_seq[i]).encode()

        # The absence grace is wall-clock; shrink it so the test is instant.
        with mock.patch.object(comfyui.urllib.request, "urlopen", side_effect=lambda *a, **k: _Ctx()), \
             mock.patch.object(comfyui, "_check_queue", return_value=queue_status), \
             mock.patch.object(comfyui, "_QUEUE_ABSENT_GRACE", 0), \
             mock.patch.object(comfyui.time, "sleep"):
            comfyui._poll_completion("pid", deadline=comfyui.time.time() + seconds,
                                     comfy_url="http://w:8188")

    def test_completed_job_returns(self):
        self._run([{"pid": {"status": {"completed": True}}}], "absent")

    def test_forgotten_job_gives_up_instead_of_waiting_out_the_deadline(self):
        # Worker answers, knows nothing about the job, and its queue is empty:
        # it restarted. Raise so the scene re-dispatches to another worker.
        with self.assertRaises(comfyui.DroppedJobError):
            self._run([{}], "absent")

    def test_still_queued_job_keeps_waiting(self):
        # In the queue but not yet in history: keep waiting (bounded by the
        # deadline) rather than treating it as forgotten.
        # Sleep is mocked, so give it a deadline it reaches immediately.
        with self.assertRaises(RuntimeError) as ctx:
            self._run([{}], "running", seconds=0.05)
        self.assertNotIsInstance(ctx.exception, comfyui.DroppedJobError)


class TurboNodeContractTests(unittest.TestCase):
    def test_ref2va_turbo_sends_low_vram(self):
        # The pinned H3-Turbo nodes (55fee86) made low_vram a REQUIRED input on
        # MiniMaxH3TurboLoRA — the i2v workflow was fixed, this one was missed,
        # so every performance render on a rebuilt worker was rejected.
        import json
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for wf in ("h3_turbo_i2v.json", "h3_ref2v_turbo.json"):
            graph = json.loads((root / "workflows" / wf).read_text())
            lora = next(n for n in graph.values()
                        if n["class_type"] == "MiniMaxH3TurboLoRA")
            self.assertIn("low_vram", lora["inputs"], wf)
            self.assertIs(lora["inputs"]["low_vram"], False, wf)


class EngineVersionGateTests(unittest.TestCase):
    """An engine that renders black frames on an old worker must refuse first."""

    def _gate(self, worker_version):
        eng = engines.resolve_reference({}, "minimax-h3-ref-w4a8")
        with mock.patch.object(comfyui, "comfyui_version", return_value=worker_version):
            comfyui.check_engine_supported(eng, "http://w:8188")

    def test_old_worker_is_refused_with_an_actionable_message(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._gate((0, 30, 0))
        msg = str(ctx.exception)
        self.assertIn("0.31", msg)
        self.assertIn("black frames", msg)

    def test_new_enough_worker_passes(self):
        self._gate((0, 31, 1))
        self._gate((0, 32, 0))

    def test_unknown_version_does_not_block(self):
        # A worker that won't report its version is not proof of an old one.
        self._gate(())

    def test_engines_without_a_floor_are_never_gated(self):
        with mock.patch.object(comfyui, "comfyui_version", return_value=(0, 1, 0)):
            comfyui.check_engine_supported(
                engines.resolve_reference({}, "minimax-h3-ref-turbo"), "http://w:8188")
