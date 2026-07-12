"""Data-model foundation for dialogue/performance scenes (additive, back-compat).

Locks the invariant: a Scene built the classic way is narration, and the new
per-character voice field defaults empty — so existing scripts/characters are
unchanged.
"""
import unittest
from pathlib import Path

import app
from pipeline.llm import Scene


class DialogueSceneModelTests(unittest.TestCase):
    def test_scene_defaults_to_narration(self):
        s = Scene(id=1, title="t", image_prompt="i", video_prompt="v", narration="n")
        self.assertEqual(s.mode, "narration")
        self.assertEqual(s.lines, [])
        self.assertEqual(s.duration, 0.0)

    def test_scene_lines_not_shared(self):
        a = Scene(id=1, title="", image_prompt="", video_prompt="", narration="")
        b = Scene(id=2, title="", image_prompt="", video_prompt="", narration="")
        a.lines.append({"speaker": "X", "text": "hi"})
        self.assertEqual(b.lines, [])  # default_factory, not a shared list

    def test_dialogue_scene_fields(self):
        s = Scene(
            id=3, title="", image_prompt="", video_prompt="", narration="",
            mode="dialogue", lines=[{"speaker": "Kinho", "text": "Hi"}],
        )
        self.assertEqual(s.mode, "dialogue")
        self.assertEqual(s.lines[0]["speaker"], "Kinho")

    def test_character_voice_field_defaults_empty(self):
        chars = app._norm_characters([{"name": "Kinho"}])
        self.assertEqual(chars[0].get("voice"), "")

    def test_character_voice_preserved(self):
        chars = app._norm_characters([{"name": "Kinho", "voice": "Luiz"}])
        self.assertEqual(chars[0]["voice"], "Luiz")

    def test_script_character_voice_save_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            chars = app.add_script_character(wd, "Kinho")
            cid = chars[0]["id"]
            self.assertEqual(chars[0].get("voice"), "")  # default empty
            app.update_script_character(wd, cid, voice="Luiz")
            self.assertEqual(app._read_script_characters(wd)[0]["voice"], "Luiz")


if __name__ == "__main__":
    unittest.main()
