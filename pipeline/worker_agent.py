#!/usr/bin/env python3
"""Worker agent — runs on each GPU worker machine.

Subscribes to the shared MQTT job queue, executes jobs locally
(ComfyUI for video/music, F5-TTS for narration), and publishes results.

Usage:
    python pipeline/worker_agent.py --worker-id s1 --broker 192.168.1.100
    python pipeline/worker_agent.py --worker-id local --broker localhost --comfy-url http://localhost:8188

Environment variables (override CLI defaults):
    MQTT_BROKER      Mosquitto host   (default: localhost)
    MQTT_PORT        Mosquitto port   (default: 1883)
    COMFY_URL        ComfyUI URL      (default: http://localhost:8188)
    WORKER_ID        Worker name      (default: hostname)
"""

import argparse
import base64
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("worker_agent")

# ── Paths (deployed alongside this file) ─────────────────────────────────────

_HERE = Path(__file__).parent
_ASSETS_DIR = _HERE.parent / "assets"
_DEFAULT_REF_WAV = _ASSETS_DIR / "default_narrator.wav"

# ── F5-TTS python path ────────────────────────────────────────────────────────

def _find_f5tts_python() -> str:
    if "F5TTS_PYTHON" in os.environ:
        return os.environ["F5TTS_PYTHON"]
    # Check standalone venv first (e.g. ~/f5tts-env)
    for venv in [
        Path.home() / "f5tts-env",
        Path.home() / "f5-tts-env",
        Path.home() / "f5tts",
    ]:
        candidate = venv / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    # Check conda-style envs
    for base in [
        Path.home() / "opt" / "anaconda3",
        Path.home() / "opt" / "miniconda3",
        Path.home() / "opt" / "miniforge3",
        Path.home() / "anaconda3",
        Path.home() / "miniconda3",
        Path.home() / "miniforge3",
        Path("/opt/conda"),
    ]:
        candidate = base / "envs" / "f5tts" / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    return str(Path.home() / "f5tts-env" / "bin" / "python")


_F5TTS_PYTHON = _find_f5tts_python()
_TTS_TIMEOUT  = int(os.environ.get("TTS_TIMEOUT", "300"))


class WorkerAgent:
    """Subscribes to shared MQTT job queue, executes jobs, publishes results."""

    _HEARTBEAT_INTERVAL = 30  # seconds

    def __init__(self, worker_id: str, broker_host: str, broker_port: int,
                 comfy_url: str):
        self._worker_id = worker_id
        self._comfy_url = comfy_url
        self._job_queue: queue.Queue = queue.Queue()

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect   = self._on_connect
        self._client.on_message   = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect(broker_host, broker_port, keepalive=60)
        self._client.loop_start()

        threading.Thread(target=self._process_jobs, daemon=True, name="job-processor").start()
        threading.Thread(target=self._heartbeat,    daemon=True, name="heartbeat").start()

        logger.info("Worker %s started — broker=%s:%d comfy=%s",
                    worker_id, broker_host, broker_port, comfy_url)

    # ── MQTT callbacks ─────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe("$share/workers/jobs/submit", qos=1)
        logger.info("Subscribed to job queue")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code=None, properties=None):
        logger.warning("Disconnected from broker (rc=%s) — reconnecting…", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            job = json.loads(msg.payload)
            self._job_queue.put(job)
            logger.debug("Queued job %s (type=%s)", job.get("job_id"), job.get("job_type"))
        except Exception as exc:
            logger.error("Failed to parse job message: %s", exc)

    # ── Job processing ─────────────────────────────────────────────────────────

    def _process_jobs(self) -> None:
        while True:
            job = self._job_queue.get()
            job_id   = job.get("job_id", "?")
            job_type = job.get("job_type", "?")
            attempt  = job.get("attempt", 1)
            max_att  = job.get("max_attempts", 3)
            payload  = job.get("payload", {})

            logger.info("Claiming job %s (type=%s attempt=%d/%d)",
                        job_id, job_type, attempt, max_att)
            self._publish_claim(job_id)

            t0 = time.monotonic()
            try:
                result = self._execute(job_type, payload)
                elapsed = time.monotonic() - t0
                logger.info("Job %s done in %.1fs", job_id, elapsed)
                self._publish_result(job_id, success=True, result=result)
            except Exception as exc:
                elapsed = time.monotonic() - t0
                logger.error("Job %s failed after %.1fs: %s", job_id, elapsed, exc)
                self._publish_result(job_id, success=False, error=str(exc))

    def _execute(self, job_type: str, payload: dict) -> dict:
        if   job_type == "video_t2v": return self._run_video_t2v(payload)
        elif job_type == "video_i2v": return self._run_video_i2v(payload)
        elif job_type == "tts":       return self._run_tts(payload)
        elif job_type == "music":     return self._run_music(payload)
        else:
            raise ValueError(f"Unknown job_type: {job_type!r}")

    # ── Job implementations ────────────────────────────────────────────────────

    def _fix_comfy_url(self, item: dict) -> dict:
        """Replace localhost in comfy_url with worker_id so Mac can download files."""
        if "comfy_url" in item and "localhost" in item["comfy_url"]:
            item = dict(item)
            item["comfy_url"] = item["comfy_url"].replace("localhost", self._worker_id)
        return item

    def _run_video_t2v(self, payload: dict) -> dict:
        sys.path.insert(0, str(_HERE.parent))
        from pipeline.comfyui import generate_video_clip_metadata
        item = generate_video_clip_metadata(
            positive_prompt  = payload["positive_prompt"],
            negative_prompt  = payload["negative_prompt"],
            width            = payload.get("width",  832),
            height           = payload.get("height", 480),
            length           = payload["length"],
            seed             = payload.get("seed"),
            lora_strength    = payload.get("lora_strength", 0.5),
            first_pass_cfg   = payload.get("first_pass_cfg", 1.0),
            first_pass_steps = payload.get("first_pass_steps", 8),
            second_pass_cfg  = payload.get("second_pass_cfg", 3.0),
            second_pass_steps= payload.get("second_pass_steps", 6),
            comfy_url        = self._comfy_url,
        )
        return self._fix_comfy_url(item)

    def _run_video_i2v(self, payload: dict) -> dict:
        sys.path.insert(0, str(_HERE.parent))
        from pipeline.comfyui import generate_video_continuation_metadata
        item = generate_video_continuation_metadata(
            positive_prompt  = payload["positive_prompt"],
            negative_prompt  = payload["negative_prompt"],
            last_frame_b64   = payload["last_frame_b64"],
            width            = payload.get("width",  832),
            height           = payload.get("height", 480),
            length           = payload["length"],
            seed             = payload.get("seed"),
            lora_strength    = payload.get("lora_strength", 0.5),
            first_pass_cfg   = payload.get("first_pass_cfg", 1.0),
            first_pass_steps = payload.get("first_pass_steps", 8),
            second_pass_cfg  = payload.get("second_pass_cfg", 3.0),
            second_pass_steps= payload.get("second_pass_steps", 6),
            comfy_url        = self._comfy_url,
        )
        return self._fix_comfy_url(item)

    def _run_tts(self, payload: dict) -> dict:
        text         = payload["text"]
        ref_b64      = payload.get("ref_audio_b64")

        ref_path: Path | None = None
        tmp_ref:  Path | None = None

        if ref_b64:
            tmp_ref = Path(tempfile.mktemp(suffix=".wav"))
            tmp_ref.write_bytes(base64.b64decode(ref_b64))
            ref_path = tmp_ref
        elif _DEFAULT_REF_WAV.exists():
            ref_path = _DEFAULT_REF_WAV

        out_path = Path(tempfile.mktemp(suffix=".wav"))
        try:
            self._f5tts_run(text, ref_path, out_path)
            wav_b64 = base64.b64encode(out_path.read_bytes()).decode()
            return {"wav_b64": wav_b64}
        finally:
            out_path.unlink(missing_ok=True)
            if tmp_ref:
                tmp_ref.unlink(missing_ok=True)

    def _f5tts_run(self, text: str, ref: Path | None, output_path: Path) -> None:
        cmd = [
            _F5TTS_PYTHON, "-m", "f5_tts.infer.infer_cli",
            "--model",       "F5TTS_v1_Base",
            "--ref_audio",   str(ref or _DEFAULT_REF_WAV),
            "--ref_text",    "",
            "--gen_text",    text,
            "--output_file", str(output_path),
            "--speed",       "1.0",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TTS_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"F5-TTS timed out after {_TTS_TIMEOUT}s")
        if result.returncode != 0:
            raise RuntimeError(f"F5-TTS failed:\n{result.stderr[-1000:]}")

    def _run_music(self, payload: dict) -> dict:
        sys.path.insert(0, str(_HERE.parent))
        from pipeline.comfyui import generate_music_metadata
        item = generate_music_metadata(
            topic            = payload["topic"],
            duration_seconds = payload["duration_seconds"],
            tags             = payload.get("tags"),
            seed             = payload.get("seed"),
            comfy_url        = self._comfy_url,
        )
        return self._fix_comfy_url(item)

    # ── MQTT publish helpers ───────────────────────────────────────────────────

    def _publish_claim(self, job_id: str) -> None:
        self._client.publish(
            f"jobs/claimed/{job_id}",
            json.dumps({"worker_id": self._worker_id}),
            qos=1,
        )

    def _publish_result(self, job_id: str, success: bool,
                        result: dict | None = None, error: str | None = None) -> None:
        self._client.publish(
            f"jobs/result/{job_id}",
            json.dumps({
                "job_id":    job_id,
                "worker_id": self._worker_id,
                "success":   success,
                "result":    result,
                "error":     error,
            }),
            qos=1,
        )

    def _heartbeat(self) -> None:
        while True:
            time.sleep(self._HEARTBEAT_INTERVAL)
            self._client.publish(
                f"workers/heartbeat/{self._worker_id}",
                json.dumps({"worker_id": self._worker_id, "ts": time.time()}),
                qos=0,
            )

    def run_forever(self) -> None:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Worker %s stopping…", self._worker_id)
            self._client.loop_stop()
            self._client.disconnect()


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stephen Spielbot worker agent")
    parser.add_argument("--worker-id", default=os.environ.get("WORKER_ID", socket.gethostname()),
                        help="Worker identifier (default: hostname)")
    parser.add_argument("--broker", default=os.environ.get("MQTT_BROKER", "localhost"),
                        help="MQTT broker hostname/IP")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")),
                        help="MQTT broker port")
    parser.add_argument("--comfy-url", default=os.environ.get("COMFY_URL", "http://localhost:8188"),
                        help="Local ComfyUI URL")
    args = parser.parse_args()

    agent = WorkerAgent(
        worker_id   = args.worker_id,
        broker_host = args.broker,
        broker_port = args.port,
        comfy_url   = args.comfy_url,
    )
    agent.run_forever()


if __name__ == "__main__":
    main()
