"""Per-style automation: a global baseline every style inherits and overrides.

Automation used to be one set of switches for the whole machine. Now each style
can differ — one channel prepares scripts for review while another renders music
videos end to end — through a sparse `automation` dict on the style, resolved
through the parent chain over the flat `youtube_auto_*` globals.

The distinction that matters: these flat keys are NOT the STYLE_FIELD_TO_FLAT
mirror of the default style. They are a baseline of their own, so a second root
style inherits them too.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app
import webapp.backend.main as backend
from tests.test_automation_start import TempConfigCase


def _styles(*rows):
    """A styles list of {name, parent?, automation?} rows only — every other
    field is filled in by _ensure_styles on load."""
    return [dict(r) for r in rows]


class AutomationSettingsTests(TempConfigCase):

    def _cfg(self, **kw):
        self.write_config(kw)
        return app.load_config()

    def test_a_style_with_no_overrides_is_the_global(self):
        cfg = self._cfg(youtube_auto_format="song", youtube_auto_song=True,
                        styles=_styles({"name": "Docs"}), default_style="Docs")
        auto = app.automation_settings(cfg, "Docs")
        self.assertEqual(auto["auto_format"], "song")
        self.assertTrue(auto["auto_song"])

    def test_a_style_overrides_only_what_it_sets(self):
        cfg = self._cfg(youtube_auto_format="narration", youtube_auto_critic=True,
                        styles=_styles({"name": "Docs"},
                                       {"name": "Bangers", "automation": {"auto_format": "song"}}),
                        default_style="Docs")
        docs = app.automation_settings(cfg, "Docs")
        bangers = app.automation_settings(cfg, "Bangers")
        self.assertEqual(docs["auto_format"], "narration")
        self.assertEqual(bangers["auto_format"], "song")
        # Untouched flags still come from the global, not from the default style.
        self.assertTrue(bangers["auto_critic"])

    def test_a_second_root_style_still_inherits_the_global(self):
        # The whole reason automation is not part of STYLE_FIELD_TO_FLAT: those
        # flat keys mirror the DEFAULT style, so a root style would be dense and
        # never follow a global at all.
        cfg = self._cfg(youtube_auto_write_scripts=True,
                        styles=_styles({"name": "Docs"}, {"name": "Bangers"}),
                        default_style="Docs")
        self.assertTrue(app.automation_settings(cfg, "Bangers")["auto_write_scripts"])

    def test_default_style_overrides_do_not_leak_into_the_global(self):
        cfg = self._cfg(youtube_auto_format="narration",
                        styles=_styles({"name": "Docs", "automation": {"auto_format": "song"}},
                                       {"name": "Bangers"}),
                        default_style="Docs")
        self.assertEqual(app.automation_settings(cfg, "Docs")["auto_format"], "song")
        self.assertEqual(app.automation_settings(cfg, "Bangers")["auto_format"], "narration")
        self.assertEqual(app.load_config()["youtube_auto_format"], "narration")

    def test_a_child_inherits_its_parents_overrides(self):
        cfg = self._cfg(youtube_auto_format="narration",
                        styles=_styles({"name": "Docs", "automation": {"auto_format": "song",
                                                                       "auto_song": True}},
                                       {"name": "Shorts", "parent": "Docs"}),
                        default_style="Docs")
        auto = app.automation_settings(cfg, "Shorts")
        self.assertEqual(auto["auto_format"], "song")
        self.assertTrue(auto["auto_song"])

    def test_a_child_overrides_its_parent_field_by_field(self):
        cfg = self._cfg(styles=_styles(
            {"name": "Docs", "automation": {"auto_format": "song", "auto_song": True}},
            {"name": "Shorts", "parent": "Docs", "automation": {"auto_song": False}}),
            default_style="Docs")
        auto = app.automation_settings(cfg, "Shorts")
        self.assertEqual(auto["auto_format"], "song")   # still the parent's
        self.assertFalse(auto["auto_song"])             # its own

    def test_an_unknown_style_resolves_like_the_default_one(self):
        cfg = self._cfg(styles=_styles({"name": "Docs", "automation": {"auto_critic": True}},
                                       {"name": "Bangers"}),
                        default_style="Docs")
        for name in ("", "Nope"):
            self.assertTrue(app.automation_settings(cfg, name)["auto_critic"], name)

    def test_values_are_coerced_on_read(self):
        cfg = self._cfg(styles=_styles({"name": "Docs", "automation": {
            "auto_format": "interpretive-dance", "auto_critic_passes": "99",
            "auto_song_critic_passes": -4, "auto_song_voice": "  Lucy  ",
            "auto_song": "yes"}}), default_style="Docs")
        auto = app.automation_settings(cfg, "Docs")
        self.assertEqual(auto["auto_format"], "narration")
        self.assertEqual(auto["auto_critic_passes"], 5)      # clamped
        self.assertEqual(auto["auto_song_critic_passes"], 0)
        self.assertEqual(auto["auto_song_voice"], "Lucy")
        self.assertIs(auto["auto_song"], True)


class EnsureStylesAutomationTests(TempConfigCase):

    def test_overrides_survive_a_save_and_stay_sparse(self):
        self.write_config({"styles": _styles(
            {"name": "Docs"},
            {"name": "Bangers", "automation": {"auto_format": "song"}}),
            "default_style": "Docs"})
        rows = {s["name"]: s for s in app.load_config()["styles"]}
        self.assertEqual(rows["Bangers"]["automation"], {"auto_format": "song"})
        # Never densified: an absent flag must keep following the global, not
        # freeze today's value onto the style.
        self.assertNotIn("automation", rows["Docs"])
        self.assertEqual(list(rows["Bangers"]["automation"]), ["auto_format"])

    def test_unknown_and_malformed_overrides_are_dropped(self):
        self.write_config({"styles": _styles(
            {"name": "Docs", "automation": {"auto_song": True, "publish_everything": True}},
            {"name": "Junk", "automation": "not a dict"}),
            "default_style": "Docs"})
        rows = {s["name"]: s for s in app.load_config()["styles"]}
        self.assertEqual(rows["Docs"]["automation"], {"auto_song": True})
        self.assertNotIn("automation", rows["Junk"])


class EnabledAnywhereTests(TempConfigCase):
    """The tick has no single style to resolve against, so it asks this."""

    def test_true_when_the_global_is_on(self):
        self.write_config({"youtube_auto_start_job": True,
                           "styles": _styles({"name": "Docs"}), "default_style": "Docs"})
        self.assertTrue(app.automation_enabled_anywhere(app.load_config(), "auto_start_job"))

    def test_true_when_only_one_style_overrides_it_on(self):
        self.write_config({"youtube_auto_start_job": False,
                           "styles": _styles({"name": "Docs"},
                                             {"name": "Bangers", "automation": {"auto_start_job": True}}),
                           "default_style": "Docs"})
        self.assertTrue(app.automation_enabled_anywhere(app.load_config(), "auto_start_job"))

    def test_false_when_nothing_enables_it(self):
        self.write_config({"youtube_auto_start_job": False,
                           "styles": _styles({"name": "Docs", "automation": {"auto_critic": True}}),
                           "default_style": "Docs"})
        self.assertFalse(app.automation_enabled_anywhere(app.load_config(), "auto_start_job"))


class PerStyleWriteScriptsTests(TempConfigCase):
    """_auto_write_scripts resolves every flag against the ITEM's style."""

    def setUp(self):
        super().setUp()
        self.write_config({
            "youtube_auto_write_scripts": False,
            "youtube_auto_format": "narration",
            "styles": _styles(
                {"name": "Docs"},
                {"name": "Bangers", "automation": {"auto_write_scripts": True,
                                                   "auto_format": "song", "auto_song": True,
                                                   "auto_song_approve": True}}),
            "default_style": "Docs"})
        self.queue = [
            {"id": "q1", "status": "pending", "final_title": "A doc", "gen_style_name": "Docs"},
            {"id": "q2", "status": "pending", "final_title": "A banger", "gen_style_name": "Bangers"},
        ]

    def _run(self):
        with mock.patch.object(backend.yt, "load_queue", return_value=[dict(q) for q in self.queue]), \
             mock.patch.object(backend.yt, "update_queue_item", return_value=True), \
             mock.patch.object(backend, "_auto_song_first",
                               return_value={"work_dir": str(self.output_dir / "song"),
                                             "job_id": "job-song"}) as song, \
             mock.patch.object(backend, "_do_script_generate",
                               return_value={"job_id": "j", "work_dir": "w", "scenes": [],
                                             "style": "", "music_desc": "", "style_name": ""}) as gen:
            written = backend._auto_write_scripts(app.load_config())
        return written, song, gen

    def test_only_the_style_that_opted_in_gets_a_script(self):
        written, _, gen = self._run()
        self.assertEqual(written, 1)
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(gen.call_args.args[0].video_title, "A banger")

    def test_that_style_gets_its_own_format_and_song_step(self):
        _, song, gen = self._run()
        song.assert_called_once()
        self.assertEqual(gen.call_args.args[0].format, "song")


class PerStyleStartTests(TempConfigCase):
    """_auto_start_best asks each item's own style whether it may start."""

    def setUp(self):
        super().setUp()
        self.write_config({
            "youtube_auto_start_job": False,
            "youtube_auto_approve_script": False,
            "styles": _styles({"name": "Docs"},
                              {"name": "Bangers", "automation": {"auto_start_job": True,
                                                                 "auto_approve_script": True}}),
            "default_style": "Docs"})

    def _start(self, queue):
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=queue), \
             mock.patch.object(backend.gapp, "_auto_pick_suggestion", return_value=None), \
             mock.patch.object(backend, "_start_queue_item",
                               side_effect=lambda item: {"id": item["id"]}):
            return backend._auto_start_best()

    def test_a_style_that_does_not_auto_start_is_skipped(self):
        # Script-less AND ahead in the queue: with the old global flags this
        # would have been the item that ran.
        docs = {"id": "q1", "status": "pending", "final_title": "A doc", "gen_style_name": "Docs"}
        banger = {"id": "q2", "status": "pending", "final_title": "A banger",
                  "gen_style_name": "Bangers"}
        self.assertEqual(self._start([docs, banger]), {"id": "q2"})

    def test_an_approved_doc_still_waits_when_its_style_never_auto_starts(self):
        docs = {"id": "q1", "status": "pending", "final_title": "A doc", "gen_style_name": "Docs",
                "approved": True, "script_ready": True, "work_dir": "/tmp/q1",
                "video_job_id": "job-q1"}
        self.assertIsNone(self._start([docs]))

    def test_a_style_can_auto_start_while_keeping_the_review_gate(self):
        self.write_config({
            "youtube_auto_start_job": False, "youtube_auto_approve_script": False,
            "styles": _styles({"name": "Docs", "automation": {"auto_start_job": True}}),
            "default_style": "Docs"})
        unapproved = {"id": "q1", "status": "pending", "final_title": "Unreviewed",
                      "gen_style_name": "Docs", "script_ready": True,
                      "work_dir": "/tmp/q1", "video_job_id": "job-q1"}
        approved = {**unapproved, "id": "q2", "approved": True}
        self.assertIsNone(self._start([unapproved]))
        self.assertEqual(self._start([unapproved, approved]), {"id": "q2"})


class AutoFeedStylesTests(TempConfigCase):
    """_auto_feed_styles: which styles automation may INVENT ideas for.

    Per-style ai-ideas plus the review-gate closure: an invented idea has no
    reviewed script, so only styles whose own automation auto-approves and
    auto-starts are ever fed — a review-mode style must not ride on another
    style's auto-approve (the old global gate let it)."""

    def _cfg(self, **kw):
        self.write_config(kw)
        return app.load_config()

    def test_style_scoped_ai_ideas_feeds_only_that_style(self):
        # Global ai-ideas OFF: one style opts in on its own.
        cfg = self._cfg(
            youtube_auto_start_job=True,
            styles=_styles({"name": "Docs"},
                           {"name": "H3", "automation": {"auto_ai_ideas": True,
                                                         "auto_approve_script": True}}),
            default_style="Docs")
        self.assertEqual(app._auto_feed_styles(cfg), ["H3"])
        self.assertTrue(app.automation_enabled_anywhere(cfg, "auto_ai_ideas"))

    def test_review_mode_style_is_never_fed(self):
        # Global ai-ideas ON, but only H3 auto-approves: Docs stays in review
        # mode and must not receive invented (unreviewed) films.
        cfg = self._cfg(
            youtube_auto_start_job=True, youtube_auto_ai_ideas=True,
            styles=_styles({"name": "Docs"},
                           {"name": "H3", "automation": {"auto_approve_script": True}}),
            default_style="Docs")
        self.assertEqual(app._auto_feed_styles(cfg), ["H3"])

    def test_auto_pick_exclude_still_wins(self):
        cfg = self._cfg(
            youtube_auto_start_job=True, youtube_auto_ai_ideas=True,
            styles=_styles({"name": "H3", "auto_pick_exclude": True,
                            "automation": {"auto_approve_script": True}}),
            default_style="H3")
        self.assertEqual(app._auto_feed_styles(cfg), [])

    def test_style_without_auto_start_is_not_fed(self):
        cfg = self._cfg(
            youtube_auto_ai_ideas=True, youtube_auto_approve_script=True,
            styles=_styles({"name": "H3"}), default_style="H3")
        self.assertEqual(app._auto_feed_styles(cfg), [])


if __name__ == "__main__":
    unittest.main()
