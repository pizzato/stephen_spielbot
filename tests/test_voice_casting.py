"""Auto-casting of library voices onto story characters (gender/age matched)."""
import unittest

import app


def _cfg():
    return {"voices": [
        {"name": "Meredith",  "path": "/v/m.wav", "gender": "female", "age": "adult",   "accent": "American"},
        {"name": "Walter",    "path": "/v/w.wav", "gender": "male",   "age": "mature",  "accent": "American"},
        {"name": "Nigel",     "path": "/v/n.wav", "gender": "male",   "age": "adult",   "accent": "British"},
        {"name": "Kara",      "path": "/v/k.wav", "gender": "female", "age": "young",   "accent": "American"},
        {"name": "Dorothea",  "path": "/v/d.wav", "gender": "female", "age": "elderly", "accent": "British"},
        {"name": "David Attenbot", "path": "/v/da.m4a"},  # user voice, no metadata → never auto-cast
    ]}


class VoiceCastingTests(unittest.TestCase):
    def test_gender_and_age_matching(self):
        chars = [
            {"name": "Billy", "gender": "male", "age": "young", "voice": ""},
            {"name": "Granny", "gender": "female", "age": "elderly", "voice": ""},
        ]
        out = app._auto_assign_character_voices(chars, _cfg())
        self.assertIn(out[0]["voice"], ("Nigel", "Walter"))   # male
        self.assertEqual(out[1]["voice"], "Dorothea")          # elderly female exact

    def test_no_duplicate_until_pool_dry(self):
        chars = [{"name": f"M{i}", "gender": "male", "voice": ""} for i in range(3)]
        out = app._auto_assign_character_voices(chars, _cfg())
        voices = [c["voice"] for c in out]
        # two male voices exist; third assignment must reuse rather than fail
        self.assertEqual(set(voices[:2]), {"Walter", "Nigel"})
        self.assertIn(voices[2], {"Walter", "Nigel"})

    def test_narrator_voice_excluded(self):
        chars = [{"name": "X", "gender": "female", "voice": ""}]
        cfg = _cfg()
        cfg["voices"][0]["name"] = "Narratrice"
        out = app._auto_assign_character_voices(chars, cfg, exclude="Narratrice")
        self.assertNotEqual(out[0]["voice"], "Narratrice")

    def test_existing_voice_untouched_and_description_heuristic(self):
        chars = [
            {"name": "Set", "voice": "Kara"},
            {"name": "Duchess", "description": "An elderly woman in a lace gown, her grey hair pinned up.", "voice": ""},
        ]
        out = app._auto_assign_character_voices(chars, _cfg())
        self.assertEqual(out[0]["voice"], "Kara")
        self.assertEqual(out[1]["voice"], "Dorothea")  # female via cues + elderly... age unknown → female pick
        # (Dorothea or Meredith acceptable; exact age unknown) — allow either female
        self.assertIn(out[1]["voice"], ("Dorothea", "Meredith"))

    def test_no_metadata_pool_is_noop(self):
        chars = [{"name": "X", "gender": "male", "voice": ""}]
        out = app._auto_assign_character_voices(chars, {"voices": [{"name": "Plain", "path": "/p.wav"}]})
        self.assertEqual(out[0]["voice"], "")

    def test_norm_characters_keeps_hints(self):
        c = app._norm_characters([{"name": "A", "gender": "Female", "age": "ELDERLY"}])[0]
        self.assertEqual((c["gender"], c["age"]), ("female", "elderly"))


if __name__ == "__main__":
    unittest.main()
