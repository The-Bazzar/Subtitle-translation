from __future__ import annotations

import json
import math
import os
import stat
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


ASR_CACHE_SCHEMA_VERSION = 2
PREPARE_STATE_SCHEMA_VERSION = 1
ASR_CACHE_LOCK_POLL_SECONDS = 0.05


class AsrCacheLockCancelled(RuntimeError):
    pass


class MediaGenerationMismatch(RuntimeError):
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
class FileSnapshot:
    path: str
    size: int
    mtime_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }

    @staticmethod
    def from_dict(value: Any) -> "FileSnapshot | None":
        if not isinstance(value, dict):
            return None
        path = value.get("path")
        size = value.get("size")
        mtime_ns = value.get("mtime_ns")
        if (
            not isinstance(path, str)
            or not path
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or isinstance(mtime_ns, bool)
            or not isinstance(mtime_ns, int)
            or mtime_ns < 0
        ):
            return None
        return FileSnapshot(path=path, size=size, mtime_ns=mtime_ns)


@dataclass(frozen=True)
class PreparedMediaState:
    generation: str
    render_snapshot: FileSnapshot
    edit_snapshot: FileSnapshot


@dataclass(frozen=True)
class WavArtifact:
    media_generation: str
    edit_snapshot: FileSnapshot
    wav_snapshot: FileSnapshot


@dataclass(frozen=True)
class AsrCacheEntry:
    generation: str
    media_generation: str
    wav_snapshot: FileSnapshot
    result: dict[str, Any]


def capture_file_snapshot(path: str | os.PathLike[str]) -> FileSnapshot:
    resolved = Path(path).resolve()
    file_stat = resolved.stat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise OSError(f"media artifact is not a regular file: {resolved}")
    return FileSnapshot(
        path=str(resolved),
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
    )


def build_asr_fingerprint_from_snapshot(
    edit_snapshot: FileSnapshot,
    *,
    model: str,
    compute_type: str,
    source_language: str,
    asr_options: Mapping[str, Any],
) -> AsrFingerprint:
    return AsrFingerprint(
        edit_video_path=edit_snapshot.path,
        edit_video_size=edit_snapshot.size,
        edit_video_mtime_ns=edit_snapshot.mtime_ns,
        model=model,
        compute_type=compute_type,
        source_language=source_language,
        asr_options=_canonical_options(asr_options),
    )


def build_asr_fingerprint(
    edit_video_path: str | os.PathLike[str],
    *,
    model: str,
    compute_type: str,
    source_language: str,
    asr_options: Mapping[str, Any],
) -> AsrFingerprint:
    return build_asr_fingerprint_from_snapshot(
        capture_file_snapshot(edit_video_path),
        model=model,
        compute_type=compute_type,
        source_language=source_language,
        asr_options=asr_options,
    )


def asr_sidecar_path(edit_video_path: str | os.PathLike[str]) -> Path:
    return Path(edit_video_path).with_suffix(".asr.json")


def prepare_state_path(edit_video_path: str | os.PathLike[str]) -> Path:
    return Path(edit_video_path).with_suffix(".prepare.json")


def invalidate_beautified_cache(aligned_json_path: str | os.PathLike[str]) -> None:
    aligned_path = Path(aligned_json_path)
    beautified_path = aligned_path.with_name(
        f"{aligned_path.stem}.beautified.json"
    )
    beautified_path.unlink(missing_ok=True)
    _fsync_parent_directory(beautified_path)


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
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        os.ftruncate(lock_descriptor, 1)
        lock_file = os.fdopen(lock_descriptor, "r+b")
    except BaseException:
        os.close(lock_descriptor)
        raise
    acquired = False
    try:
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


def _atomic_write_json(destination: Path, payload: Mapping[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
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
        temporary_path = None
        _fsync_parent_directory(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_prepare_state_unlocked(
    edit_video_path: str | os.PathLike[str],
) -> PreparedMediaState | None:
    payload = _read_json_object(prepare_state_path(edit_video_path))
    if payload is None or payload.get("schema_version") != PREPARE_STATE_SCHEMA_VERSION:
        return None
    generation = payload.get("generation")
    render_snapshot = FileSnapshot.from_dict(payload.get("render_source"))
    edit_snapshot = FileSnapshot.from_dict(payload.get("edit_media"))
    if (
        not is_valid_asr_generation(generation)
        or render_snapshot is None
        or edit_snapshot is None
    ):
        return None
    return PreparedMediaState(
        generation=generation,
        render_snapshot=render_snapshot,
        edit_snapshot=edit_snapshot,
    )


def _validate_prepare_state_unlocked(
    state: PreparedMediaState,
    *,
    render_video_path: str | os.PathLike[str] | None = None,
) -> bool:
    if render_video_path is not None:
        if str(Path(render_video_path).resolve()) != state.render_snapshot.path:
            return False
    try:
        return (
            capture_file_snapshot(state.render_snapshot.path) == state.render_snapshot
            and capture_file_snapshot(state.edit_snapshot.path) == state.edit_snapshot
        )
    except OSError:
        return False


def read_valid_prepare_state_unlocked(
    render_video_path: str | os.PathLike[str],
    edit_video_path: str | os.PathLike[str],
) -> PreparedMediaState | None:
    state = _read_prepare_state_unlocked(edit_video_path)
    if state is None or not _validate_prepare_state_unlocked(
        state,
        render_video_path=render_video_path,
    ):
        return None
    if state.edit_snapshot.path != str(Path(edit_video_path).resolve()):
        return None
    return state


def read_valid_prepare_state(
    render_video_path: str | os.PathLike[str],
    edit_video_path: str | os.PathLike[str],
) -> PreparedMediaState | None:
    with asr_cache_lock(edit_video_path):
        return read_valid_prepare_state_unlocked(render_video_path, edit_video_path)


def write_prepare_state(
    render_video_path: str | os.PathLike[str],
    edit_video_path: str | os.PathLike[str],
    *,
    expected_render_snapshot: FileSnapshot | None = None,
) -> PreparedMediaState:
    with asr_cache_lock(edit_video_path):
        return write_prepare_state_unlocked(
            render_video_path,
            edit_video_path,
            expected_render_snapshot=expected_render_snapshot,
        )


def write_prepare_state_unlocked(
    render_video_path: str | os.PathLike[str],
    edit_video_path: str | os.PathLike[str],
    *,
    expected_render_snapshot: FileSnapshot | None = None,
) -> PreparedMediaState:
    render_snapshot = capture_file_snapshot(render_video_path)
    if (
        expected_render_snapshot is not None
        and render_snapshot != expected_render_snapshot
    ):
        raise MediaGenerationMismatch(
            "render source changed while preparing edit media"
        )
    state = PreparedMediaState(
        generation=str(uuid.uuid4()),
        render_snapshot=render_snapshot,
        edit_snapshot=capture_file_snapshot(edit_video_path),
    )
    _atomic_write_json(
        prepare_state_path(edit_video_path),
        {
            "schema_version": PREPARE_STATE_SCHEMA_VERSION,
            "generation": state.generation,
            "render_source": state.render_snapshot.to_dict(),
            "edit_media": state.edit_snapshot.to_dict(),
        },
    )
    return state


def validate_wav_artifact_unlocked(artifact: WavArtifact) -> PreparedMediaState:
    state = _read_prepare_state_unlocked(artifact.edit_snapshot.path)
    if (
        state is None
        or state.generation != artifact.media_generation
        or state.edit_snapshot != artifact.edit_snapshot
        or not _validate_prepare_state_unlocked(state)
    ):
        raise MediaGenerationMismatch("media generation no longer matches prepare state")
    try:
        current_wav = capture_file_snapshot(artifact.wav_snapshot.path)
    except OSError as exc:
        raise MediaGenerationMismatch(
            "media generation WAV artifact is missing"
        ) from exc
    if current_wav != artifact.wav_snapshot:
        raise MediaGenerationMismatch("media generation WAV artifact changed")
    return state


def validate_wav_artifact(artifact: WavArtifact) -> PreparedMediaState:
    with asr_cache_lock(artifact.edit_snapshot.path):
        return validate_wav_artifact_unlocked(artifact)


def bind_wav_artifact(
    edit_video_path: str | os.PathLike[str],
    wav_path: str | os.PathLike[str],
    media_generation: str,
) -> WavArtifact:
    with asr_cache_lock(edit_video_path):
        return bind_wav_artifact_unlocked(
            edit_video_path,
            wav_path,
            media_generation,
        )


def bind_wav_artifact_unlocked(
    edit_video_path: str | os.PathLike[str],
    wav_path: str | os.PathLike[str],
    media_generation: str,
) -> WavArtifact:
    state = _read_prepare_state_unlocked(edit_video_path)
    if (
        state is None
        or state.generation != media_generation
        or not _validate_prepare_state_unlocked(state)
    ):
        raise MediaGenerationMismatch(
            "media generation changed before WAV artifact binding"
        )
    artifact = WavArtifact(
        media_generation=state.generation,
        edit_snapshot=state.edit_snapshot,
        wav_snapshot=capture_file_snapshot(wav_path),
    )
    validate_wav_artifact_unlocked(artifact)
    return artifact


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


def read_asr_cache_identity(
    sidecar_path: str | os.PathLike[str],
) -> tuple[str, str] | None:
    try:
        payload = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    generation = payload.get("generation")
    media_generation = payload.get("media_generation")
    if not is_valid_asr_generation(generation) or not is_valid_asr_generation(
        media_generation
    ):
        return None
    return generation, media_generation


def read_asr_cache_generation(
    sidecar_path: str | os.PathLike[str],
) -> str | None:
    identity = read_asr_cache_identity(sidecar_path)
    return identity[0] if identity is not None else None


def write_asr_cache(
    edit_video_path: str | os.PathLike[str],
    fingerprint: AsrFingerprint,
    result: Mapping[str, Any],
    *,
    media_generation: str,
    wav_snapshot: FileSnapshot,
) -> Path:
    with asr_cache_lock(edit_video_path):
        return _write_asr_cache_unlocked(
            edit_video_path,
            fingerprint,
            result,
            media_generation=media_generation,
            wav_snapshot=wav_snapshot,
        )


def write_asr_cache_for_artifact(
    artifact: WavArtifact,
    fingerprint: AsrFingerprint,
    result: Mapping[str, Any],
) -> Path:
    with asr_cache_lock(artifact.edit_snapshot.path):
        validate_wav_artifact_unlocked(artifact)
        if (
            fingerprint.edit_video_path != artifact.edit_snapshot.path
            or fingerprint.edit_video_size != artifact.edit_snapshot.size
            or fingerprint.edit_video_mtime_ns != artifact.edit_snapshot.mtime_ns
        ):
            raise MediaGenerationMismatch(
                "media generation does not match ASR fingerprint"
            )
        return _write_asr_cache_unlocked(
            artifact.edit_snapshot.path,
            fingerprint,
            result,
            media_generation=artifact.media_generation,
            wav_snapshot=artifact.wav_snapshot,
        )


def _write_asr_cache_unlocked(
    edit_video_path: str | os.PathLike[str],
    fingerprint: AsrFingerprint,
    result: Mapping[str, Any],
    *,
    media_generation: str,
    wav_snapshot: FileSnapshot,
) -> Path:
    if not is_valid_asr_generation(media_generation):
        raise ValueError("media generation is invalid")
    destination = asr_sidecar_path(edit_video_path)
    payload = {
        "schema_version": ASR_CACHE_SCHEMA_VERSION,
        "generation": str(uuid.uuid4()),
        "media_generation": media_generation,
        "wav_snapshot": wav_snapshot.to_dict(),
        "fingerprint": fingerprint.to_dict(),
        "result": dict(result),
    }
    _atomic_write_json(destination, payload)
    return destination


def read_valid_asr_cache(
    edit_video_path: str | os.PathLike[str],
    expected_fingerprint: AsrFingerprint,
    expected_media_generation: str,
) -> AsrCacheEntry | None:
    with asr_cache_lock(edit_video_path):
        return read_valid_asr_cache_unlocked(
            edit_video_path,
            expected_fingerprint,
            expected_media_generation,
        )


def read_valid_asr_cache_unlocked(
    edit_video_path: str | os.PathLike[str],
    expected_fingerprint: AsrFingerprint,
    expected_media_generation: str,
) -> AsrCacheEntry | None:
    cache_path = asr_sidecar_path(edit_video_path)
    payload = _read_json_object(cache_path)
    if payload is None:
        return None
    if payload.get("schema_version") != ASR_CACHE_SCHEMA_VERSION:
        return None
    generation = payload.get("generation")
    if not is_valid_asr_generation(generation):
        return None
    media_generation = payload.get("media_generation")
    if (
        not is_valid_asr_generation(media_generation)
        or media_generation != expected_media_generation
    ):
        return None
    wav_snapshot = FileSnapshot.from_dict(payload.get("wav_snapshot"))
    if wav_snapshot is None:
        return None
    if payload.get("fingerprint") != expected_fingerprint.to_dict():
        return None
    result = payload.get("result")
    if not _is_valid_asr_result(result):
        return None
    return AsrCacheEntry(
        generation=generation,
        media_generation=media_generation,
        wav_snapshot=wav_snapshot,
        result=dict(result),
    )


def read_valid_asr_cache_for_artifact(
    artifact: WavArtifact,
    expected_fingerprint: AsrFingerprint,
) -> AsrCacheEntry | None:
    with asr_cache_lock(artifact.edit_snapshot.path):
        validate_wav_artifact_unlocked(artifact)
        entry = read_valid_asr_cache_unlocked(
            artifact.edit_snapshot.path,
            expected_fingerprint,
            artifact.media_generation,
        )
        if entry is None or entry.wav_snapshot != artifact.wav_snapshot:
            return None
        return entry
