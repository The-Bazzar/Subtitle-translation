#!/bin/bash
# =============================================================================
# setup.sh — 安装字幕流水线全部依赖 (Linux / WSL)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

copy_config_if_missing() {
    local example_name="$1"
    local target_name="$2"
    local example_path="$SCRIPT_DIR/$example_name"
    local target_path="$SCRIPT_DIR/$target_name"
    if [ -f "$example_path" ] && [ ! -f "$target_path" ]; then
        cp "$example_path" "$target_path"
        echo "  created $target_name from $example_name"
    fi
}

update_env_from_example() {
    local example_path="$SCRIPT_DIR/.env.example"
    local env_path="$SCRIPT_DIR/.env"
    [ -f "$example_path" ] || return 0
    if [ ! -f "$env_path" ]; then
        cp "$example_path" "$env_path"
        echo "  created .env from .env.example"
        return 0
    fi

    local tmp
    tmp="$(mktemp)"
    awk -v env_path="$env_path" '
        BEGIN {
            while ((getline line < env_path) > 0) {
                if (match(line, /^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=/, m)) {
                    existing[m[1]] = 1
                }
            }
            close(env_path)
            pending_count = 0
        }
        /^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=/ {
            key = $0
            sub(/^[[:space:]]*/, "", key)
            sub(/[[:space:]]*=.*/, "", key)
            if (!(key in existing)) {
                for (i = 1; i <= pending_count; i++) {
                    print pending[i]
                }
                pending_count = 0
                print $0
                existing[key] = 1
            } else {
                pending_count = 0
            }
            next
        }
        /^[[:space:]]*($|#)/ {
            pending[++pending_count] = $0
            next
        }
        {
            pending_count = 0
        }
    ' "$example_path" > "$tmp"

    if [ -s "$tmp" ]; then
        {
            echo ""
            echo "# Added by setup from .env.example"
            cat "$tmp"
        } >> "$env_path"
        echo "  updated .env with missing template variables"
    else
        echo "  .env: up to date"
    fi
    rm -f "$tmp"
}

echo ">>> 准备本地配置文件..."
update_env_from_example
copy_config_if_missing "providers.example.json" "providers.json"
copy_config_if_missing "tavily_domains.example.json" "tavily_domains.json"
copy_config_if_missing "glossary_prompt.example.md" "glossary_prompt.md"
copy_config_if_missing "translate_prompt.example.md" "translate_prompt.md"
copy_config_if_missing "proofread_prompt.example.md" "proofread_prompt.md"
copy_config_if_missing "split_prompt.example.md" "split_prompt.md"

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
echo "Next: 编辑 .env，填入 API keys 和本机偏好配置"
