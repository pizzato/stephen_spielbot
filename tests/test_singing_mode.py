"""Song films (the "Music video" format): singing scenes + sung soundtrack.

The contract: a "song" film's silent scenes are stamped ``singing`` at divide
time and PERFORM the film's song on camera — acted takes whatever the style's
h3_silent_scenes says, prompted to visibly sing, shipped muted — while the real
vocals come from the music engine singing the film's tagged lyrics
(song.json), captioned to match the cast singer's library voice.
"""
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp.backend.main as backend  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pipeline import performance as perf  # noqa: E402
from pipeline import story  # noqa: E402
from pipeline.llm import Scene  # noqa: E402
from pipeline.orchestrator import DurableStore, _renders_acted  # noqa: E402
from test_styles import TempConfigCase, _style  # noqa: E402


def _singing(cast=("Ada",), duration=8.0):
    md = {"mode": "silent", "singing": True, "cast": list(cast),
          "setting": "a rain-washed rooftop at night"}
    return Scene(id=2, title="chorus", image_prompt="i", video_prompt="v",
                 narration="", mode="silent", duration=duration, metadata_extra=md)


def _silent(duration=8.0):
    return Scene(id=3, title="beat", image_prompt="i", video_prompt="v",
                 narration="", mode="silent", duration=duration,
                 metadata_extra={"mode": "silent"})


class RoutingTests(unittest.TestCase):
    def test_singing_scene_is_acted_whatever_the_style_says(self):
        scene = _singing()
        self.assertTrue(perf.renders_acted(scene, {"h3_silent_scenes": False}))
        self.assertTrue(perf.renders_acted(scene, {}))
        self.assertTrue(perf.renders_acted(scene, None))

    def test_plain_silent_scene_still_needs_the_toggle(self):
        self.assertFalse(perf.renders_acted(_silent(), {"h3_silent_scenes": False}))
        self.assertTrue(perf.renders_acted(_silent(), {"h3_silent_scenes": True}))

    def test_planner_and_renderer_agree_on_singing(self):
        # The orchestrator keeps its own dependency-free copy; a disagreement
        # plans a task nobody ever completes and the render hangs.
        for cfg in ({}, {"h3_silent_scenes": False}, {"h3_silent_scenes": True}):
            for scene in (_singing(), _silent()):
                self.assertEqual(
                    perf.renders_acted(scene, cfg),
                    _renders_acted(scene.metadata, cfg),
                    f"planner/renderer disagree for {scene.metadata} with {cfg}")

    def test_is_singing_needs_silent_mode(self):
        # The flag only means something on a silent scene — a dialogue scene
        # keeps its spoken contract even if metadata carries a stray key.
        talk = Scene(id=4, title="t", image_prompt="", video_prompt="", narration="",
                     mode="dialogue", lines=[{"speaker": "Ada", "text": "Hi."}],
                     metadata_extra={"mode": "dialogue", "singing": True})
        self.assertFalse(perf.is_singing(talk))
        self.assertTrue(perf.is_singing(_singing()))


class PromptTests(unittest.TestCase):
    def _prompt(self, **extra):
        meta = {"mode": "silent", "singing": True, "seconds": 8.0,
                "setting": "a rooftop at night", **extra}
        return perf.build_h3_prompt(meta, picture_names=["Ada"])

    def test_singing_prompt_asks_for_a_performance(self):
        prompt = self._prompt()
        self.assertIn("performing a song", prompt)
        self.assertIn("visibly singing", prompt)
        self.assertNotIn("mouth stays completely closed", prompt)

    def test_singing_prompt_allows_the_voice_but_not_instruments(self):
        prompt = self._prompt()
        self.assertIn("live singing voice", prompt)
        self.assertIn("no instruments", prompt)
        # The blanket refusal would fight the singing it just asked for.
        self.assertNotIn("no music of any kind", prompt)
        self.assertIn("no instrumental music", prompt)

    def test_plain_silent_prompt_is_unchanged(self):
        meta = {"mode": "silent", "seconds": 8.0, "setting": "a wharf"}
        prompt = perf.build_h3_prompt(meta, picture_names=["Ada"])
        self.assertIn("mouth stays completely closed", prompt)
        self.assertIn("No speech and no voices at all", prompt)
        self.assertIn("no music of any kind", prompt)

    def test_dialogue_prompt_is_unchanged(self):
        meta = {"mode": "dialogue", "seconds": 8.0,
                "lines": [{"speaker": "Ada", "text": "You came back."}]}
        prompt = perf.build_h3_prompt(meta, picture_names=["Ada"], audio_names=["Ada"])
        self.assertIn("says exactly", prompt)
        self.assertIn("no music of any kind", prompt)


class ProseBeatsTests(unittest.TestCase):
    def test_a_prose_beats_string_becomes_one_whole_take_beat(self):
        # The LLM sometimes writes "beats" as a sentence instead of a timed
        # array — it must survive as an action, not crash or vanish.
        beats = perf.norm_beats("Kinho rides easy down the hill", 10.0)
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0]["action"], "Kinho rides easy down the hill")
        self.assertEqual(perf.norm_beats("   ", 10.0), [])
        prompt = perf.build_h3_prompt(
            {"mode": "silent", "singing": True, "seconds": 8.0,
             "beats": "Thomas flies the skate ramp"}, picture_names=["Thomas"])
        self.assertIn("Thomas flies the skate ramp", prompt)


class MarkSingingTests(unittest.TestCase):
    def test_marks_only_silent_scenes(self):
        scenes = [
            Scene(id=1, title="n", image_prompt="i", video_prompt="v",
                  narration="Words."),
            _silent(),
            Scene(id=4, title="t", image_prompt="", video_prompt="", narration="",
                  mode="dialogue", lines=[{"speaker": "Ada", "text": "Hi."}]),
        ]
        story.mark_singing(scenes)
        self.assertNotIn("singing", scenes[0].metadata)
        self.assertTrue(scenes[1].metadata.get("singing"))
        self.assertNotIn("singing", scenes[2].metadata)

    def test_flag_survives_the_script_snapshot_roundtrip(self):
        scene = story.mark_singing([_silent()])[0]
        # script.json rows carry the metadata property — the render rebuilds
        # Scene objects from exactly this dict.
        row = json.loads(json.dumps(scene.metadata))
        self.assertTrue(perf.renders_acted({"metadata": row}, {}))


class LyricsTemplateTests(unittest.TestCase):
    def _filled(self, workflow_name, lyrics):
        from pipeline import comfyui
        text = (Path(__file__).resolve().parent.parent / "workflows"
                / workflow_name).read_text()
        return comfyui._fill_template(text, {
            "TAGS": "synthpop, 110 BPM", "DURATION": 120.0, "SEED": 7,
            "LYRICS": lyrics,
        })

    def test_lyrics_reach_both_music_graphs(self):
        lyrics = "[Verse]\nRain on the wire\n[Chorus]\nSing it \"louder\" now"
        wf = self._filled("minimax_music.json", lyrics)
        self.assertEqual(wf["5"]["inputs"]["lyrics"], lyrics)
        wf = self._filled("ace_music.json", lyrics)
        self.assertEqual(wf["6"]["inputs"]["lyrics"], lyrics)

    def test_empty_lyrics_keep_the_instrumental_contract(self):
        wf = self._filled("minimax_music.json", "")
        self.assertEqual(wf["5"]["inputs"]["lyrics"], "")


class VocalistNoteTests(unittest.TestCase):
    def test_note_describes_the_singers_cast_voice(self):
        import app
        cfg = {"voices": [{"name": "June", "path": "/tmp/june.wav",
                           "gender": "female", "age": "mature",
                           "accent": "Irish", "tone": "warm, smoky"}]}
        chars = [{"name": "Ada", "voice": "June"}]
        with unittest.mock.patch.object(app, "_job_characters", return_value=chars):
            note = app.vocalist_note(cfg, "style", Path("/tmp"), ["Ada"])
        self.assertIn("mature female vocalist", note)
        self.assertIn("warm, smoky voice", note)
        self.assertIn("Irish accent", note)

    def test_singer_order_prefers_the_scenes_cast(self):
        import app
        cfg = {"voices": [
            {"name": "June", "path": "/x", "gender": "female", "age": "mature"},
            {"name": "Tom", "path": "/y", "gender": "male", "age": "young"},
        ]}
        chars = [{"name": "Ada", "voice": "June"}, {"name": "Ben", "voice": "Tom"}]
        with unittest.mock.patch.object(app, "_job_characters", return_value=chars):
            note = app.vocalist_note(cfg, "style", Path("/tmp"), ["Ben"])
        self.assertIn("young male vocalist", note)

    def test_no_cast_voice_returns_empty(self):
        import app
        with unittest.mock.patch.object(app, "_job_characters", return_value=[]):
            self.assertEqual(app.vocalist_note({"voices": []}, "s", Path("/tmp")), "")


class AudioArtifactTests(unittest.TestCase):
    def test_audio_upload_becomes_a_scoped_soundtrack(self):
        import tempfile
        import app
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            visuals = app.add_script_visual(wd, name="Beat", kind="audio")
            vid = visuals[-1]["id"]
            app.set_script_visual_media(wd, vid, b"RIFFxxxxWAVE", filename="beat.wav")
            # No scene list = the whole film.
            track = app.scene_track_audio(wd, 1)
            self.assertIsNotNone(track)
            self.assertTrue(str(track).endswith(".wav"))
            # Scoped to scene 2 only.
            app.update_script_visual(wd, vid, scenes=[2])
            self.assertIsNone(app.scene_track_audio(wd, 1))
            self.assertIsNotNone(app.scene_track_audio(wd, 2))
            # Disabled = off everywhere; deletion removes the file.
            app.update_script_visual(wd, vid, enabled=False)
            self.assertIsNone(app.scene_track_audio(wd, 2))
            app.update_script_visual(wd, vid, enabled=True)
            app.delete_script_visual(wd, vid)
            self.assertIsNone(app.scene_track_audio(wd, 2))

    def test_audio_assets_never_join_the_picture_wall(self):
        import tempfile
        import app
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            visuals = app.add_script_visual(wd, name="Beat", kind="audio")
            app.set_script_visual_media(wd, visuals[-1]["id"], b"RIFFxxxxWAVE",
                                        filename="beat.wav")
            pics = app.scene_visuals(wd, 1, cast=[], cfg={"styles": [], "assets": []})
            self.assertEqual(pics, [])


class SvcTests(unittest.TestCase):
    def test_convert_song_builds_the_singing_command(self):
        import tempfile
        from pipeline import svc
        calls = {}

        def fake_run(cmd, **kw):
            calls["cmd"] = cmd
            out = Path(cmd[cmd.index("--output") + 1]) / "vc_out.wav"
            out.write_bytes(b"RIFF")

            class P:
                returncode, stderr, stdout = 0, "", ""
            return P()

        with unittest.mock.patch.object(svc, "available", return_value=True), \
             unittest.mock.patch.object(svc, "_normalize_loudness"), \
             unittest.mock.patch.object(svc, "_separate_stems", return_value=None), \
             unittest.mock.patch.object(svc.subprocess, "run", fake_run), \
             tempfile.TemporaryDirectory() as td:
            out = Path(td) / "converted.wav"
            svc.convert_song(Path("/x/src.wav"), Path("/x/ref.wav"), out)
            self.assertTrue(out.exists())
            # f0 conditioning is what makes it a SINGING conversion.
            self.assertIn("--f0-condition", calls["cmd"])
            self.assertIn(str(Path("/x/ref.wav")), calls["cmd"])

    def test_missing_install_is_a_clear_error(self):
        from pipeline import svc
        with unittest.mock.patch.object(svc, "available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "install_svc"):
                svc.convert_song(Path("/x"), Path("/y"), Path("/z"))


class FilmKindTests(unittest.TestCase):
    def test_a_music_video_is_never_called_a_documentary(self):
        from pipeline.llm import film_kind, _film_content_lines
        sung = [{"narration": "", "metadata": {"mode": "silent", "singing": True,
                                               "sings": "Wobbly knees\nThen she flies"}}]
        self.assertEqual(film_kind(sung), "music video")
        # The lyrics ARE the content — a song film used to produce the empty
        # string here and fall to "A documentary about …".
        self.assertIn("(sung) Wobbly knees / Then she flies",
                      _film_content_lines(sung))

    def test_other_kinds(self):
        from pipeline.llm import film_kind
        self.assertEqual(film_kind([{"narration": "Words.", "metadata": {}}]),
                         "documentary")
        self.assertEqual(film_kind([{"metadata": {"mode": "dialogue",
                                                  "lines": [{"speaker": "A", "text": "hi"}]}}]),
                         "acted short film")
        self.assertEqual(film_kind([{"metadata": {"mode": "silent"}}]),
                         "silent short film")
        self.assertEqual(film_kind([{"metadata": {"mode": "dialogue"}},
                                    {"narration": "x", "metadata": {}}]),
                         "short film")


class SongFormatNoteTests(unittest.TestCase):
    def test_staging_note_forbids_voices_and_asks_for_the_performer(self):
        from webapp.backend.main import _build_dialogue_note, _story_format_note
        note = _build_dialogue_note("song", ["Ada"])
        self.assertIn("MUSIC VIDEO", note)
        self.assertIn('NEVER use "narration"', note)
        self.assertIn("SINGING to camera", note)
        # The silent-performance schema must always be requested — a song
        # film's takes are performed whatever the style toggle says.
        self.assertIn("silent scenes are PERFORMED", note)
        draft = _story_format_note("song")
        self.assertIn("MUSIC VIDEO STORY", draft)
        self.assertIn("lead performer", draft)


class SongRewriteTests(TempConfigCase):
    """The Song tab's Re-generate: one half of the song is re-written by the
    LLM, the half the editor sent is kept — and both are saved."""

    def setUp(self):
        super().setUp()
        self.write_config({"styles": [_style("Hero")], "default_style": "Hero",
                           "characters": [], "characters_migrated_v2": True})
        p = unittest.mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir)
        p.start()
        self.addCleanup(p.stop)
        self.wd = self.output_dir / "song-film"
        self.wd.mkdir()
        (self.wd / "song.json").write_text(json.dumps(
            {"caption": "slow piano ballad, 70 BPM, wistful",
             "lyrics": "[Verse]\nold words", "seconds": 45,
             "title": "Rooftop", "style_name": "Hero"}))
        (self.wd / "create_brief.json").write_text(json.dumps(
            {"topic": "a rooftop goodbye", "video_title": "Rooftop"}))
        store = DurableStore.default()
        try:
            self.job_id = backend.job_id_from_work_dir(self.wd)
            store.create_or_update_job(self.job_id, self.wd, "Rooftop")
        finally:
            store.close()

    def _regen(self, **kw):
        return backend.regenerate_job_song(self.job_id, backend.SongRegenBody(**kw))

    def test_new_lyrics_are_written_to_the_sound_the_editor_kept(self):
        with unittest.mock.patch.object(
                backend.story_mode, "write_song",
                return_value={"caption": "a caption nobody asked for",
                              "lyrics": "[Chorus]\nnew words"}) as ws:
            res = self._regen(field="lyrics", caption="fast punk, 180 BPM",
                              lyrics="[Verse]\nunsaved edit", instruction="fewer words")
        self.assertEqual(res["lyrics"], "[Chorus]\nnew words")
        # The Sound box the editor showed survives — including unsaved edits.
        self.assertEqual(res["caption"], "fast punk, 180 BPM")
        self.assertEqual(ws.call_args.kwargs["music_hint"], "fast punk, 180 BPM")
        self.assertEqual(ws.call_args.kwargs["instruction"], "fewer words")
        # …written from the film's own brief, not from the song file's title.
        self.assertIn("a rooftop goodbye", ws.call_args.kwargs["topic"])
        saved = json.loads((self.wd / "song.json").read_text())
        self.assertEqual(saved["lyrics"], "[Chorus]\nnew words")
        self.assertEqual(saved["caption"], "fast punk, 180 BPM")

    def test_new_sound_describes_the_lyrics_the_editor_kept(self):
        with unittest.mock.patch.object(
                backend, "_llm_complete", return_value="driving synthwave, 110 BPM, hopeful") as llm:
            res = self._regen(field="caption", caption="slow piano ballad",
                              lyrics="[Verse]\nunsaved edit", instruction="bigger")
        self.assertEqual(res["caption"], "driving synthwave, 110 BPM, hopeful")
        self.assertEqual(res["lyrics"], "[Verse]\nunsaved edit")
        user = llm.call_args.args[1]
        self.assertIn("unsaved edit", user)      # written FOR these lyrics
        self.assertIn("bigger", user)            # "tell it how" steering
        self.assertEqual(json.loads((self.wd / "song.json").read_text())["caption"],
                         "driving synthwave, 110 BPM, hopeful")

    def test_only_the_two_halves_can_be_re_written(self):
        with self.assertRaises(HTTPException) as ctx:
            self._regen(field="voice")
        self.assertEqual(ctx.exception.status_code, 400)


class SongInstructionTests(unittest.TestCase):
    def test_instruction_rides_the_song_prompt(self):
        cfg = {}
        calls = []

        def fake_call(system, user, max_tokens, label, retries=3):
            calls.append(user)
            return json.dumps({"caption": "c", "lyrics": "[Verse]\nl"})

        with unittest.mock.patch.object(story, "_load_cfg", return_value=cfg), \
             unittest.mock.patch.object(story, "_call_fn", return_value=fake_call):
            story.write_song(None, 45, topic="t")
            story.write_song(None, 45, topic="t", instruction="simpler words")
        self.assertNotIn("Additional instruction", calls[0])
        self.assertIn("simpler words", calls[1])


if __name__ == "__main__":
    unittest.main()
