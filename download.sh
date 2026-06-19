#!/bin/bash
# =============================================================================
# download.sh — 下载 YouTube 视频 + 元数据
#
# 用法:
#   ./download.sh <YouTube URL>
#
# 输出:
#   OUTPUT_VIDEO=<视频绝对路径>
#
# 环境变量:
#   YTDLP_PATH_LINUX  yt-dlp 路径 (默认: yt-dlp)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source <(tr -d '\r' < "$SCRIPT_DIR/.env") && set +a
YTDLP="${YTDLP_PATH_LINUX:-yt-dlp}"

if [ -z "${1:-}" ]; then
    echo "用法: $0 <YouTube URL>" >&2
    exit 1
fi

URL="$1"

echo "============================================="
echo "download — 步骤 1/3: 抓取视频标题"
echo "============================================="

VIDEO_TITLE=$($YTDLP --get-title "$URL")
FOLDER_NAME=$(python3 - "$VIDEO_TITLE" <<'PY'
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
        VIDEO_FILE="$FOLDER_NAME.$ext"
        break
    fi
done
if [ -z "$VIDEO_FILE" ]; then
    echo "错误: 未找到视频文件 ($FOLDER_NAME/$FOLDER_NAME.<ext>)" >&2
    exit 1
fi
echo "定位: $VIDEO_FILE"

echo "============================================="
echo "download — 完成"
echo "============================================="

VIDEO_ABS_PATH="$(realpath "$FOLDER_NAME/$VIDEO_FILE" 2>/dev/null || readlink -f "$FOLDER_NAME/$VIDEO_FILE" 2>/dev/null || echo "$PWD/$FOLDER_NAME/$VIDEO_FILE")"
echo "OUTPUT_VIDEO=$VIDEO_ABS_PATH"
