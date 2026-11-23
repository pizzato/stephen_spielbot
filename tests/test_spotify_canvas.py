import json
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw

import app
from pipeline import comfyui, spotify_canvas, working_files
from pipeline.assembler import _FFPROBE
from webapp.backend import main as backend


@pytest.fixture
def music_video(tmp_path, monkeypatch):
    wd = tmp_path / "music-video"
    wd.mkdir()
    (wd / "song.json").write_text("{}")
    (wd / "job_config.json").write_text("{}")
    frame = Image.new("RGB", (320, 480), "#183146")
    ImageDraw.Draw(frame).rectangle((100, 120, 220, 300), fill="#963451")
    frame.save(wd / "scene_01_first_frame.png")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    return wd


def test_default_canvas_encoding_and_download_after_cleanup(music_video):
    wd = music_video
    path = wd / spotify_canvas.FILENAME
    spotify_canvas.finish_music_video(wd, wd / "combined.mp4")
    probe = json.loads(subprocess.check_output([
        _FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ]))
    assert len(probe["streams"]) == 1
    video = probe["streams"][0]
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 576, 1024)
    assert video["display_aspect_ratio"] == "9:16"
    assert float(probe["format"]["duration"]) == 6
    final = wd.parent / "music-video.mp4"
    shutil.copyfile(path, final)
    (wd / "combined.mp4").touch()
    original = path.read_bytes()
    working_files.clean(wd, final)
    spotify_canvas.finish_music_video(wd, final)
    assert not (wd / "combined.mp4").exists()
    assert path.read_bytes() == original
    state = backend.film_spotify_canvas(str(wd))
    assert state["download_url"].endswith("&download=true")
    response = backend.film_spotify_canvas_file(str(wd), download=True)
    assert response.path == path
    assert "music-video-spotify-canvas.mp4" in response.headers["content-disposition"]


def test_prompted_canvas_preserves_download_if_render_fails(music_video, monkeypatch):
    wd = music_video
    path = wd / spotify_canvas.FILENAME
    path.write_bytes(b"old Canvas")
    final = wd.parent / "music-video.mp4"
    calls = []
    should_fail = True

    class Worker:
        urls = ["worker"]

        def acquire(self, *, allowed):
            assert allowed == self.urls
            return self.urls[0]

        def release(self, url):
            assert url == self.urls[0]
            calls.append("released")

    def image(engine, prompt, output, **kwargs):
        assert "curtains sway" in prompt
        shutil.copyfile(kwargs["reference_images"][0], output)

    def motion(engine, prompt, negative, frame, output, **kwargs):
        assert frame.is_file() and "curtains sway" in prompt
        assert kwargs["duration_seconds"] == 3
        output.write_bytes(b"generated motion")

    def render(source, output):
        assert source.read_bytes() == b"generated motion"
        output.write_bytes(b"new Canvas")
        if should_fail:
            raise RuntimeError("Encoding failed")

    monkeypatch.setattr(backend, "_film_tasks", {"task": {"status": "running"}})
    monkeypatch.setattr(backend, "_shared_edit_render_pool", Worker)
    monkeypatch.setattr(app, "load_config", lambda: {})
    monkeypatch.setattr(app, "scene_frame_engine", lambda *_: {})
    monkeypatch.setattr(backend, "_resolve_video_for_job", lambda *_: {})
    monkeypatch.setattr(comfyui, "generate_with_engine", image)
    monkeypatch.setattr(comfyui, "generate_video_with_engine", motion)
    monkeypatch.setattr(spotify_canvas, "render_loop", render)

    backend._run_spotify_canvas("task", wd, final, "curtains sway")
    assert backend._film_tasks["task"]["status"] == "error"
    assert path.read_bytes() == b"old Canvas"
    assert calls == ["released"]
    should_fail = False
    backend._film_tasks["task"] = {"status": "running"}
    backend._run_spotify_canvas("task", wd, final, "curtains sway")
    assert backend._film_tasks["task"]["status"] == "done"
    assert path.read_bytes() == b"new Canvas"
