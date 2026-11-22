"""Saved H3 4K frames remain the selected image when a film is rendered again."""
import json
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

import app
import resume_generation as rg
import webapp.backend.main as backend
from pipeline import assembler, comfyui, scene_video, worker_pool
from pipeline.orchestrator import DurableStore, job_id_from_work_dir, task_id


@pytest.fixture(params=[
    ("h3", "4k", "flux2-klein", "Landscape 720p (1280×720)", (1280, 704), (3840, 2112)),
    ("h3", "4k", "flux2-klein", "Portrait 720p (720×1280)", (704, 1280), (2112, 3840)),
    ("h3", "video", "flux2-klein", "Landscape 720p (1280×720)", (1280, 704), (1280, 704)),
    ("image", "video", "flux2-klein", "Landscape 720p (1280×720)", (1280, 704), (1280, 704)),
    ("image", "video", "qwen-image-2.1-turbo", "Landscape 720p (1280×720)", (1280, 704), (1280, 704)),
], ids=["h3-4k-landscape", "h3-4k-portrait", "h3-video", "default-image", "qwen-video"])
def saved_film(tmp_path, monkeypatch, request):
    source, size, image_engine, resolution, video_size, frame_size = request.param
    wd = tmp_path / "film-20261122-120000"
    wd.mkdir()
    row = {"id": 1, "title": "Firefly", "image_prompt": "A firefly on a leaf",
           "video_prompt": "A firefly glows", "narration": "A firefly glows."}
    cfg = {"title": "Firefly", "video_engine": "ltx25", "image_engine": image_engine,
           "first_frame_source": source, "h3_image_size": size, "resolution": resolution,
           "music_enabled": False, "tts_workers": ["tts"], "h3_silent_scenes": False}
    (wd / "script.json").write_text(json.dumps([row]))
    (wd / "job_config.json").write_text(json.dumps(cfg))
    (wd / "scene_01_narration.wav").write_bytes(b"a" * 2000)
    preview = wd / "scene_01_preview.png"
    Image.new("RGB", frame_size, (10, 20, 30)).save(preview)
    monkeypatch.setenv("SPIELBOT_ORCHESTRATOR_DB", str(tmp_path / "orchestrator.sqlite3"))
    monkeypatch.setattr(worker_pool, "LEASE_DIR", tmp_path / "leases")
    monkeypatch.setattr(app, "load_config", lambda: cfg.copy())
    monkeypatch.setattr(backend, "_film_tasks", {})
    monkeypatch.setattr(rg, "_PROGRESS_STORE", None)
    monkeypatch.setattr(rg, "_PROGRESS_JOB_ID", None)
    return wd, row, cfg, preview, video_size, frame_size


def _write_media(*args, **kwargs):
    destination = next(p for p in reversed(args) if isinstance(p, Path))
    destination.write_bytes(b"v" * 20_000)
    return destination


@pytest.fixture
def video_boundary(monkeypatch):
    """Exercise shared first-frame selection; replace only GPU and media I/O."""
    video = mock.Mock(side_effect=_write_media)
    image = mock.Mock(side_effect=AssertionError("Selected frame was regenerated"))
    monkeypatch.setattr(scene_video, "generate_video_with_engine", video)
    monkeypatch.setattr(scene_video, "generate_with_engine", image)
    monkeypatch.setattr(comfyui, "generate_with_engine", image)
    monkeypatch.setattr(scene_video, "_get_duration", lambda _: 5.0)
    monkeypatch.setattr(scene_video, "extract_audio", mock.Mock(side_effect=RuntimeError("no audio")))
    monkeypatch.setattr(assembler, "_get_duration", lambda _: 5.0)
    monkeypatch.setattr(assembler, "mux_video_audio", _write_media)
    return video, image


def _assert_video_used_selected_frame(video, image, preview, video_size, frame_size, original):
    image.assert_not_called()
    video.assert_called_once()
    assert video.call_args.args[3] == preview
    assert (video.call_args.kwargs["width"], video.call_args.kwargs["height"]) == video_size
    assert preview.read_bytes() == original
    with Image.open(preview) as selected:
        assert selected.size == frame_size


def test_resumed_film_reuses_selected_frame_at_original_video_resolution(
        saved_film, video_boundary, tmp_path, monkeypatch):
    wd, row, cfg, preview, video_size, frame_size = saved_film
    video, image = video_boundary
    original = preview.read_bytes()
    output = tmp_path / "out"
    output.mkdir()
    store = DurableStore.default()
    monkeypatch.setattr(rg.DurableStore, "default", lambda: store)
    monkeypatch.setattr(rg, "load_config", lambda: {})
    monkeypatch.setattr(rg, "OUTPUT_DIR", output)
    monkeypatch.setattr(rg, "alive_workers", lambda _: ["http://test-worker"])
    monkeypatch.setattr(rg.ui_activity, "is_active", lambda **kwargs: False)
    monkeypatch.setattr(rg, "_MAX_SCENE_ATTEMPTS", 1)
    monkeypatch.setattr(rg, "generate_cover_image", mock.Mock())
    monkeypatch.setattr(rg, "_get_duration", lambda _: 5.0)
    for name in ("mux_video_audio", "concatenate_scenes", "copy_video_with_attribution"):
        monkeypatch.setattr(rg, name, _write_media)
    monkeypatch.setattr(rg, "ensure_video_resolution", mock.Mock())
    try:
        rg.main(wd)
        _assert_video_used_selected_frame(video, image, preview, video_size, frame_size, original)
        jid = job_id_from_work_dir(wd)
        assert store.get_task(task_id(jid, "scene", 1, "image")).status == "succeeded"
        assert store.get_scene(jid, 1)["preview_path"] == str(preview)
        assert store.get_job(jid)["status"] == "done"
        assert len(list(output.glob("*.mp4"))) == 1
    finally:
        store.close()


def test_film_video_rerender_reuses_selected_frame_at_original_video_resolution(
        saved_film, video_boundary, monkeypatch):
    wd, row, cfg, preview, video_size, frame_size = saved_film
    video, image = video_boundary
    original = preview.read_bytes()
    pool = mock.Mock()
    pool.acquire.return_value = "http://test-worker"
    monkeypatch.setattr(backend, "_shared_edit_render_pool", lambda: pool)
    monkeypatch.setattr(backend.video_history, "record", mock.Mock())
    tid = "rerender-selected-frame"
    backend._film_tasks[tid] = {"status": "running"}

    backend._run_video_rerender(tid, wd, 1, cfg, {**row, "preview_path": str(preview)})

    assert backend._film_tasks[tid] == {"status": "done"}
    _assert_video_used_selected_frame(video, image, preview, video_size, frame_size, original)
    assert (wd / "scene_01_final.mp4").exists()
    assert not (wd / "scene_01_first_frame.png").exists()
    pool.acquire.assert_called_once()
    pool.release.assert_called_once_with("http://test-worker")


@pytest.mark.parametrize("size", [(2112, 3840), (3840, 2160), (1024, 576), (640, 352), None])
def test_reuse_rejects_wrong_canvas_or_unreadable_images(tmp_path, size):
    image = tmp_path / "frame.png"
    if size is None:
        image.write_bytes(b"not a readable image")
    else:
        Image.new("RGB", size).save(image)
    # Another orientation, a subtly different aspect, and arbitrary scaled
    # images must not be silently reused as a 1280x704 video opening.
    assert not rg._image_matches_resolution(image, 1280, 704)
    assert not backend._image_matches_resolution(image, 1280, 704)


def test_missing_saved_frame_is_not_reusable(tmp_path):
    image = tmp_path / "missing.png"
    assert not rg._image_matches_resolution(image, 1280, 704)
    assert not backend._image_matches_resolution(image, 1280, 704)
