"""Read original-container chapters without changing media or subtitle timing."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path

from .config import ProjectConfig
from .process import capture_command
from .stages import _ffprobe_path


@dataclass(frozen=True)
class Chapter:
    id: int
    start_time: float
    title: str

    @property
    def timecode(self) -> str:
        seconds = math.floor(self.start_time)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def to_request(self) -> dict:
        return {"id": self.id, "start_time": self.start_time, "title": self.title}


def original_chapter_media(directory: str, base: str) -> Path | None:
    original = Path(directory) / f"{base}.original.mkv"
    return original if original.is_file() else None


def parse_chapters(payload: dict) -> list[Chapter]:
    if not isinstance(payload, dict) or not isinstance(payload.get("chapters"), list):
        raise ValueError("ffprobe response must contain a chapters array")
    chapters = []
    for item in payload["chapters"]:
        if not isinstance(item, dict) or isinstance(item.get("start_time"), bool):
            continue
        try:
            start = float(item["start_time"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(start) or start < 0:
            continue
        tags = item.get("tags")
        title = ""
        if isinstance(tags, dict):
            value = next((value for key, value in tags.items() if key.lower() == "title"), "")
            if isinstance(value, str):
                title = " ".join(value.split())
        chapters.append((start, title))
    chapters.sort(key=lambda item: item[0])
    return [Chapter(index, start, title or f"Chapter {index}")
            for index, (start, title) in enumerate(chapters, 1)]


def read_original_chapters(directory: str, base: str) -> list[Chapter]:
    original = original_chapter_media(directory, base)
    if original is None:
        return []
    config = ProjectConfig.load()
    ffmpeg = config.resolve_tool("FFMPEG_PATH_WIN" if os.name == "nt" else "FFMPEG_PATH_LINUX", "ffmpeg")
    ffprobe = _ffprobe_path(ffmpeg or "ffmpeg")
    result = capture_command(
        [ffprobe, "-v", "error", "-show_chapters", "-of", "json", str(original)],
        cwd=original.parent, timeout=30,
    )
    if result.returncode:
        raise ValueError(f"ffprobe chapter extraction failed with exit code {result.returncode}")
    return parse_chapters(json.loads(result.stdout))


def translated_chapters(chapters: list[Chapter], values) -> list[Chapter]:
    expected = {chapter.id for chapter in chapters}
    titles, seen, ambiguous = {}, set(), set()
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] not in expected:
                continue
            item_id = item["id"]
            if item_id in seen:
                ambiguous.add(item_id)
            seen.add(item_id)
            title = item.get("title")
            if isinstance(title, str) and title.strip():
                titles[item_id] = " ".join(title.split())
    return [replace(chapter, title=titles.get(chapter.id, chapter.title))
            if chapter.id not in ambiguous else chapter for chapter in chapters]
