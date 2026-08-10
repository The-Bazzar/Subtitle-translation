param(
    [Parameter(Position = 0, HelpMessage = "Video file path")]
    [string]$VideoPath,

    [Alias("o")]
    [Parameter(HelpMessage = "Output path (default: burned.mkv in video dir)")]
    [string]$Output,

    [Alias("s")]
    [Parameter(HelpMessage = "Subtitle file to burn (e.g. .en-zh.ass)")]
    [string]$SubFile,

    [Parameter(HelpMessage = "Video encoder (default: hevc_nvenc)")]
    [string]$Ovc = "hevc_nvenc",

    [Parameter(HelpMessage = "Video encoder options (default: source-bitrate)")]
    [string]$Ovcopts = "source-bitrate",

    [Parameter(HelpMessage = "Audio encoder (default: aac)")]
    [string]$Oac = "aac",

    [Alias("r")]
    [Parameter(HelpMessage = "Output resolution (e.g. 1920x1080, 1280x720)")]
    [string]$Res,

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
  使用 ffmpeg 的 ass 滤镜将 ASS 字幕硬压到视频中, 保留原视频封面图。
  ffmpeg 路径从 .env 的 FFMPEG_PATH_WIN 读取, 留空则用系统 ffmpeg。

参数:
  -VideoPath          视频文件路径 (必选, 位置 0)
  -Output             输出文件路径 (默认: 视频同目录 burned.mkv)
  -SubFile            字幕文件路径 (如 .en-zh.ass 双语 ASS)
  -Ovc                视频编码器 (默认: hevc_nvenc)
  -Ovcopts            视频编码器参数 (默认: source-bitrate, 自动接近源视频码率)
  -Oac                音频编码器 (默认: aac)
  -Res                输出分辨率 (如 1920x1080, 默认: 原视频)
  -DryRun             仅打印命令, 不执行
  -Help               显示此帮助

示例:
  .\ffmpeg-burn.ps1 video.webm -SubFile video.en-zh.ass
  .\ffmpeg-burn.ps1 video.webm -SubFile sub.ass -o result.mkv
  .\ffmpeg-burn.ps1 video.webm -Ovcopts source-bitrate
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

# 从 .env 读取 ffmpeg 路径
. "$PSScriptRoot\.env.ps1"
$FfmpegPath = Get-EnvValue 'FFMPEG_PATH_WIN' 'ffmpeg'

function Resolve-FfprobePath([string]$FfmpegPath) {
    try {
        if ($FfmpegPath -and $FfmpegPath -ne 'ffmpeg') {
            $FfmpegFull = [System.IO.Path]::GetFullPath($FfmpegPath)
            $Dir = Split-Path $FfmpegFull -Parent
            foreach ($Name in @('ffprobe.exe', 'ffprobe')) {
                $Candidate = Join-Path $Dir $Name
                if (Test-Path $Candidate -PathType Leaf) { return $Candidate }
            }
        }
    } catch {}
    return 'ffprobe'
}

function Test-SourceBitrateOvcopts([string]$Value) {
    $Normalized = "$Value".Trim().ToLowerInvariant()
    return $Normalized -in @('auto', 'source', 'source-bitrate', 'source_bitrate', 'match-source')
}

function ConvertTo-Int64OrZero([string]$Value) {
    $Parsed = 0L
    if ([Int64]::TryParse("$Value".Trim(), [ref]$Parsed) -and $Parsed -gt 0) {
        return $Parsed
    }
    return 0L
}

function ConvertTo-DoubleOrZero([string]$Value) {
    $Parsed = 0.0
    if ([Double]::TryParse(
        "$Value".Trim(),
        [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$Parsed
    ) -and $Parsed -gt 0) {
        return $Parsed
    }
    return 0.0
}

function Get-SourceVideoBitrateKbps([string]$Video, [string]$FfprobePath) {
    $StreamBitrate = & $FfprobePath -v error -select_streams v:0 -show_entries stream=bit_rate -of default=noprint_wrappers=1:nokey=1 $Video 2>$null | Select-Object -First 1
    $StreamBps = ConvertTo-Int64OrZero $StreamBitrate
    if ($StreamBps -gt 0) { return [int][Math]::Ceiling($StreamBps / 1000.0) }

    $FormatBitrate = & $FfprobePath -v error -show_entries format=bit_rate -of default=noprint_wrappers=1:nokey=1 $Video 2>$null | Select-Object -First 1
    $FormatBps = ConvertTo-Int64OrZero $FormatBitrate
    if ($FormatBps -gt 0) { return [int][Math]::Ceiling($FormatBps / 1000.0) }

    $DurationText = & $FfprobePath -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $Video 2>$null | Select-Object -First 1
    $Duration = ConvertTo-DoubleOrZero $DurationText
    if ($Duration -gt 0) {
        $Bytes = (Get-Item $Video).Length
        return [int][Math]::Ceiling(($Bytes * 8.0 / $Duration) / 1000.0)
    }
    return 0
}

function Get-SourceBitrateOvcopts([string]$Video, [string]$FfprobePath, [string]$Encoder) {
    $Kbps = Get-SourceVideoBitrateKbps $Video $FfprobePath
    if ($Kbps -le 0) { return $null }
    $Maxrate = [int][Math]::Ceiling($Kbps * 1.25)
    $Bufsize = [int][Math]::Ceiling($Kbps * 2.0)
    $Prefix = if ("$Encoder".ToLowerInvariant().Contains('nvenc')) { 'rc=vbr,' } else { '' }
    return @{
        Kbps = $Kbps
        Text = "${Prefix}b=${Kbps}k,maxrate=${Maxrate}k,bufsize=${Bufsize}k"
    }
}

function ConvertTo-FfmpegVideoArgs([string]$Options) {
    $Args = @()
    foreach ($Part in ("$Options" -split ',')) {
        $Item = $Part.Trim()
        if (-not $Item) { continue }
        $Key, $Value = $Item -split '=', 2
        if (-not $Value) {
            $Args += @('-qp', $Key)
            continue
        }
        switch ($Key) {
            'b' { $Args += @('-b:v', $Value) }
            'b:v' { $Args += @('-b:v', $Value) }
            default { $Args += @("-$Key", $Value) }
        }
    }
    return $Args
}

$FfprobePath = Resolve-FfprobePath $FfmpegPath
$ResolvedOvcopts = $Ovcopts
if (Test-SourceBitrateOvcopts $Ovcopts) {
    $SourceBitrate = Get-SourceBitrateOvcopts $VideoAbs $FfprobePath $Ovc
    if ($SourceBitrate) {
        $ResolvedOvcopts = $SourceBitrate.Text
    } else {
        Write-Host "Warning: failed to probe source bitrate with ffprobe; fallback to qp=20." -ForegroundColor Yellow
        $ResolvedOvcopts = 'qp=20'
    }
}
$VideoEncodeArgs = ConvertTo-FfmpegVideoArgs $ResolvedOvcopts

# ── 执行 ──────────────────────────────────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "ffmpeg-burn — 字幕硬压" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "输入:    $VideoAbs" -ForegroundColor Gray
Write-Host "输出:    $OutputAbs" -ForegroundColor Gray
if ($SubFile) {
    Write-Host "字幕:    $SubFileAbs" -ForegroundColor Gray
}
Write-Host "视频:    -c:v $Ovc $($VideoEncodeArgs -join ' ')" -ForegroundColor Gray
if ($SourceBitrate) { Write-Host "码率:    source-bitrate -> $($SourceBitrate.Kbps)k" -ForegroundColor Gray }
if ($Res) { Write-Host "分辨率:  $Res" -ForegroundColor Gray }
Write-Host "音频:    -c:a $Oac" -ForegroundColor Gray
if ($FfmpegExtra.Count -gt 0) {
    Write-Host "额外:    $($FfmpegExtra -join ' ')" -ForegroundColor Gray
}
Write-Host "=============================================" -ForegroundColor Cyan

# 构建滤镜链: ass + 可选 scale
$Vf = "ass='$SubFileFfm'"
if ($Res) {
    $resW, $resH = $Res -split 'x', 2
    # 保持宽高比 + 黑边填充, 不拉伸变形
    if ($resW -and $resH) { $Vf += ",scale=${resW}:${resH}:force_original_aspect_ratio=decrease,pad=${resW}:${resH}:(ow-iw)/2:(oh-ih)/2" }
}

$FfmpegArgs = @(
    '-i', $VideoAbs,
    '-vf', $Vf,
    '-c:v', $Ovc
)
$FfmpegArgs += $VideoEncodeArgs
$FfmpegArgs += @(
    '-c:a', $Oac,
    '-map', '0:v:0?',
    '-map', '0:a:0?',
    '-map', '0:v:1?',
    '-map_metadata', '0',
    '-disposition:v:1', 'attached_pic',
    '-movflags', '+faststart',
    '-y'
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
