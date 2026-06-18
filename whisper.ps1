param(
    [Parameter(Mandatory, Position = 0, HelpMessage = "Video file path")]
    [string]$VideoPath,

    [Parameter(HelpMessage = "ASR model (default: large-v3-turbo)")]
    [string]$Model,

    [Parameter(HelpMessage = "Align model (default: empty = auto)")]
    [string]$AlignModel,

    [Parameter(HelpMessage = "Compute type (default: float16)")]
    [string]$ComputeType,

    [Parameter(HelpMessage = "Segmentation: sentence | chunk (default: sentence)")]
    [string]$SegmentResolution,

    [Parameter(HelpMessage = "Max characters per line (default: 42, requires alignment)")]
    [ValidateRange(20, 100)]
    [int]$MaxLineWidth,

    [Parameter(HelpMessage = "Max lines per segment (default: 2, requires alignment)")]
    [ValidateRange(1, 4)]
    [int]$MaxLineCount,

    [Parameter(HelpMessage = "Chunk size in seconds (default: 15, WhisperX: 30)")]
    [ValidateRange(5, 60)]
    [int]$ChunkSize,

    [Parameter(HelpMessage = "VAD speech onset threshold (default: 0.5)")]
    [ValidateRange(0.1, 0.9)]
    [float]$VadOnset,

    [Parameter(HelpMessage = "VAD speech offset threshold (default: 0.363)")]
    [ValidateRange(0.1, 0.9)]
    [float]$VadOffset,

    [Parameter(HelpMessage = "Condition on previous text (default: false, shorter segments)")]
    [bool]$ConditionOnPreviousText,

    [Alias("h")]
    [Parameter(HelpMessage = "Show help")]
    [switch]$Help
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ── 读取 .env 配置 (优先级: CLI 参数 > .env > 硬编码默认) ──────────────────
. "$PSScriptRoot\.env.ps1"
$Model                   = Merge-EnvDefault 'WHISPER_MODEL'                    $Model                   'large-v3-turbo'
$AlignModel              = Merge-EnvDefault 'WHISPER_ALIGN_MODEL'              $AlignModel              ''
$ComputeType             = Merge-EnvDefault 'WHISPER_COMPUTE'                  $ComputeType             'float16'
$SegmentResolution       = Merge-EnvDefault 'WHISPER_SEGMENT_RESOLUTION'       $SegmentResolution       'sentence'
$MaxLineWidth            = Merge-EnvDefault 'WHISPER_MAX_LINE_WIDTH'           $MaxLineWidth            '42'
$MaxLineCount            = Merge-EnvDefault 'WHISPER_MAX_LINE_COUNT'           $MaxLineCount            '2'
$ChunkSize               = Merge-EnvDefault 'WHISPER_CHUNK_SIZE'               $ChunkSize               '15'
$VadOnset                = Merge-EnvDefault 'WHISPER_VAD_ONSET'                $VadOnset                '0.5'
$VadOffset               = Merge-EnvDefault 'WHISPER_VAD_OFFSET'               $VadOffset               '0.363'
$ConditionOnPreviousText = Merge-EnvDefault 'WHISPER_CONDITION_ON_PREVIOUS'    $ConditionOnPreviousText 'false'

if ($Help -or (-not $VideoPath)) {
    @"
whisper.ps1 — WhisperX 语音识别生成英文字幕 (.srt)

用法:
  .\whisper.ps1 <视频文件路径> [选项...]

选项:
  -SegmentResolution  sentence|chunk  分割粒度 (默认: sentence)
  -MaxLineWidth       20-100          每行最大字符数 (默认: 42)
  -MaxLineCount       1-4             每段最大行数 (默认: 2)
  -ChunkSize          5-60            处理块大小秒 (默认: 15)
  -VadOnset           0.1-0.9         VAD 语音起始阈值 (默认: 0.5)
  -VadOffset          0.1-0.9         VAD 语音结束阈值 (默认: 0.363)
  -Model              ASR 模型 (默认: large-v3-turbo)
  -AlignModel         对齐模型 (默认: 按语言自动匹配)

输出:
  同目录输出 <文件名>.srt

调参指南:
  - 句子太长 → -ChunkSize 10 -SegmentResolution sentence
  - 句子太短 → -ChunkSize 30 -SegmentResolution chunk
  - 字幕行溢出 → -MaxLineWidth 36
  - 出现大量单字行 → -MaxLineWidth 50 -MaxLineCount 3
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
Write-Host "视频:        $VideoAbs" -ForegroundColor Gray
Write-Host "语言:        $VideoLang" -ForegroundColor Gray
Write-Host "模型:        $Model" -ForegroundColor Gray
Write-Host "分割粒度:    $SegmentResolution" -ForegroundColor Gray
Write-Host "行宽/行数:   $MaxLineWidth 字符/行, $MaxLineCount 行/段" -ForegroundColor Gray
Write-Host "块大小:      ${ChunkSize}s" -ForegroundColor Gray
if ($AlignModel) { Write-Host "对齐:        $AlignModel" -ForegroundColor Gray }
Write-Host "=============================================" -ForegroundColor Cyan

$WhisperArgs = @(
    $VideoAbs,
    '--model', $Model,
    '--language', $VideoLang,
    '--output_dir', $VideoDir,
    '--output_format', 'srt',
    '--compute_type', $ComputeType,
    '--segment_resolution', $SegmentResolution,
    '--chunk_size', $ChunkSize,
    '--vad_onset', $VadOnset,
    '--vad_offset', $VadOffset,
    '--condition_on_previous_text', $ConditionOnPreviousText,
    '--max_line_width', $MaxLineWidth,
    '--max_line_count', $MaxLineCount
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
