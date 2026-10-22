"""Working-file cleanup keeps publish/edit inputs and only runs when opted in."""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app
import webapp.backend.main as backend
from pipeline import final_video_history, image_history, music_history, video_history, working_files
from pipeline import publish_queue as pq


def put(path, data=b"media"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def film(tmp_path, monkeypatch):
    root = tmp_path / "videos"
    wd = root / "film"
    wd.mkdir(parents=True)
    final = put(root / "film.mp4", b"final" * 4000)
    put(wd / "combined.mp4", b"combined")
    put(wd / "job_config.json", b"{}")
    put(wd / "job.json", json.dumps({"status": "done", "final_path": str(final)}).encode())
    for name in ("scene_01_final.mp4", "scene_01_video.mp4", "scene_01_clip_01.mp4",
                 "scene_01_preview.png", "scene_01_first_frame.png", "scene_01_narration.wav",
                 "scene_01_ambient.wav", "scene_01_track.wav", "background_music.wav",
                 "background_music_vocals.wav", "cover.png", "cover_bg.png",
                 "captions.srt", "script.json", "notes.txt", "unknown/large.mp4"):
        put(wd / name)
    monkeypatch.setattr(app, "OUTPUT_DIR", root)
    monkeypatch.setattr(pq, "PUBLISH_QUEUE_PATH", tmp_path / "publish_queue.json")
    monkeypatch.setattr(backend, "_film_tasks", {})
    monkeypatch.setattr(backend, "_film_task_meta", {})
    monkeypatch.setattr(backend, "_upload_tasks", {})
    monkeypatch.setattr(backend, "_x_post_tasks", {})
    monkeypatch.setattr(app, "_list_recent_jobs", lambda **kw: [("Film", str(wd))])
    return wd, final


def add_cache(wd):
    return put(wd / "final_upscale_scenes/flashvsr_2x-1080/scene_01_final.upscaled.mp4")


def test_preview_and_cleanup_preserve_current_assets_and_metadata(film):
    wd, final = film
    original = {p: p.read_bytes() for p in wd.rglob("*") if p.is_file()}
    for _ in range(2):
        image_history.record(wd, 1, wd / "scene_01_preview.png")
        image_history.cover_record(wd, wd / "cover.png")
        video_history.record(wd, 1, wd / "scene_01_final.mp4")
        final_video_history.record(wd, final, lang="pt", kind="upscale")
    music_history.record(wd, wd / "background_music.wav", desc="Unused")
    music_history.record(wd, wd / "background_music.wav", desc="Original singing")
    music_history.record(wd, wd / "background_music.wav", voice="Singer", source_id=2)
    cache = add_cache(wd)
    put(wd / "finish_upscale_scenes/scene_01_final.1080.up.mp4")
    put(wd / "final_upscale_scenes/keep.json", b"{}")
    preview = working_files.preview(wd, final)
    assert preview["files"] == 8  # old image, cover + background, video, final, music, 2 caches
    assert cache.exists()  # preview is read-only
    result = working_files.clean(wd, final)
    assert result == {"files": 8, "bytes": preview["bytes"], "errors": []}
    assert final.read_bytes() == b"final" * 4000
    assert all(p.read_bytes() == content for p, content in original.items())
    assert (wd / "final_upscale_scenes/keep.json").exists()
    assert image_history.history(wd, 1)["selected"] == 2
    assert len(image_history.history(wd, 1)["versions"]) == 1
    assert len(image_history.cover_history(wd)["versions"]) == 1
    assert len(video_history.history(wd, 1)["versions"]) == 1
    assert final_video_history.history(wd)["versions"][0]["lang"] == "pt"
    assert final_video_history.history(wd)["versions"][0]["kind"] == "upscale"
    assert [v["id"] for v in music_history.history(wd)["versions"]] == [2, 3]
    assert working_files.clean(wd, final)["files"] == 0
    # Histories remain usable after pruning, without recycling ids.
    video_history.record(wd, 1, wd / "scene_01_final.mp4")
    assert video_history.history(wd, 1)["selected"] == 3
    final_video_history.select(wd, 2, final)


def test_fallback_selection_and_final_inside_cache_are_kept(film):
    wd, _ = film
    final = add_cache(wd)
    final_video_history.record(wd, final, kind="upscale")
    final_video_history.record(wd, final, kind="upscale")
    path = wd / "final_video_history.json"
    data = json.loads(path.read_text())
    data["selected"] = 99
    path.write_text(json.dumps(data))
    working_files.clean(wd, final)
    assert final.exists()
    assert [v["id"] for v in final_video_history.history(wd)["versions"]] == [2]


def test_latest_base_cut_is_retained_for_repeat_upscaling(film):
    wd, final = film
    final_video_history.record(wd, final, label="Original")
    final_video_history.record(wd, final, label="Rebuilt from scenes")
    final_video_history.record(wd, final, kind="upscale")
    final_video_history.record(wd, final, kind="cover")
    working_files.clean(wd, final)
    history = final_video_history.history(wd)
    assert history["selected"] == 4
    assert [v["id"] for v in history["versions"]] == [2, 4]


def test_links_and_manifest_traversal_never_delete_other_files(film, tmp_path):
    wd, final = film
    outside = put(tmp_path / "outside.mp4")
    hist = wd / "video_history"
    hist.mkdir()
    (hist / "scene_01_v1.mp4").symlink_to(outside)
    put(hist / "scene_01_v2.mp4")
    (wd / "video_history.json").write_text(json.dumps({"1": {
        "selected": 2, "versions": [{"id": 0, "file": "../scene_01_final.mp4"},
                                    {"id": 1, "file": "scene_01_v1.mp4"},
                                    {"id": 2, "file": "scene_01_v2.mp4"}]}}))
    (wd / "final_upscale_scenes").symlink_to(tmp_path, target_is_directory=True)
    put(tmp_path / "scene_99.mp4")
    assert working_files.clean(wd, final)["files"] == 0
    assert outside.exists()
    assert (wd / "scene_01_final.mp4").exists()


def test_damaged_manifest_fails_before_any_deletion(film):
    wd, final = film
    cache = add_cache(wd)
    (wd / "image_history.json").write_text("{")
    with pytest.raises(json.JSONDecodeError):
        working_files.clean(wd, final)
    assert cache.exists()


def test_failed_deletion_is_reported_and_can_be_retried(film, monkeypatch):
    wd, final = film
    cache = add_cache(wd)
    unlink = Path.unlink
    def fail(path, **kw):
        if path == cache:
            raise PermissionError(13, "Permission denied")
        return unlink(path, **kw)
    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", fail)
        result = working_files.clean(wd, final)
    assert result["files"] == result["bytes"] == 0
    assert result["errors"] and cache.exists()
    assert working_files.clean(wd, final)["files"] == 1


def test_manual_is_default_and_cleanup_returns_actual_savings(film):
    wd, _ = film
    cache = add_cache(wd)
    backend._sweep_working_files()
    assert cache.exists()
    preview = backend.film_working_files(str(wd))
    assert preview["policy"] == "manual"
    result = backend.film_working_files_clean(backend.WorkingFilesBody(work_dir=str(wd)))
    assert result["result"]["bytes"] == 5
    assert result["reclaimable"]["files"] == 0
    assert not cache.exists()


@pytest.mark.parametrize("policy", ["after_approval", "after_upload"])
def test_automatic_waits_for_milestone_survives_restart_and_runs_once(film, policy):
    wd, _ = film
    cache = add_cache(wd)
    backend.film_working_files_policy(backend.WorkingFilesPolicyBody(work_dir=str(wd), policy=policy))
    assert cache.exists()
    if policy == "after_approval":
        backend._record_publish_approval(wd, True)
    else:
        app._write_job_meta(wd, youtube_video_id="uploaded", youtube_published_at=123)
    backend._sweep_working_files()  # only persisted state is needed
    assert not cache.exists()
    assert backend._working_files_state(wd)["last_cleanup"]["automatic"]
    cache = add_cache(wd)
    backend._sweep_working_files()
    assert cache.exists()  # later editing is left alone until a fresh milestone


@pytest.mark.parametrize("platform", ["youtube", "x"])
def test_enabling_after_existing_upload_runs_now(film, platform):
    wd, _ = film
    cache = add_cache(wd)
    app._write_job_meta(wd, **({"youtube_video_id": "yt"} if platform == "youtube" else {"x_tweet_id": "x"}))
    result = backend.film_working_files_policy(backend.WorkingFilesPolicyBody(work_dir=str(wd), policy="after_upload"))
    assert not cache.exists()
    assert result["last_cleanup"]["files"] == 1


@pytest.mark.parametrize("busy", ["render", "edit", "youtube", "x", "reassembly"])
def test_busy_film_defers_cleanup_and_retries(film, busy):
    wd, _ = film
    cache = add_cache(wd)
    backend._record_publish_approval(wd, True)
    if busy == "render":
        app._write_job_meta(wd, status="running")
    elif busy == "edit":
        backend._film_task_meta["task"] = {"work_dir": str(wd)}
        backend._film_tasks["task"] = {"status": "running"}
    elif busy == "youtube":
        backend._upload_tasks["task"] = {"status": "uploading", "work_dir": str(wd)}
    elif busy == "x":
        backend._x_post_tasks["task"] = {"status": "posting", "work_dir": str(wd)}
    lock = backend._reassemble_lock(wd)
    if busy == "reassembly":
        lock.acquire()
    try:
        with pytest.raises(HTTPException) as exc:
            backend.film_working_files_clean(backend.WorkingFilesBody(work_dir=str(wd)))
        assert exc.value.status_code == 409
        if busy == "reassembly":
            with pytest.raises(HTTPException) as setting_error:
                backend.film_working_files_policy(
                    backend.WorkingFilesPolicyBody(work_dir=str(wd), policy="after_approval"))
            assert setting_error.value.status_code == 409
        backend._save_working_files_state(wd, {"policy": "after_approval"})
        backend._maybe_clean_working_files(wd)
        assert cache.exists()
    finally:
        if busy == "reassembly":
            lock.release()
    app._write_job_meta(wd, status="done")
    backend._film_tasks.clear()
    backend._upload_tasks.clear()
    backend._x_post_tasks.clear()
    backend._sweep_working_files()
    assert not cache.exists()


def test_missing_final_and_unsafe_folders_cannot_be_cleaned(film, tmp_path):
    wd, final = film
    cache = add_cache(wd)
    final.unlink()
    # A nearby unrelated export must not satisfy the final-video gate.
    put(wd.parent / "unrelated-export.mp4", b"other" * 4000)
    with pytest.raises(HTTPException) as exc:
        backend.film_working_files_clean(backend.WorkingFilesBody(work_dir=str(wd)))
    assert exc.value.status_code == 409
    assert cache.exists()
    for path in [wd.parent, tmp_path, wd / "video_history"]:
        with pytest.raises(HTTPException):
            backend.film_working_files(str(path))
    with pytest.raises(ValidationError):
        backend.WorkingFilesPolicyBody(work_dir=str(wd), policy="always")


def test_turning_off_pending_cleanup_preserves_files(film):
    wd, _ = film
    cache = add_cache(wd)
    for policy in ("after_approval", "manual"):
        backend.film_working_files_policy(backend.WorkingFilesPolicyBody(work_dir=str(wd), policy=policy))
    backend._record_publish_approval(wd, True)
    backend._sweep_working_files()
    assert cache.exists()


def test_publish_approval_hook_cleans_only_when_approved(film, monkeypatch):
    wd, _ = film
    cache = add_cache(wd)
    entry = pq.add_item(str(wd), title="Film")
    monkeypatch.setattr(backend, "_ensure_publish_entry", lambda p: entry)
    backend._save_working_files_state(wd, {"policy": "after_approval"})
    backend.publish_approve(backend.PublishApproveBody(work_dir=str(wd), approved=False))
    assert cache.exists()
    backend.publish_approve(backend.PublishApproveBody(work_dir=str(wd), approved=True))
    assert not cache.exists()


@pytest.mark.parametrize("succeeds", [False, True])
def test_youtube_upload_hook_only_cleans_after_success(film, monkeypatch, succeeds):
    wd, final = film
    cache = add_cache(wd)
    backend._save_working_files_state(wd, {"policy": "after_upload"})
    monkeypatch.setattr(app, "load_config", lambda: {})
    monkeypatch.setattr(backend, "_client_secrets_path", lambda: "unused")
    monkeypatch.setattr(backend, "_upload_prefs_for_channel", lambda *a: ("en", False))
    monkeypatch.setattr(backend, "_resolve_upload_playlist", lambda *a: "")
    monkeypatch.setattr(backend, "_youtube_tags_for", lambda *a: [])
    monkeypatch.setattr(backend, "_post_completion_reply", lambda *a: {})
    monkeypatch.setattr(backend._c2pa, "sign_if_enabled", lambda *a: None)
    def upload(**kwargs):
        if not succeeds:
            raise RuntimeError("Upload failed")
        return {"video_id": "test-upload"}
    monkeypatch.setattr(backend.yt, "upload_video", upload)
    backend._upload_tasks["test"] = {"status": "uploading", "work_dir": str(wd)}
    backend._run_upload_task("test", {"title": "Test", "description": "", "category": "22",
                                      "privacy": "private", "channel": "test"}, wd, final, None)
    assert backend._upload_tasks["test"]["status"] == ("done" if succeeds else "error")
    assert cache.exists() is not succeeds


@pytest.mark.parametrize("succeeds", [False, True])
def test_x_upload_hook_only_cleans_after_success(film, monkeypatch, succeeds):
    wd, final = film
    cache = add_cache(wd)
    backend._save_working_files_state(wd, {"policy": "after_upload"})
    monkeypatch.setattr(app, "load_config", lambda: {})
    monkeypatch.setattr(backend, "_x_client_creds", lambda: ("unused", "unused"))
    monkeypatch.setattr(backend, "_upload_prefs_for_channel", lambda *a: ("en", False))
    monkeypatch.setattr(backend, "_x_hashtags_for", lambda *a: "")
    monkeypatch.setattr(backend._c2pa, "sign_if_enabled", lambda *a: None)
    monkeypatch.setattr(backend.xt, "check_auth_status", lambda *a, **kw: {"premium": True})
    monkeypatch.setattr(backend.xt, "post_video", lambda *a, **kw:
                        {"tweet_id": "test-post"} if succeeds else {"error": "Upload failed"})
    backend._x_post_tasks["test"] = {"status": "posting", "work_dir": str(wd)}
    backend._run_x_post_task("test", {"title": "Test", "text": "Test", "account": "test"}, wd, final)
    assert backend._x_post_tasks["test"]["status"] == ("done" if succeeds else "error")
    assert cache.exists() is not succeeds
