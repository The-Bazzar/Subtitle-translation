#!/bin/bash
# =============================================================================
# ffmpeg-burn.sh — Linux 字幕硬压脚本 (ffmpeg 滤镜)
#
# 用法:
#   ./ffmpeg-burn.sh <视频文件> [选项...] [-- ffmpeg额外参数...]
#
# 示例:
#   ./ffmpeg-burn.sh video.webm --sub-file video.en-zh.ass
#   ./ffmpeg-burn.sh video.webm --sub-file sub.ass -o result.mkv --ovc libx265 --ovcopts crf=23
#
# 相比 mpv-burn: 保留原视频封面图, 无需 Windows mpv.com
# =============================================================================

set -euo pipefail

# ── 默认值 ──────────────────────────────────────────────────────────────────────

OUTPUT=""
SUB_FILE=""
OVC="hevc_nvenc"
OVCOPTS="source-bitrate"
OAC="aac"
RES=""
# 从 .env 读取配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source <(tr -d '\r' < "$SCRIPT_DIR/.env") && set +a
FFMPEG="${FFMPEG_PATH_LINUX:-ffmpeg}"

# ── 帮助 ──────────────────────────────────────────────────────────────────────

show_help() {
    cat << 'EOF'
ffmpeg-burn.sh — Linux 字幕硬压脚本 (ffmpeg 滤镜)

用法:
  ./ffmpeg-burn.sh <视频文件> [选项...] [-- ffmpeg额外参数...]

说明:
  使用 ffmpeg 的 ass 滤镜将 ASS 字幕硬压到视频中。
  相比 mpv-burn: 保留原视频封面图, 无需 Windows mpv.com。

选项:
  -o, --output PATH       输出文件路径 (默认: 输入同目录 burned.mkv)
  --sub-file PATH         字幕文件路径 (如 .en-zh.ass 双语 ASS)
  --ovc CODEC             视频编码器 (默认: hevc_nvenc)
  --ovcopts OPTS          视频编码器参数 (默认: source-bitrate, 自动接近源视频码率)
  --oac CODEC             音频编码器 (默认: aac)
  --res WxH               输出分辨率 (如 1920x1080, 默认: 原视频)
  --dry-run               仅打印命令, 不执行
  -h, --help              显示帮助

示例:
  ./ffmpeg-burn.sh video.webm --sub-file video.en-zh.ass
  ./ffmpeg-burn.sh video.webm --sub-file sub.ass -o result.mkv
  ./ffmpeg-burn.sh video.webm --ovcopts source-bitrate
  ./ffmpeg-burn.sh video.webm --ovc libx265 --ovcopts crf=23
  ./ffmpeg-burn.sh video.webm --dry-run

常用编码器:
  hevc_nvenc    NVIDIA GPU H.265 硬编码 (默认, 速度快)
  libx265       CPU H.265 软编码 (体积最小)
  libx264       CPU H.264 软编码 (兼容性最好)
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

EXTRA_FFMPEG_ARGS=()
PASSTHROUGH=false
DRY_RUN=false

while [ $# -gt 0 ]; do
    if [ "$PASSTHROUGH" = true ]; then
        EXTRA_FFMPEG_ARGS+=("$1")
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
            EXTRA_FFMPEG_ARGS+=("$1")
            shift
            ;;
    esac
done

# ── 验证 ──────────────────────────────────────────────────────────────────────

if [ ! -f "$VIDEO" ]; then
    echo "Error: Video file not found: $VIDEO" >&2
    exit 1
fi

if ! command -v "$FFMPEG" &>/dev/null; then
    echo "Error: ffmpeg not found: $FFMPEG" >&2
    exit 1
fi

# 转为绝对路径
VIDEO_ABS="$(realpath "$VIDEO" 2>/dev/null || readlink -f "$VIDEO")"

# 确定输出路径
if [ -z "$OUTPUT" ]; then
    VIDEO_DIR="$(dirname "$VIDEO_ABS")"
    OUTPUT="$VIDEO_DIR/burned.mkv"
fi
OUTPUT_ABS="$(realpath "$OUTPUT" 2>/dev/null || echo "$OUTPUT")"

# 字幕文件绝对路径
if [ -n "$SUB_FILE" ]; then
    SUB_FILE_ABS="$(realpath "$SUB_FILE" 2>/dev/null || readlink -f "$SUB_FILE")"
fi

resolve_ffprobe() {
    if [ -n "${FFPROBE_PATH_LINUX:-}" ]; then
        echo "$FFPROBE_PATH_LINUX"
        return
    fi
    if [ "$FFMPEG" != "ffmpeg" ]; then
        local dir
        dir="$(dirname "$FFMPEG")"
        if [ -x "$dir/ffprobe" ]; then
            echo "$dir/ffprobe"
            return
        fi
        if [ -x "$dir/ffprobe.exe" ]; then
            echo "$dir/ffprobe.exe"
            return
        fi
    fi
    echo "ffprobe"
}

is_source_bitrate_ovcopts() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        auto|source|source-bitrate|source_bitrate|match-source) return 0 ;;
        *) return 1 ;;
    esac
}

probe_first_number() {
    "$FFPROBE" "$@" 2>/dev/null | awk 'NF && $1 != "N/A" { print $1; exit }'
}

source_video_bitrate_kbps() {
    local stream_bps format_bps duration size
    stream_bps="$(probe_first_number -v error -select_streams v:0 -show_entries stream=bit_rate -of default=noprint_wrappers=1:nokey=1 "$VIDEO_ABS" || true)"
    if [[ "$stream_bps" =~ ^[0-9]+$ ]] && [ "$stream_bps" -gt 0 ]; then
        awk -v bps="$stream_bps" 'BEGIN { printf "%d\n", int((bps + 999) / 1000) }'
        return 0
    fi

    format_bps="$(probe_first_number -v error -show_entries format=bit_rate -of default=noprint_wrappers=1:nokey=1 "$VIDEO_ABS" || true)"
    if [[ "$format_bps" =~ ^[0-9]+$ ]] && [ "$format_bps" -gt 0 ]; then
        awk -v bps="$format_bps" 'BEGIN { printf "%d\n", int((bps + 999) / 1000) }'
        return 0
    fi

    duration="$(probe_first_number -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$VIDEO_ABS" || true)"
    size="$(stat -c%s "$VIDEO_ABS" 2>/dev/null || wc -c < "$VIDEO_ABS")"
    if [[ "$duration" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v d="$duration" 'BEGIN { exit !(d > 0) }'; then
        awk -v bytes="$size" -v seconds="$duration" 'BEGIN { printf "%d\n", int(((bytes * 8 / seconds) + 999) / 1000) }'
        return 0
    fi
    return 1
}

source_bitrate_ovcopts() {
    local kbps maxrate bufsize prefix encoder
    kbps="$(source_video_bitrate_kbps)" || return 1
    [ -n "$kbps" ] && [ "$kbps" -gt 0 ] || return 1
    maxrate=$(( (kbps * 125 + 99) / 100 ))
    bufsize=$(( kbps * 2 ))
    encoder="$(printf '%s' "$OVC" | tr '[:upper:]' '[:lower:]')"
    prefix=""
    case "$encoder" in
        *nvenc*) prefix="rc=vbr," ;;
    esac
    echo "${prefix}b=${kbps}k,maxrate=${maxrate}k,bufsize=${bufsize}k"
}

ffmpeg_ovcopts_to_args() {
    local opts="$1"
    local part key val
    IFS=',' read -ra parts <<< "$opts"
    for part in "${parts[@]}"; do
        part="${part#"${part%%[![:space:]]*}"}"
        part="${part%"${part##*[![:space:]]}"}"
        [ -n "$part" ] || continue
        if [[ "$part" == *"="* ]]; then
            key="${part%%=*}"
            val="${part#*=}"
            case "$key" in
                b|b:v) FFMPEG_VIDEO_ARGS+=(-b:v "$val") ;;
                *) FFMPEG_VIDEO_ARGS+=("-$key" "$val") ;;
            esac
        else
            FFMPEG_VIDEO_ARGS+=(-qp "$part")
        fi
    done
}

FFPROBE="$(resolve_ffprobe)"
RESOLVED_OVCOPTS="$OVCOPTS"
SOURCE_BITRATE_KBPS=""
if is_source_bitrate_ovcopts "$OVCOPTS"; then
    if AUTO_OVCOPTS="$(source_bitrate_ovcopts)"; then
        RESOLVED_OVCOPTS="$AUTO_OVCOPTS"
        SOURCE_BITRATE_KBPS="${AUTO_OVCOPTS#*b=}"
        SOURCE_BITRATE_KBPS="${SOURCE_BITRATE_KBPS%%k,*}"
    else
        echo "Warning: failed to probe source bitrate with ffprobe; fallback to qp=20." >&2
        RESOLVED_OVCOPTS="qp=20"
    fi
fi
FFMPEG_VIDEO_ARGS=(-c:v "$OVC")
ffmpeg_ovcopts_to_args "$RESOLVED_OVCOPTS"

# ── 执行 ──────────────────────────────────────────────────────────────────────

echo "============================================="
echo "ffmpeg-burn — 字幕硬压"
echo "============================================="
echo "输入:    $VIDEO_ABS"
echo "输出:    $OUTPUT_ABS"
if [ -n "$SUB_FILE" ]; then
    echo "字幕:    $SUB_FILE_ABS"
fi
[ -n "$RES" ] && echo "分辨率:  $RES"
echo "视频:    ${FFMPEG_VIDEO_ARGS[*]}"
[ -n "$SOURCE_BITRATE_KBPS" ] && echo "码率:    source-bitrate -> ${SOURCE_BITRATE_KBPS}k"
echo "音频:    -c:a $OAC"
if [ ${#EXTRA_FFMPEG_ARGS[@]} -gt 0 ]; then
    echo "额外:    ${EXTRA_FFMPEG_ARGS[*]}"
fi
echo "============================================="

# 构建滤镜链: ass + 可选 scale
VF="ass='${SUB_FILE_ABS//:/\\:}'"
if [ -n "$RES" ]; then
    VF="${VF},scale=${RES}:force_original_aspect_ratio=decrease,pad=${RES}:(ow-iw)/2:(oh-ih)/2"
fi

# 组装 ffmpeg 命令
FFMPEG_CMD=(
    "$FFMPEG"
    -i "$VIDEO_ABS"
    -vf "$VF"
    "${FFMPEG_VIDEO_ARGS[@]}"
    -c:a "$OAC"
    -map 0:v:0?
    -map 0:a:0?
    -map 0:v:1?
    -map_metadata 0
    -disposition:v:1 attached_pic
    -movflags +faststart
	-y
)

if [ -n "$SUB_FILE" ]; then
    FFMPEG_CMD+=(-map 0:s?)
fi

FFMPEG_CMD+=("${EXTRA_FFMPEG_ARGS[@]}")
FFMPEG_CMD+=("$OUTPUT_ABS")

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "[DRY RUN] 将执行的命令:"
    echo "${FFMPEG_CMD[*]}"
    exit 0
fi

echo ""
echo "正在压制字幕..."

"${FFMPEG_CMD[@]}"
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo ""
    echo "============================================="
    echo "硬字幕压制完成!"
    echo "输出: $OUTPUT_ABS"
    echo "============================================="
else
    echo ""
    echo "Error: ffmpeg encoding failed (exit code: $EXIT_CODE)" >&2
    exit $EXIT_CODE
fi
