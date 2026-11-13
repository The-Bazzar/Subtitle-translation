import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from subtitle_translation import cli, mfa_setup
from subtitle_translation import mfa_prefetch
from subtitle_translation.artifacts import atomic_write_json
from subtitle_translation.mfa import vad_cache_fingerprint


class MfaSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_windows_host_is_native_without_wsl_or_shell_initialization(self):
        with patch.object(mfa_setup.platform, "system", return_value="Windows"), \
             patch.object(mfa_setup.platform, "machine", return_value="AMD64"), \
             patch.dict(mfa_setup.os.environ, {"LOCALAPPDATA": str(self.root)}), \
             patch.object(mfa_setup, "capture_command") as capture:
            host = mfa_setup.ToolHost()
        self.assertTrue(host.windows)
        self.assertEqual(host.prefix, [])
        self.assertEqual(host.root, self.root / "subtitle-translation")
        capture.assert_not_called()
        command, env = host.execution(["tool.exe", "path with spaces"], {"HF_HOME": "cache path"})
        self.assertEqual(command, ["tool.exe", "path with spaces"])
        self.assertEqual(env["HF_HOME"], "cache path")

    def test_windows_bootstrap_uses_only_the_pinned_executable_member(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:bz2") as file:
            item = tarfile.TarInfo("Library/bin/micromamba.exe")
            item.size = 2
            file.addfile(item, io.BytesIO(b"MZ"))
        destination = self.root / "micromamba.exe"
        with patch.object(mfa_setup.urllib.request, "urlopen", return_value=io.BytesIO(archive.getvalue())) as download:
            mfa_setup.install_micromamba(destination, windows=True)
        self.assertEqual(destination.read_bytes(), b"MZ")
        download.assert_called_once_with(mfa_setup.WINDOWS_MICROMAMBA_URL, timeout=60)

    def test_explicit_distribution_preserves_windows_wsl_backend(self):
        with patch.object(mfa_setup.platform, "system", return_value="Windows"), \
             patch.object(mfa_setup.shutil, "which", return_value="wsl.exe"), \
             patch.object(mfa_setup, "capture_command", side_effect=[
                 SimpleNamespace(returncode=0, stdout="/home/test\n"),
                 SimpleNamespace(returncode=0, stdout="x86_64\n")]):
            host = mfa_setup.ToolHost("Debian")
        self.assertFalse(host.windows)
        self.assertEqual(host.prefix, ["wsl.exe", "--distribution", "Debian", "--exec"])

    def test_native_windows_setup_registers_native_runtime_and_cpu_wheels(self):
        host = self.host()
        host.windows = True
        (host.root / "tools/bin/micromamba").rename(host.root / "tools/bin/micromamba.exe")
        versions = {"torch": "2.8.0+cpu", "torchaudio": "2.8.0+cpu", "speechbrain": "1.0.3", "huggingface_hub": "0.36.0"}
        with patch.object(mfa_setup, "ToolHost", return_value=host), \
             patch.object(mfa_setup, "environment_versions", side_effect=[{**versions, "torch": ""}, versions]):
            result = mfa_setup.install_runtime(self.root)
        config = json.loads(result.read_text(encoding="utf-8"))["mfa"]
        self.assertEqual(config["transport"], "native")
        self.assertEqual(config["distribution"], "")
        self.assertTrue(config["command"][0].endswith("micromamba.exe"))
        commands = [call.args[0] for call in host.run.call_args_list]
        create = next(command for command in commands if "create" in command)
        self.assertIn("kaldi=*=cpu*", create)
        self.assertNotIn("torchaudio=2.8.0=cpu*", create)
        wheels = next(command for command in commands if "torch==2.8.0" in command)
        self.assertIn("torchaudio==2.8.0", wheels)
        self.assertIn("https://download.pytorch.org/whl/cpu", wheels)
        self.assertFalse(any(any(arg in {"/usr/bin/env", "/usr/bin/chmod", "wsl.exe"} for arg in command) for command in commands))

    def test_vad_preload_copies_files_without_windows_symlink_privileges(self):
        loader = Mock()
        strategy = object()
        snapshot = self.root / "snapshot with spaces"
        snapshot.mkdir()
        for name in ("hyperparams.yaml", "model.ckpt", "mean_var_norm.ckpt"):
            (snapshot / name).write_text("fixture", encoding="utf-8")
        pretrainer = Mock()
        hparams = {"modules": {}, "pretrainer": pretrainer}
        download = Mock(return_value=str(snapshot))
        modules = {"huggingface_hub": SimpleNamespace(snapshot_download=download),
                   "hyperpyyaml": SimpleNamespace(load_hyperpyyaml=Mock(return_value=hparams)),
                   "speechbrain.inference.VAD": SimpleNamespace(VAD=loader),
                   "speechbrain.utils.fetching": SimpleNamespace(LocalStrategy=SimpleNamespace(COPY=strategy))}
        with patch.dict(sys.modules, modules), patch.dict(mfa_prefetch.os.environ, {"HF_HOME": str(self.root)}), \
             patch.object(mfa_prefetch, "cache_info", return_value={"schema_version": 1}):
            savedir = self.root / "VAD with spaces"
            mfa_prefetch.main(["--output", str(self.root / "vad.json"), "--savedir", str(savedir)])
        download.assert_called_once_with("speechbrain/vad-crdnn-libriparty", revision="main", local_files_only=True)
        pretrainer.set_collect_in.assert_called_once_with(savedir)
        pretrainer.collect_files.assert_called_once_with(default_source=snapshot.as_posix(), local_strategy=strategy)
        pretrainer.load_collected.assert_called_once_with()
        loader.assert_called_once_with(hparams["modules"], hparams, run_opts={"device": "cpu"})
        self.assertFalse((snapshot / "custom.py").exists())
        self.assertFalse((savedir / "custom.py").exists())

    def test_partial_vad_cache_triggers_download_but_complete_cache_does_not(self):
        partial, complete = self.root / "partial cache", self.root / "complete cache"
        partial.mkdir()
        complete.mkdir()
        for name in ("hyperparams.yaml", "model.ckpt", "mean_var_norm.ckpt"):
            (complete / name).write_bytes(b"fixture")
        download = Mock(side_effect=[str(partial), str(complete)])
        with patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(snapshot_download=download)}):
            self.assertEqual(mfa_prefetch.model_snapshot(), complete)
        self.assertEqual(download.call_count, 2)
        self.assertNotIn("local_files_only", download.call_args.kwargs)

    def test_missing_vad_checkpoint_propagates_and_does_not_publish_manifest(self):
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        (snapshot / "hyperparams.yaml").write_text("fixture", encoding="utf-8")
        pretrainer = Mock()
        pretrainer.collect_files.side_effect = FileNotFoundError("missing checkpoint")
        loader = Mock()
        modules = {"hyperpyyaml": SimpleNamespace(load_hyperpyyaml=Mock(return_value={"modules": {}, "pretrainer": pretrainer})),
                   "speechbrain.inference.VAD": SimpleNamespace(VAD=loader),
                   "speechbrain.utils.fetching": SimpleNamespace(LocalStrategy=SimpleNamespace(COPY="copy"))}
        manifest = self.root / "manifest.json"
        with patch.dict(sys.modules, modules), patch.object(mfa_prefetch, "model_snapshot", return_value=snapshot), \
             self.assertRaises(FileNotFoundError):
            mfa_prefetch.main(["--output", str(manifest), "--savedir", str(self.root / "VAD")])
        self.assertFalse(manifest.exists())
        loader.assert_not_called()

    def test_tool_execution_excludes_inherited_python_and_user_packages(self):
        host = object.__new__(mfa_setup.ToolHost)
        host.prefix = []
        with patch.dict(mfa_setup.os.environ, {"PYTHONPATH": "unrelated packages", "PYTHONHOME": "unrelated Python"}):
            command, env = host.execution(["tool.exe", "script with spaces.py"])
        self.assertEqual(command, ["tool.exe", "script with spaces.py"])
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        host.prefix = ["wsl.exe", "--distribution", "Debian", "--exec"]
        command, _ = host.execution(["python", "script with spaces.py"])
        self.assertIn("PYTHONNOUSERSITE=1", command)
        self.assertEqual(command[-1], "script with spaces.py")

    def test_bootstrap_extracts_only_expected_regular_executable(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:bz2") as file:
            item = tarfile.TarInfo("bin/micromamba")
            item.size = len(b"synthetic executable")
            file.addfile(item, io.BytesIO(b"synthetic executable"))
            extra = tarfile.TarInfo("../../outside")
            extra.size = 1
            file.addfile(extra, io.BytesIO(b"x"))
        destination = self.root / "tools" / "micromamba"
        with patch.object(mfa_setup.urllib.request, "urlopen", return_value=io.BytesIO(archive.getvalue())):
            mfa_setup.install_micromamba(destination)
        self.assertEqual(destination.read_bytes(), b"synthetic executable")
        self.assertEqual(list(destination.parent.iterdir()), [destination])
        self.assertFalse((self.root / "outside").exists())

    def test_bad_bootstrap_does_not_publish_partial_executable(self):
        destination = self.root / "tools" / "micromamba"
        with patch.object(mfa_setup.urllib.request, "urlopen", return_value=io.BytesIO(b"invalid archive")):
            with self.assertRaises(tarfile.TarError):
                mfa_setup.install_micromamba(destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.iterdir()), [])

    def test_existing_micromamba_is_not_redownloaded(self):
        destination = self.root / "micromamba"
        destination.write_bytes(b"existing")
        with patch.object(mfa_setup.urllib.request, "urlopen") as download:
            mfa_setup.install_micromamba(destination)
        download.assert_not_called()

    def test_model_lookup_never_substitutes_another_model(self):
        folder = self.root / "pretrained_models" / "dictionary"
        folder.mkdir(parents=True)
        (folder / "another.dict").write_text("dictionary", encoding="utf-8")
        with self.assertRaises(ValueError):
            mfa_setup.locate_model(self.root, "dictionary", "english_us_mfa")
        model = folder / "english_us_mfa.dict"
        model.write_text("dictionary", encoding="utf-8")
        self.assertEqual(mfa_setup.locate_model(self.root, "dictionary", "english_us_mfa"), model)

    def test_vad_revision_cannot_escape_snapshot_cache(self):
        refs = self.root / "hub/models--speechbrain--vad-crdnn-libriparty/refs"
        refs.mkdir(parents=True)
        (refs / "main").write_text("../../outside", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid MFA VAD revision"):
            mfa_prefetch.cache_info(self.root)

    def host(self):
        root = self.root / "managed"
        host = SimpleNamespace(root=root, prefix=[], distribution="", path=lambda value: Path(value), run=Mock())
        def capture(command):
            if "--version" in command:
                return mfa_setup.MICROMAMBA_VERSION
            if any("m.distributions()" in value for value in command):
                return json.dumps({"torch": "2.8.0", "torchaudio": "2.8.0", "speechbrain": "1.0.3", "huggingface_hub": "0.36.0"})
            return mfa_setup.MFA_VERSION
        host.capture = Mock(side_effect=capture)
        for value in (root / "tools/bin/micromamba", root / "mfa-3.4/bin/mfa"):
            value.parent.mkdir(parents=True, exist_ok=True)
            value.write_text("mock executable", encoding="utf-8")
        for kind, model in (("dictionary", "english_us_mfa.dict"), ("acoustic", "english_mfa.zip")):
            path = root / f"models/english-{mfa_setup.MODEL_VERSION}/pretrained_models" / kind / model
            path.parent.mkdir(parents=True)
            path.write_text("mock resource", encoding="utf-8")
        cache = root / "huggingface/hub/models--speechbrain--vad-crdnn-libriparty"
        (cache / "refs").mkdir(parents=True)
        (cache / "refs/main").write_text("a" * 40, encoding="utf-8")
        snapshot = cache / "snapshots" / ("a" * 40)
        snapshot.mkdir(parents=True)
        (snapshot / "model.ckpt").write_bytes(b"mock model")
        revision, digest = vad_cache_fingerprint(root / "huggingface")
        atomic_write_json(root / "vad_manifest.json", {"schema_version": 1, "speechbrain_version": "1.0.3",
                                                   "revision": revision, "files_sha256": digest})
        return host

    def test_setup_reuses_installed_environment_and_models_and_preserves_other_runtime_entries(self):
        host = self.host()
        destination = self.root / "asr_runtime.json"
        atomic_write_json(destination, {"schema_version": 1, "other_tool": {"keep": True}})
        with patch.object(mfa_setup, "ToolHost", return_value=host):
            result = mfa_setup.install_runtime(self.root)
        payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertTrue(payload["other_tool"]["keep"])
        self.assertEqual(payload["mfa"]["version"], "3.4.3")
        self.assertEqual(payload["mfa"]["languages"], ["en"])
        self.assertFalse(any("create" in call.args[0] for call in host.run.call_args_list))
        self.assertFalse(any("download" in call.args[0] for call in host.run.call_args_list))

    def test_missing_environment_uses_pinned_mfa_and_cpu_kaldi(self):
        host = self.host()
        (host.root / "mfa-3.4/bin/mfa").unlink()
        with patch.object(mfa_setup, "ToolHost", return_value=host):
            mfa_setup.install_runtime(self.root)
        command = next(call.args[0] for call in host.run.call_args_list if "create" in call.args[0])
        self.assertIn("montreal-forced-aligner=3.4.3", command)
        self.assertIn("kaldi=*=cpu*", command)
        self.assertIn("pytorch=2.8.0=cpu*", command)

    def test_failed_model_download_does_not_replace_ready_manifest(self):
        host = self.host()
        (host.root / f"models/english-{mfa_setup.MODEL_VERSION}/pretrained_models/acoustic/english_mfa.zip").unlink()
        destination = self.root / "asr_runtime.json"
        destination.write_text('{"schema_version":1,"original":true}', encoding="utf-8")
        before = destination.read_bytes()
        with patch.object(mfa_setup, "ToolHost", return_value=host), \
             patch.object(mfa_setup, "download_model", side_effect=ValueError("download failed")), self.assertRaises(ValueError):
            mfa_setup.install_runtime(self.root)
        self.assertEqual(destination.read_bytes(), before)

    def test_setup_failure_is_an_error_not_a_successful_cli_result(self):
        with patch.object(mfa_setup, "install_runtime", side_effect=ValueError("failed")):
            self.assertEqual(mfa_setup.main([]), 1)

    def test_cli_dispatch_keeps_setup_in_managed_python(self):
        with patch.object(mfa_setup, "main", return_value=0) as install:
            self.assertEqual(cli.main(["setup-mfa", "--distribution", "Debian"]), 0)
        install.assert_called_once_with(["--distribution", "Debian"])


if __name__ == "__main__":
    unittest.main()
