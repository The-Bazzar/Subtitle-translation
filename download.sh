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

run_edit_reencode() {
    local input_path="$1"
    local output_path="$2"

    echo "============================================="
    echo "download — 步骤 4/4: 重编码生成编辑视频"
    echo "============================================="
    echo "原片: $input_path"
    echo "编辑: $output_path"
    echo "模式: CPU 默认解码出 yuv4mpegpipe 纯净帧流，再与原音频合并编码"

    run_frame_pipe_attempt() {
        local encode_mode="$1"
        local label="$2"
        local -a decode_cmd
        local -a encode_cmd

        rm -f "$output_path"
        echo "尝试: $label"

        decode_cmd=("$FFMPEG" -hide_banner -stats -i "$input_path" -map 0:v:0 -f yuv4mpegpipe -)

        encode_cmd=(
            "$FFMPEG"
            -hide_banner
            -stats
            -y
            -fflags +genpts
            -i pipe:0
            -i "$input_path"
            -filter_complex "[1:a]aresample=async=1:first_pts=0[aout]"
            -map 0:v:0
            -map "[aout]"
            -pix_fmt yuv420p
        )
        if [ "$encode_mode" = "nvenc" ]; then
            encode_cmd+=(-c:v hevc_nvenc -preset p5 -rc vbr -cq 19 -b:v 0)
        else
            encode_cmd+=(-c:v libx264 -preset fast -crf 23)
        fi
        encode_cmd+=(-c:a aac -b:a 192k -movflags +faststart -avoid_negative_ts make_zero "$output_path")

        if "${decode_cmd[@]}" | "${encode_cmd[@]}"; then
            return 0
        fi

        rm -f "$output_path"
        return 1
    }

    if run_frame_pipe_attempt "nvenc" "NVENC encode + original audio"; then
        return 0
    fi
    if run_frame_pipe_attempt "x264" "libx264 encode + original audio"; then
        return 0
    fi

    echo "Error: ffmpeg frame-pipe re-encode failed." >&2
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

echo "============================================="
echo "download — 步骤 2/3: 下载视频 + 元数据 + 封面"
echo "============================================="

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

echo "============================================="
echo "download — 步骤 3/3: 定位视频文件"
echo "============================================="

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
EDIT_VIDEO_PATH="$FOLDER_NAME/$FOLDER_NAME.mp4"

rm -f "$RENDER_VIDEO_PATH"
mv -f "$ORIGINAL_VIDEO_PATH" "$RENDER_VIDEO_PATH"

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
