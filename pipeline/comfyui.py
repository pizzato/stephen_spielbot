"""ComfyUI API client for video and music generation."""

from __future__ import annotations

import json
import logging
import random
import subprocess
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
import websocket  # websocket-client
from pathlib import Path

logger = logging.getLogger("video_gen")


class StuckJobError(RuntimeError):
    """ComfyUI job was running but made no node progress for too long."""


class DroppedJobError(RuntimeError):
    """ComfyUI job disappeared from the queue without completing."""

_BOUNDARY = "----ComfyUIBoundary"

COMFYUI_URL = "http://localhost:8188"
WORKFLOWS_DIR = Path(__file__).parent.parent / "workflows"

# LTX 2.3 video defaults (25 fps, two-pass upscaled)
DEFAULT_WIDTH  = 832
DEFAULT_HEIGHT = 480
LTX_FPS        = 25
DEFAULT_LENGTH = LTX_FPS * 5 + 1   # 126 frames ≈ 5 seconds at 25 fps
LTX_MAX_FRAMES = 1000              # LTXVEmptyLatentAudio rejects frames_number > 1000 (~40 s)


def _frame_count(length: int, duration_seconds: float | None) -> int:
    """Frames for a clip request, clamped to what the LTX graph accepts.
    A clip cut short by the clamp is still usable: the muxer freeze-pads the
    last frame to cover the rest of the narration."""
    if duration_seconds is not None:
        length = max(1, int(duration_seconds * LTX_FPS) + 1)
    if length > LTX_MAX_FRAMES:
        logger.warning("[comfy] clamping %d frames to LTX max %d", length, LTX_MAX_FRAMES)
        length = LTX_MAX_FRAMES
    return length


def ltx_dimensions(width: int, height: int) -> tuple[int, int]:
    """Snap (width, height) to the grid LTX can actually render.

    The i2v workflow builds its first-pass latent at half resolution and the LTX
    VAE compresses space by 32, so each half-dimension is floored to a multiple
    of 32 — i.e. the full output dimension lands on a multiple of 64. Passing an
    arbitrary size (e.g. 1080) makes LTX silently shrink it (1080 → 1024), so the
    clips no longer match the FLUX first frame. Snapping up front keeps the first
    frame, the clips and the final video identical, with no rescaling.
    """
    return (max(64, (width // 64) * 64), max(64, (height // 64) * 64))

# First-pass sigma schedules.
# The 8-step schedule is the distilled LoRA preset (trained specifically for these values).
# For the dev model (LoRA strength=0), use more steps with a linear schedule.
_DISTILLED_SIGMAS = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"


def _gen_first_pass_sigmas(steps: int) -> str:
    """Return sigma schedule for the first diffusion pass.
    8 steps → distilled LoRA preset.  Other counts → linear (for dev model).
    """
    if steps == 8:
        return _DISTILLED_SIGMAS
    vals = [round(1.0 - i / steps, 6) for i in range(steps + 1)]
    return ", ".join(str(v) for v in vals)


def _video_timeout_seconds(width: int, height: int, length: int) -> int:
    base_px = DEFAULT_WIDTH * DEFAULT_HEIGHT
    base_frames = DEFAULT_LENGTH
    return max(900, int(900 * (width * height) / base_px * (length / base_frames)))


# Second-pass (refinement) sigma schedules.
_SECOND_PASS_SIGMAS: dict[int, str] = {
    3: "0.85, 0.7250, 0.4219, 0.0",
    4: "0.85, 0.750, 0.550, 0.300, 0.0",
    5: "0.85, 0.750, 0.600, 0.400, 0.200, 0.0",
    6: "0.85, 0.750, 0.625, 0.500, 0.350, 0.175, 0.0",
    8: "0.85, 0.750, 0.650, 0.550, 0.450, 0.325, 0.200, 0.100, 0.0",
}


def _load_workflow(name: str) -> str:
    path = WORKFLOWS_DIR / name
    return path.read_text()


def _fill_template(workflow_text: str, replacements: dict) -> dict:
    """Replace {{KEY}} placeholders in raw workflow JSON text, then parse."""
    for key, val in replacements.items():
        placeholder = "{{" + key + "}}"
        workflow_text = workflow_text.replace('"' + placeholder + '"', json.dumps(val))
        workflow_text = workflow_text.replace(placeholder, str(val))
    return json.loads(workflow_text)


def _ws_url(comfy_url: str, client_id: str) -> str:
    """Derive WebSocket URL from an HTTP ComfyUI base URL."""
    return (comfy_url.replace("https://", "wss://")
                     .replace("http://", "ws://")) + f"/ws?clientId={client_id}"


def _comfy_has_cuda(comfy_url: str) -> bool:
    """True if the ComfyUI backend reports a CUDA device."""
    try:
        with urllib.request.urlopen(f"{comfy_url}/system_stats", timeout=5) as r:
            stats = json.loads(r.read())
        return any(d.get("type") == "cuda" for d in (stats.get("devices") or []))
    except Exception:
        return False


def _queue_prompt(workflow: dict, client_id: str, comfy_url: str = COMFYUI_URL) -> str:
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{comfy_url}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"ComfyUI rejected workflow ({comfy_url}): {e.code}\n{body}")
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"ComfyUI is not reachable at {comfy_url} — is it running?\n"
            f"Start it with:  cd ~/github/ComfyUI && source ../comfyui-env/bin/activate && python main.py --listen\n"
            f"({e.reason})"
        )

    if "error" in data:
        raise RuntimeError(f"ComfyUI error: {data['error']}\nNode errors: {data.get('node_errors', {})}")

    return data["prompt_id"]


def _poll_completion(prompt_id: str, deadline: float, comfy_url: str = COMFYUI_URL) -> None:
    """Poll /history until the prompt completes or deadline passes."""
    while time.time() < deadline:
        url = f"{comfy_url}/history/{prompt_id}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                history = json.loads(resp.read())
            if prompt_id in history:
                entry  = history[prompt_id]
                status = entry.get("status", {})
                if status.get("completed"):
                    return
                if status.get("status_str") == "error":
                    msgs = status.get("messages", [])
                    raise RuntimeError(f"ComfyUI job failed: {msgs}")
        except urllib.error.URLError:
            pass
        time.sleep(5)
    raise RuntimeError(f"ComfyUI timed out polling history for {prompt_id} ({comfy_url})")


def _check_queue(prompt_id: str, comfy_url: str = COMFYUI_URL) -> str:
    """Return 'running', 'pending', or 'absent' for prompt_id in ComfyUI's queue."""
    try:
        with urllib.request.urlopen(f"{comfy_url}/queue", timeout=10) as resp:
            queue = json.loads(resp.read())
        for item in queue.get("queue_running", []):
            if len(item) > 1 and item[1] == prompt_id:
                return "running"
        for item in queue.get("queue_pending", []):
            if len(item) > 1 and item[1] == prompt_id:
                return "pending"
    except Exception:
        pass
    return "absent"


def _post_json(path: str, payload: dict | None, comfy_url: str = COMFYUI_URL, timeout: int = 10) -> None:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f"{comfy_url}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def _delete_pending_prompt(prompt_id: str, comfy_url: str = COMFYUI_URL) -> None:
    """Remove a prompt from ComfyUI's pending queue."""
    try:
        _post_json("/queue", {"delete": [prompt_id]}, comfy_url=comfy_url)
        logger.info("[comfy] deleted pending job %s… on %s", prompt_id[:8], comfy_url)
    except Exception as exc:
        logger.warning("[comfy] could not delete pending job %s… on %s: %s",
                       prompt_id[:8], comfy_url, exc)


def _interrupt_running_prompt(prompt_id: str, comfy_url: str = COMFYUI_URL) -> None:
    """Interrupt the currently running prompt on a worker."""
    try:
        _post_json("/interrupt", {}, comfy_url=comfy_url)
        logger.info("[comfy] interrupted running job %s… on %s", prompt_id[:8], comfy_url)
    except Exception as exc:
        logger.warning("[comfy] could not interrupt running job %s… on %s: %s",
                       prompt_id[:8], comfy_url, exc)


def _cleanup_prompt(prompt_id: str, comfy_url: str = COMFYUI_URL, reason: str = "") -> None:
    q_status = _check_queue(prompt_id, comfy_url)
    if q_status == "pending":
        _delete_pending_prompt(prompt_id, comfy_url)
    elif q_status == "running":
        logger.warning("[comfy] interrupting job %s… on %s (%s)",
                       prompt_id[:8], comfy_url, reason or "cleanup")
        _interrupt_running_prompt(prompt_id, comfy_url)


def _check_history(prompt_id: str, comfy_url: str = COMFYUI_URL) -> str:
    """Return 'completed', 'error', or 'absent' from /history."""
    try:
        with urllib.request.urlopen(f"{comfy_url}/history/{prompt_id}", timeout=10) as resp:
            hist = json.loads(resp.read())
        if prompt_id in hist:
            s = hist[prompt_id].get("status", {})
            if s.get("completed"):
                return "completed"
            if s.get("status_str") == "error":
                return "error"
    except Exception:
        pass
    return "absent"


_QUEUE_CHECK_INTERVAL      = 30    # seconds between /queue health checks (was 60)
_STUCK_TIMEOUT             = 1800  # seconds of WS silence from a *running* job → StuckJobError (was 7200)
_PENDING_TIMEOUT           = 180   # seconds a job may sit *pending* before we try another worker
_HEARTBEAT_INTERVAL        = 90    # seconds between GPU idle checks when job is running (was 300)
_HEARTBEAT_INITIAL_DELAY   = 120   # seconds to skip heartbeat after job start (model load warmup)
_HEARTBEAT_IDLE_THRESHOLD  = 15    # GPU utilisation % below this is considered idle
_HEARTBEAT_IDLE_SAMPLES    = 2     # consecutive idle heartbeats required to declare stuck (was 3)
_HEARTBEAT_SSH_FAIL_LIMIT  = 5     # consecutive SSH failures before logging a WARNING


def _hostname_from_url(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or url


def _check_gpu_idle(host: str) -> bool | None:
    """SSH to host, check GPU utilisation via nvidia-smi.

    Returns True if all GPUs are below _HEARTBEAT_IDLE_THRESHOLD, False if any
    are busy, None if the check could not be completed (SSH/nvidia-smi failure).
    """
    try:
        result = subprocess.run(
            [
                "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no", host,
                "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null",
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None
        utils = [int(ln.strip()) for ln in result.stdout.splitlines() if ln.strip().isdigit()]
        if not utils:
            return None
        busy = any(u >= _HEARTBEAT_IDLE_THRESHOLD for u in utils)
        logger.debug(
            "[heartbeat] %s GPU util: %s — %s",
            host, utils, "busy" if busy else "idle",
        )
        return not busy  # True = idle, False = busy
    except Exception as exc:
        logger.debug("[heartbeat] %s GPU check failed: %s", host, exc)
        return None


def _wait_for_completion(
    prompt_id: str, client_id: str,
    timeout: int = 900, comfy_url: str = COMFYUI_URL,
) -> None:
    """Block until ComfyUI finishes executing prompt_id.

    Actively monitors the job via two independent stuck-detection paths:

    1. WebSocket silence: /queue is polled every _QUEUE_CHECK_INTERVAL s.
       If the job is running but no WS activity has been seen for _STUCK_TIMEOUT s
       (reset by any progress/executing message), raises StuckJobError.

    2. GPU heartbeat (primary fast-path): every _HEARTBEAT_INTERVAL s (after an
       initial _HEARTBEAT_INITIAL_DELAY warm-up for model loading), SSH to the
       worker and run nvidia-smi.  If GPU utilisation stays below
       _HEARTBEAT_IDLE_THRESHOLD for _HEARTBEAT_IDLE_SAMPLES consecutive samples,
       interrupt the job and raise StuckJobError immediately.
       Detection time: _HEARTBEAT_INITIAL_DELAY + _HEARTBEAT_IDLE_SAMPLES *
       _HEARTBEAT_INTERVAL = 120 + 2*90 = ~5 min (vs old ~15 min).

    Falls back to polling /history if the WebSocket drops.
    """
    ws = websocket.WebSocket()
    ws.connect(_ws_url(comfy_url, client_id))
    start            = time.time()
    deadline         = start + timeout
    last_activity_at: float | None = None   # None until the job actually starts executing
    last_queue_check = time.time()
    nodes_done       = 0
    # Schedule first heartbeat after the initial warm-up delay so that model-loading
    # time (where the GPU may be at 0 %) doesn't trigger a false stuck detection.
    last_heartbeat_at = start - _HEARTBEAT_INTERVAL + _HEARTBEAT_INITIAL_DELAY
    consecutive_idle  = 0
    consecutive_ssh_failures = 0
    host              = _hostname_from_url(comfy_url)

    try:
        while time.time() < deadline:
            now = time.time()

            # ── periodic queue health check ────────────────────────────────
            if now - last_queue_check >= _QUEUE_CHECK_INTERVAL:
                last_queue_check = now
                q_status = _check_queue(prompt_id, comfy_url)
                logger.info(
                    "[comfy] job %s… queue=%s nodes_done=%d elapsed=%.0fs",
                    prompt_id[:8], q_status, nodes_done, now - start,
                )

                if q_status == "absent":
                    h_status = _check_history(prompt_id, comfy_url)
                    if h_status == "completed":
                        return
                    if h_status == "error":
                        raise RuntimeError(f"ComfyUI job {prompt_id} failed (history error)")
                    raise DroppedJobError(
                        f"Job {prompt_id} vanished from queue on {comfy_url} without completing"
                        f" — will re-submit"
                    )

                # Pending timeout: worker's queue is blocked by another job.
                if q_status == "pending" and now - start > _PENDING_TIMEOUT:
                    _delete_pending_prompt(prompt_id, comfy_url)
                    raise StuckJobError(
                        f"Job {prompt_id} on {comfy_url} has been pending in queue for"
                        f" {now - start:.0f}s (limit {_PENDING_TIMEOUT}s)"
                        f" — worker blocked, will try another"
                    )

                if q_status == "running":
                    # ── GPU heartbeat: sample utilisation every _HEARTBEAT_INTERVAL ──
                    if now - last_heartbeat_at >= _HEARTBEAT_INTERVAL:
                        last_heartbeat_at = now
                        gpu_idle = _check_gpu_idle(host)
                        if gpu_idle is False:
                            # GPU is busy — worker is genuinely doing work.
                            # Reset the idle counter and treat it as WebSocket activity
                            # so the silence-based stuck timer doesn't fire.
                            consecutive_idle = 0
                            consecutive_ssh_failures = 0
                            last_activity_at = now
                            logger.info(
                                "[comfy] job %s… heartbeat OK — GPU active on %s",
                                prompt_id[:8], host,
                            )
                        elif gpu_idle is True:
                            consecutive_idle += 1
                            consecutive_ssh_failures = 0
                            logger.warning(
                                "[comfy] job %s… heartbeat IDLE %d/%d — GPU < %d%% on %s "
                                "(elapsed %.0fs)",
                                prompt_id[:8], consecutive_idle,
                                _HEARTBEAT_IDLE_SAMPLES, _HEARTBEAT_IDLE_THRESHOLD, host,
                                now - start,
                            )
                            if consecutive_idle >= _HEARTBEAT_IDLE_SAMPLES:
                                _interrupt_running_prompt(prompt_id, comfy_url)
                                raise StuckJobError(
                                    f"Job {prompt_id} on {comfy_url}: GPU idle for "
                                    f"{consecutive_idle} consecutive heartbeats "
                                    f"({_HEARTBEAT_INTERVAL}s apart, threshold {_HEARTBEAT_IDLE_THRESHOLD}%) "
                                    f"after {now - start:.0f}s — worker appears hung, will re-submit"
                                )
                        else:
                            # gpu_idle is None → SSH to worker failed
                            consecutive_ssh_failures += 1
                            if consecutive_ssh_failures >= _HEARTBEAT_SSH_FAIL_LIMIT:
                                logger.warning(
                                    "[comfy] job %s… heartbeat: SSH to %s failed %d consecutive "
                                    "times — GPU monitoring unavailable, relying on WS silence "
                                    "timeout (%ds)",
                                    prompt_id[:8], host, consecutive_ssh_failures, _STUCK_TIMEOUT,
                                )
                            else:
                                logger.debug(
                                    "[comfy] job %s… heartbeat: SSH to %s failed (%d/%d)",
                                    prompt_id[:8], host,
                                    consecutive_ssh_failures, _HEARTBEAT_SSH_FAIL_LIMIT,
                                )

                    # Stuck detection: fires when WebSocket has been silent too long.
                    # GPU heartbeats above reset last_activity_at when the GPU is busy,
                    # so this only triggers when the machine is genuinely idle.
                    baseline = last_activity_at if last_activity_at is not None else start
                    stalled = now - baseline
                    if stalled > _STUCK_TIMEOUT:
                        _cleanup_prompt(prompt_id, comfy_url, reason="node activity timeout")
                        raise StuckJobError(
                            f"Job {prompt_id} on {comfy_url} has been running but produced no"
                            f" node activity for {stalled:.0f}s (limit {_STUCK_TIMEOUT}s)"
                            f" — will re-submit"
                        )

            # ── WebSocket receive ──────────────────────────────────────────
            ws.settimeout(30)
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketConnectionClosedException, OSError):
                break  # fall back to history polling

            mtype = msg.get("type")
            data  = msg.get("data", {})

            if data.get("prompt_id") != prompt_id:
                continue  # message for a different job

            if mtype == "progress":
                last_activity_at = time.time()
                nodes_done = data.get("value", nodes_done)
            elif mtype == "executing":
                node = data.get("node")
                if node is None:
                    return   # execution finished
                last_activity_at = time.time()
            elif mtype == "execution_error":
                info = data if isinstance(data, dict) else {}
                detail = info.get("exception_message") or info.get("exception_type") or info
                where = info.get("node_type") or info.get("node_id") or "?"
                raise RuntimeError(f"ComfyUI execution error at {where}: {detail}")

    finally:
        try:
            ws.close()
        except Exception:
            pass

    try:
        _poll_completion(prompt_id, deadline, comfy_url=comfy_url)
    except Exception:
        _cleanup_prompt(prompt_id, comfy_url, reason="completion timeout")
        raise


def _get_outputs(prompt_id: str, comfy_url: str = COMFYUI_URL) -> list[dict]:
    """Return output file info from history."""
    url = f"{comfy_url}/history/{prompt_id}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        history = json.loads(resp.read())

    outputs = []
    for node_outputs in history.get(prompt_id, {}).get("outputs", {}).values():
        for key in ("videos", "images", "audio"):
            for item in node_outputs.get(key, []):
                outputs.append(item)
    return outputs


def _download_output(item: dict, dest: Path, comfy_url: str = COMFYUI_URL) -> Path:
    params = f"filename={item['filename']}&subfolder={item.get('subfolder', '')}&type={item.get('type', 'output')}"
    url = f"{comfy_url}/view?{params}"
    with urllib.request.urlopen(url, timeout=120) as resp:
        dest.write_bytes(resp.read())
    return dest


def _upload_image(image_path: Path, comfy_url: str = COMFYUI_URL) -> str:
    """Upload an image to ComfyUI input. Returns the filename ComfyUI assigned."""
    data = image_path.read_bytes()
    filename = image_path.name
    body = (
        f"--{_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + data + f"\r\n--{_BOUNDARY}--\r\n".encode()

    req = urllib.request.Request(
        f"{comfy_url}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result["name"]


def _apply_second_pass(workflow: dict, cfg: float, steps: int) -> None:
    """Patch second-pass CFG and sigma schedule in-place (T2V and I2V share node IDs 25/28)."""
    workflow["25"]["inputs"]["cfg"] = float(cfg)
    sigmas = _SECOND_PASS_SIGMAS.get(int(steps), _SECOND_PASS_SIGMAS[6])
    workflow["28"]["inputs"]["sigmas"] = sigmas


def generate_video_clip(
    positive_prompt: str,
    negative_prompt: str,
    output_path: Path,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    length: int = DEFAULT_LENGTH,
    seed: int | None = None,
    duration_seconds: float | None = None,
    lora_strength: float = 0.5,
    first_pass_cfg: float = 1.0,
    first_pass_steps: int = 8,
    second_pass_cfg: float = 1.0,
    second_pass_steps: int = 6,
    comfy_url: str = COMFYUI_URL,
) -> Path:
    """Generate a video clip using LTX 2.3 T2V and save to output_path."""
    length = _frame_count(length, duration_seconds)

    if seed is None:
        seed = random.randint(0, 2**32 - 1)

    half_w = width // 2
    half_h = height // 2

    workflow = _load_workflow("ltx23_t2v.json")
    workflow = _fill_template(workflow, {
        "POSITIVE_PROMPT":   positive_prompt,
        "NEGATIVE_PROMPT":   negative_prompt,
        "WIDTH":             width,
        "HEIGHT":            height,
        "HALF_WIDTH":        half_w,
        "HALF_HEIGHT":       half_h,
        "LENGTH":            length,
        "SEED":              seed,
        "FIRST_PASS_CFG":    first_pass_cfg,
        "FIRST_PASS_SIGMAS": _gen_first_pass_sigmas(first_pass_steps),
    })
    workflow["3"]["inputs"]["strength_model"] = lora_strength
    _apply_second_pass(workflow, second_pass_cfg, second_pass_steps)

    _video_timeout = _video_timeout_seconds(width, height, length)
    logger.info(
        "[comfy] generate_video_clip %dx%d length=%d timeout=%ds",
        width, height, length, _video_timeout,
    )

    client_id = str(uuid.uuid4())
    prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
    _wait_for_completion(prompt_id, client_id, timeout=_video_timeout, comfy_url=comfy_url)

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No output files from ComfyUI for prompt {prompt_id} ({comfy_url})")

    video_item = next((o for o in outputs if o.get("type") == "output"), outputs[0])
    return _download_output(video_item, output_path, comfy_url=comfy_url)


def generate_video_continuation(
    positive_prompt: str,
    negative_prompt: str,
    last_frame_path: Path,
    output_path: Path,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    length: int = DEFAULT_LENGTH,
    seed: int | None = None,
    duration_seconds: float | None = None,
    lora_strength: float = 0.5,
    first_pass_cfg: float = 1.0,
    first_pass_steps: int = 8,
    second_pass_cfg: float = 1.0,
    second_pass_steps: int = 6,
    comfy_url: str = COMFYUI_URL,
) -> Path:
    """Continue a video clip from its last frame using LTX 2.3 I2V."""
    length = _frame_count(length, duration_seconds)

    if seed is None:
        seed = random.randint(0, 2**32 - 1)

    half_w = width // 2
    half_h = height // 2

    image_name = _upload_image(last_frame_path, comfy_url=comfy_url)

    workflow = _load_workflow("ltx23_i2v.json")
    workflow = _fill_template(workflow, {
        "POSITIVE_PROMPT":   positive_prompt,
        "NEGATIVE_PROMPT":   negative_prompt,
        "WIDTH":             width,
        "HEIGHT":            height,
        "HALF_WIDTH":        half_w,
        "HALF_HEIGHT":       half_h,
        "LENGTH":            length,
        "SEED":              seed,
        "IMAGE_NAME":        image_name,
        "FIRST_PASS_CFG":    first_pass_cfg,
        "FIRST_PASS_SIGMAS": _gen_first_pass_sigmas(first_pass_steps),
    })
    workflow["3"]["inputs"]["strength_model"] = lora_strength
    _apply_second_pass(workflow, second_pass_cfg, second_pass_steps)

    _video_timeout = _video_timeout_seconds(width, height, length)
    logger.info(
        "[comfy] generate_video_continuation %dx%d length=%d timeout=%ds",
        width, height, length, _video_timeout,
    )

    client_id = str(uuid.uuid4())
    prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
    _wait_for_completion(prompt_id, client_id, timeout=_video_timeout, comfy_url=comfy_url)

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No output files from ComfyUI for I2V prompt {prompt_id} ({comfy_url})")

    video_item = next((o for o in outputs if o.get("type") == "output"), outputs[0])
    return _download_output(video_item, output_path, comfy_url=comfy_url)


def generate_scene_image(
    positive_prompt: str,
    output_path: Path,
    width: int = 1024,
    height: int = 576,
    seed: int | None = None,
    steps: int = 4,
    flux_model: str = "flux1-schnell-fp8.safetensors",
    clip_t5: str = "t5xxl_fp8_e4m3fn.safetensors",
    clip_l: str = "clip_l.safetensors",
    flux_vae: str = "ae.safetensors",
    comfy_url: str = COMFYUI_URL,
) -> Path:
    """Generate a high-detail scene preview image using FLUX.1-schnell.

    FLUX produces far better still images than LTX (a video model) and runs in
    4 steps, making it much faster for preview generation.
    """
    if seed is None:
        seed = random.randint(0, 2**32 - 1)

    workflow = _load_workflow("flux_t2i.json")
    # fp8 weights require CUDA; on MPS/CPU use "default" (needs a non-fp8 model).
    weight_dtype = "fp8_e4m3fn" if _comfy_has_cuda(comfy_url) else "default"

    workflow = _fill_template(workflow, {
        "FLUX_MODEL":      flux_model,
        "CLIP_T5":         clip_t5,
        "CLIP_L":          clip_l,
        "FLUX_VAE":        flux_vae,
        "WEIGHT_DTYPE":    weight_dtype,
        "POSITIVE_PROMPT": positive_prompt,
        "WIDTH":           width,
        "HEIGHT":          height,
        "STEPS":           steps,
        "SEED":            seed,
    })

    client_id = str(uuid.uuid4())
    prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
    _wait_for_completion(prompt_id, client_id, timeout=600, comfy_url=comfy_url)

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No image output from FLUX for prompt {prompt_id} ({comfy_url})")

    img_item = outputs[0]
    suffix = Path(img_item.get("filename", "preview.png")).suffix or ".png"
    tmp = output_path.with_suffix(suffix)
    _download_output(img_item, tmp, comfy_url=comfy_url)
    if tmp != output_path:
        tmp.rename(output_path)
    return output_path


def inpaint_scene_image(
    positive_prompt: str,
    base_image: Path,
    mask_image: Path,
    output_path: Path,
    *,
    seed: int | None = None,
    steps: int = 4,
    denoise: float = 0.85,
    flux_model: str = "flux1-schnell-fp8.safetensors",
    clip_t5: str = "t5xxl_fp8_e4m3fn.safetensors",
    clip_l: str = "clip_l.safetensors",
    flux_vae: str = "ae.safetensors",
    comfy_url: str = COMFYUI_URL,
) -> Path:
    """Edit a region of *base_image* using FLUX masked img2img and save to *output_path*.

    *mask_image* is a black/white PNG (white = the region to change). The masked
    region is regenerated from *positive_prompt* while the rest of the image is
    preserved; *denoise* controls how much the masked region is allowed to change
    (lower keeps more of the original, 1.0 fully repaints it).
    """
    if seed is None:
        seed = random.randint(0, 2**32 - 1)

    workflow = _load_workflow("flux_inpaint.json")
    # fp8 weights require CUDA; on MPS/CPU use "default" (needs a non-fp8 model).
    weight_dtype = "fp8_e4m3fn" if _comfy_has_cuda(comfy_url) else "default"

    base_name = _upload_image(Path(base_image), comfy_url=comfy_url)
    mask_name = _upload_image(Path(mask_image), comfy_url=comfy_url)

    workflow = _fill_template(workflow, {
        "FLUX_MODEL":      flux_model,
        "CLIP_T5":         clip_t5,
        "CLIP_L":          clip_l,
        "FLUX_VAE":        flux_vae,
        "WEIGHT_DTYPE":    weight_dtype,
        "POSITIVE_PROMPT": positive_prompt,
        "BASE_IMAGE":      base_name,
        "MASK_IMAGE":      mask_name,
        "STEPS":           steps,
        "DENOISE":         denoise,
        "SEED":            seed,
    })

    client_id = str(uuid.uuid4())
    prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
    _wait_for_completion(prompt_id, client_id, timeout=600, comfy_url=comfy_url)

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No image output from FLUX inpaint for prompt {prompt_id} ({comfy_url})")

    img_item = outputs[0]
    suffix = Path(img_item.get("filename", "preview.png")).suffix or ".png"
    tmp = output_path.with_suffix(suffix)
    _download_output(img_item, tmp, comfy_url=comfy_url)
    if tmp != output_path:
        tmp.rename(output_path)
    return output_path


def _run_and_save(workflow: dict, output_path: Path, comfy_url: str) -> Path:
    """Queue a prepared workflow, wait, and save its first image output."""
    client_id = str(uuid.uuid4())
    prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
    _wait_for_completion(prompt_id, client_id, timeout=600, comfy_url=comfy_url)
    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No image output from ComfyUI for prompt {prompt_id} ({comfy_url})")
    item = outputs[0]
    suffix = Path(item.get("filename", "out.png")).suffix or ".png"
    tmp = output_path.with_suffix(suffix)
    _download_output(item, tmp, comfy_url=comfy_url)
    if tmp != output_path:
        tmp.rename(output_path)
    return output_path


def _weight_dtype(engine: dict, comfy_url: str) -> str:
    """Pick the UNETLoader weight_dtype. An engine may override it (e.g. a bf16
    model cast to fp8 to dodge a failing bf16 GEMM path on some GPUs); otherwise
    FLUX.2 fp8-mixed weights load as-is and FLUX.1 fp8 needs CUDA."""
    has_cuda = _comfy_has_cuda(comfy_url)
    override = engine.get("weight_dtype")
    if override:
        return override if has_cuda else "default"
    if engine.get("family") == "flux2":
        return "default"
    return "fp8_e4m3fn" if has_cuda else "default"


def _build_flux2_ref_workflow(engine: dict, repl: dict, ref_names: list[str]) -> dict:
    """FLUX.2 reference-conditioned t2i workflow for one or more reference images.

    Starts from ``t2i_ref_workflow`` (a single-reference graph: nodes 20 LoadImage
    → 21 VAEEncode → 22 ReferenceLatent feeding BasicGuider node 8) and chains an
    extra LoadImage/VAEEncode/ReferenceLatent triple per additional reference, so
    several characters can condition the same image. The last ReferenceLatent is
    wired into the guider."""
    wf = _fill_template(_load_workflow(engine["t2i_ref_workflow"]),
                        {**repl, "REF_IMAGE": ref_names[0]})
    last_ref, nid = "22", 23
    for name in ref_names[1:]:
        load, enc, ref = str(nid), str(nid + 1), str(nid + 2)
        wf[load] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf[enc] = {"class_type": "VAEEncode", "inputs": {"pixels": [load, 0], "vae": ["3", 0]}}
        wf[ref] = {"class_type": "ReferenceLatent",
                   "inputs": {"conditioning": [last_ref, 0], "latent": [enc, 0]}}
        last_ref, nid = ref, nid + 3
    wf["8"]["inputs"]["conditioning"] = [last_ref, 0]
    return wf


def generate_with_engine(engine: dict, prompt: str, output_path: Path, *,
                         width: int, height: int, seed: int | None = None,
                         reference_images: list[Path] | None = None,
                         comfy_url: str = COMFYUI_URL) -> Path:
    """Text→image for the given engine. Dispatches by engine family.

    flux1 reuses :func:`generate_scene_image`; flux2 runs ``flux2_t2i.json``, or
    ``flux2_t2i_ref.json`` when *reference_images* are given (character reference
    conditioning — FLUX.2 only; ignored by flux1)."""
    if not engine.get("can_generate"):
        raise RuntimeError(f"Engine {engine.get('key')!r} cannot generate images.")
    refs = [Path(p) for p in (reference_images or []) if p]
    if engine.get("family") != "flux2":
        if refs:
            logger.debug("Engine %r has no reference conditioning — ignoring %d reference image(s).",
                         engine.get("key"), len(refs))
        return generate_scene_image(
            prompt, output_path, width=width, height=height, seed=seed,
            steps=int(engine["steps"]), flux_model=engine["model_file"],
            clip_t5=engine["clip_t5"], clip_l=engine["clip_l"], flux_vae=engine["vae"],
            comfy_url=comfy_url)
    if seed is None:
        seed = random.randint(0, 2**32 - 1)
    repl = {
        "FLUX_MODEL": engine["model_file"], "CLIP_T5": engine["clip_t5"],
        "FLUX_VAE": engine["vae"], "WEIGHT_DTYPE": _weight_dtype(engine, comfy_url),
        "POSITIVE_PROMPT": prompt, "WIDTH": width, "HEIGHT": height,
        "STEPS": int(engine["steps"]), "GUIDANCE": float(engine.get("guidance") or 4.0),
        "SEED": seed,
    }
    if refs and engine.get("t2i_ref_workflow"):
        ref_names = [_upload_image(p, comfy_url=comfy_url) for p in refs]
        workflow = _build_flux2_ref_workflow(engine, repl, ref_names)
    else:
        workflow = _fill_template(_load_workflow(engine["t2i_workflow"]), repl)
    return _run_and_save(workflow, output_path, comfy_url)


def edit_with_engine(engine: dict, prompt: str, base_image: Path, mask_image: Path,
                     output_path: Path, *, denoise: float | None = None,
                     seed: int | None = None, comfy_url: str = COMFYUI_URL) -> Path:
    """Masked image edit for the given engine, dispatched by ``edit_mode``.

    noise_mask → :func:`inpaint_scene_image` (schnell); flux2 → ``flux2_edit.json``."""
    if not engine.get("can_edit"):
        raise RuntimeError(f"Engine {engine.get('key')!r} cannot edit images.")
    mode = engine.get("edit_mode")
    dn = denoise if denoise is not None else float(engine.get("edit_denoise") or 0.85)
    if mode == "noise_mask":
        return inpaint_scene_image(
            prompt, base_image, mask_image, output_path, seed=seed,
            steps=int(engine["steps"]), denoise=dn, flux_model=engine["model_file"],
            clip_t5=engine["clip_t5"], clip_l=engine["clip_l"], flux_vae=engine["vae"],
            comfy_url=comfy_url)

    # flux2 — Flux2Scheduler needs the image's pixel dimensions
    if seed is None:
        seed = random.randint(0, 2**32 - 1)
    base_name = _upload_image(Path(base_image), comfy_url=comfy_url)
    mask_name = _upload_image(Path(mask_image), comfy_url=comfy_url)
    repl = {
        "WEIGHT_DTYPE": _weight_dtype(engine, comfy_url),
        "FLUX_VAE": engine["vae"], "CLIP_T5": engine["clip_t5"],
        "POSITIVE_PROMPT": prompt, "BASE_IMAGE": base_name, "MASK_IMAGE": mask_name,
        "STEPS": int(engine["steps"]), "DENOISE": dn,
        "GUIDANCE": float(engine.get("guidance") or 30.0), "SEED": seed,
        "FLUX_MODEL": engine["model_file"],
    }
    try:
        from PIL import Image
        with Image.open(base_image) as im:
            repl["WIDTH"], repl["HEIGHT"] = im.size
    except Exception:
        repl["WIDTH"], repl["HEIGHT"] = 1024, 1024
    workflow = _fill_template(_load_workflow(engine["edit_workflow"]), repl)
    return _run_and_save(workflow, output_path, comfy_url)


def engine_model_present(comfy_url: str, probe) -> bool | None:
    """True/False if the engine's model file is loadable on this worker, or None
    if the probe couldn't be evaluated. *probe* is (node_class, input_name, filename);
    we read ComfyUI's /object_info to see if the filename is in that loader's list."""
    if not probe:
        return None
    node_class, input_name, filename = probe
    try:
        with urllib.request.urlopen(f"{comfy_url}/object_info/{node_class}", timeout=8) as r:
            data = json.loads(r.read())
        info = data.get(node_class) or {}
        inputs = info.get("input") or {}
        spec = (inputs.get("required") or {}).get(input_name) or (inputs.get("optional") or {}).get(input_name)
        names = spec[0] if isinstance(spec, list) and spec and isinstance(spec[0], list) else []
        return filename in names
    except Exception:
        return None


def generate_music(
    topic: str,
    duration_seconds: float,
    output_path: Path,
    tags: str | None = None,
    seed: int | None = None,
    comfy_url: str = COMFYUI_URL,
) -> Path:
    """Generate background music using ACE-Step 1.5 and save to output_path."""
    if seed is None:
        seed = random.randint(0, 2**32 - 1)

    if tags is None:
        tags = (
            f"ambient documentary background music, {topic}, "
            "cinematic, atmospheric, instrumental, orchestral, peaceful"
        )

    workflow = _load_workflow("ace_music.json")
    workflow = _fill_template(workflow, {
        "TAGS":     tags,
        "DURATION": round(duration_seconds, 1),
        "SEED":     seed,
    })

    client_id = str(uuid.uuid4())
    prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
    _wait_for_completion(prompt_id, client_id, timeout=600, comfy_url=comfy_url)

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No audio output from ComfyUI for prompt {prompt_id} ({comfy_url})")

    audio_item = outputs[0]
    return _download_output(audio_item, output_path, comfy_url=comfy_url)
