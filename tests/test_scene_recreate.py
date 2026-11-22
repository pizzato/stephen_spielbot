"""A fresh scene replaces its concept, opening image and take as one operation."""
import json
from unittest import mock

import pytest

import webapp.backend.main as backend
from pipeline.orchestrator import DurableStore, job_id_from_work_dir


@pytest.fixture
def film(tmp_path, monkeypatch):
    monkeypatch.setenv("SPIELBOT_ORCHESTRATOR_DB", str(tmp_path / "store.sqlite3"))
    monkeypatch.setattr(backend.gapp, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(backend.gapp, "load_config", lambda: {})
    monkeypatch.setattr(backend, "_cast_pool", lambda *a: ["Ana"])
    monkeypatch.setattr(backend, "_RERENDER_JOURNAL_PATH", tmp_path / "journal.json")
    monkeypatch.setattr(backend, "_film_tasks", {})
    monkeypatch.setattr(backend, "_film_task_meta", {})
    monkeypatch.setattr(backend, "_film_cancelled_tids", set())
    wd = tmp_path / "film"
    wd.mkdir()
    (wd / "combined.mp4").write_bytes(b"original film")
    (wd / "scene_01_final.mp4").write_bytes(b"original video")
    (wd / "scene_01_preview.png").write_bytes(b"original preview")
    (wd / "scene_01_first_frame.png").write_bytes(b"original frame")
    jid = job_id_from_work_dir(wd)
    meta = {"mode": "silent", "singing": True, "cast": ["Ana"],
            "setting": "old room", "seconds": 5, "duration": 5,
            "continues_previous": True, "sings": "We are here",
            "song_window": [10, 15], "line_times": [[0.5, 3]],
            "vocal_ranges": [[0.5, 3]]}
    store = DurableStore.default()
    try:
        store.create_or_update_job(jid, wd, "Song", config={"h3_silent_scenes": True}, metadata={})
        store.upsert_scene(jid, 1, title="Old scene", image_prompt="old image concept",
                           video_prompt="old motion", narration="", metadata=meta)
    finally:
        store.close()
    reply = {"title": "A New Dawn", "cast": ["Ana"], "setting": "a bright rooftop",
             "lines": [], "beats": [{"t0": 0, "t1": 5, "action": "Ana faces the sunrise"}],
             "camera": "wide shot", "soundscape": "wind", "image_prompt": "Ana on a bright rooftop"}
    llm = mock.Mock(return_value=json.dumps(reply))
    monkeypatch.setattr(backend, "_llm_complete", llm)
    threads = []
    monkeypatch.setattr(backend.threading, "Thread",
                        lambda **kwargs: threads.append(kwargs) or mock.Mock())
    return wd, jid, meta, reply, llm, threads


def saved_scene(jid):
    store = DurableStore.default()
    try:
        return store.get_scene(jid, 1)
    finally:
        store.close()


@pytest.mark.parametrize("no_frame", [False, True])
def test_recreate_changes_music_concept_preserving_timing_and_previous_takes(film, no_frame):
    wd, jid, meta, reply, llm, threads = film
    if no_frame:
        store = DurableStore.default()
        try:
            row = store.get_scene(jid, 1)
            store.upsert_scene(jid, 1, title=row["title"], image_prompt=row["image_prompt"],
                               video_prompt=row["video_prompt"], narration="",
                               metadata={**meta, "no_first_frame": True})
        finally:
            store.close()
    result = backend.regenerate_acted_scene(jid, 1, backend.ActedRegenBody(recreate=True))
    row = saved_scene(jid)
    assert row["image_prompt"] == reply["image_prompt"]
    assert "bright rooftop" in row["video_prompt"]
    assert "old motion" not in row["video_prompt"]
    assert row["metadata"]["setting"] == reply["setting"]
    assert not row["metadata"].get("continues_previous")
    for key in ("seconds", "duration", "singing", "sings", "song_window", "line_times", "vocal_ranges"):
        assert row["metadata"][key] == meta[key]
    assert bool(row["metadata"].get("no_first_frame")) == no_frame
    assert (wd / "scene_01_final.mp4").read_bytes() == b"original video"
    assert len(backend.video_history.history(wd, 1)["versions"]) == 1
    assert (wd / "scene_01_preview.png").exists() == no_frame
    if not no_frame:
        assert len(backend.image_history.history(wd, 1)["versions"]) == 2
    args = threads[0]["args"]
    assert args[0] is backend._run_scene_rerender
    assert args[4] == "scene"
    assert args[5]["h3_silent_scenes"] is True
    assert result["task_id"] == args[1]
    assert backend._load_rerender_journal()[0]["component"] == "scene"
    assert "We are here" in llm.call_args.args[1]


def test_missing_image_description_uses_the_new_setting(film):
    wd, jid, meta, reply, llm, threads = film
    reply.pop("image_prompt")
    llm.return_value = json.dumps(reply)
    backend.regenerate_acted_scene(jid, 1, backend.ActedRegenBody(recreate=True))
    prompt = saved_scene(jid)["image_prompt"]
    assert "bright rooftop" in prompt
    assert "old" not in prompt


def test_dialogue_recreation_paints_from_new_setting_and_keeps_unfinished_video(film, monkeypatch):
    wd, jid, meta, reply, llm, threads = film
    (wd / "combined.mp4").unlink()
    reply["lines"] = [{"speaker": "Ana", "text": "The sun is rising."}]
    llm.return_value = json.dumps(reply)
    store = DurableStore.default()
    try:
        store.upsert_scene(jid, 1, title="Old scene", image_prompt="old image concept",
                           video_prompt="old motion", narration="Old words",
                           metadata={"mode": "dialogue", "cast": ["Ana"],
                                     "setting": "old room", "lines": [{"speaker": "Ana", "text": "Old words"}]})
    finally:
        store.close()
    backend.regenerate_acted_scene(jid, 1, backend.ActedRegenBody(recreate=True))
    row = saved_scene(jid)
    monkeypatch.setattr(backend.gapp, "_characters_prompt_and_refs", lambda p, *a, **kw: (p, []))
    prompt, _ = backend._film_scene_image_prompt({}, row, {}, wd)
    assert "bright rooftop" in prompt
    assert "old" not in prompt
    assert '"image_prompt":' not in llm.call_args.args[1]
    assert (wd / "scene_01_final.mp4").read_bytes() == b"original video"


def test_rewrite_alone_keeps_image_and_does_not_render(film):
    wd, jid, meta, reply, llm, threads = film
    result = backend.regenerate_acted_scene(jid, 1, backend.ActedRegenBody())
    assert saved_scene(jid)["image_prompt"] == "old image concept"
    assert (wd / "scene_01_preview.png").read_bytes() == b"original preview"
    assert "task_id" not in result
    assert not threads


@pytest.mark.parametrize("blocked", ["busy", "cleaned"])
def test_recreate_rejects_unavailable_scene_before_rewriting(film, monkeypatch, blocked):
    wd, jid, meta, reply, llm, threads = film
    if blocked == "busy":
        backend._film_tasks["busy"] = {"status": "running"}
        backend._film_task_meta["busy"] = {"work_dir": str(wd), "scene_id": 1}
    else:
        monkeypatch.setattr(backend.working_files, "is_cleaned", lambda _: True)
    with pytest.raises(backend.HTTPException) as error:
        backend.regenerate_acted_scene(jid, 1, backend.ActedRegenBody(recreate=True))
    assert error.value.status_code == (409 if blocked == "busy" else 400)
    llm.assert_not_called()
    assert saved_scene(jid)["image_prompt"] == "old image concept"


@pytest.mark.parametrize("image_status", ["running", "error", "cancelled"])
def test_recreate_waits_for_image_and_stops_after_failure(film, monkeypatch, image_status):
    wd, jid, *_ = film
    states = []
    class ObservedTasks(dict):
        def __setitem__(self, key, value):
            states.append(value["status"])
            super().__setitem__(key, value)
    monkeypatch.setattr(backend, "_film_tasks", ObservedTasks())
    backend._film_tasks["task"] = {"status": "running"}
    def image(tid, *args, complete=True):
        assert complete is False
        backend._film_tasks[tid] = {"status": image_status}
    def video(tid, *args, staged=False):
        assert staged is True
        assert "done" not in states
        backend._film_tasks[tid] = {"status": "done"}
    monkeypatch.setattr(backend, "_run_image_rerender", image)
    render = mock.Mock(side_effect=video)
    monkeypatch.setattr(backend, "_run_acted_rerender", render)
    backend._run_scene_rerender("task", wd, 1, {}, saved_scene(jid))
    assert render.call_count == (1 if image_status == "running" else 0)


def test_no_first_frame_skips_image_generation(film, monkeypatch):
    wd, jid, *_ = film
    image = mock.Mock()
    video = mock.Mock()
    monkeypatch.setattr(backend, "_run_image_rerender", image)
    monkeypatch.setattr(backend, "_run_acted_rerender", video)
    backend._film_tasks["task"] = {"status": "running"}
    backend._run_scene_rerender("task", wd, 1, {}, {"metadata": {"no_first_frame": True}})
    image.assert_not_called()
    video.assert_called_once()


def test_real_image_phase_stays_running_and_saves_preview_path(film, monkeypatch):
    from pipeline import comfyui
    wd, jid, *_ = film
    portrait = wd / "portrait.png"
    portrait.write_bytes(b"portrait")
    backend._film_tasks["task"] = {"status": "running"}
    monkeypatch.setattr(backend, "_shared_edit_render_pool", lambda: mock.Mock())
    monkeypatch.setattr(backend.gapp, "scene_frame_engine", lambda *a: {})
    monkeypatch.setattr(backend.gapp, "_characters_prompt_and_refs", lambda p, *a, **kw: (p, [portrait]))
    monkeypatch.setattr(backend.gapp, "record_scene_frame_cast", mock.Mock())
    def paint(engine, prompt, output, **kwargs):
        assert kwargs["reference_images"] == [portrait]
        output.write_bytes(b"fresh image")
    monkeypatch.setattr(comfyui, "generate_with_engine", paint)
    def video(tid, *args, **kwargs):
        assert backend._film_tasks[tid]["status"] == "running"
        row = saved_scene(jid)
        assert row["preview_path"] == str(wd / "scene_01_preview.png")
        assert backend._scene_to_json(row, wd)["has_preview"]
        backend._film_tasks[tid] = {"status": "done"}
    monkeypatch.setattr(backend, "_run_acted_rerender", video)
    assert not saved_scene(jid)["preview_path"]
    backend._run_scene_rerender("task", wd, 1, {}, saved_scene(jid))
    assert backend._film_tasks["task"]["status"] == "done"
    assert (wd / "scene_01_preview.png").read_bytes() == b"fresh image"


@pytest.mark.parametrize("component", ["image", "video", "scene"])
def test_other_rerenders_wait_for_scene_recreation(film, component):
    wd, jid, *_ = film
    backend._film_tasks["recreate"] = {"status": "running"}
    backend._film_task_meta["recreate"] = {"work_dir": str(wd), "scene_id": 1, "component": "scene"}
    with pytest.raises(ValueError, match="rendering right now"):
        backend._start_scene_rerender(wd, 1, component)


@pytest.mark.parametrize("fails", [False, True])
def test_recreated_video_replaces_original_only_after_success(film, monkeypatch, fails):
    import resume_generation
    wd, jid, *_ = film
    backend.scene_context.save(wd, 1, latent="old latent")
    backend.scene_context.stamp_final(wd, 1, wd / "scene_01_final.mp4")
    monkeypatch.setattr(backend, "_shared_edit_render_pool", lambda: mock.Mock())
    def render(*args, output_path, **kwargs):
        assert (wd / "scene_01_final.mp4").read_bytes() == b"original video"
        backend.scene_context.save(wd, 1, latent="new latent")
        assert not backend.scene_context.continuable(wd, 1, wd / "scene_01_final.mp4")
        output_path.write_bytes(b"new video")
        if fails:
            raise RuntimeError("render failed")
        backend.scene_context.stamp_final(wd, 1, output_path)
    monkeypatch.setattr(resume_generation, "render_performance_scene", render)
    backend._film_tasks["task"] = {"status": "running"}
    backend._run_acted_rerender("task", wd, 1, {}, saved_scene(jid), staged=True)
    assert (wd / "scene_01_final.mp4").read_bytes() == (b"original video" if fails else b"new video")
    assert backend._film_tasks["task"]["status"] == ("error" if fails else "done")
    assert not (wd / "scene_01_final.recreate.mp4").exists()
    assert backend.scene_context.continuable(wd, 1, wd / "scene_01_final.mp4") is not fails
