from pathlib import Path

import numpy as np
import pytest

from pipeline import face_repair, video_history


def test_face_repair_mode_honours_scene_override():
    assert face_repair.scene_mode({}, "auto") == "auto"
    assert face_repair.scene_mode({"metadata": {"face_repair_mode": "off"}}, "on") == "off"
    assert face_repair.scene_mode({"metadata": {"face_repair_mode": "on"}}, "off") == "on"
    assert face_repair.scene_mode({"metadata": {"face_repair_mode": "inherit"}}, "on") == "on"


def test_automatic_repair_skips_large_faces_and_fades_a_lost_track():
    found = np.array([True, True, False])
    actual = face_repair._blend_strength(np.array([100, 145, 100]), 1080, found, "auto")
    assert actual.tolist() == [1, .5, 0]
    with pytest.raises(face_repair.Skipped, match="large enough"):
        face_repair._blend_strength(np.array([160, 160, 90]), 1080, found, "auto")
    assert face_repair._blend_strength(np.array([160, 160, 90]), 1080, found, "on")[0] > 0


@pytest.mark.parametrize("select", [False, True])
def test_repair_keeps_original_and_can_be_selected(tmp_path: Path, select: bool):
    source = tmp_path / "scene_01_final.mp4"
    source.write_bytes(b"original")
    fixed = tmp_path / "repaired.mp4"
    fixed.write_bytes(b"repaired")
    history = video_history.record_face_repair(
        tmp_path, 1, source, fixed, video_history.file_identity(source), select=select)
    assert [Path(x["path"]).read_bytes() for x in history["versions"]] == [b"original", b"repaired"]
    assert history["versions"][1]["label"] == "H3 face repair"
    assert history["selected"] == (2 if select else 1)
    assert source.read_bytes() == (b"repaired" if select else b"original")
    assert video_history.current_is_face_repair(tmp_path, 1, source) is select
    video_history.select(tmp_path, 1, 1)
    assert source.read_bytes() == b"original"


def test_does_not_keep_repair_of_replaced_source(tmp_path: Path):
    source = tmp_path / "scene_01_final.mp4"
    source.write_bytes(b"original")
    identity = video_history.file_identity(source)
    source.write_bytes(b"new selection")
    fixed = tmp_path / "repaired.mp4"
    fixed.write_bytes(b"unrelated")
    with pytest.raises(RuntimeError, match="changed during face repair"):
        video_history.record_face_repair(tmp_path, 1, source, fixed, identity, select=True)
    assert source.read_bytes() == b"new selection"
    assert video_history.history(tmp_path, 1)["versions"] == []
