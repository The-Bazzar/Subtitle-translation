#!/bin/bash
# =============================================================================
# download_and_sub.sh — 下载 YouTube 视频 + WhisperX 生成英文字幕
#
# 用法:
#   ./download_and_sub.sh <YouTube URL>
#
# 流程:
#   1. yt-dlp --get-title → 视频标题 (过滤特殊字符作文件夹名)
#   2. yt-dlp 下载视频/缩略图/元数据 (SponsorBlock 去广告)
#   3. 定位视频文件 → 文件名 = 文件夹名 (可预测)
#   4. uvx whisperx large-v3 → 生成 .srt 字幕 (已存在则跳过)
#
# 输出标记:
#   OUTPUT_VIDEO=<绝对路径>  (供 pipeline.sh 解析)
#
# 依赖: yt-dlp, uvx (whisperx), ffmpeg
# =============================================================================

# 1. 检查是否输入了链接
if [ -z "$1" ]; then
	echo "错误：请提供有效的 YouTube 视频链接！"
	echo "用法: $0 <视频链接>"
	exit 1
fi

URL="$1"

echo "============================================="
echo "步骤 1: 抓取视频标题并创建独立文件夹"
echo "============================================="
# 获取视频标题（作为文件夹名 + 视频文件名）
VIDEO_TITLE=$(yt-dlp --get-title "$URL")
# 过滤掉 Linux/Windows 文件名中不合法的特殊字符
FOLDER_NAME=$(echo "$VIDEO_TITLE" | sed 's/[\\/:*?"<>|]/_/g')

# 创建文件夹
mkdir -p "$FOLDER_NAME"
echo "视频下载目录: $FOLDER_NAME"
echo "视频标题: $VIDEO_TITLE"

echo "============================================="
echo "步骤 2: 使用 yt-dlp 下载视频、元数据及封面"
echo "============================================="

# yt-dlp 文件名 = FOLDER_NAME (确保可预测)
yt-dlp -o "$FOLDER_NAME/$FOLDER_NAME.%(ext)s" \
	--cookies cookies.txt \
	--embed-metadata \
	--embed-thumbnail \
	--write-thumbnail \
	--convert-thumbnails png \
	--write-info-json \
	--write-description \
	--no-mtime \
	--sponsorblock-remove sponsor,selfpromo \
	--print-to-file tags "$FOLDER_NAME/${FOLDER_NAME}_tags.txt" \
	"$URL"

echo "============================================="
echo "步骤 3: 寻找下载好的视频文件"
echo "============================================="

# 文件名可预测: $FOLDER_NAME.<ext> (yt-dlp 的 %(ext)s 展开为 mp4/webm/mkv 等)
VIDEO_FILE=""
for ext in mp4 mkv webm flv avi; do
	if [ -f "$FOLDER_NAME/$FOLDER_NAME.$ext" ]; then
		VIDEO_FILE="$FOLDER_NAME.$ext"
		break
	fi
done

if [ -z "$VIDEO_FILE" ]; then
	echo "错误：未找到下载完成的视频文件，无法生成字幕！"
	echo "预期: $FOLDER_NAME/$FOLDER_NAME.<mp4|mkv|webm|...>"
	exit 1
fi

echo "成功定位视频文件: $VIDEO_FILE"

echo "============================================="
echo "步骤 4: 运行 uvx whisperx 生成 SRT 字幕"
echo "============================================="

# 推导预期 SRT 路径，已存在则跳过 WhisperX
SRT_NAME="${VIDEO_FILE%.*}.srt"

if [ -f "$FOLDER_NAME/$SRT_NAME" ]; then
	echo "字幕已存在, 跳过 WhisperX — $FOLDER_NAME/$SRT_NAME"
else
	# 在子 shell 中 cd, 不改变外层工作目录 (避免影响后续 realpath)
	(cd "$FOLDER_NAME" && uvx whisperx "$VIDEO_FILE" \
		--lang en \
		--model large-v3 \
		--output_dir . \
		--output_format srt \
		--compute_type float16)
fi

echo "============================================="
echo "Finish! 所有文件已保存在文件夹：$FOLDER_NAME"
echo "============================================="

# 输出视频文件绝对路径，方便下游脚本 (如 pipeline.sh) 串联
VIDEO_ABS_PATH="$(realpath "$FOLDER_NAME/$VIDEO_FILE" 2>/dev/null || readlink -f "$FOLDER_NAME/$VIDEO_FILE" 2>/dev/null || echo "$PWD/$FOLDER_NAME/$VIDEO_FILE")"
echo "OUTPUT_VIDEO=$VIDEO_ABS_PATH"
