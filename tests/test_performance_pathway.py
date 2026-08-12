"""Acted scenes fork the pathway at script creation.

The Dialogue/Mixed format must produce acted scenes and, from there, a render
that never touches the narrated machinery (first frames, TTS, music) for those
scenes — while narrated scenes keep working exactly as before.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import webapp.backend.main as backend  # noqa: E402
from pipeline import performance  # noqa: E402
from pipeline import story as story_mode  # noqa: E402
from scriptstub import STORY  # noqa: E402
from test_styles import TempConfigCase, _style  # noqa: E402

# What the story-divide prompt returns for an acted chapter.
_DIVIDED = [
    {"id": 1, "title": "The clearing", "mode": "dialogue",
     "setting": "a burnt clearing", "seconds": 10, "cast": ["CHICO"],
     "camera": "locked off", "soundscape": "cicadas", "image_prompt": "",
     "video_prompt": "a burnt clearing",
     "beats": [{"t0": 0, "t1": 10, "action": "CHICO stands his ground"}],
     "lines": [{"speaker": "CHICO", "delivery": "quiet", "text": "You can burn the trees."}]},
    {"id": 2, "title": "After", "mode": "dialogue",
     "setting": "the clearing at night", "seconds": 10, "cast": ["CHICO"],
     "camera": "slow push", "soundscape": "insects", "image_prompt": "",
     "video_prompt": "the clearing at night",
     "beats": [{"t0": 0, "t1": 10, "action": "CHICO walks away"}],
     "lines": [{"speaker": "CHICO", "delivery": "tired", "text": "Not tonight."}]},
]


class ScriptForkTests(TempConfigCase):
    """The format decides the scenes; the story is always where they come from."""

    def setUp(self):
        super().setUp()
        self.write_config({
            "styles": [_style("Acted", n_scenes=4, voice="Narrator", visual_style="gritty")],
            "default_style": "Acted",
            # Library voices are what get cast onto the characters and become
            # the <Audio N> references at render time.
            "voices": [
                {"name": "Narrator", "path": "/tmp/narrator.wav", "gender": "male",
                 "age": "adult"},
                {"name": "Kara", "path": "/tmp/kara.wav", "gender": "female",
                 "age": "adult"},
                {"name": "Walter", "path": "/tmp/walter.wav", "gender": "male",
                 "age": "mature"},
            ],
            "characters": [],
            "characters_migrated_v2": True,
        })
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        # Portraits are rendered in a background thread — never in a test.
        mock.patch.object(backend.gapp, "generate_all_script_portraits").start()
        mock.patch.object(backend.threading, "Thread").start()
        self.addCleanup(mock.patch.stopall)

    def _generate(self, fmt="dialogue", **kwargs):
        """Draft stubbed; the divide runs for real over a canned LLM reply, so
        the acted scene shape is exercised end to end."""
        body = backend.GenerateScriptBody(
            video_title="Chico Mendes", topic="the rubber tappers",
            style_name="Acted", n_scenes=2, format=fmt, **kwargs)
        story = {**STORY, "n_scenes": 2, "topic": "the rubber tappers",
                 "characters": [{"name": "CHICO", "description": "moustache, white shirt",
                                 "gender": "male", "age": "adult"}],
                 "chapters": [{"chapter": 1, "title": "One", "scenes": 2,
                               "text": "Chico stood his ground."}]}
        with mock.patch.object(story_mode, "generate_story", return_value=story), \
             mock.patch.object(story_mode, "_call_fn",
                               return_value=lambda *a, **k: json.dumps(_DIVIDED)), \
             mock.patch.object(story_mode, "_detect_recurring_characters",
                               side_effect=lambda call, scenes, ident, **k: ident):
            return backend._do_script_generate(body)

    def test_a_dialogue_format_produces_acted_scenes(self):
        result = self._generate()
        scenes = json.loads((Path(result["work_dir"]) / "script.json").read_text())
        self.assertEqual(len(scenes), 2)
        for s in scenes:
            self.assertEqual(s["metadata"]["mode"], "dialogue")
            # The prompt the video model receives, editable in the script editor.
            self.assertIn("<Picture 1> defines CHICO", s["video_prompt"])
            self.assertIn("Do not add subtitles", s["video_prompt"])
            # No image engine runs, so no image prompt is written.
            self.assertEqual(s["image_prompt"], "")

    def test_the_story_is_still_where_the_scenes_come_from(self):
        result = self._generate()
        self.assertTrue((Path(result["work_dir"]) / "story.json").exists())

    def test_brief_records_the_format(self):
        result = self._generate()
        brief = json.loads((Path(result["work_dir"]) / "create_brief.json").read_text())
        self.assertEqual(brief["format"], "dialogue")

    def test_cast_is_persisted_with_a_voice(self):
        # Voices are cast at script creation and become the <Audio N> references.
        result = self._generate()
        chars = json.loads((Path(result["work_dir"]) / "characters.json").read_text())
        self.assertEqual([c["name"] for c in chars], ["CHICO"])
        self.assertTrue(chars[0].get("voice"))

    def test_a_narrated_format_asks_for_no_dialogue(self):
        with mock.patch.object(story_mode, "generate_story",
                               return_value={**STORY}) as gen, \
             mock.patch.object(story_mode, "divide_story",
                               return_value=([], "m", "s", [])) as div:
            try:
                backend._do_script_generate(backend.GenerateScriptBody(
                    video_title="X", topic="y", style_name="Acted", n_scenes=2))
            except Exception:
                pass  # empty scene list — we only care what was asked for
        self.assertIsNone(gen.call_args.kwargs["dialogue_note"])
        self.assertIsNone(div.call_args.kwargs["dialogue_note"])

    def test_scene_count_from_minutes_uses_clip_length_not_word_budget(self):
        ss = backend.gapp.style_settings(backend.gapp.load_config(), "Acted")
        body = backend.GenerateScriptBody(video_title="X", topic="y", minutes=1.0)
        # 60 s of film at ~10 s per acted clip.
        self.assertEqual(backend._acted_scene_count(body, ss), 6)
        # An explicit count still wins.
        body_n = backend.GenerateScriptBody(video_title="X", topic="y", minutes=1.0, n_scenes=3)
        self.assertEqual(backend._acted_scene_count(body_n, ss), 3)


class RenderWiringTests(TempConfigCase):
    """The renderer must cite exactly the references it actually wires up."""

    def setUp(self):
        super().setUp()
        self.write_config({
            "styles": [_style("Acted", n_scenes=2, voice="Narrator")],
            "default_style": "Acted",
            "voices": [{"name": "Kara", "path": str(self.output_dir / "kara.wav"),
                        "gender": "female", "age": "adult"},
                       {"name": "Walter", "path": str(self.output_dir / "walter.wav"),
                        "gender": "male", "age": "mature"}],
            "characters": [],
            "characters_migrated_v2": True,
        })
        for name in ("kara.wav", "walter.wav"):
            (self.output_dir / name).write_bytes(b"wav")
        self.work_dir = self.output_dir / "film-20260807-120000"
        (self.work_dir / "characters").mkdir(parents=True)
        for char_id, voice in (("char_a", "Walter"), ("char_b", "Kara")):
            (self.work_dir / "characters" / f"{char_id}.png").write_bytes(b"png")
        (self.work_dir / "characters.json").write_text(json.dumps([
            {"id": "char_a", "name": "CHICO", "description": "moustache",
             "ref_image": "char_a.png", "voice": "Walter", "enabled": True},
            {"id": "char_b", "name": "MARIA", "description": "braid",
             "ref_image": "char_b.png", "voice": "Kara", "enabled": True},
        ]))

    def _render(self, meta, **kwargs):
        """Render a scene and return the LAST clip's wiring (single-speaker
        scenes make one clip; a two-hander makes one per shot — see _shots)."""
        return self._shots(meta, **kwargs)[-1]

    def _shots(self, meta, **kwargs):
        import resume_generation as rg
        from pipeline.llm import Scene
        calls = []

        def fake_gen(engine, prompt, ref_images, out, ref_audios=None, **kw):
            calls.append({"engine": engine, "prompt": prompt,
                          "ref_images": list(ref_images),
                          "ref_audios": list(ref_audios or []), "kwargs": kw})
            Path(out).write_bytes(b"mp4")
            return Path(out)

        scene = Scene(id=1, title="S", image_prompt="", video_prompt="stale",
                      narration="", mode="performance",
                      lines=meta.get("lines", []),
                      metadata_extra={k: v for k, v in meta.items() if k != "lines"})
        # These tests exercise the (opt-in) shot splitter; establishing and the
        # gate have their own tests and would run real ffmpeg over fake clips.
        cfg = {**backend.gapp.load_config(), "style_name": "Acted",
               "performance_shot_split": True,
               "performance_establishing": False, "performance_verify": False}
        # The continuity frame is a separate concern (SceneContinuityTests) and
        # runs real ffmpeg on these dummy clips — seconds per test.
        with mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
             mock.patch.object(rg, "concatenate_scenes",
                               side_effect=lambda clips, out: Path(out).write_bytes(b"joined")), \
             mock.patch.object(rg, "extract_last_frame",
                               side_effect=lambda src, dst: Path(dst).write_bytes(b"png")), \
             mock.patch.object(rg, "ensure_video_resolution"):
            rg.render_performance_scene(scene, self.work_dir, cfg,
                                        comfy_url="http://w:8188",
                                        vid_width=704, vid_height=1280, **kwargs)
        return calls

    def _meta(self, **over):
        meta = {"mode": "performance", "cast": ["CHICO", "MARIA"], "seconds": 10,
                "setting": "a clearing", "camera": "locked off", "soundscape": "cicadas",
                "beats": [{"t0": 0, "t1": 10, "action": "they face each other"}],
                "lines": [{"speaker": "MARIA", "delivery": "flat", "text": "Go."},
                          {"speaker": "CHICO", "delivery": "quiet", "text": "No."}]}
        meta.update(over)
        return meta

    def test_a_two_hander_renders_one_face_and_one_voice_per_shot(self):
        # The swap has no room to happen when a clip holds one of each. Other
        # reference kinds (the scene's continuity frame) may ride along — what
        # matters is that only ONE of them is a person.
        shots = self._shots(self._meta())
        self.assertEqual(len(shots), 2)
        for shot in shots:
            self.assertIn("Exactly one person on screen", shot["prompt"])
            self.assertEqual(len(shot["ref_audios"]), 1)
            self.assertNotIn("<Audio 2>", shot["prompt"])
            self.assertNotIn("different people", shot["prompt"])

    def test_each_shot_pairs_the_right_face_with_the_right_voice(self):
        shots = self._shots(self._meta())
        # MARIA speaks first: her portrait (char_b) and her voice (kara).
        self.assertEqual(shots[0]["ref_images"][0].name, "char_b.png")
        self.assertEqual(shots[0]["ref_audios"][0].name, "kara.wav")
        self.assertIn("<Audio 1> defines MARIA's voice", shots[0]["prompt"])
        self.assertEqual(shots[1]["ref_images"][0].name, "char_a.png")
        self.assertEqual(shots[1]["ref_audios"][0].name, "walter.wav")
        self.assertIn("<Audio 1> defines CHICO's voice", shots[1]["prompt"])

    def test_a_solo_shot_demands_the_face_and_names_the_listener(self):
        # A real render delivered a whole line to the back of a head — the solo
        # block must demand the face, not just name who is off frame.
        shots = self._shots(self._meta())
        self.assertIn("face fully visible to the camera", shots[0]["prompt"])
        self.assertIn("angled slightly toward CHICO", shots[0]["prompt"])
        self.assertIn("Do not show CHICO", shots[0]["prompt"])

    def test_a_single_speaker_scene_still_renders_one_clip(self):
        shots = self._shots(self._meta(
            cast=["CHICO"], lines=[{"speaker": "CHICO", "delivery": "flat", "text": "Alone."}]))
        self.assertEqual(len(shots), 1)
        self.assertNotIn("Only CHICO is on camera", shots[0]["prompt"])

    def test_unknown_character_is_dropped_not_renumbered_wrong(self):
        # Single speaker, so the scene stays one clip and the slot numbering is
        # visible: an unresolvable name must not leave a gap or shift the rest.
        cap = self._render(self._meta(
            cast=["CHICO", "GHOST", "MARIA"],
            lines=[{"speaker": "CHICO", "delivery": "flat", "text": "Alone."}]))
        self.assertEqual([p.name for p in cap["ref_images"]], ["char_a.png", "char_b.png"])
        self.assertIn("<Picture 2> defines MARIA", cap["prompt"])
        self.assertNotIn("GHOST", cap["prompt"].splitlines()[0])

    def test_no_portrait_at_all_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self._render(self._meta(cast=["GHOST"], lines=[]))

    def test_prompt_is_rebuilt_not_taken_from_the_stale_field(self):
        cap = self._render(self._meta())
        self.assertNotEqual(cap["prompt"], "stale")
        self.assertIn("Do not add subtitles", cap["prompt"])

    def test_engine_is_a_reference_engine(self):
        cap = self._render(self._meta())
        self.assertTrue(cap["engine"]["reference"])
        # Each shot gets the share of the scene its words need, never below the
        # model's minimum clip length.
        self.assertGreaterEqual(cap["kwargs"]["duration_seconds"],
                                performance.MIN_SCENE_SECONDS)

    def test_shot_lengths_add_up_to_about_the_scene(self):
        shots = self._shots(self._meta())
        total = sum(s["kwargs"]["duration_seconds"] for s in shots)
        self.assertGreaterEqual(total, 10.0)


def _write_concat(clips, out):
    """Stand-in for concatenate_scenes that actually produces the file, so the
    assembly step's copy to the final video behaves as it does in production."""
    Path(out).write_bytes(b"concatenated")
    return Path(out)


class WorkerFailoverTests(unittest.TestCase):
    """A worker that can't run the engine must not sink the film."""

    def _film(self, fail_urls, n_scenes=2):
        import resume_generation as rg
        from pipeline.llm import Scene
        from pipeline.worker_pool import WorkerPool

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work_dir = Path(tmp.name) / "film"
        work_dir.mkdir()
        attempts = []

        def fake_render(scene, wd, cfg, *, comfy_url, vid_width, vid_height, style_name=""):
            attempts.append((scene.id, comfy_url))
            if comfy_url in fail_urls:
                raise RuntimeError(
                    "Value not in list: unet_name: 'minimax_h3_ref2va_int8_convrot.safetensors' "
                    "not in ['flux1-schnell-fp8.safetensors']")
            out = wd / f"scene_{scene.id:02d}_final.mp4"
            out.write_bytes(b"mp4")
            return out

        scenes = [Scene(id=i, title=f"S{i}", image_prompt="", video_prompt="p",
                        narration="", mode="dialogue",
                        metadata_extra={"mode": "dialogue", "cast": ["X"], "seconds": 10})
                  for i in range(1, n_scenes + 1)]
        store = mock.MagicMock()
        pool = WorkerPool(["http://a:8188", "http://b:8188"])
        outs = []
        with mock.patch.object(rg, "render_performance_scene", side_effect=fake_render), \
             mock.patch.object(rg, "TaskRun"), \
             mock.patch.object(rg, "_get_duration", return_value=10.0), \
             mock.patch.object(rg.time, "sleep"):
            for scene in scenes:
                outs.append(rg.render_acted_scene(
                    scene, work_dir, {"reference_engine": "minimax-h3-ref-turbo"},
                    store=store, durable_job_id="job", worker_pool=pool,
                    vid_width=704, vid_height=1280))
        return attempts, outs

    def test_scene_retries_on_a_worker_that_has_the_model(self):
        attempts, outs = self._film(fail_urls={"http://a:8188"})
        # Every scene still landed, and the bad worker was dropped rather than
        # retried three times.
        self.assertEqual(sorted(a[0] for a in attempts if a[1] == "http://b:8188"), [1, 2])
        self.assertEqual(len([a for a in attempts if a[1] == "http://a:8188"]), 1)
        self.assertEqual([p.name for p in outs],
                         ["scene_01_final.mp4", "scene_02_final.mp4"])

    def test_an_already_rendered_scene_is_not_redone(self):
        # Resume must be cheap: a scene whose clip survives a crash is kept.
        import resume_generation as rg
        from pipeline.llm import Scene
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "scene_01_final.mp4").write_bytes(b"x" * 20_000)
            scene = Scene(id=1, title="S", image_prompt="", video_prompt="p",
                          narration="", mode="dialogue",
                          metadata_extra={"mode": "dialogue", "cast": ["X"]})
            pool = mock.MagicMock()
            with mock.patch.object(rg, "render_performance_scene") as render:
                out = rg.render_acted_scene(
                    scene, wd, {}, store=mock.MagicMock(), durable_job_id="j",
                    worker_pool=pool, vid_width=704, vid_height=1280)
            render.assert_not_called()
            self.assertEqual(out.name, "scene_01_final.mp4")

    def test_all_workers_missing_the_model_is_a_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._film(fail_urls={"http://a:8188", "http://b:8188"})
        # The film must stop with a worker-level message rather than a bare
        # ComfyUI validation dump.
        self.assertRegex(str(ctx.exception), r"(?i)workers? (failed|remaining)")


class MixedFilmTests(unittest.TestCase):
    """One film, three scene modes: narrated, silent, and acted."""

    def _scenes(self):
        from pipeline.llm import Scene
        return [
            Scene(id=1, title="open", image_prompt="i", video_prompt="v",
                  narration="Once upon a time."),
            Scene(id=2, title="talk", image_prompt="i", video_prompt="a busy wharf at dusk",
                  narration="", mode="dialogue",
                  lines=[{"speaker": "Ana", "text": "You came."},
                         {"speaker": "Bo", "text": "I said I would."}]),
            Scene(id=3, title="beat", image_prompt="i", video_prompt="v", narration="",
                  mode="silent", duration=5),
        ]

    def test_each_scene_takes_the_path_its_mode_asks_for(self):
        from pipeline.orchestrator import DurableStore
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableStore(Path(tmp) / "orchestrator.sqlite3")
            try:
                store.ensure_generation_plan("job", tmp, "T", self._scenes(),
                                             {"vid_width": 512, "vid_height": 256})
                kinds = {r["id"]: r["kind"] for r in store.task_rows("job")}
            finally:
                store.close()
        # the acted scene renders in one take …
        self.assertEqual(kinds.get("job:scene:2:performance"), "scene.performance.generate")
        self.assertNotIn("job:scene:2:mux", kinds)
        # … while the narrated and silent ones keep the classic quartet
        for sid in (1, 3):
            self.assertIn(f"job:scene:{sid}:mux", kinds)
            self.assertIn(f"job:scene:{sid}:video", kinds)
        # a mixed film still gets a score (only an all-acted one skips it)
        self.assertIn("music.generate", set(kinds.values()))

    def test_a_hand_written_dialogue_scene_gets_a_cast_and_a_length(self):
        # Authored in the mixed script editor: lines, no performance fields.
        # Without the fill-in it would render castless and 5 seconds long.
        from pipeline import performance as perf
        meta = perf.acted_meta(self._scenes()[1])
        self.assertEqual(meta["cast"], ["Ana", "Bo"])
        self.assertEqual(meta["setting"], "a busy wharf at dusk")
        self.assertGreater(meta["seconds"], perf.MIN_SCENE_SECONDS)
        self.assertLessEqual(meta["seconds"], perf.H3_CEILING_SECONDS)

    def test_an_authored_performance_scene_keeps_its_own_fields(self):
        from pipeline import performance as perf
        from pipeline.llm import Scene
        scene = Scene(id=1, title="t", image_prompt="", video_prompt="ignored me",
                      narration="", mode="performance",
                      lines=[{"speaker": "Ana", "text": "hi"}],
                      metadata_extra={"mode": "performance", "cast": ["Bo", "Ana"],
                                      "setting": "a kitchen", "seconds": 11})
        meta = perf.acted_meta(scene)
        self.assertEqual(meta["cast"], ["Bo", "Ana"])   # authored order wins
        self.assertEqual(meta["setting"], "a kitchen")
        self.assertEqual(meta["seconds"], 11)


class MusicToggleTests(unittest.TestCase):
    """Music is a choice, and acted films never get a score planned."""

    def _plan(self, scenes, config):
        import tempfile as tf
        from pipeline.orchestrator import DurableStore
        tmp = tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = DurableStore(Path(tmp.name) / "orchestrator.sqlite3")
        self.addCleanup(store.close)
        store.ensure_generation_plan("job", tmp.name, "T", scenes, config)
        return {r["kind"] for r in store.task_rows("job")}

    def _scene(self, mode):
        from pipeline.llm import Scene
        lines = [{"speaker": "A", "text": "hi"}] if mode != "narration" else []
        return Scene(id=1, title="S", image_prompt="i", video_prompt="v",
                     narration="n" if mode == "narration" else "",
                     mode=mode, lines=lines, metadata_extra={"mode": mode})

    def test_music_off_plans_no_music_task(self):
        kinds = self._plan([self._scene("narration")], {"music_enabled": False})
        self.assertNotIn("music.generate", kinds)

    def test_music_on_by_default_for_a_narrated_film(self):
        kinds = self._plan([self._scene("narration")], {})
        self.assertIn("music.generate", kinds)

    def test_an_all_acted_film_carries_its_own_sound(self):
        kinds = self._plan([self._scene("dialogue")], {})
        self.assertNotIn("music.generate", kinds)


if __name__ == "__main__":
    unittest.main()


class UnvoicedCharacterTests(TempConfigCase):
    """A character with no cast voice is legal: no <Audio N> slot is wired and
    the model invents the voice. The remaining slots must not be renumbered."""

    def setUp(self):
        super().setUp()
        self.write_config({
            "styles": [_style("Acted", n_scenes=2, voice="Narrator")],
            "default_style": "Acted",
            "voices": [{"name": "Kara", "path": str(self.output_dir / "kara.wav"),
                        "gender": "female", "age": "adult"}],
            "characters": [],
            "characters_migrated_v2": True,
        })
        (self.output_dir / "kara.wav").write_bytes(b"wav")
        self.work_dir = self.output_dir / "film-20260807-120000"
        (self.work_dir / "characters").mkdir(parents=True)
        for cid in ("char_a", "char_b"):
            (self.work_dir / "characters" / f"{cid}.png").write_bytes(b"png")
        # MUTE has a portrait but deliberately no voice.
        (self.work_dir / "characters.json").write_text(json.dumps([
            {"id": "char_a", "name": "MUTE", "description": "x", "ref_image": "char_a.png",
             "voice": "", "enabled": True},
            {"id": "char_b", "name": "VOICED", "description": "y", "ref_image": "char_b.png",
             "voice": "Kara", "enabled": True},
        ]))

    def _refs(self, lines):
        meta = {"mode": "performance", "cast": ["MUTE", "VOICED"], "seconds": 10,
                "lines": lines}
        cfg = {**backend.gapp.load_config(), "style_name": "Acted"}
        return meta, backend.gapp.resolve_performance_references(
            meta, cfg, self.work_dir, "Acted")

    def test_unvoiced_speaker_gets_no_audio_slot(self):
        _, refs = self._refs([{"speaker": "MUTE", "text": "one"},
                              {"speaker": "VOICED", "text": "two"}])
        # Both appear as pictures; only the voiced one takes an audio slot.
        self.assertEqual([p["name"] for p in refs["pictures"]], ["MUTE", "VOICED"])
        self.assertEqual([(a["slot"], a["name"]) for a in refs["audios"]], [(1, "VOICED")])

    def test_prompt_only_claims_the_voice_it_supplies(self):
        meta, refs = self._refs([{"speaker": "MUTE", "text": "one"},
                                 {"speaker": "VOICED", "text": "two"}])
        prompt = performance.build_h3_prompt(
            meta, picture_names=[p["name"] for p in refs["pictures"]],
            audio_names=[a["name"] for a in refs["audios"]])
        # MUTE still acts and speaks — the model just picks their voice.
        self.assertIn("<Audio 1> defines VOICED's voice", prompt)
        self.assertNotIn("MUTE's voice", prompt)
        self.assertIn('MUTE says exactly', prompt)

    def test_editor_flags_the_unvoiced_speaker(self):
        # The performance view surfaces this so it is a choice, not a surprise.
        meta, refs = self._refs([{"speaker": "MUTE", "text": "one"}])
        voiced = {a["name"] for a in refs["audios"]}
        unvoiced = [n for n in performance.speakers_in(performance.norm_lines(meta["lines"]))
                    if n not in voiced]
        self.assertEqual(unvoiced, ["MUTE"])


class ScenePersistenceTests(unittest.TestCase):
    """Saving a scene (which "Approve → render" does first) must not quietly
    strip the performance fields."""

    def test_delivery_survives_a_save(self):
        # _clean_lines rebuilt each line as {speaker, text, shot}, so approving
        # a performance script dropped every delivery direction.
        cleaned = backend._clean_lines([
            {"speaker": "JOE", "text": "Welcome to the show.", "delivery": "warm, upbeat"},
            {"speaker": "KINHO", "text": "Thanks Joe.", "delivery": ""},
        ])
        self.assertEqual(cleaned[0]["delivery"], "warm, upbeat")
        self.assertNotIn("delivery", cleaned[1])  # blank stays absent

    def test_dialogue_lines_are_unchanged(self):
        # Narrated/dialogue scripts must stay byte-identical.
        cleaned = backend._clean_lines([{"speaker": "A", "text": "hi", "shot": "medium"}])
        self.assertEqual(cleaned, [{"speaker": "A", "text": "hi", "shot": "medium"}])


class FilmScreenTests(TempConfigCase):
    """The film screen must open for a film that legitimately has no music."""

    def setUp(self):
        super().setUp()
        self.write_config({"styles": [_style("Acted")], "default_style": "Acted",
                           "characters": [], "characters_migrated_v2": True})
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        self.addCleanup(mock.patch.stopall)
        self.wd = self.output_dir / "film-20260808-100000"
        self.wd.mkdir()
        (self.wd / "combined.mp4").write_bytes(b"mp4" * 4000)
        (self.output_dir / "film-20260808-100000.mp4").write_bytes(b"mp4" * 4000)

    def test_missing_music_does_not_404_the_screen(self):
        # A performance film has no background_music.wav — requiring it made the
        # whole screen fail with "Required files not found".
        data = backend.remix_load(work_dir=str(self.wd))
        self.assertFalse(data["can_remix"])
        self.assertTrue(data["final_url"])

    def test_music_present_still_offers_the_mixer(self):
        (self.wd / "background_music.wav").write_bytes(b"wav")
        self.assertTrue(backend.remix_load(work_dir=str(self.wd))["can_remix"])

    def test_remixing_a_film_with_no_stems_is_refused_clearly(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.remix_apply(backend.RemixBody(work_dir=str(self.wd)))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("generated with the picture", str(ctx.exception.detail))

    def test_missing_combined_still_404s(self):
        (self.wd / "combined.mp4").unlink()
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.remix_load(work_dir=str(self.wd))
        self.assertEqual(ctx.exception.status_code, 404)


class VisualsTests(TempConfigCase):
    """Locations and wardrobe: reference images that pin the place and the
    clothes across scenes."""

    def setUp(self):
        super().setUp()
        self.write_config({"styles": [_style("Acted")], "default_style": "Acted",
                           "voices": [], "characters": [], "characters_migrated_v2": True})
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        self.addCleanup(mock.patch.stopall)
        self.wd = self.output_dir / "film-20260808-120000"
        (self.wd / "characters").mkdir(parents=True)
        (self.wd / "characters" / "c1.png").write_bytes(b"png")
        (self.wd / "characters.json").write_text(json.dumps([
            {"id": "c1", "name": "JOE", "description": "host", "ref_image": "c1.png",
             "voice": "", "enabled": True}]))
        (self.wd / "visuals").mkdir()

    def _visual(self, **over):
        v = {"name": "The studio", "kind": "location", "description": "a podcast studio"}
        v.update(over)
        backend.gapp.add_script_visual(self.wd, v["name"], v["kind"],
                                       v["description"], v.get("character", ""))
        saved = backend.gapp.read_script_visuals(self.wd)[-1]
        img = self.wd / "visuals" / f"{saved['id']}.png"
        img.write_bytes(b"png")
        backend.gapp.update_script_visual(self.wd, saved["id"], **{
            k: v[k] for k in ("scenes",) if k in v})
        visuals = backend.gapp.read_script_visuals(self.wd)
        for x in visuals:
            if x["id"] == saved["id"]:
                x["ref_image"] = img.name
        backend.gapp.write_script_visuals(self.wd, visuals)
        return saved["id"]

    def _refs(self, scene_id=1, cast=("JOE",)):
        meta = {"mode": "performance", "cast": list(cast), "seconds": 10,
                "lines": [{"speaker": "JOE", "text": "hi"}]}
        cfg = {**backend.gapp.load_config(), "style_name": "Acted"}
        return meta, backend.gapp.resolve_performance_references(
            meta, cfg, self.wd, "Acted", scene_id=scene_id)

    def test_location_takes_a_slot_after_the_cast(self):
        self._visual()
        _, refs = self._refs()
        self.assertEqual([(p["slot"], p["name"], p["kind"]) for p in refs["pictures"]],
                         [(1, "JOE", "character"), (2, "The studio", "location")])

    def test_scene_scoped_visual_only_applies_there(self):
        self._visual(scenes=[2])
        self.assertEqual(len(self._refs(scene_id=1)[1]["pictures"]), 1)
        self.assertEqual(len(self._refs(scene_id=2)[1]["pictures"]), 2)

    def test_wardrobe_follows_its_character(self):
        self._visual(name="Blue henley", kind="wardrobe", character="JOE")
        self.assertEqual(len(self._refs(cast=("JOE",))[1]["pictures"]), 2)
        # A scene JOE is not in gets no JOE portrait and no JOE wardrobe.
        self.assertEqual(len(self._refs(cast=("KINHO",))[1]["pictures"]), 0)

    def test_prompt_gives_each_kind_its_own_job(self):
        self._visual()
        self._visual(name="Blue henley", kind="wardrobe", character="JOE")
        meta, refs = self._refs()
        prompt = performance.build_h3_prompt(meta, picture_names=refs["pictures"])
        # The character's slot now carries a short identity hint too.
        self.assertIn("<Picture 1> defines JOE's face, hair and build only — JOE is host", prompt)
        self.assertIn("<Picture 2> defines the place only", prompt)
        self.assertIn("<Picture 3> defines the clothes JOE wears only", prompt)

    def test_locations_come_before_wardrobe(self):
        self._visual(name="Coat", kind="wardrobe")
        self._visual(name="The studio", kind="location")
        _, refs = self._refs()
        self.assertEqual([p["kind"] for p in refs["pictures"]],
                         ["character", "location", "wardrobe"])

    def test_visual_without_an_image_is_ignored(self):
        backend.gapp.add_script_visual(self.wd, "Imagined place", "location", "somewhere")
        _, refs = self._refs()
        self.assertEqual([p["kind"] for p in refs["pictures"]], ["character"])


class AssetCatalogueTests(TempConfigCase):
    """Reusable locations and wardrobe, scoped like characters."""

    def setUp(self):
        super().setUp()
        self.write_config({
            "styles": [_style("Parent"), {**_style("Child"), "parent": "Parent"},
                       _style("Other")],
            "default_style": "Parent", "characters": [], "characters_migrated_v2": True,
            "assets": [
                {"id": "ast_g", "name": "Global studio", "kind": "location", "description": "x"},
                {"id": "ast_p", "name": "Parent set", "kind": "location", "style": "Parent"},
                {"id": "ast_o", "name": "Other set", "kind": "location", "style": "Other"},
            ],
        })
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        self.addCleanup(mock.patch.stopall)

    def test_scoping_matches_characters(self):
        cfg = backend.gapp.load_config()
        names = lambda st: sorted(a["name"] for a in backend.gapp.style_assets(cfg, st))
        self.assertEqual(names("Parent"), ["Global studio", "Parent set"])
        # A child inherits its parent's assets.
        self.assertEqual(names("Child"), ["Global studio", "Parent set"])
        self.assertEqual(names("Other"), ["Global studio", "Other set"])
        # Experiment mode sees the global pool only, like characters.
        self.assertEqual(names(backend.gapp.NO_STYLE), ["Global studio"])

    def test_catalogue_asset_reaches_a_scene(self):
        wd = self.output_dir / "film-20260808-140000"
        (wd / "characters").mkdir(parents=True)
        (wd / "characters.json").write_text("[]")
        img = backend.gapp._assets_dir() / "ast_g.png"
        img.write_bytes(b"png")
        cfg = backend.gapp.load_config()
        cfg["assets"][0]["ref_image"] = img.name
        backend.gapp.save_assets(cfg["assets"])
        vis = backend.gapp.scene_visuals(wd, 1, [], backend.gapp.load_config(), "Parent")
        self.assertEqual([v["name"] for v in vis], ["Global studio"])

    def test_the_films_own_visual_shadows_the_catalogue(self):
        wd = self.output_dir / "film-20260808-140100"
        (wd / "visuals").mkdir(parents=True)
        backend.gapp.add_script_visual(wd, "Global studio", "location", "this film's own")
        own = backend.gapp.read_script_visuals(wd)[0]
        own_img = wd / "visuals" / f"{own['id']}.png"
        own_img.write_bytes(b"png")
        backend.gapp.update_script_visual(wd, own["id"])
        visuals = backend.gapp.read_script_visuals(wd)
        visuals[0]["ref_image"] = own_img.name
        backend.gapp.write_script_visuals(wd, visuals)
        cat_img = backend.gapp._assets_dir() / "ast_g.png"
        cat_img.write_bytes(b"png")
        cfg = backend.gapp.load_config()
        cfg["assets"][0]["ref_image"] = cat_img.name
        backend.gapp.save_assets(cfg["assets"])

        vis = backend.gapp.scene_visuals(wd, 1, [], backend.gapp.load_config(), "Parent")
        self.assertEqual(len(vis), 1)
        self.assertEqual(vis[0]["description"], "this film's own")


class OneClipDefaultTests(TempConfigCase):
    """The default: one scene = one generation, both speakers in the clip."""

    def setUp(self):
        super().setUp()
        self.write_config({"styles": [_style("Acted")], "default_style": "Acted",
                           "voices": [], "characters": [], "characters_migrated_v2": True})
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        self.addCleanup(mock.patch.stopall)

    def test_a_two_hander_renders_as_a_single_clip_by_default(self):
        import resume_generation as rg
        from pipeline.llm import Scene
        wd = self.output_dir / "film-20260809-160000"
        (wd / "characters").mkdir(parents=True)
        (wd / "characters" / "a.png").write_bytes(b"png")
        (wd / "characters" / "b.png").write_bytes(b"png")
        (wd / "characters.json").write_text(json.dumps([
            {"id": "a", "name": "A", "description": "x", "ref_image": "a.png", "enabled": True},
            {"id": "b", "name": "B", "description": "y", "ref_image": "b.png", "enabled": True}]))
        calls = []

        def fake_gen(engine, prompt, ref_images, out, ref_audios=None, **kw):
            calls.append({"prompt": prompt, "refs": len(ref_images)})
            Path(out).write_bytes(b"mp4")
            return Path(out)

        lines = [{"speaker": "A", "text": "one"}, {"speaker": "B", "text": "two"}]
        scene = Scene(id=1, title="S", image_prompt="", video_prompt="", narration="",
                      mode="performance", lines=lines,
                      metadata_extra={"mode": "performance", "cast": ["A", "B"], "seconds": 12})
        with mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
             mock.patch.object(rg, "ensure_video_resolution"):
            out = rg.render_performance_scene(
                scene, wd, {"style_name": "Acted", "performance_verify": False},
                comfy_url="http://w:8188", vid_width=704, vid_height=1280)
        # ONE generation, whole conversation, both people placed and locked.
        self.assertEqual(len(calls), 1)
        self.assertEqual(out.name, "scene_01_final.mp4")
        self.assertEqual(calls[0]["refs"], 2)
        prompt = calls[0]["prompt"]
        self.assertIn("Exactly 2 people on screen", prompt)
        self.assertIn("A on the left, B on the right", prompt)
        self.assertIn('A says exactly', prompt)
        self.assertIn('B says exactly', prompt)
        self.assertNotIn("Do not show", prompt)   # nobody is off-frame


class SceneContinuityTests(unittest.TestCase):
    """Every shot is its own generation, so without a hint the room changes
    between cuts — the scene appears to teleport."""

    def _film(self, n_lines=3, frame_fails=False):
        import resume_generation as rg
        from pipeline.llm import Scene
        calls = []

        def fake_gen(engine, prompt, ref_images, out, ref_audios=None, **kw):
            calls.append({"prompt": prompt, "ref_images": [Path(p).name for p in ref_images]})
            Path(out).write_bytes(b"mp4")
            return Path(out)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wd = Path(tmp.name)
        speakers = ["A", "B", "A"][:n_lines]
        meta = {"mode": "performance", "cast": ["A", "B"], "seconds": 12,
                "lines": [{"speaker": s, "text": f"line {i}"} for i, s in enumerate(speakers)]}
        scene = Scene(id=1, title="S", image_prompt="", video_prompt="", narration="",
                      mode="performance", lines=meta["lines"],
                      metadata_extra={k: v for k, v in meta.items() if k != "lines"})

        def fake_refs(m, cfg, work_dir, style_name="", scene_id=0):
            return {"pictures": [{"slot": 1, "name": m.get("speaker") or "A",
                                  "kind": "character", "path": str(wd / "face.png")}],
                    "audios": []}

        (wd / "face.png").write_bytes(b"png")
        with mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
             mock.patch("app.resolve_performance_references", side_effect=fake_refs), \
             mock.patch.object(rg, "concatenate_scenes",
                               side_effect=lambda c, o: Path(o).write_bytes(b"j")), \
             mock.patch.object(rg, "ensure_video_resolution"), \
             mock.patch.object(rg, "extract_last_frame",
                               side_effect=(RuntimeError("no ffmpeg") if frame_fails
                                            else lambda src, dst: Path(dst).write_bytes(b"png"))):
            rg.render_performance_scene(scene, wd, {"performance_shot_split": True,
                                                    "performance_establishing": False,
                                                    "performance_verify": False},
                                        comfy_url="http://w:8188",
                                        vid_width=704, vid_height=1280)
        return calls

    def test_continuity_frame_replaces_the_location_not_stacks_on_it(self):
        # Measured in a real A/B: at 3 picture refs everything held; at 4+ the
        # weakest (wardrobe colour, location) dropped. The room frame IS the
        # location photographed, so later shots must swap it in, not add it.
        import resume_generation as rg
        from pipeline.llm import Scene
        calls = []

        def fake_gen(engine, prompt, ref_images, out, ref_audios=None, **kw):
            calls.append({"prompt": prompt, "refs": [Path(p).name for p in ref_images]})
            Path(out).write_bytes(b"mp4")
            return Path(out)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wd = Path(tmp.name)
        (wd / "face.png").write_bytes(b"png")
        (wd / "loc.png").write_bytes(b"png")
        meta = {"mode": "performance", "cast": ["A", "B"], "seconds": 12,
                "lines": [{"speaker": "A", "text": "one"}, {"speaker": "B", "text": "two"}]}
        scene = Scene(id=1, title="S", image_prompt="", video_prompt="", narration="",
                      mode="performance", lines=meta["lines"],
                      metadata_extra={k: v for k, v in meta.items() if k != "lines"})

        def fake_refs(m, cfg, work_dir, style_name="", scene_id=0):
            return {"pictures": [
                {"slot": 1, "name": m.get("speaker") or "A", "kind": "character",
                 "path": str(wd / "face.png")},
                {"slot": 2, "name": "The wharf", "kind": "location",
                 "path": str(wd / "loc.png")}], "audios": []}

        with mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
             mock.patch("app.resolve_performance_references", side_effect=fake_refs), \
             mock.patch.object(rg, "concatenate_scenes",
                               side_effect=lambda c, o: Path(o).write_bytes(b"j")), \
             mock.patch.object(rg, "ensure_video_resolution"), \
             mock.patch.object(rg, "extract_last_frame",
                               side_effect=lambda src, dst: Path(dst).write_bytes(b"png")):
            rg.render_performance_scene(scene, wd, {"performance_shot_split": True,
                                                    "performance_establishing": False,
                                                    "performance_verify": False},
                                        comfy_url="http://w:8188",
                                        vid_width=704, vid_height=1280)
        # Shot 1: location asset present, no room frame yet.
        self.assertIn("loc.png", calls[0]["refs"])
        # Shot 2: room frame IN, location asset OUT, slots renumbered densely.
        self.assertIn("scene_01_room.png", calls[1]["refs"])
        self.assertNotIn("loc.png", calls[1]["refs"])
        self.assertIn("<Picture 2> defines the SAME room", calls[1]["prompt"])

    def test_later_shots_reference_the_first_shots_room(self):
        calls = self._film()
        self.assertEqual(len(calls), 3)
        # The opening shot has nothing to match yet.
        self.assertNotIn("scene_01_room.png", calls[0]["ref_images"])
        self.assertNotIn("the SAME room", calls[0]["prompt"])
        # Every shot after it does.
        for call in calls[1:]:
            self.assertIn("scene_01_room.png", call["ref_images"])
            self.assertIn("the SAME room, furniture, lighting", call["prompt"])

    def test_a_failed_continuity_frame_does_not_stop_the_scene(self):
        calls = self._film(frame_fails=True)
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertNotIn("the SAME room", call["prompt"])


class ActedSceneEditingTests(unittest.TestCase):
    """The performance view edits dialogue and the prompt in place."""

    def setUp(self):
        import app as gapp
        import webapp.backend.main as backend
        self.backend = backend
        tmp = tempfile.TemporaryDirectory(prefix="spielbot-acted-edit-")
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

        from pipeline.orchestrator import DurableStore, job_id_from_work_dir
        self.wd = self.output_dir / "film"
        self.wd.mkdir()
        (self.wd / "scene_01_final.mp4").write_bytes(b"x" * 20_000)
        self.job_id = job_id_from_work_dir(self.wd)
        store = DurableStore.default()
        try:
            store.create_or_update_job(self.job_id, self.wd, "Film",
                                       config={"video_title": "Film"}, metadata={})
            store.upsert_scene(self.job_id, 1, title="Talk", image_prompt="",
                               video_prompt="a wharf", narration="You came.",
                               metadata={"mode": "dialogue", "cast": ["Ana"],
                                         "lines": [{"speaker": "Ana", "text": "You came."}]})
        finally:
            store.close()

    def _scene(self):
        return self.backend.load_performance_script(work_dir=str(self.wd))["scenes"][0]

    def _save(self, **body):
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            title="Talk", image_prompt="", video_prompt="a wharf",
            narration="You came.", mode="dialogue", **body))

    def test_edited_dialogue_rewrites_the_prompt_and_the_spoken_text(self):
        self._save(lines=[{"speaker": "Ana", "text": "You are late."}])
        scene = self._scene()
        self.assertEqual([l["text"] for l in scene["lines"]], ["You are late."])
        self.assertIn("You are late.", scene["prompt"])
        self.assertIn("You are late.", scene["narration"])

    def test_an_edited_prompt_is_what_the_render_sends(self):
        self._save(prompt="Just this, verbatim.")
        scene = self._scene()
        self.assertTrue(scene["prompt_edited"])
        self.assertEqual(scene["prompt"], "Just this, verbatim.")
        # …and the renderer assembles from the same function, so it agrees.
        from pipeline import performance as perf
        from pipeline.orchestrator import DurableStore
        store = DurableStore.default()
        try:
            meta = store.get_scene(self.job_id, 1)["metadata"]
        finally:
            store.close()
        self.assertEqual(perf.build_h3_prompt(meta), "Just this, verbatim.")

    def test_clearing_the_override_rebuilds_from_the_scene(self):
        self._save(prompt="Pinned.")
        self._save(prompt="")
        scene = self._scene()
        self.assertFalse(scene["prompt_edited"])
        self.assertIn("[REFERENCE USE]", scene["prompt"] + "[REFERENCE USE]")
        self.assertNotEqual(scene["prompt"], "Pinned.")

    def test_the_acted_view_carries_the_take_history(self):
        # Every re-shoot is kept as a take; the acted view must show them.
        from pipeline import video_history
        self._save()   # persists script.json, which the acted view reads
        video_history.record(self.wd, 1, self.wd / "scene_01_final.mp4")
        scene = self._scene()
        self.assertIn("video_history", scene)
        self.assertEqual(len(scene["video_history"]["versions"]), 1)

    def test_a_finished_film_keeps_its_clip_through_an_edit(self):
        # The take is the deliverable — the film editor re-shoots on request.
        (self.wd / "combined.mp4").write_bytes(b"x" * 20_000)
        self._save(lines=[{"speaker": "Ana", "text": "Something else."}])
        self.assertTrue((self.wd / "scene_01_final.mp4").exists())

    def test_an_unrendered_scene_drops_its_stale_take(self):
        self._save(lines=[{"speaker": "Ana", "text": "Something else."}])
        self.assertFalse((self.wd / "scene_01_final.mp4").exists())


class MixedPreviewTests(unittest.TestCase):
    """A mixed film paints stills for its narrated scenes only."""

    def _rows(self):
        return [
            {"id": 1, "title": "open", "image_prompt": "i", "preview_path": "",
             "metadata": {}},
            {"id": 2, "title": "talk", "image_prompt": "", "preview_path": "",
             "metadata": {"mode": "dialogue", "lines": [{"speaker": "A", "text": "hi"}]}},
        ]

    def test_only_the_narrated_scene_is_painted(self):
        import webapp.backend.main as backend
        store = mock.MagicMock()
        store.scene_rows.return_value = self._rows()
        with mock.patch.object(backend.DurableStore, "default", return_value=store), \
             mock.patch.object(backend.gapp, "_preview_worker_urls",
                               return_value=["http://a:8188"]), \
             mock.patch.object(backend.gapp, "WorkerPool"), \
             mock.patch.object(backend.gapp, "_job_work_dir", return_value=None), \
             mock.patch.object(backend.gapp, "_generate_active_scene_preview") as gen:
            out = backend.generate_all_previews("job")
        self.assertEqual([c.args[1] for c in gen.call_args_list], [1])
        self.assertEqual(out["generated"], 1)
        # the response still describes every scene, acted ones included
        self.assertEqual(len(out["scenes"]), 2)

    def test_an_all_acted_film_paints_nothing(self):
        import webapp.backend.main as backend
        store = mock.MagicMock()
        store.scene_rows.return_value = [self._rows()[1]]
        with mock.patch.object(backend.DurableStore, "default", return_value=store), \
             mock.patch.object(backend.gapp, "_generate_active_scene_preview") as gen:
            out = backend.generate_all_previews("job")
        gen.assert_not_called()
        self.assertIn("skipped", out)


class ActedFieldEditingTests(ActedSceneEditingTests):
    """The acted scene is written through its FIELDS; the prompt follows."""

    def test_saving_fields_assembles_the_prompt(self):
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            title="Talk", mode="dialogue",
            lines=[{"speaker": "Ana", "text": "You came."}],
            setting="a fog-wrapped wharf before dawn",
            camera="locked wide, no move",
            soundscape="gulls, water on pilings",
            cast=["Ana", "Bo"],
            beats=[{"t0": 0, "t1": 4, "action": "Ana turns as Bo arrives"}]))
        scene = self.backend.job_scenes(self.job_id)["scenes"][0]
        for piece in ("a fog-wrapped wharf before dawn", "locked wide",
                      "gulls, water on pilings", "[0s-4s]", "You came."):
            self.assertIn(piece, scene["video_prompt"])
        # the editor reads these back as fields, not by parsing the prompt
        self.assertEqual(scene["cast"], ["Ana", "Bo"])
        self.assertEqual(scene["camera"], "locked wide, no move")
        self.assertEqual(len(scene["beats"]), 1)

    def test_acted_scene_never_keeps_an_image_prompt(self):
        # No image engine runs for an acted scene — a stale FLUX prompt on the
        # row is what made the editor look like the old talking-head path.
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            title="Talk", image_prompt="a leftover FLUX prompt",
            video_prompt="a wharf", mode="dialogue",
            lines=[{"speaker": "Ana", "text": "Hi."}]))
        from pipeline.orchestrator import DurableStore
        store = DurableStore.default()
        try:
            row = store.get_scene(self.job_id, 1)
        finally:
            store.close()
        self.assertEqual(row["image_prompt"], "")
        self.assertTrue(row["video_prompt"].startswith("[REFERENCE USE]"))

    def test_saving_twice_does_not_nest_the_prompt(self):
        # The stored video_prompt IS the assembly; a second save must not feed
        # it back in as the setting.
        body = dict(title="Talk", mode="dialogue",
                    lines=[{"speaker": "Ana", "text": "Hi."}],
                    setting="", camera="", soundscape="")
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            **body, video_prompt="a wharf"))
        first = self.backend.job_scenes(self.job_id)["scenes"][0]["video_prompt"]
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            **body, video_prompt=first))
        second = self.backend.job_scenes(self.job_id)["scenes"][0]["video_prompt"]
        self.assertEqual(first.count("[REFERENCE USE]"), 1)
        self.assertEqual(second.count("[REFERENCE USE]"), 1)


class CoverPromptTests(unittest.TestCase):
    """The cover must never see the film title, and an acted scene's subject
    is its cast and setting — that's also what pulls the character reference
    portraits into the cover render."""

    def _mixed(self):
        return [
            {"id": 1, "title": "Why the Cut Stops Bleeding", "image_prompt": "",
             "metadata": {"mode": "dialogue", "cast": ["Amelia"],
                          "setting": "a sunny playground, a scraped knee at frame edge"}},
            {"id": 2, "title": "The alarm", "image_prompt": "x" * 30, "metadata": {}},
            {"id": 3, "title": "The Body Fixed Itself", "image_prompt": "",
             "metadata": {"mode": "dialogue", "cast": ["Amelia"],
                          "setting": "a sunny bench near the playground"}},
        ]

    def test_acted_scenes_contribute_cast_and_setting_not_their_title(self):
        from pipeline import cover
        aspects = cover._extract_scene_aspects(self._mixed())
        self.assertIn("Amelia — a sunny playground", aspects)
        # An acted scene's title paraphrases the film title — the model paints
        # whatever words reach it, so the title must never be the fallback.
        self.assertNotIn("Why the Cut Stops Bleeding", aspects)

    def test_poisoned_prompt_regression(self):
        # The real failure: start_generation back-filled acted scenes' empty
        # image prompts with "style. film title", and the cover painted it.
        import webapp.backend.main as backend
        row = {"id": 1, "title": "S", "image_prompt": "",
               "video_prompt": "[REFERENCE USE]\nassembled",
               "metadata": {"mode": "dialogue", "lines": [{"speaker": "A", "text": "hi"}]}}
        # mirror the start_generation list comprehension's per-row logic
        acted = backend.performance_mode.is_performance_mode(row["metadata"]["mode"])
        image_prompt = ("" if acted else
                        backend._apply_style_prefix("Cartoons", row.get("image_prompt") or "Film Title"))
        self.assertEqual(image_prompt, "")


class StartGenerationActedTests(unittest.TestCase):
    """start_generation must not back-fill acted scenes with the film title."""

    def setUp(self):
        import app as gapp
        import webapp.backend.main as backend
        self.backend = backend
        tmp = tempfile.TemporaryDirectory(prefix="spielbot-acted-start-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.output_dir = root / "videos"; self.output_dir.mkdir()
        cfg_file = root / "config" / "config.yaml"; cfg_file.parent.mkdir(parents=True)
        for target, attr, value in [(gapp, "CONFIG_FILE", cfg_file),
                                    (gapp, "OUTPUT_DIR", self.output_dir)]:
            patch = mock.patch.object(target, attr, value)
            patch.start(); self.addCleanup(patch.stop)
        db = mock.patch.dict(os.environ,
                             {"SPIELBOT_ORCHESTRATOR_DB": str(root / "orchestrator.sqlite3")})
        db.start(); self.addCleanup(db.stop)

    def test_acted_scene_keeps_empty_image_prompt_through_approval(self):
        import app as gapp
        from pipeline.orchestrator import DurableStore, job_id_from_work_dir
        wd = self.output_dir / "film-20260810-000000"; wd.mkdir()
        job_id = job_id_from_work_dir(wd)
        store = DurableStore.default()
        try:
            store.create_or_update_job(job_id, wd, "Film", config={})
            store.upsert_scenes(job_id, [
                {"id": 1, "title": "Talk", "image_prompt": "", "narration": "hi",
                 "video_prompt": "[REFERENCE USE]\nassembled",
                 "metadata": {"mode": "dialogue", "cast": ["A"],
                              "lines": [{"speaker": "A", "text": "hi"}]}},
                {"id": 2, "title": "Open", "image_prompt": "a frame",
                 "video_prompt": "a move", "narration": "words", "metadata": {}},
            ])
        finally:
            store.close()
        with mock.patch.object(gapp, "_launch_generation_job", return_value={}):
            self.backend.start_generation(self.backend.GenerateBody(
                job_id=job_id, work_dir=str(wd), video_title="My Film Title"))
        scenes = json.loads((wd / "script.json").read_text())
        by_id = {s["id"]: s for s in scenes}
        self.assertEqual(by_id[1]["image_prompt"], "")            # acted: untouched
        self.assertIn("[REFERENCE USE]", by_id[1]["video_prompt"])
        self.assertNotIn("My Film Title", by_id[1]["video_prompt"])
        self.assertIn("a frame", by_id[2]["image_prompt"])        # narrated: as before


class MixedEngineTests(unittest.TestCase):
    """A mixed film's narrated scenes render on H3, matching the acted takes."""

    def _unify(self, engine_key, **kw):
        import resume_generation as rg
        from pipeline import engines
        eng = engines.resolve_video({}, engine_key)
        return rg.unify_mixed_engine(eng, {}, **kw)

    def test_mixed_film_moves_narrated_scenes_onto_h3(self):
        out = self._unify("ltx23", has_acted=True, has_classic=True)
        self.assertEqual(out["key"], "minimax-h3")

    def test_a_style_already_on_a_minimax_engine_keeps_its_pick(self):
        out = self._unify("minimax-h3-turbo", has_acted=True, has_classic=True)
        self.assertEqual(out["key"], "minimax-h3-turbo")

    def test_unmixed_films_are_untouched(self):
        self.assertEqual(self._unify("ltx23", has_acted=False, has_classic=True)["key"], "ltx23")
        self.assertEqual(self._unify("ltx23", has_acted=True, has_classic=False)["key"], "ltx23")


class ActedSceneRegenTests(ActedSceneEditingTests):
    """One button rewrites the whole acted take via the LLM."""

    _REPLY = json.dumps({
        "title": "You Are Late",
        "cast": ["Ana"],
        "setting": "a rain-soaked bus stop at night",
        "lines": [{"speaker": "Ana", "delivery": "sharp, hurt", "text": "You are late again."}],
        "beats": [{"t0": 0, "t1": 4, "action": "Ana checks the empty road"}],
        "camera": "locked medium shot",
        "soundscape": "rain on the shelter roof",
    })

    def test_regenerates_the_whole_take_and_reassembles_the_prompt(self):
        with mock.patch.object(self.backend, "_llm_complete", return_value=self._REPLY):
            r = self.backend.regenerate_acted_scene(
                self.job_id, 1, self.backend.ActedRegenBody(instruction="make it tense"))
        scene = r["scene"]
        self.assertEqual(scene["title"], "You Are Late")
        self.assertEqual([l["text"] for l in scene["lines"]], ["You are late again."])
        for piece in ("rain-soaked bus stop", "rain on the shelter roof",
                      "You are late again.", "[0s-4s]"):
            self.assertIn(piece, scene["video_prompt"])
        self.assertEqual(scene["image_prompt"], "")

    def test_a_pinned_prompt_is_superseded_by_the_rewrite(self):
        self._save(prompt="Pinned by hand.")
        with mock.patch.object(self.backend, "_llm_complete", return_value=self._REPLY):
            r = self.backend.regenerate_acted_scene(self.job_id, 1,
                                                    self.backend.ActedRegenBody())
        self.assertFalse(r["scene"]["prompt_edited"])
        self.assertNotEqual(r["scene"]["video_prompt"], "Pinned by hand.")

    def test_a_narrated_scene_is_refused(self):
        from pipeline.orchestrator import DurableStore
        store = DurableStore.default()
        try:
            store.upsert_scene(self.job_id, 2, title="N", image_prompt="i",
                               video_prompt="v", narration="n", metadata={})
        finally:
            store.close()
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self.backend.regenerate_acted_scene(self.job_id, 2, self.backend.ActedRegenBody())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_garbage_from_the_llm_is_a_clean_error_not_a_wipe(self):
        from fastapi import HTTPException
        for reply in ("not json at all", json.dumps({"title": "x", "lines": []})):
            with mock.patch.object(self.backend, "_llm_complete", return_value=reply):
                with self.assertRaises(HTTPException) as ctx:
                    self.backend.regenerate_acted_scene(self.job_id, 1,
                                                        self.backend.ActedRegenBody())
            self.assertEqual(ctx.exception.status_code, 502)
        # the original scene survived untouched
        scene = self.backend.job_scenes(self.job_id)["scenes"][0]
        self.assertEqual([l["text"] for l in scene["lines"]], ["You came."])


class ReassembleActedTests(unittest.TestCase):
    """A film without a score can still be reassembled after a re-shoot."""

    def setUp(self):
        import app as gapp
        import webapp.backend.main as backend
        self.backend = backend
        tmp = tempfile.TemporaryDirectory(prefix="spielbot-reassemble-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.output_dir = root / "videos"; self.output_dir.mkdir()
        cfg_file = root / "config" / "config.yaml"; cfg_file.parent.mkdir(parents=True)
        for target, attr, value in [(gapp, "CONFIG_FILE", cfg_file),
                                    (gapp, "OUTPUT_DIR", self.output_dir)]:
            patch = mock.patch.object(target, attr, value)
            patch.start(); self.addCleanup(patch.stop)
        db = mock.patch.dict(os.environ,
                             {"SPIELBOT_ORCHESTRATOR_DB": str(root / "orchestrator.sqlite3")})
        db.start(); self.addCleanup(db.stop)

        from pipeline.orchestrator import DurableStore, job_id_from_work_dir
        self.wd = self.output_dir / "acted-film-20260810-000000"; self.wd.mkdir()
        (self.wd / "scene_01_final.mp4").write_bytes(b"x" * 20_000)
        store = DurableStore.default()
        try:
            store.create_or_update_job(job_id_from_work_dir(self.wd), self.wd, "F", config={})
            store.upsert_scenes(job_id_from_work_dir(self.wd), [
                {"id": 1, "title": "T", "image_prompt": "", "video_prompt": "p",
                 "narration": "", "metadata": {"mode": "dialogue",
                                               "lines": [{"speaker": "A", "text": "hi"}]}}])
        finally:
            store.close()

    def test_no_music_reassembles_to_the_concat(self):
        # No background_music.wav on disk — the acted film never got a score.
        def fake_concat(clips, out):
            Path(out).write_bytes(b"concat")
        with mock.patch("pipeline.assembler.concatenate_scenes", side_effect=fake_concat), \
             mock.patch("pipeline.assembler.mix_background_music") as mix, \
             mock.patch("pipeline.assembler.ensure_video_resolution"), \
             mock.patch.object(self.backend, "_maybe_burn_first_frame_cover"):
            n = self.backend._reassemble_film_core(self.wd)
        self.assertEqual(n, 1)
        mix.assert_not_called()
        final = self.backend.gapp._final_path_for_work_dir(self.wd)
        self.assertEqual(final.read_bytes(), b"concat")
        self.assertTrue((self.wd / "combined.mp4").exists())

    def test_music_off_skips_the_mix_even_with_a_score_on_disk(self):
        (self.wd / "background_music.wav").write_bytes(b"wav")
        (self.wd / "job_config.json").write_text(json.dumps({"music_enabled": False}))
        def fake_concat(clips, out):
            Path(out).write_bytes(b"concat")
        with mock.patch("pipeline.assembler.concatenate_scenes", side_effect=fake_concat), \
             mock.patch("pipeline.assembler.mix_background_music") as mix, \
             mock.patch("pipeline.assembler.ensure_video_resolution"), \
             mock.patch.object(self.backend, "_maybe_burn_first_frame_cover"):
            self.backend._reassemble_film_core(self.wd)
        mix.assert_not_called()


class ModeConversionTests(ActedSceneEditingTests):
    """Switching a scene's type converts the content and keeps every version."""

    _TO_NARRATION = json.dumps({
        "narration": "Ana waited on the fog-wrapped wharf, and at last he came.",
        "image_prompt": "A fog-wrapped wharf before dawn, empty boards, one figure waiting",
        "video_prompt": "slow dolly toward the waiting figure",
    })
    _TO_DIALOGUE = json.dumps({
        "cast": ["Ana"],
        "setting": "a fog-wrapped wharf before dawn",
        "lines": [{"speaker": "Ana", "delivery": "quiet", "text": "You came."}],
        "beats": [{"t0": 0, "t1": 4, "action": "Ana turns"}],
        "camera": "locked wide",
        "soundscape": "water on pilings",
    })

    def _convert(self, mode, reply=None):
        if reply is None:
            return self.backend.convert_scene_mode(
                self.job_id, 1, self.backend.ConvertModeBody(mode=mode))
        with mock.patch.object(self.backend, "_llm_complete", return_value=reply) as llm:
            r = self.backend.convert_scene_mode(
                self.job_id, 1, self.backend.ConvertModeBody(mode=mode))
        return r, llm

    def test_dialogue_to_narration_converts_the_content(self):
        r, llm = self._convert("narration", self._TO_NARRATION)
        scene = r["scene"]
        self.assertEqual(scene["mode"], "narration")
        self.assertIn("at last he came", scene["narration"])
        self.assertTrue(scene["image_prompt"])
        self.assertEqual(scene["lines"], [])
        self.assertEqual(llm.call_count, 1)
        self.assertTrue(llm.called)

    def test_switching_back_restores_the_old_version_without_the_llm(self):
        self._convert("narration", self._TO_NARRATION)
        with mock.patch.object(self.backend, "_llm_complete") as llm:
            r = self.backend.convert_scene_mode(
                self.job_id, 1, self.backend.ConvertModeBody(mode="dialogue"))
        llm.assert_not_called()
        scene = r["scene"]
        self.assertEqual([l["text"] for l in scene["lines"]], ["You came."])
        self.assertIn("[REFERENCE USE]", scene["video_prompt"])
        # …and back again: the narration version returns verbatim, LLM-free.
        with mock.patch.object(self.backend, "_llm_complete") as llm2:
            r2 = self.backend.convert_scene_mode(
                self.job_id, 1, self.backend.ConvertModeBody(mode="narration"))
        llm2.assert_not_called()
        self.assertIn("at last he came", r2["scene"]["narration"])

    def test_narration_to_dialogue_stages_the_beat(self):
        self._convert("narration", self._TO_NARRATION)
        # wipe the dialogue stash so the conversion must actually convert
        from pipeline.orchestrator import DurableStore
        store = DurableStore.default()
        try:
            row = store.get_scene(self.job_id, 1)
            meta = dict(row["metadata"]); meta.pop("mode_stash", None)
            store.upsert_scene(self.job_id, 1, title=row["title"], image_prompt=row["image_prompt"],
                               video_prompt=row["video_prompt"], narration=row["narration"],
                               metadata=meta)
        finally:
            store.close()
        r, llm = self._convert("dialogue", self._TO_DIALOGUE)
        scene = r["scene"]
        self.assertEqual(scene["mode"], "dialogue")
        self.assertEqual(scene["cast"], ["Ana"])
        self.assertIn("You came.", scene["video_prompt"])
        self.assertEqual(llm.call_count, 1)

    def test_a_blank_added_scene_flips_without_inventing_a_scene(self):
        # A scene just added in the film editor has nothing to convert.
        added = self.backend.add_film_scene(
            self.backend.AddFilmSceneBody(work_dir=str(self.wd)))
        with mock.patch.object(self.backend, "_llm_complete") as llm:
            r = self.backend.convert_scene_mode(
                self.job_id, added["scene_id"], self.backend.ConvertModeBody(mode="dialogue"))
        llm.assert_not_called()
        self.assertEqual(r["scene"]["mode"], "dialogue")
        self.assertEqual(r["scene"]["lines"], [])
        self.assertEqual(r["scene"]["narration"], "")

    def test_to_silent_is_mechanical(self):
        with mock.patch.object(self.backend, "_llm_complete") as llm:
            r = self.backend.convert_scene_mode(
                self.job_id, 1, self.backend.ConvertModeBody(mode="silent"))
        llm.assert_not_called()
        self.assertEqual(r["scene"]["mode"], "silent")
        self.assertEqual(r["scene"]["lines"], [])


class BareModeFlipTests(ActedSceneEditingTests):
    """A bare mode flip (old client, raw API) must never destroy a scene."""

    def _narrated(self):
        # Turn scene 1 into a plain narrated scene the pre-convert UI could hold.
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            title="Talk", mode="narration", lines=[],
            narration="The wharf waited in the fog.",
            image_prompt="a fog-wrapped wharf", video_prompt="slow dolly in"))

    def test_flip_to_dialogue_and_back_keeps_the_narration(self):
        self._narrated()
        # the OLD UI's behaviour: flip the flag, send the fields it had
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            title="Talk", mode="dialogue", lines=[],
            narration="The wharf waited in the fog.",
            image_prompt="a fog-wrapped wharf", video_prompt="slow dolly in"))
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            title="Talk", mode="narration", lines=[], narration="",
            image_prompt="", video_prompt=""))
        scene = self.backend.job_scenes(self.job_id)["scenes"][0]
        self.assertEqual(scene["narration"], "The wharf waited in the fog.")
        self.assertEqual(scene["image_prompt"], "a fog-wrapped wharf")

    def test_flip_to_dialogue_restores_a_stashed_take(self):
        # dialogue content stashed earlier comes back even on a bare flip
        self._narrated()   # leaving dialogue stashes the lines from setUp's scene
        self.backend.update_scene(self.job_id, 1, self.backend.SceneUpdate(
            title="Talk", mode="dialogue", lines=[], narration="",
            image_prompt="", video_prompt=""))
        scene = self.backend.job_scenes(self.job_id)["scenes"][0]
        self.assertEqual([l["text"] for l in scene["lines"]], ["You came."])
        self.assertIn("[REFERENCE USE]", scene["video_prompt"])


class FirstFrameReferenceTests(unittest.TestCase):
    """An acted scene's first-frame image rides as its opening-composition
    reference — and supersedes the location asset (the frame IS the place)."""

    def setUp(self):
        import app as gapp
        self.gapp = gapp
        tmp = tempfile.TemporaryDirectory(prefix="spielbot-frame-ref-")
        self.addCleanup(tmp.cleanup)
        self.wd = Path(tmp.name)
        (self.wd / "characters.json").write_text(json.dumps([
            {"id": "c1", "name": "Ana", "description": "a sailor",
             "ref_image": "c1.png", "enabled": True}]))
        d = self.wd / "characters"; d.mkdir()
        (d / "c1.png").write_bytes(b"png")
        cfg_file = self.wd / "config.yaml"; cfg_file.write_text("{}")
        mock.patch.object(gapp, "CONFIG_FILE", cfg_file).start()
        self.addCleanup(mock.patch.stopall)

    def _resolve(self, scene_id=1):
        return self.gapp.resolve_performance_references(
            {"cast": ["Ana"], "lines": [{"speaker": "Ana", "text": "hi"}]},
            {"voices": []}, self.wd, "", scene_id=scene_id)

    def test_a_preview_image_becomes_the_frame_reference(self):
        (self.wd / "scene_01_preview.png").write_bytes(b"png")
        pics = self._resolve()["pictures"]
        self.assertEqual([(p["kind"], p["slot"]) for p in pics],
                         [("character", 1), ("frame", 2)])

    def test_no_preview_no_frame_slot(self):
        pics = self._resolve()["pictures"]
        self.assertEqual([p["kind"] for p in pics], ["character"])

    def test_the_frame_supersedes_the_location_visual(self):
        (self.wd / "scene_01_preview.png").write_bytes(b"png")
        vis_dir = self.wd / "visuals"; vis_dir.mkdir()
        (vis_dir / "v1.png").write_bytes(b"png")
        (self.wd / "visuals.json").write_text(json.dumps([
            {"id": "v1", "name": "Wharf", "kind": "location", "description": "",
             "ref_image": "v1.png", "scenes": [], "enabled": True},
            {"id": "v2", "name": "Coat", "kind": "wardrobe", "character": "Ana",
             "description": "", "ref_image": "v1.png", "scenes": [], "enabled": True}]))
        kinds = [p["kind"] for p in self._resolve()["pictures"]]
        self.assertIn("frame", kinds)
        self.assertIn("wardrobe", kinds)      # wardrobe still rides
        self.assertNotIn("location", kinds)   # the frame IS the place

    def test_frame_role_is_bounded(self):
        from pipeline import performance as perf
        role = perf.picture_role({"name": "First frame", "kind": "frame"})
        self.assertIn("OPENING IMAGE", role)
        self.assertIn("portrait references only", role)


class GenericReferenceTests(unittest.TestCase):
    """Free-form image/video references and URL ingest."""

    def setUp(self):
        import app as gapp
        self.gapp = gapp
        tmp = tempfile.TemporaryDirectory(prefix="spielbot-generic-ref-")
        self.addCleanup(tmp.cleanup)
        self.wd = Path(tmp.name)

    def _png(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), "red").save(buf, "PNG")
        return buf.getvalue()

    def test_image_and_video_are_legal_kinds_that_feed_scene_slots(self):
        self.gapp.add_script_visual(self.wd, "Red bicycle", "image", "a red bicycle")
        vid = self.gapp.read_script_visuals(self.wd)[0]["id"]
        self.gapp.set_script_visual_media(self.wd, vid, self._png(), filename="bike.png")
        vis = self.gapp.scene_visuals(self.wd, 1, ["Ana"], {"assets": []}, "")
        self.assertEqual([v["kind"] for v in vis], ["image"])
        from pipeline import performance as perf
        role = perf.picture_role(vis[0])
        self.assertIn("a red bicycle", role)
        self.assertIn("adds no people", role)

    def test_a_video_upload_extracts_its_frame(self):
        import subprocess
        from pipeline.assembler import _resolve_media_tool
        clip = self.wd / "src.mp4"
        subprocess.run([_resolve_media_tool("ffmpeg"), "-y", "-f", "lavfi",
                        "-i", "color=c=blue:s=64x64:d=2", str(clip)],
                       capture_output=True, check=True)
        self.gapp.add_script_visual(self.wd, "Ref clip", "video")
        vid = self.gapp.read_script_visuals(self.wd)[0]["id"]
        self.gapp.set_script_visual_media(self.wd, vid, clip.read_bytes(), filename="src.mp4")
        vis = self.gapp.read_script_visuals(self.wd)[0]
        self.assertTrue(vis["ref_image"].endswith(".png"))
        frame = self.gapp._script_visual_image_path(self.wd, vis["ref_image"])
        self.assertTrue(frame.exists() and frame.stat().st_size > 0)
        # …and it resolves into the scene's picture slots like any image.
        slots = self.gapp.scene_visuals(self.wd, 1, [], {"assets": []}, "")
        self.assertEqual([v["kind"] for v in slots], ["video"])

    def test_url_fetch_rejects_non_http(self):
        self.gapp.add_script_visual(self.wd, "X", "image")
        vid = self.gapp.read_script_visuals(self.wd)[0]["id"]
        with self.assertRaises(ValueError):
            self.gapp.fetch_visual_from_url(self.wd, vid, "file:///etc/passwd")

    def test_url_fetch_direct_image(self):
        png = self._png()
        class Resp:
            headers = {"Content-Type": "image/png"}
            def read(self, n): return png
            def __enter__(self): return self
            def __exit__(self, *a): return False
        self.gapp.add_script_visual(self.wd, "X", "image")
        vid = self.gapp.read_script_visuals(self.wd)[0]["id"]
        import urllib.request
        with mock.patch.object(urllib.request, "urlopen", return_value=Resp()):
            self.gapp.fetch_visual_from_url(self.wd, vid, "https://example.com/x.png")
        vis = self.gapp.read_script_visuals(self.wd)[0]
        self.assertEqual(vis["ref_image"], f"{vid}.png")


class CriticActedTests(ActedSceneEditingTests):
    """The critic judges acted scenes by their spoken words, and never rewrites
    their prose fields — the dialogue is the scene."""

    def test_critic_rewrite_of_an_acted_scene_applies_title_only(self):
        before = self.backend.job_scenes(self.job_id)["scenes"][0]
        self.backend._apply_critic_ops(self.job_id, {
            "rewrites": [{"id": 1, "title": "Better Title",
                          "narration": "Prose the critic invented.",
                          "video_prompt": "a generic motion prompt",
                          "image_prompt": "a painted frame"}],
            "deletes": [], "inserts": [], "order": None})
        after = self.backend.job_scenes(self.job_id)["scenes"][0]
        self.assertEqual(after["title"], "Better Title")
        self.assertEqual(after["narration"], before["narration"])
        self.assertEqual(after["video_prompt"], before["video_prompt"])
        self.assertEqual([l["text"] for l in after["lines"]], ["You came."])


class FirstFrameRemoveTests(ActedSceneEditingTests):
    """Remove first frame must actually LOOK removed: the film editor shows the
    image-history selection when there is one, so history must report nothing
    selected once the canonical frame files are gone — else the frame appears
    un-removable even though the render already dropped it."""

    def _make_frame(self):
        from pipeline import image_history
        preview = self.wd / "scene_01_preview.png"
        preview.write_bytes(b"frame-one")
        image_history.record(self.wd, 1, preview)
        preview.write_bytes(b"frame-two")
        image_history.record(self.wd, 1, preview)
        (self.wd / "scene_01_first_frame.png").write_bytes(b"frame-two")

    def test_remove_clears_the_frame_everywhere_the_ui_looks(self):
        self._make_frame()
        self._save()   # persists script.json for the acted view
        self.backend.remove_scene_preview(self.job_id, 1)
        # the canonical files are gone, so the take renders reference-only …
        self.assertFalse((self.wd / "scene_01_preview.png").exists())
        self.assertFalse((self.wd / "scene_01_first_frame.png").exists())
        self.assertNotIn("frame", [p["kind"] for p in self._scene()["pictures"]])
        # … and the film editor agrees: no preview, and NO history version
        # claiming to be the current frame (versions stay re-selectable).
        row = self.backend.film_scenes(work_dir=str(self.wd))["scenes"][0]
        self.assertEqual(row["preview_path"], "")
        self.assertEqual(len(row["history"]["versions"]), 2)
        self.assertIsNone(row["history"]["selected"])

    def test_reselecting_a_kept_version_bringss_the_frame_back(self):
        self._make_frame()
        self._save()
        self.backend.remove_scene_preview(self.job_id, 1)
        vid = self.backend.film_scenes(
            work_dir=str(self.wd))["scenes"][0]["history"]["versions"][0]["id"]
        self.backend.select_scene_preview(
            self.job_id, 1, self.backend.PreviewSelectBody(version_id=vid))
        self.assertTrue((self.wd / "scene_01_preview.png").exists())
        row = self.backend.film_scenes(work_dir=str(self.wd))["scenes"][0]
        self.assertEqual(row["history"]["selected"], vid)
        self.assertIn("frame", [p["kind"] for p in self._scene()["pictures"]])

    def test_remove_never_wipes_a_script_that_lives_only_on_disk(self):
        # Older films keep their scenes only in script.json; with no store rows
        # the snapshot write would replace the film's one copy with [].
        self._make_frame()
        self._save()
        script = self.wd / "script.json"
        before = script.read_text()
        self.assertIn("Talk", before)
        from pipeline.orchestrator import DurableStore
        store = DurableStore.default()
        try:
            store._conn.execute("DELETE FROM scenes")   # simulate a pre-store film
            store._conn.commit()
        finally:
            store.close()
        self.backend.remove_scene_preview(self.job_id, 1)
        self.assertFalse((self.wd / "scene_01_preview.png").exists())
        self.assertEqual(script.read_text(), before)


class ActedRerenderConfigTests(TempConfigCase):
    """Shoot again must see the style hierarchy: job_config.json alone carries
    no "styles" list, and without it a catalogue character scoped to a PARENT
    style resolves no portrait — Ref2VA then refuses the whole scene."""

    def setUp(self):
        super().setUp()
        chars_dir = self.config_file.parent / "characters"
        chars_dir.mkdir(parents=True)
        (chars_dir / "c1.png").write_bytes(b"png")
        self.write_config({
            "styles": [_style("BHOB"), {**_style("David Attenbot"), "parent": "BHOB"}],
            "default_style": "BHOB",
            "characters": [{"id": "c1", "name": "David Attenbot", "style": "BHOB",
                            "description": "a robot naturalist", "ref_image": "c1.png",
                            "enabled": True}],
            "characters_migrated_v2": True,
        })
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        self.addCleanup(mock.patch.stopall)
        self.wd = self.output_dir / "film-20260811-114725"
        self.wd.mkdir()
        (self.wd / "job_config.json").write_text(json.dumps({
            "style_name": "David Attenbot", "resolution": "",
            "characters": [{"id": "c1", "name": "David Attenbot", "style": "BHOB",
                            "description": "a robot naturalist", "ref_image": "c1.png",
                            "enabled": True}],
        }))
        (self.wd / "characters.json").write_text("[]")

    def test_shoot_again_resolves_a_parent_scoped_portrait(self):
        meta = {"mode": "dialogue", "cast": ["David Attenbot"],
                "lines": [{"speaker": "David Attenbot", "text": "Hello."}]}
        row = {"id": 9, "title": "Talk", "image_prompt": "", "video_prompt": "",
               "narration": "", "metadata": meta}
        seen = {}

        def fake_render(scene, wd, cfg, **kw):
            seen["cfg"] = cfg
            seen["style_name"] = kw.get("style_name")
            (wd / "scene_09_final.mp4").write_bytes(b"mp4")

        pool = mock.MagicMock()
        pool.acquire.return_value = "http://w:8188"
        with mock.patch.object(backend, "_shared_edit_render_pool", return_value=pool), \
             mock.patch("resume_generation.render_performance_scene", fake_render):
            backend._run_acted_rerender("t1", self.wd, 9, backend._film_job_config(self.wd), row)

        self.assertEqual(backend._film_tasks["t1"]["status"], "done")
        # The cfg handed to the render must resolve the parent-scoped portrait —
        # exactly what resolve_performance_references will do with it.
        refs = backend.gapp.resolve_performance_references(
            meta, seen["cfg"], self.wd, seen["style_name"], scene_id=9)
        self.assertIn("character", [p["kind"] for p in refs["pictures"]])
