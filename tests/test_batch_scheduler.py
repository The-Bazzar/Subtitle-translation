import asyncio
import contextlib
import io
import json
import multiprocessing
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from unittest import mock

import batch_scheduler
from batch import (
    _run_stage_command,
    build_parser,
    build_stage_environment,
    create_platform_postprocess_runner,
    create_platform_runners,
    run_acquisition,
    write_report,
)
from batch_cache import build_asr_fingerprint, read_valid_asr_cache, write_asr_cache
from batch_scheduler import (
    AcquisitionRunners,
    AcquisitionScheduler,
    BatchTask,
    ResourceLimits,
    TaskState,
    aggregate_exit_code,
)
from whisper_worker import (
    AsrWorkerConfig,
    AsrWorkerController,
    WorkerCommand,
    WorkerExitedError,
    WorkerResult,
    WorkerUnresponsiveError,
    resolve_source_language,
    write_aligned_candidate_json,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def write_asr_cache_process_target(
    edit_video_path,
    fingerprint,
    result,
    started,
    finished,
):
    started.set()
    try:
        write_asr_cache(edit_video_path, fingerprint, result)
    finally:
        finished.set()


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

    def test_successful_phase_can_explicitly_start_the_next_phase(self):
        task = BatchTask(index=1, url="next")
        task.start("download")
        task.succeed("wav_ready")

        task.start_next_phase("asr_waiting")

        self.assertIs(task.state, TaskState.RUNNING)
        self.assertEqual(task.stage, "asr_waiting")
        self.assertIsNone(task.finished_at)


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

    async def test_acquisition_cancellation_sets_release_without_spawning_worker(self):
        for canceled_stage in ("download", "prepare", "extract_audio"):
            with self.subTest(canceled_stage=canceled_stage):
                root = pathlib.Path(self.output_dir.name)
                render_video = root / f"{canceled_stage}.original.mkv"
                edit_video = root / f"{canceled_stage}.mkv"
                wav_path = root / f"{canceled_stage}.wav"
                render_video.write_bytes(b"original")
                edit_video.write_bytes(b"edit")
                wav_path.write_bytes(b"wav")
                started = asyncio.Event()
                blocker = asyncio.Event()
                worker_factory = mock.Mock(
                    side_effect=AssertionError("worker must not be spawned")
                )

                async def stage(name, result):
                    if name == canceled_stage:
                        started.set()
                        await blocker.wait()
                    return result

                async def download(_url):
                    return await stage("download", render_video)

                async def prepare(_render_video):
                    return await stage("prepare", edit_video)

                async def extract_audio(_edit_video):
                    return await stage("extract_audio", wav_path)

                scheduler = AcquisitionScheduler(
                    urls=["url-1", "url-2"],
                    limits=ResourceLimits(cpu_io=1),
                    runners=AcquisitionRunners(download, prepare, extract_audio),
                    asr_config=AsrWorkerConfig(),
                    worker_factory=worker_factory,
                    postprocess_runner=mock.AsyncMock(),
                )
                run_task = asyncio.create_task(scheduler.run())
                await asyncio.wait_for(started.wait(), 1.0)
                run_task.cancel()

                with self.assertRaises(asyncio.CancelledError):
                    await run_task

                self.assertTrue(scheduler.worker_released.is_set())
                self.assertTrue(
                    all(task.state is TaskState.CANCELED for task in scheduler.tasks)
                )
                self.assertIsNone(scheduler._worker)
                worker_factory.assert_not_called()


class FakeAsrController:
    def __init__(
        self,
        config,
        calls,
        *,
        fail_name="",
        crash_name="",
        crash_on_unload=False,
        hang_on_shutdown=False,
        fail_align_name="",
        crash_align_name="",
        invalid_align_name="",
        block_align_name="",
        align_started=None,
        align_release=None,
        align_finished=None,
        abort_error=None,
        close_error=None,
        before_align=None,
        after_align=None,
    ):
        self.config = config
        self.calls = calls
        self.fail_name = fail_name
        self.crash_name = crash_name
        self.crash_on_unload = crash_on_unload
        self.hang_on_shutdown = hang_on_shutdown
        self.fail_align_name = fail_align_name
        self.crash_align_name = crash_align_name
        self.invalid_align_name = invalid_align_name
        self.block_align_name = block_align_name
        self.align_started = align_started
        self.align_release = align_release
        self.align_finished = align_finished
        self.abort_error = abort_error
        self.close_error = close_error
        self.before_align = before_align
        self.after_align = after_align
        self.is_alive = False
        self.alignment_language = ""

    def start(self):
        self.calls.append(("worker_start",))
        self.is_alive = True

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.shutdown()
        self.close()

    def shutdown(self):
        self.calls.append(("worker_shutdown",))
        self.is_alive = False
        if self.hang_on_shutdown:
            raise WorkerUnresponsiveError(
                WorkerCommand.SHUTDOWN,
                "operation timeout exceeded",
                0.1,
            )
        return WorkerResult(command=WorkerCommand.SHUTDOWN, ok=True)

    def close(self):
        self.is_alive = False
        if self.close_error is not None:
            raise self.close_error

    def abort(self):
        self.calls.append(("worker_abort",))
        self.is_alive = False
        if self.align_release is not None:
            self.align_release.set()
        if self.abort_error is not None:
            raise self.abort_error

    def load_asr(self):
        self.calls.append(("load_asr",))
        return WorkerResult(command=WorkerCommand.LOAD_ASR, ok=True)

    def transcribe(self, wav_path):
        wav_path = pathlib.Path(wav_path).resolve()
        self.calls.append(("transcribe", wav_path.name))
        if wav_path.name == self.crash_name:
            self.is_alive = False
            raise WorkerExitedError(17, WorkerCommand.TRANSCRIBE)
        if wav_path.name == self.fail_name:
            return WorkerResult(
                command=WorkerCommand.TRANSCRIBE,
                ok=False,
                path=str(wav_path),
                error_type="ValueError",
                error="fake ASR failure",
            )
        edit_video = wav_path.with_suffix(".mkv")
        source_language = resolve_source_language(edit_video)
        fingerprint = build_asr_fingerprint(
            edit_video,
            model=self.config.model,
            compute_type=self.config.compute_type,
            source_language=source_language,
            asr_options=self.config.options_dict(),
        )
        output_path = write_asr_cache(
            edit_video,
            fingerprint,
            {
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": wav_path.stem}
                ],
                "language": source_language,
            },
        )
        return WorkerResult(
            command=WorkerCommand.TRANSCRIBE,
            ok=True,
            path=str(wav_path),
            output_path=str(output_path),
            language=source_language,
        )

    def unload_asr(self):
        self.calls.append(("unload_asr",))
        if self.crash_on_unload:
            self.is_alive = False
            raise WorkerExitedError(19, WorkerCommand.UNLOAD_ASR)
        return WorkerResult(command=WorkerCommand.UNLOAD_ASR, ok=True)

    def load_align(self, language):
        self.calls.append(("load_align", language, self.config.align_model or "auto"))
        self.alignment_language = language
        return WorkerResult(
            command=WorkerCommand.LOAD_ALIGN,
            ok=True,
            language=language,
        )

    def align(self, sidecar_path, generation, candidate_path=None):
        sidecar_path = pathlib.Path(sidecar_path).resolve()
        candidate_path = pathlib.Path(candidate_path).resolve()
        self.calls.append(("align", sidecar_path.name, self.alignment_language))
        if sidecar_path.name == self.block_align_name:
            try:
                if self.align_started is not None:
                    self.align_started.set()
                if self.align_release is None or not self.align_release.wait(2.0):
                    raise AssertionError("blocked ALIGN was not released")
            finally:
                if self.align_finished is not None:
                    self.align_finished.set()
        if self.before_align is not None:
            self.before_align(sidecar_path)
        if sidecar_path.name == self.crash_align_name:
            self.is_alive = False
            raise WorkerExitedError(29, WorkerCommand.ALIGN)
        if sidecar_path.name == self.fail_align_name:
            return WorkerResult(
                command=WorkerCommand.ALIGN,
                ok=False,
                path=str(sidecar_path),
                error_type="ValueError",
                error="fake alignment failure",
            )
        if sidecar_path.name == self.invalid_align_name:
            candidate_path.write_text("{}", encoding="utf-8")
            return WorkerResult(
                command=WorkerCommand.ALIGN,
                ok=True,
                path=str(sidecar_path),
                output_path=str(candidate_path),
                language=self.alignment_language,
                generation=generation,
            )
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if payload["generation"] != generation:
            raise AssertionError("scheduler passed stale ASR generation")
        result = payload["result"]
        written_candidate = write_aligned_candidate_json(
            sidecar_path,
            generation,
            candidate_path,
            {
                "language": result["language"],
                "segments": [
                    {
                        **segment,
                        "words": [
                            {
                                "word": segment["text"],
                                "start": segment["start"],
                                "end": segment["end"],
                            }
                        ],
                    }
                    for segment in result["segments"]
                ],
            },
        )
        if self.after_align is not None:
            self.after_align(sidecar_path)
        return WorkerResult(
            command=WorkerCommand.ALIGN,
            ok=True,
            path=str(sidecar_path),
            output_path=str(written_candidate),
            language=result["language"],
            generation=generation,
        )

    def unload_align(self):
        self.calls.append(("unload_align", self.alignment_language))
        self.alignment_language = ""
        return WorkerResult(command=WorkerCommand.UNLOAD_ALIGN, ok=True)


class BatchAsrExecutionTests(unittest.TestCase):
    def test_run_acquisition_enables_asr_wave_with_project_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            render_video = root / "video.original.mkv"
            edit_video = root / "video.mkv"
            wav_path = root / "video.wav"
            render_video.write_bytes(b"original")
            edit_video.write_bytes(b"edit")
            wav_path.write_bytes(b"wav")

            async def download(_url):
                return render_video

            async def prepare(_render_video):
                return edit_video

            async def extract_audio(_edit_video):
                return wav_path

            worker_calls = []
            seen_configs = []

            def worker_factory(config):
                seen_configs.append(config)
                return FakeAsrController(config, worker_calls)

            args = build_parser().parse_args(["url"])
            with mock.patch.dict(
                os.environ,
                {
                    "TORCH_BACKEND": "cpu",
                    "WHISPER_MODEL": "test-model",
                    "HF_TOKEN": "test-token",
                },
                clear=True,
            ):
                tasks = run_acquisition(
                    args,
                    ResourceLimits(cpu_io=1),
                    script_dir=root,
                    runners=AcquisitionRunners(download, prepare, extract_audio),
                    worker_factory=worker_factory,
                    postprocess_runner=mock.AsyncMock(),
                )

        self.assertEqual(seen_configs[0].model, "test-model")
        self.assertEqual(seen_configs[0].device, "cpu")
        self.assertEqual(seen_configs[0].compute_type, "float32")
        self.assertEqual(seen_configs[0].options_dict(), {"batch_size": 8})
        self.assertEqual(seen_configs[0].hf_token, "test-token")
        self.assertEqual(tasks[0].stage, "translated")
        self.assertEqual(worker_calls.count(("load_asr",)), 1)
        self.assertEqual(worker_calls.count(("unload_asr",)), 1)


class AsrWaveSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = pathlib.Path(self.temp_dir.name)
        self.config = AsrWorkerConfig(
            model="fake-model",
            device="cpu",
            compute_type="int8",
            asr_options={"batch_size": 2},
        )

    def create_media(self, name, language="en"):
        render_video = self.root / f"{name}.original.mkv"
        edit_video = self.root / f"{name}.mkv"
        wav_path = self.root / f"{name}.wav"
        render_video.write_bytes(b"original")
        edit_video.write_bytes(b"edit")
        wav_path.write_bytes(b"wav")
        (self.root / f"{name}.info.json").write_text(
            '{"language": "' + language + '"}',
            encoding="utf-8",
        )
        return render_video, edit_video, wav_path

    def runners_for(self, media, acquisition_calls):
        async def download(url):
            acquisition_calls.append(("download", url))
            return media[url][0]

        async def prepare(render_video):
            acquisition_calls.append(("prepare", pathlib.Path(render_video).name))
            for values in media.values():
                if values[0] == pathlib.Path(render_video):
                    return values[1]
            raise AssertionError(f"unknown render video: {render_video}")

        async def extract_audio(edit_video):
            acquisition_calls.append(("extract_audio", pathlib.Path(edit_video).name))
            for values in media.values():
                if values[1] == pathlib.Path(edit_video):
                    return values[2]
            raise AssertionError(f"unknown edit video: {edit_video}")

        return AcquisitionRunners(download, prepare, extract_audio)

    def fingerprint(self, edit_video):
        return build_asr_fingerprint(
            edit_video,
            model=self.config.model,
            compute_type=self.config.compute_type,
            source_language=resolve_source_language(edit_video),
            asr_options=self.config.options_dict(),
        )

    async def test_asr_starts_after_acquisition_and_skips_valid_cache(self):
        media = {
            "cached": self.create_media("cached", "en-US"),
            "fresh": self.create_media("fresh", "ja"),
        }
        write_asr_cache(
            media["cached"][1],
            self.fingerprint(media["cached"][1]),
            {
                "segments": [{"start": 0.0, "end": 1.0, "text": "cached"}],
                "language": "en",
            },
        )
        acquisition_calls = []
        worker_calls = []

        def worker_factory(config):
            extracted = [call for call in acquisition_calls if call[0] == "extract_audio"]
            self.assertEqual(len(extracted), 2)
            return FakeAsrController(config, worker_calls)

        scheduler = AcquisitionScheduler(
            urls=["cached", "fresh"],
            limits=ResourceLimits(cpu_io=2),
            runners=self.runners_for(media, acquisition_calls),
            asr_config=self.config,
            worker_factory=worker_factory,
        )

        tasks = await scheduler.run()

        self.assertEqual(
            worker_calls,
            [
                ("worker_start",),
                ("load_asr",),
                ("transcribe", "fresh.wav"),
                ("unload_asr",),
                ("load_align", "en", "auto"),
                ("align", "cached.asr.json", "en"),
                ("unload_align", "en"),
                ("load_align", "ja", "auto"),
                ("align", "fresh.asr.json", "ja"),
                ("unload_align", "ja"),
                ("worker_shutdown",),
            ],
        )
        self.assertTrue(all(task.state is TaskState.SUCCEEDED for task in tasks))
        self.assertTrue(all(task.stage == "alignment_ready" for task in tasks))
        self.assertEqual(tasks[0].asr_path, media["cached"][1].with_suffix(".asr.json"))
        self.assertEqual(tasks[1].asr_path, media["fresh"][1].with_suffix(".asr.json"))
        self.assertTrue(all(not task.asr_path.exists() for task in tasks))
        self.assertTrue(all(task.json_path.is_file() for task in tasks))

    async def test_all_cached_tasks_still_open_and_close_reusable_controller(self):
        media = {"cached": self.create_media("cached")}
        write_asr_cache(
            media["cached"][1],
            self.fingerprint(media["cached"][1]),
            {
                "segments": [{"start": 0.0, "end": 1.0, "text": "cached"}],
                "language": "en",
            },
        )
        worker_calls = []
        scheduler = AcquisitionScheduler(
            urls=["cached"],
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, worker_calls),
        )

        tasks = await scheduler.run()

        self.assertEqual(
            worker_calls,
            [
                ("worker_start",),
                ("load_align", "en", "auto"),
                ("align", "cached.asr.json", "en"),
                ("unload_align", "en"),
                ("worker_shutdown",),
            ],
        )
        self.assertIs(tasks[0].state, TaskState.SUCCEEDED)
        self.assertEqual(tasks[0].stage, "alignment_ready")

    async def test_structured_task_failure_does_not_stop_later_asr_task(self):
        media = {
            "bad": self.create_media("bad"),
            "good": self.create_media("good"),
        }
        worker_calls = []
        scheduler = AcquisitionScheduler(
            urls=["bad", "good"],
            limits=ResourceLimits(cpu_io=2),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                worker_calls,
                fail_name="bad.wav",
            ),
        )

        tasks = await scheduler.run()

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "asr")
        self.assertIn("ValueError: fake ASR failure", tasks[0].error_detail)
        self.assertIs(tasks[1].state, TaskState.SUCCEEDED)
        self.assertIn(("transcribe", "good.wav"), worker_calls)
        self.assertEqual(worker_calls.count(("unload_asr",)), 1)

    async def test_worker_crash_fails_current_and_blocks_waiting_without_restart(self):
        media = {
            "crash": self.create_media("crash"),
            "waiting": self.create_media("waiting"),
        }
        worker_calls = []
        factory_calls = 0

        def worker_factory(config):
            nonlocal factory_calls
            factory_calls += 1
            return FakeAsrController(
                config,
                worker_calls,
                crash_name="crash.wav",
            )

        scheduler = AcquisitionScheduler(
            urls=["crash", "waiting"],
            limits=ResourceLimits(cpu_io=2),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=worker_factory,
        )

        tasks = await scheduler.run()

        self.assertEqual(factory_calls, 1)
        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertIs(tasks[1].state, TaskState.BLOCKED_BY_WORKER_FAILURE)
        self.assertIn("exit code: 17", tasks[0].error_detail)
        self.assertIn("exit code: 17", tasks[1].error_detail)
        self.assertNotIn(("transcribe", "waiting.wav"), worker_calls)
        self.assertNotIn(("unload_asr",), worker_calls)
        self.assertEqual(worker_calls[-1], ("transcribe", "crash.wav"))
        self.assertTrue(scheduler.worker_released.is_set())
        self.assertFalse(scheduler._worker.is_alive)
        self.assertEqual(aggregate_exit_code(tasks), 1)

    async def test_unresponsive_worker_fails_current_and_blocks_waiting(self):
        asyncio.get_running_loop().slow_callback_duration = 2.0
        media = {
            "hang": self.create_media("heartbeat-hang"),
            "waiting": self.create_media("waiting"),
        }
        controllers = []

        def worker_factory(config):
            controller = AsrWorkerController(
                config,
                backend_factory="tests.test_whisper_worker:FakeBackend",
                heartbeat_interval=0.01,
                max_heartbeat_silence=0.5,
                operation_timeout=0.75,
            )
            controllers.append(controller)
            return controller

        scheduler = AcquisitionScheduler(
            urls=["hang", "waiting"],
            limits=ResourceLimits(cpu_io=2),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=worker_factory,
        )

        tasks = await scheduler.run()

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertIs(tasks[1].state, TaskState.BLOCKED_BY_WORKER_FAILURE)
        self.assertIn("operation timeout", tasks[0].error_detail)
        self.assertIn("operation timeout", tasks[1].error_detail)
        self.assertFalse(controllers[0].is_alive)
        self.assertEqual(aggregate_exit_code(tasks), 1)

    async def test_worker_exit_during_unload_is_reported_as_batch_failure(self):
        media = {"video": self.create_media("video")}
        worker_calls = []
        scheduler = AcquisitionScheduler(
            urls=["video"],
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                worker_calls,
                crash_on_unload=True,
            ),
        )

        tasks = await scheduler.run()

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "unload_asr")
        self.assertIn("exit code: 19", tasks[0].error_detail)
        self.assertTrue(media["video"][1].with_suffix(".asr.json").is_file())
        self.assertEqual(aggregate_exit_code(tasks), 1)

    async def test_unresponsive_shutdown_marks_completed_asr_wave_failed(self):
        media = {"video": self.create_media("video")}
        worker_calls = []
        scheduler = AcquisitionScheduler(
            urls=["video"],
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                worker_calls,
                hang_on_shutdown=True,
            ),
        )

        tasks = await scheduler.run()

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "shutdown")
        self.assertIn("operation timeout", tasks[0].error_detail)
        self.assertEqual(worker_calls.count(("unload_asr",)), 1)
        self.assertEqual(worker_calls.count(("worker_shutdown",)), 1)
        self.assertEqual(aggregate_exit_code(tasks), 1)

    async def test_structured_unload_failure_is_attempted_once_then_shutdown(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0
        media = {"video": self.create_media("video")}
        log_path = self.root / "worker.log"
        controllers = []

        def worker_factory(config):
            controller = AsrWorkerController(
                config,
                backend_factory="tests.test_whisper_worker:FakeBackend",
            )
            controllers.append(controller)
            return controller

        scheduler = AcquisitionScheduler(
            urls=["video"],
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=worker_factory,
        )

        with mock.patch.dict(
            os.environ,
            {
                "WHISPER_WORKER_TEST_LOG": str(log_path),
                "WHISPER_WORKER_TEST_UNLOAD_ERROR": "1",
            },
        ):
            tasks = await scheduler.run()

        self.assertEqual(
            log_path.read_text(encoding="utf-8").splitlines(),
            ["load", "transcribe:video.wav", "unload"],
        )
        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "unload_asr")
        self.assertEqual(tasks[0].error_detail, "RuntimeError: fake unload failure")
        self.assertTrue(controllers[0]._asr_unload_attempted)
        self.assertFalse(controllers[0].is_alive)
        self.assertEqual(controllers[0].exitcode, 0)

    async def test_alignment_groups_mixed_cached_and_fresh_asr_by_iso_language(self):
        media = {
            "ja-first": self.create_media("ja-first", "ja"),
            "en-cached": self.create_media("en-cached", "en-US"),
            "ja-second": self.create_media("ja-second", "ja"),
            "en-fresh": self.create_media("en-fresh", "en-GB"),
        }
        write_asr_cache(
            media["en-cached"][1],
            self.fingerprint(media["en-cached"][1]),
            {
                "segments": [{"start": 0.0, "end": 1.0, "text": "cached"}],
                "language": "en-US",
            },
        )
        worker_calls = []
        postprocess_calls = []

        async def postprocess(task):
            postprocess_calls.append(
                ("postprocess", task.index, task.json_path.name)
            )

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=2),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, worker_calls),
            postprocess_runner=postprocess,
        )

        tasks = await scheduler.run()

        self.assertEqual(
            worker_calls,
            [
                ("worker_start",),
                ("load_asr",),
                ("transcribe", "ja-first.wav"),
                ("transcribe", "ja-second.wav"),
                ("transcribe", "en-fresh.wav"),
                ("unload_asr",),
                ("load_align", "en", "auto"),
                ("align", "en-cached.asr.json", "en"),
                ("align", "en-fresh.asr.json", "en"),
                ("unload_align", "en"),
                ("load_align", "ja", "auto"),
                ("align", "ja-first.asr.json", "ja"),
                ("align", "ja-second.asr.json", "ja"),
                ("unload_align", "ja"),
                ("worker_shutdown",),
            ],
        )
        self.assertEqual(
            postprocess_calls,
            [
                ("postprocess", 2, "en-cached.json"),
                ("postprocess", 4, "en-fresh.json"),
                ("postprocess", 1, "ja-first.json"),
                ("postprocess", 3, "ja-second.json"),
            ],
        )
        self.assertTrue(all(task.state is TaskState.SUCCEEDED for task in tasks))
        self.assertTrue(all(task.stage == "translated" for task in tasks))
        self.assertEqual([task.detected_language for task in tasks], ["ja", "en", "ja", "en"])
        self.assertTrue(all(task.json_path.is_file() for task in tasks))
        self.assertTrue(all(not task.asr_path.exists() for task in tasks))
        self.assertTrue(scheduler.worker_released.is_set())

    async def test_postprocess_overlaps_later_alignment_and_waits_for_release_event(self):
        media = {
            "first": self.create_media("first", "en"),
            "second": self.create_media("second", "en"),
        }
        worker_calls = []
        first_postprocess_started = threading.Event()
        second_alignment_finished = threading.Event()
        observations = []

        def before_align(sidecar_path):
            if sidecar_path.name == "second.asr.json":
                if not first_postprocess_started.wait(1.0):
                    raise AssertionError("first postprocess did not overlap alignment")

        def after_align(sidecar_path):
            if sidecar_path.name == "second.asr.json":
                second_alignment_finished.set()

        async def postprocess(task):
            if task.index == 1:
                observations.append(("postprocess_started", scheduler.worker_released.is_set()))
                first_postprocess_started.set()
                aligned = await asyncio.to_thread(
                    second_alignment_finished.wait,
                    1.0,
                )
                self.assertTrue(aligned)
                await scheduler.worker_released.wait()
                observations.append(("worker_released", True))

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=2),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                worker_calls,
                before_align=before_align,
                after_align=after_align,
            ),
            postprocess_runner=postprocess,
        )

        tasks = await scheduler.run()

        self.assertEqual(
            observations,
            [("postprocess_started", False), ("worker_released", True)],
        )
        self.assertTrue(all(task.stage == "translated" for task in tasks))
        self.assertTrue(scheduler.worker_released.is_set())

    async def test_alignment_failure_keeps_sidecar_and_continues_same_language(self):
        media = {
            "bad": self.create_media("bad", "en"),
            "good": self.create_media("good", "en"),
        }
        worker_calls = []
        postprocessed = []

        async def postprocess(task):
            postprocessed.append(task.index)

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=2),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                worker_calls,
                fail_align_name="bad.asr.json",
            ),
            postprocess_runner=postprocess,
        )

        tasks = await scheduler.run()

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "alignment")
        self.assertIn("ValueError: fake alignment failure", tasks[0].error_detail)
        self.assertTrue(media["bad"][1].with_suffix(".asr.json").is_file())
        self.assertIs(tasks[1].state, TaskState.SUCCEEDED)
        self.assertEqual(tasks[1].stage, "translated")
        self.assertEqual(postprocessed, [2])
        self.assertIn(("align", "good.asr.json", "en"), worker_calls)
        self.assertEqual(worker_calls.count(("unload_align", "en")), 1)
        self.assertTrue(scheduler.worker_released.is_set())

    async def test_parent_rejects_invalid_final_json_and_keeps_sidecar(self):
        media = {"invalid": self.create_media("invalid", "en")}
        worker_calls = []
        postprocess = mock.AsyncMock()
        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                worker_calls,
                invalid_align_name="invalid.asr.json",
            ),
            postprocess_runner=postprocess,
        )

        tasks = await scheduler.run()

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "alignment")
        self.assertTrue(media["invalid"][1].with_suffix(".asr.json").is_file())
        self.assertEqual(worker_calls.count(("unload_align", "en")), 1)
        postprocess.assert_not_awaited()

    async def test_alignment_lock_commits_old_generation_before_new_writer(self):
        media = {"race": self.create_media("race", "en")}
        worker_calls = []
        align_started = threading.Event()
        align_release = threading.Event()
        context = multiprocessing.get_context("spawn")
        writer_started = context.Event()
        writer_finished = context.Event()
        writer = context.Process(
            target=write_asr_cache_process_target,
            args=(
                media["race"][1],
                self.fingerprint(media["race"][1]),
                {
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "new generation"}
                    ],
                    "language": "en",
                },
                writer_started,
                writer_finished,
            ),
        )

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                worker_calls,
                block_align_name="race.asr.json",
                align_started=align_started,
                align_release=align_release,
            ),
        )
        run_task = asyncio.create_task(scheduler.run())
        self.assertTrue(await asyncio.to_thread(align_started.wait, 1.0))
        writer.start()
        self.assertTrue(await asyncio.to_thread(writer_started.wait, 1.0))
        writer_was_blocked = not await asyncio.to_thread(writer_finished.wait, 0.2)
        align_release.set()

        tasks = await asyncio.wait_for(run_task, 2.0)
        await asyncio.to_thread(writer.join, 2.0)
        if writer.is_alive():
            writer.terminate()
            writer.join()

        sidecar_path = media["race"][1].with_suffix(".asr.json")
        current_payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        self.assertTrue(writer_was_blocked)
        self.assertEqual(writer.exitcode, 0)
        self.assertIs(tasks[0].state, TaskState.SUCCEEDED)
        self.assertEqual(current_payload["result"]["segments"][0]["text"], "new generation")
        self.assertTrue(media["race"][1].with_suffix(".json").is_file())
        self.assertEqual(worker_calls.count(("unload_align", "en")), 1)
        lock_path = media["race"][1].with_suffix(".asr.lock")
        self.assertTrue(lock_path.is_file())
        self.assertLessEqual(lock_path.stat().st_size, 1)
        self.assertEqual(list(self.root.glob(".*.candidate.json")), [])

    async def test_failed_alignment_releases_lock_for_new_writer(self):
        media = {"failed": self.create_media("failed", "en")}
        align_started = threading.Event()
        align_release = threading.Event()
        context = multiprocessing.get_context("spawn")
        writer_started = context.Event()
        writer_finished = context.Event()
        writer = context.Process(
            target=write_asr_cache_process_target,
            args=(
                media["failed"][1],
                self.fingerprint(media["failed"][1]),
                {
                    "segments": [{"start": 0.0, "end": 1.0, "text": "new"}],
                    "language": "en",
                },
                writer_started,
                writer_finished,
            ),
        )
        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                [],
                block_align_name="failed.asr.json",
                fail_align_name="failed.asr.json",
                align_started=align_started,
                align_release=align_release,
            ),
        )
        run_task = asyncio.create_task(scheduler.run())
        self.assertTrue(await asyncio.to_thread(align_started.wait, 1.0))
        writer.start()
        self.assertTrue(await asyncio.to_thread(writer_started.wait, 1.0))
        writer_was_blocked = not await asyncio.to_thread(writer_finished.wait, 0.2)
        align_release.set()

        tasks = await asyncio.wait_for(run_task, 2.0)
        await asyncio.to_thread(writer.join, 2.0)
        if writer.is_alive():
            writer.terminate()
            writer.join()

        current = json.loads(
            media["failed"][1].with_suffix(".asr.json").read_text(encoding="utf-8")
        )
        self.assertTrue(writer_was_blocked)
        self.assertEqual(writer.exitcode, 0)
        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(current["result"]["segments"][0]["text"], "new")
        self.assertFalse(media["failed"][1].with_suffix(".json").exists())
        self.assertEqual(list(self.root.glob(".*.candidate.json")), [])

    async def test_canceled_alignment_releases_lock_for_new_writer(self):
        media = {"canceled": self.create_media("canceled", "en")}
        align_started = threading.Event()
        align_release = threading.Event()
        context = multiprocessing.get_context("spawn")
        writer_started = context.Event()
        writer_finished = context.Event()
        writer = context.Process(
            target=write_asr_cache_process_target,
            args=(
                media["canceled"][1],
                self.fingerprint(media["canceled"][1]),
                {
                    "segments": [{"start": 0.0, "end": 1.0, "text": "new"}],
                    "language": "en",
                },
                writer_started,
                writer_finished,
            ),
        )
        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                [],
                block_align_name="canceled.asr.json",
                align_started=align_started,
                align_release=align_release,
            ),
        )
        run_task = asyncio.create_task(scheduler.run())
        self.assertTrue(await asyncio.to_thread(align_started.wait, 1.0))
        writer.start()
        self.assertTrue(await asyncio.to_thread(writer_started.wait, 1.0))
        writer_was_blocked = not await asyncio.to_thread(writer_finished.wait, 0.2)
        run_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, 2.0)
        await asyncio.to_thread(writer.join, 2.0)
        if writer.is_alive():
            writer.terminate()
            writer.join()

        current = json.loads(
            media["canceled"][1].with_suffix(".asr.json").read_text(encoding="utf-8")
        )
        self.assertTrue(writer_was_blocked)
        self.assertEqual(writer.exitcode, 0)
        self.assertEqual(current["result"]["segments"][0]["text"], "new")
        self.assertFalse(media["canceled"][1].with_suffix(".json").exists())
        self.assertEqual(list(self.root.glob(".*.candidate.json")), [])

    async def test_cancel_before_alignment_commit_preserves_sidecar(self):
        media = {"cancel-first": self.create_media("cancel-first", "en")}
        worker_calls = []
        precommit_ready = threading.Event()
        precommit_release = threading.Event()
        postprocess = mock.AsyncMock()
        real_read_aligned_json = batch_scheduler.read_aligned_json

        def block_before_commit(candidate_path):
            result = real_read_aligned_json(candidate_path)
            precommit_ready.set()
            if not precommit_release.wait(2.0):
                raise AssertionError("precommit barrier was not released")
            return result

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, worker_calls),
            postprocess_runner=postprocess,
        )
        with mock.patch(
            "batch_scheduler.read_aligned_json",
            side_effect=block_before_commit,
        ):
            run_task = asyncio.create_task(scheduler.run())
            self.assertTrue(await asyncio.to_thread(precommit_ready.wait, 1.0))
            run_task.cancel()
            for _ in range(100):
                if ("worker_abort",) in worker_calls:
                    break
                await asyncio.sleep(0.01)
            self.assertIn(("worker_abort",), worker_calls)
            precommit_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(run_task, 2.0)

        self.assertIs(scheduler.tasks[0].state, TaskState.CANCELED)
        self.assertTrue(media["cancel-first"][1].with_suffix(".asr.json").is_file())
        self.assertFalse(media["cancel-first"][1].with_suffix(".json").exists())
        self.assertEqual(list(self.root.glob(".*.candidate.json")), [])
        postprocess.assert_not_awaited()

    async def test_commit_before_cancel_completes_and_runs_postprocess(self):
        media = {"commit-first": self.create_media("commit-first", "en")}
        worker_calls = []
        commit_started = threading.Event()
        commit_release = threading.Event()
        cancel_handler_started = threading.Event()
        postprocess = mock.AsyncMock()
        real_promote = batch_scheduler.promote_aligned_candidate
        real_request_cancel = batch_scheduler._AlignmentCommitState.try_request_cancel

        def block_during_commit(candidate_path, final_path):
            commit_started.set()
            if not commit_release.wait(2.0):
                raise AssertionError("commit barrier was not released")
            return real_promote(candidate_path, final_path)

        def observe_cancel_handler(commit_state):
            cancel_handler_started.set()
            return real_request_cancel(commit_state)

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, worker_calls),
            postprocess_runner=postprocess,
        )
        with (
            mock.patch(
                "batch_scheduler.promote_aligned_candidate",
                side_effect=block_during_commit,
            ),
            mock.patch.object(
                batch_scheduler._AlignmentCommitState,
                "try_request_cancel",
                new=observe_cancel_handler,
            ),
        ):
            run_task = asyncio.create_task(scheduler.run())
            self.assertTrue(await asyncio.to_thread(commit_started.wait, 1.0))
            run_task.cancel()
            self.assertTrue(
                await asyncio.to_thread(cancel_handler_started.wait, 1.0)
            )
            aborted_before_release = ("worker_abort",) in worker_calls
            commit_release.set()
            tasks = await asyncio.wait_for(run_task, 2.0)

        final_path = media["commit-first"][1].with_suffix(".json")
        final_result = json.loads(final_path.read_text(encoding="utf-8"))
        self.assertFalse(aborted_before_release)
        self.assertIs(tasks[0].state, TaskState.SUCCEEDED)
        self.assertEqual(tasks[0].stage, "translated")
        self.assertFalse(media["commit-first"][1].with_suffix(".asr.json").exists())
        self.assertEqual(final_result["language"], "en")
        self.assertTrue(final_result["segments"][0]["words"])
        postprocess.assert_awaited_once_with(tasks[0])

    async def test_repeated_cancel_waits_for_commit_thread_and_stable_result(self):
        media = {"double-commit": self.create_media("double-commit", "en")}
        worker_calls = []
        commit_started = threading.Event()
        commit_release = threading.Event()
        transaction_finished = threading.Event()
        postprocess = mock.AsyncMock()
        real_promote = batch_scheduler.promote_aligned_candidate

        def block_during_commit(candidate_path, final_path):
            commit_started.set()
            if not commit_release.wait(2.0):
                raise AssertionError("commit barrier was not released")
            return real_promote(candidate_path, final_path)

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, worker_calls),
            postprocess_runner=postprocess,
        )
        real_transaction = scheduler._run_locked_alignment_transaction

        def observe_transaction(*args):
            try:
                return real_transaction(*args)
            finally:
                transaction_finished.set()

        with (
            mock.patch.object(
                scheduler,
                "_run_locked_alignment_transaction",
                side_effect=observe_transaction,
            ),
            mock.patch(
                "batch_scheduler.promote_aligned_candidate",
                side_effect=block_during_commit,
            ),
            mock.patch(
                "batch_scheduler.WORKER_CALL_JOIN_TIMEOUT_SECONDS",
                0.05,
                create=True,
            ),
        ):
            run_task = asyncio.create_task(scheduler.run())
            self.assertTrue(await asyncio.to_thread(commit_started.wait, 1.0))
            run_task.cancel()
            await asyncio.sleep(0)
            run_task.cancel()
            await asyncio.sleep(0.1)
            returned_before_transaction = run_task.done()
            released_before_transaction = scheduler.worker_released.is_set()
            commit_release.set()
            canceled_outcome = False
            try:
                tasks = await asyncio.wait_for(run_task, 2.0)
            except asyncio.CancelledError:
                canceled_outcome = True
                tasks = scheduler.tasks
            self.assertTrue(await asyncio.to_thread(transaction_finished.wait, 1.0))

        final_path = media["double-commit"][1].with_suffix(".json")
        final_bytes = final_path.read_bytes()
        final_mtime = final_path.stat().st_mtime_ns
        await asyncio.sleep(0.05)
        self.assertFalse(returned_before_transaction)
        self.assertFalse(released_before_transaction)
        self.assertFalse(canceled_outcome)
        self.assertTrue(scheduler.worker_released.is_set())
        self.assertFalse(scheduler._worker.is_alive)
        self.assertIs(tasks[0].state, TaskState.SUCCEEDED)
        self.assertEqual(tasks[0].stage, "translated")
        self.assertFalse(media["double-commit"][1].with_suffix(".asr.json").exists())
        self.assertEqual(final_path.read_bytes(), final_bytes)
        self.assertEqual(final_path.stat().st_mtime_ns, final_mtime)
        postprocess.assert_awaited_once_with(tasks[0])

    async def test_same_tick_triple_cancel_commit_wins_clears_cancellation_count(self):
        media = {"triple-commit": self.create_media("triple-commit", "en")}
        worker_calls = []
        commit_started = threading.Event()
        commit_release = threading.Event()
        postprocess = mock.AsyncMock()
        real_promote = batch_scheduler.promote_aligned_candidate

        def block_during_commit(candidate_path, final_path):
            commit_started.set()
            if not commit_release.wait(2.0):
                raise AssertionError("commit barrier was not released")
            return real_promote(candidate_path, final_path)

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, worker_calls),
            postprocess_runner=postprocess,
        )
        with mock.patch(
            "batch_scheduler.promote_aligned_candidate",
            side_effect=block_during_commit,
        ):
            run_task = asyncio.create_task(scheduler.run())
            self.assertTrue(await asyncio.to_thread(commit_started.wait, 1.0))
            run_task.cancel()
            run_task.cancel()
            run_task.cancel()
            await asyncio.sleep(0)
            commit_release.set()
            tasks = await asyncio.wait_for(run_task, 2.0)

        self.assertIs(tasks[0].state, TaskState.SUCCEEDED)
        self.assertEqual(tasks[0].stage, "translated")
        postprocess.assert_awaited_once_with(tasks[0])
        self.assertTrue(scheduler.worker_released.is_set())
        self.assertFalse(scheduler._worker.is_alive)
        self.assertEqual(run_task.cancelling(), 0)

    async def test_same_tick_triple_cancel_commit_failure_clears_cancellation_count(self):
        media = {"failed-commit": self.create_media("failed-commit", "en")}
        worker_calls = []
        commit_started = threading.Event()
        commit_release = threading.Event()
        postprocess = mock.AsyncMock()

        def fail_during_commit(_candidate_path, _final_path):
            commit_started.set()
            if not commit_release.wait(2.0):
                raise AssertionError("commit barrier was not released")
            raise OSError("fake promote failure")

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, worker_calls),
            postprocess_runner=postprocess,
        )
        with mock.patch(
            "batch_scheduler.promote_aligned_candidate",
            side_effect=fail_during_commit,
        ):
            run_task = asyncio.create_task(scheduler.run())
            self.assertTrue(await asyncio.to_thread(commit_started.wait, 1.0))
            run_task.cancel()
            run_task.cancel()
            run_task.cancel()
            await asyncio.sleep(0)
            commit_release.set()
            tasks = await asyncio.wait_for(run_task, 2.0)

        sidecar_path = media["failed-commit"][1].with_suffix(".asr.json")
        sidecar_bytes = sidecar_path.read_bytes()
        sidecar_mtime = sidecar_path.stat().st_mtime_ns
        await asyncio.sleep(0.05)
        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "alignment")
        self.assertIn("fake promote failure", tasks[0].error_detail)
        self.assertEqual(run_task.cancelling(), 0)
        self.assertTrue(scheduler.worker_released.is_set())
        self.assertFalse(scheduler._worker.is_alive)
        self.assertEqual(sidecar_path.read_bytes(), sidecar_bytes)
        self.assertEqual(sidecar_path.stat().st_mtime_ns, sidecar_mtime)
        self.assertFalse(media["failed-commit"][1].with_suffix(".json").exists())
        self.assertEqual(list(self.root.glob(".*.candidate.json")), [])
        postprocess.assert_not_awaited()

    async def test_repeated_cancel_waits_for_abort_cleanup_before_release(self):
        media = {"double-abort": self.create_media("double-abort", "en")}
        worker_calls = []
        precommit_ready = threading.Event()
        precommit_release = threading.Event()
        transaction_finished = threading.Event()
        postprocess = mock.AsyncMock()
        real_read_aligned_json = batch_scheduler.read_aligned_json

        def block_before_commit(candidate_path):
            result = real_read_aligned_json(candidate_path)
            precommit_ready.set()
            if not precommit_release.wait(2.0):
                raise AssertionError("precommit barrier was not released")
            return result

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, worker_calls),
            postprocess_runner=postprocess,
        )
        real_transaction = scheduler._run_locked_alignment_transaction

        def observe_transaction(*args):
            try:
                return real_transaction(*args)
            finally:
                transaction_finished.set()

        with (
            mock.patch.object(
                scheduler,
                "_run_locked_alignment_transaction",
                side_effect=observe_transaction,
            ),
            mock.patch(
                "batch_scheduler.read_aligned_json",
                side_effect=block_before_commit,
            ),
            mock.patch(
                "batch_scheduler.WORKER_CALL_JOIN_TIMEOUT_SECONDS",
                0.05,
                create=True,
            ),
        ):
            run_task = asyncio.create_task(scheduler.run())
            self.assertTrue(await asyncio.to_thread(precommit_ready.wait, 1.0))
            run_task.cancel()
            for _ in range(100):
                if ("worker_abort",) in worker_calls:
                    break
                await asyncio.sleep(0.01)
            self.assertIn(("worker_abort",), worker_calls)
            run_task.cancel()
            await asyncio.sleep(0.1)
            returned_before_transaction = run_task.done()
            released_before_transaction = scheduler.worker_released.is_set()
            precommit_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(run_task, 2.0)
            self.assertTrue(await asyncio.to_thread(transaction_finished.wait, 1.0))

        self.assertFalse(returned_before_transaction)
        self.assertFalse(released_before_transaction)
        self.assertTrue(scheduler.worker_released.is_set())
        self.assertFalse(scheduler._worker.is_alive)
        self.assertIs(scheduler.tasks[0].state, TaskState.CANCELED)
        self.assertTrue(media["double-abort"][1].with_suffix(".asr.json").is_file())
        self.assertFalse(media["double-abort"][1].with_suffix(".json").exists())
        self.assertEqual(list(self.root.glob(".*.candidate.json")), [])
        postprocess.assert_not_awaited()

    async def test_canceled_candidate_cleanup_failure_is_diagnostic(self):
        media = {"cleanup": self.create_media("cleanup", "en")}
        worker_calls = []
        precommit_ready = threading.Event()
        precommit_release = threading.Event()
        postprocess = mock.AsyncMock()
        real_read_aligned_json = batch_scheduler.read_aligned_json
        real_unlink = pathlib.Path.unlink

        def block_before_commit(candidate_path):
            result = real_read_aligned_json(candidate_path)
            precommit_ready.set()
            if not precommit_release.wait(2.0):
                raise AssertionError("precommit barrier was not released")
            return result

        def fail_candidate_unlink(path, *args, **kwargs):
            if path.name.endswith(".candidate.json"):
                raise OSError("fake candidate unlink failure")
            return real_unlink(path, *args, **kwargs)

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, worker_calls),
            postprocess_runner=postprocess,
        )
        with (
            mock.patch(
                "batch_scheduler.read_aligned_json",
                side_effect=block_before_commit,
            ),
            mock.patch("pathlib.Path.unlink", new=fail_candidate_unlink),
        ):
            run_task = asyncio.create_task(scheduler.run())
            self.assertTrue(await asyncio.to_thread(precommit_ready.wait, 1.0))
            run_task.cancel()
            for _ in range(100):
                if ("worker_abort",) in worker_calls:
                    break
                await asyncio.sleep(0.01)
            self.assertIn(("worker_abort",), worker_calls)
            precommit_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(run_task, 2.0)

        self.assertTrue(media["cleanup"][1].with_suffix(".asr.json").is_file())
        self.assertEqual(len(list(self.root.glob(".*.candidate.json"))), 1)
        self.assertIn(
            ("alignment_candidate_cleanup", "fake candidate unlink failure"),
            scheduler._release_errors,
        )
        self.assertIn("fake candidate unlink failure", scheduler.tasks[0].error_detail)
        self.assertEqual(aggregate_exit_code(scheduler.tasks), 1)
        report_path = self.root / "cleanup-report.txt"
        write_report(report_path, scheduler.tasks, datetime.now())
        self.assertIn(
            "alignment_candidate_cleanup: fake candidate unlink failure",
            report_path.read_text(encoding="utf-8"),
        )
        postprocess.assert_not_awaited()

    async def test_alignment_worker_exit_blocks_waiting_and_sets_release_event(self):
        media = {
            "crash": self.create_media("crash", "en"),
            "waiting": self.create_media("waiting", "ja"),
        }
        worker_calls = []

        async def postprocess(_task):
            raise AssertionError("postprocess must not run after worker exit")

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=2),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                worker_calls,
                crash_align_name="crash.asr.json",
            ),
            postprocess_runner=postprocess,
        )

        tasks = await scheduler.run()

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "alignment")
        self.assertIs(tasks[1].state, TaskState.BLOCKED_BY_WORKER_FAILURE)
        self.assertNotIn(("load_align", "ja", "auto"), worker_calls)
        self.assertTrue(scheduler.worker_released.is_set())
        self.assertFalse(scheduler._worker.is_alive)

    async def test_slow_alignment_cancellation_aborts_worker_and_keeps_sidecar(self):
        media = {"slow": self.create_media("slow-align", "en")}
        ready_path = self.root / "align-ready"
        log_path = self.root / "cancel-worker.log"
        controllers = []
        postprocess = mock.AsyncMock()

        def worker_factory(config):
            controller = AsrWorkerController(
                config,
                backend_factory="tests.test_whisper_worker:FakeBackend",
                heartbeat_interval=0.01,
                max_heartbeat_silence=1.0,
                operation_timeout=5.0,
            )
            controllers.append(controller)
            return controller

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=worker_factory,
            postprocess_runner=postprocess,
        )

        with mock.patch.dict(
            os.environ,
            {
                "WHISPER_WORKER_TEST_ALIGN_READY": str(ready_path),
                "WHISPER_WORKER_TEST_LOG": str(log_path),
            },
        ):
            run_task = asyncio.create_task(scheduler.run())
            for _ in range(200):
                if ready_path.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(ready_path.exists(), "ALIGN did not start")
            canceled_at = asyncio.get_running_loop().time()
            run_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(run_task, 2.0)
            cancel_elapsed = asyncio.get_running_loop().time() - canceled_at

        self.assertLess(cancel_elapsed, 0.5)
        self.assertTrue(scheduler.worker_released.is_set())
        self.assertFalse(controllers[0].is_alive)
        self.assertTrue(media["slow"][1].with_suffix(".asr.json").is_file())
        self.assertFalse(media["slow"][1].with_suffix(".json").exists())
        self.assertEqual(list(self.root.glob(".*.candidate.json")), [])
        self.assertIs(scheduler.tasks[0].state, TaskState.CANCELED)
        self.assertNotIn("unload_align", log_path.read_text(encoding="utf-8").splitlines())
        postprocess.assert_not_awaited()

    async def test_cancellation_preserves_cancelled_error_when_abort_fails(self):
        media = {
            "first": self.create_media("first", "en"),
            "blocked": self.create_media("blocked", "en"),
        }
        worker_calls = []
        align_started = threading.Event()
        align_release = threading.Event()
        align_finished = threading.Event()
        postprocess_started = asyncio.Event()
        postprocess_finished = asyncio.Event()

        async def postprocess(task):
            if task.index != 1:
                raise AssertionError("blocked task must not reach postprocess")
            postprocess_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                postprocess_finished.set()

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                worker_calls,
                block_align_name="blocked.asr.json",
                align_started=align_started,
                align_release=align_release,
                align_finished=align_finished,
                abort_error=OSError("fake abort failure"),
            ),
            postprocess_runner=postprocess,
        )
        run_task = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(postprocess_started.wait(), 1.0)
        self.assertTrue(await asyncio.to_thread(align_started.wait, 1.0))

        run_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, 1.0)

        self.assertTrue(scheduler.worker_released.is_set())
        self.assertTrue(align_finished.is_set())
        self.assertTrue(postprocess_finished.is_set())
        self.assertTrue(
            all(task.state is TaskState.CANCELED for task in scheduler.tasks)
        )
        self.assertIn(("abort", "fake abort failure"), scheduler._release_errors)
        self.assertNotIn(("unload_align", "en"), worker_calls)

    async def test_close_error_is_applied_as_aggregate_failure_without_raising(self):
        media = {"video": self.create_media("video", "en")}
        worker_calls = []
        postprocess = mock.AsyncMock()
        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(
                config,
                worker_calls,
                close_error=OSError("fake close failure"),
            ),
            postprocess_runner=postprocess,
        )

        tasks = await scheduler.run()

        self.assertTrue(scheduler.worker_released.is_set())
        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "close")
        self.assertIn("fake close failure", tasks[0].error_detail)
        self.assertEqual(aggregate_exit_code(tasks), 1)
        postprocess.assert_awaited_once()

    async def test_worker_factory_failure_marks_tasks_and_sets_release_event(self):
        media = {
            "first": self.create_media("first", "en"),
            "second": self.create_media("second", "ja"),
        }

        def worker_factory(_config):
            raise RuntimeError("fake worker factory failure")

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=2),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=worker_factory,
            postprocess_runner=mock.AsyncMock(),
        )

        tasks = await scheduler.run()

        self.assertTrue(scheduler.worker_released.is_set())
        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "asr_worker")
        self.assertIn("fake worker factory failure", tasks[0].error_detail)
        self.assertIs(tasks[1].state, TaskState.BLOCKED_BY_WORKER_FAILURE)

    async def test_postprocess_failure_is_terminal_after_worker_release(self):
        media = {"video": self.create_media("video", "en")}

        async def postprocess(_task):
            await scheduler.worker_released.wait()
            raise RuntimeError("fake translate failure")

        scheduler = AcquisitionScheduler(
            urls=list(media),
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, []),
            postprocess_runner=postprocess,
        )

        tasks = await scheduler.run()

        self.assertTrue(scheduler.worker_released.is_set())
        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertEqual(tasks[0].stage, "postprocess")
        self.assertEqual(tasks[0].error_detail, "fake translate failure")


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

    async def test_postprocess_runner_uses_existing_wrappers_for_all_three_stages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script_dir = pathlib.Path(temp_dir)
            edit_video = script_dir / "video.mkv"
            json_path = script_dir / "video.json"
            edit_video.write_bytes(b"edit")
            json_path.write_text('{"language":"en","segments":[]}', encoding="utf-8")
            task = BatchTask(
                index=1,
                url="url",
                edit_video_path=edit_video,
                json_path=json_path,
            )

            for platform, expected_prefix, wrapper_name in (
                (
                    "nt",
                    ["pwsh", "-NoProfile", "-File"],
                    "translate_srt.ps1",
                ),
                ("posix", ["bash"], "translate_srt.sh"),
            ):
                commands = []

                async def fake_stage_command(command, **kwargs):
                    commands.append((command, kwargs))
                    if kwargs["stage"] == "beautify":
                        script_dir.joinpath("video.beautified.json").write_text(
                            "{}",
                            encoding="utf-8",
                        )
                    return ""

                with self.subTest(platform=platform):
                    with mock.patch(
                        "batch._run_stage_command",
                        side_effect=fake_stage_command,
                    ):
                        with mock.patch("batch.shutil.which", return_value="pwsh"):
                            runner = create_platform_postprocess_runner(
                                script_dir,
                                {"TRANSLATE_PROVIDER": "test"},
                                platform=platform,
                            )
                            await runner(task)

                wrapper = str(script_dir / wrapper_name)
                beautified = str(script_dir / "video.beautified.json")
                self.assertEqual(
                    [entry[1]["stage"] for entry in commands],
                    ["beautify", "glossary", "translate"],
                )
                self.assertEqual(
                    commands[0][0],
                    expected_prefix
                    + [
                        wrapper,
                        str(json_path),
                        "--video",
                        str(edit_video),
                        "--only-beautify",
                    ],
                )
                self.assertEqual(
                    commands[1][0],
                    expected_prefix
                    + [
                        wrapper,
                        beautified,
                        "--video",
                        str(edit_video),
                        "--only-glossary",
                        "--skip-beautify",
                    ],
                )
                self.assertEqual(
                    commands[2][0],
                    expected_prefix
                    + [
                        wrapper,
                        beautified,
                        "--video",
                        str(edit_video),
                        "--skip-beautify",
                        "--skip-knowledge",
                    ],
                )
                self.assertTrue(
                    all(entry[1]["env"]["TRANSLATE_PROVIDER"] == "test" for entry in commands)
                )


if __name__ == "__main__":
    unittest.main()
