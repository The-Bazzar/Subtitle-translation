param(
    [Alias("i")]
    [Parameter(Mandatory, Position = 0, HelpMessage = "Original video path")]
    [string]$OriginalVideo
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

. "$PSScriptRoot\.env.ps1"
$Ffmpeg = Get-EnvValue 'FFMPEG_PATH_WIN' 'ffmpeg'
try {
    $FfmpegCommand = Get-Command -Name $Ffmpeg -ErrorAction Stop
} catch {
    Write-Host "Error: ffmpeg command not found: $Ffmpeg" -ForegroundColor Red
    exit 1
}

function Format-NativeCommand {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    $quoted = foreach ($arg in $Arguments) {
        $text = [string]$arg
        if ($text -match '[\s"`]') {
            '"' + ($text -replace '"', '\"') + '"'
        } else {
            $text
        }
    }

    return ((@($FilePath) + @($quoted)) -join ' ').Trim()
}

function Test-FfmpegEncoder {
    param([Parameter(Mandatory)][string]$Name)
    try {
        $LASTEXITCODE = $null
        $encoders = & $FfmpegCommand -hide_banner -encoders 2>$null
        return (($LASTEXITCODE -eq 0) -and (($encoders | Select-String -SimpleMatch $Name -Quiet) -eq $true))
    } catch {
        return $false
    }
}

function Test-NvidiaAvailable {
    $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $nvidiaSmi) {
        return $false
    }

    try {
        $LASTEXITCODE = $null
        & $nvidiaSmi -L *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function New-EditVideoReencodeArgs {
    param(
        [Parameter(Mandatory)][string]$InputPath,
        [Parameter(Mandatory)][string]$OutputPath,
        [Parameter(Mandatory)][string[]]$VideoArgs
    )

    return @(
        '-hide_banner',
        '-stats',
        '-i', $InputPath,
        '-pix_fmt', 'yuv420p'
    ) + $VideoArgs + @(
        '-filter_complex', '[0:a]aresample=async=1:out_sample_fmt=s16[aout]',
        '-map', '0:v:0',
        '-map', '[aout]',
        '-c:a', 'flac',
        '-map_metadata', '-1',
        '-movflags', '+faststart',
        '-y',
        $OutputPath
    )
}

function Test-NonEmptyFile {
    param([Parameter(Mandatory)][string]$Path)
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        return (
            $item -is [System.IO.FileInfo] -and
            -not $item.PSIsContainer -and
            ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0 -and
            $item.Length -gt 0
        )
    } catch {
        return $false
    }
}

function Get-FileIdentity {
    param([Parameter(Mandatory)][string]$Path)

    if (-not ('PrepareVideo.NativeMethods' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace PrepareVideo {
    [StructLayout(LayoutKind.Sequential)]
    public struct ByHandleFileInformation {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    public static class NativeMethods {
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool GetFileInformationByHandle(
            SafeFileHandle fileHandle,
            out ByHandleFileInformation fileInformation);
    }
}
'@
    }

    $handle = [System.IO.File]::OpenHandle(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete),
        [System.IO.FileOptions]::None
    )
    try {
        $information = [PrepareVideo.ByHandleFileInformation]::new()
        if (-not [PrepareVideo.NativeMethods]::GetFileInformationByHandle($handle, [ref]$information)) {
            throw [System.ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
        return '{0:X8}:{1:X8}:{2:X8}' -f $information.VolumeSerialNumber, $information.FileIndexHigh, $information.FileIndexLow
    } finally {
        $handle.Dispose()
    }
}

function Invoke-EditVideoReencode {
    param(
        [Parameter(Mandatory)][string]$InputPath,
        [Parameter(Mandatory)][string]$OutputPath
    )

    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host "prepare-video: 重编码生成编辑视频" -ForegroundColor Cyan
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host "原片: $InputPath" -ForegroundColor Gray
    Write-Host "编辑: $OutputPath" -ForegroundColor Gray
    Write-Host "模式: 优先 h264_nvenc；不可用时回退 libx264；音频统一 aresample s16 + flac" -ForegroundColor Gray

    $outputDirectory = Split-Path $OutputPath -Parent
    $outputBaseName = [System.IO.Path]::GetFileNameWithoutExtension($OutputPath)
    $temporaryPath = Join-Path $outputDirectory ".$outputBaseName.prepare.$([guid]::NewGuid().ToString('N')).mkv"

    $attempts = @()
    if ((Test-NvidiaAvailable) -and (Test-FfmpegEncoder -Name 'h264_nvenc')) {
        $attempts += @{
            Name = 'h264_nvenc'
            Args = New-EditVideoReencodeArgs -InputPath $InputPath -OutputPath $temporaryPath -VideoArgs @(
                '-c:v', 'h264_nvenc',
                '-cq', '12'
            )
        }
    } else {
        Write-Host "跳过 h264_nvenc: 未检测到可用 NVIDIA GPU 或 ffmpeg h264_nvenc 编码器" -ForegroundColor Yellow
    }

    $attempts += @{
        Name = 'libx264'
        Args = New-EditVideoReencodeArgs -InputPath $InputPath -OutputPath $temporaryPath -VideoArgs @(
            '-c:v', 'libx264',
            '-crf', '12'
        )
    }

    $lastExitCode = 1
    try {
        foreach ($attempt in $attempts) {
            if (Test-Path -LiteralPath $temporaryPath) {
                Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction Stop
            }

            Write-Host "尝试: $($attempt.Name)" -ForegroundColor DarkGray
            [Console]::Error.WriteLine("ffmpeg cmd: $(Format-NativeCommand -FilePath $Ffmpeg -Arguments $attempt.Args)")
            try {
                $LASTEXITCODE = $null
                & $FfmpegCommand @($attempt.Args)
                $lastExitCode = if ($null -eq $LASTEXITCODE) { 1 } else { [int]$LASTEXITCODE }
            } catch {
                Write-Host "Error: failed to invoke ffmpeg command '$Ffmpeg': $($_.Exception.Message)" -ForegroundColor Red
                return 1
            }

            $accepted = ($lastExitCode -eq 0 -and (Test-NonEmptyFile -Path $temporaryPath))
            if ($attempt.Name -eq 'h264_nvenc' -and $lastExitCode -ne 0 -and (Test-NonEmptyFile -Path $temporaryPath)) {
                Write-Host "Warning: h264_nvenc 返回 exit=$lastExitCode，但本次已输出非 0B 文件，继续使用该文件" -ForegroundColor Yellow
                $accepted = $true
            }

            if ($accepted) {
                try {
                    [System.IO.File]::Move($temporaryPath, $OutputPath, $true)
                    return 0
                } catch {
                    Write-Host "Error: failed to replace prepared edit video '$OutputPath': $($_.Exception.Message)" -ForegroundColor Red
                    return 1
                }
            }

            if ($lastExitCode -eq 0) {
                $lastExitCode = 1
            }

            Write-Host "Warning: $($attempt.Name) 重编码失败: exit=$lastExitCode" -ForegroundColor Yellow
        }

        Write-Host "Error: ffmpeg re-encode failed." -ForegroundColor Red
        return $lastExitCode
    } finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            try {
                Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction Stop
            } catch {
                Write-Warning "Could not clean temporary edit video '$temporaryPath': $($_.Exception.Message)"
            }
        }
    }
}

if (-not (Test-Path -LiteralPath $OriginalVideo -PathType Leaf)) {
    Write-Host "Error: Original video not found: $OriginalVideo" -ForegroundColor Red
    exit 1
}

$OriginalVideoAbs = (Get-Item -LiteralPath $OriginalVideo).FullName
$OriginalDirectory = Split-Path $OriginalVideoAbs -Parent
$OriginalBaseName = [System.IO.Path]::GetFileNameWithoutExtension($OriginalVideoAbs)
$EditBaseName = $OriginalBaseName -replace '\.original$', ''
$EditVideoAbs = Join-Path $OriginalDirectory "$EditBaseName.mkv"
if ([System.StringComparer]::OrdinalIgnoreCase.Equals($OriginalVideoAbs, $EditVideoAbs)) {
    Write-Host "Error: Edit video path would overwrite the original: $OriginalVideoAbs" -ForegroundColor Red
    exit 1
}

try {
    $OriginalVideoIdentity = Get-FileIdentity -Path $OriginalVideoAbs
} catch {
    Write-Host "Error: failed to verify original video file identity: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$EditVideoItem = Get-Item -LiteralPath $EditVideoAbs -Force -ErrorAction SilentlyContinue
if ($null -ne $EditVideoItem) {
    if ($EditVideoItem.PSIsContainer) {
        Write-Host "Error: Edit video output path is a directory: $EditVideoAbs" -ForegroundColor Red
        exit 1
    }
    if ($EditVideoItem -isnot [System.IO.FileInfo]) {
        Write-Host "Error: Edit video output path is not a regular file: $EditVideoAbs" -ForegroundColor Red
        exit 1
    }
    try {
        $EditVideoIdentity = Get-FileIdentity -Path $EditVideoAbs
        if ($OriginalVideoIdentity -eq $EditVideoIdentity) {
            Write-Host "Error: Edit video path resolves to the original file: $EditVideoAbs" -ForegroundColor Red
            exit 1
        }
    } catch {
        Write-Host "Error: failed to replace prepared edit video '$EditVideoAbs': could not verify file identity: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
    if (($EditVideoItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Write-Host "Error: Edit video output path is not a regular file: $EditVideoAbs" -ForegroundColor Red
        exit 1
    }
}

$reencodeExitCode = Invoke-EditVideoReencode -InputPath $OriginalVideoAbs -OutputPath $EditVideoAbs
if ($reencodeExitCode -ne 0) {
    exit $reencodeExitCode
}

Write-Output "OUTPUT_VIDEO=$EditVideoAbs"
exit 0
