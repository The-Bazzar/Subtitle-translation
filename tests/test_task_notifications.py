import io
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from subtitle_translation.notifications import emit_bell


class NotificationTests(unittest.TestCase):
    def test_success_and_error_patterns_are_distinct(self):
        success = io.StringIO()
        error = io.StringIO()
        with mock.patch("subtitle_translation.notifications.time.sleep"):
            emit_bell("success", stream=success)
            emit_bell("error", stream=error)
        self.assertEqual(success.getvalue(), "\a\a")
        self.assertEqual(error.getvalue(), "\a\a\a")

    def test_batch_runtime_keeps_aggregate_notification(self):
        import batch_runtime

        args = batch_runtime.build_parser().parse_args(["fixture"])
        args.dry_run = args.burn = args.mfa = False
        for code, kind in ((0, "success"), (1, "error")):
            with self.subTest(code=code), mock.patch.object(batch_runtime, "run_acquisition", return_value=[]), \
                 mock.patch.object(batch_runtime, "write_report"), mock.patch.object(batch_runtime, "aggregate_exit_code", return_value=code), \
                 mock.patch.object(batch_runtime, "log"), mock.patch.object(batch_runtime, "emit_task_bell") as bell:
                result = batch_runtime._run_batch(args, {}, [], Path("unused"), batch_runtime.ResourceLimits(cpu_io=1),
                                                 datetime.now(), Path.cwd())
            self.assertEqual(result, code)
            bell.assert_called_once_with(kind)


if __name__ == "__main__":
    unittest.main()
