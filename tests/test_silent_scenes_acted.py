"""Silent scenes performed on H3 Ref2VA: the h3_silent_scenes toggle, end to end.

The contract: with the toggle on, a silent scene whose writer named a cast is
PERFORMED from those portraits — one acted take, no first frame, no TTS, no mux
— and says nothing. Off (or castless), it renders exactly as it always did.
"""
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import performance as perf  # noqa: E402
from pipeline.llm import Scene  # noqa: E402


def _silent(cast=None, duration=6.0):
    md = {"mode": "silent"}
    if cast is not None:
        md["cast"] = cast
    return Scene(id=3, title="beat", image_prompt="i", video_prompt="a wharf at dusk",
                 narration="", mode="silent", duration=duration, metadata_extra=md)


ON = {"h3_silent_scenes": True}
OFF = {"h3_silent_scenes": False}


class RoutingTests(unittest.TestCase):
    def test_dialogue_scene_is_acted_either_way(self):
        scene = Scene(id=2, title="talk", image_prompt="", video_prompt="v", narration="",
                      mode="dialogue", lines=[{"speaker": "Ana", "text": "You came."}])
        self.assertTrue(perf.renders_acted(scene, OFF))
        self.assertTrue(perf.renders_acted(scene, ON))

    def test_silent_scene_is_acted_only_when_asked(self):
        scene = _silent(["Ana"])
        self.assertTrue(perf.renders_acted(scene, ON))
        self.assertFalse(perf.renders_acted(scene, OFF))
        self.assertFalse(perf.renders_acted(scene, {}))

    def test_the_toggle_alone_decides_cast_or_no_cast(self):
        # Every silent scene in the style is shot the same way — a castless one
        # opens on its own first frame instead of portraits.
        self.assertTrue(perf.renders_acted(_silent(), ON))
        self.assertTrue(perf.renders_acted(_silent([]), ON))
        self.assertFalse(perf.renders_acted(_silent(), OFF))

    def test_narrated_scene_is_never_acted(self):
        scene = Scene(id=1, title="open", image_prompt="i", video_prompt="v",
                      narration="Once upon a time.")
        self.assertFalse(perf.renders_acted(scene, ON))

    def test_planner_and_renderer_agree(self):
        # The orchestrator keeps its own copy (it stays dependency-free); a
        # disagreement plans a task nobody ever completes.
        from pipeline.orchestrator import _renders_acted
        for scene in (_silent(["Ana"]), _silent(), _silent([]),
                      Scene(id=1, title="t", image_prompt="i", video_prompt="v",
                            narration="n"),
                      Scene(id=2, title="t", image_prompt="", video_prompt="v",
                            narration="", mode="dialogue",
                            lines=[{"speaker": "Ana", "text": "hi"}])):
            for cfg in (ON, OFF):
                self.assertEqual(_renders_acted(scene.metadata, cfg),
                                 perf.renders_acted(scene, cfg),
                                 f"{scene.metadata} / {cfg}")


class PlanTests(unittest.TestCase):
    """What the task planner actually creates for a mixed film."""

    def _kinds(self, config):
        from pipeline.orchestrator import DurableStore
        scenes = [
            Scene(id=1, title="open", image_prompt="i", video_prompt="v",
                  narration="Once upon a time."),
            _silent(["Ana"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp) / "orchestrator.sqlite3")
            try:
                store.ensure_generation_plan("job", tmp, "T", scenes,
                                             {"vid_width": 512, "vid_height": 256, **config})
                return {r["id"]: r["kind"] for r in store.task_rows("job")}
            finally:
                store.close()

    def test_acted_silent_scene_plans_one_task(self):
        kinds = self._kinds(ON)
        self.assertEqual(kinds.get("job:scene:3:performance"), "scene.performance.generate")
        for part in ("image", "narration", "video", "mux"):
            self.assertNotIn(f"job:scene:3:{part}", kinds)
        # the narrated scene in the same film is untouched
        self.assertIn("job:scene:1:mux", kinds)

    def test_toggle_off_keeps_the_classic_quartet(self):
        kinds = self._kinds(OFF)
        self.assertNotIn("job:scene:3:performance", kinds)
        for part in ("image", "narration", "video", "mux"):
            self.assertIn(f"job:scene:3:{part}", kinds)


class OpeningFrameTests(unittest.TestCase):
    """A silent take opens on the scene's own image — Ref2VA takes no literal
    first frame, but the picture rides as the opening-composition reference,
    and for a castless beat it is the ONLY reference the take has."""

    def _run(self, scene, files=()):
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            for name in files:
                (wd / name).write_bytes(b"x")
            made = []

            def _fake_gen(engine, prompt, out, **kw):
                Path(out).write_bytes(b"png")
                made.append((prompt, kw.get("width"), kw.get("height")))
                return out

            with unittest.mock.patch.object(rg, "generate_with_engine", _fake_gen):
                got = rg.ensure_opening_frame(scene, wd, {}, comfy_url="http://x",
                                              vid_width=512, vid_height=256)
            return got, made

    def test_a_silent_scene_without_one_gets_one(self):
        got, made = self._run(_silent(["Ana"]))
        self.assertTrue(str(got).endswith("scene_03_first_frame.png"))
        self.assertEqual(made, [("i", 512, 256)])

    def test_an_existing_preview_is_reused(self):
        got, made = self._run(_silent(), files=["scene_03_preview.png"])
        self.assertTrue(str(got).endswith("scene_03_preview.png"))
        self.assertEqual(made, [])   # nothing regenerated

    def test_a_dialogue_scene_is_untouched(self):
        scene = Scene(id=3, title="t", image_prompt="i", video_prompt="v", narration="",
                      mode="dialogue", lines=[{"speaker": "Ana", "text": "hi"}])
        got, made = self._run(scene)
        self.assertIsNone(got)
        self.assertEqual(made, [])

    def test_no_image_prompt_is_not_an_error(self):
        scene = Scene(id=3, title="t", image_prompt="", video_prompt="v", narration="",
                      mode="silent", duration=6.0, metadata_extra={"mode": "silent"})
        got, made = self._run(scene)
        self.assertIsNone(got)
        self.assertEqual(made, [])


class MetaTests(unittest.TestCase):
    def test_silent_scene_keeps_its_cast_and_authored_length(self):
        meta = perf.acted_meta(_silent(["Ana", "Bo"], duration=7.0))
        self.assertEqual(meta["cast"], ["Ana", "Bo"])
        self.assertEqual(meta["seconds"], 7.0)
        self.assertEqual(meta["lines"], [])
        self.assertEqual(meta["setting"], "a wharf at dusk")

    def test_stored_row_takes_its_length_from_metadata(self):
        # A scene row out of the store carries duration in the sidecar, not on
        # the row itself — reading only the row gave every re-shoot 10 s.
        row = {"id": 3, "title": "beat", "video_prompt": "a wharf at dusk",
               "metadata": {"mode": "silent", "cast": ["Ana"], "duration": 6.0}}
        self.assertEqual(perf.acted_meta(row)["seconds"], 6.0)


class ChainedTests(unittest.TestCase):
    """h3_chain_scenes widens a silent take the same way it widens an acted one:
    two clips joined by Motion Context instead of one, so the beat can run past
    H3's single-clip ceiling."""

    def test_an_authored_length_survives_the_single_clip_cap(self):
        long_beat = _silent(["Ana"], duration=20.0)
        self.assertEqual(perf.acted_meta(long_beat)["seconds"], perf.MAX_SCENE_SECONDS)
        chained = perf.acted_meta(long_beat, chained=True)["seconds"]
        self.assertEqual(chained, 20.0)
        self.assertGreater(chained, perf.H3_CEILING_SECONDS)

    def test_render_seconds_follows(self):
        meta = {"seconds": 20.0}
        self.assertEqual(perf.render_seconds(meta), perf.MAX_SCENE_SECONDS)
        self.assertEqual(perf.render_seconds(meta, chained=True), 20.0)

    def test_the_split_divides_the_window_and_its_beats(self):
        meta = perf.acted_meta(
            Scene(id=3, title="t", image_prompt="i", video_prompt="v", narration="",
                  mode="silent", duration=20.0,
                  metadata_extra={"mode": "silent", "cast": ["Ana"], "beats": [
                      {"t0": 0, "t1": 4, "action": "she steps onto the wharf"},
                      {"t0": 14, "t1": 19, "action": "the light goes out"}]}),
            chained=True)
        halves = perf.split_silent_for_chain(meta)
        self.assertEqual(len(halves), 2)
        self.assertAlmostEqual(sum(h["seconds"] for h in halves), 20.0)
        # each clip keeps the beats that fall in ITS window, re-based to zero —
        # sending them all to clip one leaves the second with nothing to do
        self.assertEqual([b["action"] for b in halves[0]["beats"]],
                         ["she steps onto the wharf"])
        self.assertEqual([b["action"] for b in halves[1]["beats"]],
                         ["the light goes out"])
        self.assertAlmostEqual(halves[1]["beats"][0]["t0"], 4.0)
        # one take: the space and the people are shared
        self.assertEqual(halves[1]["cast"], ["Ana"])

    def test_a_scene_that_fits_one_clip_is_not_split(self):
        meta = perf.acted_meta(_silent(["Ana"], duration=8.0), chained=True)
        self.assertLess(perf.content_seconds(meta, chained=True),
                        perf.acted_limits(False)[1])   # renderer keeps it single-clip

    def test_a_chained_film_is_planned_in_longer_scenes(self):
        # One scene per take: counting a chained film at the unchained length
        # would deliver twice the runtime asked for.
        import webapp.backend.main as m
        body = m.GenerateScriptBody(video_title="t", topic="t", minutes=2.0, format="silent")
        self.assertEqual(m._acted_scene_plan(body, {})[0], 12)
        self.assertEqual(m._acted_scene_plan(body, {"h3_chain_scenes": True})[0], 6)

    def test_the_writer_is_given_the_wider_window(self):
        import webapp.backend.main as m
        plain = m._build_dialogue_note("silent", ["Ana"], acted_silent=True)
        chained = m._build_dialogue_note("silent", ["Ana"], chained=True, acted_silent=True)
        self.assertIn('"seconds" of about 10 (never below 5 or above 12)', plain)
        self.assertIn('"seconds" of about 19 (never below 5 or above 23)', chained)
        self.assertIn("beats spread across the whole take", chained)


class ChainedRenderTests(unittest.TestCase):
    """The renderer's own decision, end to end: a castless silent beat shot
    from its first frame, as one clip or as two joined ones."""

    def _render(self, duration, cfg_extra):
        import resume_generation as rg
        calls = {"single": [], "chained": []}

        def fake_single(engine, prompt, ref_images, out, **kw):
            calls["single"].append({"refs": [Path(p).name for p in ref_images],
                                    "secs": kw.get("duration_seconds"), "prompt": prompt})
            Path(out).write_bytes(b"mp4")
            return Path(out)

        def fake_chained(engine, prompts, ref_images, out, **kw):
            calls["chained"].append({"prompts": prompts, "durations": kw.get("durations")})
            Path(out).write_bytes(b"mp4")
            return Path(out)

        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "scene_03_preview.png").write_bytes(b"png")   # the opening image
            with unittest.mock.patch("pipeline.comfyui.generate_video_h3_ref",
                                     side_effect=fake_single), \
                 unittest.mock.patch("pipeline.comfyui.generate_video_h3_ref_chained",
                                     side_effect=fake_chained), \
                 unittest.mock.patch.object(rg, "ensure_video_resolution"):
                rg.render_performance_scene(
                    _silent(duration=duration), wd,
                    {"performance_verify": False, **cfg_extra},
                    comfy_url="http://w:8188", vid_width=704, vid_height=1280)
        return calls

    def test_unchained_shoots_one_clip_off_the_first_frame(self):
        calls = self._render(20.0, {})
        self.assertEqual(len(calls["single"]), 1)
        self.assertEqual(calls["chained"], [])
        # castless: the scene's own image is the only reference it needs
        self.assertEqual(calls["single"][0]["refs"], ["scene_03_preview.png"])
        # …and 20 s is held back to the single-clip cap
        self.assertEqual(calls["single"][0]["secs"], perf.MAX_SCENE_SECONDS)

    def test_chained_shoots_two_joined_clips_for_the_full_length(self):
        calls = self._render(20.0, {"h3_chain_scenes": True})
        self.assertEqual(calls["single"], [])
        self.assertEqual(len(calls["chained"]), 1)
        self.assertEqual(len(calls["chained"][0]["prompts"]), 2)
        self.assertAlmostEqual(sum(calls["chained"][0]["durations"]), 20.0, places=3)

    def test_a_short_beat_stays_one_clip_even_when_chaining_is_on(self):
        calls = self._render(8.0, {"h3_chain_scenes": True})
        self.assertEqual(calls["chained"], [])
        self.assertEqual(calls["single"][0]["secs"], 8.0)


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = perf.build_h3_prompt(
            perf.acted_meta(_silent(["Ana"])),
            style_note="grainy 16mm",
            picture_names=[{"slot": 1, "name": "Ana", "kind": "character"}])

    def test_no_dialogue_block(self):
        self.assertNotIn("[DIALOGUE]", self.prompt)

    def test_says_nobody_speaks(self):
        # H3 babbles into a lines-less shot unless told not to.
        self.assertIn("Nobody speaks", self.prompt)
        self.assertIn("No speech and no voices at all", self.prompt)

    def test_still_locks_identity_and_the_scene(self):
        self.assertIn("Ana", self.prompt)
        self.assertIn("a wharf at dusk", self.prompt)

    def test_a_talking_scene_is_unchanged(self):
        meta = perf.acted_meta(Scene(
            id=2, title="t", image_prompt="", video_prompt="a kitchen", narration="",
            mode="dialogue", lines=[{"speaker": "Ana", "text": "You came."}]))
        prompt = perf.build_h3_prompt(
            meta, picture_names=[{"slot": 1, "name": "Ana", "kind": "character"}],
            audio_names=["Ana"])
        self.assertIn("[DIALOGUE]", prompt)
        self.assertIn("Clear dialogue", prompt)
        self.assertNotIn("Nobody speaks", prompt)


class ScriptTests(unittest.TestCase):
    def test_divide_keeps_the_performance_fields_of_a_silent_scene(self):
        from pipeline.story import _scene_from_item
        scene = _scene_from_item(4, {"title": "beat", "mode": "silent", "seconds": 8,
                                     "image_prompt": "i", "video_prompt": "v",
                                     "cast": ["Ana"], "setting": "a wharf",
                                     "camera": "slow push", "soundscape": "gulls"}, "T", None)
        self.assertEqual(scene.mode, "silent")
        self.assertEqual(scene.duration, 8.0)
        self.assertEqual(scene.metadata["cast"], ["Ana"])
        self.assertEqual(scene.metadata["setting"], "a wharf")
        self.assertTrue(perf.renders_acted(scene, ON))

    def test_a_bare_string_cast_becomes_one_name(self):
        from pipeline.story import _scene_from_item
        scene = _scene_from_item(4, {"mode": "silent", "cast": "Ana"}, "T", None)
        self.assertEqual(scene.metadata["cast"], ["Ana"])

    def test_a_plain_silent_scene_carries_no_extra_metadata(self):
        # Nothing the writer didn't give it — the scene is still acted when the
        # style asks (it opens on its first frame), but its sidecar is bare.
        from pipeline.story import _scene_from_item
        scene = _scene_from_item(4, {"mode": "silent", "image_prompt": "i"}, "T", None)
        self.assertEqual(scene.metadata, {"mode": "silent", "duration": 5.0})
        self.assertFalse(perf.renders_acted(scene, OFF))


class SettingsTests(unittest.TestCase):
    def test_toggle_is_a_per_style_field_defaulting_off(self):
        import app as gapp
        self.assertIn("h3_silent_scenes", gapp.STYLE_FIELD_TO_FLAT)
        self.assertFalse(gapp.DEFAULT_CFG["default_h3_silent_scenes"])
        cfg = {"styles": [{"name": "S", "h3_silent_scenes": "yes"}], "default_style": "S"}
        self.assertTrue(gapp.style_settings(gapp._ensure_styles(cfg), "S")["h3_silent_scenes"])

    def test_the_writer_is_asked_for_a_silent_cast_only_when_it_is_on(self):
        import webapp.backend.main as m
        on = m._build_dialogue_note("mixed", ["Ana"], acted_silent=True)
        off = m._build_dialogue_note("mixed", ["Ana"], acted_silent=False)
        self.assertIn("silent scenes are PERFORMED", on)
        self.assertIn('give it a "cast" too', on)
        self.assertNotIn("PERFORMED", off)
        # Narrated films never see any of it.
        self.assertIsNone(m._build_dialogue_note("narration", ["Ana"], acted_silent=True))


class AssemblyTests(unittest.TestCase):
    """An acted silent film must reach its final cut.

    The regression: acted silent scenes are planned as ONE performance task —
    they have no mux task — but the mux loop only skipped DIALOGUE scenes, so
    it recorded a scene_final artifact against a task id that was never
    created. SQLite's FK check killed the render after every clip was already
    on disk ("FOREIGN KEY constraint failed"), leaving the film unassembled.
    """

    def _run(self, script_rows, job_cfg):
        import resume_generation as rg
        from pipeline.orchestrator import DurableStore, job_id_from_work_dir

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        wd = root / "film-20260813-194405"
        wd.mkdir()
        out_dir = root / "out"
        out_dir.mkdir()
        (wd / "script.json").write_text(json.dumps(script_rows))
        (wd / "job_config.json").write_text(json.dumps(job_cfg))
        for row in script_rows:
            (wd / f"scene_{row['id']:02d}_final.mp4").write_bytes(b"m" * 20_000)

        store = DurableStore(root / "orchestrator.sqlite3")
        self.addCleanup(store.close)
        m = unittest.mock
        with m.patch.object(rg, "load_config", return_value={}), \
             m.patch.object(rg.DurableStore, "default", classmethod(lambda cls: store)), \
             m.patch.object(rg, "OUTPUT_DIR", out_dir), \
             m.patch.object(rg, "alive_workers", return_value=["http://w:8188"]), \
             m.patch.object(rg, "generate_cover_image"), \
             m.patch.object(rg, "ensure_video_resolution"), \
             m.patch.object(rg, "_get_duration", return_value=6.0), \
             m.patch.object(rg, "concatenate_scenes",
                            side_effect=lambda clips, out: Path(out).write_bytes(b"c" * 20_000)):
            rg.main(wd)
        return store, job_id_from_work_dir(wd), out_dir

    def _silent_row(self, sid):
        return {"id": sid, "title": f"beat {sid}", "image_prompt": "a wharf at dusk",
                "video_prompt": "a wharf at dusk", "narration": "",
                "metadata": {"mode": "silent", "cast": ["Ana"], "duration": 6.0}}

    def test_an_acted_silent_film_assembles(self):
        store, job, out_dir = self._run(
            [self._silent_row(1), self._silent_row(2)],
            {"h3_silent_scenes": True, "music_enabled": False,
             "tts_workers": ["http://t:8000"],   # unused: nothing is narrated
             "resolution": "Landscape 720p (1280×720)", "title": "Scene 1"})
        finals = list(out_dir.glob("*.mp4"))
        self.assertEqual(len(finals), 1, "the film never reached its final cut")
        self.assertEqual(store.get_job(job)["status"], "done")
        # …and the scene_final artifacts hung off the performance tasks, not a
        # mux task that was never planned.
        kinds = {r["id"]: r["status"] for r in store.task_rows(job)}
        self.assertEqual(kinds.get(f"{job}:final"), "succeeded")
        self.assertNotIn(f"{job}:scene:1:mux", kinds)


if __name__ == "__main__":
    unittest.main()
