"""Native H3 still graphs, exact saved canvases and safe replacement."""
import json
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image, ImageCms, PngImagePlugin, UnidentifiedImageError

from pipeline import comfyui, engines
from pipeline.scene_image import read_cast


@pytest.fixture
def engine():
    return {**engines.get("minimax-h3-image"), "h3_image_size": "video"}


@pytest.fixture(autouse=True)
def current_worker(monkeypatch):
    monkeypatch.setattr(comfyui, "comfyui_version", lambda *a, **kw: (0, 45, 0))
    monkeypatch.setattr(comfyui, "comfy_node_exists", lambda *a, **kw: True)


@pytest.mark.parametrize("mode,size,expected", [
    ("video", (1000, 568), (1000, 568)),
    ("video", (720, 1280), (720, 1280)),
    ("4k", (1280, 720), (3840, 2160)),
    ("4k", (720, 1280), (2160, 3840)),
    ("4k", (832, 480), (3840, 2215)),
])
def test_native_still_saves_requested_canvas_without_video(tmp_path, engine, mode, size, expected):
    out = tmp_path / "new" / "opening.png"
    prompt = 'Alice holds a sign reading "HELLO".\nWarm evening light.'
    captured = {}

    def render(workflow, path, url, **kwargs):
        captured.update(workflow=workflow, path=path, url=url, **kwargs)
        canvas = workflow["4"]["inputs"]
        Image.new("RGB", (canvas["width"], canvas["height"]), (20, 90, 140)).save(path)
        return path

    with mock.patch.object(comfyui, "_run_and_save", side_effect=render), \
         mock.patch.object(comfyui, "_h3_submit") as video, \
         mock.patch.object(comfyui, "_upload_image") as upload:
        result = comfyui.generate_with_engine(
            {**engine, "h3_image_size": mode}, prompt, out,
            width=size[0], height=size[1], seed=0, comfy_url="http://worker:8188")
    assert result == out
    with Image.open(out) as image:
        assert image.size == expected
        assert image.format == "PNG"
        assert image.getpixel((0, 0)) == (20, 90, 140)
    wf = captured["workflow"]
    assert wf["1"]["inputs"]["unet_name"] == engine["model_file"]
    assert wf["2"]["inputs"]["clip_name"] == engine["clip_t5"]
    assert wf["2"]["inputs"]["type"] == "minimax"
    assert wf["3"]["inputs"]["vae_name"] == engine["vae"]
    assert wf["4"]["class_type"] == "MiniMaxH3ImageConditioning"
    assert wf["4"]["inputs"]["prompt"] == prompt
    assert wf["4"]["inputs"]["vae"] == ["3", 0]
    for actual, target in zip((wf["4"]["inputs"]["width"], wf["4"]["inputs"]["height"]), expected):
        assert actual % 32 == 0
        assert abs(actual - target) <= 16
    sampler = wf["6"]["inputs"]
    assert (sampler["steps"], sampler["cfg"], sampler["seed"]) == (6, 1.0, 0)
    assert (sampler["sampler_name"], sampler["scheduler"]) == ("euler", "simple")
    assert sampler["latent_image"] == ["4", 1]
    assert sampler["negative"] == ["5", 0]
    assert wf["5"]["class_type"] == "ConditioningZeroOut"
    assert wf["8"]["class_type"] == "SaveImage"
    assert "{{" not in json.dumps(wf)
    assert not any("Video" in n["class_type"] or n["class_type"] == "TextGenerate" for n in wf.values())
    assert captured["timeout"] == 1800
    assert captured["heartbeat_warmup"] == 600
    assert captured["url"] == "http://worker:8188"
    assert captured["path"] != out
    assert not captured["path"].parent.exists()
    video.assert_not_called()
    upload.assert_not_called()


def test_all_sixteen_references_keep_attachment_order(tmp_path, engine):
    refs = [Path(f"person{i}.png") for i in range(1, 17)]
    captured = {}

    def render(workflow, path, *args, **kwargs):
        captured.update(workflow)
        Image.new("RGB", (64, 64), "white").save(path)
        return path

    with mock.patch.object(comfyui, "_run_and_save", side_effect=render), \
         mock.patch.object(comfyui, "_upload_image", side_effect=lambda p, **kw: p.name) as upload:
        comfyui.generate_with_engine(engine, "<Picture 1> beside <Picture 16>.", tmp_path / "out.png",
                                     width=64, height=64, seed=7, reference_images=refs)
    assert [call.args[0] for call in upload.call_args_list] == refs
    for i, ref in enumerate(refs, 1):
        node = str(8 + i)
        assert captured["4"]["inputs"][f"images.image_{i}"] == [node, 0]
        assert captured[node] == {"class_type": "LoadImage", "inputs": {"image": ref.name}}
    assert "images.image_17" not in captured["4"]["inputs"]


@pytest.mark.parametrize("mode,expected", [("video", (1000, 568)), ("4k", (3840, 2181))])
@pytest.mark.parametrize("explicit_cast", [False, True], ids=["graph-provenance", "recorded-cast"])
def test_resized_still_preserves_png_metadata_and_character_provenance(
        tmp_path, engine, mode, expected, explicit_cast):
    out = tmp_path / "opening.png"
    text = {"workflow": json.dumps({"title": "Ana's café scene"})}
    if explicit_cast:
        text["spielbot_scene_cast"] = json.dumps([{"id": "ana", "name": "Ana"}])
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = Image.Exif()
    exif[315] = "Spielbot"

    def render(workflow, path, *args, **kwargs):
        text["prompt"] = json.dumps(workflow)
        metadata = PngImagePlugin.PngInfo()
        for key, value in text.items():
            metadata.add_text(key, value)
        canvas = workflow["4"]["inputs"]
        Image.new("RGB", (canvas["width"], canvas["height"]), "blue").save(
            path, pnginfo=metadata, icc_profile=profile, dpi=(144, 144), exif=exif)
        return path

    with mock.patch.object(comfyui, "_run_and_save", side_effect=render), \
         mock.patch.object(comfyui, "_upload_image", return_value="ana.png"):
        comfyui.generate_with_engine(
            {**engine, "h3_image_size": mode}, "Ana from <Picture 1> waits.", out,
            width=1000, height=568, reference_images=[tmp_path / "ana.png"])

    with Image.open(out) as image:
        assert image.size == expected
        assert image.text == text
        assert image.info["icc_profile"] == profile
        assert image.info["dpi"] == pytest.approx((144, 144), abs=0.02)
        assert image.getexif()[315] == "Spielbot"
    assert read_cast(out, [{"id": "ana", "name": "Ana", "ref_image": "ana.png"}]) == ["Ana"]


def test_excess_references_are_rejected_before_upload(tmp_path, engine):
    with mock.patch.object(comfyui, "_upload_image") as upload, \
         mock.patch.object(comfyui, "_run_and_save") as run, \
         pytest.raises(ValueError, match="up to 16"):
        comfyui.generate_with_engine(engine, "cast", tmp_path / "out.png", width=64, height=64,
                                     reference_images=[Path(f"{i}.png") for i in range(17)])
    upload.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("corrupt", [False, True])
def test_failed_or_invalid_download_preserves_selected_image(tmp_path, engine, corrupt):
    out = tmp_path / "opening.png"
    Image.new("RGB", (64, 64), "red").save(out)
    original = out.read_bytes()
    staged = []

    def fail(workflow, path, *args, **kwargs):
        assert out.read_bytes() == original
        staged.append(path)
        path.write_bytes(b"partial or corrupt download")
        if not corrupt:
            raise RuntimeError("download failed")
        return path

    with mock.patch.object(comfyui, "_run_and_save", side_effect=fail), \
         pytest.raises(UnidentifiedImageError if corrupt else RuntimeError):
        comfyui.generate_with_engine(engine, "scene", out, width=832, height=480)
    assert out.read_bytes() == original
    assert not staged[0].parent.exists()


@pytest.mark.parametrize("version,node", [((0, 44, 0), True), ((0, 45, 0), False), ((), None)])
def test_unsupported_worker_fails_before_upload_or_render(tmp_path, engine, version, node):
    with mock.patch.object(comfyui, "comfyui_version", return_value=version), \
         mock.patch.object(comfyui, "comfy_node_exists", return_value=node), \
         mock.patch.object(comfyui, "_upload_image") as upload, \
         mock.patch.object(comfyui, "_run_and_save") as run, \
         pytest.raises(RuntimeError, match="H3 Image"):
        comfyui.generate_with_engine(engine, "scene", tmp_path / "out.png", width=64, height=64,
                                     reference_images=[Path("a.png")])
    upload.assert_not_called()
    run.assert_not_called()


def test_image_wait_honours_h3_cold_loading_budget(tmp_path):
    with mock.patch.object(comfyui, "_queue_prompt", return_value="prompt"), \
         mock.patch.object(comfyui, "_wait_for_completion") as wait, \
         mock.patch.object(comfyui, "_get_outputs", return_value=[{"filename": "out.png"}]), \
         mock.patch.object(comfyui, "_download_output"):
        comfyui._run_and_save({}, tmp_path / "out.png", "http://worker", timeout=1800,
                              heartbeat_warmup=600)
    assert wait.call_args.kwargs == {"timeout": 1800, "comfy_url": "http://worker", "heartbeat_warmup": 600}
