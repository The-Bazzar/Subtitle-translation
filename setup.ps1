# =============================================================================
# setup.ps1 — 安装字幕流水线全部依赖 (Windows PowerShell)
# =============================================================================

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ScriptDir = Split-Path $PSCommandPath -Parent

function Copy-ConfigIfMissing {
    param(
        [string]$ExampleName,
        [string]$TargetName
    )
    $ExamplePath = Join-Path $ScriptDir $ExampleName
    $TargetPath = Join-Path $ScriptDir $TargetName
    if ((Test-Path $ExamplePath) -and -not (Test-Path $TargetPath)) {
        Copy-Item -LiteralPath $ExamplePath -Destination $TargetPath
        Write-Host "  created $TargetName from $ExampleName" -ForegroundColor Gray
    }
}

function Update-EnvFromExample {
    $ExamplePath = Join-Path $ScriptDir ".env.example"
    $EnvPath = Join-Path $ScriptDir ".env"
    if (-not (Test-Path $ExamplePath)) {
        return
    }
    if (-not (Test-Path $EnvPath)) {
        Copy-Item -LiteralPath $ExamplePath -Destination $EnvPath
        Write-Host "  created .env from .env.example" -ForegroundColor Gray
        return
    }

    $ExistingKeys = @{}
    foreach ($Line in Get-Content -LiteralPath $EnvPath -Encoding UTF8) {
        if ($Line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
            $ExistingKeys[$Matches[1]] = $true
        }
    }

    $MissingLines = New-Object System.Collections.Generic.List[string]
    $PendingComments = New-Object System.Collections.Generic.List[string]
    foreach ($Line in Get-Content -LiteralPath $ExamplePath -Encoding UTF8) {
        if ($Line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
            $Key = $Matches[1]
            if (-not $ExistingKeys.ContainsKey($Key)) {
                foreach ($Comment in $PendingComments) {
                    $MissingLines.Add($Comment)
                }
                $PendingComments.Clear()
                $MissingLines.Add($Line)
                $ExistingKeys[$Key] = $true
            } else {
                $PendingComments.Clear()
            }
        } elseif ($Line.Trim() -eq "" -or $Line.TrimStart().StartsWith("#")) {
            $PendingComments.Add($Line)
        } else {
            $PendingComments.Clear()
        }
    }

    if ($MissingLines.Count -gt 0) {
        Add-Content -LiteralPath $EnvPath -Value ""
        Add-Content -LiteralPath $EnvPath -Value "# Added by setup from .env.example"
        Add-Content -LiteralPath $EnvPath -Value $MissingLines
        Write-Host "  updated .env with $($MissingLines.Count) missing template line(s)" -ForegroundColor Gray
    } else {
        Write-Host "  .env: up to date" -ForegroundColor Gray
    }
}

Write-Host ">>> 准备本地配置文件..." -ForegroundColor Yellow
Update-EnvFromExample
Copy-ConfigIfMissing "providers.example.json" "providers.json"
Copy-ConfigIfMissing "tavily_domains.example.json" "tavily_domains.json"
Copy-ConfigIfMissing "glossary_prompt.example.md" "glossary_prompt.md"
Copy-ConfigIfMissing "translate_prompt.example.md" "translate_prompt.md"
Copy-ConfigIfMissing "proofread_prompt.example.md" "proofread_prompt.md"
Copy-ConfigIfMissing "split_prompt.example.md" "split_prompt.md"

. "$ScriptDir\.env.ps1"

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "setup — 字幕流水线环境安装 (Windows)" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan

# ── uv ──────────────────────────────────────────────────────────────────────
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host ">>> 安装 uv..." -ForegroundColor Yellow
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
} else {
    Write-Host "  uv: $(uv --version)" -ForegroundColor Gray
}

# ── yt-dlp ──────────────────────────────────────────────────────────────────
if (-not (Get-Command yt-dlp -ErrorAction SilentlyContinue)) {
    Write-Host ">>> 安装 yt-dlp (winget)..." -ForegroundColor Yellow
    winget install yt-dlp.yt-dlp --silent
} else {
    Write-Host "  yt-dlp: $(yt-dlp --version 2>&1 | Select-Object -First 1)" -ForegroundColor Gray
}

# ── FFmpeg ──────────────────────────────────────────────────────────────────
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host ">>> 安装 FFmpeg (winget)..." -ForegroundColor Yellow
    winget install Gyan.FFmpeg --silent
    Write-Host "  (重启终端后生效)" -ForegroundColor Yellow
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
} else {
    Write-Host "  ffmpeg: $(ffmpeg -version 2>&1 | Select-Object -First 1)" -ForegroundColor Gray
}

# ── Python venv ────────────────────────────────────────────────────────────
Write-Host ">>> 创建/复用项目 .venv..." -ForegroundColor Yellow
Push-Location $ScriptDir
uv venv .venv --python 3.13.12
Pop-Location

# ── PyTorch backend ────────────────────────────────────────────────────────
$TorchBackend = Get-EnvValue 'TORCH_BACKEND' 'auto'
switch ($TorchBackend.ToLowerInvariant()) {
    'auto' {
        $NvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
        if ($NvidiaSmi) {
            & nvidia-smi *> $null
            if ($LASTEXITCODE -eq 0) {
                $TorchBackend = 'cuda128'
                $TorchReason = 'detected NVIDIA GPU'
            } else {
                $TorchBackend = 'cpu'
                $TorchReason = 'nvidia-smi failed'
            }
        } else {
            $TorchBackend = 'cpu'
            $TorchReason = 'nvidia-smi not available'
        }
    }
    'cuda128' {
        $TorchBackend = 'cuda128'
        $TorchReason = 'configured'
    }
    'cpu' {
        $TorchBackend = 'cpu'
        $TorchReason = 'configured'
    }
    default {
        Write-Host "Error: TORCH_BACKEND must be auto, cuda128, or cpu (got: $TorchBackend)" -ForegroundColor Red
        exit 1
    }
}

$PythonExe = Join-Path $ScriptDir ".venv\Scripts\python.exe"

# ── Python packages ────────────────────────────────────────────────────────
Write-Host ">>> 同步 pyproject.toml Python packages (asr)..." -ForegroundColor Yellow
Push-Location $ScriptDir
uv sync --inexact --extra asr
$SyncExitCode = $LASTEXITCODE
Pop-Location
if ($SyncExitCode -ne 0) {
    Write-Host "Error: uv sync failed (exit code $SyncExitCode)." -ForegroundColor Red
    exit $SyncExitCode
}

Write-Host ">>> 安装 PyTorch backend: $TorchBackend ($TorchReason)" -ForegroundColor Yellow
if ($TorchBackend -eq 'cuda128') {
    uv pip install --python $PythonExe `
        "torch==2.8.0+cu128" "torchaudio==2.8.0+cu128" `
        --index-url "https://download.pytorch.org/whl/cu128"
} else {
    uv pip install --python $PythonExe `
        "torch==2.8.0" "torchaudio==2.8.0" `
        --index-url "https://download.pytorch.org/whl/cpu"
}

# ── 验证 ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "验证安装" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
$WhisperXExe = Join-Path $ScriptDir ".venv\Scripts\whisperx.exe"
& $PythonExe -c "import openai, langcodes; from tavily import TavilyClient; print('  openai/langcodes/tavily: OK')"
& $WhisperXExe --version 2>&1 | Select-Object -First 1 | ForEach-Object { Write-Host "  whisperx: $_" -ForegroundColor Gray }
Write-Host "  yt-dlp $(yt-dlp --version 2>&1 | Select-Object -First 1)" -ForegroundColor Gray
Write-Host "  ffmpeg $(ffmpeg -version 2>&1 | Select-Object -First 1)" -ForegroundColor Gray
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "setup — 完成!" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next: 编辑 .env，填入 API keys 和本机偏好配置" -ForegroundColor Gray
