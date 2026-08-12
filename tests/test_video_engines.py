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
        self.assertTrue(entries["ltx25"]["downloadable"])

    def test_resolve_video_ltx25(self):
        eng = engines.resolve_video({}, "ltx25")
        self.assertEqual(eng["family"], "ltx")
        self.assertEqual(eng["workflow"], "ltx25_i2v.json")
        # 2.5 runs at 24 fps and its latent wants 8k+1 frame counts.
        self.assertEqual(eng["fps"], 24)
        self.assertEqual(eng["frame_multiple"], 8)
        # Native support (gemma4 'ltxv' CLIP + the 2.5 transformer) landed in
        # ComfyUI v0.32.0 — older workers must be refused, and the probed node
        # only exists from that release.
        self.assertEqual(eng["min_comfyui"], (0, 32, 0))
        self.assertEqual(eng["requires_node"], "LTXVDualCFGGuider")
        # No distill-LoRA steps knob: the distilled transformer has a fixed
        # sigma schedule, so the per-style video_steps override must not bite.
        self.assertNotIn("steps", engines.resolve_video({"video_steps": 6}, "ltx25"))
        # Every model file the workflow names must be in the download list,
        # and the click-through Lightricks repo needs a token.
        files = {m["file"] for m in eng["models"]}
        for key in ("unet", "clip", "video_vae", "audio_vae", "upscaler"):
            self.assertIn(eng[key], files)
        self.assertTrue(all(m["gated"] for m in eng["models"]))

    def test_ltx25_workflow_is_wired_for_the_shared_ltx_path(self):
        import json
        from pathlib import Path
        eng = engines.resolve_video({}, "ltx25")
        root = Path(__file__).resolve().parent.parent
        graph = json.loads((root / "workflows" / eng["workflow"]).read_text())
        # generate_video_continuation patches these nodes by ID — the 2.5 graph
        # must keep the 2.3 layout: 25/28 second pass, no "3" distill LoRA.
        self.assertNotIn("3", graph)
        self.assertEqual(graph["25"]["class_type"], "CFGGuider")
        self.assertEqual(graph["28"]["class_type"], "ManualSigmas")
        # Loaders must name exactly the files the engine downloads.
        self.assertEqual(graph["1"]["inputs"]["unet_name"], eng["unet"])
        self.assertEqual(graph["2"]["inputs"]["clip_name"], eng["clip"])
        self.assertEqual(graph["2"]["inputs"]["type"], "ltxv")
        self.assertEqual(graph["38"]["inputs"]["vae_name"], eng["video_vae"])
        self.assertEqual(graph["7"]["inputs"]["vae_name"], eng["audio_vae"])
        self.assertEqual(graph["19"]["inputs"]["model_name"], eng["upscaler"])
        # Both passes sample the same standalone transformer.
        for node in ("13", "25"):
            self.assertEqual(graph[node]["inputs"]["model"], ["1", 0])


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


class ReferenceEngineRegistryTests(unittest.TestCase):
    """Ref2VA engines (performance films) — separate from the I2V picker."""

    def test_resolve_reference_defaults_to_turbo(self):
        self.assertEqual(engines.resolve_reference({}, None)["key"],
                         engines.DEFAULT_REFERENCE_ENGINE)
        # An I2V key is not a usable Ref2VA graph — fall back, never return it.
        self.assertEqual(engines.resolve_reference({}, "minimax-h3")["key"],
                         engines.DEFAULT_REFERENCE_ENGINE)

    def test_reference_engines_declare_their_stack(self):
        for key, workflow in (("minimax-h3-ref", "h3_ref2v.json"),
                              ("minimax-h3-ref-turbo", "h3_ref2v_turbo.json")):
            eng = engines.resolve_reference({}, key)
            self.assertEqual(eng["key"], key)
            self.assertTrue(eng["reference"])
            self.assertEqual(eng["workflow"], workflow)
            self.assertIn("ref2va", eng["unet"])
            files = {m["file"] for m in eng["models"]}
            for field in ("unet", "clip", "video_vae", "audio_vae"):
                self.assertIn(eng[field], files)

    def test_turbo_reference_needs_the_full_checkpoint(self):
        # The distill LoRA adapts adaln_proj, which the pruned checkpoints bake
        # to F16 (and they drop time_embedder entirely) — full unet only.
        eng = engines.resolve_reference({}, "minimax-h3-ref-turbo")
        self.assertNotIn("pruned", eng["unet"])
        self.assertIn(eng["lora"], {m["file"] for m in eng["models"]})

    def test_reference_engines_stay_out_of_the_i2v_picker(self):
        i2v = {e["key"] for e in engines.public_list_video()}
        ref = {e["key"] for e in engines.public_list_reference()}
        self.assertFalse(i2v & ref)
        self.assertIn("ltx23", i2v)
        self.assertEqual(ref, {"minimax-h3-ref", "minimax-h3-ref-turbo",
                               "minimax-h3-ref-w4a8"})

    def test_reference_workflows_exist_and_are_wired(self):
        import json
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for key in ("minimax-h3-ref", "minimax-h3-ref-turbo"):
            eng = engines.resolve_reference({}, key)
            graph = json.loads((root / "workflows" / eng["workflow"]).read_text())
            node = next(n for n in graph.values()
                        if n["class_type"] == "MiniMaxH3ReferenceToVideo")
            # R2V needs the audio VAE on the conditioning node (I2V does not),
            # and must NOT carry a first frame.
            self.assertIn("audio_vae", node["inputs"])
            self.assertNotIn("first_frame", node["inputs"])


class W4A8EngineTests(unittest.TestCase):
    """4-bit Ref2VA: base fidelity at lower memory cost, gated on ComfyUI."""

    def test_it_is_a_multi_step_reference_engine_not_a_turbo(self):
        eng = engines.resolve_reference({}, "minimax-h3-ref-w4a8")
        self.assertTrue(eng["reference"])
        self.assertEqual(eng["workflow"], "h3_ref2v.json")   # EasyCache graph
        self.assertGreater(eng["steps"], 4)
        # The distill LoRA only fits the NON-pruned DiT; w4a8 is pruned.
        self.assertNotIn("lora", eng)
        self.assertIn("w4a8", eng["unet"])

    def test_it_declares_the_comfyui_floor(self):
        # Below 0.31.0 the render succeeds and returns BLACK frames.
        self.assertEqual(engines.resolve_reference({}, "minimax-h3-ref-w4a8")["min_comfyui"],
                         (0, 31, 0))
        for key in ("minimax-h3-ref", "minimax-h3-ref-turbo"):
            self.assertIsNone(engines.resolve_reference({}, key).get("min_comfyui"))
