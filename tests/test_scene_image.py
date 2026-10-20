"""Scene-image provenance follows the actual image through selection and renames."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, PngImagePlugin

from pipeline import scene_image


class SceneImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "frame.png"
        self.mick = {"id": "char_mick", "name": "Mick Dargo",
                     "ref_image": "char_mick.png"}

    def _png(self, **metadata):
        info = PngImagePlugin.PngInfo()
        for key, value in metadata.items():
            info.add_text(key, value)
        info.add_itxt("international", "café 日本語", lang="en", tkey="translated")
        Image.new("RGB", (8, 12), (20, 40, 60)).save(
            self.image, pnginfo=info, dpi=(96, 96))

    def test_round_trip_preserves_pixels_and_existing_metadata(self):
        self._png(prompt='{"existing": "workflow"}', workflow='{"nodes": []}')
        with Image.open(self.image) as im:
            old_pixels, old_info = im.tobytes(), dict(im.info)
        scene_image.record_cast(self.image, [self.mick])
        with Image.open(self.image) as im:
            self.assertEqual(im.tobytes(), old_pixels)
            for key, value in old_info.items():
                self.assertEqual(im.info[key], value)
            self.assertEqual(im.info["international"].lang, "en")
            self.assertEqual(im.info["international"].tkey, "translated")
        self.assertEqual(scene_image.read_cast(self.image, [self.mick]), ["Mick Dargo"])

    def test_stable_id_resolves_renamed_character_and_history_copy(self):
        self._png()
        scene_image.record_cast(self.image, [self.mick])
        selected = self.root / "selected.png"
        shutil.copy2(self.image, selected)
        renamed = {**self.mick, "name": "Mick"}
        self.assertEqual(scene_image.read_cast(selected, [renamed]), ["Mick"])
        self.assertEqual(scene_image.read_cast(selected, [
            {**self.mick, "id": "different_character"}]), [])

    def test_names_without_ids_work_and_missing_names_are_ignored(self):
        self._png()
        scene_image.record_cast(self.image, [
            {"name": " ana "}, {"id": "no_name"}, {}, {"name": None}])
        self.assertEqual(scene_image.read_cast(self.image, [{"name": "Ana"}]), ["Ana"])

    def test_existing_qwen_graph_matches_only_attached_known_portraits(self):
        graph = {
            "4": {"class_type": "TextEncodeQwenImage21",
                  "inputs": {"prompt": "Mick Dargo and Ana in the style",
                             "images.image_1": ["20", 0]}},
            "20": {"class_type": "LoadImage",
                   "inputs": {"image": "subfolder/char_mick.png"}},
            "21": {"class_type": "LoadImage",
                   "inputs": {"image": "unknown.png"}},
        }
        self._png(prompt=json.dumps(graph))
        self.assertEqual(scene_image.read_cast(self.image, [
            self.mick, {"id": "ana", "name": "Ana", "ref_image": "ana.png"}]),
            ["Mick Dargo"])
        self.assertEqual(scene_image.read_cast(self.image, [
            {"name": "Mick", "_ref_path": self.root / "char_mick.png"}]), ["Mick"])

    def test_explicit_empty_cast_overrides_legacy_graph_and_replaces_record(self):
        self._png(prompt=json.dumps({
            "20": {"class_type": "LoadImage", "inputs": {"image": "char_mick.png"}}}))
        scene_image.record_cast(self.image, [self.mick])
        scene_image.record_cast(self.image, [])
        self.assertEqual(scene_image.read_cast(self.image, [self.mick]), [])
        self.assertEqual(self.image.read_bytes().count(b"spielbot_scene_cast"), 1)

    def test_unknown_refs_and_invalid_metadata_do_not_invent_cast(self):
        for prompt in ('{"20":{"class_type":"LoadImage","inputs":{"image":"other.png"}}}',
                       "not json", "[]", "null"):
            self._png(prompt=prompt)
            self.assertEqual(scene_image.read_cast(self.image, [self.mick]), [])
        self._png(spielbot_scene_cast="not json")
        self.assertEqual(scene_image.read_cast(self.image, [self.mick]), [])

    def test_missing_invalid_and_non_png_images_are_unchanged(self):
        self.assertEqual(scene_image.read_cast(self.image, [self.mick]), [])
        scene_image.record_cast(self.image, [self.mick])
        self.assertFalse(self.image.exists())
        for raw in (b"not an image", b"\x89PNG\r\n\x1a\n"):
            self.image.write_bytes(raw)
            scene_image.record_cast(self.image, [self.mick])
            self.assertEqual(self.image.read_bytes(), raw)
            self.assertEqual(scene_image.read_cast(self.image, [self.mick]), [])
        Image.new("RGB", (8, 12)).save(self.image, format="JPEG")
        raw = self.image.read_bytes()
        scene_image.record_cast(self.image, [self.mick])
        self.assertEqual(self.image.read_bytes(), raw)
        self.assertEqual(scene_image.read_cast(self.image, [self.mick]), [])


if __name__ == "__main__":
    unittest.main()
