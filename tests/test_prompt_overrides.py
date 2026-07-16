"""Editable prompts with a revertible baseline.

The packaged ``prompts.yaml`` is the baseline and must never be written to; user
edits live in a sparse override file under the config dir. These tests cover the
merge, the sparseness (which is what makes "revert to original" work), and that
the baseline survives every editing path.
"""
import os
import tempfile
import unittest
from pathlib import Path

# Point HOME at a scratch dir BEFORE importing the module — the override path is
# resolved from Path.home() at import time, and a test must never touch the real
# ~/.config/video-generator/prompts.yaml.
os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

from pipeline import prompts as _prompts  # noqa: E402

NAME = "script_claude_initial"
CUSTOM = "You are a pirate. Write the script as a sea shanty."


class PromptOverrideTests(unittest.TestCase):
    def setUp(self):
        _prompts.reset()
        self.baseline = Path(_prompts._PROMPTS_PATH).read_text()

    def tearDown(self):
        _prompts.reset()
        # The shipped file is the only thing standing between a bad edit and an
        # unrecoverable prompt — nothing in the editing path may touch it.
        self.assertEqual(Path(_prompts._PROMPTS_PATH).read_text(), self.baseline)

    def test_override_path_is_isolated_from_the_repo(self):
        self.assertNotIn(str(_prompts._OVERRIDE_PATH), str(_prompts._PROMPTS_PATH))
        self.assertTrue(str(_prompts._OVERRIDE_PATH).endswith("video-generator/prompts.yaml"))

    def test_no_override_reads_the_baseline(self):
        self.assertFalse(_prompts._OVERRIDE_PATH.exists())
        self.assertEqual(_prompts.system(NAME), _prompts.defaults()[NAME]["system"].rstrip("\n"))
        self.assertFalse(any(p["modified"] for p in _prompts.catalogue()))

    def test_saved_edit_wins_and_is_visible_to_readers(self):
        _prompts.save(NAME, {"system": CUSTOM})
        self.assertEqual(_prompts.system(NAME), CUSTOM)
        entry = next(p for p in _prompts.catalogue() if p["name"] == NAME)
        self.assertTrue(entry["modified"])
        self.assertEqual(next(f for f in entry["fields"] if f["key"] == "system")["value"], CUSTOM)

    def test_untouched_fields_still_come_from_the_baseline(self):
        # The point of a per-field merge: overriding `system` must not freeze
        # `user` at today's text — an upgrade should still improve it.
        _prompts.save(NAME, {"system": CUSTOM})
        self.assertEqual(_prompts.user(NAME), _prompts.defaults()[NAME]["user"].rstrip("\n"))
        self.assertNotIn("user", _prompts.overrides()[NAME])

    def test_saving_the_original_text_stores_nothing(self):
        _prompts.save(NAME, {"system": _prompts.defaults()[NAME]["system"]})
        self.assertFalse(_prompts._OVERRIDE_PATH.exists())
        self.assertFalse(next(p for p in _prompts.catalogue() if p["name"] == NAME)["modified"])

    def test_reset_one_prompt_restores_the_original(self):
        _prompts.save(NAME, {"system": CUSTOM})
        _prompts.save("cover_negative", {"value": "no pirates"})
        _prompts.reset(NAME)
        self.assertEqual(_prompts.system(NAME), _prompts.defaults()[NAME]["system"].rstrip("\n"))
        self.assertEqual(_prompts.value("cover_negative"), "no pirates")

    def test_reset_all_removes_the_override_file(self):
        _prompts.save(NAME, {"system": CUSTOM})
        _prompts.reset()
        self.assertFalse(_prompts._OVERRIDE_PATH.exists())
        self.assertEqual(_prompts.system(NAME), _prompts.defaults()[NAME]["system"].rstrip("\n"))

    def test_override_for_an_unknown_prompt_is_ignored(self):
        # An upgrade that renames or drops a prompt leaves a stale override
        # behind; it must not resurrect a dead prompt or break the merge.
        _prompts._OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _prompts._OVERRIDE_PATH.write_text("prompt_from_an_old_version:\n  system: gone\n")
        _prompts.reload()
        self.assertNotIn("prompt_from_an_old_version", _prompts._load())
        with self.assertRaises(KeyError):
            _prompts.system("prompt_from_an_old_version")

    def test_unreadable_override_falls_back_to_the_baseline(self):
        _prompts._OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _prompts._OVERRIDE_PATH.write_text("{{ not: valid: yaml")
        _prompts.reload()
        self.assertEqual(_prompts.system(NAME), _prompts.defaults()[NAME]["system"].rstrip("\n"))

    def test_saving_an_unknown_prompt_is_rejected(self):
        with self.assertRaises(KeyError):
            _prompts.save("no_such_prompt", {"system": "x"})

    def test_round_trip_preserves_exact_text(self):
        # The override is re-read through YAML, so trailing whitespace and block
        # scalars must not mangle a prompt.
        text = "Line one.\n\n  indented line\nLine three with ${title_line}\n"
        _prompts.save(NAME, {"system": text})
        _prompts.reload()
        self.assertEqual(_prompts.overrides()[NAME]["system"], text)

    def test_placeholders_reports_baseline_tokens(self):
        entry = next(p for p in _prompts.catalogue() if p["name"] == NAME)
        user_field = next(f for f in entry["fields"] if f["key"] == "user")
        self.assertIn("n_scenes", user_field["placeholders"])
        # First-seen order, de-duplicated.
        self.assertEqual(_prompts.placeholders("${a} ${b} ${a}"), ["a", "b"])

    def test_static_value_prompts_are_editable(self):
        _prompts.save("video_negative", {"value": "blurry, watermark"})
        self.assertEqual(_prompts.value("video_negative"), "blurry, watermark")
        _prompts.reset("video_negative")
        self.assertEqual(_prompts.value("video_negative"),
                         _prompts.defaults()["video_negative"]["value"].strip())


if __name__ == "__main__":
    unittest.main()
