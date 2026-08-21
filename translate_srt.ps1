[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PythonArgs
)

& (Join-Path $PSScriptRoot 'py_launcher.ps1') translate_srt @PythonArgs
exit $LASTEXITCODE
