#!/bin/bash
# =============================================================================
# download.sh — 下载 YouTube 视频 + 元数据
#
# 用法:
#   ./download.sh <YouTube URL>
#
# 输出:
#   OUTPUT_VIDEO=<编辑视频绝对路径>
#   OUTPUT_RENDER_VIDEO=<原始渲染视频绝对路径>
#
# 环境变量:
#   YTDLP_PATH_LINUX  yt-dlp 路径 (默认: yt-dlp)
#   FFMPEG_PATH_LINUX ffmpeg 路径 (默认: ffmpeg)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source <(tr -d '\r' < "$SCRIPT_DIR/.env") && set +a
YTDLP="${YTDLP_PATH_LINUX:-yt-dlp}"
FFMPEG="${FFMPEG_PATH_LINUX:-ffmpeg}"
PYTHON_BIN="${PYTHON_PATH_LINUX:-$SCRIPT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "错误: Python venv not found. Run ./setup.sh first, or set PYTHON_PATH_LINUX." >&2
    exit 1
fi

if [ -z "${1:-}" ]; then
    echo "用法: $0 <YouTube URL>" >&2
    exit 1
fi

URL="$1"

resolve_abs_path() {
    realpath "$1" 2>/dev/null || readlink -f "$1" 2>/dev/null || echo "$PWD/$1"
}

print_native_cmd() {
    local label="$1"
    shift
    {
        printf '%s' "$label"
        printf ' %q' "$@"
        printf '\n'
    } >&2
}

ffmpeg_encoder_available() {
    "$FFMPEG" -hide_banner -encoders 2>/dev/null | grep -Fq "$1"
}

nvidia_available() {
    command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1
}

run_edit_reencode() {
    local input_path="$1"
    local output_path="$2"

    echo "============================================="
    echo "download — 步骤 4/4: 重编码生成编辑视频"
    echo "============================================="
    echo "原片: $input_path"
    echo "编辑: $output_path"
    echo "模式: 优先 h264_nvenc；不可用时回退 libx264；音频统一 aresample s16 + flac"

    rm -f "$output_path"

    run_reencode_attempt() {
        local label="$1"
        shift
        local -a video_args=("$@")
        local -a ffmpeg_cmd=(
            "$FFMPEG"
            -hide_banner
            -stats
            -i "$input_path"
            -pix_fmt yuv420p
            "${video_args[@]}"
            -filter_complex "[0:a]aresample=async=1:out_sample_fmt=s16[aout]"
            -map 0:v:0
            -map "[aout]"
            -c:a flac
            -map_metadata -1
            -movflags +faststart
            -y
            "$output_path"
        )

        rm -f "$output_path"
        echo "尝试: $label"
        print_native_cmd "ffmpeg cmd:" "${ffmpeg_cmd[@]}"
        if "${ffmpeg_cmd[@]}"; then
            return 0
        fi

        if [ "$label" = "h264_nvenc" ] && [ -s "$output_path" ]; then
            echo "Warning: h264_nvenc 返回非零退出码，但已输出非 0B 文件，继续使用该文件" >&2
            return 0
        fi

        rm -f "$output_path"
        echo "Warning: $label 重编码失败" >&2
        return 1
    }

    if nvidia_available && ffmpeg_encoder_available "h264_nvenc"; then
        if run_reencode_attempt "h264_nvenc" -c:v h264_nvenc -preset p7 -cq 19; then
            return 0
        fi
    else
        echo "跳过 h264_nvenc: 未检测到可用 NVIDIA GPU 或 ffmpeg h264_nvenc 编码器" >&2
    fi

    if run_reencode_attempt "libx264" -c:v libx264 -preset fast -crf 19; then
        return 0
    fi

    echo "Error: ffmpeg re-encode failed." >&2
    return 1
}

echo "============================================="
echo "download — 步骤 1/3: 抓取视频标题"
echo "============================================="

VIDEO_TITLE=$($YTDLP --get-title "$URL")
FOLDER_NAME=$("$PYTHON_BIN" - "$VIDEO_TITLE" <<'PY'
import re
import sys
import unicodedata

title = sys.argv[1]
name = unicodedata.normalize("NFKD", title)
name = re.sub(r"[\u2018\u2019\u201A\u201B\u2032\u02BC]", "", name)
name = re.sub(r"[\u201C\u201D\u201E\u201F\u2033]", "", name)
name = re.sub(r"[\u2010-\u2015]", "-", name)
name = re.sub(r"[^\w. -]+", "_", name, flags=re.UNICODE)
name = re.sub(r"[\\/:*?\"<>|]", "_", name)
name = re.sub(r"\s+", " ", name)
name = re.sub(r"_+", "_", name)
name = name.strip(" ._") or "video"
print(name)
PY
)
mkdir -p "$FOLDER_NAME"
echo "目录: $FOLDER_NAME"
EXISTING_ORIGINAL_MKV="$FOLDER_NAME/$FOLDER_NAME.original.mkv"
HAS_EXISTING_ORIGINAL_MKV=false
if [ -f "$EXISTING_ORIGINAL_MKV" ]; then
    HAS_EXISTING_ORIGINAL_MKV=true
    echo "发现已有原片: $EXISTING_ORIGINAL_MKV"
    echo "将跳过视频下载，仅补充 metadata / thumbnail / description / tags"
fi

echo "============================================="
if [ "$HAS_EXISTING_ORIGINAL_MKV" = true ]; then
    echo "download — 步骤 2/3: 下载元数据 + 封面"
else
    echo "download — 步骤 2/3: 下载视频 + 元数据 + 封面"
fi
echo "============================================="

if [ "$HAS_EXISTING_ORIGINAL_MKV" = true ]; then
    $YTDLP -o "$FOLDER_NAME/$FOLDER_NAME.%(ext)s" \
        --cookies cookies.txt \
        --skip-download \
        --write-thumbnail \
        --convert-thumbnails png \
        --write-info-json \
        --write-description \
        --no-mtime \
        --print-to-file tags "$FOLDER_NAME/${FOLDER_NAME}.tags.txt" \
        "$URL"
else
    $YTDLP -o "$FOLDER_NAME/$FOLDER_NAME.%(ext)s" \
        --cookies cookies.txt \
        --embed-metadata \
        --embed-thumbnail \
        --write-thumbnail \
        --convert-thumbnails png \
        --write-info-json \
        --write-description \
        --no-mtime \
        --sponsorblock-remove sponsor,selfpromo \
        --print-to-file tags "$FOLDER_NAME/${FOLDER_NAME}.tags.txt" \
        "$URL"
fi

echo "============================================="
echo "download — 步骤 3/3: 定位视频文件"
echo "============================================="

EDIT_VIDEO_PATH="$FOLDER_NAME/$FOLDER_NAME.mkv"
if [ "$HAS_EXISTING_ORIGINAL_MKV" = true ]; then
    RENDER_VIDEO_PATH="$EXISTING_ORIGINAL_MKV"
    echo "使用已有原片: $RENDER_VIDEO_PATH"
else
    VIDEO_FILE=""
    for ext in mp4 mkv webm flv avi; do
        if [ -f "$FOLDER_NAME/$FOLDER_NAME.$ext" ]; then
            VIDEO_FILE="$FOLDER_NAME/$FOLDER_NAME.$ext"
            break
        fi
    done
    if [ -z "$VIDEO_FILE" ]; then
        echo "错误: 未找到视频文件 ($FOLDER_NAME/$FOLDER_NAME.<ext>)" >&2
        exit 1
    fi
    echo "定位: $VIDEO_FILE"

    ORIGINAL_VIDEO_PATH="$VIDEO_FILE"
    ORIGINAL_EXT="${ORIGINAL_VIDEO_PATH##*.}"
    RENDER_VIDEO_PATH="$FOLDER_NAME/$FOLDER_NAME.original.$ORIGINAL_EXT"

    rm -f "$RENDER_VIDEO_PATH"
    mv -f "$ORIGINAL_VIDEO_PATH" "$RENDER_VIDEO_PATH"
fi

RENDER_VIDEO_ABS="$(resolve_abs_path "$RENDER_VIDEO_PATH")"
EDIT_VIDEO_ABS="$(resolve_abs_path "$EDIT_VIDEO_PATH")"
run_edit_reencode "$RENDER_VIDEO_ABS" "$EDIT_VIDEO_ABS"

echo "============================================="
echo "download — 完成"
echo "============================================="

echo "编辑视频: $EDIT_VIDEO_ABS"
echo "渲染原片: $RENDER_VIDEO_ABS"
echo "OUTPUT_VIDEO=$EDIT_VIDEO_ABS"
echo "OUTPUT_RENDER_VIDEO=$RENDER_VIDEO_ABS"
