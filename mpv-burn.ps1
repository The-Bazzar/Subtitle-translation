param (
    [Parameter(Mandatory=$true, HelpMessage="请提供要压制字幕的视频文件路径")]
    [string]$videoPath
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (Test-Path $videoPath) {
    $AbsoluteVideoPath = [System.IO.Path]::GetFullPath((Get-Item $videoPath).FullName)
} else {
    Write-Host "错误：找不到指定的视频文件，请检查路径是否正确！" -ForegroundColor Red
    exit
}

$ParentDir        = Split-Path $AbsoluteVideoPath -Parent
$FileNameWithoutExt = [System.IO.Path]::GetFileNameWithoutExtension($AbsoluteVideoPath)

# 2. 拼接输出文件路径（在原视频同目录下，命名为“原文件名_burned.mp4”）
$OutputPath = Join-Path $ParentDir "burned.mkv"

Write-Host "正在准备压制字幕..." -ForegroundColor Cyan
Write-Host "输入视频: $AbsoluteVideoPath" -ForegroundColor Gray
Write-Host "输出视频: $OutputPath" -ForegroundColor Gray

# 3. 执行 mpv 命令
# 注意：mpv 的 --o 参数需要接收完整的绝对路径
& "C:\Users\oculi\mpv-lazy\mpv.com" `
    $AbsoluteVideoPath `
    --slang=zh-en `
    --o=$OutputPath `
    --ovc=hevc_nvenc `
    --ovcopts="qp=20" `
    # --vf-append=vapoursynth="~~/vs/MEMC_RIFE_NV.vpy" `
    --oac=aac

Write-Host "硬字幕压制完成！" -ForegroundColor Green