from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TypeAlias

from batch_cache import (
    AsrFingerprint,
    WavArtifact,
    asr_sidecar_path,
    bind_wav_artifact,
    build_asr_fingerprint_for_artifact,
    capture_file_snapshot,
    read_valid_asr_cache_for_artifact,
    read_valid_prepare_state,
    write_prepare_state,
)
from whisper_worker import (
    AsrWorkerConfig,
    AsrWorkerController,
    WorkerExitedError,
    WorkerResult,
    WorkerUnresponsiveError,
    resolve_source_language,
)


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    BLOCKED_BY_WORKER_FAILURE = "blocked_by_worker_failure"


class StageCommandError(RuntimeError):
    pass


TERMINAL_STATES = frozenset(
    {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELED,
        TaskState.BLOCKED_BY_WORKER_FAILURE,
    }
)


@dataclass(frozen=True)
class ResourceLimits:
    cpu_io: int
    nvenc: int = 4

    def __post_init__(self) -> None:
        if self.cpu_io < 1 or self.nvenc < 1:
            raise ValueError("resource capacities must be at least one")

    @classmethod
    def detect(cls, logical_cpus: int | None = None) -> ResourceLimits:
        count = logical_cpus if logical_cpus is not None else os.cpu_count()
        return cls(cpu_io=max(1, (count or 1) // 4))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BatchTask:
    url: str
    index: int
    stage: str = "pending"
    state: TaskState = TaskState.PENDING
    render_video_path: Path | None = None
    edit_video_path: Path | None = None
    wav_path: Path | None = None
    wav_artifact: WavArtifact | None = None
    media_generation: str = ""
    asr_path: Path | None = None
    error_detail: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def _reject_terminal_transition(self) -> None:
        if self.state in TERMINAL_STATES:
            raise RuntimeError(f"task {self.index} is already terminal: {self.state.value}")

    def start(self, stage: str) -> None:
        self._reject_terminal_transition()
        if self.state is not TaskState.PENDING:
            raise RuntimeError(f"task {self.index} cannot start from {self.state.value}")
        self.state = TaskState.RUNNING
        self.stage = stage
        self.started_at = _utc_now()

    def advance(self, stage: str) -> None:
        self._reject_terminal_transition()
        if self.state is not TaskState.RUNNING:
            raise RuntimeError(f"task {self.index} cannot advance from {self.state.value}")
        self.stage = stage

    def start_next_phase(self, stage: str) -> None:
        if self.state is not TaskState.SUCCEEDED:
            raise RuntimeError(
                f"task {self.index} cannot start next phase from {self.state.value}"
            )
        self.state = TaskState.RUNNING
        self.stage = stage
        self.finished_at = None

    def succeed(self, stage: str) -> None:
        if self.state is not TaskState.RUNNING:
            raise RuntimeError(
                f"task {self.index} cannot succeed from {self.state.value}"
            )
        self._finish(TaskState.SUCCEEDED, stage=stage)

    def fail(self, stage: str, detail: str) -> None:
        self._finish(TaskState.FAILED, stage=stage, detail=detail)

    def cancel(self, detail: str = "") -> None:
        self._finish(TaskState.CANCELED, stage=self.stage, detail=detail)

    def block_by_worker_failure(self, detail: str = "") -> None:
        self._finish(
            TaskState.BLOCKED_BY_WORKER_FAILURE,
            stage=self.stage,
            detail=detail,
        )

    def _finish(self, state: TaskState, stage: str, detail: str = "") -> None:
        self._reject_terminal_transition()
        if self.started_at is None:
            self.started_at = _utc_now()
        self.state = state
        self.stage = stage
        self.error_detail = detail
        self.finished_at = _utc_now()

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at or _utc_now()
        start = self.started_at or self.created_at
        return max(0.0, (end - start).total_seconds())


StageOutput: TypeAlias = str | os.PathLike[str]
StageRunner: TypeAlias = Callable[[str], Awaitable[StageOutput]]
WorkerFactory: TypeAlias = Callable[[AsrWorkerConfig], AsrWorkerController]


def _validate_wav_output(path: Path) -> None:
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise StageCommandError(
            f"WAV output is not a non-empty regular file: {path}"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0:
        raise StageCommandError(f"WAV output is not a non-empty regular file: {path}")


def _expected_edit_video_path(render_video_path: Path) -> Path | None:
    if not render_video_path.stem.endswith(".original"):
        return None
    return render_video_path.with_name(
        f"{render_video_path.stem.removesuffix('.original')}.mkv"
    )


@dataclass(frozen=True)
class AcquisitionRunners:
    download: StageRunner
    prepare: StageRunner
    extract_audio: StageRunner


class AcquisitionScheduler:
    def __init__(
        self,
        urls: Sequence[str],
        limits: ResourceLimits,
        runners: AcquisitionRunners,
        asr_config: AsrWorkerConfig | None = None,
        worker_factory: WorkerFactory = AsrWorkerController,
    ) -> None:
        self.limits = limits
        self.runners = runners
        self.asr_config = asr_config
        self.worker_factory = worker_factory
        self.tasks = [
            BatchTask(index=index, url=url)
            for index, url in enumerate(urls, start=1)
        ]
        self._cpu_io_slots = asyncio.Semaphore(limits.cpu_io)
        self._nvenc_slots = asyncio.Semaphore(limits.nvenc)

    async def run(self) -> list[BatchTask]:
        child_tasks = [
            asyncio.create_task(self._run_task(task))
            for task in self.tasks
        ]
        try:
            await asyncio.gather(*child_tasks)
        except asyncio.CancelledError:
            for child_task in child_tasks:
                child_task.cancel()
            await asyncio.gather(*child_tasks, return_exceptions=True)
            for task in self.tasks:
                if task.state not in TERMINAL_STATES:
                    task.cancel(detail="batch acquisition canceled")
            raise
        if self.asr_config is not None:
            try:
                await self._run_asr_wave()
            except asyncio.CancelledError:
                for task in self.tasks:
                    if task.state not in TERMINAL_STATES:
                        task.cancel(detail="batch ASR canceled")
                raise
        return self.tasks

    async def _worker_call(self, worker, operation, *args):
        call_task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(call_task)
        except asyncio.CancelledError as cancellation:
            try:
                await asyncio.to_thread(worker.abort)
            except Exception:
                pass
            await asyncio.gather(call_task, return_exceptions=True)
            raise cancellation

    async def _run_asr_wave(self) -> None:
        uncached_tasks: list[tuple[BatchTask, AsrFingerprint]] = []
        for task in self.tasks:
            if task.state is not TaskState.SUCCEEDED:
                continue
            task.start_next_phase("asr_waiting")
            try:
                if task.wav_artifact is None:
                    raise StageCommandError("ASR task is missing bound WAV artifact")
                source_language = resolve_source_language(
                    task.wav_artifact.edit_snapshot.path
                )
                fingerprint = build_asr_fingerprint_for_artifact(
                    task.wav_artifact,
                    model=self.asr_config.model,
                    compute_type=self.asr_config.compute_type,
                    source_language=source_language,
                    asr_options=self.asr_config.options_dict(),
                )
                cached_result = read_valid_asr_cache_for_artifact(
                    task.wav_artifact,
                    fingerprint,
                )
            except Exception as exc:
                task.fail(stage="asr_cache", detail=str(exc))
                continue
            if cached_result is not None:
                task.asr_path = asr_sidecar_path(task.edit_video_path)
                task.succeed(stage="asr_ready")
                continue
            uncached_tasks.append((task, fingerprint))

        worker_tasks = [
            task
            for task in self.tasks
            if task.state is TaskState.RUNNING or task.stage == "asr_ready"
        ]
        if not worker_tasks:
            return

        worker = self.worker_factory(self.asr_config)
        try:
            await self._worker_call(worker, worker.start)
            if uncached_tasks:
                try:
                    load_result = await self._worker_call(worker, worker.load_asr)
                except (WorkerExitedError, WorkerUnresponsiveError) as exc:
                    self._mark_worker_exit(uncached_tasks, 0, str(exc))
                    return
                if not load_result.ok:
                    detail = self._worker_result_error(load_result)
                    for task, _fingerprint in uncached_tasks:
                        task.fail(stage="asr", detail=detail)
                    return

                worker_failed = False
                for index, (task, fingerprint) in enumerate(uncached_tasks):
                    task.advance("asr")
                    try:
                        result = await self._worker_call(
                            worker,
                            worker.transcribe,
                            task.wav_artifact,
                        )
                    except (WorkerExitedError, WorkerUnresponsiveError) as exc:
                        self._mark_worker_exit(uncached_tasks, index, str(exc))
                        worker_failed = True
                        break
                    if not result.ok:
                        task.fail(stage="asr", detail=self._worker_result_error(result))
                        continue
                    cached_result = read_valid_asr_cache_for_artifact(
                        task.wav_artifact,
                        fingerprint,
                    )
                    if cached_result is None:
                        task.fail(
                            stage="asr",
                            detail="Whisper worker did not write a valid ASR recovery sidecar",
                        )
                        continue
                    task.asr_path = asr_sidecar_path(task.edit_video_path)
                    task.succeed(stage="asr_ready")

                if not worker_failed:
                    try:
                        unload_result = await self._worker_call(
                            worker,
                            worker.unload_asr,
                        )
                    except (WorkerExitedError, WorkerUnresponsiveError) as exc:
                        self._fail_last_asr_success(
                            uncached_tasks,
                            stage="unload_asr",
                            detail=str(exc),
                        )
                        return
                    if not unload_result.ok:
                        self._fail_last_asr_success(
                            uncached_tasks,
                            stage="unload_asr",
                            detail=self._worker_result_error(unload_result),
                        )
        except (WorkerExitedError, WorkerUnresponsiveError) as exc:
            running = [
                item
                for item in uncached_tasks
                if item[0].state is TaskState.RUNNING
            ]
            if running:
                self._mark_worker_exit(running, 0, str(exc))
            else:
                self._fail_last_asr_success(
                    uncached_tasks,
                    stage=exc.command.value,
                    detail=str(exc),
                )
        except Exception as exc:
            for task, _fingerprint in uncached_tasks:
                if task.state is TaskState.RUNNING:
                    task.fail(stage="asr", detail=str(exc))
        finally:
            try:
                await asyncio.to_thread(worker.close)
            except Exception as exc:
                self._fail_last_asr_success(
                    uncached_tasks,
                    stage="asr_worker",
                    detail=str(exc),
                )

    @staticmethod
    def _worker_result_error(result: WorkerResult) -> str:
        error_type = getattr(result, "error_type", "")
        error = getattr(result, "error", "")
        return f"{error_type}: {error}" if error_type else str(error)

    @staticmethod
    def _mark_worker_exit(
        tasks: Sequence[tuple[BatchTask, AsrFingerprint]],
        current_index: int,
        detail: str,
    ) -> None:
        current_task = tasks[current_index][0]
        if current_task.state is TaskState.RUNNING:
            current_task.fail(stage="asr", detail=detail)
        for task, _fingerprint in tasks[current_index + 1:]:
            if task.state is TaskState.RUNNING:
                task.block_by_worker_failure(detail=detail)

    @staticmethod
    def _fail_last_asr_success(
        tasks: Sequence[tuple[BatchTask, AsrFingerprint]],
        *,
        stage: str,
        detail: str,
    ) -> None:
        for task, _fingerprint in reversed(tasks):
            if task.state is TaskState.SUCCEEDED:
                task.start_next_phase(stage)
                task.fail(stage=stage, detail=detail)
                return

    async def _run_task(self, task: BatchTask) -> None:
        try:
            task.start("download")
            async with self._cpu_io_slots:
                render_video = await self.runners.download(task.url)
            task.render_video_path = Path(render_video)

            task.advance("prepare")
            prepared_state = None
            expected_edit = _expected_edit_video_path(task.render_video_path)
            if self.asr_config is not None and expected_edit is not None:
                prepared_state = read_valid_prepare_state(
                    task.render_video_path,
                    expected_edit,
                )
            if prepared_state is None:
                render_snapshot = (
                    capture_file_snapshot(task.render_video_path)
                    if self.asr_config is not None
                    else None
                )
                async with self._nvenc_slots:
                    edit_video = await self.runners.prepare(str(task.render_video_path))
                task.edit_video_path = Path(edit_video)
                if self.asr_config is not None:
                    prepared_state = write_prepare_state(
                        task.render_video_path,
                        task.edit_video_path,
                        expected_render_snapshot=render_snapshot,
                    )
            else:
                task.edit_video_path = Path(prepared_state.edit_snapshot.path)
            if prepared_state is not None:
                task.media_generation = prepared_state.generation

            task.advance("extract_audio")
            async with self._cpu_io_slots:
                wav_path = await self.runners.extract_audio(str(task.edit_video_path))
            task.wav_path = Path(wav_path)
            _validate_wav_output(task.wav_path)
            if prepared_state is not None:
                task.wav_artifact = bind_wav_artifact(
                    task.edit_video_path,
                    task.wav_path,
                    prepared_state.generation,
                )
            task.succeed(stage="wav_ready")
        except asyncio.CancelledError:
            if task.state not in TERMINAL_STATES:
                task.cancel(detail="batch acquisition canceled")
            raise
        except Exception as exc:
            if task.state not in TERMINAL_STATES:
                task.fail(stage=task.stage, detail=str(exc))


def aggregate_exit_code(tasks: Sequence[BatchTask]) -> int:
    return 0 if all(task.state is TaskState.SUCCEEDED for task in tasks) else 1
