#!/bin/bash
# =============================================================================
# pipeline.sh — 一键流水线: 下载视频 + 生成字幕 + 美化时间码
#
# 串联 download_and_sub.sh → beautify_srt.sh，
# 从 YouTube 链接直达美化后的 SRT 字幕。
#
# 用法:
#   ./pipeline.sh <YouTube URL> [-- <beautify选项>]
#
# 示例:
#   ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
#   ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --preview
#   ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --backup --scene-threshold 0.2
#   ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --extend
#
# 环境变量:
#   SKIP_BEAUTIFY=1    跳过字幕美化步骤
#   SKIP_DOWNLOAD=1    跳过下载步骤 (仅美化已有文件)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOAD_SCRIPT="$SCRIPT_DIR/download_and_sub.sh"
BEAUTIFY_SCRIPT="$SCRIPT_DIR/beautify_srt.sh"

# ── 帮助 ──────────────────────────────────────────────────────────────────────

show_help() {
    cat << 'EOF'
pipeline.sh — 一键流水线: 下载视频 + WhisperX 字幕 + 时间码美化

用法:
  ./pipeline.sh <YouTube URL> [-- <beautify选项>]

流程:
  1. yt-dlp 下载视频 + 元数据 (SponsorBlock 去广告)
  2. WhisperX large-v3 生成英文字幕
  3. ffmpeg/ffprobe 场景检测 + 关键帧提取
  4. 字幕时间码吸附对齐到场景切换 & 关键帧
  5. 修复重叠/间隙, 强制最小时长

示例:
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --preview
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --backup
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --scene-threshold 0.2 --snap-frames 10
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --extend --min-duration 1.5

beautify 选项 (在 -- 之后, 默认值遵循 Netflix 规范):
  --scene-threshold N           场景检测灵敏度 (默认 0.25)
  --snap-frames N               吸附到场景切换的最大帧数 (默认 7)
  --end-offset-frames N         出点对齐到场景前 N 帧 (默认 2)
  --min-scene-interval-frames N 场景切换最小帧间隔 (默认 7)
  --min-duration N              最短字幕时长秒 (默认 1.0, Netflix: 1000ms)
  --max-duration N              最长字幕时长秒 (默认 8.0, Netflix: 8000ms)
  --min-gap N                   字幕最小间距秒 (默认 0.083, Netflix: 2帧)
  --max-gap-merge N             小于此值的间隙合并秒 (默认 0.5, Netflix: 500ms)
  --use-keyframes               启用关键帧吸附 (默认关闭)
  --extend                      延伸字幕填充间隙 (默认不启用)
  --no-scene-snap               完全跳过场景吸附
  --preview                     仅预览, 不写入
  --backup                      覆盖前备份原文件

环境变量:
  SKIP_BEAUTIFY=1        跳过美化步骤
  SKIP_DOWNLOAD=1        跳过下载 (仅对已有视频做美化)
EOF
    exit 0
}

# ── 参数解析 ──────────────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    show_help
fi

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    show_help
fi

URL="$1"
shift

# 收集 -- 之后的 beautify 参数
BEAUTIFY_ARGS=()
PASSTHROUGH=false
for arg in "$@"; do
    if [ "$PASSTHROUGH" = true ]; then
        BEAUTIFY_ARGS+=("$arg")
    elif [ "$arg" = "--" ]; then
        PASSTHROUGH=true
    else
        echo "Warning: Unexpected argument '$arg' (use -- to pass beautify options)" >&2
    fi
done

# ── 步骤 1: 下载 + 字幕生成 ───────────────────────────────────────────────────

if [ "${SKIP_DOWNLOAD:-0}" = "1" ]; then
    echo "============================================="
    echo "SKIP_DOWNLOAD=1 — 跳过下载, 仅做美化"
    echo "============================================="
    echo ""
    echo "请提供视频文件路径:"
    read -r VIDEO_PATH
else
    echo "============================================="
    echo "pipeline — 步骤 1/2: 下载视频 + 生成字幕"
    echo "============================================="
    echo ""

    # 运行下载脚本, 捕获 OUTPUT_VIDEO 行
    DOWNLOAD_OUTPUT=$(bash "$DOWNLOAD_SCRIPT" "$URL" 2>&1) || {
        echo "$DOWNLOAD_OUTPUT"
        echo ""
        echo "Error: download_and_sub.sh failed." >&2
        exit 1
    }
    echo "$DOWNLOAD_OUTPUT"

    # 提取视频路径
    VIDEO_PATH=$(echo "$DOWNLOAD_OUTPUT" | grep '^OUTPUT_VIDEO=' | tail -1 | cut -d= -f2-)

    if [ -z "$VIDEO_PATH" ] || [ ! -f "$VIDEO_PATH" ]; then
        echo ""
        echo "Error: Failed to locate downloaded video file." >&2
        echo "Output line: $(echo "$DOWNLOAD_OUTPUT" | grep '^OUTPUT_VIDEO=' || echo '(none)')" >&2
        exit 1
    fi

    echo ""
fi

# ── 步骤 2: 字幕时间码美化 ─────────────────────────────────────────────────────

if [ "${SKIP_BEAUTIFY:-0}" = "1" ]; then
    echo "============================================="
    echo "SKIP_BEAUTIFY=1 — 跳过字幕美化"
    echo "============================================="
    echo ""
    echo "视频文件: $VIDEO_PATH"
    exit 0
fi

echo "============================================="
echo "pipeline — 步骤 2/2: 字幕时间码美化"
echo "============================================="
echo ""

bash "$BEAUTIFY_SCRIPT" "$VIDEO_PATH" "${BEAUTIFY_ARGS[@]}"

echo ""
echo "============================================="
echo "pipeline — 全部完成!"
echo "============================================="
