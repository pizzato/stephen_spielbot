"""MQTT-based job dispatcher — Mac side.

MQTTJobClient publishes jobs to a shared worker queue. Workers claim jobs and
publish results. A background thread re-queues any claimed job that doesn't
complete within the per-type timeout.

Topics:
  jobs/submit                   Mac publishes all job types (QoS 1)
  $share/workers/jobs/submit    Workers subscribe (shared → round-robin)
  jobs/claimed/{job_id}         Worker claims a job
  jobs/result/{job_id}          Worker publishes result
  workers/heartbeat/{worker_id} Worker heartbeats every 30s
"""

import json
import logging
import threading
import time
import uuid

import paho.mqtt.client as mqtt

logger = logging.getLogger("video_gen")

# Completion timeouts (seconds) — from when the worker claims the job.
# If no result arrives within this window, the job is re-queued.
_COMPLETION_TIMEOUTS: dict[str, int] = {
    "video_t2v": 1200,  # 20 min — long GPU render
    "video_i2v": 1200,
    "music":      600,  # 10 min
    "tts":        300,  # 5 min
}
_MAX_ATTEMPTS = 3
_CHECKER_INTERVAL = 30  # seconds between timeout sweeps
_CLAIM_TIMEOUT = 60     # re-publish if not claimed within this many seconds


class MQTTJobClient:
    """Publish jobs to MQTT, collect results. Thread-safe."""

    def __init__(self, broker_host: str = "localhost", broker_port: int = 1883):
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._lock = threading.Lock()
        # request_id → {job_id, job_type, payload, attempt,
        #                submitted_at, claimed_at, event, result}
        self._pending: dict[str, dict] = {}

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=10)
        self._client.connect(broker_host, broker_port, keepalive=60)
        self._client.loop_start()

        self._checker_thread = threading.Thread(
            target=self._timeout_checker, daemon=True, name="mqtt-checker"
        )
        self._checker_thread.start()
        logger.info("[mqtt] client connected to %s:%d", broker_host, broker_port)

    # ── MQTT callbacks ─────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe("jobs/claimed/+", qos=1)
        client.subscribe("jobs/result/+", qos=1)
        logger.info("[mqtt] subscribed to result topics")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code=None, properties=None):
        logger.warning("[mqtt] disconnected (rc=%s) — will reconnect", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except Exception:
            return

        topic = msg.topic
        job_id = topic.rsplit("/", 1)[-1]

        if topic.startswith("jobs/claimed/"):
            with self._lock:
                for req in self._pending.values():
                    if req["job_id"] == job_id and req.get("claimed_at") is None:
                        req["claimed_at"] = time.time()
                        logger.info("[mqtt] job %s claimed by %s", job_id, data.get("worker_id"))
                        break

        elif topic.startswith("jobs/result/"):
            with self._lock:
                for req_id, req in self._pending.items():
                    if req["job_id"] == job_id:
                        req["result"] = data
                        req["event"].set()
                        logger.info("[mqtt] result for job %s (success=%s)", job_id, data.get("success"))
                        break

    # ── Public API ─────────────────────────────────────────────────────────────

    def submit_job(self, job_type: str, payload: dict) -> str:
        """Publish a job. Returns request_id (stable across retries)."""
        request_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        event = threading.Event()

        with self._lock:
            self._pending[request_id] = {
                "job_id":       job_id,
                "job_type":     job_type,
                "payload":      payload,
                "attempt":      1,
                "submitted_at": time.time(),
                "claimed_at":   None,
                "event":        event,
                "result":       None,
            }

        self._publish_job(job_id, job_type, payload, attempt=1)
        return request_id

    def await_result(self, request_id: str) -> dict:
        """Block until the job succeeds or exhausts all retry attempts.
        Returns the result payload dict on success; raises RuntimeError on failure."""
        with self._lock:
            req = self._pending.get(request_id)
        if req is None:
            raise KeyError(f"Unknown request_id: {request_id}")

        req["event"].wait()  # timeout_checker sets this after max attempts

        with self._lock:
            req = self._pending.pop(request_id, None)

        if req is None or req.get("result") is None:
            raise RuntimeError("Job result lost — internal error")

        result = req["result"]
        if not result.get("success"):
            raise RuntimeError(f"Job failed after {req['attempt']} attempt(s): {result.get('error', 'unknown')}")

        return result["result"]

    def close(self):
        self._client.loop_stop()
        self._client.disconnect()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _publish_job(self, job_id: str, job_type: str, payload: dict, attempt: int) -> None:
        msg = {
            "job_id":       job_id,
            "job_type":     job_type,
            "submitted_at": time.time(),
            "attempt":      attempt,
            "max_attempts": _MAX_ATTEMPTS,
            "payload":      payload,
        }
        self._client.publish("jobs/submit", json.dumps(msg), qos=1)
        logger.info("[mqtt] published job %s (type=%s attempt=%d)", job_id, job_type, attempt)

    def _timeout_checker(self) -> None:
        """Background thread: re-queue claimed jobs that haven't completed in time."""
        while True:
            time.sleep(_CHECKER_INTERVAL)
            now = time.time()
            with self._lock:
                for req_id, req in list(self._pending.items()):
                    if req.get("result") is not None:
                        continue  # result received, event will fire soon

                    claimed_at = req.get("claimed_at")
                    if claimed_at is None:
                        # Re-publish if no worker claimed the job within _CLAIM_TIMEOUT.
                        # QoS 1 only delivers to connected subscribers at publish time;
                        # if all workers were offline, the message is lost.
                        if now - req["submitted_at"] <= _CLAIM_TIMEOUT:
                            continue
                        attempt = req["attempt"] + 1
                        if attempt > _MAX_ATTEMPTS:
                            logger.error(
                                "[mqtt] job %s (type=%s) never claimed after %d attempts — giving up",
                                req["job_id"], req["job_type"], _MAX_ATTEMPTS,
                            )
                            req["result"] = {
                                "success": False,
                                "error":   f"never claimed after {_MAX_ATTEMPTS} attempts",
                            }
                            req["event"].set()
                        else:
                            new_job_id = str(uuid.uuid4())
                            logger.warning(
                                "[mqtt] job %s never claimed after %.0fs (attempt %d/%d) — re-queuing as %s",
                                req["job_id"], now - req["submitted_at"], req["attempt"], _MAX_ATTEMPTS, new_job_id,
                            )
                            req["job_id"]      = new_job_id
                            req["attempt"]     = attempt
                            req["submitted_at"] = now
                            self._publish_job(new_job_id, req["job_type"], req["payload"], attempt)
                        continue

                    timeout = _COMPLETION_TIMEOUTS.get(req["job_type"], 1200)
                    if now - claimed_at <= timeout:
                        continue

                    attempt = req["attempt"] + 1
                    if attempt > _MAX_ATTEMPTS:
                        logger.error(
                            "[mqtt] job %s (type=%s) exhausted %d attempts — giving up",
                            req["job_id"], req["job_type"], _MAX_ATTEMPTS,
                        )
                        req["result"] = {
                            "success": False,
                            "error":   f"timed out after {_MAX_ATTEMPTS} attempts",
                        }
                        req["event"].set()
                    else:
                        new_job_id = str(uuid.uuid4())
                        logger.warning(
                            "[mqtt] job %s timed out after %.0fs (attempt %d/%d) — re-queuing as %s",
                            req["job_id"], now - claimed_at, req["attempt"], _MAX_ATTEMPTS, new_job_id,
                        )
                        req["job_id"]     = new_job_id
                        req["attempt"]    = attempt
                        req["claimed_at"] = None
                        self._publish_job(new_job_id, req["job_type"], req["payload"], attempt)
