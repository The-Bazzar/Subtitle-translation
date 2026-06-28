param(
    [Alias("u")]
    [Parameter(Position = 0, HelpMessage = "YouTube video URL")]
    [string]$Url,

    [Alias("o")]
    [Parameter(HelpMessage = "Output burned video path (default: burned.mkv in video dir)")]
    [string]$Output,

    [Parameter(HelpMessage = "Video encoder (default: hevc_nvenc)")]
    [string]$Ovc = "hevc_nvenc",

    [Parameter(HelpMessage = "Video encoder options (default: source-bitrate)")]
    [string]$Ovcopts = "source-bitrate",

    [Parameter(HelpMessage = "Audio encoder (default: aac)")]
    [string]$Oac = "aac",

    [Alias("r")]
    [Parameter(HelpMessage = "Output resolution (e.g. 1920x1080)")]
    [string]$Res,

    [Parameter(HelpMessage = "Skip download step")]
    [switch]$SkipDownload,

    [Parameter(HelpMessage = "Skip WhisperX subtitle generation")]
    [switch]$SkipWhisper,

    [Parameter(HelpMessage = "Skip subtitle beautify step")]
    [switch]$SkipBeautify,

    [Parameter(HelpMessage = "Skip glossary knowledge base generation")]
    [switch]$SkipKnowledge,

    [Parameter(HelpMessage = "Skip translation step")]
    [switch]$SkipTranslate,

    [Parameter(HelpMessage = "Disable proofread (translate only, no review pass)")]
    [switch]$NoProofread,

    [Parameter(HelpMessage = "Skip burn step (output subtitle files only)")]
    [switch]$SkipBurn,

    [Parameter(HelpMessage = "Source language name/tag for prompts; empty uses WhisperX JSON language")]
    [string]$SourceLang,

    [Parameter(HelpMessage = "Target language name/tag for prompts and ISO 639 output suffix (default: TARGET_LANG or zh)")]
    [string]$TargetLang,

    [Parameter(HelpMessage = "Print commands only, do not execute")]
    [switch]$DryRun,

    [Alias("h")]
    [Parameter(HelpMessage = "Show help")]
    [switch]$Help,

    [Parameter(HelpMessage = "Path to existing bilingual .ass file (skip translation, use for burn)")]
    [string]$ExistingAss,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$FfmpegExtra
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = Split-Path $PSCommandPath -Parent
. "$PSScriptRoot\.env.ps1"

# ── 从 .env 读取阶段默认值 (CLI 显式传参优先) ───────────────────────────────

if (-not $PSBoundParameters.ContainsKey('SkipDownload') -and (Get-EnvFlag 'PIPELINE_SKIP_DOWNLOAD' $false)) {
    $SkipDownload = $true
}
if (-not $PSBoundParameters.ContainsKey('SkipWhisper') -and (Get-EnvFlag 'PIPELINE_SKIP_WHISPER' $false)) {
    $SkipWhisper = $true
}
if (-not $PSBoundParameters.ContainsKey('SkipBeautify') -and (Get-EnvFlag 'PIPELINE_SKIP_BEAUTIFY' $false)) {
    $SkipBeautify = $true
}
if (-not $PSBoundParameters.ContainsKey('SkipKnowledge') -and (Get-EnvFlag 'PIPELINE_SKIP_KNOWLEDGE' $false)) {
    $SkipKnowledge = $true
}
if (-not $PSBoundParameters.ContainsKey('SkipTranslate') -and (Get-EnvFlag 'PIPELINE_SKIP_TRANSLATE' $false)) {
    $SkipTranslate = $true
}
if (-not $PSBoundParameters.ContainsKey('SkipBurn') -and (Get-EnvFlag 'PIPELINE_SKIP_BURN' $false)) {
    $SkipBurn = $true
}
if (-not $PSBoundParameters.ContainsKey('NoProofread') -and -not (Get-EnvFlag 'PROOFREAD' $true)) {
    $NoProofread = $true
}

if (-not $PSBoundParameters.ContainsKey('Ovc')) {
    $Ovc = Merge-EnvDefault 'BURN_OVC' '' 'hevc_nvenc'
}
if (-not $PSBoundParameters.ContainsKey('Ovcopts')) {
    $Ovcopts = Merge-EnvDefault 'BURN_OVCOPTS' '' 'source-bitrate'
}
if (-not $PSBoundParameters.ContainsKey('Oac')) {
    $Oac = Merge-EnvDefault 'BURN_OAC' '' 'aac'
}
if (-not $PSBoundParameters.ContainsKey('Res')) {
    $Res = Merge-EnvDefault 'BURN_RES' '' ''
}
if (-not $PSBoundParameters.ContainsKey('SourceLang')) {
    $SourceLang = Merge-EnvDefault 'SOURCE_LANG' '' ''
}
if (-not $PSBoundParameters.ContainsKey('TargetLang')) {
    $TargetLang = Merge-EnvDefault 'TARGET_LANG' '' 'zh'
}

$PythonExe = Get-EnvValue 'PYTHON_PATH_WIN' ''
if (-not $PythonExe) {
    $PythonExe = Join-Path $ScriptDir ".venv\Scripts\python.exe"
}

# ── 帮助 ──────────────────────────────────────────────────────────────────────

if ($Help -or (-not $Url)) {
    @"
pipeline.ps1 — 超级流水线: YouTube URL → burned.mkv

用法: .\pipeline.ps1 <YouTube URL> [选项...]

流程: 下载 → JSON 语音识别 → JSON 美化 → 术语库 → 翻译 → 硬压 (纯 Windows)
  1. yt-dlp 下载视频 + 元数据
  2. WhisperX 生成词级 JSON
  3. 场景检测美化 JSON 时间轴 (Netflix 规范)
  4. translate_srt.py 可选联网搜索 + LLM 生成术语知识库 (glossary.md)
  5. 整句翻译 + 校对 + 分割 + 词级对轴 → .<source>-<target>.ass
  6. ffmpeg 硬压 → burned.mkv

参数:
  -Url                YouTube 视频链接 (必选)
  -o, -Output         输出视频路径
  -Ovc / -Ovcopts / -Oac  视频/音频编码器参数
  -r, -Res            输出分辨率 (保持宽高比+黑边)
  -SkipDownload       跳过下载
  -SkipWhisper        跳过语音识别
  -SkipBeautify       跳过时间码美化
  -SkipKnowledge      跳过术语知识库
  -SkipTranslate      跳过翻译
  -NoProofread        关闭校对
  -SkipBurn           跳过压制
  -SourceLang         源语言提示词标签；空则使用 WhisperX JSON language
  -TargetLang         目标语言提示词标签；输出后缀会规范为 ISO 639 (默认 TARGET_LANG 或 zh)
  -ExistingAss        已有双语 .ass 路径
  -DryRun             仅打印命令
  -h, -Help           帮助

示例:
  .\pipeline.ps1 "https://youtube.com/watch?v=xxx"
  .\pipeline.ps1 "url" -SkipBurn
  .\pipeline.ps1 "url" -r 1920x1080
"@
    exit 0
}
# ── 工具路径 ──────────────────────────────────────────────────────────────────

$DownloadPs1  = Join-Path $ScriptDir "download.ps1"
$WhisperPs1   = Join-Path $ScriptDir "whisper.ps1"
$TranslatePy  = Join-Path $ScriptDir "translate_srt.py"
$BurnPs1      = Join-Path $ScriptDir "ffmpeg-burn.ps1"

if (-not (Test-Path $PythonExe -PathType Leaf)) {
    Write-Host "Error: Python venv not found. Run .\setup.ps1 first, or set PYTHON_PATH_WIN." -ForegroundColor Red
    exit 1
}

# ── 启动信息 ──────────────────────────────────────────────────────────────────

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "pipeline — Super Pipeline (纯 Windows)" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "URL:       $Url" -ForegroundColor Gray
$SourceLangDisplay = if ($SourceLang) { $SourceLang } else { '(JSON language)' }
Write-Host "Language:  $SourceLangDisplay → $TargetLang" -ForegroundColor Gray
if ($ExistingAss)    { Write-Host "Existing:  $ExistingAss" -ForegroundColor Gray }
$steps = @()
if (-not $SkipDownload)  { $steps += "Download" }
if (-not $SkipWhisper)   { $steps += "Whisper" }
if (-not $SkipBeautify)  { $steps += "Beautify" }
if (-not $SkipKnowledge) { $steps += "Knowledge" }
if (-not $SkipTranslate) { $steps += "Translate" }
if (-not $SkipBurn)      { $steps += "Burn" }
Write-Host "Steps:     $($steps -join ' → ')" -ForegroundColor Gray
Write-Host "=============================================" -ForegroundColor Cyan

if ($DryRun) {
    if (-not $SkipDownload) { Write-Host "[DRY RUN] .\download.ps1 `"$Url`"" -ForegroundColor Yellow }
    exit 0
}

# ── 步骤 1: 下载 ─────────────────────────────────────────────────────────────

if ($SkipDownload) {
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host "SKIP: 下载 (使用已有视频)" -ForegroundColor Cyan
    Write-Host "=============================================" -ForegroundColor Cyan
    $VideoPath = Read-Host "视频文件路径"
    if (-not $VideoPath -or -not (Test-Path $VideoPath)) {
        Write-Host "Error: Video file not found: $VideoPath" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host ""
    Write-Host ">>> Step 1/6: Download" -ForegroundColor Cyan
    $VideoPath = $null
    & $DownloadPs1 $Url 2>&1 | ForEach-Object {
        $_
        if ($_ -match '^OUTPUT_VIDEO=(.+)$') {
            $VideoPath = $Matches[1].Trim()
        }
    }
    $DownloadExitCode = $LASTEXITCODE
    if ($DownloadExitCode -ne 0) {
        Write-Host "Error: Download step failed (exit code $DownloadExitCode)." -ForegroundColor Red
        exit $DownloadExitCode
    }
    if (-not $VideoPath -or -not (Test-Path $VideoPath)) {
        Write-Host "Error: Failed to locate downloaded video." -ForegroundColor Red
        exit 1
    }
}

# ── 推导成果物路径链 ──────────────────────────────────────────────────────────

$VideoDir  = Split-Path $VideoPath -Parent
$VideoBase = [System.IO.Path]::GetFileNameWithoutExtension($VideoPath)
$JsonPath   = Join-Path $VideoDir "$VideoBase.json"
$BeautifiedJson = Join-Path $VideoDir "$VideoBase.beautified.json"
$GlossaryPath = Join-Path $VideoDir "glossary.md"
$TranslateSrc = if ((Test-Path $BeautifiedJson) -and -not $SkipBeautify) { $BeautifiedJson } else { $JsonPath }

# ── 步骤 2: WhisperX 字幕 ─────────────────────────────────────────────────────

if ($SkipWhisper) {
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host "SKIP: WhisperX 语音识别" -ForegroundColor Cyan
    Write-Host "=============================================" -ForegroundColor Cyan
} elseif (Test-Path $JsonPath) {
    Write-Host ""
    Write-Host "SKIP: WhisperX — $JsonPath 已存在" -ForegroundColor Gray
} else {
    Write-Host ""
    Write-Host "Step 2/6: WhisperX" -ForegroundColor Cyan
    & $WhisperPs1 $VideoPath
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($ExistingAss) {
    $AssPath = $ExistingAss
} else {
    $LangArgs = @()
    if ($SourceLang) { $LangArgs += @('--source-lang', $SourceLang) }
    $LangArgs += @('--target-lang', $TargetLang)
    $AssOutput = & $PythonExe $TranslatePy $JsonPath @LangArgs --print-output-path
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $AssPath = ($AssOutput | Where-Object { $_ -match '^OUTPUT_ASS=' } | Select-Object -Last 1) -replace '^OUTPUT_ASS=', ''
}

# ── 步骤 3: 美化时间码 ────────────────────────────────────────────────────────

if ($SkipBeautify) {
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host "SKIP: 时间码美化" -ForegroundColor Cyan
    Write-Host "=============================================" -ForegroundColor Cyan
} elseif (Test-Path $BeautifiedJson) {
    Write-Host ""
    Write-Host "SKIP: 美化 — $BeautifiedJson 已存在" -ForegroundColor Gray
} else {
    Write-Host ""
    Write-Host "Step 3/6: Beautify JSON" -ForegroundColor Cyan
    & $PythonExe $TranslatePy $JsonPath --video $VideoPath --only-beautify
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# ── 步骤 4: 术语知识库 ──────────────────────────────────────────────────────

$TranslateSrc = if (Test-Path $BeautifiedJson) { $BeautifiedJson } else { $JsonPath }

if ($SkipKnowledge) {
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host "SKIP: 术语知识库" -ForegroundColor Cyan
    Write-Host "=============================================" -ForegroundColor Cyan
} elseif (Test-Path $GlossaryPath) {
    Write-Host ""
    Write-Host "SKIP: Knowledge — $GlossaryPath 已存在" -ForegroundColor Gray
} else {
    Write-Host ""
    Write-Host "Step 4/6: Knowledge" -ForegroundColor Cyan
    & $PythonExe $TranslatePy $TranslateSrc --video $VideoPath --only-glossary --skip-beautify
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# ── 步骤 5: 翻译 ──────────────────────────────────────────────────────────────

if ($SkipTranslate) {
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host "SKIP: LLM 翻译" -ForegroundColor Cyan
    Write-Host "=============================================" -ForegroundColor Cyan
} elseif (Test-Path $AssPath) {
    Write-Host ""
    Write-Host "SKIP: 翻译 — $AssPath 已存在" -ForegroundColor Gray
} else {
    Write-Host ""
    Write-Host "Step 5/6: Translate ($SourceLangDisplay → $TargetLang)" -ForegroundColor Cyan
    if ($NoProofread) { $env:PROOFREAD = "0" }
    $LangArgs = @()
    if ($SourceLang) { $LangArgs += @('--source-lang', $SourceLang) }
    $LangArgs += @('--target-lang', $TargetLang)
    & $PythonExe $TranslatePy $TranslateSrc --video $VideoPath --skip-beautify --skip-knowledge @LangArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# ── 步骤 6: 硬压 ──────────────────────────────────────────────────────────────

if ($SkipBurn) {
    $FinalJsonPath = if (Test-Path $BeautifiedJson) { $BeautifiedJson } else { $JsonPath }
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Green
    Write-Host "Done! (burn skipped)" -ForegroundColor Green
    Write-Host "Video:   $VideoPath" -ForegroundColor Gray
    Write-Host "JSON:    $FinalJsonPath" -ForegroundColor Gray
    Write-Host "ASS:     $AssPath" -ForegroundColor Gray
    Write-Host "=============================================" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $AssPath)) {
    Write-Host "Error: ASS not found, cannot burn: $AssPath" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Step 6/6: Burn" -ForegroundColor Cyan

$BurnParams = @{
    VideoPath = $VideoPath
    SubFile   = $AssPath
    Ovc       = $Ovc
    Ovcopts   = $Ovcopts
    Oac       = $Oac
}
if ($Res)    { $BurnParams['Res'] = $Res }
if ($Output) { $BurnParams['Output'] = $Output }

if ($FfmpegExtra.Count -gt 0) {
    & $BurnPs1 @BurnParams @FfmpegExtra
} else {
    & $BurnPs1 @BurnParams
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Burn failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host "Pipeline complete!" -ForegroundColor Green
Write-Host "Video:   $VideoPath" -ForegroundColor Gray
Write-Host "ASS:     $AssPath" -ForegroundColor Gray
Write-Host "=============================================" -ForegroundColor Green


