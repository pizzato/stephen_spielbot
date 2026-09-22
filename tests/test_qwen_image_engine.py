"""Qwen-Image 2.1 as an opt-in image engine."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
from pipeline import comfyui, engines


class QwenEngineRegistryTests(unittest.TestCase):
    def test_default_stays_flux2(self):
        self.assertEqual(engines.DEFAULT_ENGINE, "flux2-klein")
        self.assertEqual(engines.resolve({}, "qwen-image-2.1")["key"], "qwen-image-2.1")
        self.assertEqual(engines.resolve({}, "nope")["key"], "flux2-klein")

    def test_generation_only_and_research_license(self):
        eng = engines.get("qwen-image-2.1")
        self.assertTrue(eng["can_generate"])
        self.assertFalse(eng["can_edit"])
        self.assertFalse(eng["commercial_ok"])
        self.assertEqual(eng["license"], "Qwen Research License")
        self.assertIn("Research", eng["license_note"])
        self.assertEqual(eng["min_comfyui"], (0, 37, 0))
        self.assertEqual(eng["requires_node"], "TextEncodeQwenImage21")
        self.assertEqual(eng["steps"], 25)
        self.assertEqual(eng["cfg"], 1.0)
        pub = {e["key"]: e for e in engines.public_list()}
        self.assertIn("qwen-image-2.1", pub)
        self.assertFalse(pub["qwen-image-2.1"]["commercial_ok"])
        self.assertTrue(pub["qwen-image-2.1"]["license_note"])
        # Edit slot refuses it; generation keeps it.
        self.assertEqual(app._norm_engine("qwen-image-2.1", "generate"), "qwen-image-2.1")
        self.assertEqual(app._norm_engine("qwen-image-2.1", "edit"), "flux2-klein")

    def test_nvfp4_is_the_faster_blackwell_build_under_the_same_license(self):
        fast = engines.get("qwen-image-2.1-nvfp4")
        base = engines.get("qwen-image-2.1")
        self.assertEqual(fast["family"], "qwen-image")
        self.assertEqual(fast["license"], base["license"])
        self.assertFalse(fast["commercial_ok"])
        self.assertFalse(fast["can_edit"])
        self.assertIn("Research", fast["license_note"])
        self.assertIn("NVFP4", fast["license_note"])
        self.assertEqual(fast["model_file"], "qwen_image_2.1_nvfp4.safetensors")
        self.assertEqual(fast["clip_t5"], "qwen3vl_8b_w4a8.safetensors")
        self.assertEqual(fast["vae"], base["vae"])
        self.assertEqual(fast["steps"], base["steps"])
        dit = next(m for m in fast["models"] if m["file"].endswith("nvfp4.safetensors"))
        self.assertEqual(dit["revision"], "1a38d44a3a2f35cb0b543a25b04da0a963e7b5e6")
        self.assertEqual(app._norm_engine("qwen-image-2.1-nvfp4", "generate"),
                         "qwen-image-2.1-nvfp4")
        self.assertEqual(app._norm_engine("qwen-image-2.1-nvfp4", "edit"), "flux2-klein")
        pub = {e["key"]: e for e in engines.public_list()}
        self.assertFalse(pub["qwen-image-2.1-nvfp4"]["commercial_ok"])
        self.assertTrue(pub["qwen-image-2.1-nvfp4"]["license_note"])

    def test_workflow_file_has_the_template_nodes(self):
        text = (comfyui.WORKFLOWS_DIR / "qwen_image_2_1_t2i.json").read_text()
        for placeholder in ("{{UNET}}", "{{CLIP}}", "{{VAE}}", "{{POSITIVE_PROMPT}}",
                            "{{WIDTH}}", "{{HEIGHT}}", "{{STEPS}}", "{{CFG}}", "{{SEED}}"):
            self.assertIn(placeholder, text)
        self.assertIn("TextEncodeQwenImage21", text)
        self.assertIn('"type": "qwen_image"', text)


class QwenWorkflowTests(unittest.TestCase):
    def _generate(self, refs=None, width=1024, height=576, key="qwen-image-2.1"):
        captured = {}

        def fake_queue(workflow, client_id, comfy_url=None):
            captured["workflow"] = workflow
            return "pid"

        def fake_upload(path, comfy_url=None):
            return Path(path).name

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "still.png"
            with mock.patch.object(comfyui, "_queue_prompt", side_effect=fake_queue), \
                 mock.patch.object(comfyui, "check_engine_supported"), \
                 mock.patch.object(comfyui, "_upload_image", side_effect=fake_upload), \
                 mock.patch.object(comfyui, "_wait_for_completion") as waited, \
                 mock.patch.object(comfyui, "_get_outputs",
                                   return_value=[{"filename": "out.png", "type": "output"}]), \
                 mock.patch.object(comfyui, "_download_output",
                                   side_effect=lambda item, dest, comfy_url=None: dest):
                comfyui.generate_with_engine(
                    engines.resolve({}, key),
                    'a neon sign that reads "QWEN"',
                    out, width=width, height=height, seed=42,
                    reference_images=refs,
                )
            captured["timeout"] = waited.call_args.kwargs["timeout"]
        return captured

    def test_text_to_image_matches_the_template(self):
        cap = self._generate()
        wf = cap["workflow"]
        by_class = {n["class_type"]: n for n in wf.values()}
        self.assertEqual(by_class["UNETLoader"]["inputs"]["unet_name"],
                         "qwen_image_2.1_int8_convrot.safetensors")
        self.assertEqual(by_class["UNETLoader"]["inputs"]["weight_dtype"], "default")
        self.assertEqual(by_class["CLIPLoader"]["inputs"]["clip_name"],
                         "qwen3vl_8b_int8_convrot.safetensors")
        self.assertEqual(by_class["CLIPLoader"]["inputs"]["type"], "qwen_image")
        encode = by_class["TextEncodeQwenImage21"]
        self.assertIn("QWEN", encode["inputs"]["prompt"])
        self.assertEqual(encode["inputs"]["negative_prompt"], "")
        self.assertNotIn("vae", encode["inputs"])
        self.assertFalse(any(k.startswith("images.") for k in encode["inputs"]))
        self.assertEqual(by_class["EmptyLatentImage"]["inputs"]["width"], 1024)
        self.assertEqual(by_class["EmptyLatentImage"]["inputs"]["height"], 576)
        sampler = by_class["KSampler"]
        self.assertEqual(sampler["inputs"]["steps"], 25)
        self.assertEqual(sampler["inputs"]["cfg"], 1.0)
        self.assertEqual(sampler["inputs"]["sampler_name"], "euler")
        self.assertEqual(sampler["inputs"]["scheduler"], "simple")
        self.assertEqual(sampler["inputs"]["seed"], 42)
        self.assertEqual(sampler["inputs"]["positive"], ["4", 0])
        self.assertEqual(sampler["inputs"]["negative"], ["4", 1])
        self.assertEqual(cap["timeout"], 1800)
        self.assertNotIn("{{", json.dumps(wf))

    def test_nvfp4_graph_loads_the_fp4_weights(self):
        cap = self._generate(key="qwen-image-2.1-nvfp4")
        by_class = {n["class_type"]: n for n in cap["workflow"].values()}
        self.assertEqual(by_class["UNETLoader"]["inputs"]["unet_name"],
                         "qwen_image_2.1_nvfp4.safetensors")
        self.assertEqual(by_class["UNETLoader"]["inputs"]["weight_dtype"], "default")
        self.assertEqual(by_class["CLIPLoader"]["inputs"]["clip_name"],
                         "qwen3vl_8b_w4a8.safetensors")
        self.assertEqual(by_class["CLIPLoader"]["inputs"]["type"], "qwen_image")
        self.assertEqual(by_class["KSampler"]["inputs"]["steps"], 25)

    def test_references_are_spliced_in_slot_order(self):
        cap = self._generate(refs=[Path("bob.png"), Path("ada.png")])
        wf = cap["workflow"]
        encode = next(n for n in wf.values() if n["class_type"] == "TextEncodeQwenImage21")
        self.assertEqual(encode["inputs"]["vae"], ["3", 0])
        self.assertEqual(encode["inputs"]["images.image_1"], ["20", 0])
        self.assertEqual(encode["inputs"]["images.image_2"], ["21", 0])
        self.assertEqual(wf["20"]["inputs"]["image"], "bob.png")
        self.assertEqual(wf["21"]["inputs"]["image"], "ada.png")
        self.assertNotIn("images.image_3", encode["inputs"])

    def test_references_stop_at_the_model_cap(self):
        refs = [Path(f"r{i}.png") for i in range(12)]
        cap = self._generate(refs=refs)
        encode = next(n for n in cap["workflow"].values()
                      if n["class_type"] == "TextEncodeQwenImage21")
        wired = [k for k in encode["inputs"] if k.startswith("images.")]
        self.assertEqual(len(wired), 10)
        self.assertNotIn("images.image_11", encode["inputs"])

    def test_size_snaps_to_a_multiple_of_32(self):
        cap = self._generate(width=1080, height=1000)
        latent = next(n for n in cap["workflow"].values()
                      if n["class_type"] == "EmptyLatentImage")
        self.assertEqual(latent["inputs"]["width"], 1056)
        self.assertEqual(latent["inputs"]["height"], 992)
