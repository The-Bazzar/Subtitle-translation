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
"@
    exit 0
}

if (-not (Test-Path $VideoPath -PathType Leaf)) {
    Write-Host "Error: Video file not found: $VideoPath" -ForegroundColor Red
    exit 1
}

$VideoAbs = (Get-Item $VideoPath).FullName
$VideoDir = Split-Path $VideoAbs -Parent
$VideoName = [System.IO.Path]::GetFileNameWithoutExtension($VideoAbs)

# 已存在则跳过
$SrtPath = Join-Path $VideoDir "$VideoName.srt"
if (Test-Path $SrtPath) {
    Write-Host "字幕已存在, 跳过: $SrtPath"
    exit 0
}

# 从 .info.json 读取视频语言
$VideoLang = "en"
$InfoJson = Join-Path $VideoDir "$VideoName.info.json"
if (Test-Path $InfoJson) {
    try {
        $Info = Get-Content $InfoJson -Raw | ConvertFrom-Json
        $Lang = if ($Info.language) { $Info.language } else { "" }
        if ($Lang) {
            $VideoLang = ($Lang -split '-')[0].ToLower()
        }
    } catch {}
}

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "whisper — 语音识别 → .srt" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "视频: $VideoAbs" -ForegroundColor Gray
Write-Host "语言: $VideoLang" -ForegroundColor Gray
Write-Host "模型: $Model" -ForegroundColor Gray
if ($AlignModel) { Write-Host "对齐: $AlignModel" -ForegroundColor Gray }
Write-Host "=============================================" -ForegroundColor Cyan

$WhisperArgs = @(
    $VideoAbs,
    '--model', $Model,
    '--language', $VideoLang,
    '--output_dir', $VideoDir,
    '--output_format', 'srt',
    '--compute_type', $ComputeType
)
if ($AlignModel) {
    $WhisperArgs += '--align_model'
    $WhisperArgs += $AlignModel
}

& uvx whisperx @WhisperArgs
$ExitCode = $LASTEXITCODE

if ($ExitCode -eq 0) {
    Write-Host "=============================================" -ForegroundColor Green
    Write-Host "whisper — 完成: $VideoName.srt" -ForegroundColor Green
    Write-Host "=============================================" -ForegroundColor Green
} else {
    Write-Host "Error: whisperx failed (exit code: $ExitCode)" -ForegroundColor Red
    exit $ExitCode
}
