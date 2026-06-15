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
  下载视频、缩略图 (PNG)、元数据、简介、标签。
  功能对齐 download_and_sub.sh 的下载部分, 但不运行 WhisperX。
"@
    exit 0
}

# ── 从 .env 读取 yt-dlp 路径 ─────────────────────────────────────────────────

$ScriptDir = Split-Path $PSCommandPath -Parent
$EnvFile = Join-Path $ScriptDir '.env'
function Get-EnvValue([string]$Key, [string]$Default) {
    if (-not (Test-Path $EnvFile)) { return $Default }
    $m = Select-String -Path $EnvFile -Pattern "^\s*$Key\s*=\s*(.*)" | Select-Object -First 1
    if ($m) { $v = $m.Matches.Groups[1].Value.Trim(); if ($v) { return $v } }
    return $Default
}
$Ytdlp = Get-EnvValue 'YTDLP_PATH_WIN' 'yt-dlp'

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

# 过滤非法文件名字符
$FolderName = $VideoTitle -replace '[\\/:*?"<>|]', '_'
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
        $VideoFile = "$FolderName.$ext"
        break
    }
}

if (-not $VideoFile) {
    Write-Host "Error: 未找到下载完成的视频文件!" -ForegroundColor Red
    Write-Host "预期: $FolderName/$FolderName.<mp4|mkv|webm|...>" -ForegroundColor Gray
    exit 1
}

Write-Host "成功定位视频文件: $VideoFile" -ForegroundColor Green

# ── 完成 ──────────────────────────────────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Green
Write-Host "Finish! 所有文件已保存在文件夹: $FolderName" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green

$VideoAbs = Join-Path (Resolve-Path $FolderName).Path $VideoFile
Write-Host "OUTPUT_VIDEO=$VideoAbs"
