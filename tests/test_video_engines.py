"""Per-style video engine (scene I2V model) — registry + style plumbing."""
import os
import tempfile
import unittest

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app
from pipeline import engines
from test_styles import TempConfigCase, _style


class VideoEngineRegistryTests(unittest.TestCase):
    def test_resolve_video_falls_back_to_ltx(self):
        self.assertEqual(engines.resolve_video({}, None)["key"], "ltx23")
        self.assertEqual(engines.resolve_video({}, "nope")["key"], "ltx23")
        self.assertEqual(engines.DEFAULT_VIDEO_ENGINE, "ltx23")

    def test_resolve_video_minimax(self):
        eng = engines.resolve_video({}, "minimax-h3")
        self.assertEqual(eng["family"], "minimax")
        self.assertEqual(eng["workflow"], "h3_i2v.json")
        # Every model file the workflow names must be in the download list.
        files = {m["file"] for m in eng["models"]}
        for key in ("unet", "clip", "video_vae", "audio_vae"):
            self.assertIn(eng[key], files)

    def test_resolve_video_minimax_turbo(self):
        eng = engines.resolve_video({}, "minimax-h3-turbo")
        self.assertEqual(eng["family"], "minimax")
        self.assertEqual(eng["workflow"], "h3_turbo_i2v.json")
        # The turbo LoRA is incompatible with the pruned DiT variants (different
        # time-conditioning layer) — this engine must stay on the full unet.
        self.assertNotIn("pruned", eng["unet"])
        files = {m["file"] for m in eng["models"]}
        for key in ("unet", "clip", "video_vae", "audio_vae", "lora"):
            self.assertIn(eng[key], files)

    def test_video_steps_override(self):
        # Per-style/job override reaches MiniMax engines; 0/absent/garbage keep
        # the engine default; LTX has no single-pass steps to override.
        self.assertEqual(engines.resolve_video({"video_steps": 6}, "minimax-h3-turbo")["steps"], 6)
        self.assertEqual(engines.resolve_video({"video_steps": 0}, "minimax-h3-turbo")["steps"], 4)
        self.assertEqual(engines.resolve_video({"video_steps": "12"}, "minimax-h3")["steps"], 12)
        self.assertEqual(engines.resolve_video({"video_steps": "junk"}, "minimax-h3")["steps"], 15)
        self.assertNotIn("steps", engines.resolve_video({"video_steps": 6}, "ltx23"))

    def test_public_list_video_has_license_info(self):
        entries = {e["key"]: e for e in engines.public_list_video()}
        self.assertIn("ltx23", entries)
        self.assertTrue(entries["minimax-h3"]["license_note"])
        self.assertTrue(entries["minimax-h3"]["downloadable"])
        self.assertFalse(entries["ltx23"]["downloadable"])


class VideoEngineStyleTests(TempConfigCase):
    def test_bogus_video_engine_is_coerced_to_default(self):
        self.write_config({"styles": [_style("BHOB", video_engine="bogus")],
                           "default_style": "BHOB"})
        cfg = app.load_config()
        root = next(s for s in cfg["styles"] if s["name"] == "BHOB")
        self.assertEqual(root["video_engine"], "ltx23")
        self.assertEqual(cfg["default_video_engine"], "ltx23")

    def test_child_inherits_parent_video_engine(self):
        self.write_config({
            "styles": [_style("BHOB", video_engine="minimax-h3"),
                       {"name": "BHOB ES", "parent": "BHOB"}],
            "default_style": "BHOB",
        })
        cfg = app.load_config()
        child = next(s for s in cfg["styles"] if s["name"] == "BHOB ES")
        self.assertNotIn("video_engine", child)  # stays sparse
        self.assertEqual(app.style_settings(cfg, "BHOB ES")["video_engine"],
                         "minimax-h3")
        # The flat mirror follows the default style.
        self.assertEqual(cfg["default_video_engine"], "minimax-h3")


class VideoStepsStyleTests(TempConfigCase):
    def test_video_steps_coerced_and_inherited(self):
        self.write_config({
            "styles": [_style("BHOB", video_engine="minimax-h3-turbo", video_steps="8"),
                       {"name": "BHOB ES", "parent": "BHOB"}],
            "default_style": "BHOB",
        })
        cfg = app.load_config()
        root = next(s for s in cfg["styles"] if s["name"] == "BHOB")
        self.assertEqual(root["video_steps"], 8)  # string coerced to int
        self.assertEqual(app.style_settings(cfg, "BHOB ES")["video_steps"], 8)
        self.assertEqual(cfg["default_video_steps"], 8)  # flat mirror

    def test_bogus_video_steps_coerced_to_engine_default(self):
        self.write_config({"styles": [_style("BHOB", video_steps=-3)],
                           "default_style": "BHOB"})
        cfg = app.load_config()
        root = next(s for s in cfg["styles"] if s["name"] == "BHOB")
        self.assertEqual(root["video_steps"], 0)


if __name__ == "__main__":
    unittest.main()
