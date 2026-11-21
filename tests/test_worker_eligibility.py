"""A partly upgraded fleet must queue pi-H3 only on compatible workers."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from pipeline.worker_pool import WorkerPool


def _waiting(pool, count):
    deadline = time.monotonic() + 2
    with pool._cond:
        while len(pool._waiters) < count and time.monotonic() < deadline:
            pool._cond.wait(timeout=0.01)
        assert len(pool._waiters) == count


def _acquire_thread(pool, **kwargs):
    result = []

    def run():
        try:
            result.append(pool.acquire(**kwargs))
        except RuntimeError as exc:
            result.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def test_allowed_subset_skips_incompatible_idle_worker():
    pool = WorkerPool(["old", "pi"])
    try:
        assert pool.acquire(allowed=["pi", "not-in-pool"]) == "pi"
        assert pool.acquire() == "old"
    finally:
        pool.shutdown()


@pytest.mark.parametrize("allowed", [[], ["not-in-pool"]])
def test_empty_eligible_subset_fails_without_blocking_other_requests(allowed):
    pool = WorkerPool(["old"])
    try:
        with pytest.raises(RuntimeError, match="No compatible workers"):
            pool.acquire(allowed=allowed)
        assert pool.acquire() == "old"
    finally:
        pool.shutdown()


def test_only_constraint_cannot_bypass_allowed_subset():
    pool = WorkerPool(["old", "pi"])
    try:
        with pytest.raises(RuntimeError, match="No compatible workers"):
            pool.acquire(only="old", allowed=["pi"])
        assert pool.acquire(only="pi", allowed=["pi"]) == "pi"
    finally:
        pool.shutdown()


def test_busy_eligible_worker_is_waited_for_even_when_other_worker_is_idle():
    pool = WorkerPool(["old", "pi"])
    try:
        pool.acquire(only="pi")
        thread, result = _acquire_thread(pool, allowed=["pi"])
        _waiting(pool, 1)
        assert result == []
        pool.release("pi")
        thread.join(2)
        assert result == ["pi"]
    finally:
        pool.shutdown()


def test_last_eligible_worker_is_not_held_as_ui_reservation():
    pool = WorkerPool(["pi", "old"], reserve_check=lambda: True)
    pool.shutdown()  # drive reservation synchronously
    try:
        assert pool.acquire(allowed=["pi"]) == "pi"
    finally:
        pool.shutdown()


def test_failed_eligible_worker_wakes_waiter_without_using_incompatible_worker():
    pool = WorkerPool(["old", "pi"])
    try:
        pool.acquire(only="pi")
        thread, result = _acquire_thread(pool, allowed=["pi"])
        _waiting(pool, 1)
        pool.mark_failed("pi")
        thread.join(2)
        assert len(result) == 1
        assert isinstance(result[0], RuntimeError)
        assert "No compatible workers" in str(result[0])
        assert pool.acquire() == "old"
    finally:
        pool.shutdown()


def test_subset_waiter_preserves_fifo_with_unrestricted_waiter():
    pool = WorkerPool(["pi", "old"])
    try:
        pool.acquire(only="pi")
        first, first_result = _acquire_thread(pool, allowed=["pi"])
        _waiting(pool, 1)
        second, second_result = _acquire_thread(pool)
        _waiting(pool, 2)
        pool.release("pi")
        first.join(2)
        second.join(2)
        assert first_result == ["pi"]
        assert second_result == ["old"]
    finally:
        pool.shutdown()


def test_allowed_subset_still_respects_cross_pool_lease():
    first_pool = WorkerPool(["pi"])
    second_pool = WorkerPool(["old", "pi"])
    try:
        first_pool.acquire()
        thread, result = _acquire_thread(second_pool, allowed=["pi"])
        _waiting(second_pool, 1)
        assert result == []
        first_pool.release("pi")
        thread.join(2)
        assert result == ["pi"]
    finally:
        first_pool.shutdown()
        second_pool.shutdown()


def _agent_task(tmp_path):
    return SimpleNamespace(id="scene-video", job_id="film", kind="scene.video.generate", payload={
        "work_dir": str(tmp_path), "scene_id": 1, "narration_duration": 5,
        "video_engine": "minimax-h3-pi-dwd2", "video_prompt": "A firefly",
    })


def test_incompatible_agent_defers_before_generating_first_frame_or_video(tmp_path):
    import worker_agent
    from pipeline import comfyui

    task = _agent_task(tmp_path)
    store = mock.Mock()
    with mock.patch.object(comfyui, "compatible_video_workers",
                           side_effect=RuntimeError("pi-H3 needs a worker update")) as eligible, \
         mock.patch.object(worker_agent, "generate_scene_video_task") as render:
        with pytest.raises(worker_agent.RetryLater, match="needs a worker update"):
            worker_agent._execute_scene_video(store, task, "http://old")
    eligible.assert_called_once()
    assert eligible.call_args.args[0] == ["http://old"]
    render.assert_not_called()
    store.heartbeat_task.assert_not_called()


def test_compatible_agent_passes_selected_engine_to_scene_renderer(tmp_path):
    import worker_agent
    from pipeline import comfyui

    task = _agent_task(tmp_path)
    with mock.patch.object(comfyui, "compatible_video_workers", return_value=["http://pi"]), \
         mock.patch.object(worker_agent, "generate_scene_video_task",
                           return_value=(tmp_path / "clip.mp4", None)) as render, \
         mock.patch.object(worker_agent, "_get_duration", return_value=5):
        worker_agent._execute_scene_video(mock.Mock(), task, "http://pi")
    assert render.call_args.kwargs["video_engine"]["key"] == "minimax-h3-pi-dwd2"


def test_existing_completed_clip_does_not_require_pi_worker(tmp_path):
    import worker_agent
    from pipeline import comfyui

    task = _agent_task(tmp_path)
    (tmp_path / "scene_01_clip_01.mp4").write_bytes(b"v" * 10_001)
    store = mock.Mock()
    with mock.patch.object(comfyui, "compatible_video_workers") as eligible, \
         mock.patch.object(worker_agent, "_get_duration", return_value=5):
        worker_agent._execute_scene_video(store, task, "http://old")
    eligible.assert_not_called()
    assert store.complete_task.call_args.kwargs["result"]["skipped"]


def test_durable_agent_skips_incompatible_head_without_spending_attempts(tmp_path):
    import worker_agent
    from pipeline import comfyui
    from pipeline.orchestrator import DurableStore

    db = tmp_path / "orchestrator.sqlite3"
    store = DurableStore(db)
    store.create_or_update_job("film", tmp_path, "Film", {}, {})
    task = _agent_task(tmp_path)
    store.create_task(task.id, "film", task.kind, "Pi video", worker_kind="comfy",
                      payload=task.payload, priority=1)
    store.create_task("image", "film", "scene.image.generate", "Image", worker_kind="comfy",
                      payload=task.payload, priority=2)
    args = SimpleNamespace(db=db, debug=False, worker_id="old", kind="comfy",
                           endpoint="http://old", lease_seconds=900, once=True, poll_interval=1)

    def finish(claim_store, claimed_task, endpoint, seconds):
        claim_store.complete_task(claimed_task.id)

    try:
        with mock.patch.object(comfyui, "compatible_video_workers",
                               side_effect=RuntimeError("requires piFlow")), \
             mock.patch.object(worker_agent, "execute_task", side_effect=finish) as execute:
            # More than the task's max_attempts: the unsupported head must stay
            # queued, while this same endpoint can finish unrelated work.
            for _ in range(5):
                assert worker_agent.run_agent(args) == 0
        assert execute.call_count == 1
        assert execute.call_args.args[1].id == "image"
        pending = store.get_task(task.id)
        assert pending.status == "queued"
        assert pending.attempt == 0
        assert pending.lease_owner is None

        args.endpoint, args.worker_id = "http://pi", "pi"
        with mock.patch.object(comfyui, "compatible_video_workers", return_value=[args.endpoint]), \
             mock.patch.object(worker_agent, "execute_task", side_effect=finish):
            assert worker_agent.run_agent(args) == 0
        completed = store.get_task(task.id)
        assert completed.status == "succeeded"
        assert completed.attempt == 1
    finally:
        store.close()


def test_agent_readiness_cache_refreshes_after_install_without_polling_each_tick(tmp_path):
    import worker_agent
    from pipeline import comfyui

    cache = {}
    task = _agent_task(tmp_path)
    with mock.patch.object(comfyui, "compatible_video_workers",
                           side_effect=[RuntimeError("not installed"), ["http://pi"]]) as ready, \
         mock.patch.object(worker_agent.time, "monotonic", return_value=100) as clock:
        assert not worker_agent._task_eligible_for_worker(task, "http://pi", cache)
        clock.return_value = 129
        for _ in range(5):
            assert not worker_agent._task_eligible_for_worker(task, "http://pi", cache)
        assert ready.call_count == 1
        clock.return_value = 130
        assert worker_agent._task_eligible_for_worker(task, "http://pi", cache)
        assert ready.call_count == 2


def test_agent_claim_filter_does_not_probe_existing_engines_or_saved_videos(tmp_path):
    import worker_agent
    from pipeline import comfyui

    task = _agent_task(tmp_path)
    with mock.patch.object(comfyui, "compatible_video_workers") as ready:
        task.payload["video_engine"] = "ltx25"
        assert worker_agent._task_eligible_for_worker(task, "http://old", {})
        task.payload["video_engine"] = "minimax-h3-pi-dwd2"
        (tmp_path / "scene_01_clip_01.mp4").write_bytes(b"v" * 10_001)
        assert worker_agent._task_eligible_for_worker(task, "http://old", {})
    ready.assert_not_called()


def test_claim_filter_cannot_return_another_agents_lease(tmp_path):
    from pipeline.orchestrator import DurableStore

    db = tmp_path / "orchestrator.sqlite3"
    first, second = DurableStore(db), DurableStore(db)
    first.create_or_update_job("film", tmp_path, "Film", {}, {})
    first.create_task("video", "film", "scene.video.generate", "Video", worker_kind="comfy")

    def eligible(task):
        assert second.acquire_next_task("second", "comfy").id == task.id
        return True

    try:
        assert first.acquire_next_task("first", "comfy", task_eligible=eligible) is None
        claimed = first.get_task("video")
        assert claimed.lease_owner == "second"
        assert claimed.attempt == 1
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("key,expected_worker", [
    ("minimax-h3-pi-dwd2", "http://pi"),
    ("ltx25", "http://old"),
])
def test_resumed_film_routes_selected_engine_without_chaining_or_unrelated_probes(
        tmp_path, key, expected_worker):
    import resume_generation as rg
    from PIL import Image
    from pipeline.orchestrator import DurableStore

    wd = tmp_path / "film-20261122-120000"
    wd.mkdir()
    output = tmp_path / "out"
    output.mkdir()
    (wd / "script.json").write_text(json.dumps([{
        "id": 1, "title": "Firefly", "image_prompt": "A firefly",
        "video_prompt": "A firefly glows", "narration": "A firefly glows.",
    }]))
    (wd / "scene_01_narration.wav").write_bytes(b"a" * 2000)
    Image.new("RGB", (1280, 704)).save(wd / "scene_01_first_frame.png")
    (wd / "job_config.json").write_text(json.dumps({
        "title": "Firefly", "video_engine": key, "h3_chain_scenes": True,
        "video_steps": 8, "music_enabled": False, "tts_workers": ["tts"],
        "resolution": "Landscape 720p (1280×720)",
    }))
    store = DurableStore(tmp_path / "orchestrator.sqlite3")
    rendered = []

    def render(scene, work_dir, *args, **kwargs):
        rendered.append(kwargs)
        clip = work_dir / f"scene_{scene.id:02d}_clip_01.mp4"
        clip.write_bytes(b"v" * 20_000)
        return clip, None

    def write_media(*args, **kwargs):
        destination = next(p for p in reversed(args) if isinstance(p, Path))
        destination.write_bytes(b"v" * 20_000)
        return destination

    try:
        with mock.patch.object(rg, "load_config", return_value={}), \
             mock.patch.object(rg.DurableStore, "default", return_value=store), \
             mock.patch.object(rg, "OUTPUT_DIR", output), \
             mock.patch.object(rg, "alive_workers", return_value=["http://old", "http://pi"]), \
             mock.patch.object(rg.ui_activity, "is_active", return_value=False), \
             mock.patch.object(rg, "compatible_video_workers", return_value=["http://pi"]) as eligible, \
             mock.patch.object(rg, "generate_cover_image"), \
             mock.patch.object(rg, "_generate_scene_video", side_effect=render), \
             mock.patch.object(rg, "_get_duration", return_value=5), \
             mock.patch.object(rg, "mux_video_audio", side_effect=write_media), \
             mock.patch.object(rg, "concatenate_scenes", side_effect=write_media), \
             mock.patch.object(rg, "copy_video_with_attribution", side_effect=write_media), \
             mock.patch.object(rg, "ensure_video_resolution"):
            rg.main(wd)
        assert len(rendered) == 1
        assert rendered[0]["comfy_url"] == expected_worker
        assert rendered[0]["video_engine"]["key"] == key
        assert rendered[0]["chained"] is False
        if key == "minimax-h3-pi-dwd2":
            eligible.assert_called_once()
            assert rendered[0]["video_engine"]["steps"] == 2
        else:
            eligible.assert_not_called()
        assert len(list(output.glob("*.mp4"))) == 1
    finally:
        store.close()
