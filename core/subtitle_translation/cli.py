from __future__ import annotations

import argparse
import importlib.resources
import shutil
import sys
from pathlib import Path
from typing import Sequence

import batch_runtime
import merge_ass
import translate_srt

from . import __version__
from . import pipeline, stages


COMMANDS = (
    "pipeline",
    "batch",
    "translate",
    "merge-ass",
    "download",
    "prepare-video",
    "whisper",
    "burn",
    "init",
)


def _forwarding_command(subparsers, name: str, help_text: str) -> None:
    command = subparsers.add_parser(name, help=help_text)
    command.add_argument("args", nargs=argparse.REMAINDER)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subtitle-translation",
        description="JSON-first WhisperX subtitle translation pipeline",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--project-dir", default=None, help="Project configuration directory")
    subparsers = parser.add_subparsers(dest="command", required=False)
    _forwarding_command(subparsers, "pipeline", "Run the complete subtitle pipeline")
    _forwarding_command(subparsers, "batch", "Process multiple YouTube URLs with stage scheduling")
    _forwarding_command(subparsers, "translate", "Translate a WhisperX JSON transcript")
    _forwarding_command(subparsers, "merge-ass", "Merge source and target ASS files")
    _forwarding_command(subparsers, "download", "Download a video and its metadata")
    _forwarding_command(subparsers, "prepare-video", "Create the timestamp-stabilized edit video")
    _forwarding_command(subparsers, "whisper", "Run WhisperX and create a word-level JSON")
    _forwarding_command(subparsers, "burn", "Burn an ASS subtitle into a video with ffmpeg")
    init_parser = subparsers.add_parser("init", help="Create missing project configuration files")
    init_parser.add_argument("--directory", default=".", help="Project configuration directory")
    return parser


def _normalize_result(result) -> int:
    return int(result or 0)


def _command_index(raw_args: Sequence[str]) -> int | None:
    index = 0
    while index < len(raw_args):
        value = raw_args[index]
        if value == "--project-dir":
            index += 2
            continue
        if value.startswith("--project-dir="):
            index += 1
            continue
        if value in {"-h", "--help", "--version"}:
            return None
        return index
    return None


def _run_init(directory: str) -> int:
    root = Path(directory).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        examples = {
            ".env.example": ".env",
            "providers.example.json": "providers.json",
            "tavily_domains.example.json": "tavily_domains.json",
            "glossary_prompt.example.md": "glossary_prompt.md",
            "translate_prompt.example.md": "translate_prompt.md",
            "proofread_prompt.example.md": "proofread_prompt.md",
            "split_prompt.example.md": "split_prompt.md",
            "template.ass.example": "template.ass",
        }
        for name, target_name in examples.items():
            source = importlib.resources.files("subtitle_translation").joinpath("examples", name)
            target = root / target_name
            if not target.exists():
                with source.open("rb") as source_file, target.open("wb") as target_file:
                    shutil.copyfileobj(source_file, target_file)
                print(f"created {target_name}")
        return 0
    except OSError as error:
        print(f"Error: failed to initialize project: {error}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    command_index = _command_index(raw_args)
    if command_index is None:
        if any(value in {"-h", "--help", "--version"} for value in raw_args):
            build_parser().parse_args(raw_args)
        build_parser().print_help()
        return 2
    global_args = build_parser().parse_args(raw_args[:command_index])
    command = raw_args[command_index]
    command_args = raw_args[command_index + 1 :]
    if global_args.project_dir:
        import os

        os.environ["SUBTITLE_TRANSLATION_PROJECT_DIR"] = str(Path(global_args.project_dir).expanduser().resolve())
    if command == "translate":
        return _normalize_result(translate_srt.main(command_args))
    if command == "merge-ass":
        return _normalize_result(merge_ass.main(command_args))
    if command == "batch":
        return _normalize_result(batch_runtime.main(command_args))
    if command == "pipeline":
        return _normalize_result(pipeline.main(command_args))
    if command == "init":
        init_parser = argparse.ArgumentParser(prog="subtitle-translation init")
        init_parser.add_argument("--directory", default=".")
        return _run_init(init_parser.parse_args(command_args).directory)
    if command == "download":
        return stages.main_download(command_args)
    if command == "prepare-video":
        return stages.main_prepare_video(command_args)
    if command == "whisper":
        return stages.main_whisper(command_args)
    if command == "burn":
        return stages.main_burn(command_args)
    build_parser().error(f"unsupported command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
