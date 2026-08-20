import contextlib
import importlib.util
import io
import pathlib
import sys
import tempfile
from unittest import TestCase, main, mock

from batch_scheduler import BatchTask


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def load_batch_module():
    spec = importlib.util.spec_from_file_location("batch_module", ROOT / "batch.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TaskNotificationTests(TestCase):
    def test_pipeline_scripts_define_success_and_error_notifications(self):
        powershell = read_script("pipeline.ps1")
        bash = read_script("pipeline.sh")

        self.assertIn("function Invoke-TaskBell", powershell)
        self.assertIn("function Exit-Pipeline", powershell)
        self.assertIn("[Console]::Beep", powershell)

        self.assertIn("task_bell()", bash)
        self.assertIn("notify_pipeline_exit()", bash)
        self.assertIn("trap notify_pipeline_exit EXIT", bash)
        self.assertIn("task_bell success", bash)
        self.assertIn("task_bell error", bash)

    def test_retired_batch_child_protocol_is_absent_from_all_entrypoints(self):
        entrypoints = (
            "batch.py",
            "batch.ps1",
            "batch.sh",
            "pipeline.ps1",
            "pipeline.sh",
        )
        for script_name in entrypoints:
            content = read_script(script_name)
            with self.subTest(script_name=script_name):
                self.assertNotIn("PIPELINE_BATCH_CHILD", content)
                self.assertNotIn("__PIPELINE_BATCH_EXIT__", content)

        powershell = read_script("batch.ps1")
        bash = read_script("batch.sh")
        python = read_script("batch.py")
        self.assertNotIn("pipeline.ps1", powershell)
        self.assertNotIn("pipeline.sh", bash)
        self.assertRegex(
            python,
            r"raise\s+SystemExit\(main\(\)\)",
        )

    def test_burn_wrappers_emit_the_same_success_marker(self):
        powershell = read_script("ffmpeg-burn.ps1")
        bash = read_script("ffmpeg-burn.sh")

        self.assertIn('OUTPUT_BURNED_VIDEO=', powershell)
        self.assertIn('OUTPUT_BURNED_VIDEO=', bash)

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
        batch = load_batch_module()

        self.assertLess(
            pipeline_powershell.index("if ($DryRun)"),
            pipeline_powershell.index("$script:PipelineNotificationActive = $true"),
        )
        self.assertLess(
            pipeline_bash.index('"${1:-}" = "--help"'),
            pipeline_bash.index('ENV_FILE="$SCRIPT_DIR/.env"'),
        )
        with mock.patch.object(batch, "emit_task_bell") as bell, \
                mock.patch.object(batch, "run_acquisition") as run_acquisition, \
                mock.patch("builtins.print"):
            self.assertEqual(batch.main(["url", "--dry-run"]), 0)
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    batch.main(["--help"])
        bell.assert_not_called()
        run_acquisition.assert_not_called()

    def test_python_batch_notifies_each_terminal_failure_and_aggregate_result(self):
        batch = load_batch_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            report = str(pathlib.Path(temp_dir) / "batch-result.txt")
            first = BatchTask(index=1, url="bad-one")
            first.fail("download", "network error")
            second = BatchTask(index=2, url="ok")
            second.start("download")
            second.succeed("wav_ready")
            third = BatchTask(index=3, url="bad-two")
            third.fail("prepare", "encoder error")

            with mock.patch.object(batch, "run_acquisition", return_value=[first, second, third]), \
                    mock.patch.object(batch, "emit_task_bell") as bell, \
                    mock.patch("builtins.print"):
                self.assertEqual(batch.main(["--report", report, "bad-one", "ok", "bad-two"]), 1)

        self.assertEqual(
            [call.args[0] for call in bell.call_args_list],
            ["error", "error", "error"],
        )

    def test_python_batch_success_only_notifies_aggregate_success(self):
        batch = load_batch_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            report = str(pathlib.Path(temp_dir) / "batch-result.txt")
            task = BatchTask(index=1, url="ok")
            task.start("download")
            task.succeed("wav_ready")
            with mock.patch.object(batch, "run_acquisition", return_value=[task]), \
                    mock.patch.object(batch, "emit_task_bell") as bell, \
                    mock.patch("builtins.print"):
                self.assertEqual(batch.main(["--report", report, "ok"]), 0)

        self.assertEqual(
            [call.args[0] for call in bell.call_args_list],
            ["success"],
        )

    def test_python_batch_interrupt_reports_tasks_and_returns_130(self):
        batch = load_batch_module()
        task = BatchTask(index=1, url="interrupted")
        task.start("download")
        task.cancel("batch interrupted after download")

        with tempfile.TemporaryDirectory() as temp_dir:
            report = str(pathlib.Path(temp_dir) / "batch-result.txt")
            interrupted = batch.BatchInterrupted([task], interrupt_count=1)
            with mock.patch.object(
                batch,
                "run_acquisition",
                side_effect=interrupted,
            ), mock.patch.object(batch, "emit_task_bell") as bell, mock.patch(
                "builtins.print"
            ):
                self.assertEqual(
                    batch.main(["--report", report, "interrupted"]),
                    130,
                )

        self.assertEqual(
            [call.args[0] for call in bell.call_args_list],
            ["error", "error"],
        )


if __name__ == "__main__":
    main()
