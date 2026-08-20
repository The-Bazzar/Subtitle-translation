from __future__ import annotations

import gc
import importlib
import json
import math
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import langcodes

from batch_cache import (
    ASR_CACHE_SCHEMA_VERSION,
    _fsync_parent_directory,
    build_asr_fingerprint,
    is_valid_asr_generation,
    write_asr_cache,
)


class WorkerCommand(str, Enum):
    LOAD_ASR = "load_asr"
    TRANSCRIBE = "transcribe"
    UNLOAD_ASR = "unload_asr"
    LOAD_ALIGN = "load_align"
    ALIGN = "align"
    UNLOAD_ALIGN = "unload_align"
    SHUTDOWN = "shutdown"


WORKER_HEARTBEAT_INTERVAL_SECONDS = 5.0
WORKER_MAX_HEARTBEAT_SILENCE_SECONDS = 30.0
WORKER_OPERATION_TIMEOUT_SECONDS = 24 * 60 * 60
WORKER_RESPONSE_POLL_SECONDS = 0.05
WORKER_RESPONSE_DRAIN_LIMIT = 64
WORKER_REAP_TIMEOUT_SECONDS = 5.0
WORKER_CAPTURE_TAIL_BYTES = 256 * 1024


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
    align_model: str = ""
    source_language: str = ""
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
    generation: str = ""
    error_type: str = ""
    error: str = ""
    request_id: int = field(default=0, repr=False, compare=False)


@dataclass(frozen=True)
class _WorkerRequest:
    request_id: int
    command: str
    path: str = ""
    language: str = ""
    generation: str = ""
    candidate_path: str = ""


@dataclass(frozen=True)
class _WorkerHeartbeat:
    request_id: int


class WorkerExitedError(RuntimeError):
    def __init__(
        self,
        exitcode: int | None,
        command: WorkerCommand,
        *,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.exitcode = exitcode
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
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
        *,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.command = command
        self.reason = reason
        self.timeout_seconds = timeout_seconds
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"Whisper worker became unresponsive during {command.value}: "
            f"{reason} after {timeout_seconds:.3f}s"
        )


def resolve_source_language(
    media_path: str | os.PathLike[str],
    fallback: str = "",
) -> str:
    info_path = Path(media_path).with_suffix(".info.json")
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return normalize_language_code(fallback) if fallback else "en"
    language = info.get("language") if isinstance(info, dict) else None
    if not isinstance(language, str) or not language.strip():
        return normalize_language_code(fallback) if fallback else "en"
    return normalize_language_code(language, fallback=fallback)


def normalize_language_code(language: object, *, fallback: object = "") -> str:
    def normalize(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("language is required")
        candidate = value.strip().replace("_", "-")
        try:
            parsed = langcodes.Language.get(candidate, normalize=True)
        except ValueError as exc:
            raise ValueError(f"language is not a valid ISO 639/BCP-47 code: {value}") from exc
        code = parsed.language
        if not parsed.is_valid() or not code or code == "und":
            raise ValueError(f"language is not a valid ISO 639/BCP-47 code: {value}")
        return code.lower()

    try:
        return normalize(language)
    except ValueError:
        if not isinstance(fallback, str) or not fallback.strip():
            raise
        try:
            return normalize(fallback)
        except ValueError as fallback_error:
            raise ValueError(
                f"language and fallback are invalid: {language}, {fallback}"
            ) from fallback_error


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
        align_model=values.get("WHISPER_ALIGN_MODEL", "").strip(),
        source_language=values.get("SOURCE_LANG", "").strip(),
        asr_options={"batch_size": 8},
        hf_token=(
            values.get("HF_TOKEN", "").strip()
            or values.get("HUGGING_FACE_HUB_TOKEN", "").strip()
        ),
    )


class _WhisperXBackend:
    def __init__(
        self,
        config: AsrWorkerConfig,
        alignment_language: str = "",
    ) -> None:
        import whisperx

        self._whisperx = whisperx
        self._device = config.device
        self._metadata = None
        if alignment_language:
            alignment_options = {
                "language_code": alignment_language,
                "device": config.device,
            }
            if config.align_model:
                alignment_options["model_name"] = config.align_model
            self._model, self._metadata = whisperx.load_align_model(
                **alignment_options
            )
            self._batch_size = 0
        else:
            options = config.options_dict()
            self._batch_size = int(options.pop("batch_size", 8))
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

    def align(
        self,
        wav_path: str,
        segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        audio = self._whisperx.load_audio(wav_path)
        return self._whisperx.align(
            segments,
            self._model,
            self._metadata,
            audio,
            self._device,
            return_char_alignments=False,
        )

    def unload(self) -> None:
        self._model = None
        gc.collect()
        if self._device == "cuda":
            import torch

            torch.cuda.empty_cache()


def _is_finite_timestamp(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not isinstance(value, float) or math.isfinite(value)


def _validate_timed_text_segment(segment: Any) -> dict[str, Any]:
    if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
        raise ValueError("alignment segment must contain text")
    start = segment.get("start")
    end = segment.get("end")
    if (
        not _is_finite_timestamp(start)
        or not _is_finite_timestamp(end)
        or end < start
    ):
        raise ValueError("alignment segment has invalid timestamps")
    return segment


def read_alignment_input(
    sidecar_path: str | os.PathLike[str],
    expected_generation: str = "",
) -> tuple[str, str, list[dict[str, Any]]]:
    path = Path(sidecar_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid ASR recovery sidecar: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("ASR recovery sidecar must be an object")
    if payload.get("schema_version") != ASR_CACHE_SCHEMA_VERSION:
        raise ValueError("ASR recovery sidecar schema version is invalid")
    generation = payload.get("generation")
    if not is_valid_asr_generation(generation):
        raise ValueError("ASR recovery sidecar generation is invalid")
    if expected_generation and generation != expected_generation:
        raise ValueError("ASR recovery sidecar generation changed before alignment")
    if not isinstance(payload.get("fingerprint"), dict):
        raise ValueError("ASR recovery sidecar fingerprint is invalid")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("ASR recovery sidecar result must be an object")
    language = result.get("language")
    if not isinstance(language, str) or not language.strip():
        raise ValueError("ASR recovery sidecar language is invalid")
    segments = result.get("segments")
    if not isinstance(segments, list):
        raise ValueError("ASR recovery sidecar segments must be a list")
    return (
        generation,
        language.strip(),
        [_validate_timed_text_segment(item) for item in segments],
    )


def _validate_aligned_result(result: Mapping[str, Any]) -> dict[str, Any]:
    language = result.get("language")
    if not isinstance(language, str) or not language.strip():
        raise ValueError("aligned result language is invalid")
    segments = result.get("segments")
    if not isinstance(segments, list):
        raise ValueError("aligned result segments must be a list")
    for segment in segments:
        validated_segment = _validate_timed_text_segment(segment)
        words = validated_segment.get("words")
        if not isinstance(words, list):
            raise ValueError("aligned segment words must be a list")
        for word in words:
            if not isinstance(word, dict) or not isinstance(word.get("word"), str):
                raise ValueError("aligned word must contain text")
            has_start = "start" in word
            has_end = "end" in word
            if has_start != has_end:
                raise ValueError("aligned word timestamps must be paired")
            if has_start:
                start = word["start"]
                end = word["end"]
                if (
                    not _is_finite_timestamp(start)
                    or not _is_finite_timestamp(end)
                    or end < start
                ):
                    raise ValueError("aligned word has invalid timestamps")
    return dict(result)


def read_aligned_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    final_path = Path(path)
    try:
        result = json.loads(final_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid aligned JSON: {final_path}") from exc
    if not isinstance(result, Mapping):
        raise ValueError("aligned JSON must be an object")
    return _validate_aligned_result(result)


def _final_json_path(sidecar_path: Path) -> Path:
    suffix = ".asr.json"
    if not sidecar_path.name.endswith(suffix):
        raise ValueError(f"alignment input must end with {suffix}: {sidecar_path}")
    return sidecar_path.with_name(f"{sidecar_path.name[:-len(suffix)]}.json")


def alignment_candidate_path(
    sidecar_path: str | os.PathLike[str],
    generation: str,
) -> Path:
    recovery_path = Path(sidecar_path)
    if not is_valid_asr_generation(generation):
        raise ValueError("alignment generation is invalid")
    final_path = _final_json_path(recovery_path)
    return final_path.with_name(
        f".{final_path.name}.{generation}.{uuid.uuid4().hex}.candidate.json"
    )


def validate_alignment_candidate_path(
    sidecar_path: str | os.PathLike[str],
    generation: str,
    candidate_path: str | os.PathLike[str],
) -> Path:
    recovery_path = Path(sidecar_path).resolve()
    if not is_valid_asr_generation(generation):
        raise ValueError("alignment generation is invalid")
    final_path = _final_json_path(recovery_path)
    candidate = Path(candidate_path).resolve()
    expected_prefix = f".{final_path.name}.{generation}."
    if (
        candidate.parent != final_path.parent
        or not candidate.name.startswith(expected_prefix)
        or not candidate.name.endswith(".candidate.json")
    ):
        raise ValueError(f"alignment candidate path is invalid: {candidate}")
    return candidate


def write_aligned_candidate_json(
    sidecar_path: str | os.PathLike[str],
    generation: str,
    candidate_path: str | os.PathLike[str],
    result: Mapping[str, Any],
) -> Path:
    destination = validate_alignment_candidate_path(
        sidecar_path,
        generation,
        candidate_path,
    )
    validated_result = _validate_aligned_result(result)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(validated_result, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        _fsync_parent_directory(destination)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return destination


def promote_aligned_candidate(
    candidate_path: str | os.PathLike[str],
    final_path: str | os.PathLike[str],
) -> Path:
    candidate = Path(candidate_path).resolve()
    destination = Path(final_path).resolve()
    if candidate.parent != destination.parent:
        raise ValueError("alignment candidate and final JSON must be siblings")
    os.replace(candidate, destination)
    _fsync_parent_directory(destination)
    return destination


def _load_backend_factory(factory_path: str) -> Callable[[AsrWorkerConfig], Any]:
    module_name, separator, attribute_name = factory_path.rpartition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError(f"invalid backend factory path: {factory_path}")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute_name)
    if not callable(factory):
        raise TypeError(f"backend factory is not callable: {factory_path}")
    return factory


def _worker_process_entry(
    process_target: Callable[..., None],
    request_queue: Any,
    response_connection: Any,
    config: AsrWorkerConfig,
    backend_factory_path: str,
    heartbeat_interval: float,
    stdout_path: str,
    stderr_path: str,
) -> None:
    with (
        open(stdout_path, "ab", buffering=0) as stdout_capture,
        open(stderr_path, "ab", buffering=0) as stderr_capture,
    ):
        os.dup2(stdout_capture.fileno(), 1)
        os.dup2(stderr_capture.fileno(), 2)
        sys.stdout = open(
            1,
            "w",
            encoding="utf-8",
            errors="replace",
            buffering=1,
            closefd=False,
        )
        sys.stderr = open(
            2,
            "w",
            encoding="utf-8",
            errors="replace",
            buffering=1,
            closefd=False,
        )
        process_target(
            request_queue,
            response_connection,
            config,
            backend_factory_path,
            heartbeat_interval,
        )


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
        alignment_language = ""
        while True:
            request = request_queue.get()
            if not isinstance(request, _WorkerRequest):
                raise TypeError("Whisper worker received an invalid request")
            command = WorkerCommand(request.command)
            path = str(request.path or "")
            language = str(request.language or "").strip()
            generation = str(request.generation or "").strip()
            candidate_path = str(request.candidate_path or "").strip()
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
                elif command is WorkerCommand.LOAD_ALIGN:
                    if backend is not None:
                        raise RuntimeError("ASR or alignment model is already loaded")
                    language = normalize_language_code(language)
                    backend_factory = _load_backend_factory(backend_factory_path)
                    backend = backend_factory(config, language)
                    alignment_language = language
                    result = WorkerResult(
                        command=command,
                        ok=True,
                        language=language,
                        request_id=request.request_id,
                    )
                elif command is WorkerCommand.ALIGN:
                    if backend is None or not alignment_language:
                        raise RuntimeError("alignment model is not loaded")
                    sidecar_path = Path(path).resolve()
                    (
                        input_generation,
                        detected_language,
                        segments,
                    ) = read_alignment_input(sidecar_path, generation)
                    detected_language = normalize_language_code(
                        detected_language,
                        fallback=alignment_language,
                    )
                    if detected_language != alignment_language:
                        raise ValueError(
                            "alignment language mismatch: "
                            f"{detected_language} != {alignment_language}"
                        )
                    wav_path = _final_json_path(sidecar_path).with_suffix(".wav")
                    aligned = backend.align(str(wav_path), segments)
                    if not isinstance(aligned, Mapping):
                        raise TypeError("alignment backend returned a non-object result")
                    final_result = dict(aligned)
                    final_result["language"] = detected_language
                    output_path = write_aligned_candidate_json(
                        sidecar_path,
                        input_generation,
                        candidate_path,
                        final_result,
                    )
                    result = WorkerResult(
                        command=command,
                        ok=True,
                        path=str(sidecar_path),
                        output_path=str(output_path.resolve()),
                        language=detected_language,
                        generation=input_generation,
                        request_id=request.request_id,
                    )
                elif command is WorkerCommand.UNLOAD_ALIGN:
                    if backend is None or not alignment_language:
                        raise RuntimeError("alignment model is not loaded")
                    backend.unload()
                    backend = None
                    alignment_language = ""
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
        self._align_loaded = False
        self._align_unload_attempted = False
        self._alignment_language = ""
        self._shutdown_complete = False
        self._force_reaped = False
        self._unexpected_exit_reported = False
        self._closed = False
        self._capture_directory: Path | None = None
        self._stdout_capture_path: Path | None = None
        self._stderr_capture_path: Path | None = None

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

    @property
    def capture_paths(self) -> tuple[Path, Path]:
        if self._stdout_capture_path is None or self._stderr_capture_path is None:
            raise RuntimeError("Whisper worker capture files are unavailable")
        return self._stdout_capture_path, self._stderr_capture_path

    @staticmethod
    def _read_capture_tail(path: Path | None) -> str:
        if path is None:
            return ""
        try:
            with path.open("rb") as capture_file:
                capture_file.seek(0, os.SEEK_END)
                size = capture_file.tell()
                if size > WORKER_CAPTURE_TAIL_BYTES:
                    capture_file.seek(-WORKER_CAPTURE_TAIL_BYTES, os.SEEK_END)
                    marker = (
                        "[... worker output truncated; showing last "
                        f"{WORKER_CAPTURE_TAIL_BYTES} bytes ...]\n"
                    )
                else:
                    capture_file.seek(0)
                    marker = ""
                payload = capture_file.read()
        except OSError:
            return ""
        return marker + payload.decode("utf-8", errors="replace")

    def captured_output(self) -> tuple[str, str]:
        return (
            self._read_capture_tail(self._stdout_capture_path),
            self._read_capture_tail(self._stderr_capture_path),
        )

    def _create_capture_files(self) -> None:
        capture_directory = Path(
            tempfile.mkdtemp(prefix="batch-whisper-worker-")
        ).resolve()
        stdout_path = capture_directory / "stdout.log"
        stderr_path = capture_directory / "stderr.log"
        stdout_path.touch()
        stderr_path.touch()
        self._capture_directory = capture_directory
        self._stdout_capture_path = stdout_path
        self._stderr_capture_path = stderr_path

    def _cleanup_capture_files(self) -> None:
        capture_directory = self._capture_directory
        if capture_directory is None:
            return
        shutil.rmtree(capture_directory)
        self._capture_directory = None
        self._stdout_capture_path = None
        self._stderr_capture_path = None

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("Whisper worker controller is closed")
        if self._process is not None:
            raise RuntimeError("Whisper worker controller cannot be restarted")
        self._create_capture_files()
        self._request_queue = self._context.Queue()
        receive_connection, send_connection = self._context.Pipe(duplex=False)
        self._response_connection = receive_connection
        self._process = self._context.Process(
            target=_worker_process_entry,
            args=(
                self._process_target,
                self._request_queue,
                send_connection,
                self.config,
                self.backend_factory,
                self._heartbeat_interval,
                str(self._stdout_capture_path),
                str(self._stderr_capture_path),
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
            try:
                self._cleanup_capture_files()
            except OSError:
                pass
            raise
        send_connection.close()

    def _unexpected_exit(self, command: WorkerCommand) -> WorkerExitedError:
        stdout, stderr = self.captured_output()
        if self._process is None:
            self._unexpected_exit_reported = True
            return WorkerExitedError(
                None,
                command,
                stdout=stdout,
                stderr=stderr,
            )
        self._process.join(timeout=0.2)
        stdout, stderr = self.captured_output()
        self._unexpected_exit_reported = True
        return WorkerExitedError(
            self.exitcode,
            command,
            stdout=stdout,
            stderr=stderr,
        )

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
        self._align_loaded = False
        self._alignment_language = ""
        stdout, stderr = self.captured_output()
        return WorkerUnresponsiveError(
            command,
            reason,
            timeout_seconds,
            stdout=stdout,
            stderr=stderr,
        )

    def abort(self) -> None:
        if self._closed or self._force_reaped:
            return
        termination_error: Exception | None = None
        try:
            self._terminate_process()
        except Exception as exc:
            termination_error = exc
        finally:
            self._force_reaped = True
            self._shutdown_complete = True
            self._asr_loaded = False
            self._align_loaded = False
            self._alignment_language = ""
            if (
                self._response_connection is not None
                and not self._response_connection.closed
            ):
                try:
                    self._response_connection.close()
                except Exception as exc:
                    if termination_error is None:
                        termination_error = exc
        if termination_error is not None:
            raise termination_error

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

    def _request(
        self,
        command: WorkerCommand,
        path: str = "",
        language: str = "",
        generation: str = "",
        candidate_path: str = "",
    ) -> WorkerResult:
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
                language=language,
                generation=generation,
                candidate_path=candidate_path,
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

    def load_align(self, language: str) -> WorkerResult:
        normalized_language = normalize_language_code(language)
        if self._asr_loaded:
            raise RuntimeError("ASR model must be unloaded before alignment")
        if self._align_loaded:
            raise RuntimeError("alignment model is already loaded")
        result = self._request(
            WorkerCommand.LOAD_ALIGN,
            language=normalized_language,
        )
        if result.ok:
            self._align_loaded = True
            self._align_unload_attempted = False
            self._alignment_language = normalized_language
        return result

    def align(
        self,
        sidecar_path: str | os.PathLike[str],
        generation: str,
        candidate_path: str | os.PathLike[str] | None = None,
    ) -> WorkerResult:
        if not self._align_loaded:
            raise RuntimeError("alignment model is not loaded")
        if not is_valid_asr_generation(generation):
            raise ValueError("alignment generation is invalid")
        recovery_path = Path(sidecar_path).resolve()
        candidate = (
            alignment_candidate_path(recovery_path, generation)
            if candidate_path is None
            else validate_alignment_candidate_path(
                recovery_path,
                generation,
                candidate_path,
            )
        )
        return self._request(
            WorkerCommand.ALIGN,
            str(recovery_path),
            generation=generation,
            candidate_path=str(candidate),
        )

    def unload_align(self) -> WorkerResult:
        if self._align_unload_attempted:
            raise RuntimeError("alignment unload has already been attempted")
        self._align_unload_attempted = True
        result = self._request(WorkerCommand.UNLOAD_ALIGN)
        if result.ok:
            self._align_loaded = False
            self._alignment_language = ""
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
                self._align_loaded = False
                self._alignment_language = ""
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
        self._align_loaded = False
        self._alignment_language = ""
        return result

    def close(self) -> None:
        if self._closed:
            return
        cleanup_error: Exception | None = None
        try:
            if self._process is not None and not self._force_reaped:
                if (
                    self._process.is_alive()
                    and self._asr_loaded
                    and not self._asr_unload_attempted
                ):
                    self.unload_asr()
                if (
                    self._process.is_alive()
                    and self._align_loaded
                    and not self._align_unload_attempted
                ):
                    self.unload_align()
                if self._process.is_alive():
                    self.shutdown()
                else:
                    self._process.join(timeout=0.2)
                    if (
                        self.exitcode not in (None, 0)
                        and not self._unexpected_exit_reported
                    ):
                        raise self._unexpected_exit(WorkerCommand.SHUTDOWN)
        except Exception as exc:
            cleanup_error = exc
        finally:
            try:
                self._terminate_process()
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            if self._request_queue is not None:
                try:
                    self._request_queue.close()
                    self._request_queue.join_thread()
                except Exception as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                self._request_queue = None
            if (
                self._response_connection is not None
                and not self._response_connection.closed
            ):
                try:
                    self._response_connection.close()
                except Exception as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if self._process is not None:
                try:
                    self._process.join(timeout=0)
                    self._last_pid = self._process.pid
                    self._last_exitcode = self._process.exitcode
                    self._process.close()
                    self._process = None
                except Exception as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            captured_stdout, captured_stderr = self.captured_output()
            try:
                self._cleanup_capture_files()
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            self._closed = True
        if cleanup_error is not None:
            try:
                if not getattr(cleanup_error, "stdout", ""):
                    cleanup_error.stdout = captured_stdout
                if not getattr(cleanup_error, "stderr", ""):
                    cleanup_error.stderr = captured_stderr
            except (AttributeError, TypeError):
                pass
            raise cleanup_error

    def __enter__(self) -> AsrWorkerController:
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
