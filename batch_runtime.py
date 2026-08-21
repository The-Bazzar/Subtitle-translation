"""Stage-aware batch runtime through translation and optional burn."""

from __future__ import annotations

import argparse
import asyncio
import codecs
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import langcodes

from batch_scheduler import (
    AcquisitionRunners,
    AcquisitionScheduler,
    BatchControl,
    BatchTask,
    BurnRunner,
    CURRENT_TASK_INDEX,
    PostprocessRunner,
    ResourceLimits,
    StageAdvancementStopped,
    StageCommandError,
    TaskState,
    aggregate_exit_code,
)
from whisper_worker import (
    AsrWorkerController,
    asr_worker_config_from_environment,
)


def emit_task_bell(kind: str, stream=None) -> None:
    """Emit a dependency-free terminal bell pattern without affecting task status."""
    stream = stream if stream is not None else sys.stderr
    delays = (0.08,) if kind == "success" else (0.18, 0.18)
    try:
        stream.write("\a")
        stream.flush()
        for delay in delays:
            time.sleep(delay)
            stream.write("\a")
            stream.flush()
    except (OSError, ValueError):
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量字幕流水线 - 自动按阶段调度资源",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "url1" "url2" "url3"
  %(prog)s --skip-burn --translate-provider deepseek "url1" "url2"
  %(prog)s --dry-run --report batch-result.txt "url1"
        """,
    )
    parser.add_argument("urls", nargs="+", help="YouTube 链接列表")
    burn_group = parser.add_mutually_exclusive_group()
    burn_group.add_argument(
        "-B",
        "--burn",
        dest="burn",
        type=int,
        choices=(0, 1),
        default=1,
        help="硬压开关: 1=启用, 0=跳过 (默认: 1)",
    )
    burn_group.add_argument(
        "--skip-burn",
        "-SkipBurn",
        dest="burn",
        action="store_false",
        help="跳过后续硬压阶段",
    )
    parser.add_argument(
        "-r",
        "--report",
        "-Report",
        default=None,
        help="结果报告路径 (默认: 脚本同目录 batch-result.txt)",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        "-DryRun",
        action="store_true",
        help="仅打印阶段计划, 不执行",
    )
    parser.add_argument(
        "-p",
        "--translate-provider",
        "-TranslateProvider",
        default=None,
        help="翻译 provider 覆盖值",
    )
    parser.add_argument(
        "-tm",
        "--translate-model",
        "-TranslateModel",
        default=None,
        help="翻译 model 覆盖值",
    )
    parser.add_argument("-Help", action="help", help=argparse.SUPPRESS)
    return parser


def _language_suffix(value: str, fallback: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return fallback
    for candidate in (raw, raw.replace("_", "-")):
        try:
            language = langcodes.Language.get(langcodes.standardize_tag(candidate))
        except Exception:
            continue
        if language.is_valid() and language.language and language.language != "und":
            return language.language.lower()
    try:
        language = langcodes.find(raw)
    except Exception:
        language = None
    if language is not None and language.language and language.language != "und":
        return language.language.lower()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw.lower()).strip("-")
    return safe or fallback


@dataclass(frozen=True)
class LogEvent:
    task_index: int
    stage: str
    stream: str
    text: str
    line_complete: bool = True


class BatchRunResult(list[BatchTask]):
    def __init__(
        self,
        tasks: Sequence[BatchTask],
        report_metadata: Mapping[str, object],
    ) -> None:
        super().__init__(tasks)
        self.report_metadata = dict(report_metadata)


class BatchInterrupted(Exception):
    def __init__(
        self,
        tasks: Sequence[BatchTask],
        interrupt_count: int,
        report_metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.tasks = list(tasks)
        self.interrupt_count = interrupt_count
        self.report_metadata = dict(report_metadata or {})
        super().__init__(f"batch interrupted {interrupt_count} time(s)")


PROCESS_STREAM_READ_BYTES = 64 * 1024
TERMINAL_DISPLAY_CHUNK_BYTES = 64 * 1024
TERMINAL_QUEUE_MAXSIZE = 256
TERMINAL_RAW_TAIL_BYTES = 256 * 1024


class _BoundedByteTail:
    def __init__(self, limit: int = TERMINAL_RAW_TAIL_BYTES) -> None:
        self.limit = limit
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self.truncated = False

    def append(self, data: bytes) -> None:
        if not data:
            return
        if len(data) >= self.limit:
            self._chunks.clear()
            self._chunks.append(data[-self.limit:])
            self._size = self.limit
            self.truncated = True
            return
        self._chunks.append(data)
        self._size += len(data)
        overflow = self._size - self.limit
        if overflow <= 0:
            return
        self.truncated = True
        while overflow > 0:
            first = self._chunks.popleft()
            if len(first) <= overflow:
                overflow -= len(first)
                self._size -= len(first)
                continue
            self._chunks.appendleft(first[overflow:])
            self._size -= overflow
            overflow = 0

    def text(self, *, strip_final_newline: bool = False) -> str:
        payload = b"".join(self._chunks)
        if strip_final_newline and payload.endswith(b"\n"):
            payload = payload[:-1]
        text = payload.decode("utf-8", errors="replace")
        if not self.truncated:
            return text
        marker = f"[... output truncated; showing last {self.limit} bytes ...]\n"
        return marker + text


class TerminalEventQueue:
    def __init__(self, *, stdout=None, stderr=None) -> None:
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self._queue: asyncio.Queue[LogEvent | None] = asyncio.Queue(
            maxsize=TERMINAL_QUEUE_MAXSIZE
        )
        self._event_slots = asyncio.Semaphore(TERMINAL_QUEUE_MAXSIZE - 1)
        self._stage_tails: dict[tuple[int, str, str], _BoundedByteTail] = {}
        self._task_tails: dict[tuple[int, str], _BoundedByteTail] = {}
        self._task_line_open: set[tuple[int, str]] = set()
        self._closed = False

    async def publish(self, event: LogEvent) -> None:
        await self._event_slots.acquire()
        if self._closed:
            self._event_slots.release()
            return
        encoded = event.text.encode("utf-8", errors="replace")
        stage_key = (event.task_index, event.stage, event.stream)
        stage_tail = self._stage_tails.setdefault(stage_key, _BoundedByteTail())
        stage_tail.append(encoded)
        if event.line_complete:
            stage_tail.append(b"\n")

        task_key = (event.task_index, event.stream)
        task_tail = self._task_tails.setdefault(task_key, _BoundedByteTail())
        if task_key not in self._task_line_open:
            task_tail.append(f"[{event.stage}] ".encode("utf-8"))
        task_tail.append(encoded)
        if event.line_complete:
            task_tail.append(b"\n")
            self._task_line_open.discard(task_key)
        else:
            self._task_line_open.add(task_key)
        self._queue.put_nowait(event)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)

    async def run_printer(self) -> None:
        try:
            while True:
                event = await self._queue.get()
                try:
                    if event is None:
                        return
                    stream = self.stderr if event.stream == "stderr" else self.stdout
                    print(
                        f"[{event.task_index:02d}][{event.stage}] {event.text}",
                        file=stream,
                        flush=True,
                    )
                finally:
                    if event is not None:
                        self._event_slots.release()
                    self._queue.task_done()
        finally:
            self._closed = True
            while True:
                try:
                    queued_event = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if queued_event is not None:
                    self._event_slots.release()
                self._queue.task_done()

    def stage_output(self, task_index: int, stage: str, stream: str) -> str:
        tail = self._stage_tails.get((task_index, stage, stream))
        return "" if tail is None else tail.text(strip_final_newline=True)

    def consume_stage_output(
        self,
        task_index: int,
        stage: str,
    ) -> tuple[str, str]:
        output = []
        for stream in ("stdout", "stderr"):
            tail = self._stage_tails.pop((task_index, stage, stream), None)
            output.append("" if tail is None else tail.text(strip_final_newline=True))
        return output[0], output[1]

    def task_output(self, task_index: int) -> tuple[str, str]:
        output = []
        for stream in ("stdout", "stderr"):
            tail = self._task_tails.get((task_index, stream))
            output.append("" if tail is None else tail.text(strip_final_newline=True))
        return output[0], output[1]


async def _read_process_stream(
    reader: asyncio.StreamReader | None,
    *,
    terminal: TerminalEventQueue,
    task_index: int,
    stage: str,
    stream_name: str,
) -> None:
    if reader is None:
        return
    buffer = bytearray()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    skip_lf = False

    async def publish_fragment(data: bytes, *, line_complete: bool) -> None:
        nonlocal decoder
        text = decoder.decode(data, final=line_complete)
        await terminal.publish(
            LogEvent(
                task_index=task_index,
                stage=stage,
                stream=stream_name,
                text=text,
                line_complete=line_complete,
            )
        )
        if line_complete:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    while True:
        chunk = await reader.read(PROCESS_STREAM_READ_BYTES)
        if not chunk:
            break
        if skip_lf:
            if chunk.startswith(b"\n"):
                chunk = chunk[1:]
            skip_lf = False
        buffer.extend(chunk)
        while True:
            newline_index = buffer.find(b"\n")
            carriage_index = buffer.find(b"\r")
            delimiter_indexes = [
                index for index in (newline_index, carriage_index) if index >= 0
            ]
            if not delimiter_indexes:
                while len(buffer) > TERMINAL_DISPLAY_CHUNK_BYTES:
                    fragment = bytes(buffer[:TERMINAL_DISPLAY_CHUNK_BYTES])
                    del buffer[:TERMINAL_DISPLAY_CHUNK_BYTES]
                    await publish_fragment(fragment, line_complete=False)
                break
            delimiter_index = min(delimiter_indexes)
            while delimiter_index > TERMINAL_DISPLAY_CHUNK_BYTES:
                fragment = bytes(buffer[:TERMINAL_DISPLAY_CHUNK_BYTES])
                del buffer[:TERMINAL_DISPLAY_CHUNK_BYTES]
                delimiter_index -= TERMINAL_DISPLAY_CHUNK_BYTES
                await publish_fragment(fragment, line_complete=False)
            delimiter = buffer[delimiter_index]
            fragment = bytes(buffer[:delimiter_index])
            del buffer[:delimiter_index + 1]
            await publish_fragment(fragment, line_complete=True)
            if delimiter == ord("\r"):
                if buffer.startswith(b"\n"):
                    del buffer[:1]
                elif not buffer:
                    skip_lf = True
    if buffer:
        await publish_fragment(bytes(buffer), line_complete=True)


def _process_group_kwargs(platform: str | None = None) -> dict[str, object]:
    platform = platform or os.name
    if platform == "nt":
        return {
            "creationflags": getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
        }
    return {"start_new_session": True}


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    platform: str | None = None,
) -> None:
    platform = platform or os.name
    if platform == "nt":
        try:
            terminator = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            taskkill_exit = await terminator.wait()
            if taskkill_exit != 0 and process.returncode is None:
                process.kill()
        except (FileNotFoundError, OSError):
            if process.returncode is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.returncode is None:
                process.kill()

    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def _run_stage_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stage: str,
    output_marker: str | None = None,
    task_index: int | None = None,
    terminal: TerminalEventQueue | None = None,
    control: BatchControl | None = None,
) -> str:
    reservation = control.reserve_command(stage) if control is not None else None
    task_index = CURRENT_TASK_INDEX.get() if task_index is None else task_index
    owns_terminal = terminal is None
    terminal = terminal or TerminalEventQueue()
    printer_task = (
        asyncio.create_task(terminal.run_printer()) if owns_terminal else None
    )
    process = None
    force_token = None
    stdout_task = None
    stderr_task = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_process_group_kwargs(),
        )
        if control is not None:
            force_token = control.register_force_callback(
                lambda: _terminate_process_tree(process)
            )
        stdout_task = asyncio.create_task(
            _read_process_stream(
                process.stdout,
                terminal=terminal,
                task_index=task_index,
                stage=stage,
                stream_name="stdout",
            )
        )
        stderr_task = asyncio.create_task(
            _read_process_stream(
                process.stderr,
                terminal=terminal,
                task_index=task_index,
                stage=stage,
                stream_name="stderr",
            )
        )
        return_code, _stdout_result, _stderr_result = await asyncio.gather(
            process.wait(),
            stdout_task,
            stderr_task,
        )
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_process_tree(process)
        pending_streams = tuple(
            task for task in (stdout_task, stderr_task) if task is not None
        )
        if pending_streams:
            await asyncio.gather(*pending_streams, return_exceptions=True)
        raise
    except Exception:
        if process is not None and process.returncode is None:
            await _terminate_process_tree(process)
        pending_streams = tuple(
            task for task in (stdout_task, stderr_task) if task is not None
        )
        if pending_streams:
            await asyncio.gather(*pending_streams, return_exceptions=True)
        raise
    finally:
        if control is not None and force_token is not None:
            control.unregister_force_callback(force_token)
        if owns_terminal:
            await terminal.close()
            await printer_task
        if control is not None and reservation is not None:
            control.release_command(reservation)
    stdout, stderr = terminal.consume_stage_output(task_index, stage)
    if control is not None and control.force_requested:
        raise StageAdvancementStopped(f"batch force-interrupted during {stage}")
    if return_code != 0:
        detail = stderr or stdout or f"exit code {return_code}"
        raise StageCommandError(f"{stage} failed: {detail[-2000:]}")
    if output_marker is None:
        return ""
    matches = [
        line[len(output_marker):]
        for line in stdout.splitlines()
        if line.startswith(output_marker)
    ]
    if not matches or not matches[-1]:
        raise StageCommandError(f"{stage} did not emit {output_marker}<path>")
    return matches[-1]


def load_project_environment(
    script_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    env_path = script_dir / ".env"
    if env_path.is_file():
        with env_path.open("r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key and key not in env:
                    env[key] = value.strip()
    return env


def build_stage_environment(
    args: argparse.Namespace,
    *,
    script_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = load_project_environment(
        script_dir or Path(__file__).resolve().parent,
        environ=environ,
    )
    env["BURN"] = "1" if args.burn else "0"
    env["PIPELINE_SKIP_BURN"] = "0" if args.burn else "1"
    if args.translate_provider:
        env["TRANSLATE_PROVIDER"] = args.translate_provider
    if args.translate_model:
        env["TRANSLATE_MODEL"] = args.translate_model
    return env


def create_platform_runners(
    script_dir: Path,
    env: dict[str, str],
    *,
    platform: str | None = None,
    terminal: TerminalEventQueue | None = None,
    control: BatchControl | None = None,
) -> AcquisitionRunners:
    platform = platform or os.name
    if platform == "nt":
        powershell = shutil.which("pwsh") or "pwsh"

        async def download(url: str) -> str:
            return await _run_stage_command(
                [powershell, "-NoProfile", "-File", str(script_dir / "download.ps1"), url],
                cwd=script_dir,
                env=env,
                stage="download",
                output_marker="OUTPUT_RENDER_VIDEO=",
                terminal=terminal,
                control=control,
            )

        async def prepare(render_video: str) -> str:
            return await _run_stage_command(
                [powershell, "-NoProfile", "-File", str(script_dir / "prepare-video.ps1"), render_video],
                cwd=script_dir,
                env=env,
                stage="prepare",
                output_marker="OUTPUT_VIDEO=",
                terminal=terminal,
                control=control,
            )

        ffmpeg = env.get("FFMPEG_PATH_WIN") or "ffmpeg"
    else:
        async def download(url: str) -> str:
            return await _run_stage_command(
                ["bash", str(script_dir / "download.sh"), url],
                cwd=script_dir,
                env=env,
                stage="download",
                output_marker="OUTPUT_RENDER_VIDEO=",
                terminal=terminal,
                control=control,
            )

        async def prepare(render_video: str) -> str:
            return await _run_stage_command(
                ["bash", str(script_dir / "prepare-video.sh"), render_video],
                cwd=script_dir,
                env=env,
                stage="prepare",
                output_marker="OUTPUT_VIDEO=",
                terminal=terminal,
                control=control,
            )

        ffmpeg = env.get("FFMPEG_PATH_LINUX") or "ffmpeg"

    async def extract_audio(edit_video: str) -> str:
        wav_path = str(Path(edit_video).with_suffix(".wav"))
        await _run_stage_command(
            [
                ffmpeg,
                "-i",
                edit_video,
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                wav_path,
                "-y",
                "-loglevel",
                "error",
            ],
            cwd=script_dir,
            env=env,
            stage="extract_audio",
            terminal=terminal,
            control=control,
        )
        return wav_path

    return AcquisitionRunners(download, prepare, extract_audio)


def create_platform_postprocess_runner(
    script_dir: Path,
    env: dict[str, str],
    *,
    platform: str | None = None,
    terminal: TerminalEventQueue | None = None,
    control: BatchControl | None = None,
) -> PostprocessRunner:
    platform = platform or os.name
    if platform == "nt":
        wrapper_prefix = [
            shutil.which("pwsh") or "pwsh",
            "-NoProfile",
            "-File",
            str(script_dir / "py_launcher.ps1"),
            "translate_srt",
        ]
    else:
        wrapper_prefix = [
            "bash",
            str(script_dir / "py_launcher.sh"),
            "translate_srt",
        ]

    async def postprocess(task: BatchTask) -> None:
        if (
            task.json_path is None
            or task.edit_video_path is None
            or task.beautified_candidate_path is None
        ):
            raise StageCommandError(
                "postprocess task is missing aligned JSON, edit-video path, "
                "or beautified candidate"
            )
        aligned_json = str(task.json_path)
        edit_video = str(task.edit_video_path)
        beautified_json = task.beautified_candidate_path
        await _run_stage_command(
            wrapper_prefix
            + [
                aligned_json,
                "--video",
                edit_video,
                "--only-beautify",
                "--beautified-json",
                str(beautified_json),
            ],
            cwd=script_dir,
            env=env,
            stage="beautify",
            terminal=terminal,
            control=control,
        )
        if not beautified_json.is_file():
            raise StageCommandError(
                f"beautify did not write expected output: {beautified_json}"
            )
        await _run_stage_command(
            wrapper_prefix
            + [
                aligned_json,
                "--video",
                edit_video,
                "--only-glossary",
                "--skip-beautify",
                "--beautified-json",
                str(beautified_json),
            ],
            cwd=script_dir,
            env=env,
            stage="glossary",
            terminal=terminal,
            control=control,
        )
        await _run_stage_command(
            wrapper_prefix
            + [
                aligned_json,
                "--video",
                edit_video,
                "--skip-beautify",
                "--skip-knowledge",
                "--beautified-json",
                str(beautified_json),
            ],
            cwd=script_dir,
            env=env,
            stage="translate",
            terminal=terminal,
            control=control,
        )
        source_code = _language_suffix(
            env.get("SOURCE_LANG") or task.detected_language,
            task.detected_language or "source",
        )
        target_code = _language_suffix(env.get("TARGET_LANG") or "zh", "zh")
        ass_path = task.edit_video_path.with_name(
            f"{task.edit_video_path.stem}.{source_code}-{target_code}.ass"
        )
        if not ass_path.is_file() or ass_path.stat().st_size <= 0:
            raise StageCommandError(
                f"translate did not write expected bilingual ASS: {ass_path}"
            )
        task.ass_path = ass_path

    return postprocess


def create_platform_burn_runner(
    script_dir: Path,
    env: dict[str, str],
    *,
    platform: str | None = None,
    terminal: TerminalEventQueue | None = None,
    control: BatchControl | None = None,
) -> BurnRunner:
    platform = platform or os.name
    if platform == "nt":
        wrapper_prefix = [
            shutil.which("pwsh") or "pwsh",
            "-NoProfile",
            "-File",
            str(script_dir / "ffmpeg-burn.ps1"),
        ]
        sub_file_flag = "-SubFile"
    else:
        wrapper_prefix = ["bash", str(script_dir / "ffmpeg-burn.sh")]
        sub_file_flag = "--sub-file"

    async def burn(task: BatchTask) -> None:
        if task.render_video_path is None or task.edit_video_path is None:
            raise StageCommandError("burn task is missing original or edit video path")
        source_code = _language_suffix(
            env.get("SOURCE_LANG") or task.detected_language,
            task.detected_language or "source",
        )
        target_code = _language_suffix(env.get("TARGET_LANG") or "zh", "zh")
        ass_path = task.ass_path or task.edit_video_path.with_name(
            f"{task.edit_video_path.stem}.{source_code}-{target_code}.ass"
        )
        if not ass_path.is_file() or ass_path.stat().st_size <= 0:
            raise StageCommandError(f"burn subtitle is missing or empty: {ass_path}")
        output = await _run_stage_command(
            wrapper_prefix
            + [str(task.render_video_path), sub_file_flag, str(ass_path)],
            cwd=script_dir,
            env=env,
            stage="burn",
            output_marker="OUTPUT_BURNED_VIDEO=",
            terminal=terminal,
            control=control,
        )
        output_path = Path(output).resolve()
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise StageCommandError(
                f"burn did not write expected non-empty output: {output_path}"
            )
        task.ass_path = ass_path
        task.burned_video_path = output_path

    return burn


def run_acquisition(
    args: argparse.Namespace,
    limits: ResourceLimits,
    *,
    script_dir: Path,
    runners: AcquisitionRunners | None = None,
    worker_factory=AsrWorkerController,
    postprocess_runner: PostprocessRunner | None = None,
    burn_runner: BurnRunner | None = None,
) -> BatchRunResult:
    stage_environment = build_stage_environment(args, script_dir=script_dir)
    control = BatchControl()
    terminal = TerminalEventQueue()
    stage_runners = runners or create_platform_runners(
        script_dir,
        stage_environment,
        terminal=terminal,
        control=control,
    )
    stage_postprocess_runner = postprocess_runner
    if stage_postprocess_runner is None:
        stage_postprocess_runner = create_platform_postprocess_runner(
            script_dir,
            stage_environment,
            terminal=terminal,
            control=control,
        )
    stage_burn_runner = burn_runner
    if args.burn and stage_burn_runner is None:
        stage_burn_runner = create_platform_burn_runner(
            script_dir,
            stage_environment,
            terminal=terminal,
            control=control,
        )
    scheduler = AcquisitionScheduler(
        args.urls,
        limits,
        stage_runners,
        asr_config=asr_worker_config_from_environment(stage_environment),
        worker_factory=worker_factory,
        postprocess_runner=stage_postprocess_runner,
        burn_runner=stage_burn_runner if args.burn else None,
        control=control,
        failure_log_dir=Path.cwd(),
        log_snapshot_provider=terminal.task_output,
    )

    async def execute() -> list[BatchTask]:
        printer_task = asyncio.create_task(terminal.run_printer())
        previous_sigint = None
        signal_installed = threading.current_thread() is threading.main_thread()
        if signal_installed:
            previous_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(
                signal.SIGINT,
                lambda _signum, _frame: scheduler.request_interrupt(),
            )
        try:
            return await scheduler.run()
        finally:
            if signal_installed:
                signal.signal(signal.SIGINT, previous_sigint)
            await terminal.close()
            await printer_task

    tasks = asyncio.run(execute())
    report_metadata = scheduler.report_metadata
    if control.interrupted:
        raise BatchInterrupted(
            tasks,
            control.interrupt_count,
            report_metadata=report_metadata,
        )
    return BatchRunResult(tasks, report_metadata)


def _task_status(task: BatchTask) -> str:
    return "OK" if task.state is TaskState.SUCCEEDED else "FAIL"


def _task_output_directory(task: BatchTask) -> str | None:
    for output_path in (
        task.burned_video_path,
        task.ass_path,
        task.json_path,
        task.edit_video_path,
        task.render_video_path,
    ):
        if output_path is not None:
            return str(output_path.resolve().parent)
    return None


def write_report(
    path: Path,
    tasks: Sequence[BatchTask],
    started_at: datetime,
    *,
    diagnostics: Mapping[str, object] | None = None,
) -> None:
    diagnostics = dict(diagnostics or {})
    worker_failure = bool(diagnostics.get("worker_failure", False))
    worker_failure_log = diagnostics.get("worker_failure_log")
    worker_failure_root_cause = diagnostics.get("worker_failure_root_cause")
    worker_failure_detail = diagnostics.get("worker_failure_detail")
    output_directory = str(
        Path(str(diagnostics.get("output_directory") or Path.cwd())).resolve()
    )
    cleanup_diagnostics = list(diagnostics.get("cleanup_diagnostics") or [])
    total_elapsed = (datetime.now() - started_at).total_seconds() / 60
    lines = [
        f"batch report - {started_at.strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
    ]
    for task in tasks:
        detail = f" - {task.error_detail}" if task.error_detail else ""
        lines.append(
            f"{task.index:3d}. [{_task_status(task)}] "
            f"{task.elapsed_seconds / 60:5.1f}min "
            f"stage={task.stage} {task.url}{detail}"
        )
    failed = sum(task.state is not TaskState.SUCCEEDED for task in tasks)
    lines.extend(
        [
            "=" * 60,
            (
                f"Total: {len(tasks)}, Success: {len(tasks) - failed}, "
                f"Failed: {failed}, Elapsed: {total_elapsed:.1f}min"
            ),
            f"Worker failure: {'yes' if worker_failure else 'no'}",
            f"Worker failure log: {worker_failure_log or '-'}",
            (
                "Worker failure root cause: "
                + (
                    json.dumps(worker_failure_root_cause, ensure_ascii=False)
                    if worker_failure_root_cause
                    else "-"
                )
            ),
            f"Worker failure detail: {worker_failure_detail or '-'}",
            f"Output directory: {output_directory}",
        ]
    )
    if cleanup_diagnostics:
        lines.append("Cleanup diagnostics:")
        lines.extend(
            f"  - {item.get('stage', 'cleanup')}: {item.get('detail', '')}"
            for item in cleanup_diagnostics
        )
    else:
        lines.append("Cleanup diagnostics: none")

    report_payload = {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "elapsed_seconds": total_elapsed * 60,
        "worker_failure": worker_failure,
        "worker_failure_log": worker_failure_log,
        "worker_failure_root_cause": worker_failure_root_cause,
        "worker_failure_detail": worker_failure_detail,
        "output_directory": output_directory,
        "cleanup_diagnostics": cleanup_diagnostics,
        "summary": {
            "total": len(tasks),
            "success": len(tasks) - failed,
            "failed": failed,
        },
        "tasks": [
            {
                "index": task.index,
                "url": task.url,
                "state": task.state.value,
                "stage": task.stage,
                "elapsed_seconds": task.elapsed_seconds,
                "error_detail": task.error_detail or None,
                "output_directory": _task_output_directory(task),
                "render_video": (
                    str(task.render_video_path.resolve())
                    if task.render_video_path is not None
                    else None
                ),
                "edit_video": (
                    str(task.edit_video_path.resolve())
                    if task.edit_video_path is not None
                    else None
                ),
                "aligned_json": (
                    str(task.json_path.resolve())
                    if task.json_path is not None
                    else None
                ),
                "subtitle": (
                    str(task.ass_path.resolve())
                    if task.ass_path is not None
                    else None
                ),
                "burned_video": (
                    str(task.burned_video_path.resolve())
                    if task.burned_video_path is not None
                    else None
                ),
            }
            for task in tasks
        ],
    }
    text_path = path.with_suffix(".txt") if path.suffix.lower() == ".json" else path
    json_path = path if path.suffix.lower() == ".json" else path.with_suffix(".json")
    text_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    script_dir = Path(__file__).resolve().parent
    report_path = Path(args.report) if args.report else script_dir / "batch-result.txt"
    limits = ResourceLimits.detect()
    started_at = datetime.now()

    print("=" * 60)
    print(f"batch - {len(args.urls)} videos, automatic stage capacities")
    print("=" * 60)
    print(f"Start:    {started_at.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"CPU/IO:   {limits.cpu_io}")
    print(f"NVENC:    {limits.nvenc}")
    print(f"Burn:     {'yes' if args.burn else 'no'}")
    if args.translate_provider:
        print(f"Provider: {args.translate_provider}")
    if args.translate_model:
        print(f"Model:    {args.translate_model}")
    print("Current:  download -> prepare -> ASR -> align -> translate -> burn")
    print("=" * 60)

    if args.dry_run:
        for index, url in enumerate(args.urls, start=1):
            print(
                f"[DRY RUN][{index:02d}] "
                "download -> prepare -> extract_audio -> asr -> align -> translate"
                f"{' -> burn' if args.burn else ''} <- {url}"
            )
        return 0

    interrupted = False
    report_metadata: Mapping[str, object] = {}
    try:
        tasks = run_acquisition(args, limits, script_dir=script_dir)
        report_metadata = getattr(tasks, "report_metadata", {})
    except BatchInterrupted as exc:
        tasks = exc.tasks
        report_metadata = exc.report_metadata
        interrupted = True
    for task in tasks:
        print(
            f"[{task.index}/{len(tasks)}] {_task_status(task)} "
            f"stage={task.stage} ({task.elapsed_seconds / 60:.1f}min) <- {task.url}"
        )
        if task.state is not TaskState.SUCCEEDED:
            if task.error_detail:
                print(f"  {task.error_detail}")
            emit_task_bell("error")

    write_report(
        report_path,
        tasks,
        started_at,
        diagnostics=report_metadata,
    )
    exit_code = 130 if interrupted else aggregate_exit_code(tasks)
    emit_task_bell("error" if exit_code else "success")
    return exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    _notify_unhandled: bool = False,
) -> int:
    if not _notify_unhandled:
        return _main(argv)
    try:
        return _main(argv)
    except SystemExit:
        raise
    except BaseException:
        emit_task_bell("error")
        raise
