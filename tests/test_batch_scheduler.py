import asyncio
import pathlib
import tempfile
import unittest
from unittest import mock

from batch_scheduler import (
    AcquisitionRunners,
    AcquisitionScheduler,
    BatchTask,
    ResourceLimits,
    TaskState,
    aggregate_exit_code,
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


if __name__ == "__main__":
    unittest.main()
