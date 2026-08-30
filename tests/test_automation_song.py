"""Music-video automation: the song steps unattended runs need.

The Create screen asks a human for the format and the Song tab walks them
through the song by hand; automation has neither. These cover the settings that
answer for it — the format automation writes in, and for music videos the
song-first sequence (write → QC the lyrics → render the track → re-voice), its
review gate, and the ORDER it all happens in: the track has to exist before the
story is divided or the scene windows are timed against a guess and the
performed takes have nothing to sing to.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app
import webapp.backend.main as backend
from pipeline import story as story_mode
from tests.test_automation_start import TempConfigCase


SONG_CFG = {
    "youtube_auto_write_scripts": True,
    "youtube_auto_format": "song",
    "youtube_auto_song": True,
    "youtube_auto_song_approve": True,
}


class AutoFormatTests(TempConfigCase):
    """The format automation writes in — and its normalisation."""

    def test_defaults_to_narration(self):
        self.write_config({})
        self.assertEqual(backend._auto_format(app.load_config()), "narration")

    def test_round_trips_every_offered_format(self):
        for fmt in app.VIDEO_FORMATS:
            self.write_config({"youtube_auto_format": fmt})
            self.assertEqual(backend._auto_format(app.load_config()), fmt)

    def test_unknown_format_falls_back_to_narration(self):
        self.write_config({"youtube_auto_format": "interpretive-dance"})
        self.assertEqual(backend._auto_format(app.load_config()), "narration")

    def test_song_steps_need_both_the_format_and_the_toggle(self):
        for cfg, wanted in [({"youtube_auto_format": "song", "youtube_auto_song": True}, True),
                            ({"youtube_auto_format": "song", "youtube_auto_song": False}, False),
                            ({"youtube_auto_format": "narration", "youtube_auto_song": True}, False)]:
            self.write_config(cfg)
            self.assertIs(backend._auto_song_needed(app.load_config()), wanted, cfg)


class SongFirstSequenceTests(TempConfigCase):
    """_auto_song_first: what runs, in what order, into which work dir."""

    def setUp(self):
        super().setUp()
        self.wd = self.output_dir / "the-song"
        self.wd.mkdir()
        self.calls = []
        self.song = {"caption": "slow folk waltz", "lyrics": "[Verse]\nthe long way home",
                     "seconds": 60.0}

        def _draft(body):
            self.calls.append("draft")
            self.drafted = body
            (self.wd / "song.json").write_text(json.dumps(self.song))
            return {"work_dir": str(self.wd), "job_id": "job-song"}

        for name, fn in [("song_draft", _draft),
                         ("_do_song_generate", lambda wd: self.calls.append("generate")),
                         ("_do_song_convert", lambda wd, v: self.calls.append(f"convert:{v}"))]:
            p = mock.patch.object(backend, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, cfg_extra=None):
        self.write_config({**SONG_CFG, **(cfg_extra or {})})
        return backend._auto_song_first(
            app.load_config(), title="The Long Way Home", topic="a drive at dusk",
            minutes=1.0, style_name="", n_scenes=0, queue_item_id="q1")

    def test_writes_then_renders_the_track(self):
        out = self._run()
        self.assertEqual(self.calls, ["draft", "generate"])
        self.assertEqual(out, {"work_dir": str(self.wd), "job_id": "job-song"})

    def test_carries_the_queue_slot_so_the_script_links_back(self):
        self._run()
        self.assertEqual(self.drafted.queue_item_id, "q1")

    def test_singing_voice_is_asked_for_but_not_cloned_by_default(self):
        self._run({"youtube_auto_song_voice": "Alice"})
        self.assertEqual(self.drafted.voice, "Alice")
        self.assertEqual(self.calls, ["draft", "generate"])  # no conversion

    def test_revoices_the_track_after_generating_it(self):
        self._run({"youtube_auto_song_voice": "Alice", "youtube_auto_song_revoice": True})
        self.assertEqual(self.calls, ["draft", "generate", "convert:Alice"])

    def test_revoice_without_a_voice_is_a_no_op(self):
        self._run({"youtube_auto_song_revoice": True})
        self.assertEqual(self.calls, ["draft", "generate"])

    def test_a_failed_revoice_keeps_the_sung_original(self):
        with mock.patch.object(backend, "_do_song_convert", side_effect=RuntimeError("no seed-vc")):
            out = self._run({"youtube_auto_song_voice": "Alice",
                             "youtube_auto_song_revoice": True})
        self.assertEqual(out["work_dir"], str(self.wd))
        self.assertEqual(self.calls, ["draft", "generate"])

    def test_critic_rewrites_the_lyrics_before_the_track_is_rendered(self):
        rewritten = {"caption": "slow folk waltz", "lyrics": "[Verse]\nhome"}
        with mock.patch.object(backend.story_mode, "critique_song",
                               side_effect=["cut the second verse", ""]) as judge, \
             mock.patch.object(backend.story_mode, "write_song",
                               return_value=rewritten) as rewrite:
            self._run({"youtube_auto_song_critic_passes": 2})
        self.assertEqual(judge.call_count, 2)          # second pass passed it
        self.assertEqual(rewrite.call_args.kwargs["instruction"], "cut the second verse")
        # The rewrite lands on disk BEFORE the track is rendered from it.
        saved = json.loads((self.wd / "song.json").read_text())
        self.assertEqual(saved["lyrics"], rewritten["lyrics"])
        self.assertEqual(self.calls, ["draft", "generate"])

    def test_critic_off_sings_the_first_draft(self):
        with mock.patch.object(backend.story_mode, "critique_song") as judge:
            self._run({"youtube_auto_song_critic_passes": 0})
        judge.assert_not_called()


class AutoWriteScriptsSongTests(TempConfigCase):
    """_auto_write_scripts driving a music video end to end."""

    def setUp(self):
        super().setUp()
        self.updates = {}
        self.queue = [{"id": "q1", "status": "pending", "final_title": "The Long Way Home",
                       "video_prompt": "a drive at dusk"}]

        def _update(item_id, **kw):
            self.updates.setdefault(item_id, {}).update(kw)
            for row in self.queue:
                if row["id"] == item_id:
                    row.update(kw)
            return True

        for name, fn in [("load_queue", lambda: [dict(q) for q in self.queue]),
                         ("update_queue_item", _update)]:
            p = mock.patch.object(backend.yt, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, cfg_extra=None, song=None):
        self.write_config({**SONG_CFG, **(cfg_extra or {})})
        song = song or {"work_dir": str(self.output_dir / "the-song"), "job_id": "job-song"}
        with mock.patch.object(backend, "_auto_song_first", return_value=song) as first, \
             mock.patch.object(backend, "_do_script_generate",
                               return_value={"job_id": "job-song", "work_dir": song["work_dir"],
                                             "scenes": [{}, {}], "style": "", "music_desc": "",
                                             "style_name": "Default"}) as gen:
            written = backend._auto_write_scripts(app.load_config())
        return written, first, gen

    def test_the_script_is_drafted_from_the_generated_song(self):
        written, first, gen = self._run()
        self.assertEqual(written, 1)
        first.assert_called_once()
        body = gen.call_args.args[0]
        self.assertEqual(body.format, "song")
        # The song's work dir, not a fresh one: the story is drafted INTO it,
        # alongside the track its scene windows are timed against.
        self.assertEqual(body.work_dir, str(self.output_dir / "the-song"))
        self.assertTrue(self.updates["q1"]["script_ready"])

    def test_other_formats_skip_the_song_step(self):
        _, first, gen = self._run({"youtube_auto_format": "silent"})
        first.assert_not_called()
        self.assertEqual(gen.call_args.args[0].format, "silent")
        self.assertEqual(gen.call_args.args[0].work_dir, "")

    def test_review_gate_parks_the_song_and_writes_no_script(self):
        written, first, gen = self._run({"youtube_auto_song_approve": False})
        self.assertEqual(written, 0)
        first.assert_called_once()
        gen.assert_not_called()          # no story, no scenes, no render
        parked = self.updates["q1"]
        self.assertTrue(parked["song_parked"])
        self.assertFalse(parked["script_ready"])
        self.assertFalse(parked["approved"])
        # Linked so the Song tab can open it and carry the flow on by hand.
        self.assertEqual(parked["work_dir"], str(self.output_dir / "the-song"))
        self.assertEqual(parked["video_job_id"], "job-song")

    def test_a_parked_song_is_not_re_drafted_next_tick(self):
        self._run({"youtube_auto_song_approve": False})
        _, first, gen = self._run({"youtube_auto_song_approve": False})
        first.assert_not_called()
        gen.assert_not_called()


class ParkedSongStartTests(TempConfigCase):
    """A parked song holds its film back even when scripts auto-approve."""

    def setUp(self):
        super().setUp()
        self.write_config({**SONG_CFG, "youtube_auto_song_approve": False,
                           "youtube_auto_start_job": True,
                           "youtube_auto_approve_script": True})

    def _start(self, queue):
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=queue), \
             mock.patch.object(backend.gapp, "_auto_pick_suggestion", return_value=None), \
             mock.patch.object(backend, "_start_queue_item",
                               side_effect=lambda item: {"id": item["id"]}) as start:
            return backend._auto_start_best(), start

    def test_parked_item_is_not_started(self):
        parked = {"id": "q1", "status": "pending", "final_title": "Parked",
                  "song_parked": True, "work_dir": "/tmp/q1", "video_job_id": "job-q1"}
        out, start = self._start([parked])
        self.assertIsNone(out)
        start.assert_not_called()

    def test_a_parked_item_does_not_block_the_one_behind_it(self):
        parked = {"id": "q1", "status": "pending", "final_title": "Parked",
                  "song_parked": True}
        ready = {"id": "q2", "status": "pending", "final_title": "Ready",
                 "script_ready": True, "approved": True,
                 "work_dir": "/tmp/q2", "video_job_id": "job-q2"}
        out, _ = self._start([parked, ready])
        self.assertEqual(out, {"id": "q2"})

    def test_the_song_being_approved_releases_it(self):
        # The Song tab's "Draft the story" links a script to the same slot,
        # which is what clears the hold — song_parked itself is just a marker.
        released = {"id": "q1", "status": "pending", "final_title": "Approved by hand",
                    "song_parked": True, "script_ready": True, "approved": True,
                    "work_dir": "/tmp/q1", "video_job_id": "job-q1"}
        out, _ = self._start([released])
        self.assertEqual(out, {"id": "q1"})


class CritiqueSongTests(unittest.TestCase):
    """The lyric judge itself — verdict in, rewrite instruction out."""

    def _judge(self, reply):
        song = {"caption": "slow folk waltz", "lyrics": "[Verse]\nthe long way home"}
        with mock.patch.object(story_mode, "_load_cfg", return_value={}), \
             mock.patch.object(story_mode, "_call_fn",
                               return_value=lambda *a, **k: reply):
            return story_mode.critique_song(song, 60, topic="a drive at dusk")

    def test_pass_returns_no_instruction(self):
        self.assertEqual(self._judge('{"verdict": "pass", "issues": ""}'), "")

    def test_revise_returns_the_instruction(self):
        self.assertEqual(
            self._judge('{"verdict": "revise", "issues": "cut the second verse"}'),
            "cut the second verse")

    def test_revise_with_no_notes_is_treated_as_a_pass(self):
        self.assertEqual(self._judge('{"verdict": "revise", "issues": ""}'), "")

    def test_a_broken_judge_keeps_the_song(self):
        with mock.patch.object(story_mode, "_load_cfg", return_value={}), \
             mock.patch.object(story_mode, "_call_fn",
                               return_value=mock.Mock(side_effect=RuntimeError("no llm"))):
            self.assertEqual(story_mode.critique_song({"lyrics": "x"}, 60), "")


class SongWordBudgetTests(unittest.TestCase):
    """The judge is given the SAME budget the songwriter was held to."""

    def test_budget_matches_the_songwriter(self):
        for secs in (0, 15, 30, 60, 180):
            n = story_mode._song_seconds(secs)
            self.assertEqual(story_mode._song_word_budget(n), max(10, int(n * 1.2)))

    def test_a_missing_length_falls_back_to_three_minutes(self):
        self.assertEqual(story_mode._song_seconds(0), 180)
        self.assertEqual(story_mode._song_seconds(5), 15)


if __name__ == "__main__":
    unittest.main()


class ReleasedSongHoldTests(TempConfigCase):
    """Auto-approving songs releases the ones already parked on that flag.

    Parking is a review hold, not a one-way door: the whole point of turning
    auto-approve ON is that the songs waiting for it carry on — from the track
    already rendered, not from a second one sung over the top of it.
    """

    def setUp(self):
        super().setUp()
        self.updates = {}
        self.queue = [{"id": "q1", "status": "pending", "final_title": "The Long Way Home",
                       "video_prompt": "a drive at dusk"}]
        self.song_dir = self.output_dir / "the-song"

        def _update(item_id, **kw):
            self.updates.setdefault(item_id, {}).update(kw)
            for row in self.queue:
                if row["id"] == item_id:
                    row.update(kw)
            return True

        for name, fn in [("load_queue", lambda: [dict(q) for q in self.queue]),
                         ("update_queue_item", _update)]:
            p = mock.patch.object(backend.yt, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, cfg_extra=None):
        self.write_config({**SONG_CFG, **(cfg_extra or {})})
        with mock.patch.object(backend, "_auto_song_first",
                               return_value={"work_dir": str(self.song_dir),
                                             "job_id": "job-song"}) as first, \
             mock.patch.object(backend, "_do_script_generate",
                               return_value={"job_id": "job-song", "work_dir": str(self.song_dir),
                                             "scenes": [{}, {}], "style": "", "music_desc": "",
                                             "style_name": "Default"}) as gen:
            written = backend._auto_write_scripts(app.load_config())
        return written, first, gen

    def _park(self):
        self._run({"youtube_auto_song_approve": False})
        self.assertTrue(self.queue[0]["song_parked"])

    def test_turning_auto_approve_on_carries_the_parked_song_on(self):
        self._park()
        self.song_dir.mkdir(parents=True, exist_ok=True)
        written, first, gen = self._run()
        self.assertEqual(written, 1)
        # The track that was parked, not a second one sung over it.
        first.assert_not_called()
        self.assertEqual(gen.call_args.args[0].work_dir, str(self.song_dir))
        self.assertEqual(gen.call_args.args[0].format, "song")
        self.assertTrue(self.updates["q1"]["script_ready"])
        self.assertFalse(self.updates["q1"]["song_parked"])

    def test_a_released_song_whose_folder_is_gone_is_sung_again(self):
        # Nothing left on disk to carry on from (the folder was cleaned up):
        # the hold still lifts, it just costs a fresh song.
        self._park()
        written, first, _ = self._run()
        self.assertEqual(written, 1)
        first.assert_called_once()

    def test_a_style_that_left_the_song_format_keeps_its_parked_items(self):
        # Song approval has nothing to say about a film that is no longer a
        # music video — that one waits for the Song tab.
        self._park()
        self.song_dir.mkdir(parents=True, exist_ok=True)
        written, first, gen = self._run({"youtube_auto_format": "narration"})
        self.assertEqual(written, 0)
        first.assert_not_called()
        gen.assert_not_called()

    def test_the_hold_still_holds_while_songs_are_reviewed(self):
        self._park()
        self.song_dir.mkdir(parents=True, exist_ok=True)
        written, first, gen = self._run({"youtube_auto_song_approve": False})
        self.assertEqual(written, 0)
        first.assert_not_called()
        gen.assert_not_called()


class ReleasedSongStartTests(TempConfigCase):
    """_auto_start_best on an item whose song hold has been lifted."""

    def _start(self, queue, cfg_extra=None):
        self.write_config({**SONG_CFG, "youtube_auto_start_job": True,
                           "youtube_auto_approve_script": True, **(cfg_extra or {})})
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=queue), \
             mock.patch.object(backend.gapp, "_auto_pick_suggestion", return_value=None), \
             mock.patch.object(backend, "_start_queue_item",
                               side_effect=lambda item: {"id": item["id"]}):
            return backend._auto_start_best()

    def _parked(self):
        return {"id": "q1", "status": "pending", "final_title": "Parked",
                "song_parked": True, "work_dir": str(self.output_dir / "the-song"),
                "video_job_id": "job-q1"}

    def test_auto_approving_songs_starts_a_parked_item(self):
        self.assertEqual(self._start([self._parked()]), {"id": "q1"})

    def test_it_still_waits_while_songs_are_reviewed(self):
        self.assertIsNone(
            self._start([self._parked()], {"youtube_auto_song_approve": False}))


class ParkedSongRenderTests(TempConfigCase):
    """_start_queue_item: a parked item renders from the track it already has."""

    def setUp(self):
        super().setUp()
        self.write_config(SONG_CFG)
        self.song_dir = self.output_dir / "the-song"
        self.song_dir.mkdir()
        self.updates = {}
        for name, fn in [
                ("load_queue", lambda: [{"id": "q1", "status": "pending"}]),
                ("update_queue_item",
                 lambda qid, **kw: bool(self.updates.setdefault(qid, {}).update(kw)) or True)]:
            p = mock.patch.object(backend.yt, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def _render(self, item):
        with mock.patch.object(
                backend, "_auto_song_first",
                return_value={"work_dir": str(self.output_dir / "a-second-song"),
                              "job_id": "job-2"}) as first, \
             mock.patch.object(backend, "_do_script_generate",
                               return_value={"job_id": "job-song", "work_dir": str(self.song_dir),
                                             "scenes": [{}], "style": "", "music_desc": "",
                                             "style_name": "Default"}) as gen, \
             mock.patch.object(backend, "start_generation", return_value={}), \
             mock.patch.object(backend, "_link_queue_item_to_work_dir"):
            backend._start_queue_item(item)
        return first, gen

    def test_the_parked_track_is_used_instead_of_a_second_one(self):
        first, gen = self._render({"id": "q1", "final_title": "Parked", "song_parked": True,
                                   "work_dir": str(self.song_dir), "video_job_id": "job-song"})
        first.assert_not_called()
        self.assertEqual(gen.call_args.args[0].work_dir, str(self.song_dir))
        self.assertFalse(self.updates["q1"]["song_parked"])

    def test_an_item_with_no_song_yet_still_gets_one(self):
        first, gen = self._render({"id": "q1", "final_title": "Fresh"})
        first.assert_called_once()
        self.assertEqual(gen.call_args.args[0].work_dir,
                         str(self.output_dir / "a-second-song"))
