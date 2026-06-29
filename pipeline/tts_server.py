#!/usr/bin/env python3
"""HTTP F5-TTS server — the containerized TTS worker (issue #12).

Wraps the F5-TTS CLI in a tiny HTTP service so the TTS worker can run as a
container with no SSH access.

Endpoints:
  GET  /health            -> {"status": "ok"}
  POST /tts               -> audio/wav bytes
      body: {"text": "...", "ref_audio_b64": "<base64>" | null, "speed": 1.0}

The controller reaches this via ``pipeline/tts_worker.py``'s HTTP transport
whenever a ``tts_workers`` entry is an ``http://`` URL. When ``ref_audio_b64``
is null the bundled default narrator (assets/default_narrator.wav) is used,
matching the SSH runner's behaviour.
"""

from __future__ import annotations

import base64
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from pipeline import tts_engines  # selectable narration model (per style)

# We run inside the f5tts environment, so the current interpreter is the one
# that can import f5_tts.
F5TTS_PYTHON = sys.executable
DEFAULT_REF = Path(__file__).parent.parent / "assets" / "default_narrator.wav"

app = FastAPI(title="Stephen Spielbot F5-TTS worker")


class TTSRequest(BaseModel):
    text: str
    ref_audio_b64: str | None = None
    speed: float = 1.0
    engine: str = "openf5"


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/models")
def models() -> dict:
    """Which narration models are already downloaded in this worker's HF cache."""
    return {"cached": {k: tts_engines.is_cached(k) for k in tts_engines.TTS_ENGINES}}


class PrewarmRequest(BaseModel):
    engine: str


@app.post("/prewarm")
def prewarm(req: PrewarmRequest) -> dict:
    """Download an engine's weights into the HF cache so the first render is fast."""
    if not tts_engines.get(req.engine):
        raise HTTPException(status_code=400, detail=f"Unknown engine: {req.engine!r}")
    try:
        tts_engines.ensure(req.engine)
    except Exception as e:  # noqa: BLE001 — surface the fetch error to the caller
        raise HTTPException(status_code=500, detail=str(e)[:500])
    return {"ok": True, "engine": req.engine, "cached": tts_engines.is_cached(req.engine)}


@app.post("/tts")
def tts(req: TTSRequest) -> Response:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.wav"
        if req.ref_audio_b64:
            ref = Path(tmp) / "ref.wav"
            ref.write_bytes(base64.b64decode(req.ref_audio_b64))
        else:
            ref = DEFAULT_REF

        result = subprocess.run(
            [
                F5TTS_PYTHON, "-m", "f5_tts.infer.infer_cli",
                *tts_engines.cli_args(req.engine),
                "--ref_audio",   str(ref),
                "--ref_text",    "",
                "--gen_text",    text,
                "--output_file", str(out),
                "--speed",       str(req.speed),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Trim to keep the error payload reasonable.
            raise HTTPException(status_code=500, detail=result.stderr[-2000:])

        return Response(content=out.read_bytes(), media_type="audio/wav")
