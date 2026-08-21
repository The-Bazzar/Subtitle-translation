from __future__ import annotations

import asyncio
import os
import stat
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TypeAlias, TypeVar

from batch_cache import (
    AsrFingerprint,
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
    validate_wav_artifact_unlocked,
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
    beautified_candidate_path: Path | None = None
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
PostprocessRunner: TypeAlias = Callable[[BatchTask], Awaitable[None]]


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
        postprocess_runner: PostprocessRunner | None = None,
    ) -> None:
        self.limits = limits
        self.runners = runners
        self.asr_config = asr_config
        self.worker_factory = worker_factory
        self.postprocess_runner = postprocess_runner
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
        self._worker_abort_attempted = False
        self._worker_calls: set[asyncio.Task] = set()

    async def run(self) -> list[BatchTask]:
        try:
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
                await self._run_worker_pipeline()
            return self.tasks
        finally:
            if self._worker is None:
                self.worker_released.set()

    async def _run_worker_pipeline(self) -> None:
        uncached_tasks: list[tuple[BatchTask, AsrFingerprint]] = []
        postprocess_jobs: list[asyncio.Task[None]] = []
        for task in self.tasks:
            if task.state is not TaskState.SUCCEEDED:
                continue
            task.start_next_phase("asr_waiting")
            try:
                if task.wav_artifact is None:
                    raise StageCommandError("ASR task is missing bound WAV artifact")
                source_language = resolve_source_language(
                    task.wav_artifact.edit_snapshot.path,
                    fallback=self.asr_config.source_language,
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
            await self._worker_call(worker, worker.start)
            worker_failed = await self._run_asr_wave(worker, uncached_tasks)
            if not worker_failed:
                await self._run_alignment_wave(worker, postprocess_jobs)
        except asyncio.CancelledError as exc:
            pipeline_cancellation = exc
            self._cancel_pipeline(worker, postprocess_jobs)
        except (WorkerExitedError, WorkerUnresponsiveError) as exc:
            self._fail_worker_dependents(
                self._first_worker_dependent(),
                stage=exc.command.value,
                detail=str(exc),
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
            self._fail_worker_dependents(
                uncached_tasks[0][0],
                stage="asr",
                detail=str(exc),
            )
            return True
        if not load_result.ok:
            detail = self._worker_result_error(load_result)
            for task, _fingerprint in uncached_tasks:
                if task.state is TaskState.RUNNING:
                    task.fail(stage="asr", detail=detail)
            return False

        worker_failed = False
        for task, fingerprint in uncached_tasks:
            task.advance("asr")
            self._active_worker_task = task
            try:
                result = await self._worker_call(
                    worker,
                    worker.transcribe,
                    task.wav_artifact,
                )
            except (WorkerExitedError, WorkerUnresponsiveError) as exc:
                self._fail_worker_dependents(task, stage="asr", detail=str(exc))
                worker_failed = True
                break
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

        if worker_failed:
            return True
        try:
            unload_result = await self._worker_call(worker, worker.unload_asr)
        except (WorkerExitedError, WorkerUnresponsiveError) as exc:
            self._fail_worker_dependents(
                self._first_worker_dependent(),
                stage="unload_asr",
                detail=str(exc),
            )
            return True
        if not unload_result.ok:
            self._fail_worker_dependents(
                self._first_worker_dependent(),
                stage="unload_asr",
                detail=self._worker_result_error(unload_result),
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
            group = groups[language]
            try:
                load_result = await self._worker_call(
                    worker,
                    worker.load_align,
                    language,
                )
            except (WorkerExitedError, WorkerUnresponsiveError) as exc:
                self._fail_worker_dependents(
                    group[0],
                    stage="load_align",
                    detail=str(exc),
                )
                return
            if not load_result.ok:
                detail = self._worker_result_error(load_result)
                for task in group:
                    task.fail(stage="load_align", detail=detail)
                continue

            for task in group:
                task.advance("alignment")
                self._active_worker_task = task
                candidate_path = alignment_candidate_path(
                    task.asr_path,
                    task.asr_generation,
                )
                commit_state = _AlignmentCommitState()
                try:
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
                    self._fail_worker_dependents(
                        task,
                        stage="alignment",
                        detail=str(exc),
                    )
                    return
                except Exception as exc:
                    task.fail(stage="alignment", detail=str(exc))
                    self._active_worker_task = None
                    continue
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
            except (WorkerExitedError, WorkerUnresponsiveError) as exc:
                self._record_release_error("unload_align", exc)
                self._block_later_alignment_groups(
                    groups,
                    ordered_languages[group_index + 1:],
                    str(exc),
                )
                return
            if not unload_result.ok:
                detail = self._worker_result_error(unload_result)
                self._record_release_error("unload_align", detail)
                self._block_later_alignment_groups(
                    groups,
                    ordered_languages[group_index + 1:],
                    detail,
                )
                return

    async def _run_postprocess(self, task: BatchTask) -> None:
        try:
            task.advance("postprocess")
            task.beautified_candidate_path = self._new_beautified_candidate_path(task)
            await asyncio.to_thread(self._validate_postprocess_generation_locked, task)
            async with self._cpu_io_slots:
                await self.postprocess_runner(task)
            await asyncio.to_thread(self._publish_postprocess_result_locked, task)
            task.succeed(stage="translated")
        except asyncio.CancelledError:
            if task.state not in TERMINAL_STATES:
                task.cancel(detail="postprocess canceled")
            raise
        except Exception as exc:
            if task.state not in TERMINAL_STATES:
                task.fail(stage="postprocess", detail=str(exc))
        finally:
            self._cleanup_beautified_candidate(task)

    @staticmethod
    def _beautified_path(task: BatchTask) -> Path:
        if task.json_path is None:
            raise StageCommandError("postprocess task is missing aligned JSON")
        return task.json_path.with_name(f"{task.json_path.stem}.beautified.json")

    def _new_beautified_candidate_path(self, task: BatchTask) -> Path:
        beautified_path = self._beautified_path(task).resolve()
        return beautified_path.with_name(
            f".{beautified_path.name}.{task.asr_generation}."
            f"{uuid.uuid4().hex}.candidate.json"
        )

    def _validated_beautified_candidate_path(self, task: BatchTask) -> Path:
        if task.beautified_candidate_path is None:
            raise StageCommandError("postprocess task is missing beautified candidate")
        beautified_path = self._beautified_path(task).resolve()
        candidate = task.beautified_candidate_path.resolve()
        expected_prefix = f".{beautified_path.name}.{task.asr_generation}."
        if (
            candidate.parent != beautified_path.parent
            or not candidate.name.startswith(expected_prefix)
            or not candidate.name.endswith(".candidate.json")
        ):
            raise StageCommandError(
                f"postprocess beautified candidate path is invalid: {candidate}"
            )
        return candidate

    @staticmethod
    def _validate_postprocess_generation(task: BatchTask) -> None:
        if task.json_path is None:
            raise StageCommandError("postprocess task is missing aligned JSON")
        read_aligned_json(
            task.json_path,
            expected_media_generation=task.media_generation,
            expected_alignment_generation=task.asr_generation,
        )

    def _validate_postprocess_generation_locked(self, task: BatchTask) -> None:
        with asr_cache_lock(task.edit_video_path):
            self._validate_postprocess_generation(task)

    def _publish_postprocess_result_locked(self, task: BatchTask) -> None:
        with asr_cache_lock(task.edit_video_path):
            self._validate_postprocess_generation(task)
            candidate = self._validated_beautified_candidate_path(task)
            if not candidate.exists():
                return
            try:
                read_aligned_json(
                    candidate,
                    expected_media_generation=task.media_generation,
                    expected_alignment_generation=task.asr_generation,
                )
            except (OSError, ValueError) as exc:
                raise StageCommandError(
                    "postprocess produced beautified JSON for another generation"
                ) from exc
            beautified_path = self._beautified_path(task).resolve()
            os.replace(candidate, beautified_path)
            _fsync_parent_directory(beautified_path)

    def _cleanup_beautified_candidate(self, task: BatchTask) -> None:
        if task.beautified_candidate_path is None:
            return
        try:
            candidate = self._validated_beautified_candidate_path(task)
            candidate.unlink(missing_ok=True)
            _fsync_parent_directory(candidate)
        except Exception as exc:
            self._record_cleanup_diagnostic("beautified_candidate_cleanup", exc)

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
                validate_wav_artifact_unlocked(task.wav_artifact)
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
                    try:
                        task.asr_path.unlink()
                        _fsync_parent_directory(task.asr_path)
                    except Exception as exc:
                        self._record_cleanup_diagnostic("asr_sidecar_cleanup", exc)
                    self._cleanup_wav_after_alignment(task)
                return replace(result, output_path=str(final_path))
        finally:
            if candidate_written or candidate.exists():
                try:
                    candidate.unlink(missing_ok=True)
                    _fsync_parent_directory(candidate)
                except Exception as exc:
                    self._record_release_error(
                        "alignment_candidate_cleanup",
                        exc,
                    )

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

    async def _release_worker(self, worker: AsrWorkerController) -> None:
        try:
            if worker.is_alive and not self._worker_abort_attempted:
                result = await self._worker_call(worker, worker.shutdown)
                if not result.ok:
                    self._record_release_error(
                        "shutdown",
                        self._worker_result_error(result),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_release_error("shutdown", exc)
        finally:
            try:
                await self._worker_call(worker, worker.close)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_release_error("close", exc)
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
                task.block_by_worker_failure(detail=detail)

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
