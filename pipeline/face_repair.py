"""Track one character's face, redraw it with H3, and blend it into the take."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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


def _track(source: Path, width: int, count: int, detect, checkpoint: Callable) -> tuple[np.ndarray, np.ndarray]:
    boxes = np.full((count, 4), np.nan)
    prior = None
    with _capture(source) as capture:
        for i in range(count):
            if i % 24 == 0:
                checkpoint()
            result = detect.predict(_read(capture), conf=.35, verbose=False, device="cpu")[0]
            candidates = result.boxes.xyxy.cpu().numpy()
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
    found = np.isfinite(boxes[:, 0])
    # Gaps are only used to smooth the crop. The composite never repairs a lost face.
    if found.mean() < .85:
        raise Skipped("No single face could be tracked reliably through this scene.")
    frames = np.arange(count)
    for col in range(4):
        boxes[:, col] = np.interp(frames, frames[found], boxes[found, col])
    return boxes, found


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


def _composite(source: Path, mask: Path, redraw: Path, dest: Path, crops: np.ndarray,
               strengths: np.ndarray, width: int, height: int, fps: int, checkpoint: Callable) -> None:
    import cv2

    with (_capture(source) as base, _capture(mask) as masks, _capture(redraw) as repaired,
          _encoder(dest, width, height, audio=source, fps=fps) as output):
        for i, ((x, y, side), strength) in enumerate(zip(crops, strengths)):
            if i % 24 == 0:
                checkpoint()
            frame, m, region = _read(base), _read(masks), _read(repaired)
            if strength:
                target = frame[y:y + side, x:x + side]
                edited = cv2.resize(region, (side, side), interpolation=cv2.INTER_AREA)
                alpha = cv2.resize(m[..., 0], (side, side), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
                edge = (alpha > .20) & (alpha < .70)
                if edge.sum() > 100:
                    lab = cv2.cvtColor(edited, cv2.COLOR_BGR2LAB)
                    ref = cv2.cvtColor(target, cv2.COLOR_BGR2LAB)
                    luma = np.clip(float(np.median(ref[..., 0][edge])) - float(np.median(lab[..., 0][edge])), -9, 9)
                    lab[..., 0] = (lab[..., 0].astype(np.float32) + luma).clip(0, 255).astype(np.uint8)
                    edited = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
                alpha = alpha[..., None] * strength
                target[:] = np.rint((1 - alpha) * target + alpha * edited).astype(np.uint8)
            output.write(frame.tobytes())


def repair_take(work_dir: Path, scene: dict, cfg: dict, pool, *, mode: str, select: bool,
                acquire=None, checkpoint=lambda: None) -> dict:
    """Create an H3 take from the current scene. Automatic calls select it on success."""
    from app import _scene_reference_characters, style_settings

    sid = int(scene["id"])
    source = work_dir / f"scene_{sid:02d}_final.mp4"
    if not source.is_file():
        raise Skipped("Render this scene before repairing its face.")
    style_name = cfg.get("style_name") or ""
    pairs = _scene_reference_characters(scene.get("video_prompt") or "", scene, cfg, style_name, work_dir)
    if len(pairs) != 1:
        raise Skipped("Face repair needs exactly one matched character with a reference portrait or sheet.")
    char, reference = pairs[0]
    # Prefer the portrait over a multi-view sheet, as in the comparison tests.
    portrait = char.get("_ref_path")
    if portrait and portrait.is_file():
        reference = portrait
    if video_history.current_is_face_repair(work_dir, sid, source):
        raise Skipped("Choose an original take before running face repair again.")
    identity = video_history.file_identity(source)
    media = _media(source)
    width, height, count, fps = _clock(media)
    boxes, found = _track(source, width, count, _detector(), checkpoint)
    crops, face_heights = _geometry(boxes, width, height)
    strengths = _blend_strength(face_heights, height, found, mode)
    if pool is None:
        raise RuntimeError("No ComfyUI workers are reachable for H3 face repair.")
    allowed = comfyui.h3_face_repair_workers(pool.urls)
    length = comfyui.h3_frame_count(count / 24)

    with tempfile.TemporaryDirectory(prefix="h3-face-repair-", dir=work_dir) as temp_name:
        temp = Path(temp_name)
        staged_source = temp / "source.mp4"
        shutil.copyfile(source, staged_source)
        if video_history.file_identity(source) != identity:
            raise RuntimeError("The original take changed during face tracking. Run face repair again.")
        crop, mask = _prepare(staged_source, temp, boxes, crops, length, checkpoint)
        audio = temp / "audio.wav"
        _model_audio(staged_source, audio, media, length, fps)
        redraw = temp / "redraw.mp4"
        style = (cfg.get("style") or style_settings(cfg, style_name).get("visual_style") or "").strip()
        prompt = (f"{style[:1200]} Preserve the reference character, original art style, head motion, "
                  "eye direction, expression, and mouth movements.").strip()
        url = acquire(allowed) if acquire else pool.acquire(allowed=allowed)
        try:
            checkpoint()
            comfyui.repair_h3_face(crop, mask, audio, reference, redraw, length, prompt, comfy_url=url)
        finally:
            pool.release(url)
        checkpoint()
        out = temp / "repaired.mp4"
        _composite(staged_source, mask, redraw, out, crops, strengths, width, height, fps, checkpoint)
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
