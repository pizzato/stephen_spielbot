"""Create-form inputs must survive script generation so Re-draft can restore them.

After generate, create_brief.json (+ durable job config) holds title, direction,
scene count, style, narrator, resolution, etc. load_script returns that brief so
the Script editor's Re-draft button can open Create fully prefilled.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import webapp.backend.main as backend  # noqa: E402
from pipeline.llm import Scene  # noqa: E402
from test_styles import TempConfigCase, _style  # noqa: E402


class CreateBriefTests(TempConfigCase):
    def setUp(self):
        super().setUp()
        self.write_config({
            "styles": [_style("Hero", n_scenes=8, voice="Narrator",
                              resolution="Landscape FHD (1920×1080)",
                              visual_style="cinematic",
                              extra_instructions="Always mention Rome.")],
            "default_style": "Hero",
            "characters": [],
            "characters_migrated_v2": True,
        })
        mock.patch.object(backend.gapp, "OUTPUT_DIR", self.output_dir).start()
        self.addCleanup(mock.patch.stopall)

    def _generate(self, **kwargs):
        scene = Scene(id=1, title="T", image_prompt="p", video_prompt="v", narration="n")
        body = backend.GenerateScriptBody(
            video_title="Caesar's Crossing",
            topic="Focus on the Rubicon decision",
            n_scenes=7,
            visual_style="oil painting",
            voice="Custom Voice",
            resolution="Portrait FHD (1080×1920)",
            style_name="Hero",
            **kwargs,
        )
        with mock.patch.object(backend, "generate_script",
                               return_value=([scene], "music", "vis", [])), \
             mock.patch.object(backend, "_describe_in_background"):
            return backend._do_script_generate(body)

    def test_generate_writes_create_brief_file(self):
        res = self._generate()
        wd = Path(res["work_dir"])
        brief_path = wd / "create_brief.json"
        self.assertTrue(brief_path.exists())
        brief = json.loads(brief_path.read_text())
        self.assertEqual(brief["video_title"], "Caesar's Crossing")
        self.assertEqual(brief["topic"], "Focus on the Rubicon decision")
        # Style extras must NOT be baked into the stored user topic.
        self.assertNotIn("Always mention Rome", brief["topic"])
        self.assertEqual(brief["n_scenes"], 7)
        self.assertEqual(brief["visual_style"], "oil painting")
        self.assertEqual(brief["voice"], "Custom Voice")
        self.assertNotIn("voice_robotic", brief)   # toggle removed — level is per style
        self.assertEqual(brief["resolution"], "Portrait FHD (1080×1920)")
        self.assertEqual(brief["style_name"], "Hero")

    def test_generate_result_includes_create_brief(self):
        res = self._generate()
        self.assertIn("create_brief", res)
        self.assertEqual(res["create_brief"]["topic"], "Focus on the Rubicon decision")
        self.assertEqual(res["voice"], "Custom Voice")
        self.assertEqual(res["resolution"], "Portrait FHD (1080×1920)")

    def test_load_script_returns_create_brief(self):
        res = self._generate()
        loaded = backend.load_script(res["work_dir"])
        self.assertEqual(loaded["create_brief"]["topic"], "Focus on the Rubicon decision")
        self.assertEqual(loaded["create_brief"]["n_scenes"], 7)
        self.assertEqual(loaded["voice"], "Custom Voice")
        self.assertEqual(loaded["resolution"], "Portrait FHD (1080×1920)")
        self.assertEqual(loaded["topic"], "Focus on the Rubicon decision")

    def test_duplicate_copies_create_brief(self):
        res = self._generate()
        src = Path(res["work_dir"])
        dup = backend.duplicate_script(backend.DuplicateScriptBody(work_dir=str(src)))
        self.assertTrue((Path(dup["work_dir"]) / "create_brief.json").exists())
        self.assertEqual(dup["create_brief"]["topic"], "Focus on the Rubicon decision")


if __name__ == "__main__":
    unittest.main()
