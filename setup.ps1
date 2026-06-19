# =============================================================================
# setup.ps1 — 安装字幕流水线全部依赖 (Windows PowerShell)
# =============================================================================

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ScriptDir = Split-Path $PSCommandPath -Parent
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
Write-Host "Next: 配置 .env (cp .env.example .env 后编辑)" -ForegroundColor Gray
