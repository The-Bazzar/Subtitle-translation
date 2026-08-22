[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $PSCommandPath
$PythonExe = Join-Path $ScriptDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { Write-Error "Run setup.ps1 first: $PythonExe"; exit 127 }
& $PythonExe -m subtitle_translation whisper @Arguments
exit $LASTEXITCODE
