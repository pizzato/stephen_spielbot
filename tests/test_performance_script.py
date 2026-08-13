"""Acted scenes: the prompt they assemble into, and the shape they take."""
import unittest

from pipeline import performance


class PromptAssemblyTests(unittest.TestCase):
    def _meta(self, **over):
        meta = {
            "seconds": 10,
            "setting": "a rainforest clearing at dusk",
            "camera": "locked off at chest height",
            "soundscape": "cicadas throughout, one branch snap at 7s",
            "beats": [{"t0": 0, "t1": 4, "action": "CHICO faces DARLY across a felled log"},
                      {"t0": 4, "t1": 10, "action": "smoke drifts between them"}],
            "lines": [{"speaker": "CHICO", "delivery": "steady and quiet",
                       "text": "You can burn the trees."},
                      {"speaker": "DARLY", "delivery": "low and flat",
                       "text": "Then remember this one."}],
        }
        meta.update(over)
        return meta

    def test_six_blocks_present(self):
        p = performance.build_h3_prompt(
            self._meta(), style_note="35mm grain",
            picture_names=["CHICO", "DARLY"], audio_names=["CHICO", "DARLY"])
        # Section per concern, references first with bounded authority.
        for header in ("[REFERENCE USE]", "[IDENTITY LOCKS]", "[SCENE]",
                       "[DIALOGUE]", "[SHOT LIST]", "[CAMERA]",
                       "[PRODUCTION SOUND]", "[NEGATIVES]"):
            self.assertIn(header, p)
        self.assertIn("<Picture 1> defines CHICO's appearance", p)
        self.assertIn("<Picture 2> defines DARLY's appearance", p)
        self.assertIn("<Audio 1> defines CHICO's voice only", p)
        self.assertIn("35mm grain", p)
        self.assertIn("[0s-4s]", p)
        self.assertIn('says exactly, steady and quiet: "You can burn the trees."', p)
        self.assertIn("locked off at chest height", p)
        self.assertIn("cicadas throughout", p)
        self.assertIn("Do not add subtitles", p)

    def test_every_line_closes_the_lips(self):
        # Without this the mouth keeps moving through the tail of the clip.
        p = performance.build_h3_prompt(self._meta())
        self.assertEqual(p.count("lips close and all mouth movement stops"), 2)

    def test_music_is_always_refused(self):
        # Performance films carry no score — the model must not invent one.
        p = performance.build_h3_prompt(self._meta(soundscape="rain on a tin roof"))
        self.assertIn("no music of any kind", p)
        self.assertIn("no music.", p)

    def test_refusals_survive_an_empty_scene(self):
        p = performance.build_h3_prompt({})
        self.assertIn("Do not add subtitles", p)
        self.assertIn("no music", p)

    def test_quotes_inside_the_line_are_not_doubled(self):
        p = performance.build_h3_prompt(self._meta(
            lines=[{"speaker": "X", "delivery": "flat", "text": "“hello”"}]))
        self.assertIn('says exactly, flat: "hello"', p)


class NormalizationTests(unittest.TestCase):
    def test_lines_drop_incomplete_entries(self):
        lines = performance.norm_lines([
            {"speaker": "A", "text": "hi"},
            {"speaker": "", "text": "no speaker"},
            {"speaker": "B", "text": ""},
            "not a dict",
        ])
        self.assertEqual([line["speaker"] for line in lines], ["A"])
        self.assertTrue(lines[0]["delivery"])  # defaulted, never blank

    def test_beats_are_clipped_and_ordered(self):
        beats = performance.norm_beats(
            [{"t0": -3, "t1": 99, "action": "x"}, {"t0": 5, "t1": 2, "action": "y"}], 10.0)
        self.assertEqual(beats[0], {"t0": 0.0, "t1": 10.0, "action": "x"})
        self.assertGreater(beats[1]["t1"], beats[1]["t0"])
        self.assertLessEqual(beats[1]["t1"], 10.0)

    def test_beat_starting_past_the_clip_never_inverts(self):
        # Seen from a real generation: a beat at [18s-14s] on a 14 s clip.
        # Clamping only the end leaves t1 < t0, which reaches the model as a
        # backwards time window.
        for raw in ({"t0": 18, "t1": 14, "action": "x"},
                    {"t0": 99, "t1": 0, "action": "x"},
                    {"t0": 14, "t1": 14, "action": "x"}):
            beat = performance.norm_beats([raw], 14.0)[0]
            self.assertLess(beat["t0"], beat["t1"], beat)
            self.assertLessEqual(beat["t1"], 14.0)
            self.assertGreaterEqual(beat["t0"], 0.0)

    def test_beats_never_invert_for_any_input(self):
        for t0 in (-5, 0, 3, 9.5, 10, 40):
            for t1 in (-5, 0, 1, 9.5, 10, 40):
                beat = performance.norm_beats([{"t0": t0, "t1": t1, "action": "x"}], 10.0)[0]
                self.assertLess(beat["t0"], beat["t1"], f"{t0},{t1} -> {beat}")

    def test_speakers_in_first_spoken_order(self):
        lines = performance.norm_lines([
            {"speaker": "B", "text": "1"}, {"speaker": "A", "text": "2"},
            {"speaker": "B", "text": "3"}])
        self.assertEqual(performance.speakers_in(lines), ["B", "A"])

    def test_seconds_clamped_to_the_model_window(self):
        for raw, expected in ((0, performance.SCENE_SECONDS),
                              ("junk", performance.SCENE_SECONDS),
                              (2, performance.MIN_SCENE_SECONDS),
                              (30, performance.MAX_SCENE_SECONDS)):
            self.assertEqual(performance._clamp_seconds(raw), expected)


_RAW_SCENES = [
    {"title": "The felled log", "setting": "a burnt clearing at dusk", "seconds": 10,
     "mode": "dialogue", "cast": ["CHICO", "DARLY"], "camera": "locked off",
     "soundscape": "cicadas, distant fire",
     "beats": [{"t0": 0, "t1": 10, "action": "the two men square up"}],
     "lines": [{"speaker": "CHICO", "delivery": "quiet", "text": "You can burn the trees."},
               {"speaker": "DARLY", "delivery": "flat", "text": "Then remember this one."}]},
    {"title": "After", "setting": "the same clearing, night", "seconds": 30,
     "mode": "dialogue", "cast": ["CHICO"], "camera": "slow push",
     "soundscape": "insects",
     "beats": [{"t0": 0, "t1": 8, "action": "CHICO walks away"}],
     "lines": [{"speaker": "CHICO", "delivery": "tired", "text": "Not tonight."}]},
]


class SceneShapeTests(unittest.TestCase):
    """One divide-prompt object → the Scene the renderer and editor share."""

    def _scenes(self, style_note="handheld 16mm documentary, humid greens"):
        return [performance.scene_from_raw(i, raw, style_note=style_note)
                for i, raw in enumerate(_RAW_SCENES, 1)]

    def test_scene_shape(self):
        s = self._scenes()[0]
        self.assertEqual(s.id, 1)
        self.assertEqual(s.mode, "dialogue")
        # No image engine runs for an acted scene.
        self.assertEqual(s.image_prompt, "")
        # The editable video_prompt IS the assembled H3 prompt.
        self.assertIn("<Picture 1> defines CHICO", s.video_prompt)
        self.assertIn("Do not add subtitles", s.video_prompt)
        self.assertIn("handheld 16mm documentary", s.video_prompt)
        # Narration mirrors the spoken words (captions/description), never TTS input.
        self.assertEqual(s.narration, "You can burn the trees. Then remember this one.")

    def test_metadata_carries_the_structure(self):
        meta = self._scenes()[0].metadata
        self.assertEqual(meta["mode"], "dialogue")
        self.assertEqual(meta["cast"], ["CHICO", "DARLY"])
        self.assertEqual(len(meta["lines"]), 2)
        self.assertEqual(meta["beats"][0]["action"], "the two men square up")
        # Content-driven: two short lines need ~6.6 s, not the LLM's 10.
        self.assertAlmostEqual(meta["seconds"], 6.6, delta=0.2)

    def test_scene_length_comes_from_its_words_not_the_llm_guess(self):
        # Scene 2 claims 30 s for one short line — content decides now, so it
        # lands at the model minimum instead of a padded (babble-prone) clip.
        scene = self._scenes()[1]
        self.assertEqual(scene.metadata["seconds"], performance.MIN_SCENE_SECONDS)
        self.assertEqual(scene.duration, performance.MIN_SCENE_SECONDS)

    def test_is_performance_detects_scenes_and_rows(self):
        self.assertTrue(performance.is_performance(self._scenes()[0]))
        self.assertTrue(performance.is_performance({"metadata": {"mode": "performance"}}))
        self.assertTrue(performance.is_performance({"metadata": {"mode": "dialogue"}}))
        self.assertFalse(performance.is_performance({"metadata": {"mode": "narration"}}))
        self.assertFalse(performance.is_performance({}))


class PunctuationTests(unittest.TestCase):
    def test_camera_and_audio_blocks_do_not_double_punctuate(self):
        # The LLM ends these fields with a period and the blocks append their
        # own, which read as "no push, no zoom.." and "…second 10., no music".
        p = performance.build_h3_prompt({
            "camera": "Slow steady walk, holding him centre-frame.",
            "soundscape": "Light breeze; seagulls at second 4.",
        })
        self.assertNotIn("..", p)
        self.assertIn("Slow steady walk, holding him centre-frame.", p)
        # (No lines in this meta, so the sound block asks for silence — see
        # test_silent_scenes_acted for the talking variant.)
        self.assertIn("seagulls at second 4. No speech and no voices at all, "
                      "no music of any kind.", p)


class IdentityBindingTests(unittest.TestCase):
    """Two people on screen: the model swapped them — one character appearing
    in the other's seat, with the other's voice."""

    def _prompt(self, pics, audios=("Joe", "Kinho")):
        return performance.build_h3_prompt(
            {"lines": [{"speaker": "Joe", "text": "hi"}]},
            picture_names=pics, audio_names=list(audios))

    def test_hint_gives_each_name_something_to_bind_to(self):
        p = self._prompt([{"name": "Joe", "kind": "character", "hint": "Bald, broad build"},
                          {"name": "Kinho", "kind": "character", "hint": "Younger, dark hair"}])
        self.assertIn("Joe's appearance — Joe is Bald, broad build", p)
        self.assertIn("Kinho's appearance — Kinho is Younger, dark hair", p)

    def test_missing_hint_still_names_the_character(self):
        p = self._prompt([{"name": "Joe", "kind": "character"},
                          {"name": "Kinho", "kind": "character"}])
        self.assertIn("<Picture 1> defines Joe's appearance.", p)
        self.assertNotIn("()", p)

    def test_two_people_get_an_explicit_no_swap_instruction(self):
        p = self._prompt([{"name": "Joe", "kind": "character"},
                          {"name": "Kinho", "kind": "character"}])
        self.assertIn("Joe and Kinho are different people", p)
        self.assertIn("never swap or merge", p)
        self.assertIn("Exactly 2 people on screen", p)

    def test_a_lone_character_gets_no_swap_line(self):
        p = self._prompt([{"name": "Joe", "kind": "character"}], audios=("Joe",))
        self.assertNotIn("different people", p)

    def test_a_location_is_not_counted_as_a_person(self):
        p = self._prompt([{"name": "Joe", "kind": "character"},
                          {"name": "The studio", "kind": "location"}], audios=("Joe",))
        self.assertNotIn("different people", p)


class ShotSizingTests(unittest.TestCase):
    def _meta(self, *texts, seconds=14):
        return {"seconds": seconds, "cast": ["A", "B"],
                "lines": [{"speaker": "AB"[i % 2], "text": t} for i, t in enumerate(texts)]}

    def test_shots_are_sized_to_their_words_not_the_scene(self):
        # Oversized shots made the model pad the tail — with speech nobody
        # scripted ("keep your little chovia", heard in a real render).
        long_line = " ".join(["word"] * 25)
        shots = performance.shots_for(self._meta(long_line, "No."))
        self.assertAlmostEqual(shots[0]["seconds"],
                               25 / performance.WORDS_PER_SECOND
                               + performance.SHOT_AIR_SECONDS, delta=0.2)
        self.assertEqual(shots[1]["seconds"], performance.MIN_SCENE_SECONDS)

    def test_establishing_wide_is_optional_and_silent(self):
        meta = self._meta("Hello there Joe.", "Hello back.")
        plain = performance.shots_for(meta)
        self.assertFalse(any(s.get("establishing") for s in plain))
        shots = performance.shots_for(meta, establishing=True)
        wide = shots[0]
        self.assertTrue(wide["establishing"])
        self.assertEqual(wide["lines"], [])            # nobody speaks
        self.assertEqual(wide["cast"], ["A", "B"])     # everyone in frame
        self.assertEqual(len(shots), 3)

    def test_establishing_prompt_promises_silence_and_company(self):
        wide = performance.shots_for(self._meta("Hi.", "Hi."), establishing=True)[0]
        p2 = performance.build_h3_prompt(
            wide, picture_names=[{"name": "A", "kind": "character"},
                                 {"name": "B", "kind": "character"}])
        self.assertIn("A and B are together in the frame, A on the left, B on the right", p2)
        self.assertIn("Nobody speaks", p2)

    def test_solo_prompt_demands_the_face(self):
        shot = performance.shots_for(self._meta("Hello there my friend.", "Hi."))[0]
        p = performance.build_h3_prompt({**shot, "scene_cast": ["A", "B"]})
        self.assertIn("face fully visible to the camera", p)
        self.assertIn("never turning away from it", p)


class SceneLengthTests(unittest.TestCase):
    def _lines(self, *word_counts, alternate=True):
        return [{"speaker": "AB"[i % 2] if alternate else "A",
                 "text": " ".join(["word"] * n)} for i, n in enumerate(word_counts)]

    def test_content_seconds_counts_words_and_turns(self):
        secs = performance.content_seconds({"lines": self._lines(10, 10)})
        self.assertAlmostEqual(secs, 20 / 2.5 + 2.0 + 1.0, delta=0.01)

    def test_overloaded_scene_splits_at_speaker_turns(self):
        # Four long lines cannot fit one 12 s clip — that is where the user's
        # "scene was cut short" came from. More short scenes, never truncation.
        raw = {"title": "Big talk", "setting": "x",
               "lines": self._lines(20, 18, 16, 4)}
        pieces = performance.split_overloaded(raw)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(
                performance.content_seconds(piece), performance.MAX_SCENE_SECONDS + 0.01)
        # Nothing lost, order kept.
        joined = [l["text"] for p in pieces for l in p["lines"]]
        self.assertEqual(joined, [l["text"] for l in raw["lines"]])
        self.assertIn("(cont.)", pieces[1]["title"])

    def test_light_scene_is_not_split(self):
        raw = {"title": "Quick", "lines": self._lines(8, 6)}
        self.assertEqual(len(performance.split_overloaded(raw)), 1)

    def test_render_seconds_never_exceeds_the_model_ceiling(self):
        heavy = {"seconds": 12, "lines": self._lines(25, 25)}
        self.assertEqual(performance.render_seconds(heavy), performance.H3_CEILING_SECONDS)
        light = {"seconds": 10, "lines": self._lines(5)}
        self.assertLessEqual(performance.render_seconds(light), 10.0)

    def test_a_legacy_overlong_scene_is_not_padded_to_its_stored_guess(self):
        # Scripts written before content-sizing carry seconds=14 regardless of
        # their words. Using that as a floor pads the clip — and padding is
        # where the model babbles.
        short = {"seconds": 14, "lines": self._lines(6)}
        self.assertLess(performance.render_seconds(short), 14.0)
        self.assertGreaterEqual(performance.render_seconds(short),
                                performance.MIN_SCENE_SECONDS)


if __name__ == "__main__":
    unittest.main()


class SplitOrderTests(unittest.TestCase):
    """A long exchange splits across scenes without swapping the two people."""

    def test_the_cast_order_survives_a_split(self):
        raw = {"title": "The argument", "cast": ["ANA", "BO"], "seconds": 10,
               "setting": "a wharf", "camera": "locked wide", "soundscape": "gulls",
               "lines": [
                   {"speaker": "ANA", "text": "You said a lot of things and you meant "
                                              "almost none of them, I stood here believing."},
                   {"speaker": "BO", "text": "That is not fair and you know exactly why "
                                             "it is not fair, so do not pretend."},
                   {"speaker": "ANA", "text": "Then tell me what is fair, because I have "
                                              "run out of ways to ask you nicely."}]}
        pieces = performance.split_overloaded(raw)
        self.assertGreater(len(pieces), 1)
        scenes = [performance.scene_from_raw(i, p) for i, p in enumerate(pieces, 1)]
        # Picture 1 is the same person in every piece — the geography block puts
        # Picture 1 on the left, so a per-piece order would swap them mid-scene.
        self.assertEqual([s.metadata["cast"] for s in scenes],
                         [["ANA", "BO"]] * len(scenes))

    def test_a_speaker_missing_from_the_cast_is_added(self):
        scene = performance.scene_from_raw(1, {
            "cast": ["ANA"], "seconds": 8,
            "lines": [{"speaker": "BO", "text": "Hello."}]})
        self.assertEqual(scene.metadata["cast"], ["ANA", "BO"])
