import importlib.util
import io
import pathlib
import tempfile
from unittest import TestCase, main, mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def load_batch_module():
    spec = importlib.util.spec_from_file_location("batch_module", ROOT / "batch.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TaskNotificationTests(TestCase):
    def test_pipeline_scripts_define_success_and_error_notifications(self):
        powershell = read_script("pipeline.ps1")
        bash = read_script("pipeline.sh")

        self.assertIn("function Invoke-TaskBell", powershell)
        self.assertIn("function Exit-Pipeline", powershell)
        self.assertIn("[Console]::Beep", powershell)
        self.assertRegex(
            powershell,
            r"PIPELINE_BATCH_CHILD[^\r\n]+-ne\s+'1'",
        )

        self.assertIn("task_bell()", bash)
        self.assertIn("notify_pipeline_exit()", bash)
        self.assertIn("trap notify_pipeline_exit EXIT", bash)
        self.assertIn("task_bell success", bash)
        self.assertIn("task_bell error", bash)
        self.assertRegex(bash, r"PIPELINE_BATCH_CHILD:-0.+!=.+1")

    def test_batch_scripts_mark_children_and_notify_aggregate_result(self):
        powershell = read_script("batch.ps1")
        python = read_script("batch.py")

        self.assertRegex(
            powershell,
            r"\$env:PIPELINE_BATCH_CHILD\s*=\s*'1'",
        )
        self.assertRegex(
            powershell,
            r"\$NotificationKind\s*=\s*if\s*\(\$Failed\s+-gt\s+0\)",
        )
        self.assertRegex(
            powershell,
            r"Invoke-TaskBell\s+-Kind\s+\$NotificationKind",
        )
        self.assertIn("__PIPELINE_BATCH_EXIT__", powershell)
        self.assertIn("__PIPELINE_BATCH_EXIT__", read_script("pipeline.ps1"))
        self.assertIn("^__PIPELINE_BATCH_EXIT__=(-?\\d+)$", powershell)
        self.assertRegex(powershell, r"else\s*\{\s*1\s*\}")
        self.assertIn("Invoke-TaskBell -Kind Error", powershell)
        self.assertIn("exit ($Failed -gt 0 ? 1 : 0)", powershell)

        self.assertRegex(
            python,
            r"env\[['\"]PIPELINE_BATCH_CHILD['\"]\]\s*=\s*['\"]1['\"]",
        )
        self.assertIn('emit_task_bell("error" if failed else "success")', python)
        self.assertIn("return 1 if failed else 0", python)
        self.assertRegex(
            python,
            r"raise\s+SystemExit\(main\(\)\)",
        )

    def test_python_bell_patterns_are_distinct(self):
        batch = load_batch_module()
        success = io.StringIO()
        error = io.StringIO()

        with mock.patch.object(batch.time, "sleep"):
            batch.emit_task_bell("success", success)
            batch.emit_task_bell("error", error)

        self.assertEqual(success.getvalue(), "\a\a")
        self.assertEqual(error.getvalue(), "\a\a\a")

    def test_help_and_dry_run_paths_stay_silent(self):
        pipeline_powershell = read_script("pipeline.ps1")
        pipeline_bash = read_script("pipeline.sh")
        batch_powershell = read_script("batch.ps1")
        batch = load_batch_module()

        self.assertLess(
            pipeline_powershell.index("if ($DryRun)"),
            pipeline_powershell.index("$script:PipelineNotificationActive = $true"),
        )
        self.assertLess(
            pipeline_bash.index('"${1:-}" = "--help"'),
            pipeline_bash.index('ENV_FILE="$SCRIPT_DIR/.env"'),
        )
        self.assertLess(
            batch_powershell.index("if ($DryRun)"),
            batch_powershell.index("$script:BatchNotificationActive = $true"),
        )

        with mock.patch.object(batch.sys, "argv", ["batch.py", "url", "--dry-run"]), \
                mock.patch.object(batch, "emit_task_bell") as bell:
            self.assertEqual(batch.main(), 0)
        bell.assert_not_called()

    def test_python_batch_notifies_each_failure_and_aggregate_result(self):
        batch = load_batch_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            report = str(pathlib.Path(temp_dir) / "batch-result.txt")
            argv = ["batch.py", "-j", "1", "-r", report, "ok-url", "bad-url"]
            completed = [
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=9, stdout="failed", stderr=""),
            ]
            with mock.patch.object(batch.sys, "argv", argv), \
                    mock.patch.object(batch.subprocess, "run", side_effect=completed), \
                    mock.patch.object(batch, "emit_task_bell") as bell, \
                    mock.patch("builtins.print"):
                self.assertEqual(batch.main(), 1)

        self.assertEqual(
            [call.args[0] for call in bell.call_args_list],
            ["error", "error"],
        )

    def test_python_batch_timeout_gets_child_and_aggregate_error_bells(self):
        batch = load_batch_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            report = str(pathlib.Path(temp_dir) / "batch-result.txt")
            argv = ["batch.py", "-j", "1", "-r", report, "timeout-url"]
            timeout = batch.subprocess.TimeoutExpired(["bash", "pipeline.sh"], 7200)
            with mock.patch.object(batch.sys, "argv", argv), \
                    mock.patch.object(batch.subprocess, "run", side_effect=timeout), \
                    mock.patch.object(batch, "emit_task_bell") as bell, \
                    mock.patch("builtins.print"):
                self.assertEqual(batch.main(), 1)

        self.assertEqual(
            [call.args[0] for call in bell.call_args_list],
            ["error", "error"],
        )


if __name__ == "__main__":
    main()
