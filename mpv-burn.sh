#!/bin/bash
# =============================================================================
# mpv-burn.sh — WSL 字幕硬压脚本 (mpv 编码模式)
#
# 用法:
#   ./mpv-burn.sh <视频文件> [选项...] [-- mpv额外参数...]
#
# 示例:
#   ./mpv-burn.sh video.webm
#   ./mpv-burn.sh video.webm -o result.mkv
#   ./mpv-burn.sh video.webm --sub-file video.zh-en.ass --ovc libx265 --ovcopts crf=23
#   ./mpv-burn.sh video.webm -- --vf-append=vapoursynth="~~/vs/MEMC_RIFE_NV.vpy"
#
# 环境变量:
#   MPV_PATH — mpv.com 路径 (默认: /mnt/c/Users/oculi/mpv-lazy/mpv.com)
# =============================================================================

set -euo pipefail

# ── 默认值 ──────────────────────────────────────────────────────────────────────

# 从 .env 读取配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source <(tr -d '\r' < "$SCRIPT_DIR/.env") && set +a
MPV="${MPV_PATH_LINUX:-mpv}"

OUTPUT=""
SUB_FILE=""
OVC="hevc_nvenc"
OVCOPTS="qp=20"
OAC="aac"
RES=""

# ── 帮助 ──────────────────────────────────────────────────────────────────────

show_help() {
    cat << 'EOF'
mpv-burn.sh — WSL 字幕硬压脚本 (mpv 编码模式)

用法:
  ./mpv-burn.sh <视频文件> [选项...] [-- mpv额外参数...]

说明:
  使用 mpv 的 --o= 编码模式将字幕硬压到视频中。
  mpv.com 是 Windows 可执行文件，在 WSL 中通过 /mnt/c/... 路径调用。

选项:
  -o, --output PATH       输出文件路径 (默认: 输入同目录 burned.mkv)
  --sub-file PATH         字幕文件路径 (如 .zh-en.ass 双语字幕)
  --ovc CODEC             视频编码器 (默认: hevc_nvenc)
  --ovcopts OPTS          视频编码器参数 (默认: qp=20)
  --res WxH               输出分辨率 (如 1920x1080, 保持宽高比加黑边)
  --oac CODEC             音频编码器 (默认: aac)
  --dry-run               仅打印命令, 不执行
  -h, --help              显示帮助

示例:
  ./mpv-burn.sh video.webm --sub-file video.zh-en.ass
  ./mpv-burn.sh video.webm --sub-file sub.ass -o burned.mkv
  ./mpv-burn.sh video.webm --ovc libx265 --ovcopts crf=23
  ./mpv-burn.sh video.webm -- --vf-append=vapoursynth="~~/vs/MEMC_RIFE_NV.vpy"

环境变量:


常用编码器:
  hevc_nvenc              NVIDIA GPU H.265 硬编码 (默认, 速度快)
  libx265                 CPU H.265 软编码 (体积最小)
  libx264                 CPU H.264 软编码 (兼容性最好)
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

VIDEO="$1"
shift

EXTRA_MPV_ARGS=()
PASSTHROUGH=false
DRY_RUN=false

while [ $# -gt 0 ]; do
    if [ "$PASSTHROUGH" = true ]; then
        EXTRA_MPV_ARGS+=("$1")
        shift
        continue
    fi

    case "$1" in
        --)
            PASSTHROUGH=true
            shift
            ;;
        -h|--help)
            show_help
            ;;
        -o|--output)
            OUTPUT="$2"
            shift 2
            ;;
        --sub-file)
            SUB_FILE="$2"
            shift 2
            ;;
        --ovc)
            OVC="$2"
            shift 2
            ;;
        --ovcopts)
            OVCOPTS="$2"
            shift 2
            ;;
        --oac)
            OAC="$2"
            shift 2
            ;;
        --res)
            RES="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            # 未识别的参数透传给 mpv
            EXTRA_MPV_ARGS+=("$1")
            shift
            ;;
    esac
done

# ── 验证 ──────────────────────────────────────────────────────────────────────

if [ ! -f "$VIDEO" ]; then
    echo "Error: Video file not found: $VIDEO" >&2
    exit 1
fi

# 转为绝对路径
VIDEO_ABS="$(realpath "$VIDEO" 2>/dev/null || readlink -f "$VIDEO")"

if [ ! -f "$MPV" ]; then
    echo "Error: mpv.com not found: $MPV" >&2
    echo "Set MPV_PATH_LINUX in .env." >&2
    exit 1
fi

# 确定输出路径
if [ -z "$OUTPUT" ]; then
    VIDEO_DIR="$(dirname "$VIDEO_ABS")"
    OUTPUT="$VIDEO_DIR/burned.mkv"
fi
OUTPUT_ABS="$(realpath "$OUTPUT" 2>/dev/null || echo "$OUTPUT")"

# WSL → Windows 路径转换 (mpv.com 是 Windows 可执行文件, 不识别 /mnt/c/...)
if command -v wslpath &>/dev/null; then
    VIDEO_WIN="$(wslpath -w "$VIDEO_ABS")"
    OUTPUT_WIN="$(wslpath -w "$OUTPUT_ABS")"
else
    # 手动回退: /mnt/c/... → C:\...
    VIDEO_WIN="$(echo "$VIDEO_ABS" | sed 's|^/mnt/\([a-zA-Z]\)/|\1:\\|; s|/|\\|g')"
    OUTPUT_WIN="$(echo "$OUTPUT_ABS" | sed 's|^/mnt/\([a-zA-Z]\)/|\1:\\|; s|/|\\|g')"
fi

# 字幕文件路径也需 WSL → Windows 转换
if [ -n "$SUB_FILE" ]; then
    SUB_FILE_ABS="$(realpath "$SUB_FILE" 2>/dev/null || readlink -f "$SUB_FILE")"
    if command -v wslpath &>/dev/null; then
        SUB_FILE_WIN="$(wslpath -w "$SUB_FILE_ABS")"
    else
        SUB_FILE_WIN="$(echo "$SUB_FILE_ABS" | sed 's|^/mnt/\([a-zA-Z]\)/|\1:\\|; s|/|\\|g')"
    fi
fi

# ── 执行 ──────────────────────────────────────────────────────────────────────

echo "============================================="
echo "mpv-burn — WSL 字幕硬压"
echo "============================================="
echo "mpv:     $MPV"
echo "输入:    $VIDEO_ABS"
echo "       → $VIDEO_WIN"
echo "输出:    $OUTPUT_ABS"
echo "       → $OUTPUT_WIN"
if [ -n "$SUB_FILE" ]; then
    echo "字幕:    --sub-file=$SUB_FILE_ABS"
    echo "       → $SUB_FILE_WIN"
fi
[ -n "$RES" ] && echo "分辨率:  $RES (保持宽高比+黑边)"
echo "视频:    --ovc=$OVC --ovcopts=$OVCOPTS"
echo "音频:    --oac=$OAC"
if [ ${#EXTRA_MPV_ARGS[@]} -gt 0 ]; then
    echo "额外:    ${EXTRA_MPV_ARGS[*]}"
fi
echo "============================================="

# 组装 mpv 命令 (传入 Windows 路径 — mpv.com 是 Windows 可执行文件)
MPV_CMD=(
    "$MPV"
    "$VIDEO_WIN"
    "--o=$OUTPUT_WIN"
    "--ovc=$OVC"
    "--ovcopts=$OVCOPTS"
    "--oac=$OAC"
    "${EXTRA_MPV_ARGS[@]}"
)

# 分辨率缩放: 保持宽高比 + 黑边填充
if [ -n "$RES" ]; then
    MPV_CMD+=("--vf-add=lavfi=[scale=$RES:force_original_aspect_ratio=decrease,pad=$RES:(ow-iw)/2:(oh-ih)/2]")
fi

# 仅当指定字幕文件时添加 --sub-file
if [ -n "$SUB_FILE" ]; then
    MPV_CMD+=("--sub-file=$SUB_FILE_WIN")
fi

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "[DRY RUN] 将执行的命令:"
    echo "${MPV_CMD[*]}"
    exit 0
fi

echo ""
echo "正在压制字幕..."

"${MPV_CMD[@]}"
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo ""
    echo "============================================="
    echo "硬字幕压制完成!"
    echo "输出: $OUTPUT_ABS"
    echo "============================================="
else
    echo ""
    echo "Error: mpv encoding failed (exit code: $EXIT_CODE)" >&2
    exit $EXIT_CODE
fi
