"""Shared local audio preparation and adopted-source MFA normalization."""

from __future__ import annotations

import os
import uuid
import wave
from pathlib import Path

from .artifacts import atomic_write_json, file_sha256, json_sha256, read_json_object
from .mfa import canonical_audio_duration
from .process import run_command


def _media_snapshot(media: Path, generation: dict) -> dict:
    stat = media.stat()
    return {"path": str(media.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "generation": generation, "audio_profile": "pcm-s16le-16k-mono-v1"}


def canonical_audio(media: Path, config, work: Path, generation: dict, admission_guard=None) -> Path:
    if not media.is_file():
        raise ValueError(f"MFA requires an existing video or WAV: {media}")
    if media.suffix.lower() == ".wav":
        try:
            canonical_audio_duration(media)
            return media
        except (ValueError, wave.Error, EOFError):
            pass
    snapshot = _media_snapshot(media, generation)
    root = work / "canonical_audio" / json_sha256(snapshot)
    root.mkdir(parents=True, exist_ok=True)
    output, manifest = root / "audio.wav", root / "audio.json"
    try:
        cached = read_json_object(manifest)
        if cached["source"] == snapshot and cached["audio_sha256"] == file_sha256(output):
            canonical_audio_duration(output)
            return output
    except (OSError, ValueError, KeyError, wave.Error, EOFError):
        pass
    ffmpeg = config.resolve_tool("FFMPEG_PATH_WIN" if os.name == "nt" else "FFMPEG_PATH_LINUX", "ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is required to prepare MFA audio")
    temporary = root / f".audio.{uuid.uuid4().hex}.wav"
    try:
        result = run_command([ffmpeg, "-nostdin", "-v", "error", "-threads", "1", "-i", str(media),
                              "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(temporary)],
                             cwd=media.parent, label="MFA audio")
        if admission_guard:
            admission_guard()
        if result.returncode:
            raise ValueError(f"MFA audio preparation failed with exit code {result.returncode}")
        canonical_audio_duration(temporary)
        if _media_snapshot(media, generation) != snapshot:
            raise ValueError("MFA media changed during audio extraction")
        digest = file_sha256(temporary)
        os.replace(temporary, output)
        atomic_write_json(manifest, {"source": snapshot, "audio_sha256": digest})
    finally:
        temporary.unlink(missing_ok=True)
    return output
