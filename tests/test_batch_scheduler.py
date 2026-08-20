import asyncio
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from batch import (
    _run_stage_command,
    build_parser,
    build_stage_environment,
    create_platform_runners,
)
from batch_scheduler import (
    AcquisitionRunners,
    AcquisitionScheduler,
    BatchTask,
    ResourceLimits,
    TaskState,
    aggregate_exit_code,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


class StageCommandCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_kills_process_tree_before_releasing_scheduler_slot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = pathlib.Path(temp_dir)
            ready_path = work_dir / "ready"
            finished_path = work_dir / "finished"
            child_code = (
                "import pathlib,time;"
                "time.sleep(0.25);"
                f"pathlib.Path({str(finished_path)!r}).write_text('finished')"
            )
            parent_code = (
                "import pathlib,subprocess,sys,time;"
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]);"
                f"pathlib.Path({str(ready_path)!r}).write_text('ready');"
                "time.sleep(0.6)"
            )

            async def download(_url):
                return await _run_stage_command(
                    [sys.executable, "-c", parent_code],
                    cwd=work_dir,
                    env=os.environ.copy(),
                    stage="download",
                )

            async def unused_runner(value):
                return value

            scheduler = AcquisitionScheduler(
                urls=["cancel-me"],
                limits=ResourceLimits(cpu_io=1),
                runners=AcquisitionRunners(download, unused_runner, unused_runner),
            )
            run_task = asyncio.create_task(scheduler.run())
            for _ in range(100):
                if ready_path.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(ready_path.exists(), "child process did not start")

            run_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await run_task
            await asyncio.sleep(0.8)

            self.assertFalse(finished_path.exists())
            self.assertIs(scheduler.tasks[0].state, TaskState.CANCELED)
            self.assertEqual(scheduler._cpu_io_slots._value, 1)

    def test_process_group_options_cover_windows_and_posix(self):
        from batch import _process_group_kwargs

        self.assertEqual(_process_group_kwargs("posix"), {"start_new_session": True})
        self.assertEqual(
            _process_group_kwargs("nt"),
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)},
        )

    async def test_windows_tree_termination_falls_back_when_taskkill_fails(self):
        from batch import _terminate_process_tree

        process = mock.Mock(pid=1234, returncode=None)
        process.wait = mock.AsyncMock(return_value=1)
        terminator = mock.Mock(returncode=1)
        terminator.wait = mock.AsyncMock(return_value=1)
        with mock.patch(
            "batch.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=terminator),
        ):
            await _terminate_process_tree(process, platform="nt")

        process.kill.assert_called_once_with()


class ResourceLimitsTests(unittest.TestCase):
    def test_detects_automatic_cpu_io_and_fixed_nvenc_capacity(self):
        self.assertEqual(ResourceLimits.detect(logical_cpus=32).cpu_io, 8)
        self.assertEqual(ResourceLimits.detect(logical_cpus=2).cpu_io, 1)
        self.assertEqual(ResourceLimits.detect(logical_cpus=32).nvenc, 4)

    def test_detect_uses_os_cpu_count_and_never_returns_less_than_one(self):
        with mock.patch("batch_scheduler.os.cpu_count", return_value=None):
            self.assertGreaterEqual(ResourceLimits.detect().cpu_io, 1)
        with mock.patch("batch_scheduler.os.cpu_count", return_value=12):
            self.assertEqual(ResourceLimits.detect().cpu_io, 3)


class BatchTaskStateTests(unittest.TestCase):
    def test_task_state_values_are_stable(self):
        self.assertEqual(
            [state.value for state in TaskState],
            [
                "pending",
                "running",
                "succeeded",
                "failed",
                "canceled",
                "blocked_by_worker_failure",
            ],
        )

    def test_task_failure_is_terminal_without_failing_other_tasks(self):
        first = BatchTask(index=1, url="bad")
        second = BatchTask(index=2, url="good")

        first.fail(stage="download", detail="network error")

        self.assertIs(first.state, TaskState.FAILED)
        self.assertEqual(first.error_detail, "network error")
        self.assertIsNotNone(first.finished_at)
        self.assertIs(second.state, TaskState.PENDING)

    def test_terminal_task_rejects_every_later_transition(self):
        task = BatchTask(index=1, url="done")
        task.start("download")
        task.succeed(stage="wav_ready")

        transitions = (
            lambda: task.start("download"),
            lambda: task.advance("prepare"),
            lambda: task.succeed(stage="wav_ready"),
            lambda: task.fail(stage="prepare", detail="late failure"),
            lambda: task.cancel(detail="late cancel"),
            lambda: task.block_by_worker_failure(detail="late block"),
        )
        for transition in transitions:
            with self.subTest(transition=transition):
                with self.assertRaises(RuntimeError):
                    transition()

    def test_every_terminal_state_rejects_later_transitions(self):
        terminal_tasks = []
        succeeded = BatchTask(index=1, url="succeeded")
        succeeded.start("download")
        succeeded.succeed("wav_ready")
        terminal_tasks.append(succeeded)

        failed = BatchTask(index=2, url="failed")
        failed.fail("download", "failed")
        terminal_tasks.append(failed)

        canceled = BatchTask(index=3, url="canceled")
        canceled.cancel("canceled")
        terminal_tasks.append(canceled)

        blocked = BatchTask(index=4, url="blocked")
        blocked.block_by_worker_failure("worker failed")
        terminal_tasks.append(blocked)

        for task in terminal_tasks:
            with self.subTest(state=task.state):
                with self.assertRaises(RuntimeError):
                    task.advance("prepare")


class AcquisitionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.output_dir.cleanup)

    def write_wav(self, name):
        wav_path = pathlib.Path(self.output_dir.name) / name
        wav_path.write_bytes(b"wav")
        return str(wav_path)

    async def run_with_wav_output(self, wav_path):
        async def download(_url):
            return "video.original.mkv"

        async def prepare(_render_video):
            return "video.mkv"

        async def extract_audio(_edit_video):
            return wav_path

        scheduler = AcquisitionScheduler(
            urls=["video"],
            limits=ResourceLimits(cpu_io=1),
            runners=AcquisitionRunners(download, prepare, extract_audio),
        )
        return await scheduler.run()

    async def test_missing_wav_fails_at_extract_audio(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = pathlib.Path(temp_dir) / "missing.wav"
            tasks = await self.run_with_wav_output(wav_path)

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "extract_audio")
        self.assertIn("non-empty regular file", tasks[0].error_detail)
        self.assertEqual(aggregate_exit_code(tasks), 1)

    async def test_empty_wav_fails_at_extract_audio(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = pathlib.Path(temp_dir) / "empty.wav"
            wav_path.touch()
            tasks = await self.run_with_wav_output(wav_path)

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "extract_audio")
        self.assertIn("non-empty regular file", tasks[0].error_detail)
        self.assertEqual(aggregate_exit_code(tasks), 1)

    async def test_nonempty_wav_completes_acquisition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = pathlib.Path(temp_dir) / "ready.wav"
            wav_path.write_bytes(b"wav")
            tasks = await self.run_with_wav_output(wav_path)

        self.assertIs(tasks[0].state, TaskState.SUCCEEDED)
        self.assertEqual(tasks[0].wav_path, wav_path)
        self.assertEqual(aggregate_exit_code(tasks), 0)

    async def test_successful_tasks_stop_after_wav_preparation(self):
        calls = []

        async def download(url):
            calls.append(("download", url))
            return f"{url}.original.mkv"

        async def prepare(render_video):
            calls.append(("prepare", render_video))
            return render_video.replace(".original.mkv", ".mkv")

        async def extract_audio(edit_video):
            calls.append(("extract_audio", edit_video))
            return self.write_wav("one.wav")

        scheduler = AcquisitionScheduler(
            urls=["one"],
            limits=ResourceLimits(cpu_io=1),
            runners=AcquisitionRunners(download, prepare, extract_audio),
        )

        tasks = await scheduler.run()

        self.assertEqual(calls, [
            ("download", "one"),
            ("prepare", "one.original.mkv"),
            ("extract_audio", "one.mkv"),
        ])
        self.assertEqual(len(tasks), 1)
        self.assertIs(tasks[0].state, TaskState.SUCCEEDED)
        self.assertEqual(tasks[0].stage, "wav_ready")
        self.assertEqual(tasks[0].render_video_path, pathlib.Path("one.original.mkv"))
        self.assertEqual(tasks[0].edit_video_path, pathlib.Path("one.mkv"))
        self.assertEqual(tasks[0].wav_path, pathlib.Path(self.output_dir.name) / "one.wav")
        self.assertEqual(aggregate_exit_code(tasks), 0)

    async def test_one_failure_does_not_pollute_other_tasks(self):
        prepared = []
        extracted = []

        async def download(url):
            if url == "bad":
                raise RuntimeError("network error")
            return f"{url}.original.mkv"

        async def prepare(render_video):
            prepared.append(render_video)
            return "good.mkv"

        async def extract_audio(edit_video):
            extracted.append(edit_video)
            return self.write_wav("good.wav")

        scheduler = AcquisitionScheduler(
            urls=["bad", "good"],
            limits=ResourceLimits(cpu_io=2),
            runners=AcquisitionRunners(download, prepare, extract_audio),
        )

        tasks = await scheduler.run()

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "download")
        self.assertIn("network error", tasks[0].error_detail)
        self.assertIs(tasks[1].state, TaskState.SUCCEEDED)
        self.assertEqual(prepared, ["good.original.mkv"])
        self.assertEqual(extracted, ["good.mkv"])
        self.assertEqual(aggregate_exit_code(tasks), 1)

    async def test_download_and_audio_share_cpu_io_capacity(self):
        active = 0
        peak = 0

        async def cpu_io_stage(value):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return value

        async def extract_audio(value):
            await cpu_io_stage(value)
            return self.write_wav(f"{pathlib.Path(value).name}.wav")

        async def prepare(value):
            await asyncio.sleep(0)
            return value

        scheduler = AcquisitionScheduler(
            urls=[f"url-{index}" for index in range(8)],
            limits=ResourceLimits(cpu_io=2),
            runners=AcquisitionRunners(cpu_io_stage, prepare, extract_audio),
        )

        tasks = await scheduler.run()

        self.assertEqual(peak, 2)
        self.assertTrue(all(task.state is TaskState.SUCCEEDED for task in tasks))

    async def test_prepare_uses_four_nvenc_slots(self):
        active = 0
        peak = 0

        async def passthrough(value):
            return value

        async def prepare(value):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return value

        async def extract_audio(value):
            return self.write_wav(f"{pathlib.Path(value).name}.wav")

        scheduler = AcquisitionScheduler(
            urls=[f"url-{index}" for index in range(8)],
            limits=ResourceLimits(cpu_io=8),
            runners=AcquisitionRunners(passthrough, prepare, extract_audio),
        )

        await scheduler.run()

        self.assertEqual(peak, 4)


class BatchCliTests(unittest.TestCase):
    def test_manual_job_arguments_do_not_exist(self):
        parser = build_parser()
        for arguments in (
            ["-j", "2", "url"],
            ["--jobs", "2", "url"],
            ["--io-jobs", "2", "url"],
        ):
            with self.subTest(arguments=arguments):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(arguments)

        self.assertNotIn("MaxJobs", (ROOT / "batch.ps1").read_text(encoding="utf-8"))

    def test_existing_batch_options_remain_representable(self):
        args = build_parser().parse_args([
            "--skip-burn",
            "--report", "report.txt",
            "--dry-run",
            "--translate-provider", "deepseek",
            "--translate-model", "deepseek-chat",
            "url-1",
            "url-2",
        ])

        self.assertFalse(args.burn)
        self.assertEqual(args.report, "report.txt")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.translate_provider, "deepseek")
        self.assertEqual(args.translate_model, "deepseek-chat")
        self.assertEqual(args.urls, ["url-1", "url-2"])

        with mock.patch.dict(os.environ, {}, clear=True):
            stage_environment = build_stage_environment(args)
        self.assertEqual(stage_environment["BURN"], "0")
        self.assertEqual(stage_environment["PIPELINE_SKIP_BURN"], "1")
        self.assertEqual(stage_environment["TRANSLATE_PROVIDER"], "deepseek")
        self.assertEqual(stage_environment["TRANSLATE_MODEL"], "deepseek-chat")


class BatchEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    def test_project_env_uses_process_values_before_dotenv(self):
        from batch import load_project_environment

        with tempfile.TemporaryDirectory() as temp_dir:
            script_dir = pathlib.Path(temp_dir)
            (script_dir / ".env").write_text(
                "FFMPEG_PATH_WIN=C:\\Dot Env\\ffmpeg.exe\n"
                "FFMPEG_PATH_LINUX=/dot env/ffmpeg\n",
                encoding="utf-8",
            )
            env = load_project_environment(
                script_dir,
                environ={"FFMPEG_PATH_LINUX": "/process env/ffmpeg"},
            )

        self.assertEqual(env["FFMPEG_PATH_WIN"], "C:\\Dot Env\\ffmpeg.exe")
        self.assertEqual(env["FFMPEG_PATH_LINUX"], "/process env/ffmpeg")

    def test_explicit_cli_values_override_process_and_dotenv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script_dir = pathlib.Path(temp_dir)
            (script_dir / ".env").write_text(
                "TRANSLATE_PROVIDER=dotenv\nTRANSLATE_MODEL=dotenv-model\n",
                encoding="utf-8",
            )
            args = build_parser().parse_args([
                "--translate-provider", "cli",
                "--translate-model", "cli-model",
                "url",
            ])
            env = build_stage_environment(
                args,
                script_dir=script_dir,
                environ={
                    "TRANSLATE_PROVIDER": "process",
                    "TRANSLATE_MODEL": "process-model",
                },
            )

        self.assertEqual(env["TRANSLATE_PROVIDER"], "cli")
        self.assertEqual(env["TRANSLATE_MODEL"], "cli-model")

    async def test_dotenv_ffmpeg_paths_with_spaces_are_exact_argv_zero(self):
        from batch import load_project_environment

        configured_paths = {
            "nt": ("FFMPEG_PATH_WIN", r"C:\Program Files\FFmpeg Build\ffmpeg.exe"),
            "posix": ("FFMPEG_PATH_LINUX", "/opt/FFmpeg Build/bin/ffmpeg"),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            script_dir = pathlib.Path(temp_dir)
            (script_dir / ".env").write_text(
                "\n".join(f"{key}={value}" for key, value in configured_paths.values()) + "\n",
                encoding="utf-8",
            )
            env = load_project_environment(script_dir, environ={})
            for platform, (_key, expected_path) in configured_paths.items():
                process = mock.Mock(returncode=0)
                process.communicate = mock.AsyncMock(return_value=(b"", b""))

                with self.subTest(platform=platform):
                    with mock.patch(
                        "batch.asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(return_value=process),
                    ) as create_process:
                        runners = create_platform_runners(
                            script_dir,
                            env,
                            platform=platform,
                        )
                        await runners.extract_audio(str(script_dir / "video.mkv"))
                    self.assertEqual(create_process.await_args.args[0], expected_path)

    async def test_ffmpeg_defaults_when_project_env_has_no_override(self):
        commands = []

        async def fake_stage_command(command, **_kwargs):
            commands.append(command)
            pathlib.Path(command[10]).write_bytes(b"wav")
            return ""

        with tempfile.TemporaryDirectory() as temp_dir:
            script_dir = pathlib.Path(temp_dir)
            with mock.patch("batch._run_stage_command", side_effect=fake_stage_command):
                runners = create_platform_runners(
                    script_dir,
                    {},
                    platform="posix",
                )
                await runners.extract_audio(str(script_dir / "video.mkv"))

        self.assertEqual(commands[0][0], "ffmpeg")

    async def test_empty_dotenv_ffmpeg_value_uses_default(self):
        from batch import load_project_environment

        commands = []

        async def fake_stage_command(command, **_kwargs):
            commands.append(command)
            pathlib.Path(command[10]).write_bytes(b"wav")
            return ""

        with tempfile.TemporaryDirectory() as temp_dir:
            script_dir = pathlib.Path(temp_dir)
            (script_dir / ".env").write_text(
                "FFMPEG_PATH_LINUX=\n",
                encoding="utf-8",
            )
            env = load_project_environment(script_dir, environ={})
            with mock.patch("batch._run_stage_command", side_effect=fake_stage_command):
                runners = create_platform_runners(
                    script_dir,
                    env,
                    platform="posix",
                )
                await runners.extract_audio(str(script_dir / "video.mkv"))

        self.assertEqual(commands[0][0], "ffmpeg")


if __name__ == "__main__":
    unittest.main()
