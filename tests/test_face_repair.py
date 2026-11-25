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


def test_targeted_tracks_follow_clicked_faces_when_detector_order_changes():
    left, right = [10, 20, 30, 50], [70, 20, 90, 50]
    frames = map(np.array, [[left, right], [right, left], [left, right]])
    tracks = face_repair._track_targets(frames, np.array([[80, 35], [20, 35]]), 100, 3)
    for (boxes, found), expected in zip(tracks, [right, left]):
        np.testing.assert_equal(boxes, [expected] * 3)
        assert found.all()


@pytest.mark.parametrize(("points", "message"), [
    ([[50, 35]], "inside a single detected face"),
    ([[20, 35], [20, 35]], "different face"),
])
def test_targeted_tracks_require_distinct_opening_faces(points, message):
    frames = iter([np.array([[10, 20, 30, 50], [70, 20, 90, 50]])])
    with pytest.raises(face_repair.Skipped, match=message):
        face_repair._track_targets(frames, np.array(points), 100, 1)


def test_targeted_track_rejects_overlap_with_unselected_face():
    frames = map(np.array, [[[10, 20, 30, 50], [70, 20, 90, 50]],
                            [[19, 20, 39, 50], [39, 20, 59, 50]]])
    with pytest.raises(face_repair.Skipped, match="overlaps another face"):
        face_repair._track_targets(frames, np.array([[20, 35]]), 100, 2)


def test_unselected_bystander_cannot_inherit_a_missing_faces_track():
    frames = map(np.array, [[[40, 10, 50, 60], [60, 10, 70, 60]],
                            [[58, 10, 68, 60]]])
    with pytest.raises(face_repair.Skipped, match="tracked reliably"):
        face_repair._track_targets(frames, np.array([[45, 35]]), 100, 2)


@pytest.mark.parametrize(("selected", "fail_second"), [
    (["Right"], False), (["Right", "Left"], False), (["Right", "Left"], True),
])
def test_targeted_repair_publishes_only_the_complete_take(tmp_path, monkeypatch, selected, fail_second):
    from unittest.mock import Mock

    source = tmp_path / "scene_01_final.mp4"
    source.write_bytes(b"original")
    pairs = [({"name": name}, tmp_path / name) for name in ["Left", "Right"]]
    monkeypatch.setattr(face_repair, "reference_pairs", lambda *args: pairs)
    monkeypatch.setattr(face_repair, "_media", lambda path: {"streams": [{
        "codec_type": "video", "width": 600, "height": 800, "nb_frames": "2",
        "avg_frame_rate": "24/1", "r_frame_rate": "24/1", "duration": str(2 / 24),
    }]})
    monkeypatch.setattr(face_repair, "_detector", lambda: None)
    left, right = [100, 170, 140, 230], [460, 170, 500, 230]
    detect = Mock(return_value=map(np.array, [[left, right], [right, left]]))
    monkeypatch.setattr(face_repair, "_detections", detect)
    monkeypatch.setattr(face_repair.comfyui, "h3_face_repair_workers", lambda urls: urls)
    monkeypatch.setattr(face_repair, "_model_audio", lambda *args: None)

    def prepare(staged, folder, boxes, crops, length, checkpoint):
        assert staged.read_bytes() == b"original"
        (folder / "crop").write_text("Right" if boxes[0, 0] == right[0] else "Left")
        return folder / "crop", folder / "mask"
    monkeypatch.setattr(face_repair, "_prepare", prepare)

    passes = []
    def redraw(crop, mask, audio, reference, output, *args, **kwargs):
        assert crop.read_text() == reference.name
        passes.append(reference.name)
        if fail_second and len(passes) == 2:
            raise RuntimeError("second redraw failed")
        output.write_text(reference.name)
    monkeypatch.setattr(face_repair.comfyui, "repair_h3_face", redraw)

    def composite(staged, repairs, out, *args):
        out.write_text(" ".join(redraw.read_text() for mask, redraw, crops, strengths in repairs))
    monkeypatch.setattr(face_repair, "_composite", composite)
    targets = [{"character": name, "x": .8 if name == "Right" else .2, "y": .25} for name in selected]
    pool = Mock(urls=["worker"])
    def run():
        return face_repair.repair_take(tmp_path, {"id": 1}, {"style": "cartoon"}, pool, mode="on", select=False,
                                       targets=targets, source_token=face_repair.source_token(source))
    if fail_second:
        with pytest.raises(RuntimeError, match="second redraw failed"):
            run()
        assert video_history.history(tmp_path, 1)["versions"] == []
    else:
        history = run()
        assert [Path(v["path"]).read_bytes() for v in history["versions"]] == [b"original", " ".join(selected).encode()]
        assert history["selected"] == 1
    assert passes == selected
    assert pool.release.call_count == len(selected)
    detect.assert_called_once()
    assert source.read_bytes() == b"original"


def test_face_assignments_require_the_same_take(tmp_path):
    source = tmp_path / "scene_01_final.mp4"
    source.write_bytes(b"original")
    token = face_repair.source_token(source)
    source.write_bytes(b"different selected take")
    with pytest.raises(face_repair.Skipped, match="selected take changed"):
        face_repair.repair_take(tmp_path, {"id": 1}, {}, None, mode="on", select=False,
                                targets=[{"character": "Left", "x": .2, "y": .3}], source_token=token)


def test_automatic_repair_never_guesses_multi_character_assignment(tmp_path, monkeypatch):
    source = tmp_path / "scene_01_final.mp4"
    source.write_bytes(b"original")
    monkeypatch.setattr(face_repair, "reference_pairs", lambda *args: [
        ({"name": "Left"}, tmp_path / "left"), ({"name": "Right"}, tmp_path / "right")])
    with pytest.raises(face_repair.Skipped, match="multi-character"):
        face_repair.repair_take(tmp_path, {"id": 1}, {}, None, mode="on", select=False)
    face_repair.auto_repair_take(tmp_path, {"id": 1}, {"face_repair_mode": "on"}, None)
    assert video_history.history(tmp_path, 1)["versions"] == []
    assert source.read_bytes() == b"original"


def test_composites_both_faces_into_the_same_frames_and_leaves_lost_faces_alone(monkeypatch):
    from contextlib import nullcontext
    from io import BytesIO

    output = BytesIO()
    # Represent the left and right face as one pixel each, and verify the final
    # encoder receives a single stream with all visible repairs applied.
    frames = {
        "source": [np.array([5, 7], dtype=np.uint8) for _ in range(2)],
        "mask": [255, 255],
        "left": [11, 12],
        "right": [21, 22],
    }
    monkeypatch.setattr(face_repair, "_capture", lambda path: nullcontext(iter(frames[path])))
    monkeypatch.setattr(face_repair, "_read", next)
    monkeypatch.setattr(face_repair, "_encoder", lambda *args, **kwargs: nullcontext(output))

    def blend(frame, mask, color, index, strength):
        frame[index] = color
    monkeypatch.setattr(face_repair, "_blend_face", blend)
    passes = [("mask", "left", [0, 0], [1, 1]), ("mask", "right", [1, 1], [1, 0])]
    face_repair._composite("source", passes, "out", 2, 1, 2, 24, lambda: None)
    assert list(output.getvalue()) == [11, 21, 12, 7]
