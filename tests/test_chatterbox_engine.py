"""Chatterbox Multilingual TTS engine (issue #176).

The chatterbox-multilingual engine is a second inference backend (not an
F5 checkpoint swap): the worker runs `python -m pipeline.chatterbox` and the
per-style tts_language picks which of its 23 languages is spoken. These tests
cover the registry entry, the language plumbing through both transports, and
the config-side language normalization — without invoking real TTS.
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import chatterbox, tts_engines, tts_worker


class RegistryTests(unittest.TestCase):
    def test_engine_registered_and_commercial_safe(self):
        e = tts_engines.get("chatterbox-multilingual")
        self.assertIsNotNone(e)
        self.assertEqual(e["license"], "MIT")
        self.assertTrue(e["commercial_ok"])

    def test_backend_dispatch(self):
        self.assertEqual(tts_engines.backend("chatterbox-multilingual"), "chatterbox")
        self.assertEqual(tts_engines.backend("openf5"), "f5")
        # Unknown keys fall back to the default engine's backend.
        self.assertEqual(tts_engines.backend("nope"), "f5")

    def test_default_engine_unchanged(self):
        # The default must stay the commercial-safe English model.
        self.assertEqual(tts_engines.DEFAULT_TTS_ENGINE, "openf5")

    def test_public_list_carries_languages(self):
        by_key = {e["key"]: e for e in tts_engines.public_list()}
        self.assertEqual(len(by_key["chatterbox-multilingual"]["languages"]), 23)
        self.assertEqual(by_key["openf5"]["languages"], {})

    def test_cli_args_rejects_non_f5_backend(self):
        with self.assertRaises(ValueError):
            tts_engines.cli_args("chatterbox-multilingual")


class LanguageNormTests(unittest.TestCase):
    def test_supported_codes_pass_through(self):
        self.assertEqual(chatterbox.norm_language("pt"), "pt")
        self.assertEqual(chatterbox.norm_language(" PT "), "pt")

    def test_unknown_falls_back_to_english(self):
        for bad in ("", None, "xx", "portuguese"):
            self.assertEqual(chatterbox.norm_language(bad), "en")


class TransportTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        f.write(b"RIFFfake-reference")
        f.close()
        self.ref = Path(f.name)
        self.out = Path(tempfile.mkdtemp()) / "out.wav"

    def test_http_payload_carries_engine_and_language(self):
        captured = {}

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return FakeResp(b"WAVDATA")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            tts_worker.generate_narration(
                "olá", self.out, reference_wav=self.ref, host="http://s1:8189",
                tts_engine="chatterbox-multilingual", language="pt")

        body = json.loads(captured["body"])
        self.assertEqual(body["engine"], "chatterbox-multilingual")
        self.assertEqual(body["language"], "pt")

    def test_local_chatterbox_runs_its_own_cli(self):
        with mock.patch.object(tts_worker.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stderr="")
            tts_worker._f5_local("olá", self.ref, self.out, 1.0,
                                 tts_engine="chatterbox-multilingual", language="pt")
        cmd = run.call_args[0][0]
        self.assertIn("pipeline.chatterbox", cmd)
        self.assertNotIn("f5_tts.infer.infer_cli", cmd)
        self.assertEqual(cmd[cmd.index("--language") + 1], "pt")

    def test_local_f5_engines_keep_f5_cli(self):
        with mock.patch.object(tts_worker.subprocess, "run") as run, \
             mock.patch.object(tts_engines, "cli_args", return_value=["--model", "X"]):
            run.return_value = mock.Mock(returncode=0, stderr="")
            tts_worker._f5_local("hello", self.ref, self.out, 1.0,
                                 tts_engine="openf5", language="pt")
        cmd = run.call_args[0][0]
        self.assertIn("f5_tts.infer.infer_cli", cmd)
        self.assertNotIn("--language", cmd)


class ConfigTests(unittest.TestCase):
    def test_style_language_normalization(self):
        import app
        self.assertEqual(app._norm_tts_language("pt"), "pt")
        self.assertEqual(app._norm_tts_language("bogus"), "en")
        self.assertEqual(app._norm_tts_language(None), "en")
        self.assertEqual(app._norm_tts_language(123), "en")

    def test_language_is_a_style_field_with_default(self):
        import app
        self.assertEqual(app.STYLE_FIELD_TO_FLAT.get("tts_language"), "default_tts_language")
        self.assertEqual(app.DEFAULT_CFG.get("default_tts_language"), "en")


if __name__ == "__main__":
    unittest.main()
