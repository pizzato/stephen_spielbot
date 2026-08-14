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

from pipeline import performance as perf  # noqa: E402
from pipeline import story  # noqa: E402
from pipeline.llm import Scene  # noqa: E402
from pipeline.orchestrator import _renders_acted  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
