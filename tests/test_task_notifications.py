import io
import inspect
import unittest
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

        source = inspect.getsource(batch_runtime._main)
        self.assertIn("emit_task_bell(\"error\" if exit_code else \"success\")", source)


if __name__ == "__main__":
    unittest.main()
