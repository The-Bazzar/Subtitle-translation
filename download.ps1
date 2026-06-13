param (
    [Parameter(Mandatory=$true, HelpMessage="请提供有效的 YouTube 视频链接")]
    [string]$url
)

# 确保输出时不会因为特殊字符乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "正在准备下载视频..." -ForegroundColor Cyan
Write-Host "目标链接: $Url" -ForegroundColor Gray

# 执行 yt-dlp 命令
yt-dlp -o "%(title)s/%(title)s.%(ext)s" `
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