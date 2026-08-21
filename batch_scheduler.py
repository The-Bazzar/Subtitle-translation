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


def _validate_wav_output(path: Path) -> None:
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise StageCommandError(
            f"WAV output is not a non-empty regular file: {path}"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0:
        raise StageCommandError(f"WAV output is not a non-empty regular file: {path}")


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
    ) -> None:
        self.limits = limits
        self.runners = runners
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
        return self.tasks

    async def _run_task(self, task: BatchTask) -> None:
        try:
            task.start("download")
            async with self._cpu_io_slots:
                render_video = await self.runners.download(task.url)
            task.render_video_path = Path(render_video)

            task.advance("prepare")
            async with self._nvenc_slots:
                edit_video = await self.runners.prepare(str(task.render_video_path))
            task.edit_video_path = Path(edit_video)

            task.advance("extract_audio")
            async with self._cpu_io_slots:
                wav_path = await self.runners.extract_audio(str(task.edit_video_path))
            task.wav_path = Path(wav_path)
            _validate_wav_output(task.wav_path)
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
