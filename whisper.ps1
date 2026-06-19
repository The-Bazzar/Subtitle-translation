param(
    [Parameter(Mandatory, Position = 0, HelpMessage = "Video file path")]
    [string]$VideoPath,

    [Parameter(HelpMessage = "ASR model (default: large-v3-turbo)")]
    [string]$Model,

    [Parameter(HelpMessage = "Align model (default: empty = auto)")]
    [string]$AlignModel,

    [Parameter(HelpMessage = "Device: cuda | cpu (default: cuda)")]
    [string]$Device,

    [Alias("h")]
    [Parameter(HelpMessage = "Show help")]
    [switch]$Help
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ── 读取 .env 配置 (优先级: CLI 参数 > .env > 硬编码默认) ──────────────────
. "$PSScriptRoot\.env.ps1"
$Model                   = Merge-EnvDefault 'WHISPER_MODEL'                    $Model                   'large-v3-turbo'
$AlignModel              = Merge-EnvDefault 'WHISPER_ALIGN_MODEL'              $AlignModel              ''
$Device                  = Merge-EnvDefault 'WHISPER_DEVICE'                   $Device                  'cuda'

if ($Help -or (-not $VideoPath)) {
    @"
whisper.ps1 — WhisperX 语音识别生成词级 JSON

用法:
  .\whisper.ps1 <视频文件路径> [选项...]

选项:
  -Model       ASR 模型 (默认: large-v3-turbo)
  -AlignModel  对齐模型 (默认: 按语言自动匹配)
  -Device      cuda|cpu (默认: cuda)

输出:
  同目录输出 <文件名>.json (词级时间码)

translate_srt.py 以 .json 为唯一字幕入口，负责美化、翻译、校对、分割和对轴
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
$JsonPath = Join-Path $VideoDir "$VideoName.json"
if (Test-Path $JsonPath) {
    Write-Host "JSON 已存在, 跳过: $JsonPath"
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
Write-Host "whisper — 语音识别 → .json" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "视频:        $VideoAbs" -ForegroundColor Gray
Write-Host "语言:        $VideoLang" -ForegroundColor Gray
Write-Host "模型:        $Model" -ForegroundColor Gray
Write-Host "设备:        $Device" -ForegroundColor Gray
if ($AlignModel) { Write-Host "对齐:        $AlignModel" -ForegroundColor Gray }
Write-Host "=============================================" -ForegroundColor Cyan

# 提取音频为 WAV (避免长视频时间码漂移)
$WavPath = Join-Path $VideoDir "$VideoName.wav"
Write-Host "提取音频..." -ForegroundColor Gray
& ffmpeg -i $VideoAbs -vn -acodec pcm_s16le -ar 16000 -ac 1 $WavPath -y -loglevel error

$WhisperArgs = @(
    $WavPath,
    '--model', $Model,
    '--language', $VideoLang,
    '--output_dir', $VideoDir,
    '--output_format', 'json',
    '--device', $Device
)
if ($AlignModel) {
    $WhisperArgs += '--align_model'
    $WhisperArgs += $AlignModel
}

$env:TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD = "1"
& whisperx @WhisperArgs
Remove-Item $WavPath -Force
$ExitCode = $LASTEXITCODE

if ($ExitCode -eq 0) {
    Write-Host "=============================================" -ForegroundColor Green
    Write-Host "whisper — 完成: $VideoName.json" -ForegroundColor Green
    Write-Host "=============================================" -ForegroundColor Green
    Write-Host "OUTPUT_JSON=$JsonPath"
} else {
    Write-Host "Error: whisperx failed (exit code: $ExitCode)" -ForegroundColor Red
    exit $ExitCode
}
