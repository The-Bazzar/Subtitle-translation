#!/bin/bash
# =============================================================================
# beautify_srt.sh — 美化 SRT 字幕时间码
#
# 用 ffmpeg/ffprobe 检测视频场景切换和关键帧，
# 将字幕起止时间吸附对齐，类似 Subtitle Edit 的 "Beautify timecodes" 功能。
#
# 用法:
#   ./beautify_srt.sh <视频文件> [SRT文件] [选项...]
#
# 示例:
#   ./beautify_srt.sh video.mp4                     # 自动查找 .srt → .beautified.srt
#   ./beautify_srt.sh video.mp4 subtitle.srt        # 指定字幕 → .beautified.srt
#   ./beautify_srt.sh video.mp4 -o result.srt       # 输出到指定文件
#   ./beautify_srt.sh video.mp4 -o video.srt        # 覆盖原文件 (显式指定)
#   ./beautify_srt.sh video.mp4 --preview           # 仅预览
#   ./beautify_srt.sh video.mp4 --backup            # 备份原文件后覆盖 (需 -o)
#
# 依赖: python3, ffmpeg, ffprobe
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/beautify_srt.py"

# ── 帮助 ──────────────────────────────────────────────────────────────────────

show_help() {
    cat << 'EOF'
beautify_srt.sh — 美化 SRT 字幕时间码

用法:
  ./beautify_srt.sh <视频文件> [SRT文件] [选项...]

说明:
  用 ffmpeg 检测视频场景切换、ffprobe 提取关键帧，
  将 SRT 字幕的起止时间吸附对齐到最近的场景切换点和关键帧，
  自动修复字幕重叠和过小间隙，确保最短/最长时长。

示例:
  ./beautify_srt.sh video.mp4                     # 自动查找同目录 .srt → .beautified.srt
  ./beautify_srt.sh video.mp4 subtitle.srt        # 指定字幕 → .beautified.srt
  ./beautify_srt.sh video.mp4 -o result.srt       # 输出到指定文件
  ./beautify_srt.sh video.mp4 -o video.srt        # 覆盖原文件 (显式指定 -o)
  ./beautify_srt.sh video.mp4 --preview           # 仅预览变化 (不写入)
  ./beautify_srt.sh video.mp4 --backup            # 覆盖前备份原文件为 .bak

常用选项 (默认值遵循 Netflix 字幕规范):
  --scene-threshold N           场景检测灵敏度 (0.1-0.5, 默认 0.25)
  --snap-frames N               吸附到场景切换的最大帧数 (默认 7)
  --end-offset-frames N         出点对齐到场景切换前 N 帧 (默认 2)
  --min-scene-interval-frames N 场景切换最小帧间隔 (默认 7)
  --min-duration N              最短字幕时长秒 (默认 1.0, Netflix: 1000ms)
  --max-duration N              最长字幕时长秒 (默认 8.0, Netflix: 8000ms)
  --min-gap N                   字幕最小间距秒 (默认 0.083, Netflix: 2帧)
  --max-gap-merge N             小于此值的间隙合并秒 (默认 0.5, Netflix: 500ms)
  --use-keyframes               启用关键帧吸附 (默认关闭)
  --extend                      延伸字幕填充间隙 (默认不启用)
  --no-scene-snap               完全跳过场景吸附
  --preview                     仅预览变化 (不写入)
  --backup                      覆盖前备份为 .bak

实战推荐 (激进对齐, 剪辑密集的视频):
  ./beautify_srt.sh video.mp4 --scene-threshold 0.2 --snap-frames 10

实战推荐 (保守对齐, 长镜头视频):
  ./beautify_srt.sh video.mp4 --scene-threshold 0.35 --snap-frames 4

依赖:
  python3, ffmpeg, ffprobe (均已包含在 WSL Ubuntu 中)
EOF
    exit 0
}

# ── 检查参数 ──────────────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    show_help
fi

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    show_help
fi

# ── 检查依赖 ──────────────────────────────────────────────────────────────────

check_dep() {
    if ! command -v "$1" &>/dev/null; then
        echo "Error: $1 not found. Please install it first." >&2
        exit 1
    fi
}

check_dep python3
check_dep ffmpeg
check_dep ffprobe

if [ ! -f "$PYTHON_SCRIPT" ]; then
    echo "Error: Python script not found: $PYTHON_SCRIPT" >&2
    exit 1
fi

# ── 运行 ──────────────────────────────────────────────────────────────────────

echo "============================================="
echo "beautify_srt — 字幕时间码美化"
echo "============================================="

python3 "$PYTHON_SCRIPT" "$@"

echo "============================================="
echo "Done!"
echo "============================================="
