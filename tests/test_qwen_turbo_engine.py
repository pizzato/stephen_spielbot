"""The Turbo checkpoint must use its trained schedule, including with references."""
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import app
from pipeline import comfyui, engines

KEY = "qwen-image-2.1-turbo"
ENGINE = engines.get(KEY)


def test_turbo_is_an_optional_research_generation_engine():
    assert engines.DEFAULT_ENGINE == "flux2-klein"
    assert app._norm_engine(KEY, "generate") == KEY
    assert app._norm_engine(KEY, "edit") == "flux2-klein"
    assert ENGINE["steps"] == 8
    public = next(e for e in engines.public_list() if e["key"] == KEY)
    assert not public["commercial_ok"]
    assert "Research" in public["license_note"]
    assert KEY not in {e["key"] for e in engines.public_list(commercial_only=True)}
    assert all(m["repo"] == "Comfy-Org/Qwen-Image-2.1" for m in ENGINE["models"])


@pytest.mark.parametrize("ref_count", [0, 2, 12])
def test_turbo_uses_fixed_sigmas_cache_and_ordered_references(tmp_path, ref_count):
    refs = [Path(f"person{i}.png") for i in range(ref_count)]
    with mock.patch.object(comfyui, "comfyui_version", return_value=(0, 41, 3)), \
         mock.patch.object(comfyui, "comfy_node_exists", return_value=True), \
         mock.patch.object(comfyui, "_upload_image", side_effect=lambda p, **kw: p.name) as upload, \
         mock.patch.object(comfyui, "_run_and_save", return_value=tmp_path / "out.png") as run:
        comfyui.generate_with_engine(ENGINE, 'Portrait of <image1>, holding a sign "HELLO".',
                                     tmp_path / "out.png", width=1080, height=1000,
                                     seed=0, reference_images=refs)
    wf = run.call_args.args[0]
    assert "{{" not in json.dumps(wf)
    assert wf["1"]["inputs"]["unet_name"] == ENGINE["model_file"]
    assert wf["2"]["inputs"]["clip_name"] == ENGINE["clip_t5"]
    assert wf["5"]["class_type"] == "QwenImage21Cache"
    assert wf["5"]["inputs"] == {"model": ["1", 0], "device": "auto", "dtype": "default"}
    assert wf["7"]["inputs"]["model"] == ["5", 0]
    assert wf["7"]["inputs"]["cfg"] == 1.0
    assert wf["9"]["inputs"]["noise_seed"] == 0
    assert wf["10"]["inputs"]["sampler_name"] == "euler"
    sigmas = [float(s) for s in wf["11"]["inputs"]["sigmas"].split(",")]
    assert sigmas == [1.0, 0.978453, 0.95418, 0.926626, 0.89508,
                      0.845148, 0.704534, 0.414568, 0.0]
    assert len(sigmas) - 1 == ENGINE["steps"]
    assert wf["8"]["class_type"] == "SamplerCustomAdvanced"
    assert wf["8"]["inputs"]["sigmas"] == ["11", 0]
    assert wf["8"]["inputs"]["latent_image"] == ["6", 0]
    assert wf["6"]["inputs"] == {"width": 1056, "height": 992, "batch_size": 1}
    assert wf["12"]["inputs"]["samples"] == ["8", 0]
    assert upload.call_count == min(ref_count, 10)
    for i in range(min(ref_count, 10)):
        assert wf["4"]["inputs"][f"images.image_{i + 1}"] == [str(20 + i), 0]
        assert wf[str(20 + i)]["inputs"]["image"] == refs[i].name
    assert ("vae" in wf["4"]["inputs"]) == bool(ref_count)


@pytest.mark.parametrize("missing", ENGINE["extra_nodes"])
def test_missing_turbo_node_fails_before_uploading_or_queueing(tmp_path, missing):
    with mock.patch.object(comfyui, "comfyui_version", return_value=(0, 41, 3)), \
         mock.patch.object(comfyui, "comfy_node_exists", side_effect=lambda url, node: node != missing), \
         mock.patch.object(comfyui, "_upload_image") as upload, \
         mock.patch.object(comfyui, "_run_and_save") as run:
        with pytest.raises(RuntimeError, match=missing):
            comfyui.generate_with_engine(ENGINE, "portrait", tmp_path / "out.png",
                                         width=1024, height=576,
                                         reference_images=[Path("person.png")])
    upload.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("probe", ENGINE["extra_probes"])
def test_settings_does_not_report_turbo_installed_without_companions(probe):
    from webapp.backend import main as web
    with mock.patch.object(web.gapp, "load_config", return_value={"comfy_workers": ["http://worker:8188"]}), \
         mock.patch("pipeline.worker_pool.queue_depth", return_value=0), \
         mock.patch.object(comfyui, "engine_model_present", side_effect=lambda url, p: p != probe), \
         mock.patch.object(comfyui, "comfy_node_exists", return_value=True), \
         mock.patch.object(comfyui, "comfyui_version", return_value=(0, 41, 3)):
        assert web.list_engines()["availability"][KEY] is False


@pytest.mark.parametrize("target,script", [("install", "install.sh"), ("download-models", "download_models.sh")])
def test_make_passes_both_optional_install_flags(tmp_path, target, script):
    (tmp_path / script).write_text('printf "%s %s\\n" "$INSTALL_FLUX3" "$INSTALL_QWEN_TURBO"\n')
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(["make", "--no-print-directory", "-s", target, f"SCRIPTS={tmp_path}",
                           "INSTALL_FLUX3=1", "INSTALL_QWEN_TURBO=1"], cwd=root,
                          text=True, capture_output=True, check=True)
    assert proc.stdout.strip() == "1 1"
