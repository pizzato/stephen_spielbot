"""Train / evaluate / predict on synthetic embeddings (issue #50).

Mocks _embed so nothing downloads the real embedding model, and mocks
build_dataset so nothing hits the network. Exercises the real scikit-learn
train + leave-one-out evaluation + persistence + warm-cache predict path.
"""
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import pipeline.engagement as eng


def _synthetic(n, signal=True, seed=0):
    """n rows whose views either follow a real linear signal in the embeddings or
    are pure noise. The signal spans all dims (a recoverable regime needs n > the
    384-dim feature space — which also exercises the fast 5-fold CV path)."""
    rng = np.random.default_rng(seed)
    emb = rng.standard_normal((n, 384)).astype("float32")
    if signal:
        w = rng.standard_normal(384)
        w /= np.linalg.norm(w)
        base = emb @ w                  # true linear signal across all dimensions
    else:
        base = rng.standard_normal(n)   # unrelated to the embeddings
    views = np.clip(np.expm1(3.0 + 1.2 * base), 0, None).round().astype(int)
    dataset = [
        {"video_id": f"v{i}", "title": f"title {i}", "description": "",
         "views": int(views[i]), "weekday": i % 7, "hour": i % 24}
        for i in range(n)
    ]
    return dataset, emb


class EngagementModelTests(unittest.TestCase):
    def _build(self, n, signal=True, seed=0):
        dataset, emb = _synthetic(n, signal=signal, seed=seed)
        with mock.patch.object(eng, "build_dataset", return_value=(dataset, 3)), \
             mock.patch.object(eng, "_embed", return_value=emb):
            result = eng.build("/tmp/secrets.json")
        return result, emb

    def test_beats_baseline_with_signal(self):
        result, _ = self._build(400, signal=True)
        self.assertTrue(result["available"])
        self.assertEqual(result["n_samples"], 400)
        self.assertEqual(result["n_dropped"], 3)
        # The honesty centerpiece: a real signal must beat predict-the-mean. We
        # judge it in log space (robust to view-count outliers), matching how the
        # reliability flag is decided.
        self.assertLess(result["content"]["log_mae"], result["content"]["baseline_log_mae"])
        self.assertEqual(result["reliability"], "ok")
        self.assertEqual(len(result["samples"]), 400)
        self.assertIn("actual", result["samples"][0])
        self.assertIn("predicted", result["samples"][0])

    def test_no_signal_flagged_weak(self):
        # Views unrelated to the embeddings → the model must NOT claim to beat
        # guessing; it should be flagged "weak", not "ok".
        result, _ = self._build(400, signal=False)
        self.assertTrue(result["available"])
        self.assertEqual(result["reliability"], "weak")

    def test_small_n_flagged_insufficient(self):
        result, _ = self._build(6, signal=True)
        self.assertTrue(result["available"])            # still trains, never raises
        self.assertEqual(result["reliability"], "insufficient")

    def test_too_few_rows_returns_unavailable(self):
        with mock.patch.object(eng, "build_dataset", return_value=([], 0)):
            result = eng.build("/tmp/secrets.json")
        self.assertFalse(result["available"])
        self.assertIn("error", result)

    def test_predict_after_build_uses_warm_model(self):
        _result, emb = self._build(30, signal=True)
        with mock.patch.object(eng, "_embed", return_value=emb[:1]):
            pred = eng.predict("some idea", "about something")
        self.assertTrue(pred["available"])
        self.assertGreaterEqual(pred["predicted_views"], 0)
        self.assertIn(pred["reliability"], ("ok", "weak", "insufficient"))

    def test_best_times_returns_full_grid(self):
        _result, emb = self._build(30, signal=True)
        with mock.patch.object(eng, "_embed", return_value=emb[:1]):
            res = eng.best_times("idea", "desc", top_k=5)
        self.assertTrue(res["available"])
        self.assertEqual(len(res["grid"]), 7 * 24)
        self.assertLessEqual(len(res["best"]), 5)
        # best is sorted descending by predicted views
        preds = [b["predicted_views"] for b in res["best"]]
        self.assertEqual(preds, sorted(preds, reverse=True))
        self.assertTrue(all(0 <= b["weekday"] <= 6 and 0 <= b["hour"] <= 23 for b in res["grid"]))

    def test_predict_unavailable_when_no_model(self):
        # Point the model paths at a guaranteed-empty location.
        tmp = tempfile.mkdtemp(prefix="spielbot-test-empty-")
        with mock.patch.object(eng, "_METRICS_PATH", eng.Path(tmp) / "nope.json"), \
             mock.patch.object(eng, "_CONTENT_PATH", eng.Path(tmp) / "nope.pkl"):
            self.assertEqual(eng.predict("a", "b"), {"available": False})
            self.assertEqual(eng.best_times("a", "b"), {"available": False})


if __name__ == "__main__":
    unittest.main()
