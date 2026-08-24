"""The H3 prompt experiment variants each isolate exactly their one idea.

Every variant is built from the same complex demo scene, so these tests pin
the properties the A/B comparison depends on: the baseline is byte-identical
to build_h3_prompt, each transform changes only its own axis, and the schema
variants carry the same content in H3's native six-section container.
"""
import os
import tempfile
import unittest

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

from pipeline import h3_prompt_experiments as exp  # noqa: E402
from pipeline import performance  # noqa: E402

_SCHEMA_FIELDS = ("subject_definitions:", "summary:", "retention_analysis:",
                  "detailed_description:", "overall_soundscape:",
                  "non_diegetic_music:")


def _variants():
    d = exp.demo_scene()
    return d, exp.build_variants(d["meta"], style_note=d["style_note"],
                                 picture_names=d["picture_names"],
                                 audio_names=d["audio_names"])


class VariantMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo, cls.v = _variants()

    def test_baseline_is_untouched_build_h3_prompt(self):
        d = self.demo
        self.assertEqual(
            self.v["baseline"],
            performance.build_h3_prompt(d["meta"], style_note=d["style_note"],
                                        picture_names=d["picture_names"],
                                        audio_names=d["audio_names"]))

    def test_all_variants_present(self):
        self.assertEqual(tuple(self.v), exp.VARIANTS)

    def test_positive_audio_removes_only_audio_prohibitions(self):
        p = self.v["positive-audio"]
        self.assertNotIn("no music", p)
        self.assertIn("The complete soundtrack is this ambience", p)
        # Visual negatives and the identity contract stay word for word.
        self.assertIn("Do not add subtitles", p)
        self.assertIn("no voice swaps", p)
        self.assertIn("no watermark", p)
        # Everything outside the two audio sections is untouched.
        for name in ("DIALOGUE", "SCREEN GEOGRAPHY", "CAMERA", "SCENE"):
            self.assertEqual(exp.section(p, name),
                             exp.section(self.v["baseline"], name))

    def test_positive_audio_silent_scene_states_the_positive(self):
        meta = {**self.demo["meta"], "lines": []}
        base = performance.build_h3_prompt(meta)
        p = exp.apply_positive_audio(base, meta)
        self.assertNotIn("No speech and no voices at all", p)
        self.assertIn("this ambience alone", p)

    def test_dialogue_markup_wraps_lines_and_keeps_lips_lock(self):
        p = self.v["dialogue-markup"]
        self.assertEqual(p.count("<d>[English]"), 3)
        self.assertEqual(p.count("</d>"), 3)
        self.assertNotIn("says exactly", p)
        self.assertEqual(p.count("lips close and all mouth movement stops"), 3)
        # Only the [DIALOGUE] section differs from baseline.
        for name in ("REFERENCE USE", "IDENTITY LOCKS", "NEGATIVES", "CAMERA"):
            self.assertEqual(exp.section(p, name),
                             exp.section(self.v["baseline"], name))

    def test_schema_has_the_six_native_sections_in_order(self):
        p = self.v["schema"]
        positions = [p.find(f) for f in _SCHEMA_FIELDS]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))
        self.assertIn("[Shot 1]", p)
        self.assertIn("fully_preserved", p)
        self.assertNotIn("[REFERENCE USE]", p)
        self.assertNotIn("[NEGATIVES]", p)
        self.assertIn("non_diegetic_music:\nN/A", p)

    def test_schema_keeps_names_and_content(self):
        p = self.v["schema"]
        self.assertIn("MARA slides the folded manifest", p)
        self.assertIn("wire-rim glasses", p)          # hint carried
        self.assertIn("<Picture 4>", p)               # wardrobe folded in
        self.assertIn("partially_preserved", p)       # continuity picture
        self.assertIn("Then the company sank that boat.", p)
        self.assertNotIn("<Subject 1> slides", p)     # labels off

    def test_labels_substitute_prose_but_never_spoken_names(self):
        p = self.v["schema-labels"]
        body = p.split("detailed_description:")[1]
        self.assertIn("<Subject 1> slides the folded manifest", body)
        self.assertIn("<Subject 2> reads it", body)
        # ELLIS is spoken aloud in line 1 — dialogue text stays verbatim.
        self.assertIn("Ellis, look at the third column", body)
        # The label<->name binding survives in the definition sections.
        self.assertIn("<Subject 1> is MARA", p)
        self.assertIn("(MARA, appears in [Shot 1])", p)

    def test_reinforce_repeats_appearance_in_the_description(self):
        base, p = self.v["schema"], self.v["schema-reinforce"]
        self.assertGreater(exp.description_word_count(p),
                           exp.description_word_count(base))
        self.assertIn("grey-streaked dark hair pinned back",
                      p.split("detailed_description:")[1])

    def test_native_full_composes_every_axis(self):
        p = self.v["native-full"]
        for f in _SCHEMA_FIELDS:
            self.assertIn(f, p)
        self.assertIn("<d>[English]", p)
        self.assertIn("<Subject 1> slides", p)
        self.assertNotIn("no music", p)
        self.assertIn("Do not add subtitles", p)

    def test_baseline_tail_adds_only_the_end_state(self):
        p, base = self.v["baseline-tail"], self.v["baseline"]
        self.assertIn("no one speaks again", p)
        for name in ("REFERENCE USE", "SCREEN GEOGRAPHY", "NEGATIVES", "CAMERA"):
            self.assertEqual(exp.section(p, name), exp.section(base, name))

    def test_schema_sid_binds_every_speaker(self):
        p = self.v["schema-sid"]
        self.assertIn("MARA (S1), the person", p)
        self.assertIn("ELLIS (S2), the person", p)
        self.assertIn("MARA (S1) says exactly", p)
        self.assertIn("speaks only in this voice", p)
        # sid off leaves no IDs behind.
        self.assertNotIn("(S1)", self.v["schema"])

    def test_native_v2_composes_sid_labels_markup_tail(self):
        p = self.v["native-v2"]
        self.assertIn("<Subject 1> (S1) says", p)
        self.assertIn("<d>[English]", p)
        self.assertIn("no one speaks again", p)

    def test_singing_scenes_are_rejected(self):
        with self.assertRaises(ValueError):
            exp.build_variants({**self.demo["meta"], "singing": True})


class LintTests(unittest.TestCase):
    def test_quality_tokens_flagged(self):
        out = exp.lint_prompt("A cinematic masterpiece shot in 4k.", {})
        self.assertTrue(any("quality tokens" in f for f in out))

    def test_voice_swaps_is_not_an_audio_prohibition(self):
        out = exp.lint_prompt("no voice swaps, no broken eyelines", {})
        self.assertFalse(any("prohibition" in f for f in out))
        out = exp.lint_prompt("no voices at all", {})
        self.assertTrue(any("prohibition" in f for f in out))

    def test_short_schema_description_flagged(self):
        p = ("subject_definitions:\nx\n\nsummary:\ny\n\nretention_analysis:\nz"
             "\n\ndetailed_description:\nshort body\n\noverall_soundscape:\nq"
             "\n\nnon_diegetic_music:\nN/A")
        out = exp.lint_prompt(p, {})
        self.assertTrue(any("350–500" in f for f in out))

    def test_dialogue_pace_flagged(self):
        meta = {"lines": [{"speaker": "A", "delivery": "fast",
                           "text": " ".join(["word"] * 60)}]}
        out = exp.lint_prompt("x", meta)
        self.assertTrue(any("w/s" in f for f in out))


if __name__ == "__main__":
    unittest.main()
