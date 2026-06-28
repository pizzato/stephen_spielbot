"""Unit tests for pipeline.music_history — the film-level background-music store.

The module only copies files around, so the "tracks" here are tiny byte strings;
distinct bytes per version let us assert that ``select`` copies the *right* one.
Unlike the per-scene image/video stores, music is film-level (no scene id) and
keeps *all* versions (no pruning), tagging each with the prompt that made it.
"""
from pathlib import Path

from pipeline import music_history as mh


def _write(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _music(wd: Path) -> Path:
    return wd / "background_music.wav"


def test_seed_if_empty_records_existing_as_v1(tmp_path):
    _write(_music(tmp_path), b"original")
    mh.seed_if_empty(tmp_path, _music(tmp_path), "cinematic original")

    h = mh.history(tmp_path)
    assert len(h["versions"]) == 1
    assert h["selected"] == h["versions"][0]["id"]
    assert h["versions"][0]["desc"] == "cinematic original"
    assert Path(h["versions"][0]["path"]).read_bytes() == b"original"


def test_seed_if_empty_is_noop_when_history_exists(tmp_path):
    _write(_music(tmp_path), b"original")
    mh.seed_if_empty(tmp_path, _music(tmp_path))
    _write(_music(tmp_path), b"changed")
    mh.seed_if_empty(tmp_path, _music(tmp_path))
    assert len(mh.history(tmp_path)["versions"]) == 1


def test_seed_if_empty_is_noop_when_current_missing(tmp_path):
    mh.seed_if_empty(tmp_path, _music(tmp_path))  # no file on disk
    assert mh.history(tmp_path)["versions"] == []


def test_record_keeps_all_versions_and_selects_newest(tmp_path):
    for tag, desc in ((b"v1", "a"), (b"v2", "b"), (b"v3", "c"), (b"v4", "d")):
        _write(_music(tmp_path), tag)
        h = mh.record(tmp_path, _music(tmp_path), desc)

    # All four are kept — no pruning, unlike video takes.
    assert len(h["versions"]) == 4
    assert h["selected"] == h["versions"][-1]["id"]
    contents = sorted(Path(v["path"]).read_bytes() for v in h["versions"])
    assert contents == [b"v1", b"v2", b"v3", b"v4"]
    assert h["versions"][-1]["desc"] == "d"


def test_select_copies_chosen_version_onto_canonical(tmp_path):
    _write(_music(tmp_path), b"one")
    mh.record(tmp_path, _music(tmp_path), "first")
    _write(_music(tmp_path), b"two")
    h = mh.record(tmp_path, _music(tmp_path), "second")
    older = h["versions"][0]["id"]

    mh.select(tmp_path, older)

    assert _music(tmp_path).read_bytes() == b"one"
    assert mh.history(tmp_path)["selected"] == older


def test_select_unknown_version_raises(tmp_path):
    _write(_music(tmp_path), b"one")
    mh.record(tmp_path, _music(tmp_path))
    try:
        mh.select(tmp_path, 999)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown version id")


def test_history_filters_missing_files(tmp_path):
    _write(_music(tmp_path), b"one")
    mh.record(tmp_path, _music(tmp_path))
    _write(_music(tmp_path), b"two")
    h = mh.record(tmp_path, _music(tmp_path))

    Path(h["versions"][-1]["path"]).unlink()
    h2 = mh.history(tmp_path)
    assert len(h2["versions"]) == 1
    assert h2["selected"] == h2["versions"][0]["id"]
