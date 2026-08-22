[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $PSCommandPath
$PythonExe = Join-Path $ScriptDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    Write-Error "Project Python environment not found: $PythonExe. Run setup.ps1 first."
    exit 127
}
& $PythonExe -m subtitle_translation pipeline @Arguments
exit $LASTEXITCODE
