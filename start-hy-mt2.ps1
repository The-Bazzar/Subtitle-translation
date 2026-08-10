param(
    [int]$Port = 8080,
    [int]$ContextSize = 8192,
    [int]$GpuLayers = 99,
    [switch]$Help
)

$Utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8

$ScriptDir = Split-Path $PSCommandPath -Parent
$ModelPath = Join-Path $ScriptDir 'models\Hy-MT2-30B-A3B-GGUF\Hy-MT2-30B-A3B-Q4_K_M.gguf'
$RuntimeDir = Join-Path $ScriptDir 'llama-runtime'

if ($Help) {
    Write-Host '启动本地 Hy-MT2-30B-A3B Q4_K_M OpenAI 兼容服务'
    Write-Host '用法: .\start-hy-mt2.ps1 [-Port 8080] [-ContextSize 8192] [-GpuLayers 99]'
    exit 0
}

if (-not (Test-Path -LiteralPath $ModelPath -PathType Leaf)) {
    Write-Error "Hy-MT2 GGUF 未找到: $ModelPath"
    exit 1
}

$Server = Get-ChildItem -LiteralPath $RuntimeDir -Filter 'llama-server.exe' -File -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $Server) {
    $Server = Get-Command llama-server.exe -ErrorAction SilentlyContinue
}
if (-not $Server) {
    Write-Error "llama-server.exe 未找到。请将官方 llama.cpp CUDA 12.4 Windows 包解压到: $RuntimeDir"
    exit 1
}
$ServerPath = if ($Server.PSObject.Properties.Name -contains 'FullName') { $Server.FullName } else { $Server.Source }

$Arguments = @(
    '-m', $ModelPath,
    '--alias', 'Hy-MT2-30B-A3B-Q4_K_M',
    '--host', '127.0.0.1',
    '--port', $Port.ToString(),
    '-ngl', $GpuLayers.ToString(),
    '-c', $ContextSize.ToString(),
    '-b', '256',
    '-ub', '128',
    '--parallel', '1',
    '--cont-batching',
    '--jinja'
)

Write-Host "Model:    $ModelPath" -ForegroundColor Cyan
Write-Host "Server:   $ServerPath" -ForegroundColor Cyan
Write-Host "Endpoint: http://127.0.0.1:$Port/v1" -ForegroundColor Green
Write-Host "Context:  $ContextSize tokens; GPU layers: $GpuLayers" -ForegroundColor Gray
Write-Host '保持此窗口运行；另开 PowerShell 执行 pipeline.ps1。' -ForegroundColor Yellow

& $ServerPath @Arguments
exit $LASTEXITCODE
