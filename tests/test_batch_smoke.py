import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
WSL = shutil.which("wsl.exe") or shutil.which("wsl")
BASH = shutil.which("bash")

try:
    from tests.fixtures import production_batch_smoke
except ImportError:
    production_batch_smoke = None


EXPECTED_STAGES = [
    "download",
    "prepare",
    "extract_audio",
    "asr",
    "alignment",
    "beautify",
    "glossary",
    "translate",
    "burn",
]


class ProductionBatchSmokeMixin:
    platform_name = ""
    wrapper_chain = ""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="production-batch-smoke-",
            dir=ROOT,
        )
        self.sandbox = pathlib.Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_smoke(self):
        raise NotImplementedError

    def assert_serial_intervals(self, intervals, label):
        ordered = sorted(intervals, key=lambda interval: interval["start_ns"])
        self.assertEqual(len(ordered), 6, label)
        for interval in ordered:
            self.assertLess(interval["start_ns"], interval["end_ns"], interval)
        for previous, current in zip(ordered, ordered[1:]):
            self.assertLessEqual(previous["end_ns"], current["start_ns"], label)

    def test_production_cli_with_fake_external_boundaries(self):
        self.assertIsNotNone(
            production_batch_smoke,
            "production batch smoke support is missing",
        )
        evidence = self.run_smoke()

        self.assertEqual(evidence["platform"], self.platform_name)
        self.assertEqual(evidence["wrapper_chain"], self.wrapper_chain)
        self.assertEqual(
            evidence["scope"],
            "production batch CLI with fake external boundaries",
        )
        self.assertEqual(evidence["exit_code"], 0)
        self.assertEqual(evidence["production_hashes"], evidence["copied_hashes"])
        self.assertEqual(
            set(evidence["production_hashes"]),
            {
                "batch.py",
                "batch_cache.py",
                "batch_scheduler.py",
                "whisper_worker.py",
                self.wrapper_chain.split(" -> ")[0],
                self.wrapper_chain.split(" -> ")[1],
            },
        )
        self.assertTrue(evidence["argparse_main_exercised"])
        self.assertTrue(evidence["python_environment_verified"])
        self.assertIn("langcodes=", evidence["python_environment"])
        version = tuple(int(part) for part in evidence["python_version"].split(".")[:2])
        self.assertGreaterEqual(version, (3, 10))
        self.assertLess(version, (3, 14))
        self.assertTrue(evidence["python_executable"])
        self.assertTrue(evidence["python_resolved_executable"])
        self.assertEqual(evidence["observed_cpu_io"], evidence["expected_cpu_io"])
        self.assertEqual(evidence["observed_nvenc"], 4)
        self.assertEqual(evidence["task_count"], 6)
        self.assertEqual(evidence["report_success_count"], 6)
        self.assertEqual(evidence["report_failure_count"], 0)
        machine_report = evidence["machine_report"]
        self.assertEqual(machine_report["schema_version"], 1)
        self.assertFalse(machine_report["worker_failure"])
        self.assertIsNone(machine_report["worker_failure_log"])
        self.assertIsNone(machine_report["worker_failure_root_cause"])
        self.assertIsNone(machine_report["worker_failure_detail"])
        self.assertTrue(machine_report["output_directory"])
        self.assertEqual(machine_report["cleanup_diagnostics"], [])
        self.assertEqual(
            machine_report["summary"],
            {"total": 6, "success": 6, "failed": 0},
        )
        self.assertEqual(len(machine_report["tasks"]), 6)
        for task_report in machine_report["tasks"]:
            self.assertEqual(task_report["state"], "succeeded")
            self.assertEqual(task_report["stage"], "burned")
            self.assertTrue(task_report["output_directory"])
        self.assertEqual(evidence["aggregate_notification_bells"], 2)
        self.assertEqual(evidence["peak_prepare_nvenc"], 4)
        self.assertEqual(evidence["peak_burn_nvenc"], 4)
        self.assertLessEqual(evidence["peak_combined_nvenc"], 4)
        self.assertEqual(evidence["asr_load_count"], 1)
        self.assertEqual(evidence["align_loads"], {"en": 1, "ja": 1})
        self.assertEqual(evidence["align_calls"], {"en": 3, "ja": 3})
        self.assertEqual(evidence["worker_process_names"], ["batch-whisper-worker"])
        self.assertEqual(evidence["recovery_sidecars_remaining"], 0)
        self.assertEqual(evidence["persistent_lock_files"], 6)
        self.assertEqual(evidence["prepare_state_files"], 6)
        self.assertEqual(
            evidence["relative_output_paths"],
            [f"video-{index}/burned.mkv" for index in range(1, 7)],
        )
        self.assertTrue(evidence["output_files_nonempty"])
        for stages in evidence["task_stages"].values():
            self.assertEqual(stages, EXPECTED_STAGES)

        prepare_intervals = evidence["prepare_intervals"]
        audio_intervals = evidence["audio_intervals"]
        burn_intervals = evidence["burn_intervals"]
        worker_events = evidence["worker_events"]
        self.assertEqual(len(prepare_intervals), 6)
        self.assertEqual(len(audio_intervals), 6)
        self.assertEqual(len(burn_intervals), 6)
        for interval in prepare_intervals + audio_intervals + burn_intervals:
            self.assertLess(interval["start_ns"], interval["end_ns"], interval)
        for event in worker_events:
            self.assertIsInstance(event["wall_ns"], int)
            self.assertIsInstance(event["monotonic_ns"], int)

        asr_load_ns = next(
            event["wall_ns"] for event in worker_events if event["event"] == "load_asr"
        )
        worker_shutdown_ns = next(
            event["wall_ns"]
            for event in worker_events
            if event["event"] == "worker_shutdown"
        )
        acquisition_end_ns = max(
            interval["end_ns"] for interval in prepare_intervals + audio_intervals
        )
        self.assertLessEqual(acquisition_end_ns, asr_load_ns)
        for interval in prepare_intervals:
            self.assertLessEqual(interval["end_ns"], asr_load_ns)
        for interval in burn_intervals:
            self.assertLessEqual(worker_shutdown_ns, interval["start_ns"])

        self.assert_serial_intervals(evidence["asr_intervals"], "ASR commands")
        self.assert_serial_intervals(evidence["align_intervals"], "alignment commands")
        self.assertEqual(
            evidence["worker_command_sequence"],
            [
                ("load_asr", "", ""),
                *[
                    item
                    for index in range(1, 7)
                    for item in (
                        (
                            "transcribe_start",
                            "en" if index % 2 else "ja",
                            f"video-{index}",
                        ),
                        (
                            "transcribe_end",
                            "en" if index % 2 else "ja",
                            f"video-{index}",
                        ),
                    )
                ],
                ("unload_asr", "", ""),
                ("load_align", "en", ""),
                *[
                    item
                    for index in (1, 3, 5)
                    for item in (
                        ("align_start", "en", f"video-{index}"),
                        ("align_end", "en", f"video-{index}"),
                    )
                ],
                ("unload_align", "en", ""),
                ("load_align", "ja", ""),
                *[
                    item
                    for index in (2, 4, 6)
                    for item in (
                        ("align_start", "ja", f"video-{index}"),
                        ("align_end", "ja", f"video-{index}"),
                    )
                ],
                ("unload_align", "ja", ""),
                ("worker_shutdown", "", ""),
            ],
        )


class WslInterpreterResolverTests(unittest.TestCase):
    def test_wsl_root_capability_skips_by_default_and_fails_when_required(self):
        def unavailable_runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "root unavailable")

        with self.assertRaisesRegex(unittest.SkipTest, "WSL root.*unavailable"):
            production_batch_smoke._verify_wsl_root(
                wsl="wsl.exe",
                environ={},
                runner=unavailable_runner,
            )
        with self.assertRaisesRegex(
            AssertionError,
            "BATCH_SMOKE_REQUIRE_WSL=1.*WSL root.*unavailable",
        ):
            production_batch_smoke._verify_wsl_root(
                wsl="wsl.exe",
                environ={"BATCH_SMOKE_REQUIRE_WSL": "1"},
                runner=unavailable_runner,
            )

    def test_missing_runtime_dependencies_skip_without_installing(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[-3:] == ["sh", "-c", "command -v python3"]:
                return subprocess.CompletedProcess(command, 0, "/usr/bin/python3\n", "")
            return subprocess.CompletedProcess(command, 1, "", "missing langcodes")

        with self.assertRaisesRegex(
            unittest.SkipTest,
            "BATCH_SMOKE_WSL_PYTHON.*langcodes",
        ):
            production_batch_smoke._resolve_wsl_python(
                wsl="wsl.exe",
                repo_path="/repo",
                environ={},
                runner=fake_run,
            )

        probes = [command[4] for command in calls if len(command) > 5 and command[4] != "sh"]
        self.assertEqual(probes, ["/repo/.venv/bin/python", "/usr/bin/python3"])
        flattened = {argument for command in calls for argument in command}
        self.assertTrue({"uv", "pip", "install"}.isdisjoint(flattened))

    def test_missing_interpreter_fails_when_wsl_is_required(self):
        def fake_run(command, **kwargs):
            if command[-3:] == ["sh", "-c", "command -v python3"]:
                return subprocess.CompletedProcess(command, 0, "/usr/bin/python3\n", "")
            return subprocess.CompletedProcess(command, 1, "", "missing langcodes")

        with self.assertRaisesRegex(
            AssertionError,
            "BATCH_SMOKE_REQUIRE_WSL=1.*BATCH_SMOKE_WSL_PYTHON.*langcodes",
        ):
            production_batch_smoke._resolve_wsl_python(
                wsl="wsl.exe",
                repo_path="/repo",
                environ={"BATCH_SMOKE_REQUIRE_WSL": "1"},
                runner=fake_run,
            )

    def test_override_is_first_accepted_existing_interpreter(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                "3\n/opt/smoke-python\n3.13.12\nok\n",
                "",
            )

        executable, probe_lines = production_batch_smoke._resolve_wsl_python(
            wsl="wsl.exe",
            repo_path="/repo",
            environ={"BATCH_SMOKE_WSL_PYTHON": "/opt/smoke-python"},
            runner=fake_run,
        )

        self.assertEqual(executable, "/opt/smoke-python")
        self.assertEqual(
            probe_lines,
            ["3", "/opt/smoke-python", "3.13.12", "ok"],
        )
        self.assertEqual(calls[0][4], "/opt/smoke-python")
        self.assertNotIn("command -v python3", calls[0])

    def test_unsupported_versions_continue_until_valid_python(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[-3:] == ["sh", "-c", "command -v python3"]:
                return subprocess.CompletedProcess(command, 0, "/usr/bin/python3\n", "")
            candidate = command[4]
            if candidate == "/opt/python3.9":
                return subprocess.CompletedProcess(command, 1, "", "Python 3.9 unsupported")
            if candidate == "/repo/.venv/bin/python":
                return subprocess.CompletedProcess(command, 1, "", "Python 3.14 unsupported")
            return subprocess.CompletedProcess(
                command,
                0,
                "4\n/usr/bin/python3\n3.13.7\nok\n",
                "",
            )

        executable, probe_lines = production_batch_smoke._resolve_wsl_python(
            wsl="wsl.exe",
            repo_path="/repo",
            environ={"BATCH_SMOKE_WSL_PYTHON": "/opt/python3.9"},
            runner=fake_run,
        )

        probed = [command[4] for command in calls if command[4] != "sh"]
        self.assertEqual(
            probed,
            ["/opt/python3.9", "/repo/.venv/bin/python", "/usr/bin/python3"],
        )
        self.assertEqual(executable, "/usr/bin/python3")
        self.assertEqual(probe_lines[2], "3.13.7")
        probe_commands = [command for command in calls if command[4] != "sh"]
        for command in probe_commands:
            self.assertIn("import langcodes", command[6])
            self.assertIn("(3, 10)", command[6])
            self.assertIn("(3, 14)", command[6])


@unittest.skipUnless(PWSH, "requires PowerShell 7")
class WindowsProductionBatchSmokeTests(ProductionBatchSmokeMixin, unittest.TestCase):
    platform_name = "windows-powershell"
    wrapper_chain = "batch.ps1 -> py_launcher.ps1 -> batch.py"

    def run_smoke(self):
        return production_batch_smoke.run_windows(
            root=ROOT,
            sandbox=self.sandbox,
            python_executable=pathlib.Path(sys.executable),
            powershell=PWSH,
        )


class BashProductionBatchSmokeTests(ProductionBatchSmokeMixin, unittest.TestCase):
    platform_name = "wsl-bash" if os.name == "nt" else "bash"
    wrapper_chain = "batch.sh -> py_launcher.sh -> batch.py"

    def run_smoke(self):
        if os.name == "nt":
            if not WSL:
                production_batch_smoke._handle_wsl_unavailable(
                    "WSL executable is unavailable",
                )
            return production_batch_smoke.run_wsl_root(
                root=ROOT,
                sandbox=self.sandbox,
                wsl=WSL,
            )
        if os.environ.get("BATCH_SMOKE_REQUIRE_WSL") == "1":
            production_batch_smoke._handle_wsl_unavailable(
                "BATCH_SMOKE_REQUIRE_WSL=1 requires Windows WSL root",
            )
        if not BASH:
            production_batch_smoke._handle_wsl_unavailable(
                "bash executable is unavailable",
            )
        return production_batch_smoke.run_bash(
            root=ROOT,
            sandbox=self.sandbox,
            python_executable=pathlib.Path(sys.executable),
            bash=BASH,
        )


if __name__ == "__main__":
    unittest.main()
