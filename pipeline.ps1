param(
    [Alias("u")]
    [Parameter(Position = 0, HelpMessage = "YouTube video URL")]
    [string]$Url,

    [Alias("o")]
    [Parameter(HelpMessage = "Output burned video path (default: burned.mkv in video dir)")]
    [string]$Output,

    [Alias("p")]
    [Parameter(HelpMessage = "Translation provider: openrouter | deepseek | gemini (default: from .env)")]
    [ValidateSet("openrouter", "deepseek", "gemini")]
    [string]$TranslateProvider,

    [Alias("tm")]
    [Parameter(HelpMessage = "Translation model override (default: provider built-in)")]
    [string]$TranslateModel,

    [Alias("m")]
    [Parameter(HelpMessage = "ffmpeg path for burning (default: ffmpeg in PATH)")]
    [string]$FfmpegPath = "ffmpeg",

    [Parameter(HelpMessage = "Video encoder (default: hevc_nvenc)")]
    [string]$Ovc = "hevc_nvenc",

    [Parameter(HelpMessage = "Video encoder options (default: qp=20)")]
    [string]$Ovcopts = "qp=20",

    [Parameter(HelpMessage = "Audio encoder (default: aac)")]
    [string]$Oac = "aac",

    [Alias("r")]
    [Parameter(HelpMessage = "Output resolution (e.g. 1920x1080)")]
    [string]$Res,

    [Parameter(HelpMessage = "Skip download step (use existing video)")]
    [switch]$SkipDownload,

    [Parameter(HelpMessage = "Skip subtitle beautify step")]
    [switch]$SkipBeautify,

    [Parameter(HelpMessage = "Skip translation step")]
    [switch]$SkipTranslate,

    [Parameter(HelpMessage = "Disable proofread (translate only, no review pass)")]
    [switch]$NoProofread,

    [Parameter(HelpMessage = "Skip burn step (output subtitle files only)")]
    [switch]$SkipBurn,

    [Parameter(HelpMessage = "Print commands only, do not execute")]
    [switch]$DryRun,

    [Alias("h")]
    [Parameter(HelpMessage = "Show help")]
    [switch]$Help,

    [Parameter(HelpMessage = "Path to existing .zh-en.ass file (skip translation, use for burn)")]
    [string]$ExistingAss,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$MpvExtraArgs
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ── 帮助 ──────────────────────────────────────────────────────────────────────

if ($Help -or ($PSBoundParameters.Count -eq 0 -and -not $Url)) {
    @"
pipeline.ps1 — 超级流水线: YouTube URL → burned.mkv (硬字幕)

用法:
  .\pipeline.ps1 <YouTube URL> [选项...]

流程:
  1. yt-dlp 下载视频 + SponsorBlock 去广告
  2. WhisperX large-v3 生成英文字幕
  3. ffmpeg/ffprobe 场景检测 → 字幕时间码美化 (Netflix 规范)
  4. LLM API 翻译英文 → 双语 .zh-en.ass (bi-en + bi-zh)
  5. mpv 硬压双语字幕 → burned.mkv

参数:
  -Url                YouTube 视频链接 (必选, 位置 0)
  -Output             输出视频路径 (默认: 视频目录 burned.mkv)
  -TranslateProvider  翻译后端: openrouter | deepseek | gemini (默认: openrouter)
  -TranslateModel     翻译模型 (默认: provider 内置默认)
  -FfmpegPath         ffmpeg 路径 (默认: 系统 PATH)
  -Ovc                视频编码器 (默认: hevc_nvenc)
  -Ovcopts            视频编码器参数 (默认: qp=20)
  -Oac                音频编码器 (默认: aac)
  -Res                输出分辨率 (如 1920x1080, 默认: 原视频)
  -SkipDownload       跳过下载 (使用已有视频)
  -SkipBeautify       跳过时间码美化
  -SkipTranslate      跳过翻译
  -NoProofread        关闭校对 (仅翻译, 不审校)
  -SkipBurn           跳过压制 (输出 .zh.srt + .zh.ass + .zh-en.ass)
  -ExistingAss        已有 .zh-en.ass 路径 (跳过翻译, 直接压制)
  -DryRun             仅打印命令, 不执行
  -Help               显示帮助

示例:
  .\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx"
  .\pipeline.ps1 "https://youtu.be/xxxxx" -TranslateProvider deepseek
  .\pipeline.ps1 "https://youtu.be/xxxxx" -o result.mkv -Ovc libx265 -Ovcopts crf=23
  .\pipeline.ps1 "https://youtu.be/xxxxx" -SkipBurn
  .\pipeline.ps1 "https://youtu.be/xxxxx" -DryRun
  .\pipeline.ps1 "https://youtu.be/xxxxx" --vf-append=vapoursynth="~~/vs/MEMC_RIFE_NV.vpy"

前置依赖:
  WSL: yt-dlp, uvx (whisperx), ffmpeg, ffprobe, python3
  Windows: mpv.com (mpv-lazy)
  API key: .env 中 OPENROUTER_API_KEY / DEEPSEEK_API_KEY / GEMINI_API_KEY
"@
    exit 0
}

if (-not $Url) {
    Write-Host "Error: URL is required." -ForegroundColor Red
    Write-Host "Usage: .\pipeline.ps1 <YouTube URL> [-h]" -ForegroundColor Gray
    exit 1
}

# ── 从 .env 读取默认配置 ──────────────────────────────────────────────────────

$ScriptDir = Split-Path $PSCommandPath -Parent
$EnvFile = Join-Path $ScriptDir '.env'

function Get-EnvValue([string]$Key, [string]$Default) {
    if (-not (Test-Path $EnvFile)) { return $Default }
    $m = Select-String -Path $EnvFile -Pattern "^\s*$Key\s*=\s*(.*)" | Select-Object -First 1
    if ($m) { $v = $m.Matches.Groups[1].Value.Trim(); if ($v) { return $v } }
    return $Default
}

# CLI 参数未指定时, 回退到 .env
if (-not $TranslateProvider) { $TranslateProvider = Get-EnvValue 'TRANSLATE_PROVIDER' 'openrouter' }
if (-not $TranslateModel)    { $TranslateModel    = Get-EnvValue 'TRANSLATE_MODEL' '' }

# ── 路径 ──────────────────────────────────────────────────────────────────────

# WSL 中的脚本目录 (/mnt/c/Users/...)
$WslScriptDir = ($ScriptDir -replace '\\', '/') -replace '^C:', '/mnt/c'
$PipelineSh = "$WslScriptDir/pipeline.sh"
$BurnPs1 = Join-Path $ScriptDir "ffmpeg-burn.ps1"

# ── 构建 WSL 命令 ──────────────────────────────────────────────────────────────

$WslEnv = "BURN=0 "  # pipeline.ps1 handles burn on Windows, don't double-burn in WSL
if ($SkipDownload)   { $WslEnv += "SKIP_DOWNLOAD=1 " }
if ($SkipBeautify)   { $WslEnv += "SKIP_BEAUTIFY=1 " }
if ($SkipTranslate)  { $WslEnv += "SKIP_TRANSLATE=1 " }
if ($NoProofread)    { $WslEnv += "PROOFREAD=0 " }
if ($ExistingAss)    { $WslEnv += "EXISTING_ASS=$(WinToWsl $ExistingAss) " }

$WslCmd = "cd '$WslScriptDir' && ${WslEnv}TRANSLATE_PROVIDER=$TranslateProvider"
if ($TranslateModel) { $WslCmd += " TRANSLATE_MODEL=$TranslateModel" }
$WslCmd += " bash '$PipelineSh' '$Url'"

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "pipeline — Super Pipeline" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "URL:              $Url" -ForegroundColor Gray
Write-Host "Translator:       $TranslateProvider" -ForegroundColor Gray
if ($ExistingAss)         { Write-Host "Translate:      Using existing ASS: $ExistingAss" -ForegroundColor Gray }
if (-not $SkipDownload)   { Write-Host "Step 1: Download + Subtitles" -ForegroundColor Gray }
if (-not $SkipBeautify)   { Write-Host "Step 2: Beautify Timecodes → .beautified.srt" -ForegroundColor Gray }
if (-not $SkipTranslate)  { Write-Host "Step 3: Translate (LLM) → .zh.srt + .zh.ass + .zh-en.ass" -ForegroundColor Gray }
if (-not $SkipBurn)       { Write-Host "Step 4: Burn Subtitles → burned.mkv" -ForegroundColor Gray }
Write-Host "=============================================" -ForegroundColor Cyan

if ($DryRun) {
    Write-Host ""
    Write-Host "[DRY RUN] WSL CMD:" -ForegroundColor Yellow
    Write-Host "  wsl -u root bash -lc `"$WslCmd`"" -ForegroundColor Yellow
    if (-not $SkipBurn) {
        Write-Host "[DRY RUN] Burn CMD:" -ForegroundColor Yellow
        Write-Host "  .\ffmpeg-burn.ps1 <video> -SubFile <ass> -Ovc $Ovc -Ovcopts $Ovcopts" -ForegroundColor Yellow
    }
    exit 0
}

# ── Step 1-3: WSL pipeline (download + beautify + translate) ─────────────────

Write-Host ""
Write-Host ">>> Starting WSL pipeline..." -ForegroundColor Cyan
Write-Host ""

# Stream WSL output line-by-line in real-time, while capturing for later parsing
$WslLines = [System.Collections.ArrayList]::new()
& wsl -u root bash -lc $WslCmd 2>&1 | ForEach-Object {
    $line = $_
    Write-Host $line          # real-time display
    [void]$WslLines.Add($line) # capture for parsing
}
$WslExit = $LASTEXITCODE
$WslOutput = $WslLines -join "`n"

if ($WslExit -ne 0) {
    Write-Host ""
    Write-Host "Error: WSL pipeline failed (exit code: $WslExit)" -ForegroundColor Red
    exit $WslExit
}

# Parse OUTPUT_VIDEO, OUTPUT_SRT, OUTPUT_ASS markers from WSL output
$VideoPath = $null
$BeautifiedSrtPath = $null
$AssPath = $null

foreach ($line in ($WslOutput -replace "`r`n", "`n") -split "`n") {
    if ($line -match '^OUTPUT_VIDEO=(.+)$') {
        $VideoPath = $Matches[1].Trim()
    }
    if ($line -match '^OUTPUT_SRT=(.+)$') {
        $BeautifiedSrtPath = $Matches[1].Trim()
    }
    if ($line -match '^OUTPUT_ASS=(.+)$') {
        $AssPath = $Matches[1].Trim()
    }
}

# WSL 路径 (/mnt/c/...) → Windows 路径 (C:\...)
function WslToWin([string]$WslPath) {
    if ($WslPath -match '^/mnt/([a-zA-Z])/(.+)$') {
        return $Matches[1].ToUpper() + ":\" + ($Matches[2] -replace '/', '\')
    }
    return $WslPath
}

# Windows 路径 (C:\...) → WSL 路径 (/mnt/c/...)
function WinToWsl([string]$WinPath) {
    if ($WinPath -match '^([a-zA-Z]):\\(.+)$') {
        return "/mnt/" + $Matches[1].ToLower() + "/" + ($Matches[2] -replace '\\', '/')
    }
    return $WinPath
}

if ($VideoPath)       { $VideoPath       = WslToWin $VideoPath }
if ($BeautifiedSrtPath) { $BeautifiedSrtPath = WslToWin $BeautifiedSrtPath }
if ($AssPath)         { $AssPath         = WslToWin $AssPath }

Write-Host ""
Write-Host "Video:        $VideoPath" -ForegroundColor Gray
if ($BeautifiedSrtPath) { Write-Host "Beautif. SRT: $BeautifiedSrtPath" -ForegroundColor Gray }
Write-Host "zh-en ASS:    $AssPath" -ForegroundColor Gray

if (-not $VideoPath -or -not (Test-Path $VideoPath)) {
    Write-Host "Error: Could not locate video file." -ForegroundColor Red
    exit 1
}

# ── Step 4: Burn subtitles ───────────────────────────────────────────────────

if ($SkipBurn) {
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Green
    Write-Host "Done! (burn skipped)" -ForegroundColor Green
    Write-Host "zh-en ASS: $AssPath" -ForegroundColor Green
    Write-Host "=============================================" -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host ">>> Burning subtitles..." -ForegroundColor Cyan

# 用 hashtable splatting — PowerShell 标准方式, 无 null 歧义
$BurnParams = @{
    VideoPath   = $VideoPath
    SubFile     = $AssPath
    Ovc         = $Ovc
    Ovcopts     = $Ovcopts
    Oac         = $Oac
    FfmpegPath  = $FfmpegPath
}
if ($Res)    { $BurnParams['Res'] = $Res }
if ($Output) { $BurnParams['Output'] = $Output }

if ($MpvExtraArgs.Count -gt 0) {
    & $BurnPs1 @BurnParams @MpvExtraArgs
} else {
    & $BurnPs1 @BurnParams
}
$BurnExit = $LASTEXITCODE

if ($BurnExit -ne 0) {
    Write-Host ""
    Write-Host "Error: mpv burning failed (exit code: $BurnExit)" -ForegroundColor Red
    exit $BurnExit
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host "Pipeline complete!" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
