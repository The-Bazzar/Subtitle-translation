import pathlib
import subprocess
import sys
import unittest
from unittest import mock

import batch_runtime
from batch_scheduler import ResourceLimits
from subtitle_translation.stages import StageResult


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PythonCliSmokeTests(unittest.TestCase):
    def test_module_cli_help_and_dry_run_are_dependency_free(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "subtitle_translation", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        dry_run = subprocess.run(
            [sys.executable, "-m", "subtitle_translation", "batch", "--dry-run", "url"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertNotIn("\a", dry_run.stdout + dry_run.stderr)

    def test_direct_stage_runner_returns_structured_paths(self):
        root = pathlib.Path(self._testMethodName)
        env = {"FFMPEG_PATH_WIN": "ffmpeg"}
        with mock.patch(
            "batch_runtime.download_video",
            return_value=StageResult.ok(render_video="render.mkv"),
        ), mock.patch(
            "batch_runtime.prepare_video",
            return_value=StageResult.ok(edit_video="edit.mkv"),
        ), mock.patch(
            "batch_runtime.extract_audio",
            return_value=StageResult.ok(wav="audio.wav"),
        ):
            runners = batch_runtime.create_platform_runners(root, env)
            self.assertEqual(asyncio_run(runners.download("url")), "render.mkv")
            self.assertEqual(asyncio_run(runners.prepare("render.mkv")), "edit.mkv")
            self.assertEqual(asyncio_run(runners.extract_audio("edit.mkv")), "audio.wav")

    def test_resource_limits_are_automatic_and_nvenc_is_fixed(self):
        limits = ResourceLimits.detect(logical_cpus=32)
        self.assertEqual(limits.cpu_io, 8)
        self.assertEqual(limits.nvenc, 4)


def asyncio_run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
