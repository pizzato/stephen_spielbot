"""Publishing-mode semantics after the queue rework.

The publish queue is the canonical inbox of finished videos. The automation
tick only *releases* from it: scheduled mode spaces releases on each
channel/account cadence and is self-sufficient (no longer gated behind the
immediate auto-post toggles); immediate mode posts the moment a film finishes;
the two are mutually exclusive and schedule wins as a backstop. Publishing a
film manually drops it from the queue.
"""
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
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-publish-mode-")
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


class PublishModeTickTests(TempConfigCase):
    """The tick's publish step picks exactly one release path from the config."""

    def _run_tick(self, config):
        self.write_config(config)
        with mock.patch.object(backend, "_reconcile_queue"), \
             mock.patch.object(backend, "_release_scheduled_publishes", return_value={"r": 1}) as rel, \
             mock.patch.object(backend, "_auto_post_done", return_value=["yt"]) as ytp, \
             mock.patch.object(backend, "_auto_post_x_done", return_value=["x"]) as xp:
            backend._automation_tick()
        return rel, ytp, xp

    def test_scheduled_releases_without_auto_post(self):
        # The bug this rework fixes: scheduling on its own must release, even with
        # both immediate auto-post toggles off.
        rel, ytp, xp = self._run_tick({"publish_schedule_enabled": True})
        rel.assert_called_once()
        ytp.assert_not_called()
        xp.assert_not_called()

    def test_immediate_posts_when_auto_post_on(self):
        rel, ytp, xp = self._run_tick({"youtube_auto_post": True, "x_auto_post": True})
        rel.assert_not_called()
        ytp.assert_called_once()
        xp.assert_called_once()

    def test_schedule_wins_when_both_set(self):
        # Settings makes these mutually exclusive; if a stale config has both on,
        # schedule must win so nothing double-posts.
        rel, ytp, xp = self._run_tick(
            {"publish_schedule_enabled": True, "youtube_auto_post": True})
        rel.assert_called_once()
        ytp.assert_not_called()

    def test_manual_mode_releases_nothing(self):
        rel, ytp, xp = self._run_tick({})
        rel.assert_not_called()
        ytp.assert_not_called()
        xp.assert_not_called()


class DropFromPublishQueueTests(TempConfigCase):
    """A manual publish removes the film's entry; auto/scheduled keep theirs."""

    def setUp(self):
        super().setUp()
        self.pq_path = Path(self._tmp.name) / "publish_queue.json"
        p = mock.patch.object(backend.pq, "PUBLISH_QUEUE_PATH", self.pq_path)
        p.start()
        self.addCleanup(p.stop)

    def test_drop_removes_only_the_named_entry(self):
        backend.pq.add_item("/tmp/wd-a", title="A")
        backend.pq.add_item("/tmp/wd-b", title="B")
        backend._drop_from_publish_queue("/tmp/wd-a")
        self.assertEqual([e["work_dir"] for e in backend.pq.load_queue()], ["/tmp/wd-b"])

    def test_drop_is_noop_for_unqueued_film(self):
        backend.pq.add_item("/tmp/wd-b", title="B")
        backend._drop_from_publish_queue("/tmp/not-queued")  # must not raise
        self.assertEqual(len(backend.pq.load_queue()), 1)


class StickyForceXTests(TempConfigCase):
    """'Publish now' on a both-platforms video must still reach X.

    The YouTube upload starts asynchronously, so X is deferred (it waits for the
    fresh YouTube link in case the video is too long to post natively). A one-shot
    force would then drop X into the cadence-gated backlog and it would never post
    'now'. The force must persist until X is actually released.
    """

    def setUp(self):
        super().setUp()
        self.pq_path = Path(self._tmp.name) / "publish_queue.json"
        for target, attr, value in [(backend.pq, "PUBLISH_QUEUE_PATH", self.pq_path)]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        # _reconcile_publish_queue reads job.json off disk (absent here) and would
        # error our in-memory entries; _film_job_config reads job_config.json.
        for name, ret in [("_reconcile_publish_queue", None), ("_film_job_config", {})]:
            p = mock.patch.object(backend, name, return_value=ret)
            p.start()
            self.addCleanup(p.stop)

    def _entry(self, **over):
        e = {
            "id": "e1", "work_dir": "/tmp/wd1", "title": "T", "source": "manual",
            "created_at": 1.0,
            "youtube": {"enabled": True, "channel": "chan", "status": "pending"},
            "x": {"enabled": True, "account": "acct", "status": "pending"},
        }
        e.update(over)
        return e

    def test_forced_publish_defers_x_and_persists_force(self):
        # YT starts (async upload, not 'done' yet) so X is deferred — but the force
        # is remembered so a later tick can release it.
        self.write_config({
            "youtube_channels": [{"id": "chan", "publish_per_day": 0}],
            "x_accounts": [{"id": "acct", "publish_per_day": 0}],
        })
        backend.pq.save_queue([self._entry()])
        with mock.patch.object(backend, "_claim_and_post_youtube", return_value="yt-task"), \
             mock.patch.object(backend, "_claim_and_post_x", return_value=None) as cpx:
            out = backend._release_scheduled_publishes(force_id="e1")
        self.assertTrue(cpx.call_args.kwargs.get("require_yt_link"))  # waited for link
        q = {e["id"]: e for e in backend.pq.load_queue()}
        self.assertEqual(q["e1"]["youtube"]["status"], "publishing")
        self.assertEqual(q["e1"]["x"]["status"], "pending")
        self.assertTrue(q["e1"]["x"]["force"])  # sticky force persisted
        self.assertEqual(out, {"youtube": ["e1"]})

    def test_sticky_force_releases_x_next_tick_bypassing_cadence(self):
        # State after a forced publish: YT done, X pending+force. A recent X post on
        # the same account means cadence would normally block — force must override.
        import time
        e = self._entry()
        e["youtube"] = {"enabled": True, "channel": "chan", "status": "done",
                        "video_id": "v", "published_at": 100.0}
        e["x"] = {"enabled": True, "account": "acct", "status": "pending", "force": True}
        other = self._entry(id="e0", work_dir="/tmp/wd0", created_at=0.5)
        other["youtube"] = {"enabled": False}
        other["x"] = {"enabled": True, "account": "acct", "status": "done",
                      "tweet_id": "t", "published_at": time.time() - 60}
        backend.pq.save_queue([other, e])
        self.write_config({  # 4/day → 6h spacing; the recent post above blocks cadence
            "youtube_channels": [{"id": "chan", "publish_per_day": 0}],
            "x_accounts": [{"id": "acct", "publish_per_day": 4}],
        })
        with mock.patch.object(backend, "_claim_and_post_youtube", return_value=None), \
             mock.patch.object(backend, "_claim_and_post_x", return_value="x-task") as cpx:
            out = backend._release_scheduled_publishes()  # NO force_id
        cpx.assert_called_once()
        self.assertFalse(cpx.call_args.kwargs.get("require_yt_link"))  # YT done
        q = {e["id"]: e for e in backend.pq.load_queue()}
        self.assertEqual(q["e1"]["x"]["status"], "publishing")
        self.assertFalse(q["e1"]["x"].get("force"))  # cleared on release
        self.assertEqual(out, {"x": ["e1"]})

    def test_forced_x_terminal_failure_does_not_persist_force(self):
        # No YouTube target → no link wait. A None here is terminal, not a deferral,
        # so the force must NOT stick (else it would retry every tick forever).
        self.write_config({
            "youtube_channels": [{"id": "chan", "publish_per_day": 0}],
            "x_accounts": [{"id": "acct", "publish_per_day": 0}],
        })
        e = self._entry()
        e["youtube"] = {"enabled": False}
        backend.pq.save_queue([e])
        with mock.patch.object(backend, "_claim_and_post_x", return_value=None) as cpx:
            backend._release_scheduled_publishes(force_id="e1")
        self.assertFalse(cpx.call_args.kwargs.get("require_yt_link"))
        q = {e["id"]: e for e in backend.pq.load_queue()}
        self.assertFalse(q["e1"]["x"].get("force"))
        self.assertEqual(q["e1"]["x"]["status"], "pending")


class ApprovalGateTests(TempConfigCase):
    """publish_require_approval holds a finished video in the queue until the user
    approves it in the Films tab. Comment requests and 'Publish now' bypass it."""

    def setUp(self):
        super().setUp()
        self.pq_path = Path(self._tmp.name) / "publish_queue.json"
        p = mock.patch.object(backend.pq, "PUBLISH_QUEUE_PATH", self.pq_path)
        p.start()
        self.addCleanup(p.stop)
        for name, ret in [("_reconcile_publish_queue", None), ("_film_job_config", {}),
                          ("_youtube_channel_connected", True)]:
            p = mock.patch.object(backend, name, return_value=ret)
            p.start()
            self.addCleanup(p.stop)

    def _entry(self, **over):
        e = {
            "id": "e1", "work_dir": "/tmp/wd1", "title": "T", "source": "manual",
            "created_at": 1.0,
            "youtube": {"enabled": True, "channel": "chan", "status": "pending"},
            "x": {"enabled": False, "account": "", "status": "skipped"},
        }
        e.update(over)
        return e

    def _cfg(self, **over):
        cfg = {"publish_require_approval": True,
               "youtube_channels": [{"id": "chan", "publish_per_day": 0}],
               "x_accounts": [{"id": "acct", "publish_per_day": 0}]}
        cfg.update(over)
        self.write_config(cfg)

    def test_unapproved_entry_is_held(self):
        self._cfg()
        backend.pq.save_queue([self._entry()])
        with mock.patch.object(backend, "_claim_and_post_youtube", return_value="yt") as cp:
            out = backend._release_scheduled_publishes()
        cp.assert_not_called()
        self.assertEqual(out, {})
        self.assertEqual(backend.pq.load_queue()[0]["youtube"]["status"], "pending")

    def test_approved_entry_releases(self):
        self._cfg()
        backend.pq.save_queue([self._entry(approved=True)])
        with mock.patch.object(backend, "_claim_and_post_youtube", return_value="yt") as cp:
            out = backend._release_scheduled_publishes()
        cp.assert_called_once()
        self.assertEqual(out, {"youtube": ["e1"]})

    def test_comment_request_bypasses_approval(self):
        self._cfg(publish_schedule_skip_comment_requests=True)
        backend.pq.save_queue([self._entry(source="comment")])  # not approved
        with mock.patch.object(backend, "_claim_and_post_youtube", return_value="yt") as cp:
            out = backend._release_scheduled_publishes()
        cp.assert_called_once()
        self.assertEqual(out, {"youtube": ["e1"]})

    def test_publish_now_bypasses_approval(self):
        self._cfg()
        backend.pq.save_queue([self._entry()])  # not approved
        with mock.patch.object(backend, "_claim_and_post_youtube", return_value="yt") as cp:
            out = backend._release_scheduled_publishes(force_id="e1")
        cp.assert_called_once()
        self.assertEqual(out, {"youtube": ["e1"]})

    def test_off_by_default_releases_normally(self):
        self._cfg(publish_require_approval=False)
        backend.pq.save_queue([self._entry()])  # no approved flag
        with mock.patch.object(backend, "_claim_and_post_youtube", return_value="yt") as cp:
            out = backend._release_scheduled_publishes()
        cp.assert_called_once()
        self.assertEqual(out, {"youtube": ["e1"]})


if __name__ == "__main__":
    unittest.main()
