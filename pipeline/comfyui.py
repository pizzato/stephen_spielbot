"""ComfyUI API client for video and music generation."""

import json
import logging
import random
import shutil
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
            f"Start the worker containers with:  make start W=<host>\n"
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
_QUEUE_ABSENT_GRACE        = 90    # seconds to wait for /history after a job leaves /queue
_COMFY_PROMPT_ATTEMPTS     = 3     # initial submit + bounded retries for dropped/stuck prompts


def _final_retryable_message(exc: Exception) -> str:
    return str(exc).replace(" — will re-submit", "").replace(" — worker appears hung, will re-submit", "")


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
    heartbeat_warmup: int = _HEARTBEAT_INITIAL_DELAY,
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
    # Engines with much larger cold loads (H3's ~40 GB stack) pass a longer warmup.
    last_heartbeat_at = start - _HEARTBEAT_INTERVAL + heartbeat_warmup
    consecutive_idle  = 0
    consecutive_ssh_failures = 0
    host              = _hostname_from_url(comfy_url)
    queue_absent_since: float | None = None

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
                    if queue_absent_since is None:
                        queue_absent_since = now
                        logger.warning(
                            "[comfy] job %s… left queue on %s; waiting for history",
                            prompt_id[:8], comfy_url,
                        )
                        continue
                    if now - queue_absent_since < _QUEUE_ABSENT_GRACE:
                        continue
                    raise DroppedJobError(
                        f"Job {prompt_id} vanished from queue on {comfy_url} without completing"
                    )
                queue_absent_since = None

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
        for key in ("videos", "images", "audio", "gifs"):
            for item in node_outputs.get(key, []):
                outputs.append(item)
    return outputs


def _download_output(item: dict, dest: Path, comfy_url: str = COMFYUI_URL) -> Path:
    params = f"filename={item['filename']}&subfolder={item.get('subfolder', '')}&type={item.get('type', 'output')}"
    url = f"{comfy_url}/view?{params}"
    with urllib.request.urlopen(url, timeout=120) as resp:
        dest.write_bytes(resp.read())
    return dest


def _upload_input_file(path: Path, field_name: str = "image", content_type: str = "application/octet-stream",
                       comfy_url: str = COMFYUI_URL) -> str:
    """Upload a file to ComfyUI's input folder and return its assigned name."""
    data = Path(path).read_bytes()
    filename = Path(path).name
    body = (
        f"--{_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{_BOUNDARY}--\r\n".encode()

    req = urllib.request.Request(
        f"{comfy_url}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())
    return result["name"]


def _upload_image(image_path: Path, comfy_url: str = COMFYUI_URL) -> str:
    """Upload an image to ComfyUI input. Returns the filename ComfyUI assigned."""
    return _upload_input_file(image_path, content_type="image/jpeg", comfy_url=comfy_url)


def _upload_video(video_path: Path, comfy_url: str = COMFYUI_URL) -> str:
    """Upload a video to ComfyUI input. Returns the filename ComfyUI assigned."""
    return _upload_input_file(video_path, content_type="video/mp4", comfy_url=comfy_url)


def _is_local_host(host: str) -> bool:
    return host in {"", "localhost", "127.0.0.1", "::1"}


def _safe_stage_name(path: Path) -> str:
    suffix = path.suffix if path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"} else ".mp4"
    return f"spielbot-upscale-{uuid.uuid4().hex[:12]}{suffix}"


def _run_stage_cmd(cmd: list[str], what: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{what} failed: {detail[-1000:]}")


def _stage_video_for_load(video_path: Path, comfy_url: str = COMFYUI_URL) -> str:
    """Place a large video in ComfyUI's input folder without using HTTP upload.

    ComfyUI's upload endpoint can reject finished films with 413 Request Entity
    Too Large. The worker stack is SSH/Docker-managed already, so stage the MP4
    into /opt/ComfyUI/input and let the Video Helper Suite loader read it by
    filename. Falls back to a native ComfyUI input folder when no container is
    available.
    """
    video_path = Path(video_path)
    name = _safe_stage_name(video_path)
    host = _hostname_from_url(comfy_url)
    container_dest = f"spielbot-worker-comfyui-1:/opt/ComfyUI/input/{name}"

    if _is_local_host(host):
        try:
            _run_stage_cmd(["docker", "cp", str(video_path), container_dest], "docker cp to local ComfyUI")
            return name
        except Exception as exc:
            logger.info("[comfy] local docker cp unavailable, using native input folder: %s", exc)
        native_input = Path.home() / "github" / "ComfyUI" / "input"
        native_input.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video_path, native_input / name)
        return name

    if host.startswith("-"):
        raise RuntimeError(f"Refusing to stage video to unsafe worker host: {host!r}")

    remote_tmp_dir = f"/tmp/spielbot-comfy-input-{uuid.uuid4().hex[:8]}"
    remote_tmp = f"{remote_tmp_dir}/{name}"
    try:
        _run_stage_cmd(["ssh", "--", host, "mkdir", "-p", remote_tmp_dir], "create remote staging dir")
        _run_stage_cmd(["rsync", "-az", str(video_path), f"{host}:{remote_tmp}"], "copy video to worker")
        try:
            _run_stage_cmd(["ssh", "--", host, "docker", "cp", remote_tmp, container_dest], "docker cp to worker ComfyUI")
            return name
        except Exception as exc:
            logger.info("[comfy] remote docker cp unavailable, using native input folder on %s: %s", host, exc)
        _run_stage_cmd(["ssh", "--", host, "mkdir", "-p", "$HOME/github/ComfyUI/input"], "create remote ComfyUI input")
        _run_stage_cmd(["ssh", "--", host, "cp", remote_tmp, f"$HOME/github/ComfyUI/input/{name}"], "stage video in remote input")
        return name
    finally:
        try:
            subprocess.run(["ssh", "--", host, "rm", "-rf", remote_tmp_dir], capture_output=True, text=True, timeout=60)
        except Exception:
            pass


def _apply_second_pass(workflow: dict, cfg: float, steps: int) -> None:
    """Patch second-pass CFG and sigma schedule in-place (T2V and I2V share node IDs 25/28)."""
    workflow["25"]["inputs"]["cfg"] = float(cfg)
    sigmas = _SECOND_PASS_SIGMAS.get(int(steps), _SECOND_PASS_SIGMAS[6])
    workflow["28"]["inputs"]["sigmas"] = sigmas


# LTX 2.3 generates each clip's audio from the same text prompt. Steer that audio
# toward natural diegetic sound (the real sounds of what's happening in the scene)
# and away from the music / dramatic score LTX otherwise invents — films are
# scored separately (the user adds music where wanted). Applied to every LTX video.
_AUDIO_POSITIVE = ("natural realistic diegetic sound of the scene — the actual ambient and "
                   "environmental audio of what is happening, real room tone")
_AUDIO_NEGATIVE = ("music, musical score, soundtrack, background music, dramatic orchestral score, "
                   "ominous drone, suspenseful underscore, singing, song")


def _steer_audio_natural(positive: str, negative: str) -> tuple[str, str]:
    """Append the natural-sound directive to an LTX prompt pair so the generated
    clip carries the scene's real sounds instead of invented music."""
    p = (positive or "").rstrip().rstrip(".")
    pos = f"{p}. {_AUDIO_POSITIVE}." if p else f"{_AUDIO_POSITIVE}."
    n = (negative or "").rstrip().rstrip(",")
    neg = f"{n}, {_AUDIO_NEGATIVE}" if n else _AUDIO_NEGATIVE
    return pos, neg


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
    positive_prompt, negative_prompt = _steer_audio_natural(positive_prompt, negative_prompt)

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


def generate_keyframed_clip(
    first_frame_path: Path,
    last_frame_path: Path,
    output_path: Path,
    positive_prompt: str,
    negative_prompt: str = "",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    length: int = DEFAULT_LENGTH,
    seed: int | None = None,
    duration_seconds: float | None = None,
    lora_strength: float = 0.5,
    cfg: float = 1.0,
    steps: int = 8,
    comfy_url: str = COMFYUI_URL,
) -> Path:
    """LTX 2.3 clip keyframed between a FIRST and LAST frame — the camera really
    moves from one to the other (a generated push-in), not a fake 2D zoom.

    Single-pass, silent (no audio track). Used for a dialogue scene's establishing
    shot: first = the wide setting, last = the speaker's close-up, so the push-in
    lands exactly on the still the talking head then animates."""
    length = _frame_count(length, duration_seconds)
    if seed is None:
        seed = random.randint(0, 2**32 - 1)

    first_name = _upload_image(first_frame_path, comfy_url=comfy_url)
    last_name = _upload_image(last_frame_path, comfy_url=comfy_url)

    workflow = _load_workflow("ltx23_i2v_keyframes.json")
    workflow = _fill_template(workflow, {
        "POSITIVE_PROMPT": positive_prompt,
        "NEGATIVE_PROMPT": negative_prompt,
        "WIDTH":  width,
        "HEIGHT": height,
        "LENGTH": length,
        "SEED":   seed,
        "FIRST_IMAGE": first_name,
        "LAST_IMAGE":  last_name,
        "CFG":    cfg,
        "SIGMAS": _gen_first_pass_sigmas(steps),
    })
    workflow["3"]["inputs"]["strength_model"] = lora_strength

    _video_timeout = _video_timeout_seconds(width, height, length)
    logger.info("[comfy] generate_keyframed_clip %dx%d length=%d timeout=%ds",
                width, height, length, _video_timeout)

    client_id = str(uuid.uuid4())
    prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
    _wait_for_completion(prompt_id, client_id, timeout=_video_timeout, comfy_url=comfy_url)

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No output from ComfyUI for keyframe prompt {prompt_id} ({comfy_url})")
    video_item = next((o for o in outputs if o.get("type") == "output"), outputs[0])
    return _download_output(video_item, output_path, comfy_url=comfy_url)


# ── MiniMax H3 (Hailuo 3.0) ──────────────────────────────────────────────────
# 24 fps joint audio+video DiT. The graph is CFG-free (BasicGuider — there is no
# negative-prompt path) and frame counts must satisfy n % 17 == 5 (temporal
# latent packing). Native generation is capped at 768×1344 total pixels; larger
# style resolutions render at the capped size and the caller (scene_video)
# scales the clip back up so downstream files keep the plan dimensions.
H3_FPS         = 24
H3_MAX_PIXELS  = 768 * 1344
H3_MIN_SECONDS = 4.0     # model's supported clip range is ~4–15 s
H3_MAX_SECONDS = 15.0
# Cold-loading the ~40 GB H3 stack (unet + Qwen3-VL 32B encoder + two VAEs)
# keeps the GPU near 0 % far longer than the standard 120 s heartbeat warm-up.
_H3_HEARTBEAT_WARMUP = 600


def h3_frame_count(duration_seconds: float | None) -> int:
    """Frames for an H3 clip: clamp seconds to the model's range, then round
    up to the next frame count satisfying n % 17 == 5 (the graph rejects others)."""
    secs = H3_MIN_SECONDS if duration_seconds is None else duration_seconds
    secs = min(max(secs, H3_MIN_SECONDS), H3_MAX_SECONDS)
    n = max(5, round(secs * H3_FPS))
    return n + (5 - n % 17) % 17


def h3_dimensions(width: int, height: int) -> tuple[int, int]:
    """Snap to H3's grid: multiples of 32, total pixels ≤ H3_MAX_PIXELS.
    Oversized requests keep their aspect ratio and scale down to the cap."""
    scale = min(1.0, (H3_MAX_PIXELS / float(width * height)) ** 0.5)
    w = max(32, int(width * scale) // 32 * 32)
    h = max(32, int(height * scale) // 32 * 32)
    return w, h


def _h3_timeout_seconds(width: int, height: int, length: int) -> int:
    # H3 is far heavier than LTX: budget an hour for a ~5 s 864×480 clip and
    # scale by pixels × frames. The GPU heartbeat catches hung workers earlier;
    # this is only the give-up bound.
    base_px, base_frames = 864 * 480, 124
    return max(3600, int(3600 * (width * height) / base_px * (length / base_frames)))


def generate_video_h3(
    engine: dict,
    positive_prompt: str,
    first_frame_path: Path,
    output_path: Path,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    seed: int | None = None,
    duration_seconds: float | None = None,
    steps: int | None = None,
    comfy_url: str = COMFYUI_URL,
) -> Path:
    """MiniMax H3 I2V: single CFG-free pass, stereo audio muxed into the mp4.

    *engine* is a video-engine dict from :mod:`pipeline.engines` (family
    "minimax"); it names the workflow and the four model files so quantisation
    variants can swap without touching the graph. The clip may come back
    smaller than requested (H3 pixel cap) — callers re-scale.
    """
    gen_w, gen_h = h3_dimensions(width, height)
    length = h3_frame_count(duration_seconds)
    steps = int(steps or engine.get("steps") or 20)
    # H3 models audio jointly with video; only positive steering exists.
    prompt, _ = _steer_audio_natural(positive_prompt, "")

    if seed is None:
        seed = random.randint(0, 2**32 - 1)

    image_name = _upload_image(first_frame_path, comfy_url=comfy_url)

    workflow = _fill_template(_load_workflow(engine.get("workflow", "h3_i2v.json")), {
        "UNET_NAME":       engine["unet"],
        "CLIP_NAME":       engine["clip"],
        "VIDEO_VAE":       engine["video_vae"],
        "AUDIO_VAE":       engine["audio_vae"],
        "POSITIVE_PROMPT": prompt,
        "WIDTH":           gen_w,
        "HEIGHT":          gen_h,
        "LENGTH":          length,
        "SEED":            seed,
        "STEPS":           steps,
        "IMAGE_NAME":      image_name,
        "EASYCACHE_THRESHOLD": float(engine.get("easycache_threshold") or 0.2),
        "LORA_NAME":       engine.get("lora") or "",
    })

    _video_timeout = _h3_timeout_seconds(gen_w, gen_h, length)
    logger.info(
        "[comfy] generate_video_h3 %dx%d (requested %dx%d) length=%d steps=%d timeout=%ds",
        gen_w, gen_h, width, height, length, steps, _video_timeout,
    )

    client_id = str(uuid.uuid4())
    prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
    _wait_for_completion(prompt_id, client_id, timeout=_video_timeout, comfy_url=comfy_url,
                         heartbeat_warmup=_H3_HEARTBEAT_WARMUP)

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No output files from ComfyUI for H3 prompt {prompt_id} ({comfy_url})")
    video_item = next((o for o in outputs if o.get("type") == "output"), outputs[0])
    return _download_output(video_item, output_path, comfy_url=comfy_url)


# H3 Ref2VA reference limits (comfy_extras/nodes_minimax_h3.py).
H3_MAX_REF_IMAGES = 9
H3_MAX_REF_AUDIOS = 3


def _upload_audio(audio_path: Path, comfy_url: str = COMFYUI_URL) -> str:
    """Upload a wav to ComfyUI input (LoadAudio reads it by name)."""
    return _upload_input_file(audio_path, content_type="audio/wav", comfy_url=comfy_url)


def _ref_node_id(workflow: dict) -> str:
    for node_id, node in workflow.items():
        if node.get("class_type") == "MiniMaxH3ReferenceToVideo":
            return node_id
    raise RuntimeError("Ref2VA workflow has no MiniMaxH3ReferenceToVideo node")


def generate_video_h3_ref(
    engine: dict,
    positive_prompt: str,
    ref_images: list[Path],
    output_path: Path,
    ref_audios: list[Path] | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    seed: int | None = None,
    duration_seconds: float | None = None,
    steps: int | None = None,
    comfy_url: str = COMFYUI_URL,
) -> Path:
    """MiniMax H3 Ref2VA: character portraits (and optional voice clips) → a clip
    with its own spoken dialogue. No first frame and no TTS — the model writes
    picture and audio in one pass.

    *ref_images* are character portraits, cited in the prompt as ``<Picture N>``
    in the SAME order; *ref_audios* are voice references cited as ``<Audio N>``.
    Both are capped at the model's limits. H3 requires at least one image or
    video reference — audio alone is rejected.
    """
    if not ref_images:
        raise ValueError("Ref2VA needs at least one reference image")

    gen_w, gen_h = h3_dimensions(width, height)
    length = h3_frame_count(duration_seconds)
    steps = int(steps or engine.get("steps") or 20)
    # H3 models audio jointly with video; only positive steering exists.
    prompt, _ = _steer_audio_natural(positive_prompt, "")

    if seed is None:
        seed = random.randint(0, 2**32 - 1)

    images = list(ref_images)[:H3_MAX_REF_IMAGES]
    audios = list(ref_audios or [])[:H3_MAX_REF_AUDIOS]
    if len(ref_images) > H3_MAX_REF_IMAGES or len(ref_audios or []) > H3_MAX_REF_AUDIOS:
        logger.warning("[comfy] Ref2VA: trimmed references to %d images / %d audios (model cap)",
                       len(images), len(audios))
    image_names = [_upload_image(p, comfy_url=comfy_url) for p in images]
    audio_names = [_upload_audio(p, comfy_url=comfy_url) for p in audios]

    workflow = _fill_template(_load_workflow(engine.get("workflow", "h3_ref2v.json")), {
        "UNET_NAME":       engine["unet"],
        "CLIP_NAME":       engine["clip"],
        "VIDEO_VAE":       engine["video_vae"],
        "AUDIO_VAE":       engine["audio_vae"],
        "POSITIVE_PROMPT": prompt,
        "WIDTH":           gen_w,
        "HEIGHT":          gen_h,
        "LENGTH":          length,
        "SEED":            seed,
        "STEPS":           steps,
        "EASYCACHE_THRESHOLD": float(engine.get("easycache_threshold") or 0.2),
        "LORA_NAME":       engine.get("lora") or "",
    })

    # Reference slots are autogrow inputs: the API key is the DOTTED group form
    # ("ref_images.ref_image_0"), which is what comfy_api's finalize_prefix()
    # looks up. A flat "ref_image_0" is silently ignored — the render succeeds
    # with no references at all — so this must not be "simplified".
    ref_node = workflow[_ref_node_id(workflow)]["inputs"]
    next_id = max((int(k) for k in workflow), default=100) + 1
    for i, name in enumerate(image_names):
        loader = str(next_id + i)
        workflow[loader] = {"class_type": "LoadImage", "inputs": {"image": name}}
        ref_node[f"ref_images.ref_image_{i}"] = [loader, 0]
    next_id += len(image_names)
    for i, name in enumerate(audio_names):
        loader = str(next_id + i)
        workflow[loader] = {"class_type": "LoadAudio", "inputs": {"audio": name}}
        ref_node[f"ref_audios.ref_audio_{i}"] = [loader, 0]

    _video_timeout = _h3_timeout_seconds(gen_w, gen_h, length)
    logger.info(
        "[comfy] generate_video_h3_ref %dx%d length=%d steps=%d refs=%di/%da timeout=%ds",
        gen_w, gen_h, length, steps, len(image_names), len(audio_names), _video_timeout,
    )

    client_id = str(uuid.uuid4())
    prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
    _wait_for_completion(prompt_id, client_id, timeout=_video_timeout, comfy_url=comfy_url,
                         heartbeat_warmup=_H3_HEARTBEAT_WARMUP)

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No output files from ComfyUI for H3 Ref2VA prompt {prompt_id} ({comfy_url})")
    video_item = next((o for o in outputs if o.get("type") == "output"), outputs[0])
    return _download_output(video_item, output_path, comfy_url=comfy_url)


def generate_video_with_engine(
    video_engine: dict | None,
    positive_prompt: str,
    negative_prompt: str,
    first_frame_path: Path,
    output_path: Path,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    seed: int | None = None,
    duration_seconds: float | None = None,
    lora_strength: float = 0.5,
    first_pass_cfg: float = 1.0,
    first_pass_steps: int = 8,
    second_pass_cfg: float = 1.0,
    second_pass_steps: int = 6,
    comfy_url: str = COMFYUI_URL,
) -> Path:
    """Route a scene's I2V render to the style's video engine.

    None or family "ltx" → the LTX 2.3 two-pass path, unchanged; family
    "minimax" → MiniMax H3, where the negative prompt and the LTX pass tuning
    knobs don't apply (H3 is CFG-free single-pass).
    """
    if video_engine and video_engine.get("family") == "minimax":
        return generate_video_h3(
            video_engine, positive_prompt, first_frame_path, output_path,
            width=width, height=height, seed=seed,
            duration_seconds=duration_seconds, comfy_url=comfy_url,
        )
    return generate_video_continuation(
        positive_prompt, negative_prompt, first_frame_path, output_path,
        width=width, height=height, seed=seed, duration_seconds=duration_seconds,
        lora_strength=lora_strength, first_pass_cfg=first_pass_cfg,
        first_pass_steps=first_pass_steps, second_pass_cfg=second_pass_cfg,
        second_pass_steps=second_pass_steps, comfy_url=comfy_url,
    )


# Lightricks IC-LoRA Pixel Spatial Upscaler LoRAs (models/loras/).
# https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler
_IC_LORA_X2 = "ltx-2.3-22b-ic-lora-pixel-spatial-upscaler-x2-0.9.safetensors"
_IC_LORA_X4 = "ltx-2.3-22b-ic-lora-pixel-spatial-upscaler-x4-0.9.safetensors"
_IC_UPSCALE_POSITIVE = (
    "high quality, sharp fine detail, crisp textures, clean edges, "
    "cinematic lighting, photorealistic, detailed"
)
_IC_UPSCALE_NEGATIVE = (
    "blurry, soft, low resolution, pixelated, blocky, compression artifacts, "
    "noisy, washed out, cartoon, ugly"
)


# LTX VAE spatial compression. EmptyLTXVLatentVideo of WxH → latent (W/32)×(H/32).
_LTX_VAE_SPATIAL = 32


def _snap_ltx_dim(value: int, multiple: int = 32, *, prefer: str = "round") -> int:
    """Snap a pixel dimension to *multiple*.

    prefer:
      - ``round`` — nearest (default)
      - ``up`` — ceil, never shrink below the requested size
      - ``down`` — floor
    """
    m = max(1, int(multiple))
    v = max(1, int(value))
    if prefer == "up":
        return max(m, ((v + m - 1) // m) * m)
    if prefer == "down":
        return max(m, (v // m) * m)
    return max(m, int(round(v / m) * m))


def _ic_lora_name_for_scale(src_w: int, src_h: int, dst_w: int, dst_h: int) -> str:
    """Pick 2× or 4× IC-LoRA from the largest side scale factor."""
    scale = max(float(dst_w) / max(1, src_w), float(dst_h) / max(1, src_h))
    return _IC_LORA_X4 if scale >= 3.0 else _IC_LORA_X2


def _ic_lora_downscale_factor(lora_name: str) -> int:
    """reference_downscale_factor baked into the IC-LoRA weights (2 or 4)."""
    name = (lora_name or "").lower()
    if "x4" in name or "-4x" in name:
        return 4
    return 2


def _ic_lora_pixel_grid(lora_name: str) -> int:
    """Pixel multiple so latent dims are divisible by the IC-LoRA downscale factor.

    LTXAddVideoICLoRAGuide requires ``(latent_w % factor == 0) and (latent_h % factor == 0)``
    before dilating the guide. With VAE spatial /32 that means pixels must be a
    multiple of ``32 * factor`` (64 for 2×, 128 for 4×). Without this, targets
    like 1920×1080 → 1920×1088 yield latent 60×34 and 34 % 4 fails on 4×.
    """
    return _LTX_VAE_SPATIAL * _ic_lora_downscale_factor(lora_name)


def upscale_video_ltx(
    input_path: Path,
    output_path: Path,
    width: int,
    height: int,
    fps: float,
    timeout_seconds: int = 7200,
    comfy_url: str = COMFYUI_URL,
    *,
    source_width: int | None = None,
    source_height: int | None = None,
    duration_seconds: float | None = None,
) -> Path:
    """Upscale an existing MP4 via LTX-2.3 IC-LoRA Pixel Spatial Upscaler.

    Uses Lightricks' generative IC-LoRA (2× or 4×) rather than the older
    LTXVLatentUpsampler latent upscaler. Requires ComfyUI-LTXVideo custom nodes
    and the IC-LoRA weights under models/loras/.
    """
    from pipeline.assembler import _get_duration, _get_video_dimensions

    src = Path(input_path)
    if source_width is None or source_height is None:
        source_width, source_height = _get_video_dimensions(src)
    if duration_seconds is None:
        duration_seconds = _get_duration(src)

    # Pick LoRA from the *requested* target (before grid snap) so a 4× jump
    # still selects the x4 weights even if we later pad height to 1152.
    requested_w, requested_h = int(width), int(height)
    ic_lora = _ic_lora_name_for_scale(
        int(source_width), int(source_height), requested_w, requested_h,
    )
    grid = _ic_lora_pixel_grid(ic_lora)
    # Snap up so we never undershoot the user's target; final normalize crops
    # / scales back to requested_w×requested_h.
    width = _snap_ltx_dim(requested_w, grid, prefer="up")
    height = _snap_ltx_dim(requested_h, grid, prefer="up")
    fps_val = max(1.0, float(fps or LTX_FPS))
    length = _frame_count(1, duration_seconds)

    video_name = _stage_video_for_load(src, comfy_url=comfy_url)
    workflow = _load_workflow("ltx23_ic_lora_pixel_upscale.json")
    workflow = _fill_template(workflow, {
        "VIDEO_NAME": video_name,
        "WIDTH": int(width),
        "HEIGHT": int(height),
        "LENGTH": int(length),
        "FPS": fps_val,
        "SEED": random.randint(0, 2**32 - 1),
        "IC_LORA_NAME": ic_lora,
        "POSITIVE_PROMPT": _IC_UPSCALE_POSITIVE,
        "NEGATIVE_PROMPT": _IC_UPSCALE_NEGATIVE,
    })

    logger.info(
        "[comfy] upscale_video_ltx IC-LoRA %s %s -> %dx%d (work %dx%d, grid %d, "
        "src %dx%d) fps=%.3f frames=%d timeout=%ds on %s",
        ic_lora, src.name, requested_w, requested_h, width, height, grid,
        source_width, source_height, fps_val, length, timeout_seconds, comfy_url,
    )
    last_retryable: Exception | None = None
    prompt_id = ""
    for attempt in range(1, _COMFY_PROMPT_ATTEMPTS + 1):
        client_id = str(uuid.uuid4())
        prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
        try:
            _wait_for_completion(prompt_id, client_id, timeout=timeout_seconds, comfy_url=comfy_url)
            break
        except (DroppedJobError, StuckJobError) as exc:
            last_retryable = exc
            if attempt >= _COMFY_PROMPT_ATTEMPTS:
                raise RuntimeError(
                    f"ComfyUI LTX IC-LoRA upscale did not complete after {_COMFY_PROMPT_ATTEMPTS} attempts: "
                    f"{_final_retryable_message(exc)}"
                ) from exc
            logger.warning(
                "[comfy] LTX IC-LoRA upscale prompt %s… failed attempt %d/%d on %s: %s; re-submitting",
                prompt_id[:8], attempt, _COMFY_PROMPT_ATTEMPTS, comfy_url, exc,
            )
    else:
        raise RuntimeError(f"ComfyUI LTX IC-LoRA upscale did not complete: {last_retryable}")

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(
            f"No output files from ComfyUI for LTX IC-LoRA upscale prompt {prompt_id} ({comfy_url})"
        )

    video_item = next((o for o in outputs if str(o.get("filename", "")).lower().endswith(".mp4")), outputs[0])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded = _download_output(video_item, output_path, comfy_url=comfy_url)
    # Normalize to the user-requested resolution (working size may be larger to
    # satisfy the IC-LoRA latent grid, e.g. 1080 → 1152 for 4×).
    return _ensure_exact_video_resolution(downloaded, requested_w, requested_h)


def upscale_video_ltx_latent(
    input_path: Path,
    output_path: Path,
    width: int,
    height: int,
    fps: float,
    timeout_seconds: int = 7200,
    comfy_url: str = COMFYUI_URL,
) -> Path:
    """Legacy 2× latent upscale via LTXVLatentUpsampler + spatial-upscaler-x2 model.

    Kept as a fallback for workers without ComfyUI-LTXVideo / IC-LoRA weights.
    """
    video_name = _stage_video_for_load(Path(input_path), comfy_url=comfy_url)
    workflow = _load_workflow("ltx23_video_upscale.json")
    workflow = _fill_template(workflow, {
        "VIDEO_NAME": video_name,
        "WIDTH": int(width),
        "HEIGHT": int(height),
        "FPS": max(1.0, float(fps or LTX_FPS)),
    })

    logger.info(
        "[comfy] upscale_video_ltx_latent %s -> %dx%d fps=%.3f timeout=%ds on %s",
        Path(input_path).name, width, height, fps, timeout_seconds, comfy_url,
    )
    last_retryable: Exception | None = None
    prompt_id = ""
    for attempt in range(1, _COMFY_PROMPT_ATTEMPTS + 1):
        client_id = str(uuid.uuid4())
        prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
        try:
            _wait_for_completion(prompt_id, client_id, timeout=timeout_seconds, comfy_url=comfy_url)
            break
        except (DroppedJobError, StuckJobError) as exc:
            last_retryable = exc
            if attempt >= _COMFY_PROMPT_ATTEMPTS:
                raise RuntimeError(
                    f"ComfyUI LTX latent upscale did not complete after {_COMFY_PROMPT_ATTEMPTS} attempts: "
                    f"{_final_retryable_message(exc)}"
                ) from exc
            logger.warning(
                "[comfy] LTX latent upscale prompt %s… failed attempt %d/%d on %s: %s; re-submitting",
                prompt_id[:8], attempt, _COMFY_PROMPT_ATTEMPTS, comfy_url, exc,
            )
    else:
        raise RuntimeError(f"ComfyUI LTX latent upscale did not complete: {last_retryable}")

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No output files from ComfyUI for LTX latent upscale prompt {prompt_id} ({comfy_url})")

    video_item = next((o for o in outputs if str(o.get("filename", "")).lower().endswith(".mp4")), outputs[0])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded = _download_output(video_item, output_path, comfy_url=comfy_url)
    return _ensure_exact_video_resolution(downloaded, int(width), int(height))


def _ensure_exact_video_resolution(video_path: Path, width: int, height: int) -> Path:
    from pipeline.assembler import ensure_video_resolution

    return ensure_video_resolution(video_path, width, height)


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


def comfy_node_exists(comfy_url: str, node_class: str) -> bool | None:
    """True/False if the worker's ComfyUI registers *node_class*, or None if the
    check couldn't be evaluated (worker unreachable). Used to distinguish "model
    file missing" from "ComfyUI too old for this engine's nodes"."""
    try:
        with urllib.request.urlopen(f"{comfy_url}/object_info/{node_class}", timeout=8) as r:
            data = json.loads(r.read())
        return bool(data.get(node_class))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        return None
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
