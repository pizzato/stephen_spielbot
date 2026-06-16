"""Settings backup / restore (issue #106).

Covers the config-level machinery: which files land in a full vs operational
backup, junk exclusion, a build→restore round trip, operational restore leaving
settings untouched, manifest validation, and zip-slip rejection.
"""
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import app


# A representative config dir: settings + credentials, operational state, and
# regenerable junk that must never enter a backup.
SETTINGS_FILES = {
    "config.yaml": "music_vol: 18\n",
    "client_secrets.json": '{"installed": {"client_id": "x"}}',
    "youtube_token.json": '{"token": "legacy"}',
    "youtube_token_UC123.json": '{"token": "chan"}',
    "voices/narrator.wav": "RIFFvoice-bytes",
}
OPERATIONAL_FILES = {
    "youtube_queue.json": "[]",
    "youtube_comments.json": "[]",
    "youtube_analytics.json": "{}",
    "youtube_suggestions.json": "[]",
    "youtube_dismissed_suggestions.json": "{}",
    "youtube_daily_uploads.json": "0",
    "last_session.json": "{}",
    "ui_seen.json": "{}",
    "engagement/metrics.json": '{"samples": 20}',
    "engagement/embeddings/UC123.npy": "binary-ish",
}
JUNK_FILES = {
    "voice_test_abc123.wav": "scratch",
    "config.yaml.bak-issue66": "old",
    "youtube_queue.json.bck": "old",
    "config.json.migrated-bak": "old",
    "config.yaml~": "editor backup",
}


class BackupCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-backup-")
        self.addCleanup(self._tmp.cleanup)
        self.cfg_dir = Path(self._tmp.name) / "config"
        self.cfg_dir.mkdir(parents=True)
        p = mock.patch.object(app, "CONFIG_FILE", self.cfg_dir / "config.yaml")
        p.start()
        self.addCleanup(p.stop)

    def _seed(self, *groups):
        for group in groups:
            for rel, body in group.items():
                f = self.cfg_dir / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(body)

    def _names(self, blob):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            return set(zf.namelist())

    # ── what goes in a backup ────────────────────────────────────────────────

    def test_full_backup_has_everything_but_junk(self):
        self._seed(SETTINGS_FILES, OPERATIONAL_FILES, JUNK_FILES)
        blob, fname = app.build_settings_backup("full")
        names = self._names(blob)
        self.assertIn("backup_manifest.json", names)
        for rel in {**SETTINGS_FILES, **OPERATIONAL_FILES}:
            self.assertIn(rel, names)
        for rel in JUNK_FILES:
            self.assertNotIn(rel, names)
        self.assertTrue(fname.startswith("stephen-spielbot-full-"))
        self.assertTrue(fname.endswith(".zip"))

    def test_operational_backup_excludes_settings_and_junk(self):
        self._seed(SETTINGS_FILES, OPERATIONAL_FILES, JUNK_FILES)
        blob, fname = app.build_settings_backup("operational")
        names = self._names(blob)
        for rel in OPERATIONAL_FILES:
            self.assertIn(rel, names)
        for rel in {**SETTINGS_FILES, **JUNK_FILES}:
            self.assertNotIn(rel, names)
        self.assertTrue(fname.startswith("stephen-spielbot-operational-"))

    def test_manifest_records_scope_and_files(self):
        self._seed(SETTINGS_FILES, OPERATIONAL_FILES)
        blob, _ = app.build_settings_backup("operational")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            manifest = json.loads(zf.read("backup_manifest.json"))
        self.assertEqual(manifest["app"], "stephen-spielbot")
        self.assertEqual(manifest["scope"], "operational")
        self.assertEqual(set(manifest["files"]), set(OPERATIONAL_FILES))

    def test_invalid_scope_rejected(self):
        with self.assertRaises(ValueError):
            app.build_settings_backup("everything")

    # ── round trip ───────────────────────────────────────────────────────────

    def test_full_round_trip_restores_bytes(self):
        self._seed(SETTINGS_FILES, OPERATIONAL_FILES, JUNK_FILES)
        blob, _ = app.build_settings_backup("full")
        # Wipe the dir, then restore onto the empty config dir.
        for child in list(self.cfg_dir.iterdir()):
            if child.is_dir():
                import shutil
                shutil.rmtree(child)
            else:
                child.unlink()
        result = app.restore_settings_backup(blob)
        self.assertEqual(result["scope"], "full")
        for rel, body in {**SETTINGS_FILES, **OPERATIONAL_FILES}.items():
            self.assertEqual((self.cfg_dir / rel).read_text(), body)
        # Junk was never backed up, so it does not reappear.
        for rel in JUNK_FILES:
            self.assertFalse((self.cfg_dir / rel).exists())

    def test_operational_restore_leaves_settings_untouched(self):
        self._seed(SETTINGS_FILES, OPERATIONAL_FILES)
        op_blob, _ = app.build_settings_backup("operational")
        # Simulate a fresh machine: different settings already in place, no state.
        (self.cfg_dir / "config.yaml").write_text("music_vol: 99\n")
        (self.cfg_dir / "youtube_queue.json").unlink()
        app.restore_settings_backup(op_blob)
        # Settings on disk are preserved (not in the operational backup)...
        self.assertEqual((self.cfg_dir / "config.yaml").read_text(), "music_vol: 99\n")
        # ...while operational state comes back.
        self.assertEqual((self.cfg_dir / "youtube_queue.json").read_text(), "[]")

    # ── validation / safety ──────────────────────────────────────────────────

    def test_restore_rejects_non_zip(self):
        with self.assertRaises(ValueError):
            app.restore_settings_backup(b"not a zip")

    def test_restore_requires_manifest(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("config.yaml", "music_vol: 1\n")
        with self.assertRaises(ValueError):
            app.restore_settings_backup(buf.getvalue())

    def test_restore_rejects_foreign_app(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("backup_manifest.json", json.dumps({"app": "some-other-tool"}))
            zf.writestr("config.yaml", "x")
        with self.assertRaises(ValueError):
            app.restore_settings_backup(buf.getvalue())

    def test_restore_rejects_zip_slip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("backup_manifest.json",
                        json.dumps({"app": "stephen-spielbot", "scope": "full"}))
            zf.writestr("../escape.txt", "pwned")
        with self.assertRaises(ValueError):
            app.restore_settings_backup(buf.getvalue())
        # Nothing was written outside the config dir.
        self.assertFalse((self.cfg_dir.parent / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
