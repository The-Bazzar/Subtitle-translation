$target = if ($args.Count -gt 0) { [string] $args[0] } else { '' }
[string[]] $pythonArgs = if ($args.Count -gt 1) {
    @($args[1..($args.Count - 1)])
} else {
    @()
}

switch -CaseSensitive -Exact ($target) {
    'translate_srt' { $scriptName = 'translate_srt.py'; break }
    'merge_ass' { $scriptName = 'merge_ass.py'; break }
    default {
        [Console]::Error.WriteLine("Error: unsupported Python target: $target")
        exit 2
    }
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
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

$script = Join-Path $PSScriptRoot $scriptName
& $python $script @pythonArgs
exit $LASTEXITCODE
