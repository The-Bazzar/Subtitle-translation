import asyncio
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from batch_cache import (
    bind_wav_artifact,
    build_asr_fingerprint_for_artifact,
    read_valid_asr_cache,
    write_asr_cache_for_artifact,
    write_prepare_state,
)
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
    WorkerCommand,
    WorkerExitedError,
    WorkerResult,
    resolve_source_language,
)


class ResourceLimitsTests(unittest.TestCase):
    def test_detects_automatic_cpu_io_and_fixed_nvenc_capacity(self):
        self.assertEqual(ResourceLimits.detect(logical_cpus=32).cpu_io, 8)
        self.assertEqual(ResourceLimits.detect(logical_cpus=2).cpu_io, 1)
        self.assertEqual(ResourceLimits.detect(logical_cpus=32).nvenc, 4)

    def test_detect_uses_os_cpu_count_and_never_returns_less_than_one(self):
        with mock.patch("batch_scheduler.os.cpu_count", return_value=None):
            self.assertEqual(ResourceLimits.detect().cpu_io, 1)
        with mock.patch("batch_scheduler.os.cpu_count", return_value=12):
            self.assertEqual(ResourceLimits.detect().cpu_io, 3)

    def test_rejects_non_positive_capacities(self):
        for limits in ((0, 4), (1, 0)):
            with self.subTest(limits=limits):
                with self.assertRaises(ValueError):
                    ResourceLimits(cpu_io=limits[0], nvenc=limits[1])


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

    def test_task_records_successful_stage_progress(self):
        task = BatchTask(index=1, url="video")

        task.start("download")
        task.advance("prepare")
        task.succeed("wav_ready")

        self.assertIs(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.stage, "wav_ready")
        self.assertIsNotNone(task.started_at)
        self.assertIsNotNone(task.finished_at)
        self.assertGreaterEqual(task.elapsed_seconds, 0)

    def test_pending_task_cannot_succeed_without_starting(self):
        task = BatchTask(index=1, url="video")

        with self.assertRaisesRegex(RuntimeError, "cannot succeed from pending"):
            task.succeed("wav_ready")

        self.assertIs(task.state, TaskState.PENDING)

    def test_failure_is_terminal_without_affecting_other_tasks(self):
        failed = BatchTask(index=1, url="bad")
        pending = BatchTask(index=2, url="good")

        failed.fail(stage="download", detail="network error")

        self.assertIs(failed.state, TaskState.FAILED)
        self.assertEqual(failed.error_detail, "network error")
        self.assertIs(pending.state, TaskState.PENDING)
        with self.assertRaises(RuntimeError):
            failed.advance("prepare")

    def test_every_terminal_state_rejects_later_transitions(self):
        tasks = []
        succeeded = BatchTask(index=1, url="succeeded")
        succeeded.start("download")
        succeeded.succeed("wav_ready")
        tasks.append(succeeded)

        failed = BatchTask(index=2, url="failed")
        failed.fail("download", "failed")
        tasks.append(failed)

        canceled = BatchTask(index=3, url="canceled")
        canceled.cancel("canceled")
        tasks.append(canceled)

        blocked = BatchTask(index=4, url="blocked")
        blocked.block_by_worker_failure("worker failed")
        tasks.append(blocked)

        for task in tasks:
            with self.subTest(state=task.state):
                with self.assertRaises(RuntimeError):
                    task.advance("prepare")


class AcquisitionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.output_dir.cleanup)

    def write_wav(self, name, data=b"wav"):
        wav_path = pathlib.Path(self.output_dir.name) / name
        wav_path.write_bytes(data)
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

    async def test_missing_or_empty_wav_fails_at_extract_audio(self):
        missing = pathlib.Path(self.output_dir.name) / "missing.wav"
        empty = self.write_wav("empty.wav", data=b"")

        for wav_path in (missing, empty):
            with self.subTest(wav_path=wav_path):
                tasks = await self.run_with_wav_output(wav_path)
                self.assertIs(tasks[0].state, TaskState.FAILED)
                self.assertEqual(tasks[0].stage, "extract_audio")
                self.assertIn("non-empty regular file", tasks[0].error_detail)
                self.assertEqual(aggregate_exit_code(tasks), 1)

    async def test_successful_task_stops_after_wav_preparation(self):
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

        self.assertEqual(
            calls,
            [
                ("download", "one"),
                ("prepare", "one.original.mkv"),
                ("extract_audio", "one.mkv"),
            ],
        )
        self.assertIs(tasks[0].state, TaskState.SUCCEEDED)
        self.assertEqual(tasks[0].stage, "wav_ready")
        self.assertEqual(tasks[0].wav_path, pathlib.Path(self.output_dir.name) / "one.wav")
        self.assertEqual(aggregate_exit_code(tasks), 0)

    async def test_one_failure_does_not_pollute_other_tasks(self):
        async def download(url):
            if url == "bad":
                raise RuntimeError("network error")
            return f"{url}.original.mkv"

        async def prepare(_render_video):
            return "good.mkv"

        async def extract_audio(_edit_video):
            return self.write_wav("good.wav")

        scheduler = AcquisitionScheduler(
            urls=["bad", "good"],
            limits=ResourceLimits(cpu_io=2),
            runners=AcquisitionRunners(download, prepare, extract_audio),
        )
        tasks = await scheduler.run()

        self.assertIs(tasks[0].state, TaskState.FAILED)
        self.assertIs(tasks[1].state, TaskState.SUCCEEDED)
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

        async def prepare(value):
            return value

        async def extract_audio(value):
            await cpu_io_stage(value)
            return self.write_wav(f"{pathlib.Path(value).name}.wav")

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

    async def test_cancellation_marks_active_task_canceled(self):
        started = asyncio.Event()

        async def download(_url):
            started.set()
            await asyncio.Event().wait()

        async def unused(value):
            return value

        scheduler = AcquisitionScheduler(
            urls=["cancel-me"],
            limits=ResourceLimits(cpu_io=1),
            runners=AcquisitionRunners(download, unused, unused),
        )
        run_task = asyncio.create_task(scheduler.run())
        await started.wait()
        run_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await run_task
        self.assertIs(scheduler.tasks[0].state, TaskState.CANCELED)

    async def test_outer_cancellation_cancels_and_drains_all_child_tasks(self):
        async def download(_url):
            await asyncio.Event().wait()

        async def unused(value):
            return value

        scheduler = AcquisitionScheduler(
            urls=["first", "second"],
            limits=ResourceLimits(cpu_io=2),
            runners=AcquisitionRunners(download, unused, unused),
        )
        run_task = asyncio.create_task(scheduler.run())
        asyncio.get_running_loop().call_soon(run_task.cancel)

        with self.assertRaises(asyncio.CancelledError):
            await run_task

        self.assertTrue(
            all(task.state is TaskState.CANCELED for task in scheduler.tasks)
        )

    async def test_child_cancellation_cancels_and_drains_siblings(self):
        both_started = asyncio.Event()
        sibling_finished = asyncio.Event()
        started = set()

        async def download(url):
            started.add(url)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            if url == "canceled-child":
                raise asyncio.CancelledError
            try:
                await asyncio.Event().wait()
            finally:
                sibling_finished.set()

        async def unused(value):
            return value

        scheduler = AcquisitionScheduler(
            urls=["canceled-child", "sibling"],
            limits=ResourceLimits(cpu_io=2),
            runners=AcquisitionRunners(download, unused, unused),
        )

        with self.assertRaises(asyncio.CancelledError):
            await scheduler.run()

        self.assertTrue(sibling_finished.is_set())
        self.assertTrue(
            all(task.state is TaskState.CANCELED for task in scheduler.tasks)
        )


class FakeAsrController:
    def __init__(self, config, calls, *, fail_name="", crash_name=""):
        self.config = config
        self.calls = calls
        self.fail_name = fail_name
        self.crash_name = crash_name

    def start(self):
        self.calls.append(("worker_start",))

    def close(self):
        self.calls.append(("worker_shutdown",))

    def abort(self):
        self.calls.append(("worker_abort",))

    def load_asr(self):
        self.calls.append(("load_asr",))
        return WorkerResult(command=WorkerCommand.LOAD_ASR, ok=True)

    def transcribe(self, artifact):
        wav_path = pathlib.Path(artifact.wav_snapshot.path).resolve()
        self.calls.append(("transcribe", wav_path.name))
        if wav_path.name == self.crash_name:
            raise WorkerExitedError(17, WorkerCommand.TRANSCRIBE)
        if wav_path.name == self.fail_name:
            return WorkerResult(
                command=WorkerCommand.TRANSCRIBE,
                ok=False,
                path=str(wav_path),
                error_type="ValueError",
                error="fake ASR failure",
            )
        edit_video = pathlib.Path(artifact.edit_snapshot.path)
        source_language = resolve_source_language(edit_video)
        fingerprint = build_asr_fingerprint_for_artifact(
            artifact,
            model=self.config.model,
            compute_type=self.config.compute_type,
            source_language=source_language,
            asr_options=self.config.options_dict(),
        )
        output_path = write_asr_cache_for_artifact(
            artifact,
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
        return WorkerResult(command=WorkerCommand.UNLOAD_ASR, ok=True)


class SlowAsrController(FakeAsrController):
    def __init__(self, config, calls):
        super().__init__(config, calls)
        self.transcribe_started = threading.Event()
        self.release_transcribe = threading.Event()

    def transcribe(self, artifact):
        self.transcribe_started.set()
        self.release_transcribe.wait()
        return super().transcribe(artifact)

    def abort(self):
        super().abort()
        self.release_transcribe.set()


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

    def artifact(self, media):
        prepared_state = write_prepare_state(media[0], media[1])
        return bind_wav_artifact(media[1], media[2], prepared_state.generation)

    def fingerprint(self, artifact):
        return build_asr_fingerprint_for_artifact(
            artifact,
            model=self.config.model,
            compute_type=self.config.compute_type,
            source_language=resolve_source_language(artifact.edit_snapshot.path),
            asr_options=self.config.options_dict(),
        )

    def write_cache(self, media, text="cached"):
        artifact = self.artifact(media)
        return write_asr_cache_for_artifact(
            artifact,
            self.fingerprint(artifact),
            {
                "segments": [{"start": 0.0, "end": 1.0, "text": text}],
                "language": resolve_source_language(media[1]),
            },
        )

    async def test_asr_starts_after_acquisition_and_skips_valid_cache(self):
        media = {
            "cached": self.create_media("cached", "en-US"),
            "fresh": self.create_media("fresh", "ja"),
        }
        self.write_cache(media["cached"])
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
                ("worker_shutdown",),
            ],
        )
        self.assertTrue(all(task.state is TaskState.SUCCEEDED for task in tasks))
        self.assertTrue(all(task.stage == "asr_ready" for task in tasks))
        self.assertIsNotNone(
            read_valid_asr_cache(
                media["fresh"][1],
                self.fingerprint(tasks[1].wav_artifact),
                tasks[1].media_generation,
            )
        )

    async def test_all_cached_tasks_still_open_and_close_worker(self):
        media = {"cached": self.create_media("cached")}
        self.write_cache(media["cached"])
        worker_calls = []
        scheduler = AcquisitionScheduler(
            urls=["cached"],
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda config: FakeAsrController(config, worker_calls),
        )
        tasks = await scheduler.run()

        self.assertEqual(worker_calls, [("worker_start",), ("worker_shutdown",)])
        self.assertIs(tasks[0].state, TaskState.SUCCEEDED)
        self.assertEqual(tasks[0].stage, "asr_ready")

    async def test_structured_failure_does_not_stop_later_asr_task(self):
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
        self.assertIn("ValueError: fake ASR failure", tasks[0].error_detail)
        self.assertIs(tasks[1].state, TaskState.SUCCEEDED)
        self.assertIn(("transcribe", "good.wav"), worker_calls)

    async def test_worker_crash_blocks_waiting_tasks_without_restart(self):
        media = {
            "crash": self.create_media("crash"),
            "waiting": self.create_media("waiting"),
        }
        worker_calls = []
        factory_calls = 0

        def worker_factory(config):
            nonlocal factory_calls
            factory_calls += 1
            return FakeAsrController(config, worker_calls, crash_name="crash.wav")

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
        self.assertNotIn(("transcribe", "waiting.wav"), worker_calls)

    async def test_cancel_during_long_transcribe_aborts_without_blocking_loop(self):
        media = {"slow": self.create_media("slow")}
        worker_calls = []
        controller = SlowAsrController(self.config, worker_calls)
        scheduler = AcquisitionScheduler(
            urls=["slow"],
            limits=ResourceLimits(cpu_io=1),
            runners=self.runners_for(media, []),
            asr_config=self.config,
            worker_factory=lambda _config: controller,
        )
        run_task = asyncio.create_task(scheduler.run())
        await asyncio.to_thread(controller.transcribe_started.wait, 1.0)

        run_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, 1.0)
        self.assertIn(("worker_abort",), worker_calls)
        self.assertIs(scheduler.tasks[0].state, TaskState.CANCELED)


if __name__ == "__main__":
    unittest.main()
