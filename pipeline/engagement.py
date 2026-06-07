"""Engagement prediction (issue #50).

Estimates a video's first-3-day view count from its title + description, trained
on the channel's own YouTube history. Title+description are embedded with
``BAAI/bge-small-en-v1.5`` (via fastembed / ONNX — no torch) and fed to a Ridge
regressor. A second model adds post weekday + hour to suggest *when* to publish.

Two design notes that keep the numbers honest:
  * Target = sum of views over the first 3 *calendar* days (UTC) after publish.
    A late-day upload gets a short calendar day-1; the timing model's hour
    feature absorbs that effect, so the content/timing models are not strictly
    apples-to-apples (surfaced in the eval UI).
  * The YouTube Analytics API finalises a day's views ~2-3 days late, so videos
    younger than ``engagement_data_lag_days`` (default 5) are excluded from
    training — otherwise their day-3 labels would be truncated.

Heavy deps (numpy, scikit-learn, fastembed) are imported lazily inside functions
so importing this module — and the FastAPI backend / test suite that import it —
stays cheap and never hard-fails when the extras aren't installed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import pickle
import threading
import time
import types
from pathlib import Path

import pipeline.youtube as yt

logger = logging.getLogger("video_gen")

_CONFIG_DIR = Path.home() / ".config" / "video-generator"
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"
_ENG_DIR = _CONFIG_DIR / "engagement"
_EMB_DIR = _ENG_DIR / "embeddings"
_CONTENT_PATH = _ENG_DIR / "model_content.pkl"
_TIMING_PATH = _ENG_DIR / "model_timing.pkl"
_METRICS_PATH = _ENG_DIR / "metrics.json"

_DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
_DEFAULT_MIN_SAMPLES = 15
_DEFAULT_LAG_DAYS = 5

# Warm, process-local state so predict()/best_times() are fast. Guarded by _lock;
# reloaded from disk when metrics.json changes (a fresh build needs no restart).
_lock = threading.Lock()
_state: dict = {"content": None, "timing": None, "metrics": None, "mtime": 0.0, "loaded": False}
_embedder = None
_embedder_name = ""
_embedder_lock = threading.Lock()


# ── lazy imports ──────────────────────────────────────────────────────────────

def _ml():
    """Import numpy + scikit-learn, raising a clear install hint if missing."""
    try:
        import numpy as np
        import sklearn
        from sklearn.linear_model import RidgeCV
        from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
        from sklearn.model_selection import KFold, LeaveOneOut
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise ImportError(
            "Engagement model needs numpy + scikit-learn. "
            "Run: pip install -r webapp/backend/requirements.txt"
        ) from exc
    return types.SimpleNamespace(
        np=np, sklearn_version=sklearn.__version__,
        RidgeCV=RidgeCV, StandardScaler=StandardScaler, Pipeline=Pipeline,
        KFold=KFold, LeaveOneOut=LeaveOneOut,
        mae=mean_absolute_error, mse=mean_squared_error, r2=r2_score,
    )


def _load_cfg() -> dict:
    try:
        import yaml
        return yaml.safe_load(_CONFIG_FILE.read_text()) or {}
    except Exception:
        return {}


# ── embeddings ────────────────────────────────────────────────────────────────

def _text(title: str, description: str) -> str:
    return f"{(title or '').strip()}\n{(description or '').strip()}".strip()


def _get_embedder():
    """Construct (once) and return the fastembed text embedder."""
    global _embedder, _embedder_name
    name = _load_cfg().get("engagement_embed_model", _DEFAULT_EMBED_MODEL)
    with _embedder_lock:
        if _embedder is not None and _embedder_name == name:
            return _embedder
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ImportError(
                "Engagement embeddings need fastembed. "
                "Run: pip install -r webapp/backend/requirements.txt"
            ) from exc
        logger.info("Loading embedding model %s (first run downloads it)", name)
        _embedder = TextEmbedding(model_name=name)
        _embedder_name = name
        return _embedder


def _embed(texts: list[str]):
    """Embed texts to a (len, dim) float32 array, caching each vector on disk
    keyed by a content hash so rebuilds and repeated predicts are cheap."""
    import numpy as np
    _EMB_DIR.mkdir(parents=True, exist_ok=True)
    out: list = [None] * len(texts)
    paths = []
    miss_texts: list[str] = []
    miss_idx: list[int] = []
    for i, t in enumerate(texts):
        h = hashlib.sha256(t.encode("utf-8")).hexdigest()
        p = _EMB_DIR / f"{h}.npy"
        paths.append(p)
        if p.exists():
            try:
                out[i] = np.load(p)
                continue
            except Exception:
                pass
        miss_texts.append(t)
        miss_idx.append(i)
    if miss_texts:
        vecs = list(_get_embedder().embed(miss_texts))
        for j, vec in zip(miss_idx, vecs):
            v = np.asarray(vec, dtype=np.float32)
            out[j] = v
            try:
                np.save(paths[j], v)
            except Exception:
                pass
    return np.vstack(out)


def _timing_features(np, emb, weekdays, hours):
    """Concatenate embeddings with one-hot weekday[7] + cyclical hour sin/cos[2]."""
    weekdays = np.asarray(weekdays, dtype=int)
    hours = np.asarray(hours, dtype=float)
    wd = np.eye(7, dtype=np.float32)[weekdays]
    hsin = np.sin(2 * np.pi * hours / 24.0).reshape(-1, 1)
    hcos = np.cos(2 * np.pi * hours / 24.0).reshape(-1, 1)
    return np.hstack([emb, wd, hsin, hcos]).astype(np.float32)


# ── dataset ───────────────────────────────────────────────────────────────────

def build_dataset(client_secrets_path: str) -> tuple[list[dict], int]:
    """Fetch channel history and turn it into training rows.

    Returns ``(dataset, n_dropped)`` where each row is
    ``{video_id, title, description, views, weekday, hour}`` and ``views`` is the
    first-3-calendar-day total. Drops non-public videos and ones too recent for
    finalised analytics.
    """
    import datetime
    cfg = _load_cfg()
    lag = int(cfg.get("engagement_data_lag_days", _DEFAULT_LAG_DAYS))
    cutoff = datetime.date.today() - datetime.timedelta(days=lag)
    rows = yt.fetch_training_rows(client_secrets_path)
    dataset: list[dict] = []
    dropped = 0
    for r in rows:
        if (r.get("privacy") or "") != "public":
            dropped += 1
            continue
        iso = r.get("published_at") or ""
        try:
            dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except Exception:
            dropped += 1
            continue
        pub = dt.date()
        if pub > cutoff:   # too recent — day-3 views not finalised yet
            dropped += 1
            continue
        dv = r.get("day_views") or {}
        views = sum(
            int(dv.get((pub + datetime.timedelta(days=k)).isoformat(), 0) or 0)
            for k in range(3)
        )
        dataset.append({
            "video_id": r.get("video_id", ""),
            "title": r.get("title", ""),
            "description": r.get("description", ""),
            "views": int(views),
            "weekday": dt.weekday(),   # Mon=0 .. Sun=6 (UTC)
            "hour": dt.hour,           # 0..23 (UTC)
        })
    return dataset, dropped


# ── training & evaluation ─────────────────────────────────────────────────────

def _fit_pipeline(m, X, y):
    pipe = m.Pipeline([
        ("scale", m.StandardScaler()),
        ("ridge", m.RidgeCV(alphas=m.np.logspace(-1, 4, 20))),
    ])
    pipe.fit(X, y)
    return pipe


def _cv_predict(m, X, y, n):
    """Out-of-fold predictions (log space) for the model and a predict-the-mean
    baseline, using leave-one-out for small N else 5-fold."""
    np = m.np
    splitter = m.LeaveOneOut() if n < 50 else m.KFold(n_splits=5, shuffle=True, random_state=0)
    oof = np.zeros(n)
    base = np.zeros(n)
    for tr, te in splitter.split(X):
        oof[te] = _fit_pipeline(m, X[tr], y[tr]).predict(X[te])
        base[te] = y[tr].mean()
    return oof, base


def _metric_block(m, y_log, oof_log, base_log) -> dict:
    np = m.np
    yv = np.expm1(y_log)
    pv = np.clip(np.expm1(oof_log), 0, None)
    bv = np.clip(np.expm1(base_log), 0, None)
    pearson = (float(np.corrcoef(yv, pv)[0, 1])
               if len(yv) > 1 and np.std(yv) > 0 and np.std(pv) > 0 else 0.0)
    return {
        "mae": float(m.mae(yv, pv)),
        "rmse": float(math.sqrt(m.mse(yv, pv))),
        "r2": float(m.r2(yv, pv)),
        "pearson": pearson,
        "log_mae": float(m.mae(y_log, oof_log)),
        "baseline_mae": float(m.mae(yv, bv)),
        "baseline_rmse": float(math.sqrt(m.mse(yv, bv))),
        "baseline_log_mae": float(m.mae(y_log, base_log)),
    }


def _evaluate(m, Xc, Xt, y, dataset, n, dropped, min_samples) -> dict:
    np = m.np
    out: dict = {"n_samples": n, "n_dropped": dropped}
    beats = False
    if n >= 4:
        oof_c, base = _cv_predict(m, Xc, y, n)
        oof_t, _ = _cv_predict(m, Xt, y, n)
        out["content"] = _metric_block(m, y, oof_c, base)
        out["timing"] = _metric_block(m, y, oof_t, base)
        yv = np.expm1(y)
        pc = np.clip(np.expm1(oof_c), 0, None)
        out["samples"] = [
            {"video_id": dataset[i]["video_id"], "title": dataset[i]["title"],
             "actual": int(round(float(yv[i]))), "predicted": int(round(float(pc[i])))}
            for i in range(n)
        ]
        # Judge "beats guessing" in log space — the model's training objective and
        # robust to the heavy-tailed view outliers that make view-space RMSE a
        # coin-flip on small channels (one viral video dominates the metric).
        # Require a clear margin (5%): on pure noise the model lands within ~1% of
        # the mean, so a strict "<" would award "ok" to random luck.
        beats = out["content"]["log_mae"] < 0.95 * out["content"]["baseline_log_mae"]
    else:
        out["content"] = None
        out["timing"] = None
        out["samples"] = []
    out["reliability"] = "insufficient" if n < min_samples else ("ok" if beats else "weak")
    return out


def build(client_secrets_path: str, on_phase=None) -> dict:
    """Full pipeline: fetch → embed → train both models → evaluate → persist →
    warm the in-memory cache. ``on_phase(name)`` is called with the current phase
    ('fetching'|'embedding'|'training'|'done') for progress reporting."""
    def phase(p: str) -> None:
        if on_phase:
            try:
                on_phase(p)
            except Exception:
                pass

    phase("fetching")
    dataset, dropped = build_dataset(client_secrets_path)
    n = len(dataset)
    if n < 2:
        return {"available": False, "n_samples": n, "n_dropped": dropped,
                "error": "Not enough public videos with finalised 3-day analytics "
                         "to train a model. Publish a few more, then rebuild."}

    m = _ml()
    np = m.np
    cfg = _load_cfg()

    phase("embedding")
    emb = _embed([_text(d["title"], d["description"]) for d in dataset])
    y = np.log1p(np.array([d["views"] for d in dataset], dtype=float))
    Xt = _timing_features(np, emb, [d["weekday"] for d in dataset], [d["hour"] for d in dataset])

    phase("training")
    content = _fit_pipeline(m, emb, y)
    timing = _fit_pipeline(m, Xt, y)
    metrics = _evaluate(m, emb, Xt, y, dataset, n,
                        dropped, int(cfg.get("engagement_min_samples", _DEFAULT_MIN_SAMPLES)))
    metrics.update({
        "built_at": time.time(),
        "sklearn_version": m.sklearn_version,
        "embed_model": cfg.get("engagement_embed_model", _DEFAULT_EMBED_MODEL),
        "data_lag_days": int(cfg.get("engagement_data_lag_days", _DEFAULT_LAG_DAYS)),
    })

    _ENG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CONTENT_PATH, "wb") as f:
        pickle.dump(content, f)
    with open(_TIMING_PATH, "wb") as f:
        pickle.dump(timing, f)
    _METRICS_PATH.write_text(json.dumps(metrics, indent=2))
    with _lock:
        _state.update({"content": content, "timing": timing, "metrics": metrics,
                       "mtime": _METRICS_PATH.stat().st_mtime, "loaded": True})
    phase("done")
    return {"available": True, **metrics}


# ── inference (fast path) ─────────────────────────────────────────────────────

def _get_models():
    """Return (content, timing, metrics), loading/refreshing from disk as needed.
    A scikit-learn version mismatch yields (None, None, metrics) so callers can
    prompt a rebuild instead of crashing on a stale pickle."""
    with _lock:
        if not _METRICS_PATH.exists() or not _CONTENT_PATH.exists():
            return None, None, None
        mtime = _METRICS_PATH.stat().st_mtime
        if _state["loaded"] and _state["mtime"] == mtime:
            return _state["content"], _state["timing"], _state["metrics"]
        try:
            metrics = json.loads(_METRICS_PATH.read_text())
        except Exception:
            return None, None, None
        try:
            import sklearn
            cur = sklearn.__version__
        except Exception:
            cur = ""
        if metrics.get("sklearn_version") and cur and metrics["sklearn_version"] != cur:
            logger.warning("Engagement model built with scikit-learn %s but %s is installed; rebuild needed",
                           metrics.get("sklearn_version"), cur)
            _state.update({"content": None, "timing": None, "metrics": metrics, "mtime": mtime, "loaded": True})
            return None, None, metrics
        try:
            with open(_CONTENT_PATH, "rb") as f:
                content = pickle.load(f)
            with open(_TIMING_PATH, "rb") as f:
                timing = pickle.load(f)
        except Exception as exc:
            logger.warning("Failed to load engagement models: %s", exc)
            return None, None, metrics
        _state.update({"content": content, "timing": timing, "metrics": metrics, "mtime": mtime, "loaded": True})
        return content, timing, metrics


def status() -> dict:
    """Current model state + evaluation, for the Engagement tab."""
    content, _timing, metrics = _get_models()
    if metrics is None:
        return {"available": False}
    res = {"available": content is not None, **metrics}
    if content is None:
        res["needs_rebuild"] = True
    return res


def predict(title: str, description: str) -> dict:
    """Estimate first-3-day views for an idea. Never raises — returns
    ``{"available": False}`` when no usable model exists (the common case on the
    create screens before a model is built)."""
    try:
        content, _timing, metrics = _get_models()
        if content is None:
            return {"available": False}
        import numpy as np
        pred = float(np.clip(np.expm1(content.predict(_embed([_text(title, description)]))), 0, None)[0])
        return {"available": True, "predicted_views": int(round(pred)),
                "reliability": metrics.get("reliability", "weak"),
                "n_samples": metrics.get("n_samples", 0)}
    except Exception as exc:
        logger.info("engagement.predict failed: %s", exc)
        return {"available": False, "error": str(exc)[:200]}


def best_times(title: str, description: str, top_k: int = 5) -> dict:
    """Score every (weekday, hour) slot with the timing model and return the
    top-k plus the full 7x24 grid (for a heatmap). Advisory only."""
    try:
        _content, timing, metrics = _get_models()
        if timing is None:
            return {"available": False}
        import numpy as np
        emb = _embed([_text(title, description)])
        weekdays = [wd for wd in range(7) for _ in range(24)]
        hours = [h for _ in range(7) for h in range(24)]
        X = _timing_features(np, np.repeat(emb, len(weekdays), axis=0), weekdays, hours)
        preds = np.clip(np.expm1(timing.predict(X)), 0, None)
        grid = [{"weekday": weekdays[i], "hour": hours[i], "predicted_views": int(round(float(preds[i])))}
                for i in range(len(weekdays))]
        best = sorted(grid, key=lambda g: g["predicted_views"], reverse=True)[:top_k]
        return {"available": True, "best": best, "grid": grid,
                "reliability": metrics.get("reliability", "weak"),
                "n_samples": metrics.get("n_samples", 0)}
    except Exception as exc:
        logger.info("engagement.best_times failed: %s", exc)
        return {"available": False, "error": str(exc)[:200]}
