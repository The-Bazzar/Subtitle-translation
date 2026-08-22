[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)][string]$Target,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $PSCommandPath
$PythonExe = Join-Path $ScriptDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    Write-Error "Project Python environment not found: $PythonExe. Run setup.ps1 first."
    exit 127
}
$Targets = @{
    pipeline = "pipeline"
    batch = "batch"
    translate = "translate"
    translate_srt = "translate"
    "merge-ass" = "merge-ass"
    merge_ass = "merge-ass"
    download = "download"
    "prepare-video" = "prepare-video"
    prepare_video = "prepare-video"
    whisper = "whisper"
    burn = "burn"
    "ffmpeg-burn" = "burn"
    "mpv-burn" = "burn"
    init = "init"
}
if (-not $Targets.ContainsKey($Target)) {
    Write-Error "unsupported Python target: $Target"
    exit 2
}
$Command = $Targets[$Target]
$Extra = @()
if ($Target -eq "mpv-burn") { $Extra = @("--backend", "mpv") }
& $PythonExe -m subtitle_translation $Command @Extra @Arguments
exit $LASTEXITCODE
