param(
    [Parameter(Mandatory, Position = 0, HelpMessage = "Video file path")]
    [string]$VideoPath,

    [Parameter(HelpMessage = "ASR model (default: large-v3-turbo)")]
    [string]$Model = "large-v3-turbo",

    [Parameter(HelpMessage = "Align model (default: empty = auto)")]
    [string]$AlignModel = "",

    [Parameter(HelpMessage = "Compute type (default: float16)")]
    [string]$ComputeType = "float16",

    [Alias("h")]
    [Parameter(HelpMessage = "Show help")]
    [switch]$Help
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($Help -or (-not $VideoPath)) {
    @"
whisper.ps1 — WhisperX 语音识别生成英文字幕 (.srt)

用法:
  .\whisper.ps1 <视频文件路径>

输出:
  同目录输出 <文件名>.srt

通过 WSL 调用 whisper.sh。
"@
    exit 0
}

if (-not (Test-Path $VideoPath -PathType Leaf)) {
    Write-Host "Error: Video file not found: $VideoPath" -ForegroundColor Red
    exit 1
}

$VideoAbs = (Get-Item $VideoPath).FullName

# 转 WSL 路径: C:\path\to\video → /mnt/c/path/to/video
$WslPath = ($VideoAbs -replace '\\', '/') -replace '^([a-zA-Z]):', '/mnt/$1'.ToLower()

$ScriptDir = Split-Path $PSCommandPath -Parent
$WslScriptDir = ($ScriptDir -replace '\\', '/') -replace '^([a-zA-Z]):', '/mnt/$1'.ToLower()

$EnvVars = "WHISPER_MODEL=$Model"
if ($AlignModel) { $EnvVars += " WHISPER_ALIGN_MODEL=$AlignModel" }
$EnvVars += " WHISPER_COMPUTE=$ComputeType"

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "whisper — 语音识别 → .srt" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "视频: $VideoAbs" -ForegroundColor Gray
Write-Host "模型: $Model" -ForegroundColor Gray
if ($AlignModel) { Write-Host "对齐: $AlignModel" -ForegroundColor Gray }
Write-Host "=============================================" -ForegroundColor Cyan

$WslCmd = "cd '$WslScriptDir' && $EnvVars bash whisper.sh '$WslPath'"

& wsl -u root bash -lc $WslCmd
$ExitCode = $LASTEXITCODE

if ($ExitCode -ne 0) {
    Write-Host "Error: whisper.sh failed (exit code: $ExitCode)" -ForegroundColor Red
    exit $ExitCode
}
