"""Browsing must scale without rereading shared stores for every film."""
import json
import os
from unittest import mock

import pytest
import yaml

import app
import webapp.backend.main as backend
from pipeline.orchestrator import DurableStore, job_id_from_work_dir


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(app, "CONFIG_FILE", path)
    app._read_config_yaml.cache_clear()
    yield path
    app._read_config_yaml.cache_clear()


def test_settings_cache_does_not_share_mutable_data(settings_file):
    settings_file.write_text("custom:\n  names: [original]\n")
    with mock.patch.object(app.yaml, "safe_load", wraps=yaml.safe_load) as parse:
        first = app.load_config()
        first["custom"]["names"].append("unsaved edit")
        assert app.load_config()["custom"]["names"] == ["original"]
        assert parse.call_count == 1


def test_settings_cache_observes_save_replace_delete_and_recreate(settings_file):
    settings_file.write_text("custom: first\n")
    assert app.load_config()["custom"] == "first"
    cfg = app.load_config()
    cfg["custom"] = "saved"
    app.save_config(cfg)
    assert app.load_config()["custom"] == "saved"

    # An editor/restore can replace a file while preserving its size and mtime.
    st = settings_file.stat()
    replacement = settings_file.with_suffix(".tmp")
    replacement.write_text(settings_file.read_text().replace("custom: saved", "custom: other"))
    os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
    replacement.replace(settings_file)
    assert app.load_config()["custom"] == "other"
    settings_file.unlink()
    assert "custom" not in app.load_config()
    settings_file.write_text("custom: recreated\n")
    assert app.load_config()["custom"] == "recreated"


def test_failed_settings_parse_is_not_cached(settings_file):
    settings_file.write_text("custom: [unfinished")
    with pytest.raises(yaml.YAMLError):
        app.load_config()
    settings_file.write_text("custom: repaired\n")
    assert app.load_config()["custom"] == "repaired"


@pytest.fixture
def library(tmp_path, monkeypatch):
    output = tmp_path / "videos"
    output.mkdir()
    monkeypatch.setattr(app, "OUTPUT_DIR", output)
    monkeypatch.setattr(app, "_preferred_work_dir", lambda _: None)
    monkeypatch.setenv("SPIELBOT_ORCHESTRATOR_DB", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(backend.pq, "PUBLISH_QUEUE_PATH", tmp_path / "publish_queue.json")
    cfg = {
        "styles": [{"name": "Studio", "channel": "UC1", "x_account": "X1"}],
        "youtube_channels": [{"id": "UC1", "name": "Studio Channel"}],
        "x_accounts": [{"id": "X1", "name": "Studio X"}],
        "publish_require_approval": True,
    }
    monkeypatch.setattr(app, "load_config", lambda: cfg)

    def make_film(i, *, finished=True, archived=False):
        wd = output / f"film-{i:04d}"
        wd.mkdir()
        (wd / "script.json").write_text("[]")
        (wd / "job_config.json").write_text(json.dumps({
            "style_name": "Studio", "video_title": f"Film {i}", "archived": archived,
        }))
        if finished:
            (wd / "combined.mp4").touch()
            (wd / "job.json").write_text("{}")
        os.utime(wd, (1000 + i, 1000 + i))
        return wd

    return make_film


def test_library_shared_reads_are_constant_and_publish_state_is_fresh(library):
    films = [library(i) for i in range(60)]
    (films[0] / "job.json").write_text(json.dumps({
        "youtube_video_id": "video", "youtube_url": "https://example.test/video",
        "x_tweet_id": "tweet", "x_url": "https://example.test/tweet",
    }))
    backend.pq.save_queue([{"work_dir": str(films[1]), "approved": True}])
    with mock.patch.object(app, "load_config", wraps=app.load_config) as config, \
            mock.patch.object(backend.pq, "load_queue", wraps=backend.pq.load_queue) as queue, \
            mock.patch.object(backend, "_film_job_config", wraps=backend._film_job_config) as job_config:
        result = backend.list_jobs()
        assert config.call_count == 1
        assert queue.call_count == 1
        assert job_config.call_count == len(films)
    by_dir = {f["work_dir"]: f for f in result["finished"]}
    assert [d["name"] for d in by_dir[str(films[0])]["destinations"]] == ["Studio Channel", "Studio X"]
    assert by_dir[str(films[1])]["approved"]
    assert by_dir[str(films[2])]["awaiting_approval"]
    assert len(result["scripts"]) == len(films)
    # No response cache: a queue update is reflected on the very next request.
    backend.pq.save_queue([{"work_dir": str(films[2]), "approved": True}])
    fresh = {f["work_dir"]: f for f in backend.list_jobs("films")["finished"]}
    assert fresh[str(films[2])]["approved"]
    assert fresh[str(films[1])]["awaiting_approval"]


def test_recent_skips_archived_and_only_builds_five_films(library):
    for i in range(12):
        library(i, archived=i >= 10)
    with mock.patch.object(app, "_list_script_jobs", side_effect=AssertionError("scripts scanned")), \
            mock.patch.object(app, "_list_resumable_jobs", side_effect=AssertionError("renders scanned")), \
            mock.patch.object(backend, "_film_publish_status", wraps=backend._film_publish_status) as status:
        result = backend.list_jobs("recent")
    assert [f["title"] for f in result["finished"]] == [f"Film {i}" for i in range(9, 4, -1)]
    assert status.call_count == 5
    assert result["scripts"] == result["resumable"] == []


def test_scripts_view_skips_film_and_publish_work(library):
    library(1)
    library(2, finished=False)
    with mock.patch.object(app, "_list_recent_jobs", side_effect=AssertionError("films scanned")), \
            mock.patch.object(backend.pq, "load_queue", side_effect=AssertionError("queue read")), \
            mock.patch.object(app, "_list_resumable_jobs", side_effect=AssertionError("renders scanned")):
        result = backend.list_jobs("scripts")
    assert len(result["scripts"]) == 2
    assert sum(s["rendered"] for s in result["scripts"]) == 1
    assert result["finished"] == result["resumable"] == []


def test_films_view_retains_legacy_database_metadata(library):
    wd = library(1)
    (wd / "job_config.json").write_text("{}")
    store = DurableStore.default()
    try:
        store.create_or_update_job(job_id_from_work_dir(wd), wd, "Legacy title",
                                   config={"style_name": "Studio"})
    finally:
        store.close()
    with mock.patch.object(app, "_list_script_jobs", side_effect=AssertionError("scripts scanned")):
        result = backend.list_jobs("films")
    assert result["scripts"] == []
    assert result["finished"][0]["title"] == "Legacy title"
    assert result["finished"][0]["channel"] == "Studio Channel"


def test_browsing_indexes_upgrade_existing_databases(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = DurableStore(path)
    store._conn.execute("DROP INDEX idx_jobs_updated")
    store._conn.execute("DROP INDEX idx_tasks_job_order")
    store.close()
    store = DurableStore(path)
    try:
        for query, params in (
            ("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 10", ()),
            ("SELECT * FROM tasks WHERE job_id=? ORDER BY priority, created_at", ("job",)),
        ):
            plan = store._conn.execute("EXPLAIN QUERY PLAN " + query, params).fetchall()
            assert not any("TEMP B-TREE" in row[3] for row in plan)
    finally:
        store.close()
