"""Automation auto-start semantics (auto-start the next queue item).

Covers _auto_start_best's two modes — auto-approve (write missing scripts,
render end-to-end, opt-in AI-idea fallback) and review-gate (only render items
whose script is already written; never generate scripts) — plus the
youtube_auto_ai_ideas seeding for configs saved before the toggle existed.
"""
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import yaml

import app
import webapp.backend.main as backend


class TempConfigCase(unittest.TestCase):
    """Point app.CONFIG_FILE (and friends) at per-test temp locations."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-automation-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.config_file = tmp / "config" / "config.yaml"
        self.config_file.parent.mkdir(parents=True)
        self.output_dir = tmp / "videos"
        self.output_dir.mkdir()
        for target, attr, value in [
            (app, "CONFIG_FILE", self.config_file),
            (app, "VOICES_DIR", self.config_file.parent / "voices"),
            (app, "OUTPUT_DIR", self.output_dir),
        ]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        db = mock.patch.dict(os.environ, {"VIDEO_GEN_DB": str(tmp / "orchestrator.sqlite3")})
        db.start()
        self.addCleanup(db.stop)

    def write_config(self, data: dict) -> None:
        self.config_file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _ready_item(qid, title):
    return {"id": qid, "status": "pending", "final_title": title,
            "script_ready": True, "work_dir": f"/tmp/{qid}", "video_job_id": f"job-{qid}"}


def _scriptless_item(qid, title):
    return {"id": qid, "status": "pending", "final_title": title}


class AutoStartReviewModeTests(TempConfigCase):
    """youtube_auto_approve_script OFF: render ready scripts, never write new ones."""

    def setUp(self):
        super().setUp()
        self.write_config({"youtube_auto_start_job": True,
                           "youtube_auto_approve_script": False})

    def test_starts_first_ready_item_skipping_scriptless_ones(self):
        queue = [_scriptless_item("q1", "No script yet"), _ready_item("q2", "Reviewed")]
        started = []
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=queue), \
             mock.patch.object(backend, "_start_queue_item",
                               side_effect=lambda item: started.append(item) or {"ok": True}):
            out = backend._auto_start_best()
        self.assertEqual(out, {"ok": True})
        self.assertEqual([i["id"] for i in started], ["q2"])

    def test_never_generates_scripts(self):
        queue = [_scriptless_item("q1", "No script yet")]
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=queue), \
             mock.patch.object(backend, "_start_queue_item") as start, \
             mock.patch.object(backend.gapp, "_auto_pick_suggestion") as pick:
            out = backend._auto_start_best()
        self.assertIsNone(out)
        start.assert_not_called()
        pick.assert_not_called()

    def test_ready_item_without_job_link_is_not_started(self):
        # script_ready but missing work_dir/video_job_id can't take the direct
        # render path — rendering it would mean generating a script, which the
        # review gate forbids.
        broken = {"id": "q3", "status": "pending", "final_title": "Half-linked",
                  "script_ready": True}
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=[broken]), \
             mock.patch.object(backend, "_start_queue_item") as start:
            out = backend._auto_start_best()
        self.assertIsNone(out)
        start.assert_not_called()

    def test_no_start_while_render_running(self):
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=True), \
             mock.patch.object(backend, "_start_queue_item") as start:
            out = backend._auto_start_best()
        self.assertIsNone(out)
        start.assert_not_called()


class AutoStartApproveModeTests(TempConfigCase):
    """youtube_auto_approve_script ON: next pending item end-to-end; AI ideas opt-in."""

    def _config(self, ai_ideas):
        self.write_config({"youtube_auto_start_job": True,
                           "youtube_auto_approve_script": True,
                           "youtube_auto_ai_ideas": ai_ideas})

    def test_starts_next_pending_item_even_without_script(self):
        self._config(ai_ideas=False)
        queue = [_scriptless_item("q1", "Write me")]
        started = []
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=queue), \
             mock.patch.object(backend, "_start_queue_item",
                               side_effect=lambda it: started.append(it) or {"ok": True}):
            out = backend._auto_start_best()
        self.assertEqual(out, {"ok": True})
        self.assertEqual([i["id"] for i in started], ["q1"])

    def test_empty_queue_without_ai_ideas_does_nothing(self):
        self._config(ai_ideas=False)
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=[]), \
             mock.patch.object(backend.gapp, "_auto_pick_suggestion") as pick, \
             mock.patch.object(backend, "_start_queue_item") as start:
            out = backend._auto_start_best()
        self.assertIsNone(out)
        pick.assert_not_called()
        start.assert_not_called()

    def test_empty_queue_with_ai_ideas_invents_one(self):
        self._config(ai_ideas=True)
        idea = _scriptless_item("idea1", "Invented")
        started = []
        with mock.patch.object(backend.gapp, "_is_job_running", return_value=False), \
             mock.patch.object(backend.yt, "load_queue", return_value=[]), \
             mock.patch.object(backend.gapp, "_auto_pick_suggestion", return_value=idea), \
             mock.patch.object(backend, "_start_queue_item",
                               side_effect=lambda it: started.append(it) or {"ok": True}):
            out = backend._auto_start_best()
        self.assertEqual(out, {"ok": True})
        self.assertEqual([i["id"] for i in started], ["idea1"])


class QueueSortOrderTests(TempConfigCase):
    """queue_sort_order (the Queue page sort) drives which pending item starts
    next — the persisted sort IS the consumption order, not just a view."""

    def _config(self, sort_order, approve=True):
        self.write_config({"youtube_auto_start_job": True,
                           "youtube_auto_approve_script": approve,
                           "youtube_auto_ai_ideas": False,
                           "queue_sort_order": sort_order})

    def _started_id(self, queue, extra=()):
        started = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                backend.gapp, "_is_job_running", return_value=False))
            stack.enter_context(mock.patch.object(
                backend.yt, "load_queue", return_value=queue))
            stack.enter_context(mock.patch.object(
                backend, "_start_queue_item",
                side_effect=lambda it: started.append(it) or {"ok": True}))
            for target, attr, kw in extra:
                stack.enter_context(mock.patch.object(target, attr, **kw))
            backend._auto_start_best()
        return started[0]["id"] if started else None

    def test_default_queue_order_keeps_file_order(self):
        self._config("queue")
        a = dict(_scriptless_item("q1", "First"), created_at=100)
        b = dict(_scriptless_item("q2", "Second"), created_at=200)
        self.assertEqual(self._started_id([a, b]), "q1")

    def test_newest_starts_most_recent(self):
        self._config("newest")
        a = dict(_scriptless_item("q1", "Old"), created_at=100)
        b = dict(_scriptless_item("q2", "New"), created_at=200)
        self.assertEqual(self._started_id([a, b]), "q2")

    def test_oldest_starts_least_recent(self):
        self._config("oldest")
        a = dict(_scriptless_item("q1", "New"), created_at=200)
        b = dict(_scriptless_item("q2", "Old"), created_at=100)
        self.assertEqual(self._started_id([a, b]), "q2")

    def test_interest_starts_highest_missing_ranks_last(self):
        self._config("interest")
        a = _scriptless_item("q1", "No score")              # missing → -1
        b = dict(_scriptless_item("q2", "Mild"), interestingness=0.4)
        c = dict(_scriptless_item("q3", "Hot"), interestingness=0.9)
        self.assertEqual(self._started_id([a, b, c]), "q3")

    def test_views_starts_highest_prediction(self):
        self._config("views")
        a = _scriptless_item("q1", "Few views")
        b = _scriptless_item("q2", "Many views")
        preds = {"Few views": {"available": True, "predicted_views": 10},
                 "Many views": {"available": True, "predicted_views": 5000}}
        extra = [(backend.eng, "predict",
                  {"side_effect": lambda title, *args, **kw: preds[title]})]
        self.assertEqual(self._started_id([a, b], extra=extra), "q2")

    def test_views_without_model_keeps_queue_order(self):
        self._config("views")
        a = _scriptless_item("q1", "First")
        b = _scriptless_item("q2", "Second")
        extra = [(backend.eng, "predict", {"return_value": {"available": False}})]
        self.assertEqual(self._started_id([a, b], extra=extra), "q1")

    def test_fastest_starts_smallest_estimate(self):
        self._config("fastest")
        a = _scriptless_item("q1", "Slow")
        b = _scriptless_item("q2", "Quick")
        est = {"q1": 3600, "q2": 60}

        def stamp(pending):
            for it in pending:
                it["est_seconds"] = est[it["id"]]
        extra = [(backend, "_attach_render_estimates", {"side_effect": stamp})]
        self.assertEqual(self._started_id([a, b], extra=extra), "q2")

    def test_unknown_sort_value_falls_back_to_file_order(self):
        self._config("bogus")
        a = dict(_scriptless_item("q1", "First"), created_at=100)
        b = dict(_scriptless_item("q2", "Second"), created_at=200)
        self.assertEqual(self._started_id([a, b]), "q1")

    def test_review_mode_picks_first_ready_item_in_sorted_order(self):
        # Sort says q3 (newest) first, but it has no script; the newest READY
        # item (q2) must start — never the file-order one (q1).
        self._config("newest", approve=False)
        a = dict(_ready_item("q1", "Old ready"), created_at=100)
        b = dict(_ready_item("q2", "New ready"), created_at=200)
        c = dict(_scriptless_item("q3", "Newest no script"), created_at=300)
        self.assertEqual(self._started_id([a, b, c]), "q2")


class AiIdeasSeedingTests(TempConfigCase):
    """youtube_auto_ai_ideas inherits the old auto_approve_script behaviour."""

    def test_seeded_true_from_pre_split_fully_automated_config(self):
        self.write_config({"youtube_auto_approve_script": True})
        self.assertTrue(app.load_config()["youtube_auto_ai_ideas"])

    def test_seeded_false_when_approve_script_off(self):
        self.write_config({"youtube_auto_approve_script": False})
        self.assertFalse(app.load_config()["youtube_auto_ai_ideas"])

    def test_explicit_value_wins_over_seed(self):
        self.write_config({"youtube_auto_approve_script": True,
                           "youtube_auto_ai_ideas": False})
        self.assertFalse(app.load_config()["youtube_auto_ai_ideas"])

    def test_fresh_install_defaults_off(self):
        self.assertFalse(app.load_config()["youtube_auto_ai_ideas"])


if __name__ == "__main__":
    unittest.main()
