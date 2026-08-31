from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

import translate_srt

from .config import ProjectConfig
from .notifications import emit_bell
from .stages import StageResult, burn_video, download_video, prepare_video, transcribe_video


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the complete subtitle translation pipeline")
    parser.add_argument("url", nargs="?")
    parser.add_argument("--project-dir")
    parser.add_argument("--video", "-Video", help="Existing original video when download is skipped")
    parser.add_argument("--json", "-Json", dest="json_path", help="Existing WhisperX JSON when WhisperX is skipped")
    parser.add_argument("--existing-ass", "-ExistingAss", default="")
    parser.add_argument("--output", "-Output", default="")
    parser.add_argument("--skip-download", "-SkipDownload", action="store_true")
    parser.add_argument("--skip-whisper", "-SkipWhisper", action="store_true")
    parser.add_argument("--skip-beautify", "-SkipBeautify", action="store_true")
    parser.add_argument("--skip-knowledge", "-SkipKnowledge", action="store_true")
    parser.add_argument("--skip-translate", "-SkipTranslate", action="store_true")
    parser.add_argument("--no-proofread", "-NoProofread", action="store_true")
    parser.add_argument("--skip-burn", "-SkipBurn", action="store_true")
    parser.add_argument("--source-lang", "-SourceLang", default="")
    parser.add_argument("--target-lang", "-TargetLang", default="")
    parser.add_argument("--model", "-Model", default="")
    parser.add_argument("--align-model", "-AlignModel", default="")
    parser.add_argument("--device", "-Device", default="")
    parser.add_argument("--ovc", "-Ovc", default="")
    parser.add_argument("--ovcopts", "-Ovcopts", default="")
    parser.add_argument("--oac", "-Oac", default="")
    parser.add_argument("--res", "-Res", default="")
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--no-split", action="store_true")
    parser.add_argument("--split-max-chars", type=int, default=None)
    parser.add_argument("--split-max-duration", type=float, default=None)
    parser.add_argument("--split-context-window", type=int, default=None)
    parser.add_argument("--scene-threshold", type=float, default=None)
    parser.add_argument("--snap-frames", type=int, default=None)
    parser.add_argument("--end-offset-frames", type=int, default=None)
    parser.add_argument("--min-scene-interval-frames", type=int, default=None)
    parser.add_argument("--min-duration", type=float, default=None)
    parser.add_argument("--min-gap", type=float, default=None)
    parser.add_argument("--max-gap-merge", type=float, default=None)
    parser.add_argument("--aggressive", action="store_true")
    parser.add_argument("--no-scene-snap", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--dry-run", "-DryRun", action="store_true")
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
    if args.skip_beautify:
        translate_args.append("--skip-beautify")
    if args.skip_knowledge:
        translate_args.append("--skip-knowledge")
    if args.no_proofread:
        translate_args.append("--no-proofread")
    if args.no_split:
        translate_args.append("--no-split")
    if args.quiet:
        translate_args.append("--quiet")
    if args.aggressive:
        translate_args.append("--aggressive")
    if args.no_scene_snap:
        translate_args.append("--no-scene-snap")
    for option_name, argument_name in (
        ("batch_size", "--batch-size"),
        ("split_max_chars", "--split-max-chars"),
        ("split_max_duration", "--split-max-duration"),
        ("split_context_window", "--split-context-window"),
        ("scene_threshold", "--scene-threshold"),
        ("snap_frames", "--snap-frames"),
        ("end_offset_frames", "--end-offset-frames"),
        ("min_scene_interval_frames", "--min-scene-interval-frames"),
        ("min_duration", "--min-duration"),
        ("min_gap", "--min-gap"),
        ("max_gap_merge", "--max-gap-merge"),
    ):
        value = getattr(args, option_name)
        if value is not None:
            translate_args.extend([argument_name, str(value)])
    try:
        code = translate_srt.main(translate_args)
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
        ("skip_beautify", "BEAUTIFY"),
        ("skip_knowledge", "KNOWLEDGE"),
        ("skip_translate", "TRANSLATE"),
        ("skip_burn", "BURN"),
    ):
        setattr(args, attribute, bool(getattr(args, attribute) or _env_skip(config, stage)))


def run_pipeline(args: argparse.Namespace) -> int:
    config = ProjectConfig.load(args.project_dir)
    _apply_env_skip_defaults(args, config)
    if args.dry_run:
        print("[DRY RUN] Python pipeline stages:")
        print("  download -> prepare-video -> whisper -> translate -> burn")
        return 0
    if args.skip_download:
        if not args.video:
            print("Error: --video is required with --skip-download", file=sys.stderr)
            return 2
        download_result = StageResult.ok(render_video=str(Path(args.video).resolve()))
    else:
        if not args.url:
            print("Error: URL is required", file=sys.stderr)
            return 2
        download_result = download_video(args.url, config)
    if not download_result.success:
        print(f"Error: {download_result.diagnostics[0]}", file=sys.stderr)
        emit_bell("error")
        return download_result.exit_code
    render_video = Path(download_result.outputs["render_video"])
    prepare_result = prepare_video(render_video, config)
    if not prepare_result.success:
        print(f"Error: {prepare_result.diagnostics[0]}", file=sys.stderr)
        emit_bell("error")
        return prepare_result.exit_code
    edit_video = Path(prepare_result.outputs["edit_video"])
    if args.skip_whisper:
        if not args.json_path:
            print("Error: --json is required with --skip-whisper", file=sys.stderr)
            return 2
        json_path = Path(args.json_path).expanduser().resolve()
    else:
        whisper_result = transcribe_video(edit_video, config, model=args.model, align_model=args.align_model, device=args.device)
        if not whisper_result.success:
            print(f"Error: {whisper_result.diagnostics[0]}", file=sys.stderr)
            emit_bell("error")
            return whisper_result.exit_code
        json_path = Path(whisper_result.outputs["json"])
    if args.skip_translate:
        if not args.existing_ass:
            print("Error: --existing-ass is required with --skip-translate", file=sys.stderr)
            return 2
        ass_path = Path(args.existing_ass).expanduser().resolve()
    else:
        translate_result = _translate_main(json_path, edit_video, config, args)
        if not translate_result.success:
            print(f"Error: {translate_result.diagnostics[0]}", file=sys.stderr)
            emit_bell("error")
            return translate_result.exit_code
        ass_path = Path(args.output).expanduser().resolve() if args.output else Path(translate_result.outputs["ass"])
    if args.skip_burn:
        emit_bell("success")
        return 0
    burn_result = burn_video(render_video, ass_path, config, output=args.output, ovc=args.ovc, ovcopts=args.ovcopts, oac=args.oac, resolution=args.res)
    if not burn_result.success:
        print(f"Error: {burn_result.diagnostics[0]}", file=sys.stderr)
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
