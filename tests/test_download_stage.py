import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import batch_runtime
from subtitle_translation import stages
from subtitle_translation.config import ProjectConfig
from subtitle_translation.process import CommandResult


class DownloadStageTests(unittest.TestCase):
    def test_cookies_and_cache_order_for_download_and_metadata_refresh(self):
        for existing in (False, True):
            for local_cookies, project_cookies in ((True, True), (True, False), (False, True), (False, False)):
                with self.subTest(existing=existing, local=local_cookies, project=project_cookies):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        project = root / "config root"
                        invocation = root / "invocation root"
                        project.mkdir()
                        invocation.mkdir()
                        for folder, present in ((project, project_cookies), (invocation, local_cookies)):
                            if present:
                                (folder / "cookies.txt").write_text("# mock cookies\n", encoding="utf-8")
                        video = invocation / "Title" / "Title.original.mkv"
                        if existing:
                            video.parent.mkdir()
                            video.write_bytes(b"existing video")
                        config = ProjectConfig.load(project, invocation)
                        calls = []
                        executable = str(root / "tool directory" / "yt-dlp.exe")

                        def command(args, **kwargs):
                            calls.append((args, kwargs))
                            if "--get-title" in args:
                                return CommandResult(tuple(args), 0, "Title\n")
                            if "-o" in args and "--skip-download" not in args:
                                (video.parent / "Title.mkv").write_bytes(b"downloaded video")
                            return CommandResult(tuple(args), 0)

                        with mock.patch.object(ProjectConfig, "resolve_tool", return_value=executable), mock.patch.object(
                            stages, "capture_command", side_effect=command
                        ), mock.patch.object(stages, "run_command", side_effect=command):
                            result = stages.download_video("https://example.invalid/video", config)

                        self.assertTrue(result.success, result.diagnostics)
                        self.assertEqual(Path(result.outputs["render_video"]), video.resolve())
                        self.assertEqual(len(calls), 4)
                        self.assertEqual(calls[0][0], [executable, "--rm-cache-dir"])
                        self.assertEqual(calls[2][0], [executable, "--rm-cache-dir"])
                        self.assertIn("--get-title", calls[1][0])
                        self.assertIn("-o", calls[3][0])
                        self.assertEqual("--skip-download" in calls[3][0], existing)
                        for args, kwargs in calls:
                            self.assertEqual(kwargs["cwd"], invocation.resolve())
                            self.assertEqual(args[0], executable)
                        for args, _ in (calls[1], calls[3]):
                            self.assertEqual("--cookies" in args, local_cookies)
                            if local_cookies:
                                self.assertEqual(args[args.index("--cookies") + 1], str(invocation.resolve() / "cookies.txt"))
                            self.assertNotIn(str(project / "cookies.txt"), args)

    def test_cache_cleanup_failure_stops_before_next_operation(self):
        for failed_cleanup in (1, 2):
            with self.subTest(failed_cleanup=failed_cleanup), tempfile.TemporaryDirectory() as directory:
                config = ProjectConfig(Path(directory), {})
                results = [CommandResult(("yt-dlp", "--rm-cache-dir"), 0)] * (failed_cleanup - 1)
                results.append(CommandResult(("yt-dlp", "--rm-cache-dir"), 13))
                with mock.patch.object(ProjectConfig, "resolve_tool", return_value="yt-dlp"), mock.patch.object(
                    stages, "run_command", side_effect=results
                ) as run, mock.patch.object(
                    stages, "capture_command", return_value=CommandResult(("yt-dlp", "--get-title"), 0, "Title\n")
                ) as capture:
                    result = stages.download_video("https://example.invalid/video", config)
                self.assertFalse(result.success)
                self.assertEqual(result.exit_code, 13)
                self.assertIn("cache cleanup", result.diagnostics[0])
                self.assertEqual(result.command, ("yt-dlp", "--rm-cache-dir"))
                self.assertEqual(run.call_count, failed_cleanup)
                self.assertEqual(capture.call_count, failed_cleanup - 1)

    def test_title_failure_does_not_start_download_or_second_cleanup(self):
        for code, title in ((7, ""), (0, "\n")):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                config = ProjectConfig(Path(directory), {})
                with mock.patch.object(ProjectConfig, "resolve_tool", return_value="yt-dlp"), mock.patch.object(
                    stages, "run_command", return_value=CommandResult(("yt-dlp", "--rm-cache-dir"), 0)
                ) as run, mock.patch.object(
                    stages, "capture_command", return_value=CommandResult(("yt-dlp", "--get-title"), code, title)
                ):
                    result = stages.download_video("https://example.invalid/video", config)
                self.assertFalse(result.success)
                self.assertEqual(result.exit_code, code or 1)
                run.assert_called_once_with(["yt-dlp", "--rm-cache-dir"], cwd=config.output_dir, label="yt-dlp cache cleanup")

    def test_download_failure_preserves_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            config = ProjectConfig(Path(directory), {})
            with mock.patch.object(ProjectConfig, "resolve_tool", return_value="yt-dlp"), mock.patch.object(
                stages, "run_command", side_effect=lambda args, **kwargs: CommandResult(tuple(args), 9 if "-o" in args else 0)
            ), mock.patch.object(
                stages, "capture_command", return_value=CommandResult(("yt-dlp", "--get-title"), 0, "Title\n")
            ):
                result = stages.download_video("https://example.invalid/video", config)
            self.assertFalse(result.success)
            self.assertEqual(result.exit_code, 9)
            self.assertIn("-o", result.command)

    def test_batch_runner_keeps_invocation_directory_for_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "config root"
            invocation = root / "invocation root"
            project.mkdir()
            invocation.mkdir()
            (project / "cookies.txt").write_text("# wrong cookies\n", encoding="utf-8")
            (invocation / "cookies.txt").write_text("# mock cookies\n", encoding="utf-8")

            def command(args, **kwargs):
                if "-o" in args:
                    (invocation / "Title" / "Title.mkv").write_bytes(b"video")
                return CommandResult(tuple(args), 0)

            with mock.patch.object(batch_runtime.Path, "cwd", return_value=invocation):
                runners = batch_runtime.create_platform_runners(project, {})
            with mock.patch.object(ProjectConfig, "resolve_tool", return_value="yt-dlp"), mock.patch.object(
                stages, "run_command", side_effect=command
            ) as run, mock.patch.object(
                stages, "capture_command", return_value=CommandResult(("yt-dlp", "--get-title"), 0, "Title\n")
            ) as capture:
                result = asyncio.run(runners.download("https://example.invalid/video"))
            self.assertEqual(Path(result).parent.parent, invocation.resolve())
            for call in (capture.call_args, run.call_args):
                args = call.args[0]
                self.assertEqual(args[args.index("--cookies") + 1], str(invocation.resolve() / "cookies.txt"))
                self.assertEqual(call.kwargs["cwd"], invocation.resolve())


if __name__ == "__main__":
    unittest.main()
