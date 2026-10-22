"""Cleanup retains reusable assets; only the owning style controls automation."""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import app
import webapp.backend.main as backend
from pipeline import captions, final_video_history, image_history, music_history, video_history, working_files
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
    put(wd / "job_config.json", b'{"style_name": "Example"}')
    cfg = {"styles": [{"name": "Example"}], "default_style": "Example"}
    monkeypatch.setattr(app, "load_config", lambda: cfg)
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
    assert preview["files"] == 13  # 4 working videos, 3 old images, 4 video versions, 2 caches
    assert cache.exists()  # preview is read-only
    result = working_files.clean(wd, final)
    assert result["files"] == 13
    assert result["bytes"] == preview["bytes"]
    assert result["errors"] == []
    assert {f["name"] for f in result["removed"]} == {
        f["name"] for group in preview["groups"] for f in group["items"]}
    assert final.read_bytes() == b"final" * 4000
    expendable = {"combined.mp4", "scene_01_final.mp4", "scene_01_video.mp4", "scene_01_clip_01.mp4"}
    assert all(p.read_bytes() == content for p, content in original.items() if p.name not in expendable)
    assert all(not (wd / name).exists() for name in expendable)
    assert (wd / "final_upscale_scenes/keep.json").exists()
    assert image_history.history(wd, 1)["selected"] == 2
    assert len(image_history.history(wd, 1)["versions"]) == 1
    assert len(image_history.cover_history(wd)["versions"]) == 1
    assert video_history.history(wd, 1)["versions"] == []
    assert final_video_history.history(wd)["versions"] == []
    assert [v["id"] for v in music_history.history(wd)["versions"]] == [1, 2, 3]
    assert working_files.clean(wd, final)["files"] == 0


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
    assert final_video_history.history(wd)["versions"] == []


def test_selected_cut_metadata_survives_without_duplicate_videos(film):
    wd, final = film
    final_video_history.record(wd, final, lang="en")
    final_video_history.record(wd, final, lang="pt", kind="upscale")
    backend.film_working_files_clean(backend.WorkingFilesBody(work_dir=str(wd)))
    state = backend._working_files_state(wd)
    assert state["retained_cut"]["lang"] == "pt"
    assert state["retained_cut"]["kind"] == "upscale"
    assert backend._published_cut_language(wd, "en") == "pt"
    assert final_video_history.history(wd)["versions"] == []
    assert backend.remix_load(str(wd))["working_files_cleaned"]
    assert not backend.remix_load(str(wd))["can_remix"]
    assert app._list_recent_jobs() == [("Film", str(wd))]
    assert app._list_resumable_jobs() == []
    assert not app._is_job_running()
    assert app._status_for_work_dir(wd)[0] == 100
    assert backend.progress(str(wd))["done"]
    assert backend._stale_final_films() == []
    with pytest.raises(ValueError, match="cleaned up"):
        backend._reassemble_film_core(wd)
    with pytest.raises(HTTPException, match="cleaned up"):
        backend.remix_apply(backend.RemixBody(work_dir=str(wd)))


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
    put(wd / "keep.wav")
    (wd / "scene_99_final.mp4").symlink_to(outside)
    result = working_files.clean(wd, final)
    assert outside.exists() and (tmp_path / "scene_99.mp4").exists()
    assert (wd / "scene_99_final.mp4").is_symlink()
    assert (wd / "keep.wav").exists()
    assert all(".." not in f["name"] for f in result["removed"])


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
    assert result["files"] == 4  # unrelated candidates still removed
    assert result["errors"] and cache.exists()
    assert working_files.clean(wd, final)["files"] == 1


def test_manual_is_default_and_cleanup_returns_actual_savings(film):
    wd, _ = film
    cache = add_cache(wd)
    backend._sweep_working_files()
    assert cache.exists()
    preview = backend.film_working_files(str(wd))
    assert "policy" not in preview
    result = backend.film_working_files_clean(backend.WorkingFilesBody(work_dir=str(wd)))
    assert result["result"]["bytes"] == preview["reclaimable"]["bytes"]
    assert result["reclaimable"]["files"] == 0
    assert not cache.exists()


@pytest.mark.parametrize("policy", ["after_approval", "after_upload"])
def test_automatic_waits_for_milestone_survives_restart_and_runs_once(film, policy):
    wd, _ = film
    cache = add_cache(wd)
    app.load_config()["styles"][0]["working_files_cleanup"] = policy
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
    app.load_config()["styles"][0]["working_files_cleanup"] = "after_upload"
    backend._sweep_working_files()
    result = backend._working_files_state(wd)
    assert not cache.exists()
    assert result["last_cleanup"]["files"] == 5


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
        app.load_config()["styles"][0]["working_files_cleanup"] = "after_approval"
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


def test_turning_off_pending_cleanup_preserves_files(film):
    wd, _ = film
    cache = add_cache(wd)
    for policy in ("after_approval", "off"):
        app.load_config()["styles"][0]["working_files_cleanup"] = policy
    backend._record_publish_approval(wd, True)
    backend._sweep_working_files()
    assert cache.exists()


def test_legacy_video_preferences_and_other_styles_never_enable_cleanup(film):
    wd, _ = film
    cache = add_cache(wd)
    backend._save_working_files_state(wd, {"policy": "after_approval"})
    backend._record_publish_approval(wd, True)
    cfg = app.load_config()
    cfg["styles"].append({"name": "Other", "working_files_cleanup": "after_approval"})
    cfg["default_style"] = "Other"
    backend._sweep_working_files()
    assert cache.exists()
    cfg["styles"] = [cfg["styles"][1]]  # a removed style must not fall back
    backend._sweep_working_files()
    assert cache.exists()
    assert "policy" not in backend.film_working_files(str(wd))
    assert not any(r.path == "/api/films/working-files/policy" for r in backend.api.routes)


def test_style_policy_round_trip_is_explicit_and_not_inherited():
    cfg = {"styles": [{"name": "Parent", "working_files_cleanup": "after_upload"},
                       {"name": "Child", "parent": "Parent"},
                       {"name": "Off", "parent": "Parent", "working_files_cleanup": "off"},
                       {"name": "Invalid", "working_files_cleanup": "always"}],
           "default_style": "Parent"}
    app.save_config(cfg)
    cfg = app.load_config()
    assert app.working_files_cleanup_policy(cfg, "Parent") == "after_upload"
    for name in ("Child", "Off", "Invalid", "missing", "", app.NO_STYLE):
        assert app.working_files_cleanup_policy(cfg, name) == "off"


def test_captions_keep_final_timing_after_scene_clips_are_removed(film, monkeypatch):
    wd, final = film
    (wd / "script.json").write_text(json.dumps([
        {"id": 1, "narration": "First scene."}, {"id": 2, "narration": "Second scene."}]))
    put(wd / "scene_02_final.mp4")
    put(wd / "scene_02_narration.wav")
    put(wd / "localize/pt/scene_01_final.mp4")
    put(wd / "localize/pt/scene_02_final.mp4")
    put(wd / "localize/pt/scene_01_narration.wav")
    put(wd / "localize/pt/scene_02_narration.wav")
    put(wd / "localize_scripts/pt.json", json.dumps({"scenes": {
        "1": "Primeira cena.", "2": "Segunda cena."}}).encode())
    final_video_history.record(wd, final, lang="pt", kind="localize")
    monkeypatch.setattr(captions, "_duration", lambda p: (5.0 if p.suffix == ".mp4" else 2.0) if p.exists() else 0)
    before = backend._publish_caption_tracks(wd, "en")
    paths = [Path(before[0]), *[Path(t["path"]) for t in before[3]]]
    original = {path: path.read_bytes() for path in paths}
    assert b"00:00:05,000" in original[paths[0]]
    backend.film_working_files_clean(backend.WorkingFilesBody(work_dir=str(wd)))
    assert not (wd / "localize/pt/scene_01_final.mp4").exists()
    assert (wd / "localize/pt/scene_01_narration.wav").exists()
    assert backend._publish_caption_tracks(wd, "en") == before
    assert all(path.read_bytes() == content for path, content in original.items())
    assert len(backend.film_caption_tracks(str(wd))["tracks"]) == 2
    assert Path(backend.film_captions_srt(str(wd), "pt").path) == paths[0]


def test_embedded_scene_speech_is_preserved_before_deleting_video(film, monkeypatch):
    from pipeline import assembler
    wd, _ = film
    (wd / "scene_01_narration.wav").unlink()
    monkeypatch.setattr(assembler, "_has_audio_stream", lambda p: True)
    monkeypatch.setattr(assembler, "extract_audio", lambda source, target: put(target, b"original speech"))
    backend.film_working_files_clean(backend.WorkingFilesBody(work_dir=str(wd)))
    assert (wd / "scene_01_original_audio.wav").read_bytes() == b"original speech"
    assert not (wd / "scene_01_final.mp4").exists()


def test_failed_audio_preservation_aborts_before_deletion(film, monkeypatch):
    from pipeline import assembler
    wd, _ = film
    (wd / "scene_01_narration.wav").unlink()
    monkeypatch.setattr(assembler, "_has_audio_stream", lambda p: True)
    def fail(*args):
        raise RuntimeError("Cannot extract speech")
    monkeypatch.setattr(assembler, "extract_audio", fail)
    with pytest.raises(RuntimeError, match="Cannot extract speech"):
        backend.film_working_files_clean(backend.WorkingFilesBody(work_dir=str(wd)))
    assert (wd / "scene_01_final.mp4").exists()
    assert (wd / "combined.mp4").exists()
    assert not working_files.is_cleaned(wd)


def test_publish_approval_hook_cleans_only_when_approved(film, monkeypatch):
    wd, _ = film
    cache = add_cache(wd)
    entry = pq.add_item(str(wd), title="Film")
    monkeypatch.setattr(backend, "_ensure_publish_entry", lambda p: entry)
    app.load_config()["styles"][0]["working_files_cleanup"] = "after_approval"
    backend.publish_approve(backend.PublishApproveBody(work_dir=str(wd), approved=False))
    assert cache.exists()
    backend.publish_approve(backend.PublishApproveBody(work_dir=str(wd), approved=True))
    assert not cache.exists()


@pytest.mark.parametrize("succeeds", [False, True])
def test_youtube_upload_hook_only_cleans_after_success(film, monkeypatch, succeeds):
    wd, final = film
    cache = add_cache(wd)
    app.load_config()["styles"][0]["working_files_cleanup"] = "after_upload"
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
    app.load_config()["styles"][0]["working_files_cleanup"] = "after_upload"
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
