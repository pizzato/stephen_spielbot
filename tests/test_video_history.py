"""Unit tests for pipeline.video_history — the per-scene rendered-video take store.

The module only copies files around, so the "videos" here are tiny byte strings;
distinct bytes per take let us assert that ``select`` copies the *right* one. The
key difference from image_history is the last-N pruning of large take files.
"""
from pathlib import Path

from pipeline import video_history as vh


def _write(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _final(wd: Path, sid: int = 1) -> Path:
    return wd / f"scene_{sid:02d}_final.mp4"


def test_capture_current_records_existing_as_v1(tmp_path):
    _write(_final(tmp_path), b"original")
    vh.capture_current(tmp_path, 1, _final(tmp_path))

    h = vh.history(tmp_path, 1)
    assert len(h["versions"]) == 1
    assert h["selected"] == h["versions"][0]["id"]
    assert Path(h["versions"][0]["path"]).read_bytes() == b"original"


def test_capture_current_is_noop_when_already_kept(tmp_path):
    _write(_final(tmp_path), b"original")
    vh.capture_current(tmp_path, 1, _final(tmp_path))
    vh.capture_current(tmp_path, 1, _final(tmp_path))
    assert len(vh.history(tmp_path, 1)["versions"]) == 1


def test_capture_current_keeps_unrecorded_canonical_when_history_exists(tmp_path):
    # A final written outside the editors (the full render writes finals
    # directly) is on disk but not in history — capturing before a re-render
    # must keep it even though history is not empty.
    _write(_final(tmp_path), b"editor take")
    vh.record(tmp_path, 1, _final(tmp_path))
    _write(_final(tmp_path), b"render take")
    vh.capture_current(tmp_path, 1, _final(tmp_path))

    h = vh.history(tmp_path, 1)
    assert len(h["versions"]) == 2
    assert Path(h["versions"][-1]["path"]).read_bytes() == b"render take"
    assert h["selected"] == h["versions"][-1]["id"]


def test_capture_current_is_noop_when_current_missing(tmp_path):
    vh.capture_current(tmp_path, 1, _final(tmp_path))  # no file on disk
    assert vh.history(tmp_path, 1)["versions"] == []


def test_record_appends_and_selects_newest(tmp_path):
    _write(_final(tmp_path), b"one")
    vh.record(tmp_path, 1, _final(tmp_path))
    _write(_final(tmp_path), b"two")
    h = vh.record(tmp_path, 1, _final(tmp_path))

    assert len(h["versions"]) == 2
    assert h["selected"] == h["versions"][-1]["id"]
    by_id = {v["id"]: Path(v["path"]).read_bytes() for v in h["versions"]}
    assert sorted(by_id.values()) == [b"one", b"two"]


def test_record_prunes_to_the_cap_and_deletes_files(tmp_path):
    n = vh._MAX_VERSIONS + 2
    for i in range(1, n + 1):
        _write(_final(tmp_path), f"v{i}".encode())
        vh.record(tmp_path, 1, _final(tmp_path))

    h = vh.history(tmp_path, 1)
    assert len(h["versions"]) == vh._MAX_VERSIONS        # only the cap kept
    contents = {Path(v["path"]).read_bytes() for v in h["versions"]}
    assert contents == {f"v{i}".encode() for i in range(3, n + 1)}
    # The pruned take files are gone from disk, not just the manifest.
    kept = {p.name for p in (tmp_path / "video_history").glob("*.mp4")}
    assert len(kept) == vh._MAX_VERSIONS


def test_select_copies_chosen_take_onto_canonical_final(tmp_path):
    _write(_final(tmp_path), b"one")
    vh.record(tmp_path, 1, _final(tmp_path))
    _write(_final(tmp_path), b"two")
    h = vh.record(tmp_path, 1, _final(tmp_path))
    older = h["versions"][0]["id"]

    vh.select(tmp_path, 1, older)

    assert _final(tmp_path).read_bytes() == b"one"
    assert vh.history(tmp_path, 1)["selected"] == older


def test_select_unknown_take_raises(tmp_path):
    _write(_final(tmp_path), b"one")
    vh.record(tmp_path, 1, _final(tmp_path))
    try:
        vh.select(tmp_path, 1, 999)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown take id")


def test_history_filters_missing_files(tmp_path):
    _write(_final(tmp_path), b"one")
    vh.record(tmp_path, 1, _final(tmp_path))
    _write(_final(tmp_path), b"two")
    h = vh.record(tmp_path, 1, _final(tmp_path))

    Path(h["versions"][-1]["path"]).unlink()
    h2 = vh.history(tmp_path, 1)
    assert len(h2["versions"]) == 1
    assert h2["selected"] == h2["versions"][0]["id"]


def test_scenes_are_tracked_independently(tmp_path):
    _write(_final(tmp_path, 1), b"s1")
    vh.record(tmp_path, 1, _final(tmp_path, 1))
    _write(_final(tmp_path, 2), b"s2")
    vh.record(tmp_path, 2, _final(tmp_path, 2))

    assert len(vh.history(tmp_path, 1)["versions"]) == 1
    assert len(vh.history(tmp_path, 2)["versions"]) == 1
    assert Path(vh.history(tmp_path, 2)["versions"][0]["path"]).read_bytes() == b"s2"


def test_delete_removes_unused_take_and_file(tmp_path):
    _write(_final(tmp_path), b"one")
    vh.record(tmp_path, 1, _final(tmp_path))
    _write(_final(tmp_path), b"two")
    h = vh.record(tmp_path, 1, _final(tmp_path))
    older = h["versions"][0]

    h2 = vh.delete(tmp_path, 1, older["id"])

    assert [v["id"] for v in h2["versions"]] == [h["selected"]]
    assert not Path(older["path"]).exists()
    assert _final(tmp_path).read_bytes() == b"two"   # canonical untouched


def test_delete_refuses_the_take_in_use(tmp_path):
    _write(_final(tmp_path), b"one")
    vh.record(tmp_path, 1, _final(tmp_path))
    _write(_final(tmp_path), b"two")
    h = vh.record(tmp_path, 1, _final(tmp_path))
    try:
        vh.delete(tmp_path, 1, h["selected"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for the take in use")
    assert len(vh.history(tmp_path, 1)["versions"]) == 2


def test_delete_unknown_take_raises(tmp_path):
    _write(_final(tmp_path), b"one")
    vh.record(tmp_path, 1, _final(tmp_path))
    try:
        vh.delete(tmp_path, 1, 999)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown take id")
