# =============================================================================
# setup.ps1 — 安装字幕流水线全部依赖 (Windows PowerShell)
# =============================================================================

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

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

# ── openai ─────────────────────────────────────────────────────────────────
Write-Host ">>> 安装 openai..." -ForegroundColor Yellow
uvx pip install openai

# ── WhisperX (全局工具, CUDA 12.8) ─────────────────────────────────────────
if (-not (Get-Command whisperx -ErrorAction SilentlyContinue)) {
    Write-Host ">>> 安装 WhisperX (CUDA 12.8)..." -ForegroundColor Yellow
    uv tool install git+https://github.com/m-bain/whisperx.git `
        --with "torch==2.8.0+cu128" `
        --with "torchaudio==2.8.0+cu128" `
        --python 3.13.12
} else {
    Write-Host "  whisperx: installed" -ForegroundColor Gray
}

# ── 验证 ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "验证安装" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
python -c "import openai; print('  openai: OK')"
Write-Host "  yt-dlp $(yt-dlp --version 2>&1 | Select-Object -First 1)" -ForegroundColor Gray
Write-Host "  ffmpeg $(ffmpeg -version 2>&1 | Select-Object -First 1)" -ForegroundColor Gray
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "setup — 完成!" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next: 配置 .env (cp .env.example .env 后编辑)" -ForegroundColor Gray
