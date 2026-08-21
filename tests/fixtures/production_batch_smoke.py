from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unittest


PRODUCTION_MODULES = (
    "batch.py",
    "batch_cache.py",
    "batch_scheduler.py",
    "whisper_worker.py",
)
URLS = tuple(f"video-{index}" for index in range(1, 7))
PYTHON_RUNTIME_PROBE = (
    "import langcodes, os, platform, sys; "
    "version=sys.version_info[:2]; "
    "sys.exit(f'unsupported Python {platform.python_version()}') "
    "if not ((3, 10) <= version < (3, 14)) else None; "
    "print(max(1, (os.cpu_count() or 1)//4)); "
    "print(sys.executable); "
    "print(platform.python_version()); "
    "print(langcodes.__version__ if hasattr(langcodes, '__version__') else 'ok')"
)


WINDOWS_DOWNLOAD = r'''param([string]$Url)
$workspace = $env:FAKE_BATCH_WORKSPACE
$mediaDir = Join-Path $workspace $Url
New-Item -ItemType Directory -Force -Path $mediaDir | Out-Null
$original = Join-Path $mediaDir "$Url.original.mp4"
[IO.File]::WriteAllBytes($original, [byte[]](1, 2, 3))
$number = [int]($Url -replace '^video-', '')
$language = if (($number % 2) -eq 1) { 'en-US' } else { 'ja-JP' }
@{ language = $language } | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $mediaDir "$Url.info.json") -Encoding utf8
Add-Content -LiteralPath (Join-Path $mediaDir 'stage.log') -Value 'download' -Encoding utf8
[Console]::Out.WriteLine("OUTPUT_RENDER_VIDEO=$([IO.Path]::GetFullPath($original))")
exit 0
'''


WINDOWS_PREPARE = r'''param([string]$RenderVideo)
function Get-WallNs {
    return ([DateTime]::UtcNow.Ticks - [DateTime]::UnixEpoch.Ticks) * 100
}
$name = [IO.Path]::GetFileName($RenderVideo) -replace '\.original\.mp4$', ''
$mediaDir = [IO.Path]::GetDirectoryName($RenderVideo)
$metrics = $env:FAKE_BATCH_METRICS
$start = Get-WallNs
[IO.File]::WriteAllText((Join-Path $metrics "$name.prepare.start"), $start.ToString())
[IO.File]::WriteAllText((Join-Path $metrics "$name.prepare.ready"), '1')
$deadline = [DateTime]::UtcNow.AddSeconds(10)
while (@(Get-ChildItem -LiteralPath $metrics -Filter '*.prepare.ready').Count -lt 4) {
    if ([DateTime]::UtcNow -gt $deadline) { exit 91 }
    Start-Sleep -Milliseconds 10
}
Add-Content -LiteralPath (Join-Path $mediaDir 'stage.log') -Value 'prepare' -Encoding utf8
Start-Sleep -Milliseconds 120
$editVideo = Join-Path $mediaDir "$name.mkv"
[IO.File]::WriteAllBytes($editVideo, [byte[]](4, 5, 6))
[IO.File]::WriteAllText((Join-Path $metrics "$name.prepare.end"), (Get-WallNs).ToString())
[Console]::Out.WriteLine("OUTPUT_VIDEO=$([IO.Path]::GetFullPath($editVideo))")
exit 0
'''


WINDOWS_TRANSLATE = r'''param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
$inputPath = $Arguments[0]
$videoIndex = [Array]::IndexOf($Arguments, '--video')
if ($videoIndex -lt 0) { exit 92 }
$editVideo = $Arguments[$videoIndex + 1]
$name = [IO.Path]::GetFileNameWithoutExtension($editVideo)
$mediaDir = [IO.Path]::GetDirectoryName($editVideo)
$stageLog = Join-Path $mediaDir 'stage.log'
if ($Arguments -contains '--only-beautify') {
    Add-Content -LiteralPath $stageLog -Value 'beautify' -Encoding utf8
    $beautified = Join-Path ([IO.Path]::GetDirectoryName($inputPath)) "$([IO.Path]::GetFileNameWithoutExtension($inputPath)).beautified.json"
    Copy-Item -LiteralPath $inputPath -Destination $beautified -Force
    exit 0
}
if ($Arguments -contains '--only-glossary') {
    Add-Content -LiteralPath $stageLog -Value 'glossary' -Encoding utf8
    [IO.File]::WriteAllText((Join-Path $mediaDir 'glossary.md'), '# fake glossary')
    exit 0
}
Add-Content -LiteralPath $stageLog -Value 'translate' -Encoding utf8
$payload = Get-Content -LiteralPath $inputPath -Raw | ConvertFrom-Json
$language = [string]$payload.language
$assPath = Join-Path $mediaDir "$name.$language-zh.ass"
[IO.File]::WriteAllText($assPath, 'fake ass')
exit 0
'''


WINDOWS_BURN = r'''param([string]$VideoPath, [string]$SubFile)
function Get-WallNs {
    return ([DateTime]::UtcNow.Ticks - [DateTime]::UnixEpoch.Ticks) * 100
}
if (-not (Test-Path -LiteralPath $SubFile -PathType Leaf)) { exit 93 }
$name = [IO.Path]::GetFileName($VideoPath) -replace '\.original\.mp4$', ''
$mediaDir = [IO.Path]::GetDirectoryName($VideoPath)
$metrics = $env:FAKE_BATCH_METRICS
[IO.File]::WriteAllText((Join-Path $metrics "$name.burn.start"), (Get-WallNs).ToString())
[IO.File]::WriteAllText((Join-Path $metrics "$name.burn.ready"), '1')
$deadline = [DateTime]::UtcNow.AddSeconds(10)
while (@(Get-ChildItem -LiteralPath $metrics -Filter '*.burn.ready').Count -lt 4) {
    if ([DateTime]::UtcNow -gt $deadline) { exit 94 }
    Start-Sleep -Milliseconds 10
}
Add-Content -LiteralPath (Join-Path $mediaDir 'stage.log') -Value 'burn' -Encoding utf8
Start-Sleep -Milliseconds 120
$output = Join-Path $mediaDir 'burned.mkv'
[IO.File]::WriteAllBytes($output, [byte[]](7, 8, 9))
[IO.File]::WriteAllText((Join-Path $metrics "$name.burn.end"), (Get-WallNs).ToString())
[Console]::Out.WriteLine("OUTPUT_BURNED_VIDEO=$([IO.Path]::GetFullPath($output))")
exit 0
'''


WINDOWS_FFMPEG = r'''@echo off
"%PYTHON_PATH_WIN%" "%~dp0fake_ffmpeg.py" %*
exit /b %ERRORLEVEL%
'''


BASH_DOWNLOAD = r'''#!/usr/bin/env bash
set -euo pipefail
url="$1"
media_dir="$FAKE_BATCH_WORKSPACE/$url"
mkdir -p "$media_dir"
original="$media_dir/$url.original.mp4"
printf original > "$original"
number="${url#video-}"
if (( number % 2 == 1 )); then language=en-US; else language=ja-JP; fi
printf '{"language":"%s"}\n' "$language" > "$media_dir/$url.info.json"
printf 'download\n' >> "$media_dir/stage.log"
printf 'OUTPUT_RENDER_VIDEO=%s\n' "$original"
'''


BASH_PREPARE = r'''#!/usr/bin/env bash
set -euo pipefail
render_video="$1"
media_dir="$(dirname "$render_video")"
filename="$(basename "$render_video")"
name="${filename%.original.mp4}"
date +%s%N > "$FAKE_BATCH_METRICS/$name.prepare.start"
: > "$FAKE_BATCH_METRICS/$name.prepare.ready"
attempt=0
while [ "$(find "$FAKE_BATCH_METRICS" -maxdepth 1 -type f -name '*.prepare.ready' | wc -l)" -lt 4 ]; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 1000 ] || exit 91
    sleep 0.01
done
printf 'prepare\n' >> "$media_dir/stage.log"
sleep 0.12
edit_video="$media_dir/$name.mkv"
printf edit > "$edit_video"
date +%s%N > "$FAKE_BATCH_METRICS/$name.prepare.end"
printf 'OUTPUT_VIDEO=%s\n' "$edit_video"
'''


BASH_TRANSLATE = r'''#!/usr/bin/env bash
set -euo pipefail
input_path="$1"
shift
edit_video=""
mode=translate
while [ "$#" -gt 0 ]; do
    case "$1" in
        --video) edit_video="$2"; shift 2 ;;
        --only-beautify) mode=beautify; shift ;;
        --only-glossary) mode=glossary; shift ;;
        *) shift ;;
    esac
done
[ -n "$edit_video" ] || exit 92
media_dir="$(dirname "$edit_video")"
name="$(basename "${edit_video%.*}")"
case "$mode" in
    beautify)
        printf 'beautify\n' >> "$media_dir/stage.log"
        cp "$input_path" "${input_path%.json}.beautified.json"
        ;;
    glossary)
        printf 'glossary\n' >> "$media_dir/stage.log"
        printf '# fake glossary\n' > "$media_dir/glossary.md"
        ;;
    translate)
        printf 'translate\n' >> "$media_dir/stage.log"
        language="$(sed -n 's/.*"language"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$input_path" | head -n 1)"
        [ -n "$language" ] || exit 93
        printf 'fake ass\n' > "$media_dir/$name.$language-zh.ass"
        ;;
esac
'''


BASH_BURN = r'''#!/usr/bin/env bash
set -euo pipefail
video_path="$1"
shift
sub_file=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --sub-file) sub_file="$2"; shift 2 ;;
        *) shift ;;
    esac
done
[ -s "$sub_file" ] || exit 94
media_dir="$(dirname "$video_path")"
filename="$(basename "$video_path")"
name="${filename%.original.mp4}"
date +%s%N > "$FAKE_BATCH_METRICS/$name.burn.start"
: > "$FAKE_BATCH_METRICS/$name.burn.ready"
attempt=0
while [ "$(find "$FAKE_BATCH_METRICS" -maxdepth 1 -type f -name '*.burn.ready' | wc -l)" -lt 4 ]; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 1000 ] || exit 95
    sleep 0.01
done
printf 'burn\n' >> "$media_dir/stage.log"
sleep 0.12
output="$media_dir/burned.mkv"
printf burned > "$output"
date +%s%N > "$FAKE_BATCH_METRICS/$name.burn.end"
printf 'OUTPUT_BURNED_VIDEO=%s\n' "$output"
'''


BASH_FFMPEG = r'''#!/usr/bin/env bash
set -euo pipefail
exec "${PYTHON_PATH_LINUX:-python3}" "$(dirname "$0")/fake_ffmpeg.py" "$@"
'''


FAKE_FFMPEG_PY = r'''import os
from pathlib import Path
import sys
import time


arguments = sys.argv[1:]
input_path = Path(arguments[arguments.index("-i") + 1])
output_path = Path(arguments[arguments.index("-y") - 1])
metrics = Path(os.environ["FAKE_BATCH_METRICS"])
name = input_path.stem
(metrics / f"{name}.audio.start").write_text(str(time.time_ns()))
(metrics / f"{name}.audio.ready").touch()
barrier = int(os.environ["FAKE_BATCH_AUDIO_BARRIER"])
deadline = time.monotonic() + 10
while len(list(metrics.glob("*.audio.ready"))) < barrier:
    if time.monotonic() >= deadline:
        raise SystemExit(96)
    time.sleep(0.01)
time.sleep(0.08)
output_path.write_bytes(b"wav")
with (input_path.parent / "stage.log").open("a", encoding="utf-8") as log:
    log.write("extract_audio\n")
(metrics / f"{name}.audio.end").write_text(str(time.time_ns()))
'''


FAKE_WHISPERX = r'''import json
import multiprocessing
from multiprocessing.util import Finalize
import os
from pathlib import Path
import time


def _record(event, language="", name=""):
    payload = {
        "event": event,
        "language": language,
        "name": name,
        "pid": os.getpid(),
        "process": multiprocessing.current_process().name,
        "wall_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
    }
    with open(os.environ["FAKE_WHISPERX_EVENTS"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True) + "\n")


_shutdown_finalizer = Finalize(
    None,
    _record,
    args=("worker_shutdown",),
    exitpriority=0,
)


def _stage(path, stage):
    media_path = Path(path)
    with (media_path.parent / "stage.log").open("a", encoding="utf-8") as log:
        log.write(stage + "\n")


class _AsrModel:
    def __init__(self):
        self._unloaded = False

    def __del__(self):
        if not self._unloaded:
            self._unloaded = True
            _record("unload_asr")

    def transcribe(self, wav_path, batch_size, language):
        del batch_size
        wav = Path(wav_path)
        name = wav.stem
        _stage(wav, "asr")
        _record("transcribe_start", language, name)
        time.sleep(0.04)
        _record("transcribe_end", language, name)
        return {
            "language": language,
            "segments": [{"start": 0.0, "end": 1.0, "text": name}],
        }


def load_model(model, device, compute_type, asr_options, language, use_auth_token):
    del model, device, compute_type, asr_options, language, use_auth_token
    _record("load_asr")
    return _AsrModel()


def load_align_model(language_code, device, model_name=None):
    del device, model_name
    _record("load_align", language_code)
    return _AlignModel(language_code), {"language": language_code}


class _AlignModel:
    def __init__(self, language):
        self.language = language
        self._unloaded = False

    def __del__(self):
        if not self._unloaded:
            self._unloaded = True
            _record("unload_align", self.language)


def load_audio(path):
    return path


def align(segments, model, metadata, audio, device, return_char_alignments):
    del model, device, return_char_alignments
    language = metadata["language"]
    wav = Path(audio)
    name = wav.stem
    _stage(wav, "alignment")
    _record("align_start", language, name)
    time.sleep(0.04)
    _record("align_end", language, name)
    return {
        "segments": [
            {
                **segment,
                "words": [{
                    "word": segment["text"],
                    "start": segment["start"],
                    "end": segment["end"],
                }],
            }
            for segment in segments
        ]
    }
'''


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _copy_production(root, sandbox, wrappers):
    names = (*PRODUCTION_MODULES, *wrappers)
    source_hashes = {}
    copied_hashes = {}
    for name in names:
        source = root / name
        destination = sandbox / name
        shutil.copy2(source, destination)
        source_hashes[name] = _sha256(source)
        copied_hashes[name] = _sha256(destination)
    if source_hashes != copied_hashes:
        raise AssertionError("production smoke copied non-identical modules")
    return source_hashes, copied_hashes


def _write(path, content, executable=False):
    path.write_text(content, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(0o755)


def _write_fake_whisperx(sandbox):
    fake_site = sandbox / "fake_site"
    package = fake_site / "whisperx"
    package.mkdir(parents=True)
    _write(package / "__init__.py", FAKE_WHISPERX)
    return fake_site


def _write_windows_boundaries(sandbox):
    _write(sandbox / "download.ps1", WINDOWS_DOWNLOAD)
    _write(sandbox / "prepare-video.ps1", WINDOWS_PREPARE)
    _write(sandbox / "translate_srt.ps1", WINDOWS_TRANSLATE)
    _write(sandbox / "ffmpeg-burn.ps1", WINDOWS_BURN)
    _write(sandbox / "fake-ffmpeg.cmd", WINDOWS_FFMPEG)
    _write(sandbox / "fake_ffmpeg.py", FAKE_FFMPEG_PY)
    return sandbox / "fake-ffmpeg.cmd"


def _write_bash_boundaries(sandbox):
    _write(sandbox / "download.sh", BASH_DOWNLOAD, executable=True)
    _write(sandbox / "prepare-video.sh", BASH_PREPARE, executable=True)
    _write(sandbox / "translate_srt.sh", BASH_TRANSLATE, executable=True)
    _write(sandbox / "ffmpeg-burn.sh", BASH_BURN, executable=True)
    _write(sandbox / "fake-ffmpeg.sh", BASH_FFMPEG, executable=True)
    _write(sandbox / "fake_ffmpeg.py", FAKE_FFMPEG_PY)
    return sandbox / "fake-ffmpeg.sh"


def _peak(intervals):
    points = []
    for interval in intervals:
        points.append((interval["start_ns"], 1))
        points.append((interval["end_ns"], -1))
    active = 0
    peak = 0
    for _timestamp, change in sorted(points, key=lambda item: (item[0], item[1])):
        active += change
        peak = max(peak, active)
    return peak


def _read_intervals(metrics, stage):
    intervals = []
    for name in URLS:
        start = int((metrics / f"{name}.{stage}.start").read_text().strip())
        end = int((metrics / f"{name}.{stage}.end").read_text().strip())
        intervals.append({"name": name, "start_ns": start, "end_ns": end})
    return intervals


def _event_intervals(events, stage):
    starts = {}
    intervals = []
    for event in events:
        key = (event["language"], event["name"])
        if event["event"] == f"{stage}_start":
            starts[key] = event["wall_ns"]
        elif event["event"] == f"{stage}_end":
            intervals.append(
                {
                    "language": event["language"],
                    "name": event["name"],
                    "start_ns": starts.pop(key),
                    "end_ns": event["wall_ns"],
                }
            )
    if starts:
        raise AssertionError(f"unclosed {stage} events: {starts}")
    return intervals


def _parse_capacity(stdout, label):
    match = re.search(rf"^{re.escape(label)}:\s+(\d+)\s*$", stdout, re.MULTILINE)
    if match is None:
        raise AssertionError(f"missing capacity output {label}: {stdout}")
    return int(match.group(1))


def _collect_evidence(
    *,
    platform,
    wrapper_chain,
    result,
    source_hashes,
    copied_hashes,
    workspace,
    metrics,
    events_path,
    report_path,
    expected_cpu_io,
    python_executable,
    python_resolved_executable,
    python_version,
    python_environment,
):
    if result.returncode != 0:
        raise AssertionError(
            f"production smoke failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    report = report_path.read_text(encoding="utf-8")
    machine_report_path = report_path.with_suffix(".json")
    machine_report = json.loads(machine_report_path.read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    prepare_intervals = _read_intervals(metrics, "prepare")
    audio_intervals = _read_intervals(metrics, "audio")
    burn_intervals = _read_intervals(metrics, "burn")
    asr_intervals = _event_intervals(events, "transcribe")
    align_intervals = _event_intervals(events, "align")
    outputs = [workspace / name / "burned.mkv" for name in URLS]
    task_stages = {
        name: (workspace / name / "stage.log").read_text(
            encoding="utf-8-sig"
        ).splitlines()
        for name in URLS
    }
    align_loads = Counter(
        event["language"] for event in events if event["event"] == "load_align"
    )
    align_calls = Counter(
        event["language"] for event in events if event["event"] == "align_start"
    )
    worker_process_names = sorted({event["process"] for event in events})
    summary_count = len(
        re.findall(r"^\[\d+/6\] OK stage=burned", result.stdout, re.MULTILINE)
    )
    return {
        "platform": platform,
        "wrapper_chain": wrapper_chain,
        "scope": "production batch CLI with fake external boundaries",
        "exit_code": result.returncode,
        "production_hashes": source_hashes,
        "copied_hashes": copied_hashes,
        "argparse_main_exercised": (
            "batch - 6 videos, automatic stage capacities" in result.stdout
            and summary_count == 6
            and report_path.is_file()
        ),
        "observed_cpu_io": _parse_capacity(result.stdout, "CPU/IO"),
        "expected_cpu_io": expected_cpu_io,
        "python_environment_verified": True,
        "python_environment": python_environment,
        "python_executable": python_executable,
        "python_resolved_executable": python_resolved_executable,
        "python_version": python_version,
        "observed_nvenc": _parse_capacity(result.stdout, "NVENC"),
        "task_count": summary_count,
        "report_success_count": report.count("[OK]"),
        "report_failure_count": report.count("[FAIL]"),
        "machine_report": machine_report,
        "aggregate_notification_bells": result.stderr.count("\a"),
        "peak_prepare_nvenc": _peak(prepare_intervals),
        "peak_burn_nvenc": _peak(burn_intervals),
        "peak_combined_nvenc": _peak(prepare_intervals + burn_intervals),
        "asr_load_count": sum(event["event"] == "load_asr" for event in events),
        "align_loads": dict(sorted(align_loads.items())),
        "align_calls": dict(sorted(align_calls.items())),
        "worker_process_names": worker_process_names,
        "prepare_intervals": prepare_intervals,
        "audio_intervals": audio_intervals,
        "asr_intervals": asr_intervals,
        "align_intervals": align_intervals,
        "burn_intervals": burn_intervals,
        "worker_events": events,
        "worker_command_sequence": [
            (event["event"], event["language"], event["name"])
            for event in events
        ],
        "task_stages": task_stages,
        "relative_output_paths": [
            output.relative_to(workspace).as_posix() for output in outputs
        ],
        "output_files_nonempty": all(
            output.is_file() and output.stat().st_size > 0 for output in outputs
        ),
        "recovery_sidecars_remaining": len(list(workspace.rglob("*.asr.json"))),
        "persistent_lock_files": len(list(workspace.rglob("*.asr.lock"))),
        "prepare_state_files": len(list(workspace.rglob("*.prepare.json"))),
        "stdout": result.stdout,
        "stderr": result.stderr,
        "report": report,
    }


def _base_environment(
    workspace,
    metrics,
    events_path,
    fake_site,
    *,
    cpu_io_capacity,
):
    env = os.environ.copy()
    env.update(
        {
            "FAKE_BATCH_WORKSPACE": str(workspace),
            "FAKE_BATCH_METRICS": str(metrics),
            "FAKE_WHISPERX_EVENTS": str(events_path),
            "FAKE_BATCH_AUDIO_BARRIER": str(min(len(URLS), cpu_io_capacity)),
            "PYTHONPATH": os.pathsep.join(
                filter(None, (str(fake_site), env.get("PYTHONPATH", "")))
            ),
            "TORCH_BACKEND": "cpu",
            "WHISPER_DEVICE": "cpu",
            "WHISPER_MODEL": "fake-model",
            "TARGET_LANG": "zh",
        }
    )
    return env


def _validated_probe_lines(result):
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 4:
        return None
    try:
        int(lines[0])
        version = tuple(int(part) for part in lines[2].split(".")[:2])
    except ValueError:
        return None
    if not ((3, 10) <= version < (3, 14)):
        return None
    return lines


def run_windows(*, root, sandbox, python_executable, powershell):
    source_hashes, copied_hashes = _copy_production(
        root,
        sandbox,
        ("batch.ps1", "py_launcher.ps1"),
    )
    workspace = sandbox / "work"
    metrics = sandbox / "metrics"
    workspace.mkdir()
    metrics.mkdir()
    events_path = sandbox / "whisperx-events.jsonl"
    events_path.touch()
    report_path = sandbox / "batch-result.txt"
    fake_site = _write_fake_whisperx(sandbox)
    fake_ffmpeg = _write_windows_boundaries(sandbox)
    expected_cpu_io = max(1, (os.cpu_count() or 1) // 4)
    env = _base_environment(
        workspace,
        metrics,
        events_path,
        fake_site,
        cpu_io_capacity=expected_cpu_io,
    )
    env["PYTHON_PATH_WIN"] = str(python_executable.resolve())
    env["FFMPEG_PATH_WIN"] = str(fake_ffmpeg.resolve())
    probe = subprocess.run(
        [
            str(python_executable.resolve()),
            "-c",
            PYTHON_RUNTIME_PROBE,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    probe_lines = _validated_probe_lines(probe)
    if probe_lines is None:
        raise AssertionError(
            "Windows smoke Python must be >=3.10,<3.14 and import langcodes: "
            f"{python_executable.resolve()}\n{probe.stderr}"
        )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(sandbox / "batch.ps1"),
            "--report",
            str(report_path),
            *URLS,
        ],
        cwd=sandbox,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    return _collect_evidence(
        platform="windows-powershell",
        wrapper_chain="batch.ps1 -> py_launcher.ps1 -> batch.py",
        result=result,
        source_hashes=source_hashes,
        copied_hashes=copied_hashes,
        workspace=workspace,
        metrics=metrics,
        events_path=events_path,
        report_path=report_path,
        expected_cpu_io=expected_cpu_io,
        python_executable=str(python_executable.resolve()),
        python_resolved_executable=probe_lines[1],
        python_version=probe_lines[2],
        python_environment=(
            f"{probe_lines[1]} Python={probe_lines[2]} langcodes={probe_lines[3]}"
        ),
    )


def _wsl_path(wsl, path):
    forward_path = str(Path(path).resolve()).replace("\\", "/")
    result = subprocess.run(
        [wsl, "-u", "root", "--", "wslpath", "-a", forward_path],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    return result.stdout.strip()


def _handle_wsl_unavailable(reason, *, environ=None):
    values = os.environ if environ is None else environ
    if values.get("BATCH_SMOKE_REQUIRE_WSL", "").strip() == "1":
        raise AssertionError(
            "BATCH_SMOKE_REQUIRE_WSL=1 requires the WSL production smoke to run: "
            f"{reason}"
        )
    raise unittest.SkipTest(reason)


def _verify_wsl_root(
    *,
    wsl,
    environ=None,
    runner=subprocess.run,
):
    if not wsl:
        _handle_wsl_unavailable(
            "WSL executable is unavailable; install/enable WSL and verify "
            "`wsl -u root -- true`",
            environ=environ,
        )
    try:
        result = runner(
            [wsl, "-u", "root", "--", "true"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except OSError as exc:
        _handle_wsl_unavailable(
            f"WSL root is unavailable: {exc}; verify `wsl -u root -- true`",
            environ=environ,
        )
    if result.returncode != 0:
        diagnostic = result.stderr.strip() or result.stdout.strip() or "unknown error"
        _handle_wsl_unavailable(
            "WSL root is unavailable; verify `wsl -u root -- true`: "
            f"{diagnostic}",
            environ=environ,
        )


def _resolve_wsl_python(
    *,
    wsl,
    repo_path,
    environ=None,
    runner=subprocess.run,
):
    values = os.environ if environ is None else environ
    checked = []
    candidates = [
        values.get("BATCH_SMOKE_WSL_PYTHON", "").strip(),
        f"{repo_path.rstrip('/')}/.venv/bin/python",
    ]

    def probe(candidate):
        if not candidate or candidate in checked:
            return None
        checked.append(candidate)
        result = runner(
            [wsl, "-u", "root", "--", candidate, "-c", PYTHON_RUNTIME_PROBE],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        lines = _validated_probe_lines(result)
        if lines is not None:
            return candidate, lines
        return None

    for candidate in candidates:
        selection = probe(candidate)
        if selection is not None:
            return selection

    discovered = runner(
        [wsl, "-u", "root", "--", "sh", "-c", "command -v python3"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if discovered.returncode == 0:
        selection = probe(discovered.stdout.strip())
        if selection is not None:
            return selection

    checked_text = ", ".join(checked) if checked else "none"
    _handle_wsl_unavailable(
        "Set BATCH_SMOKE_WSL_PYTHON to an existing WSL Python >=3.10,<3.14 "
        f"that can import langcodes; checked: {checked_text}",
        environ=values,
    )


def run_wsl_root(*, root, sandbox, wsl):
    _verify_wsl_root(wsl=wsl)
    source_hashes, copied_hashes = _copy_production(
        root,
        sandbox,
        ("batch.sh", "py_launcher.sh"),
    )
    workspace = sandbox / "work"
    metrics = sandbox / "metrics"
    workspace.mkdir()
    metrics.mkdir()
    events_path = sandbox / "whisperx-events.jsonl"
    events_path.touch()
    report_path = sandbox / "batch-result.txt"
    fake_site = _write_fake_whisperx(sandbox)
    fake_ffmpeg = _write_bash_boundaries(sandbox)
    paths = {
        "sandbox": _wsl_path(wsl, sandbox),
        "workspace": _wsl_path(wsl, workspace),
        "metrics": _wsl_path(wsl, metrics),
        "events": _wsl_path(wsl, events_path),
        "report": _wsl_path(wsl, report_path),
        "fake_site": _wsl_path(wsl, fake_site),
        "fake_ffmpeg": _wsl_path(wsl, fake_ffmpeg),
    }
    root_wsl = _wsl_path(wsl, root)
    wsl_python, probe_lines = _resolve_wsl_python(
        wsl=wsl,
        repo_path=root_wsl,
    )
    expected_cpu_io = int(probe_lines[0])
    subprocess.run(
        [
            wsl,
            "-u",
            "root",
            "--",
            "chmod",
            "+x",
            paths["fake_ffmpeg"],
        ],
        check=True,
        timeout=20,
    )
    result = subprocess.run(
        [
            wsl,
            "-u",
            "root",
            "--",
            "env",
            f"PYTHON_PATH_LINUX={wsl_python}",
            f"PYTHONPATH={paths['fake_site']}",
            f"FAKE_BATCH_WORKSPACE={paths['workspace']}",
            f"FAKE_BATCH_METRICS={paths['metrics']}",
            f"FAKE_WHISPERX_EVENTS={paths['events']}",
            f"FAKE_BATCH_AUDIO_BARRIER={min(len(URLS), expected_cpu_io)}",
            f"FFMPEG_PATH_LINUX={paths['fake_ffmpeg']}",
            "TORCH_BACKEND=cpu",
            "WHISPER_DEVICE=cpu",
            "WHISPER_MODEL=fake-model",
            "TARGET_LANG=zh",
            "bash",
            f"{paths['sandbox']}/batch.sh",
            "--report",
            paths["report"],
            *URLS,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    return _collect_evidence(
        platform="wsl-bash",
        wrapper_chain="batch.sh -> py_launcher.sh -> batch.py",
        result=result,
        source_hashes=source_hashes,
        copied_hashes=copied_hashes,
        workspace=workspace,
        metrics=metrics,
        events_path=events_path,
        report_path=report_path,
        expected_cpu_io=expected_cpu_io,
        python_executable=wsl_python,
        python_resolved_executable=probe_lines[1],
        python_version=probe_lines[2],
        python_environment=(
            f"{probe_lines[1]} Python={probe_lines[2]} langcodes={probe_lines[3]}"
        ),
    )


def run_bash(*, root, sandbox, python_executable, bash):
    source_hashes, copied_hashes = _copy_production(
        root,
        sandbox,
        ("batch.sh", "py_launcher.sh"),
    )
    workspace = sandbox / "work"
    metrics = sandbox / "metrics"
    workspace.mkdir()
    metrics.mkdir()
    events_path = sandbox / "whisperx-events.jsonl"
    events_path.touch()
    report_path = sandbox / "batch-result.txt"
    fake_site = _write_fake_whisperx(sandbox)
    fake_ffmpeg = _write_bash_boundaries(sandbox)
    expected_cpu_io = max(1, (os.cpu_count() or 1) // 4)
    env = _base_environment(
        workspace,
        metrics,
        events_path,
        fake_site,
        cpu_io_capacity=expected_cpu_io,
    )
    env["PYTHON_PATH_LINUX"] = str(python_executable.resolve())
    env["FFMPEG_PATH_LINUX"] = str(fake_ffmpeg.resolve())
    probe = subprocess.run(
        [
            str(python_executable.resolve()),
            "-c",
            PYTHON_RUNTIME_PROBE,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    probe_lines = _validated_probe_lines(probe)
    if probe_lines is None:
        raise unittest.SkipTest(
            "bash smoke Python must be >=3.10,<3.14 and import langcodes: "
            f"{python_executable.resolve()}"
        )
    result = subprocess.run(
        [
            bash,
            str(sandbox / "batch.sh"),
            "--report",
            str(report_path),
            *URLS,
        ],
        cwd=sandbox,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    return _collect_evidence(
        platform="bash",
        wrapper_chain="batch.sh -> py_launcher.sh -> batch.py",
        result=result,
        source_hashes=source_hashes,
        copied_hashes=copied_hashes,
        workspace=workspace,
        metrics=metrics,
        events_path=events_path,
        report_path=report_path,
        expected_cpu_io=expected_cpu_io,
        python_executable=str(python_executable.resolve()),
        python_resolved_executable=probe_lines[1],
        python_version=probe_lines[2],
        python_environment=(
            f"{probe_lines[1]} Python={probe_lines[2]} langcodes={probe_lines[3]}"
        ),
    )
