param(
    [Parameter(Position = 0, HelpMessage = "Video file path")]
    [string]$VideoPath,

    [Alias("o")]
    [Parameter(HelpMessage = "Output path (default: burned.mkv in video dir)")]
    [string]$Output,

    [Alias("s")]
    [Parameter(HelpMessage = "Subtitle file to burn (e.g. .zh-en.ass)")]
    [string]$SubFile,

    [Parameter(HelpMessage = "Video encoder (default: hevc_nvenc)")]
    [string]$Ovc = "hevc_nvenc",

    [Parameter(HelpMessage = "Video encoder options (default: qp=20)")]
    [string]$Ovcopts = "qp=20",

    [Parameter(HelpMessage = "Audio encoder (default: aac)")]
    [string]$Oac = "aac",

    [Parameter(HelpMessage = "ffmpeg.exe path (default: ffmpeg in PATH)")]
    [string]$FfmpegPath = "ffmpeg",

    [Parameter(HelpMessage = "Print command only, do not execute")]
    [switch]$DryRun,

    [Alias("h")]
    [Parameter(HelpMessage = "Show help")]
    [switch]$Help,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$FfmpegExtra
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ── 帮助 ──────────────────────────────────────────────────────────────────────

if ($Help -or ($PSBoundParameters.Count -eq 0 -and -not $VideoPath)) {
    @"
ffmpeg-burn.ps1 — 字幕硬压 (ffmpeg 滤镜)

用法:
  .\ffmpeg-burn.ps1 <视频文件> [选项...] [ffmpeg额外参数...]

说明:
  使用 ffmpeg 的 ass 滤镜将 ASS 字幕硬压到视频中。
  相比 mpv-burn: 保留原视频封面图, 无需额外依赖。

参数:
  -VideoPath          视频文件路径 (必选, 位置 0)
  -Output             输出文件路径 (默认: 视频同目录 burned.mkv)
  -SubFile            字幕文件路径 (如 .zh-en.ass 双语字幕)
  -Ovc                视频编码器 (默认: hevc_nvenc)
  -Ovcopts            视频编码器参数 (默认: qp=20)
  -Oac                音频编码器 (默认: aac)
  -FfmpegPath         ffmpeg.exe 路径 (默认: 系统 PATH)
  -DryRun             仅打印命令, 不执行
  -Help               显示此帮助

示例:
  .\ffmpeg-burn.ps1 video.webm -SubFile video.zh-en.ass
  .\ffmpeg-burn.ps1 video.webm -SubFile sub.ass -o result.mkv
  .\ffmpeg-burn.ps1 video.webm -Ovc libx265 -Ovcopts crf=23
  .\ffmpeg-burn.ps1 video.webm -DryRun

常用编码器:
  hevc_nvenc    NVIDIA GPU H.265 硬编码 (默认, 速度快)
  libx265       CPU H.265 软编码 (体积最小)
  libx264       CPU H.264 软编码 (兼容性最好)
"@
    exit 0
}

if (-not $VideoPath) {
    Write-Host "Error: VideoPath is required." -ForegroundColor Red
    Write-Host "Usage: .\ffmpeg-burn.ps1 <video> [-o output] [-h]" -ForegroundColor Gray
    exit 1
}

if (-not (Test-Path $VideoPath -PathType Leaf)) {
    Write-Host "Error: Video file not found: $VideoPath" -ForegroundColor Red
    exit 1
}

# ── 路径处理 ──────────────────────────────────────────────────────────────────

$VideoAbs = [System.IO.Path]::GetFullPath((Get-Item $VideoPath).FullName)
$VideoDir = Split-Path $VideoAbs -Parent

if (-not $Output) {
    $Output = Join-Path $VideoDir "burned.mkv"
}
$OutputAbs = [System.IO.Path]::GetFullPath($Output)

# 字幕路径转绝对路径 + 反斜杠转正斜杠 (ffmpeg ass 滤镜要求)
if ($SubFile) {
    $SubFileAbs = [System.IO.Path]::GetFullPath((Get-Item $SubFile).FullName)
    $SubFileFfm = $SubFileAbs -replace '\\', '/' -replace ':', '\:'
}

# 编码器参数拆分: "qp=20" → -qp 20, "crf=23" → -crf 23
$OvcKey, $OvcVal = $Ovcopts -split '=', 2
if (-not $OvcVal) { $OvcVal = $OvcKey; $OvcKey = 'qp' }

# ── 执行 ──────────────────────────────────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "ffmpeg-burn — 字幕硬压" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "输入:    $VideoAbs" -ForegroundColor Gray
Write-Host "输出:    $OutputAbs" -ForegroundColor Gray
if ($SubFile) {
    Write-Host "字幕:    $SubFileAbs" -ForegroundColor Gray
}
Write-Host "视频:    -c:v $Ovc -$OvcKey $OvcVal" -ForegroundColor Gray
Write-Host "音频:    -c:a $Oac" -ForegroundColor Gray
if ($FfmpegExtra.Count -gt 0) {
    Write-Host "额外:    $($FfmpegExtra -join ' ')" -ForegroundColor Gray
}
Write-Host "=============================================" -ForegroundColor Cyan

$FfmpegArgs = @(
    '-i', $VideoAbs,
    '-vf', "ass='$SubFileFfm'",
    '-c:v', $Ovc,
    "-$OvcKey", $OvcVal,
    '-c:a', $Oac,
    '-map', '0:v:0?',
    '-map', '0:a:0?',
    '-map', '0:v:1?',
    '-map_metadata', '0',
    '-disposition:v:1', 'attached_pic',
    '-movflags', '+faststart'
)
if ($SubFile) {
    $FfmpegArgs += @('-map', '0:s?')
}
$FfmpegArgs += $FfmpegExtra
$FfmpegArgs += $OutputAbs

if ($DryRun) {
    Write-Host ""
    Write-Host "[DRY RUN] 将执行的命令:" -ForegroundColor Yellow
    Write-Host "& `"$FfmpegPath`" $($FfmpegArgs -join ' ')" -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "正在压制字幕..." -ForegroundColor Cyan

& $FfmpegPath @FfmpegArgs
$ExitCode = $LASTEXITCODE

if ($ExitCode -eq 0) {
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Green
    Write-Host "硬字幕压制完成!" -ForegroundColor Green
    Write-Host "输出: $OutputAbs" -ForegroundColor Green
    Write-Host "=============================================" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Error: ffmpeg encoding failed (exit code: $ExitCode)" -ForegroundColor Red
    exit $ExitCode
}
