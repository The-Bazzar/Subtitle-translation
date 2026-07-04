param(
    [Alias("u")]
    [Parameter(Mandatory, Position = 0, HelpMessage = "YouTube video URL")]
    [string]$Url,

    [Alias("h")]
    [Parameter(HelpMessage = "Show help")]
    [switch]$Help
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($Help) {
    @"
download.ps1 — 下载 YouTube 视频 + 元数据 (不含字幕生成)

用法:
  .\download.ps1 <YouTube URL>

说明:
  下载原片、缩略图 (PNG)、元数据、简介、标签。
  然后保留 <标题>.original.<ext> 原片，并通过 CPU 默认解码 + yuv4mpegpipe 纯净帧流重编码出 <标题>.mp4 编辑版。
  只负责下载和重编码，不运行 WhisperX。
"@
    exit 0
}

# ── 从 .env 读取配置 ─────────────────────────────────────────────────────────
. "$PSScriptRoot\.env.ps1"
$Ytdlp = Get-EnvValue 'YTDLP_PATH_WIN' 'yt-dlp'
$Ffmpeg = Get-EnvValue 'FFMPEG_PATH_WIN' 'ffmpeg'

function Resolve-AbsolutePath {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-Item $Path).FullName
}

function Start-PipedNativeProcess {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$RedirectStdout,
        [switch]$RedirectStdin
    )

    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    foreach ($arg in $Arguments) {
        [void]$psi.ArgumentList.Add([string]$arg)
    }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $RedirectStdout.IsPresent
    $psi.RedirectStandardInput = $RedirectStdin.IsPresent
    $psi.CreateNoWindow = $true

    $proc = [System.Diagnostics.Process]::new()
    $proc.StartInfo = $psi
    if (-not $proc.Start()) {
        throw "Failed to start process: $FilePath"
    }
    return $proc
}

function Stop-ProcessQuietly {
    param([System.Diagnostics.Process]$Process)
    if (-not $Process) {
        return
    }
    try {
        if (-not $Process.HasExited) {
            $Process.Kill($true)
        }
    } catch {}
    try {
        $Process.Dispose()
    } catch {}
}

function Invoke-FfmpegFramePipeAttempt {
    param(
        [Parameter(Mandatory)][string[]]$DecodeArgs,
        [Parameter(Mandatory)][string[]]$EncodeArgs,
        [Parameter(Mandatory)][string]$OutputPath,
        [Parameter(Mandatory)][string]$Label
    )

    $decoder = $null
    $encoder = $null
    try {
        if (Test-Path $OutputPath) {
            Remove-Item $OutputPath -Force -ErrorAction SilentlyContinue
        }

        Write-Host "尝试: $Label" -ForegroundColor DarkGray
        $encoder = Start-PipedNativeProcess -FilePath $Ffmpeg -Arguments $EncodeArgs -RedirectStdin
        $decoder = Start-PipedNativeProcess -FilePath $Ffmpeg -Arguments $DecodeArgs -RedirectStdout

        $copyTask = $decoder.StandardOutput.BaseStream.CopyToAsync($encoder.StandardInput.BaseStream)
        $decoder.WaitForExit()
        $copyTask.GetAwaiter().GetResult()
        $encoder.StandardInput.Close()
        $encoder.WaitForExit()

        if ($decoder.ExitCode -eq 0 -and $encoder.ExitCode -eq 0 -and (Test-Path $OutputPath)) {
            return $true
        }

        Write-Host "Warning: 帧流重编码失败: decode=$($decoder.ExitCode), encode=$($encoder.ExitCode)" -ForegroundColor Yellow
        if (Test-Path $OutputPath) {
            Remove-Item $OutputPath -Force -ErrorAction SilentlyContinue
        }
        return $false
    } catch {
        Write-Host "Warning: 帧流重编码异常: $($_.Exception.Message)" -ForegroundColor Yellow
        if (Test-Path $OutputPath) {
            Remove-Item $OutputPath -Force -ErrorAction SilentlyContinue
        }
        return $false
    } finally {
        if ($decoder) {
            try { $decoder.StandardOutput.Dispose() } catch {}
        }
        if ($encoder) {
            try { $encoder.StandardInput.Dispose() } catch {}
        }
        Stop-ProcessQuietly $decoder
        Stop-ProcessQuietly $encoder
    }
}

function Invoke-EditVideoReencode {
    param(
        [Parameter(Mandatory)][string]$InputPath,
        [Parameter(Mandatory)][string]$OutputPath
    )

    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host "步骤 4: 重编码生成编辑视频" -ForegroundColor Cyan
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host "原片: $InputPath" -ForegroundColor Gray
    Write-Host "编辑: $OutputPath" -ForegroundColor Gray
    Write-Host "模式: CPU 默认解码出 yuv4mpegpipe 纯净帧流，再与原音频合并编码" -ForegroundColor Gray

    $decodeArgs = @(
        '-hide_banner',
        '-stats',
        '-i', $InputPath,
        '-map', '0:v:0',
        '-f', 'yuv4mpegpipe',
        '-'
    )

    $encodeAttempts = @(
        @{
            Name = 'NVENC encode + original audio'
            Args = @(
                '-hide_banner',
                '-stats',
                '-y',
                '-fflags', '+genpts',
                '-i', 'pipe:0',
                '-i', $InputPath,
                '-filter_complex', '[1:a]aresample=async=1:first_pts=0[aout]',
                '-map', '0:v:0',
                '-map', '[aout]',
                '-pix_fmt', 'yuv420p',
                '-c:v', 'hevc_nvenc',
                '-preset', 'p5',
                '-rc', 'vbr',
                '-cq', '19',
                '-b:v', '0',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-movflags', '+faststart',
                '-avoid_negative_ts', 'make_zero',
                $OutputPath
            )
        },
        @{
            Name = 'libx264 encode + original audio'
            Args = @(
                '-hide_banner',
                '-stats',
                '-y',
                '-fflags', '+genpts',
                '-i', 'pipe:0',
                '-i', $InputPath,
                '-filter_complex', '[1:a]aresample=async=1:first_pts=0[aout]',
                '-map', '0:v:0',
                '-map', '[aout]',
                '-pix_fmt', 'yuv420p',
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-movflags', '+faststart',
                '-avoid_negative_ts', 'make_zero',
                $OutputPath
            )
        }
    )

    foreach ($encodeAttempt in $encodeAttempts) {
        if (Invoke-FfmpegFramePipeAttempt -DecodeArgs $decodeArgs -EncodeArgs $encodeAttempt.Args -OutputPath $OutputPath -Label $encodeAttempt.Name) {
            return
        }
    }

    Write-Host "Error: ffmpeg frame-pipe re-encode failed." -ForegroundColor Red
    exit 1
}

# ── 步骤 1: 获取视频标题并创建文件夹 ──────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "步骤 1: 抓取视频标题并创建独立文件夹" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan

$VideoTitle = & $Ytdlp --get-title $Url 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Failed to get video title." -ForegroundColor Red
    exit 1
}
$VideoTitle = $VideoTitle.Trim()

# 生成跨 Windows/WSL 稳定的目录名。保留可读单词，移除容易乱码的 Unicode 标点。
$FolderName = $VideoTitle.Normalize([Text.NormalizationForm]::FormKD)
$FolderName = $FolderName -replace '[\u2018\u2019\u201A\u201B\u2032\u02BC]', ''
$FolderName = $FolderName -replace '[\u201C\u201D\u201E\u201F\u2033]', ''
$FolderName = $FolderName -replace '[\u2010-\u2015]', '-'
$FolderName = $FolderName -replace '[^\p{L}\p{Nd}._ -]+', '_'
$FolderName = $FolderName -replace '[\\/:*?"<>|]', '_'
$FolderName = $FolderName -replace '\s+', ' '
$FolderName = $FolderName -replace '_+', '_'
$FolderName = $FolderName.Trim(" ._")
if (-not $FolderName) {
    $FolderName = "video"
}
Write-Host "视频下载目录: $FolderName" -ForegroundColor Gray

New-Item -ItemType Directory -Force -Path $FolderName | Out-Null

# ── 步骤 2: 下载视频 + 元数据 ──────────────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "步骤 2: yt-dlp 下载视频、元数据及封面" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan

$YtdlArgs = @(
    '-o', "$FolderName/$FolderName.%(ext)s",
    '--cookies', 'cookies.txt',
    '--embed-metadata',
    '--embed-thumbnail',
    '--write-thumbnail',
    '--convert-thumbnails', 'png',
    '--write-info-json',
    '--write-description',
    '--no-mtime',
    '--sponsorblock-remove', 'sponsor,selfpromo',
    '--print-to-file', 'tags', "$FolderName/${FolderName}.tags.txt",
    $Url
)

& $Ytdlp @YtdlArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: yt-dlp download failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

# ── 步骤 3: 寻找下载好的视频文件 ──────────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "步骤 3: 寻找下载好的视频文件" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan

$VideoFile = $null
foreach ($ext in @('mp4', 'mkv', 'webm', 'flv', 'avi')) {
    $candidate = Join-Path $FolderName "$FolderName.$ext"
    if (Test-Path $candidate) {
        $VideoFile = $candidate
        break
    }
}

if (-not $VideoFile) {
    Write-Host "Error: 未找到下载完成的视频文件!" -ForegroundColor Red
    Write-Host "预期: $FolderName/$FolderName.<mp4|mkv|webm|...>" -ForegroundColor Gray
    exit 1
}

Write-Host "成功定位视频文件: $VideoFile" -ForegroundColor Green

$OriginalVideoAbs = Resolve-AbsolutePath $VideoFile
$OriginalExt = [System.IO.Path]::GetExtension($OriginalVideoAbs)
$RenderVideoPath = Join-Path $FolderName "$FolderName.original$OriginalExt"
$EditVideoPath = Join-Path $FolderName "$FolderName.mp4"

if (Test-Path $RenderVideoPath) {
    Remove-Item $RenderVideoPath -Force
}
Move-Item -LiteralPath $OriginalVideoAbs -Destination $RenderVideoPath -Force
$RenderVideoAbs = Resolve-AbsolutePath $RenderVideoPath
$EditVideoAbs = Join-Path (Resolve-Path $FolderName).Path "$FolderName.mp4"

Invoke-EditVideoReencode -InputPath $RenderVideoAbs -OutputPath $EditVideoAbs

# ── 完成 ──────────────────────────────────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Green
Write-Host "Finish! 所有文件已保存在文件夹: $FolderName" -ForegroundColor Green
Write-Host "编辑视频: $EditVideoAbs" -ForegroundColor Gray
Write-Host "渲染原片: $RenderVideoAbs" -ForegroundColor Gray
Write-Host "=============================================" -ForegroundColor Green

Write-Output "OUTPUT_VIDEO=$EditVideoAbs"
Write-Output "OUTPUT_RENDER_VIDEO=$RenderVideoAbs"
