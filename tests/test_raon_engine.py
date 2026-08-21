"""Raon-OpenTTS-1B narration engine (KRAFTON).

Raon is a third inference backend, not an F5 checkpoint swap: KRAFTON's fork
installs its own `f5_tts` package, so it lives in a separate virtualenv and the
worker shells out to `python -m pipeline.raon` on that interpreter. These tests
cover the registry entry (including the CC-BY-NC flag that keeps it out of the
default slot), the two-repo download bookkeeping, backend dispatch across both
transports, and the guard that fires when the virtualenv isn't installed —
without invoking real TTS.
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import raon, tts_engines, tts_worker

ENGINE = "raon-opentts-1b"


class RegistryTests(unittest.TestCase):
    def test_engine_registered_and_flagged_non_commercial(self):
        e = tts_engines.get(ENGINE)
        self.assertIsNotNone(e)
        self.assertEqual(e["license"], "CC-BY-NC-4.0")
        # The weights are CC-BY-NC, so the UI must show the non-commercial chip.
        self.assertFalse(e["commercial_ok"])

    def test_default_engine_unchanged(self):
        # Narration goes onto monetized channels: the default must stay
        # commercial-safe, so a non-commercial engine may never take that slot.
        self.assertEqual(tts_engines.DEFAULT_TTS_ENGINE, "openf5")
        self.assertNotEqual(tts_engines.DEFAULT_TTS_ENGINE, ENGINE)

    def test_backend_dispatch(self):
        self.assertEqual(tts_engines.backend(ENGINE), "raon")
        self.assertEqual(tts_engines.backend("openf5"), "f5")

    def test_english_only_so_no_language_picker(self):
        by_key = {e["key"]: e for e in tts_engines.public_list()}
        self.assertEqual(by_key[ENGINE]["languages"], {})

    def test_cli_args_rejects_non_f5_backend(self):
        with self.assertRaises(ValueError):
            tts_engines.cli_args(ENGINE)


class DownloadSpecTests(unittest.TestCase):
    def test_vocoder_repo_is_part_of_the_download(self):
        # The vocoder weights live in a second repo, so pre-warm and the cached
        # check must cover both or a "cached" worker still stalls on first use.
        pairs = tts_engines._downloads(tts_engines.get(ENGINE))
        self.assertIn((raon.RAON_REPO, "model_520000.pt"), pairs)
        self.assertIn((raon.RAON_VOCODER_REPO, "generator.ckpt"), pairs)

    def test_single_repo_engines_unaffected(self):
        pairs = tts_engines._downloads(tts_engines.get("openf5"))
        self.assertEqual({repo for repo, _ in pairs}, {tts_engines.OPENF5_REPO})

    def test_is_cached_needs_the_vocoder_too(self):
        def only_the_model(repo, filename):
            return "/cache/f" if repo == raon.RAON_REPO else None

        with mock.patch("huggingface_hub.try_to_load_from_cache", side_effect=only_the_model):
            self.assertFalse(tts_engines.is_cached(ENGINE))

        with mock.patch("huggingface_hub.try_to_load_from_cache", return_value="/cache/f"):
            self.assertTrue(tts_engines.is_cached(ENGINE))


class TransportTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        f.write(b"RIFFfake-reference")
        f.close()
        self.ref = Path(f.name)
        self.out = Path(tempfile.mkdtemp()) / "out.wav"

    def test_http_payload_carries_the_engine(self):
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
                "hello", self.out, reference_wav=self.ref, host="http://s1:8189",
                tts_engine=ENGINE)

        self.assertEqual(json.loads(captured["body"])["engine"], ENGINE)

    def test_local_raon_runs_its_own_cli_on_its_own_interpreter(self):
        with mock.patch.object(tts_worker.subprocess, "run") as run, \
             mock.patch.object(raon, "available", return_value=True), \
             mock.patch.object(raon, "RAON_PYTHON", "/opt/raon/bin/python"):
            run.return_value = mock.Mock(returncode=0, stderr="")
            tts_worker._f5_local("hello", self.ref, self.out, 1.3, tts_engine=ENGINE)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "/opt/raon/bin/python")
        self.assertIn("pipeline.raon", cmd)
        # Sharing the f5tts interpreter would import the WRONG f5_tts package.
        self.assertNotIn("f5_tts.infer.infer_cli", cmd)
        self.assertNotIn(tts_worker._LOCAL_PYTHON, cmd)
        self.assertEqual(cmd[cmd.index("--speed") + 1], "1.3")
        # English-only: no language flag to pass.
        self.assertNotIn("--language", cmd)

    def test_missing_virtualenv_says_how_to_install_it(self):
        with mock.patch.object(raon, "available", return_value=False), \
             mock.patch.object(tts_worker.subprocess, "run") as run:
            with self.assertRaises(RuntimeError) as ctx:
                tts_worker._f5_local("hello", self.ref, self.out, 1.0, tts_engine=ENGINE)
        run.assert_not_called()
        self.assertIn("INSTALL_RAON=1", str(ctx.exception))


class AvailabilityTests(unittest.TestCase):
    def test_available_follows_the_interpreter_path(self):
        with mock.patch.object(raon, "RAON_PYTHON", "/definitely/not/here/python"):
            self.assertFalse(raon.available())
        import sys
        with mock.patch.object(raon, "RAON_PYTHON", sys.executable):
            self.assertTrue(raon.available())


if __name__ == "__main__":
    unittest.main()
