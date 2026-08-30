"""Unit tests for pipeline.image_history — the per-scene image version store.

The module only copies files around, so the "images" here are tiny byte strings;
distinct bytes per version let us assert that ``select`` copies the *right* one.
"""
import os
import time
from pathlib import Path

from pipeline import image_history as ih


def _write(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _preview(wd: Path, sid: int = 1) -> Path:
    return wd / f"scene_{sid:02d}_preview.png"


def _first_frame(wd: Path, sid: int = 1) -> Path:
    return wd / f"scene_{sid:02d}_first_frame.png"


def test_capture_current_records_existing_as_v1(tmp_path):
    _write(_preview(tmp_path), b"original")
    ih.capture_current(tmp_path, 1, _preview(tmp_path))

    h = ih.history(tmp_path, 1)
    assert len(h["versions"]) == 1
    assert h["selected"] == h["versions"][0]["id"]
    assert Path(h["versions"][0]["path"]).read_bytes() == b"original"


def test_capture_current_is_noop_when_already_kept(tmp_path):
    _write(_preview(tmp_path), b"original")
    ih.capture_current(tmp_path, 1, _preview(tmp_path))
    # Capturing the same content again must not duplicate the version.
    ih.capture_current(tmp_path, 1, _preview(tmp_path))

    assert len(ih.history(tmp_path, 1)["versions"]) == 1


def test_capture_current_keeps_unrecorded_canonical_when_history_exists(tmp_path):
    # A frame written outside the editors (render-time opening-frame painter,
    # continuation handoff) is on disk but not in history — capturing before a
    # regeneration must keep it even though history is not empty.
    _write(_preview(tmp_path), b"editor version")
    ih.record(tmp_path, 1, _preview(tmp_path))
    _write(_preview(tmp_path), b"render-painted")
    ih.capture_current(tmp_path, 1, _preview(tmp_path))

    h = ih.history(tmp_path, 1)
    assert len(h["versions"]) == 2
    assert Path(h["versions"][-1]["path"]).read_bytes() == b"render-painted"
    assert h["selected"] == h["versions"][-1]["id"]


def test_capture_current_is_noop_when_current_missing(tmp_path):
    ih.capture_current(tmp_path, 1, _preview(tmp_path))  # no file on disk
    assert ih.history(tmp_path, 1)["versions"] == []


def test_record_appends_selects_newest_and_keeps_all(tmp_path):
    _write(_preview(tmp_path), b"one")
    ih.record(tmp_path, 1, _preview(tmp_path))
    _write(_preview(tmp_path), b"two")
    h = ih.record(tmp_path, 1, _preview(tmp_path))

    assert len(h["versions"]) == 2                      # keep all
    assert h["selected"] == h["versions"][-1]["id"]     # newest selected
    by_id = {v["id"]: Path(v["path"]).read_bytes() for v in h["versions"]}
    assert sorted(by_id.values()) == [b"one", b"two"]   # both retained


def test_select_copies_chosen_version_onto_canonical_preview(tmp_path):
    _write(_preview(tmp_path), b"one")
    ih.record(tmp_path, 1, _preview(tmp_path))
    _write(_preview(tmp_path), b"two")
    h = ih.record(tmp_path, 1, _preview(tmp_path))
    older = h["versions"][0]["id"]

    ih.select(tmp_path, 1, older)

    assert _preview(tmp_path).read_bytes() == b"one"
    assert ih.history(tmp_path, 1)["selected"] == older


def test_select_updates_first_frame_only_when_it_exists(tmp_path):
    _write(_preview(tmp_path), b"one")
    ih.record(tmp_path, 1, _preview(tmp_path))
    _write(_preview(tmp_path), b"two")
    h = ih.record(tmp_path, 1, _preview(tmp_path))
    older = h["versions"][0]["id"]

    # No first frame yet → select must not create one.
    ih.select(tmp_path, 1, older)
    assert not _first_frame(tmp_path).exists()

    # Once a first frame exists, select keeps it in sync with the chosen version.
    _write(_first_frame(tmp_path), b"stale")
    newer = h["versions"][-1]["id"]
    ih.select(tmp_path, 1, newer)
    assert _first_frame(tmp_path).read_bytes() == b"two"


def test_select_unknown_version_raises(tmp_path):
    _write(_preview(tmp_path), b"one")
    ih.record(tmp_path, 1, _preview(tmp_path))
    try:
        ih.select(tmp_path, 1, 999)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown version id")


def test_history_filters_missing_files(tmp_path):
    _write(_preview(tmp_path), b"one")
    ih.record(tmp_path, 1, _preview(tmp_path))
    _write(_preview(tmp_path), b"two")
    h = ih.record(tmp_path, 1, _preview(tmp_path))

    # Delete the currently-selected version's file on disk.
    Path(h["versions"][-1]["path"]).unlink()
    h2 = ih.history(tmp_path, 1)
    assert len(h2["versions"]) == 1
    assert h2["selected"] == h2["versions"][0]["id"]   # falls back to a present one


def test_scenes_are_tracked_independently(tmp_path):
    _write(_preview(tmp_path, 1), b"s1")
    ih.record(tmp_path, 1, _preview(tmp_path, 1))
    _write(_preview(tmp_path, 2), b"s2")
    ih.record(tmp_path, 2, _preview(tmp_path, 2))

    assert len(ih.history(tmp_path, 1)["versions"]) == 1
    assert len(ih.history(tmp_path, 2)["versions"]) == 1
    assert Path(ih.history(tmp_path, 2)["versions"][0]["path"]).read_bytes() == b"s2"


def test_delete_removes_unused_version_and_file(tmp_path):
    _write(_preview(tmp_path), b"one")
    ih.record(tmp_path, 1, _preview(tmp_path))
    _write(_preview(tmp_path), b"two")
    h = ih.record(tmp_path, 1, _preview(tmp_path))
    older = h["versions"][0]

    h2 = ih.delete(tmp_path, 1, older["id"])

    assert [v["id"] for v in h2["versions"]] == [h["selected"]]
    assert not Path(older["path"]).exists()
    assert _preview(tmp_path).read_bytes() == b"two"   # canonical untouched


def test_delete_refuses_the_version_in_use(tmp_path):
    _write(_preview(tmp_path), b"one")
    ih.record(tmp_path, 1, _preview(tmp_path))
    _write(_preview(tmp_path), b"two")
    h = ih.record(tmp_path, 1, _preview(tmp_path))
    try:
        ih.delete(tmp_path, 1, h["selected"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for the version in use")
    assert len(ih.history(tmp_path, 1)["versions"]) == 2


def test_delete_unknown_version_raises(tmp_path):
    _write(_preview(tmp_path), b"one")
    ih.record(tmp_path, 1, _preview(tmp_path))
    try:
        ih.delete(tmp_path, 1, 999)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown version id")


def test_cover_select_restores_bytes_and_stamps_selection_time(tmp_path):
    cover = tmp_path / "cover.png"
    _write(cover, b"one")
    ih.cover_record(tmp_path, cover)
    _write(cover, b"two")
    h = ih.cover_record(tmp_path, cover)
    v1 = h["versions"][0]["id"]

    # Age everything: plain copy2 would hand cover.png the kept version's old
    # mtime, hiding the selection from mtime-keyed consumers (the stale-final
    # sweep, /api/file cache-busters).
    old = time.time() - 3600
    for v in ih.cover_history(tmp_path)["versions"]:
        os.utime(v["path"], (old, old))
    os.utime(cover, (old, old))

    before = time.time()
    out = ih.cover_select(tmp_path, v1)

    assert out.read_bytes() == b"one"
    assert ih.cover_history(tmp_path)["selected"] == v1
    assert out.stat().st_mtime >= before - 1


def test_cover_delete_removes_unused_version(tmp_path):
    cover = tmp_path / "cover.png"
    _write(cover, b"one")
    ih.cover_record(tmp_path, cover)
    _write(cover, b"two")
    h = ih.cover_record(tmp_path, cover)
    older = h["versions"][0]

    h2 = ih.cover_delete(tmp_path, older["id"])

    assert [v["id"] for v in h2["versions"]] == [h["selected"]]
    assert not Path(older["path"]).exists()
    assert cover.read_bytes() == b"two"


def test_char_delete_removes_unused_version(tmp_path):
    look = tmp_path / "characters" / "abc.png"
    _write(look, b"one")
    ih.char_record(tmp_path, "abc", look)
    _write(look, b"two")
    h = ih.char_record(tmp_path, "abc", look)
    older = h["versions"][0]

    h2 = ih.char_delete(tmp_path, "abc", older["id"])

    assert [v["id"] for v in h2["versions"]] == [h["selected"]]
    assert not Path(older["path"]).exists()
    assert look.read_bytes() == b"two"
