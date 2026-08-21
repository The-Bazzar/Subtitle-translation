param(
    [Alias("u")]
    [Parameter(Position = 0, HelpMessage = "YouTube video URL")]
    [string]$Url,

    [Alias("h")]
    [Parameter(HelpMessage = "Show help")]
    [switch]$Help
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($Help -or (-not $Url)) {
    @"
download.ps1 — 下载 YouTube 视频 + 元数据 (不含字幕生成)

用法:
  .\download.ps1 <YouTube URL>

说明:
  下载原片、缩略图 (PNG)、元数据、简介、标签。
  将原片保留为 <标题>.original.<ext>，不生成编辑版。
  如需编辑版，请将 OUTPUT_RENDER_VIDEO 传给 prepare-video.ps1。
"@
    if ($Help) {
        exit 0
    }
    exit 1
}

# ── 从 .env 读取配置 ─────────────────────────────────────────────────────────
. "$PSScriptRoot\.env.ps1"
$Ytdlp = Get-EnvValue 'YTDLP_PATH_WIN' 'yt-dlp'

function Resolve-AbsolutePath {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-Item $Path).FullName
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
$ExistingOriginalMkv = Join-Path $FolderName "$FolderName.original.mkv"
$HasExistingOriginalMkv = Test-Path $ExistingOriginalMkv -PathType Leaf
if ($HasExistingOriginalMkv) {
    Write-Host "发现已有原片: $ExistingOriginalMkv" -ForegroundColor Green
    Write-Host "将跳过视频下载，仅补充 metadata / thumbnail / description / tags" -ForegroundColor Gray
}

# ── 步骤 2: 下载视频 + 元数据 ──────────────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Cyan
if ($HasExistingOriginalMkv) {
    Write-Host "步骤 2: yt-dlp 下载元数据及封面" -ForegroundColor Cyan
} else {
    Write-Host "步骤 2: yt-dlp 下载视频、元数据及封面" -ForegroundColor Cyan
}
Write-Host "=============================================" -ForegroundColor Cyan

if ($HasExistingOriginalMkv) {
    $YtdlArgs = @(
        '-o', "$FolderName/$FolderName.%(ext)s",
        '--cookies', 'cookies.txt',
        '--skip-download',
        '--write-thumbnail',
        '--convert-thumbnails', 'png',
        '--write-info-json',
        '--write-description',
        '--no-mtime',
        '--print-to-file', 'tags', "$FolderName/${FolderName}.tags.txt",
        $Url
    )
} else {
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
}

& $Ytdlp @YtdlArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: yt-dlp download failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

# ── 步骤 3: 寻找下载好的视频文件 ──────────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "步骤 3: 寻找下载好的视频文件" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan

if ($HasExistingOriginalMkv) {
    $RenderVideoPath = $ExistingOriginalMkv
    $RenderVideoAbs = Resolve-AbsolutePath $RenderVideoPath
    Write-Host "使用已有原片: $RenderVideoPath" -ForegroundColor Green
} else {
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

    if (Test-Path $RenderVideoPath) {
        Remove-Item $RenderVideoPath -Force
    }
    Move-Item -LiteralPath $OriginalVideoAbs -Destination $RenderVideoPath -Force
    $RenderVideoAbs = Resolve-AbsolutePath $RenderVideoPath
}

# ── 完成 ──────────────────────────────────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Green
Write-Host "Finish! 所有文件已保存在文件夹: $FolderName" -ForegroundColor Green
Write-Host "渲染原片: $RenderVideoAbs" -ForegroundColor Gray
Write-Host "=============================================" -ForegroundColor Green

Write-Output "OUTPUT_RENDER_VIDEO=$RenderVideoAbs"
