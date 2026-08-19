[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PythonArgs
)

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$script = Join-Path $PSScriptRoot 'merge_ass.py'
$configuredPython = $env:PYTHON_PATH_WIN
if (-not $configuredPython -and (Test-Path (Join-Path $PSScriptRoot '.env.ps1'))) {
    . (Join-Path $PSScriptRoot '.env.ps1')
    $configuredPython = Get-EnvValue 'PYTHON_PATH_WIN' ''
}
if ($configuredPython) {
    $python = $configuredPython
}
if (-not (Test-Path $python -PathType Leaf)) {
    Write-Error "Python executable not found: $python. Run setup.ps1 first."
    exit 1
}

& $python $script @PythonArgs
exit $LASTEXITCODE
