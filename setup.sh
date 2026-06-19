#!/bin/bash
# =============================================================================
# setup.sh — 安装字幕流水线全部依赖 (Linux / WSL)
# =============================================================================
set -euo pipefail

echo "============================================="
echo "setup — 字幕流水线环境安装 (Linux/WSL)"
echo "============================================="

# ── uv ──────────────────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo ">>> 安装 uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck source=/dev/null
    [ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"
else
    echo "  uv: $(uv --version)"
fi

# ── yt-dlp ──────────────────────────────────────────────────────────────────
if ! command -v yt-dlp &>/dev/null; then
    echo ">>> 安装 yt-dlp..."
    sudo wget -q https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -O /usr/local/bin/yt-dlp
    sudo chmod a+rx /usr/local/bin/yt-dlp
else
    echo "  yt-dlp: $(yt-dlp --version 2>&1 | head -1)"
fi

# ── FFmpeg ──────────────────────────────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
    echo ">>> 安装 FFmpeg..."
    sudo apt update -qq && sudo apt install -y ffmpeg
else
    echo "  ffmpeg: $(ffmpeg -version 2>&1 | head -1)"
fi

# ── Node.js (yt-dlp 验证需要) ───────────────────────────────────────────────
if ! command -v node &>/dev/null; then
    echo ">>> 安装 Node.js..."
    sudo apt install -y nodejs
else
    echo "  node: $(node --version)"
fi

# ── Python packages ────────────────────────────────────────────────────────
echo ">>> 安装 Python packages..."
uvx pip install openai "langcodes[data]"

# ── WhisperX (全局工具, CUDA 12.8) ─────────────────────────────────────────
if ! command -v whisperx &>/dev/null; then
    echo ">>> 安装 WhisperX (CUDA 12.8)..."
    uv tool install git+https://github.com/m-bain/whisperx.git \
        --with "torch==2.8.0+cu128" \
        --with "torchaudio==2.8.0+cu128" \
        --python 3.13.12
else
    echo "  whisperx: $(whisperx --version 2>&1 || echo installed)"
fi

# ── 验证 ────────────────────────────────────────────────────────────────────
echo ""
echo "============================================="
echo "验证安装"
echo "============================================="
python -c "import openai, langcodes; print('  openai/langcodes: OK')"
echo "  yt-dlp $(yt-dlp --version 2>&1 | head -1)"
echo "  ffmpeg $(ffmpeg -version 2>&1 | head -1 | cut -d' ' -f3)"
echo "  node $(node --version)"
echo "============================================="
echo "setup — 完成!"
echo "============================================="
echo ""
echo "Next: 配置 .env (cp .env.example .env && 编辑)"
