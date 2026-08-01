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


def test_delete_removes_unused_version_and_file(tmp_path):
    _write(_final(tmp_path), b"one")
    fvh.record(tmp_path, _final(tmp_path), "first")
    _write(_final(tmp_path), b"two")
    h = fvh.record(tmp_path, _final(tmp_path), "second")
    older = h["versions"][0]

    h2 = fvh.delete(tmp_path, older["id"])

    assert [v["id"] for v in h2["versions"]] == [h["selected"]]
    assert not Path(older["path"]).exists()
    assert _final(tmp_path).read_bytes() == b"two"   # canonical untouched


def test_delete_refuses_the_version_in_use(tmp_path):
    _write(_final(tmp_path), b"one")
    fvh.record(tmp_path, _final(tmp_path), "first")
    _write(_final(tmp_path), b"two")
    h = fvh.record(tmp_path, _final(tmp_path), "second")
    try:
        fvh.delete(tmp_path, h["selected"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for the version in use")
    assert len(fvh.history(tmp_path)["versions"]) == 2


def test_delete_unknown_version_raises(tmp_path):
    _write(_final(tmp_path), b"one")
    fvh.record(tmp_path, _final(tmp_path))
    try:
        fvh.delete(tmp_path, 999)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown version id")


def test_record_or_replace_overwrites_the_same_label_in_place(tmp_path):
    _write(_final(tmp_path), b"one")
    fvh.record(tmp_path, _final(tmp_path), "Original")
    _write(_final(tmp_path), b"rebuilt")
    fvh.record_or_replace(tmp_path, _final(tmp_path), "Rebuilt from scenes")
    _write(_final(tmp_path), b"rebuilt-again")
    h = fvh.record_or_replace(tmp_path, _final(tmp_path), "Rebuilt from scenes")

    # Reassembly re-runs on every parts change; it must not grow the history.
    assert [v["label"] for v in h["versions"]] == ["Original", "Rebuilt from scenes"]
    assert Path(h["versions"][-1]["path"]).read_bytes() == b"rebuilt-again"
    assert h["selected"] == h["versions"][-1]["id"]


def test_is_base_distinguishes_plain_concat_from_derived_cuts(tmp_path):
    assert fvh.is_base({"label": "Original", "kind": ""})
    assert not fvh.is_base({"label": "LTX IC-LoRA 1920x1080", "kind": "upscale"})
    assert not fvh.is_base({"label": "Portuguese", "kind": "localize"})


def test_is_base_falls_back_to_the_label_on_pre_kind_manifests(tmp_path):
    assert fvh.is_base({"label": "Original"})
    assert not fvh.is_base({"label": "LTX IC-LoRA 1920x1080"})
