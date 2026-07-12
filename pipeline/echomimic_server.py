#!/usr/bin/env python3
"""HTTP EchoMimic-V3 server — the containerized talking-head worker.

Wraps antgroup/echomimic_v3's ``infer_flash.py`` (a reference portrait + a speech
WAV -> a lip-synced talking-head MP4) in a tiny HTTP service, mirroring
``pipeline/tts_server.py``. The controller reaches it via ``pipeline/echomimic.py``
whenever an ``echomimic_workers`` entry is an ``http://`` URL.

Endpoints:
  GET  /health   -> {"status": "ok"}
  POST /prewarm  -> {"ok": true}                 (download weights ahead of first use)
  POST /animate  -> video/mp4 bytes
      body: {"image_b64","audio_b64","prompt","steps","size","video_length"}

Weights (~27 GB: Wan2.1-Fun-1.3B base + wav2vec + the EchoMimic flash transformer)
are fetched from HuggingFace into ``ECHOMIMIC_WEIGHTS`` on first use and persisted
via a volume (mirrors the TTS worker's HF-cache approach), so they survive
container recreation and aren't baked into the image.
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

logger = logging.getLogger("echomimic")
logging.basicConfig(level=logging.INFO)

REPO_DIR = Path(os.environ.get("ECHOMIMIC_REPO", "/app/echomimic_v3"))
WEIGHTS_DIR = Path(os.environ.get("ECHOMIMIC_WEIGHTS", "/weights"))
STUBS_DIR = os.environ.get("ECHOMIMIC_STUBS", "/app/stubs")
CONFIG_PATH = os.environ.get("ECHOMIMIC_CONFIG", "config/config.yaml")
TIMEOUT = int(os.environ.get("ECHOMIMIC_TIMEOUT", "1800"))  # seconds per animate

# HuggingFace repos and their target subdirs under WEIGHTS_DIR.
_WEIGHT_REPOS = [
    ("TencentGameMate/chinese-wav2vec2-base", "chinese-wav2vec2-base"),
    ("BadToBest/EchoMimicV3", "EchoMimicV3"),
    ("alibaba-pai/Wan2.1-Fun-V1.1-1.3B-InP", "Wan2.1-Fun-V1.1-1.3B-InP"),
]

_MODEL_NAME = str(WEIGHTS_DIR / "Wan2.1-Fun-V1.1-1.3B-InP")
_TRANSFORMER = str(WEIGHTS_DIR / "EchoMimicV3" / "echomimicv3-flash-pro" / "diffusion_pytorch_model.safetensors")
_WAV2VEC = str(WEIGHTS_DIR / "chinese-wav2vec2-base")

_weights_lock = threading.Lock()

app = FastAPI(title="Stephen Spielbot EchoMimic-V3 worker")


class AnimateRequest(BaseModel):
    image_b64: str
    audio_b64: str
    prompt: str = "A person is speaking to the camera, natural facial expression."
    steps: int = 8
    size: int = 768         # square fallback when width/height are not given
    width: int = 0          # explicit output dims (e.g. a landscape scene frame);
    height: int = 0         # 0 ⟹ size×size square
    video_length: int = 81  # frames at 25 fps; caller sizes this to its audio


def _ensure_weights() -> None:
    """Download the model weights into WEIGHTS_DIR if the flash transformer is missing.

    Serialized so concurrent first requests don't race. Idempotent — snapshot_download
    skips files already present."""
    if Path(_TRANSFORMER).exists():
        return
    with _weights_lock:
        if Path(_TRANSFORMER).exists():
            return
        from huggingface_hub import snapshot_download

        for repo, sub in _WEIGHT_REPOS:
            dest = WEIGHTS_DIR / sub
            logger.info("[echomimic] downloading %s -> %s", repo, dest)
            snapshot_download(repo_id=repo, local_dir=str(dest))
        logger.info("[echomimic] weights ready")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/prewarm")
def prewarm() -> dict:
    try:
        _ensure_weights()
    except Exception as e:  # noqa: BLE001 — surface the fetch error to the caller
        raise HTTPException(status_code=500, detail=str(e)[:500])
    return {"ok": True, "weights_ready": Path(_TRANSFORMER).exists()}


@app.post("/animate")
def animate(req: AnimateRequest) -> Response:
    _ensure_weights()
    with tempfile.TemporaryDirectory() as tmp:
        tmpp = Path(tmp)
        (tmpp / "in.png").write_bytes(base64.b64decode(req.image_b64))
        (tmpp / "in.wav").write_bytes(base64.b64decode(req.audio_b64))
        out_dir = tmpp / "out"
        out_dir.mkdir()

        env = dict(os.environ)
        env["PYTHONPATH"] = STUBS_DIR + os.pathsep + env.get("PYTHONPATH", "")

        # sample_size is (height, width) — VideoX-Fun convention EchoMimic builds on.
        out_w = int(req.width) or int(req.size)
        out_h = int(req.height) or int(req.size)
        cmd = [
            "python3", "infer_flash.py",
            "--image_path", str(tmpp / "in.png"),
            "--audio_path", str(tmpp / "in.wav"),
            "--prompt", req.prompt,
            "--num_inference_steps", str(req.steps),
            "--config_path", CONFIG_PATH,
            "--model_name", _MODEL_NAME,
            "--transformer_path", _TRANSFORMER,
            "--wav2vec_model_dir", _WAV2VEC,
            "--save_path", str(out_dir),
            "--video_length", str(max(1, int(req.video_length))),
            "--sample_size", str(out_h), str(out_w),
            "--fps", "25",
            "--weight_dtype", "bfloat16",
        ]
        result = subprocess.run(
            cmd, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=TIMEOUT, env=env
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=result.stderr[-2000:])

        mp4s = sorted(out_dir.glob("*.mp4"))
        if not mp4s:
            raise HTTPException(status_code=500, detail="no mp4 produced:\n" + result.stdout[-1500:])
        return Response(content=mp4s[0].read_bytes(), media_type="video/mp4")
