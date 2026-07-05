"""Unit tests for pipeline.final_video_history — the finished-video store."""

from pathlib import Path

from pipeline import final_video_history as fvh


def _write(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _final(wd: Path) -> Path:
    return wd.with_suffix(".mp4")


def test_seed_if_empty_records_existing_final_as_v1(tmp_path):
    _write(_final(tmp_path), b"original")
    fvh.seed_if_empty(tmp_path, _final(tmp_path), "Original")

    h = fvh.history(tmp_path)
    assert len(h["versions"]) == 1
    assert h["selected"] == h["versions"][0]["id"]
    assert h["versions"][0]["label"] == "Original"
    assert Path(h["versions"][0]["path"]).read_bytes() == b"original"


def test_seed_if_empty_is_noop_when_history_exists(tmp_path):
    _write(_final(tmp_path), b"original")
    fvh.seed_if_empty(tmp_path, _final(tmp_path))
    _write(_final(tmp_path), b"changed")
    fvh.seed_if_empty(tmp_path, _final(tmp_path))
    assert len(fvh.history(tmp_path)["versions"]) == 1


def test_record_keeps_all_versions_and_selects_newest(tmp_path):
    for tag, label in ((b"v1", "one"), (b"v2", "two"), (b"v3", "three")):
        _write(_final(tmp_path), tag)
        h = fvh.record(tmp_path, _final(tmp_path), label)

    assert len(h["versions"]) == 3
    assert h["selected"] == h["versions"][-1]["id"]
    contents = sorted(Path(v["path"]).read_bytes() for v in h["versions"])
    assert contents == [b"v1", b"v2", b"v3"]
    assert h["versions"][-1]["label"] == "three"


def test_select_copies_chosen_version_onto_canonical_final(tmp_path):
    _write(_final(tmp_path), b"one")
    fvh.record(tmp_path, _final(tmp_path), "first")
    _write(_final(tmp_path), b"two")
    h = fvh.record(tmp_path, _final(tmp_path), "second")
    older = h["versions"][0]["id"]

    fvh.select(tmp_path, older, _final(tmp_path))

    assert _final(tmp_path).read_bytes() == b"one"
    assert fvh.history(tmp_path)["selected"] == older


def test_select_unknown_version_raises(tmp_path):
    _write(_final(tmp_path), b"one")
    fvh.record(tmp_path, _final(tmp_path))
    try:
        fvh.select(tmp_path, 999, _final(tmp_path))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown version id")
