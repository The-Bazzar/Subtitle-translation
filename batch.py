"""Stage-aware batch entry point through translation postprocessing."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from batch_scheduler import (
    AcquisitionRunners,
    AcquisitionScheduler,
    BatchTask,
    PostprocessRunner,
    ResourceLimits,
    StageCommandError,
    TaskState,
    aggregate_exit_code,
)
from whisper_worker import AsrWorkerController, asr_worker_config_from_environment


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


def _decode_output(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()


def _print_stage_output(stage: str, output: str, stream=None) -> None:
    if not output:
        return
    stream = stream if stream is not None else sys.stdout
    for line in output.splitlines():
        print(f"[{stage}] {line}", file=stream)


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
) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_process_group_kwargs(),
    )
    try:
        stdout_data, stderr_data = await process.communicate()
    except asyncio.CancelledError:
        await _terminate_process_tree(process)
        raise
    stdout = _decode_output(stdout_data)
    stderr = _decode_output(stderr_data)
    _print_stage_output(stage, stdout)
    _print_stage_output(stage, stderr, stream=sys.stderr)
    if process.returncode != 0:
        detail = stderr or stdout or f"exit code {process.returncode}"
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
            )

        async def prepare(render_video: str) -> str:
            return await _run_stage_command(
                [powershell, "-NoProfile", "-File", str(script_dir / "prepare-video.ps1"), render_video],
                cwd=script_dir,
                env=env,
                stage="prepare",
                output_marker="OUTPUT_VIDEO=",
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
            )

        async def prepare(render_video: str) -> str:
            return await _run_stage_command(
                ["bash", str(script_dir / "prepare-video.sh"), render_video],
                cwd=script_dir,
                env=env,
                stage="prepare",
                output_marker="OUTPUT_VIDEO=",
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
        )
        return wav_path

    return AcquisitionRunners(download, prepare, extract_audio)


def create_platform_postprocess_runner(
    script_dir: Path,
    env: dict[str, str],
    *,
    platform: str | None = None,
) -> PostprocessRunner:
    platform = platform or os.name
    if platform == "nt":
        wrapper_prefix = [
            shutil.which("pwsh") or "pwsh",
            "-NoProfile",
            "-File",
            str(script_dir / "translate_srt.ps1"),
        ]
    else:
        wrapper_prefix = ["bash", str(script_dir / "translate_srt.sh")]

    async def postprocess(task: BatchTask) -> None:
        if task.json_path is None or task.edit_video_path is None:
            raise StageCommandError(
                "postprocess task is missing aligned JSON or edit-video path"
            )
        aligned_json = str(task.json_path)
        edit_video = str(task.edit_video_path)
        beautified_json = task.json_path.with_name(
            f"{task.json_path.stem}.beautified.json"
        )
        await _run_stage_command(
            wrapper_prefix
            + [aligned_json, "--video", edit_video, "--only-beautify"],
            cwd=script_dir,
            env=env,
            stage="beautify",
        )
        if not beautified_json.is_file():
            raise StageCommandError(
                f"beautify did not write expected output: {beautified_json}"
            )
        await _run_stage_command(
            wrapper_prefix
            + [
                str(beautified_json),
                "--video",
                edit_video,
                "--only-glossary",
                "--skip-beautify",
            ],
            cwd=script_dir,
            env=env,
            stage="glossary",
        )
        await _run_stage_command(
            wrapper_prefix
            + [
                str(beautified_json),
                "--video",
                edit_video,
                "--skip-beautify",
                "--skip-knowledge",
            ],
            cwd=script_dir,
            env=env,
            stage="translate",
        )

    return postprocess


def run_acquisition(
    args: argparse.Namespace,
    limits: ResourceLimits,
    *,
    script_dir: Path,
    runners: AcquisitionRunners | None = None,
    worker_factory=AsrWorkerController,
    postprocess_runner: PostprocessRunner | None = None,
) -> list[BatchTask]:
    stage_environment = build_stage_environment(args, script_dir=script_dir)
    stage_runners = runners or create_platform_runners(script_dir, stage_environment)
    stage_postprocess_runner = postprocess_runner
    if stage_postprocess_runner is None:
        stage_postprocess_runner = create_platform_postprocess_runner(
            script_dir,
            stage_environment,
        )
    scheduler = AcquisitionScheduler(
        args.urls,
        limits,
        stage_runners,
        asr_config=asr_worker_config_from_environment(stage_environment),
        worker_factory=worker_factory,
        postprocess_runner=stage_postprocess_runner,
    )
    return asyncio.run(scheduler.run())


def _task_status(task: BatchTask) -> str:
    return "OK" if task.state is TaskState.SUCCEEDED else "FAIL"


def write_report(path: Path, tasks: Sequence[BatchTask], started_at: datetime) -> None:
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
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
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
    print(f"Burn:     {'yes' if args.burn else 'no'} (Task 6 stage not yet scheduled)")
    if args.translate_provider:
        print(f"Provider: {args.translate_provider}")
    if args.translate_model:
        print(f"Model:    {args.translate_model}")
    print("Current:  download -> prepare -> ASR -> align -> translate")
    print("=" * 60)

    if args.dry_run:
        for index, url in enumerate(args.urls, start=1):
            print(
                f"[DRY RUN][{index:02d}] "
                f"download -> prepare -> extract_audio -> asr -> align -> translate <- {url}"
            )
        return 0

    tasks = run_acquisition(args, limits, script_dir=script_dir)
    for task in tasks:
        print(
            f"[{task.index}/{len(tasks)}] {_task_status(task)} "
            f"stage={task.stage} ({task.elapsed_seconds / 60:.1f}min) <- {task.url}"
        )
        if task.state is not TaskState.SUCCEEDED:
            if task.error_detail:
                print(f"  {task.error_detail}")
            emit_task_bell("error")

    write_report(report_path, tasks, started_at)
    exit_code = aggregate_exit_code(tasks)
    emit_task_bell("error" if exit_code else "success")
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        emit_task_bell("error")
        raise
