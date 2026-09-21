"""Scene staging casts the story's own people, never invented ones.

"Ink Runs Blacker Than Blood" defined one character, Ash Delgado, and the divide
then staged six of its sixteen scenes around Jett Vargas, Bruno Kane and Jax
Moreira — names that exist in no catalogue and no story. They had no portrait,
so each scene rendered a different stranger.

The cause was the roster: _build_dialogue_note owns the schema for a scene's
"cast", but _do_story_divide only ever handed it the *catalogue* characters the
brief named by hand (normally none). With an empty roster the closed-cast rule
had nothing to close over.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import webapp.backend.main as backend  # noqa: E402
from test_story_endpoints import _fake_scenes, _fake_story  # noqa: E402
from test_styles import TempConfigCase, _style  # noqa: E402


class CastRosterNoteTests(unittest.TestCase):
    def test_roster_closes_the_cast_not_only_the_speakers(self):
        note = backend._build_dialogue_note("dialogue", ["Ash Delgado"])
        self.assertIn("Ash Delgado", note)
        # The rule has to reach "cast", not just "speaker": a song film has no
        # speakers at all, so a speakers-only rule never binds anything.
        self.assertIn('"cast"', note)
        self.assertIn("Never invent a person", note)

    def test_song_lead_performer_comes_from_the_roster(self):
        note = backend._build_dialogue_note("song", ["Ash Delgado"])
        self.assertIn("ONE of the people named above", note)
        self.assertIn("never a newly invented singer", note)
        # Still a music video staged from portraits.
        self.assertIn("MUSIC VIDEO", note)
        self.assertIn("silent scenes are PERFORMED", note)

    def test_no_characters_at_all_keeps_the_open_wording(self):
        # An abstract explainer has nobody to close over — the writer still has
        # to be allowed to find the story's own recurring subjects.
        note = backend._build_dialogue_note("dialogue", [])
        self.assertIn("the story's recurring characters", note)
        self.assertNotIn("EXACTLY THESE", note)


class CastRosterWiringTests(TempConfigCase):
    """The divide must hand the note the story's cast, not just the catalogue's."""

    def setUp(self):
        super().setUp()
        self.write_config({
            "styles": [_style("Hero")],
            "default_style": "Hero",
            "characters": [],
            "characters_migrated_v2": True,
        })
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        mock.patch.object(backend, "_describe_in_background").start()
        self.addCleanup(mock.patch.stopall)

    def _divide_with(self, story_characters):
        """Run a real divide and return the roster the staging note was given."""
        body = backend.GenerateScriptBody(video_title="Ink Runs Blacker Than Blood",
                                          topic="A tattooist bleeds ink",
                                          n_scenes=4, style_name="Hero")
        story = _fake_story(4)
        story["characters"] = story_characters
        with mock.patch.object(backend.story_mode, "generate_story",
                               return_value=story):
            draft = backend._do_story_generate(body)
        seen = {}
        real_note = backend._build_dialogue_note

        def spy(fmt, cast_names, **kw):
            seen["names"] = list(cast_names)
            return real_note(fmt, cast_names, **kw)

        with mock.patch.object(backend.story_mode, "divide_story",
                               return_value=(_fake_scenes(4), "m", "st", [])), \
             mock.patch.object(backend, "_build_dialogue_note", spy):
            backend._do_story_divide(backend.DivideStoryBody(
                work_dir=draft["work_dir"], style_name="Hero"))
        return seen.get("names")

    def test_story_characters_reach_the_note(self):
        # The failing case: no catalogue opt-in, one story character. Before the
        # fix the roster was empty and the writer invented its own people.
        self.assertEqual(self._divide_with([{"name": "Ash Delgado"}]),
                         ["Ash Delgado"])

    def test_a_story_with_nobody_still_divides(self):
        self.assertEqual(self._divide_with([]), [])

    def test_malformed_entries_are_dropped(self):
        self.assertEqual(
            self._divide_with([{"name": "Ash Delgado"}, "junk", {}, {"name": "  "}]),
            ["Ash Delgado"])


if __name__ == "__main__":
    unittest.main()
