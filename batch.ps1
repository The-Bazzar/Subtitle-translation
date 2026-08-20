[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PythonArgs
)

& (Join-Path $PSScriptRoot 'py_launcher.ps1') batch @PythonArgs
exit $LASTEXITCODE
