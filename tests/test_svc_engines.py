"""The selected singing converter survives local, worker and fallback paths."""
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import svc


def _success():
    return subprocess.CompletedProcess([], 0, stdout="", stderr="")


def _host_command(command):
    """Decode a captured host command, leaving file-copy argv alone."""
    return shlex.split(command[-1]) if command[0] in ("ssh", "bash") else command


class ControllerConversionTests(unittest.TestCase):
    def test_default_soulx_and_explicit_seed_convert_only_vocals(self):
        for options, engine, default_steps in (
            ({}, "soulx-svc", 32),
            ({"engine": "seed-vc"}, "seed-vc", 30),
        ):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                source, reference = root / "song.wav", root / "voice.wav"
                source.write_bytes(b"original mix")
                reference.write_bytes(b"reference")
                commands = []

                def separate(song, work):
                    self.assertEqual(song, source)
                    vocals, backing = work / "vocals.wav", work / "backing.wav"
                    vocals.write_bytes(b"original vocals")
                    backing.write_bytes(b"unchanged instruments")
                    return vocals, backing

                def run(command, **kwargs):
                    commands.append((command, kwargs))
                    self.assertEqual(
                        Path(command[command.index("--source") + 1]).read_bytes(),
                        b"original vocals",
                    )
                    self.assertEqual(command[command.index("--target") + 1], str(reference.resolve()))
                    output_dir = Path(command[command.index("--output") + 1])
                    (output_dir / "converted.wav").write_bytes(b"converted vocals")
                    return _success()

                def remix(vocals, backing, output):
                    output.write_bytes(vocals.read_bytes() + b" + " + backing.read_bytes())

                with mock.patch.object(svc, "available", return_value=True), \
                     mock.patch.object(svc, "_separate_stems", side_effect=separate), \
                     mock.patch.object(svc, "_mean_volume", return_value=-18), \
                     mock.patch.object(svc, "_match_gain") as gain, \
                     mock.patch.object(svc, "_remix", side_effect=remix), \
                     mock.patch.object(svc.subprocess, "run", side_effect=run):
                    output = root / "out.wav"
                    self.assertEqual(svc.convert_song(source, reference, output, **options), output)

                self.assertEqual(output.read_bytes(), b"converted vocals + unchanged instruments")
                self.assertEqual(source.read_bytes(), b"original mix")
                self.assertEqual(gain.call_args.kwargs["to"], -18)
                self.assertEqual(len(commands), 1)
                command, kwargs = commands[0]
                model_dir = svc.SOULX_DIR if engine == "soulx-svc" else svc.SVC_DIR
                self.assertEqual(command[0], str(model_dir / ".venv/bin/python"))
                self.assertEqual(kwargs["cwd"], model_dir)
                self.assertEqual(command[command.index("--diffusion-steps") + 1], str(default_steps))
                if engine == "soulx-svc":
                    self.assertIn(str(svc.SOULX_RUNNER), command)
                    self.assertEqual(command[command.index("--model-dir") + 1], str(model_dir))
                    self.assertNotIn("--f0-condition", command)
                else:
                    self.assertIn(str(model_dir / "inference.py"), command)
                    self.assertEqual(command[command.index("--f0-condition") + 1], "True")
                    self.assertEqual(command[command.index("--length-adjust") + 1], "1.0")

    def test_explicit_diffusion_steps_reach_each_converter(self):
        for engine in ("soulx-svc", "seed-vc"):
            with self.subTest(engine=engine), \
                 mock.patch.object(svc, "available", return_value=True), \
                 mock.patch.object(svc, "_separate_stems", return_value=(Path("vocals"), Path("backing"))), \
                 mock.patch.object(svc, "_mean_volume", return_value=-18), \
                 mock.patch.object(svc, "_match_gain"), \
                 mock.patch.object(svc, "_remix"):
                observed = []

                def convert(source, reference, output, steps, timeout, **kwargs):
                    observed.append((steps, kwargs["engine"]))

                with mock.patch.object(svc, "_convert", side_effect=convert):
                    svc.convert_song(Path("song"), Path("voice"), Path("output"),
                                     diffusion_steps=47, engine=engine)
                self.assertEqual(observed, [(47, engine)])

    def test_soulx_requires_stems_instead_of_converting_instruments(self):
        with mock.patch.object(svc, "available", return_value=True), \
             mock.patch.object(svc, "_separate_stems", return_value=None), \
             mock.patch.object(svc, "_convert_anywhere") as convert:
            with self.assertRaisesRegex(RuntimeError, "SoulX-SVC requires Demucs"):
                svc.convert_song(Path("song"), Path("voice"), Path("output"))
            convert.assert_not_called()


class WorkerConversionTests(unittest.TestCase):
    def test_failed_and_busy_workers_keep_the_selected_engine_on_controller_fallback(self):
        for engine in ("soulx-svc", "seed-vc"):
            with self.subTest(engine=engine):
                events = []

                def lease(worker):
                    if worker == "http://busy:8188":
                        return None
                    return lambda: events.append(("release", worker))

                def remote(host, *args, **kwargs):
                    events.append(("remote", host, kwargs["engine"]))
                    raise RuntimeError("worker inference failed")

                def local(*args, **kwargs):
                    events.append(("local", kwargs["engine"]))

                with mock.patch("pipeline.worker_pool.try_worker_lease", side_effect=lease), \
                     mock.patch.object(svc, "_convert_remote", side_effect=remote), \
                     mock.patch.object(svc, "_convert", side_effect=local):
                    svc._convert_anywhere(Path("song"), Path("voice"), Path("out"), 32, 60,
                                          ["http://first:8188", "http://busy:8188", "http://last:8188"],
                                          engine=engine)
                self.assertEqual(events, [
                    ("remote", "first", engine), ("release", "http://first:8188"),
                    ("remote", "last", engine), ("release", "http://last:8188"),
                    ("local", engine),
                ])

    def test_successful_worker_releases_lease_without_converting_again(self):
        release = mock.Mock()
        with mock.patch("pipeline.worker_pool.try_worker_lease", return_value=release) as lease, \
             mock.patch.object(svc, "_convert_remote") as remote, \
             mock.patch.object(svc, "_convert") as local:
            svc._convert_anywhere(Path("song"), Path("voice"), Path("out"), 30, 60,
                                  ["http://first:8188", "http://second:8188"], engine="seed-vc")
        self.assertEqual(remote.call_args.kwargs["engine"], "seed-vc")
        self.assertEqual(lease.call_count, 1)
        release.assert_called_once_with()
        local.assert_not_called()

    def test_remote_engines_use_distinct_safe_jobs_and_correct_cli(self):
        source = Path("/songs/voice';$(touch injected).wav")
        reference = Path("/voices/person `touch other`.wav")
        job_sources = []
        for engine, model_dir in (("soulx-svc", "/opt/soulx-singer"), ("seed-vc", "/opt/seed-vc")):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as td:
                calls = []

                def run(command, **kwargs):
                    calls.append(command)
                    return _success()

                with mock.patch.object(svc.subprocess, "run", side_effect=run):
                    svc._convert_remote("worker", source, reference, Path(td) / "out.wav", 32, 60,
                                        engine=engine)
                remote_commands = [_host_command(command) for command in calls if command[0] == "ssh"]
                inference = next(command for command in remote_commands if "--diffusion-steps" in command)
                self.assertIn(model_dir + "/.venv/bin/python", inference)
                staged_source = inference[inference.index("--source") + 1]
                self.assertRegex(staged_source, r"^/tmp/svc_[0-9a-f]{32}_src\.wav$")
                job_sources.append(staged_source)
                self.assertTrue(any(str(source) in command for command in calls if command[0] == "scp"))
                self.assertTrue(any(str(reference) in command for command in calls if command[0] == "scp"))
                for command in calls:
                    if command[0] == "ssh":
                        self.assertNotIn(source.name, command[-1])
                        self.assertNotIn(reference.name, command[-1])
                if engine == "soulx-svc":
                    self.assertEqual(inference[inference.index("--model-dir") + 1], model_dir)
                    self.assertTrue(any(str(svc.SOULX_RUNNER) in command for command in calls))
                    self.assertNotIn("--f0-condition", inference)
                else:
                    self.assertIn(model_dir + "/inference.py", inference)
                    self.assertIn("--f0-condition", inference)
                    self.assertFalse(any(str(svc.SOULX_RUNNER) in command for command in calls))
        self.assertEqual(len(set(job_sources)), 2)

    def test_remote_failure_cleans_host_and_container_inputs(self):
        for engine in ("soulx-svc", "seed-vc"):
            with self.subTest(engine=engine):
                commands = []

                def run(command, **kwargs):
                    args = _host_command(command)
                    commands.append(args)
                    if "--diffusion-steps" in args:
                        return subprocess.CompletedProcess(command, 1, "", "GPU failed")
                    return _success()

                with mock.patch.object(svc.subprocess, "run", side_effect=run):
                    with self.assertRaisesRegex(RuntimeError, "GPU failed"):
                        svc._convert_remote("worker", Path("song.wav"), Path("voice.wav"),
                                            Path("out.wav"), 32, 60, engine=engine)
                inference = next(command for command in commands if "--source" in command)
                src = inference[inference.index("--source") + 1]
                ref = inference[inference.index("--target") + 1]
                out_dir = inference[inference.index("--output") + 1]
                host_cleanup = next(command for command in commands if command[0] == "rm")
                container_cleanup = next(command for command in commands
                                         if command[0] == "docker" and "rm" in command)
                for path in (src, ref):
                    self.assertIn(path, host_cleanup)
                    self.assertIn(path, container_cleanup)
                self.assertIn(out_dir, container_cleanup)
                if engine == "soulx-svc":
                    runner = next(arg for arg in inference if arg.endswith("_runner.py"))
                    self.assertIn(runner, host_cleanup)
                    self.assertIn(runner, container_cleanup)
                self.assertEqual(commands[-2:], [host_cleanup, container_cleanup])


class ConverterAvailabilityTests(unittest.TestCase):
    def install_soulx_fixture(self, root, runner):
        revision = "a" * 40
        cache = root / "hf-cache/hub/models--openai--whisper-base"
        files = {
            root / ".venv/bin/python": b"",
            root / "soulxsinger/models/soulxsinger_svc.py": b"",
            root / "pretrained_models/SoulX-Singer/model-svc.pt": b"",
            root / "pretrained_models/SoulX-Singer-Preprocess/rmvpe/rmvpe.pt": b"",
            runner: b"",
            root / "spielbot-models.json": json.dumps({"openai/whisper-base": revision}).encode(),
            cache / "refs/main": revision.encode(),
        }
        for name in ("config.json", "preprocessor_config.json", "model.safetensors"):
            files[cache / "snapshots" / revision / name] = b""
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return files

    def test_installation_checks_are_isolated_to_the_selected_engine(self):
        with tempfile.TemporaryDirectory() as td:
            seed_dir, soulx_dir = Path(td) / "seed-vc", Path(td) / "soulx-singer"
            runner = Path(td) / "soulx_svc.py"
            seed_files = [seed_dir / "inference.py", seed_dir / ".venv/bin/python"]
            with mock.patch.object(svc, "SVC_DIR", seed_dir), \
                 mock.patch.object(svc, "SOULX_DIR", soulx_dir), \
                 mock.patch.object(svc, "SOULX_RUNNER", runner):
                self.assertFalse(svc.available())
                for path in seed_files:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                self.assertTrue(svc.available("seed-vc"))
                self.assertFalse(svc.available())
                soulx_files = self.install_soulx_fixture(soulx_dir, runner)
                self.assertTrue(svc.available())
                seed_files[0].unlink()
                self.assertFalse(svc.available("seed-vc"))
                self.assertTrue(svc.available("soulx-svc"))
                for missing, content in soulx_files.items():
                    with self.subTest(missing=missing.relative_to(td)):
                        missing.unlink()
                        self.assertFalse(svc.available("soulx-svc"))
                        missing.write_bytes(content)

    def test_interrupted_whisper_prefetch_is_not_available(self):
        with tempfile.TemporaryDirectory() as td:
            root, runner = Path(td) / "soulx", Path(td) / "runner.py"
            self.install_soulx_fixture(root, runner)
            manifest = root / "spielbot-models.json"
            reference = root / "hf-cache/hub/models--openai--whisper-base/refs/main"
            with mock.patch.object(svc, "SOULX_DIR", root), \
                 mock.patch.object(svc, "SOULX_RUNNER", runner):
                self.assertTrue(svc.available())
                original = manifest.read_text()
                for unfinished in ('{"openai/whisper-base":', '{}', '{"openai/whisper-base": null}'):
                    with self.subTest(manifest=unfinished):
                        manifest.write_text(unfinished)
                        self.assertFalse(svc.available())
                manifest.write_text(original)
                # Downloaded weights alone are insufficient: offline main must
                # resolve to the complete snapshot recorded by the installer.
                reference.write_text("b" * 40)
                self.assertFalse(svc.available())
