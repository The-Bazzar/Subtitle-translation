param (
    [Parameter(Mandatory=$true, HelpMessage="请提供有效的 YouTube 视频链接")]
    [string]$url
)

# 确保输出时不会因为特殊字符乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 从 .env 读取 $Ytdlp 路径
$ScriptDir = Split-Path $PSCommandPath -Parent
$EnvFile = Join-Path $ScriptDir '.env'
function Get-EnvValue([string]$Key, [string]$Default) {
    if (-not (Test-Path $EnvFile)) { return $Default }
    $m = Select-String -Path $EnvFile -Pattern "^\s*$Key\s*=\s*(.*)" | Select-Object -First 1
    if ($m) { $v = $m.Matches.Groups[1].Value.Trim(); if ($v) { return $v } }
    return $Default
}
$Ytdlp = Get-EnvValue 'YTDLP_PATH_WIN' '$Ytdlp'

Write-Host "正在准备下载视频..." -ForegroundColor Cyan
Write-Host "目标链接: $Url" -ForegroundColor Gray

# 执行 $Ytdlp 命令
$Ytdlp -o "%(title)s/%(title)s.%(ext)s" `
        --no-mtime `
        --sponsorblock-mark sponsor,selfpromo `
        --sponsorblock-remove sponsor,selfpromo `
        --embed-metadata `
        --write-thumbnail `
        --write-info-json `
        --write-description `
        --print-to-file tags "%(title)s/%(title)s_tags.txt" `
        $Url

Write-Host "下载流程执行完毕！" -ForegroundColor Green