from __future__ import annotations

import gc
import importlib
import json
import multiprocessing
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from batch_cache import (
    build_asr_fingerprint,
    write_asr_cache,
)


class WorkerCommand(str, Enum):
    LOAD_ASR = "load_asr"
    TRANSCRIBE = "transcribe"
    UNLOAD_ASR = "unload_asr"
    SHUTDOWN = "shutdown"


WORKER_HEARTBEAT_INTERVAL_SECONDS = 5.0
WORKER_MAX_HEARTBEAT_SILENCE_SECONDS = 30.0
WORKER_OPERATION_TIMEOUT_SECONDS = 24 * 60 * 60
WORKER_RESPONSE_POLL_SECONDS = 0.05
WORKER_RESPONSE_DRAIN_LIMIT = 64
WORKER_REAP_TIMEOUT_SECONDS = 5.0


def _freeze_options(options: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                str(key),
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
            for key, value in options.items()
        )
    )


def _default_asr_options() -> dict[str, int]:
    return {"batch_size": 8}


@dataclass(frozen=True)
class AsrWorkerConfig:
    model: str = "large-v3-turbo"
    device: str = "cpu"
    compute_type: str = "float32"
    asr_options: Mapping[str, Any] | tuple[tuple[str, str], ...] = field(
        default_factory=_default_asr_options
    )
    hf_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.asr_options, Mapping):
            frozen_options = _freeze_options(self.asr_options)
        else:
            frozen_options = tuple(self.asr_options)
        object.__setattr__(self, "asr_options", frozen_options)

    def options_dict(self) -> dict[str, Any]:
        return {
            key: json.loads(value)
            for key, value in self.asr_options
        }


@dataclass(frozen=True)
class WorkerResult:
    command: WorkerCommand
    ok: bool
    path: str = ""
    output_path: str = ""
    language: str = ""
    error_type: str = ""
    error: str = ""
    request_id: int = field(default=0, repr=False, compare=False)


@dataclass(frozen=True)
class _WorkerRequest:
    request_id: int
    command: str
    path: str = ""


@dataclass(frozen=True)
class _WorkerHeartbeat:
    request_id: int


class WorkerExitedError(RuntimeError):
    def __init__(self, exitcode: int | None, command: WorkerCommand) -> None:
        self.exitcode = exitcode
        self.command = command
        super().__init__(
            f"Whisper worker exited unexpectedly during {command.value} "
            f"(exit code: {exitcode})"
        )


class WorkerUnresponsiveError(RuntimeError):
    def __init__(
        self,
        command: WorkerCommand,
        reason: str,
        timeout_seconds: float,
    ) -> None:
        self.command = command
        self.reason = reason
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Whisper worker became unresponsive during {command.value}: "
            f"{reason} after {timeout_seconds:.3f}s"
        )


def resolve_source_language(media_path: str | os.PathLike[str]) -> str:
    info_path = Path(media_path).with_suffix(".info.json")
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "en"
    language = info.get("language") if isinstance(info, dict) else None
    if not isinstance(language, str) or not language.strip():
        return "en"
    return language.strip().split("-", 1)[0].lower()


def _detect_device(environ: Mapping[str, str]) -> str:
    configured = environ.get("WHISPER_DEVICE", "").strip()
    if configured:
        return configured
    torch_backend = environ.get("TORCH_BACKEND", "auto").strip().lower()
    if torch_backend == "cpu":
        return "cpu"
    if torch_backend == "cuda128":
        return "cuda"
    if shutil.which("nvidia-smi"):
        completed = subprocess.run(
            ["nvidia-smi"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            return "cuda"
    return "cpu"


def asr_worker_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> AsrWorkerConfig:
    values = os.environ if environ is None else environ
    device = _detect_device(values)
    return AsrWorkerConfig(
        model=values.get("WHISPER_MODEL", "").strip() or "large-v3-turbo",
        device=device,
        compute_type="float16" if device == "cuda" else "float32",
        asr_options={"batch_size": 8},
        hf_token=(
            values.get("HF_TOKEN", "").strip()
            or values.get("HUGGING_FACE_HUB_TOKEN", "").strip()
        ),
    )


class _WhisperXBackend:
    def __init__(self, config: AsrWorkerConfig) -> None:
        import whisperx

        options = config.options_dict()
        self._batch_size = int(options.pop("batch_size", 8))
        self._device = config.device
        self._model = whisperx.load_model(
            config.model,
            config.device,
            compute_type=config.compute_type,
            asr_options=options or None,
            language=None,
            use_auth_token=config.hf_token or None,
        )

    def transcribe(self, wav_path: str, source_language: str) -> dict[str, Any]:
        return self._model.transcribe(
            wav_path,
            batch_size=self._batch_size,
            language=source_language,
        )

    def unload(self) -> None:
        self._model = None
        gc.collect()
        if self._device == "cuda":
            import torch

            torch.cuda.empty_cache()


def _load_backend_factory(factory_path: str) -> Callable[[AsrWorkerConfig], Any]:
    module_name, separator, attribute_name = factory_path.rpartition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError(f"invalid backend factory path: {factory_path}")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute_name)
    if not callable(factory):
        raise TypeError(f"backend factory is not callable: {factory_path}")
    return factory


def _worker_main(
    request_queue: Any,
    response_connection: Any,
    config: AsrWorkerConfig,
    backend_factory_path: str,
    heartbeat_interval: float,
) -> None:
    response_lock = threading.Lock()

    def send_response(response: WorkerResult | _WorkerHeartbeat) -> None:
        with response_lock:
            response_connection.send(response)

    try:
        os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
        if config.hf_token:
            os.environ["HF_TOKEN"] = config.hf_token
            os.environ["HUGGING_FACE_HUB_TOKEN"] = config.hf_token
        backend = None
        while True:
            request = request_queue.get()
            if not isinstance(request, _WorkerRequest):
                raise TypeError("Whisper worker received an invalid request")
            command = WorkerCommand(request.command)
            path = str(request.path or "")
            heartbeat_stop = threading.Event()

            def send_heartbeats() -> None:
                while not heartbeat_stop.is_set():
                    send_response(_WorkerHeartbeat(request_id=request.request_id))
                    heartbeat_stop.wait(heartbeat_interval)

            heartbeat_thread = threading.Thread(
                target=send_heartbeats,
                name="batch-whisper-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
            should_shutdown = False
            try:
                if command is WorkerCommand.LOAD_ASR:
                    if backend is not None:
                        raise RuntimeError("ASR model is already loaded")
                    backend_factory = _load_backend_factory(backend_factory_path)
                    backend = backend_factory(config)
                    result = WorkerResult(
                        command=command,
                        ok=True,
                        request_id=request.request_id,
                    )
                elif command is WorkerCommand.TRANSCRIBE:
                    if backend is None:
                        raise RuntimeError("ASR model is not loaded")
                    wav_path = Path(path).resolve()
                    edit_video_path = wav_path.with_suffix(".mkv")
                    source_language = resolve_source_language(edit_video_path)
                    transcription = backend.transcribe(str(wav_path), source_language)
                    if not isinstance(transcription, Mapping):
                        raise TypeError("ASR backend returned a non-object result")
                    fingerprint = build_asr_fingerprint(
                        edit_video_path,
                        model=config.model,
                        compute_type=config.compute_type,
                        source_language=source_language,
                        asr_options=config.options_dict(),
                    )
                    output_path = write_asr_cache(
                        edit_video_path,
                        fingerprint,
                        transcription,
                    )
                    result = WorkerResult(
                        command=command,
                        ok=True,
                        path=str(wav_path),
                        output_path=str(output_path.resolve()),
                        language=str(transcription.get("language") or source_language),
                        request_id=request.request_id,
                    )
                elif command is WorkerCommand.UNLOAD_ASR:
                    if backend is None:
                        raise RuntimeError("ASR model is not loaded")
                    backend.unload()
                    backend = None
                    result = WorkerResult(
                        command=command,
                        ok=True,
                        request_id=request.request_id,
                    )
                else:
                    should_shutdown = True
                    result = WorkerResult(
                        command=command,
                        ok=True,
                        request_id=request.request_id,
                    )
            except Exception as exc:
                result = WorkerResult(
                    command=command,
                    ok=False,
                    path=path,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    request_id=request.request_id,
                )
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join()
            send_response(result)
            if should_shutdown:
                return
    finally:
        response_connection.close()
        request_queue.close()


class AsrWorkerController:
    def __init__(
        self,
        config: AsrWorkerConfig,
        *,
        backend_factory: str = "whisper_worker:_WhisperXBackend",
        heartbeat_interval: float = WORKER_HEARTBEAT_INTERVAL_SECONDS,
        max_heartbeat_silence: float = WORKER_MAX_HEARTBEAT_SILENCE_SECONDS,
        operation_timeout: float = WORKER_OPERATION_TIMEOUT_SECONDS,
        process_target: Callable[..., None] = _worker_main,
    ) -> None:
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat interval must be positive")
        if max_heartbeat_silence <= 0:
            raise ValueError("maximum heartbeat silence must be positive")
        if operation_timeout <= 0:
            raise ValueError("operation timeout must be positive")
        self.config = config
        self.backend_factory = backend_factory
        self._heartbeat_interval = heartbeat_interval
        self._max_heartbeat_silence = max_heartbeat_silence
        self._operation_timeout = operation_timeout
        self._process_target = process_target
        self._context = multiprocessing.get_context("spawn")
        self._request_queue = None
        self._response_connection = None
        self._process = None
        self._last_pid = None
        self._last_exitcode = None
        self._request_id = 0
        self._asr_loaded = False
        self._asr_unload_attempted = False
        self._shutdown_complete = False
        self._force_reaped = False
        self._unexpected_exit_reported = False
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._last_pid if self._process is None else self._process.pid

    @property
    def exitcode(self) -> int | None:
        return (
            self._last_exitcode
            if self._process is None
            else self._process.exitcode
        )

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("Whisper worker controller is closed")
        if self._process is not None:
            raise RuntimeError("Whisper worker controller cannot be restarted")
        self._request_queue = self._context.Queue()
        receive_connection, send_connection = self._context.Pipe(duplex=False)
        self._response_connection = receive_connection
        self._process = self._context.Process(
            target=self._process_target,
            args=(
                self._request_queue,
                send_connection,
                self.config,
                self.backend_factory,
                self._heartbeat_interval,
            ),
            name="batch-whisper-worker",
        )
        try:
            self._process.start()
        except BaseException:
            receive_connection.close()
            send_connection.close()
            self._request_queue.close()
            self._request_queue.join_thread()
            self._request_queue = None
            self._process = None
            raise
        send_connection.close()

    def _unexpected_exit(self, command: WorkerCommand) -> WorkerExitedError:
        if self._process is None:
            self._unexpected_exit_reported = True
            return WorkerExitedError(None, command)
        self._process.join(timeout=0.2)
        self._unexpected_exit_reported = True
        return WorkerExitedError(self.exitcode, command)

    def _terminate_process(self) -> None:
        process = self._process
        if process is None or not process.is_alive():
            return
        process.terminate()
        process.join(timeout=WORKER_REAP_TIMEOUT_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=WORKER_REAP_TIMEOUT_SECONDS)

    def _force_reap(
        self,
        command: WorkerCommand,
        reason: str,
        timeout_seconds: float,
    ) -> WorkerUnresponsiveError:
        self._terminate_process()
        self._force_reaped = True
        self._shutdown_complete = True
        self._asr_loaded = False
        return WorkerUnresponsiveError(command, reason, timeout_seconds)

    def _handle_response(
        self,
        response: Any,
        request_id: int,
        command: WorkerCommand,
        last_heartbeat: float,
    ) -> tuple[WorkerResult | None, float]:
        if isinstance(response, _WorkerHeartbeat):
            if response.request_id == request_id:
                last_heartbeat = time.monotonic()
            return None, last_heartbeat
        if not isinstance(response, WorkerResult):
            raise RuntimeError("Whisper worker returned an invalid response")
        if response.request_id != request_id:
            raise RuntimeError(
                "Whisper worker response request mismatch: "
                f"{response.request_id} != {request_id}"
            )
        if response.command is not command:
            raise RuntimeError(
                "Whisper worker response mismatch: "
                f"{response.command.value} != {command.value}"
            )
        return response, last_heartbeat

    def _receive_response(
        self,
        request_id: int,
        command: WorkerCommand,
        last_heartbeat: float,
    ) -> tuple[WorkerResult | None, float]:
        try:
            response = self._response_connection.recv()
        except (EOFError, OSError):
            raise self._unexpected_exit(command)
        return self._handle_response(
            response,
            request_id,
            command,
            last_heartbeat,
        )

    def _drain_responses(
        self,
        request_id: int,
        command: WorkerCommand,
        last_heartbeat: float,
    ) -> tuple[WorkerResult | None, float]:
        for _index in range(WORKER_RESPONSE_DRAIN_LIMIT):
            try:
                available = self._response_connection.poll(0)
            except (EOFError, OSError):
                raise self._unexpected_exit(command)
            if not available:
                break
            result, last_heartbeat = self._receive_response(
                request_id,
                command,
                last_heartbeat,
            )
            if result is not None:
                return result, last_heartbeat
        return None, last_heartbeat

    def _request(self, command: WorkerCommand, path: str = "") -> WorkerResult:
        if self._process is None:
            raise RuntimeError("Whisper worker has not been started")
        if self._response_connection is None:
            raise RuntimeError("Whisper worker response connection is unavailable")
        if not self._process.is_alive():
            raise self._unexpected_exit(command)
        self._request_id += 1
        request_id = self._request_id
        self._request_queue.put(
            _WorkerRequest(
                request_id=request_id,
                command=command.value,
                path=path,
            )
        )
        operation_started = time.monotonic()
        last_heartbeat = operation_started
        while True:
            now = time.monotonic()
            result, last_heartbeat = self._drain_responses(
                request_id,
                command,
                last_heartbeat,
            )
            if result is not None:
                return result
            if not self._process.is_alive():
                raise self._unexpected_exit(command)
            now = max(now, time.monotonic())
            operation_elapsed = now - operation_started
            heartbeat_silence = now - last_heartbeat
            operation_expired = operation_elapsed >= self._operation_timeout
            heartbeat_expired = (
                heartbeat_silence >= self._max_heartbeat_silence
            )
            if operation_expired or heartbeat_expired:
                result, last_heartbeat = self._drain_responses(
                    request_id,
                    command,
                    last_heartbeat,
                )
                if result is not None:
                    return result
                if not self._process.is_alive():
                    raise self._unexpected_exit(command)
                now = time.monotonic()
                operation_elapsed = now - operation_started
                heartbeat_silence = now - last_heartbeat
                if operation_elapsed >= self._operation_timeout:
                    raise self._force_reap(
                        command,
                        "operation timeout exceeded",
                        self._operation_timeout,
                    )
                if heartbeat_silence >= self._max_heartbeat_silence:
                    raise self._force_reap(
                        command,
                        "heartbeat silence exceeded",
                        self._max_heartbeat_silence,
                    )
            wait_timeout = min(
                WORKER_RESPONSE_POLL_SECONDS,
                self._operation_timeout - operation_elapsed,
                self._max_heartbeat_silence - heartbeat_silence,
            )
            try:
                available = self._response_connection.poll(
                    max(wait_timeout, 0.001)
                )
            except (EOFError, OSError):
                raise self._unexpected_exit(command)
            if available:
                result, last_heartbeat = self._receive_response(
                    request_id,
                    command,
                    last_heartbeat,
                )
                if result is not None:
                    return result

    def load_asr(self) -> WorkerResult:
        result = self._request(WorkerCommand.LOAD_ASR)
        if result.ok:
            self._asr_loaded = True
            self._asr_unload_attempted = False
        return result

    def transcribe(self, wav_path: str | os.PathLike[str]) -> WorkerResult:
        return self._request(
            WorkerCommand.TRANSCRIBE,
            str(Path(wav_path).resolve()),
        )

    def unload_asr(self) -> WorkerResult:
        if self._asr_unload_attempted:
            raise RuntimeError("ASR unload has already been attempted")
        self._asr_unload_attempted = True
        result = self._request(WorkerCommand.UNLOAD_ASR)
        if result.ok:
            self._asr_loaded = False
        return result

    def shutdown(self) -> WorkerResult:
        if self._shutdown_complete or self._force_reaped:
            return WorkerResult(command=WorkerCommand.SHUTDOWN, ok=True)
        if self._process is None:
            return WorkerResult(command=WorkerCommand.SHUTDOWN, ok=True)
        if not self._process.is_alive():
            self._process.join(timeout=0.2)
            if self.exitcode == 0:
                self._shutdown_complete = True
                self._asr_loaded = False
                return WorkerResult(command=WorkerCommand.SHUTDOWN, ok=True)
            raise self._unexpected_exit(WorkerCommand.SHUTDOWN)
        result = self._request(WorkerCommand.SHUTDOWN)
        self._process.join(timeout=WORKER_REAP_TIMEOUT_SECONDS)
        if self._process.is_alive():
            raise self._force_reap(
                WorkerCommand.SHUTDOWN,
                "process did not exit after shutdown response",
                WORKER_REAP_TIMEOUT_SECONDS,
            )
        if self.exitcode not in (None, 0):
            raise self._unexpected_exit(WorkerCommand.SHUTDOWN)
        self._shutdown_complete = True
        self._asr_loaded = False
        return result

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._process is None or self._force_reaped:
                return
            if (
                self._process.is_alive()
                and self._asr_loaded
                and not self._asr_unload_attempted
            ):
                self.unload_asr()
            if self._process.is_alive():
                self.shutdown()
            else:
                self._process.join(timeout=0.2)
                if (
                    self.exitcode not in (None, 0)
                    and not self._unexpected_exit_reported
                ):
                    raise self._unexpected_exit(WorkerCommand.SHUTDOWN)
        finally:
            self._terminate_process()
            if self._request_queue is not None:
                self._request_queue.close()
                self._request_queue.join_thread()
                self._request_queue = None
            if (
                self._response_connection is not None
                and not self._response_connection.closed
            ):
                self._response_connection.close()
            if self._process is not None:
                self._process.join(timeout=0)
                self._last_pid = self._process.pid
                self._last_exitcode = self._process.exitcode
                self._process.close()
                self._process = None
            self._closed = True

    def __enter__(self) -> AsrWorkerController:
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
