from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

import translate_srt

from .config import ProjectConfig, resolve_mfa_enabled
from .notifications import emit_bell
from .stages import StageResult, burn_video, download_video, prepare_video, transcribe_video
from .project_logging import log, project_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subtitle-translation pipeline", description="Run the complete subtitle pipeline; switches/tuning are read from .env")
    parser.add_argument("url", nargs="?")
    parser.add_argument("--project-dir")
    parser.add_argument("--video", "-Video", help="Existing original video when configured to skip download")
    parser.add_argument("--json", "-Json", dest="json_path", help="Existing ASR JSON when configured to skip recognition")
    parser.add_argument("--existing-ass", "-ExistingAss", default="")
    parser.add_argument("--output", "-Output", default="")
    parser.add_argument("--source-lang", "--lang", "-SourceLang", default="")
    parser.add_argument("--target-lang", "-TargetLang", default="")
    parser.set_defaults(skip_download=False, skip_whisper=False, skip_scene_detection=False,
                        skip_knowledge=False, skip_translate=False, skip_burn=False,
                        mfa=None, no_proofread=False, no_split=False, dry_run=False,
                        model="", align_model="", device="", ovc="", ovcopts="", oac="", res="")
    return parser


def _translate_main(
    json_path: Path,
    edit_video: Path | None,
    config: ProjectConfig,
    args: argparse.Namespace,
) -> StageResult:
    old_project_dir = os.environ.get("SUBTITLE_TRANSLATION_PROJECT_DIR")
    os.environ["SUBTITLE_TRANSLATION_PROJECT_DIR"] = str(config.project_dir)
    translate_args = [str(json_path)]
    if edit_video:
        translate_args += ["--video", str(edit_video)]
    if args.source_lang:
        translate_args += ["--source-lang", args.source_lang]
    if args.target_lang:
        translate_args += ["--target-lang", args.target_lang]
    try:
        code = translate_srt.main(translate_args, stage_overrides=config.values)
    except SystemExit as error:
        code = int(error.code or 0)
    finally:
        if old_project_dir is None:
            os.environ.pop("SUBTITLE_TRANSLATION_PROJECT_DIR", None)
        else:
            os.environ["SUBTITLE_TRANSLATION_PROJECT_DIR"] = old_project_dir
    if code:
        return StageResult.fail(code, "translate_srt failed")
    source_lang = args.source_lang or config.get("SOURCE_LANG", "")
    if not source_lang:
        source_lang = translate_srt.load_transcript(str(json_path)).language or "source"
    target_lang = args.target_lang or config.get("TARGET_LANG", "") or "zh"
    source_suffix = translate_srt.iso_639_suffix(source_lang, "source")
    target_suffix = translate_srt.iso_639_suffix(target_lang, "target")
    ass_path = json_path.with_name(f"{json_path.stem}.{source_suffix}-{target_suffix}.ass")
    return StageResult.ok(ass=str(ass_path))


def _env_skip(config: ProjectConfig, stage: str) -> bool:
    return config.flag(f"PIPELINE_SKIP_{stage}")


def _apply_env_skip_defaults(args: argparse.Namespace, config: ProjectConfig) -> None:
    for attribute, stage in (
        ("skip_download", "DOWNLOAD"),
        ("skip_whisper", "WHISPER"),
        ("skip_scene_detection", "SCENE_DETECTION"),
        ("skip_knowledge", "KNOWLEDGE"),
        ("skip_translate", "TRANSLATE"),
        ("skip_burn", "BURN"),
    ):
        setattr(args, attribute, bool(getattr(args, attribute) or _env_skip(config, stage)))


def pipeline_stage_plan(args: argparse.Namespace) -> list[str]:
    stages: list[str] = []
    if not args.skip_download:
        stages.append("download")
    stages.append("prepare")
    if not args.skip_whisper:
        stages.append("whisper")
    if args.skip_translate:
        stages.append("existing_ass")
    elif getattr(args, "mfa", False):
        if not args.skip_knowledge:
            stages.append("glossary/source_review")
        stages.extend(["translate", "mfa"])
        if not args.skip_scene_detection:
            stages.append("scene_detection")
    else:
        if not args.skip_scene_detection:
            stages.append("scene_detection")
        if not args.skip_knowledge:
            stages.append("glossary")
        stages.append("translate")
    if not args.skip_burn:
        stages.append("burn")
    return stages


def run_pipeline(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(args.project_dir)
    try:
        args.mfa = resolve_mfa_enabled(None, config.values)
    except ValueError as error:
        log("ERROR", f'Error: {error}')
        return 2
    _apply_env_skip_defaults(args, config)
    args.dry_run = config.flag("DRY_RUN")
    args.model = config.get("WHISPER_MODEL", "")
    args.align_model = config.get("WHISPER_ALIGN_MODEL", "")
    args.device = config.get("WHISPER_DEVICE", "")
    args.ovc, args.ovcopts = config.get("BURN_OVC", ""), config.get("BURN_OVCOPTS", "")
    args.oac, args.res = config.get("BURN_OAC", ""), config.get("BURN_RES", "")
    if args.source_lang:
        config = ProjectConfig(config.project_dir, {**config.values, "SOURCE_LANG": args.source_lang}, config.invocation_dir)
    if args.target_lang:
        config = ProjectConfig(config.project_dir, {**config.values, "TARGET_LANG": args.target_lang}, config.invocation_dir)
    if args.dry_run:
        print("[DRY RUN] Python pipeline stages:")
        print(f"  {' -> '.join(pipeline_stage_plan(args))}")
        return 0
    if getattr(args, "mfa", False) and not args.skip_translate:
        from .mfa import MfaRuntime

        try:
            MfaRuntime.for_project(config.project_dir)
        except (OSError, ValueError) as error:
            log("ERROR", f'Error: MFA preflight failed: {error}')
            return 2
    if args.skip_download:
        if not args.video:
            log("ERROR", 'Error: --video is required with --skip-download')
            return 2
        download_result = StageResult.ok(render_video=str(Path(args.video).resolve()))
    else:
        if not args.url:
            log("ERROR", 'Error: URL is required')
            return 2
        download_result = download_video(args.url, config)
    if not download_result.success:
        log("ERROR", f'Error: {download_result.diagnostics[0]}')
        emit_bell("error")
        return download_result.exit_code
    render_video = Path(download_result.outputs["render_video"])
    with project_run(render_video.parent, config.get("LOG_LEVEL", "INFO")):
        return _run_project_pipeline(args, config, render_video)


def _run_project_pipeline(args, config, render_video):
    prepare_result = prepare_video(render_video, config)
    if not prepare_result.success:
        log("ERROR", f'Error: {prepare_result.diagnostics[0]}')
        emit_bell("error")
        return prepare_result.exit_code
    edit_video = Path(prepare_result.outputs["edit_video"])
    if args.skip_whisper:
        if not args.json_path:
            log("ERROR", 'Error: --json is required with --skip-whisper')
            return 2
        json_path = Path(args.json_path).expanduser().resolve()
    else:
        whisper_result = transcribe_video(edit_video, config, model=args.model, align_model=args.align_model, device=args.device)
        if not whisper_result.success:
            log("ERROR", f'Error: {whisper_result.diagnostics[0]}')
            emit_bell("error")
            return whisper_result.exit_code
        json_path = Path(whisper_result.outputs["json"])
    if args.skip_translate:
        if not args.existing_ass:
            log("ERROR", 'Error: --existing-ass is required with --skip-translate')
            return 2
        ass_path = Path(args.existing_ass).expanduser().resolve()
    else:
        translate_result = _translate_main(json_path, edit_video, config, args)
        if not translate_result.success:
            log("ERROR", f'Error: {translate_result.diagnostics[0]}')
            emit_bell("error")
            return translate_result.exit_code
        ass_path = Path(args.output).expanduser().resolve() if args.output else Path(translate_result.outputs["ass"])
    if args.skip_burn:
        emit_bell("success")
        return 0
    burn_result = burn_video(render_video, ass_path, config, output=args.output, ovc=args.ovc, ovcopts=args.ovcopts, oac=args.oac, resolution=args.res)
    if not burn_result.success:
        log("ERROR", f'Error: {burn_result.diagnostics[0]}')
        emit_bell("error")
        return burn_result.exit_code
    print(f"OUTPUT_BURNED_VIDEO={burn_result.outputs['burned_video']}")
    emit_bell("success")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
