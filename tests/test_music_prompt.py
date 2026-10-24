"""MiniMax uses detailed musical conditioning and the user's exact vocalist."""
import json
import unittest
from unittest import mock

from pipeline import music_prompt, story


SOUND = "Global Metadata:\ntechno / drum & bass, 160 BPM\n\nArrangement:\nRolling breaks and sub-bass."
VOCAL = "young male vocalist, Swiss German, strong German accent"


class MusicPromptTests(unittest.TestCase):
    def test_vocalist_replaces_a_stale_voice_inside_structured_sound(self):
        sound = SOUND.replace("Arrangement:", "Vocal Details:\nfemale American voice\n\nArrangement:")
        caption = music_prompt.song_caption(sound, VOCAL, "minimax-music3")
        self.assertEqual(music_prompt.sections(caption), {
            "global metadata": "techno / drum & bass, 160 BPM",
            "vocal details": VOCAL,
            "arrangement": "Rolling breaks and sub-bass.",
        })
        self.assertNotIn("female American", caption)

    def test_markdown_headings_work_and_an_existing_voice_is_kept_without_an_override(self):
        sound = SOUND.replace("Global Metadata:", "## Global Metadata").replace(
            "Arrangement:", "### Vocal Details\nA male baritone.\n\n### Arrangement")
        caption = music_prompt.song_caption(sound, "", "minimax-music3")
        self.assertEqual(music_prompt.sections(caption)["vocal details"], "A male baritone.")
        self.assertIn("Rolling breaks and sub-bass.", caption)

    def test_ace_and_legacy_continuations_keep_their_original_tags(self):
        for engine, sound in (("ace-step", SOUND), ("minimax-music3", "techno")):
            self.assertEqual(music_prompt.song_caption(sound, VOCAL, engine), f"{sound}, {VOCAL}")

    def test_notes_before_the_headings_are_preserved(self):
        caption = music_prompt.song_caption("No piano.\n\n" + SOUND, VOCAL, "minimax-music3")
        self.assertIn("No piano.", music_prompt.sections(caption)["global metadata"])

    def test_expansion_preserves_the_brief_and_uses_the_lyric_sections(self):
        brief = "techno, drum & bass, fast and slow, no piano"
        lyrics = "[Verse]\nWarm machine\n[Chorus]\nYou hum so low"
        result = {"global_metadata": "Dark electronic club production.",
                  "arrangement": "Half-time verse; fast breakbeat chorus."}
        call = mock.Mock(return_value=json.dumps(result))
        with mock.patch.object(story, "_load_cfg", return_value={}), \
             mock.patch.object(story, "_call_fn", return_value=call):
            sound = story.expand_song_sound(brief, lyrics, VOCAL)
        self.assertTrue(music_prompt.is_structured(sound))
        self.assertIn(brief, sound)
        self.assertNotIn(VOCAL, sound)  # supplied verbatim by song_caption instead
        # The arranger's prompt receives the actual section order and voice constraints.
        self.assertIn(brief, call.call_args.args[1])
        self.assertIn(lyrics, call.call_args.args[1])
        self.assertIn(VOCAL, call.call_args.args[1])
        caption = music_prompt.song_caption(sound, VOCAL, "minimax-music3")
        self.assertEqual(music_prompt.sections(caption)["vocal details"], VOCAL)

    def test_a_saved_or_hand_written_arrangement_is_not_rewritten(self):
        with mock.patch.object(story, "_call_fn") as call:
            self.assertEqual(story.expand_song_sound(SOUND, "[Verse]\nwords", VOCAL), SOUND)
        call.assert_not_called()

    def test_incomplete_arrangements_fail_before_music_generation(self):
        with mock.patch.object(story, "_load_cfg", return_value={}), \
             mock.patch.object(story, "_call_fn", return_value=lambda *a, **k: '{"global_metadata":"techno"}'):
            with self.assertRaisesRegex(RuntimeError, "arrangement"):
                story.expand_song_sound("techno", "[Verse]\nwords", VOCAL)


if __name__ == "__main__":
    unittest.main()
