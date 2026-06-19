#!/bin/bash
# =============================================================================
# whisper.sh — WhisperX 语音识别生成词级 JSON
#
# 用法:
#   ./whisper.sh <视频文件路径>
#
# 输出:
#   先提取音频 .wav → whisperx 识别 → 自动清理 .wav
#   同目录输出 <视频文件名>.json (词级时间码)
#
# 环境变量:
#   WHISPER_MODEL                  ASR 模型 (默认: large-v3-turbo)
#   WHISPER_ALIGN_MODEL            对齐模型 (默认: 空, 按语言自动匹配)
#   WHISPER_DEVICE                 推理设备: cuda | cpu (默认: 跟随 TORCH_BACKEND)
#   WHISPERX_PATH_LINUX            WhisperX 可执行文件路径 (默认: .venv/bin/whisperx)
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source <(tr -d '\r' < "$SCRIPT_DIR/.env") && set +a

if [ -n "${WHISPERX_PATH_LINUX:-}" ]; then
    WHISPERX_BIN="$WHISPERX_PATH_LINUX"
elif [ -x "$SCRIPT_DIR/.venv/bin/whisperx" ]; then
    WHISPERX_BIN="$SCRIPT_DIR/.venv/bin/whisperx"
else
    echo "错误: WhisperX venv executable not found. Run ./setup.sh first, or set WHISPERX_PATH_LINUX." >&2
    exit 1
fi
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "错误: Python venv not found. Run ./setup.sh first." >&2
    exit 1
fi

if [ -z "${1:-}" ]; then
    echo "用法: $0 <视频文件路径>" >&2
    exit 1
fi

VIDEO_PATH="$1"
if [ ! -f "$VIDEO_PATH" ]; then
    echo "错误: 视频文件不存在: $VIDEO_PATH" >&2
    exit 1
fi

VIDEO_DIR="$(dirname "$VIDEO_PATH")"
VIDEO_NAME="$(basename "$VIDEO_PATH")"
JSON_NAME="${VIDEO_NAME%.*}.json"

# 已存在则跳过
if [ -f "$VIDEO_DIR/$JSON_NAME" ]; then
    echo "JSON 已存在, 跳过: $VIDEO_DIR/$JSON_NAME"
    echo "OUTPUT_JSON=$VIDEO_DIR/$JSON_NAME"
    exit 0
fi

# 从 .info.json 读取视频语言
VIDEO_LANG="en"
VIDEO_BASE="${VIDEO_NAME%.*}"
INFO_JSON="$VIDEO_DIR/$VIDEO_BASE.info.json"
if [ -f "$INFO_JSON" ]; then
    LANG=$("$PYTHON_BIN" -c "
import json
with open('$INFO_JSON') as f:
    info = json.load(f)
lang = info.get('language') or ''
if lang:
    lang = lang.split('-')[0].lower()
print(lang if lang else 'en')
" 2>/dev/null)
    [ -n "$LANG" ] && VIDEO_LANG="$LANG"
fi

if [ -n "${WHISPER_DEVICE:-}" ]; then
    DEVICE="$WHISPER_DEVICE"
elif [ "${TORCH_BACKEND:-auto}" = "cpu" ]; then
    DEVICE="cpu"
elif [ "${TORCH_BACKEND:-auto}" = "cuda128" ]; then
    DEVICE="cuda"
elif command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
    DEVICE="cuda"
else
    DEVICE="cpu"
fi

# 提取音频为 WAV (避免长视频时间码漂移)
WAV_NAME="${VIDEO_NAME%.*}.wav"
echo "============================================="
echo "whisper — 语音识别 → .json"
echo "============================================="
echo "视频:      $VIDEO_PATH"
echo "语言:      $VIDEO_LANG"
echo "模型:      ${WHISPER_MODEL:-large-v3-turbo}"
echo "设备:      $DEVICE"
[ -n "${WHISPER_ALIGN_MODEL:-}" ] && echo "对齐:      $WHISPER_ALIGN_MODEL"
echo "============================================="

cd "$VIDEO_DIR"
echo "提取音频..."
ffmpeg -i "$VIDEO_NAME" -vn -acodec pcm_s16le -ar 16000 -ac 1 "$WAV_NAME" -y -loglevel error

WHISPER_ARGS=(
    "$WAV_NAME"
    --model "${WHISPER_MODEL:-large-v3-turbo}"
    --language "$VIDEO_LANG"
    --output_dir .
    --output_format json
    --device "$DEVICE"
)
[ -n "${WHISPER_ALIGN_MODEL:-}" ] && WHISPER_ARGS+=(--align_model "$WHISPER_ALIGN_MODEL")
TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 "$WHISPERX_BIN" "${WHISPER_ARGS[@]}"
rm -f "$WAV_NAME"

echo "============================================="
echo "whisper — 完成: $VIDEO_DIR/$JSON_NAME"
echo "============================================="
echo "OUTPUT_JSON=$VIDEO_DIR/$JSON_NAME"
