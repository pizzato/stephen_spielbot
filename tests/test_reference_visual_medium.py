"""H3 must animate the opening image without changing its visual medium."""
import unittest

from pipeline import performance
from pipeline.llm import Scene


class ReferenceVisualMediumTests(unittest.TestCase):
    def test_shot_description_reaches_video_without_the_parent_style(self):
        image = "Vintage pop-art comic-book illustration. Rua Vex in a car."
        meta = {"mode": "silent", "singing": True, "cast": ["Rua Vex"],
                "setting": "A car passing Irish bogland.", "duration": 5}
        for scene in ({"metadata": meta, "image_prompt": image},
                      Scene(id=3, title="Car", image_prompt=image,
                            video_prompt="Pan to the window.", narration="",
                            mode="silent", metadata_extra=meta)):
            with self.subTest(scene=type(scene).__name__):
                prompt = performance.build_h3_prompt(
                    performance.acted_meta(scene),
                    style_note="Contemporary photography. Stages and city streets.",
                    picture_names=[{"name": "Rua Vex", "kind": "character"},
                                   {"name": "First frame", "kind": "frame"}])
                self.assertIn(image, prompt)
                self.assertIn("follow the image", prompt)
                self.assertNotIn("Contemporary photography", prompt)
                self.assertNotIn("Stages and city streets", prompt)

    def test_opening_medium_survives_music_style_filtering_and_castless_shots(self):
        for cast in ([], [{"name": "Rua Vex", "kind": "character"}]):
            with self.subTest(cast=cast):
                prompt = performance.build_h3_prompt(
                    {"singing": True, "performs": False, "seconds": 5,
                     "setting": "A car passing Irish bogland."},
                    style_note="Contemporary photography. Stages and city streets.",
                    picture_names=cast + [{"name": "First frame", "kind": "frame"}])
                self.assertIn("[VISUAL MEDIUM]", prompt)
                self.assertIn("for both characters and environments", prompt)
                self.assertIn("linework, shading, colours and textures", prompt)
                self.assertIn("keep the entire take illustrated or animated", prompt)
                self.assertNotIn("Contemporary photography", prompt)
                self.assertNotIn("Stages and city streets", prompt)
                self.assertIn("A car passing Irish bogland", prompt)

    def test_dialogue_opening_also_controls_visual_medium(self):
        prompt = performance.build_h3_prompt(
            {"lines": [{"speaker": "Ana", "text": "Hello."}]},
            picture_names=[{"name": "Ana", "kind": "character"},
                           {"name": "First frame", "kind": "frame"}])
        self.assertIn("[VISUAL MEDIUM]", prompt)
        self.assertIn("defines the visual medium", prompt)
        self.assertIn('says exactly, even, natural: "Hello."', prompt)

    def test_portrait_only_take_does_not_claim_an_opening_image(self):
        prompt = performance.build_h3_prompt(
            {}, picture_names=[{"name": "Ana", "kind": "character"}])
        self.assertIn("[VISUAL MEDIUM]", prompt)
        self.assertIn("character references", prompt)
        self.assertNotIn("opening image", prompt)

    def test_no_first_frame_keeps_the_scene_medium_with_portrait_or_location_refs(self):
        image = "Pop-art comic-book illustration. Rua Vex beside a copper-lit river."
        for pictures in ([{"name": "Rua Vex", "kind": "character"}],
                         [{"name": "Riverbank", "kind": "location"}]):
            with self.subTest(pictures=pictures):
                row = {"image_prompt": image,
                       "metadata": {"mode": "silent", "singing": True,
                                    "no_first_frame": True, "duration": 5,
                                    "setting": "Irish riverbank, copper light."}}
                prompt = performance.build_h3_prompt(
                    performance.acted_meta(row), picture_names=pictures,
                    style_note="Contemporary photography. Stages and city streets.")
                self.assertIn(image, prompt)
                self.assertIn("[VISUAL MEDIUM]", prompt)
                self.assertIn("linework, shading, colours and textures", prompt)
                self.assertIn("all characters, backgrounds, scenery and lighting", prompt)
                self.assertNotIn("opening image", prompt)
                self.assertNotIn("Contemporary photography", prompt)

    def test_no_first_frame_preserves_hand_edited_prompt(self):
        prompt = performance.build_h3_prompt(
            {"no_first_frame": True, "opening_description": "Pop-art illustration.",
             "prompt_override": "Animate the river in watercolour."},
            picture_names=[{"name": "Ana", "kind": "character"}])
        self.assertEqual(prompt, "Animate the river in watercolour.")
