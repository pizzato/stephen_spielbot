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


if __name__ == "__main__":
    unittest.main()


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
