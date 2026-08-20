from __future__ import annotations

import json
import math
import os
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


ASR_CACHE_SCHEMA_VERSION = 1
ASR_CACHE_LOCK_POLL_SECONDS = 0.05


class AsrCacheLockCancelled(RuntimeError):
    pass


def _canonical_options(options: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                str(key),
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
            for key, value in options.items()
        )
    )


@dataclass(frozen=True)
class AsrFingerprint:
    edit_video_path: str
    edit_video_size: int
    edit_video_mtime_ns: int
    model: str
    compute_type: str
    source_language: str
    asr_options: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "edit_video_path": self.edit_video_path,
            "edit_video_size": self.edit_video_size,
            "edit_video_mtime_ns": self.edit_video_mtime_ns,
            "model": self.model,
            "compute_type": self.compute_type,
            "source_language": self.source_language,
            "asr_options": {
                key: json.loads(value)
                for key, value in self.asr_options
            },
        }


@dataclass(frozen=True)
class AsrCacheEntry:
    generation: str
    result: dict[str, Any]


def build_asr_fingerprint(
    edit_video_path: str | os.PathLike[str],
    *,
    model: str,
    compute_type: str,
    source_language: str,
    asr_options: Mapping[str, Any],
) -> AsrFingerprint:
    resolved_path = Path(edit_video_path).resolve()
    file_stat = resolved_path.stat()
    return AsrFingerprint(
        edit_video_path=str(resolved_path),
        edit_video_size=file_stat.st_size,
        edit_video_mtime_ns=file_stat.st_mtime_ns,
        model=model,
        compute_type=compute_type,
        source_language=source_language,
        asr_options=_canonical_options(asr_options),
    )


def asr_sidecar_path(edit_video_path: str | os.PathLike[str]) -> Path:
    return Path(edit_video_path).with_suffix(".asr.json")


def asr_cache_lock_path(media_path: str | os.PathLike[str]) -> Path:
    path = Path(media_path)
    if path.name.endswith(".asr.json"):
        return path.with_name(
            path.name.removesuffix(".asr.json") + ".asr.lock"
        )
    return path.with_suffix(".asr.lock")


def _lock_cancelled(cancel_event: Any) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _acquire_cache_lock(lock_file, cancel_event: Any) -> None:
    if os.name == "nt":
        lock_file.seek(0)
        while True:
            if _lock_cancelled(cancel_event):
                raise AsrCacheLockCancelled("ASR cache lock acquisition canceled")
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in (13, 36):
                    raise
                time.sleep(ASR_CACHE_LOCK_POLL_SECONDS)
    else:
        while True:
            if _lock_cancelled(cancel_event):
                raise AsrCacheLockCancelled("ASR cache lock acquisition canceled")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                time.sleep(ASR_CACHE_LOCK_POLL_SECONDS)


def _release_cache_lock(lock_file) -> None:
    try:
        if os.name == "nt":
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def asr_cache_lock(
    media_path: str | os.PathLike[str],
    *,
    cancel_event: Any = None,
) -> Iterator[Path]:
    lock_path = asr_cache_lock_path(media_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+b")
    acquired = False
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        _acquire_cache_lock(lock_file, cancel_event)
        acquired = True
        yield lock_path
    finally:
        if acquired:
            _release_cache_lock(lock_file)
        lock_file.close()


def _fsync_parent_directory(
    path: str | os.PathLike[str],
    *,
    platform: str | None = None,
) -> None:
    platform_name = os.name if platform is None else platform
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if platform_name == "nt" or directory_flag is None:
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(
            str(Path(path).parent),
            os.O_RDONLY | directory_flag,
        )
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _is_valid_asr_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    language = result.get("language")
    if not isinstance(language, str) or not language.strip():
        return False
    segments = result.get("segments")
    if not isinstance(segments, list):
        return False
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            return False
        start = segment.get("start")
        end = segment.get("end")
        if (
            not _is_finite_timestamp(start)
            or not _is_finite_timestamp(end)
            or end < start
        ):
            return False
    return True


def _is_finite_timestamp(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not isinstance(value, float) or math.isfinite(value)


def is_valid_asr_generation(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value.lower()


def read_asr_cache_generation(
    sidecar_path: str | os.PathLike[str],
) -> str | None:
    try:
        payload = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    generation = payload.get("generation")
    return generation if is_valid_asr_generation(generation) else None


def write_asr_cache(
    edit_video_path: str | os.PathLike[str],
    fingerprint: AsrFingerprint,
    result: Mapping[str, Any],
) -> Path:
    with asr_cache_lock(edit_video_path):
        return _write_asr_cache_unlocked(edit_video_path, fingerprint, result)


def _write_asr_cache_unlocked(
    edit_video_path: str | os.PathLike[str],
    fingerprint: AsrFingerprint,
    result: Mapping[str, Any],
) -> Path:
    destination = asr_sidecar_path(edit_video_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": ASR_CACHE_SCHEMA_VERSION,
        "generation": str(uuid.uuid4()),
        "fingerprint": fingerprint.to_dict(),
        "result": dict(result),
    }
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
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
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


def read_valid_asr_cache(
    edit_video_path: str | os.PathLike[str],
    expected_fingerprint: AsrFingerprint,
) -> AsrCacheEntry | None:
    cache_path = asr_sidecar_path(edit_video_path)
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != ASR_CACHE_SCHEMA_VERSION:
        return None
    generation = payload.get("generation")
    if not is_valid_asr_generation(generation):
        return None
    if payload.get("fingerprint") != expected_fingerprint.to_dict():
        return None
    result = payload.get("result")
    if not _is_valid_asr_result(result):
        return None
    return AsrCacheEntry(generation=generation, result=dict(result))
