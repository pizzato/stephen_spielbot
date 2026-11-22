"""pi-H3's released schedule, worker readiness and single-clip contract."""
import importlib.util
import json
import math
from pathlib import Path
import types
from unittest import mock

import pytest

import app
from pipeline import cadence, comfyui, engines

KEY = "minimax-h3-pi-dwd2"
ENGINE = engines.get_video(KEY)


def test_optional_engine_preserves_fixed_steps_and_reference_defaults():
    assert engines.DEFAULT_VIDEO_ENGINE == "ltx25"
    assert app._norm_video_engine(KEY) == KEY
    assert engines.resolve_video({"video_steps": 20}, KEY)["steps"] == 2
    assert engines.resolve_reference({}, KEY)["key"] == engines.DEFAULT_REFERENCE_ENGINE
    public = next(e for e in engines.public_list_video() if e["key"] == KEY)
    assert public["fixed_steps"] == 2
    assert public["supports_chaining"] is False
    assert public["downloadable"]
    assert KEY not in {e["key"] for e in engines.public_list_reference()}
    assert {ENGINE[k] for k in ("unet", "clip", "video_vae", "audio_vae", "adapter")} == {
        m["file"] for m in ENGINE["models"]}


def test_real_dispatch_uses_tested_schedule_and_ignores_inherited_chaining(tmp_path):
    with mock.patch.object(comfyui, "compatible_video_workers", return_value=["http://ready"]), \
         mock.patch.object(comfyui, "_upload_image", return_value="first.png"), \
         mock.patch.object(comfyui, "_queue_prompt", return_value="pid") as queue, \
         mock.patch.object(comfyui, "_wait_for_completion"), \
         mock.patch.object(comfyui, "_get_outputs", return_value=[{"type": "output"}]), \
         mock.patch.object(comfyui, "_download_output", return_value=tmp_path / "out.mp4"), \
         mock.patch.object(comfyui, "generate_video_h3_chained") as chain:
        comfyui.generate_video_with_engine(
            engines.resolve_video({"video_steps": 8}, KEY), 'A robot waves "hello".',
            "negative prompt has no H3 path", tmp_path / "first.png", tmp_path / "out.mp4",
            width=704, height=1280, duration_seconds=5, seed=0, chained=True,
            comfy_url="http://ready")
    chain.assert_not_called()
    graph = queue.call_args.args[0]
    assert "{{" not in json.dumps(graph)
    assert graph["2"]["inputs"]["adapter_name"] == ENGINE["adapter"]
    assert graph["2"]["inputs"]["adapter_strength"] == 1
    assert graph["6"]["inputs"]["length"] == 124
    assert graph["6"]["inputs"]["prompt"].startswith('A robot waves "hello".')
    assert graph["8"]["inputs"] == {
        "model": ["20", 0], "steps": 2, "min_raw_t": 0.0,
        "max_raw_t": 0.93, "final_step_size_scale": 0.6}
    assert graph["10"]["class_type"] == "SpielbotDWDNoise"
    assert graph["10"]["inputs"]["noise_seed"] == 0
    assert graph["12"]["class_type"] == "DWD Decode"
    assert graph["12"]["inputs"]["samples"] == ["11", 0]
    assert graph["13"]["class_type"] == "VAEDecodeAudio"
    assert graph["19"]["inputs"]["shift_video"] == 12
    assert graph["19"]["inputs"]["shift_audio"] == 3
    assert graph["20"]["class_type"] == "SolAttnPatch"
    assert graph["20"]["inputs"] == {
        "model": ["19", 0], "tau": 1.0, "start_percent": 0.0,
        "end_percent": 1.0, "min_tokens": 0, "int8_qk": False,
        "sink_conditioning": "exact_kv_and_rows", "morton": False,
        "morton_curve": "2d_frame", "int8_pv": False, "verbose": False,
        "use_tma": False, "dense_blocks": "0-1"}


@pytest.mark.parametrize("missing", ["version", "unknown_version", *ENGINE["extra_nodes"],
                                    *(p[2] for p in ENGINE["extra_probes"])])
def test_incomplete_worker_refused_before_upload_or_submission(tmp_path, missing):
    version = {"version": (0, 42, 5), "unknown_version": ()}.get(missing, (0, 45, 0))
    with mock.patch.object(comfyui, "comfyui_version", return_value=version), \
         mock.patch.object(comfyui, "comfy_node_exists", side_effect=lambda url, n: n != missing), \
         mock.patch.object(comfyui, "engine_model_present", side_effect=lambda url, p: p[2] != missing), \
         mock.patch.object(comfyui, "_upload_image") as upload, \
         mock.patch.object(comfyui, "_queue_prompt") as queue:
        with pytest.raises(RuntimeError, match="INSTALL_PIH3=1"):
            comfyui.generate_video_h3(ENGINE, "hello", tmp_path / "in.png", tmp_path / "out.mp4")
    upload.assert_not_called()
    queue.assert_not_called()


def test_worker_selection_and_settings_find_ready_second_worker():
    from webapp.backend import main as web
    urls = ["http://old", "http://ready"]
    with mock.patch.object(web.gapp, "load_config", return_value={"comfy_workers": urls}), \
         mock.patch("pipeline.worker_pool.queue_depth", return_value=0), \
         mock.patch.object(comfyui, "comfyui_version", side_effect=lambda u: (0, 45, 0) if u.endswith("ready") else (0, 42, 5)), \
         mock.patch.object(comfyui, "comfy_node_exists", return_value=True), \
         mock.patch.object(comfyui, "engine_model_present", return_value=True):
        assert comfyui.compatible_video_workers(urls, ENGINE) == [urls[1]]
        assert web.list_engines()["video_availability"][KEY] is True


@pytest.mark.parametrize("outdated", [True, False])
def test_settings_distinguishes_worker_rebuild_from_model_download(outdated):
    from webapp.backend import main as web
    url = "http://worker"
    with mock.patch.object(web.gapp, "load_config", return_value={"comfy_workers": [url]}), \
         mock.patch("pipeline.worker_pool.queue_depth", return_value=0), \
         mock.patch.object(comfyui, "comfyui_version", return_value=(0, 42, 5) if outdated else (0, 45, 0)), \
         mock.patch.object(comfyui, "comfy_node_exists", return_value=not outdated), \
         mock.patch.object(comfyui, "engine_model_present", return_value=False):
        result = web.list_engines()
    assert result["video_availability"][KEY] is False
    assert result["worker_updates"].get(KEY) == (url if outdated else None)


def test_spaced_node_names_are_url_encoded():
    node = "Load LakonLab Adapter"
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({node: {
        "input": {"required": {"adapter_name": [[ENGINE["adapter"]]]}}}}).encode()
    with mock.patch.object(comfyui.urllib.request, "urlopen", return_value=response) as opened:
        assert comfyui.comfy_node_exists("http://worker", node)
        assert comfyui.engine_model_present("http://worker", ENGINE["extra_probes"][-1])
    assert all(call.args[0].endswith("/Load%20LakonLab%20Adapter") for call in opened.call_args_list)


def test_planning_and_rerenders_ignore_pi_chain_flag_but_keep_acted_chains():
    from webapp.backend import main as web
    settings = {"video_engine": KEY, "h3_chain_scenes": True}
    plan = app.style_script_plan(settings, minutes=1)
    assert plan["chained"] is False
    assert plan["chained_acted"] is True
    assert cadence.engine_scene_ceiling(KEY, True) == cadence.engine_scene_ceiling(KEY, False)
    with mock.patch.object(web.gapp, "style_settings", return_value=settings):
        assert web._chain_scenes_for_job({}, {}) is False
    with pytest.raises(ValueError, match="does not support"):
        comfyui.generate_video_h3_chained(ENGINE, "p", Path("in"), Path("out"))


def test_switch_from_chained_h3_requires_replan_even_with_toggle_still_on(tmp_path):
    from webapp.backend import main as web
    with mock.patch.object(web, "_read_create_brief", return_value={"style_name": "Test"}), \
         mock.patch.object(web.gapp, "load_config", return_value={}), \
         mock.patch.object(web.gapp, "style_settings", return_value={"video_engine": KEY, "h3_chain_scenes": True}), \
         mock.patch.object(web, "_story_style_plan", return_value={"n_scenes": 8, "scene_secs_target": 10}):
        result = web._chain_plan_mismatch(tmp_path, {"scene_plan": {
            "chained": True, "n_scenes": 4, "scene_secs_target": 20, "minutes": 1}})
    assert result is not None
    assert result["replan_scenes"] == 8


def test_dwd_noise_preserves_seed_but_never_reuses_expired_pixels():
    native = types.ModuleType("comfy_extras.nodes_custom_sampler")
    native.Noise_RandomNoise = mock.Mock(side_effect=lambda seed: ("native-noise", seed))
    path = Path(__file__).resolve().parents[1] / "docker/comfyui/pih3/__init__.py"
    spec = importlib.util.spec_from_file_location("spielbot_pih3_test", path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {"comfy_extras.nodes_custom_sampler": native}):
        spec.loader.exec_module(module)
    node = module.NODE_CLASS_MAPPINGS["SpielbotDWDNoise"]()
    assert node.get_noise(42) == (("native-noise", 42),)
    assert node.get_noise(42) == (("native-noise", 42),)
    assert math.isnan(node.IS_CHANGED(42))
