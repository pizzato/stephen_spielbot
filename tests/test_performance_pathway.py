"""Performance films fork the pathway at script creation.

script_mode = "performance" must produce acted scenes and, from there, a render
that never touches the narrated machinery (first frames, TTS, music) — while a
narrated style keeps working exactly as before.
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
from test_styles import TempConfigCase, _style  # noqa: E402

_REPLY = json.dumps({
    "style": "handheld 16mm, humid greens",
    "characters": [{"name": "CHICO", "description": "moustache, white shirt",
                    "gender": "male", "age": "adult"}],
    "scenes": [
        {"title": "The clearing", "setting": "a burnt clearing", "seconds": 10,
         "cast": ["CHICO"], "camera": "locked off", "soundscape": "cicadas",
         "beats": [{"t0": 0, "t1": 10, "action": "CHICO stands his ground"}],
         "lines": [{"speaker": "CHICO", "delivery": "quiet", "text": "You can burn the trees."}]},
        {"title": "After", "setting": "the clearing at night", "seconds": 10,
         "cast": ["CHICO"], "camera": "slow push", "soundscape": "insects",
         "beats": [{"t0": 0, "t1": 10, "action": "CHICO walks away"}],
         "lines": [{"speaker": "CHICO", "delivery": "tired", "text": "Not tonight."}]},
    ],
})


class ScriptForkTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        self.write_config({
            "styles": [
                _style("Acted", n_scenes=4, voice="Narrator", visual_style="gritty"),
                _style("Narrated", n_scenes=4, voice="Narrator", visual_style="cinematic"),
            ],
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
        cfg = backend.gapp.load_config()
        for s in cfg["styles"]:
            if s["name"] == "Acted":
                s["script_mode"] = "performance"
        backend.gapp.save_config(cfg)
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        # Portraits are rendered in a background thread — never in a test.
        mock.patch.object(backend.gapp, "generate_all_script_portraits").start()
        mock.patch.object(backend.threading, "Thread").start()
        self.addCleanup(mock.patch.stopall)

    def _generate(self, style_name="Acted", **kwargs):
        body = backend.GenerateScriptBody(
            video_title="Chico Mendes", topic="the rubber tappers",
            style_name=style_name, n_scenes=2, **kwargs)
        with mock.patch.object(performance, "_chat_complete", return_value=_REPLY):
            return backend._do_script_generate(body)

    def test_performance_style_produces_acted_scenes(self):
        result = self._generate()
        scenes = json.loads((Path(result["work_dir"]) / "script.json").read_text())
        self.assertEqual(len(scenes), 2)
        for s in scenes:
            self.assertEqual(s["metadata"]["mode"], "performance")
            # The prompt the video model receives, editable in the script editor.
            self.assertIn("<Picture 1> is CHICO", s["video_prompt"])
            self.assertIn("Do not add subtitles", s["video_prompt"])
            # No image engine runs, so no image prompt is written.
            self.assertEqual(s["image_prompt"], "")

    def test_brief_records_the_mode(self):
        result = self._generate()
        brief = json.loads((Path(result["work_dir"]) / "create_brief.json").read_text())
        self.assertEqual(brief["script_mode"], "performance")

    def test_cast_is_persisted_with_a_voice(self):
        # Voices are cast at script creation and become the <Audio N> references.
        result = self._generate()
        chars = json.loads((Path(result["work_dir"]) / "characters.json").read_text())
        self.assertEqual([c["name"] for c in chars], ["CHICO"])
        self.assertTrue(chars[0].get("voice"))

    def test_body_override_forces_performance_on_a_narrated_style(self):
        result = self._generate(style_name="Narrated", script_mode="performance")
        scenes = json.loads((Path(result["work_dir"]) / "script.json").read_text())
        self.assertEqual(scenes[0]["metadata"]["mode"], "performance")

    def test_narrated_style_never_reaches_the_performance_generator(self):
        called = []
        with mock.patch.object(performance, "generate_performance_script",
                               side_effect=lambda *a, **k: called.append(1)), \
             mock.patch.object(backend, "generate_script",
                               return_value=([], "music", "style", [])) as classic:
            body = backend.GenerateScriptBody(video_title="X", topic="y",
                                              style_name="Narrated", n_scenes=2)
            try:
                backend._do_script_generate(body)
            except Exception:
                pass  # empty scene list — we only care which generator ran
        self.assertEqual(called, [])
        self.assertTrue(classic.called)

    def test_scene_count_from_minutes_uses_clip_length_not_word_budget(self):
        ss = backend.gapp.style_settings(backend.gapp.load_config(), "Acted")
        body = backend.GenerateScriptBody(video_title="X", topic="y", minutes=1.0)
        # 60 s of film at ~10 s per acted clip.
        self.assertEqual(backend._performance_scene_count(body, ss), 6)
        # An explicit count still wins.
        body_n = backend.GenerateScriptBody(video_title="X", topic="y", minutes=1.0, n_scenes=3)
        self.assertEqual(backend._performance_scene_count(body_n, ss), 3)


class ModeResolutionTests(unittest.TestCase):
    def test_effective_mode(self):
        body = backend.GenerateScriptBody()
        self.assertEqual(backend._effective_script_mode(body, {"script_mode": "performance"}),
                         "performance")
        self.assertEqual(backend._effective_script_mode(body, {"script_mode": "nonsense"}),
                         "classic")
        # Performance ignores the dialogue/mixed format switch (it IS dialogue).
        dlg = backend.GenerateScriptBody(format="dialogue")
        self.assertEqual(backend._effective_script_mode(dlg, {"script_mode": "performance"}),
                         "performance")
        # Story still falls back to classic for those formats.
        self.assertEqual(backend._effective_script_mode(dlg, {"script_mode": "story"}),
                         "classic")



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
        import resume_generation as rg
        from pipeline.llm import Scene
        captured = {}

        def fake_gen(engine, prompt, ref_images, out, ref_audios=None, **kw):
            captured.update(engine=engine, prompt=prompt, ref_images=list(ref_images),
                            ref_audios=list(ref_audios or []), kwargs=kw)
            Path(out).write_bytes(b"mp4")
            return Path(out)

        scene = Scene(id=1, title="S", image_prompt="", video_prompt="stale",
                      narration="", mode="performance",
                      lines=meta.get("lines", []),
                      metadata_extra={k: v for k, v in meta.items() if k != "lines"})
        cfg = {**backend.gapp.load_config(), "style_name": "Acted"}
        with mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
             mock.patch.object(rg, "ensure_video_resolution"):
            rg.render_performance_scene(scene, self.work_dir, cfg,
                                        comfy_url="http://w:8188",
                                        vid_width=704, vid_height=1280, **kwargs)
        return captured

    def _meta(self, **over):
        meta = {"mode": "performance", "cast": ["CHICO", "MARIA"], "seconds": 10,
                "setting": "a clearing", "camera": "locked off", "soundscape": "cicadas",
                "beats": [{"t0": 0, "t1": 10, "action": "they face each other"}],
                "lines": [{"speaker": "MARIA", "delivery": "flat", "text": "Go."},
                          {"speaker": "CHICO", "delivery": "quiet", "text": "No."}]}
        meta.update(over)
        return meta

    def test_pictures_follow_cast_order_and_audio_follows_speaking_order(self):
        cap = self._render(self._meta())
        self.assertEqual([p.name for p in cap["ref_images"]], ["char_a.png", "char_b.png"])
        self.assertIn("<Picture 1> is CHICO", cap["prompt"])
        self.assertIn("<Picture 2> is MARIA", cap["prompt"])
        # MARIA speaks first, so she is <Audio 1> and HER voice leads the slots.
        self.assertEqual([p.name for p in cap["ref_audios"]], ["kara.wav", "walter.wav"])
        self.assertIn("<Audio 1> is MARIA's voice", cap["prompt"])
        self.assertIn("<Audio 2> is CHICO's voice", cap["prompt"])

    def test_audio_slots_match_the_wired_voices(self):
        # The Nth <Audio> tag must be the Nth wired clip, or H3 casts the wrong
        # voice onto the wrong character.
        cap = self._render(self._meta())
        order = [cap["prompt"].index(f"<Audio {i + 1}>") for i in range(2)]
        self.assertEqual(order, sorted(order))
        voices = {"MARIA": "kara.wav", "CHICO": "walter.wav"}
        names = [line.split("is ")[1].split("'s")[0]
                 for line in cap["prompt"].splitlines()[0].split(". ")
                 if line.startswith("<Audio ")]
        self.assertEqual([voices[n] for n in names],
                         [p.name for p in cap["ref_audios"]])

    def test_unknown_character_is_dropped_not_renumbered_wrong(self):
        cap = self._render(self._meta(cast=["CHICO", "GHOST", "MARIA"]))
        self.assertEqual([p.name for p in cap["ref_images"]], ["char_a.png", "char_b.png"])
        self.assertIn("<Picture 2> is MARIA", cap["prompt"])
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
        self.assertEqual(cap["kwargs"]["duration_seconds"], 10.0)


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
                        narration="", mode="performance",
                        metadata_extra={"mode": "performance", "cast": ["X"], "seconds": 10})
                  for i in range(1, n_scenes + 1)]
        store = mock.MagicMock()
        pool = WorkerPool(["http://a:8188", "http://b:8188"])
        with mock.patch.object(rg, "render_performance_scene", side_effect=fake_render), \
             mock.patch.object(rg, "TaskRun"), \
             mock.patch.object(rg, "_get_duration", return_value=10.0), \
             mock.patch.object(rg, "concatenate_scenes",
                               side_effect=_write_concat) as concat, \
             mock.patch.object(rg, "ensure_video_resolution"), \
             mock.patch.object(rg, "write_progress"), \
             mock.patch.object(rg, "generate_cover_image"), \
             mock.patch.object(rg.time, "sleep"):
            rg._run_performance_film(
                work_dir, scenes, {"reference_engine": "minimax-h3-ref-turbo"},
                store=store, durable_job_id="job", worker_pool=pool,
                status_file=work_dir / "progress.json",
                vid_width=704, vid_height=1280,
                final_path=work_dir / "final.mp4", cover_path=work_dir / "cover.png")
        return attempts, concat

    def test_scene_retries_on_a_worker_that_has_the_model(self):
        attempts, concat = self._film(fail_urls={"http://a:8188"})
        # Every scene still landed, and the bad worker was dropped rather than
        # retried three times.
        self.assertEqual(sorted(a[0] for a in attempts if a[1] == "http://b:8188"), [1, 2])
        self.assertEqual(len([a for a in attempts if a[1] == "http://a:8188"]), 1)
        clips = concat.call_args[0][0]
        self.assertEqual(len(clips), 2)

    def test_scene_order_is_preserved_after_a_failover(self):
        _, concat = self._film(fail_urls={"http://a:8188"}, n_scenes=3)
        names = [Path(p).name for p in concat.call_args[0][0]]
        self.assertEqual(names, ["scene_01_final.mp4", "scene_02_final.mp4",
                                 "scene_03_final.mp4"])

    def test_all_workers_missing_the_model_is_a_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._film(fail_urls={"http://a:8188", "http://b:8188"})
        # Whichever thread loses the race reports it, but the film must stop
        # with a worker-level message rather than a bare ComfyUI validation dump.
        self.assertRegex(str(ctx.exception), r"(?i)workers? (failed|remaining)")

if __name__ == "__main__":
    unittest.main()


class AssemblyArtifactTests(unittest.TestCase):
    """Assembly must leave the artifacts the rest of the app keys off."""

    def test_combined_mp4_is_written_next_to_the_final(self):
        # The backend reads combined.mp4's existence as "this film finished"
        # (Activity %, film editor, re-render, publish). Concatenating straight
        # to the final left performance films pinned at 99% forever.
        import resume_generation as rg
        from pipeline.llm import Scene

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "film"
            work_dir.mkdir()
            final_path = Path(tmp) / "film.mp4"
            scenes = [Scene(id=1, title="S", image_prompt="", video_prompt="p",
                            narration="", mode="performance",
                            metadata_extra={"mode": "performance", "cast": ["X"],
                                            "seconds": 10})]

            def fake_render(scene, wd, cfg, **kw):
                out = wd / f"scene_{scene.id:02d}_final.mp4"
                out.write_bytes(b"mp4")
                return out

            def fake_concat(clips, out):
                Path(out).write_bytes(b"concatenated")
                return Path(out)

            pool = mock.MagicMock()
            pool.urls = ["http://a:8188"]
            pool.acquire.return_value = "http://a:8188"
            pool.has_healthy.return_value = True
            with mock.patch.object(rg, "render_performance_scene", side_effect=fake_render), \
                 mock.patch.object(rg, "TaskRun"), \
                 mock.patch.object(rg, "_get_duration", return_value=10.0), \
                 mock.patch.object(rg, "concatenate_scenes", side_effect=fake_concat), \
                 mock.patch.object(rg, "ensure_video_resolution"), \
                 mock.patch.object(rg, "write_progress"), \
                 mock.patch.object(rg, "generate_cover_image"):
                rg._run_performance_film(
                    work_dir, scenes, {"reference_engine": "minimax-h3-ref-turbo"},
                    store=mock.MagicMock(), durable_job_id="job", worker_pool=pool,
                    status_file=work_dir / "progress.json",
                    vid_width=704, vid_height=1280,
                    final_path=final_path, cover_path=work_dir / "cover.png")

            self.assertTrue((work_dir / "combined.mp4").exists(), "combined.mp4 missing")
            self.assertTrue(final_path.exists(), "final video missing")
            self.assertEqual(final_path.read_bytes(), (work_dir / "combined.mp4").read_bytes())


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
        self.assertIn("<Audio 1> is VOICED's voice", prompt)
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
        self.assertIn("<Picture 1> is JOE (host).", prompt)
        self.assertIn("<Picture 2> is The studio — the place this scene happens in", prompt)
        self.assertIn("<Picture 3> is Blue henley — what JOE is wearing", prompt)

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
