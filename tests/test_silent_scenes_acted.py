"""Silent scenes performed on H3 Ref2VA: the h3_silent_scenes toggle, end to end.

The contract: with the toggle on, a silent scene whose writer named a cast is
PERFORMED from those portraits — one acted take, no first frame, no TTS, no mux
— and says nothing. Off (or castless), it renders exactly as it always did.
"""
import json
import os
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

    def test_a_location_reference_stops_the_frame_paint(self):
        # A location reference IS the place, chosen by hand — and a frame
        # outranks it, so painting one here would silently supersede the
        # reference (resurrecting a frame the user removed). The take opens on
        # the location instead.
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "visuals").mkdir()
            (wd / "visuals" / "loc.png").write_bytes(b"png")
            (wd / "visuals.json").write_text(json.dumps([
                {"id": "vis_1", "name": "Bar", "kind": "location",
                 "description": "a bar", "scenes": [3], "ref_image": "loc.png",
                 "enabled": True}]))
            made = []

            def _fake_gen(engine, prompt, out, **kw):
                made.append(prompt)

            with unittest.mock.patch.object(rg, "generate_with_engine", _fake_gen):
                got = rg.ensure_opening_frame(_silent(["Ana"]), wd, {},
                                              comfy_url="http://x",
                                              vid_width=512, vid_height=256)
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


class ReferenceWallTests(unittest.TestCase):
    """The editor's Characters & visuals wall follows the RENDER, not the mode.

    A performed silent take is fed the same locations, wardrobe and stills as a
    dialogue take, so a film made of them needs the wall just as much — reading
    the mode alone hid it and left the film with characters only.
    """

    def _usage(self, flag):
        import webapp.backend.main as m
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wd = Path(tmp.name) / "film-20260813-194405"
        wd.mkdir()
        (wd / "script.json").write_text(json.dumps([
            {"id": 1, "title": "beat", "image_prompt": "i", "video_prompt": "v",
             "narration": "", "metadata": {"mode": "silent", "cast": ["Ana"]}}]))
        store = unittest.mock.Mock()
        store.scene_rows.return_value = []
        mock = unittest.mock
        with mock.patch.object(m.DurableStore, "default", classmethod(lambda cls: store)), \
             mock.patch.object(m, "_film_job_config",
                               return_value={"style_name": "S", "h3_silent_scenes": flag}):
            return m._film_reference_usage(wd)

    def test_a_performed_silent_film_gets_the_wall(self):
        names, has_acted = self._usage(True)
        self.assertTrue(has_acted)
        self.assertEqual(names, {"ana"})

    def test_an_animated_silent_film_does_not(self):
        self.assertFalse(self._usage(False)[1])


class FilmEditorFlagTests(unittest.TestCase):
    """The film editor has to be TOLD the film performs its silent scenes —
    that flag is what puts the acted setup on a silent scene's card."""

    def test_film_scenes_reports_the_flag(self):
        import webapp.backend.main as m
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "videos"
        out.mkdir()
        wd = out / "film-20260813-194405"
        wd.mkdir()
        (wd / "script.json").write_text(json.dumps([
            {"id": 1, "title": "beat", "image_prompt": "i", "video_prompt": "v",
             "narration": "", "metadata": {"mode": "silent", "cast": ["Ana"]}}]))
        (wd / "job_config.json").write_text(json.dumps(
            {"style_name": "S", "h3_silent_scenes": True}))
        store = unittest.mock.Mock()
        store.scene_rows.return_value = []
        store.get_job.return_value = None
        mock = unittest.mock
        with mock.patch.object(m.gapp, "OUTPUT_DIR", out), \
             mock.patch.object(m.DurableStore, "default", classmethod(lambda cls: store)), \
             mock.patch.object(m.gapp, "load_config", return_value={}), \
             mock.patch.object(m.gapp, "get_voice_choices", return_value=[]):
            payload = m.film_scenes(work_dir=str(wd))
        self.assertTrue(payload["acted_silent"])
        self.assertEqual(payload["scenes"][0]["cast"], ["Ana"])


class ActedViewTests(unittest.TestCase):
    """A performed silent take is shot on H3, so it belongs on the H3 screens:
    the acted view lists it with its resolved slots and its prompt, and the
    scene editor shows the same prompt beside the fields it is built from."""

    def setUp(self):
        import app as gapp
        import webapp.backend.main as backend
        from pipeline.orchestrator import DurableStore, job_id_from_work_dir
        self.backend = backend
        mock = unittest.mock
        tmp = tempfile.TemporaryDirectory(prefix="spielbot-silent-view-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.output_dir = root / "videos"
        self.output_dir.mkdir()
        cfg_file = root / "config" / "config.yaml"
        cfg_file.parent.mkdir(parents=True)
        for target, attr, value in [(gapp, "CONFIG_FILE", cfg_file),
                                    (gapp, "OUTPUT_DIR", self.output_dir)]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        db = mock.patch.dict(os.environ,
                             {"SPIELBOT_ORCHESTRATOR_DB": str(root / "orchestrator.sqlite3")})
        db.start()
        self.addCleanup(db.stop)
        backend._ACTED_CTX.clear()          # the per-work-dir context is cached
        self.addCleanup(backend._ACTED_CTX.clear)

        self.wd = self.output_dir / "film"
        self.wd.mkdir()
        self.job_id = job_id_from_work_dir(self.wd)
        row = {"id": 1, "title": "A hand lowers the component",
               "image_prompt": "a shadowed bench", "video_prompt": "the hand descends",
               "narration": "",
               "metadata": {"mode": "silent", "cast": ["Ana"], "duration": 8.0,
                            "setting": "A shadow-drowned laboratory bench.",
                            "camera": "Extreme close-up",
                            "beats": [{"t0": 0, "t1": 8, "action": "the hand descends"}]}}
        # The acted view reads the on-disk script; the editors read the store.
        (self.wd / "script.json").write_text(json.dumps([row]))
        store = DurableStore.default()
        try:
            store.create_or_update_job(self.job_id, self.wd, "Film",
                                       config={"video_title": "Film"}, metadata={})
            store.upsert_scene(self.job_id, 1, title=row["title"],
                               image_prompt=row["image_prompt"],
                               video_prompt=row["video_prompt"], narration="",
                               metadata=row["metadata"])
        finally:
            store.close()

    def _flag(self, on):
        (self.wd / "job_config.json").write_text(
            json.dumps({"style_name": "S", "h3_silent_scenes": on}))
        self.backend._ACTED_CTX.clear()

    def test_the_acted_view_lists_a_performed_silent_take(self):
        self._flag(True)
        scenes = self.backend.load_performance_script(work_dir=str(self.wd))["scenes"]
        self.assertEqual(len(scenes), 1)
        scene = scenes[0]
        self.assertTrue(scene["silent"])
        self.assertEqual(scene["lines"], [])
        # …with the prompt the model is actually sent, built from the fields.
        self.assertIn("A shadow-drowned laboratory bench.", scene["prompt"])
        self.assertIn("[SHOT LIST]", scene["prompt"])

    def test_an_animated_silent_scene_stays_off_the_acted_view(self):
        self._flag(False)
        self.assertEqual(
            self.backend.load_performance_script(work_dir=str(self.wd))["scenes"], [])

    def test_the_scene_editor_gets_the_same_prompt(self):
        self._flag(True)
        scene = self.backend.job_scenes(self.job_id)["scenes"][0]
        self.assertIn("A shadow-drowned laboratory bench.", scene["acted_prompt"])
        self._flag(False)
        self.assertNotIn("acted_prompt", self.backend.job_scenes(self.job_id)["scenes"][0])

    def test_a_pinned_prompt_wins_without_wiping_the_opening_frame(self):
        self._flag(True)
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            title="A hand lowers the component", image_prompt="a shadowed bench",
            video_prompt="the hand descends", narration="", mode="silent",
            prompt="Just this, verbatim."))
        scene = self.backend.job_scenes(self.job_id)["scenes"][0]
        self.assertEqual(scene["acted_prompt"], "Just this, verbatim.")
        self.assertTrue(scene["prompt_edited"])
        self.assertEqual(scene["image_prompt"], "a shadowed bench")

    def test_a_rewrite_leaves_the_beat_silent(self):
        self._flag(True)
        answer = json.dumps({
            "title": "The hand settles", "cast": ["Ana"],
            "setting": "The same bench, colder now.",
            "lines": [{"speaker": "Ana", "text": "It is done."}],   # ignored: silent
            "beats": [{"t0": 0, "t1": 8, "action": "the hand withdraws"}],
            "camera": "Slow push in", "soundscape": "room tone"})
        with unittest.mock.patch.object(self.backend, "_llm_complete", return_value=answer):
            self.backend.regenerate_acted_scene(self.job_id, 1)
        scene = self.backend.job_scenes(self.job_id)["scenes"][0]
        self.assertEqual(scene["mode"], "silent")
        self.assertEqual(scene["lines"], [])
        self.assertEqual(scene["narration"], "")
        self.assertIn("The same bench, colder now.", scene["setting"])
        # The take still opens on its own frame, and keeps the length it was
        # written for (a silent beat has no words to size it from).
        self.assertEqual(scene["image_prompt"], "a shadowed bench")
        self.assertEqual(scene["duration"], 8.0)

    def _make_singing(self):
        """Turn the stored scene into a music-video beat."""
        from pipeline.orchestrator import DurableStore
        store = DurableStore.default()
        try:
            store.upsert_scene(
                self.job_id, 1, title="Chorus", image_prompt="a rooftop stage",
                video_prompt="", narration="",
                metadata={"mode": "silent", "cast": ["Ana"], "duration": 8.0,
                          "singing": True, "sings": "Neon hearts keep burning",
                          "setting": "A rooftop stage at night."})
        finally:
            store.close()

    def _scene_meta(self):
        from pipeline.orchestrator import DurableStore
        store = DurableStore.default()
        try:
            return dict((store.get_scene(self.job_id, 1) or {}).get("metadata") or {})
        finally:
            store.close()

    def test_a_music_video_rewrite_can_stop_the_singing(self):
        # "Ana does not sing" must actually stop the miming: the singing text in
        # the H3 prompt comes from the scene's flags, not from the rewrite's
        # prose, so the rewrite is given a switch and its answer is persisted.
        self._make_singing()
        calls = []
        answer = json.dumps({
            "title": "Ana listens", "cast": ["Ana"], "performs": False,
            "setting": "The rooftop, lights low.", "lines": [],
            "beats": [{"t0": 0, "t1": 8, "action": "Ana sways, eyes closed"}],
            "camera": "Slow orbit", "soundscape": "night wind"})
        def llm(system, user, cfg, max_tokens=900):
            calls.append(user)
            return answer
        with unittest.mock.patch.object(self.backend, "_llm_complete", side_effect=llm):
            self.backend.regenerate_acted_scene(
                self.job_id, 1,
                self.backend.ActedRegenBody(instruction="Ana does not sing"))
        # The rewrite prompt names the music-video context and offers the switch.
        self.assertIn("MUSIC VIDEO", calls[0])
        self.assertIn('"performs"', calls[0])
        self.assertIn("Ana does not sing", calls[0])
        meta = self._scene_meta()
        self.assertIs(meta.get("performs"), False)
        self.assertTrue(meta.get("singing"))   # still a music-video beat
        # …and the prompt the take renders from stops ordering a performance.
        prompt = perf.build_h3_prompt(meta, picture_names=["Ana"])
        self.assertIn("NOT singing", prompt)
        self.assertNotIn("visibly singing", prompt)

    def test_a_quiet_rewrite_keeps_the_scenes_answer(self):
        # No "performs" in the reply — the scene keeps the answer it had, and a
        # true flips it back to the sparse default (no key stored).
        self._make_singing()
        base = {"title": "Chorus", "cast": ["Ana"], "setting": "The stage.",
                "lines": [], "beats": [{"t0": 0, "t1": 8, "action": "Ana sways"}],
                "camera": "Locked", "soundscape": "wind"}
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            mode="silent", performs=False))
        with unittest.mock.patch.object(self.backend, "_llm_complete",
                                        return_value=json.dumps(base)):
            self.backend.regenerate_acted_scene(self.job_id, 1)
        self.assertIs(self._scene_meta().get("performs"), False)
        with unittest.mock.patch.object(
                self.backend, "_llm_complete",
                return_value=json.dumps({**base, "performs": True})):
            self.backend.regenerate_acted_scene(self.job_id, 1)
        self.assertNotIn("performs", self._scene_meta())


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
                            side_effect=lambda clips, out, **kw: Path(out).write_bytes(b"c" * 20_000)):
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
