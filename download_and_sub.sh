#!/bin/bash

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
# 获取视频标题（作为文件夹名）
VIDEO_TITLE=$(yt-dlp --get-title "$URL")
# 过滤掉 Linux/Windows 文件名中不合法的特殊字符
FOLDER_NAME=$(echo "$VIDEO_TITLE" | sed 's/[\\/:*?"<>|]/_/g')

# 创建文件夹
mkdir -p "$FOLDER_NAME"
echo "视频下载目录: $FOLDER_NAME"

echo "============================================="
echo "步骤 2: 使用 yt-dlp 下载视频、元数据及封面"
echo "============================================="

yt-dlp -o "$FOLDER_NAME/%(title)s.%(ext)s" \
	--cookies cookies.txt \
	--embed-metadata \
	--write-thumbnail \
	--write-info-json \
	--write-description \
	--no-mtime \
	--sponsorblock-remove sponsor,selfpromo \
	"$URL"

echo "============================================="
echo "步骤 3: 寻找下载好的视频文件"
echo "============================================="

VIDEO_FILE=$(ls "$FOLDER_NAME" | grep -E '\.(mp4|mkv|webm|flv|avi)$' | head -n 1)

if [ -z "$VIDEO_FILE" ]; then
	echo "错误：未找到下载完成的视频文件，无法生成字幕！"
	exit 1
fi

echo "成功定位视频文件: $VIDEO_FILE"

echo "============================================="
echo "步骤 4: 运行 uvx whisperx 生成 SRT 字幕"
echo "============================================="

cd "$FOLDER_NAME"
uvx whisperx "$VIDEO_FILE" \
	--lang en \
	--model large-v3 \
	--output_dir . \
	--output_format srt \
	--compute_type float16

echo "============================================="
echo "Finish! 所有文件已保存在文件夹：$FOLDER_NAME"
echo "============================================="