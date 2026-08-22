"""Song films (the "Music video" format): singing scenes + sung soundtrack.

The contract: a "song" film's silent scenes are stamped ``singing`` at divide
time and PERFORM the film's song on camera — acted takes whatever the style's
h3_silent_scenes says, prompted to visibly sing, shipped carrying their own
slice of the track — while the real vocals come from the music engine singing
the film's tagged lyrics (song.json), captioned to match the cast singer's
library voice.
"""
import base64
import json
import math
import sys
import tempfile
import unittest
import unittest.mock
import wave
from array import array
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

    def test_a_scene_told_not_to_sing_listens_instead(self):
        # performs=False is the authored "they don't sing in this shot" — the
        # song still plays, but the prompt must stop ordering a performance.
        prompt = self._prompt(performs=False, sings="Neon hearts keep burning")
        self.assertIn("NOT singing", prompt)
        self.assertIn("No miming", prompt)
        self.assertNotIn("visibly singing", prompt)
        self.assertNotIn("performing a song", prompt)
        # No lyrics to mime, and no request for a singing voice.
        self.assertNotIn("Neon hearts", prompt)
        self.assertNotIn("live singing voice", prompt)

    def test_performing_stays_the_default(self):
        for extra in ({}, {"performs": True}):
            prompt = self._prompt(**extra)
            self.assertIn("visibly singing", prompt)
            self.assertNotIn("NOT singing", prompt)

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

    def test_any_worker_can_take_it_least_busy_first(self):
        from pipeline import svc
        cfg = {"comfy_workers": ["http://s1:8188", "http://s2:8188",
                                 "http://s3:8188"]}
        with unittest.mock.patch("pipeline.worker_pool.idle_workers",
                                 return_value=["http://s3:8188", "http://s1:8188"]):
            self.assertEqual(svc.candidate_workers(cfg),
                             ["http://s3:8188", "http://s1:8188"])

    def test_a_pinned_worker_wins_and_a_dead_fleet_still_lists_hosts(self):
        from pipeline import svc
        cfg = {"comfy_workers": ["http://s1:8188", "http://s2:8188"]}
        # A pinned host resolves to its comfy URL so the worker lease keys on it.
        self.assertEqual(svc.candidate_workers({**cfg, "svc_worker": "s2"}),
                         ["http://s2:8188"])
        # Pinned host outside the fleet: no URL to lease on — bare host.
        self.assertEqual(svc.candidate_workers({**cfg, "svc_worker": "s9"}), ["s9"])
        # Nothing reachable: idle_workers raises, the configured order stands.
        with unittest.mock.patch("pipeline.worker_pool.idle_workers",
                                 side_effect=RuntimeError("no workers")):
            self.assertEqual(svc.candidate_workers(cfg),
                             ["http://s1:8188", "http://s2:8188"])
        # A host ssh would read as an option is not a host.
        self.assertEqual(svc.candidate_workers({"svc_worker": "-oProxyCommand=x"}), [])

    def test_a_failed_worker_falls_through_to_the_next_then_the_controller(self):
        from pipeline import svc
        tried = []

        def fake_remote(host, *a, **kw):
            tried.append(host)
            raise RuntimeError("boom")

        with unittest.mock.patch.object(svc, "_convert_remote", fake_remote), \
             unittest.mock.patch.object(svc, "_convert") as local:
            svc._convert_anywhere(Path("/x/src.wav"), Path("/x/ref.wav"),
                                  Path("/x/out.wav"), 30, 60, ["s1", "s2"])

        self.assertEqual(tried, ["s1", "s2"])
        self.assertTrue(local.called)

    def test_the_worker_runs_the_diffusion_in_its_comfyui_container(self):
        from pipeline import svc
        cmds = []

        def fake_run(cmd, **kw):
            cmds.append(cmd)

            class P:
                returncode, stderr, stdout = 0, "", ""
            return P()

        with unittest.mock.patch.object(svc.subprocess, "run", fake_run):
            svc._convert_remote("s2", Path("/x/src.wav"), Path("/x/ref.wav"),
                                Path("/tmp/out.wav"), 30, 60)

        joined = " ".join(" ".join(c) for c in cmds)
        self.assertIn("docker exec spielbot-worker-comfyui-1", joined)
        self.assertIn("/opt/seed-vc/.venv/bin/python", joined)
        self.assertIn("--f0-condition", joined)
        self.assertTrue(all(c[0] in ("ssh", "scp") for c in cmds), cmds)

    def test_a_single_machine_setup_skips_ssh(self):
        from pipeline import svc
        cmds = []

        def fake_run(cmd, **kw):
            cmds.append(cmd)

            class P:
                returncode, stderr, stdout = 0, "", ""
            return P()

        with unittest.mock.patch.object(svc.subprocess, "run", fake_run):
            svc._convert_remote("localhost", Path("/x/src.wav"), Path("/x/ref.wav"),
                                Path("/tmp/out.wav"), 30, 60)

        self.assertTrue(cmds)
        self.assertFalse(any(c[0] in ("ssh", "scp") for c in cmds), cmds)
        self.assertIn("docker exec spielbot-worker-comfyui-1",
                      " ".join(" ".join(c) for c in cmds))


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

    def test_a_direction_sent_with_the_re_write_persists_and_steers_it(self):
        """A direction given in the studio must not be one-shot: it lands in
        the create brief BEFORE the re-write, so it steers this one, every
        later one, and the Brief button's restore."""
        with unittest.mock.patch.object(
                backend.story_mode, "write_song",
                return_value={"caption": "c", "lyrics": "[Chorus]\nnew"}) as ws:
            self._regen(field="lyrics", caption="fast punk", lyrics="x",
                        direction="a rooftop reunion — joyful, not sad")
        self.assertIn("a rooftop reunion", ws.call_args.kwargs["topic"])
        brief = json.loads((self.wd / "create_brief.json").read_text())
        self.assertEqual(brief["topic"], "a rooftop reunion — joyful, not sad")

    def test_a_re_write_without_a_direction_leaves_the_brief_alone(self):
        with unittest.mock.patch.object(
                backend.story_mode, "write_song",
                return_value={"caption": "c", "lyrics": "[Chorus]\nnew"}):
            self._regen(field="lyrics", caption="fast punk", lyrics="x")
        brief = json.loads((self.wd / "create_brief.json").read_text())
        self.assertEqual(brief["topic"], "a rooftop goodbye")

    def test_clearing_the_direction_falls_back_to_the_title(self):
        with unittest.mock.patch.object(
                backend.story_mode, "write_song",
                return_value={"caption": "c", "lyrics": "[Chorus]\nnew"}):
            self._regen(field="lyrics", caption="fast punk", lyrics="x",
                        direction="")
        brief = json.loads((self.wd / "create_brief.json").read_text())
        self.assertEqual(brief["topic"], "Rooftop")

    def test_direction_saved_with_song_edits_comes_back_from_the_studio(self):
        backend.update_job_song(self.job_id, backend.SongUpdateBody(
            caption="c", lyrics="[Verse]\nwords", direction="a rooftop wedding"))
        self.assertEqual(
            json.loads((self.wd / "create_brief.json").read_text())["topic"],
            "a rooftop wedding")
        self.assertEqual(backend.get_job_song(self.job_id)["direction"],
                         "a rooftop wedding")

    def test_a_title_only_brief_shows_a_blank_direction(self):
        # A film created with an empty Direction box stores the bare title as
        # its topic — the studio must show that as "no direction yet", not
        # parrot the title back as if it were one.
        (self.wd / "create_brief.json").write_text(json.dumps(
            {"topic": "Rooftop", "video_title": "Rooftop"}))
        self.assertEqual(backend.get_job_song(self.job_id)["direction"], "")


class _SongFilmCase(TempConfigCase):
    """A generated song film in a work dir, ready for the studio's operations."""

    def setUp(self):
        super().setUp()
        self.voice_ref = self.output_dir / "nora.wav"
        self.voice_ref.write_bytes(b"ref")
        self.write_config({"styles": [_style("Hero")], "default_style": "Hero",
                           "characters": [], "characters_migrated_v2": True,
                           "voices": [{"name": "Nora", "path": str(self.voice_ref)}]})
        p = unittest.mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir)
        p.start()
        self.addCleanup(p.stop)
        d = unittest.mock.patch("pipeline.assembler._get_duration", return_value=45.0)
        d.start()
        self.addCleanup(d.stop)
        self.wd = self.output_dir / "song-film"
        self.wd.mkdir()
        (self.wd / "song.json").write_text(json.dumps(
            {"caption": "slow piano ballad", "lyrics": "[Verse]\nwords",
             "seconds": 45, "title": "Rooftop", "style_name": "Hero"}))
        (self.wd / "background_music.wav").write_bytes(b"as-generated")
        store = DurableStore.default()
        try:
            self.job_id = backend.job_id_from_work_dir(self.wd)
            store.create_or_update_job(self.job_id, self.wd, "Rooftop")
        finally:
            store.close()


class SongVersionTests(_SongFilmCase):
    """Every song a film has sung is kept and can be put back: the engine's
    own vocals, and each seed-vc re-voicing of them. Both sides of a
    re-voicing are remixable into the final."""

    def _convert(self, voice="Nora", output=b"as-nora"):
        """Run a re-voicing with seed-vc stubbed out; returns the source path
        the converter was handed."""
        seen = {}

        def fake_convert(source, ref, out, **kw):
            seen["source"] = Path(source)
            Path(out).write_bytes(output)
            return out

        with unittest.mock.patch("pipeline.svc.convert_song", fake_convert):
            backend._do_song_convert(self.wd, voice)
        return seen["source"]

    def test_the_original_and_the_revoicing_are_both_kept(self):
        self._convert()

        h = backend.music_history.history(self.wd)
        self.assertEqual(len(h["versions"]), 2)
        self.assertEqual(Path(h["versions"][0]["path"]).read_bytes(), b"as-generated")
        self.assertEqual(Path(h["versions"][1]["path"]).read_bytes(), b"as-nora")
        self.assertEqual(h["versions"][0]["voice"], "")
        self.assertEqual(h["versions"][1]["voice"], "Nora")
        # The re-voicing is what the film sings now.
        self.assertEqual(h["selected"], h["versions"][1]["id"])
        self.assertEqual((self.wd / "background_music.wav").read_bytes(), b"as-nora")

    def test_a_second_revoicing_converts_the_original_not_the_clone(self):
        self._convert(output=b"as-nora")
        source = self._convert(voice="Nora", output=b"as-nora-again")

        self.assertEqual(source.read_bytes(), b"as-generated")
        self.assertEqual(len(backend.music_history.history(self.wd)["versions"]), 3)

    def test_revoicing_converts_the_take_in_use_not_the_newest(self):
        """Two engine takes, the older one put back In use: "sing this as"
        must convert what the user is hearing, not the latest generation."""
        backend.music_history.seed_if_empty(self.wd, self.wd / "background_music.wav")
        newer = self.wd / "newer.wav"
        newer.write_bytes(b"newer-take")
        backend.music_history.record(self.wd, newer)
        first = backend.music_history.history(self.wd)["versions"][0]["id"]
        backend.select_song_version(self.job_id, {"version_id": first})

        source = self._convert()

        self.assertEqual(source.read_bytes(), b"as-generated")

    def test_putting_the_original_back_stops_claiming_a_revoicing(self):
        self._convert()
        original = backend.music_history.history(self.wd)["versions"][0]["id"]

        res = backend.select_song_version(self.job_id, {"version_id": original})

        self.assertEqual(res["sung_as"], "")
        self.assertEqual((self.wd / "background_music.wav").read_bytes(), b"as-generated")
        self.assertEqual(json.loads((self.wd / "song.json").read_text())["sung_as"], "")

    def test_the_film_editor_revoices_and_remuxes(self):
        """The edit tab's path: convert, then re-mix the final so the film
        actually plays the new vocals."""
        (self.wd / "combined.mp4").write_bytes(b"mp4")
        final = self.output_dir / "rooftop.mp4"
        final.write_bytes(b"mp4")
        task = "song_revoice_test"
        backend._film_task_meta[task] = {"work_dir": str(self.wd), "scene_id": 0,
                                         "component": "music", "started_at": 0}
        self.addCleanup(backend._film_task_meta.pop, task, None)
        self.addCleanup(backend._film_tasks.pop, task, None)

        def fake_convert(source, ref, out, **kw):
            Path(out).write_bytes(b"as-nora")
            return out

        with unittest.mock.patch("pipeline.svc.convert_song", fake_convert), \
             unittest.mock.patch.object(backend.gapp, "on_remix",
                                        return_value=(str(final), "")) as mix, \
             unittest.mock.patch.object(backend, "_maybe_burn_first_frame_cover"):
            backend._run_song_revoice(task, self.wd, "Nora")

        self.assertEqual(backend._film_tasks[task]["status"], "done",
                         backend._film_tasks[task].get("error"))
        self.assertEqual(backend._film_tasks[task]["sung_as"], "Nora")
        # Re-mixed from the freshly converted track, at the song film's volume.
        self.assertEqual(mix.call_args[0][1], str(self.wd / "background_music.wav"))
        self.assertEqual(len(backend._film_tasks[task]["music_history"]["versions"]), 2)


class SongVersionDeleteTests(_SongFilmCase):
    """Landing a song takes many takes and every one is kept — the ones nobody
    wants have to be able to leave the list."""

    def _record(self, blob: bytes, desc: str) -> int:
        (self.wd / "background_music.wav").write_bytes(blob)
        h = backend.music_history.record(self.wd, self.wd / "background_music.wav", desc)
        return h["versions"][-1]["id"]

    def setUp(self):
        super().setUp()
        backend.music_history.seed_if_empty(self.wd, self.wd / "background_music.wav",
                                            "slow piano ballad")

    def test_deleting_a_take_drops_it_from_the_list(self):
        second = self._record(b"take-two", "another")
        res = backend.delete_song_version(self.job_id, {"version_id": second})

        self.assertEqual(len(res["versions"]), 1)
        self.assertEqual(Path(res["versions"][0]["path"]).read_bytes(), b"as-generated")

    def test_deleting_the_take_in_use_puts_the_newest_one_left_back(self):
        self._record(b"take-two", "another")
        in_use = backend.music_history.history(self.wd)["selected"]

        res = backend.delete_song_version(self.job_id, {"version_id": in_use})

        self.assertEqual(res["selected"], res["versions"][-1]["id"])
        self.assertEqual((self.wd / "background_music.wav").read_bytes(), b"as-generated")
        # …and stops claiming a re-voicing the surviving take never had.
        self.assertEqual(res["sung_as"], "")

    def test_the_only_take_cannot_be_deleted(self):
        only = backend.music_history.history(self.wd)["versions"][0]["id"]
        with self.assertRaises(HTTPException) as ctx:
            backend.delete_song_version(self.job_id, {"version_id": only})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(len(backend.music_history.history(self.wd)["versions"]), 1)


def _audio_payload(blob: bytes = b"my-own-song") -> str:
    return "data:audio/mpeg;base64," + base64.b64encode(blob).decode()


class SongUploadTests(_SongFilmCase):
    """A song the user already has — their own recording, or one generated for
    another film — takes the place of the generated one."""

    def setUp(self):
        super().setUp()
        def fake_transcode(src, out):
            Path(out).write_bytes(b"wav:" + Path(src).read_bytes())
            return out
        t = unittest.mock.patch("pipeline.assembler.transcode_to_wav", fake_transcode)
        t.start()
        self.addCleanup(t.stop)

    def test_the_upload_becomes_the_films_song(self):
        res = backend.song_upload(backend.SongUploadBody(
            work_dir=str(self.wd), filename="rooftop.mp3", data=_audio_payload()))

        self.assertEqual((self.wd / "background_music.wav").read_bytes(), b"wav:my-own-song")
        self.assertEqual(res["duration"], 45.0)
        # The song's real length is the film's — it is what the scenes divide.
        song = json.loads((self.wd / "song.json").read_text())
        self.assertEqual(song["duration"], 45.0)
        self.assertEqual(song["seconds"], 45.0)
        # Nothing of the upload is left lying around beside the track.
        self.assertFalse((self.wd / "uploaded_song.mp3").exists())

    def test_the_take_it_replaces_is_kept_and_can_be_put_back(self):
        backend.song_upload(backend.SongUploadBody(
            work_dir=str(self.wd), filename="rooftop.mp3", data=_audio_payload()))

        h = backend.music_history.history(self.wd)
        self.assertEqual(len(h["versions"]), 2)
        self.assertEqual(Path(h["versions"][0]["path"]).read_bytes(), b"as-generated")
        self.assertIn("rooftop.mp3", h["versions"][1]["desc"])

        backend.select_song_version(self.job_id, {"version_id": h["versions"][0]["id"]})
        self.assertEqual((self.wd / "background_music.wav").read_bytes(), b"as-generated")

    def test_an_upload_is_nobodys_revoicing(self):
        """A stale "sung as X" would label the uploaded file with a conversion
        it never had."""
        (self.wd / "song.json").write_text(json.dumps(
            {"caption": "slow piano ballad", "lyrics": "[Verse]\nwords",
             "seconds": 45, "sung_as": "Nora"}))

        backend.song_upload(backend.SongUploadBody(
            work_dir=str(self.wd), filename="rooftop.mp3", data=_audio_payload()))

        self.assertEqual(json.loads((self.wd / "song.json").read_text())["sung_as"], "")

    def test_an_oversized_file_is_refused(self):
        big = base64.b64encode(b"x" * (backend._MAX_SONG_UPLOAD_BYTES + 1)).decode()
        with self.assertRaises(HTTPException) as ctx:
            backend.song_upload(backend.SongUploadBody(
                work_dir=str(self.wd), filename="huge.wav", data=big))
        self.assertEqual(ctx.exception.status_code, 413)

    def test_a_film_without_a_song_refuses_the_upload(self):
        (self.wd / "song.json").unlink()
        with self.assertRaises(HTTPException) as ctx:
            backend.song_upload(backend.SongUploadBody(
                work_dir=str(self.wd), filename="rooftop.mp3", data=_audio_payload()))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_importing_a_file_opens_a_song_film_with_no_model_involved(self):
        """The other way into the flow: no LLM writes the song and no music
        model sings it — the file IS the film's track, and the job that lands
        is the same song-first job a drafted song makes."""
        res = backend.song_import(backend.SongImportBody(
            video_title="Rooftop Rain", style_name="Hero", n_scenes=4,
            filename="rooftop.wav", data=_audio_payload(b"finished-song")))

        wd = Path(res["work_dir"])
        self.assertEqual((wd / "background_music.wav").read_bytes(), b"wav:finished-song")
        self.assertEqual(res["create_brief"]["format"], "song")
        self.assertEqual(res["create_brief"]["n_scenes"], 4)
        # The film's length comes from the song, not from the style's default.
        self.assertAlmostEqual(res["create_brief"]["minutes"], 0.75)
        # Nothing was written for it: the lyrics are the user's to fill in.
        self.assertEqual(res["lyrics"], "")
        self.assertEqual(len(backend.music_history.history(wd)["versions"]), 1)

    def test_an_imported_song_is_a_job_the_song_tab_can_load(self):
        res = backend.song_import(backend.SongImportBody(
            video_title="Rooftop Rain", style_name="Hero",
            filename="rooftop.wav", data=_audio_payload(b"finished-song")))

        song = backend.get_job_song(res["job_id"])
        self.assertTrue(song["song_url"])
        self.assertEqual(song["duration"], 45.0)


class SongEndingTests(_SongFilmCase):
    """A song that ends abruptly has two cures: pad the take's ending with a
    faded tail, or sing it again with room to land the ending."""

    def _extend(self, seconds=3.0, output=b"as-generated-plus-tail"):
        def fake_extend(src, out, extra, **kw):
            Path(out).write_bytes(output)
            return out

        with unittest.mock.patch("pipeline.assembler.extend_audio_tail", fake_extend), \
             unittest.mock.patch("pipeline.assembler._get_duration", return_value=48.0):
            return backend.song_extend(backend.SongExtendBody(
                work_dir=str(self.wd), seconds=seconds))

    def test_the_abrupt_take_survives_the_extension(self):
        res = self._extend()

        h = backend.music_history.history(self.wd)
        self.assertEqual(len(h["versions"]), 2)
        self.assertEqual(Path(h["versions"][0]["path"]).read_bytes(), b"as-generated")
        self.assertEqual(h["selected"], h["versions"][1]["id"])
        self.assertEqual(h["versions"][1]["source_id"], h["versions"][0]["id"])
        self.assertIn("+3s", h["versions"][1]["desc"])
        # The longer track is the film's, and its new length is what the scene
        # division will divide.
        self.assertEqual((self.wd / "background_music.wav").read_bytes(),
                         b"as-generated-plus-tail")
        self.assertEqual(res["duration"], 48.0)
        self.assertEqual(json.loads((self.wd / "song.json").read_text())["duration"], 48.0)

    def test_extending_a_revoicing_stays_that_voice(self):
        def fake_convert(source, ref, out, **kw):
            Path(out).write_bytes(b"as-nora")
            return out

        with unittest.mock.patch("pipeline.svc.convert_song", fake_convert):
            backend._do_song_convert(self.wd, "Nora")
        self._extend()

        versions = backend.music_history.history(self.wd)["versions"]
        self.assertEqual(versions[-1]["voice"], "Nora")
        # …so the next "sing this as" still converts the engine's own vocals:
        # the extended re-voicing's source chain walks back to the original.
        seen = {}

        def fake_convert(source, ref, out, **kw):
            seen["source"] = Path(source)
            Path(out).write_bytes(b"as-luiz")
            return out

        with unittest.mock.patch("pipeline.svc.convert_song", fake_convert):
            backend._do_song_convert(self.wd, "Nora")
        self.assertEqual(seen["source"].read_bytes(), b"as-generated")

    def test_the_tail_has_to_be_a_sane_length(self):
        for bad in (0, 45):
            with self.assertRaises(HTTPException) as ctx:
                backend.song_extend(backend.SongExtendBody(
                    work_dir=str(self.wd), seconds=bad))
            self.assertEqual(ctx.exception.status_code, 400)

    def _generate(self, add=0.0, can_extend=True):
        """Run _do_song_generate with the worker and engine stubbed out;
        returns (result, what generate_music was called with)."""
        calls = {}

        def fake_generate_music(title, secs, staged, tags, **kw):
            calls.update(secs=secs, extend_from=kw.get("extend_from"),
                         keep_seconds=kw.get("keep_seconds"))
            Path(staged).write_bytes(b"new-take")

        with unittest.mock.patch("pipeline.comfyui.generate_music", fake_generate_music), \
             unittest.mock.patch("pipeline.comfyui.music_engine_can_extend",
                                 return_value=can_extend), \
             unittest.mock.patch.object(backend.gapp, "_preview_worker_urls",
                                        return_value=["http://w1:8188"]), \
             unittest.mock.patch.object(backend.subprocess, "run"):
            res = backend._do_song_generate(self.wd, add_seconds=add)
        return res, calls

    def test_regenerating_longer_extends_the_take_in_use(self):
        """"5 seconds longer than this" keeps THIS song: the take in use rides
        along as the repaint source, its real length (not song.json's) is what
        the seconds land on, and only the tail past it is generated."""
        res, calls = self._generate(add=5)

        self.assertTrue(res["extended"])
        self.assertEqual(calls["secs"], 50.0)          # measured 45.0 + 5
        self.assertEqual(calls["keep_seconds"], 45.0)  # the take survives whole
        self.assertIsNotNone(calls["extend_from"])
        self.assertEqual(json.loads((self.wd / "song.json").read_text())["seconds"], 50.0)

    def test_a_worker_without_the_extend_node_falls_back_to_a_fresh_take(self):
        res, calls = self._generate(add=5, can_extend=False)

        self.assertFalse(res["extended"])
        self.assertEqual(calls["secs"], 50.0)
        self.assertIsNone(calls["extend_from"])

    def test_a_plain_regeneration_keeps_the_length(self):
        res, calls = self._generate()

        self.assertFalse(res["extended"])
        self.assertEqual(calls["secs"], 45.0)
        self.assertIsNone(calls["extend_from"])
        self.assertEqual(json.loads((self.wd / "song.json").read_text())["seconds"], 45.0)


class MusicOnlyMixTests(_SongFilmCase):
    """A music video's final mix is the song and nothing else: the takes are
    shipped muted, so any voice or ambience left in the cut would only be a
    stray spoken beat bleeding in under the track."""

    def test_a_music_video_mixes_music_only(self):
        (self.wd / "job_config.json").write_text(json.dumps(
            {"music_vol": 100, "voice_vol": 100, "ambient_vol": 40}))

        self.assertEqual(backend._mix_volumes(self.wd), (0.0, 100.0, 0.0))

    def test_every_other_film_keeps_its_own_levels(self):
        wd = self.output_dir / "documentary"
        wd.mkdir()
        (wd / "job_config.json").write_text(json.dumps(
            {"music_vol": 18, "voice_vol": 100, "ambient_vol": 40}))

        self.assertEqual(backend._mix_volumes(wd), (100.0, 18.0, 40.0))

    def test_the_mixer_card_shows_the_song_alone(self):
        (self.wd / "combined.mp4").write_bytes(b"mp4")
        (self.wd / "job_config.json").write_text(json.dumps(
            {"music_vol": 100, "voice_vol": 100, "ambient_vol": 40}))

        data = backend.remix_load(str(self.wd))

        self.assertEqual((data["voice_vol"], data["music_vol"], data["ambient_vol"]),
                         (0.0, 100.0, 0.0))

    def test_a_stale_client_cannot_mix_voice_back_in(self):
        (self.wd / "combined.mp4").write_bytes(b"mp4")
        final = self.output_dir / "rooftop.mp4"
        final.write_bytes(b"mp4")

        with unittest.mock.patch.object(backend.gapp, "on_remix",
                                        return_value=(str(final), "ok")) as mix, \
             unittest.mock.patch.object(backend, "_maybe_burn_first_frame_cover"):
            backend.remix_apply(backend.RemixBody(
                work_dir=str(self.wd), voice_vol=100, music_vol=100, ambient_vol=60))

        self.assertEqual(mix.call_args.kwargs["voice_vol"], 0.0)
        self.assertEqual(mix.call_args.kwargs["ambient_vol"], 0.0)
        self.assertEqual(mix.call_args.kwargs["music_vol"], 100)


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


def _write_song(path, plan, rate=22050):
    """A stand-in song: *plan* is [(seconds, amplitude), …]. A bare
    instrumental bed is quiet; a bed with a voice over it is loud."""
    samples = array("h")
    for secs, amp in plan:
        for i in range(int(rate * secs)):
            samples.append(int(amp * 32000 * math.sin(2 * math.pi * 220 * i / rate)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return path


class VocalDetectionTests(unittest.TestCase):
    """Where the singing actually is — the measurement the prompt depends on."""

    def test_finds_the_intro_and_the_instrumental_break(self):
        from pipeline import song_timing
        with tempfile.TemporaryDirectory() as tmp:
            track = _write_song(Path(tmp) / "song.wav", [
                (4.0, 0.08),    # intro, bed alone
                (10.0, 0.5),    # verse
                (4.0, 0.08),    # instrumental break
                (6.0, 0.5),     # chorus
            ])
            regions = song_timing.vocal_regions(track)
        self.assertEqual(len(regions), 2, regions)
        self.assertAlmostEqual(regions[0][0], 4.0, delta=0.5)
        self.assertAlmostEqual(regions[0][1], 14.0, delta=0.5)
        self.assertAlmostEqual(regions[1][0], 18.0, delta=0.5)

    def test_a_track_that_never_drops_is_sung_end_to_end(self):
        # No bare-instrumental stretch to find. Inventing a split here would
        # silence a mouth that should be moving, so the whole track is sung.
        from pipeline import song_timing
        with tempfile.TemporaryDirectory() as tmp:
            track = _write_song(Path(tmp) / "song.wav", [(12.0, 0.5)])
            regions = song_timing.vocal_regions(track)
        self.assertEqual(len(regions), 1)
        self.assertAlmostEqual(regions[0][0], 0.0, delta=0.3)
        self.assertAlmostEqual(regions[0][1], 12.0, delta=0.3)

    def test_unreadable_track_reports_nothing_rather_than_guessing(self):
        from pipeline import song_timing
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "not-a-song.wav"
            bad.write_text("nope")
            self.assertEqual(song_timing.vocal_regions(bad), [])
            self.assertEqual(song_timing.vocal_regions(Path(tmp) / "missing.wav"), [])

    def test_lines_are_paced_through_the_singing_not_the_clock(self):
        # Four lines over a track whose first 4s are instrumental: the first
        # line starts when the voice does, not at zero.
        from pipeline import song_timing
        spans = song_timing.line_times([(4.0, 20.0)], 4)
        self.assertEqual(spans[0], (4.0, 8.0))
        self.assertEqual(spans[-1], (16.0, 20.0))

    def test_line_pacing_steps_over_an_instrumental_break(self):
        from pipeline import song_timing
        spans = song_timing.line_times([(0.0, 10.0), (20.0, 30.0)], 4)
        # Five seconds of singing each: the third line resumes after the gap.
        self.assertEqual(spans[0], (0.0, 5.0))
        self.assertEqual(spans[2], (20.0, 25.0))

    def test_window_vocals_are_relative_to_the_clip(self):
        from pipeline import song_timing
        self.assertEqual(song_timing.window_vocals([(7.5, 30.0)], 0.0, 10.0),
                         [[7.5, 10.0]])
        self.assertEqual(song_timing.window_vocals([(7.5, 30.0)], 10.0, 20.0),
                         [[0.0, 10.0]])
        self.assertEqual(song_timing.window_vocals([(7.5, 30.0)], 40.0, 50.0), [])


class SnapCutTests(unittest.TestCase):
    """Scene seams land between lyric lines, never through one — within the
    take-length bounds, and never worse than the even grid."""

    def test_a_seam_snaps_to_the_boundary_between_lines(self):
        from pipeline import song_timing
        # Lines at 4–8–12–16–20: the even cut at 10 sits mid-line, the
        # boundaries at 8 and 12 are equally near — the earlier one wins.
        spans = [(4.0, 8.0), (8.0, 12.0), (12.0, 16.0), (16.0, 20.0)]
        cuts = song_timing.snap_cuts(2, 20.0, spans, min_secs=5.0, max_secs=12.0)
        self.assertEqual(cuts, [0.0, 8.0, 20.0])

    def test_an_instrumental_break_cuts_at_its_middle(self):
        from pipeline import song_timing
        # A 4s break between lines 2 and 3 (10s–14s): the cut falls in the
        # middle of the silence, where nothing can be clipped.
        spans = [(0.0, 5.0), (5.0, 10.0), (14.0, 19.0), (19.0, 24.0)]
        cuts = song_timing.snap_cuts(2, 24.0, spans, min_secs=5.0, max_secs=12.0)
        self.assertEqual(cuts, [0.0, 12.0, 24.0])

    def test_takes_at_the_floor_keep_the_grid_even(self):
        from pipeline import song_timing
        # 4 scenes over 20s is 5s each — the minimum. There is no slack to
        # snap with, so the even grid stands whatever the lines say.
        spans = [(0.0, 3.0), (3.0, 7.0), (7.0, 13.0), (13.0, 20.0)]
        cuts = song_timing.snap_cuts(4, 20.0, spans, min_secs=5.0, max_secs=12.0)
        self.assertEqual(cuts, [0.0, 5.0, 10.0, 15.0, 20.0])

    def test_a_seam_with_no_line_in_reach_falls_back_to_the_grid(self):
        from pipeline import song_timing
        # One 20s line: its only boundaries are its ends, far outside the
        # allowed window around the even cut — the grid is kept.
        cuts = song_timing.snap_cuts(2, 20.0, [(0.0, 20.0)],
                                     min_secs=5.0, max_secs=12.0)
        self.assertEqual(cuts, [0.0, 10.0, 20.0])

    def test_every_window_stays_inside_the_bounds(self):
        from pipeline import song_timing
        # Boundaries at 7 and 13 pull the first seam early; the second seam
        # then has no candidate inside its window and falls back, clamped so
        # the last window still fits.
        spans = [(0.0, 7.0), (7.0, 13.0), (13.0, 22.0), (22.0, 30.0)]
        cuts = song_timing.snap_cuts(3, 30.0, spans, min_secs=5.0, max_secs=12.0)
        self.assertEqual(cuts[0], 0.0)
        self.assertEqual(cuts[-1], 30.0)
        widths = [b - a for a, b in zip(cuts, cuts[1:])]
        for w in widths:
            self.assertGreaterEqual(w, 6.0 - 0.01)
            self.assertLessEqual(w, 12.0 + 0.01)
        self.assertEqual(cuts[1], 7.0)

    def test_a_measured_break_outranks_a_nearby_estimated_seam(self):
        from pipeline import song_timing
        # Observed on a real render: the estimated seam at 9.6 sat right on
        # the even cut, but the singing actually ran to 10.0 — the cut grazed
        # the line's last word. A real instrumental break (10.0–12.5) was
        # 1.65s away; cutting its middle is exact, so it must win. Note the
        # 4th line's ESTIMATED span straddles the break — line seams alone
        # never offer the gap as a candidate.
        spans = [(0.0, 3.2), (3.2, 6.4), (6.4, 9.6), (9.6, 13.9),
                 (13.9, 16.9), (16.9, 20.0)]
        regions = [(0.0, 10.0), (12.5, 20.0)]
        cuts = song_timing.snap_cuts(2, 20.0, spans, regions,
                                     min_secs=5.0, max_secs=12.0)
        self.assertEqual(cuts, [0.0, 11.25, 20.0])
        # Without the measured regions the nearer estimated seam wins — on the
        # frame grid, so 9.6 rounds forward to the 231st frame.
        cuts = song_timing.snap_cuts(2, 20.0, spans,
                                     min_secs=5.0, max_secs=12.0)
        self.assertEqual(cuts, [0.0, 9.625, 20.0])

    def test_nothing_to_snap_to_reports_nothing(self):
        from pipeline import song_timing
        self.assertEqual(song_timing.snap_cuts(2, 20.0, [],
                                               min_secs=5.0, max_secs=12.0), [])
        self.assertEqual(song_timing.snap_cuts(1, 20.0, [(0.0, 20.0)],
                                               min_secs=5.0, max_secs=12.0), [])


class FrameGridTests(unittest.TestCase):
    """Windows land on the film's frame grid, so a take is exactly as long as
    its own stretch of the song.

    Off the grid the trim keeps the frame straddling the window's end — up to
    42 ms of picture the overlaid track never gets back — and the error adds
    up scene by scene: a shipped 14-scene film ran 0.44 s behind its own song
    by the last shot, 20-scene and 24-scene films 0.38 s and 0.25 s."""

    def test_a_cut_never_lands_between_frames(self):
        from pipeline import song_timing
        # 4.9s windows: 117.6 frames, which no trim can deliver.
        spans = [(0.0, 4.9), (4.9, 9.8), (9.8, 14.7), (14.7, 19.6)]
        cuts = song_timing.snap_cuts(4, 19.6, spans, min_secs=4.0, max_secs=6.0)
        for cut in cuts:
            self.assertAlmostEqual(cut * 24, round(cut * 24), delta=1e-4,
                                   msg=f"cut {cut} is not on the frame grid")

    def test_the_takes_add_up_to_the_song_and_never_outrun_it(self):
        from pipeline import song_timing
        spans = [(0.0, 4.9), (4.9, 9.8), (9.8, 14.7), (14.7, 19.6)]
        cuts = song_timing.snap_cuts(4, 19.63, spans, min_secs=4.0, max_secs=6.0)
        # Each window is a whole number of frames, so the trim delivers it
        # exactly and the seams stay where the song put them.
        clock = 0.0
        for start, end in zip(cuts, cuts[1:]):
            self.assertAlmostEqual(clock, start, places=6)
            clock += round((end - start) * 24) / 24
        self.assertLessEqual(cuts[-1], 19.63)

    def test_a_seam_rounds_forward_off_the_line_it_protects(self):
        from pipeline import song_timing
        # The line ends at 8.06 — 193.44 frames. Rounding back would put the
        # seam inside it and hand the line to both scenes.
        spans = [(4.0, 8.06), (8.06, 12.12)]
        cuts = song_timing.snap_cuts(2, 16.12, spans, min_secs=5.0, max_secs=12.0)
        self.assertGreaterEqual(cuts[1], 8.06)
        self.assertEqual(song_timing.lines_in_window(["one", "two"], spans,
                                                     0.0, cuts[1]), ["one"])

    def test_a_line_grazing_the_seam_is_not_sung_in_both(self):
        from pipeline import song_timing
        # Under a frame of overlap is not a sung line: the seam sits 20 ms
        # inside "two", which belongs to the scene that holds the rest of it.
        spans = [(0.0, 5.0), (5.0, 10.0)]
        self.assertEqual(song_timing.lines_in_window(["one", "two"], spans,
                                                     0.0, 5.02), ["one"])
        self.assertEqual(song_timing.lines_in_window(["one", "two"], spans,
                                                     5.02, 10.0), ["two"])


class VoicedRegionTests(unittest.TestCase):
    """What counts as singing: the level split, held to the words actually
    transcribed inside it."""

    REGIONS = [(0.0, 18.0), (21.0, 40.0)]

    def test_a_loud_region_with_no_word_in_it_is_bleed(self):
        from pipeline import lyric_align
        # Measured on a shipped film: 13 s of stem-loud intro that whisper
        # heard nothing in — the lead mimed a fingerpicked guitar.
        words = [("paper", 22.0, 22.5), ("crown", 22.5, 23.2)]
        self.assertEqual(lyric_align.voiced_regions(self.REGIONS, words),
                         [(21.25, 23.95)])

    def test_a_region_keeps_the_singing_it_carries(self):
        from pipeline import lyric_align
        words = [("one", 1.0, 1.5), ("two", 16.0, 17.5),
                 ("three", 22.0, 23.0), ("four", 38.0, 39.5)]
        self.assertEqual(lyric_align.voiced_regions(self.REGIONS, words),
                         [(0.25, 18.0), (21.25, 40.0)])

    def test_a_silent_stretch_is_asked_again_on_its_own(self):
        from pipeline import lyric_align
        # A whole-track pass reads a stretch in the context of its neighbours
        # and skips a wordless vocal in it: a real "ooh-ooh" intro came back
        # empty beside the verses and as 130 "oh"s when handed over alone.
        # Anything about to be dropped gets that second chance.
        asked = []

        def reask(stem, start, end, language):
            asked.append((round(start, 2), round(end, 2)))
            return [("oh", start + 0.5, end - 0.5)]

        with unittest.mock.patch.object(lyric_align, "_words_in_slice",
                                        side_effect=reask):
            kept = lyric_align.voiced_regions(
                self.REGIONS, [("paper", 22.0, 22.5), ("crown", 22.5, 23.2)],
                stem=Path("stem.wav"), language="en")
        # The empty region and the unheard tail of the second one, nothing else.
        self.assertEqual(asked, [(0.0, 18.0), (23.95, 40.0)])
        self.assertEqual(kept, [(0.0, 18.0), (21.25, 40.0)])

    def test_a_stretch_still_empty_the_second_time_is_bleed(self):
        from pipeline import lyric_align
        with unittest.mock.patch.object(lyric_align, "_words_in_slice",
                                        return_value=[]):
            kept = lyric_align.voiced_regions(
                self.REGIONS, [("paper", 22.0, 22.5), ("crown", 22.5, 23.2)],
                stem=Path("stem.wav"), language="en")
        self.assertEqual(kept, [(21.25, 23.95)])

    def test_no_transcript_keeps_every_measured_region(self):
        from pipeline import lyric_align
        self.assertEqual(lyric_align.voiced_regions(self.REGIONS, None),
                         self.REGIONS)
        self.assertEqual(lyric_align.voiced_regions(self.REGIONS, []),
                         self.REGIONS)


class StemTimingTests(unittest.TestCase):
    """Vocal timing on the separated stem: the fix for tracks whose
    instruments are as loud as the voice (a distorted-guitar intro read as
    sung end to end on the mix, and the lead mouthed a verse over it)."""

    def test_a_quiet_stem_is_nobody_singing(self):
        from pipeline import song_timing
        with tempfile.TemporaryDirectory() as tmp:
            # Nothing but low-level bleed: percentile dynamics would still
            # split it, the absolute floor knows better.
            stem = _write_song(Path(tmp) / "stem.wav", [(6.0, 0.001), (6.0, 0.004)])
            self.assertEqual(song_timing.vocal_regions(stem, min_level_db=-45.0), [])

    def test_the_floor_ignores_bleed_below_it(self):
        from pipeline import song_timing
        with tempfile.TemporaryDirectory() as tmp:
            # Silence, then bleed at ~-47 dB, then a voice: true silence drags
            # the midpoint threshold under the bleed (~-49 dB), so without the
            # absolute floor the bleed would count as singing from 6s.
            stem = _write_song(Path(tmp) / "stem.wav",
                               [(6.0, 0.0), (6.0, 0.006), (8.0, 0.5)])
            self.assertAlmostEqual(
                song_timing.vocal_regions(stem)[0][0], 6.0, delta=0.5)
            regions = song_timing.vocal_regions(stem, min_level_db=-45.0)
        self.assertEqual(len(regions), 1)
        self.assertAlmostEqual(regions[0][0], 12.0, delta=0.5)

    def test_measure_regions_prefers_the_stem(self):
        # A hard-rock mix: loud wall to wall, so the mix split calls it sung
        # end to end. The stem knows the first 8 seconds are instrumental.
        from pipeline import song_timing
        with tempfile.TemporaryDirectory() as tmp:
            mix = _write_song(Path(tmp) / "song.wav", [(20.0, 0.5)])
            stem = _write_song(Path(tmp) / "stem.wav", [(8.0, 0.0), (12.0, 0.5)])
            # What the mix alone would say: sung end to end.
            (m0, m1), = song_timing.vocal_regions(mix)
            self.assertEqual(m0, 0.0)
            self.assertAlmostEqual(m1, 20.0, delta=0.5)
            with unittest.mock.patch.object(song_timing, "vocal_stem",
                                            return_value=stem):
                regions = song_timing.measure_regions(mix)
        self.assertEqual(len(regions), 1)
        self.assertAlmostEqual(regions[0][0], 8.0, delta=0.5)

    def test_no_stem_falls_back_to_the_mix(self):
        from pipeline import song_timing
        with tempfile.TemporaryDirectory() as tmp:
            mix = _write_song(Path(tmp) / "song.wav", [(4.0, 0.08), (8.0, 0.5)])
            with unittest.mock.patch.object(song_timing, "vocal_stem",
                                            return_value=None):
                regions = song_timing.measure_regions(mix)
        self.assertEqual(len(regions), 1)
        self.assertAlmostEqual(regions[0][0], 4.0, delta=0.5)

    def test_the_stem_is_cached_beside_the_track_until_it_changes(self):
        import os
        from pipeline import song_timing
        calls = []

        def fake_separate(source, output):
            calls.append(source)
            _write_song(output, [(2.0, 0.0), (2.0, 0.5)])
            return output

        with tempfile.TemporaryDirectory() as tmp:
            track = _write_song(Path(tmp) / "background_music.wav", [(4.0, 0.5)])
            with unittest.mock.patch("pipeline.svc.separate_vocals",
                                     side_effect=fake_separate):
                first = song_timing.vocal_stem(track)
                again = song_timing.vocal_stem(track)
                self.assertEqual(first, again)
                self.assertEqual(len(calls), 1)
                # A new track under the same name (another version picked, or
                # uploaded) invalidates the cache.
                os.utime(track, (track.stat().st_atime,
                                 track.stat().st_mtime + 10))
                song_timing.vocal_stem(track)
                self.assertEqual(len(calls), 2)
        self.assertEqual(first.name, "background_music_vocals.wav")

    def test_a_loud_intro_no_longer_sings(self):
        # The end-to-end shape of the user's bug: guitar as loud as the voice,
        # vocals entering at 8s. With the stem measured, scene 1 sings nothing
        # and its prompt can hold the mouth shut.
        scenes = [Scene(id=i, title="t", image_prompt="i", video_prompt="v",
                        narration="", mode="silent", duration=10.0,
                        metadata_extra={"mode": "silent", "singing": True,
                                        "cast": ["Ada"]})
                  for i in (1, 2)]
        from pipeline import song_timing
        with tempfile.TemporaryDirectory() as tmp:
            mix = _write_song(Path(tmp) / "song.wav", [(20.0, 0.5)])
            stem = _write_song(Path(tmp) / "stem.wav", [(8.0, 0.0), (12.0, 0.5)])
            with unittest.mock.patch.object(song_timing, "vocal_stem",
                                            return_value=stem):
                story.assign_song_slices(scenes, "[Verse]\none\ntwo",
                                         total_seconds=20.0, track=mix)
        first, second = scenes[0].metadata, scenes[1].metadata
        # The seam snaps to the vocal onset: the intro stands as its own take.
        self.assertAlmostEqual(first["song_window"][1], 8.0, delta=0.6)
        self.assertEqual(first["sings"], "")
        self.assertEqual(first["vocal_ranges"], [])
        self.assertIn("one", second["sings"])
        self.assertAlmostEqual(second["vocal_ranges"][0][0], 0.0, delta=0.6)


class LyricAlignTests(unittest.TestCase):
    """Whisper-aligned lyric lines (the song_align_lyrics option): each line's
    time is MEASURED off the stem's transcript; every failure path falls back
    to the energy-paced estimate."""

    LINES = ["one two three", "four five six", "seven eight nine"]

    def _words(self):
        # A perfect transcript: nine words a second apart, entering at 4s.
        return [(w, 4.0 + i, 4.0 + i + 0.8) for i, w in enumerate(
            "one two three four five six seven eight nine".split())]

    def test_matched_words_become_line_spans(self):
        from pipeline import lyric_align
        with unittest.mock.patch.object(lyric_align, "word_times",
                                        return_value=self._words()):
            spans = lyric_align.align_lines(Path("stem.wav"), self.LINES)
        self.assertEqual(spans, [(4.0, 6.8), (7.0, 9.8), (10.0, 12.8)])

    def test_a_garbled_line_is_interpolated_between_neighbours(self):
        from pipeline import lyric_align
        words = [w for w in self._words()
                 if w[0] not in ("four", "five", "six")]
        with unittest.mock.patch.object(lyric_align, "word_times",
                                        return_value=words):
            spans = lyric_align.align_lines(Path("stem.wav"), self.LINES)
        # 6/9 words matched clears the bar; line 2 takes the gap between its
        # matched neighbours and the result stays monotonic.
        self.assertEqual(spans[0], (4.0, 6.8))
        self.assertEqual(spans[2], (10.0, 12.8))
        self.assertTrue(spans[0][1] <= spans[1][0] < spans[1][1] <= spans[2][0])

    def test_too_weak_a_match_falls_back(self):
        from pipeline import lyric_align
        with unittest.mock.patch.object(
                lyric_align, "word_times",
                return_value=[("la", 1.0, 1.4), ("laa", 2.0, 2.4)]):
            self.assertIsNone(
                lyric_align.align_lines(Path("stem.wav"), self.LINES))

    def test_no_whisper_install_falls_back(self):
        from pipeline import lyric_align
        with unittest.mock.patch.object(lyric_align, "word_times",
                                        return_value=None):
            self.assertIsNone(
                lyric_align.align_lines(Path("stem.wav"), self.LINES))

    def test_hallucinations_outside_the_singing_are_dropped(self):
        from pipeline import lyric_align
        # The chorus "hallucinated" again at 50s, past the measured singing:
        # region gating discards it before matching, so no line is dragged out.
        words = self._words() + [("seven", 50.0, 50.4), ("eight", 50.6, 51.0),
                                 ("nine", 51.2, 51.6)]
        with unittest.mock.patch.object(lyric_align, "word_times",
                                        return_value=words):
            spans = lyric_align.align_lines(Path("stem.wav"), self.LINES,
                                            regions=[(4.0, 13.0)])
        self.assertEqual(spans[2], (10.0, 12.8))

    def test_slices_use_aligned_spans_when_the_option_is_on(self):
        # All four lines measured inside the first 8s of a 20s track: with
        # alignment on, scene 1 carries every word and scene 2 stays silent;
        # off, even pacing spreads them across both.
        lyrics = "[Verse]\none\ntwo\nthree\nfour"
        aligned = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 8.0)]

        def scenes():
            return [Scene(id=i, title="t", image_prompt="i", video_prompt="v",
                          narration="", mode="silent", duration=10.0,
                          metadata_extra={"mode": "silent", "singing": True,
                                          "cast": ["Ada"]})
                    for i in (1, 2)]

        with unittest.mock.patch("pipeline.song_timing.measure_regions",
                                 return_value=[(0.0, 20.0)]), \
             unittest.mock.patch("pipeline.song_timing.vocal_stem",
                                 return_value=Path("stem.wav")), \
             unittest.mock.patch("pipeline.lyric_align.align_lines",
                                 return_value=aligned):
            on = scenes()
            story.assign_song_slices(on, lyrics, total_seconds=20.0,
                                     track=Path("song.wav"), align_lyrics=True)
            off = scenes()
            story.assign_song_slices(off, lyrics, total_seconds=20.0,
                                     track=Path("song.wav"))
        self.assertIn("four", on[0].metadata["sings"])
        self.assertEqual(on[1].metadata["sings"], "")
        self.assertIn("four", off[1].metadata["sings"])


class SongSliceTimingTests(unittest.TestCase):
    """assign_song_slices against a real track: the words a scene is told to
    mouth must be the words its own pinned slice contains.

    Pinned to the MIX measurement — vocal_stem is mocked away so no test ever
    invokes a real demucs install; the stem path has its own tests below."""

    LYRICS = "[Verse]\none\ntwo\n\n[Chorus]\nthree\nfour"

    def setUp(self):
        patcher = unittest.mock.patch("pipeline.song_timing.vocal_stem",
                                      return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _scenes(self):
        return [Scene(id=i, title="t", image_prompt="i", video_prompt="v",
                      narration="", mode="silent", duration=10.0,
                      metadata_extra={"mode": "silent", "singing": True,
                                      "cast": ["Ada"]})
                for i in (1, 2)]

    def test_an_intro_is_not_filled_with_lyrics(self):
        scenes = self._scenes()
        with tempfile.TemporaryDirectory() as tmp:
            track = _write_song(Path(tmp) / "song.wav",
                                [(4.0, 0.08), (16.0, 0.5)])
            story.assign_song_slices(scenes, self.LYRICS, total_seconds=20.0,
                                     track=track)
        first, second = scenes[0].metadata, scenes[1].metadata
        # The voice enters at 4s, so scene 1 opens on 4 seconds of nothing.
        self.assertEqual(first["song_window"][0], 0.0)
        self.assertAlmostEqual(first["vocal_ranges"][0][0], 4.0, delta=0.5)
        # The singing runs to the end of the scene's own window.
        self.assertAlmostEqual(first["vocal_ranges"][0][1],
                               first["song_window"][1], delta=0.3)
        # Scene 2 is sung throughout.
        self.assertAlmostEqual(second["vocal_ranges"][0][0], 0.0, delta=0.3)
        # Four lines over 16s of singing = 4s each, so scene 1 opens the song
        # and never holds the last line — the bug put "three"/"four" under it.
        self.assertIn("one", first["sings"])
        self.assertNotIn("four", first["sings"])
        self.assertIn("four", second["sings"])

    def test_the_seam_lands_between_lines_not_through_one(self):
        # Four lines at ~4s each over singing that starts at 4s: the line
        # boundaries sit near 8s and 12s, and the even cut at 10s would land
        # mid-line. The seam must snap to one of the boundaries.
        scenes = self._scenes()
        with tempfile.TemporaryDirectory() as tmp:
            track = _write_song(Path(tmp) / "song.wav",
                                [(4.0, 0.08), (16.0, 0.5)])
            story.assign_song_slices(scenes, self.LYRICS, total_seconds=20.0,
                                     track=track)
        seam = scenes[0].metadata["song_window"][1]
        self.assertTrue(min(abs(seam - 8.0), abs(seam - 12.0)) < 0.6,
                        f"seam {seam} is not a line boundary")
        # Whole lines only: no line is sung in both scenes.
        first = set(scenes[0].metadata["sings"].splitlines())
        second = set(scenes[1].metadata["sings"].splitlines())
        self.assertFalse(first & second)

    def test_without_a_track_the_old_proportional_split_stands(self):
        # No measurement available — fall back rather than guess, and leave no
        # vocal_ranges behind so the prompt keeps its old assumption.
        scenes = self._scenes()
        story.assign_song_slices(scenes, self.LYRICS, total_seconds=20.0)
        self.assertEqual(scenes[0].metadata["sings"], "one\ntwo")
        self.assertEqual(scenes[1].metadata["sings"], "three\nfour")
        self.assertNotIn("vocal_ranges", scenes[0].metadata)

    def test_windows_still_tile_the_whole_track(self):
        # The concat length must stay equal to the track length: snapped or
        # not, the windows meet exactly and cover [0, total].
        scenes = self._scenes()
        with tempfile.TemporaryDirectory() as tmp:
            track = _write_song(Path(tmp) / "song.wav",
                                [(4.0, 0.08), (16.0, 0.5)])
            story.assign_song_slices(scenes, self.LYRICS, total_seconds=20.0,
                                     track=track)
        first, second = scenes[0].metadata, scenes[1].metadata
        self.assertEqual(first["song_window"][0], 0.0)
        self.assertEqual(first["song_window"][1], second["song_window"][0])
        self.assertEqual(second["song_window"][1], 20.0)
        for s in scenes:
            window = s.metadata["song_window"]
            self.assertAlmostEqual(s.duration, window[1] - window[0], places=2)


class SingingPromptTests(unittest.TestCase):
    """What the model is actually told to do with its mouth, and whether a
    shot with nobody in it gets handed a singer."""

    def _prompt(self, meta, pictures=("Ada",)):
        return perf.build_h3_prompt(
            {"mode": "silent", "singing": True, "seconds": 10.0, **meta},
            picture_names=list(pictures))

    def test_a_shot_with_no_cast_gets_no_performer(self):
        # The scene was authored with nobody in it; asking for a performer
        # anyway made H3 invent a different stranger in every such scene.
        prompt = self._prompt({"sings": "one\ntwo", "cast": []}, pictures=[])
        self.assertIn("NO people in it", prompt)
        self.assertIn("Do not add a person", prompt)
        self.assertNotIn("[SONG]", prompt)
        self.assertNotIn("performing a song", prompt)
        # And no voice is asked for in the take's own audio.
        self.assertIn("No speech and no voices at all", prompt)

    def test_an_instrumental_window_keeps_the_mouth_shut(self):
        prompt = self._prompt({"sings": "", "vocal_ranges": []})
        self.assertIn("No voice sings anywhere in this shot", prompt)
        self.assertIn("mouth closed and completely still", prompt)
        self.assertNotIn("[SONG]", prompt)

    def test_a_partly_sung_window_says_when_the_voice_comes_in(self):
        prompt = self._prompt({"sings": "one", "vocal_ranges": [[7.75, 10.0]]})
        self.assertIn("7.75s to 10s", prompt)
        self.assertIn("Outside that, Ada is not singing", prompt)
        self.assertIn("[SONG]", prompt)

    def test_the_mouth_is_never_ordered_to_move_throughout(self):
        # The absolute instruction overrode the pinned audio and had the lead
        # mouthing a verse through an instrumental intro.
        for meta in ({"sings": "one"},
                     {"sings": "one", "vocal_ranges": [[0.0, 10.0]]},
                     {"sings": "one", "vocal_ranges": [[2.0, 8.0]]}):
            prompt = self._prompt(meta)
            self.assertNotIn("The mouth is never closed and still", prompt)
            self.assertIn("closes the instant the singing stops", prompt)

    def test_an_unmeasured_scene_still_performs(self):
        # Scripts written before the song was analysed carry no vocal_ranges;
        # they must keep behaving exactly as they did.
        prompt = self._prompt({"sings": "one\ntwo"})
        self.assertIn("performing a song on camera", prompt)
        self.assertIn("[SONG]", prompt)
        self.assertIn("A clear live singing voice", prompt)
        self.assertNotIn("Outside that", prompt)


class SoundedTakeTests(unittest.TestCase):
    """A singing take ships with ITS stretch of the song under the picture.

    It used to ship muted — the film's mix is music-only, so the take's audio is
    never heard in the final — which left every scene clip silent in the editor
    and on the render wall, with nothing to check the performance against."""

    def _render(self, clip_secs=5.17, window=(4.0, 9.0), track=True):
        import resume_generation as rg
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wd = Path(tmp.name)
        (wd / "scene_02_preview.png").write_bytes(b"png")
        if track:
            (wd / "background_music.wav").write_bytes(b"wav")
        scene = _singing()
        scene.metadata_extra = {**scene.metadata_extra, "song_window": list(window),
                                "seconds": window[1] - window[0]}
        cuts, muxes, trims = [], [], []

        def fake_cut(src, out, t0, t1):
            cuts.append((Path(src).name, Path(out).name, round(t0, 2), round(t1, 2)))
            Path(out).write_bytes(b"wav")
            return Path(out)

        def fake_refs(m, cfg, work_dir, style_name="", scene_id=0):
            return {"pictures": [{"slot": 1, "name": "Ada", "kind": "character",
                                  "path": str(wd / "scene_02_preview.png")}],
                    "audios": []}

        def fake_gen(engine, prompt, ref_images, out, **kw):
            Path(out).write_bytes(b"mp4")
            return Path(out)

        def fake_mux(video, audio, out, extra_tail_secs=0.0):
            muxes.append((Path(video).name, Path(audio).name))
            Path(out).write_bytes(b"mp4")
            return Path(out)

        def fake_trim(src, out, secs):
            trims.append(round(secs, 2))
            Path(out).write_bytes(b"mp4")
            return Path(out)

        with unittest.mock.patch("pipeline.comfyui.generate_video_h3_ref", side_effect=fake_gen), \
             unittest.mock.patch("app.resolve_performance_references", side_effect=fake_refs), \
             unittest.mock.patch.object(rg, "_cut_audio_segment", side_effect=fake_cut), \
             unittest.mock.patch.object(rg, "mux_video_audio", side_effect=fake_mux), \
             unittest.mock.patch.object(rg, "trim_video", side_effect=fake_trim), \
             unittest.mock.patch.object(rg, "_get_duration", return_value=clip_secs), \
             unittest.mock.patch.object(rg, "ensure_video_resolution"):
            out = rg.render_performance_scene(
                scene, wd, {"style_name": "Song", "performance_verify": False,
                            "h3_silent_scenes": True},
                comfy_url="http://w:8188", vid_width=704, vid_height=1280)
        return out, cuts, muxes, trims

    def test_the_take_carries_its_slice_of_the_song(self):
        out, cuts, muxes, trims = self._render()
        # Two cuts of the same window: the one PINNED into the generation, then
        # the one laid back under the finished take.
        self.assertEqual([c[2:] for c in cuts], [(4.0, 9.0), (4.0, 9.0)])
        self.assertEqual(muxes, [(out.name, "scene_02_final.song.wav")])
        # The mux trims the picture to the audio, so the old trim step is gone.
        self.assertEqual(trims, [])

    def test_the_slice_never_outruns_the_take(self):
        # H3 renders on a frame grid and a window can outrun its own clip
        # ceiling; muxing longer audio would freeze frames onto the end.
        _, cuts, muxes, _ = self._render(clip_secs=3.5)
        self.assertEqual(cuts[-1][2:], (4.0, 7.5))
        self.assertEqual(len(muxes), 1)

    def test_the_cut_keeps_the_windows_own_frame(self):
        # The window sits on the frame grid (song_timing.frame_snap) and a
        # frame is 41.6667 ms, so a cut rounded to milliseconds lands back
        # between frames — and the mux that trims the picture to it keeps a
        # whole extra frame, which is the drift this path exists to avoid.
        # Cut for real: what matters is the audio that comes out.
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as tmp:
            song = _write_song(Path(tmp) / "song.wav", [(20.0, 0.4)])
            out = rg._cut_audio_segment(song, Path(tmp) / "cut.wav",
                                        4.916667, 9.833333)
            with wave.open(str(out), "rb") as handle:
                secs = handle.getnframes() / handle.getframerate()
        # 118 frames of picture, to within a hundredth of one.
        self.assertAlmostEqual(secs * 24, 118, delta=0.01)

    def test_no_song_on_disk_falls_back_to_the_old_trim(self):
        # Nothing to lay under it: the take keeps its own voice, but still has
        # to hold the song's timeline.
        _, cuts, muxes, trims = self._render(track=False)
        self.assertEqual(cuts, [])
        self.assertEqual(muxes, [])
        self.assertEqual(trims, [5.0])


class SongFitTests(unittest.TestCase):
    """A song generated, re-voiced, uploaded or picked AFTER the divide can be
    another length; the scenes keep the windows cut for the old take. "The
    Cycle" was divided for a 230 s take, its song re-sung at 185 s, and the
    render shot 37 of 46 scenes before the first empty slice failed."""

    def _scenes(self, windows, singing=True):
        out = []
        for i, (t0, t1) in enumerate(windows, start=1):
            md = {"mode": "silent", "singing": singing, "song_window": [t0, t1]}
            out.append(Scene(id=i, title=f"s{i}", image_prompt="i", video_prompt="v",
                             narration="", mode="silent", duration=t1 - t0,
                             metadata_extra=md))
        return out

    def test_windows_inside_the_track_fit(self):
        scenes = self._scenes([(0, 5), (5, 10), (10, 15)])
        self.assertEqual(perf.song_windows_past_track(scenes, 15.1), (15.0, []))
        # The last cut is frame-snapped and a re-voiced take runs a hair short.
        self.assertEqual(perf.song_windows_past_track(scenes, 14.8), (15.0, []))

    def test_scenes_past_a_shorter_song_are_named(self):
        scenes = self._scenes([(0, 5), (5, 10), (10, 15), (15, 20)])
        end, past = perf.song_windows_past_track(scenes, 12.0)
        self.assertEqual((end, past), (20.0, [3, 4]))
        msg = perf.song_length_mismatch_message(12.0, end, past)
        self.assertIn("12s long", msg)
        self.assertIn("divided for 20s of song", msg)
        self.assertIn("scenes 3–4 would perform past its end", msg)
        self.assertIn("Draft the story from this song again", msg)

    def test_stored_rows_and_non_singing_scenes(self):
        rows = [{"id": 1, "metadata": {"mode": "silent", "singing": True, "song_window": [0, 9]}},
                {"id": 2, "metadata": {"mode": "narration", "song_window": [9, 30]}}]
        self.assertEqual(perf.song_windows_past_track(rows, 6.0), (9.0, [1]))
        self.assertEqual(perf.song_windows_past_track(self._scenes([(0, 9)], singing=False), 6.0),
                         (0.0, []))
        self.assertIn("scene 1 would", perf.song_length_mismatch_message(6.0, 9.0, [1]))

    def test_a_single_scene_past_the_song_is_refused_before_it_is_shot(self):
        """The film editor's re-shoot of one scene: no take without its track."""
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "background_music.wav").write_bytes(b"wav")
            scene = _singing()
            scene.metadata_extra = {**scene.metadata_extra, "song_window": [185.2, 190.2],
                                    "seconds": 5.0}
            with unittest.mock.patch.object(rg, "_track_seconds", return_value=185.24), \
                 unittest.mock.patch("app.resolve_performance_references",
                                     return_value={"pictures": [{"slot": 1, "name": "Ada",
                                                                 "kind": "character",
                                                                 "path": str(wd / "p.png")}],
                                                   "audios": []}), \
                 unittest.mock.patch.object(rg, "_cut_audio_segment") as cut, \
                 unittest.mock.patch("pipeline.comfyui.generate_video_h3_ref") as gen:
                with self.assertRaises(RuntimeError) as caught:
                    rg._render_performance_clip(
                        scene, scene.metadata_extra, wd,
                        {"style_name": "Song", "h3_silent_scenes": True},
                        wd / "scene_02_clip.mp4", comfy_url="http://w:8188",
                        vid_width=704, vid_height=1280, style_name="Song")
            self.assertIn("185s long", str(caught.exception))
            cut.assert_not_called()
            gen.assert_not_called()

    def test_a_cut_past_the_end_of_the_song_does_not_pass_as_audio(self):
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as tmp:
            song = _write_song(Path(tmp) / "song.wav", [(20.0, 0.4)])
            with self.assertRaises(ValueError):
                rg._cut_audio_segment(song, Path(tmp) / "cut.wav", 25.0, 30.0)
            self.assertFalse((Path(tmp) / "cut.wav").exists())


if __name__ == "__main__":
    unittest.main()
