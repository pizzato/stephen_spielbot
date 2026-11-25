"""Track character faces, redraw each with its H3 reference, and blend them into the take."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import numpy as np

from pipeline import assembler, comfyui, video_history

LOG = logging.getLogger(__name__)
MODES = ("off", "auto", "on")
CANVAS = 768
DETECTOR_REV = "53cc19de382014514d9d4038601d261a7faa9b7b"
DETECTOR_HASH = "70b640f8f60b1cf0dcc72f30caf3da9495eb2fb6509da48c53374ad6806e6a9c"
DETECTOR_URL = f"https://huggingface.co/Bingsu/adetailer/resolve/{DETECTOR_REV}/face_yolov8n.pt"


class Skipped(ValueError):
    """The shot is outside the repair workflow's supported inputs."""


def scene_mode(scene: dict, default: str) -> str:
    mode = (scene.get("metadata") or {}).get("face_repair_mode")
    return mode if mode in MODES else default if default in MODES else "off"


def _detector():
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError("Install the controller's face repair dependencies (make install-face-repair).") from exc

    from app import CONFIG_FILE

    path = CONFIG_FILE.parent / "models" / "face_yolov8n.pt"
    if not path.exists():
        with urllib.request.urlopen(DETECTOR_URL, timeout=30) as response:
            data = response.read(7_000_000)
        if hashlib.sha256(data).hexdigest() != DETECTOR_HASH:
            raise RuntimeError("The downloaded H3 face detector failed its checksum.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
            staged.write(data)
        Path(staged.name).replace(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != DETECTOR_HASH:
        raise RuntimeError(f"The H3 face detector failed its checksum: {path}")
    return YOLO(str(path))


def _media(path: Path) -> dict:
    result = subprocess.check_output(
        [assembler._FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(path)],
        timeout=30,
    )
    return json.loads(result)


def _clock(media: dict) -> tuple[int, int, int, int]:
    video = next((s for s in media["streams"] if s["codec_type"] == "video"), None)
    rate = video.get("avg_frame_rate") if video else None
    if rate not in ("24/1", "25/1", "30/1") or video.get("r_frame_rate") != rate:
        raise Skipped("H3 face repair needs a constant-rate 24, 25 or 30 fps scene.")
    fps = int(rate.split("/")[0])
    count = int(video.get("nb_frames") or 0)
    if not 1 <= count <= 360 or abs(float(video.get("duration") or 0) - count / fps) > .01:
        raise Skipped("H3 face repair needs a scene with a fixed frame rate and at most 360 frames.")
    return int(video["width"]), int(video["height"]), count, fps


@contextmanager
def _capture(path: Path) -> Iterator:
    import cv2

    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise RuntimeError(f"Could not open video: {path.name}")
        yield capture
    finally:
        capture.release()


def _read(capture) -> np.ndarray:
    ok, frame = capture.read()
    if not ok:
        raise RuntimeError("Video ended before the expected frame count.")
    return frame


def _detections(source: Path, count: int, detect, checkpoint: Callable) -> Iterator[np.ndarray]:
    with _capture(source) as capture:
        for i in range(count):
            if i % 24 == 0:
                checkpoint()
            result = detect.predict(_read(capture), conf=.35, verbose=False, device="cpu")[0]
            yield result.boxes.xyxy.cpu().numpy()


def _finish_track(boxes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    found = np.isfinite(boxes[:, 0])
    # Gaps are only used to smooth the crop. The composite never repairs a lost face.
    if found.mean() < .85:
        raise Skipped("A face could not be tracked reliably through this scene.")
    frames = np.arange(len(boxes))
    for col in range(4):
        boxes[:, col] = np.interp(frames, frames[found], boxes[found, col])
    return boxes, found


def _track(detections: Iterator[np.ndarray], width: int, count: int) -> tuple[np.ndarray, np.ndarray]:
    boxes = np.full((count, 4), np.nan)
    prior = None
    for i, candidates in enumerate(detections):
        if not len(candidates):
            continue
        centers = (candidates[:, :2] + candidates[:, 2:]) / 2
        heights = candidates[:, 3] - candidates[:, 1]
        if prior is None:
            # Start with the most prominent face near the shot's centre.
            cost = abs(centers[:, 0] - width / 2) - heights * .6
            ordered = np.argsort(cost)
            if len(ordered) > 1 and cost[ordered[1]] - cost[ordered[0]] < .04 * width:
                continue
            choice = ordered[0]
        else:
            distance = np.linalg.norm(centers - (prior[:2] + prior[2:]) / 2, axis=1)
            choice = int(np.argmin(distance + .25 * abs(heights - (prior[3] - prior[1]))))
            if distance[choice] > max(width * .05, .9 * (prior[3] - prior[1])):
                continue
        prior = boxes[i] = candidates[choice]
    return _finish_track(boxes)


def _track_targets(
    detections: Iterator[np.ndarray], points: np.ndarray, width: int, count: int,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Follow each opening-frame point in image pixels, in assignment order."""
    boxes = np.full((len(points), count, 4), np.nan)
    last_seen = np.zeros(len(points), dtype=int)
    bystanders = np.empty((0, 4))
    for i, candidates in enumerate(detections):
        centers = (candidates[:, :2] + candidates[:, 2:]) / 2
        sizes = candidates[:, 2:] - candidates[:, :2]
        choices = set()
        for t, point in enumerate(points):
            if i == 0:
                hits = np.flatnonzero(np.all((candidates[:, :2] <= point) & (point <= candidates[:, 2:]), axis=1))
                if len(hits) != 1:
                    raise Skipped("Click inside a single detected face on the opening frame for each character.")
                choice = hits[0]
            else:
                if not len(candidates):
                    continue
                prior = boxes[t, last_seen[t]]
                center = (prior[:2] + prior[2:]) / 2
                height = prior[3] - prior[1]
                distances = np.linalg.norm(centers - center, axis=1)
                cost = distances + .25 * abs(sizes[:, 1] - height)
                choice = int(np.argmin(cost))
                if distances[choice] > min(max(width * .025, .55 * height), .9 * height):
                    continue
                # A nearby face that belonged to a bystander on the previous frame
                # must not take over a temporarily missing character's track.
                if len(bystanders):
                    other_centers = (bystanders[:, :2] + bystanders[:, 2:]) / 2
                    if np.linalg.norm(other_centers - centers[choice], axis=1).min() <= distances[choice]:
                        continue
                if i - last_seen[t] > 4:
                    raise Skipped("A selected face disappeared for too long to identify it again reliably.")
            if choice in choices:
                raise Skipped("Each character needs a different face that can be tracked separately.")
            # The feathered masks extend just beyond each box. Fail on a crossing
            # or occlusion, including with a face that the user did not select.
            nearby = np.all(abs(centers - centers[choice]) < .65 * (sizes + sizes[choice]), axis=1)
            if nearby.sum() > 1:
                raise Skipped("A selected face overlaps another face and cannot be tracked separately.")
            choices.add(choice)
            boxes[t, i] = candidates[choice]
            last_seen[t] = i
        bystanders = candidates[[j for j in range(len(candidates)) if j not in choices]]
    return [_finish_track(track) for track in boxes]


def _smooth(values: np.ndarray, sigma: float) -> np.ndarray:
    radius = round(3 * sigma)
    weights = np.exp(-.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    return np.convolve(np.pad(values, radius, mode="edge"), weights / weights.sum(), mode="valid")


def _geometry(boxes: np.ndarray, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    centers = (boxes[:, :2] + boxes[:, 2:]) / 2
    face_heights = _smooth(boxes[:, 3] - boxes[:, 1], 8)
    side = np.rint(face_heights * 2.5).astype(int).clip(2, min(width, height))
    x = np.rint(_smooth(centers[:, 0], 3) - side / 2).astype(int).clip(0, width - side)
    y = np.rint(_smooth(centers[:, 1], 3) - side / 2).astype(int).clip(0, height - side)
    return np.column_stack((x, y, side)), face_heights


def _blend_strength(heights: np.ndarray, height: int, found: np.ndarray, mode: str) -> np.ndarray:
    relative = heights * 1080 / height
    if mode == "auto" and np.median(relative[found]) > 150:
        raise Skipped("The main face is already large enough for this shot.")
    fade = np.clip((relative - 110) / 70, 0, 1)
    strength = (1 - fade**2 * (3 - 2 * fade)) * found
    if not strength.any():
        raise Skipped("The main face is already large enough for this shot.")
    return strength


@contextmanager
def _encoder(path: Path, width: int, height: int, pixel: str = "bgr24", crf: int = 16,
             audio: Path | None = None, fps: int = 24) -> Iterator:
    cmd = [assembler._FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", pixel,
           "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0"]
    if audio is not None:
        cmd += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a?", "-c:a", "copy"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", str(crf), "-vf",
            "scale=out_color_matrix=bt709", "-pix_fmt", "yuv420p", "-colorspace", "bt709",
            "-color_trc", "bt709", "-color_primaries", "bt709", "-movflags", "+faststart", str(path)]
    with subprocess.Popen(cmd, stdin=subprocess.PIPE) as process:
        try:
            yield process.stdin
            process.stdin.close()
            if process.wait(timeout=90):
                raise RuntimeError("Could not encode the H3 face repair clip.")
        except BaseException:
            process.kill()
            raise


def _prepare(source: Path, temp: Path, boxes: np.ndarray, crops: np.ndarray, length: int,
             checkpoint: Callable) -> tuple[Path, Path]:
    import cv2

    crop_file, mask_file = temp / "crop.mp4", temp / "mask.mp4"
    ys, xs = np.mgrid[:CANVAS, :CANVAS].astype(np.float32)
    with (_capture(source) as capture,
          _encoder(crop_file, CANVAS, CANVAS, crf=14) as crop_out,
          _encoder(mask_file, CANVAS, CANVAS, pixel="gray", crf=0) as mask_out):
        for i, ((x, y, side), (x0, y0, x1, y1)) in enumerate(zip(crops, boxes)):
            if i % 24 == 0:
                checkpoint()
            frame = _read(capture)
            crop = cv2.resize(frame[y:y + side, x:x + side], (CANVAS, CANVAS), interpolation=cv2.INTER_CUBIC)
            z = CANVAS / side
            fw, fh = (x1 - x0) * z, (y1 - y0) * z
            cx, cy = ((x0 + x1) / 2 - x) * z, ((y0 + y1) / 2 - y) * z
            radius = np.hypot((xs - cx) / (fw * .57), (ys - cy - fh * .018) / (fh * .55))
            opacity = np.clip((1.12 - radius) / .34, 0, 1)
            mask = np.uint8(np.rint(opacity**2 * (3 - 2 * opacity) * 255))
            crop_out.write(crop.tobytes())
            mask_out.write(mask.tobytes())
        # H3 accepts a restricted length grid. Hold the final image over model padding.
        for _ in range(length - len(crops)):
            crop_out.write(crop.tobytes())
            mask_out.write(mask.tobytes())
    return crop_file, mask_file


def _model_audio(source: Path, dest: Path, media: dict, length: int, fps: int) -> None:
    cmd = [assembler._FFMPEG, "-v", "error", "-y"]
    if any(s["codec_type"] == "audio" for s in media["streams"]):
        cmd += ["-i", str(source), "-map", "0:a:0", "-af", f"atempo={24 / fps},apad" if fps != 24 else "apad"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    subprocess.run(cmd + ["-t", str(length / 24), "-c:a", "pcm_s16le", str(dest)],
                   check=True, timeout=60)


def _blend_face(frame: np.ndarray, mask: np.ndarray, region: np.ndarray, crop: np.ndarray, strength: float) -> None:
    import cv2

    x, y, side = crop
    target = frame[y:y + side, x:x + side]
    edited = cv2.resize(region, (side, side), interpolation=cv2.INTER_AREA)
    alpha = cv2.resize(mask[..., 0], (side, side), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    edge = (alpha > .20) & (alpha < .70)
    if edge.sum() > 100:
        lab = cv2.cvtColor(edited, cv2.COLOR_BGR2LAB)
        ref = cv2.cvtColor(target, cv2.COLOR_BGR2LAB)
        luma = np.clip(float(np.median(ref[..., 0][edge])) - float(np.median(lab[..., 0][edge])), -9, 9)
        lab[..., 0] = (lab[..., 0].astype(np.float32) + luma).clip(0, 255).astype(np.uint8)
        edited = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    alpha = alpha[..., None] * strength
    target[:] = np.rint((1 - alpha) * target + alpha * edited).astype(np.uint8)


def _composite(source: Path, passes: list[tuple[Path, Path, np.ndarray, np.ndarray]], dest: Path,
               width: int, height: int, count: int, fps: int, checkpoint: Callable) -> None:
    with ExitStack() as stack:
        base = stack.enter_context(_capture(source))
        streams = [(stack.enter_context(_capture(mask)), stack.enter_context(_capture(redraw)), crops, strengths)
                   for mask, redraw, crops, strengths in passes]
        output = stack.enter_context(_encoder(dest, width, height, audio=source, fps=fps))
        for i in range(count):
            if i % 24 == 0:
                checkpoint()
            frame = _read(base)
            for masks, repaired, crops, strengths in streams:
                mask, region = _read(masks), _read(repaired)
                if strengths[i]:
                    _blend_face(frame, mask, region, crops[i], strengths[i])
            output.write(frame.tobytes())


def _identity_token(identity: tuple[int, int, int]) -> str:
    return "-".join(format(value, "x") for value in identity)


def source_token(path: Path) -> str:
    return _identity_token(video_history.file_identity(path))


def reference_pairs(work_dir: Path, scene: dict, cfg: dict) -> list[tuple[dict, Path]]:
    from app import _scene_reference_characters

    pairs = _scene_reference_characters(scene.get("video_prompt") or "", scene, cfg,
                                        cfg.get("style_name") or "", work_dir)
    # Prefer the portrait over a multi-view sheet, as in the comparison tests.
    refs = []
    for char, sheet in pairs:
        portrait = char.get("_ref_path")
        refs.append((char, portrait if portrait and portrait.is_file() else sheet))
    return refs


def repair_take(work_dir: Path, scene: dict, cfg: dict, pool, *, mode: str, select: bool,
                acquire=None, checkpoint=lambda: None, targets: list[dict] | None = None,
                source_token: str | None = None) -> dict:
    """Redraw each assigned face from the same take, then publish a single completed composite."""
    from app import style_settings

    sid = int(scene["id"])
    source = work_dir / f"scene_{sid:02d}_final.mp4"
    if not source.is_file():
        raise Skipped("Render this scene before repairing its face.")
    identity = video_history.file_identity(source)
    if source_token is not None and source_token != _identity_token(identity):
        raise Skipped("The selected take changed. Reopen face repair and select the faces again.")
    pairs = reference_pairs(work_dir, scene, cfg)
    if not pairs:
        raise Skipped("Face repair needs a matched character with a reference portrait or sheet.")
    if targets is None:
        if len(pairs) != 1:
            raise Skipped("Choose the character and face on the opening frame for a multi-character repair.")
        references = [pairs[0][1]]
    else:
        names = [target["character"] for target in targets]
        if not names or len(set(names)) != len(names):
            raise Skipped("Select at least one character and assign each character only once.")
        references = []
        for name in names:
            matches = [ref for char, ref in pairs if char.get("name") == name]
            if len(matches) != 1:
                raise Skipped(f"Cannot resolve a unique character reference for {name}.")
            references.append(matches[0])
        points = np.array([(target["x"], target["y"]) for target in targets], dtype=float)
        if not np.all((points >= 0) & (points <= 1)):
            raise Skipped("Choose face positions inside the opening frame.")
    if video_history.current_is_face_repair(work_dir, sid, source):
        raise Skipped("Choose an original take before running face repair again.")

    with tempfile.TemporaryDirectory(prefix="h3-face-repair-", dir=work_dir) as temp_name:
        temp = Path(temp_name)
        staged_source = temp / "source.mp4"
        shutil.copyfile(source, staged_source)
        if video_history.file_identity(source) != identity:
            raise RuntimeError("The original take changed during face tracking. Run face repair again.")
        media = _media(staged_source)
        width, height, count, fps = _clock(media)
        detections = _detections(staged_source, count, _detector(), checkpoint)
        if targets is None:
            tracks = [_track(detections, width, count)]
        else:
            tracks = _track_targets(detections, points * [width, height], width, count)
        plans = []
        for (boxes, found), reference in zip(tracks, references):
            crops, face_heights = _geometry(boxes, width, height)
            strengths = _blend_strength(face_heights, height, found, mode)
            plans.append((reference, boxes, crops, strengths))
        if pool is None:
            raise RuntimeError("No ComfyUI workers are reachable for H3 face repair.")
        allowed = comfyui.h3_face_repair_workers(pool.urls)
        length = comfyui.h3_frame_count(count / 24)
        audio = temp / "audio.wav"
        _model_audio(staged_source, audio, media, length, fps)
        style = (cfg.get("style") or style_settings(cfg, cfg.get("style_name") or "").get("visual_style") or "").strip()
        prompt = (f"{style[:1200]} Preserve the reference character, original art style, head motion, "
                  "eye direction, expression, and mouth movements.").strip()
        passes = []
        for index, (reference, boxes, crops, strengths) in enumerate(plans):
            folder = temp / str(index)
            folder.mkdir()
            crop, mask = _prepare(staged_source, folder, boxes, crops, length, checkpoint)
            redraw = folder / "redraw.mp4"
            url = acquire(allowed) if acquire else pool.acquire(allowed=allowed)
            try:
                checkpoint()
                comfyui.repair_h3_face(crop, mask, audio, reference, redraw, length, prompt, comfy_url=url)
            finally:
                pool.release(url)
            checkpoint()
            passes.append((mask, redraw, crops, strengths))
        out = temp / "repaired.mp4"
        _composite(staged_source, passes, out, width, height, count, fps, checkpoint)
        if _clock(_media(out)) != (width, height, count, fps):
            raise RuntimeError("The repaired video did not retain the source frame count.")
        checkpoint()
        return video_history.record_face_repair(work_dir, sid, source, out, identity, select=select)


def auto_repair_take(work_dir: Path, scene: dict, cfg: dict, pool, **kwargs) -> None:
    mode = scene_mode(scene, cfg.get("face_repair_mode", "off"))
    if mode == "off":
        return
    try:
        repair_take(work_dir, scene, cfg, pool, mode=mode, select=True, **kwargs)
    except Skipped as exc:
        LOG.info("Scene %s face repair skipped: %s", scene["id"], exc)
    except Exception as exc:
        if kwargs.get("checkpoint"):
            kwargs["checkpoint"]()  # allow cancellation to propagate
        LOG.warning("Scene %s face repair failed; kept the original take: %s", scene["id"], exc)
