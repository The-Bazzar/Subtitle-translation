[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $PSCommandPath
$ProjectRoot = Split-Path -Parent $ScriptDir
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    Write-Error "Project Python environment not found: $PythonExe. Run scripts/setup.ps1 first."
    exit 127
}
& $PythonExe -m subtitle_translation --project-dir $ProjectRoot pipeline @Arguments
exit $LASTEXITCODE
