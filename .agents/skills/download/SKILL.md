---
name: download
description: 下载 YouTube 视频 + 元数据（不含字幕生成）
platform: Win + Linux
---

# 视频下载 (Win + Linux)

使用 yt-dlp 下载 YouTube 视频及相关元数据。**不包含**语音识别。

## Win — PowerShell

```powershell
.\download.ps1 "https://www.youtube.com/watch?v=xxxxx"
```

输出 `OUTPUT_VIDEO=<路径>`，供 `whisper.ps1` / `pipeline.ps1` 串联。

## Linux — Bash

```bash
./download.sh "https://www.youtube.com/watch?v=xxxxx"
```

输出 `OUTPUT_VIDEO=<路径>`，供 `whisper.sh` / `pipeline.sh` 串联。

## 输出

```
视频目录/
├── 视频标题.webm           # 视频 (内嵌封面)
├── 视频标题.png            # 封面缩略图
├── 视频标题.info.json      # 元数据
├── 视频标题.description    # 简介
└── 视频标题.tags.txt       # 标签
```

## 注意事项

- 需要 `cookies.txt` (YouTube 凭证, gitignored)
- yt-dlp 从 `.env` 的 `YTDLP_PATH_WIN` / `YTDLP_PATH_LINUX` 读取
