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


def test_a_revoicing_records_its_voice_and_source(tmp_path):
    _write(_music(tmp_path), b"as-generated")
    mh.record(tmp_path, _music(tmp_path), "folk ballad")
    original = mh.history(tmp_path)["versions"][0]["id"]
    _write(_music(tmp_path), b"as-nora")
    h = mh.record(tmp_path, _music(tmp_path), "sung as Nora", voice="Nora",
                  source_id=original)

    assert h["versions"][0]["voice"] == ""          # the engine's own singer
    assert h["versions"][-1]["voice"] == "Nora"
    assert h["versions"][-1]["source_id"] == original


def test_find_returns_the_version_or_none(tmp_path):
    _write(_music(tmp_path), b"one")
    h = mh.record(tmp_path, _music(tmp_path), "first", voice="Nora")
    vid = h["versions"][0]["id"]

    assert mh.find(tmp_path, vid)["voice"] == "Nora"
    assert mh.find(tmp_path, 999) is None


def test_delete_removes_the_version_and_its_file(tmp_path):
    _write(_music(tmp_path), b"one")
    mh.record(tmp_path, _music(tmp_path), "first")
    _write(_music(tmp_path), b"two")
    h = mh.record(tmp_path, _music(tmp_path), "second")
    doomed = h["versions"][0]
    kept = h["versions"][1]["id"]

    left = mh.delete(tmp_path, doomed["id"])

    assert [v["id"] for v in left["versions"]] == [kept]
    assert not Path(doomed["path"]).exists()
    assert _music(tmp_path).read_bytes() == b"two"   # the one in use is untouched


def test_deleting_the_version_in_use_promotes_the_newest_left(tmp_path):
    """The film's track always has to be a version somebody can point at."""
    for tag, desc in ((b"one", "first"), (b"two", "second"), (b"three", "third")):
        _write(_music(tmp_path), tag)
        h = mh.record(tmp_path, _music(tmp_path), desc)
    newest = h["versions"][-1]["id"]
    assert mh.history(tmp_path)["selected"] == newest

    left = mh.delete(tmp_path, newest)

    assert left["selected"] == left["versions"][-1]["id"]
    assert _music(tmp_path).read_bytes() == b"two"


def test_delete_refuses_the_only_version(tmp_path):
    _write(_music(tmp_path), b"one")
    mh.record(tmp_path, _music(tmp_path), "first")
    try:
        mh.delete(tmp_path, mh.history(tmp_path)["versions"][0]["id"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError deleting the last version")
    assert len(mh.history(tmp_path)["versions"]) == 1


def test_delete_unknown_version_raises(tmp_path):
    _write(_music(tmp_path), b"one")
    mh.record(tmp_path, _music(tmp_path))
    _write(_music(tmp_path), b"two")
    mh.record(tmp_path, _music(tmp_path))
    try:
        mh.delete(tmp_path, 999)
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
