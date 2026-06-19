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


if __name__ == "__main__":
    unittest.main()
