#!/bin/bash
# =============================================================================
# whisper.sh — WhisperX 语音识别生成英文字幕 (.srt)
#
# 用法:
#   ./whisper.sh <视频文件路径>
#
# 输出:
#   同目录输出 <视频文件名>.srt
#
# 环境变量:
#   WHISPER_MODEL                  ASR 模型 (默认: large-v3-turbo)
#   WHISPER_ALIGN_MODEL            对齐模型 (默认: 空, 按语言自动匹配)
#   WHISPER_DEVICE                 推理设备: cuda | cpu (默认: cuda, 自动检测)
#   WHISPER_SEGMENT_RESOLUTION     分割粒度: sentence | chunk (默认: sentence)
#   WHISPER_MAX_LINE_WIDTH         每行最大字符数 (默认: 42, 需要 alignment)
#   WHISPER_MAX_LINE_COUNT         每段最大行数 (默认: 2, 需要 alignment)
#   WHISPER_CHUNK_SIZE             处理块大小秒 (默认: 15, 原始: 30)
#   WHISPER_VAD_ONSET              VAD 语音起始阈值 (默认: 0.5)
#   WHISPER_VAD_OFFSET             VAD 语音结束阈值 (默认: 0.363)
#   WHISPER_CONDITION_ON_PREVIOUS  是否用前文做 prompt (默认: False)
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source <(tr -d '\r' < "$SCRIPT_DIR/.env") && set +a

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
SRT_NAME="${VIDEO_NAME%.*}.srt"

# 已存在则跳过
if [ -f "$VIDEO_DIR/$SRT_NAME" ]; then
    echo "字幕已存在, 跳过: $VIDEO_DIR/$SRT_NAME"
    exit 0
fi

# 从 .info.json 读取视频语言
VIDEO_LANG="en"
VIDEO_BASE="${VIDEO_NAME%.*}"
INFO_JSON="$VIDEO_DIR/$VIDEO_BASE.info.json"
if [ -f "$INFO_JSON" ]; then
    LANG=$(python -c "
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

# 分割参数默认值
DEVICE="${WHISPER_DEVICE:-cuda}"
SEGMENT_RESOLUTION="${WHISPER_SEGMENT_RESOLUTION:-sentence}"
MAX_LINE_WIDTH="${WHISPER_MAX_LINE_WIDTH:-42}"
MAX_LINE_COUNT="${WHISPER_MAX_LINE_COUNT:-2}"
CHUNK_SIZE="${WHISPER_CHUNK_SIZE:-15}"
VAD_ONSET="${WHISPER_VAD_ONSET:-0.5}"
VAD_OFFSET="${WHISPER_VAD_OFFSET:-0.363}"
CONDITION_ON_PREVIOUS="${WHISPER_CONDITION_ON_PREVIOUS:-False}"

echo "============================================="
echo "whisper — 语音识别 → .srt"
echo "============================================="
echo "视频:      $VIDEO_PATH"
echo "语言:      $VIDEO_LANG"
echo "模型:      ${WHISPER_MODEL:-large-v3-turbo}"
echo "设备:      $DEVICE"
echo "分割粒度:  $SEGMENT_RESOLUTION"
echo "行宽限制:  $MAX_LINE_WIDTH 字符/行"
echo "行数限制:  $MAX_LINE_COUNT 行/段"
echo "块大小:    ${CHUNK_SIZE}s"
[ -n "${WHISPER_ALIGN_MODEL:-}" ] && echo "对齐:      $WHISPER_ALIGN_MODEL"
echo "============================================="

cd "$VIDEO_DIR"
WHISPER_ARGS=(
    "$VIDEO_NAME"
    --model "${WHISPER_MODEL:-large-v3-turbo}"
    --language "$VIDEO_LANG"
    --output_dir .
    --output_format srt
    --device "$DEVICE"
    --segment_resolution "$SEGMENT_RESOLUTION"
    --chunk_size "$CHUNK_SIZE"
    --vad_onset "$VAD_ONSET"
    --vad_offset "$VAD_OFFSET"
    --condition_on_previous_text "$CONDITION_ON_PREVIOUS"
)
# 行宽/行数限制仅在 alignment 启用时有效 (默认启用)
[ -n "$MAX_LINE_WIDTH" ] && WHISPER_ARGS+=(--max_line_width "$MAX_LINE_WIDTH")
[ -n "$MAX_LINE_COUNT" ] && WHISPER_ARGS+=(--max_line_count "$MAX_LINE_COUNT")
[ -n "${WHISPER_ALIGN_MODEL:-}" ] && WHISPER_ARGS+=(--align_model "$WHISPER_ALIGN_MODEL")
uvx whisperx "${WHISPER_ARGS[@]}"

echo "============================================="
echo "whisper — 完成: $VIDEO_DIR/$SRT_NAME"
echo "============================================="
