#!/bin/bash
# =============================================================================
# setup.sh — 安装字幕流水线全部依赖 (Linux / WSL)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source <(tr -d '\r' < "$SCRIPT_DIR/.env") && set +a

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

# ── Python venv ────────────────────────────────────────────────────────────
echo ">>> 创建/复用项目 .venv..."
cd "$SCRIPT_DIR"
uv venv .venv --python 3.13.12

# ── PyTorch backend ────────────────────────────────────────────────────────
TORCH_BACKEND="${TORCH_BACKEND:-auto}"
case "$TORCH_BACKEND" in
    auto)
        if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
            TORCH_BACKEND="cuda128"
            TORCH_REASON="detected NVIDIA GPU"
        else
            TORCH_BACKEND="cpu"
            TORCH_REASON="nvidia-smi not available"
        fi
        ;;
    cuda128)
        TORCH_REASON="configured"
        ;;
    cpu)
        TORCH_REASON="configured"
        ;;
    *)
        echo "Error: TORCH_BACKEND must be auto, cuda128, or cpu (got: $TORCH_BACKEND)" >&2
        exit 1
        ;;
esac

# ── Python packages ────────────────────────────────────────────────────────
echo ">>> 同步 pyproject.toml Python packages (asr)..."
if ! uv sync --inexact --extra asr; then
    echo "Error: uv sync failed." >&2
    exit 1
fi

echo ">>> 安装 PyTorch backend: $TORCH_BACKEND ($TORCH_REASON)"
if [ "$TORCH_BACKEND" = "cuda128" ]; then
    uv pip install --python "$SCRIPT_DIR/.venv/bin/python" \
        torch==2.8.0+cu128 torchaudio==2.8.0+cu128 \
        --index-url https://download.pytorch.org/whl/cu128
else
    uv pip install --python "$SCRIPT_DIR/.venv/bin/python" \
        torch==2.8.0 torchaudio==2.8.0 \
        --index-url https://download.pytorch.org/whl/cpu
fi

# ── 验证 ────────────────────────────────────────────────────────────────────
echo ""
echo "============================================="
echo "验证安装"
echo "============================================="
"$SCRIPT_DIR/.venv/bin/python" -c "import openai, langcodes; from tavily import TavilyClient; print('  openai/langcodes/tavily: OK')"
"$SCRIPT_DIR/.venv/bin/whisperx" --version 2>&1 | head -1 | sed 's/^/  whisperx: /' || echo "  whisperx: installed"
echo "  yt-dlp $(yt-dlp --version 2>&1 | head -1)"
echo "  ffmpeg $(ffmpeg -version 2>&1 | head -1 | cut -d' ' -f3)"
echo "  node $(node --version)"
echo "============================================="
echo "setup — 完成!"
echo "============================================="
echo ""
echo "Next: 配置 .env (cp .env.example .env && 编辑)"
