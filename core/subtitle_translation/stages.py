from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .config import ProjectConfig
from .process import CommandResult, capture_command, child_environment, run_command


@dataclass(frozen=True)
class StageResult:
    success: bool
    exit_code: int = 0
    outputs: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    command: tuple[str, ...] = ()

    @classmethod
    def ok(cls, **outputs: str) -> "StageResult":
        return cls(True, 0, outputs)

    @classmethod
    def fail(cls, exit_code: int, detail: str, command: Sequence[str] = ()) -> "StageResult":
        return cls(False, exit_code or 1, {}, (detail,), tuple(command))


VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".flv", ".avi", ".mov", ".m4v")


def _config_from_args(args: argparse.Namespace) -> ProjectConfig:
    return ProjectConfig.load(getattr(args, "project_dir", None))


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _safe_folder_name(title: str) -> str:
    name = unicodedata.normalize("NFKD", title)
    name = re.sub(r"[\u2018\u2019\u201a\u201b\u2032\u02bc]", "", name)
    name = re.sub(r"[\u201c\u201d\u201e\u201f\u2033]", "", name)
    name = re.sub(r"[\u2010-\u2015]", "-", name)
    name = re.sub(r"[^\w. -]+", "_", name, flags=re.UNICODE)
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"_+", "_", name).strip(" ._")
    return name or "video"


def _run_failed(result: CommandResult, label: str) -> StageResult | None:
    if result.returncode == 0:
        return None
    return StageResult.fail(result.returncode, f"{label} failed with exit code {result.returncode}", result.args)


def download_video(url: str, config: ProjectConfig) -> StageResult:
    ytdlp = config.resolve_tool("YTDLP_PATH_WIN" if os.name == "nt" else "YTDLP_PATH_LINUX", "yt-dlp")
    if not ytdlp:
        return StageResult.fail(127, "yt-dlp command not found")
    title_result = capture_command([ytdlp, "--get-title", url], cwd=config.project_dir)
    if title_result.returncode != 0:
        return StageResult.fail(title_result.returncode, "failed to get video title", title_result.args)
    title_lines = [line.strip() for line in title_result.stdout.splitlines() if line.strip()]
    if not title_lines:
        return StageResult.fail(1, "yt-dlp returned an empty video title", title_result.args)
    folder_name = _safe_folder_name(title_lines[-1])
    folder = config.project_dir / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    existing = folder / f"{folder_name}.original.mkv"
    cookies = config.project_dir / "cookies.txt"
    common = ["-o", str(folder / f"{folder_name}.%(ext)s")]
    if cookies.is_file():
        common += ["--cookies", str(cookies)]
    if existing.is_file():
        ytdlp_args = common + [
            "--skip-download",
            "--write-thumbnail",
            "--convert-thumbnails",
            "png",
            "--write-info-json",
            "--write-description",
            "--no-mtime",
            "--print-to-file",
            "tags",
            str(folder / f"{folder_name}.tags.txt"),
            url,
        ]
        render_video = existing
    else:
        ytdlp_args = common + [
            "--embed-metadata",
            "--embed-thumbnail",
            "--write-thumbnail",
            "--convert-thumbnails",
            "png",
            "--write-info-json",
            "--write-description",
            "--no-mtime",
            "--sponsorblock-remove",
            "sponsor,selfpromo",
            "--print-to-file",
            "tags",
            str(folder / f"{folder_name}.tags.txt"),
            url,
        ]
        render_video = None
    result = run_command([ytdlp, *ytdlp_args], cwd=config.project_dir, label="yt-dlp")
    failed = _run_failed(result, "yt-dlp")
    if failed:
        return failed
    if render_video is None:
        for extension in VIDEO_EXTENSIONS:
            candidate = folder / f"{folder_name}{extension}"
            if candidate.is_file():
                render_video = candidate
                break
        if render_video is None:
            return StageResult.fail(1, f"downloaded video not found in {folder}")
        destination = folder / f"{folder_name}.original{render_video.suffix}"
        if destination.exists():
            destination.unlink()
        render_video.replace(destination)
        render_video = destination
    return StageResult.ok(render_video=str(render_video.resolve()))


def _has_nvidia() -> bool:
    nvidia = shutil.which("nvidia-smi")
    if not nvidia:
        return False
    return capture_command([nvidia, "-L"]).returncode == 0


def _has_encoder(ffmpeg: str, encoder: str) -> bool:
    result = capture_command([ffmpeg, "-hide_banner", "-encoders"])
    return result.returncode == 0 and encoder in result.stdout


def _prepare_args(input_path: Path, output_path: Path, video_args: Sequence[str]) -> list[str]:
    return [
        "-hide_banner",
        "-stats",
        "-i",
        str(input_path),
        "-pix_fmt",
        "yuv420p",
        *video_args,
        "-filter_complex",
        "[0:a]aresample=async=1:out_sample_fmt=s16[aout]",
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-c:a",
        "flac",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        "-y",
        str(output_path),
    ]


def prepare_video(original_video: str | Path, config: ProjectConfig) -> StageResult:
    original = _absolute(original_video)
    if not original.is_file():
        return StageResult.fail(1, f"original video not found: {original}")
    ffmpeg = config.resolve_tool("FFMPEG_PATH_WIN" if os.name == "nt" else "FFMPEG_PATH_LINUX", "ffmpeg")
    if not ffmpeg:
        return StageResult.fail(127, "ffmpeg command not found")
    base = original.stem[:-9] if original.stem.endswith(".original") else original.stem
    output = original.with_name(f"{base}.mkv")
    if output.resolve() == original.resolve():
        return StageResult.fail(1, "prepared video would overwrite the original video")
    temporary = output.with_name(f".{output.stem}.prepare.{uuid.uuid4().hex}.mkv")
    attempts: list[tuple[str, list[str]]] = []
    if _has_nvidia() and _has_encoder(ffmpeg, "h264_nvenc"):
        attempts.append(("h264_nvenc", _prepare_args(original, temporary, ["-c:v", "h264_nvenc", "-cq", "12"])))
    attempts.append(("libx264", _prepare_args(original, temporary, ["-c:v", "libx264", "-crf", "12"])))
    last_code = 1
    try:
        for encoder, command in attempts:
            if temporary.exists():
                temporary.unlink()
            print(f"prepare-video: {encoder}")
            result = run_command([ffmpeg, *command], cwd=original.parent, label="ffmpeg")
            last_code = result.returncode
            if (last_code == 0 and temporary.is_file()) or (
                encoder == "h264_nvenc" and temporary.is_file() and temporary.stat().st_size > 0
            ):
                temporary.replace(output)
                return StageResult.ok(edit_video=str(output.resolve()))
        return StageResult.fail(last_code, "ffmpeg edit-video re-encode failed")
    finally:
        if temporary.exists():
            temporary.unlink()


def extract_audio(video: str | Path, config: ProjectConfig) -> StageResult:
    video_path = _absolute(video)
    if not video_path.is_file():
        return StageResult.fail(1, f"video file not found: {video_path}")
    ffmpeg = config.resolve_tool("FFMPEG_PATH_WIN" if os.name == "nt" else "FFMPEG_PATH_LINUX", "ffmpeg")
    if not ffmpeg:
        return StageResult.fail(127, "ffmpeg command not found")
    wav_path = video_path.with_suffix(".wav")
    command = [
        ffmpeg,
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(wav_path),
        "-y",
        "-loglevel",
        "error",
    ]
    result = run_command(command, cwd=video_path.parent, label="ffmpeg audio")
    failed = _run_failed(result, "audio extraction")
    if failed:
        return failed
    if not wav_path.is_file() or wav_path.stat().st_size == 0:
        return StageResult.fail(1, f"audio extraction did not create {wav_path}", result.args)
    return StageResult.ok(wav=str(wav_path.resolve()))


def _whisperx_path(config: ProjectConfig) -> str | None:
    key = "WHISPERX_PATH_WIN" if os.name == "nt" else "WHISPERX_PATH_LINUX"
    configured = config.get(key, "").strip()
    if configured:
        return str(_absolute(configured)) if Path(configured).is_file() else shutil.which(configured)
    candidates = [
        config.project_dir / (".venv/Scripts/whisperx.exe" if os.name == "nt" else ".venv/bin/whisperx"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("whisperx")


def transcribe_video(video: str | Path, config: ProjectConfig, *, model: str = "", align_model: str = "", device: str = "") -> StageResult:
    video_path = _absolute(video)
    if not video_path.is_file():
        return StageResult.fail(1, f"video file not found: {video_path}")
    whisperx = _whisperx_path(config)
    if not whisperx:
        return StageResult.fail(127, "WhisperX executable not found; run setup first")
    json_path = video_path.with_suffix(".json")
    if json_path.is_file():
        return StageResult.ok(json=str(json_path))
    ffmpeg = config.resolve_tool("FFMPEG_PATH_WIN" if os.name == "nt" else "FFMPEG_PATH_LINUX", "ffmpeg")
    if not ffmpeg:
        return StageResult.fail(127, "ffmpeg command not found")
    model = model or config.get("WHISPER_MODEL", "large-v3-turbo")
    align_model = align_model or config.get("WHISPER_ALIGN_MODEL", "")
    device = device or config.get("WHISPER_DEVICE", "")
    if not device:
        device = "cuda" if config.get("TORCH_BACKEND", "auto") == "cuda128" or _has_nvidia() else "cpu"
    compute_type = "float16" if device == "cuda" else "float32"
    wav_path = video_path.with_suffix(".wav")
    language = "en"
    info_path = video_path.with_suffix(".info.json")
    if info_path.is_file():
        try:
            language = str(
                json.loads(info_path.read_text(encoding="utf-8")).get("language", "en")
            ).split("-")[0].lower()
        except (OSError, json.JSONDecodeError):
            language = "en"
    ffmpeg_result = run_command(
        [ffmpeg, "-i", str(video_path), "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(wav_path), "-y", "-loglevel", "error"],
        cwd=video_path.parent,
        label="ffmpeg audio",
    )
    failed = _run_failed(ffmpeg_result, "audio extraction")
    if failed:
        return failed
    env = child_environment(config.values)
    env["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
    token = config.get("HF_TOKEN", "") or config.get("HUGGING_FACE_HUB_TOKEN", "")
    if token:
        env["HF_TOKEN"] = token
        env["HUGGING_FACE_HUB_TOKEN"] = token
    command = [
        whisperx,
        str(wav_path),
        "--model",
        model,
        "--language",
        language,
        "--output_dir",
        str(video_path.parent),
        "--output_format",
        "json",
        "--device",
        device,
        "--batch_size",
        "8",
        "--compute_type",
        compute_type,
    ]
    if align_model:
        command += ["--align_model", align_model]
    result = run_command(command, cwd=video_path.parent, env=env, label="whisperx")
    try:
        if wav_path.exists():
            wav_path.unlink()
    except OSError:
        pass
    failed = _run_failed(result, "whisperx")
    if failed:
        return failed
    if not json_path.is_file():
        return StageResult.fail(1, f"WhisperX did not create {json_path}")
    return StageResult.ok(json=str(json_path.resolve()))


def _parser_with_project(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--project-dir", default=None)
    return parser


def main_download(argv: Sequence[str]) -> int:
    parser = _parser_with_project("Download a YouTube video and metadata")
    parser.add_argument("url")
    args = parser.parse_args(argv)
    result = download_video(args.url, _config_from_args(args))
    if result.success:
        print(f"OUTPUT_RENDER_VIDEO={result.outputs['render_video']}")
    else:
        print(f"Error: {result.diagnostics[0]}", file=sys.stderr)
    return result.exit_code


def main_prepare_video(argv: Sequence[str]) -> int:
    parser = _parser_with_project("Prepare an edit video")
    parser.add_argument("original_video")
    args = parser.parse_args(argv)
    result = prepare_video(args.original_video, _config_from_args(args))
    if result.success:
        print(f"OUTPUT_VIDEO={result.outputs['edit_video']}")
    else:
        print(f"Error: {result.diagnostics[0]}", file=sys.stderr)
    return result.exit_code


def main_whisper(argv: Sequence[str]) -> int:
    parser = _parser_with_project("Run WhisperX")
    parser.add_argument("video")
    parser.add_argument("--model", default="")
    parser.add_argument("--align-model", default="")
    parser.add_argument("--device", default="")
    args = parser.parse_args(argv)
    result = transcribe_video(args.video, _config_from_args(args), model=args.model, align_model=args.align_model, device=args.device)
    if result.success:
        print(f"OUTPUT_JSON={result.outputs['json']}")
    else:
        print(f"Error: {result.diagnostics[0]}", file=sys.stderr)
    return result.exit_code


def main_burn(argv: Sequence[str]) -> int:
    parser = _parser_with_project("Burn an ASS subtitle into a video")
    parser.add_argument("video")
    parser.add_argument("subtitle", nargs="?")
    parser.add_argument("-s", "--sub-file", "-SubFile", dest="subtitle_option", default="")
    parser.add_argument("-o", "--output", "-Output", default="")
    parser.add_argument("--ovc", "-Ovc", default="")
    parser.add_argument("--ovcopts", "-Ovcopts", default="")
    parser.add_argument("--oac", "-Oac", default="")
    parser.add_argument("--res", "-Res", default="")
    parser.add_argument("--backend", choices=("ffmpeg", "mpv"), default="ffmpeg")
    parser.add_argument("--dry-run", "-DryRun", action="store_true")
    parser.add_argument("extra_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    subtitle = args.subtitle or args.subtitle_option
    if not subtitle:
        parser.error("a subtitle path is required")
    result = burn_video(args.video, subtitle, _config_from_args(args), output=args.output, ovc=args.ovc, ovcopts=args.ovcopts, oac=args.oac, resolution=args.res, backend=args.backend, extra_args=args.extra_args, dry_run=args.dry_run)
    if result.success and result.outputs.get("burned_video"):
        print(f"OUTPUT_BURNED_VIDEO={result.outputs['burned_video']}")
    elif not result.success:
        print(f"Error: {result.diagnostics[0]}", file=sys.stderr)
    return result.exit_code


def _ffprobe_path(ffmpeg: str) -> str:
    candidate = Path(ffmpeg)
    if candidate.is_file():
        sibling = candidate.with_name("ffprobe" + candidate.suffix)
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


def _source_bitrate_kbps(video: Path, ffprobe: str) -> int:
    for arguments in (
        ["-v", "error", "-select_streams", "v:0", "-show_entries", "stream=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        ["-v", "error", "-show_entries", "format=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
    ):
        result = capture_command([ffprobe, *arguments])
        try:
            value = int(float(result.stdout.strip().splitlines()[0]))
        except (IndexError, ValueError):
            value = 0
        if value > 0:
            return max(1, (value + 999) // 1000)
    duration_result = capture_command([
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ])
    try:
        duration = float(duration_result.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        duration = 0.0
    if duration > 0:
        return max(1, int((video.stat().st_size * 8 / duration) / 1000 + 0.999))
    return 0


def _encode_options(value: str) -> list[str]:
    result: list[str] = []
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            result.extend(["-qp", item])
            continue
        key, option_value = item.split("=", 1)
        result.extend(["-b:v" if key in {"b", "b:v"} else f"-{key}", option_value])
    return result


def burn_video(
    video: str | Path,
    subtitle: str | Path,
    config: ProjectConfig,
    *,
    output: str = "",
    ovc: str = "",
    ovcopts: str = "",
    oac: str = "",
    resolution: str = "",
    backend: str = "ffmpeg",
    extra_args: Sequence[str] = (),
    dry_run: bool = False,
) -> StageResult:
    video_path = _absolute(video)
    subtitle_path = _absolute(subtitle)
    if not video_path.is_file():
        return StageResult.fail(1, f"video file not found: {video_path}")
    if not subtitle_path.is_file():
        return StageResult.fail(1, f"subtitle file not found: {subtitle_path}")
    normalized_backend = backend.strip().lower() or "ffmpeg"
    if normalized_backend not in {"ffmpeg", "mpv"}:
        return StageResult.fail(2, f"unsupported burn backend: {backend}")
    tool_key = (
        ("MPV_PATH_WIN" if os.name == "nt" else "MPV_PATH_LINUX")
        if normalized_backend == "mpv"
        else ("FFMPEG_PATH_WIN" if os.name == "nt" else "FFMPEG_PATH_LINUX")
    )
    tool = config.resolve_tool(tool_key, normalized_backend)
    if not tool:
        return StageResult.fail(127, f"{normalized_backend} command not found")
    output_path = _absolute(output) if output else video_path.parent / "burned.mkv"
    ovc = ovc or config.get("BURN_OVC", "hevc_nvenc")
    ovcopts = ovcopts or config.get("BURN_OVCOPTS", "source-bitrate")
    oac = oac or config.get("BURN_OAC", "aac")
    if ovcopts.strip().lower() in {"source", "source-bitrate", "source_bitrate", "match-source", "auto"}:
        bitrate = _source_bitrate_kbps(video_path, _ffprobe_path(tool))
        if bitrate:
            prefix = "rc=vbr," if "nvenc" in ovc.lower() else ""
            ovcopts = f"{prefix}b={bitrate}k,maxrate={int(bitrate * 1.25)}k,bufsize={bitrate * 2}k"
        else:
            ovcopts = "qp=20"
    if normalized_backend == "mpv":
        command = [
            tool,
            str(video_path),
            f"--o={output_path}",
            f"--ovc={ovc}",
            f"--ovcopts={ovcopts}",
            f"--oac={oac}",
            f"--sub-file={subtitle_path}",
        ]
        if resolution:
            parts = resolution.lower().split("x", 1)
            if len(parts) == 2 and all(parts):
                width, height = parts
                command.append(
                    "--vf-add=lavfi="
                    f"[scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2]"
                )
    else:
        escaped_subtitle = str(subtitle_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        video_filter = f"ass='{escaped_subtitle}'"
        if resolution:
            parts = resolution.lower().split("x", 1)
            if len(parts) == 2 and all(parts):
                width, height = parts
                video_filter += f",scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        command = [
            tool,
            "-i",
            str(video_path),
            "-vf",
            video_filter,
            "-c:v",
            ovc,
            *_encode_options(ovcopts),
            "-c:a",
            oac,
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-map",
            "0:v:1?",
            "-map_metadata",
            "0",
            "-disposition:v:1",
            "attached_pic",
            "-movflags",
            "+faststart",
            "-y",
            str(output_path),
        ]
    command.extend(str(arg) for arg in extra_args)
    if dry_run:
        print("[DRY RUN] " + " ".join(command))
        return StageResult.ok()
    result = run_command(command, cwd=video_path.parent, label=f"{normalized_backend} burn")
    if result.returncode != 0:
        return StageResult.fail(result.returncode, f"{normalized_backend} subtitle burn failed", result.args)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        return StageResult.fail(1, f"ffmpeg did not create a non-empty output: {output_path}", result.args)
    return StageResult.ok(burned_video=str(output_path.resolve()))
