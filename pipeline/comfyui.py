"""ComfyUI API client for video and music generation."""

import json
import logging
import random
import shutil
import time
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
COMFYUI_INPUT_DIR = Path.home() / "github" / "ComfyUI" / "input"

# LTX 2.3 video defaults (25 fps, two-pass upscaled)
DEFAULT_WIDTH  = 832
DEFAULT_HEIGHT = 480
LTX_FPS        = 25
DEFAULT_LENGTH = LTX_FPS * 5 + 1   # 126 frames ≈ 5 seconds at 25 fps
MAX_LENGTH     = LTX_FPS * 20 + 1  # 501 frames ≈ 20 seconds

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


_QUEUE_CHECK_INTERVAL = 60   # seconds between /queue health checks
_STUCK_TIMEOUT        = 600  # seconds of silence from a *running* job → StuckJobError


def _wait_for_completion(
    prompt_id: str, client_id: str,
    timeout: int = 900, comfy_url: str = COMFYUI_URL,
) -> None:
    """Block until ComfyUI finishes executing prompt_id.

    Actively monitors the job:
    - WebSocket progress/executing messages reset a "last-seen-activity" timer.
    - /queue is polled every 60 s:
        * 'absent' and not in /history → DroppedJobError (re-queued by caller)
        * 'running' with no activity for _STUCK_TIMEOUT → StuckJobError (retried by caller)
    Falls back to polling /history if the WebSocket drops.
    """
    ws = websocket.WebSocket()
    ws.connect(_ws_url(comfy_url, client_id))
    start            = time.time()
    deadline         = start + timeout
    last_activity_at: float | None = None   # None until the job actually starts executing
    last_queue_check = time.time()
    nodes_done       = 0

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

                # Stuck detection: only once the job has started executing
                if last_activity_at is not None and q_status == "running":
                    stalled = now - last_activity_at
                    if stalled > _STUCK_TIMEOUT:
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
                raise RuntimeError(f"ComfyUI execution error: {data}")

    finally:
        try:
            ws.close()
        except Exception:
            pass

    _poll_completion(prompt_id, deadline, comfy_url=comfy_url)


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
    if duration_seconds is not None:
        length = min(int(duration_seconds * LTX_FPS) + 1, MAX_LENGTH)

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

    # Timeout scales with pixel count: 15 min baseline at 832×480, capped at 60 min.
    _base_px  = DEFAULT_WIDTH * DEFAULT_HEIGHT   # 399 360
    _video_timeout = min(3600, max(900, int(900 * (width * height) / _base_px)))
    logger.info("[comfy] generate_video_clip %dx%d timeout=%ds", width, height, _video_timeout)

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
    if duration_seconds is not None:
        length = min(int(duration_seconds * LTX_FPS) + 1, MAX_LENGTH)

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

    _base_px  = DEFAULT_WIDTH * DEFAULT_HEIGHT
    _video_timeout = min(3600, max(900, int(900 * (width * height) / _base_px)))
    logger.info("[comfy] generate_video_continuation %dx%d timeout=%ds", width, height, _video_timeout)

    client_id = str(uuid.uuid4())
    prompt_id = _queue_prompt(workflow, client_id, comfy_url=comfy_url)
    _wait_for_completion(prompt_id, client_id, timeout=_video_timeout, comfy_url=comfy_url)

    outputs = _get_outputs(prompt_id, comfy_url=comfy_url)
    if not outputs:
        raise RuntimeError(f"No output files from ComfyUI for I2V prompt {prompt_id} ({comfy_url})")

    video_item = next((o for o in outputs if o.get("type") == "output"), outputs[0])
    return _download_output(video_item, output_path, comfy_url=comfy_url)


def _upload_video(video_path: Path) -> str:
    """Copy video into local ComfyUI input folder. Used only by ltx_upscale_video."""
    COMFYUI_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"upload_{uuid.uuid4().hex[:8]}_{video_path.name}"
    dest = COMFYUI_INPUT_DIR / name
    shutil.copy2(video_path, dest)
    return name


def ltx_upscale_video(video_path: Path, output_path: Path) -> Path:
    """Upscale a video using the LTX 2.3 spatial upscaler (local ComfyUI only)."""
    video_name = _upload_video(video_path)
    try:
        workflow = _load_workflow("ltx23_upscale.json")
        workflow = _fill_template(workflow, {"VIDEO_FILE": video_name})

        client_id = str(uuid.uuid4())
        prompt_id = _queue_prompt(workflow, client_id)
        _wait_for_completion(prompt_id, client_id, timeout=1800)

        outputs = _get_outputs(prompt_id)
        if not outputs:
            raise RuntimeError(f"No output from LTX upscale for prompt {prompt_id}")

        video_item = next((o for o in outputs if o.get("type") == "output"), outputs[0])
        return _download_output(video_item, output_path)
    finally:
        (COMFYUI_INPUT_DIR / video_name).unlink(missing_ok=True)


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
