"""Silent scenes performed on H3 Ref2VA: the h3_silent_scenes toggle, end to end.

The contract: with the toggle on, a silent scene whose writer named a cast is
PERFORMED from those portraits — one acted take, no first frame, no TTS, no mux
— and says nothing. Off (or castless), it renders exactly as it always did.
"""
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


if __name__ == "__main__":
    unittest.main()
