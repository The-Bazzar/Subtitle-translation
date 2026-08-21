from __future__ import annotations

import asyncio
import inspect
import os
import re
import stat
import tempfile
import threading
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TypeAlias, TypeVar

from batch_cache import (
    AsrFingerprint,
    PreparedMediaState,
    WavArtifact,
    _fsync_parent_directory,
    asr_cache_lock,
    asr_sidecar_path,
    bind_wav_artifact,
    build_asr_fingerprint_from_snapshot,
    capture_file_snapshot,
    invalidate_beautified_cache,
    read_asr_cache_identity,
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
    alignment_candidate_path,
    normalize_language_code,
    promote_aligned_candidate,
    read_aligned_json,
    resolve_source_language,
    validate_alignment_candidate_path,
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


class StageAdvancementStopped(RuntimeError):
    pass


CURRENT_TASK_INDEX: ContextVar[int] = ContextVar("batch_task_index", default=0)


class BatchControl:
    def __init__(self) -> None:
        self.accepting_tasks = True
        self.stage_advancement_open = True
        self.worker_admission_open = True
        self.interrupt_count = 0
        self.force_requested = False
        self._admission_lock = threading.RLock()
        self._command_admission_open = True
        self._next_command_reservation = 0
        self._command_reservations: set[int] = set()
        self._next_callback_token = 0
        self._force_callbacks: dict[int, Callable[[], object]] = {}
        self._force_tasks: set[asyncio.Task[object]] = set()

    @property
    def interrupted(self) -> bool:
        return self.interrupt_count > 0

    def close_for_worker_failure(self) -> None:
        with self._admission_lock:
            self.accepting_tasks = False
            self.worker_admission_open = False

    @property
    def active_command_count(self) -> int:
        with self._admission_lock:
            return len(self._command_reservations)

    def reserve_command(self, stage: str) -> int:
        with self._admission_lock:
            if not self._command_admission_open:
                raise StageAdvancementStopped(f"batch interrupted before {stage}")
            self._next_command_reservation += 1
            reservation = self._next_command_reservation
            self._command_reservations.add(reservation)
            if not self._command_admission_open:
                self._command_reservations.discard(reservation)
                raise StageAdvancementStopped(f"batch interrupted before {stage}")
            return reservation

    def release_command(self, reservation: int) -> None:
        with self._admission_lock:
            self._command_reservations.discard(reservation)

    def register_force_callback(self, callback: Callable[[], object]) -> int:
        with self._admission_lock:
            self._next_callback_token += 1
            token = self._next_callback_token
            self._force_callbacks[token] = callback
            force_requested = self.force_requested
        if force_requested:
            self._schedule_force_callback(callback)
        return token

    def unregister_force_callback(self, token: int) -> None:
        with self._admission_lock:
            self._force_callbacks.pop(token, None)

    def request_interrupt(self) -> int:
        callbacks: tuple[Callable[[], object], ...] = ()
        with self._admission_lock:
            self.interrupt_count += 1
            self.accepting_tasks = False
            self.stage_advancement_open = False
            self.worker_admission_open = False
            self._command_admission_open = False
            if self.interrupt_count >= 2 and not self.force_requested:
                self.force_requested = True
                callbacks = tuple(self._force_callbacks.values())
            interrupt_count = self.interrupt_count
        for callback in callbacks:
            self._schedule_force_callback(callback)
        return interrupt_count

    def _schedule_force_callback(self, callback: Callable[[], object]) -> None:
        try:
            result = callback()
        except Exception:
            return
        if not inspect.isawaitable(result):
            return
        task = asyncio.create_task(result)
        self._force_tasks.add(task)
        task.add_done_callback(self._force_tasks.discard)

    async def wait_for_force_cleanup(self) -> None:
        while self._force_tasks:
            await asyncio.gather(*tuple(self._force_tasks), return_exceptions=True)


class _AlignmentCommitPhase(str, Enum):
    PRECOMMIT = "precommit"
    CANCELED = "canceled"
    COMMITTING = "committing"
    COMMITTED = "committed"
    FAILED = "failed"


class _AlignmentCancelDecision(str, Enum):
    CANCEL_WON = "cancel_won"
    COMMIT_WON = "commit_won"


@dataclass
class _AlignmentCommitState:
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _phase: _AlignmentCommitPhase = _AlignmentCommitPhase.PRECOMMIT

    def try_request_cancel(self) -> _AlignmentCancelDecision:
        self.cancel_requested.set()
        if not self._lock.acquire(blocking=False):
            return _AlignmentCancelDecision.COMMIT_WON
        try:
            if self._phase is _AlignmentCommitPhase.PRECOMMIT:
                self._phase = _AlignmentCommitPhase.CANCELED
                self.cancel_event.set()
                return _AlignmentCancelDecision.CANCEL_WON
            if self._phase is _AlignmentCommitPhase.CANCELED:
                return _AlignmentCancelDecision.CANCEL_WON
            return _AlignmentCancelDecision.COMMIT_WON
        finally:
            self._lock.release()

    def raise_if_canceled(self) -> None:
        if self.cancel_event.is_set():
            raise StageCommandError("alignment transaction canceled before commit")

    @contextmanager
    def commit_section(self) -> Iterator[None]:
        self._lock.acquire()
        try:
            if self._phase is _AlignmentCommitPhase.CANCELED:
                raise StageCommandError("alignment transaction canceled before commit")
            if self._phase is not _AlignmentCommitPhase.PRECOMMIT:
                raise RuntimeError(
                    f"alignment transaction cannot commit from {self._phase.value}"
                )
            self._phase = _AlignmentCommitPhase.COMMITTING
            try:
                yield
            except BaseException:
                self._phase = _AlignmentCommitPhase.FAILED
                raise
            else:
                self._phase = _AlignmentCommitPhase.COMMITTED
        finally:
            self._lock.release()


TERMINAL_STATES = frozenset(
    {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELED,
        TaskState.BLOCKED_BY_WORKER_FAILURE,
    }
)
_ResultT = TypeVar("_ResultT")


def _clear_current_task_cancellation() -> None:
    current = asyncio.current_task()
    if current is not None:
        while current.cancelling() > 0:
            current.uncancel()


async def _await_uninterruptibly(
    task: asyncio.Future[_ResultT],
    *,
    on_cancel: Callable[[asyncio.CancelledError], None] | None = None,
) -> tuple[_ResultT, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            if on_cancel is not None:
                on_cancel(exc)
            if task.done():
                return task.result(), cancellation


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
    asr_generation: str = ""
    json_path: Path | None = None
    ass_path: Path | None = None
    burned_video_path: Path | None = None
    detected_language: str = ""
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
PostprocessRunner: TypeAlias = Callable[[BatchTask], Awaitable[None]]
BurnRunner: TypeAlias = Callable[[BatchTask], Awaitable[None]]
LogSnapshotProvider: TypeAlias = Callable[[int], tuple[str, str]]
ResumeProbe: TypeAlias = Callable[[Path], PreparedMediaState | None]


_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _strip_ansi(value: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", value)


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
    stem = render_video_path.stem
    if not stem.endswith(".original"):
        return None
    return render_video_path.with_name(f"{stem.removesuffix('.original')}.mkv")


def probe_prepared_media(render_video_path: Path) -> PreparedMediaState | None:
    edit_video_path = _expected_edit_video_path(render_video_path)
    if edit_video_path is None:
        return None
    return read_valid_prepare_state(render_video_path, edit_video_path)


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
        postprocess_runner: PostprocessRunner | None = None,
        burn_runner: BurnRunner | None = None,
        control: BatchControl | None = None,
        failure_log_dir: Path | None = None,
        log_snapshot_provider: LogSnapshotProvider | None = None,
        resume_probe: ResumeProbe = probe_prepared_media,
    ) -> None:
        self.limits = limits
        self.runners = runners
        self.asr_config = asr_config
        self.worker_factory = worker_factory
        self.postprocess_runner = postprocess_runner
        self.burn_runner = burn_runner
        self.control = control or BatchControl()
        self.failure_log_dir = (failure_log_dir or Path.cwd()).resolve()
        self.log_snapshot_provider = log_snapshot_provider
        self.resume_probe = resume_probe
        self.failure_log_path: Path | None = None
        self.tasks = [
            BatchTask(index=index, url=url)
            for index, url in enumerate(urls, start=1)
        ]
        self._cpu_io_slots = asyncio.Semaphore(limits.cpu_io)
        self._nvenc_slots = asyncio.Semaphore(limits.nvenc)
        self.worker_released = asyncio.Event()
        self._worker: AsrWorkerController | None = None
        self._active_worker_task: BatchTask | None = None
        self._release_errors: list[tuple[str, str]] = []
        self._cleanup_diagnostics: list[tuple[str, str]] = []
        self._release_errors_lock = threading.Lock()
        self._worker_failure_recorded = False
        self._worker_failure_task: BatchTask | None = None
        self._worker_failure_root_cause: dict[str, object] | None = None
        self._worker_failure_detail: str | None = None
        self._worker_abort_attempted = False
        self._worker_calls: set[asyncio.Task] = set()
        self._media_transaction_locks: dict[str, asyncio.Lock] = {}
        self._active_alignment_commit: _AlignmentCommitState | None = None
        self._force_callback_token = self.control.register_force_callback(
            self._force_abort_worker
        )

    @asynccontextmanager
    async def _media_transaction(
        self,
        media_path: str | os.PathLike[str],
    ) -> AsyncIterator[None]:
        key = str(Path(media_path).resolve())
        lock = self._media_transaction_locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    def request_interrupt(self) -> int:
        count = self.control.request_interrupt()
        commit_state = self._active_alignment_commit
        if commit_state is not None:
            commit_state.try_request_cancel()
        return count

    async def _force_abort_worker(self) -> None:
        worker = self._worker
        if worker is not None:
            await asyncio.to_thread(self._abort_worker, worker)

    async def run(self) -> list[BatchTask]:
        try:
            await asyncio.gather(*(self._run_task(task) for task in self.tasks))
            if self.asr_config is not None and self.control.worker_admission_open:
                await self._run_worker_pipeline()
            elif self.control.interrupted:
                self._cancel_tasks_waiting_for_worker("batch interrupted")
            return self.tasks
        finally:
            if self._worker is None:
                self.worker_released.set()
            await self.control.wait_for_force_cleanup()
            self.control.unregister_force_callback(self._force_callback_token)

    async def _run_worker_pipeline(self) -> None:
        uncached_tasks: list[tuple[BatchTask, AsrFingerprint]] = []
        postprocess_jobs: list[asyncio.Task[None]] = []
        for task in self.tasks:
            if task.state is not TaskState.SUCCEEDED:
                continue
            if not self.control.worker_admission_open:
                self._finish_closed_worker_admission(task)
                continue
            task.start_next_phase("asr_waiting")
            try:
                if task.edit_video_path is None or task.wav_path is None:
                    raise StageCommandError("ASR task is missing edit-video or WAV path")
                if task.wav_artifact is None:
                    raise StageCommandError("ASR task is missing bound WAV artifact")
                source_language = resolve_source_language(
                    task.wav_artifact.edit_snapshot.path
                )
                fingerprint = build_asr_fingerprint_from_snapshot(
                    task.wav_artifact.edit_snapshot,
                    model=self.asr_config.model,
                    compute_type=self.asr_config.compute_type,
                    source_language=source_language,
                    asr_options=self.asr_config.options_dict(),
                )
                cached_entry = read_valid_asr_cache_for_artifact(
                    task.wav_artifact,
                    fingerprint,
                )
            except Exception as exc:
                task.fail(stage="asr_cache", detail=str(exc))
                continue
            if cached_entry is not None:
                task.asr_path = asr_sidecar_path(task.edit_video_path)
                task.asr_generation = cached_entry.generation
                try:
                    task.detected_language = self._normalize_detected_language(
                        cached_entry.result["language"]
                    )
                except StageCommandError as exc:
                    task.fail(stage="asr_cache", detail=str(exc))
                    continue
                task.advance(stage="asr_ready")
                continue
            uncached_tasks.append((task, fingerprint))

        worker_tasks = [task for task in self.tasks if task.state is TaskState.RUNNING]
        if not worker_tasks:
            self.worker_released.set()
            return

        try:
            worker = self.worker_factory(self.asr_config)
        except Exception as exc:
            self._fail_worker_dependents(
                self._first_worker_dependent(),
                stage="asr_worker",
                detail=str(exc),
            )
            self.worker_released.set()
            return
        self._worker = worker
        pipeline_cancellation: asyncio.CancelledError | None = None
        try:
            worker.start()
            worker_failed = await self._run_asr_wave(worker, uncached_tasks)
            if not worker_failed and self.control.worker_admission_open:
                await self._run_alignment_wave(worker, postprocess_jobs)
        except asyncio.CancelledError as exc:
            pipeline_cancellation = exc
            self._cancel_pipeline(worker, postprocess_jobs)
        except (WorkerExitedError, WorkerUnresponsiveError) as exc:
            self._handle_worker_failure(
                self._first_worker_dependent(), exc.command.value, exc
            )
        except Exception as exc:
            self._fail_worker_dependents(
                self._first_worker_dependent(),
                stage="asr_worker",
                detail=str(exc),
            )
        finally:
            release_task = asyncio.create_task(self._release_worker(worker))
            _, release_cancellation = await _await_uninterruptibly(release_task)
            if pipeline_cancellation is None and release_cancellation is not None:
                pipeline_cancellation = release_cancellation
                self._cancel_worker_dependents("batch worker pipeline canceled")
                for job in postprocess_jobs:
                    job.cancel()
            if pipeline_cancellation is not None and postprocess_jobs:
                postprocess_cleanup = asyncio.ensure_future(
                    asyncio.gather(*postprocess_jobs, return_exceptions=True)
                )
                await _await_uninterruptibly(postprocess_cleanup)
            if pipeline_cancellation is not None:
                self._apply_release_error()

        if pipeline_cancellation is not None:
            raise pipeline_cancellation
        if postprocess_jobs:
            await asyncio.gather(*postprocess_jobs)
        self._apply_release_error()

    async def _run_asr_wave(
        self,
        worker: AsrWorkerController,
        uncached_tasks: Sequence[tuple[BatchTask, AsrFingerprint]],
    ) -> bool:
        if not uncached_tasks:
            return False
        try:
            load_result = await self._worker_call(worker, worker.load_asr)
        except (WorkerExitedError, WorkerUnresponsiveError) as exc:
            self._handle_worker_failure(uncached_tasks[0][0], "asr", exc)
            return True
        if not self.control.worker_admission_open:
            self._cancel_worker_dependents("batch interrupted")
            return True
        if not load_result.ok:
            detail = self._worker_result_error(load_result)
            for task, _fingerprint in uncached_tasks:
                if task.state is TaskState.RUNNING:
                    task.fail(stage="asr", detail=detail)
            return False

        worker_failed = False
        for task, fingerprint in uncached_tasks:
            if not self.control.worker_admission_open:
                self._cancel_worker_dependents("batch interrupted")
                return True
            task.advance("asr")
            self._active_worker_task = task
            try:
                result = await self._worker_call(
                    worker,
                    worker.transcribe,
                    task.wav_artifact,
                )
            except (WorkerExitedError, WorkerUnresponsiveError) as exc:
                self._handle_worker_failure(task, "asr", exc)
                worker_failed = True
                break
            except Exception as exc:
                task.fail(stage="asr", detail=str(exc))
                self._active_worker_task = None
                continue
            self._active_worker_task = None
            if not result.ok:
                task.fail(stage="asr", detail=self._worker_result_error(result))
                continue
            cached_entry = read_valid_asr_cache_for_artifact(
                task.wav_artifact,
                fingerprint,
            )
            if cached_entry is None:
                task.fail(
                    stage="asr",
                    detail="Whisper worker did not write a valid ASR recovery sidecar",
                )
                continue
            try:
                task.detected_language = self._normalize_detected_language(
                    cached_entry.result["language"]
                )
            except StageCommandError as exc:
                task.fail(stage="asr", detail=str(exc))
                continue
            task.asr_path = asr_sidecar_path(task.edit_video_path)
            task.asr_generation = cached_entry.generation
            task.advance(stage="asr_ready")
            if not self.control.worker_admission_open:
                self._cancel_worker_dependents("batch interrupted")
                return True

        if worker_failed:
            return True
        try:
            unload_result = await self._worker_call(worker, worker.unload_asr)
        except Exception as exc:
            self._handle_worker_failure(
                self._first_worker_dependent(), "unload_asr", exc
            )
            return True
        if not unload_result.ok:
            self._handle_worker_failure(
                self._first_worker_dependent(),
                "unload_asr",
                RuntimeError(self._worker_result_error(unload_result)),
            )
            return True
        return False

    async def _run_alignment_wave(
        self,
        worker: AsrWorkerController,
        postprocess_jobs: list[asyncio.Task[None]],
    ) -> None:
        groups: dict[str, list[BatchTask]] = {}
        for task in self.tasks:
            if task.state is TaskState.RUNNING and task.stage == "asr_ready":
                groups.setdefault(task.detected_language, []).append(task)

        ordered_languages = sorted(groups)
        for group_index, language in enumerate(ordered_languages):
            if not self.control.worker_admission_open:
                self._cancel_worker_dependents("batch interrupted")
                return
            group = groups[language]
            try:
                load_result = await self._worker_call(
                    worker,
                    worker.load_align,
                    language,
                )
            except (WorkerExitedError, WorkerUnresponsiveError) as exc:
                self._handle_worker_failure(group[0], "load_align", exc)
                return
            if not load_result.ok:
                detail = self._worker_result_error(load_result)
                for task in group:
                    task.fail(stage="load_align", detail=detail)
                continue

            for task in group:
                if not self.control.worker_admission_open:
                    self._cancel_worker_dependents("batch interrupted")
                    return
                task.advance("alignment")
                self._active_worker_task = task
                candidate_path = alignment_candidate_path(
                    task.asr_path,
                    task.asr_generation,
                )
                commit_state = _AlignmentCommitState()
                self._active_alignment_commit = commit_state
                try:
                    async with self._media_transaction(task.edit_video_path):
                        result = await self._worker_call(
                            worker,
                            self._run_locked_alignment_transaction,
                            worker,
                            task,
                            candidate_path,
                            commit_state,
                            commit_state=commit_state,
                        )
                except (WorkerExitedError, WorkerUnresponsiveError) as exc:
                    self._handle_worker_failure(task, "alignment", exc)
                    return
                except Exception as exc:
                    if self.control.interrupted:
                        task.cancel(detail="batch interrupted during alignment")
                    else:
                        task.fail(stage="alignment", detail=str(exc))
                    self._active_worker_task = None
                    if self.control.interrupted:
                        self._cancel_worker_dependents("batch interrupted")
                        return
                    continue
                finally:
                    self._active_alignment_commit = None
                if not result.ok:
                    task.fail(
                        stage="alignment",
                        detail=self._worker_result_error(result),
                    )
                    self._active_worker_task = None
                    continue
                output_path = Path(result.output_path).resolve()
                self._active_worker_task = None
                task.json_path = output_path
                self._cleanup_wav_after_alignment(task)
                if not self.control.worker_admission_open:
                    task.cancel(detail="batch interrupted after alignment")
                    self._cancel_worker_dependents("batch interrupted")
                    return
                if self.postprocess_runner is None:
                    task.succeed(stage="alignment_ready")
                else:
                    task.advance("postprocess_waiting")
                    postprocess_jobs.append(
                        asyncio.create_task(self._run_postprocess(task))
                    )
                    await asyncio.sleep(0)

            try:
                unload_result = await self._worker_call(
                    worker,
                    worker.unload_align,
                )
            except Exception as exc:
                current_task = self._first_worker_dependent()
                self._handle_worker_failure(current_task, "unload_align", exc)
                if current_task is None:
                    self._record_release_error("unload_align", exc)
                self._block_later_alignment_groups(
                    groups,
                    ordered_languages[group_index + 1:],
                    str(exc),
                )
                return
            if not unload_result.ok:
                detail = self._worker_result_error(unload_result)
                error = RuntimeError(detail)
                current_task = self._first_worker_dependent()
                self._handle_worker_failure(current_task, "unload_align", error)
                if current_task is None:
                    self._record_release_error("unload_align", error)
                self._block_later_alignment_groups(
                    groups,
                    ordered_languages[group_index + 1:],
                    detail,
                )
                return

    async def _run_postprocess(self, task: BatchTask) -> None:
        token = CURRENT_TASK_INDEX.set(task.index)
        try:
            task.advance("postprocess")
            async with self._media_transaction(task.edit_video_path):
                self._validate_postprocess_generation(task)
                self._invalidate_stale_beautified(task)
                async with self._cpu_io_slots:
                    if not self.control.stage_advancement_open:
                        raise StageAdvancementStopped(
                            "batch interrupted before postprocess"
                        )
                    await self.postprocess_runner(task)
                self._validate_postprocess_output_generation(task)
            if not self.control.stage_advancement_open:
                raise StageAdvancementStopped("batch interrupted after postprocess")
            if self.burn_runner is None:
                task.succeed(stage="translated")
                return
            task.advance("burn_waiting")
            await self.worker_released.wait()
            if not self.control.stage_advancement_open:
                raise StageAdvancementStopped("batch interrupted before burn")
            task.advance("burn")
            async with self._nvenc_slots:
                if not self.control.stage_advancement_open:
                    raise StageAdvancementStopped("batch interrupted before burn")
                await self.burn_runner(task)
            task.succeed(stage="burned")
        except asyncio.CancelledError:
            if task.state not in TERMINAL_STATES:
                task.cancel(detail="postprocess canceled")
            raise
        except StageAdvancementStopped as exc:
            if task.state not in TERMINAL_STATES:
                task.cancel(detail=str(exc))
        except Exception as exc:
            if task.state not in TERMINAL_STATES:
                task.fail(stage=task.stage, detail=str(exc))
        finally:
            CURRENT_TASK_INDEX.reset(token)

    @staticmethod
    def _beautified_path(task: BatchTask) -> Path:
        return task.json_path.with_name(f"{task.json_path.stem}.beautified.json")

    def _validate_postprocess_generation(self, task: BatchTask) -> None:
        if task.json_path is None:
            raise StageCommandError("postprocess task is missing aligned JSON")
        read_aligned_json(
            task.json_path,
            expected_media_generation=task.media_generation,
            expected_alignment_generation=task.asr_generation,
        )

    def _invalidate_stale_beautified(self, task: BatchTask) -> None:
        beautified_path = self._beautified_path(task)
        if not beautified_path.exists():
            return
        try:
            read_aligned_json(
                beautified_path,
                expected_media_generation=task.media_generation,
                expected_alignment_generation=task.asr_generation,
            )
        except (OSError, ValueError):
            try:
                invalidate_beautified_cache(task.json_path)
            except Exception as exc:
                self._record_cleanup_diagnostic(
                    "beautified_cache_invalidation",
                    exc,
                )
                raise

    def _validate_postprocess_output_generation(self, task: BatchTask) -> None:
        beautified_path = self._beautified_path(task)
        if not beautified_path.exists():
            return
        try:
            read_aligned_json(
                beautified_path,
                expected_media_generation=task.media_generation,
                expected_alignment_generation=task.asr_generation,
            )
        except (OSError, ValueError) as exc:
            try:
                invalidate_beautified_cache(task.json_path)
            except Exception as cleanup_error:
                self._record_cleanup_diagnostic(
                    "beautified_cache_invalidation",
                    cleanup_error,
                )
            raise StageCommandError(
                "postprocess produced beautified JSON for another generation"
            ) from exc

    def _run_locked_alignment_transaction(
        self,
        worker: AsrWorkerController,
        task: BatchTask,
        candidate_path: Path,
        commit_state: _AlignmentCommitState,
    ) -> WorkerResult:
        candidate = candidate_path.resolve()
        candidate_written = False
        try:
            with asr_cache_lock(
                task.edit_video_path,
                cancel_event=commit_state.cancel_event,
            ):
                commit_state.raise_if_canceled()
                result = worker.align(
                    task.asr_path,
                    task.asr_generation,
                    task.wav_artifact,
                    candidate,
                )
                candidate_written = candidate.is_file()
                if not result.ok:
                    return result
                commit_state.raise_if_canceled()
                if Path(result.path).resolve() != task.asr_path.resolve():
                    raise StageCommandError(
                        f"Whisper worker returned unexpected sidecar path: {result.path}"
                    )
                if result.generation != task.asr_generation:
                    raise StageCommandError(
                        "stale alignment result: worker generation does not match task"
                    )
                response_candidate = validate_alignment_candidate_path(
                    task.asr_path,
                    task.asr_generation,
                    result.output_path,
                )
                if response_candidate != candidate:
                    raise StageCommandError(
                        f"Whisper worker returned unexpected candidate: {response_candidate}"
                    )
                if read_asr_cache_identity(task.asr_path) != (
                    task.asr_generation,
                    task.media_generation,
                ):
                    raise StageCommandError(
                        "stale alignment result: ASR sidecar identity changed"
                    )
                read_aligned_json(
                    candidate,
                    expected_media_generation=task.media_generation,
                    expected_alignment_generation=task.asr_generation,
                )
                with commit_state.commit_section():
                    final_path = task.edit_video_path.with_suffix(".json").resolve()
                    try:
                        invalidate_beautified_cache(final_path)
                    except Exception as exc:
                        self._record_cleanup_diagnostic(
                            "beautified_cache_invalidation",
                            exc,
                        )
                        raise
                    promote_aligned_candidate(candidate, final_path)
                    candidate_written = False
                    task.asr_path.unlink()
                    _fsync_parent_directory(task.asr_path)
                return replace(result, output_path=str(final_path))
        finally:
            if candidate_written or candidate.exists():
                try:
                    candidate.unlink(missing_ok=True)
                    _fsync_parent_directory(candidate)
                except Exception as exc:
                    self._record_release_error("alignment_candidate_cleanup", exc)

    async def _worker_call(
        self,
        worker,
        operation,
        *args,
        cancel_event: threading.Event | None = None,
        commit_state: _AlignmentCommitState | None = None,
    ):
        call_task = asyncio.create_task(asyncio.to_thread(operation, *args))
        self._worker_calls.add(call_task)
        call_task.add_done_callback(self._worker_calls.discard)
        try:
            return await asyncio.shield(call_task)
        except asyncio.CancelledError as cancellation:
            if commit_state is not None:
                decision = commit_state.try_request_cancel()
                if decision is _AlignmentCancelDecision.COMMIT_WON:
                    try:
                        result, _ = await _await_uninterruptibly(
                            call_task,
                            on_cancel=lambda _exc: commit_state.cancel_requested.set(),
                        )
                        return result
                    finally:
                        _clear_current_task_cancellation()
                cancel_event = commit_state.cancel_event
            if cancel_event is not None:
                cancel_event.set()
            self._abort_worker(worker)
            try:
                await _await_uninterruptibly(
                    call_task,
                    on_cancel=(
                        (lambda _exc: commit_state.cancel_requested.set())
                        if commit_state is not None
                        else None
                    ),
                )
            except Exception:
                pass
            raise cancellation

    def _abort_worker(self, worker: AsrWorkerController) -> None:
        if self._worker_abort_attempted:
            return
        self._worker_abort_attempted = True
        try:
            worker.abort()
        except Exception as exc:
            self._record_release_error("abort", exc)

    def _cancel_pipeline(
        self,
        worker: AsrWorkerController,
        postprocess_jobs: Sequence[asyncio.Task[None]],
    ) -> None:
        self._abort_worker(worker)
        self._cancel_worker_dependents("batch worker pipeline canceled")
        for job in postprocess_jobs:
            job.cancel()

    def _record_release_error(self, stage: str, error: object) -> None:
        diagnostic = (stage, str(error))
        with self._release_errors_lock:
            if diagnostic not in self._cleanup_diagnostics:
                self._cleanup_diagnostics.append(diagnostic)
            if diagnostic not in self._release_errors:
                self._release_errors.append(diagnostic)

    def _record_cleanup_diagnostic(self, stage: str, error: object) -> None:
        diagnostic = (stage, str(error))
        with self._release_errors_lock:
            if diagnostic not in self._cleanup_diagnostics:
                self._cleanup_diagnostics.append(diagnostic)

    def _cleanup_wav_after_alignment(self, task: BatchTask) -> None:
        if task.wav_path is None:
            return
        try:
            task.wav_path.unlink(missing_ok=True)
            _fsync_parent_directory(task.wav_path)
        except Exception as exc:
            self._record_cleanup_diagnostic("wav_cleanup", exc)

    def _record_release_failure(self, stage: str, error: object) -> None:
        self._record_release_error(stage, error)
        self._handle_worker_failure(None, "shutdown", error)

    async def _release_worker(self, worker: AsrWorkerController) -> None:
        try:
            if worker.is_alive and not self._worker_abort_attempted:
                result = await self._worker_call(worker, worker.shutdown)
                if not result.ok:
                    self._record_release_failure(
                        "shutdown",
                        RuntimeError(self._worker_result_error(result)),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_release_failure("shutdown", exc)
        finally:
            try:
                await self._worker_call(worker, worker.close)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_release_failure("close", exc)
            finally:
                try:
                    pending_calls = [
                        task for task in self._worker_calls if not task.done()
                    ]
                    if pending_calls:
                        await asyncio.gather(
                            *pending_calls,
                            return_exceptions=True,
                        )
                finally:
                    self.worker_released.set()

    def _normalize_detected_language(self, language: object) -> str:
        try:
            return normalize_language_code(
                language,
                fallback=self.asr_config.source_language,
            )
        except ValueError as exc:
            raise StageCommandError(f"invalid ASR detected language: {language}") from exc

    @staticmethod
    def _worker_result_error(result: WorkerResult) -> str:
        error_type = getattr(result, "error_type", "")
        error = getattr(result, "error", "")
        return f"{error_type}: {error}" if error_type else str(error)

    def _first_worker_dependent(self) -> BatchTask | None:
        return next(
            (
                task
                for task in self.tasks
                if task.state is TaskState.RUNNING
                and task.stage
                in {"asr_waiting", "asr", "asr_ready", "alignment"}
            ),
            None,
        )

    def _fail_worker_dependents(
        self,
        current_task: BatchTask | None,
        *,
        stage: str,
        detail: str,
    ) -> None:
        self.control.close_for_worker_failure()
        if current_task is not None and current_task.state is TaskState.RUNNING:
            current_task.fail(stage=stage, detail=detail)
        for task in self.tasks:
            if task is current_task or task.state is not TaskState.RUNNING:
                continue
            if task.stage in {"asr_waiting", "asr", "asr_ready", "alignment"}:
                task.block_by_worker_failure(detail=detail)

    def _cancel_worker_dependents(self, detail: str) -> None:
        current_task = self._active_worker_task or self._first_worker_dependent()
        if current_task is not None and current_task.state is TaskState.RUNNING:
            current_task.cancel(detail=detail)
        for task in self.tasks:
            if task is current_task or task.state is not TaskState.RUNNING:
                continue
            if task.stage in {"asr_waiting", "asr", "asr_ready", "alignment"}:
                if self.control.interrupted:
                    task.cancel(detail=detail)
                else:
                    task.block_by_worker_failure(detail=detail)

    def _cancel_tasks_waiting_for_worker(self, detail: str) -> None:
        for task in self.tasks:
            if task.state is TaskState.SUCCEEDED:
                task.start_next_phase("asr_waiting")
                task.cancel(detail=detail)

    def _finish_closed_worker_admission(self, task: BatchTask) -> None:
        task.start_next_phase("asr_waiting")
        if self.control.interrupted:
            task.cancel(detail="batch interrupted")
        else:
            task.block_by_worker_failure(detail="worker admission is closed")

    def _handle_worker_failure(
        self,
        current_task: BatchTask | None,
        stage: str,
        error: object,
    ) -> None:
        if self.control.interrupted:
            self._cancel_worker_dependents("batch interrupted")
            return
        if self._worker_failure_recorded:
            return
        self._worker_failure_recorded = True
        self._worker_failure_task = current_task
        self._worker_failure_root_cause = {
            "task_index": current_task.index if current_task is not None else None,
            "stage": stage,
            "error_type": type(error).__name__,
            "message": str(error),
            "worker_exit_code": getattr(error, "exitcode", None),
        }
        self._worker_failure_detail = str(error)
        self.control.close_for_worker_failure()
        self._fail_worker_dependents(current_task, stage=stage, detail=str(error))
        self._write_worker_failure_log(current_task, stage, error)

    def _write_worker_failure_log(
        self,
        current_task: BatchTask | None,
        stage: str,
        error: object,
    ) -> None:
        if self.failure_log_path is not None:
            return
        timestamp = _utc_now()
        context_task = current_task or self._active_worker_task
        if context_task is None:
            context_task = self._first_worker_dependent()
        if context_task is None:
            context_task = next(
                (
                    task
                    for task in reversed(self.tasks)
                    if task.state is not TaskState.PENDING
                ),
                None,
            )
        task_index = context_task.index if context_task is not None else 0
        stdout = ""
        stderr = ""
        if self.log_snapshot_provider is not None:
            try:
                stdout, stderr = self.log_snapshot_provider(task_index)
            except Exception as snapshot_error:
                stderr = f"log snapshot failed: {snapshot_error}"
        worker_stdout = str(getattr(error, "stdout", "") or "")
        worker_stderr = str(getattr(error, "stderr", "") or "")
        if self._worker is not None and (not worker_stdout or not worker_stderr):
            capture_output = getattr(self._worker, "captured_output", None)
            if callable(capture_output):
                try:
                    captured_stdout, captured_stderr = capture_output()
                    worker_stdout = worker_stdout or captured_stdout
                    worker_stderr = worker_stderr or captured_stderr
                except Exception as capture_error:
                    worker_stderr = (
                        worker_stderr
                        or f"worker log capture failed: {capture_error}"
                    )
        if worker_stdout:
            stdout = "\n".join(
                part for part in (stdout, f"[worker]\n{worker_stdout.rstrip()}") if part
            )
        if worker_stderr:
            stderr = "\n".join(
                part for part in (stderr, f"[worker]\n{worker_stderr.rstrip()}") if part
            )
        exitcode = getattr(error, "exitcode", None)
        if exitcode is None and self._worker is not None:
            exitcode = getattr(self._worker, "exitcode", None)
        task_lines = [
            f"  [{task.index:02d}] state={task.state.value} stage={task.stage}"
            for task in self.tasks
        ]
        queue_snapshot = [
            f"cpu_io_available={getattr(self._cpu_io_slots, '_value', 'unknown')}",
            f"nvenc_available={getattr(self._nvenc_slots, '_value', 'unknown')}",
            f"worker_admission_open={self.control.worker_admission_open}",
            *task_lines,
        ]
        traceback_text = traceback.format_exc()
        if traceback_text.strip() == "NoneType: None":
            traceback_text = f"{type(error).__name__}: {error}\n"
        path = self.failure_log_dir / (
            "batch-worker-failure-"
            f"{timestamp.strftime('%Y%m%d-%H%M%S-%f')}.log"
        )
        content = (
            f"timestamp: {timestamp.isoformat()}\n"
            f"task: {task_index}\n"
            f"phase={stage}\n"
            f"stage: {stage}\n"
            f"worker_exit_code: {exitcode}\n"
            "queue_snapshot:\n"
            + "\n".join(queue_snapshot)
            + "\ntraceback:\n"
            + _strip_ansi(traceback_text).rstrip()
            + "\nstdout:\n"
            + _strip_ansi(stdout).rstrip()
            + "\nstderr:\n"
            + _strip_ansi(stderr).rstrip()
            + "\n"
        )
        temporary_path: Path | None = None
        try:
            self.failure_log_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.failure_log_dir,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            self.failure_log_path = path
        except Exception as exc:
            self._record_release_error(
                "failure_log",
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _block_later_alignment_groups(
        groups: dict[str, list[BatchTask]],
        languages: Sequence[str],
        detail: str,
    ) -> None:
        for language in languages:
            for task in groups[language]:
                if task.state is TaskState.RUNNING:
                    task.block_by_worker_failure(detail=detail)

    def _apply_release_error(self) -> None:
        with self._release_errors_lock:
            release_errors = list(self._release_errors)
        if not release_errors:
            return
        diagnostic_text = "; ".join(
            f"{name}: {message}" for name, message in release_errors
        )
        if (
            self._worker_failure_task is not None
            and self._worker_failure_task.state is not TaskState.SUCCEEDED
        ):
            task = self._worker_failure_task
            separator = "; " if task.error_detail else ""
            task.error_detail += (
                f"{separator}cleanup diagnostics: {diagnostic_text}"
            )
            return
        for task in reversed(self.tasks):
            if task.state is not TaskState.SUCCEEDED:
                separator = "; " if task.error_detail else ""
                task.error_detail += (
                    f"{separator}cleanup diagnostics: {diagnostic_text}"
                )
                return
        stage, detail = release_errors[0]
        if len(release_errors) > 1:
            diagnostics = "; ".join(
                f"{name}: {message}"
                for name, message in release_errors[1:]
            )
            detail = f"{detail}; cleanup diagnostics: {diagnostics}"
        for task in reversed(self.tasks):
            if task.state is TaskState.SUCCEEDED:
                task.start_next_phase(stage)
                task.fail(stage=stage, detail=detail)
                return

    @property
    def report_metadata(self) -> dict[str, object]:
        with self._release_errors_lock:
            cleanup_diagnostics = [
                {"stage": stage, "detail": detail}
                for stage, detail in self._cleanup_diagnostics
            ]
        return {
            "worker_failure": self._worker_failure_recorded,
            "worker_failure_log": (
                str(self.failure_log_path.resolve())
                if self.failure_log_path is not None
                else None
            ),
            "worker_failure_root_cause": self._worker_failure_root_cause,
            "worker_failure_detail": self._worker_failure_detail,
            "output_directory": str(self.failure_log_dir),
            "cleanup_diagnostics": cleanup_diagnostics,
        }

    async def _run_task(self, task: BatchTask) -> None:
        token = CURRENT_TASK_INDEX.set(task.index)
        try:
            if not self.control.accepting_tasks:
                task.cancel(detail="batch admission is closed")
                return
            task.start("download")
            async with self._cpu_io_slots:
                if not self.control.stage_advancement_open:
                    raise StageAdvancementStopped("batch interrupted before download")
                render_video = await self.runners.download(task.url)
            task.render_video_path = Path(render_video)
            if not self.control.stage_advancement_open:
                raise StageAdvancementStopped("batch interrupted after download")

            task.advance("prepare")
            edit_video = None
            prepared_state = None
            if self.asr_config is not None:
                expected_edit = _expected_edit_video_path(task.render_video_path)
                transaction_path = expected_edit or task.render_video_path
                async with self._media_transaction(transaction_path):
                    prepared_state = self.resume_probe(task.render_video_path)
                    if prepared_state is None:
                        render_snapshot = capture_file_snapshot(task.render_video_path)
                        async with self._nvenc_slots:
                            if not self.control.stage_advancement_open:
                                raise StageAdvancementStopped(
                                    "batch interrupted before prepare"
                                )
                            edit_video = await self.runners.prepare(
                                str(task.render_video_path)
                            )
                        prepared_state = write_prepare_state(
                            task.render_video_path,
                            edit_video,
                            expected_render_snapshot=render_snapshot,
                        )
                    edit_video = Path(prepared_state.edit_snapshot.path)
                    task.media_generation = prepared_state.generation
            else:
                async with self._nvenc_slots:
                    if not self.control.stage_advancement_open:
                        raise StageAdvancementStopped("batch interrupted before prepare")
                    edit_video = await self.runners.prepare(str(task.render_video_path))
            task.edit_video_path = Path(edit_video)
            if not self.control.stage_advancement_open:
                raise StageAdvancementStopped("batch interrupted after prepare")

            task.advance("extract_audio")
            async with self._cpu_io_slots:
                if not self.control.stage_advancement_open:
                    raise StageAdvancementStopped(
                        "batch interrupted before extract_audio"
                    )
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
        except StageAdvancementStopped as exc:
            if task.state not in TERMINAL_STATES:
                task.cancel(detail=str(exc))
        except Exception as exc:
            if task.state not in TERMINAL_STATES:
                task.fail(stage=task.stage, detail=str(exc))
        finally:
            CURRENT_TASK_INDEX.reset(token)


def aggregate_exit_code(tasks: Sequence[BatchTask]) -> int:
    return 0 if all(task.state is TaskState.SUCCEEDED for task in tasks) else 1
