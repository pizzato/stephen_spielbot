"""FLUX 3 workflow, character binding, capability and install regressions."""
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import app
from pipeline import comfyui, engines

ROOT = Path(__file__).resolve().parents[1]
KEY = "flux3-klein-nvfp4"
ENGINE = engines.get(KEY)


def test_engine_is_optional_gated_and_generation_only():
    assert engines.DEFAULT_ENGINE == "flux2-klein"
    assert app._norm_engine(KEY, "generate") == KEY
    assert app._norm_engine(KEY, "edit") == "flux2-klein"
    assert not ENGINE["can_edit"]
    assert all(m["gated"] for m in ENGINE["models"])
    public = next(e for e in engines.public_list() if e["key"] == KEY)
    assert public["commercial_label"] == "commercial use subject to licence"
    assert "US$5M" in public["license_note"]
    assert "affiliates" in public["license_note"]


@pytest.fixture(autouse=True)
def current_worker(monkeypatch):
    monkeypatch.setattr(comfyui, "comfyui_version", lambda *a, **kw: (0, 41, 3))


def generate(tmp_path, refs=(), width=1024, height=576, engine=ENGINE):
    with mock.patch.object(comfyui, "comfyui_version", return_value=(0, 41, 3)), \
         mock.patch.object(comfyui, "comfy_node_exists", return_value=True), \
         mock.patch.object(comfyui, "_comfy_has_cuda", return_value=True), \
         mock.patch.object(comfyui, "_upload_image", side_effect=lambda p, **kw: p.name) as upload, \
         mock.patch.object(comfyui, "_run_and_save", return_value=tmp_path / "out.png") as run:
        comfyui.generate_with_engine(engine, 'Alice holds a sign reading "hello".',
                                     tmp_path / "out.png", width=width, height=height,
                                     seed=0, reference_images=refs)
    return run.call_args.args[0], upload, run.call_args.kwargs


def test_distilled_graph_upsamples_before_encoding_and_preserves_output_canvas(tmp_path):
    wf, upload, kwargs = generate(tmp_path, width=1080, height=1000)
    assert wf["1"]["inputs"]["unet_name"] == "flux3-image-klein-nvfp4.safetensors"
    assert wf["2"]["inputs"]["type"] == "flux3"
    assert wf["2"]["inputs"]["clip_name"] == "qwen3vl_4b_int8_convrot.safetensors"
    assert wf["4"]["inputs"]["text"] == ["11", 0]
    pup = wf["11"]["inputs"]
    assert pup["clip"] == ["10", 0]
    assert wf["10"]["inputs"]["clip_name"] == ENGINE["prompt_upsampler"]
    assert pup["prompt"] == 'TARGET CANVAS: 1072:992\nAlice holds a sign reading "hello".'
    assert pup["system_prompt"] == json.loads(comfyui._load_workflow("prompts/flux3.json"))["t2i"]
    assert pup["sampling_mode.seed"] == 0
    assert pup["sampling_mode.presence_penalty"] == 1.5
    assert "image" not in pup
    size = wf["6"]["inputs"]
    assert size["width"] * size["height"] == pytest.approx(2 * 1024**2, rel=0.02)
    assert size["width"] / size["height"] == pytest.approx(1072 / 992, rel=0.015)
    assert size["width"] % 16 == size["height"] % 16 == 0
    assert wf["12"]["inputs"]["max_shift"] == wf["12"]["inputs"]["base_shift"] == math.log(16)
    assert wf["7"]["inputs"]["model"] == ["12", 0]
    assert wf["13"]["inputs"]["width"] == 1072
    assert wf["13"]["inputs"]["height"] == 992
    assert wf["9"]["inputs"]["images"] == ["13", 0]
    sampler = wf["7"]["inputs"]
    assert (sampler["steps"], sampler["cfg"], sampler["seed"]) == (16, 1.0, 0)
    assert (sampler["sampler_name"], sampler["scheduler"]) == ("euler", "simple")
    assert sampler["latent_image"] == ["6", 0]
    assert "17" not in wf
    assert "{{" not in json.dumps(wf)
    assert kwargs["timeout"] == 1800
    upload.assert_not_called()


def test_references_keep_order_limit_and_scene_aspect_ratio(tmp_path):
    wf, upload, _ = generate(tmp_path, [Path(f"person{i}.png") for i in range(12)])
    assert upload.call_count == 10
    refs = wf["17"]["inputs"]
    assert refs["vae"] == ["3", 0]
    assert refs["resolution"] == "resize"
    assert refs["resolution.megapixels"] == 1.0
    for i in range(10):
        assert refs[f"images.image{i}"] == [str(20 + i), 0]
        assert wf[str(20 + i)]["inputs"]["image"] == f"person{i}.png"
        assert wf["18"]["inputs"][f"images.image{i}"] == [str(20 + i), 0]
    assert "images.image10" not in refs
    assert wf["7"]["inputs"]["positive"] == ["17", 0]
    assert wf["7"]["inputs"]["negative"] == ["17", 1]
    # A square portrait must not replace the widescreen scene canvas.
    assert wf["7"]["inputs"]["latent_image"] == ["6", 0]
    assert wf["6"]["inputs"]["width"] / wf["6"]["inputs"]["height"] == pytest.approx(16 / 9, rel=0.015)
    assert wf["13"]["inputs"]["height"] == 576
    pup = wf["11"]["inputs"]
    assert pup["image"] == ["18", 0]
    assert pup["system_prompt"] == json.loads(comfyui._load_workflow("prompts/flux3.json"))["i2i"]
    assert pup["sampling_mode.presence_penalty"] == 0
    assert "<ref_image_9>" in pup["prompt"]
    assert "<ref_image_10>" not in pup["prompt"]


@pytest.mark.parametrize("version,node", [((0, 41, 0), True), ((0, 41, 2), True), ((0, 41, 3), False), ((), None)])
def test_stale_or_unverifiable_worker_is_refused_before_upload(tmp_path, version, node):
    with mock.patch.object(comfyui, "comfyui_version", return_value=version), \
         mock.patch.object(comfyui, "comfy_node_exists", return_value=node), \
         mock.patch.object(comfyui, "_upload_image") as upload, \
         mock.patch.object(comfyui, "_queue_prompt") as queue:
        with pytest.raises(RuntimeError, match="FLUX 3"):
            comfyui.generate_with_engine(ENGINE, "scene", tmp_path / "out.png",
                                         width=1024, height=576, reference_images=[Path("a.png")])
    upload.assert_not_called()
    queue.assert_not_called()


@pytest.mark.parametrize("override", [False, True])
def test_character_reference_tokens_follow_the_uploaded_order(override):
    pairs = [({"name": "Alice"}, Path("a.png")), ({"name": "Bob"}, Path("b.png"))]
    with mock.patch.object(app, "_inject_characters", return_value="The scene."), \
         mock.patch.object(app, "_scene_reference_characters", return_value=pairs):
        prompt, refs = app._characters_prompt_and_refs(
            "scene", {}, {}, "style", engine=ENGINE, allow_appearance_override=override)
    assert refs == [Path("a.png"), Path("b.png")]
    assert prompt.index("Alice") < prompt.index("<ref_image_0>") < prompt.index("Bob")
    assert "<ref_image_1>" in prompt
    assert "<image1>" not in prompt
    assert ("Apply the requested changes" in prompt) == override


@pytest.mark.parametrize("missing", [None, ENGINE["model_file"], ENGINE["clip_t5"], ENGINE["vae"], ENGINE["prompt_upsampler"]])
def test_settings_requires_the_entire_model_bundle(missing):
    from webapp.backend import main as web
    with mock.patch.object(web.gapp, "load_config", return_value={"comfy_workers": ["http://worker:8188"]}), \
         mock.patch("pipeline.worker_pool.queue_depth", return_value=0), \
         mock.patch.object(comfyui, "engine_model_present", side_effect=lambda url, p: p[2] != missing), \
         mock.patch.object(comfyui, "comfy_node_exists", return_value=True):
        assert web.list_engines()["availability"][KEY] is (missing is None)


@pytest.mark.parametrize("node,missing,available,update", [
    (False, None, False, True),
    (True, None, True, False),
    (None, None, None, False),
    (False, ENGINE["clip_t5"], False, False),
])
def test_settings_distinguishes_downloaded_models_from_worker_update(node, missing, available, update):
    from webapp.backend import main as web
    url = "http://worker:8188"
    with mock.patch.object(web.gapp, "load_config", return_value={"comfy_workers": [url]}), \
         mock.patch("pipeline.worker_pool.queue_depth", return_value=0), \
         mock.patch.object(comfyui, "engine_model_present", side_effect=lambda u, p: p[2] != missing), \
         mock.patch.object(comfyui, "comfy_node_exists", return_value=node):
        result = web.list_engines()
    assert result["availability"][KEY] is available
    assert result["worker_updates"].get(KEY) == (url if update else None)


def test_settings_does_not_report_offline_worker_as_needing_update():
    from webapp.backend import main as web
    with mock.patch.object(web.gapp, "load_config", return_value={"comfy_workers": ["http://worker:8188"]}), \
         mock.patch("pipeline.worker_pool.queue_depth", return_value=-1):
        result = web.list_engines()
    assert result["availability"][KEY] is None
    assert result["worker_updates"] == {}


def test_gated_download_requires_a_token_before_starting_a_task():
    from fastapi import HTTPException
    from webapp.backend import main as web
    with mock.patch.object(web.gapp, "load_config", return_value={"comfy_workers": ["http://worker:8188"]}), \
         mock.patch.object(web.threading, "Thread") as thread:
        with pytest.raises(HTTPException) as exc:
            web.install_engine(web.EngineInstallBody(engine=KEY))
    assert exc.value.status_code == 400
    assert "Hugging Face token" in exc.value.detail
    thread.assert_not_called()


def shell(code, env):
    return subprocess.run(["bash", "-c", code], env={**os.environ, **env}, text=True,
                          capture_output=True, timeout=30)


def touch_files(root, files):
    for file in files:
        p = root / file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("test")


DEFAULT_SENTINELS = [
    "models/diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
    "models/checkpoints/ltx-2.3-22b-dev-fp8.safetensors",
    "models/latent_upscale_models/ltx-2.3-spatial-upscaler-x2-1.1.safetensors",
    "models/diffusion_models/acestep_v1.5_turbo.safetensors",
    "models/diffusion_models/flux-2-klein-4b.safetensors",
]
FLUX3_FILES = [f'{m["dir"]}/{m["file"]}' for m in ENGINE["models"]]


@pytest.mark.parametrize("missing_index", [1, 3])
def test_make_install_detects_missing_optional_encoder_on_an_existing_install(tmp_path, missing_index):
    source = (ROOT / "scripts/install.sh").read_text()
    function = source[source.index("_models_present_on() {"):source.index("_ask_hf_token() {")]
    code = 'is_local_host() { [[ "$1" == localhost || "$1" == 127.0.0.1 ]]; };\n' + function
    code += '\n_models_present_on 127.0.0.1'
    env = {"COMFY_DIR": str(tmp_path), "INSTALL_FLUX3": "1"}
    touch_files(tmp_path, DEFAULT_SENTINELS)
    assert shell(code, {**env, "INSTALL_FLUX3": "0"}).returncode == 0
    assert shell(code, env).returncode != 0
    touch_files(tmp_path, FLUX3_FILES)
    assert shell(code, env).returncode == 0
    (tmp_path / FLUX3_FILES[missing_index]).unlink()
    assert shell(code, env).returncode != 0


def test_downloader_installs_registry_bundle_and_skips_present_files(tmp_path):
    # Exercise the real shell downloader with a local fake HF CLI; no network or weights.
    script = tmp_path / "scripts/download_models.sh"
    script.parent.mkdir()
    script.write_text((ROOT / "scripts/download_models.sh").read_text())
    hf = tmp_path / ".venv/bin/hf"
    hf.parent.mkdir(parents=True)
    hf.write_text(f"#!{sys.executable}\n" + '''import os, pathlib, sys
remote = sys.argv[3]
if os.environ.get("FAIL_MODEL") == remote:
    sys.exit(1)
dest = pathlib.Path(sys.argv[sys.argv.index("--local-dir") + 1]) / remote
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text("test weight")
with open(os.environ["HF_TEST_LOG"], "a") as log:
    log.write(remote + "\\n")
''')
    hf.chmod(0o755)
    models = tmp_path / "comfy"
    models.mkdir()
    log = tmp_path / "downloads.log"
    env = {"INSTALL_FLUX3": "1", "HF_TEST_LOG": str(log), "SKIP_LTX": "1",
           "SKIP_FLUX2": "1", "HF_TOKEN": "", "ENGINE_MODELS": ""}
    cmd = f'bash "{script}" "{models}"'
    proc = shell(cmd, env)
    assert proc.returncode == 0, proc.stderr
    for file in FLUX3_FILES:
        assert (models / file).is_file()
    initial = log.read_text()
    assert shell(cmd, env).returncode == 0
    assert log.read_text() == initial
    (models / FLUX3_FILES[1]).unlink()
    proc = shell(cmd, {**env, "FAIL_MODEL": ENGINE["models"][1]["remote"]})
    assert proc.returncode != 0
    assert "Model download complete" not in proc.stdout


@pytest.mark.parametrize("missing_index", [1, 3])
def test_worker_syncs_optional_bundle_even_when_default_models_exist(tmp_path, missing_index):
    source = (ROOT / "scripts/install_worker_container.sh").read_text()
    block = source[source.index("_flux3_models_present() {"):source.index("# ── 4. Stop")]
    dest = tmp_path / "worker"
    touch_files(dest, DEFAULT_SENTINELS)
    source_models = tmp_path / "source"
    touch_files(source_models, FLUX3_FILES)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rsync = bin_dir / "rsync"
    rsync.write_text(f"#!{sys.executable}\n" + '''import os, pathlib, shutil, sys
folder = pathlib.Path(sys.argv[-1])
source = pathlib.Path(os.environ["TEST_SOURCE_MODELS"]) / folder.name
if source.is_dir():
    shutil.copytree(source, folder, dirs_exist_ok=True)
''')
    rsync.chmod(0o755)
    env = {"REMOTE_MODELS": str(dest / "models"), "MODEL_SOURCE": "source",
           "TARGET": "localhost", "LOCAL": "true", "INSTALL_FLUX3": "1",
           "TEST_SOURCE_MODELS": str(source_models / "models"),
           "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
    code = '_sh() { bash -c "$*"; };\n' + block
    proc = shell(code, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "rsyncing" in proc.stdout
    assert all((dest / f).is_file() for f in FLUX3_FILES)
    assert "rsyncing" not in shell(code, env).stdout
    (dest / FLUX3_FILES[missing_index]).unlink()
    (source_models / FLUX3_FILES[missing_index]).unlink()
    proc = shell(code, env)
    assert proc.returncode != 0
    assert "bundle incomplete" in proc.stdout


def test_all_worker_install_paths_pin_the_native_flux3_revision():
    revision = "8713616fcc321624e306bbd36be5906203dcb0dc"
    for file in ("docker/comfyui/Dockerfile", "docker/docker-compose.yml",
                 "scripts/install_worker_container.sh"):
        assert revision in (ROOT / file).read_text()
    required = (ROOT / "docker/comfyui/required_nodes.txt").read_text()
    assert ENGINE["requires_node"] in required
    assert "ReferenceMultiLatent" in required


@pytest.mark.parametrize("key", ["flux2-klein", "qwen-image-2.1", "qwen-image-2.1-nvfp4"])
def test_other_image_engines_keep_original_prompts_and_resolution(tmp_path, key):
    wf, _, _ = generate(tmp_path, engine=engines.get(key))
    assert not {"TextGenerate", "BatchImagesNode", "ImageScale"} & {n["class_type"] for n in wf.values()}
    encodings = [n for n in wf.values() if n["class_type"] in ("CLIPTextEncode", "TextEncodeQwenImage21")]
    assert any('Alice holds a sign reading "hello".' in n["inputs"].values() for n in encodings)
    assert any(n["inputs"].get("width") == 1024 and n["inputs"].get("height") == 576 for n in wf.values())


def test_larger_flux3_canvas_is_not_reduced_before_sampling(tmp_path):
    wf, _, _ = generate(tmp_path, width=2048, height=2048)
    assert wf["6"]["inputs"] == {"width": 2048, "height": 2048, "batch_size": 1}


@pytest.mark.parametrize("missing_node", ENGINE["extra_nodes"])
def test_missing_upsampling_capability_is_refused_before_upload(tmp_path, missing_node):
    with mock.patch.object(comfyui, "comfy_node_exists", side_effect=lambda u, n: n != missing_node), \
         mock.patch.object(comfyui, "_upload_image") as upload:
        with pytest.raises(RuntimeError, match=missing_node):
            comfyui.generate_with_engine(ENGINE, "scene", tmp_path / "out.png", width=1024, height=576)
    upload.assert_not_called()


def test_settings_requires_recommended_version_even_with_all_nodes_and_weights():
    from webapp.backend import main as web
    url = "http://worker:8188"
    with mock.patch.object(web.gapp, "load_config", return_value={"comfy_workers": [url]}), \
         mock.patch("pipeline.worker_pool.queue_depth", return_value=0), \
         mock.patch.object(comfyui, "engine_model_present", return_value=True), \
         mock.patch.object(comfyui, "comfy_node_exists", return_value=True), \
         mock.patch.object(comfyui, "comfyui_version", return_value=(0, 41, 2)):
        result = web.list_engines()
    assert result["availability"][KEY] is False
    assert result["worker_updates"][KEY] == url
    assert result["availability"]["qwen-image-2.1-nvfp4"] is True
