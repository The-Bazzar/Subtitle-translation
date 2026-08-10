param(
    [Alias("p")]
    [Parameter(HelpMessage = "Translation provider: openrouter | deepseek | gemini")]
    [ValidateSet("openrouter", "deepseek", "gemini")]
    [string]$TranslateProvider,

    [Alias("tm")]
    [Parameter(HelpMessage = "Translation model override")]
    [string]$TranslateModel,

    [Alias("j")]
    [Parameter(HelpMessage = "Max parallel jobs (default: CPU core count)")]
    [int]$MaxJobs = [Environment]::ProcessorCount,

    [Parameter(HelpMessage = "Skip burn step (output subtitle files only)")]
    [switch]$SkipBurn,

    [Parameter(HelpMessage = "Print commands only, do not execute")]
    [switch]$DryRun,

    [Parameter(HelpMessage = "Report file path (default: batch-result.txt in script dir)")]
    [string]$Report,

    [Alias("h")]
    [Parameter(HelpMessage = "Show help")]
    [switch]$Help,

    [Parameter(Mandatory, Position = 0, ValueFromRemainingArguments)]
    [string[]]$Urls
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ── 帮助 ──────────────────────────────────────────────────────────────────────

if ($Help -or ($Urls.Count -eq 0)) {
    @"
batch.ps1 — 批量字幕流水线 (并行)

用法:
  .\batch.ps1 "URL1" "URL2" "URL3" ... [选项...]

说明:
  对多个 YouTube 链接并行执行 pipeline.ps1，最大化利用 CPU/GPU/网络资源。
  每个视频独立下载 → 字幕 → 美化 → 翻译 → 硬压。

参数:
  -Urls                YouTube 链接列表 (必选, 位置 0, 可多个)
  -p, -TranslateProvider  翻译后端 (默认: 从 .env 读取)
  -tm, -TranslateModel    翻译模型
  -j, -MaxJobs            最大并行数 (默认: CPU 核心数)
  -SkipBurn               跳过硬压 (仅输出字幕)
  -DryRun                 仅打印命令, 不执行
  -Report                 结果报告路径 (默认: 脚本同目录 batch-result.txt)
  -Help                   显示帮助

示例:
  .\batch.ps1 "https://youtube.com/watch?v=xxx" "https://youtube.com/watch?v=yyy"
  .\batch.ps1 -j 4 url1 url2 url3 url4 url5
  .\batch.ps1 -p deepseek url1 url2 -SkipBurn

资源利用:
  - 下载/翻译: 网络 I/O 密集型, 高并发
  - WhisperX: GPU VRAM 限制 (large-v3 ~6-8GB), 建议 1-2 个并发
  - ffmpeg 硬压: NVIDIA NVENC 支持 3-5 路并发
  - 推荐 -j 3~5 平衡 GPU 资源
"@
    exit 0
}

# ── 准备 ──────────────────────────────────────────────────────────────────────

$ScriptDir = Split-Path $PSCommandPath -Parent
$PipelinePs1 = Join-Path $ScriptDir "pipeline.ps1"

if (-not $Report) {
    $Report = Join-Path $ScriptDir "batch-result.txt"
}

$Total = $Urls.Count
$Completed = 0
$Failed = 0
$StartTime = Get-Date

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "batch — $Total videos, max $MaxJobs parallel" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "Start:    $($StartTime.ToString('yyyy-MM-dd HH:mm:ss'))" -ForegroundColor Gray
Write-Host "Pipeline: $PipelinePs1" -ForegroundColor Gray
if ($TranslateProvider) { Write-Host "Provider: $TranslateProvider" -ForegroundColor Gray }
if ($TranslateModel)    { Write-Host "Model:    $TranslateModel" -ForegroundColor Gray }
Write-Host "=============================================" -ForegroundColor Cyan

if ($DryRun) {
    foreach ($url in $Urls) {
        Write-Host "[DRY RUN] .\pipeline.ps1 `"$url`"" -ForegroundColor Yellow
    }
    exit 0
}

# ── 并行执行 ──────────────────────────────────────────────────────────────────

# 使用 RunspacePool 实现真正的并行 + 并发控制
$Pool = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspacePool(1, $MaxJobs)
$Pool.Open()

$Jobs = [System.Collections.Generic.List[PSObject]]::new()
$ResultLock = [System.Threading.Mutex]::new()

foreach ($url in $Urls) {
    $ps = [System.Management.Automation.PowerShell]::Create()
    $ps.RunspacePool = $Pool

    # 构建 pipeline.ps1 调用
    $null = $ps.AddCommand($PipelinePs1)
    $null = $ps.AddArgument($url)
    if ($SkipBurn)   { $null = $ps.AddParameter('SkipBurn') }
    if ($TranslateProvider) { $null = $ps.AddParameter('TranslateProvider', $TranslateProvider) }
    if ($TranslateModel)    { $null = $ps.AddParameter('TranslateModel', $TranslateModel) }

    $job = [PSCustomObject]@{
        Url     = $url
        PS      = $ps
        Handle  = $null
        Started = $null
    }

    $job.Handle = $ps.BeginInvoke()
    $job.Started = Get-Date
    $Jobs.Add($job)

    Write-Host "[$($Jobs.Count)/$Total] Started: $url" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "All $Total jobs launched. Waiting for completion..." -ForegroundColor Cyan
Write-Host ""

# ── 轮询等待, 实时汇报进度 ───────────────────────────────────────────────────

$Results = [System.Collections.Generic.List[PSCustomObject]]::new()

while ($Jobs.Count -gt 0) {
    $finished = @()
    foreach ($job in $Jobs) {
        if ($job.Handle.IsCompleted) {
            $finished += $job
        }
    }

    foreach ($job in $finished) {
        try {
            $result = $job.PS.EndInvoke($job.Handle)
            $exitCode = 0
            $output = if ($result) { $result -join "`n" } else { "" }
        } catch {
            $exitCode = 1
            $output = $_.Exception.Message
        } finally {
            $job.PS.Dispose()
        }

        $elapsed = [math]::Round(((Get-Date) - $job.Started).TotalMinutes, 1)
        $Completed++
        if ($exitCode -eq 0) { $FailedCount = 0 } else { $FailedCount = 1; $Failed++ }

        $status = if ($exitCode -eq 0) { "OK" } else { "FAIL" }
        $color = if ($exitCode -eq 0) { "Green" } else { "Red" }
        $remaining = $Jobs.Count - 1
        $eta = if ($Completed -gt 0) {
            $avg = ((Get-Date) - $StartTime).TotalMinutes / $Completed
            [math]::Round($avg * $remaining, 0)
        } else { "?" }

        Write-Host ("[{0}/{1}] {2} ({3}min) [{4}]  ← {5}" -f $Completed, $Total, $status, $elapsed, "ETA ${eta}min", $job.Url) -ForegroundColor $color

        $Results.Add([PSCustomObject]@{
            Url       = $job.Url
            Status    = $status
            Elapsed   = "${elapsed}min"
            Started   = $job.Started.ToString('yyyy-MM-dd HH:mm:ss')
        })

        $Jobs.Remove($job)
    }

    if ($Jobs.Count -gt 0) {
        Start-Sleep -Seconds 5
    }
}

$Pool.Close()
$Pool.Dispose()

# ── 报告 ──────────────────────────────────────────────────────────────────────

$EndTime = Get-Date
$TotalElapsed = [math]::Round(($EndTime - $StartTime).TotalMinutes, 1)

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "batch — All done!" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "Total:    $Total" -ForegroundColor Gray
Write-Host "Success:  $($Total - $Failed)" -ForegroundColor Green
if ($Failed -gt 0) {
    Write-Host "Failed:   $Failed" -ForegroundColor Red
}
Write-Host "Elapsed:  ${TotalElapsed}min" -ForegroundColor Gray
Write-Host "Report:   $Report" -ForegroundColor Gray
Write-Host "=============================================" -ForegroundColor Cyan

# Write report file
$Results | Select-Object @{N='#';E={$Results.IndexOf($_) + 1}}, Url, Status, Elapsed, Started |
    Format-Table -AutoSize |
    Out-File -FilePath $Report -Encoding UTF8

"`nTotal: $Total, Success: $($Total - $Failed), Failed: $Failed, Elapsed: ${TotalElapsed}min`n" |
    Add-Content -Path $Report -Encoding UTF8

exit ($Failed -gt 0 ? 1 : 0)
