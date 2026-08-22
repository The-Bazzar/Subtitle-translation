"""Stage-aware batch runtime through translation and optional burn."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

import langcodes

import translate_srt

from subtitle_translation.config import ProjectConfig
from subtitle_translation.stages import (
    burn_video,
    download_video,
    extract_audio,
    prepare_video,
)
from subtitle_translation.process import terminate_active_processes

from batch_scheduler import (
    AcquisitionRunners,
    AcquisitionScheduler,
    BatchControl,
    BatchTask,
    BurnRunner,
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


async def _run_python_stage(
    function,
    *function_args,
    stage: str,
    config: ProjectConfig,
    control: BatchControl | None,
):
    reservation = control.reserve_command(stage) if control is not None else None
    force_token = (
        control.register_force_callback(terminate_active_processes)
        if control is not None
        else None
    )
    try:
        if control is not None and control.force_requested:
            raise StageAdvancementStopped(f"batch force-interrupted before {stage}")
        result = await asyncio.to_thread(function, *function_args, config)
    finally:
        if control is not None and force_token is not None:
            control.unregister_force_callback(force_token)
        if control is not None and reservation is not None:
            control.release_command(reservation)
    if control is not None and control.force_requested:
        raise StageAdvancementStopped(f"batch force-interrupted during {stage}")
    if not result.success:
        detail = result.diagnostics[0] if result.diagnostics else f"exit code {result.exit_code}"
        raise StageCommandError(f"{stage} failed: {detail}")
    return result


def create_platform_runners(
    script_dir: Path,
    env: dict[str, str],
    *,
    platform: str | None = None,
    terminal: object | None = None,
    control: BatchControl | None = None,
) -> AcquisitionRunners:
    del platform, terminal
    config = ProjectConfig(script_dir, dict(env))

    async def download(url: str) -> str:
        result = await _run_python_stage(
            download_video,
            url,
            stage="download",
            config=config,
            control=control,
        )
        return result.outputs["render_video"]

    async def prepare(render_video: str) -> str:
        result = await _run_python_stage(
            prepare_video,
            render_video,
            stage="prepare",
            config=config,
            control=control,
        )
        return result.outputs["edit_video"]

    async def extract(edit_video: str) -> str:
        result = await _run_python_stage(
            extract_audio,
            edit_video,
            stage="extract_audio",
            config=config,
            control=control,
        )
        return result.outputs["wav"]

    return AcquisitionRunners(download, prepare, extract)


def create_platform_postprocess_runner(
    script_dir: Path,
    env: dict[str, str],
    *,
    platform: str | None = None,
    terminal: object | None = None,
    control: BatchControl | None = None,
) -> PostprocessRunner:
    del platform, terminal
    config = ProjectConfig(script_dir, dict(env))
    os.environ["SUBTITLE_TRANSLATION_PROJECT_DIR"] = str(script_dir)
    for key in (
        "TRANSLATE_PROVIDER",
        "TRANSLATE_MODEL",
        "SOURCE_LANG",
        "TARGET_LANG",
        "PROOFREAD_PROVIDER",
        "PROOFREAD_MODEL",
    ):
        if env.get(key):
            os.environ[key] = env[key]

    async def invoke_translate(arguments: list[str], stage: str) -> None:
        reservation = control.reserve_command(stage) if control is not None else None
        force_token = (
            control.register_force_callback(terminate_active_processes)
            if control is not None
            else None
        )
        try:
            if control is not None and control.force_requested:
                raise StageAdvancementStopped(f"batch force-interrupted before {stage}")
            code = await asyncio.to_thread(translate_srt.main, arguments)
        finally:
            if control is not None and force_token is not None:
                control.unregister_force_callback(force_token)
            if control is not None and reservation is not None:
                control.release_command(reservation)
        if control is not None and control.force_requested:
            raise StageAdvancementStopped(f"batch force-interrupted during {stage}")
        if code:
            raise StageCommandError(f"{stage} failed with exit code {code}")

    async def postprocess(task: BatchTask) -> None:
        if task.json_path is None or task.edit_video_path is None or task.beautified_candidate_path is None:
            raise StageCommandError("postprocess task is missing aligned JSON, edit-video path, or beautified candidate")
        aligned_json = str(task.json_path)
        edit_video = str(task.edit_video_path)
        beautified_json = str(task.beautified_candidate_path)
        common = [aligned_json, "--video", edit_video, "--beautified-json", beautified_json]
        await invoke_translate(common + ["--only-beautify"], "beautify")
        if not task.beautified_candidate_path.is_file():
            raise StageCommandError(f"beautify did not write expected output: {beautified_json}")
        await invoke_translate(common + ["--only-glossary", "--skip-beautify"], "glossary")
        await invoke_translate(common + ["--skip-beautify", "--skip-knowledge"], "translate")
        source_code = _language_suffix(env.get("SOURCE_LANG") or task.detected_language, task.detected_language or "source")
        target_code = _language_suffix(env.get("TARGET_LANG") or "zh", "zh")
        ass_path = task.edit_video_path.with_name(f"{task.edit_video_path.stem}.{source_code}-{target_code}.ass")
        if not ass_path.is_file() or ass_path.stat().st_size <= 0:
            raise StageCommandError(f"translate did not write expected bilingual ASS: {ass_path}")
        task.ass_path = ass_path

    return postprocess


def create_platform_burn_runner(
    script_dir: Path,
    env: dict[str, str],
    *,
    platform: str | None = None,
    terminal: object | None = None,
    control: BatchControl | None = None,
) -> BurnRunner:
    del platform, terminal
    config = ProjectConfig(script_dir, dict(env))

    async def burn(task: BatchTask) -> None:
        if task.render_video_path is None or task.edit_video_path is None:
            raise StageCommandError("burn task is missing original or edit video path")
        source_code = _language_suffix(env.get("SOURCE_LANG") or task.detected_language, task.detected_language or "source")
        target_code = _language_suffix(env.get("TARGET_LANG") or "zh", "zh")
        ass_path = task.ass_path or task.edit_video_path.with_name(f"{task.edit_video_path.stem}.{source_code}-{target_code}.ass")
        if not ass_path.is_file() or ass_path.stat().st_size <= 0:
            raise StageCommandError(f"burn subtitle is missing or empty: {ass_path}")
        result = await _run_python_stage(
            burn_video,
            task.render_video_path,
            ass_path,
            stage="burn",
            config=config,
            control=control,
        )
        task.ass_path = ass_path
        task.burned_video_path = Path(result.outputs["burned_video"]).resolve()

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
    stage_runners = runners or create_platform_runners(
        script_dir,
        stage_environment,
        control=control,
    )
    stage_postprocess_runner = postprocess_runner
    if stage_postprocess_runner is None:
        stage_postprocess_runner = create_platform_postprocess_runner(
            script_dir,
            stage_environment,
            control=control,
        )
    stage_burn_runner = burn_runner
    if args.burn and stage_burn_runner is None:
        stage_burn_runner = create_platform_burn_runner(
            script_dir,
            stage_environment,
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
    )

    async def execute() -> list[BatchTask]:
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
    script_dir = Path(os.environ.get("SUBTITLE_TRANSLATION_PROJECT_DIR") or os.getcwd()).resolve()
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
